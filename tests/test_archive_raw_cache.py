"""`ArchiveStore` 원문 MD 캐시 — 바이트 상한 LRU.

2026-11-02 OOM 후속. 직접 원인은 첨부였지만 **같은 계열의 위험**이 여기 남아
있었다: 예전 상한은 개수 4096 이고, 넘쳤을 때 치우는 것은 없어진 파일뿐이라
살아 있는 원문이 4096개 이하면 **파일이 아무리 커도 전부 상주**했다.

개수로 막으면 「몇 개인가」 를 세는데 메모리는 「몇 바이트인가」 로 쓰인다.

자료는 전부 합성이다 — 원문·검색어·사람 이름·실경로를 fixture 에 넣지 않는다
(원칙 5). 상한 초과 경고도 경로를 싣지 않는지 본다(아카이브 경로에는 채널명이,
채널명에는 조직 이름이 들어 있다).
"""
from __future__ import annotations

import gc
import threading
import time
import tracemalloc
import unittest.mock as mock

import pytest

from tybot.access import RequestContext
from tybot.archive import store as store_mod
from tybot.archive.store import ArchiveStore, RawCache

CTX = RequestContext(workspace="pilot")
WS = "pilot"


def _write(root, index: int, *, lines: int = 40, body: str | None = None):
    """합성 원문 MD 한 장."""
    name = f"#팀_합성{index:03d}(ZZZ{index:03d})_생성"
    out = root / "workspaces" / WS / "channels" / f"C{index:08d}" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    rows = body if body is not None else "\n".join(
        f"> [2026-01-01 09:{i % 60:02d}] 화자{i % 7}: 합성 {index}-{i} " + "가" * 30
        for i in range(lines)
    )
    path = out / "2026-01-01.md"
    path.write_text(
        f'---\nworkspace: {WS}\nchannel: "{name}"\nchannel_id: C{index:08d}\n'
        f"visibility: public\nacl: []\ndoc_count: 1\n"
        f"last_ingested: 2026-01-01T17:00+09:00\n---\n\n"
        f"## 원문 (자동 취합, 편집 금지)\n{rows}\n",
        encoding="utf-8",
    )
    return path


def _broken(root, index: int, *, lines: int = 4000):
    """프론트매터가 없는 큰 파일 — 형식 위반으로 거절된다."""
    out = root / "workspaces" / WS / "channels" / f"C{index:08d}" / "raw"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "2026-01-01.md"
    path.write_text(
        "프론트매터 없는 파일\n"
        + "\n".join(f"> [2026-01-01 09:00] 화자: 합성 {i} " + "가" * 60 for i in range(lines)),
        encoding="utf-8",
    )
    return path


# --- 1. 상한은 개수가 아니라 바이트다 ---------------------------------------


def test_the_cap_counts_bytes_not_files(tmp_path):
    """작은 파일 여럿은 다 들어가고, 같은 바이트의 큰 파일 하나는 자리를 다 쓴다."""
    small = [_write(tmp_path, i, lines=20) for i in range(8)]
    unit = small[0].stat().st_size
    cache = RawCache(max_bytes=unit * 4)

    for path in small:
        st = path.stat()
        cache.get(path, (st.st_mtime_ns, st.st_size), lambda p=path: store_mod.load_doc(p))

    assert cache.held_bytes <= unit * 4
    # 개수 상한이었다면 8개가 전부 남았을 자리다.
    assert cache.held_files < 8
    assert cache.held_files >= 1


def test_many_small_files_are_all_kept_when_they_fit(tmp_path):
    """개수가 많아도 바이트가 맞으면 전부 들고 있는다 — 개수로 막지 않는다."""
    paths = [_write(tmp_path, i, lines=5) for i in range(40)]
    total = sum(p.stat().st_size for p in paths)
    cache = RawCache(max_bytes=total * 2)

    for path in paths:
        st = path.stat()
        cache.get(path, (st.st_mtime_ns, st.st_size), lambda p=path: store_mod.load_doc(p))

    assert cache.held_files == 40


