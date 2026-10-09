"""Create or reset staff logins from the command line (handy for the very first accounts).

    python -m app.users add mina "Mina Park"            # prompts for a password
    python -m app.users add jun "Jun Lee" --manager
    python -m app.users reset mina                      # prints a new temporary password
    python -m app.users list
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import auth
from .db import connect


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("username")
    a.add_argument("display_name")
    a.add_argument("--manager", action="store_true")
    r = sub.add_parser("reset")
    r.add_argument("username")
    sub.add_parser("list")
    args = ap.parse_args()
    with connect() as conn:
        if args.cmd == "add":
            pw = getpass.getpass("Password (min 8 characters, empty = generate a temporary one): ") or auth.temp_password()
            if len(pw) < 8:
                sys.exit("Password too short.")
            try:
                u = auth.create_user(conn, args.username, args.display_name, pw,
                                     "manager" if args.manager else "staff", created_by="cli")
            except ValueError as e:
                sys.exit(str(e))
            print(f"Created {u['username']}. Temporary password: {pw} (they will be asked to change it).")
        elif args.cmd == "reset":
            pw = auth.temp_password()
            row = conn.execute("UPDATE staff_users SET password_hash = %s, must_change_password = TRUE, "
                               "session_version = session_version + 1 WHERE username = %s RETURNING id",
                               (auth.hash_password(pw), args.username.lower())).fetchone()
            print(f"New temporary password for {args.username}: {pw}" if row else "No such user.")
        else:
            for u in conn.execute("SELECT username, display_name, role, is_active, last_login_at FROM staff_users "
                                  "ORDER BY username").fetchall():
                print(f"{u['username']:20} {u['display_name']:25} {u['role']:8} "
                      f"{'active' if u['is_active'] else 'disabled':9} last login {u['last_login_at'] or '-'}")


if __name__ == "__main__":
    main()
