from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.config import settings
from app.db.base import Base, engine

# Loaded by `alembic.ini` for stdout logging config.
if context.config.config_file_name is not None:
    fileConfig(context.config.config_file_name)

# Override sqlalchemy.url with the value our app uses, so a single source of
# truth (.env) drives both runtime and migrations.
context.config.set_main_option("sqlalchemy.url", settings.database_url)

# Import models for autogenerate. Phase 1+ adds modules to app/db/models/.
# Import-side-effects register them on Base.metadata.
import app.db.models  # noqa: F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=settings.database_url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=settings.database_url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
