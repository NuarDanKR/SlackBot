"""봇 관리 read model 과 판단 — **Hermes 는 하나다.**

설계: `docs/design/workspace-service-console-redesign.md` §0·§4·§7 (2026-09-28)

여기서 막는 것 넷.

1. **Hermes 가 둘로 보이는 것.** Slack 직접 연결과 Master 내부 호출은 같은 봇의
   다른 연결이다. 두 행으로 나누면 하나를 끈 사람이 다른 하나도 끈 줄 안다
2. **정체성과 런타임 상태를 합치는 것.** 「봇이 살아 있나」 와 「지금 켜져 있나」
   는 다른 질문이고, 합치면 한쪽을 끈 것이 다른 쪽까지 끈 것으로 읽힌다
3. **연결 저장만으로 무언가 켜지는 것.** 토큰을 넣는 것과 수집이 시작되는 것은
   다른 일이다
4. **토큰이 나가는 것.** 나가는 것은 mask 뿐이다

가짜 저장소를 쓴다. 커서를 흉내 내면 시험이 규칙이 아니라 SQL 문장 모양을 지킨다.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_bot_repo import FakeBotRepo

from tybot.console import bot_admin as admin

ROOT = Path(__file__).resolve().parent.parent
BOT = "xoxb-" + "1" * 30
APP = "xapp-" + "2" * 30


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    """암호화 키. **시험마다 새로 만든다** — 저장소의 키를 빌려 오지 않는다."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("WORKSPACE_SECRET_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def repo() -> FakeBotRepo:
    store = FakeBotRepo()
    store.given_bot("master", category="orchestrator", slack_connectable=True,
                    internally_invokable=False, display_name="TYBot Master")
    store.given_bot("archiver", category="collector", slack_connectable=True,
                    internally_invokable=False, display_name="Archiving Bot")
    store.given_bot("hermes", category="specialist", slack_connectable=True,
                    internally_invokable=True, display_name="Hermes")
    store.given_bot("clio", category="specialist", slack_connectable=False,
                    internally_invokable=True, display_name="Clio")
    store.given_specialist("hermes")
    return store


@pytest.fixture
def actor() -> admin.Actor:
    return admin.Actor("dan@taeyoung.com", "PF 공존 준비")


def _fingerprint(repo, workspace: str, bot_key: str) -> str:
    """지금 저장된 토큰 지문. 실제 검증 경로가 들고 오는 값과 같은 것이다."""
    row = repo.connection(workspace, bot_key)
    return repo.secret_fingerprint(int(row["id"])) if row else ""


# --- 사람과 사유 ---------------------------------------------------------------

def test_an_actor_without_a_name_is_refused():
    with pytest.raises(admin.BotAdminRefused, match="바꾼 사람"):
        admin.Actor("", "연결")


def test_an_actor_without_a_reason_is_refused():
    """기본값을 두면 전부 그 기본값으로 남고, 그건 기록이 아니다."""
    with pytest.raises(admin.BotAdminRefused, match="사유"):
        admin.Actor("dan", "  ")


# --- 하나의 Hermes ------------------------------------------------------------

def test_hermes_appears_once_with_both_bindings(repo):
    """**이 시험이 이 화면의 이유다.**

    PF 는 Slack 으로 직접 부르고 TY 는 Master 가 내부로 부른다. 둘이 다른 봇으로
    보이면, 직접 연결을 중지한 사람이 Hermes 런타임까지 껐다고 믿는다.
    """
    repo.given_connection("pf", "hermes", state="enabled", team_id="T9", bot_user_id="U9")
    repo.given_assignment("hermes", "pf")
    repo.given_route("hermes", "pf", "shadow")

    rows = admin.bots(repo)["bots"]

    hermes = [row for row in rows if row["key"] == "hermes"]
    assert len(hermes) == 1, "Hermes 가 두 번 나오면 봇이 둘인 것처럼 보인다"
    kinds = sorted(binding["type"] for binding in hermes[0]["bindings"])
    assert kinds == ["master_internal", "slack_socket"]


