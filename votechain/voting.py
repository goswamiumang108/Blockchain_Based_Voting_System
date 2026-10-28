"""Election and voting logic: lifecycle, the vote transaction, mining, validation, tallying.

All rules are enforced here (and backed by SQLite constraints). Nothing in the UI or in
Streamlit session state is trusted to enforce them.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from .blockchain import (
	Block, Blockchain, ChainVerification, mine_block, verify_chain,
)
from .blockchain import create_genesis_block
from .database import Database, insert_block, load_blocks
from .errors import (
	AlreadyVotedError, ChainIntegrityError, ElectionStateError, InvalidCandidateError,
	NotEligibleError, PermissionDeniedError, ValidationError, VoteChainError,
)
from .models import (
	AcceptedVote, Candidate, Election, ElectionResults, RejectedVote, TransactionAudit, User, VoteReceipt,
)

log = logging.getLogger(__name__)

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
VOTE_TX_KEYS = frozenset({"type", "election_id", "candidate_id", "ballot_id", "voter_tag"})


def _now() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _election(row: sqlite3.Row) -> Election:
	return Election(row["id"], row["title"], row["description"], row["status"],
	                row["created_at"], row["opened_at"], row["closed_at"])


def _candidate(row: sqlite3.Row) -> Candidate:
	return Candidate(row["id"], row["election_id"], row["name"], row["description"])


class VotingService:
	def __init__(
			self,
			db: Database,
			tag_secret: bytes,
			difficulty: int = 3,
			auto_mine: bool = True,
			max_txs_per_block: int = 50,
	):
		if not tag_secret:
			raise ValueError("tag_secret must not be empty")
		self.db = db
		self._secret = tag_secret
		self.difficulty = difficulty
		self.auto_mine = auto_mine
		self.max_txs = max_txs_per_block
		self.initialize()
	
	def initialize(self) -> None:
		"""Create the schema and the genesis block if missing (idempotent, restart-safe)."""
		self.db.init_schema()
		with self.db.transaction() as conn:
			if conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0:
				insert_block(conn, create_genesis_block())
	
	# ---------------------------------------------------------------- identity separation
	def voter_tag(self, election_id: int, user_id: int) -> str:
		"""Anonymous per-election voter tag. Not reversible without the server secret."""
		msg = f"{int(election_id)}:{int(user_id)}".encode("ascii")
		return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
	
	# ---------------------------------------------------------------- helpers
	@staticmethod
	def _require_admin(conn: sqlite3.Connection, actor_id: int) -> None:
		row = conn.execute("SELECT role FROM users WHERE id = ?", (actor_id,)).fetchone()
		if row is None or row["role"] != "admin":
			raise PermissionDeniedError("Administrator privileges are required.")
	
	@staticmethod
	def _election_row(conn: sqlite3.Connection, election_id: int) -> sqlite3.Row:
		row = conn.execute("SELECT * FROM elections WHERE id = ?", (election_id,)).fetchone()
		if row is None:
			raise ValidationError("Election not found.")
		return row
	
	# ---------------------------------------------------------------- elections (admin)
	def create_election(self, actor_id: int, title: str, description: str = "") -> Election:
		title, description = (title or "").strip(), (description or "").strip()
		if not 3 <= len(title) <= 120:
			raise ValidationError("Election title must be 3-120 characters.")
		with self.db.transaction() as conn:
			self._require_admin(conn, actor_id)
			cur = conn.execute(
				"INSERT INTO elections (title, description, created_by, created_at) VALUES (?,?,?,?)",
				(title, description, actor_id, _now()),
			)
			return _election(self._election_row(conn, cur.lastrowid))
	
	def add_candidate(self, actor_id: int, election_id: int, name: str, description: str = "") -> Candidate:
		name, description = (name or "").strip(), (description or "").strip()
		if not 1 <= len(name) <= 100:
			raise ValidationError("Candidate name is required (max 100 characters).")
		try:
			with self.db.transaction() as conn:
				self._require_admin(conn, actor_id)
				election = self._election_row(conn, election_id)
				if election["status"] != "draft":
					raise ElectionStateError("Candidates can only be changed while the election is a draft.")
				cur = conn.execute(
					"INSERT INTO candidates (election_id, name, description) VALUES (?,?,?)",
					(election_id, name, description),
				)
				return Candidate(cur.lastrowid, election_id, name, description)
		except sqlite3.IntegrityError:
			raise ValidationError("A candidate with that name already exists in this election.") from None
	
	def remove_candidate(self, actor_id: int, candidate_id: int) -> None:
		try:
			with self.db.transaction() as conn:
				self._require_admin(conn, actor_id)
				row = conn.execute("SELECT election_id FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
				if row is None:
					raise ValidationError("Candidate not found.")
				if self._election_row(conn, row["election_id"])["status"] != "draft":
					raise ElectionStateError("Candidates can only be changed while the election is a draft.")
				conn.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
		except sqlite3.IntegrityError as exc:
			raise ElectionStateError(f"Candidate cannot be removed: {exc}") from None
	
	def enroll_voters(self, actor_id: int, election_id: int, user_ids: Iterable[int]) -> int:
		"""Add users to the voter roll (draft or open elections). Returns the number newly added."""
		added = 0
		with self.db.transaction() as conn:
			self._require_admin(conn, actor_id)
			if self._election_row(conn, election_id)["status"] == "closed":
				raise ElectionStateError("The election is closed.")
			for uid in set(user_ids):
				if conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone() is None:
					raise ValidationError(f"Unknown user id {uid}.")
				cur = conn.execute(
					"INSERT OR IGNORE INTO eligible_voters (election_id, user_id) VALUES (?,?)", (election_id, uid))
				added += cur.rowcount
		return added
	
	def enroll_all_voters(self, actor_id: int, election_id: int) -> int:
		with self.db.connection() as conn:
			ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE role = 'voter'")]
		return self.enroll_voters(actor_id, election_id, ids)
	
	def unenroll_voter(self, actor_id: int, election_id: int, user_id: int) -> None:
		try:
			with self.db.transaction() as conn:
				self._require_admin(conn, actor_id)
				if self._election_row(conn, election_id)["status"] != "draft":
					raise ElectionStateError("The voter roll is frozen once the election is open.")
				conn.execute("DELETE FROM eligible_voters WHERE election_id = ? AND user_id = ?",
				             (election_id, user_id))
		except sqlite3.IntegrityError as exc:
			raise ElectionStateError(f"Voter cannot be removed: {exc}") from None
	
	def open_election(self, actor_id: int, election_id: int) -> Election:
		with self.db.transaction() as conn:
			self._require_admin(conn, actor_id)
			election = self._election_row(conn, election_id)
			if election["status"] != "draft":
				raise ElectionStateError("Only a draft election can be opened.")
			n_cand = conn.execute("SELECT COUNT(*) FROM candidates WHERE election_id = ?", (election_id,)).fetchone()[0]
			n_elig = \
				conn.execute("SELECT COUNT(*) FROM eligible_voters WHERE election_id = ?", (election_id,)).fetchone()[0]
			if n_cand < 2:
				raise ElectionStateError("At least two candidates are required to open an election.")
			if n_elig < 1:
				raise ElectionStateError("Enroll at least one eligible voter before opening the election.")
			conn.execute("UPDATE elections SET status = 'open', opened_at = ? WHERE id = ?", (_now(), election_id))
			return _election(self._election_row(conn, election_id))
	
	def close_election(self, actor_id: int, election_id: int) -> Election:
		"""Closes voting, then mines every remaining pending ballot so the result is final."""
		with self.db.transaction() as conn:
			self._require_admin(conn, actor_id)
			if self._election_row(conn, election_id)["status"] != "open":
				raise ElectionStateError("Only an open election can be closed.")
			conn.execute("UPDATE elections SET status = 'closed', closed_at = ? WHERE id = ?", (_now(), election_id))
		self.mine_pending()
		return self.get_election(election_id)
	
	# ---------------------------------------------------------------- queries
	def list_elections(self) -> list[Election]:
		with self.db.connection() as conn:
			return [_election(r) for r in conn.execute("SELECT * FROM elections ORDER BY id DESC")]
	
	def get_election(self, election_id: int) -> Election:
		with self.db.connection() as conn:
			return _election(self._election_row(conn, election_id))
	
	def list_candidates(self, election_id: int) -> list[Candidate]:
		with self.db.connection() as conn:
			return [_candidate(r) for r in conn.execute(
				"SELECT * FROM candidates WHERE election_id = ? ORDER BY name COLLATE NOCASE", (election_id,))]
	
	def list_eligible(self, election_id: int) -> list[User]:
		with self.db.connection() as conn:
			rows = conn.execute(
				"SELECT u.* FROM users u JOIN eligible_voters e ON e.user_id = u.id "
				"WHERE e.election_id = ? ORDER BY u.username COLLATE NOCASE", (election_id,)).fetchall()
		return [User(r["id"], r["username"], r["full_name"], r["role"]) for r in rows]
	
	def is_eligible(self, election_id: int, user_id: int) -> bool:
		with self.db.connection() as conn:
			return conn.execute("SELECT 1 FROM eligible_voters WHERE election_id = ? AND user_id = ?",
			                    (election_id, user_id)).fetchone() is not None
	
	def has_voted(self, election_id: int, user_id: int) -> bool:
		with self.db.connection() as conn:
			return conn.execute("SELECT 1 FROM voter_participation WHERE election_id = ? AND user_id = ?",
			                    (election_id, user_id)).fetchone() is not None
	
	def pending_count(self, election_id: Optional[int] = None) -> int:
		with self.db.connection() as conn:
			if election_id is None:
				return conn.execute("SELECT COUNT(*) FROM pending_ballots").fetchone()[0]
			return conn.execute("SELECT COUNT(*) FROM pending_ballots WHERE election_id = ?",
			                    (election_id,)).fetchone()[0]
	
	def list_pending(self) -> list[dict]:
		with self.db.connection() as conn:
			return [dict(r) for r in conn.execute(
				"SELECT election_id, candidate_id, ballot_id, voter_tag FROM pending_ballots ORDER BY id")]
	
	# ---------------------------------------------------------------- casting a vote
	def cast_vote(self, election_id: int, user_id: int, candidate_id: int) -> VoteReceipt:
		"""Cast one ballot. Server-side, atomic, and idempotent against double submission.

		Inside ONE write transaction: validate state/eligibility/candidate, insert the
		participation record (PK makes a 2nd vote impossible) and the anonymous pending ballot.
		Either both rows exist or neither does.
		"""
		ballot_id = secrets.token_hex(16)
		tag = self.voter_tag(election_id, user_id)
		try:
			with self.db.transaction() as conn:
				if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
					raise NotEligibleError("Unknown voter account.")
				election = self._election_row(conn, election_id)
				if election["status"] != "open":
					raise ElectionStateError("This election is not open for voting.")
				if conn.execute("SELECT 1 FROM eligible_voters WHERE election_id = ? AND user_id = ?",
				                (election_id, user_id)).fetchone() is None:
					raise NotEligibleError("You are not on the voter roll for this election.")
				if conn.execute("SELECT 1 FROM voter_participation WHERE election_id = ? AND user_id = ?",
				                (election_id, user_id)).fetchone() is not None:
					raise AlreadyVotedError("You have already voted in this election.")
				if conn.execute("SELECT 1 FROM candidates WHERE id = ? AND election_id = ?",
				                (candidate_id, election_id)).fetchone() is None:
					raise InvalidCandidateError("That candidate is not part of this election.")
				conn.execute("INSERT INTO voter_participation (election_id, user_id) VALUES (?,?)",
				             (election_id, user_id))
				conn.execute(
					"INSERT INTO pending_ballots (election_id, candidate_id, ballot_id, voter_tag) VALUES (?,?,?,?)",
					(election_id, candidate_id, ballot_id, tag))
		except sqlite3.IntegrityError as exc:
			# Database constraints are the last line of defence (e.g. a racing duplicate).
			if self.has_voted(election_id, user_id):
				raise AlreadyVotedError("You have already voted in this election.") from None
			raise VoteChainError(f"Vote rejected by database constraints: {exc}") from None
		
		block_index: Optional[int] = None
		if self.auto_mine:
			try:
				for block in self.mine_pending():
					if any(t.get("ballot_id") == ballot_id for t in block.transactions):
						block_index = block.index
			except (VoteChainError, sqlite3.Error) as exc:
				# The vote is already durably recorded as pending; mining can be retried later.
				log.warning("Auto-mining failed, ballot stays pending: %s", exc)
		return VoteReceipt(election_id, ballot_id, block_index)
	
	# ---------------------------------------------------------------- mining
	def mine_pending(self) -> list[Block]:
		"""Mine all pending ballots into one or more blocks. Safe to call concurrently."""
		mined: list[Block] = []
		while True:
			block = self._mine_one_block()
			if block is None:
				return mined
			mined.append(block)
	
	def _mine_one_block(self) -> Optional[Block]:
		with self.db.transaction() as conn:  # write lock held: nobody else can extend the chain meanwhile
			rows = conn.execute("SELECT * FROM pending_ballots ORDER BY id LIMIT ?", (self.max_txs,)).fetchall()
			if not rows:
				return None
			blocks = load_blocks(conn)
			check = verify_chain(blocks)
			if not check.valid:
				raise ChainIntegrityError(
					"Refusing to extend a blockchain that fails verification: " + "; ".join(check.errors[:3]))
			tip = blocks[-1]
			txs = [
				{"type": "vote", "election_id": r["election_id"], "candidate_id": r["candidate_id"],
				 "ballot_id": r["ballot_id"], "voter_tag": r["voter_tag"]}
				for r in rows
			]
			txs.sort(key=lambda t: t["ballot_id"])  # random order: block order must not reveal cast order
			block = mine_block(tip.index + 1, tip.hash, txs, self.difficulty)
			insert_block(conn, block)
			conn.execute(f"DELETE FROM pending_ballots WHERE id IN ({','.join('?' * len(rows))})",
			             [r["id"] for r in rows])
			return block
	
	# ---------------------------------------------------------------- chain access
	def get_blocks(self) -> list[Block]:
		with self.db.connection() as conn:
			return load_blocks(conn)
	
	def get_chain(self) -> Blockchain:
		return Blockchain(self.difficulty, self.get_blocks())
	
	def verify_chain(self) -> ChainVerification:
		return verify_chain(self.get_blocks())
	
	# ---------------------------------------------------------------- validation + results
	def _validation_context(self) -> tuple[dict, dict]:
		"""Per-election candidate-id sets and eligible-voter-tag sets, from the database."""
		candidates: dict[int, set[int]] = {}
		tags: dict[int, set[str]] = {}
		with self.db.connection() as conn:
			for r in conn.execute("SELECT id, election_id FROM candidates"):
				candidates.setdefault(r["election_id"], set()).add(r["id"])
			for r in conn.execute("SELECT id FROM elections"):
				candidates.setdefault(r["id"], set())
				tags.setdefault(r["id"], set())
			for r in conn.execute("SELECT election_id, user_id FROM eligible_voters"):
				tags[r["election_id"]].add(self.voter_tag(r["election_id"], r["user_id"]))
		return candidates, tags
	
	def audit_transactions(self) -> TransactionAudit:
		"""Verify the chain, then validate every transaction in the *trusted* blocks.

		Accepted = valid shape, known election, candidate belongs to it, voter tag is on that
		election's roll, first use of that tag, first use of that ballot id.
		"""
		blocks = self.get_blocks()
		check = verify_chain(blocks)
		audit = TransactionAudit(chain_valid=check.valid, chain_errors=list(check.errors),
		                         trusted_blocks=check.trusted_length, total_blocks=len(blocks))
		candidates, tags = self._validation_context()
		seen_tags: set[tuple[int, str]] = set()
		seen_ballots: set[str] = set()
		
		for block in blocks[: check.trusted_length]:
			for tx in block.transactions:
				ballot_id = tx.get("ballot_id", "?") if isinstance(tx, dict) else "?"
				eid = tx.get("election_id") if isinstance(tx, dict) else None
				
				def reject(reason: str) -> None:
					audit.rejected.append(RejectedVote(block.index, str(ballot_id), reason,
					                                   eid if isinstance(eid, int) else None))
				
				if not isinstance(tx, dict) or tx.get("type") != "vote" or set(tx) != VOTE_TX_KEYS:
					reject("malformed or unsupported transaction");
					continue
				cid, tag = tx["candidate_id"], tx["voter_tag"]
				if not (isinstance(eid, int) and isinstance(cid, int) and not isinstance(eid, bool)
				        and not isinstance(cid, bool)
				        and isinstance(ballot_id, str) and HEX32.match(ballot_id)
				        and isinstance(tag, str) and HEX64.match(tag)):
					reject("malformed transaction fields");
					continue
				if eid not in candidates:
					reject("unknown election");
					continue
				if cid not in candidates[eid]:
					reject("candidate does not belong to the election");
					continue
				if tag not in tags[eid]:
					reject("voter tag is not on the election's voter roll");
					continue
				if (eid, tag) in seen_tags:
					reject("duplicate vote by the same voter");
					continue
				if ballot_id in seen_ballots:
					reject("duplicate ballot id");
					continue
				seen_tags.add((eid, tag))
				seen_ballots.add(ballot_id)
				audit.accepted.append(AcceptedVote(eid, cid, ballot_id, block.index))
		return audit
	
	def compute_results(self, election_id: int, audit: Optional[TransactionAudit] = None) -> ElectionResults:
		"""Results are computed ONLY from validated, accepted on-chain votes (never from pending)."""
		audit = audit or self.audit_transactions()
		election = self.get_election(election_id)
		candidates = self.list_candidates(election_id)
		counts = {c.id: 0 for c in candidates}
		accepted = [v for v in audit.accepted if v.election_id == election_id]
		for v in accepted:
			counts[v.candidate_id] += 1
		with self.db.connection() as conn:
			eligible = conn.execute("SELECT COUNT(*) FROM eligible_voters WHERE election_id = ?",
			                        (election_id,)).fetchone()[0]
			participants = conn.execute("SELECT COUNT(*) FROM voter_participation WHERE election_id = ?",
			                            (election_id,)).fetchone()[0]
		return ElectionResults(
			election=election, candidates=candidates, counts=counts, accepted_total=len(accepted),
			rejected=[r for r in audit.rejected if r.election_id in (election_id, None)],
			pending_ballots=self.pending_count(election_id), eligible_voters=eligible,
			participants=participants, chain_valid=audit.chain_valid, chain_errors=audit.chain_errors,
		)