def test_eviction_takes_the_least_recently_used(tmp_path):
    """오래 안 쓴 것부터 버린다 — 방금 쓴 것을 버리면 캐시가 매번 헛돈다."""
    paths = [_write(tmp_path, i, lines=40) for i in range(3)]
    unit = paths[0].stat().st_size
    store = ArchiveStore(tmp_path)
    store._cache = RawCache(max_bytes=int(unit * 2.5))

    for path in paths[:2]:
        store._load(path)
    store._load(paths[0])  # 0 번을 다시 써서 뒤로 보낸다
    store._load(paths[2])  # 축출 유발 — 가장 오래된 1 번이 나가야 한다

    reads: list[str] = []
    real = store_mod.load_doc
    with mock.patch.object(
        store_mod, "load_doc", lambda p: (reads.append(str(p)), real(p))[1]
    ):
        store._load(paths[0])
        store._load(paths[1])

    assert str(paths[0]) not in reads
    assert str(paths[1]) in reads


# --- 2. 기본값 ---------------------------------------------------------------


def test_the_default_cap_is_128_mib(monkeypatch):
    monkeypatch.delenv("TYBOT_ARCHIVE_CACHE_MB", raising=False)
    assert store_mod.DEFAULT_ARCHIVE_CACHE_MB == 128
    assert store_mod._archive_cache_bytes() == 128 * 1024 * 1024


def test_the_cap_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("TYBOT_ARCHIVE_CACHE_MB", "7")
    assert store_mod._archive_cache_bytes() == 7 * 1024 * 1024
    monkeypatch.setenv("TYBOT_ARCHIVE_CACHE_MB", "쓰레기")
    assert store_mod._archive_cache_bytes() == 128 * 1024 * 1024
    monkeypatch.setenv("TYBOT_ARCHIVE_CACHE_MB", "0")
    assert store_mod._archive_cache_bytes() == 0


def test_the_cache_can_be_turned_off(tmp_path):
    """0 이면 한 바이트도 안 들고 있는다 — 원인을 가리려면 끄는 길이 있어야 한다."""
    path = _write(tmp_path, 0)
    store = ArchiveStore(tmp_path)
    store._cache = RawCache(max_bytes=0)

    assert isinstance(store._load(path), store_mod.ArchiveDoc)
    assert store._cache.held_bytes == 0
    assert store._cache.oversize == {}  # 끈 것은 「너무 크다」 가 아니다


# --- 3. 상한보다 큰 단일 파일 -----------------------------------------------