def test_an_internal_binding_has_no_token_fields(repo):
    """내부 호출에는 Slack 토큰이 없다. 칸을 두면 화면이 입력란을 만든다."""
    repo.given_assignment("hermes", "tyit")
    repo.given_route("hermes", "tyit", "active")

    hermes = admin.bot_detail("hermes", repo)
    (binding,) = [b for b in hermes["bindings"] if b["type"] == "master_internal"]

    assert "botTokenMask" not in binding
    assert "appTokenMask" not in binding
    assert binding["mode"] == "active"


def test_the_catalog_state_and_the_runtime_state_stay_apart(repo):
    """합치면 「봇이 살아 있나」 와 「지금 켜져 있나」 를 구분할 수 없다(§4.2)."""
    repo.specialist_rows.clear()
    repo.given_specialist("hermes", state="disabled", health="error")

    hermes = admin.bot_detail("hermes", repo)

    assert hermes["state"] == "active", "정체성은 런타임이 꺼져도 살아 있다"
    assert hermes["runtime"]["state"] == "disabled"
    assert hermes["runtime"]["health"] == "error"


def test_a_bot_without_a_runtime_says_so(repo):
    """Master 는 전문 봇이 아니다. 런타임 칸을 지어내면 화면이 빈 값을 보여 준다."""
    assert admin.bot_detail("master", repo)["runtime"] is None


def test_every_catalog_bot_appears_even_without_connections(repo):
    """연결이 없다고 목록에서 빠지면, 붙이러 갈 자리가 화면에 없다."""
    keys = [row["key"] for row in admin.bots(repo)["bots"]]

    assert keys == ["master", "archiver", "hermes", "clio"]


# --- 워크스페이스 화면 ----------------------------------------------------------

def test_a_workspace_shows_slack_and_internal_together(repo):
    """두 번 부르면 서로 다른 시각의 상태를 한 화면에서 보게 된다."""
    repo.given_connection("pf", "hermes", state="enabled")
    repo.given_assignment("hermes", "pf")
    repo.given_route("hermes", "pf", "shadow")

    detail = admin.workspace_connections("pf", repo)

    hermes = next(row for row in detail["bots"] if row["key"] == "hermes")
    assert len(hermes["slack"]) == 1 and len(hermes["internal"]) == 1


def test_a_bot_unrelated_to_this_workspace_is_left_out(repo):
    """빈 행을 늘리면 정작 볼 것이 밀린다."""
    detail = admin.workspace_connections("tyit", repo)

    assert [row["key"] for row in detail["bots"]] == ["master", "archiver", "hermes"]


def test_an_assigned_bot_without_a_route_row_is_disabled(repo):
    """행이 없는 것은 `disabled` 다(§4.5). 화면에서 빠지면 「배정했는데 안 보인다」."""
    repo.given_assignment("clio", "tyit")

    routes = admin.workspace_routes("tyit", repo)["routes"]

    (clio,) = [row for row in routes if row["key"] == "clio"]
    assert clio["mode"] == "disabled"
    assert clio["assigned"] is True


# --- 연결 저장 ------------------------------------------------------------------

def test_saving_a_connection_does_not_enable_it(repo, actor):
    """등록과 동시에 켜지면 토큰을 잘못 붙인 채로 수집이 시작된다."""
    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )

    (row,) = repo.connections("tyit")
    assert row["state"] == "draft"
    assert row["identity_ok"] is None


def test_saving_never_carries_a_state(repo, actor):
    """저장이 상태를 들고 가면, 저장소가 그 값을 쓰는 날 **검사 없이 켜진다.**

    상태는 신원 검사를 통과할 때만 바뀐다. 저장 경로에 상태 칸이 있으면 언젠가
    호출부 하나가 거기에 `enabled` 를 넣는다.
    """
    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )

    (saved,) = repo.saved_connections
    assert "state" not in saved
    assert set(saved) == {"workspace", "bot_key", "note", "actor"}


def test_a_route_whose_assignment_was_removed_cannot_be_turned_on(repo, actor):
    """배정을 뗀 뒤에도 라우트 행은 남는다(지우지 않으므로).

    그 행을 그대로 켤 수 있으면, 배정 없는 봇이 켜진 것처럼 보이는데 라우터는
    그 봇을 못 찾는다 — 아무 질문도 안 가는 상태가 제일 오래 간다.
    """
    repo.given_route("hermes", "tyit", "disabled")

    with pytest.raises(admin.BotAdminRefused, match="배정"):
        admin.set_route("tyit", "hermes", "shadow", actor=actor, repo=repo)


