"""변환 런타임 정책의 DB 정본 이전 (B-89 후속).

거는 것은 하나다 — **모르는 것을 「꺼짐」 으로 읽지 않는다.** DB 미접속·표 없음·
행 없음·잘못된 값에서 TYBot 이 HWP 를 직접 변환하거나 야간 소급이 돌면 안 된다.
그리고 전환 기간 fallback 은 DB 값이 있으면 절대 이기지 못한다.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from tybot import conversion_policy as cp
from tybot import conversion_recheck as recheck
from tybot.archive import convert as convert_module
from tybot.archive import converter_bots

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

APP = "A0RHWP00001"
OTHER = "A0RHWP00002"


@pytest.fixture(autouse=True)
def _real_resolution(monkeypatch, tmp_path):
    """공용 conftest 의 고정 정책을 걷고, 옛 설정이 시험 밖에서 새어 들지 않게 한다."""
    cp.set_override(None)
    monkeypatch.setenv("ENV_SETTINGS_PATH", str(tmp_path / "managed.env"))
    monkeypatch.setattr(converter_bots, "PRODUCTION_MANAGED_ENV", tmp_path / "absent.env")
    for key in (cp.LEGACY_APPS_ENV, cp.LEGACY_LIMIT_ENV, cp.LEGACY_GRACE_ENV, cp.LEGACY_SWITCH_ENV):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield
    cp.set_override(None)


def _row(**over) -> dict:
    row = {
        "rhwp_app_ids": [APP],
        "recheck_limit": 40,
        "rhwp_grace_hours": 8,
        "revision": 3,
        "updated_by": "owner@taeyoung.com",
        "updated_at": datetime(2026, 11, 11, 9, tzinfo=UTC),
    }
    row.update(over)
    return row


def _raises(reason):
    def fetch():
        raise cp.PolicyUnavailable(reason)

    return fetch


def _assert_fail_closed(policy):
    assert policy.delegation != cp.DELEGATION_OFF
    assert policy.direct_conversion_allowed("hwp") is False
    assert policy.direct_conversion_allowed("HWPX") is False
    assert policy.direct_conversion_allowed("pdf") is True
    assert policy.nightly_apply_allowed is False


# --- DB 정본 ------------------------------------------------------------------


def test_a_valid_row_is_the_single_source_of_truth():
    policy = cp.resolve(fetch=lambda: _row(), env={})
    assert policy.state == cp.STATE_DB
    assert policy.app_ids == frozenset({APP})
    assert (policy.recheck_limit, policy.rhwp_grace_hours, policy.revision) == (40, 8, 3)
    assert set(policy.sources.values()) == {cp.SOURCE_DB}
    assert policy.delegation == cp.DELEGATION_ON
    assert policy.nightly_apply_allowed is True
    assert policy.direct_conversion_allowed("hwp") is False


def test_an_empty_list_in_the_db_is_the_only_way_to_switch_delegation_off():
    policy = cp.resolve(fetch=lambda: _row(rhwp_app_ids=[]), env={})
    assert policy.delegation == cp.DELEGATION_OFF
    assert policy.direct_conversion_allowed("hwp") is True
    assert policy.nightly_apply_allowed is True


def test_the_db_wins_over_legacy_values_when_a_row_exists():
    env = {cp.LEGACY_APPS_ENV: OTHER, cp.LEGACY_LIMIT_ENV: "7"}
    policy = cp.resolve(fetch=lambda: _row(rhwp_app_ids=[]), env=env)
    assert policy.state == cp.STATE_DB
    assert policy.app_ids == frozenset()
    assert policy.recheck_limit == 40


# --- 모르면 꺼짐이 아니다 ------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [cp.DB_UNCONFIGURED, cp.DB_UNAVAILABLE, cp.TABLE_MISSING],
)
def test_db_failures_without_legacy_values_are_unknown(reason):
    policy = cp.resolve(fetch=_raises(reason), env={})
    assert policy.state == cp.STATE_UNKNOWN
    assert policy.reason == reason
    _assert_fail_closed(policy)
    assert policy.to_json()["recheckLimit"] is None


def test_delegation_off_needs_the_db_state_even_for_hand_built_policies():
    """`off` 는 DB 정본의 빈 목록에서만 나온다. 다른 상태의 빈 목록·앱 ID 는 모름이다."""
    assert cp.ConversionPolicy(state=cp.STATE_LEGACY).delegation == cp.DELEGATION_UNKNOWN
    odd = cp.ConversionPolicy(state=cp.STATE_UNKNOWN, app_ids=frozenset({APP}))
    assert odd.delegation == cp.DELEGATION_UNKNOWN
    assert odd.direct_conversion_allowed("hwp") is False


def test_a_missing_row_is_unknown_not_off():
    policy = cp.resolve(fetch=lambda: None, env={})
    assert (policy.state, policy.reason) == (cp.STATE_UNKNOWN, cp.ROW_MISSING)
    _assert_fail_closed(policy)


@pytest.mark.parametrize(
    "over",
    [
        {"rhwp_app_ids": ["rhwp"]},  # 형식이 틀린 ID
        {"rhwp_app_ids": [APP.lower()]},  # 정규화 안 된 값(손으로 고친 흔적)
        {"rhwp_app_ids": [APP, APP]},  # 중복
        {"rhwp_app_ids": APP},  # 배열이 아니다
        {"rhwp_app_ids": None},
        {"recheck_limit": -1},
        {"recheck_limit": 1001},
        {"recheck_limit": True},
        {"recheck_limit": "50"},
        {"rhwp_grace_hours": 0},
        {"rhwp_grace_hours": 169},
        {"rhwp_grace_hours": 6.5},
        {"revision": 0},
        {"revision": None},
    ],
)
def test_invalid_db_values_are_unknown_and_never_fall_back_to_legacy(over, caplog):
    # 옛 설정이 멀쩡히 있어도 DB 가 틀리면 그쪽으로 내려가지 않는다.
    env = {cp.LEGACY_APPS_ENV: OTHER}
    with caplog.at_level(logging.ERROR, logger="tybot.conversion_policy"):
        policy = cp.resolve(fetch=lambda: _row(**over), env=env)
    assert (policy.state, policy.reason) == (cp.STATE_UNKNOWN, cp.INVALID_VALUE)
    assert policy.app_ids == frozenset()
    _assert_fail_closed(policy)
    assert "변환 정책 DB 값이 틀렸습니다" in caplog.text


def test_a_fetch_error_of_an_unexpected_kind_is_unavailable_not_off():
    class Boom:
        def execute(self, _sql):
            raise RuntimeError("connection reset")

        def close(self):
            pass

    with pytest.raises(cp.PolicyUnavailable) as caught:
        cp.fetch_row(Boom())
    assert caught.value.reason == cp.DB_UNAVAILABLE


def test_an_undefined_table_is_reported_as_table_missing():
    class UndefinedTable(Exception):
        sqlstate = "42P01"

    class Conn:
        def execute(self, _sql):
            raise UndefinedTable()

    with pytest.raises(cp.PolicyUnavailable) as caught:
        cp.fetch_row(Conn())
    assert caught.value.reason == cp.TABLE_MISSING


def test_no_database_url_is_unknown(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    policy = cp.resolve(env={})
    assert (policy.state, policy.reason) == (cp.STATE_UNKNOWN, cp.DB_UNCONFIGURED)


# --- 전환 기간 fallback --------------------------------------------------------


def test_legacy_env_is_read_only_while_the_db_cannot_answer():
    env = {
        cp.LEGACY_APPS_ENV: f" {APP.lower()} ",
        cp.LEGACY_LIMIT_ENV: "12",
        cp.LEGACY_GRACE_ENV: "6.0",
    }
    policy = cp.resolve(fetch=lambda: None, env=env)
    assert policy.state == cp.STATE_LEGACY
    assert policy.reason == cp.ROW_MISSING
    assert policy.app_ids == frozenset({APP})
    assert (policy.recheck_limit, policy.rhwp_grace_hours) == (12, 6)
    assert policy.sources == {
        "appIds": cp.SOURCE_LEGACY_ENV,
        "recheckLimit": cp.SOURCE_LEGACY_ENV,
        "rhwpGraceHours": cp.SOURCE_LEGACY_ENV,
    }
    # 옛 설정으로는 위임이 켜진 쪽만 말할 수 있다. 직접 변환도 야간 apply 도 없다.
    assert policy.delegation == cp.DELEGATION_ON
    _assert_fail_closed(policy)


def test_the_legacy_console_file_wins_over_the_legacy_environment(tmp_path):
    (tmp_path / "managed.env").write_text(f'{cp.LEGACY_APPS_ENV}="{OTHER}"\n', encoding="utf-8")
    policy = cp.resolve(fetch=_raises(cp.DB_UNAVAILABLE), env={cp.LEGACY_APPS_ENV: APP})
    assert policy.app_ids == frozenset({OTHER})
    assert policy.sources["appIds"] == cp.SOURCE_LEGACY_FILE
    assert policy.sources["recheckLimit"] == cp.SOURCE_DEFAULT
    assert policy.recheck_limit == cp.DEFAULT_RECHECK_LIMIT


@pytest.mark.parametrize(
    "env",
    [
        {},  # 옛 설정도 없다
        {cp.LEGACY_APPS_ENV: ""},  # 옛 「빈 값 = 꺼짐」 은 이제 모름이다
        {cp.LEGACY_APPS_ENV: f"{APP}, rhwp"},  # 하나라도 틀리면 전체가 모름
        {cp.LEGACY_APPS_ENV: APP, cp.LEGACY_LIMIT_ENV: "x"},
        {cp.LEGACY_APPS_ENV: APP, cp.LEGACY_GRACE_ENV: "0"},
        {cp.LEGACY_APPS_ENV: APP, cp.LEGACY_SWITCH_ENV: "0"},  # 전환 종료
    ],
)
def test_legacy_values_that_cannot_prove_delegation_are_unknown(env):
    policy = cp.resolve(fetch=lambda: None, env=env)
    assert (policy.state, policy.reason) == (cp.STATE_UNKNOWN, cp.ROW_MISSING)
    _assert_fail_closed(policy)


# --- 캐시·로그 -----------------------------------------------------------------


def test_current_is_cached_and_logs_the_source_once_per_change(monkeypatch, caplog):
    rows = iter([_row(), _row(rhwp_app_ids=[], revision=4)])
    monkeypatch.setattr(cp, "fetch_row", lambda: next(rows))
    monkeypatch.setattr(cp, "_last_logged", None)
    cp.clear_cache()
    with caplog.at_level(logging.INFO, logger="tybot.conversion_policy"):
        assert cp.current(now=0.0).revision == 3
        assert cp.current(now=30.0).revision == 3  # 캐시
        assert cp.current(now=61.0).revision == 4
    messages = [r.getMessage() for r in caplog.records if "변환 정책 state=" in r.getMessage()]
    assert len(messages) == 2
    assert "state=db" in messages[0] and "appIds=db" in messages[0]


def test_an_unknown_policy_is_logged_as_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(cp, "fetch_row", _raises(cp.DB_UNAVAILABLE))
    monkeypatch.setattr(cp, "_last_logged", None)
    cp.clear_cache()
    with caplog.at_level(logging.WARNING, logger="tybot.conversion_policy"):
        cp.current(now=0.0)
    assert any(
        r.levelno == logging.WARNING
        and "state=unknown" in r.getMessage()
        and "HWP 직접 변환" in r.getMessage()
        for r in caplog.records
    )


# --- 저장 검증 -----------------------------------------------------------------


def test_validate_accepts_only_clean_numbers_and_ids():
    assert cp.validate([f" {APP.lower()} ", APP, ""], 0, 1) == ([APP], 0, 1)
    assert cp.validate([], 1000, 168) == ([], 1000, 168)
    for args in (
        (["rhwp"], 50, 6),
        ([APP], True, 6),
        ([APP], 50, 0),
        ([APP], 1001, 6),
        ([APP], 50, "6시간"),
        (None, 50, 6),
        ([f"A0RHWP{i:05d}" for i in range(cp.MAX_APP_IDS + 1)], 50, 6),
    ):
        with pytest.raises(cp.PolicyInvalid):
            cp.validate(*args)


class _FakeDb:
    """`save()` 가 쓰는 만큼만 흉내 낸다. 행 하나와 감사 목록."""

    def __init__(self, row=None):
        self.row = dict(row) if row else None
        self.audit: list[dict] = []
        self.closed = False

    @contextlib.contextmanager
    def transaction(self):
        snapshot = (dict(self.row) if self.row else None, list(self.audit))
        try:
            yield
        except Exception:
            self.row, self.audit = snapshot
            raise

    def execute(self, sql, params=None):
        db = self
        text = " ".join(sql.split())

        class Result:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        if text.startswith("SELECT") and "FOR UPDATE" in text:
            return Result(dict(db.row) if db.row else None)
        if text.startswith("UPDATE"):
            if not db.row or db.row["revision"] != params["expected"]:
                return Result(None)
            db.row.update(
                rhwp_app_ids=params["ids"],
                recheck_limit=params["limit"],
                rhwp_grace_hours=params["grace"],
                revision=db.row["revision"] + 1,
                updated_by=params["actor"],
                updated_at=datetime(2026, 11, 11, tzinfo=UTC),
            )
            return Result(dict(db.row))
        if text.startswith(f"INSERT INTO {cp.TABLE} "):
            if db.row:
                return Result(None)
            db.row = {
                "rhwp_app_ids": params["ids"],
                "recheck_limit": params["limit"],
                "rhwp_grace_hours": params["grace"],
                "revision": 1,
                "updated_by": params["actor"],
                "updated_at": datetime(2026, 11, 11, tzinfo=UTC),
            }
            return Result(dict(db.row))
        if text.startswith(f"INSERT INTO {cp.AUDIT_TABLE}"):
            db.audit.append(dict(params))
            return Result(None)
        raise AssertionError(text)


def test_the_first_save_creates_the_row_and_an_audit_entry():
    pytest.importorskip("psycopg")
    db = _FakeDb()
    saved = cp.save(
        app_ids=[APP.lower()],
        recheck_limit=20,
        rhwp_grace_hours=4,
        actor="owner@taeyoung.com",
        expected_revision=0,
        reason="전환",
        conn=db,
    )
    assert saved.state == cp.STATE_DB and saved.revision == 1
    assert db.row["rhwp_app_ids"] == [APP]
    (entry,) = db.audit
    assert entry["revision"] == 1 and entry["previous"] is None
    assert entry["actor"] == "owner@taeyoung.com" and entry["reason"] == "전환"


def test_an_update_records_the_previous_values():
    pytest.importorskip("psycopg")
    db = _FakeDb(_row())
    saved = cp.save(
        app_ids=[],
        recheck_limit=10,
        rhwp_grace_hours=6,
        actor="owner@taeyoung.com",
        expected_revision=3,
        conn=db,
    )
    assert saved.revision == 4 and saved.delegation == cp.DELEGATION_OFF
    (entry,) = db.audit
    assert entry["previous"].obj == {"rhwpAppIds": [APP], "recheckLimit": 40, "rhwpGraceHours": 8}


def test_a_stale_revision_is_a_conflict_and_writes_nothing():
    pytest.importorskip("psycopg")
    db = _FakeDb(_row())
    with pytest.raises(cp.PolicyConflict, match="현재 판 3"):
        cp.save(
            app_ids=[],
            recheck_limit=10,
            rhwp_grace_hours=6,
            actor="owner@taeyoung.com",
            expected_revision=2,
            conn=db,
        )
    assert db.row["revision"] == 3 and db.audit == []


def test_invalid_input_never_reaches_the_database():
    db = _FakeDb(_row())
    with pytest.raises(cp.PolicyInvalid):
        cp.save(
            app_ids=["rhwp"],
            recheck_limit=10,
            rhwp_grace_hours=6,
            actor="owner@taeyoung.com",
            expected_revision=3,
            conn=db,
        )
    with pytest.raises(cp.PolicyInvalid):
        cp.save(
            app_ids=[APP],
            recheck_limit=10,
            rhwp_grace_hours=6,
            actor="",
            expected_revision=3,
            conn=db,
        )
    assert db.row["revision"] == 3 and db.audit == []


# --- 수집·변환 경로 ------------------------------------------------------------


def test_the_isolated_worker_checks_only_the_handler(monkeypatch, tmp_path):
    """워커는 DB 에 닿지 않는다. 정책을 보면 늘 「모름」 이 되어 아무것도 변환 못 한다."""
    cp.set_override(cp.unknown(cp.DB_UNCONFIGURED))
    assert convert_module.can_convert("hwp") is False
    assert convert_module.has_handler("hwp") is True
    source = (ROOT / "src/tybot/archive/conversion_worker.py").read_text(encoding="utf-8")
    assert "can_convert" not in source and "has_handler(suffix)" in source


def test_question_hwp_asks_for_pdf_while_the_policy_is_unknown():
    from tybot import question_files

    cp.set_override(cp.unknown(cp.ROW_MISSING))

    def loader(*_args):
        raise AssertionError("HWP must not be downloaded while delegation is unknown")

    got = question_files.read_question_files(
        [
            {
                "id": "F01234561",
                "name": "주간보고.hwp",
                "filetype": "hwp",
                "size": 1024,
                "url_private_download": "https://files.slack.com/files-pri/T1-F1/a.hwp",
                "permalink": "https://tyit.slack.com/files/U1/F01234561",
            }
        ],
        "xoxb-fixture",
        loader=loader,
    )
    assert got.codes == (question_files.HWP_USE_PDF_CODE,)


def test_channel_app_status_says_unknown_not_not_applicable(monkeypatch):
    from tybot import channel_app_probe
    from tybot import channel_apps as ca

    binding = ca.converter_binding([], delegated=None)
    assert binding.required is True and binding.missing_code == ca.POLICY_UNKNOWN
    status = ca.evaluate_one(binding, ca.ChannelAppFacts(member_ids=frozenset()))
    assert status.state == ca.UNKNOWN
    assert "변환 정책" in status.fix

    cp.set_override(cp.unknown(cp.DB_UNAVAILABLE))
    monkeypatch.setattr(channel_app_probe, "_connections", lambda _ws: {})
    monkeypatch.setattr(channel_app_probe, "_hermes_route_mode", lambda _ws: "disabled")
    rhwp = next(b for b in channel_app_probe.bindings("tyit") if b.role == ca.RHWP)
    assert rhwp.missing_code == ca.POLICY_UNKNOWN


# --- 야간 재점검 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    [
        cp.unknown(cp.DB_UNAVAILABLE),
        cp.unknown(cp.ROW_MISSING),
        cp.unknown(cp.INVALID_VALUE),
        cp.legacy_policy(cp.ROW_MISSING, {cp.LEGACY_APPS_ENV: APP}),
    ],
)
def test_nightly_apply_is_refused_unless_the_policy_comes_from_the_db(
    tmp_path, monkeypatch, capsys, policy
):
    import drain_conversion_queue as drain

    from tybot import conversion_queue as queue

    cp.set_override(policy)
    monkeypatch.setattr(queue, "enqueue", lambda **_kw: pytest.fail("must not enqueue"))
    monkeypatch.setattr(drain, "drain", lambda *_a, **_k: pytest.fail("must not drain"))
    monkeypatch.setattr(drain, "nightly_backfill", lambda *_a, **_k: pytest.fail("no steps"))

    assert drain.run_nightly(str(tmp_path / "archive"), apply=True, limit=5) == drain.EXIT_POLICY

    saved = recheck.read_report(tmp_path / "archive")
    assert saved["exit_code"] == drain.EXIT_POLICY
    assert saved["blocked_reason"].startswith(f"policy_{policy.state}")
    assert saved["policy"]["state"] == policy.state
    assert saved["policy"]["nightlyApplyAllowed"] is False
    captured = capsys.readouterr()
    assert "야간 apply 를 거절합니다" in captured.err
    assert "변환 정책: state=" in captured.out


def test_nightly_dry_run_still_previews_with_an_unknown_policy(tmp_path, monkeypatch, capsys):
    import drain_conversion_queue as drain

    from tybot import conversion_queue as queue

    cp.set_override(cp.unknown(cp.ROW_MISSING))
    monkeypatch.setattr(queue, "enqueue", lambda **_kw: pytest.fail("dry run"))
    monkeypatch.setattr(drain, "drain", lambda *_a, **_k: pytest.fail("dry run"))

    assert drain.run_nightly(str(tmp_path / "archive"), apply=False, limit=5) == drain.EXIT_OK
    out = capsys.readouterr().out
    assert "state=unknown reason=row_missing" in out
    assert "`--apply` 는 거절됩니다" in out
    assert "rhwp 위임 상태 모름" in out
    metrics = json.loads(out.split("convert_nightly_metrics ", 1)[1].splitlines()[0])
    # 모르는 상한으로 소급 분량을 세지 않는다.
    assert metrics["recheck_limit"] == 0
    assert not recheck.report_path(tmp_path / "archive").exists()


# --- readiness -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "code"),
    [
        (cp.from_db([APP]), 0),
        (cp.legacy_policy(cp.ROW_MISSING, {cp.LEGACY_APPS_ENV: APP}), 3),
        (cp.unknown(cp.TABLE_MISSING), 1),
    ],
)
def test_readiness_exit_codes(monkeypatch, capsys, policy, code):
    import check_conversion_policy as readiness

    monkeypatch.setattr(readiness, "load_env_file", lambda: None)
    monkeypatch.setattr(cp, "resolve", lambda: policy)
    assert readiness.main(["--json"]) == code
    data = json.loads(capsys.readouterr().out)
    assert data["exitCode"] == code
    assert data["policy"]["state"] == policy.state
    assert readiness.main([]) == code
    text = capsys.readouterr().out
    assert "야간 apply" in text
    if code == 0:
        assert "저절로 켜지지 않습니다" in text


def test_readiness_never_prints_secrets(monkeypatch, capsys):
    import check_conversion_policy as readiness

    monkeypatch.setattr(readiness, "load_env_file", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:supersecret@db/x")
    monkeypatch.setattr(cp, "resolve", lambda: cp.unknown(cp.DB_UNAVAILABLE))
    readiness.main([])
    assert "supersecret" not in capsys.readouterr().out


# --- 콘솔 API ------------------------------------------------------------------


def _routes(**deps):
    from tybot.console import app as console_app
    from tybot.console import conversion_policy_routes as routes

    audited: list[dict] = []
    router = routes.build_router(
        current_user=console_app.current_user,
        require_admin=console_app._require_admin,
        check_write_request=console_app._check_write_request,
        audit_event=deps.pop("audit_event", lambda **kw: audited.append(kw)),
        resolve=deps.pop("resolve", lambda: cp.from_db([APP], revision=3)),
        history=deps.pop("history", lambda _n: []),
        legacy=deps.pop(
            "legacy",
            lambda: {"appIds": "", "appIdsSource": "", "recheckLimit": "", "rhwpGraceHours": ""},
        ),
        **deps,
    )
    endpoints = {(next(iter(r.methods)), r.path): r.endpoint for r in router.routes}
    return endpoints, audited


def _user(*, role="admin", all_workspaces=True):
    from tybot.console.auth import ConsoleUser

    return ConsoleUser(
        email="owner@taeyoung.com",
        name="운영자",
        role=role,
        workspaces=frozenset({"tyit"}),
        all_workspaces=all_workspaces,
    )


def _request(csrf="1") -> Request:
    headers = [(b"host", b"console.example"), (b"origin", b"https://console.example")]
    if csrf:
        headers.append((b"x-tybot-csrf", csrf.encode()))
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "scheme": "https",
            "path": "/",
            "headers": headers,
            "server": ("console.example", 443),
            "client": ("127.0.0.1", 1),
        }
    )


def _body(**over):
    from tybot.console import conversion_policy_routes as routes

    base = {
        "rhwpAppIds": [APP],
        "recheckLimit": 30,
        "rhwpGraceHours": 6,
        "expectedRevision": 3,
        "reason": "DB 정본 전환",
    }
    return routes.ConversionPolicyBody(**(base | over))


def test_get_shows_state_sources_and_limits_without_secrets():
    endpoints, _ = _routes()
    view = endpoints[("GET", "/api/conversion-policy")](user=_user())
    assert view["policy"]["state"] == "db"
    assert view["policy"]["sources"]["appIds"] == "db"
    assert view["limits"]["recheckLimit"] == [0, 1000]
    assert "저절로" not in view["timerNote"] and "켜지지 않습니다" in view["timerNote"]
    assert not re.search(r"xox[abp]-|sk-|password|token", json.dumps(view, ensure_ascii=False))


def test_get_still_renders_when_the_db_is_down():
    def broken(_n):
        raise cp.PolicyUnavailable(cp.DB_UNAVAILABLE)

    endpoints, _ = _routes(resolve=lambda: cp.unknown(cp.DB_UNAVAILABLE), history=broken)
    view = endpoints[("GET", "/api/conversion-policy")](user=_user())
    assert view["policy"]["state"] == "unknown"
    assert view["historyError"] == "PolicyUnavailable"


def test_get_and_put_require_an_admin():
    endpoints, _ = _routes(save=lambda **_kw: pytest.fail("must not save"))
    with pytest.raises(HTTPException) as caught:
        endpoints[("GET", "/api/conversion-policy")](user=_user(role="developer"))
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        endpoints[("PUT", "/api/conversion-policy")](
            body=_body(), request=_request(), user=_user(role="developer")
        )
    assert caught.value.status_code == 403


def test_put_requires_all_workspaces_and_csrf():
    endpoints, _ = _routes(save=lambda **_kw: pytest.fail("must not save"))
    put = endpoints[("PUT", "/api/conversion-policy")]
    with pytest.raises(HTTPException) as caught:
        put(body=_body(), request=_request(), user=_user(all_workspaces=False))
    assert caught.value.status_code == 403
    with pytest.raises(HTTPException) as caught:
        put(body=_body(), request=_request(csrf=""), user=_user())
    assert caught.value.status_code == 403


def test_put_body_rejects_unknown_fields_and_missing_reason():
    with pytest.raises(ValidationError):
        _body(actor="someone@else")
    with pytest.raises(ValidationError):
        _body(slackToken="xoxb-1")
    with pytest.raises(ValidationError):
        _body(reason="")
    with pytest.raises(ValidationError):
        _body(expectedRevision=-1)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (cp.PolicyInvalid("틀림"), 422),
        (cp.PolicyConflict("먼저 저장됨"), 409),
        (cp.PolicyUnavailable(cp.TABLE_MISSING), 503),
    ],
)
def test_put_maps_store_errors(error, status):
    def save(**_kw):
        raise error

    endpoints, audited = _routes(save=save)
    with pytest.raises(HTTPException) as caught:
        endpoints[("PUT", "/api/conversion-policy")](body=_body(), request=_request(), user=_user())
    assert caught.value.status_code == status
    assert audited == []


def test_put_saves_with_the_logged_in_actor_and_audits():
    seen = {}

    def save(**kwargs):
        seen.update(kwargs)
        return cp.from_db([APP], recheck_limit=30, revision=4, updated_by=kwargs["actor"])

    endpoints, audited = _routes(save=save)
    result = endpoints[("PUT", "/api/conversion-policy")](
        body=_body(), request=_request(), user=_user()
    )
    assert seen["actor"] == "owner@taeyoung.com"
    assert seen["expected_revision"] == 3 and seen["reason"] == "DB 정본 전환"
    assert result["saved"]["revision"] == 4 and result["audited"] is True
    (event,) = audited
    assert event["action"] == "policy_change" and event["metadata"]["revision"] == 4


def test_put_reports_an_audit_failure_instead_of_failing_the_save():
    def audit_event(**_kw):
        raise RuntimeError("audit down")

    endpoints, _ = _routes(
        audit_event=audit_event, save=lambda **_kw: cp.from_db([APP], revision=4)
    )
    result = endpoints[("PUT", "/api/conversion-policy")](
        body=_body(), request=_request(), user=_user()
    )
    assert result["audited"] is False


def test_the_router_is_registered_in_the_console_app():
    from tybot.console import app as console_app

    assert console_app.app.url_path_for("get_conversion_policy") == "/api/conversion-policy"
    assert console_app.app.url_path_for("put_conversion_policy") == "/api/conversion-policy"


# --- 배포 ----------------------------------------------------------------------


def _code(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_deploy_never_restarts_the_nightly_timer():
    update = _code((ROOT / "deploy/update.sh").read_text(encoding="utf-8"))
    assert "tybot-convert-retry.timer" in update
    assert "tybot-convert-nightly" not in update


def test_the_run_time_stays_in_systemd_not_in_the_db():
    timer = (ROOT / "deploy/tybot-convert-nightly.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 03:20:00" in timer
    sql = (ROOT / "deploy/sql/conversion_policy_schema.sql").read_text(encoding="utf-8")
    assert "03:20" not in _code(sql.replace("--", "#")) and "calendar" not in sql.lower()


def test_schema_ships_no_default_row_and_is_applied_after_the_queue():
    sql = (ROOT / "deploy/sql/conversion_policy_schema.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    assert "INSERT INTO" not in code.upper()
    assert "CHECK (id = 1)" in code
    assert "recheck_limit BETWEEN 0 AND 1000" in code
    assert "rhwp_grace_hours BETWEEN 1 AND 168" in code
    files = re.findall(
        r"^\s+([a-z_]+\.sql)",
        (ROOT / "deploy/apply-schema.sh").read_text(encoding="utf-8"),
        flags=re.M,
    )
    assert files.index("conversion_queue_schema.sql") < files.index("conversion_policy_schema.sql")


def test_schema_ranges_match_the_code():
    sql = (ROOT / "deploy/sql/conversion_policy_schema.sql").read_text(encoding="utf-8")
    low, high = cp.RECHECK_LIMIT_RANGE
    assert f"recheck_limit BETWEEN {low} AND {high}" in sql
    low, high = cp.RHWP_GRACE_RANGE
    assert f"rhwp_grace_hours BETWEEN {low} AND {high}" in sql
    assert f"<= {cp.MAX_APP_IDS}" in sql


# --- 폐기 가능한 전용 DB --------------------------------------------------------

_DSN = os.getenv("TEST_DATABASE_URL") or os.getenv("TYBOT_SCHEMA_TEST_DSN")


@pytest.mark.skipif(not _DSN, reason="전용 시험 DB 가 없다")
def test_on_a_disposable_database_the_policy_round_trips():
    """**폐기 가능한 전용 DB 에서만** 돈다. 운영 DB 는 쓰지 않는다."""
    import psycopg

    sql = (ROOT / "deploy/sql/conversion_policy_schema.sql").read_text(encoding="utf-8")
    schema = "b89_conversion_policy_test"
    with psycopg.connect(_DSN, autocommit=True, row_factory=psycopg.rows.dict_row) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute(f"CREATE SCHEMA {schema}")
        try:
            conn.execute(f"SET search_path TO {schema}")
            with pytest.raises(cp.PolicyUnavailable) as caught:
                cp.fetch_row(conn)
            assert caught.value.reason == cp.TABLE_MISSING
            conn.execute(sql)
            assert cp.fetch_row(conn) is None  # 행을 미리 넣지 않는다
            saved = cp.save(
                app_ids=[APP],
                recheck_limit=20,
                rhwp_grace_hours=4,
                actor="owner@taeyoung.com",
                expected_revision=0,
                conn=conn,
            )
            assert saved.revision == 1
            again = cp.save(
                app_ids=[],
                recheck_limit=0,
                rhwp_grace_hours=6,
                actor="owner@taeyoung.com",
                expected_revision=1,
                conn=conn,
            )
            assert again.delegation == cp.DELEGATION_OFF
            with pytest.raises(cp.PolicyConflict):
                cp.save(
                    app_ids=[],
                    recheck_limit=0,
                    rhwp_grace_hours=6,
                    actor="owner@taeyoung.com",
                    expected_revision=1,
                    conn=conn,
                )
            assert cp.policy_from_row(cp.fetch_row(conn)).revision == 2
            assert [h["revision"] for h in cp.history(conn=conn)] == [2, 1]
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(f"UPDATE {cp.TABLE} SET recheck_limit = 5000")
            # SQL 로 소문자를 넣으면 CHECK 는 통과하지만 코드가 모른다고 읽는다.
            conn.execute(f"UPDATE {cp.TABLE} SET rhwp_app_ids = ARRAY['a0rhwp00001']")
            assert cp.policy_from_row(cp.fetch_row(conn)).state == cp.STATE_UNKNOWN
        finally:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
