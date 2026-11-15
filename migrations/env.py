"""Alembic environment for the notification service.

Dedicated version table (``alembic_version_notify``) so this chain coexists with the menu, device,
order, offerings, promotions and restaurant-mgmt chains in the shared Aurora database. A live
``connection`` passed via ``config.attributes`` wins; else an explicit ``sqlalchemy.url``; else the
app's resolved URL.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sg_commons.config import get_settings
from sg_commons.db import Base, resolve_database_url
from sqlalchemy import create_engine, pool

from notification_service.db import models  # noqa: F401  (registers tables on the metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
_VERSION_TABLE = "alembic_version_notify"


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or resolve_database_url(get_settings())


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=_VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(
            connection=connection, target_metadata=target_metadata, version_table=_VERSION_TABLE
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = create_engine(_url(), poolclass=pool.NullPool, future=True)
    with connectable.connect() as conn:
        context.configure(
            connection=conn, target_metadata=target_metadata, version_table=_VERSION_TABLE
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