def test_a_route_whose_assignment_was_removed_can_still_be_turned_off(repo, actor):
    """끄는 길은 열어 둔다. 배정이 없다고 못 끄면 켜진 행이 영원히 남는다."""
    repo.given_route("hermes", "tyit", "shadow")

    admin.set_route("tyit", "hermes", "disabled", actor=actor, repo=repo)

    assert repo.route_rows[("hermes", "tyit")]["route_mode"] == "disabled"


def test_a_specialist_that_cannot_take_slack_is_refused(repo, actor):
    """전문 봇이 Slack 에 직접 붙으면 권한을 판정할 자리가 사라진다."""
    with pytest.raises(admin.BotAdminRefused, match="Slack 에 직접"):
        admin.save_slack_connection(
            "tyit", "clio", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
        )


def test_one_token_alone_is_refused(repo, actor):
    """봇 토큰만 바꾸면 Socket 은 옛 앱 토큰으로 열린다. 그 조합은 확인된 적이 없다."""
    with pytest.raises(admin.BotAdminRefused, match="함께"):
        admin.save_slack_connection(
            "tyit", "archiver", actor=actor, bot_token=BOT, repo=repo,
        )


def test_a_token_shape_is_checked(repo, actor):
    with pytest.raises(Exception, match="봇 토큰"):
        admin.save_slack_connection(
            "tyit", "archiver", actor=actor, bot_token="xoxp-1234", app_token=APP,
            repo=repo,
        )


def test_replacing_a_token_clears_the_identity_and_turns_it_off(repo, actor):
    """새 토큰이 옛 검사 결과를 물려받으면 **검사 없이 켜진 채로** 남는다."""
    repo.given_connection("tyit", "archiver", state="enabled", identity_ok=True,
                          team_id="T1", bot_user_id="U2")

    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )

    (row,) = repo.connections("tyit")
    assert row["identity_ok"] is None
    assert row["state"] == "disabled"


def test_only_the_mask_reaches_the_audit(repo, actor):
    """평문은 화면·로그·감사 어디에도 안 나간다(§7.5)."""
    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )

    (entry,) = repo.audit_rows
    assert BOT not in entry["new_value"] and APP not in entry["new_value"]
    assert "bot=" in entry["new_value"] and "…" in entry["new_value"]


def test_the_read_model_never_carries_ciphertext(repo, actor):
    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )

    detail = admin.workspace_connections("tyit", repo)
    (archiver,) = [row for row in detail["bots"] if row["key"] == "archiver"]

    (slack,) = archiver["slack"]
    assert set(slack) & {"ciphertext", "botToken", "appToken"} == set()
    assert slack["botTokenMask"].startswith("xoxb-")


# --- 신원 ----------------------------------------------------------------------

def test_identity_passes_and_enables(repo, actor):
    repo.given_connection("tyit", "archiver", state="draft")

    problem = admin.record_identity(
        "tyit", "archiver", team_id="T1", bot_user_id="U2", actor=actor,
        token_fingerprint=_fingerprint(repo, "tyit", "archiver"), repo=repo,
    )

    assert problem == ""
    (row,) = repo.connections("tyit")
    assert row["state"] == "enabled" and row["identity_ok"] is True


def test_a_token_from_another_workspace_is_refused(repo, actor):
    """토큰을 잘못 붙이면 **다른 워크스페이스에 수집한다.** 오류가 아니라 유출이다."""
    repo.given_connection("tyit", "master", state="enabled", team_id="T1",
                          bot_user_id="U1", identity_ok=True)
    repo.given_connection("tyit", "archiver", state="draft")

    problem = admin.record_identity(
        "tyit", "archiver", team_id="T9", bot_user_id="U2", actor=actor,
        token_fingerprint=_fingerprint(repo, "tyit", "archiver"), repo=repo,
    )

    assert "다른 워크스페이스" in problem
    row = next(r for r in repo.connections("tyit") if r["bot_key"] == "archiver")
    assert row["state"] == "disabled"


