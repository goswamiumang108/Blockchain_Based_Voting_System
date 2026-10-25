"""A minimal blockchain implemented from scratch.

This module is pure: it knows nothing about SQLite, users, elections or Streamlit.

Block header fields: index, timestamp, transactions, previous_hash, nonce
(+ ``difficulty``, the PoW target the block was mined with, so the difficulty can be
reconfigured later without invalidating older blocks).

Block hash = SHA-256 over the canonical JSON (sorted keys, no whitespace) of the header
fields. Proof of Work: the hex digest must start with ``difficulty`` zero characters.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

GENESIS_TIMESTAMP = "2024-01-01T00:00:00+00:00"
GENESIS_PREVIOUS_HASH = "0" * 64
MIN_ACCEPTED_DIFFICULTY = 1  # non-genesis blocks mined below this are rejected by verify_chain
MAX_DIFFICULTY = 8


def canonical_json(obj: Any) -> str:
	return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def utc_now_iso() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _payload(index: int, timestamp: str, tx_json: str, previous_hash: str, nonce: int, difficulty: int) -> str:
	"""Canonical JSON of the header, assembled by hand so the (large, constant) transaction
	JSON is serialised once during mining. Key order is alphabetical, i.e. identical to
	``canonical_json`` of the equivalent dict."""
	return (
		f'{{"difficulty":{int(difficulty)},"index":{int(index)},"nonce":{int(nonce)},'
		f'"previous_hash":{json.dumps(previous_hash)},"timestamp":{json.dumps(timestamp)},'
		f'"transactions":{tx_json}}}'
	)


def compute_block_hash(
		index: int, timestamp: str, transactions: list, previous_hash: str, nonce: int, difficulty: int
) -> str:
	payload = _payload(index, timestamp, canonical_json(transactions), previous_hash, nonce, difficulty)
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class Block:
	index: int
	timestamp: str
	transactions: list
	previous_hash: str
	nonce: int
	difficulty: int
	hash: str = ""
	
	def calculate_hash(self) -> str:
		return compute_block_hash(
			self.index, self.timestamp, self.transactions, self.previous_hash, self.nonce, self.difficulty
		)
	
	def meets_difficulty(self) -> bool:
		return isinstance(self.hash, str) and self.hash.startswith("0" * int(self.difficulty))
	
	def to_dict(self) -> dict:
		return {
			"index": self.index,
			"timestamp": self.timestamp,
			"transactions": self.transactions,
			"previous_hash": self.previous_hash,
			"nonce": self.nonce,
			"difficulty": self.difficulty,
			"hash": self.hash,
		}
	
	@classmethod
	def from_dict(cls, data: dict) -> "Block":
		return cls(**{k: data[k] for k in (
			"index", "timestamp", "transactions", "previous_hash", "nonce", "difficulty", "hash")})


def create_genesis_block() -> Block:
	"""Deterministic genesis block (not mined; verify_chain compares it to this exact value)."""
	block = Block(0, GENESIS_TIMESTAMP, [], GENESIS_PREVIOUS_HASH, 0, 0)
	block.hash = block.calculate_hash()
	return block


def mine_block(
		index: int,
		previous_hash: str,
		transactions: list,
		difficulty: int,
		timestamp: Optional[str] = None,
) -> Block:
	"""Proof of Work: search for a nonce whose block hash has ``difficulty`` leading hex zeros."""
	if not 0 <= difficulty <= MAX_DIFFICULTY:
		raise ValueError(f"difficulty must be between 0 and {MAX_DIFFICULTY}")
	ts = timestamp or utc_now_iso()
	tx_json = canonical_json(transactions)
	prefix = "0" * difficulty
	nonce = 0
	while True:
		digest = hashlib.sha256(
			_payload(index, ts, tx_json, previous_hash, nonce, difficulty).encode("utf-8")
		).hexdigest()
		if digest.startswith(prefix):
			return Block(index, ts, transactions, previous_hash, nonce, difficulty, digest)
		nonce += 1


@dataclass
class ChainVerification:
	valid: bool
	block_count: int
	first_invalid_position: Optional[int] = None
	errors: list[str] = field(default_factory=list)
	
	@property
	def trusted_length(self) -> int:
		"""Number of leading blocks that may be trusted (everything before the first failure)."""
		return self.block_count if self.first_invalid_position is None else self.first_invalid_position


def verify_chain(blocks: list[Block], min_difficulty: int = MIN_ACCEPTED_DIFFICULTY) -> ChainVerification:
	"""Structural verification: genesis, indices, hash links, recomputed hashes, PoW target.

	Semantic checks on transactions (eligibility, duplicates, ...) are the voting layer's job.
	"""
	errors: list[str] = []
	first_bad: Optional[int] = None
	
	def fail(pos: int, message: str) -> None:
		nonlocal first_bad
		errors.append(f"Block {pos}: {message}")
		if first_bad is None:
			first_bad = pos
	
	if not blocks:
		return ChainVerification(False, 0, 0, ["Chain is empty (no genesis block)."])
	
	if blocks[0].to_dict() != create_genesis_block().to_dict():
		fail(0, "genesis block does not match the expected genesis block")
	
	for pos, block in enumerate(blocks):
		if pos > 0:
			prev = blocks[pos - 1]
			if block.index != pos:
				fail(pos, f"index is {block.index}, expected {pos}")
			if block.previous_hash != prev.hash:
				fail(pos, "previous_hash does not match the preceding block's hash (broken link)")
			if not isinstance(block.difficulty, int) or block.difficulty < min_difficulty:
				fail(pos, f"difficulty {block.difficulty!r} is below the accepted minimum {min_difficulty}")
			elif not block.meets_difficulty():
				fail(pos, "hash does not satisfy the proof-of-work target")
			if not isinstance(block.transactions, list):
				fail(pos, "transactions field is malformed")
		try:
			recomputed = block.calculate_hash()
		except (TypeError, ValueError) as exc:
			fail(pos, f"block contents cannot be hashed ({exc})")
			continue
		if recomputed != block.hash:
			fail(pos, "stored hash does not match the block contents (block was altered)")
	
	return ChainVerification(first_bad is None, len(blocks), first_bad, errors)


class Blockchain:
	"""In-memory chain helper: build, extend, verify. Persistence lives in database.py."""
	
	def __init__(self, difficulty: int = 3, blocks: Optional[Iterable[Block]] = None):
		self.difficulty = difficulty
		self.blocks: list[Block] = list(blocks) if blocks else [create_genesis_block()]
	
	@property
	def latest_block(self) -> Block:
		return self.blocks[-1]
	
	def __len__(self) -> int:
		return len(self.blocks)
	
	def add_block(self, transactions: list) -> Block:
		block = mine_block(self.latest_block.index + 1, self.latest_block.hash, transactions, self.difficulty)
		self.blocks.append(block)
		return block
	
	def verify(self, min_difficulty: int = MIN_ACCEPTED_DIFFICULTY) -> ChainVerification:
		return verify_chain(self.blocks, min_difficulty)
	
	def to_dicts(self) -> list[dict]:
		return [b.to_dict() for b in self.blocks]
