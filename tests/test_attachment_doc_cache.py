"""`attachment_reader.DocCache` — 상한·무효화·동시성.

2026-11-02 OOM 후속. 캐시 자체가 새 OOM 원인이 되지 않는지 본다.

세 가지가 걸려 있다.

1. **상한보다 큰 파일 한 장**도 들고 있지 않는다. 「마지막 한 장은 남긴다」 로
   두면 상한을 둔 이유(큰 변환본)에만 상한이 꺼진다.
2. **같은 파일을 둘이 동시에 파싱하지 않는다.** 캐시가 채워지기 전에 두 요청이
   만나면 같은 본문이 두 벌 올라가고, 그건 고치려던 것과 같은 모양이다.
3. 되풀이해도 **평탄하다.** 축출이 계속 도는 상태에서도 고점이 올라가지 않는다.

자료는 전부 합성이다 — 원문·검색어·사람 이름을 fixture 에 넣지 않는다(원칙 5).
"""
from __future__ import annotations

import gc
import os
import threading
import time
import tracemalloc

import pytest
from test_archive_memory_growth import _attachment, _channel

from tybot.access import RequestContext
from tybot.archive import attachment_reader
from tybot.archive.store import ArchiveStore

CTX = RequestContext(workspace="pilot")
WS = "pilot"

#: 86MB × 42회 는 분 단위다. 기본으로는 돌리지 않고 조사할 때만 켠다.
FULL_SCALE = (os.getenv("TYBOT_OOM_FULL_SCALE") or "").strip() in {"1", "true", "yes"}


# --- 1. 상한보다 큰 파일 한 장 ----------------------------------------------


def test_a_single_file_larger_than_the_cap_is_not_kept(tmp_path):
    """한 장이 상한을 넘으면 **안 들고 있는다.** 상한에 예외를 두지 않는다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0, body_lines=400)
    size = path.stat().st_size
    cache = attachment_reader.DocCache(max_bytes=size // 2)

    doc = attachment_reader.load_checked(path, tmp_path, cache=cache)

    # 돌려주기는 한다 — 근거가 사라지면 안 된다. 들고 있지 않을 뿐이다.
    assert doc is not None
    assert cache.held_bytes == 0
    assert cache.oversize.get(path) is not None


def test_an_oversize_file_is_reported_so_the_cap_can_be_raised(tmp_path, caplog):
    """왜 캐시가 안 듣는지 운영이 알 수 있어야 한다 — 조용히 못 쓰면 못 고친다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0, body_lines=400)
    cache = attachment_reader.DocCache(max_bytes=64)

    with caplog.at_level("WARNING", logger="tybot.archive.attachment_reader"):
        attachment_reader.load_checked(path, tmp_path, cache=cache)

    (record,) = [r for r in caplog.records if "캐시 상한" in r.getMessage()]
    assert "TYBOT_ATTACHMENT_CACHE_MB" in record.getMessage()


