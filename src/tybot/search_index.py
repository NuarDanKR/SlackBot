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

from .archive.text_normalize import remove_nul

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


# 어절 끝의 조사. 긴 것부터 본다(「에서」 를 「에」 로 먼저 자르지 않게).
#
# 검색어는 어절 단위로 잘리므로 「기성률은」 은 `%기성률은%` 로 찾게 되고, 본문의
# 「기성률」 에 맞지 않는다. 분류기가 핵심어를 뽑지 못해 질문 원문이 그대로 오는
# 경로에서 특히 그랬다.
#
# **떼면 넓어지기만 한다.** 남는 말은 원래 어절의 앞부분이라, 원래 어절이 든 줄은
# 떼고 난 말로도 그대로 찾힌다. 그래서 남는 말이 2자 이상일 때만 뗀다 — 「회의」 를
# 「회」 로 만들면 넓어지는 정도가 아니라 아무 줄이나 맞는다.
JOSA = (
    "에서는", "으로는", "에서", "으로", "에게", "한테", "까지", "부터", "보다", "처럼",
    "이랑", "에는", "에도", "이나", "과는", "와는",
    "은", "는", "이", "가", "을", "를", "의", "에", "로", "와", "과", "도", "만", "랑",
)
_HANGUL_WORD = re.compile(r"^[가-힣]+$")


def _strip_josa(token: str) -> str:
    if len(token) < 3 or not _HANGUL_WORD.match(token):
        return token
    for particle in JOSA:
        if token.endswith(particle) and len(token) - len(particle) >= 2:
            return token[: -len(particle)]
    return token


def tokens_of(query: str) -> list[str]:
    return [_strip_josa(t.lower()) for t in TOKEN_RE.findall(query or "")]


def score_line(tokens: list[str], query: str, speaker: str, text: str) -> int:
    """한 줄의 점수. **색인 경로와 파일 경로가 이 함수 하나를 쓴다.**

    두 곳에 적으면 갈려도 에러가 안 나고, 같은 질문에 다른 답이 나온다.

    낱말은 **띄어쓰기를 무시하고** 맞춘다 — 후보 SQL(`_match`)과 같은 기준이다
    (`COMPACT_BODY`). 발화자와 본문 사이에는 공백이 아닌 구분자를 둔다. 공백으로
    이으면 띄어쓰기를 지울 때 「김부장」+「공사비」 가 붙어 「부장공」 같은 없는 낱말이
    맞는다.
    """
    hay = f"{speaker} {text}".lower()
    compact = f"{speaker}\n{text}".lower().replace(" ", "")
    score = sum(1 for t in tokens if t in compact)
    if not score:
        return 0
    phrase = (query or "").strip().lower()
    # 구절이 통째로 들어 있으면 얹는다. 토큰이 우연히 흩어져 맞은 줄보다 앞세운다.
    # 구절은 띄어쓰기까지 같은 경우만 — 사람이 적은 그대로인 줄을 앞세운다.
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
    words = list(dict.fromkeys(tokens_of(query)))
    if not words or not channels:
        return [] if channels else None

    conn = _connect()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            return _ranked_candidates(cur, words, channels, CANDIDATE_LIMIT)
    except Exception as exc:  # noqa: BLE001 - 조회 실패는 파일 스캔으로 넘긴다
        log.warning("검색 색인 조회 실패 — 파일 스캔으로 넘어갑니다: %s", exc)
        return None


def _variants(word: str) -> list[str]:
    """한 낱말을 찾을 표기들. 영문이 섞이면 대소문자 세 가지를 함께 찾는다.

    `ILIKE` 를 쓰면 pg_bigm 인덱스를 못 탄다(실측 2026-11-09: 전 질의 110만 줄
    전체 스캔, 5~7초). 한글에는 대소문자가 없으므로 `LIKE` 로 충분하다. 영문
    약어(「PF」·「RFI」)는 본문에 대문자로 적히는 일이 많아 소문자만 찾으면
    빠진다 — 그래서 소문자·대문자·첫 글자 대문자를 같이 찾는다. 「iPhone」 처럼
    섞인 표기는 이 셋에 안 들어가는데, 그 대가로 인덱스를 지킨다.
    """
    if not re.search(r"[a-z]", word):
        return [word]
    return list(dict.fromkeys([word, word.upper(), word.capitalize()]))


