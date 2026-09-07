"""기간 요약 + exec 통합조회 — 권한/출처가 지켜지는지 검증."""
from __future__ import annotations

import datetime as dt

import pytest

from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router

TODAY = dt.date.today()
OLD = TODAY - dt.timedelta(days=60)


def _doc(channel: str, lines: list[tuple[str, str, str]]) -> str:
    body = "\n".join(f"> [{ts}] {who}: {what}" for ts, who, what in lines)
    return (
        "---\n"
        "workspace: pilot\n"
        f'channel: "{channel}"\n'
        "visibility: private\n"
        f"acl: [{channel}]\n"
        f"doc_count: {len(lines)}\n"
        "last_ingested: 2026-08-19T17:00+09:00\n"
        "---\n\n"
        "## 요약 (사람이 관리, 봇은 수정 금지)\n-\n\n"
        "## 원문 (자동 취합, 편집 금지)\n" + body + "\n"
    )


class FakeProvider:
    name = "anthropic"

    def __init__(self):
        self.calls: list[list[Message]] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append(list(messages))
        return LLMResponse("정리 결과", spec.model, self.name, 500, 100, 0.005)


@pytest.fixture
def engine(tmp_path):
    base = tmp_path / "channels" / "pilot"
    base.mkdir(parents=True)
    (base / "자금.md").write_text(
        _doc(
            "#팀_자금(ABB540)_주간보고",
            [
                (f"{TODAY} 09:15", "홍길동", "김해외동 기성금 3억 2천만원 청구 완료"),
                (f"{OLD} 10:00", "홍길동", "작년 예산 회의 내용"),
            ],
        ),
        encoding="utf-8",
    )
    (base / "현장.md").write_text(
        _doc("#현장_김해외동(180182)_채팅방", [(f"{TODAY} 11:00", "이순신", "3공구 골조 완료")]),
        encoding="utf-8",
    )
    fake = FakeProvider()
    router = Router(
        providers={"anthropic": fake},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    return AnswerEngine(ArchiveStore(tmp_path), router), fake


def test_summary_covers_only_member_channels(engine):
    eng, fake = engine
    ctx = RequestContext(workspace="pilot", channels=frozenset({"#팀_자금(ABB540)_주간보고"}))
    ans = eng.summarize(ctx, days=7)
    assert ans.reason == "answered"
    evidence = fake.calls[0][1].content
    assert "기성금 3억 2천만원" in evidence
    assert "골조" not in evidence  # 멤버가 아닌 채널은 근거에서 제외
    assert len(ans.citations) == 1


def test_summary_exec_sees_all_channels(engine):
    eng, fake = engine
    ans = eng.summarize(RequestContext(workspace="pilot", role="exec"), days=7)
    evidence = fake.calls[0][1].content
    assert "기성금 3억 2천만원" in evidence and "골조" in evidence
    assert len(ans.citations) == 2


def test_summary_respects_period(engine):
    eng, fake = engine
    eng.summarize(RequestContext(workspace="pilot", role="exec"), days=7)
    assert "작년 예산 회의" not in fake.calls[0][1].content  # 기간 밖 원문 제외


def test_summary_without_permission_leaks_nothing(engine):
    eng, fake = engine
    ans = eng.summarize(RequestContext(workspace="pilot", channels=frozenset()), days=7)
    assert ans.reason == "no_access"
    assert "김해외동" not in ans.text and "자금" not in ans.text
    assert fake.calls == []


def test_summary_has_citations_in_slack_output(engine):
    eng, _ = engine
    ans = eng.summarize(RequestContext(workspace="pilot", role="exec"), days=7)
    out = ans.to_slack()
    assert "출처:" in out and "📄" in out


def _bare_router():
    """이 절의 테스트는 모델 문장이 아니라 **출처 문자열**을 본다."""
    return Router(
        providers={"anthropic": FakeProvider()},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )


# ---------------------------------------------------------------------------
# 출처가 실제 파일을 가리키는가
# ---------------------------------------------------------------------------


def _v2_doc(
    channel: str, channel_id: str, date: str, lines: list[tuple[str, str]],
    ingested: str = "",
) -> str:
    body = "\n".join(f"> [{date} {hhmm}] 홍길동: {text}" for hhmm, text in lines)
    return (
        "---\n"
        "workspace: pilot\n"
        f'channel: "{channel}"\n'
        f"channel_id: {channel_id}\n"
        "schema_version: 2\n"
        f"source_date: {date}\n"
        "visibility: private\n"
        f'acl: ["{channel}"]\n'
        f"last_ingested: {ingested or date}T17:00+09:00\n"
        "---\n\n"
        "## 요약 (사람이 관리, 봇은 수정 금지)\n-\n\n"
        "## 원문 (자동 취합, 편집 금지)\n"
        f"{body}\n"
    )


def test_the_citation_names_the_file_the_line_came_from(tmp_path):
    """`_merge()` 는 여러 일자 파일을 한 논리 채널로 합치고 `path` 에는 대표 파일

    하나만 남긴다. 그걸 출처로 쓰면 **파일명과 날짜가 서로 다른 것을 가리킨다** —
    `📄2026-09-03.md(2026-09-07)` 처럼(2026-09-07 실제 발생). 3일 자 문서를 찾으러
    가면 없고, 오류는 안 난다.
    """
    from datetime import date, timedelta

    from tybot.access import RequestContext
    from tybot.answer import AnswerEngine
    from tybot.archive.store import ArchiveStore

    channel = "#팀_전산(ABB155)_공지"
    old_day = (date.today() - timedelta(days=4)).isoformat()
    new_day = date.today().isoformat()
    root = tmp_path / "workspaces" / "pilot" / "channels" / "C1__공지" / "raw"
    root.mkdir(parents=True)
    # **옛 날짜 파일을 나중에 다시 수집한 상태를 만든다.**
    #
    # `_merge()` 는 대표 파일을 `(last_ingested, 마지막 줄 시각)` 으로 고른다.
    # 그래서 backfill 로 옛 파일이 더 늦게 수집되면 **대표 파일은 옛 날짜인데
    # 마지막 줄은 새 날짜**가 되고, 출처가 서로 다른 것을 가리킨다.
    (root / f"{old_day}.md").write_text(
        _v2_doc(
            channel, "C1", old_day, [("09:00", "예산 편성 시작")], ingested=new_day
        ),
        encoding="utf-8",
    )
    (root / f"{new_day}.md").write_text(
        _v2_doc(channel, "C1", new_day, [("09:00", "예산 확정")], ingested=old_day),
        encoding="utf-8",
    )

    engine = AnswerEngine(ArchiveStore(tmp_path), _bare_router())
    answer = engine.summarize(
        RequestContext(workspace="pilot", channels=frozenset({channel})), days=7
    )

    assert answer.citations, "출처가 비었다"
    citation = answer.citations[0]
    # 파일명과 날짜가 같은 날을 가리켜야 한다.
    assert f"{new_day}.md({new_day})" in citation, citation
    # 여러 날에 걸쳤으면 밝힌다 — 한 파일만 적으면 근거가 실제보다 좁아 보인다.
    assert "외 1일" in citation, citation


def test_a_single_day_citation_has_no_span_suffix(tmp_path):
    """한 날짜뿐이면 「외 N일」 을 붙이지 않는다. 없는 범위를 말하면 안 된다."""
    from datetime import date

    from tybot.access import RequestContext
    from tybot.answer import AnswerEngine
    from tybot.archive.store import ArchiveStore

    channel = "#팀_전산(ABB155)_공지"
    today = date.today().isoformat()
    root = tmp_path / "workspaces" / "pilot" / "channels" / "C1__공지" / "raw"
    root.mkdir(parents=True)
    (root / f"{today}.md").write_text(
        _v2_doc(channel, "C1", today, [("09:00", "예산 확정")]), encoding="utf-8"
    )

    engine = AnswerEngine(ArchiveStore(tmp_path), _bare_router())
    answer = engine.summarize(
        RequestContext(workspace="pilot", channels=frozenset({channel})), days=7
    )

    assert "외" not in answer.citations[0], answer.citations[0]
