"""Composition root: builds the service objects from Settings (used by the UI and the admin CLI)."""
from __future__ import annotations

from dataclasses import dataclass

from .auth import AuthService
from .config import Settings
from .database import Database
from .voting import VotingService


@dataclass
class Services:
	settings: Settings
	db: Database
	auth: AuthService
	voting: VotingService


def build_services(settings: Settings) -> Services:
	db = Database(settings.db_path)
	voting = VotingService(
		db, settings.tag_secret, difficulty=settings.difficulty,
		auto_mine=settings.auto_mine, max_txs_per_block=settings.max_txs_per_block,
	)
	auth = AuthService(db, bcrypt_rounds=settings.bcrypt_rounds)
	services = Services(settings, db, auth, voting)
	bootstrap_admin(services)
	return services


def bootstrap_admin(services: Services) -> None:
	"""Create the operator-defined admin (ADMIN_USERNAME / ADMIN_PASSWORD) if it does not exist yet."""
	s = services.settings
	if not (s.admin_username and s.admin_password):
		return
	with services.db.connection() as conn:
		exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (s.admin_username,)).fetchone()
	if not exists:
		services.auth.create_admin(s.admin_username, "Administrator", s.admin_password)