def test_an_oversize_file_is_only_reported_once_per_revision(tmp_path, caplog):
    """호출마다 같은 경고를 찍으면 로그가 그 한 줄로 덮인다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0, body_lines=400)
    cache = attachment_reader.DocCache(max_bytes=64)

    with caplog.at_level("WARNING", logger="tybot.archive.attachment_reader"):
        for _ in range(5):
            attachment_reader.load_checked(path, tmp_path, cache=cache)

    assert len([r for r in caplog.records if "캐시 상한" in r.getMessage()]) == 1


def test_oversize_notice_history_is_bounded(tmp_path, monkeypatch):
    """본문을 버려도 경고 이력의 경로가 무한히 쌓이면 또 다른 누수다."""
    monkeypatch.setattr(attachment_reader, "OVERSIZE_NOTICE_LIMIT", 3)
    _channel(tmp_path, 0)
    cache = attachment_reader.DocCache(max_bytes=64)

    for index in range(5):
        path = _attachment(tmp_path, index, body_lines=100)
        attachment_reader.load_checked(path, tmp_path, cache=cache)

    assert len(cache.oversize) == 3


def test_an_oversize_file_never_pushes_held_bytes_over_the_cap(tmp_path):
    """큰 파일 한 장이 작은 것들을 밀어내고 자기가 눌러앉지 않는다."""
    _channel(tmp_path, 0)
    small = [_attachment(tmp_path, i, body_lines=20) for i in range(3)]
    big = _attachment(tmp_path, 9, body_lines=600)
    cap = small[0].stat().st_size * 3
    cache = attachment_reader.DocCache(max_bytes=cap)

    for path in small:
        attachment_reader.load_checked(path, tmp_path, cache=cache)
    held_before = cache.held_bytes
    assert held_before > 0

    attachment_reader.load_checked(big, tmp_path, cache=cache)

    assert cache.held_bytes <= cap
    # 작은 것들을 쫓아내지도 않았다 — 큰 파일은 애초에 들어오지 않았다.
    assert cache.held_bytes == held_before


def test_the_cap_is_respected_when_many_files_are_loaded(tmp_path):
    """축출이 돌아도 보유량이 상한을 넘지 않는다."""
    _channel(tmp_path, 0)
    paths = [_attachment(tmp_path, i, body_lines=120) for i in range(10)]
    cap = paths[0].stat().st_size * 3
    cache = attachment_reader.DocCache(max_bytes=cap)

    for path in paths:
        attachment_reader.load_checked(path, tmp_path, cache=cache)
        assert cache.held_bytes <= cap

    assert cache.held_bytes > 0  # 아무것도 못 들고 있으면 캐시가 아니다


def test_eviction_takes_the_least_recently_used(tmp_path):
    """오래 안 쓴 것부터 버린다 — 방금 쓴 것을 버리면 캐시가 매번 헛돈다."""
    _channel(tmp_path, 0)
    paths = [_attachment(tmp_path, i, body_lines=120) for i in range(3)]
    cap = int(paths[0].stat().st_size * 2.5)
    cache = attachment_reader.DocCache(max_bytes=cap)

    for path in paths[:2]:
        attachment_reader.load_checked(path, tmp_path, cache=cache)
    # 0 번을 다시 써서 뒤로 보낸다. 이제 가장 오래된 것은 1 번이다.
    attachment_reader.load_checked(paths[0], tmp_path, cache=cache)

    reads: list[str] = []
    real = attachment_reader.load

    def counting(path):
        reads.append(str(path))
        return real(path)

    attachment_reader.load_checked(paths[2], tmp_path, cache=cache)  # 축출 유발

    import unittest.mock as mock

    with mock.patch.object(attachment_reader, "load", counting):
        attachment_reader.load_checked(paths[0], tmp_path, cache=cache)
        attachment_reader.load_checked(paths[1], tmp_path, cache=cache)

    assert str(paths[0]) not in reads  # 최근에 쓴 것은 남아 있다
    assert str(paths[1]) in reads      # 오래된 것이 나갔다


def test_the_cache_can_be_turned_off_completely(tmp_path):
    """0 이면 한 바이트도 안 들고 있는다. 끄는 길이 있어야 원인을 가린다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0)
    cache = attachment_reader.DocCache(max_bytes=0)

    assert attachment_reader.load_checked(path, tmp_path, cache=cache) is not None
    assert cache.held_bytes == 0
    assert cache.oversize == {}  # 끈 것은 「너무 크다」 가 아니다


# --- 2. 지문 무효화 ----------------------------------------------------------


