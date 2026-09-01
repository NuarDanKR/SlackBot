"""헬스 체크 — '조용한 고장'이 실제로 드러나는지 본다.

이 기능의 값어치는 **문제를 놓치지 않는 것**에 있다. 그래서 정상 판정보다
"이건 반드시 걸려야 한다" 를 더 많이 검사한다.
"""
from __future__ import annotations

import pytest

from tybot.console import health


def _ws(key="pilot", *, connected=True, broken=0, uninvited=0,
        write_problem=None, health_value="fresh"):
    return {
        "key": key, "label": key, "connected": connected,
        "broken": broken, "uninvitedChannels": uninvited,
        "writeProblem": write_problem, "health": health_value,
    }


def _qa(hits=3, error="", ms=1000, reason="answered", workspace="pilot"):
    return {
        "workspace": workspace, "hits": hits, "error": error,
        "elapsed_ms": ms, "reason": reason, "ts": "2026-09-01T09:00:00+09:00",
    }


def _fb(kind="positive", action="added", workspace="pilot", actor="U1", text=""):
    return {"kind": kind, "action": action, "workspace": workspace, "actor": actor,
            "text": text, "at": "2026-09-01T09:00:00+09:00"}


# ---------------------------------------------------------------------------
# 판정 합치기
# ---------------------------------------------------------------------------


def test_worst_wins():
    assert health._worst("ok", "warn", "bad") == "bad"
    assert health._worst("ok", "warn") == "warn"


def test_unknown_does_not_count_as_bad():
    """자료가 없는 항목 때문에 전체가 '문제 있음'이 되면 아무도 안 본다."""
    assert health._worst("ok", "unknown") == "ok"
    assert health._worst("unknown", "unknown") == "unknown"


# ---------------------------------------------------------------------------
# 봇
# ---------------------------------------------------------------------------


def test_disconnected_bot_is_bad():
    assert health.bot_section([_ws(connected=False)])["level"] == "bad"


def test_stale_status_file_is_warn_not_ok():
    """상태 파일이 낡으면 봇이 죽었는지 알 수 없다. '정상'으로 칠하면 안 된다."""
    section = health.bot_section([_ws(connected=None)])
    assert section["level"] == "warn"
    assert any("오래됐" in p for p in section["workspaces"][0]["problems"])


def test_write_problem_is_bad():
    """저장이 안 되면 봇은 멀쩡히 답하면서 원문을 잃는다."""
    assert health.bot_section([_ws(write_problem="Read-only file system")])["level"] == "bad"


def test_uninvited_channels_surface_as_warning():
    """오류가 아니라서 아무도 모르는 채로 수집이 비어 간다."""
    section = health.bot_section([_ws(uninvited=4)])
    assert section["level"] == "warn"
    assert any("초대되지 않은" in p for p in section["workspaces"][0]["problems"])


def test_healthy_bot_is_ok():
    assert health.bot_section([_ws()])["level"] == "ok"


# ---------------------------------------------------------------------------
# 아카이브
# ---------------------------------------------------------------------------


def test_broken_documents_are_bad():
    """깨진 문서는 검색에서 조용히 빠진다 — 답이 '없다'로 바뀌는 원인이다."""
    section = health.archive_section([_ws(broken=2)], [{"workspace": "pilot"}])
    assert section["level"] == "bad"
    assert section["brokenDocuments"] == 2


def test_stale_collection_is_warn():
    section = health.archive_section([_ws(health_value="stale")], [{"workspace": "pilot"}])
    assert section["level"] == "warn"


def test_no_documents_is_unknown_not_ok():
    assert health.archive_section([_ws()], [])["level"] == "unknown"


# ---------------------------------------------------------------------------
# 답변 품질
# ---------------------------------------------------------------------------


def test_small_sample_is_unknown():
    """질문이 3건인 날을 '품질 좋음'으로 칠하면 그 지표는 아무 말도 못 한다."""
    section = health.answer_section([_qa() for _ in range(3)])
    assert section["level"] == "unknown"
    assert "표본이 적어" in section["note"]


def test_mostly_grounded_is_ok():
    section = health.answer_section([_qa() for _ in range(20)])
    assert section["level"] == "ok"
    assert section["groundedRate"] == 1.0


def test_low_grounded_rate_is_bad():
    """근거를 못 찾는 답이 절반을 넘으면 봇이 '없다'만 반복하고 있는 것이다."""
    records = [_qa(hits=0) for _ in range(15)] + [_qa() for _ in range(5)]
    section = health.answer_section(records)
    assert section["level"] == "bad"
    assert section["noHits"] == 15


