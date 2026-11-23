"""채널×일 다이제스트 배치 (B-93 2단계 §7-3). 먼저 세고, 상한 안에서만 만든다."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot import channel_digest as cd
from tybot import channel_digest_batch as batch
from tybot.archive import writer

WS, CH = "mgmt", "C0PLAN0001"
ROOT = Path(__file__).resolve().parents[1]


def _archive(root: Path) -> None:
    for day in (9, 10, 11):
        writer.ingest(
            root, workspace=WS, channel="#팀-기획_abb110-사업계획", channel_id=CH, acl=["#x"],
            messages=[writer.IncomingMessage(datetime(2026, 11, day, 1, tzinfo=UTC), "담당", f"부채비율 {day}")],
        )


class _Conn:
    def __init__(self, current=None):
        self.current = current or {}
        self.sql = []
        self.commits = 0

    def cursor(self):
        conn = self

        class C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=()):
                conn.sql.append((" ".join(sql.split()), params))

            def fetchall(self):
                return [{"day": d, "revision": r} for d, r in conn.current.items()]

        return C()

    def commit(self):
        self.commits += 1


def test_plan_counts_without_writing_and_skips_unchanged_days(tmp_path):
    _archive(tmp_path)
    days = cd.collect_channel_days(tmp_path, WS, CH)
    unchanged = {days[0].day: cd.revision(days[0], model=batch.DIGEST_MODEL)}
    conn = _Conn(unchanged)
    p = batch.plan(conn, tmp_path, [(WS, CH)], since=date(2026, 11, 9), until=date(2026, 11, 11))
    assert (p.channels, p.days_seen, p.unchanged, p.calls) == (1, 3, 1, 2)
    assert p.est_input_tokens > 0
    assert all(sql.startswith("SELECT") for sql, _ in conn.sql)  # 세기만 한다


def test_plan_respects_the_date_window(tmp_path):
    _archive(tmp_path)
    p = batch.plan(_Conn(), tmp_path, [(WS, CH)], since=date(2026, 11, 10), until=date(2026, 11, 10))
    assert [d.day for d in p.todo] == [date(2026, 11, 10)]


def test_nightly_window_excludes_today():
    assert batch.nightly_window(date(2026, 11, 12)) == (date(2026, 11, 9), date(2026, 11, 11))


class _Router:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def complete(self, messages, **kw):
        self.calls += 1
        return SimpleNamespace(text=self.outputs.pop(0), model="m", provider="p", cost_usd=0.01)


def _ok(n):
    return json.dumps({"body": f"부채비율 {n} 논의", "terms": ["재무구조"]}, ensure_ascii=False)


def test_run_stops_at_max_calls_and_commits_each_day(tmp_path):
    _archive(tmp_path)
    p = batch.plan(_Conn(), tmp_path, [(WS, CH)], since=date(2026, 11, 9), until=date(2026, 11, 11))
    router = _Router([_ok(9), "엉터리 출력"])
    conn = _Conn()
    stats = batch.run(conn, p, router, max_calls=2)
    assert router.calls == 2
    assert stats.made == 1 and stats.rejected == {"digest_shape": 1}
    assert stats.skipped_over_cap == 1
    assert conn.commits == 1
    assert round(stats.cost_usd, 2) == 0.02


def test_dsn_must_be_the_dedicated_role(tmp_path, monkeypatch):
    f = tmp_path / "dsn"
    monkeypatch.setenv(batch.DSN_FILE_ENV, str(f))
    f.write_text("postgresql://tybot_digest_runtime:pw@127.0.0.1:55432/tyslackai\n", encoding="utf-8")
    if hasattr(__import__("os"), "chmod"):
        f.chmod(0o600)
    assert batch.resolve_dsn().startswith("postgresql://tybot_digest_runtime")
    f.write_text("postgresql://tyslackai:pw@127.0.0.1:55432/tyslackai\n", encoding="utf-8")
    with pytest.raises(batch.DigestDsnError) as err:
        batch.resolve_dsn()
    assert err.value.code == "digest_database_wrong_role"
    monkeypatch.setenv(batch.DSN_FILE_ENV, str(tmp_path / "missing"))
    with pytest.raises(batch.DigestDsnError):
        batch.resolve_dsn()


def test_units_use_the_dedicated_dsn_and_the_timer_is_not_enabled_by_install():
    unit = (ROOT / "deploy/tybot-channel-digest.service").read_text(encoding="utf-8")
    assert "TYBOT_DIGEST_DATABASE_URL_FILE=/etc/tybot/tybot-digest-database-url" in unit
    assert "DATABASE_URL=" not in unit.replace("TYBOT_DIGEST_DATABASE_URL_FILE=", "")
    assert "ReadOnlyPaths=/var/lib/tybot/archive" in unit
    for name in ("install.sh", "update.sh"):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "enable --now tybot-channel-digest" not in text, name


def test_schema_is_applied_after_bot_source():
    order = (ROOT / "deploy/apply-schema.sh").read_text(encoding="utf-8")
    assert order.index("bot_source_schema.sql") < order.index("channel_day_digest_schema.sql")
    sql = (ROOT / "deploy/sql/channel_day_digest_schema.sql").read_text(encoding="utf-8")
    assert "GRANT SELECT ON channel_day_digest TO tyslackai;" in sql
    assert "GRANT SELECT (bot, workspace, channel_id, removed_at) ON bot_source_channel TO tybot_digest;" in sql
    assert "TO tyslackai" not in sql.replace("GRANT SELECT ON channel_day_digest TO tyslackai;", "")
