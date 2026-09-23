"""두 저장 구조가 **Hermes 에게 같게 보이는가.**

실측을 성능 비교가 아니라 동등성 증명으로 하기로 했다(설계
`docs/design/archive-layout-benchmark.md` §5). 이유는 도구 층이 배치를 지우기
때문이다 — `ArchiveStore.docs()` 가 일자 파일을 병합하므로 Hermes 가 받는 것은
어느 구조든 같은 문자열이어야 한다.

같으면 토큰도 시간도 같다. **재는 것이 아니라 증명하는 것**이고, 모델을 부르지
않으므로 공짜이며 결정적이다. 그리고 다르게 나오면 그것은 구조 발견이 아니라
**버그**다 — 두 배치가 같은 내용을 담고 있지 않다는 뜻이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import archive_layout_convert as conv

from tybot.access import RequestContext
from tybot.archive.store import ArchiveStore
from tybot.specialist_tools import ToolBox

WS = "tyit"


def _msg(day: str, hhmm: str, speaker: str, text: str, **kw) -> conv.Message:
    return conv.Message(ts=f"{day} {hhmm}", speaker=speaker, text=text, **kw)


def _channels() -> list[conv.Channel]:
    """합성 자료. **사내 원문을 테스트에 넣지 않는다.**

    날짜가 여러 개인 채널을 반드시 둔다 — 구조 2 에서 파일이 갈리고 `_merge()` 가
    다시 붙이는 경로가 바로 그것이라, 하루짜리만 두면 시험이 아무것도 안 본다.
    """
    busy = conv.Channel(
        workspace=WS,
        channel="#팀_자금(ABB540)_주간보고",
        channel_id="C0FUND",
        visibility="public",
        acl=frozenset({"#팀_자금(ABB540)_주간보고"}),
        share_with=frozenset(),
        messages=[
            _msg("2026-09-01", "09:10", "김자금", "기성 청구분 정리했습니다"),
            _msg("2026-09-01", "09:12", "박과장", "기성 금액 확인 부탁"),
            _msg("2026-09-02", "14:00", "김자금", "타설 일정 변경 공유"),
            _msg("2026-09-02", "14:05", "박과장", "확인", attachments=("일정표.xlsx",)),
            _msg("2026-09-30", "08:00", "김자금", "월말 마감 보고 올립니다"),
        ],
    )
    quiet = conv.Channel(
        workspace=WS,
        channel="#현장_김해외동(180182)_채팅방",
        channel_id="C0SITE",
        visibility="private",
        acl=frozenset({"#현장_김해외동(180182)_채팅방"}),
        share_with=frozenset(),
        messages=[
            _msg("2026-09-01", "07:00", "현장소장", "기성 검측 오전 진행"),
        ],
    )
    # 같은 문장이 두 번 나오는 채널. 해시를 **집합**으로 세면 통과해 버리는
    # 경우라, 중복이 두 배치에서 똑같이 보존되는지 여기서 본다.
    dup = conv.Channel(
        workspace=WS,
        channel="#팀_전산(ABB100)_공지",
        channel_id="C0IT",
        visibility="public",
        acl=frozenset({"#팀_전산(ABB100)_공지"}),
        share_with=frozenset(),
        messages=[
            _msg("2026-09-03", "10:00", "전산팀", "서버 점검 예정"),
            _msg("2026-09-04", "10:00", "전산팀", "서버 점검 예정"),
        ],
    )
    return [busy, quiet, dup]


@pytest.fixture
def batches(tmp_path: Path) -> tuple[ArchiveStore, ArchiveStore]:
    channels = _channels()
    a, b = tmp_path / "a", tmp_path / "b"
    # `build` 를 쓴다. `emit_*` 만 부르면 출처 사이드카가 안 써지고, 그러면 이
    # 시험이 **운영에서 나올 모양과 다른 것**을 비교하게 된다.
    conv.build(channels, a, "a", source_kind="ty_archive")
    conv.build(channels, b, "b", source_kind="ty_archive")
    return ArchiveStore(a), ArchiveStore(b)


def _ctx(**kw) -> RequestContext:
    base = {"workspace": WS, "channels": frozenset({"C0FUND", "C0SITE", "C0IT"})}
    base.update(kw)
    return RequestContext(**base)


CASES: list[tuple[str, dict]] = [
    # 2글자 한국어 — pg_bigm 을 고른 이유이자 우리 쓰임에서 가장 흔한 모양
    ("search", {"query": "기성"}),
    ("search", {"query": "서버 점검"}),
    ("search", {"query": "존재하지않는문자열zzz"}),
    ("search", {"query": "기성", "channel": "#팀_자금(ABB540)_주간보고"}),
    ("read_channel", {"channel": "#팀_자금(ABB540)_주간보고"}),
    ("read_channel", {"channel": "#팀_자금(ABB540)_주간보고", "limit": 400}),
    ("read_channel", {"channel": "#현장_김해외동(180182)_채팅방"}),
    ("read_channel", {"channel": "없는채널"}),
    ("read_document", {"name": "#팀_전산(ABB100)_공지"}),
]


@pytest.mark.parametrize(("tool", "args"), CASES, ids=lambda v: str(v)[:40])
def test_tool_output_is_byte_identical(batches, tool, args):
    """Hermes 가 받는 문자열이 두 구조에서 **같은 바이트**인가."""
    store_a, store_b = batches
    ctx = _ctx()
    out_a = ToolBox(store=store_a, ctx=ctx).run(tool, args)
    out_b = ToolBox(store=store_b, ctx=ctx).run(tool, args)
    assert out_a == out_b, f"{tool}{args} 가 구조에 따라 다르다"


@pytest.mark.parametrize(
    "ctx",
    [
        _ctx(channels=frozenset({"C0FUND"})),
        _ctx(channels=frozenset(), role="exec"),
        _ctx(channels=frozenset()),
    ],
    ids=["one-channel", "exec", "no-channel"],
)
def test_visible_range_is_identical(batches, ctx):
    """**보이는 범위**가 두 구조에서 같은가.

    구조가 바뀌면 프론트매터가 있는 파일 수가 바뀌고, 그때 `_merge()` 의 ACL
    합집합이 달라질 수 있다. 내용은 같은데 보이는 범위가 달라지는 것이 가장 나쁜
    실패다 — 한쪽에서만 새고, 아무 오류도 안 난다.
    """
    store_a, store_b = batches
    key = lambda doc: (doc.workspace, doc.channel_id, doc.visibility, tuple(sorted(doc.acl)))  # noqa: E731
    assert sorted(map(key, store_a.visible_docs(ctx))) == sorted(
        map(key, store_b.visible_docs(ctx))
    )
    for tool, args in CASES:
        assert ToolBox(store=store_a, ctx=ctx).run(tool, args) == ToolBox(
            store=store_b, ctx=ctx
        ).run(tool, args), f"{tool}{args}"


def test_structure_one_is_actually_read(batches):
    """구조 1 이 **정말 읽히는가.**

    이 시험이 따로 있는 이유: 처음 돌렸을 때 구조 1 은 `_files()` 글롭
    (`*/channels/*/raw/*.md`)에 걸리지 않아 **한 장도 안 읽혔다.** 그런데도 위
    비교는 통과할 수 있다 — 양쪽이 아무것도 안 읽으면 출력이 똑같이 빈 문자열이라
    「같다」 가 나온다. 동등성 시험은 비어 있는 것끼리도 같다고 말한다.
    """
    store_a, store_b = batches
    assert len(store_a.source_files()) == 3, "구조 1 은 채널당 파일 하나"
    assert len(store_b.source_files()) > 3, "구조 2 는 날짜별로 갈린다"
    lines_a = sum(len(d.raw_lines) for d in store_a.docs())
    assert lines_a == 8
    assert lines_a == sum(len(d.raw_lines) for d in store_b.docs())


def test_refuses_to_write_into_the_operational_archive(tmp_path: Path):
    """운영 경로에는 쓰지 않는다. 실측이 운영 자료를 건드리면 안 된다."""
    for guarded in ("/var/lib/tybot/archive", "/var/lib/tybot/archive/workspaces"):
        assert conv._refuse_operational(Path(guarded), "출력") is not None
    assert conv._refuse_operational(Path("/var/lib/tybot/archive-lab"), "출력") is None
    assert conv._refuse_operational(tmp_path, "출력") is None


def test_round_trip_through_our_own_reader(tmp_path: Path):
    """구조 2 로 쓴 것을 우리 reader 로 다시 읽어 구조 1 로 써도 같은가.

    `read_ty` 가 `ArchiveStore` 를 쓰는 이유가 여기 있다 — 새 파서를 만들면
    **reader 가 보는 것과 다른 것**을 옮기게 되고, 그건 오류 없이 「그 자료가
    없다」 로만 나타난다.
    """
    channels = _channels()
    b = tmp_path / "b"
    conv.build(channels, b, "b", source_kind="ty_archive")
    again, notes = conv.read_ty(b)
    a2 = tmp_path / "a2"
    conv.build(again, a2, "a", source_kind="ty_archive")
    conv.build(channels, tmp_path / "a", "a", source_kind="ty_archive")
    # 출처는 비교하지 않는다 — a2 는 b 를 읽어서 나왔으므로 `source_path` 가
    # b 의 일자 파일을 가리킨다. 그게 **맞는** 값이다. 원문만 같으면 된다.
    assert [p for p in conv.verify(tmp_path / "a", a2) if "출처" not in p] == [], notes
