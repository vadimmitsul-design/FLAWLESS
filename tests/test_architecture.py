"""Stable HTTP contracts and one-way application dependencies."""

import ast
import json
import re
from pathlib import Path

from app.main import app, create_app

ROOT = Path(__file__).resolve().parents[1]


def test_http_methods_and_paths_preserve_original_contract():
    expected_routes = json.loads(
        (Path(__file__).parent / "fixtures" / "route_contract.json").read_text(encoding="utf-8")
    )
    expected = {
        (item["method"].lower(), re.sub(r"\{(\w+):[^}]+\}", r"{\1}", item["path"]))
        for item in expected_routes
    }
    actual = {
        (method, path) for path, methods in app.openapi()["paths"].items() for method in methods
    }
    assert (
        actual - {(method, path) for method, path in actual if path.startswith("/cabinet-api/")}
        == expected
    )


def test_application_instances_own_their_template_environments():
    first, second = create_app(), create_app()
    assert first.state.templates is not second.state.templates
    assert first.state.templates.env is not second.state.templates.env


def test_services_do_not_depend_on_http_or_application_entry_point():
    forbidden = ("fastapi", "starlette", "app.api", "app.main")
    violations = []
    for path in (ROOT / "app" / "services").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [name.name for name in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}: {module}")
    assert not violations, "\n".join(violations)