def test_a_file_larger_than_the_cap_is_returned_but_not_cached(tmp_path):
    """돌려주되 들고 있지 않는다. **근거는 사라지지 않는다.**"""
    path = _write(tmp_path, 0, lines=200)
    store = ArchiveStore(tmp_path)
    store._cache = RawCache(max_bytes=path.stat().st_size // 2)

    got = store._load(path)

    assert isinstance(got, store_mod.ArchiveDoc)
    assert got.raw_lines  # 줄이 다 들어 있다
    assert store._cache.held_bytes == 0
    assert path in store._cache.oversize


def test_an_oversize_file_does_not_evict_the_others(tmp_path):
    """큰 파일이 작은 것들을 밀어내고 자기가 눌러앉지 않는다."""
    small = [_write(tmp_path, i, lines=10) for i in range(3)]
    big = _write(tmp_path, 9, lines=4000)
    cap = small[0].stat().st_size * 4
    store = ArchiveStore(tmp_path)
    store._cache = RawCache(max_bytes=cap)

    for path in small:
        store._load(path)
    before = store._cache.held_bytes
    assert before > 0

    store._load(big)

    assert store._cache.held_bytes == before
    assert store._cache.held_files == 3


def test_search_still_finds_an_oversize_file(tmp_path):
    """캐시에 못 들어간 파일도 **근거로 나온다** — 느려질 뿐이다."""
    _write(tmp_path, 0, lines=200)
    store = ArchiveStore(tmp_path)
    store._cache = RawCache(max_bytes=1024)

    hits = store.search("합성", CTX, limit=5)

    assert hits
    assert store._cache.held_bytes == 0


# --- 4. 지문 무효화 ----------------------------------------------------------


def test_a_rewritten_file_is_reparsed(tmp_path):
    path = _write(tmp_path, 0)
    store = ArchiveStore(tmp_path)
    assert not store.search("착공일", CTX)

    time.sleep(0.01)  # mtime 해상도 여유
    with path.open("a", encoding="utf-8") as f:
        f.write("> [2026-01-02 10:00] 화자1: 착공일 합성\n")

    hits = store.search("착공일", CTX)
    assert len(hits) == 1


def test_a_stale_entry_releases_its_bytes_immediately(tmp_path):
    """지문이 달라지면 **그 자리에서** 자리를 돌려준다 — 두 벌이 겹치지 않는다."""
    path = _write(tmp_path, 0, lines=40)
    store = ArchiveStore(tmp_path)
    store._load(path)
    held = store._cache.held_bytes
    assert held > 0

    time.sleep(0.01)
    _write(tmp_path, 0, lines=5)  # 더 작게 다시 쓴다
    store._load(path)

    assert store._cache.held_bytes < held


def test_a_stale_entry_is_dropped_before_the_new_one_is_built(tmp_path):
    """낡은 벌을 **다시 읽기 전에** 놓는다.

    `_put` 이 끝날 때 치워도 숫자는 맞는다. 하지만 그 사이 — 같은 파일을 다시
    파싱하는 동안 — 낡은 벌과 새 벌이 **둘 다** 메모리에 있다. 큰 파일에서는
    그 한 순간이 두 배다. 이번 OOM 이 바로 그 모양이었다.
    """
    path = _write(tmp_path, 0, lines=200)
    cache = RawCache()
    st = path.stat()
    cache.get(path, (st.st_mtime_ns, st.st_size), lambda: store_mod.load_doc(path))
    assert cache.held_bytes > 0

    time.sleep(0.01)
    _write(tmp_path, 0, lines=200)
    st2 = path.stat()
    seen: list[int] = []

    def build():
        # 새 벌을 만드는 **도중**에 잰다.
        seen.append(cache.held_bytes)
        return store_mod.load_doc(path)

    cache.get(path, (st2.st_mtime_ns, st2.st_size), build)

    assert seen == [0]


def test_an_unreadable_path_is_not_cached(tmp_path):
    store = ArchiveStore(tmp_path)
    got = store._load(tmp_path / "없는파일.md")
    assert isinstance(got, str)
    assert store._cache.held_bytes == 0
    assert store._cache.held_files == 0


# --- 5. 동시 파싱은 한 번만 ---------------------------------------------------


def test_concurrent_requests_parse_a_file_only_once(tmp_path):
    path = _write(tmp_path, 0, lines=2000)
    cache = RawCache()
    threads = 8
    ready = threading.Barrier(threads)
    builds: list[int] = []
    guard = threading.Lock()
    st = path.stat()
    fingerprint = (st.st_mtime_ns, st.st_size)
    results: list[object] = []

    def build():
        with guard:
            builds.append(1)
        time.sleep(0.05)  # 겹칠 틈을 실제로 만든다
        return store_mod.load_doc(path)

    def worker():
        ready.wait()
        got = cache.get(path, fingerprint, build)
        with guard:
            results.append(got)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=30)

    assert len(results) == threads
    assert sum(builds) == 1
    assert len({id(r) for r in results}) == 1  # 벌이 하나다


def test_concurrent_parsing_does_not_double_the_peak(tmp_path):
    path = _write(tmp_path, 0, lines=8000)
    st = path.stat()
    fingerprint = (st.st_mtime_ns, st.st_size)

    def peak_of(run):
        gc.collect()
        tracemalloc.start()
        try:
            base, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            run()
            _, high = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        return high - base

    one = peak_of(
        lambda: RawCache().get(path, fingerprint, lambda: store_mod.load_doc(path))
    )
    assert one > 100 * 1024

    shared = RawCache()
    threads = 8
    ready = threading.Barrier(threads)

    def many():
        def worker():
            ready.wait()
            shared.get(path, fingerprint, lambda: store_mod.load_doc(path))

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=30)

    assert peak_of(many) < one * 2


