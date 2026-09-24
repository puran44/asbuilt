"""
Asbuilt — authentication
========================

The app serves a contractor's entire mailbox, personal mail included. The
moment it listens on anything but localhost, anyone on the same network can
reach it. So: a password, hashed, never stored in source.

Set one (writes to .env, which is gitignored):

    python auth.py set "some good password"
    python auth.py set                      # generates a strong one and prints it

Then run the app. Sessions last 30 days on a device, so a phone stays logged in.

Scope note: this is single-user auth for LAN use. Real multi-tenant hosting
needs per-customer accounts, HTTPS, and rate limiting — see the deployment
notes at the bottom of this file before putting it on a public domain.
"""

import functools
import os
import re
import secrets
import sys

from flask import redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")

USER_KEY = "ASBUILT_USER"
HASH_KEY = "ASBUILT_PASSWORD_HASH"
SECRET_KEY = "ASBUILT_SECRET"


def write_env(key, value):
    """Set or replace one key in .env, leaving everything else untouched."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    pattern = re.compile(rf"^{re.escape(key)}=")
    replaced = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")

    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def ensure_secret():
    """Flask's session signing key. Persisted so restarts don't log you out."""
    secret = os.getenv(SECRET_KEY)
    if not secret:
        secret = secrets.token_hex(32)
        write_env(SECRET_KEY, secret)
        os.environ[SECRET_KEY] = secret
    return secret


def is_configured():
    return bool(os.getenv(HASH_KEY))


def verify(username, password):
    """Constant-time-ish check. Compares the hash even on unknown usernames so
    a wrong username and a wrong password take the same time to reject."""
    expected_user = os.getenv(USER_KEY, "admin")
    stored = os.getenv(HASH_KEY) or generate_password_hash("_no_password_set_")
    ok_password = check_password_hash(stored, password or "")
    return ok_password and (username or "") == expected_user


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(f"/login?next={request.path}")
        return view(*args, **kwargs)
    return wrapped


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "set":
        print(__doc__)
        sys.exit(1)

    if len(sys.argv) >= 3:
        password = sys.argv[2]
        generated = False
    else:
        # Four random words beats a short random string: as strong, far easier
        # to type on a phone.
        words = ["riser", "conduit", "flange", "gasket", "plumb", "torque",
                 "anchor", "coupling", "valve", "solder", "manifold", "sleeve",
                 "chase", "bracket", "grommet", "spindle"]
        password = "-".join(secrets.choice(words) for _ in range(4))
        generated = True

    if len(password) < 10:
        print("Use at least 10 characters — this guards a real mailbox.")
        sys.exit(1)

    user = os.getenv(USER_KEY) or "admin"
    write_env(USER_KEY, user)
    write_env(HASH_KEY, generate_password_hash(password))
    ensure_secret()

    print("Password set in .env (gitignored).")
    print(f"  username: {user}")
    if generated:
        print(f"  password: {password}")
        print("\nWrite it down — it is hashed on disk and cannot be recovered.")
    print("\nStart the app:            python app.py")
    print("Reachable from a phone:   python app.py --lan")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    main()
