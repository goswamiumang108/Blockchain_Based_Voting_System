"""SQLite access layer: schema, constraints, triggers, transactions, block persistence.

Rules are enforced in the database as a backstop to the service layer:
  * PK(election_id, user_id) on voter_participation  -> one vote per voter per election
  * FK(election_id, user_id) -> eligible_voters      -> only eligible voters can vote
  * UNIQUE(election_id, voter_tag) on pending ballots
  * triggers: voting only while election is 'open'; candidates/roll frozen after draft;
    election status can only move draft -> open -> closed; blocks and participation
    records are append-only.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .blockchain import Block, canonical_json

SCHEMA_VERSION = 2

SCHEMA = """
         CREATE TABLE IF NOT EXISTS users
         (
             id
             INTEGER
             PRIMARY
             KEY
             AUTOINCREMENT,
             username
             TEXT
             NOT
             NULL
             UNIQUE
             COLLATE
             NOCASE,
             full_name
             TEXT
             NOT
             NULL,
             password_hash
             TEXT
             NOT
             NULL,
             role
             TEXT
             NOT
             NULL
             DEFAULT
             'voter'
             CHECK (
             role
             IN
         (
             'admin',
             'voter'
         )),
             failed_attempts INTEGER NOT NULL DEFAULT 0,
             locked_until TEXT,
             created_at TEXT NOT NULL
             );

         CREATE TABLE IF NOT EXISTS elections
         (
             id
             INTEGER
             PRIMARY
             KEY
             AUTOINCREMENT,
             title
             TEXT
             NOT
             NULL
             CHECK (
             length (
             trim
         (
             title
         )) > 0),
             description TEXT NOT NULL DEFAULT '',
             status TEXT NOT NULL DEFAULT 'draft' CHECK
         (
             status
             IN
         (
             'draft',
             'open',
             'closed'
         )),
             created_by INTEGER NOT NULL REFERENCES users
         (
             id
         ),
             created_at TEXT NOT NULL,
             opened_at TEXT,
             closed_at TEXT
             );

         CREATE TABLE IF NOT EXISTS candidates
         (
             id
             INTEGER
             PRIMARY
             KEY
             AUTOINCREMENT,
             election_id
             INTEGER
             NOT
             NULL
             REFERENCES
             elections
         (
             id
         ),
             name TEXT NOT NULL CHECK
         (
             length (
             trim
         (
             name
         )) > 0),
             description TEXT NOT NULL DEFAULT '',
             UNIQUE
         (
             election_id,
             name
             COLLATE
             NOCASE
         ),
             UNIQUE
         (
             election_id,
             id
         )
             );

-- The voter roll.
         CREATE TABLE IF NOT EXISTS eligible_voters
         (
             election_id
             INTEGER
             NOT
             NULL
             REFERENCES
             elections
         (
             id
         ),
             user_id INTEGER NOT NULL REFERENCES users
         (
             id
         ),
             PRIMARY KEY
         (
             election_id,
             user_id
         )
             );

         -- Records THAT a user voted, never WHAT they voted for (no candidate, no ballot id,
-- no timestamp that could be correlated with a ballot).
         CREATE TABLE IF NOT EXISTS voter_participation
         (
             election_id
             INTEGER
             NOT
             NULL,
             user_id
             INTEGER
             NOT
             NULL,
             PRIMARY
             KEY
         (
             election_id,
             user_id
         ),
             FOREIGN KEY
         (
             election_id,
             user_id
         ) REFERENCES eligible_voters
         (
             election_id,
             user_id
         )
             );