def test_the_gate_is_released_when_parsing_raises(tmp_path):
    """만들다 터져도 문이 잠긴 채 남지 않는다 — 남으면 그 파일이 영원히 막힌다."""
    path = _write(tmp_path, 0)
    st = path.stat()
    cache = RawCache()

    def boom():
        raise RuntimeError("synthetic")

    with pytest.raises(RuntimeError):
        cache.get(path, (st.st_mtime_ns, st.st_size), boom)

    assert cache.gates_open == 0
    assert cache.get(path, (st.st_mtime_ns, st.st_size), lambda: "다시 됨") == "다시 됨"


def test_gates_do_not_accumulate(tmp_path):
    paths = [_write(tmp_path, i, lines=3) for i in range(30)]
    store = ArchiveStore(tmp_path)
    store._cache = RawCache(max_bytes=0)
    for path in paths:
        store._load(path)
    assert store._cache.gates_open == 0


# --- 6. SchemaError 문자열 캐시 유지 -----------------------------------------


def test_a_schema_violation_is_cached_as_a_reason_string(tmp_path):
    """형식 위반은 사유 문자열로 캐시한다 — 호출마다 다시 파싱하지 않는다."""
    path = _broken(tmp_path, 0, lines=20)
    store = ArchiveStore(tmp_path)

    first = store._load(path)
    assert isinstance(first, str)

    reads: list[str] = []
    real = store_mod.load_doc
    with mock.patch.object(
        store_mod, "load_doc", lambda p: (reads.append(str(p)), real(p))[1]
    ):
        for _ in range(4):
            assert store._load(path) == first
    assert reads == []


