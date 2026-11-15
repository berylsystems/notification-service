"""Pytest fixtures. DB fixtures use Testcontainers and skip cleanly when Docker is unavailable.

The notify tables FK to ``tenant`` (restaurant-mgmt). Tests create a minimal copy of it first
(same column names as user-service's model), then run the notify chain and give the ``sg_notify``
role the SAME grants as ``handlers/migrate_handler.py`` (imported). Every test then talks to the
DB as ``sg_notify``, so RLS is enforced exactly as in production.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_FOREIGN_DDL = """
CREATE TABLE IF NOT EXISTS tenant (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug varchar(63) UNIQUE NOT NULL,
    name varchar(200) NOT NULL,
    status varchar(20) NOT NULL DEFAULT 'ACTIVE'
);
"""


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def _postgres() -> Iterator[object]:
    if not _docker_available():
        pytest.skip("Docker not available — skipping Postgres/RLS integration tests")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        "postgres:16-alpine", username="postgres", password="postgres", dbname="sg_user"
    ) as container:
        yield container


def _url(container: object, user: str, password: str) -> str:
    host = container.get_container_host_ip()  # type: ignore[attr-defined]
    port = container.get_exposed_port(5432)  # type: ignore[attr-defined]
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/sg_user"


@pytest.fixture(scope="session")
def admin_url(_postgres: object) -> str:
    return _url(_postgres, "postgres", "postgres")


@pytest.fixture(scope="session")
def app_url(_postgres: object, admin_url: str) -> str:
    """Non-superuser (``sg_notify``) URL — RLS is enforced for this role."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from notification_service.handlers.migrate_handler import grant_statements

    engine = create_engine(admin_url, future=True)
    with engine.begin() as conn:
        conn.execute(text(_FOREIGN_DDL))
    engine.dispose()

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", admin_url)
    command.upgrade(cfg, "head")

    engine = create_engine(admin_url, future=True)
    with engine.begin() as conn:
        conn.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='sg_notify') "
                "THEN CREATE ROLE sg_notify LOGIN PASSWORD 'sg_notify_pw'; END IF; END $$;"
            )
        )
        for stmt in grant_statements("sg_notify"):
            conn.execute(text(stmt))
    engine.dispose()

    return _url(_postgres, "sg_notify", "sg_notify_pw")


@pytest.fixture
def engine(app_url: str) -> Iterator[object]:
    from sg_commons.db import make_engine

    eng = make_engine(app_url)
    yield eng
    eng.dispose()


@pytest.fixture
def provider() -> object:
    from helpers import provider_config
    from notification_service.providers.fake import FakeProvider

    return FakeProvider(provider_config())


@pytest.fixture
def publisher() -> object:
    from notification_service.services.queue import ListPublisher

    return ListPublisher()


@pytest.fixture
def wired(engine: object, provider: object, publisher: object) -> Iterator[object]:
    """Router wired to the RLS role, an in-process signer, the fake provider and a list
    publisher. Yields the signer."""
    from sg_commons.crypto.signer import LocalRsaSigner

    from notification_service.api import deps

    signer = LocalRsaSigner.ephemeral()
    deps.configure(engine=engine, signer=signer, provider=provider, publisher=publisher)  # type: ignore[arg-type]
    yield signer
    deps.reset()
