"""수정·삭제된 메시지를 **일반 근거에서 뺀다.**

결정: 2026-09-25 오너 §3, 2026-09-26 후속 지시(revision reader).
쓰는 쪽: `archiving_bot._ingest_revision` · 표 `archive_message_revision`.

## 무엇이 문제였나

수집기는 수정·삭제를 **원문에 쌓는다**(원칙 1 — 원문은 안 고친다). 그래서 한
메시지가 이렇게 남는다.

```
> [… |1790070000.000001] U1: 10시입니다            ← 고치기 전 본문
> [… |1790070000.000001] U1: [수정 전] 10시입니다   ← 같은 것이 한 번 더
> [… |1790070000.000001] U1: [수정 후] 11시입니다   ← 지금 본문
```

reader 가 없으면 검색이 **세 줄을 다 집는다.** 「10시」 로 물으면 고치기 전
문장이 근거로 나오고, 지운 메시지도 `[삭제 전]` 줄로 그대로 나온다. 사람이
고치거나 지운 뜻이 뒤집힌다.

## 두 단계로 거른다

**1단계 — 파일만 보고**(DB 없이도 된다)

좌표 `(channel_id, message_ts)` 로 묶는다. 그 묶음에 표시줄(`[수정 전]` 등)이
하나라도 있으면, **표시 없는 줄은 고치기 전 원본**이다. 지금 본문이 아니다.

이건 파일 자체의 성질이라 DB 와 무관하게 참이다.

**2단계 — DB 로 좁힌다**

두 번 고친 메시지에는 `[수정 후]` 가 두 줄이다. 어느 쪽이 지금인지는 파일만
봐서는 모른다(둘 다 같은 모양이다). `archive_message_revision` 의 최신 행이
그 답을 들고 있다 — `kind` 와 `body_sha256`.

- 최신이 `delete`·`redact` → **그 좌표 전체를 뺀다**
- 최신이 `change`·`create` → 본문 해시가 맞는 줄 **하나만** 남긴다

## DB 를 못 보면 통째로 뺀다

「지워졌는지 모른다」 와 「안 지워졌다」 는 다르다. 모르는 채로 보여 주면 지운
메시지가 근거로 나갈 수 있고, 그건 되돌릴 수 없다 — 사람은 이미 그 내용을 봤다.

그래서 표시줄이 있는 좌표는 DB 를 못 보는 동안 **전부 제외**한다. 표시줄이 없는
좌표(= 수정·삭제된 적 없는 메시지)는 영향을 받지 않는다. 옛 자료가 그대로
검색되는 이유가 이것이다.

## 원문은 손대지 않는다

여기는 **읽을 때만** 거른다. 파일에서 줄을 지우지 않는다 — 지우면 감사에서 무엇이
바뀌었는지 못 본다. 과거 revision 은 `audit_lines()` 로만 보인다.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass

log = logging.getLogger("tybot.archive.revision_reader")

#: 수집기가 원문에 쓰는 표시. `archiving_bot._ingest_revision` 과 **같아야 한다** —
#: 한쪽만 고치면 그 표시가 일반 근거로 새어 나온다.
SUPERSEDED_MARKERS = ("[수정 전]", "[삭제 전]", "[삭제됨]")
CURRENT_MARKER = "[수정 후]"

ALL_MARKERS = (*SUPERSEDED_MARKERS, CURRENT_MARKER)

_MARKER_RE = re.compile(r"^\s*(\[(?:수정 전|수정 후|삭제 전|삭제됨)\])\s*")


def body_digest(body: str) -> str:
    """`revision_store.body_digest` 와 같은 규칙. 두 곳이 갈리면 매칭이 전부 실패한다."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest() if body else ""


def strip_marker(text: str) -> tuple[str, str]:
    """`("[수정 후]", "11시입니다")`. 표시가 없으면 `("", 원문)`."""
    found = _MARKER_RE.match(text or "")
    if not found:
        return "", (text or "").strip()
    return found.group(1), (text or "")[found.end():].strip()


@dataclass(frozen=True)
class LatestRevision:
    """한 좌표의 최신 revision. `None` 은 **기록이 없다**(옛 자료) 다."""

    kind: str
    body_sha256: str

    @property
    def removed(self) -> bool:
        return self.kind in ("delete", "redact")