def test_error_rate_pushes_to_bad():
    records = [_qa(error="AuthError") for _ in range(3)] + [_qa() for _ in range(17)]
    section = health.answer_section(records)
    assert section["level"] == "bad"
    assert section["errors"] == 3


def test_single_error_is_warn_not_bad():
    records = [_qa(error="boom")] + [_qa() for _ in range(19)]
    assert health.answer_section(records)["level"] == "warn"


def test_slow_answers_are_flagged():
    records = [_qa(ms=30_000)] + [_qa() for _ in range(19)]
    section = health.answer_section(records)
    assert section["slowAnswers"] == 1
    assert section["level"] == "warn"


# ---------------------------------------------------------------------------
# 슬래시 명령 — 매니페스트 ↔ 코드
# ---------------------------------------------------------------------------

MANIFEST = """
  slash_commands:
    - command: /채널
      description: 채널
    - command: /투표
      description: 투표
"""
CODE = '''
        @self.app.command("/채널")
        def channel(ack): ...
        @self.app.command("/투표")
        def poll(ack): ...
'''


def test_matching_commands_are_ok():
    section = health.command_section(MANIFEST, CODE)
    assert section["level"] == "ok"
    assert {c["name"] for c in section["commands"]} == {"/채널", "/투표"}


def test_command_missing_from_manifest_is_bad():
    """Slack 이 그 명령을 모른다. 사용자에게 '명령을 찾을 수 없다'가 뜬다."""
    code = CODE + '\n        @self.app.command("/일정")\n'
    section = health.command_section(MANIFEST, code)
    assert section["level"] == "bad"
    assert any("/일정" in p for p in section["problems"])


def test_command_missing_from_code_is_warn():
    """Slack 은 보내는데 봇이 응답하지 않는다. 서버 로그에도 안 남는다."""
    manifest = MANIFEST + "    - command: /수집상태\n"
    section = health.command_section(manifest, CODE)
    assert section["level"] == "warn"
    assert any("/수집상태" in p for p in section["problems"])


def test_unreadable_sources_are_unknown_not_ok():
    assert health.command_section("", "")["level"] == "unknown"


def test_real_repo_commands_match():
    """저장소의 실제 매니페스트와 코드가 어긋나 있으면 여기서 걸린다."""
    section = health.command_section(health.manifest_text(), health.code_text())
    assert section["level"] != "unknown", "매니페스트나 pilot.py 를 읽지 못했습니다"
    assert section["level"] == "ok", section["problems"]


# ---------------------------------------------------------------------------
# 피드백 만족도
# ---------------------------------------------------------------------------


def test_removed_reactions_are_not_counted():
    """눌렀다 취소한 것을 세면 만족도가 실제보다 좋게 나온다."""
    events = [_fb("positive", "added")] * 5 + [_fb("positive", "removed")] * 5
    section = health.feedback_section(events)
    assert section["positive"] == 5


def test_small_feedback_sample_is_unknown():
    assert health.feedback_section([_fb() for _ in range(3)])["level"] == "unknown"


def test_high_satisfaction_is_ok():
    section = health.feedback_section([_fb("positive") for _ in range(20)])
    assert section["level"] == "ok"
    assert section["satisfaction"] == 1.0


def test_low_satisfaction_is_bad():
    events = ([_fb("negative")] * 8 + [_fb("missing")] * 4 + [_fb("positive")] * 3)
    section = health.feedback_section(events)
    assert section["level"] == "bad"
    assert section["satisfaction"] < 0.5


def test_corrections_are_surfaced_as_work():
    """정정 제보는 나쁜 신호가 아니라 처리해야 할 일감이다."""
    events = [_fb("positive") for _ in range(15)] + [_fb("correction")] * 2
    section = health.feedback_section(events)
    assert section["corrections"] == 2
    assert any("정정" in p for p in section["problems"])


def test_corrections_do_not_lower_satisfaction():
    only_positive = health.feedback_section([_fb("positive") for _ in range(15)])
    with_correction = health.feedback_section(
        [_fb("positive") for _ in range(15)] + [_fb("correction")] * 5
    )
    assert with_correction["satisfaction"] == only_positive["satisfaction"]


# ---------------------------------------------------------------------------
# 기여도
# ---------------------------------------------------------------------------


