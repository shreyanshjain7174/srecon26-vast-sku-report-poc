"""Static compatibility contract for the standalone Azure guard deployment."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEPLOYED_PYTHON = (
    ROOT / "guard/ssh_rpc.py",
    ROOT / "guard/guard_worker.py",
)


def test_deployed_guard_parses_as_python310_and_avoids_newer_datetime_utc() -> None:
    for path in DEPLOYED_PYTHON:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path), feature_version=(3, 10))
        datetime_imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "datetime"
            for alias in node.names
        }
        assert "UTC" not in datetime_imports, f"{path} imports datetime.UTC, which requires Python 3.11"
