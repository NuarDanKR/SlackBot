"""B-12 색인 실행 진입점 — DB·프로바이더·아카이브 없이 돈다.

지키는 것:
* 켜진 곳이 없으면 모델도 아카이브도 건드리지 않는다
* 계획 모드는 이력 행을 만들지 않고 프로바이더를 만들지 않는다
* 다른 실행이 돌고 있으면 시작하지 않는다
* 실패해도 이력은 닫히고, 예외 본문은 출력·이력 어디에도 없다
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from tybot import embedding_index_cli as cli
from tybot.archive.embedding_source import VectorSource, source_revision

SECRET = "본문-경로-/var/lib/tybot/archive/비밀.md"


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    """`main()` 은 env 파일을 읽는다. 시험에서 저장소 `.env` 를 읽으면 운영 DSN 이
    프로세스에 올라가고, 연결 주입을 빠뜨린 시험 하나가 운영 DB 에 붙는다."""
    monkeypatch.setattr("tybot.envfile.load_env_file", lambda *a, **k: "test")
    monkeypatch.delenv("DATABASE_URL", raising=False)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.conn.executed.append((text, params))
        self._rows = self.conn.answer(text, params)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, *, models=(), running=False, price=None, local_rows=()):
        self.models = list(models)
        self.local_rows = list(local_rows)
        self.running = running
        self.price = price
        self.executed: list[tuple[str, object]] = []
        self.next_id = 41

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTransaction()

    def answer(self, sql, params):
        if sql.startswith("SELECT p.workspace, m.provider"):
            return list(self.local_rows)
        if sql.startswith("SELECT DISTINCT p.model_revision"):
            return [(m,) for m in self.models]
        if sql.startswith("SELECT input_price_per_mtok"):
            return [(self.price,)] if self.price is not None else []
        if sql.startswith("SELECT 1 FROM search_embedding_index_run"):
            return [(1,)] if self.running else []
        if sql.startswith("INSERT INTO search_embedding_index_run"):
            self.next_id += 1
            return [(self.next_id,)]
        return []

    def statements(self, prefix):
        return [(sql, params) for sql, params in self.executed if sql.startswith(prefix)]


@dataclass
class Batch:
    published: int = 3
    embed_calls: int = 2
    embedded_chars: int = 1200
    embedded_chunks: int = 4
    cost_usd: float | None = 0.0001
    stopped_reason: str = ""
    outcomes: tuple = (("published", 3), ("policy_denied", 1))


@dataclass
class Result:
    batch: Batch
    inventory_count: int = 10
    unchanged_count: int = 6
    requested_count: int = 4
    migrations_failed: int = 0


def run(argv, conn, capsys, **kw):
    code = cli.main(argv, connect=lambda: conn, store_factory=lambda _: "store", **kw)
    return code, json.loads(capsys.readouterr().out)


def test_nothing_enabled_touches_no_model_or_archive(capsys):
    conn = FakeConn(models=())

    def boom(_):
        raise AssertionError("store must not open")

    code = cli.main(["--execute"], connect=lambda: conn, store_factory=boom,
                    providers={"x": object()})
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_OK
    assert out == {"modelCalls": 0, "result": "nothing_enabled"}
    assert not conn.statements("INSERT")


def test_plan_mode_writes_nothing_and_builds_no_provider(monkeypatch, capsys):
    conn = FakeConn(models=["m1"], price=0.02)
    seen = {}

    def fake_plan(store, *, model_revision, price_per_mtok, limits=None, sources=None):
        seen["args"] = (store, model_revision, price_per_mtok)
        return {"modelRevision": model_revision, "plannedChars": 10}

    monkeypatch.setattr(cli, "plan_model", fake_plan)
    import tybot.gateway.providers.openai_embeddings as oe

    monkeypatch.setattr(oe, "build_embedding_providers",
                        lambda: (_ for _ in ()).throw(AssertionError("no provider")))
    code, out = run([], conn, capsys)
    assert code == cli.EXIT_OK
    assert out["result"] == "planned" and out["modelCalls"] == 0
    assert seen["args"] == ("store", "m1", 0.02)
    assert not conn.statements("INSERT") and not conn.statements("UPDATE")


def test_plan_model_counts_and_estimates_cost_from_chars():
    text = "가나다라 마바사 " * 50
    source = VectorSource(
        workspace="tyit", channel_id="C1", doc_path="tyit/C1/2026-11-01.md",
        kind="channel", text=text, source_revision=source_revision(text),
    )
    report = cli.plan_model(None, model_revision="m1", price_per_mtok=2.0, sources=[source])
    assert report["readOnly"] is True and report["modelCalls"] == 0
    assert report["plannedDocuments"] == 1
    assert report["estimatedCostUsd"] == round(report["plannedChars"] / 1e6 * 2.0, 4)


def test_plan_without_price_reports_unknown_cost_not_zero():
    report = cli.plan_model(None, model_revision="m1", price_per_mtok=None, sources=[])
    assert report["estimatedCostUsd"] is None


def test_running_run_blocks_new_one(capsys):
    conn = FakeConn(models=["m1"], running=True)

    def never(*a, **k):
        raise AssertionError("must not run")

    with pytest.raises(cli.RunRefused) as info:
        cli.execute_model(conn, "store", model_revision="m1", trigger="timer",
                          requested_by="timer", providers={}, run_index=never)
    assert info.value.code == "already_running"
    assert not conn.statements("INSERT")
    # 죽은 실행 정리가 판정보다 먼저다
    order = [sql.split()[0] for sql, _ in conn.executed]
    assert order.index("UPDATE") < order.index("SELECT", 1)


def test_successful_run_closes_ledger_with_counts_only(capsys):
    conn = FakeConn(models=["m1"])
    calls = []

    def fake_run(connection, store, **kw):
        calls.append(kw)
        return Result(batch=Batch(outcomes=(("published", 3), ("Weird Code!", 2))))

    summary = cli.execute_model(conn, "store", model_revision="m1", trigger="console",
                                requested_by="dan@example.com", providers={"openai": 1},
                                run_index=fake_run)
    assert calls[0]["scan_complete"] is True
    assert calls[0]["model_revision"] == "m1"
    assert summary.error_code == "" and summary.published_count == 3
    # 모르는 모양의 코드는 그대로 나가지 않는다
    assert summary.outcomes == {"published": 3, "unknown": 2}
    (_sql, params), = conn.statements("UPDATE search_embedding_index_run SET state=%s")
    assert params[0] == "succeeded" and params[-1] == summary.run_id
    insert = conn.statements("INSERT")[0][1]
    assert insert == ("m1", "console", "dan@example.com")


def test_failure_keeps_body_out_of_output_and_ledger(monkeypatch, capsys):
    conn = FakeConn(models=["m1"])

    def explode(*a, **k):
        raise RuntimeError(SECRET)

    # main 경로 전체로 확인한다 — 실제 runtime 함수 자리를 바꿔 끼운다
    import tybot.embedding_index_runtime as runtime

    monkeypatch.setattr(runtime, "run_embedding_index", explode)
    code, out = run(["--execute"], conn, capsys, providers={"openai": object()})
    assert code == cli.EXIT_REFUSED
    assert out["runs"][0]["errorCode"] == "run_failed"
    dumped = json.dumps(out, ensure_ascii=False) + repr(conn.executed)
    assert SECRET not in dumped and "비밀" not in dumped
    (_sql, params), = conn.statements("UPDATE search_embedding_index_run SET state=%s")
    assert params[0] == "failed" and params[1] == "run_failed"


def test_migration_retire_failure_is_recorded_as_failed():
    conn = FakeConn(models=["m1"])
    summary = cli.execute_model(
        conn, "store", model_revision="m1", trigger="cli", requested_by="cli",
        providers={}, run_index=lambda *a, **k: Result(batch=Batch(), migrations_failed=1),
    )
    assert summary.error_code == "migration_retire_failed"


def test_invalid_explicit_model_revision_is_refused(capsys):
    conn = FakeConn()
    code, out = run(["--model-revision", "bad revision;--"], conn, capsys)
    assert code == cli.EXIT_REFUSED and out["code"] == "model_revision_invalid"


def test_invalid_limits_are_refused_before_db(capsys):
    def no_db():
        raise AssertionError("db must not open")

    code = cli.main(["--max-documents", "0"], connect=no_db)
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_REFUSED and out["code"] == "limits_invalid"


def test_missing_database_url_is_environment_error(capsys):
    code = cli.main([])
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_ENV and out["code"] == "database_unconfigured"


def test_db_error_text_never_printed(capsys):
    def broken():
        raise RuntimeError("postgresql://user:pw@host/db")

    code = cli.main([], connect=broken)
    out = capsys.readouterr().out
    assert code == cli.EXIT_ENV and "pw@" not in out


@pytest.mark.parametrize("value,expected", [
    ("published", "published"),
    ("x" * 81, "unknown"),
    ("has space", "unknown"),
    (None, "unknown"),
    ("/etc/passwd", "unknown"),
])
def test_safe_code(value, expected):
    assert cli.safe_code(value) == expected


def test_run_schema_is_applied_and_holds_no_body_columns():
    root = Path(__file__).resolve().parents[1]
    files = (root / "deploy" / "apply-schema.sh").read_text(encoding="utf-8")
    assert "search_embedding_run_schema.sql" in files
    sql = (root / "deploy" / "sql" / "search_embedding_run_schema.sql").read_text(encoding="utf-8")
    for forbidden in ("body", "text ", "question", "vector", "message "):
        columns = [line for line in sql.splitlines()
                   if line.startswith("    ") and not line.strip().startswith("--")
                   and line.split()[0] == forbidden.strip()]
        assert not columns, forbidden


def test_external_provider_declared_local_stops_every_run(capsys):
    """`local` 이면 외부 승인을 안 보는 판정을 사실로 덮는다 — 실행 전체를 멈춘다."""
    conn = FakeConn(models=["m1"], local_rows=[("tyit", "openai")])

    def never(*a, **k):
        raise AssertionError("store must not open")

    code = cli.main(["--execute"], connect=lambda: conn, store_factory=never,
                    providers={"openai": object()})
    out = json.loads(capsys.readouterr().out)
    assert code == cli.EXIT_REFUSED
    assert out["code"] == "transport_misdeclared" and out["workspaces"] == ["tyit"]
    assert not conn.statements("INSERT")


def test_local_policy_for_unknown_local_provider_is_not_flagged(capsys):
    conn = FakeConn(models=(), local_rows=[("tyit", "local-model-server")])
    code, out = run(["--execute"], conn, capsys, providers={})
    assert code == cli.EXIT_OK and out["result"] == "nothing_enabled"
