"""출처 사이드카 — **원문 밖에서, 배치와 무관하게, 되짚을 수 있게.**

PF 자료에는 Slack `message_ts` 가 하나도 없다. 그래서 변환된 줄을 원본으로
되짚는 길은 `legacy_source_ref` 뿐이고, 그게 틀리면 되짚을 방법이 아주 없다.

여기서 지키는 것 셋.

1. **원문을 건드리지 않는다** — 출처는 사이드카로 나가고, `ArchiveStore` 도
   검색도 그 파일을 보지 않는다
2. **배치를 타지 않는다** — 구조 1 과 구조 2 에서 경로도 바이트도 같다.
   다르면 「어느 배치로 만들었나」 가 되짚기에 영향을 주게 된다
3. **지문이 내용을 가리킨다** — 내용이 바뀌면 스냅샷 해시가 바뀌고, 변환한
   사람의 자리(절대 경로)가 바뀐다고 해서 바뀌지는 않는다
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import archive_layout_convert as conv

from tybot.access import RequestContext
from tybot.archive.store import ArchiveStore
from tybot.specialist_tools import ToolBox

PF_CHANNEL = """# #팀_자금(ABB540)_주간보고

## 2026-06

**2026-06-11 08:58 · 홍길동**
기성 청구분 정리했습니다

---

**2026-06-11 09:10 · 김과장**
확인했습니다
📎 첨부: `기성내역.xlsx`

---

**2026-06-12 10:00 · 홍길동**
확인했습니다

---

## 참여 기록 (요약)
- 홍길동 2건
- 김과장 1건
"""

SECOND = """# #팀_전산(ABB100)_공지

## 2026-06

**2026-06-11 08:58 · 전산팀**
서버 점검 예정

