"""Tamper-evident admin audit log.

Every administrative action (and every admin sign-in) is appended to the ``audit_log`` table
inside the SAME database transaction as the action itself, so an action and its log entry either
both happen or neither does. Entries are hash-linked (each one stores the hash of the previous
entry, like the blockchain itself) and SQLite triggers forbid UPDATE and DELETE, so edits or
deletions are detected by :func:`verify`.

Votes are never logged here: the log records who administered the election, not who voted.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Optional

from .blockchain import canonical_json, utc_now_iso

GENESIS_HASH = "0" * 64
MAX_DETAILS = 500

ACTIONS = (
	"admin_account_created", "admin_login", "election_created", "nota_enabled", "nota_disabled",
	"candidate_added", "candidate_removed", "voters_enrolled", "voter_removed",
	"election_opened", "election_closed", "ballots_mined",
)


@dataclass(frozen=True)
class AuditEntry:
	id: int
	timestamp: str
	actor_id: Optional[int]
	actor_username: Optional[str]
	action: str
	election_id: Optional[int]
	details: str


def _entry_hash(prev_hash: str, timestamp: str, actor_id, actor_username, action: str, election_id, details: str) -> str:
	payload = canonical_json([prev_hash, timestamp, actor_id, actor_username, action, election_id, details])
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write(conn: sqlite3.Connection, actor_id: Optional[int], action: str,
          election_id: Optional[int] = None, details: str = "") -> None:
	"""Append one entry. Must be called inside the caller's write transaction (BEGIN IMMEDIATE),
	which serialises writers and keeps the hash chain consistent."""
	username = None
	if actor_id is not None:
		row = conn.execute("SELECT username FROM users WHERE id = ?", (actor_id,)).fetchone()
		username = row[0] if row else None
	last = conn.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
	prev_hash = last[0] if last else GENESIS_HASH
	timestamp = utc_now_iso()
	details = (details or "")[:MAX_DETAILS]
	entry_hash = _entry_hash(prev_hash, timestamp, actor_id, username, action, election_id, details)
	conn.execute(
		"INSERT INTO audit_log (timestamp, actor_id, actor_username, action, election_id, details, "
		"prev_hash, entry_hash) VALUES (?,?,?,?,?,?,?,?)",
		(timestamp, actor_id, username, action, election_id, details, prev_hash, entry_hash),
	)


def list_entries(conn: sqlite3.Connection, limit: int = 200, action: Optional[str] = None,
                 election_id: Optional[int] = None) -> list[AuditEntry]:
	"""Newest first."""
	query, params, where = "SELECT * FROM audit_log", [], []
	if action:
		where.append("action = ?"); params.append(action)
	if election_id is not None:
		where.append("election_id = ?"); params.append(election_id)
	if where:
		query += " WHERE " + " AND ".join(where)
	query += " ORDER BY id DESC LIMIT ?"
	params.append(max(1, int(limit)))
	return [AuditEntry(r["id"], r["timestamp"], r["actor_id"], r["actor_username"], r["action"],
	                   r["election_id"], r["details"]) for r in conn.execute(query, params)]


def verify(conn: sqlite3.Connection) -> tuple[bool, list[str]]:
	"""Recompute the whole hash chain. Returns (ok, errors)."""
	errors: list[str] = []
	prev = GENESIS_HASH
	for r in conn.execute("SELECT * FROM audit_log ORDER BY id"):
		if r["prev_hash"] != prev:
			errors.append(f"Entry {r['id']}: previous-hash link is broken (an entry before it was removed or changed).")
		expected = _entry_hash(r["prev_hash"], r["timestamp"], r["actor_id"], r["actor_username"],
		                       r["action"], r["election_id"], r["details"])
		if expected != r["entry_hash"]:
			errors.append(f"Entry {r['id']}: contents do not match the stored hash (entry was altered).")
		prev = r["entry_hash"]
	return (not errors), errors
