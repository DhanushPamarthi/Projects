"""Workspace authentication using hashed passwords and opaque bearer sessions."""

import hashlib
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr, field_validator

from .config import AUTH_ALLOW_REGISTRATION, AUTH_SESSION_TTL_HOURS
from .db import connection


router = APIRouter(prefix="/auth", tags=["authentication"])
_PASSWORD_HASH_ITERATIONS = 600_000
_LOGIN_WINDOW_SECONDS = 60
_LOGIN_MAX_FAILURES = 5
_LOGIN_FAILURES: dict[str, tuple[float, int]] = {}
_LOGIN_FAILURES_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


def _stamp(value=None):
    return (value or _now()).isoformat()


def _uid(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"


def _email(value):
    return " ".join(str(value or "").strip().casefold().split())


def _password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PASSWORD_HASH_ITERATIONS)
    return f"pbkdf2_sha256${_PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password, encoded):
    try:
        algorithm, rounds, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
        return secrets.compare_digest(actual.hex(), expected_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_bearer_user(token):
    """Return the active session user, or None for missing, expired, or revoked tokens."""
    if not token or len(token) > 256:
        return None
    with connection() as conn:
        row = conn.execute(
            "SELECT u.user_id,u.email,u.display_name,u.role,u.is_active,s.expires_at,s.revoked_at "
            "FROM auth_session s JOIN app_user u ON u.user_id=s.user_id WHERE s.token_hash=%s",
            (_token_hash(token),),
        ).fetchone()
        if not row or not row["is_active"] or row["revoked_at"]:
            return None
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
        except (TypeError, ValueError):
            return None
        if expires_at <= _now():
            return None
        conn.execute("UPDATE auth_session SET last_seen_at=%s WHERE token_hash=%s", (_stamp(), _token_hash(token)))
        return {"user_id": row["user_id"], "email": row["email"], "name": row["display_name"], "role": row["role"]}


def _new_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    created = _stamp()
    expires = _stamp(_now() + timedelta(hours=AUTH_SESSION_TTL_HOURS))
    conn.execute(
        "INSERT INTO auth_session (session_id,user_id,token_hash,expires_at,created_at,last_seen_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (_uid("sess"), user_id, _token_hash(token), expires, created, created),
    )
    return token, expires


def _user_response(row):
    return {"user_id": row["user_id"], "email": row["email"], "name": row["display_name"], "role": row["role"]}


class RegisterRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: SecretStr = Field(min_length=12, max_length=128)
    name: str = Field(default="", max_length=120)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        value = _email(value)
        if not re.fullmatch(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+", value):
            raise ValueError("Enter a valid email address")
        return value

    @field_validator("name")
    @classmethod
    def trim_name(cls, value):
        return " ".join(str(value or "").split())


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return _email(value)


def _failure_key(request, email):
    client_host = request.client.host if request.client else "unknown"
    return f"{client_host}:{email}"


def _check_login_limit(key):
    now = time.monotonic()
    with _LOGIN_FAILURES_LOCK:
        started, count = _LOGIN_FAILURES.get(key, (now, 0))
        if now - started >= _LOGIN_WINDOW_SECONDS:
            _LOGIN_FAILURES[key] = (now, 0)
            return
        if count >= _LOGIN_MAX_FAILURES:
            raise HTTPException(429, "Too many sign-in attempts. Wait a minute and try again.")


def _record_login_failure(key):
    now = time.monotonic()
    with _LOGIN_FAILURES_LOCK:
        started, count = _LOGIN_FAILURES.get(key, (now, 0))
        if now - started >= _LOGIN_WINDOW_SECONDS:
            _LOGIN_FAILURES[key] = (now, 1)
        else:
            _LOGIN_FAILURES[key] = (started, count + 1)


def _clear_login_failures(key):
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES.pop(key, None)


@router.post("/register", status_code=201)
def register_user(request: RegisterRequest):
    if not AUTH_ALLOW_REGISTRATION:
        raise HTTPException(403, "New account registration is disabled.")
    email = request.email
    timestamp = _stamp()
    with connection() as conn:
        if conn.execute("SELECT 1 FROM app_user WHERE email=%s", (email,)).fetchone():
            raise HTTPException(409, "An account with this email already exists.")
        count = conn.execute("SELECT COUNT(*) AS user_count FROM app_user").fetchone()["user_count"]
        user_id = _uid("user")
        role = "workspace_admin" if int(count or 0) == 0 else "workspace_member"
        conn.execute(
            "INSERT INTO app_user (user_id,email,display_name,password_hash,role,is_active,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s)",
            (user_id, email, request.name or email.split("@", 1)[0], _password_hash(request.password.get_secret_value()), role, timestamp, timestamp),
        )
        user = conn.execute("SELECT user_id,email,display_name,role FROM app_user WHERE user_id=%s", (user_id,)).fetchone()
        token, expires_at = _new_session(conn, user_id)
    return {"token": token, "token_type": "bearer", "expires_at": expires_at, "user": _user_response(user)}


@router.post("/login")
def login_user(request: LoginRequest, http_request: Request):
    email = request.email
    key = _failure_key(http_request, email)
    _check_login_limit(key)
    with connection() as conn:
        user = conn.execute("SELECT * FROM app_user WHERE email=%s", (email,)).fetchone()
        stored_hash = user["password_hash"] if user else "pbkdf2_sha256$600000$00112233445566778899aabbccddeeff$" + "00" * 32
        verified = _verify_password(request.password.get_secret_value(), stored_hash)
        if not user or not user["is_active"] or not verified:
            _record_login_failure(key)
            raise HTTPException(401, "Invalid email or password.")
        token, expires_at = _new_session(conn, user["user_id"])
    _clear_login_failures(key)
    return {"token": token, "token_type": "bearer", "expires_at": expires_at, "user": _user_response(user)}


@router.get("/me")
def current_user(authorization: str | None = Header(default=None)):
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    user = resolve_bearer_user(token)
    if not user:
        raise HTTPException(401, "A valid sign-in is required.")
    return user


@router.post("/logout")
def logout_user(authorization: str | None = Header(default=None)):
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if token and len(token) <= 256:
        with connection() as conn:
            conn.execute("UPDATE auth_session SET revoked_at=%s WHERE token_hash=%s AND revoked_at IS NULL", (_stamp(), _token_hash(token)))
    return {"ok": True}
