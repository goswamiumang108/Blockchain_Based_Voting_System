"""Streamlit glue: cached services, session helpers, flash messages."""
from __future__ import annotations

import os
from typing import Optional

import streamlit as st

from ..config import Settings
from ..models import User
from ..services import Services, build_services

SESSION_USER_ID = "auth_user_id"
FLASH_KEY = "_flash"
SECRET_KEYS = (
	"VOTECHAIN_DB_PATH", "VOTECHAIN_DIFFICULTY", "VOTECHAIN_TAG_SECRET", "VOTECHAIN_BCRYPT_ROUNDS",
	"VOTECHAIN_AUTO_MINE", "VOTECHAIN_MAX_TXS_PER_BLOCK", "ADMIN_USERNAME", "ADMIN_PASSWORD",
)


def bridge_secrets() -> None:
	"""Expose Streamlit Cloud 'Secrets' (secrets.toml) as environment variables for config.py."""
	try:
		for key in SECRET_KEYS:
			if key in st.secrets and key not in os.environ:
				os.environ[key] = str(st.secrets[key])
	except Exception:  # no secrets file configured: that's fine
		pass


@st.cache_resource(show_spinner=False)
def _cached_services(cache_key: str, _settings: Settings) -> Services:
	"""One Services object per process/config. Services are stateless (connection per call),
	so sharing them across sessions and Streamlit reruns is safe."""
	return build_services(_settings)


def get_services() -> Services:
	settings = Settings.from_env()
	return _cached_services(settings.cache_key(), settings)


def current_user() -> Optional[User]:
	"""Re-read the user from the database on EVERY run, so role changes/deletions take effect and
	session state only ever carries an id, never authority."""
	uid = st.session_state.get(SESSION_USER_ID)
	if uid is None:
		return None
	user = get_services().auth.get_user(uid)
	if user is None:
		st.session_state.pop(SESSION_USER_ID, None)
	return user


def login(user: User) -> None:
	st.session_state.clear()  # drop any stale state from before authentication
	st.session_state[SESSION_USER_ID] = user.id


def logout() -> None:
	st.session_state.clear()


def flash(kind: str, message: str) -> None:
	st.session_state.setdefault(FLASH_KEY, []).append((kind, message))


def show_flashes() -> None:
	for kind, message in st.session_state.pop(FLASH_KEY, []):
		getattr(st, kind, st.info)(message)