def test_a_rewritten_file_invalidates_the_entry(tmp_path):
    """재변환본이 들어오면 캐시가 옛 본문을 지키지 않는다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0, body_lines=10)
    cache = attachment_reader.DocCache()

    first = attachment_reader.load_checked(path, tmp_path, cache=cache)
    assert "합성 본문 0-9" in first.text

    time.sleep(0.01)  # mtime 해상도 여유
    path.write_text(
        path.read_text(encoding="utf-8").replace("합성 본문 0-9", "고쳐진 본문"),
        encoding="utf-8",
    )

    second = attachment_reader.load_checked(path, tmp_path, cache=cache)
    assert "고쳐진 본문" in second.text
    assert "합성 본문 0-9" not in second.text


def test_attachment_cache_stats_count_hits_misses_and_stale_revisions(tmp_path):
    """첨부 캐시 감사 계수에도 파일명이나 본문은 들어가지 않는다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0, body_lines=10)
    cache = attachment_reader.DocCache()

    attachment_reader.load_checked(path, tmp_path, cache=cache)
    attachment_reader.load_checked(path, tmp_path, cache=cache)
    time.sleep(0.01)
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    attachment_reader.load_checked(path, tmp_path, cache=cache)

    stats = cache.stats
    assert (stats.lookups, stats.hits, stats.misses) == (3, 1, 2)
    assert stats.invalidations == 1
    assert stats.evictions == 0


def test_disabled_attachment_cache_records_a_bypass(tmp_path):
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0)
    cache = attachment_reader.DocCache(max_bytes=0)

    attachment_reader.load_checked(path, tmp_path, cache=cache)

    assert cache.stats.bypasses == 1
    assert cache.stats.misses == 1


def test_a_rewritten_file_invalidates_the_archive_doc_too(tmp_path):
    """줄 객체 쪽도 같이 무효가 된다 — 한쪽만 낡으면 검색과 본문이 갈린다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0, body_lines=10)
    store = ArchiveStore(tmp_path)

    def texts():
        return {
            line.text
            for doc in store.visible_docs(CTX)
            if attachment_reader.is_attachment_doc(doc)
            for line in doc.raw_lines
        }

    assert "합성 본문 0-9" in texts()
    time.sleep(0.01)
    path.write_text(
        path.read_text(encoding="utf-8").replace("합성 본문 0-9", "고쳐진 본문"),
        encoding="utf-8",
    )
    assert "고쳐진 본문" in texts()
    assert "합성 본문 0-9" not in texts()


def test_an_unreadable_file_is_not_cached_as_an_answer(tmp_path):
    """지문을 못 읽으면 캐시를 건너뛴다. 없는 파일을 「없다」 로 굳히지 않는다."""
    cache = attachment_reader.DocCache()
    missing = tmp_path / "없는파일.md"
    assert attachment_reader.load_checked(missing, tmp_path, cache=cache) is None
    assert cache.held_bytes == 0


# --- 3. 실제 경로에 붙어 있나 ------------------------------------------------


def test_the_store_routes_both_read_paths_through_its_cache(tmp_path, monkeypatch):
    """`load_checked` 와 `as_archive_doc` **둘 다** 캐시를 지난다.

    한쪽만 붙으면 파일은 안 읽는데 줄 객체는 매번 다시 만든다 — 부피의 대부분이
    줄 객체라 그러면 고친 것이 거의 없다.
    """
    _channel(tmp_path, 0)
    for i in range(4):
        _attachment(tmp_path, i, body_lines=30)

    reads: list[str] = []
    builds: list[str] = []
    real_load = attachment_reader.load
    real_as = attachment_reader.as_archive_doc

    monkeypatch.setattr(
        attachment_reader, "load", lambda p: (reads.append(str(p)), real_load(p))[1]
    )
    monkeypatch.setattr(
        attachment_reader,
        "as_archive_doc",
        lambda d, r: (builds.append(d.file_id), real_as(d, r))[1],
    )

    store = ArchiveStore(tmp_path)
    for _ in range(6):
        store.visible_docs(CTX)

    assert len(reads) == 4
    assert len(builds) == 4


def test_the_store_cache_survives_eviction_without_losing_evidence(tmp_path, monkeypatch):
    """상한이 작아 축출이 돌아도 **근거는 그대로 나온다** — 느려질 뿐이다."""
    monkeypatch.setenv("TYBOT_ATTACHMENT_CACHE_MB", "1")
    _channel(tmp_path, 0)
    for i in range(6):
        _attachment(tmp_path, i, body_lines=400)

    store = ArchiveStore(tmp_path)
    seen = {
        doc.channel_id
        for doc in store.visible_docs(CTX)
        if attachment_reader.is_attachment_doc(doc)
    }
    counts = [
        len([d for d in store.visible_docs(CTX) if attachment_reader.is_attachment_doc(d)])
        for _ in range(4)
    ]
    assert seen
    assert counts == [6, 6, 6, 6]


# --- 4. 동시 요청이 같은 파일을 두 벌 올리지 않는다 --------------------------


def test_concurrent_requests_parse_a_large_file_only_once(tmp_path):
    """같은 큰 정본을 여럿이 동시에 만나도 **한 번만** 만든다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0, body_lines=400)
    cache = attachment_reader.DocCache()
    threads = 8
    ready = threading.Barrier(threads)
    builds: list[int] = []
    guard = threading.Lock()
    real = attachment_reader.load

    def slow_load(p):
        with guard:
            builds.append(1)
        time.sleep(0.05)  # 겹칠 틈을 실제로 만든다
        return real(p)

    results: list[object] = []

    def worker():
        ready.wait()
        import unittest.mock as mock

        with mock.patch.object(attachment_reader, "load", slow_load):
            got = attachment_reader.load_checked(path, tmp_path, cache=cache)
        with guard:
            results.append(got)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=30)

    assert len(results) == threads
    assert all(r is not None for r in results)
    assert sum(builds) == 1
    # 모두 같은 객체를 받는다 — 벌이 하나라는 뜻이다.
    assert len({id(r) for r in results}) == 1


