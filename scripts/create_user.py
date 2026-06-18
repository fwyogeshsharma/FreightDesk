"""Admin CLI to manage internal operator accounts (telecallers / admins).

Telecallers and admins are NOT self-service — an admin creates and manages them here,
on the server. Contributors self-register through the mobile app instead.

Examples (host venv):
    python scripts/create_user.py create  --phone 9811008120 --role telecaller --name "Asha"
    python scripts/create_user.py set-password --phone 9811008120
    python scripts/create_user.py set-role  --phone 9811008120 --role admin
    python scripts/create_user.py deactivate --phone 9811008120
    python scripts/create_user.py list

In a container:
    docker compose run --rm web python scripts/create_user.py create --phone … --role telecaller
"""
import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from pipeline.db import User, get_session_factory, init_db  # noqa: E402
from pipeline import auth  # noqa: E402


def _open_session():
    init_db()  # make sure the users table exists
    return get_session_factory()()


def _resolve_password(arg_password):
    """Use --password if given, else prompt on a TTY, else generate and print one."""
    if arg_password:
        return arg_password, False
    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("Password: "), False
    return secrets.token_urlsafe(9), True


def cmd_create(args):
    password, generated = _resolve_password(args.password)
    s = _open_session()
    try:
        user = auth.create_user(
            s, args.phone, password, display_name=args.name, role=args.role,
            email=args.email, registration_source="cli")
        s.commit()
        print(f"created {user.role}: id={user.id} phone={user.phone} "
              f"name={user.display_name or '-'}")
        if generated:
            print(f"generated password: {password}")
    except auth.DuplicatePhone:
        print(f"ERROR: a user with phone {args.phone} already exists")
        sys.exit(1)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        s.close()


def _require_user(s, phone):
    user = auth.find_by_phone(s, phone)
    if not user:
        print(f"ERROR: no user with phone {phone}")
        sys.exit(1)
    return user


def cmd_set_password(args):
    password, generated = _resolve_password(args.password)
    if len(password) < 6:
        print("ERROR: password must be at least 6 characters")
        sys.exit(1)
    s = _open_session()
    try:
        user = _require_user(s, args.phone)
        user.password_hash = auth.hash_password(password)
        s.commit()
        print(f"password updated for {user.phone}")
        if generated:
            print(f"generated password: {password}")
    finally:
        s.close()


def cmd_set_role(args):
    s = _open_session()
    try:
        user = _require_user(s, args.phone)
        if args.role not in auth.ROLES:
            print(f"ERROR: role must be one of {auth.ROLES}")
            sys.exit(1)
        user.role = args.role
        s.commit()
        print(f"{user.phone} is now '{user.role}'")
    finally:
        s.close()


def cmd_deactivate(args):
    s = _open_session()
    try:
        user = _require_user(s, args.phone)
        user.is_active = False
        s.commit()
        print(f"{user.phone} deactivated (existing sessions stop working)")
    finally:
        s.close()


def cmd_activate(args):
    s = _open_session()
    try:
        user = _require_user(s, args.phone)
        user.is_active = True
        s.commit()
        print(f"{user.phone} activated")
    finally:
        s.close()


def cmd_list(args):
    s = _open_session()
    try:
        rows = s.execute(select(User).order_by(User.created_at)).scalars().all()
        if not rows:
            print("(no users yet)")
            return
        for u in rows:
            flag = "" if u.is_active else "  [disabled]"
            print(f"  {u.id:>4}  {u.role:<11} {u.phone:<16} {u.display_name or '-'}{flag}")
    finally:
        s.close()


def main():
    p = argparse.ArgumentParser(description="Manage internal operator accounts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a telecaller/admin/contributor")
    c.add_argument("--phone", required=True)
    c.add_argument("--role", default="telecaller", choices=auth.ROLES)
    c.add_argument("--name", default=None)
    c.add_argument("--email", default=None)
    c.add_argument("--password", default=None, help="omit to be prompted / auto-generated")
    c.set_defaults(func=cmd_create)

    c = sub.add_parser("set-password", help="reset a user's password")
    c.add_argument("--phone", required=True)
    c.add_argument("--password", default=None)
    c.set_defaults(func=cmd_set_password)

    c = sub.add_parser("set-role", help="change a user's role")
    c.add_argument("--phone", required=True)
    c.add_argument("--role", required=True, choices=auth.ROLES)
    c.set_defaults(func=cmd_set_role)

    c = sub.add_parser("deactivate", help="disable an account")
    c.add_argument("--phone", required=True)
    c.set_defaults(func=cmd_deactivate)

    c = sub.add_parser("activate", help="re-enable an account")
    c.add_argument("--phone", required=True)
    c.set_defaults(func=cmd_activate)

    c = sub.add_parser("list", help="list all accounts")
    c.set_defaults(func=cmd_list)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
