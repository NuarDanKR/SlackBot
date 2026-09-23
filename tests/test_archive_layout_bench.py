"""실측 스크립트가 **운영 DB 에 닿지 않는지.**

M4(증분 재색인)만 DB 를 쓴다. 그리고 `search_index.reindex` 는 `_connect()` 로
붙는데, `_connect()` 는 `DATABASE_URL` **하나만** 읽는다 — 전용 DSN 을 인자로
넘기는 길이 없다. 그래서 환경 변수를 잠시 바꿔 끼우는 수밖에 없고, 그 순간이
운영 색인을 더럽힐 수 있는 자리다.

`raw_line` 에는 **지우는 경로가 없다.** 원문을 편집하지 않는 것이 계약이라 옛 행은
쌓이기만 한다(`search_index.reindex` 주석). 실수로 운영 DB 에 넣으면 되돌릴 수
없고, 「실측용 줄이 답변 근거로 나간다」 가 된다.

그래서 이 시험들은 성능이 아니라 **닿지 않음**을 본다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import archive_layout_bench as bench

BENCH_DSN = "postgresql://u:p@localhost:5432/tybot_bench_index"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """두 변수 다 비운 상태에서 시작한다. 시험이 서로의 환경을 물려받지 않게."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv(bench.BENCH_DSN_ENV, raising=False)


# --- 자물쇠 1: 운영 DSN 이 있으면 거부 ---------------------------------------

