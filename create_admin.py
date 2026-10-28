"""Create an administrator account from the command line.

Why this exists
---------------
The app's Register form only creates voter accounts, so nobody can make themselves an admin from the browser.
Admin accounts can only be created by someone with access to the server. There are two ways to do that:
  1. Set ADMIN_USERNAME and ADMIN_PASSWORD in .streamlit/secrets.toml. The admin is created automatically on first start.
  2. Run this script (the alternative, described below).

When to use it
--------------
  - You want to add a second or replacement admin later. The secrets-based admin is only created if that username does not exist yet, so editing
    the secrets afterwards will not add another one.
  - You prefer not to keep an admin password in a file.
  - You lost an admin password. This script cannot reset it, but you can create a new admin with it and sign in again.

If your admin from secrets.toml already works, this file is not needed. The app never calls it; it only runs when you run it yourself.

Usage
-----
Run from the project folder, with the virtual environment active:

    python create_admin.py USERNAME

You will be asked for the password twice; it is not shown as you type. The account is stored in the same database the app uses (VOTECHAIN_DB_PATH,
default data/votechain.db), so the app must be pointed at the same database for the new admin to be able to sign in.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from votechain.config import Settings
from votechain.errors import VoteChainError
from votechain.services import build_services


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("username")
	args = parser.parse_args()
	services = build_services(Settings.from_env())
	password = getpass.getpass("Password: ")
	if password != getpass.getpass("Repeat password: "):
		print("Passwords do not match.", file=sys.stderr)
		return 1
	try:
		user = services.auth.create_admin(args.username, "Administrator", password)
	except VoteChainError as exc:
		print(f"Error: {exc}", file=sys.stderr)
		return 1
	print(f"Created admin '{user.username}'.")
	return 0


if __name__ == "__main__":
	sys.exit(main())