def test_the_same_bot_user_twice_is_refused(repo, actor):
    """같은 앱을 두 번 등록하면 Socket Mode 를 두 곳에서 열게 된다."""
    repo.given_connection("tyit", "master", state="enabled", team_id="T1",
                          bot_user_id="U1", identity_ok=True)
    repo.given_connection("tyit", "archiver", state="draft")

    problem = admin.record_identity(
        "tyit", "archiver", team_id="T1", bot_user_id="U1", actor=actor,
        token_fingerprint=_fingerprint(repo, "tyit", "archiver"), repo=repo,
    )

    assert "봇 사용자" in problem


def test_a_retired_connection_does_not_block_a_new_one(repo, actor):
    """앱을 갈아 끼운 뒤 옛 연결이 새 연결을 막으면, 막는 이유가 화면에 없다."""
    repo.given_connection("tyit", "master", state="retired", team_id="T1",
                          bot_user_id="U1")
    repo.given_connection("tyit", "archiver", state="draft")

    problem = admin.record_identity(
        "tyit", "archiver", team_id="T1", bot_user_id="U1", actor=actor,
        token_fingerprint=_fingerprint(repo, "tyit", "archiver"), repo=repo,
    )

    assert problem == ""


def test_recording_identity_for_a_missing_connection_is_refused(repo, actor):
    with pytest.raises(admin.BotAdminRefused, match="등록되지 않은 연결"):
        admin.record_identity(
            "tyit", "archiver", team_id="T1", bot_user_id="U2", actor=actor,
            token_fingerprint="", repo=repo,
        )


def test_a_token_swapped_during_verification_is_not_recorded(repo, actor):
    """**옛 토큰으로 받은 「확인됨」 이 새 토큰에 붙으면 안 된다.**

    Slack 에 묻는 동안 다른 사람이 토큰을 갈아 끼울 수 있다. 그 결과를 그대로
    적으면 연결이 검사 없이 켜지고, 화면은 초록불인데 실제로 붙는 앱은 아무도
    확인하지 않은 것이다.
    """
    repo.given_connection("tyit", "archiver", state="draft")
    stale = _fingerprint(repo, "tyit", "archiver")
    # 검사 도중 누군가 토큰을 교체했다.
    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )

    with pytest.raises(admin.TokenChanged, match="토큰이 바뀌었"):
        admin.record_identity(
            "tyit", "archiver", team_id="T1", bot_user_id="U2", actor=actor,
            token_fingerprint=stale, repo=repo,
        )

    row = repo.connections("tyit")[0]
    assert row["state"] == "draft", "검사 안 된 채로 남아야 한다"
    assert row["identity_ok"] is None


def test_the_same_token_pair_verifies_normally(repo, actor):
    """지문이 그대로면 평소처럼 켜진다 — 잠금이 너무 세면 아무도 못 켠다."""
    repo.given_connection("tyit", "archiver", state="draft")
    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )
    fingerprint = _fingerprint(repo, "tyit", "archiver")

    problem = admin.record_identity(
        "tyit", "archiver", team_id="T1", bot_user_id="U2", actor=actor,
        token_fingerprint=fingerprint, repo=repo,
    )

    assert problem == ""
    assert repo.connections("tyit")[0]["state"] == "enabled"


def test_re_saving_the_same_token_still_changes_the_fingerprint(repo, actor):
    """Fernet 은 같은 평문도 다르게 암호화한다. 안전한 쪽으로 틀린다."""
    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )
    first = _fingerprint(repo, "tyit", "archiver")
    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )

    assert _fingerprint(repo, "tyit", "archiver") != first


# --- 연결 중지 ------------------------------------------------------------------

def test_a_connection_can_be_disabled_and_retired(repo, actor):
    repo.given_connection("pf", "hermes", state="enabled", team_id="T1", bot_user_id="U3")

    admin.set_connection_state("pf", "hermes", "disabled", actor=actor, repo=repo)
    assert repo.connections("pf")[0]["state"] == "disabled"

    admin.set_connection_state("pf", "hermes", "retired", actor=actor, repo=repo)
    assert repo.connections("pf")[0]["state"] == "retired"


def test_a_connection_cannot_be_enabled_by_hand(repo, actor):
    """켜는 것은 신원 검사를 통과할 때뿐이다."""
    repo.given_connection("pf", "hermes", state="disabled")

    with pytest.raises(admin.BotAdminRefused, match="신원 검사"):
        admin.set_connection_state("pf", "hermes", "enabled", actor=actor, repo=repo)