-- Anonymous ballots waiting to be mined. voter_tag = HMAC(server secret, election:user).
         CREATE TABLE IF NOT EXISTS pending_ballots
         (
             id
             INTEGER
             PRIMARY
             KEY
             AUTOINCREMENT,
             election_id
             INTEGER
             NOT
             NULL,
             candidate_id
             INTEGER
             NOT
             NULL,
             ballot_id
             TEXT
             NOT
             NULL
             UNIQUE,
             voter_tag
             TEXT
             NOT
             NULL,
             UNIQUE
         (
             election_id,
             voter_tag
         ),
             FOREIGN KEY
         (
             election_id,
             candidate_id
         ) REFERENCES candidates
         (
             election_id,
             id
         )
             );

         CREATE TABLE IF NOT EXISTS blocks
         (
             block_index
             INTEGER
             PRIMARY
             KEY,
             timestamp
             TEXT
             NOT
             NULL,
             transactions
             TEXT
             NOT
             NULL,
             previous_hash
             TEXT
             NOT
             NULL,
             nonce
             INTEGER
             NOT
             NULL,
             difficulty
             INTEGER
             NOT
             NULL,
             hash
             TEXT
             NOT
             NULL
             UNIQUE
         );

         CREATE TRIGGER IF NOT EXISTS trg_participation_requires_open
BEFORE INSERT ON voter_participation
WHEN (SELECT status FROM elections WHERE id = NEW.election_id) IS NOT 'open'
         BEGIN
         SELECT RAISE(ABORT, 'election is not open');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_pending_requires_open
BEFORE INSERT ON pending_ballots
WHEN (SELECT status FROM elections WHERE id = NEW.election_id) IS NOT 'open'
         BEGIN
         SELECT RAISE(ABORT, 'election is not open');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_participation_no_update
BEFORE
         UPDATE ON voter_participation
         BEGIN
         SELECT RAISE(ABORT, 'participation records are immutable');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_participation_no_delete
BEFORE
         DELETE
         ON voter_participation
         BEGIN
         SELECT RAISE(ABORT, 'participation records are immutable');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_blocks_no_update
BEFORE
         UPDATE ON blocks
         BEGIN
         SELECT RAISE(ABORT, 'blocks are append-only');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_blocks_no_delete
BEFORE
         DELETE
         ON blocks
         BEGIN
         SELECT RAISE(ABORT, 'blocks are append-only');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_candidates_insert_draft_only
BEFORE INSERT ON candidates
WHEN (SELECT status FROM elections WHERE id = NEW.election_id) IS NOT 'draft'
         BEGIN
         SELECT RAISE(ABORT, 'candidates can only be added while the election is a draft');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_candidates_delete_draft_only
BEFORE
         DELETE
         ON candidates
WHEN (SELECT status FROM elections WHERE id = OLD.election_id) IS NOT 'draft'
         BEGIN
         SELECT RAISE(ABORT, 'candidates can only be removed while the election is a draft');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_roll_delete_draft_only
BEFORE
         DELETE
         ON eligible_voters
WHEN (SELECT status FROM elections WHERE id = OLD.election_id) IS NOT 'draft'
         BEGIN
         SELECT RAISE(ABORT, 'voters can only be removed while the election is a draft');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_roll_insert_not_closed
BEFORE INSERT ON eligible_voters
WHEN (SELECT status FROM elections WHERE id = NEW.election_id) IS 'closed'
         BEGIN
         SELECT RAISE(ABORT, 'election is closed');
         END;

         CREATE TRIGGER IF NOT EXISTS trg_election_status_forward_only
