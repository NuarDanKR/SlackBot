#!/usr/bin/env python3
"""한국어 부분일치 검색이 실제로 인덱스를 타는지 측정한다.

사용: python scripts/check_search_index.py [--rows 50000]

## 왜 이 스크립트가 있나
`pg_trgm` 은 **세 글자 묶음**으로 색인해 2글자 검색어에 인덱스를 쓰지 못한다.
우리 쓰임에서 2글자 명사는 흔하다(기성·타설·결재·예산·공정·검측).
그래서 `pg_bigm` 을 쓰기로 했는데, 그 판단이 이 환경에서도 맞는지 **말이 아니라 측정으로**
확인한다. 확장을 설치한 뒤 다시 돌려 결과가 바뀌는지 보면 된다.

## 실제 DB 를 건드리는가
임시 데이터를 `raw_line` 에 넣지만 **전부 한 트랜잭션 안에서 하고 마지막에 되돌린다.**
중간에 죽어도 커밋되지 않으므로 남지 않는다. 넣는 문장은 합성한 공사 용어뿐이다.

주의: `SET enable_seqscan = off` 를 쓰지 않는다. 그걸 켜면 인덱스를 억지로 쓰게 만들어
측정이 무의미해진다(전에 이 실수로 잘못된 결론을 냈다).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from tybot.envfile import load_env_file

# 2글자 / 3글자 이상을 나눠서 본다 — 갈리는 지점이 여기다.
QUERIES = ["기성", "타설", "결재", "기성률", "김해외동", "공정회의"]

# 검색어가 전체 행의 몇 %에 나오게 할지.
#
# **이 값이 측정의 핵심이다.** 어휘를 몇 개로만 만들면 검색어가 절반 넘는 행에 나오고,
# 그러면 인덱스를 안 쓰는 게 정상이라 pg_bigm 이 있으나 없으나 전체 스캔이 나온다
# (실제로 처음에 그 실수를 했다). 아카이브 검색은 '드문 낱말을 찾는' 쓰임이므로
# 낮은 비율로 심고, 나머지는 겹치지 않는 채움말로 채운다.
HIT_RATIO = 0.005  # 0.5% = 5만 행 중 250행


def _filler_vocab(rnd: random.Random, size: int = 800) -> list[str]:
    """검색어와 겹치지 않는 채움말. 한글 음절을 무작위로 이어 붙인다."""
    words = []
    while len(words) < size:
        w = "".join(chr(rnd.randint(0xAC00, 0xD7A3)) for _ in range(rnd.randint(2, 4)))
        # 검색어를 우연히 포함하면 버린다 — 그러면 적중률이 흐트러진다.
        if any(q in w or w in q for q in QUERIES):
            continue
        words.append(w)
    return words


def sample_rows(n: int) -> list[tuple]:
    rnd = random.Random(20260830)  # 재현 가능하게 고정
    filler = _filler_vocab(rnd)
    rows = []
    for i in range(n):
        body = [rnd.choice(filler) for _ in range(rnd.randint(6, 14))]
        for q in QUERIES:
            if rnd.random() < HIT_RATIO:
                body.insert(rnd.randrange(len(body) + 1), q)
        rows.append((
            "ws-bench", "채널-bench", f"bench/{i % 100}.md", i,
            "2026-08-30T09:00:00+09:00", "bench", " ".join(body), f"sha-{i}",
        ))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=50_000)
    ap.add_argument(
        "--compare",
        action="store_true",
        help="pg_bigm 인덱스를 잠시 pg_trgm 으로 바꿔 같은 데이터로 맞대어 본다(되돌린다)."
             " 인덱스를 두 번 만들므로 --rows 20000 정도가 적당하다.",
    )
    args = ap.parse_args()

    load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL 이 없다. .env 를 확인한다.")
        return 2

    try:
        import psycopg
    except ImportError:
        print("psycopg 가 없다:  pip install 'psycopg[binary]'")
        return 2

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("select extname from pg_extension where extname in ('pg_bigm','pg_trgm')")
        exts = sorted(r[0] for r in cur.fetchall())
        print(f"설치된 검색 확장: {', '.join(exts) or '없음'}")

        cur.execute("select indexname from pg_indexes where tablename='raw_line' order by 1")
        idx = [r[0] for r in cur.fetchall()]
        print(f"raw_line 인덱스: {', '.join(idx)}\n")

        rows = sample_rows(args.rows)
        with cur.copy(
            "copy raw_line (workspace, channel, doc_path, line_no, spoken_at,"
            " speaker, body, content_sha) from stdin"
        ) as cp:
            for r in rows:
                cp.write_row(r)
        cur.execute("analyze raw_line")
        print(f"측정용 {args.rows:,}행 투입(끝나면 되돌린다)\n")

        def measure() -> list[tuple[str, bool, float]]:
            out = []
            for q in QUERIES:
                cur.execute(
                    "explain (analyze) select id from raw_line where body like %s",
                    (f"%{q}%",),
                )
                plan = "\n".join(r[0] for r in cur.fetchall())
                used = "Index Scan" in plan  # Bitmap Index Scan 도 포함된다
                ms = float(plan.rsplit("Execution Time: ", 1)[-1].split(" ms")[0])
                out.append((q, used, ms))
            return out

        primary = measure()

        if not args.compare:
            print(f"{'검색어':<10} {'글자':<4} {'계획':<12} 실제 시간")
            print("-" * 48)
            for q, used, ms in primary:
                print(f"{q:<10} {len(q):<4} {'인덱스' if used else '전체 스캔':<12} {ms:.2f} ms")
        else:
            # 같은 데이터·같은 트랜잭션 안에서 인덱스만 바꿔 끼운다.
            # PostgreSQL 은 인덱스 생성도 트랜잭션이라 rollback 으로 원상복구된다.
            cur.execute("drop index if exists raw_line_bigm")
            cur.execute(
                "create index raw_line_trgm_probe on raw_line using gin (body gin_trgm_ops)"
            )
            cur.execute("analyze raw_line")
            other = measure()

            print(f"{'검색어':<10} {'글자':<4} {'pg_bigm':<20} pg_trgm")
            print("-" * 60)
            for (q, bu, bm), (_, tu, tm) in zip(primary, other, strict=True):
                left = f"{'인덱스' if bu else '전체 스캔':<6} {bm:7.2f}ms"
                right = f"{'인덱스' if tu else '전체 스캔':<6} {tm:7.2f}ms"
                print(f"{q:<10} {len(q):<4} {left:<20} {right}")

        # 측정 데이터와 임시 인덱스는 남기지 않는다
        conn.rollback()

    if not args.compare:
        print("\n2글자가 '전체 스캔' 이면 pg_bigm 이 없거나 인덱스가 안 걸린 것이다:")
        print('    sudo -u postgres psql -p 55432 -d tyslackai'
              ' -c "CREATE EXTENSION pg_bigm;"')
        print("    psql -h 127.0.0.1 -p 55432 -U <사용자> -d tyslackai"
              " -f deploy/sql/index_schema.sql")
        print("\npg_trgm 과 맞대어 보려면:  --compare --rows 20000")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