def test_disabling_a_connection_leaves_the_runtime_alone(repo, actor):
    """PF 직접 연결을 중지해도 Hermes 런타임과 내부 호출은 그대로 돈다(§0)."""
    repo.given_connection("pf", "hermes", state="enabled", team_id="T1", bot_user_id="U3")
    repo.given_assignment("hermes", "tyit")
    repo.given_route("hermes", "tyit", "active")

    admin.set_connection_state("pf", "hermes", "disabled", actor=actor, repo=repo)

    hermes = admin.bot_detail("hermes", repo)
    assert hermes["runtime"]["state"] == "enabled"
    internal = [b for b in hermes["bindings"] if b["type"] == "master_internal"]
    assert internal[0]["mode"] == "active"


# --- 내부 호출 라우트 -----------------------------------------------------------

def test_a_route_can_be_shadowed(repo, actor):
    """그림자는 사용자에게 결과를 전달하지 않으므로 막지 않는다(§7.3)."""
    repo.given_assignment("hermes", "tyit")

    admin.set_route("tyit", "hermes", "shadow", actor=actor, repo=repo)

    assert repo.route_rows[("hermes", "tyit")]["route_mode"] == "shadow"


def test_an_active_route_needs_a_healthy_runtime(repo, actor):
    """죽은 런타임으로 켜면 질문이 전부 대기하다 fallback 으로 떨어진다."""
    repo.specialist_rows.clear()
    repo.given_specialist("hermes", state="enabled", health="error")
    repo.given_assignment("hermes", "tyit")

    with pytest.raises(admin.BotAdminRefused, match="health"):
        admin.set_route("tyit", "hermes", "active", actor=actor, repo=repo)


def test_an_active_route_needs_an_enabled_runtime(repo, actor):
    repo.specialist_rows.clear()
    repo.given_specialist("hermes", state="disabled", health="ok")
    repo.given_assignment("hermes", "tyit")

    with pytest.raises(admin.BotAdminRefused, match="런타임이 켜져"):
        admin.set_route("tyit", "hermes", "active", actor=actor, repo=repo)


def test_an_unassigned_bot_cannot_be_routed(repo, actor):
    """배정이 없으면 라우터가 그 봇을 못 찾는다 — 켠 것처럼 보이는데 아무 질문도 안 간다."""
    with pytest.raises(admin.BotAdminRefused, match="배정"):
        admin.set_route("tyit", "hermes", "active", actor=actor, repo=repo)


def test_a_bot_that_is_not_internally_invokable_is_refused(repo, actor):
    """Master 가 자기를 내부 호출하면 라우팅이 자기 자신으로 돈다(§7.5 → 422)."""
    repo.given_assignment("master", "tyit")

    with pytest.raises(admin.BotAdminRefused, match="내부 호출 대상"):
        admin.set_route("tyit", "master", "shadow", actor=actor, repo=repo)


def test_an_unknown_route_mode_is_refused(repo, actor):
    repo.given_assignment("hermes", "tyit")

    with pytest.raises(admin.BotAdminRefused, match="모르는 라우트 모드"):
        admin.set_route("tyit", "hermes", "on", actor=actor, repo=repo)


def test_route_changes_are_audited(repo, actor):
    repo.given_assignment("hermes", "tyit")
    repo.given_route("hermes", "tyit", "shadow")

    admin.set_route("tyit", "hermes", "active", actor=actor, repo=repo)

    (entry,) = repo.audit_rows
    assert entry["old_value"] == "shadow" and entry["new_value"] == "active"
    assert entry["reason"] == "PF 공존 준비"


# --- 이관 비교 ------------------------------------------------------------------

def test_the_legacy_diff_maps_hermes_direct_to_hermes(repo):
    """`hermes_direct` 가 `hermes` 다. 못 맞추면 이관이 안 된 것처럼 보인다."""
    repo.given_legacy("pf", "hermes_direct", bot_mask="xoxb-1…9", app_mask="xapp-2…8")
    repo.given_connection("pf", "hermes", state="enabled", team_id="T1",
                          bot_user_id="U1", bot_mask="xoxb-1…9", app_mask="xapp-2…8",
                          token_count=2)

    report = admin.legacy_diff("pf", repo)

    assert report["ok"] is True and report["checked"] == 1