def test_refuses_when_database_url_is_set(monkeypatch):
    """운영 셸에서 무심코 돌리는 경우가 이 모양이다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/tybot")
    monkeypatch.setenv(bench.BENCH_DSN_ENV, BENCH_DSN)

    refusal = bench.index_bench_refusal()

    assert "DATABASE_URL" in refusal
    assert "거부" in refusal


def test_refuses_even_when_database_url_points_at_the_bench_db(monkeypatch):
    """전용 DB 를 가리키고 있어도 거부한다.

    「이 값이 안전한가」 를 판정하기 시작하면 판정이 틀리는 날이 온다. 설정돼
    있으면 거부, 라는 규칙이 지켜지는지 확인하기 훨씬 쉽다.
    """
    monkeypatch.setenv("DATABASE_URL", BENCH_DSN)
    monkeypatch.setenv(bench.BENCH_DSN_ENV, BENCH_DSN)

    assert "DATABASE_URL" in bench.index_bench_refusal()


# --- 자물쇠 2: 전용 DSN 이 있어야 한다 ---------------------------------------

def test_skips_when_no_bench_dsn():
    refusal = bench.index_bench_refusal()

    assert bench.BENCH_DSN_ENV in refusal
    assert "건너뛴다" in refusal


# --- 자물쇠 3: DB 이름으로 실측용임이 드러나야 한다 ---------------------------

@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@db:5432/tybot",
        "postgresql://u:p@db:5432/tybot_prod",
        "host=db port=5432 dbname=tybot user=u",
    ],
    ids=["url-tybot", "url-prod", "keyword-tybot"],
)
def test_refuses_a_dsn_whose_database_is_not_marked(monkeypatch, dsn):
    monkeypatch.setenv(bench.BENCH_DSN_ENV, dsn)

    refusal = bench.index_bench_refusal()

    assert "거부" in refusal
    assert "이름" in refusal


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@db:5432/tybot_bench_index",
        "postgresql://u:p@db:5432/archive_lab?sslmode=disable",
        "host=db dbname=tybot_test user=u",
        "dbname='tybot_bench' host=db",
    ],
    ids=["bench", "lab-with-query", "test-keyword", "quoted"],
)
def test_accepts_a_marked_database(monkeypatch, dsn):
    monkeypatch.setenv(bench.BENCH_DSN_ENV, dsn)

    assert bench.index_bench_refusal() == ""


def test_refuses_a_dsn_whose_database_cannot_be_read(monkeypatch):
    """이름을 못 읽으면 막는다. **모르면 막는다**(절대 원칙 3 과 같은 축)."""
    monkeypatch.setenv(bench.BENCH_DSN_ENV, "host=db user=u")

    assert "읽지 못해" in bench.index_bench_refusal()


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://u:p@h:5432/name", "name"),
        ("postgres://h/name?sslmode=require", "name"),
        ("host=h dbname=name user=u", "name"),
        ("dbname=\"name\"", "name"),
        ("host=h user=u", ""),
    ],
)
def test_dsn_database_parsing(dsn, expected):
    assert bench._dsn_database(dsn) == expected


# --- 환경 변수를 되돌리는가 ---------------------------------------------------

def test_bench_dsn_is_restored_afterwards(monkeypatch):
    """안 되돌리면 이어 도는 코드가 실측 DB 를 운영 DB 로 알고 쓴다."""
    monkeypatch.setenv("DATABASE_URL", "operational")
    with bench._use_bench_dsn(BENCH_DSN):
        import os

        assert os.environ["DATABASE_URL"] == BENCH_DSN
    import os

    assert os.environ["DATABASE_URL"] == "operational"


def test_bench_dsn_is_removed_when_there_was_none():
    import os

    with bench._use_bench_dsn(BENCH_DSN):
        assert os.environ["DATABASE_URL"] == BENCH_DSN
    assert "DATABASE_URL" not in os.environ


def test_bench_dsn_is_restored_even_when_the_body_raises(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "operational")
    with pytest.raises(RuntimeError), bench._use_bench_dsn(BENCH_DSN):
        raise RuntimeError("색인 실패")
    import os

    assert os.environ["DATABASE_URL"] == "operational"


# --- M4 를 실제로 돌린다 (DB 는 가짜) ----------------------------------------

@pytest.fixture
def batch(tmp_path):
    """작은 배치 하나. 시험은 **파일만** 진짜다."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import archive_layout_convert as conv

    channel = conv.Channel(
        workspace="pf", channel="#팀_자금(ABB540)_주간보고", channel_id="C0FUND",
        visibility="private", acl=frozenset({"#팀_자금(ABB540)_주간보고"}),
        share_with=frozenset(),
        messages=[
            conv.Message(ts="2026-09-01 09:10", speaker="김자금", text="기성 청구분"),
            conv.Message(ts="2026-09-02 09:10", speaker="박과장", text="확인"),
        ],
    )
    out = tmp_path / "a"
    conv.build([channel], out, "a", source_kind="ty_archive")
    target = bench.pick_targets(bench.ArchiveStore(out), out)[-1]
    return out, target


@pytest.fixture
def fake_index(monkeypatch):
    """`reindex` 와 연결을 갈아 끼운다. **진짜 DB 에 붙지 않는다.**

    시험이 DB 를 요구하면 개발 PC 에서 안 돌고, 안 도는 시험은 지켜 주지 않는다.
    """
    calls: dict[str, list] = {"reindex": [], "truncate": [], "dsn_seen": []}

    import os

    def fake_reindex(docs, root=None, **_):
        # **재색인이 불릴 때 DATABASE_URL 이 실측 DSN 이어야 한다.**
        # 이게 이 시험의 핵심이다 — `reindex` 는 이 값으로만 붙는다.
        calls["dsn_seen"].append(os.environ.get("DATABASE_URL"))
        docs = list(docs)
        calls["reindex"].append(len(docs))
        return {"docs": len(docs), "lines": 0}

    class FakeCursor:
        def execute(self, sql, *_):
            calls["truncate"].append(sql)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake_connect(dsn):
        calls["dsn_seen"].append(dsn)
        return FakeConn()

    from tybot import search_index

    monkeypatch.setattr(search_index, "reindex", fake_reindex)
    monkeypatch.setattr(bench, "bench_connect", fake_connect)
    return calls