BEFORE
         UPDATE OF status
         ON elections
             WHEN NOT (
             (OLD.status = 'draft' AND NEW.status IN ('draft','open')) OR
             (OLD.status = 'open' AND NEW.status IN ('open','closed')) OR
             (OLD.status = 'closed' AND NEW.status = 'closed')
             )
         BEGIN
         SELECT RAISE(ABORT, 'invalid election status transition');
         END; \
         """


# Additions to the original schema. Applied on every start, so existing databases are upgraded in place.
EXTENSIONS_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_nota_per_election
	ON candidates (election_id) WHERE is_nota = 1;

CREATE TABLE IF NOT EXISTS audit_log (
	id             INTEGER PRIMARY KEY AUTOINCREMENT,
	timestamp      TEXT NOT NULL,
	actor_id       INTEGER,
	actor_username TEXT,
	action         TEXT NOT NULL,
	election_id    INTEGER,
	details        TEXT NOT NULL DEFAULT '',
	prev_hash      TEXT NOT NULL,
	entry_hash     TEXT NOT NULL UNIQUE
);

CREATE TRIGGER IF NOT EXISTS trg_audit_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log entries are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log entries are immutable'); END;
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
	"""Add a column to an existing table if it is missing (idempotent, safe if two processes race)."""
	if column in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
		return
	try:
		conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
	except sqlite3.OperationalError as exc:
		if "duplicate column" not in str(exc).lower():
			raise


class Database:
	"""Thin wrapper that hands out short-lived connections (safe across Streamlit threads)."""
	
	def __init__(self, path: str | Path):
		self.path = Path(path)
		self.path.parent.mkdir(parents=True, exist_ok=True)
	
	def connect(self) -> sqlite3.Connection:
		conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)  # autocommit; we issue BEGIN
		conn.row_factory = sqlite3.Row
		conn.execute("PRAGMA foreign_keys = ON")
		conn.execute("PRAGMA busy_timeout = 15000")
		conn.execute("PRAGMA journal_mode = WAL")
		return conn
	
	@contextmanager
	def connection(self) -> Iterator[sqlite3.Connection]:
		"""Read-only style access (no explicit transaction)."""
		conn = self.connect()
		try:
			yield conn
		finally:
			conn.close()
	
	@contextmanager
	def transaction(self) -> Iterator[sqlite3.Connection]:
		"""Serialised write transaction: BEGIN IMMEDIATE takes the write lock up-front, so
		'check then insert' sequences cannot interleave with another writer."""
		conn = self.connect()
		try:
			conn.execute("BEGIN IMMEDIATE")
			try:
				yield conn
			except BaseException:
				try:
					conn.execute("ROLLBACK")
				except sqlite3.Error:
					pass
				raise
			else:
				conn.execute("COMMIT")
		finally:
			conn.close()
	
	def init_schema(self) -> None:
		with self.connection() as conn:
			conn.executescript(SCHEMA)
			_ensure_column(conn, "elections", "allow_nota", "INTEGER NOT NULL DEFAULT 0 CHECK (allow_nota IN (0, 1))")
			_ensure_column(conn, "candidates", "is_nota", "INTEGER NOT NULL DEFAULT 0 CHECK (is_nota IN (0, 1))")
			conn.executescript(EXTENSIONS_SQL)
			conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


# ---- block persistence -------------------------------------------------------------------

def insert_block(conn: sqlite3.Connection, block: Block) -> None:
	conn.execute(
		"INSERT INTO blocks (block_index, timestamp, transactions, previous_hash, nonce, difficulty, hash) "
		"VALUES (?, ?, ?, ?, ?, ?, ?)",
		(block.index, block.timestamp, canonical_json(block.transactions),
		 block.previous_hash, block.nonce, block.difficulty, block.hash),
	)


def row_to_block(row: sqlite3.Row) -> Block:
	raw = row["transactions"]
	try:
		transactions = json.loads(raw)
	except (TypeError, json.JSONDecodeError):
		transactions = [{"_unparseable": str(raw)}]  # will fail hash verification
	return Block(
		index=row["block_index"], timestamp=row["timestamp"], transactions=transactions,
		previous_hash=row["previous_hash"], nonce=row["nonce"], difficulty=row["difficulty"], hash=row["hash"],
	)


def load_blocks(conn: sqlite3.Connection) -> list[Block]:
	return [row_to_block(r) for r in conn.execute("SELECT * FROM blocks ORDER BY block_index")]


def latest_block(conn: sqlite3.Connection) -> Optional[Block]:
	row = conn.execute("SELECT * FROM blocks ORDER BY block_index DESC LIMIT 1").fetchone()
	return row_to_block(row) if row else None
