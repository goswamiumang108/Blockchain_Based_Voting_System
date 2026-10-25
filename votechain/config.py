"""Runtime configuration, read from environment variables (see README)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "votechain.db"

MIN_DIFFICULTY = 1
MAX_DIFFICULTY = 6  # each +1 is ~16x more work; keep mining fast


def _env_bool(name: str, default: bool) -> bool:
	raw = os.environ.get(name)
	if raw is None:
		return default
	return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
	try:
		value = int(os.environ.get(name, default))
	except ValueError:
		value = default
	return max(lo, min(hi, value))


def load_tag_secret(db_path: Path) -> bytes:
	"""Secret key used to derive anonymous voter tags.

	Priority: VOTECHAIN_TAG_SECRET env var, else a key file next to the database
	(created on first run, with owner-only 0600 permissions on Linux/macOS). Losing this secret makes previously
	cast ballots fail eligibility validation, so back it up with the database.
	"""
	env = os.environ.get("VOTECHAIN_TAG_SECRET")
	if env:
		return env.encode("utf-8")
	key_file = db_path.parent / "tag_secret.key"
	if key_file.exists():
		return key_file.read_text().strip().encode("utf-8")
	key_file.parent.mkdir(parents=True, exist_ok=True)
	value = secrets.token_hex(32)
	fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
	with os.fdopen(fd, "w") as fh:
		fh.write(value)
	return value.encode("utf-8")


@dataclass(frozen=True)
class Settings:
	db_path: Path
	difficulty: int
	tag_secret: bytes
	bcrypt_rounds: int
	auto_mine: bool
	max_txs_per_block: int
	admin_username: str | None = None
	admin_password: str | None = None
	
	@classmethod
	def from_env(cls) -> "Settings":
		db_path = Path(os.environ.get("VOTECHAIN_DB_PATH", DEFAULT_DB_PATH)).expanduser()
		return cls(
			db_path=db_path,
			difficulty=_env_int("VOTECHAIN_DIFFICULTY", 3, MIN_DIFFICULTY, MAX_DIFFICULTY),
			tag_secret=load_tag_secret(db_path),
			bcrypt_rounds=_env_int("VOTECHAIN_BCRYPT_ROUNDS", 12, 4, 15),
			auto_mine=_env_bool("VOTECHAIN_AUTO_MINE", True),
			max_txs_per_block=_env_int("VOTECHAIN_MAX_TXS_PER_BLOCK", 50, 1, 1000),
			admin_username=os.environ.get("ADMIN_USERNAME") or None,
			admin_password=os.environ.get("ADMIN_PASSWORD") or None,
		)
	
	def cache_key(self) -> str:
		return "|".join(
			map(str, (self.db_path, self.difficulty, self.bcrypt_rounds, self.auto_mine, self.max_txs_per_block))
		)