def test_a_workspace_that_did_not_move_is_reported(repo):
    """이관을 「했다」 로 믿으면 한 워크스페이스만 안 옮겨진 채 남는다."""
    repo.given_legacy("pf", "archiver")

    report = admin.legacy_diff("pf", repo)

    assert report["ok"] is False
    assert report["problems"][0]["problem"] == "새 표에 연결이 없습니다"


def test_a_changed_mask_is_reported(repo):
    repo.given_legacy("pf", "archiver", bot_mask="xoxb-old", app_mask="xapp-old")
    repo.given_connection("pf", "archiver", state="enabled", team_id="T1",
                          bot_user_id="U1", bot_mask="xoxb-new", app_mask="xapp-old",
                          token_count=2)

    report = admin.legacy_diff("pf", repo)

    assert [p["problem"] for p in report["problems"]] == ["botTokenMask 불일치"]


# --- 감사 ----------------------------------------------------------------------

def test_the_audit_view_carries_no_secret(repo, actor):
    admin.save_slack_connection(
        "tyit", "archiver", actor=actor, bot_token=BOT, app_token=APP, repo=repo,
    )

    entries = admin.audit("tyit", 10, repo)["entries"]

    assert entries and BOT not in str(entries) and APP not in str(entries)


def test_the_module_never_reads_ciphertext():
    """복호화 조회가 있으면 언젠가 로그에 찍히고, 로그는 감사보다 오래 남는다."""
    source = (ROOT / "src" / "tybot" / "console" / "bot_admin.py").read_text(
        encoding="utf-8"
    )

    assert "decrypt" not in source
    assert "ciphertext" not in source.split('"""')[-1]


# --- 이 화면의 변경이 지금 돌고 있는 것에 무엇을 하나 ---------------------------
#
# 2026-09-29 오너 지시. 사람이 제일 자주 틀리는 자리다 — 콘솔에서 껐는데 봇이
# 계속 도는 것을 보면 「콘솔이 고장났다」 로 읽고, 그때 서버에 들어가 프로세스를
# 죽인다.

RUNTIME_READERS = {
    "Master 워크스페이스·토큰": ROOT / "src" / "tybot" / "workspaces.py",
    "Archiver 기동 설정": ROOT / "src" / "tybot" / "archiver_runtime_store.py",
    "Master 의 전문 봇 라우팅": ROOT / "src" / "tybot" / "specialist_router.py",
}
NEW_TABLES = (
    "bot_connection", "bot_catalog", "specialist_route", "archiver_connection_config",
)


def mentions_table(source: str, table: str) -> bool:
    """이 소스가 그 표를 **낱말로** 말하나.

    `specialist_route` 는 `specialist_router` 의 앞부분이기도 하다. 그냥 부분
    문자열로 보면 라우터가 자기 이름 때문에 「새 표를 읽는다」 로 잡힌다.

    2026-09-29: 여기 낱말 경계가 **백스페이스 문자**로 들어가 있었다. 백스페이스
    뒤에 표 이름이 오는 소스는 없으므로 이 검사는 아무것도 못 찾았다 — 통과하는
    빈 시험이었고, 런타임이 새 표를 읽기 시작해도 말해 주지 않았다. 그래서
    탐지기를 밖으로 꺼내 **양성 시험**을 따로 붙인다.
    """
    pattern = r"(?<![A-Za-z0-9_])" + re.escape(table) + r"(?![A-Za-z0-9_])"
    return re.search(pattern, source) is not None


@pytest.mark.parametrize("source, table, expected", [
    ("cur.execute('SELECT 1 FROM bot_connection')", "bot_connection", True),
    ("    JOIN bot_catalog c ON c.key = s.key", "bot_catalog", True),
    ("FROM specialist_route r", "specialist_route", True),
    # 잡으면 **안 되는** 것. 이게 없으면 탐지기를 넓히다가 라우터 이름에 걸린다.
    ('log = logging.getLogger("tybot.specialist_router")', "specialist_route", False),
    ("from .bot_connection_helpers import x", "bot_connection", False),
    ("workspace_service", "bot_connection", False),
])
def test_the_table_detector_actually_matches(source, table, expected):
    """**탐지기 자체를 시험한다.** 못 찾는 탐지기는 언제나 통과한다."""
    assert mentions_table(source, table) is expected


