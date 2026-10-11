import importlib
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def auth_client(monkeypatch):
    data = Path(".test-data") / f"auth-{uuid.uuid4().hex}"
    monkeypatch.setenv("TESTIQ_DATA_DIR", str(data))
    monkeypatch.setenv("TESTIQ_DB_PATH", str(data / "test.db"))
    monkeypatch.setenv("TESTIQ_AUTH_REQUIRED", "true")
    monkeypatch.setenv("TESTIQ_AUTH_ALLOW_REGISTRATION", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TESTIQ_POSTGRES_HOST", raising=False)

    import app.config, app.db, app.auth, app.knowledge_fabric, app.main
    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.auth)
    importlib.reload(app.knowledge_fabric)
    importlib.reload(app.main)
    with TestClient(app.main.app) as client:
        yield client, app.db


def _register(client, email="demo@example.com", password="LongDemoPassword123"):
    return client.post("/auth/register", json={"email": email, "password": password, "name": " Demo User "})


def test_register_login_protect_workspace_and_logout(auth_client):
    client, db = auth_client
    unauthenticated = client.get("/api/v1/projects/workspace-summary")
    assert unauthenticated.status_code == 401

    registered = _register(client, " Demo@Example.com ")
    assert registered.status_code == 201, registered.text
    payload = registered.json()
    assert payload["user"]["email"] == "demo@example.com"
    assert payload["user"]["role"] == "workspace_admin"
    assert len(payload["token"]) >= 40
    assert "password" not in payload and "password_hash" not in payload["user"]

    headers = {"Authorization": f"Bearer {payload['token']}"}
    current = client.get("/auth/me", headers=headers)
    assert current.status_code == 200
    assert current.json()["user_id"] == payload["user"]["user_id"]
    workspace = client.get("/api/v1/projects/workspace-summary", headers=headers)
    assert workspace.status_code == 200, workspace.text

    with db.connection() as conn:
        user_row = conn.execute("SELECT password_hash FROM app_user WHERE email=%s", ("demo@example.com",)).fetchone()
        session_row = conn.execute("SELECT token_hash FROM auth_session").fetchone()
    assert user_row["password_hash"] != "LongDemoPassword123"
    assert user_row["password_hash"].startswith("pbkdf2_sha256$")
    assert session_row["token_hash"] != payload["token"]

    logged_out = client.post("/auth/logout", headers=headers)
    assert logged_out.status_code == 200
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert client.get("/api/v1/projects/workspace-summary", headers=headers).status_code == 401


def test_login_rejects_invalid_credentials_and_duplicate_registration(auth_client):
    client, _ = auth_client
    assert _register(client).status_code == 201
    duplicate = _register(client, "DEMO@example.com")
    assert duplicate.status_code == 409
    wrong_password = client.post("/auth/login", json={"email": "demo@example.com", "password": "incorrect-password"})
    assert wrong_password.status_code == 401
    login = client.post("/auth/login", json={"email": " DEMO@example.com ", "password": "LongDemoPassword123"})
    assert login.status_code == 200, login.text
    assert login.json()["user"]["email"] == "demo@example.com"


def test_registration_requires_valid_email_and_long_password(auth_client):
    client, _ = auth_client
    assert client.post("/auth/register", json={"email": "bad-email", "password": "short"}).status_code == 422
    assert client.post("/auth/register", json={"email": "valid@example.com", "password": "short"}).status_code == 422


def test_login_rate_limit_blocks_repeated_failures(auth_client, monkeypatch):
    client, _ = auth_client
    import app.auth
    monkeypatch.setattr(app.auth, "_LOGIN_MAX_FAILURES", 2)
    for _ in range(2):
        assert client.post("/auth/login", json={"email": "missing@example.com", "password": "not-the-password"}).status_code == 401
    blocked = client.post("/auth/login", json={"email": "missing@example.com", "password": "not-the-password"})
    assert blocked.status_code == 429