#: DB 를 못 봤다는 표식. `None`(기록 없음)과 **구분해야 한다** — 하나는 「옛
#: 자료라 그대로 보여 준다」 이고 다른 하나는 「모르니 감춘다」 이다.
UNKNOWN = object()


def visible_lines(lines, *, workspace: str, channel_id: str, lookup=None) -> list:
    """일반 근거로 쓸 줄만 남긴다. **원문은 안 건드린다.**

    좌표는 `workspace + channel_id + message_ts` **셋 다**여야 한다. Slack `ts`
    는 워크스페이스·채널을 가로질러 같은 값이 나올 수 있고, 둘만 보면 남의 채널
    삭제 기록이 이 채널 줄을 감춘다.

    `lookup(workspace, channel_id, message_ts)` 는 `LatestRevision`, `None`
    (기록 없음), 또는 `UNKNOWN`(DB 를 못 봄)을 돌려준다.
    """
    groups = _group(lines)
    if not groups:
        return list(lines)

    resolver = lookup or latest_revision
    keep: set[int] = set()
    for message_ts, members in groups.items():
        keep.update(
            _keep_in_group(members, resolver, workspace, channel_id, message_ts)
        )
    return [line for index, line in enumerate(lines) if index in keep]


def _group(lines) -> dict[str, list[tuple[int, object]]]:
    """`message_ts` 로 묶는다. 채널은 호출부가 안다(문서 하나가 한 채널이다).

    좌표가 없는 줄은 **그대로 통과**시킨다 — 옛 자료에는 `message_ts` 가 없다.
    """
    groups: dict[str, list[tuple[int, object]]] = {}
    for index, line in enumerate(lines):
        coordinate = str(getattr(line, "message_ts", "") or "")
        groups.setdefault(coordinate, []).append((index, line))
    return groups


def _keep_in_group(members, resolver, workspace, channel_id, message_ts) -> set[int]:
    if not message_ts:
        return {index for index, _ in members}

    marked = [
        (index, line, strip_marker(str(getattr(line, "text", "") or "")))
        for index, line in members
    ]
    has_marker = any(marker for _, _, (marker, _) in marked)
    if not has_marker:
        # 수정·삭제된 적이 없다. **옛 자료가 여기 해당한다.**
        return {index for index, _, _ in marked}

    try:
        latest = resolver(workspace, channel_id, message_ts)
    except Exception:  # noqa: BLE001 - 못 보면 감춘다
        log.warning("revision 상태를 못 봤다 ws=%s ts=%s — 제외한다", workspace, message_ts)
        latest = UNKNOWN

    if latest is UNKNOWN or latest is None:
        # `None` 도 감춘다. 표시줄이 있는데 기록이 없다는 것은 **둘이 어긋났다**는
        # 뜻이고, 어긋난 상태에서 보여 줄 쪽을 고를 근거가 없다.
        return set()
    if latest.removed:
        return set()

    if latest.kind == "create" and len(members) == 1:
        # 사람이 실제 본문을 `[수정 후]` 같은 글자로 시작할 수 있다. DB 최신 상태가
        # create 라면 수집기가 만든 revision 표시가 아니라 사람 원문이므로, 표시를
        # 떼지 않은 전체 본문 해시로 확인해 그대로 남긴다.
        for index, line, _ in marked:
            text = str(getattr(line, "text", "") or "").strip()
            if body_digest(text) == latest.body_sha256:
                return {index}
        return set()

    # 최신 본문과 해시가 맞는 줄 **하나만**. 두 번 고치면 `[수정 후]` 가 둘인데,
    # 지금 본문은 하나뿐이다. 같은 본문으로 되돌린 경우에는 가장 나중에 append 된
    # 줄이 최신 revision 이므로 뒤에서부터 찾는다.
    for index, _, (marker, body) in reversed(marked):
        if marker == CURRENT_MARKER and body_digest(body) == latest.body_sha256:
            return {index}
    # 파일과 DB 가 안 맞는다. 무엇이 지금인지 모르므로 보여 주지 않는다.
    log.warning(
        "revision 본문이 원문과 맞지 않는다 ws=%s ts=%s — 제외한다", workspace, message_ts
    )
    return set()


