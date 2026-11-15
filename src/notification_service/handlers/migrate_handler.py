"""One-shot migration + role-provisioning Lambda for the notification service.

Runs the notify Alembic chain (own version table ``alembic_version_notify``), then creates the
non-superuser ``sg_notify`` role and grants it:

* CRUD on the notify tables (RLS on the tenant ones),
* SELECT + INSERT only on the append-only ``customer_consent`` (UPDATE/DELETE explicitly revoked;
  a trigger blocks them for every role too),
* CRUD on the non-RLS cross-tenant index tables (``message_ref``, ``customer_phone_ref``,
  ``inbound_event``, ``campaign_due``; ids only).

It reads no other service's tables (``tenant`` is referenced by FK only, checked as the owner).

Env (set by CDK): ``SG_ADMIN_SECRET_ARN``, ``SG_APP_SECRET_ARN``, ``SG_DB_HOST``, ``SG_DB_NAME``,
``SG_AWS_REGION``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import boto3  # type: ignore[import-untyped]
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_ROOT = Path(__file__).resolve().parents[3]

_OWN_TABLES = "customer, wa_template, message, campaign, tenant_notify_settings"
_APPEND_ONLY_TABLES = "customer_consent"
_INDEX_TABLES = "message_ref, customer_phone_ref, inbound_event, campaign_due"


def grant_statements(app_user: str) -> list[str]:
    """The grants for the app role (shared with the tests so they can't drift)."""
    return [
        f"GRANT USAGE ON SCHEMA public TO {app_user};",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_OWN_TABLES} TO {app_user};",
        f"GRANT SELECT, INSERT ON {_APPEND_ONLY_TABLES} TO {app_user};",
        f"REVOKE UPDATE, DELETE, TRUNCATE ON {_APPEND_ONLY_TABLES} FROM {app_user};",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_INDEX_TABLES} TO {app_user};",
    ]


def _secret(arn: str, region: str) -> dict[str, Any]:
    client = boto3.client("secretsmanager", region_name=region)
    data: dict[str, Any] = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
    return data


def _url(secret: dict[str, Any], host: str, db_name: str) -> str:
    user = quote_plus(secret["username"])
    password = quote_plus(secret["password"])
    return f"postgresql+psycopg://{user}:{password}@{host}:5432/{db_name}?sslmode=require"


def handler(event: dict[str, Any] | None = None, context: object | None = None) -> dict[str, Any]:
    region = os.environ["SG_AWS_REGION"]
    host = os.environ["SG_DB_HOST"]
    db_name = os.environ.get("SG_DB_NAME", "sg_user")

    admin = _secret(os.environ["SG_ADMIN_SECRET_ARN"], region)
    app = _secret(os.environ["SG_APP_SECRET_ARN"], region)
    engine = create_engine(_url(admin, host, db_name), future=True)

    with engine.connect() as connection:
        cfg = Config()
        cfg.set_main_option("script_location", str(_ROOT / "migrations"))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")

    app_user = app["username"]
    app_password = app["password"]
    with engine.begin() as conn:
        conn.execute(
            text(
                f"DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{app_user}') THEN "
                f"CREATE ROLE {app_user} LOGIN PASSWORD '{app_password}'; "
                f"ELSE ALTER ROLE {app_user} LOGIN PASSWORD '{app_password}'; "
                f"END IF; END $$;"
            )
        )
        for stmt in grant_statements(app_user):
            conn.execute(text(stmt))
    engine.dispose()

    return {"status": "ok", "migrated": True, "app_role": app_user}
