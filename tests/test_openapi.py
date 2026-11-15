"""The hand-listed OpenAPI paths must match the routes actually registered in ``api/app.py``."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _spec_ops() -> set[tuple[str, str]]:
    spec_path = _ROOT / "scripts" / "gen_openapi.py"
    mod_spec = importlib.util.spec_from_file_location("gen_openapi", spec_path)
    assert mod_spec and mod_spec.loader
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return {(m.upper(), p) for p, ops in mod.build_spec()["paths"].items() for m in ops}


def _app_ops() -> set[tuple[str, str]]:
    src = (_ROOT / "src" / "notification_service" / "api" / "app.py").read_text()
    found = re.findall(r'@app\.(get|post|put|patch|delete)\("([^"]+)"\)', src)
    out = {(m.upper(), re.sub(r"<(\w+)>", r"{\1}", p)) for m, p in found}
    return {(m, p) for m, p in out if p != "/health"}


def _norm(ops: set[tuple[str, str]]) -> set[tuple[str, str]]:
    # path param NAMES differ by convention (ticket_id vs ticketId); compare the shape only
    return {(m, re.sub(r"\{\w+\}", "{}", p)) for m, p in ops}


def test_every_route_is_documented_and_nothing_extra() -> None:
    app, spec = _norm(_app_ops()), _norm(_spec_ops())
    assert app - spec == set(), f"routes missing from the OpenAPI spec: {sorted(app - spec)}"
    assert spec - app == set(), f"spec paths with no route: {sorted(spec - app)}"


def test_generated_spec_is_current() -> None:
    import json

    spec_path = _ROOT / "scripts" / "gen_openapi.py"
    mod_spec = importlib.util.spec_from_file_location("gen_openapi", spec_path)
    assert mod_spec and mod_spec.loader
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    on_disk = json.loads((_ROOT / "openapi" / "openapi.json").read_text())
    assert on_disk == json.loads(json.dumps(mod.build_spec())), (
        "openapi/openapi.json is stale: run `uv run python scripts/gen_openapi.py`"
    )
