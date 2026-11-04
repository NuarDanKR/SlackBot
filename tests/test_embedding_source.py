"""벡터 근거의 정본 창구 시험 (B-12).

**여기 쓰는 글은 전부 합성이다.** 실제 사내 원문·채널명·사람 이름을 넣지 않는다 —
시험 fixture 는 저장소에 남고, 저장소는 아카이브가 아니다(원칙 5).

시험이 붙들고 있는 것은 열 가지다. 번호는 지시의 요구 번호와 같다.
"""
from __future__ import annotations

import hashlib
import pathlib
from datetime import date

import pytest

from tybot.access import RequestContext
from tybot.archive import attachment_reader, embedding_source
from tybot.archive.chunking import split
from tybot.archive.embedding_source import (
    ATTACHMENT,
    CHANNEL,
    DM,
    SourceUnavailable,
    VectorSource,
    VisibleSources,
    iter_index_sources,
)
from tybot.archive.store import ArchiveStore
from tybot.embedding_index_store import prepare_chunks
from tybot.embedding_index_store import source_revision as writer_revision
from tybot.embedding_resolver import VisibleVectorSource, resolve_candidates
from tybot.embedding_search import VectorCandidate

WS = "pilot"
CTX = RequestContext(workspace=WS)
ME = RequestContext(workspace=WS, user_id="U1")
SOMEONE_ELSE = RequestContext(workspace=WS, user_id="U2")


# --- 합성 아카이브 -----------------------------------------------------------


def _channel(root, index: int = 0, *, days: int = 2, lines: int = 6) -> str:
    name = f"#팀_합성{index:03d}(ZZZ{index:03d})_생성"
    out = root / "workspaces" / WS / "channels" / f"C{index:08d}" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    for day in range(days):
        rows = "\n".join(
            f"> [2026-01-{day + 1:02d} 09:{i:02d} |17670{day}{i:04d}.0001] "
            f"화자{i % 3}: 합성 본문 {index}-{day}-{i}"
            for i in range(lines)
        )
        (out / f"2026-01-{day + 1:02d}.md").write_text(
            f'---\nworkspace: {WS}\nchannel: "{name}"\n'
            f"channel_id: C{index:08d}\nvisibility: public\nacl: []\n"
            f"last_ingested: 2026-01-{day + 1:02d}T17:00+09:00\n---\n\n"
            f"## 원문 (자동 취합, 편집 금지)\n{rows}\n",
            encoding="utf-8",
        )
    return name


def _attachment(
    root,
    index: int = 0,
    *,
    channel: int = 0,
    revision: str = "r1",
    state: str = "succeeded",
    converted: str = "2026-01-01T01:00+09:00",
    body: str | None = None,
) -> pathlib.Path:
    out = (
        root / "workspaces" / WS / "channels" / f"C{channel:08d}"
        / "attachments" / f"F{index:08d}"
    )
    out.mkdir(parents=True, exist_ok=True)
    text = body if body is not None else "\n".join(
        [f"# 합성 보고 {index}", "", "| 칸 | 값 |", "| --- | --- |", "| 가 | 1 |"]
    )
    path = out / f"{revision}.md"
    path.write_text(
        f"---\nkind: attachment\nworkspace: {WS}\nchannel_id: C{channel:08d}\n"
        f'channel: "#팀_합성{channel:03d}(ZZZ{channel:03d})_생성"\n'
        f"file_id: F{index:08d}\nfile_name: 합성{index}.pdf\nrevision: {revision}\n"
        f"visibility: public\nacl: \nconversion_state: {state}\n"
        f"message_ts: '170000000.{index:06d}'\nfiletype: pdf\nsha256: {'a' * 64}\n"
        f"staged_at: 2026-01-01T00:00+09:00\nconverted_at: {converted}\n"
        f"converter_name: synth\nconverter_version: '1'\n"
        f"human_upload_confirmed: true\n---\n\n# 합성{index}.pdf\n\n{text}\n",
        encoding="utf-8",
    )
    return path


