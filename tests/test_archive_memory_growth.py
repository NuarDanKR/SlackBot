"""요청당 메모리 증가 — 2026-11-02 `tybot.service` OOM 회귀 시험.

사고: 검색 42 건짜리 질문 하나가 anon RSS 3.0GiB 에서 global OOM 으로 죽었다.

원인은 누수가 아니라 **한 질문 안의 되풀이**였다. 첨부 정본은 캐시가 없어
`visible_docs()` 마다 전부 다시 읽혔고, 본문 한 줄이 `RawLine` 객체 하나가 되므로
파일의 두 배 남짓으로 부풀었다. 한 번의 `search` 도구 호출이 `visible_docs()` 를
2~7 번 부른다(`_channels_matching` · `search` · 0건일 때의 낱말별 재검색).

여기 쓰는 자료는 **전부 합성**이다. 원문·검색어·사람 이름은 한 글자도 넣지
않는다 — 시험 fixture 는 저장소에 남고, 아카이브 권한 밖이다(원칙 5).
"""
from __future__ import annotations

import gc
import time
import tracemalloc

import pytest

from tybot.access import RequestContext
from tybot.archive import attachment_reader
from tybot.archive.store import ArchiveStore

CTX = RequestContext(workspace="pilot")
WS = "pilot"


def _channel(root, index: int, *, days: int = 2, lines: int = 20) -> str:
    """합성 채널 하나. 돌려주는 값은 채널 표시명."""
    name = f"#팀_합성{index:03d}(ZZZ{index:03d})_생성"
    out = root / "workspaces" / WS / "channels" / f"C{index:08d}" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    for day in range(days):
        rows = "\n".join(
            f"> [2026-01-{day + 1:02d} 09:{i % 60:02d}] 화자{i % 7}: 합성 {index}-{day}-{i}"
            for i in range(lines)
        )
        (out / f"2026-01-{day + 1:02d}.md").write_text(
            f'---\nworkspace: {WS}\nchannel: "{name}"\n'
            f"channel_id: C{index:08d}\nvisibility: public\nacl: []\ndoc_count: 1\n"
            f"last_ingested: 2026-01-{day + 1:02d}T17:00+09:00\n---\n\n"
            f"## 원문 (자동 취합, 편집 금지)\n{rows}\n",
            encoding="utf-8",
        )
    return name


