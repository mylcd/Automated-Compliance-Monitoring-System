import time

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.bootstrap import BACKEND_DIR, PASSWORD_HASH, bootstrap, migrate
from app.config import Settings
from app.database import create_database_engine, session_factory
from app.main import COOKIE_NAME, create_app, hash_token
from app.models import LoginSession, User

PASSWORD = "test-only-long-password"
WRITE_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Origin": "http://testserver"}


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, admin_username="admin", admin_password=PASSWORD, database_url=f"sqlite:///{tmp_path / 'test.db'}", evidence_dir=tmp_path / "evidence", app_origin="http://testserver")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def login(client, **overrides):
    return client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD, **overrides}, headers=WRITE_HEADERS)


def test_health_readiness_and_protected_routes(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/ready").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/system").status_code == 401
    assert client.post("/api/auth/logout", headers=WRITE_HEADERS).status_code == 401


def test_login_session_and_logout_revoke_cookie(client):
    response = login(client)
    assert response.status_code == 200
    csrf = response.json()["csrf_token"]
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api" in cookie
    token = client.cookies.get(COOKIE_NAME)
    assert client.get("/api/auth/me").json()["username"] == "admin"
    assert client.get("/api/system").json()["organization"] == "MYL Inc."
    assert client.post("/api/auth/logout", headers={**WRITE_HEADERS, "X-CSRF-Token": csrf}).status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set(COOKIE_NAME, token)
    assert client.get("/api/system").status_code == 401


@pytest.mark.parametrize("credentials", [{"password": "wrong"}, {"username": "missing-user"}])
def test_bad_credentials_are_generic(client, credentials):
    response = login(client, **credentials)
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password"
    assert COOKIE_NAME not in client.cookies


def test_csrf_protection_and_origin_validation(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 403
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}, headers={**WRITE_HEADERS, "Origin": "http://other-site.example"}).status_code == 403
    identity = login(client).json()
    assert client.post("/api/auth/logout", headers=WRITE_HEADERS).status_code == 403
    assert client.post("/api/auth/logout", headers={**WRITE_HEADERS, "X-CSRF-Token": identity["csrf_token"], "Origin": "http://other-site.example"}).status_code == 403
    assert client.get("/api/auth/me").status_code == 200


def test_expired_and_forged_sessions_are_denied(client, settings):
    login(client)
    engine = create_database_engine(settings.database_url)
    with session_factory(engine).begin() as db:
        session = db.scalar(select(LoginSession))
        session.expires_at = int(time.time()) - 1
    engine.dispose()
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.clear()
    client.cookies.set(COOKIE_NAME, "forged-token")
    assert client.get("/api/system").status_code == 401


def test_login_throttle(client):
    for _ in range(10):
        assert login(client, password="wrong").status_code == 401
    response = login(client)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_restart_preserves_admin_and_session(settings):
    with TestClient(create_app(settings)) as first:
        assert login(first).status_code == 200
        token = first.cookies.get(COOKIE_NAME)
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.set(COOKIE_NAME, token)
        assert restarted.get("/api/auth/me").json()["username"] == "admin"
    engine = create_database_engine(settings.database_url)
    with session_factory(engine)() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1
        user = db.scalar(select(User))
        assert user.password_hash != PASSWORD
        assert PASSWORD_HASH.verify(PASSWORD, user.password_hash)
        assert db.get(LoginSession, token) is None
        assert db.get(LoginSession, hash_token(token)) is not None
    engine.dispose()


def test_password_reset_revokes_sessions(settings):
    with TestClient(create_app(settings)) as first:
        login(first)
        token = first.cookies.get(COOKIE_NAME)
    changed = settings.model_copy(update={"admin_password": Settings(_env_file=None, admin_password="new-long-test-password").admin_password})
    bootstrap(changed, reset_password=True)
    with TestClient(create_app(changed)) as client:
        client.cookies.set(COOKIE_NAME, token)
        assert client.get("/api/auth/me").status_code == 401
        assert login(client).status_code == 401
        assert login(client, password="new-long-test-password").status_code == 200


def test_foreign_keys_enabled(settings):
    bootstrap(settings)
    engine = create_database_engine(settings.database_url)
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
    with pytest.raises(IntegrityError), session_factory(engine).begin() as db:
        db.add(LoginSession(token_hash="x" * 64, user_id=9999, csrf_token="y" * 43, expires_at=9999999999))
    engine.dispose()


def test_migration_upgrade_downgrade_round_trip(settings):
    migrate(settings.database_url)
    migrate(settings.database_url)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["database_url"] = settings.database_url
    command.downgrade(config, "base")
    migrate(settings.database_url)
    engine = create_database_engine(settings.database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0006"
    engine.dispose()


def test_readiness_detects_missing_schema(client):
    with client.app.state.engine.begin() as connection:
        connection.execute(text("DROP TABLE login_sessions"))
        connection.execute(text("DROP TABLE users"))
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/ready").status_code == 503


@pytest.mark.parametrize("password", ["", "   ", "replace-with-a-private-password", "x" * 1025])
def test_invalid_password_configuration_hides_secret(password):
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, admin_password=password)
    assert "input_value=" not in str(error.value)


def test_local_demo_allows_existing_short_password():
    assert Settings(_env_file=None, admin_password="demo").admin_password.get_secret_value() == "demo"