def _dm(root, user: str = "U1", *, lines: int = 3) -> pathlib.Path:
    out = root / "workspaces" / WS / "dm" / user / "raw"
    out.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(
        f"> [2026-02-01 10:{i:02d} |17680000{i:04d}.0001] 본인: 개인 메모 {i}"
        for i in range(lines)
    )
    path = out / "2026-02-01.md"
    path.write_text(
        f'---\nworkspace: {WS}\nchannel: "DM:{user}"\nvisibility: private\n'
        f"acl: []\ndm_user: {user}\nlast_ingested: 2026-02-01T10:00+09:00\n---\n\n"
        f"## 원문\n{rows}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _attachment(tmp_path)
    return tmp_path


def _store(root) -> ArchiveStore:
    return ArchiveStore(root)


def _sources(root, **kwargs) -> dict[tuple[str, str, str], VectorSource]:
    found = iter_index_sources(_store(root), require_revision_db=False, **kwargs)
    return {(s.workspace, s.channel_id, s.doc_path): s for s in found}


def _candidate(source: VectorSource, chunk, number: int = 0) -> VectorCandidate:
    return VectorCandidate(
        workspace=source.workspace,
        channel_id=source.channel_id,
        doc_path=source.doc_path,
        source_revision=source.source_revision,
        chunker_revision="chunk-v1:max=1200:ov=120",
        model_revision="synth-model-1",
        chunk_no=number,
        start_offset=chunk.start,
        end_offset=chunk.end,
        text_sha256=hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
        distance=0.1,
    )


# --- 6. 본문 계약: UTF-8 · LF · NUL · offset ---------------------------------


def test_normalize_leaves_only_line_feeds_and_no_nul():
    assert embedding_source.normalize("가\r\n나\r다\x00라") == "가\n나\n다라"
    assert embedding_source.normalize("") == ""


def test_a_crlf_file_produces_line_feed_only_canonical_text(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    raw = next((tmp_path / "workspaces" / WS / "channels" / "C00000000" / "raw").iterdir())
    raw.write_bytes(raw.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))

    for source in _sources(tmp_path).values():
        assert "\r" not in source.text
        assert source.text.endswith("\n")


def test_a_nul_in_the_body_never_reaches_the_canonical_text(tmp_path, monkeypatch):
    """PostgreSQL `text` 가 NUL 을 못 받는다. 들어가면 색인 트랜잭션이 통째로 깨진다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path, lines=2)
    raw = sorted((tmp_path / "workspaces" / WS / "channels" / "C00000000" / "raw").iterdir())[0]
    raw.write_text(
        raw.read_text(encoding="utf-8").replace("합성 본문", "합성\x00본문"), encoding="utf-8"
    )

    texts = [source.text for source in _sources(tmp_path).values()]
    assert texts
    assert all("\x00" not in text for text in texts)


def test_the_source_revision_is_the_one_the_index_writer_computes(archive):
    for source in _sources(archive).values():
        assert source.source_revision == writer_revision(source.text)
        assert source.source_revision == hashlib.sha256(
            source.text.encode("utf-8")
        ).hexdigest()


def test_offsets_are_code_point_indices(tmp_path, monkeypatch):
    """바이트도 자소도 아니다. 이모지가 든 본문에서 그게 드러난다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _attachment(tmp_path, body="보고 🇰🇷 👨‍👩‍👧‍👦 합성 문장입니다.")
    _channel(tmp_path)

    source = next(s for s in _sources(tmp_path).values() if s.kind == ATTACHMENT)
    chunks = split(source.text)

    assert chunks
    for chunk in chunks:
        assert source.text[chunk.start : chunk.end] == chunk.text
    # 이 본문은 UTF-8 바이트 수가 코드포인트 수보다 많다 — 둘을 섞으면 깨진다.
    assert len(source.text.encode("utf-8")) > len(source.text)


def test_an_attachment_keeps_its_markdown_and_a_message_keeps_its_speaker(archive):
    """첨부는 본문 그대로다. 줄마다 파일명을 붙이면 표·제목이 깨져 청크가 경계를 잃는다."""
    sources = _sources(archive)
    attachment = next(s for s in sources.values() if s.kind == ATTACHMENT)
    channel = next(s for s in sources.values() if s.kind == CHANNEL)

    assert "| 칸 | 값 |" in attachment.text
    assert "| --- | --- |" in attachment.text
    assert "합성0.pdf:" not in attachment.text  # 화자 접두가 붙지 않았다
    # 사람 발언은 반대다 — 누가 언제 말했는지가 본문의 일부다.
    assert channel.text.startswith("[2026-01-01 09:00] 화자0: ")


def test_an_empty_document_yields_no_source(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    out = tmp_path / "workspaces" / WS / "channels" / "C00000000" / "raw"
    out.mkdir(parents=True)
    (out / "2026-01-01.md").write_text(
        f'---\nworkspace: {WS}\nchannel: "#팀_빈(ZZZ000)_생성"\n'
        "channel_id: C00000000\nvisibility: public\nacl: []\n---\n\n## 원문\n\n",
        encoding="utf-8",
    )

    assert _sources(tmp_path) == {}


# --- 5. 좌표가 흔들리지 않는다 ------------------------------------------------


def test_the_coordinate_is_the_file_not_the_merged_channel(archive):
    sources = _sources(archive)
    channels = {key: s for key, s in sources.items() if s.kind == CHANNEL}

    # 일자 파일 둘이 한 채널로 합쳐지지만 좌표는 **파일마다** 하나다.
    assert len(channels) == 2
    assert {key[1] for key in channels} == {"C00000000"}
    assert sorted(key[2] for key in channels) == [
        "workspaces/pilot/channels/C00000000/raw/2026-01-01.md",
        "workspaces/pilot/channels/C00000000/raw/2026-01-02.md",
    ]


def test_a_new_day_file_does_not_move_the_older_coordinates(archive):
    before = _sources(archive)
    _channel(archive, days=3)
    after = _sources(archive)

    for key, source in before.items():
        assert key in after
        assert after[key].text == source.text
        assert after[key].source_revision == source.source_revision


def test_the_doc_path_is_the_same_string_the_keyword_index_uses(archive):
    from tybot.search_index import rel_path

    files = ArchiveStore(archive).source_files()
    expected = {rel_path(path, archive) for path in files}

    assert {key[2] for key, s in _sources(archive).items() if s.kind == CHANNEL} <= expected


def test_sources_are_deterministic(archive):
    assert _sources(archive) == _sources(archive)


# --- 8. 색인과 복원이 같은 본문을 본다 ----------------------------------------


def test_indexing_and_resolving_read_the_same_canonical_text(archive):
    opener = VisibleSources(_store(archive), CTX)

    for key, indexed in _sources(archive).items():
        opened = opener.open(*key)
        assert opened is not None, key
        assert opened.text == indexed.text
        assert opened.source_revision == indexed.source_revision
        assert opened.kind == indexed.kind


def test_a_candidate_round_trip_resolves_to_the_same_slice(archive):
    """색인 → 청크 → 후보 → 복원. **네 자리가 한 글자도 어긋나면 안 된다.**"""
    source = next(s for s in _sources(archive).values() if s.kind == CHANNEL)
    chunks = split(source.text)
    prepared = prepare_chunks(source.text, chunks, [[0.1, 0.2]] * len(chunks))
    candidates = [_candidate(source, chunk, i) for i, chunk in enumerate(chunks)]

    resolution = resolve_candidates(
        candidates, open_visible=VisibleSources(_store(archive), CTX).open
    )

    assert resolution.dropped == ()
    assert len(resolution.resolved) == len(chunks)
    for got, chunk, ready in zip(resolution.resolved, chunks, prepared, strict=True):
        assert got.text == chunk.text
        assert hashlib.sha256(got.text.encode("utf-8")).hexdigest() == ready.text_sha256


def test_the_source_matches_the_resolver_contract_shape():
    """resolver 가 읽는 칸을 그대로 갖는다. 타입은 import 하지 않는다(방향)."""
    wanted = set(VisibleVectorSource.__dataclass_fields__)

    assert wanted <= set(VectorSource.__dataclass_fields__)


# --- 1. resolver 는 원본 MD 를 직접 열지 않는다 -------------------------------


def test_nothing_resolves_when_the_opener_refuses(archive):
    """창구가 막으면 파일이 그 자리에 있어도 본문이 안 나온다."""
    source = next(iter(_sources(archive).values()))
    chunk = split(source.text)[0]

    resolution = resolve_candidates(
        [_candidate(source, chunk)], open_visible=lambda *_: None
    )

    assert resolution.resolved == ()
    assert resolution.dropped == ("source_not_visible",)


def test_the_resolver_module_does_not_read_the_filesystem():
    """**구문으로** 본다. 머리말에 적어 둔 약속은 다음 사람이 한 줄로 깬다."""
    import ast

    import tybot.embedding_resolver as resolver

    tree = ast.parse(pathlib.Path(resolver.__file__).read_text(encoding="utf-8"))
    touched = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert not touched & {"open", "read_text", "read_bytes", "glob", "iterdir", "stat"}
    # `PurePosixPath` 는 파일을 안 만진다. `Path` 는 만진다.
    assert "Path" not in imported


# --- 3. ACL 판정 전에 파일을 열지 않는다 --------------------------------------


def test_a_forbidden_path_is_never_touched_on_disk(archive, monkeypatch):
    """요청받은 경로를 먼저 열면 응답이 「없음」과 「권한 없음」을 구분해 버린다."""
    private = _attachment(archive, index=7, channel=9)
    (archive / "workspaces" / WS / "channels" / "C00000009").mkdir(exist_ok=True)
    wanted = "workspaces/pilot/channels/C00000009/attachments/F00000007/r1.md"
    assert private.exists()

    seen: list[str] = []
    original = pathlib.Path.read_text

    def spy(self, *args, **kwargs):
        seen.append(self.as_posix())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", spy)
    opener = VisibleSources(_store(archive), CTX)

    assert opener.open(WS, "C00000009", wanted) is None
    assert not any(path.endswith("F00000007/r1.md") for path in seen)


def test_an_attachment_in_a_visible_channel_still_faces_its_own_acl(tmp_path, monkeypatch):
    """문 1(경로)을 지나도 문 2(권한 재판정)가 남아야 한다.

    첫 문은 「보이는 채널 안인가」 만 본다 — 그건 파일을 안 열고 할 수 있는
    판정이라 그만큼만 할 수 있다. 정본 자신이 더 좁게 닫혀 있으면 열고 나서
    한 번 더 막아야 한다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    path = _attachment(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("visibility: public", "visibility: private")
        .replace("acl: \n", "acl: [#다른팀_합성(ZZZ999)_생성]\n"),
        encoding="utf-8",
    )
    rel = "workspaces/pilot/channels/C00000000/attachments/F00000000/r1.md"

    assert VisibleSources(_store(tmp_path), CTX).open(WS, "C00000000", rel) is None


def test_an_attachment_that_claims_another_channel_is_refused(tmp_path, monkeypatch):
    """디렉터리는 이 채널인데 프론트매터는 다른 채널이라고 말한다.

    그 둘이 어긋나면 **열지 않는다.** 프론트매터를 믿으면 보이는 채널 자리에
    남의 채널 자료를 놓아 두는 것으로 권한이 넓어지고, 디렉터리만 믿으면 그
    자료의 진짜 ACL 을 안 보게 된다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    path = _attachment(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "channel_id: C00000000", "channel_id: C00000009"
        ),
        encoding="utf-8",
    )
    rel = "workspaces/pilot/channels/C00000000/attachments/F00000000/r1.md"
    opener = VisibleSources(_store(tmp_path), CTX)

    assert opener.open(WS, "C00000000", rel) is None
    assert opener.open(WS, "C00000009", rel) is None

    # 첫 문은 `attachment_reader.validate` 의 `path_mismatch` 다. 그 문을 열어
    # 두고도 막혀야 둘째 문이 **따로** 서 있다고 말할 수 있다.
    monkeypatch.setattr(attachment_reader, "validate", lambda doc, path, root: None)

    assert VisibleSources(_store(tmp_path), CTX).open(WS, "C00000000", rel) is None


def test_a_missing_path_and_a_forbidden_path_answer_the_same_way(archive):
    opener = VisibleSources(_store(archive), CTX)
    missing = "workspaces/pilot/channels/C00000000/raw/2099-12-31.md"
    forbidden = "workspaces/pilot/channels/C00000009/raw/2026-01-01.md"

    assert opener.open(WS, "C00000000", missing) is None
    assert opener.open(WS, "C00000009", forbidden) is None


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "..",
        "../../etc/passwd",
        "/workspaces/pilot/channels/C00000000/raw/2026-01-01.md",
        "C:/workspaces/pilot/raw/a.md",
        "workspaces\\pilot\\channels\\C00000000\\raw\\2026-01-01.md",
        "workspaces/pilot/channels/C00000000/raw/../raw/2026-01-01.md",
        "workspaces/pilot/channels/C00000000/raw/2026-01-01.md\x00",
    ],
)
def test_an_unsafe_path_is_refused(archive, bad):
    assert VisibleSources(_store(archive), CTX).open(WS, "C00000000", bad) is None


