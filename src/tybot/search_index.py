"""검색 색인 — MD 원문을 `raw_line` 에 넣고 거기서 찾는다 (B-40).

## 왜 필요했나

`raw_line` 테이블과 pg_bigm 인덱스는 진작 만들어져 있었는데 **아무것도 쓰지 않았다.**
검색은 `ArchiveStore.search()` 가 보이는 MD 를 전부 파싱해 파이썬으로 훑었다.
문서가 늘면 선형으로 느려지고, 점수가 토큰 포함 개수뿐이라 최근성도 구절도 없었다.

## 원문은 그대로 MD 에 둔다

색인은 **버려도 되는 사본**이다(`content_sha` 로 언제든 재빌드). 원문을 DB 로 옮기면
인덱싱이 빨라지는 것이 아니라, git 감사 이력·디렉터리 ACL·사람이 읽는 검토·DB 를
잃어도 남는 원본을 잃는다. 방향은 한쪽이다 — **MD → DB.**

## 지키는 선 셋

1. **권한은 코드가 소유한다.** 이 모듈은 질의에 채널 목록을 받기만 하고,
   `can_access` 판정은 하지 않는다(원칙 3). 판정을 SQL 로 옮기면 ACL 이 두 곳으로
   갈라지고, 어느 쪽이 맞는지 알 수 없게 된다.
2. **DB 를 못 읽으면 `None` 을 돌려준다.** 호출부가 파일 스캔으로 돌아간다.
   검색이 DB 하나에 묶이면 DB 장애가 곧 「자료를 찾지 못했습니다」 답변이 된다 —
   그건 장애가 아니라 정상 답으로 보인다.
3. **점수는 한 곳에서만 만든다**(`score_line`). 색인 경로와 파일 경로가 각자 점수를
   내면 갈려도 에러가 안 나고, 같은 질문에 다른 답이 나온다. Hermes 가 근거 대조에서
   같은 함정에 다섯 번 걸렸다.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("tybot.search_index")

# 검색어 토큰: **2자 이상** 한글/영숫자.
# 예전에는 이 규칙이 `store.py` 에도 따로 적혀 있었다. 한쪽만 고치면 색인 후보와
# 파일 스캔이 서로 다른 토큰으로 찾는데 **에러는 안 나고 결과만 달라진다.**
TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")

# 한 질문에 DB 에서 끌어올 후보 상한. 점수는 우리가 다시 매기므로 넉넉히 받는다.
CANDIDATE_LIMIT = 500

# 구절이 그대로 들어 있으면 얹는 점수. 토큰이 흩어져 맞은 줄보다 앞세운다.
PHRASE_BONUS = 3


class IndexError_(Exception):
    """색인을 쓰거나 읽을 수 없다."""


@dataclass(frozen=True)
class Candidate:
    doc_path: str
    line_no: int
    spoken_at: str


def tokens_of(query: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(query or "")]


def score_line(tokens: list[str], query: str, speaker: str, text: str) -> int:
    """한 줄의 점수. **색인 경로와 파일 경로가 이 함수 하나를 쓴다.**

    두 곳에 적으면 갈려도 에러가 안 나고, 같은 질문에 다른 답이 나온다.
    """
    hay = f"{speaker} {text}".lower()
    score = sum(1 for t in tokens if t in hay)
    if not score:
        return 0
    phrase = (query or "").strip().lower()
    # 구절이 통째로 들어 있으면 얹는다. 토큰이 우연히 흩어져 맞은 줄보다 앞세운다.
    if len(phrase) >= 2 and phrase in hay:
        score += PHRASE_BONUS
    return score


def rel_path(path, root) -> str:
    """아카이브 뿌리 기준 상대 경로. 뿌리를 모르면 파일명만.

    **색인과 검색이 같은 함수를 쓴다.** 한쪽만 절대 경로로 두면 매칭이 전부 실패하고,
    오류 없이 파일 스캔으로 되돌아간다.
    """
    from pathlib import Path

    p = Path(path)
    if root is None:
        return p.name
    try:
        return p.relative_to(Path(root)).as_posix()
    except ValueError:
        return p.name


def _connect():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except Exception as exc:  # noqa: BLE001 - 검색이 DB 하나에 묶이면 안 된다
        log.warning("검색 색인 DB 에 붙지 못했습니다: %s", exc)
        return None


# --- 읽기 -------------------------------------------------------------------
def candidates(query: str, channels: list[str]) -> list[Candidate] | None:
    """질의어가 든 줄의 후보. **DB 를 못 쓰면 `None`** — 빈 목록과 구별해야 한다.

    빈 목록은 "색인에 없다", `None` 은 "색인을 못 봤다" 다. 둘을 섞으면 DB 장애가
    「자료 없음」 답변으로 나가고, 그건 정상 답처럼 보인다.

    `channels` 는 **이미 권한을 통과한** 채널 목록이다. 이 함수는 판정하지 않는다.
    """
    words = tokens_of(query)
    if not words or not channels:
        return [] if channels else None

    conn = _connect()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            # pg_bigm 은 `LIKE '%...%'` 를 인덱스로 받는다. 토큰마다 OR 로 걸고
            # 점수는 파이썬에서 다시 매긴다 — SQL 에 점수를 넣으면 파일 경로와 갈린다.
            # clauses 는 **인덱스로만** 만든다. 값은 전부 바인딩이라 주입 경로가 없다.
            clauses = " OR ".join(
                f"body ILIKE %(w{i})s OR speaker ILIKE %(w{i})s"
                for i in range(len(words))
            )
            params: dict[str, object] = {
                f"w{i}": f"%{w}%" for i, w in enumerate(words)
            }
            params["channels"] = channels
            params["lim"] = CANDIDATE_LIMIT
            cur.execute(
                f"""
                SELECT doc_path, line_no, spoken_at
                  FROM raw_line
                 WHERE channel = ANY(%(channels)s)
                   AND ({clauses})
                 ORDER BY spoken_at DESC
                 LIMIT %(lim)s
                """,
                params,
            )
            return [
                Candidate(
                    doc_path=str(r["doc_path"]),
                    line_no=int(r["line_no"]),
                    spoken_at=str(r["spoken_at"]),
                )
                for r in cur.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001 - 조회 실패는 파일 스캔으로 넘긴다
        log.warning("검색 색인 조회 실패 — 파일 스캔으로 넘어갑니다: %s", exc)
        return None


# --- 쓰기 -------------------------------------------------------------------
def reindex(docs, root=None, *, batch: int = 1000) -> dict:
    """MD 문서들을 `raw_line` 에 넣는다. **멱등하다** — 같은 줄을 다시 넣어도 늘지 않는다.

    경로는 **아카이브 뿌리 기준 상대 경로**로 저장한다. 절대 경로로 넣으면 개발 PC 와
    서버가 달라 색인이 통째로 안 맞고, 그때 검색은 오류 없이 **파일 스캔으로 조용히
    되돌아간다** — 색인이 도는 줄 알면서 안 쓰는 상태가 된다.

    지운 줄은 여기서 지우지 않는다. 아카이브 원문은 편집하지 않는 것이 계약이므로
    (원칙 1) 줄이 사라지는 일은 문서가 다시 쓰일 때뿐이고, 그때는 `content_sha` 가
    달라져 새 행이 들어온다. 옛 행은 쌓이지만 점수 계산에서 걸러진다 —
    문서에 없는 줄 번호는 조회 뒤 매칭에서 떨어진다.
    """
    conn = _connect()
    if conn is None:
        raise IndexError_("DATABASE_URL 이 없거나 DB 에 붙지 못했습니다.")

    written = 0
    seen_docs = 0
    try:
        with conn, conn.cursor() as cur:
            rows: list[tuple] = []
            for doc in docs:
                seen_docs += 1
                for line in doc.raw_lines:
                    if not line.text.strip():
                        continue
                    rows.append((
                        doc.workspace,
                        doc.channel,
                        rel_path(doc.path, root),
                        line.lineno,
                        line.ts,
                        line.speaker,
                        line.text,
                        _sha(doc.channel, line.lineno, line.text),
                    ))
                    if len(rows) >= batch:
                        written += _flush(cur, rows)
                        rows = []
            written += _flush(cur, rows)
    except IndexError_:
        raise
    except Exception as exc:
        raise IndexError_(f"색인 실패: {exc}") from exc

    log.info("검색 색인 문서=%d 줄=%d", seen_docs, written)
    return {"docs": seen_docs, "lines": written}


def _sha(channel: str, lineno: int, text: str) -> str:
    import hashlib

    raw = f"{channel}|{lineno}|{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _flush(cur, rows: list[tuple]) -> int:
    if not rows:
        return 0
    cur.executemany(
        """
        INSERT INTO raw_line
               (workspace, channel, doc_path, line_no, spoken_at,
                speaker, body, content_sha)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (doc_path, line_no, content_sha) DO NOTHING
        """,
        rows,
    )
    return len(rows)


def indexed_at() -> str | None:
    """가장 최근 색인 시각. 헬스 체크가 색인이 멈췄는지 보는 자리다.

    색인이 멈추면 검색이 **조용히 옛 자료만** 본다 — 오류도 경고도 안 난다.
    """
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT max(indexed_at) AS at FROM raw_line")
            row = cur.fetchone()
            return str(row["at"]) if row and row["at"] else None
    except Exception as exc:  # noqa: BLE001
        log.warning("색인 시각을 읽지 못했습니다: %s", exc)
        return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .archive.store import ArchiveStore
    from .envfile import load_env_file
    from .paths import archive_dir

    load_env_file()
    ap = argparse.ArgumentParser(description="MD 원문을 검색 색인에 넣는다")
    ap.add_argument("--archive", default=None, help="기본: ARCHIVE_DIR")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = ArchiveStore(args.archive or archive_dir())
    try:
        result = reindex(store.docs(), store.root)
    except IndexError_ as exc:
        log.error("%s", exc)
        return 1
    print(f"문서 {result['docs']}건, 줄 {result['lines']}건 색인")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