def test_m4_runs_and_uses_only_the_bench_dsn(monkeypatch, batch, fake_index):
    """전용 DSN 을 줬을 때 실제로 재고, **그 DSN 으로만** 닿는다."""
    monkeypatch.setenv(bench.BENCH_DSN_ENV, BENCH_DSN)
    root, target = batch

    samples, why = bench.bench_reindex(root, "a", 2, target)

    assert why == ""
    assert [s.label for s in samples] == [
        "M4 전체 재색인 · 구조a", "M4 증분 재색인 · 구조a"
    ]
    assert all(len(s.values) == 2 for s in samples)
    # 회차마다 전체 1회 + 증분 1회
    assert len(fake_index["reindex"]) == 4
    # 오직 실측 DSN 만 봤다. 운영 값이 섞이지 않았다
    assert set(fake_index["dsn_seen"]) == {BENCH_DSN}


def test_m4_truncates_once_per_round(monkeypatch, batch, fake_index):
    """A·B 가 섞이지 않게 **회차마다** 비운다.

    안 비우면 두 번째 배치의 재색인이 이미 들어간 행을 만나 대부분
    `ON CONFLICT DO NOTHING` 으로 넘어간다. 그러면 「그 구조가 훨씬 빠르다」 가
    나오는데, 빠른 것이 아니라 일을 안 한 것이다.
    """
    monkeypatch.setenv(bench.BENCH_DSN_ENV, BENCH_DSN)
    root, target = batch

    bench.bench_reindex(root, "a", 3, target)

    assert fake_index["truncate"] == ["TRUNCATE raw_line"] * 3


def test_m4_restores_the_environment_after_running(monkeypatch, batch, fake_index):
    monkeypatch.setenv(bench.BENCH_DSN_ENV, BENCH_DSN)
    root, target = batch

    bench.bench_reindex(root, "a", 1, target)

    import os

    assert "DATABASE_URL" not in os.environ


def test_m4_reports_why_it_failed_instead_of_raising(monkeypatch, batch, fake_index):
    """실패해도 다른 항목의 측정을 끊지 않는다. 이유는 보고서에 실린다."""
    monkeypatch.setenv(bench.BENCH_DSN_ENV, BENCH_DSN)
    from tybot import search_index

    def boom(*_, **__):
        raise RuntimeError("색인 실패: relation \"raw_line\" does not exist")

    monkeypatch.setattr(search_index, "reindex", boom)
    root, target = batch

    samples, why = bench.bench_reindex(root, "a", 1, target)

    assert samples == []
    assert "M4 를 돌리다 실패했다" in why
    assert "raw_line" in why

    import os

    assert "DATABASE_URL" not in os.environ, "실패해도 환경은 되돌린다"