def audit_lines(lines) -> list:
    """**전부** 돌려준다. 감사 조회 전용.

    일반 조회와 함수를 나누는 이유: 같은 함수에 플래그로 두면 호출부 하나가
    기본값을 잘못 줘서 지워진 문장이 답변에 나간다.
    """
    return list(lines)


# ---------------------------------------------------------------------------
# DB 조회
# ---------------------------------------------------------------------------


def enabled() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def latest_revision(workspace: str, channel_id: str, message_ts: str):
    """좌표 하나의 최신 revision. 못 보면 `UNKNOWN`."""
    got = latest_revisions(workspace, [(channel_id, message_ts)])
    if got is UNKNOWN:
        return UNKNOWN
    return got.get((channel_id, message_ts))


def latest_revisions(workspace: str, coordinates):
    """여러 좌표를 한 번에. 줄마다 질의하면 문서 하나에 수백 번 간다.

    돌려주는 것은 `{(channel_id, message_ts): LatestRevision}` 이고, DB 를 못
    보면 `UNKNOWN` 이다. 빈 dict(기록 없음)와 **구분해야 한다.**
    """
    wanted = [(str(c or ""), str(t or "")) for c, t in coordinates if t]
    if not wanted:
        return {}
    if not enabled():
        return UNKNOWN
    try:
        from ..console.workspace_store import _connect

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (channel_id, message_ts)
                       channel_id, message_ts, kind, body_sha256
                  FROM archive_message_revision
                 WHERE workspace = %s AND message_ts = ANY(%s)
                 ORDER BY channel_id, message_ts, revision_no DESC
                """,
                (workspace, [ts for _, ts in wanted]),
            )
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - 못 보면 감춘다(fail-closed)
        log.warning("revision 표를 읽지 못했다 ws=%s — 해당 좌표를 감춘다", workspace)
        return UNKNOWN

    return {
        (str(row["channel_id"]), str(row["message_ts"])): LatestRevision(
            str(row["kind"]), str(row["body_sha256"] or "")
        )
        for row in rows
    }


def index_excluded_keys(
    lines, *, workspace: str, channel_id: str, lookup=None
) -> set[tuple[str, int]]:
    """색인에서 빼야 할 `(doc_path, line_no)`.

    검색 색인이 옛 revision 줄을 들고 있으면 직접 조회와 결과가 갈린다. 재색인이
    이 목록으로 지운다(`scripts/reindex_revisions.py`).
    """
    kept = {
        id(line)
        for line in visible_lines(
            lines, workspace=workspace, channel_id=channel_id, lookup=lookup
        )
    }
    return {
        (str(getattr(line, "source_path", "") or ""), int(getattr(line, "lineno", 0)))
        for line in lines
        if id(line) not in kept
    }


def apply(doc):
    """문서 하나의 `raw_lines` 를 **일반 근거용으로** 거른다.

    좌표를 한 번에 조회한다 — 줄마다 질의하면 문서 하나에 수백 번 간다.
    같은 문서 안에서 DB 를 두 번 보지 않으므로, 중간에 값이 바뀌어 같은 답 안에서
    어떤 줄은 보이고 어떤 줄은 빠지는 일도 없다.
    """
    lines = getattr(doc, "raw_lines", None) or []
    coordinates = {
        str(getattr(line, "message_ts", "") or "") for line in lines
    } - {""}
    if not coordinates:
        return doc
    channel_id = str(getattr(doc, "channel_id", "") or "")
    workspace = str(getattr(doc, "workspace", "") or "")

    # 표시줄이 하나도 없으면 DB 를 볼 이유가 없다. 옛 자료가 대부분 여기다 —
    # 매 문서마다 질의하면 DB 가 답변 경로의 병목이 된다.
    if not any(strip_marker(str(getattr(line, "text", "") or ""))[0] for line in lines):
        return doc

    table = latest_revisions(workspace, [(channel_id, ts) for ts in coordinates])

    def lookup(_ws, _ch, message_ts):
        if table is UNKNOWN:
            return UNKNOWN
        return table.get((channel_id, message_ts))

    doc.raw_lines = visible_lines(
        lines, workspace=workspace, channel_id=channel_id, lookup=lookup
    )
    return doc
