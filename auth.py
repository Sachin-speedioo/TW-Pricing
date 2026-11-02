"""Login + JWT for the pricing API.

Users: STATE_DIR/users.json (username -> PBKDF2-SHA256 hash; never plain passwords), managed with manage_users.py;
on a fresh deployment TWR_ADMIN_USER / TWR_ADMIN_PASSWORD create the first user at start-up.
Tokens: HS256 JWT, valid TWR_TOKEN_MINUTES (default 480). Signing secret: env TWR_JWT_SECRET, else STATE_DIR/.jwt_secret (created
on first use; keep it private - anyone with it can mint tokens). Changing / deleting the secret logs everyone out.
STATE_DIR defaults to app/ (app/settings.py); both files are git-ignored.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from settings import STATE_DIR, TOKEN_MINUTES

USERS_FILE = STATE_DIR / "users.json"
SECRET_FILE = STATE_DIR / ".jwt_secret"
ALGO = "HS256"
ITERATIONS = 310_000
MIN_PASSWORD = 8
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/token")


def _secret() -> str:
    if os.environ.get("TWR_JWT_SECRET"):
        return os.environ["TWR_JWT_SECRET"]
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    return SECRET_FILE.read_text(encoding="utf-8").strip()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def load_users() -> dict:
    return json.loads(USERS_FILE.read_text(encoding="utf-8")) if USERS_FILE.exists() else {}


def save_users(users: dict):
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")


def bootstrap_admin() -> str | None:
    """First deployment: no users yet and TWR_ADMIN_PASSWORD set -> create TWR_ADMIN_USER (default 'admin'). -> name or None."""
    pw = os.environ.get("TWR_ADMIN_PASSWORD")
    if load_users() or not pw:
        return None
    if len(pw) < MIN_PASSWORD:
        raise RuntimeError(f"TWR_ADMIN_PASSWORD must have at least {MIN_PASSWORD} characters")
    user = os.environ.get("TWR_ADMIN_USER", "admin")
    save_users({user: {"password": hash_password(pw)}})
    return user


def authenticate(username: str, password: str) -> bool:
    u = load_users().get(username)
    # hash even for unknown users so the response time does not reveal which usernames exist
    return verify_password(password, u["password"] if u else hash_password("x")) and u is not None


def create_token(username: str) -> tuple[str, int]:
    now = dt.datetime.now(dt.timezone.utc)
    exp = now + dt.timedelta(minutes=TOKEN_MINUTES)
    return jwt.encode({"sub": username, "iat": now, "exp": exp}, _secret(), algorithm=ALGO), TOKEN_MINUTES * 60


def current_user(token: str = Depends(oauth2)) -> str:
    fail = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not logged in or session expired", headers={"WWW-Authenticate": "Bearer"})
    try:
        user = jwt.decode(token, _secret(), algorithms=[ALGO]).get("sub")
    except jwt.PyJWTError:
        raise fail
    if not user or user not in load_users():
        raise fail
    return user