def test_concurrent_requests_do_not_double_the_peak(tmp_path):
    """동시 접근의 할당 고점이 한 벌 분량을 크게 넘지 않는다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0, body_lines=3000)

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
        lambda: attachment_reader.load_checked(
            path, tmp_path, cache=attachment_reader.DocCache()
        )
    )
    assert one > 100 * 1024  # 한 벌이 잴 만큼은 되나

    shared = attachment_reader.DocCache()
    threads = 8
    ready = threading.Barrier(threads)

    def many():
        def worker():
            ready.wait()
            attachment_reader.load_checked(path, tmp_path, cache=shared)

        workers = [threading.Thread(target=worker) for _ in range(threads)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=30)

    together = peak_of(many)

    # 문이 없으면 여기서 8 벌이 겹친다. 두 벌 미만이면 겹치지 않은 것이다.
    assert together < one * 2


def test_the_gate_is_cleaned_up(tmp_path):
    """문이 쌓이면 그게 다음 누수다 — 경로마다 하나씩 영원히 남는다."""
    _channel(tmp_path, 0)
    paths = [_attachment(tmp_path, i, body_lines=10) for i in range(20)]
    cache = attachment_reader.DocCache(max_bytes=0)  # 매번 새로 만든다

    for path in paths:
        attachment_reader.load_checked(path, tmp_path, cache=cache)

    assert cache.gates_open == 0


def test_the_gate_is_released_when_building_raises(tmp_path):
    """만들다 터져도 문이 잠긴 채 남지 않는다 — 남으면 그 파일이 영원히 막힌다."""
    _channel(tmp_path, 0)
    path = _attachment(tmp_path, 0, body_lines=10)
    cache = attachment_reader.DocCache()

    def boom():
        raise RuntimeError("synthetic")

    with pytest.raises(RuntimeError):
        cache.loaded(path, boom)

    assert cache.gates_open == 0
    assert cache.loaded(path, lambda: "다시 됨") == "다시 됨"


# --- 5. 되풀이해도 평탄하다 --------------------------------------------------


def _run_searches(store, rounds: int) -> list[int]:
    """라운드마다의 할당 고점. 반환값은 바이트 목록."""
    from tybot import search_index

    peaks: list[int] = []
    store.visible_docs(CTX)  # 캐시 채우기
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
            names = {d.channel for d in store.visible_docs(CTX) if d.channel}
            where = frozenset(sorted(names)[:1])
            store.search(f"합성 {i}", CTX, limit=30, channels=where)
            _, high = tracemalloc.get_traced_memory()
            peaks.append(high - before)
        assert search_index.tokens_of("합성")
    finally:
        tracemalloc.stop()
    return peaks


def _assert_flat(peaks: list[int]) -> None:
    half = len(peaks) // 2
    early, late = max(peaks[:half]), max(peaks[half:])
    # 뒤쪽이 앞쪽보다 크게 높으면 되풀이가 쌓이고 있다는 뜻이다.
    assert late < early * 1.5 + 256 * 1024, (early, late)


def test_forty_two_searches_stay_flat(tmp_path):
    """사고와 같은 모양으로 42 회 — 고점이 계속 오르지 않는다."""
    _channel(tmp_path, 0, days=2, lines=50)
    for i in range(20):
        _attachment(tmp_path, i, body_lines=300)

    peaks = _run_searches(ArchiveStore(tmp_path), 42)

    assert len(peaks) == 42
    _assert_flat(peaks)


def test_forty_two_searches_stay_flat_while_the_cache_thrashes(tmp_path):
    """상한이 작아 **축출이 계속 도는** 상태가 더 어렵다 — 여기서도 평탄해야 한다.

    상한은 작업 집합보다 **작게** 잡아야 한다. 넉넉하면 축출이 한 번도 안 돌고,
    그러면 이 시험은 이름만 thrash 이고 앞 시험과 같은 것을 잰다.
    `evidence()` 는 파일을 정렬 순서로 한 바퀴 돌기 때문에, 작업 집합보다 작은
    LRU 에서는 적중률이 0 에 수렴한다 — 가장 나쁜 조합이고 그래서 여기서 본다.
    """
    _channel(tmp_path, 0, days=2, lines=50)
    for i in range(20):
        _attachment(tmp_path, i, body_lines=300)

    store = ArchiveStore(tmp_path)
    working_set = sum(
        p.stat().st_size for p in attachment_reader.source_files(tmp_path)
    )
    cap = working_set // 4
    store._attachments = attachment_reader.DocCache(max_bytes=cap)

    peaks = _run_searches(store, 42)

    assert store._attachments.held_bytes <= cap
    # 상한이 실제로 걸렸나 — 안 걸렸으면 축출이 한 번도 안 돈 것이다.
    assert store._attachments.held_bytes < working_set
    _assert_flat(peaks)


@pytest.mark.skipif(
    not FULL_SCALE, reason="TYBOT_OOM_FULL_SCALE=1 일 때만 — 86MB 생성에 분 단위가 든다"
)
def test_full_scale_eighty_six_megabytes_stays_flat(tmp_path):
    """사고 규모 그대로: 합성 86MB · 검색 42 회.

    기본으로는 안 돈다. 상한 기본값(192MiB)을 바꾸기 전이나 운영 아카이브가
    커졌을 때 손으로 돌린다.

        TYBOT_OOM_FULL_SCALE=1 python -m pytest \\
            tests/test_attachment_doc_cache.py -k full_scale -q
    """
    for c in range(7):
        _channel(tmp_path, c, days=20, lines=300)
    for i in range(200):
        _attachment(tmp_path, i, channel=i % 7, body_lines=2000)

    store = ArchiveStore(tmp_path)
    peaks = _run_searches(store, 42)

    _assert_flat(peaks)
    # 고치기 전에는 한 회가 195MB 였다. 상한을 켠 뒤에도 그 근처면 안 걸린 것이다.
    assert max(peaks) < 64 * 1024 * 1024
