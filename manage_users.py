"""Manage API users (users.json in TWR_STATE_DIR, default app/).

  python manage_users.py add <username>        # asks for the password (or --password)
  python manage_users.py passwd <username>
  python manage_users.py remove <username>
  python manage_users.py list
(from the parent folder: python -m app.manage_users ...)
"""
from __future__ import annotations

import argparse
import getpass
import sys

from auth import MIN_PASSWORD, hash_password, load_users, save_users


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["add", "passwd", "remove", "list"])
    ap.add_argument("username", nargs="?")
    ap.add_argument("--password")
    a = ap.parse_args()
    users = load_users()
    if a.action == "list":
        print("\n".join(sorted(users)) or "(no users)")
        return
    if not a.username:
        sys.exit("username required")
    if a.action == "remove":
        users.pop(a.username, None)
    else:
        if a.action == "add" and a.username in users:
            sys.exit(f"user '{a.username}' exists - use passwd")
        if a.action == "passwd" and a.username not in users:
            sys.exit(f"no user '{a.username}'")
        pw = a.password or getpass.getpass("password: ")
        if len(pw) < MIN_PASSWORD:
            sys.exit(f"password must have at least {MIN_PASSWORD} characters")
        users[a.username] = {"password": hash_password(pw)}
    save_users(users)
    print(f"{a.action}: {a.username} done")


if __name__ == "__main__":
    main()