#: 본문에서 띄어쓰기를 지운 식. **`index_schema.sql` 의 `raw_line_bigm_compact` 와
#: 글자까지 같아야** 인덱스를 탄다 — 식이 다르면 오류 없이 110만 줄 전체 스캔이 된다.
#:
#: 왜 띄어쓰기를 지우나. 「주간업무보고」 로 물으면 본문의 「주간 업무보고」 를 못
#: 찾았다. 운영 실측(2026-11-09, 110만 줄, 같은 낱말을 그대로/공백 무시로 센 줄 수):
#:
#: | 낱말 | 그대로 | 공백 무시 | 늘어난 줄의 실제 표기 |
#: |---|---|---|---|
#: | 주간업무보고 | 334 | 498 | 「주간 업무보고」 143 · 「주간업무 보고」 10 |
#: | PF대출 | 2,261 | 2,616 | 「PF 대출」 347 |
#: | 안전점검 | 964 | 1,021 | 「안전 점검」 57 |
#: | 하도급 | 7,069 | 8,272 | 글자마다 띄운 표 머리글(「하 도 급」) |
#: | 회의록·품의서·준공검사 | 같음 | 같음 | — |
#:
#: 잃는 쪽도 쟀다. 「착공계」 는 「착공 계획」 19줄이 새로 맞는다 — 점수(`score_line`)와
#: 구절 가점이 사람이 적은 그대로인 줄을 앞세우므로 근거 자리를 뺏지는 않는다.
COMPACT_BODY = "replace(body, ' ', '')"


def _match(word: str, params: list) -> str:
    """낱말 하나가 본문(띄어쓰기 무시)이나 발화자에 들어 있다. 값은 전부 바인딩한다."""
    parts = []
    for form in _variants(word):
        pattern = f"%{form}%"
        parts += [f"{COMPACT_BODY} LIKE %s", "speaker LIKE %s"]
        params += [pattern, pattern]
    return "(" + " OR ".join(parts) + ")"


def _ranked_candidates(cur, words: list[str], channels: list[str], limit: int) -> list[Candidate]:
    """**낱말을 많이 맞춘 줄부터**, 같으면 최근 줄부터 후보를 고른다.

    예전에는 낱말 중 **하나라도** 든 줄을 최근 순으로 500줄 잘랐다. 「현장」·「보고」
    처럼 흔한 낱말이 끼면 최근 며칠 치가 그 500줄을 다 채워, 낱말을 모두 가진 옛
    줄이 점수를 받기도 전에 버려졌다. 실측(2026-11-09, 110만 줄):

    | 검색어 | 모두 맞는 줄 | 500줄에 남은 줄 | 창이 덮은 기간 |
    |---|---|---|---|
    | 자금 보고 | 2,635 | 0 | 그날 하루 |
    | 기성 현장 | 3,219 | 2 | 사흘 |

    그래서 두 단계로 고른다.

    1. 모두 맞는 줄 — 최근 순. 인덱스 교집합이라 빠르다(실측 10~20ms)
    2. 자리가 남으면 일부만 맞는 줄 — 맞춘 낱말 수 순, 같으면 최근 순

    점수는 여전히 파이썬(`score_line`)이 매긴다. 여기서 정하는 것은 **누가 후보에
    드는가**뿐이다.
    """
    def run(sql: str, params: list) -> list[Candidate]:
        cur.execute(sql, params)
        return [
            Candidate(
                doc_path=str(r["doc_path"]),
                line_no=int(r["line_no"]),
                spoken_at=str(r["spoken_at"]),
            )
            for r in cur.fetchall()
        ]

    params: list = [channels]
    every = " AND ".join(_match(w, params) for w in words)
    params.append(limit)
    found = run(
        f"""SELECT doc_path, line_no, spoken_at FROM raw_line
             WHERE channel = ANY(%s) AND {every}
             ORDER BY spoken_at DESC LIMIT %s""",
        params,
    )
    if len(words) == 1 or len(found) >= limit:
        return found

    params = []
    matched = " + ".join(f"({_match(w, params)})::int" for w in words)
    params.append(channels)
    some = " OR ".join(_match(w, params) for w in words)
    every = " AND ".join(_match(w, params) for w in words)
    params.append(limit - len(found))
    return found + run(
        f"""SELECT doc_path, line_no, spoken_at, {matched} AS matched FROM raw_line
             WHERE channel = ANY(%s) AND ({some}) AND NOT ({every})
             ORDER BY matched DESC, spoken_at DESC LIMIT %s""",
        params,
    )


