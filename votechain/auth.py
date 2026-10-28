"""Voter registration and secure login (bcrypt password hashing, lockout on repeated failures)."""
from __future__ import annotations

import bcrypt
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from .database import Database
from .errors import AccountLockedError, AuthenticationError, ValidationError
from .models import User

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_BYTES = 72  # bcrypt limit; we reject longer passwords instead of silently truncating
GENERIC_LOGIN_ERROR = "Invalid username or password."


def _now() -> datetime:
	return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
	return dt.isoformat(timespec="seconds")


def _row_to_user(row: sqlite3.Row) -> User:
	return User(id=row["id"], username=row["username"], full_name=row["full_name"], role=row["role"])


class AuthService:
	def __init__(self, db: Database, bcrypt_rounds: int = 12, max_failed_attempts: int = 5, lock_minutes: int = 5):
		self.db = db
		self.rounds = bcrypt_rounds
		self.max_failed = max_failed_attempts
		self.lock_minutes = lock_minutes
		self._dummy_hash: Optional[bytes] = None
	
	# -- registration ----------------------------------------------------------------------
	def register(self, username: str, full_name: str, password: str) -> User:
		"""Public self-registration. Always creates a *voter*; admins cannot be self-registered."""
		return self._create_user(username, full_name, password, "voter")
	
	def create_admin(self, username: str, full_name: str, password: str) -> User:
		"""Trusted/operator path only (admin CLI or env bootstrap); never exposed in the UI."""
		return self._create_user(username, full_name, password, "admin")
	
	def _create_user(self, username: str, full_name: str, password: str, role: str) -> User:
		username = (username or "").strip()
		full_name = (full_name or "").strip()
		if not USERNAME_RE.match(username):
			raise ValidationError("Username must be 3-32 characters: letters, digits, '_', '.', '-'.")
		if not 1 <= len(full_name) <= 100:
			raise ValidationError("Full name is required (max 100 characters).")
		self._check_password_policy(password)
		pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(self.rounds)).decode("ascii")
		try:
			with self.db.transaction() as conn:
				cur = conn.execute(
					"INSERT INTO users (username, full_name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
					(username, full_name, pw_hash, role, _iso(_now())),
				)
				user_id = cur.lastrowid
		except sqlite3.IntegrityError:
			raise ValidationError("That username is already taken.") from None
		return User(user_id, username, full_name, role)
	
	@staticmethod
	def _check_password_policy(password: str) -> None:
		if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
			raise ValidationError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
		if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
			raise ValidationError(f"Password must be at most {MAX_PASSWORD_BYTES} bytes.")
	
	# -- login -----------------------------------------------------------------------------
	def authenticate(self, username: str, password: str) -> User:
		username = (username or "").strip()
		pw_bytes = (password or "").encode("utf-8")
		with self.db.connection() as conn:
			row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
		
		if row is None:
			# Burn comparable CPU time so unknown usernames are not distinguishable by latency.
			self._burn_time(pw_bytes)
			raise AuthenticationError(GENERIC_LOGIN_ERROR)
		
		if row["locked_until"] and row["locked_until"] > _iso(_now()):
			raise AccountLockedError("Too many failed attempts. Try again in a few minutes.")
		
		ok = len(pw_bytes) <= MAX_PASSWORD_BYTES and bcrypt.checkpw(pw_bytes, row["password_hash"].encode("ascii"))
		if ok:
			with self.db.transaction() as conn:
				conn.execute("UPDATE users SET failed_attempts = 0, locked_until = NULL WHERE id = ?", (row["id"],))
			return _row_to_user(row)
		
		with self.db.transaction() as conn:
			conn.execute("UPDATE users SET failed_attempts = failed_attempts + 1 WHERE id = ?", (row["id"],))
			attempts = conn.execute("SELECT failed_attempts FROM users WHERE id = ?", (row["id"],)).fetchone()[0]
			if attempts >= self.max_failed:
				until = _iso(_now() + timedelta(minutes=self.lock_minutes))
				conn.execute("UPDATE users SET failed_attempts = 0, locked_until = ? WHERE id = ?", (until, row["id"]))
		raise AuthenticationError(GENERIC_LOGIN_ERROR)
	
	def _burn_time(self, pw_bytes: bytes) -> None:
		if self._dummy_hash is None:
			self._dummy_hash = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt(self.rounds))
		if len(pw_bytes) <= MAX_PASSWORD_BYTES:
			bcrypt.checkpw(pw_bytes, self._dummy_hash)
	
	# -- queries ---------------------------------------------------------------------------
	def get_user(self, user_id: int) -> Optional[User]:
		with self.db.connection() as conn:
			row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
		return _row_to_user(row) if row else None
	
	def list_users(self, role: Optional[str] = None) -> list[User]:
		query, params = "SELECT * FROM users", ()
		if role:
			query, params = query + " WHERE role = ?", (role,)
		with self.db.connection() as conn:
			return [_row_to_user(r) for r in conn.execute(query + " ORDER BY username COLLATE NOCASE", params)]