def _attachment(root, index: int, *, channel: int = 0, body_lines: int = 50):
    """합성 첨부 정본 하나. 변환본이 큰 상황을 흉내 낸다."""
    out = (
        root / "workspaces" / WS / "channels" / f"C{channel:08d}"
        / "attachments" / f"F{index:08d}"
    )
    out.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"합성 본문 {index}-{i}" for i in range(body_lines))
    path = out / "r1.md"
    path.write_text(
        f"---\nkind: attachment\nworkspace: {WS}\nchannel_id: C{channel:08d}\n"
        f'channel: "#팀_합성{channel:03d}(ZZZ{channel:03d})_생성"\n'
        f"file_id: F{index:08d}\nfile_name: 합성{index}.pdf\nrevision: r1\n"
        f"visibility: public\nacl: \nconversion_state: succeeded\n"
        f"message_ts: '170000000.{index:06d}'\nfiletype: pdf\nsha256: {'a' * 64}\n"
        f"staged_at: 2026-01-01T00:00+09:00\nconverted_at: 2026-01-01T01:00+09:00\n"
        f"converter_name: synth\nconverter_version: '1'\n"
        f"human_upload_confirmed: true\n---\n\n# 합성{index}.pdf\n\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def archive(tmp_path):
    _channel(tmp_path, 0)
    for i in range(6):
        _attachment(tmp_path, i)
    return tmp_path


# --- 원인: 첨부 정본이 호출마다 다시 읽혔다 --------------------------------


def test_attachments_are_read_once_across_repeated_visible_docs(archive, monkeypatch):
    """한 질문이 `visible_docs()` 를 여러 번 불러도 정본은 한 번만 읽는다.

    이것이 OOM 의 원인이었다. 원문 MD 는 이미 `ArchiveStore._load` 가 캐시했고,
    첨부만 빠져 있었다.
    """
    reads: list[str] = []
    real = attachment_reader.load

    def counting(path):
        reads.append(str(path))
        return real(path)

    monkeypatch.setattr(attachment_reader, "load", counting)
    store = ArchiveStore(archive)

    for _ in range(5):
        store.visible_docs(CTX)

    assert len(reads) == 6
    assert len(set(reads)) == 6


def test_attachment_lines_are_not_rebuilt_on_every_call(archive):
    """줄 객체까지 재사용한다 — 본문 한 줄이 객체 하나라 여기가 부피다."""
    store = ArchiveStore(archive)
    first = {
        str(d.path): d.raw_lines
        for d in store.visible_docs(CTX)
        if attachment_reader.is_attachment_doc(d)
    }
    second = {
        str(d.path): d.raw_lines
        for d in store.visible_docs(CTX)
        if attachment_reader.is_attachment_doc(d)
    }
    assert first and first.keys() == second.keys()
    assert all(first[key] is second[key] for key in first)


def test_changed_attachment_is_reread_not_served_from_cache(archive):
    """재변환본이 들어오면 그 자리에서 다시 읽는다 — 캐시가 옛 근거를 지키지 않는다."""
    store = ArchiveStore(archive)
    assert any(
        "합성 본문 0-49" in line.text
        for doc in store.visible_docs(CTX)
        for line in doc.raw_lines
    )

    time.sleep(0.01)  # mtime 해상도 여유
    path = (
        archive / "workspaces" / WS / "channels" / "C00000000"
        / "attachments" / "F00000000" / "r1.md"
    )
    path.write_text(
        path.read_text(encoding="utf-8").replace("합성 본문 0-49", "고쳐진 합성 본문"),
        encoding="utf-8",
    )

    texts = {line.text for doc in store.visible_docs(CTX) for line in doc.raw_lines}
    assert "고쳐진 합성 본문" in texts
    assert "합성 본문 0-49" not in texts


def test_deleted_attachment_stops_being_evidence(archive):
    """지운 정본을 캐시가 계속 답하지 않는다."""
    store = ArchiveStore(archive)
    before = len(store.visible_docs(CTX))
    (
        archive / "workspaces" / WS / "channels" / "C00000000"
        / "attachments" / "F00000000" / "r1.md"
    ).unlink()
    assert len(store.visible_docs(CTX)) == before - 1


def test_repeated_requests_do_not_grow_memory(tmp_path):
    """반복 조회가 **쌓이지 않는다.** 사고의 모양 그대로 — 한 질문 안의 되풀이."""
    _channel(tmp_path, 0, days=3, lines=60)
    for i in range(12):
        _attachment(tmp_path, i, body_lines=400)
    store = ArchiveStore(tmp_path)

    store.visible_docs(CTX)  # 캐시 채우기
    gc.collect()
    tracemalloc.start()
    try:
        base, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        for _ in range(10):
            docs = store.visible_docs(CTX)
            assert docs
            del docs
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # 첨부 본문 전체가 호출마다 다시 만들어지던 때는 여기서 수십 배가 났다.
    # 느슨하게 잡아도(1MiB) 고친 뒤 실측은 그 한참 아래다.
    assert peak - base < 1024 * 1024
    assert current - base < 256 * 1024


# --- 증폭: 검색 한 번이 아카이브 전체를 객체로 만들었다 ---------------------


def test_scan_keeps_the_same_order_as_an_unbounded_scan(tmp_path):
    """중간에 추려 내도 **순서가 같다.** 다르면 어느 줄이 근거가 되는지가 바뀐다."""
    from tybot import search_index
    from tybot.archive.store import SearchHit

    _channel(tmp_path, 0, days=4, lines=120)
    store = ArchiveStore(tmp_path)
    docs = store.visible_docs(CTX)

    def unbounded(query, tokens, limit):
        hits = [
            SearchHit(doc=doc, line=line, score=score)
            for doc in docs
            for line in doc.raw_lines
            if (score := search_index.score_line(tokens, query, line.speaker, line.text))
        ]
        return ArchiveStore._rank(hits, limit)

    def key(hits):
        return [(str(h.line.source_path), h.line.lineno, h.score) for h in hits]

    for query in ("합성", "화자3", "합성 0-1-5", "09:30"):
        tokens = search_index.tokens_of(query)
        for limit in (1, 5, 30, 200):
            assert key(store._scan(query, tokens, docs, limit)) == key(
                unbounded(query, tokens, limit)
            ), (query, limit)


def test_scan_holds_less_than_an_unbounded_scan_would(tmp_path):
    """거의 모든 줄이 맞는 낱말로도 중간 결과가 터지지 않는다.

    같은 자료를 **추려 내지 않고** 모았을 때와 나란히 잰다. 절대값을 박아 두면
    파이썬 판이 바뀔 때마다 숫자를 고치게 되고, 그러다 보면 회귀가 들어와도
    숫자만 올려 통과시키게 된다.
    """
    from tybot import search_index
    from tybot.archive.store import SCAN_KEEP_FACTOR, SearchHit

    _channel(tmp_path, 0, days=12, lines=1200)
    store = ArchiveStore(tmp_path)
    docs = store.visible_docs(CTX)
    tokens = search_index.tokens_of("합성")
    matching = sum(
        1
        for doc in docs
        for line in doc.raw_lines
        if search_index.score_line(tokens, "합성", line.speaker, line.text)
    )
    # 추려 내기가 실제로 걸리는 분량인가 — 아니면 이 시험은 아무것도 안 본다.
    assert matching > 30 * SCAN_KEEP_FACTOR * 2

    def peak_of(run):
        gc.collect()
        tracemalloc.start()
        try:
            base, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            got = run()
            _, high = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return got, high - base

    def unbounded():
        hits = [
            SearchHit(doc=doc, line=line, score=score)
            for doc in docs
            for line in doc.raw_lines
            if (score := search_index.score_line(tokens, "합성", line.speaker, line.text))
        ]
        return ArchiveStore._rank(hits, 30)

    loose, loose_peak = peak_of(unbounded)
    tight, tight_peak = peak_of(lambda: store._scan("합성", tokens, docs, 30))

    assert len(tight) == 30
    assert [(str(h.line.source_path), h.line.lineno) for h in tight] == [
        (str(h.line.source_path), h.line.lineno) for h in loose
    ]
    assert tight_peak * 2 < loose_peak


def test_rel_path_is_computed_once_per_file_not_once_per_line(tmp_path, monkeypatch):
    """경로 변환이 **파일 수**만큼만 일어난다.

    줄마다 부르면 같은 경로 문자열이 줄 수만큼 새로 만들어지고, 한 질문이 검색을
    수십 번 하면 그 쓰레기도 수십 배가 된다(2026-11-02 OOM).
    """
    from tybot import search_index
    from tybot.archive import store as store_mod

    _channel(tmp_path, 0, days=4, lines=200)
    store = ArchiveStore(tmp_path)
    docs = store.visible_docs(CTX)
    target = docs[0].raw_lines[5]
    source_files = {str(line.source_path) for line in docs[0].raw_lines}
    assert len(source_files) == 4
    rel = search_index.rel_path(target.source_path or docs[0].path, tmp_path)

    monkeypatch.setattr(
        search_index,
        "candidates",
        lambda query, channels: [
            search_index.Candidate(
                doc_path=rel, line_no=target.lineno, spoken_at=target.ts
            )
        ],
    )
    monkeypatch.setattr(search_index, "indexed_counts", lambda paths: None)

    calls: list[str] = []
    real = search_index.rel_path

    def counting(path, root):
        calls.append(str(path))
        return real(path, root)

    monkeypatch.setattr(search_index, "rel_path", counting)
    hits = store.search("합성", CTX, limit=30)

    assert [h.line.lineno for h in hits] == [target.lineno]
    # 800 줄짜리 채널인데 경로 변환은 파일 수만큼만.
    assert len(calls) <= len(source_files)
    assert store_mod._RelPaths  # 변환 표가 사라지면 이 시험은 의미가 없다


# --- 관측은 기본으로 꺼져 있고, 글자를 남기지 않는다 ------------------------


def test_rss_probe_is_off_by_default(monkeypatch, caplog):
    from tybot import rss_probe

    monkeypatch.delenv("TYBOT_RSS_LOG", raising=False)
    assert not rss_probe.enabled()
    with caplog.at_level("INFO", logger="tybot.rss"), rss_probe.observe("search"):
        pass
    assert caplog.records == []


def test_rss_probe_reports_nothing_when_proc_is_absent(monkeypatch):
    """리눅스가 아니면 **빈 값**이다. 0 은 「안 쓴다」 로 읽혀 못 읽은 것과 섞인다."""
    from tybot import rss_probe

    monkeypatch.setattr(rss_probe, "STATUS", "/does/not/exist")
    assert rss_probe.sample() == {}


def test_rss_probe_only_logs_numbers_and_the_label(monkeypatch, caplog):
    from tybot import rss_probe

    monkeypatch.setenv("TYBOT_RSS_LOG", "1")
    monkeypatch.setattr(
        rss_probe, "sample", lambda: {"RssAnon": 10, "VmRSS": 20, "VmSwap": 0}
    )
    with caplog.at_level("INFO", logger="tybot.rss"), rss_probe.observe("search"):
        pass

    (record,) = caplog.records
    message = record.getMessage()
    assert "label=search" in message
    assert "anon_delta_kib=+0" in message
    # 숫자·고정 낱말 말고는 아무것도 들어가지 않는다.
    assert all(token not in message for token in ("/", "\\", "질문", "합성"))


def test_rss_probe_replaces_an_unknown_tool_name(monkeypatch, caplog):
    """모델이 도구명 자리에 낸 임의 문자열을 운영 로그에 복사하지 않는다."""
    from tybot import rss_probe

    monkeypatch.setenv("TYBOT_RSS_LOG", "1")
    monkeypatch.setattr(
        rss_probe, "sample", lambda: {"RssAnon": 10, "VmRSS": 20, "VmSwap": 0}
    )
    with caplog.at_level("INFO", logger="tybot.rss"), rss_probe.observe(
        "원문이 섞인 알 수 없는 도구"
    ):
        pass

    (record,) = caplog.records
    assert "label=unknown" in record.getMessage()
    assert "원문" not in record.getMessage()


def test_tool_dispatch_does_not_name_the_question():
    """도구 경계가 남기는 것은 **도구 이름**뿐이다 — 검색어가 아니다."""
    import inspect

    from tybot import specialist_tools

    source = inspect.getsource(specialist_tools.ToolBox.run_with_status)
    assert "rss_probe.observe(name)" in source
    assert "rss_probe.observe(args" not in source
    assert "rss_probe.observe(query" not in source


# --- 상한 자체 ---------------------------------------------------------------


def test_doc_cache_drops_the_oldest_when_the_byte_cap_is_reached(tmp_path):
    """상한은 **바이트**다. 개수로 막으면 큰 변환본 몇 장이 상한을 통과한다."""
    _channel(tmp_path, 0)
    paths = [_attachment(tmp_path, i, body_lines=200) for i in range(4)]
    cap = paths[0].stat().st_size * 2
    cache = attachment_reader.DocCache(max_bytes=cap)

    for path in paths:
        attachment_reader.load_checked(path, tmp_path, cache=cache)

    assert cache.held_bytes <= cap
    # 버린 것이 사라지지는 않는다 — 다음 호출이 그 파일만 다시 읽는다.
    assert attachment_reader.load_checked(paths[0], tmp_path, cache=cache) is not None


def test_doc_cache_can_be_turned_off(tmp_path):
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0)
    cache = attachment_reader.DocCache(max_bytes=0)
    assert attachment_reader.load_checked(path, tmp_path, cache=cache) is not None
    assert cache.held_bytes == 0


def test_cache_cap_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("TYBOT_ATTACHMENT_CACHE_MB", "7")
    assert attachment_reader._cache_bytes() == 7 * 1024 * 1024
    monkeypatch.setenv("TYBOT_ATTACHMENT_CACHE_MB", "쓰레기")
    assert (
        attachment_reader._cache_bytes()
        == attachment_reader.DEFAULT_CACHE_MB * 1024 * 1024
    )