def test_m4_does_not_touch_the_database_when_refused(monkeypatch, batch, fake_index):
    """거부되면 **연결도 truncate 도 없다.**

    「거부했다고 말하면서 이미 붙어 있었다」 가 가장 나쁜 실패다.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/tybot")
    monkeypatch.setenv(bench.BENCH_DSN_ENV, BENCH_DSN)
    root, target = batch

    samples, why = bench.bench_reindex(root, "a", 2, target)

    assert samples == []
    assert "DATABASE_URL" in why
    assert fake_index["reindex"] == []
    assert fake_index["truncate"] == []
    assert fake_index["dsn_seen"] == []


# --- 운영 경로를 건드리지 않는다 ---------------------------------------------

def test_bench_refuses_operational_archive_paths():
    """M4 밖에서도 같다 — 운영 아카이브에서 재지 않는다(§8)."""
    for guarded in ("/var/lib/tybot/archive", "/var/lib/tybot/objects",
                    "/var/lib/tybot/staging"):
        assert bench._refuse_operational(Path(guarded), "구조 1") is not None
    assert bench._refuse_operational(Path("/var/lib/tybot/archive-lab"), "구조 1") is None


def test_files_are_written_while_database_url_is_absent(monkeypatch, batch, fake_index):
    """파일을 만지는 동안에는 `DATABASE_URL` 이 없어야 한다.

    `archive_write_lock` 은 `DATABASE_URL` 이 **있으면** 파일 락 대신 PostgreSQL
    advisory 락으로 바뀐다(`tybot/lock.py` 의 「백엔드 두 가지」). 실측 DSN 을 회차
    전체에 걸어 두면 아카이브 쓰기 잠금까지 실측 DB 로 간다.

    처음 구현이 그랬고, 시험에서는 **멈추는 것**으로 드러났다(붙을 수 없는 DSN).
    서버에서는 멈추지 않고 조용히 실측 DB 에 advisory 락을 잡았을 것이다. 그래서
    환경 변수를 거는 창을 재색인 호출 두 줄로만 좁혔고, 이 시험이 그 창을 지킨다.
    """
    import os

    monkeypatch.setenv(bench.BENCH_DSN_ENV, BENCH_DSN)
    seen: list[str | None] = []
    original = bench._append_one

    def watched(root, layout, target, n):
        seen.append(os.environ.get("DATABASE_URL"))
        return original(root, layout, target, n)

    monkeypatch.setattr(bench, "_append_one", watched)
    root, target = batch

    bench.bench_reindex(root, "a", 2, target)

    assert seen == [None, None], "파일을 쓰는 동안 DATABASE_URL 이 걸려 있었다"


def test_reset_runs_without_the_dsn_in_the_environment(monkeypatch, batch, fake_index):
    """`TRUNCATE` 도 환경 변수가 아니라 **인자로 받은 DSN** 으로 붙는다.

    환경 변수로 붙으면 창이 그만큼 넓어지고, 넓어진 창에서 `archive_write_lock` 이
    백엔드를 갈아탄다(위 시험 참조).
    """
    import os

    monkeypatch.setenv(bench.BENCH_DSN_ENV, BENCH_DSN)
    seen: list[tuple[str, str | None]] = []

    class NullConn:
        def cursor(self):
            return self

        def execute(self, *_):
            pass

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def watched(dsn):
        seen.append((dsn, os.environ.get("DATABASE_URL")))
        return NullConn()

    monkeypatch.setattr(bench, "bench_connect", watched)
    root, target = batch

    bench.bench_reindex(root, "a", 1, target)

    assert seen == [(BENCH_DSN, None)]


# --- 실측 자체를 시작하지 않는 경우 -------------------------------------------

def test_the_whole_run_refuses_when_database_url_is_set(monkeypatch):
    """M4 만의 문제가 아니다 — **M1·M2 도** 그 DB 에 붙는다.

    `archive_write_lock` 이 `DATABASE_URL` 유무로 백엔드를 바꾸기 때문이다.
    실제로 이걸 모르고 돌렸다가 수집 측정에서 멈췄다(붙을 수 없는 DSN).
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/tybot")

    refusal = bench.operational_db_refusal()

    assert "실측을 시작하지 않습니다" in refusal
    assert "advisory" in refusal          # 잠금 백엔드가 바뀐다는 사실을 말한다
    assert bench.BENCH_DSN_ENV in refusal  # 무엇을 대신 주어야 하는지도 말한다


def test_the_whole_run_proceeds_without_database_url():
    assert bench.operational_db_refusal() == ""


def test_main_stops_before_copying_anything(monkeypatch, tmp_path):
    """거부는 **복사 전에** 일어난다.

    작업 사본을 뜬 뒤에 멈추면 수 MB 를 쓰고 아무것도 못 잰다. 그리고 「멈췄는데
    파일은 생겼다」 는 다음 사람에게 무엇이 끝난 상태인지 알려 주지 않는다.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/tybot")
    monkeypatch.setattr(
        sys, "argv",
        ["archive_layout_bench.py", "--a", str(tmp_path / "a"),
         "--b", str(tmp_path / "b"), "--out", str(tmp_path / "out")],
    )

    assert bench.main() == 2
    assert not (tmp_path / "out").exists()
