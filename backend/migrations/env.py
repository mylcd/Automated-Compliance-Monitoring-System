from alembic import context

from app.config import Settings
from app.database import create_database_engine
from app.models import Base
from app import domain  # noqa: F401 — register imported-domain models
from app import assessment_models  # noqa: F401 — register assessment history
from app import remediation_models  # noqa: F401 — register audit/source history
from app import monitoring_models  # noqa: F401 — register scan schedule

config = context.config
url = config.attributes.get("database_url") or Settings().database_url

if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_database_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
