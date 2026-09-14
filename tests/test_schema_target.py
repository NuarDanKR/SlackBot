import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import psycopg
import pytest


@pytest.mark.parametrize("host,port,db,expected", [
    ("127.0.0.1", 55432, "tyslackai", 0),
    ("127.0.0.1", 5432, "tyslackai", 2),
    ("remote.example", 55432, "tyslackai", 2),
    ("127.0.0.1", 55432, "other", 2),
])
def test_target_check_refuses_different_database(monkeypatch, host, port, db, expected):
    path = Path(__file__).resolve().parents[1] / "scripts/check_schema_drift.py"
    main = runpy.run_path(str(path))["main"]
    monkeypatch.setitem(main.__globals__, "load_env_file", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    conn = MagicMock()
    conn.info = SimpleNamespace(host=host, port=port, dbname=db)
    manager = MagicMock()
    manager.__enter__.return_value = conn
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: manager)
    assert main(["--target-only", "--expected-db", "tyslackai",
                 "--expected-port", "55432"]) == expected
    conn.execute.assert_not_called()