def test_a_broken_file_larger_than_the_cap_still_caches_its_reason(tmp_path):
    """큰 파일이 깨졌을 때가 더 중요하다 — 사유를 안 들면 매번 그 큰 파일을 연다."""
    path = _broken(tmp_path, 0, lines=4000)
    store = ArchiveStore(tmp_path)
    store._cache = RawCache(max_bytes=path.stat().st_size // 4)

    first = store._load(path)
    assert isinstance(first, str)
    # 사유 문자열의 비용은 길이다 — 파일 크기가 아니다.
    assert 0 < store._cache.held_bytes < path.stat().st_size // 4

    reads: list[str] = []
    real = store_mod.load_doc
    with mock.patch.object(
        store_mod, "load_doc", lambda p: (reads.append(str(p)), real(p))[1]
    ):
        store._load(path)
    assert reads == []


def test_broken_and_fixed_still_works(tmp_path):
    """고치면 곧바로 반영된다 — 사유 캐시가 수리를 가리지 않는다."""
    _broken(tmp_path, 0, lines=5)
    store = ArchiveStore(tmp_path)
    assert store.broken()

    time.sleep(0.01)
    _write(tmp_path, 0)
    assert store.broken() == []
    assert len(store.docs()) == 1


# --- 7. 이력이 무한히 쌓이지 않는다 ------------------------------------------


def test_deleted_files_release_their_bytes(tmp_path):
    """지운 원문이 살아 있는 원문의 자리를 차지하고 있지 않는다."""
    paths = [_write(tmp_path, i, lines=40) for i in range(4)]
    store = ArchiveStore(tmp_path)
    store.docs()
    held = store._cache.held_bytes
    assert store._cache.held_files == 4

    paths[0].unlink()
    paths[1].unlink()
    store.docs()

    assert store._cache.held_files == 2
    assert store._cache.held_bytes < held


def test_the_oversize_history_is_bounded(tmp_path):
    """「너무 크다」 이력도 무한히 쌓이면 그 자체가 다음 누수다."""
    cache = RawCache(max_bytes=16)
    path = _write(tmp_path, 0, lines=5)
    doc = store_mod.load_doc(path)

    for i in range(store_mod.OVERSIZE_HISTORY * 2):
        cache._put(tmp_path / f"합성{i}.md", (i, 10_000), doc)

    assert len(cache.oversize) <= store_mod.OVERSIZE_HISTORY + 1


def test_a_sweep_runs_without_the_file_list(tmp_path):
    """`prune` 을 못 받는 경로(`broken()` 등)에서도 없어진 파일이 치워진다."""
    paths = [_write(tmp_path, i, lines=3) for i in range(3)]
    store = ArchiveStore(tmp_path)
    for path in paths:
        store._load(path)
    assert store._cache.held_files == 3

    paths[0].unlink()
    store._cache._sweep_missing()

    assert store._cache.held_files == 2


def test_prune_keeps_files_outside_the_listed_scope(tmp_path):
    """안 보인다고 지우지 않는다 — DM 은 요청마다 범위가 달라서 서로를 무효로 만든다."""
    paths = [_write(tmp_path, i, lines=3) for i in range(3)]
    store = ArchiveStore(tmp_path)
    for path in paths:
        store._load(path)

    store._cache.prune([paths[0]])  # 나머지 둘은 목록 밖이지만 살아 있다

    assert store._cache.held_files == 3


# --- 8. 캐시가 결과를 바꾸지 않는다 ------------------------------------------


def _coordinates(hits):
    from tybot.evidence_refs import content_hash

    return [
        (
            h.doc.workspace,
            h.doc.channel_id,
            str(h.line.source_path),
            h.line.lineno,
            h.line.ts,
            h.line.message_ts,
            content_hash(h.line.ts, h.line.speaker, h.line.text),
            h.score,
            h.citation(),
        )
        for h in hits
    ]


@pytest.mark.parametrize("query", ["합성", "화자3", "합성 0-5", "없는낱말zzzz"])
def test_results_and_evidence_coordinates_match_with_and_without_cache(tmp_path, query):
    """캐시를 켠 결과와 끈 결과가 **좌표까지** 같다.

    출처와 후속 질문이 이 좌표 위에 서 있다(원칙 1·2). 캐시가 좌표를 바꾸면
    「방금 그 문서」 가 다른 줄을 연다 — 오류 없이.
    """
    for i in range(4):
        _write(tmp_path, i, lines=60)

    hot = ArchiveStore(tmp_path)
    cold = ArchiveStore(tmp_path)
    cold._cache = RawCache(max_bytes=0)

    hot.search(query, CTX, limit=30)  # 캐시를 데운다
    assert _coordinates(hot.search(query, CTX, limit=30)) == _coordinates(
        cold.search(query, CTX, limit=30)
    )


def test_resolve_refs_returns_the_same_line_with_and_without_cache(tmp_path):
    """`EvidenceRef` 복원도 같다 — 후속 질문이 여는 줄이 캐시로 달라지면 안 된다."""
    from tybot.evidence_refs import ARCHIVE_LINE, EvidenceRef, content_hash

    for i in range(3):
        _write(tmp_path, i, lines=40)

    hot = ArchiveStore(tmp_path)
    cold = ArchiveStore(tmp_path)
    cold._cache = RawCache(max_bytes=0)

    source = hot.search("합성", CTX, limit=5)
    assert source
    refs = [
        EvidenceRef(
            kind=ARCHIVE_LINE,
            workspace=h.doc.workspace,
            channel_id=h.doc.channel_id or "",
            document_path=str(
                (h.line.source_path or h.doc.path).relative_to(tmp_path)
            ).replace("\\", "/"),
            line_no=h.line.lineno,
            source_ts=h.line.ts,
            content_hash=content_hash(h.line.ts, h.line.speaker, h.line.text),
        )
        for h in source
    ]

    hot_hits, hot_dropped = hot.resolve_refs(refs, CTX)
    cold_hits, cold_dropped = cold.resolve_refs(refs, CTX)

    assert hot_hits  # 복원이 실제로 되는 상황인가
    assert _coordinates(hot_hits) == _coordinates(cold_hits)
    assert hot_dropped == cold_dropped


def test_a_thrashing_cache_does_not_lose_evidence(tmp_path):
    """상한을 조여 축출이 계속 돌아도 **같은 결과**가 나온다."""
    for i in range(6):
        _write(tmp_path, i, lines=60)

    cold = ArchiveStore(tmp_path)
    cold._cache = RawCache(max_bytes=0)
    thrash = ArchiveStore(tmp_path)
    thrash._cache = RawCache(max_bytes=2048)

    for _ in range(3):
        assert _coordinates(thrash.search("합성", CTX, limit=30)) == _coordinates(
            cold.search("합성", CTX, limit=30)
        )
    assert thrash._cache.held_bytes <= 2048


# --- 9. 42 회 반복에서 평탄 --------------------------------------------------


def _peaks(store, rounds: int) -> list[int]:
    peaks: list[int] = []
    store.visible_docs(CTX)  # 캐시를 데운다
    gc.collect()
    tracemalloc.start()
    try:
        for i in range(rounds):
            # 라운드마다 **그 라운드가 쓴 양**을 잰다. 시작 시점 하나를 기준으로
            # 빼면, 같이 도는 다른 시험이 프로세스 수준에서 한 번 올려놓은 양이
            # 그대로 섞인다 — 혼자 돌리면 통과하고 모아 돌리면 깨지는 거짓 실패가
            # 거기서 났다(2026-11-03, 30번째 라운드에서 1.9MB 계단).
            gc.collect()
            before, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            store.visible_docs(CTX)
            store.search(f"합성 {i}", CTX, limit=30)
            _, high = tracemalloc.get_traced_memory()
            peaks.append(high - before)
    finally:
        tracemalloc.stop()
    return peaks


def _assert_flat(peaks: list[int]) -> None:
    """누수는 **매 라운드 쌓인다.** 한 라운드만 튀고 돌아오는 것은 누수가 아니다.

    뒤 절반에서 가장 큰 값 하나는 빼고 본다. 2026-11-09 전체 실행에서 42라운드 중
    38번째 한 번만 4.2MB 가 튀고 바로 511KB 로 돌아왔다 — 프로세스 어딘가의 일회성
    할당이 그 라운드에 걸린 것이다. 그 시점은 다른 모듈 구성에 따라 앞뒤로 옮겨
    다녀서, 앞 절반에 걸리면 통과하고 뒤 절반에 걸리면 깨졌다.

    진짜 누수는 두 번째로 큰 값과 마지막 라운드들에서 그대로 잡힌다.
    """
    half = len(peaks) // 2
    early = max(peaks[:half])
    late = sorted(peaks[half:])[-2]
    assert late < early * 1.5 + 256 * 1024, (early, late)
    # 끝나 갈 때 기준선으로 돌아와 있어야 한다. 쌓이는 누수는 여기서 가장 크다.
    tail = max(peaks[-3:])
    assert tail < early * 1.5 + 256 * 1024, (early, tail)


def test_forty_two_rounds_stay_flat(tmp_path):
    for i in range(8):
        _write(tmp_path, i, lines=200)
    peaks = _peaks(ArchiveStore(tmp_path), 42)
    assert len(peaks) == 42
    _assert_flat(peaks)


def test_forty_two_rounds_stay_flat_while_the_cache_thrashes(tmp_path):
    """상한이 작아 **축출이 계속 도는** 상태가 더 어렵다."""
    for i in range(8):
        _write(tmp_path, i, lines=200)
    store = ArchiveStore(tmp_path)
    store._cache = RawCache(max_bytes=4096)

    peaks = _peaks(store, 42)

    assert store._cache.held_bytes <= 4096
    _assert_flat(peaks)


# --- 10. 로그에 원문·경로·검색어를 넣지 않는다 -------------------------------


def test_the_oversize_warning_carries_no_path_or_content(tmp_path, caplog):
    """아카이브 경로에는 채널명이, 채널명에는 조직 이름이 들어 있다.

    로그는 장애 때 통째로 퍼 날라지는 자리다. 어느 파일인지는 `oversize` 로
    메모리에서만 본다.
    """
    path = _write(tmp_path, 0, lines=400)
    store = ArchiveStore(tmp_path)
    store._cache = RawCache(max_bytes=512)

    with caplog.at_level("WARNING", logger="tybot.archive.store"):
        store._load(path)

    (record,) = [r for r in caplog.records if "캐시 상한" in r.getMessage()]
    message = record.getMessage()
    assert "TYBOT_ARCHIVE_CACHE_MB" in message
    for leak in (str(tmp_path), "합성", "화자", "workspaces", "/", "\\"):
        assert leak not in message, leak
    # 그래도 어느 파일인지 알 길은 있다 — 메모리 안에서.
    assert path in store._cache.oversize