def indexed_counts(doc_paths: list[str]) -> dict[str, int] | None:
    """문서별로 색인에 든 줄 수. **DB 를 못 보면 `None`.**

    설계 §7.1 은 문서별 manifest(`archive_content_sha`)를 두자고 한다. 우리 아카이브는
    **append-only** 라(원문 편집 금지, 원칙 1) 일반적으로 줄 수로 판정할 수 있다.
    다만 revision reader가 과거 줄을 숨기면 DB 행이 파일의 현재 가시 줄보다 많을
    수 있으므로 호출부는 수가 **정확히 같은지** 본다.

    이 방법이 못 잡는 것: 파일을 손으로 고쳐 **줄 수를 유지한 채** 내용만 바꾼 경우.
    그건 원칙 1 위반이고, 그때는 재색인을 사람이 돌린다.
    """
    paths = [p for p in doc_paths if p]
    if not paths:
        return {}
    conn = _connect()
    if conn is None:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_path, count(*) AS n
                  FROM raw_line
                 WHERE doc_path = ANY(%(paths)s)
                 GROUP BY doc_path
                """,
                {"paths": paths},
            )
            return {str(r["doc_path"]): int(r["n"]) for r in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 - 조회 실패는 파일 스캔으로 넘긴다
        log.warning("색인 최신성 확인 실패 — 파일 스캔으로 보완합니다: %s", exc)
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

    `lines` 는 **실제로 들어간 줄**이다(`ON CONFLICT` 로 건너뛴 행 제외). 보낸 줄
    수는 `seen_lines` 로 따로 돌려준다 — 둘이 같으면 전부 새 줄이었다는 뜻이다.
    """
    conn = _connect()
    if conn is None:
        raise IndexError_("DATABASE_URL 이 없거나 DB 에 붙지 못했습니다.")

    written = 0
    seen_lines = 0
    seen_docs = 0
    nul_lines = 0
    nul_chars = 0
    try:
        with conn, conn.cursor() as cur:
            rows: list[tuple] = []
            for doc in docs:
                seen_docs += 1
                for line in doc.raw_lines:
                    body, n5 = remove_nul(str(line.text or ""))
                    if not body.strip():
                        if n5:
                            nul_lines += 1
                            nul_chars += n5
                        continue
                    workspace, n1 = remove_nul(str(doc.workspace or ""))
                    channel, n2 = remove_nul(str(doc.channel or ""))
                    spoken_at, n3 = remove_nul(str(line.ts or ""))
                    speaker, n4 = remove_nul(str(line.speaker or ""))
                    removed = n1 + n2 + n3 + n4 + n5
                    if removed:
                        nul_lines += 1
                        nul_chars += removed
                    rows.append((
                        workspace,
                        channel,
                        # 합쳐진 채널 문서의 대표 경로가 아니라 줄이 실제로 있는
                        # 일자 파일 경로다. 일자 파일마다 line_no가 다시 시작하므로
                        # 대표 경로를 쓰면 서로 다른 날의 같은 번호가 충돌한다.
                        rel_path(line.source_path or doc.path, root),
                        line.lineno,
                        spoken_at,
                        speaker,
                        body,
                        content_sha(channel, line.lineno, body),
                    ))
                    seen_lines += 1
                    if len(rows) >= batch:
                        written += _flush(cur, rows)
                        rows = []
            written += _flush(cur, rows)
    except IndexError_:
        raise
    except Exception as exc:
        raise IndexError_(f"색인 실패: {exc}") from exc

    if nul_chars:
        log.warning(
            "검색 색인 파생 텍스트에서 NUL 제거 lines=%d chars=%d",
            nul_lines,
            nul_chars,
        )
    log.info("검색 색인 문서=%d 보낸 줄=%d 새 줄=%d", seen_docs, seen_lines, written)
    return {"docs": seen_docs, "lines": written, "seen_lines": seen_lines}


def content_sha(channel: str, lineno: int, text: str) -> str:
    """`raw_line.content_sha` 계약. 정리·재색인도 이 함수 하나를 쓴다."""
    import hashlib

    raw = f"{channel}|{lineno}|{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _flush(cur, rows: list[tuple]) -> int:
    """넣은 줄 수. **`ON CONFLICT` 로 건너뛴 행은 안 센다.**

    보낸 줄 수를 돌려주면 두 번째 실행도 첫 번째와 같은 건수를 보고한다 — 그러면
    멱등한지 아닌지를 출력으로는 알 수 없다. `rowcount` 를 못 읽는 드라이버에서만
    보낸 줄 수로 내려간다.
    """
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
    affected = getattr(cur, "rowcount", None)
    if affected is None or affected < 0:
        return len(rows)
    return int(affected)


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
        # DM 도 색인한다(B-57). 권한은 조회 쪽이 쥔다 — `candidates()` 는 이미
        # `visible_docs()` 를 통과한 채널 목록만 받고, DM 의 `channel` 값은
        # 사람마다 다르다(`writer.dm_channel`). 색인에서 빼면 개인 작업공간만
        # 매번 파일 스캔으로 떨어져, 정작 본인 자료를 찾는 길이 제일 느려진다.
        result = reindex(store.docs(dm_scope="*"), store.root)
    except IndexError_ as exc:
        log.error("%s", exc)
        return 1
    print(f"문서 {result['docs']}건, 줄 {result['lines']}건 색인")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
