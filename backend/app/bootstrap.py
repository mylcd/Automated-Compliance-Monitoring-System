import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
from pwdlib import PasswordHash
from sqlalchemy import delete, select

from app.config import Settings
from app.database import create_database_engine, session_factory
from app.models import LoginSession, User

PASSWORD_HASH = PasswordHash.recommended()
BACKEND_DIR = Path(__file__).resolve().parents[1]


def migrate(database_url: str) -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def bootstrap(settings: Settings, reset_password: bool = False) -> None:
    migrate(settings.database_url)
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)
    engine = create_database_engine(settings.database_url)
    try:
        with session_factory(engine).begin() as db:
            user = db.scalar(select(User))
            if user is None:
                db.add(User(username=settings.admin_username, password_hash=PASSWORD_HASH.hash(settings.admin_password.get_secret_value())))
            elif reset_password:
                user.username = settings.admin_username
                user.password_hash = PASSWORD_HASH.hash(settings.admin_password.get_secret_value())
                db.execute(delete(LoginSession))
    finally:
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate SQLite and initialize the local administrator")
    parser.add_argument("--reset-password", action="store_true", help="Apply configured credentials and revoke all sessions")
    bootstrap(Settings(), reset_password=parser.parse_args().reset_password)