---
"""


def _pf_source(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """PF 스냅샷 모양의 입력. **사내 원문을 시험에 넣지 않는다.**"""
    root = tmp_path / "snap" / "slack-export" / "channels"
    root.mkdir(parents=True, exist_ok=True)
    for name, text in (files or {"자금.md": PF_CHANNEL, "공지.md": SECOND}).items():
        (root / name).write_text(text, encoding="utf-8")
    return tmp_path / "snap"


def _build(source: Path, out: Path, layout: str):
    channels, _ = conv.read_pf(source, workspace="pf")
    return channels, conv.build(
        channels, out, layout, source_kind="pf_git_snapshot", source_commit="deadbeef"
    )


def _rows(out: Path, channel_id: str) -> list[dict]:
    path = out / "workspaces" / "pf" / "provenance" / f"{channel_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- 스냅샷 지문 -------------------------------------------------------------

def _digest(source: Path) -> str:
    files = sorted((source / "slack-export" / "channels").glob("*.md"))
    return conv.snapshot_digest(files, source)


def test_one_character_change_moves_the_digest(tmp_path):
    """크기가 같아도 내용이 다르면 다른 스냅샷이다.

    전에는 파일명과 크기만 해싱했다. 그러면 이 경우가 **같은 지문**으로 나오고,
    되짚으러 간 사람이 다른 내용을 보고도 맞는 줄 안다.
    """
    a = _pf_source(tmp_path / "a", {"자금.md": PF_CHANNEL})
    changed = PF_CHANNEL.replace("기성 청구분", "기성 청구뷴")
    assert len(changed) == len(PF_CHANNEL), "크기가 같은 경우를 재고 있어야 한다"
    b = _pf_source(tmp_path / "b", {"자금.md": changed})

    assert _digest(a) != _digest(b)


def test_same_content_at_a_different_relative_path_moves_the_digest(tmp_path):
    """어느 채널 파일이었는지도 스냅샷의 일부다."""
    a = _pf_source(tmp_path / "a", {"자금.md": PF_CHANNEL})
    b = _pf_source(tmp_path / "b", {"공지.md": PF_CHANNEL})

    assert _digest(a) != _digest(b)


def test_absolute_path_does_not_move_the_digest(tmp_path):
    """같은 사본을 다른 자리에서 변환해도 같은 지문이다.

    아니면 지문이 자료가 아니라 **변환한 사람의 홈 디렉터리**를 가리키게 된다.
    """
    files = {"자금.md": PF_CHANNEL, "공지.md": SECOND}
    a = _pf_source(tmp_path / "여기", files)
    b = _pf_source(tmp_path / "저기_다른_이름", files)

    assert _digest(a) == _digest(b)


def test_digest_is_full_64_hex(tmp_path):
    """내부 기록은 줄이지 않는다. 앞자리만 남기면 언젠가 부딪힌다."""
    digest = _digest(_pf_source(tmp_path))
    assert len(digest) == 64
    assert conv.short(digest) == digest[:12]


def test_prefix_ambiguity_does_not_collide(tmp_path):
    """이름을 갈라 붙인 다른 스냅샷이 같은 지문을 내지 않는다.

    경로와 내용 사이에 길이를 끼우는 이유가 이것이다. 없으면 `ab`+`c` 와
    `a`+`bc` 가 같은 바이트열이 된다.
    """
    a = _pf_source(tmp_path / "a", {"ab.md": PF_CHANNEL, "c.md": SECOND})
    b = _pf_source(tmp_path / "b", {"a.md": PF_CHANNEL, "bc.md": SECOND})

    assert _digest(a) != _digest(b)


# --- 사이드카 ---------------------------------------------------------------

def test_two_runs_of_the_same_input_are_byte_identical(tmp_path):
    """같은 입력을 두 번 변환하면 사이드카가 **바이트로** 같다.

    `sort_keys` 나 LF 가 빠지면 여기서 걸린다. 플랫폼이나 파이썬 판이 바뀔 때
    조용히 달라지는 것이 가장 나쁘다 — 두 배치 비교에서 「구조 차이」 로 보인다.
    """
    source = _pf_source(tmp_path)
    _build(source, tmp_path / "one", "a")
    _build(source, tmp_path / "two", "a")

    assert conv.provenance_files(tmp_path / "one") == conv.provenance_files(tmp_path / "two")


def test_layout_a_and_b_sidecars_are_byte_identical(tmp_path):
    """구조가 달라도 출처는 같은 사실이다."""
    source = _pf_source(tmp_path)
    _build(source, tmp_path / "a", "a")
    _build(source, tmp_path / "b", "b")

    fa, fb = conv.provenance_files(tmp_path / "a"), conv.provenance_files(tmp_path / "b")
    assert set(fa) == set(fb)
    assert fa == fb
    assert fa, "사이드카가 비어 있으면 위 비교는 아무것도 말하지 않는다"


def test_no_crlf_anywhere(tmp_path):
    """LF 로 쓴다. 텍스트 모드로 쓰면 Windows 에서 CRLF 가 된다."""
    source = _pf_source(tmp_path)
    _build(source, tmp_path / "a", "a")

    for name, blob in conv.provenance_files(tmp_path / "a").items():
        assert b"\r" not in blob, name


def test_exactly_one_row_per_message(tmp_path):
    """메시지마다 출처 한 줄. 더도 덜도 아니다."""
    source = _pf_source(tmp_path)
    channels, _ = _build(source, tmp_path / "a", "a")

    for ch in channels:
        rows = _rows(tmp_path / "a", ch.stable_id)
        assert len(rows) == len(ch.messages), ch.channel


def test_ordinal_and_record_hash_follow_the_raw_order(tmp_path):
    """순번과 해시가 **원문 순서 그대로**여야 한다.

    `ordinal` 만 보면 줄이 하나 끼어들었을 때 그 뒤가 전부 어긋난 채 맞아 보이고,
    `record_sha256` 만 보면 같은 문장이 두 번 나온 채널에서 어느 쪽인지 못 가린다.
    둘을 함께 쓰는 이유다.
    """
    source = _pf_source(tmp_path)
    channels, _ = _build(source, tmp_path / "a", "a")

    for ch in channels:
        rows = _rows(tmp_path / "a", ch.stable_id)
        assert [row["ordinal"] for row in rows] == list(range(len(ch.messages)))
        assert [row["record_sha256"] for row in rows] == [m.key() for m in ch.messages]
        assert [row["source_timestamp_text"] for row in rows] == [m.ts for m in ch.messages]


def test_row_shape_matches_the_agreed_contract(tmp_path):
    """칸 이름과 값의 모양. 합의한 계약이라 시험으로 고정한다."""
    source = _pf_source(tmp_path, {"자금.md": PF_CHANNEL})
    _build(source, tmp_path / "a", "a")
    row = _rows(tmp_path / "a", conv.legacy_channel_id("pf", "#팀_자금(ABB540)_주간보고"))[0]

    assert row["schema_version"] == conv.PROVENANCE_SCHEMA
    # `ts` 가 아니라 `source_timestamp_text` — Slack `message_ts` 와 혼동된다
    assert "ts" not in row
    assert row["source_timestamp_text"] == "2026-06-11 08:58"
    # PF 자료에 좌표가 없다. 빈 문자열이 아니라 **null** 이다
    assert row["message_ts"] is None

    ref = row["legacy_source_ref"]
    assert ref["kind"] == "pf_git_snapshot"
    assert len(ref["snapshot_sha256"]) == 64
    assert ref["source_path"] == "slack-export/channels/자금.md"
    assert ref["source_line"] == 5
    assert len(row["record_sha256"]) == 64


def test_source_path_is_relative_posix_never_absolute(tmp_path):
    """절대 경로를 넣지 않는다. 넣으면 배치 사이 바이트가 갈린다."""
    source = _pf_source(tmp_path)
    channels, _ = _build(source, tmp_path / "a", "a")

    for ch in channels:
        for row in _rows(tmp_path / "a", ch.stable_id):
            path = row["legacy_source_ref"]["source_path"]
            assert not path.startswith("/")
            assert ":" not in path          # C:\ 같은 드라이브 문자
            assert "\\" not in path


def test_manifest_keeps_commit_and_snapshot_apart(tmp_path):
    """git 커밋과 스냅샷 해시는 **다른 값**이고 다른 칸이다.

    커밋은 「저장소의 어느 지점」, 스냅샷 해시는 「변환에 실제로 넣은 내용」 이다.
    작업 디렉터리가 더러우면 둘이 갈라지고, 그때 믿을 것은 스냅샷 해시다.
    """
    source = _pf_source(tmp_path)
    _build(source, tmp_path / "a", "a")
    manifest = json.loads(
        (tmp_path / "a" / "workspaces" / "pf" / "provenance" / "manifest.json")
        .read_text(encoding="utf-8")
    )

    assert manifest["source_commit"] == "deadbeef"
    assert manifest["source_snapshot_sha256"] == _digest(source)
    assert manifest["source_commit"] != manifest["source_snapshot_sha256"]
    assert manifest["source_kind"] == "pf_git_snapshot"
    assert manifest["channel_count"] == 2
    assert manifest["message_count"] == 4


# --- 원문 경로에 새지 않는다 -------------------------------------------------

@pytest.mark.parametrize("layout", ["a", "b"])
def test_provenance_never_becomes_evidence(tmp_path, layout):
    """`ArchiveStore` 도 검색도 사이드카를 보지 않는다.

    `channels/` 밖에 두는 이유가 이것이다. 확장자가 `.md` 가 아니라서 안전한
    것이 아니다 — 그건 지금 글롭의 성질이지 구조의 성질이 아니다.
    """
    source = _pf_source(tmp_path)
    out = tmp_path / layout
    _build(source, out, layout)

    store = ArchiveStore(out)
    assert not [p for p in store.source_files() if p.suffix in {".jsonl", ".json"}]

    ctx = RequestContext(workspace="pf", role="exec")
    tools = ToolBox(store=store, ctx=ctx)
    # 사이드카에만 있는 문자열이다. 원문에는 한 번도 안 나온다.
    #
    # 도구 출력 문자열을 보지 않고 **근거 줄**을 본다. 못 찾았다는 답에도 질문한
    # 낱말이 그대로 들어가므로, 문자열로 보면 늘 실패한다.
    for needle in ("pf_git_snapshot", "record_sha256", _digest(source)[:20]):
        assert store.search(needle, ctx, limit=20) == []
        assert "찾은 것이 없습니다" in tools.run("search", {"query": needle})


def test_raw_lines_carry_no_provenance(tmp_path):
    """원문 줄에 출처를 끼우지 않는다 — 원문 보존(절대 원칙 1)."""
    source = _pf_source(tmp_path)
    _build(source, tmp_path / "a", "a")

    for path in (tmp_path / "a" / "workspaces" / "pf" / "channels").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "snapshot" not in text
        assert "slack-export" not in text


# --- 식별자와 해시 -----------------------------------------------------------

def test_legacy_id_is_a_hash_not_a_slug():
    """자리표시자 ID 는 이름을 그대로 쓰지 않는다.

    `legacy-` 접두어는 남긴다 — 이 값이 **Slack ID 가 아니라는 사실**이 경로
    이름만 보고도 드러나야 한다. 사람이 이걸 들고 Slack 에서 찾으면 안 된다.
    """
    got = conv.legacy_channel_id("pf", "#팀_자금(ABB540)_주간보고")

    assert got.startswith("legacy-")
    assert "자금" not in got
    assert len(got) == len("legacy-") + 16
    assert got == conv.legacy_channel_id("pf", "#팀_자금(ABB540)_주간보고")


def test_channels_that_slugify_the_same_get_different_ids():
    """slug 를 쓰면 합쳐지던 두 채널이 이제 갈린다.

    `_slugify` 는 경로에 못 쓰는 글자를 지운다. 그래서 그 글자만 다른 두 채널이
    **같은 파일**이 됐다. 권한이 다른 두 채널이 합쳐지면 그건 유출이다
    (절대 원칙 3).
    """
    from tybot.archive.writer import _slugify

    a, b = "#팀_자금/주간", "#팀_자금:주간"
    assert _slugify(a) == _slugify(b), "이 시험이 재는 상황이 아니다"
    assert conv.legacy_channel_id("pf", a) != conv.legacy_channel_id("pf", b)


def test_same_channel_name_in_different_workspaces_gets_different_ids():
    """워크스페이스가 다르면 다른 채널이다."""
    assert conv.legacy_channel_id("pf", "#공지") != conv.legacy_channel_id("tyit", "#공지")


def test_duplicate_stable_id_stops_before_writing(tmp_path):
    """겹치면 **쓰기 전에** 멈춘다.

    겹친 채로 쓰면 뒤에 쓴 채널이 앞의 것을 덮는다. 구조 1 은 파일 하나라 통째로
    사라지고, 구조 2 는 같은 날짜 파일만 덮여 일부만 사라진다. 어느 쪽도 오류를
    내지 않는다 — 파일은 멀쩡하고 줄만 없다.
    """
    msg = conv.Message(ts="2026-06-11 09:00", speaker="가", text="나")
    twin = [
        conv.Channel(
            workspace="pf", channel="#같은이름", channel_id="C1",
            visibility="private", acl=frozenset(), share_with=frozenset(),
            messages=[msg],
        )
        for _ in range(2)
    ]
    twin[1].channel = "#다른이름"      # 이름은 달라도 ID 가 같다

    out = tmp_path / "out"
    with pytest.raises(conv.DuplicateChannelId) as caught:
        conv.build(twin, out, "a", source_kind="ty_archive")

    assert "pf/C1" in str(caught.value)
    assert not out.exists(), "멈추기 전에 아무것도 쓰지 않아야 한다"


def test_canonical_digest_is_not_fooled_by_separators_in_the_body():
    """칸을 이어 붙여 해싱하지 않는다.

    전에는 `"|".join([ts, speaker, text, …])` 이었다. 본문에 `|` 가 들어가면 칸
    경계가 밀려 서로 다른 메시지가 같은 해시를 냈다. 우리 자료에서 `|` 는 표를
    붙여 넣을 때 흔하다.
    """
    left = conv.Message(ts="2026-06-11 09:00", speaker="가", text="나|다")
    right = conv.Message(ts="2026-06-11 09:00", speaker="가|나", text="다")

    assert left.key() != right.key()


def test_message_key_and_tally_share_one_serialization(tmp_path):
    """검증용 해시도 같은 규칙을 쓴다.

    규칙이 갈리면 한쪽만 고쳤을 때 검증이 조용히 약해진다 — 통과는 계속 하는데
    잡아내던 것을 못 잡는다.
    """
    payload = {"ts": "2026-06-11 09:00", "speaker": "가", "text": "나"}
    assert conv.canonical_digest(payload) == conv.canonical_digest(dict(reversed(list(payload.items()))))
    assert conv.canonical_json(payload) == b'{"speaker":"\xea\xb0\x80","text":"\xeb\x82\x98","ts":"2026-06-11 09:00"}'