def test_contributors_rank_by_written_corrections():
    """👍 를 많이 누른 사람이 1등이면 순위가 아무 의미도 없다."""
    events = (
        [_fb("positive", actor="U_clicker") for _ in range(50)]
        + [_fb("negative", actor="U_writer", text="기성금은 3억 2천만원입니다") for _ in range(3)]
    )
    rows = health.contributors(events)
    assert rows[0]["actor"] == "U_writer"
    assert rows[0]["corrections"] == 3


def test_contributors_ignore_reports_without_text():
    """내용 없는 신고는 고칠 거리를 주지 않는다."""
    rows = health.contributors([_fb("negative", actor="U1", text="")])
    assert rows[0]["corrections"] == 0
    assert rows[0]["reports"] == 1


def test_contributors_use_display_names():
    rows = health.contributors([_fb("negative", actor="U1", text="올바른 값")],
                               {"U1": "류대안"})
    assert rows[0]["name"] == "류대안"


def test_contributors_fall_back_to_id_when_name_unknown():
    """이름을 모른다고 사람을 빼면 기여가 사라진다."""
    rows = health.contributors([_fb("negative", actor="U9", text="올바른 값")])
    assert rows[0]["name"] == "U9"


def test_contributors_never_include_report_text():
    """이 목록은 화면에 그대로 나간다. 신고 본문에는 업무 내용이 들어 있다."""
    rows = health.contributors([_fb("negative", actor="U1", text="대외비 금액 12억")])
    assert all("12억" not in str(v) for v in rows[0].values())


def test_contributors_skip_removed_and_anonymous():
    events = [_fb("negative", action="removed", actor="U1", text="x1234"),
              _fb("negative", actor="", text="x1234")]
    assert health.contributors(events) == []


# ---------------------------------------------------------------------------
# 전체 보고서
# ---------------------------------------------------------------------------


def test_report_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(health.reader, "workspace_status", lambda store=None: [_ws()])
    monkeypatch.setattr(health.reader, "collected_docs",
                        lambda store=None: [{"workspace": "pilot"}])
    monkeypatch.setattr(health.reader, "_read_qa_records", lambda days: [_qa() for _ in range(20)])
    monkeypatch.setattr(health.reader, "qa_log_dir", lambda: tmp_path)

    out = health.report()
    assert set(out["sections"]) == {"bot", "archive", "answers", "commands", "feedback"}
    assert "contributors" in out["sections"]["feedback"]
    assert out["level"] in {"ok", "warn", "bad", "unknown"}
    assert isinstance(out["problems"], list)


def test_report_filters_by_allowed_workspaces(monkeypatch, tmp_path):
    """담당자에게 다른 워크스페이스 숫자가 섞이면 안 된다."""
    monkeypatch.setattr(health.reader, "workspace_status",
                        lambda store=None: [_ws("pilot"), _ws("fin")])
    monkeypatch.setattr(health.reader, "collected_docs",
                        lambda store=None: [{"workspace": "pilot"}, {"workspace": "fin"}])
    monkeypatch.setattr(
        health.reader, "_read_qa_records",
        lambda days: [_qa(workspace="pilot") for _ in range(20)]
        + [_qa(workspace="fin", hits=0) for _ in range(20)],
    )
    monkeypatch.setattr(health.reader, "qa_log_dir", lambda: tmp_path)

    out = health.report(allowed={"pilot"})
    assert [w["workspace"] for w in out["sections"]["bot"]["workspaces"]] == ["pilot"]
    assert out["sections"]["archive"]["documents"] == 1
    # fin 의 무근거 답변이 섞였다면 groundedRate 가 0.5 로 떨어진다.
    assert out["sections"]["answers"]["groundedRate"] == 1.0


@pytest.mark.parametrize("level", ["ok", "warn", "bad", "unknown"])
def test_levels_are_known_values(level):
    assert level in health.WORST


# ---------------------------------------------------------------------------
# 엔드포인트 — 로그인 없이 열리면 안 된다
# ---------------------------------------------------------------------------


def _client():
    pytest.importorskip("fastapi", reason="콘솔 의존성 없음")
    from fastapi.testclient import TestClient

    from tybot.console.app import app

    return TestClient(app)


def test_health_probe_stays_open():
    """살아 있는지 확인하는 용도라 인증을 요구하지 않는다. 내용은 담지 않는다."""
    res = _client().get("/api/health")
    assert res.status_code == 200
    assert set(res.json()) == {"ok", "at"}


def test_health_report_requires_login():
    """수집 현황·질문 수가 담긴다. 열어 두면 사내 운영 정보가 새어 나간다."""
    assert _client().get("/api/health-report").status_code == 401
