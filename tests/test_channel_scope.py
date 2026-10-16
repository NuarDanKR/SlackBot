"""채널 질문의 범위 — **질문 본문이 범위를 바꾸지 못한다.**

2026-10-16 tyit `C0BQYAC28F3` 에서 난 일이다.

    「이번주 CEO 주관 경영본부 주간 회의 내용이 뭐야?」

「경영본부」 가 `mgmt` 워크스페이스의 표시 이름이라 범위가 `{mgmt}` 로 좁혀졌고,
**질문이 올라온 tyit 채널의 원문이 통째로 탈락해** `no_access` 로 끝났다. 그
채널에 바로 그 회의 기록이 있었는데도, 답은 「봇을 초대하세요」 였다.

이 파일이 지키는 것 둘.

1. **채널 질문의 범위는 현재 워크스페이스·현재 채널로 고정이다.** 질문에 다른
   본부 이름이 나와도 바뀌지 않고, 그 채널에 공유된 원문은 **다른 조직 이야기여도**
   근거가 된다. 그 방에 올라왔다는 사실이 그 방 사람들이 함께 본다는 뜻이다.
2. **근거 0건의 사유를 구별해 말한다.** 「자료 없음」·「권한 확인 실패」·「처리
   장애」 는 사람이 할 일이 전혀 다른데 전에는 전부 같은 문장이었다.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from tybot.access import RequestContext
from tybot.archive.store import ArchiveStore
from tybot.intent import Intent

CHANNEL_ID = "C0BQYAC28F3"
OTHER_ID = "C0MGMT00001"


# --- 뼈대 -------------------------------------------------------------------


def _doc(ws: str, channel: str, channel_id: str, line: str, *, visibility="private") -> str:
    today = _dt.date.today()
    return (
        "---\n"
        f"workspace: {ws}\n"
        f'channel: "{channel}"\n'
        f"channel_id: {channel_id}\n"
        f"source_date: {today}\n"
        f"visibility: {visibility}\n"
        f"acl: [{channel}]\n"
        "doc_count: 1\n"
        f"last_ingested: {today}T17:00+09:00\n"
        "---\n\n## 요약\n-\n\n## 원문 (자동 취합, 편집 금지)\n"
        f"> [{today} 09:00] 홍길동: {line}\n"
    )


def _write(tmp_path, ws, channel, channel_id, line, **kw):
    path = tmp_path / "channels" / ws / f"{channel_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_doc(ws, channel, channel_id, line, **kw), encoding="utf-8")
    return path


class _Fake:
    name = "anthropic"

    def __init__(self):
        self.calls = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        from tybot.gateway.base import LLMResponse

        self.calls.append(list(messages))
        return LLMResponse("정리 결과", spec.model, self.name, 200, 40, 0.001)


def _engine(tmp_path):
    from tybot.answer import AnswerEngine
    from tybot.gateway.base import ModelSpec, Sensitivity
    from tybot.gateway.cost import CostGuard
    from tybot.gateway.router import Router

    fake = _Fake()
    router = Router(
        providers={"anthropic": fake},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    return AnswerEngine(
        ArchiveStore(tmp_path), router, allow_master_business_answers=True
    ), fake


@pytest.fixture
def labels(monkeypatch):
    """「경영본부」 가 실제로 `mgmt` 의 표시 이름인 설치를 만든다."""
    monkeypatch.setenv("WORKSPACES", "tyit,mgmt")
    monkeypatch.setenv("WORKSPACE_LABEL_TYIT", "전산팀")
    monkeypatch.setenv("WORKSPACE_LABEL_MGMT", "경영본부")
    from tybot.console import workspace_store

    monkeypatch.setattr(workspace_store, "list_workspaces", lambda: [])


def _channel_ctx(**kw):
    base = {
        "workspace": "tyit",
        "channels": frozenset({"#전산_업무"}),
        "channel_id": CHANNEL_ID,
        "channel": "#전산_업무",
        "user_id": "U1",
    }
    base.update(kw)
    return RequestContext(**base)


QUESTION = "이번주 CEO 주관 경영본부 주간 회의 내용이 뭐야?"


# --- 1. 현재 채널의 타 조직 원문으로 답한다 ------------------------------------


def test_other_org_content_shared_in_this_channel_is_usable(tmp_path, labels):
    """**재현 사례 그대로.** 「경영본부」 라는 낱말이 현재 채널을 탈락시키면 안 된다."""
    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID,
           "CEO 주관 경영본부 주간 회의: 기성금 집행률 72% 보고")
    eng, fake = _engine(tmp_path)

    ans = eng.respond(QUESTION, _channel_ctx(), Intent("summary"))

    assert ans.reason == "answered", ans.text
    assert "집행률 72%" in fake.calls[0][1].content


def test_a_named_workspace_does_not_narrow_a_channel_question(tmp_path, labels):
    """필터는 **빼기만** 한다. 채널에서는 뺄 것이 이미 ACL 로 다 빠져 있다."""
    from tybot.answer import _mentioned_workspaces, _named_workspace_filter

    # 라벨 인식 자체는 그대로다 — 좁히는 자리만 달라진다.
    assert _mentioned_workspaces(QUESTION) == frozenset({"mgmt"})

    assert _named_workspace_filter(QUESTION, _channel_ctx()) is None
    assert _named_workspace_filter(
        QUESTION, RequestContext(workspace="tyit", user_id="U1")
    ) is None


# --- 2. 다른 채널 자료는 새지 않는다 -------------------------------------------


def test_another_channel_never_leaks_into_a_channel_answer(tmp_path, labels):
    """범위 고정은 **넓히는 것이 아니다.** 옆 채널 자료가 따라 들어오면 안 된다."""
    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "우리 채널 기성금 이야기")
    _write(tmp_path, "tyit", "#전산_비밀", "C0SECRET001", "옆 채널 임금 협상 내용")
    eng, fake = _engine(tmp_path)

    ans = eng.respond(QUESTION, _channel_ctx(), Intent("summary"))

    assert ans.reason == "answered"
    evidence = fake.calls[0][1].content
    assert "우리 채널" in evidence
    assert "임금 협상" not in evidence, "옆 채널 자료가 채널 답변에 섞였다"


def test_another_workspace_never_leaks_into_a_channel_answer(tmp_path, labels):
    """질문이 `mgmt` 를 가리켜도 **읽히는 것은 현재 워크스페이스뿐**이다."""
    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "우리 채널 회의 메모")
    _write(tmp_path, "mgmt", "#경영_내부", OTHER_ID, "경영본부 원본 회의록")
    eng, fake = _engine(tmp_path)
    # 화이트리스트가 있어도, root 여도 채널 안에서는 넘어오지 않는다.
    ctx = _channel_ctx(readable_workspaces=frozenset({"mgmt"}), is_root=True)

    ans = eng.respond(QUESTION, ctx, Intent("summary"))

    assert ans.reason == "answered"
    evidence = fake.calls[0][1].content
    assert "우리 채널" in evidence
    assert "경영본부 원본 회의록" not in evidence


# --- 3. 도구 인자로 다른 채널을 요청해도 막힌다 --------------------------------


def test_tools_refuse_another_channel_asked_by_argument(tmp_path, labels):
    """전문 봇이 **인자로** 다른 채널을 달라고 해도 TYBot 의 권한 검사가 막는다.

    프롬프트 안내로만 막으면 모델이 한 번 어기는 순간 조용히 새고, 샜다는 신호가
    어디에도 안 난다. 범위는 `ctx` 에 갇혀 있어야 한다.
    """
    from tybot.specialist_tools import ToolBox

    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "우리 채널 기성금 이야기")
    _write(tmp_path, "tyit", "#전산_비밀", "C0SECRET001", "옆 채널 임금 협상 내용")
    _write(tmp_path, "mgmt", "#경영_내부", OTHER_ID, "경영본부 원본 회의록")
    # **실시간 조회를 실제로 연결한다.** 콜백이 없으면 `fetch_recent_slack` 은
    # 권한 검사에 닿기도 전에 「실시간 조회를 쓸 수 없습니다」 로 끝나고, 그러면
    # 이 시험은 경계가 아니라 **미설정**을 확인하게 된다.
    fetched: list[str] = []

    def live_fetch(channel_id, limit):
        fetched.append(channel_id)
        return [{"ts": "1760000000.0", "speaker": "홍길동",
                 "text": "옆 채널 임금 협상 실시간", "is_bot": False}]

    box = ToolBox(
        store=ArchiveStore(tmp_path),
        ctx=_channel_ctx(readable_workspaces=frozenset({"mgmt"}), is_root=True),
        live_fetch=live_fetch,
        here="#전산_업무",
    )

    for name, args in (
        ("read_channel", {"channel": "#전산_비밀"}),
        ("read_channel", {"channel": "#경영_내부"}),
        ("fetch_recent_slack", {"channel": "#전산_비밀"}),
        ("fetch_recent_slack", {"channel": "#경영_내부"}),
    ):
        out = box.run(name, args)
        assert "임금 협상" not in out and "경영본부 원본" not in out, (name, out)

    # **Slack 을 아예 부르지 않았어야 한다.** 받아 놓고 안 보여 주는 것은 격리가
    # 아니다 — 그 호출만으로 권한 밖 채널의 내용이 프로세스 안에 들어온다.
    assert fetched == [], fetched

    # 현재 채널은 정상적으로 불린다 — 막는 것이지 잠그는 것이 아니다.
    live = box.run("fetch_recent_slack", {"channel": "#전산_업무"})
    assert fetched == [CHANNEL_ID], fetched
    assert "임금 협상 실시간" in live, live

    # 「못 찾았다」 안내는 질문 낱말을 되돌려 주므로, 샜는지는 **그 채널에만 있는
    # 문장**으로 본다.
    assert "옆 채널" not in box.run("search", {"query": "임금 협상"})
    assert "옆 채널" not in box.run("read_document", {"name": "임금 협상"})
    # 현재 채널은 그대로 읽힌다 — 막는 것이지 잠그는 것이 아니다.
    assert "우리 채널" in box.run("read_channel", {"channel": "#전산_업무"})


def test_tools_do_not_let_a_search_scope_escape_the_channel(tmp_path, labels):
    """`where` 인자는 **볼 수 있는 채널 안에서만** 풀린다."""
    from tybot.specialist_tools import ToolBox

    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "우리 채널 기성금 이야기")
    _write(tmp_path, "tyit", "#전산_비밀", "C0SECRET001", "옆 채널 임금 협상 내용")
    box = ToolBox(store=ArchiveStore(tmp_path), ctx=_channel_ctx(), here="#전산_업무")

    out = box.run("search", {"query": "임금", "where": "#전산_비밀"})

    assert "옆 채널" not in out
    assert "없거나" in out or "찾지" in out or "0" in out, out


# --- 4. 사유를 구별해 안내한다 -------------------------------------------------


def test_no_evidence_in_this_channel_is_not_a_permission_message(tmp_path, labels):
    """문서는 있는데 걸린 줄이 없다. **「초대하세요」 가 나가면 안 된다.**"""
    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "어제 점심 메뉴 이야기")
    eng, _fake = _engine(tmp_path)

    ans = eng.answer("기성금 정산 결과가 어떻게 됐어?", _channel_ctx())

    assert ans.reason == "no_hits"
    assert "현재 채널에서 해당 근거를 찾지 못했습니다." in ans.text
    assert "/invite" not in ans.text
    assert "권한" not in ans.text


def test_an_empty_channel_says_so_without_blaming_permission(tmp_path, labels):
    """채널에 원문 자체가 없다. **수집 장애일 수 있다** — 초대 문제로 단정하지 않는다."""
    _write(tmp_path, "tyit", "#전산_비밀", "C0SECRET001", "옆 채널 내용")
    eng, _fake = _engine(tmp_path)

    ans = eng.answer("기성금 정산 결과가 어떻게 됐어?", _channel_ctx())

    assert ans.reason == "channel_no_archive"
    assert "현재 채널에서 해당 근거를 찾지 못했습니다." in ans.text
    assert "수집상태" in ans.text
    assert "/invite" not in ans.text


def test_a_failed_permission_lookup_is_not_reported_as_missing_data(tmp_path, labels):
    """범위를 **모르는 것**과 자료가 **없는 것**은 다르다."""
    eng, _fake = _engine(tmp_path)

    ans = eng.answer("기성금 정산 결과가 어떻게 됐어?",
                     _channel_ctx(permission_unverified=True))

    assert ans.reason == "permission_unverified"
    assert "확인하지 못했습니다" in ans.text
    assert "자료가 없다는 뜻이 아닙니다" in ans.text


def test_the_three_notices_are_actually_different(tmp_path, labels):
    """셋이 같은 문장이면 구별한 의미가 없다. 실제로 다른지 본다."""
    eng, _fake = _engine(tmp_path)
    ctx = _channel_ctx()

    said = {
        eng._no_evidence(ctx, had_documents=True).text,
        eng._no_evidence(ctx, had_documents=False).text,
        eng._no_evidence(_channel_ctx(permission_unverified=True),
                         had_documents=True).text,
    }

    assert len(said) == 3


def test_a_processing_failure_is_not_folded_into_missing_data():
    """처리 장애를 자료 없음으로 세면 운영 지표에서 영원히 안 보인다."""
    from tybot.answer_issues import issue_codes

    missing = issue_codes({"reason": "channel_no_archive", "hits": 0}, [])
    unverified = issue_codes({"reason": "permission_unverified", "hits": 0}, [])
    broken = issue_codes({"reason": "error", "hits": 0, "error": "boom"}, [])

    assert "no_evidence" in missing
    assert unverified == ["permission_unverified"], unverified
    assert "error" in broken


# --- 5. DM 은 통합 조회, 비참여 채널은 제외 ------------------------------------


def _dm_ctx(**kw):
    base = {
        "workspace": "tyit",
        "channels": frozenset({"#전산_업무", "#전산_회계"}),
        "user_id": "U1",
    }
    base.update(kw)
    return RequestContext(**base)


def test_dm_reads_every_joined_channel_but_not_the_others(tmp_path, labels):
    """통합 조회는 DM 에서만이고, **참여 채널까지**다."""
    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "업무 채널 기성금 이야기")
    _write(tmp_path, "tyit", "#전산_회계", "C0ACC000001", "회계 채널 기성금 이야기")
    _write(tmp_path, "tyit", "#전산_비밀", "C0SECRET001", "비참여 채널 임금 협상")
    eng, fake = _engine(tmp_path)

    ans = eng.summarize(_dm_ctx(), days=7)

    assert ans.reason == "answered"
    evidence = fake.calls[0][1].content
    assert "업무 채널" in evidence and "회계 채널" in evidence
    assert "임금 협상" not in evidence, "참여하지 않은 채널이 DM 통합조회에 들어갔다"


def test_dm_still_honours_a_named_workspace(tmp_path, labels):
    """DM narrows only an explicit workspace selection, after ACL filtering."""
    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "전산 업무 이야기")
    _write(tmp_path, "mgmt", "#경영_내부", OTHER_ID, "경영본부 회의 이야기",
           visibility="public")
    eng, fake = _engine(tmp_path)
    ctx = _dm_ctx(readable_workspaces=frozenset({"mgmt"}), is_root=True)

    ans = eng.respond("경영본부 워크스페이스에서만 요약해줘", ctx, Intent("summary"))

    assert ans.reason == "answered"
    evidence = fake.calls[0][1].content
    assert "경영본부 회의" in evidence
    assert "전산 업무 이야기" not in evidence


# --- 6. 기존 정책 회귀 ---------------------------------------------------------


def test_cross_workspace_still_needs_a_whitelist(tmp_path, labels):
    """화이트리스트 없이는 DM 에서도 다른 워크스페이스가 안 열린다."""
    _write(tmp_path, "mgmt", "#경영_내부", OTHER_ID, "경영본부 회의 이야기",
           visibility="public")
    eng, _fake = _engine(tmp_path)

    ans = eng.summarize(_dm_ctx(), days=7)

    assert ans.reason in ("no_access", "no_hits"), ans.reason
    assert "경영본부 회의" not in ans.text


def test_a_personal_dm_record_stays_out_of_a_channel_answer(tmp_path, labels):
    """개인 DM 기록은 **본인이 DM 에서 물을 때만**이다(B-57). 채널에서는 안 나온다."""
    from tybot.access import can_access

    shared = {
        "visibility": "private", "acl": None, "owner_workspace": "tyit",
        "dm_user": "U1",
    }
    assert can_access(_dm_ctx(), **shared)
    assert not can_access(_channel_ctx(), **shared)
    assert not can_access(_dm_ctx(user_id="U2"), **shared)
    # 통합조회 권한은 조직 기록을 보는 권한이지 개인 작업공간을 보는 권한이 아니다.
    assert not can_access(_dm_ctx(user_id="U2", role="exec", is_root=True), **shared)


# --- 3b. PF Hermes 로 나가는 **초기 근거**도 현재 채널이다 ----------------------


def test_the_evidence_handed_to_the_specialist_is_channel_scoped(tmp_path, labels):
    """전문 봇이 도구를 쓰기 전에 받는 **초기 근거**부터 현재 채널로 잘려 있어야 한다.

    도구만 막고 초기 근거를 안 자르면, 모델은 도구를 한 번도 안 부르고도 옆 채널
    내용을 받는다. 그쪽이 더 조용하다 — 도구 거절 로그조차 안 남는다.
    """
    from tybot.answer import AnswerEngine
    from tybot.gateway.base import ModelSpec, Sensitivity
    from tybot.gateway.cost import CostGuard
    from tybot.gateway.router import Router

    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID,
           "CEO 주관 경영본부 주간 회의: 기성금 집행률 72% 보고")
    _write(tmp_path, "tyit", "#전산_비밀", "C0SECRET001", "옆 채널 임금 협상 내용")
    _write(tmp_path, "mgmt", "#경영_내부", OTHER_ID, "경영본부 원본 회의록")

    seen: list[str] = []

    def hook(task, ctx, evidence, **_kw):
        # 근거만 가로챈다. 답을 만들지 않으므로 뒤 경로는 `unavailable` 로 닫히고,
        # 이 시험이 보는 것은 **무엇을 넘겼나** 하나다.
        seen.append(evidence)
        return None

    router = Router(
        providers={"anthropic": _Fake()},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    eng = AnswerEngine(ArchiveStore(tmp_path), router, specialist=hook)
    ctx = _channel_ctx(readable_workspaces=frozenset({"mgmt"}), is_root=True)

    eng.respond(QUESTION, ctx, Intent("summary"))

    assert seen, "전문 봇 훅이 호출되지 않았다 — 이 시험이 아무것도 못 지킨다"
    handed = "\n".join(seen)
    assert "집행률 72%" in handed, "현재 채널 근거가 빠졌다"
    assert "옆 채널" not in handed
    assert "경영본부 원본 회의록" not in handed


def test_one_accessible_document_with_no_candidate_is_not_an_empty_channel(tmp_path, labels):
    """**접근 가능 문서 1건 · 후보 0건**을 「아카이브 없음」 으로 뒤집지 않는다.

    문서 집합 요약(`document_query`)이 `visible_docs` 를 소비하면서 비운다. 그
    비워진 목록으로 존재 여부를 세면, 채널에 원문이 멀쩡히 있는데도
    「이 채널에는 아직 아카이브된 원문이 없습니다」 가 나간다 — 사람은 수집이
    멈춘 줄 알고 엉뚱한 곳을 본다.
    """
    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "어제 점심 메뉴 이야기")
    eng, _fake = _engine(tmp_path)

    ans = eng.summarize(
        _channel_ctx(), days=7, question="기성금 정산서 종합해줘",
        document_query=["기성금 정산서"], terms=["기성금 정산서"],
    )

    assert ans.reason == "no_hits", ans.reason
    assert "현재 채널에서 해당 근거를 찾지 못했습니다." in ans.text
    assert "아직 아카이브된 원문이 없습니다" not in ans.text


def test_a_named_workspace_with_no_documents_is_not_a_permission_problem(tmp_path, labels):
    """DM 에서 라벨 필터가 전부 걷어낸 것은 **권한 문제가 아니다.**

    필터는 질문을 좁히는 것이지 접근 경계가 아니다. 「봇을 초대하세요」 가 나가면
    사람은 이미 들어가 있는 채널에 다시 초대를 건다.
    """
    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "전산 업무 이야기")
    eng, _fake = _engine(tmp_path)

    ans = eng.respond("경영본부 워크스페이스에서만 요약해줘", _dm_ctx(), Intent("summary"))

    assert ans.reason == "no_hits", ans.reason
    assert "/invite" not in ans.text


# --- 4b. 콘솔 소비자도 사유를 보존한다 -----------------------------------------


def test_the_console_does_not_refile_a_lookup_failure_as_missing_data():
    """콘솔이 다시 `no_hits` 로 접으면 고친 구분이 화면에서 사라진다."""
    from tybot.console.answer_records import quality_reasons

    row = {"reason": "permission_unverified", "hits": 0, "elapsed_ms": 10}

    assert quality_reasons(row) == ["permission_unverified"]
    assert "no_hits" not in quality_reasons(row)
    # 자료 쪽 문제는 그대로 `no_hits` 다 — 넓히면 구분한 의미가 없다.
    assert quality_reasons({"reason": "channel_no_archive", "hits": 0}) == ["no_hits"]
    assert quality_reasons({"reason": "no_access", "hits": 0}) == ["no_hits"]


def test_the_grounded_rate_does_not_count_questions_we_never_searched():
    """열람 범위를 못 본 질문을 근거율 분모에 넣으면 **연결 장애가 자료 문제로 보인다.**"""
    from tybot.console import health

    answered = [{"reason": "answered", "hits": 3}] * health.MIN_SAMPLE
    records = [*answered, {"reason": "permission_unverified", "hits": 0}]

    out = health.answer_section(records)

    assert out["permissionUnverified"] == 1
    assert out["searched"] == health.MIN_SAMPLE
    assert out["noHits"] == 0, "찾아보지도 못한 질문을 근거 없음으로 세면 안 된다"
    assert out["groundedRate"] == 1.0
    assert any("열람 범위를 확인하지 못해" in p for p in out["problems"])

    # 전부 확인 실패인 경우. **0 으로 나누지 않고**, 근거율이 0% 라고도 하지 않는다 —
    # 한 번도 안 찾아봤으므로 비율이 없다.
    everything = [{"reason": "permission_unverified", "hits": 0}] * health.MIN_SAMPLE
    allfail = health.answer_section(everything)

    assert allfail["searched"] == 0
    assert allfail["permissionUnverified"] == health.MIN_SAMPLE
    assert allfail["level"] == "warn"
    assert allfail["groundedRate"] is None


@pytest.mark.parametrize(("question", "expected"), [
    (QUESTION, None),
    ("경영본부 회의가 어느 워크스페이스에 공유됐어?", None),
    ("경영본부 워크스페이스에서만 요약해줘", frozenset({"mgmt"})),
    ("지금 전산팀 워크스페이스에서는 무슨 일이 벌어지고 있어?", frozenset({"tyit"})),
    ("workspace: mgmt 회의를 요약해줘", frozenset({"mgmt"})),
    ("워크스페이스=전산팀", frozenset({"tyit"})),
    ("경영본부 회의를 전산팀 워크스페이스에서 찾아줘", frozenset({"tyit"})),
    ("othermgmt workspace 내용을 요약해줘", None),
])
def test_dm_topic_and_explicit_workspace_selection_are_distinct(labels, question, expected):
    from tybot.answer import _named_workspace_filter

    assert _named_workspace_filter(question, _dm_ctx()) == expected
    assert _named_workspace_filter(question, _channel_ctx()) is None


def test_dm_org_topic_keeps_all_authorized_channel_sources(tmp_path, labels):
    _write(tmp_path, "tyit", "#전산_업무", CHANNEL_ID, "경영본부 회의 업무 공유")
    _write(tmp_path, "tyit", "#전산_회계", "C0ACC000001", "경영본부 회의 회계 공유")
    _write(tmp_path, "mgmt", "#경영_내부", OTHER_ID, "접근 불가 원문")
    eng, fake = _engine(tmp_path)

    ans = eng.respond(QUESTION, _dm_ctx(), Intent("summary"))

    assert ans.reason == "answered"
    evidence = fake.calls[0][1].content
    assert "업무 공유" in evidence and "회계 공유" in evidence
    assert "접근 불가 원문" not in evidence