def test_a_reader_that_adopts_a_new_table_is_caught():
    """전환이 시작되면 이 판정이 **반드시 걸려야** 한다.

    진짜 파일을 고치지 않고, 옮겨 온 모양의 소스에 같은 판정을 돌린다.
    """
    migrated = (
        "def load_runtime_config(workspace):\n"
        "    cur.execute('SELECT * FROM bot_connection WHERE workspace = %s')\n"
    )

    assert [table for table in NEW_TABLES if mentions_table(migrated, table)] == [
        "bot_connection",
    ]


def test_the_scan_covers_every_runtime_reader():
    """훑는 파일이 비면 「아무도 안 읽는다」 가 **아무것도 안 봤다** 가 된다."""
    assert set(RUNTIME_READERS) == {
        "Master 워크스페이스·토큰", "Archiver 기동 설정", "Master 의 전문 봇 라우팅",
    }
    for name, path in RUNTIME_READERS.items():
        assert path.is_file(), f"{name} 파일이 없다: {path}"


def test_runtime_reader_status_matches_the_actual_contracts():
    """Archiver만 전환됐다. 화면이 전부 적용/미적용이라고 뭉개면 안 된다."""
    reads = {
        name: [table for table in NEW_TABLES
               if mentions_table(path.read_text(encoding="utf-8"), table)]
        for name, path in RUNTIME_READERS.items()
    }
    touched = {name: tables for name, tables in reads.items() if tables}

    assert touched == {
        "Archiver 기동 설정": ["archiver_connection_config"],
    }
    assert admin.RUNTIME_READER_STATUS == {
        "master_connection": False,
        "archiver_connection": True,
        "specialist_route": False,
    }
    assert admin.RUNTIME_READS_NEW_TABLES is False


def test_the_read_model_says_it_does_not_apply_now(repo):
    """화면이 잊을 수 없게 **모든 읽기에** 실어 보낸다."""
    for payload in (
        admin.bots(repo),
        admin.workspace_connections("tyit", repo),
        admin.workspace_routes("tyit", repo),
    ):
        effect = payload["runtimeEffect"]
        assert effect["appliesNow"] is False
        assert "Archiver 연결만 다음 기동에 적용" in effect["summary"]
        assert len(effect["details"]) >= 3


def test_the_notice_names_what_does_not_stop(repo):
    """「연결을 껐으니 수집도 멈췄겠지」 가 제일 비싼 오해다."""
    details = " ".join(admin.runtime_effect()["details"])

    assert "수집이 멈추지 않는다" in details
    assert "답변 경로는 그대로" in details


def test_disabling_a_connection_changes_no_collection_state(repo, actor):
    """연결 상태와 채널 수집 모드는 **다른 표**다. 한쪽이 다른 쪽을 건드리면
    화면에서 본 것과 실제가 갈린다."""
    repo.given_connection("tyit", "archiver", state="enabled", team_id="T1",
                          bot_user_id="U2", identity_ok=True)

    admin.set_connection_state("tyit", "archiver", "disabled", actor=actor, repo=repo)

    fields = {key for row in repo.audit_rows for key in (row["field"],)}
    assert fields == {"connection.archiver.slack_socket.state"}
    # 수집 모드·기능 스위치·writer 인수는 `archiving_admin` 소유다. 이 모듈은
    # 그 표를 아예 만지지 않는다.
    source = (ROOT / "src" / "tybot" / "console" / "bot_admin.py").read_text(
        encoding="utf-8"
    )
    for table in ("archive_channel_mode", "archive_feature_flag", "writer_owner"):
        assert table not in source


def test_no_source_file_hides_a_control_character():
    """정규식에 **백스페이스가 들어가면 시험이 조용히 빈다**(2026-09-29 실제로 그랬다).

    눈에 안 보이므로 리뷰에서도 안 걸린다. 그래서 파일 전체를 훑는다 —
    탭·줄바꿈 말고 제어문자는 우리 소스에 있을 이유가 없다.
    """
    control = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
    offenders = []
    for folder in ("src", "tests", "scripts"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            if control.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