def test_an_empty_coordinate_is_refused(archive):
    opener = VisibleSources(_store(archive), CTX)
    path = "workspaces/pilot/channels/C00000000/raw/2026-01-01.md"

    assert opener.open("", "C00000000", path) is None
    assert opener.open(WS, "", path) is None


def test_a_coordinate_from_another_channel_does_not_open_the_file(archive):
    """경로는 맞지만 `channel_id` 가 다르면 열지 않는다 — 좌표는 셋이 한 벌이다."""
    opener = VisibleSources(_store(archive), CTX)
    path = "workspaces/pilot/channels/C00000000/raw/2026-01-01.md"

    assert opener.open(WS, "C00009999", path) is None
    assert opener.open("other-ws", "C00000000", path) is None


# --- 4. channel · attachment · DM ---------------------------------------------


def test_the_three_kinds_are_distinguished(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _attachment(tmp_path)
    _dm(tmp_path)

    kinds = {
        embedding_source.doc_kind(doc)
        for doc in _store(tmp_path).docs(dm_scope="*")
    }

    assert kinds == {CHANNEL, ATTACHMENT, DM}


def test_a_dm_opens_only_for_its_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _dm(tmp_path, "U1")
    key = (WS, "DM:U1", "workspaces/pilot/dm/U1/raw/2026-02-01.md")

    assert VisibleSources(_store(tmp_path), ME).open(*key) is not None
    assert VisibleSources(_store(tmp_path), SOMEONE_ELSE).open(*key) is None
    assert VisibleSources(_store(tmp_path), CTX).open(*key) is None


def test_a_dm_does_not_open_from_a_channel_question(tmp_path, monkeypatch):
    """본인이라도 채널에서 물으면 안 나온다 — 답을 그 채널 사람들이 함께 본다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _dm(tmp_path, "U1")
    key = (WS, "DM:U1", "workspaces/pilot/dm/U1/raw/2026-02-01.md")
    in_channel = RequestContext(
        workspace=WS, user_id="U1", channel_id="C00000000", channel="#팀_합성000(ZZZ000)_생성"
    )

    assert VisibleSources(_store(tmp_path), in_channel).open(*key) is None


def test_a_dm_is_left_out_of_the_index_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _dm(tmp_path)

    assert all(s.kind != DM for s in _sources(tmp_path).values())
    with pytest.raises(SourceUnavailable, match="DM 벡터 색인"):
        list(iter_index_sources(_store(tmp_path), dm_scope="*", require_revision_db=False))


def test_the_dm_door_holds_even_if_the_permission_judge_says_yes(tmp_path, monkeypatch):
    """개인 기록은 문이 둘이어야 한다. 하나가 무너져도 다른 하나가 남아야 한다.

    ACL 판정을 통째로 「예」 로 바꿔 놓고도 남의 DM 이 열리면 안 된다. 그 상황은
    가정이 아니다 — 2026-09-18 에 `_merge()` 가 `dm_user` 를 떨어뜨려 실제로
    통합조회 권한에 열렸다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _dm(tmp_path, "U1")
    key = (WS, "DM:U1", "workspaces/pilot/dm/U1/raw/2026-02-01.md")
    monkeypatch.setattr(ArchiveStore, "visible_predicate", lambda self, ctx: lambda d: True)
    monkeypatch.setattr(
        ArchiveStore,
        "visible_channel_docs",
        lambda self, ctx: self._docs(dm_scope="*", include_attachments=False),
    )

    assert VisibleSources(_store(tmp_path), SOMEONE_ELSE).open(*key) is None
    assert VisibleSources(_store(tmp_path), ME).open(*key) is not None


def test_a_dm_channel_does_not_lend_its_scope_to_an_attachment(tmp_path, monkeypatch):
    """DM 좌표로 첨부 문을 열 수 없다. 첨부는 채널 범위에서만 산다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _dm(tmp_path)
    opener = VisibleSources(_store(tmp_path), ME)
    opener.open(WS, "DM:U1", "workspaces/pilot/dm/U1/raw/2026-02-01.md")

    assert ("pilot", "DM:U1") not in opener._channel_scope


# --- 2 · 9. 되살아나지 않는다 ---------------------------------------------------


def _append(path: pathlib.Path, line: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + line + "\n", encoding="utf-8")


def test_a_deleted_message_cannot_come_back_through_a_vector(archive):
    """수집기는 삭제를 **원문에 쌓는다**(원칙 1). 근거에서 빼는 것은 읽을 때다."""
    raw = archive / "workspaces" / WS / "channels" / "C00000000" / "raw" / "2026-01-01.md"
    rel = "workspaces/pilot/channels/C00000000/raw/2026-01-01.md"
    before = _sources(archive)[(WS, "C00000000", rel)]
    chunk = split(before.text)[0]
    candidate = _candidate(before, chunk)

    # **같은 좌표여야 한다.** 삭제 표시는 지워진 그 메시지의 `message_ts` 를 달고
    # 원문에 쌓인다(`archiving_bot._ingest_revision`). 다른 좌표로 쓰면 이 시험은
    # 통과해도 아무것도 지키지 않는다 — 원래 줄은 그대로 남는다.
    _append(raw, "> [2026-01-01 09:00 |1767000000.0001] 화자0: [삭제됨] 합성 본문 0-0-0")
    after = _sources(archive)[(WS, "C00000000", rel)]

    assert "합성 본문 0-0-0" not in after.text  # 좌표 전체가 감춰진다
    assert after.source_revision != before.source_revision
    resolution = resolve_candidates(
        [candidate], open_visible=VisibleSources(_store(archive), CTX).open
    )
    assert resolution.resolved == ()
    assert resolution.dropped == ("source_revision_changed",)


def test_an_edited_message_changes_the_revision(archive):
    raw = archive / "workspaces" / WS / "channels" / "C00000000" / "raw" / "2026-01-01.md"
    rel = "workspaces/pilot/channels/C00000000/raw/2026-01-01.md"
    before = _sources(archive)[(WS, "C00000000", rel)]

    _append(raw, "> [2026-01-01 09:01 |1767000001.0001] 화자1: [수정 후] 고친 문장")
    after = _sources(archive)[(WS, "C00000000", rel)]

    assert after.source_revision != before.source_revision
    # revision DB 를 못 보면 **그 좌표 전체를 감춘다**(막는 쪽이 기본값).
    # 고치기 전 문장이 벡터로 남아 있어도 정본이 달라져 떨어진다.
    assert "고친 문장" not in after.text
    assert "합성 본문 0-0-1" not in after.text


def test_a_removed_document_stops_resolving(archive):
    rel = "workspaces/pilot/channels/C00000000/raw/2026-01-01.md"
    source = _sources(archive)[(WS, "C00000000", rel)]
    candidate = _candidate(source, split(source.text)[0])
    (archive / rel).unlink()

    resolution = resolve_candidates(
        [candidate], open_visible=VisibleSources(_store(archive), CTX).open
    )

    assert resolution.resolved == ()
    assert resolution.dropped == ("source_not_visible",)


def test_a_permission_change_stops_resolving(archive):
    rel = "workspaces/pilot/channels/C00000000/raw/2026-01-01.md"
    source = _sources(archive)[(WS, "C00000000", rel)]
    candidate = _candidate(source, split(source.text)[0])
    path = archive / rel
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("visibility: public", "visibility: private")
        .replace("acl: []", "acl: [#다른팀_합성(ZZZ999)_생성]"),
        encoding="utf-8",
    )

    resolution = resolve_candidates(
        [candidate], open_visible=VisibleSources(_store(archive), CTX).open
    )

    assert resolution.resolved == ()
    assert resolution.dropped == ("source_not_visible",)


def test_a_replaced_attachment_retires_the_old_revision(archive):
    old = "workspaces/pilot/channels/C00000000/attachments/F00000000/r1.md"
    new = "workspaces/pilot/channels/C00000000/attachments/F00000000/r2.md"
    before = _sources(archive)[(WS, "C00000000", old)]
    candidate = _candidate(before, split(before.text)[0])

    _attachment(
        archive, revision="r2", converted="2026-02-01T01:00+09:00", body="교체된 본문"
    )
    opener = VisibleSources(_store(archive), CTX)

    assert opener.open(WS, "C00000000", old) is None
    assert opener.open(WS, "C00000000", new) is not None
    resolution = resolve_candidates([candidate], open_visible=opener.open)
    assert resolution.dropped == ("source_not_visible",)


def test_a_partial_attachment_is_not_openable(tmp_path, monkeypatch):
    """「본문이 나왔다」 와 「다 읽었다」 는 다르다. 절반은 근거가 아니다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _attachment(tmp_path, state="partial")
    rel = "workspaces/pilot/channels/C00000000/attachments/F00000000/r1.md"

    assert (WS, "C00000000", rel) not in _sources(tmp_path)
    assert VisibleSources(_store(tmp_path), CTX).open(WS, "C00000000", rel) is None


# --- 7. 후보 하나가 첨부 전부를 깨우지 않는다 ---------------------------------


def test_one_candidate_opens_one_attachment_family(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    for i in range(20):
        _attachment(tmp_path, index=i)
    rel = "workspaces/pilot/channels/C00000000/attachments/F00000007/r1.md"

    opened: list[str] = []
    original = attachment_reader.load_checked

    def spy(path, root, **kwargs):
        opened.append(pathlib.Path(path).as_posix())
        return original(path, root, **kwargs)

    monkeypatch.setattr(attachment_reader, "load_checked", spy)
    source = VisibleSources(_store(tmp_path), CTX).open(WS, "C00000000", rel)

    assert source is not None and source.kind == ATTACHMENT
    assert len(opened) == 1, opened
    assert opened[0].endswith("F00000007/r1.md")


def test_channel_originals_are_gathered_once_per_request(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    for i in range(3):
        _attachment(tmp_path, index=i)

    calls: list[int] = []
    original = ArchiveStore.visible_channel_docs

    def spy(self, ctx):
        calls.append(1)
        return original(self, ctx)

    monkeypatch.setattr(ArchiveStore, "visible_channel_docs", spy)
    opener = VisibleSources(_store(tmp_path), CTX)
    for i in range(3):
        opener.open(
            WS,
            "C00000000",
            f"workspaces/pilot/channels/C00000000/attachments/F0000000{i}/r1.md",
        )

    assert len(calls) == 1


def test_a_repeated_miss_is_not_reopened(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _channel(tmp_path)
    _attachment(tmp_path)
    rel = "workspaces/pilot/channels/C00000000/attachments/F00000099/r1.md"

    opened: list[str] = []
    original = attachment_reader.archive_docs

    def spy(*args, **kwargs):
        opened.append("x")
        return original(*args, **kwargs)

    monkeypatch.setattr(attachment_reader, "archive_docs", spy)
    opener = VisibleSources(_store(tmp_path), CTX)
    for _ in range(5):
        assert opener.open(WS, "C00000000", rel) is None

    assert len(opened) <= 1


# --- 색인기 쪽 규칙 ------------------------------------------------------------


def test_indexing_refuses_while_the_revision_table_is_unreadable(archive, monkeypatch):
    """모르는 채로 색인하면 「그 문장이 원래 없는 문서」 가 정본으로 굳는다."""
    monkeypatch.setattr(embedding_source.revision_reader, "available", lambda: False)

    with pytest.raises(SourceUnavailable):
        list(iter_index_sources(_store(archive)))


def test_indexing_runs_when_the_revision_table_is_reachable(archive, monkeypatch):
    monkeypatch.setattr(embedding_source.revision_reader, "available", lambda: True)

    assert list(iter_index_sources(_store(archive)))


def test_an_open_kst_day_is_left_to_keyword_and_realtime_search(archive):
    sources = list(
        iter_index_sources(
            _store(archive), require_revision_db=False, today=date(2026, 1, 1)
        )
    )

    assert not any(source.doc_path.endswith("/2026-01-01.md") for source in sources)
    assert any(source.doc_path.endswith("/2026-01-02.md") for source in sources)
    assert any(source.kind == ATTACHMENT for source in sources)


def test_attachment_coordinates_agree_with_the_reader(archive):
    """경로 문자열만 보는 쪽과 실제 파일을 보는 쪽이 같은 가족을 가리켜야 한다."""
    for path in attachment_reader.source_files(archive):
        rel = path.relative_to(archive).as_posix()
        family = attachment_reader._family_key(path, archive)

        assert embedding_source.attachment_coordinates(rel) == family[:2]


def test_a_non_attachment_path_has_no_attachment_coordinates():
    assert embedding_source.attachment_coordinates(
        "workspaces/pilot/channels/C00000000/raw/2026-01-01.md"
    ) is None
    assert embedding_source.attachment_coordinates("a/b.md") is None


def test_the_plan_is_recorded_for_reindexing():
    assert embedding_source.SOURCE_PLAN
    assert embedding_source.source_chunker_revision("chunk-v1:max=1200:ov=120") == (
        "source-v1:chunk-v1:max=1200:ov=120"
    )


def test_candidate_source_uses_only_public_archive_store_apis():
    body = pathlib.Path(embedding_source.__file__).read_text(encoding="utf-8")
    assert "._visible_channel_docs" not in body
    assert "._visible_predicate" not in body
