"""Hermes 일일 요약 후보 생성, 검토 Canvas·DM, 승인 상태 전이.

후보와 승인본은 파생 DB에만 둔다. 승인본은 검색 길잡이로 사용할 수 있지만 사실
답변에는 연결된 원문을 현재 권한으로 다시 열어 사용해야 한다.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KINDS = frozenset({"number_or_schedule", "new_issue", "closed_issue"})
OPEN_STATES = ("pending", "deferred")
ACTION_APPROVE = "tybot_summary_review_approve"
ACTION_REJECT = "tybot_summary_review_reject"
ACTION_DEFER = "tybot_summary_review_defer"
ACTION_APPROVE_ALL = "tybot_summary_review_approve_all"
REJECT_CALLBACK = "tybot_summary_review_reject_modal"

# --- Canvas 회차 (B-50) --------------------------------------------------------
#
# 검토자는 긴 요약과 근거를 **Canvas 에서 읽고**, 결정은 **DM 버튼**에서 한다.
# Canvas 안에는 Block Kit 버튼을 넣을 수 없어서 두 화면을 역할로 나눴다.
#
# 렌더러 버전은 `content_hash` 에 들어간다. 렌더 규칙이 바뀌면 같은 후보라도 다른
# 문서가 되므로, 버전을 안 섞으면 「같은 회차인데 내용이 다른」 Canvas 가 생긴다.
RENDERER_VERSION = 2
ARTIFACT_STATES = (
    "creating", "ready", "partial", "completed", "expired", "failed", "ambiguous",
)
# 반려 모달의 「틀린 부분」. **필수 선택**이다 — 무엇이 틀렸는지 분류가 없으면
# 나중에 같은 실수를 세어 볼 수가 없다.
WRONG_PARTS = (
    ("number", "숫자·금액"),
    ("date", "날짜·기간"),
    ("fact", "사실관계"),
    ("missing", "누락"),
    ("other", "기타"),
)
MAX_CORRECTION = 2000
MAX_CANDIDATES = 10
MAX_SOURCE_CHARS = 40_000
MAX_APPROVED_CHARS = 8_000
MAX_NEW_SOURCE_CHARS = MAX_SOURCE_CHARS - MAX_APPROVED_CHARS - 500
MIN_CORRECTION = 5
KST = timezone(timedelta(hours=9))
log = logging.getLogger("tybot.summary_review")


class SummaryReviewError(RuntimeError):
    pass


class AmbiguousProjection(SummaryReviewError):
    """전체 예상 요약을 **확정적으로** 만들 수 없다.

    후보가 바꾸려는 기존 문장을 못 찾았거나 두 번 찾았다는 뜻이다. 어느 쪽이든
    「어느 문장이 바뀌는지」 를 우리가 모른다 — 그 상태로 예상본을 그리면 사람은
    바뀌지 않을 문장이 바뀐다고 읽는다. Canvas 를 포기하고 후보 DM 으로 간다.
    """


# 후보의 형식 (B-60).
#
# 추출(`quote`)만 허용하던 동안은 같은 사실이 여러 날 나오면 그 수만큼 후보가 됐다.
# 30일치를 소급하면 검토자 앞에 수십 건이 쌓이고, 한 회차 DM 은 10건만 싣는다.
# 생성(`abstract`)을 허용하면 그것이 몇 건으로 묶인다.
#
# **푸는 것은 「제안 == 인용」 한 줄뿐이다.** 인용이 원문에 있는지, 숫자가 인용이나
# 기존 승인에 있는지는 form 과 무관하게 그대로 본다. 문장은 모델이 써도 되지만
# 사실은 원문에서만 온다 — 그리고 검토자가 인용과 나란히 보고 승인한다.
FORM_QUOTE = "quote"
FORM_ABSTRACT = "abstract"
FORMS = (FORM_QUOTE, FORM_ABSTRACT)


@dataclass(frozen=True)
class SourceLine:
    at: str
    author: str
    text: str
    locator: str
    # Slack 메시지 ts. 있으면 후보의 출처 링크가 **그 메시지 한 건**을 연다.
    # 좌표를 남기기 전에 수집한 줄은 비어 있고, 그때는 채널 링크로 내려간다(B-56).
    message_ts: str = ""


@dataclass(frozen=True)
class Proposal:
    kind: str
    current_text: str
    proposed_text: str
    evidence_quote: str
    evidence_at: str
    evidence_author: str
    evidence_locator: str
    evidence_message_ts: str = ""
    evidence_hash: str = ""
    # `quote` 는 원문을 그대로 오려 낸 것, `abstract` 는 모델이 쓴 문장(B-60).
    # 기본이 `quote` 인 이유는 **모르면 더 엄격한 쪽**이기 때문이다 — 모델이 form 을
    # 빼먹었을 때 생성문으로 통과시키면 검증이 한 겹 사라진다.
    form: str = FORM_QUOTE


def _as_uuid(value) -> uuid.UUID | None:
    """UUID 로 못 읽으면 `None`. **예외를 올리지 않는다.**

    Slack 버튼 값과 모달 metadata 는 밖에서 온 문자열이다. 잘못된 값 하나가
    핸들러를 죽이면 그 사람의 DM 은 영영 아무 반응이 없다 — 막되, 조용히 막는다.
    """
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _normalized(value: str) -> str:
    value = re.sub(r"[`*_\"'“”‘’]", "", value or "")
    value = re.sub(r"^[\s>•◦∙]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


NUMBER_RE = re.compile(
    r"(?<![가-힣A-Za-z])\d[\d,.]*(?:백만원|억원|천원|만원|개월|%|억|만|원|일|년)?"
)


def _number_values(value: str) -> list[str]:
    """검토 화면에 보여 줄 숫자를 원문 순서로, 중복 없이 돌려준다."""
    return list(dict.fromkeys(match.group(0) for match in NUMBER_RE.finditer(value or "")))


def _numbers(value: str) -> set[str]:
    return set(_number_values(value))


@dataclass
class GenerateStats:
    """이번 회차가 **무엇을 보고 무엇을 버렸는가.**

    「오늘 검토할 새 후보가 없습니다」 한 문장으로는 두 가지가 구별되지 않는다.
    읽을 원문이 아예 없었으면 소급 검토가 답이고, 원문은 읽었는데 요약기가 낸
    후보가 원문 대조에서 전부 떨어졌으면 소급해도 결과는 같다 — 조치가 다르다.
    """

    lines: int = 0
    proposed: int = 0
    accepted: int = 0
    # 이미 요약한 구간이라 LLM 을 부르지 않고 지나갔다.
    reused: bool = False


def parse_proposals(raw: str, source: list[SourceLine],
                    approved: list[str] | None = None,
                    stats: GenerateStats | None = None) -> list[Proposal]:
    """모델 JSON을 읽고 모든 근거·수치를 원문과 다시 대조한다."""
    body = (raw or "").strip()
    if body.startswith("```json") and body.endswith("```"):
        body = body[7:-3].strip()
    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SummaryReviewError("Hermes 후보 JSON을 읽지 못했습니다.") from exc
    rows = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SummaryReviewError("Hermes 후보 목록이 없습니다.")
    if stats is not None:
        stats.proposed = len(rows)
    source_norm = [(_normalized(line.text), line) for line in source]
    accepted: list[Proposal] = []
    approved_norm = {_normalized(item) for item in (approved or [])}
    for item in rows[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        # **모르면 더 엄격한 쪽.** form 을 빼먹었거나 모르는 값이면 `quote` 로 본다 —
        # 생성문으로 통과시키면 「제안 == 인용」 검사가 조용히 사라진다.
        form = str(item.get("form") or "").strip().lower()
        if form not in FORMS:
            form = FORM_QUOTE
        proposed = str(item.get("proposed_text") or "").strip()
        quote = str(item.get("evidence_quote") or "").strip()
        quote_norm = _normalized(quote)
        # 인용은 form 과 무관하게 필수다. 생성문이라도 근거 없이는 받지 않는다.
        if kind not in KINDS or not proposed or len(quote_norm) < 5:
            continue
        matched = next((line for text, line in source_norm if quote_norm in text), None)
        if matched is None:
            continue
        current = str(item.get("current_text") or "").strip()
        if current and _normalized(current) not in approved_norm:
            continue
        # `quote` 후보는 요약기의 창작물이 아니라 원문에서 뽑은 검토 단위다.
        # `abstract` 는 여기 하나만 면제된다 — 문장을 모델이 쓰기 때문이다.
        # 나머지 검사(인용 존재·숫자 상속·기존 승인 대조)는 그대로 지난다.
        if form == FORM_QUOTE and _normalized(proposed) != quote_norm:
            continue
        # 새로 만든 숫자는 원문 인용이나 기존 승인 문장에 실제로 있어야 한다.
        if not _numbers(proposed) <= (_numbers(quote) | _numbers(current)):
            continue
        # 숫자가 든 신규 쟁점을 모델이 `new_issue` 로 분류해도 검토 화면에서는
        # 반드시 숫자 확인 대상으로 먼저 보여 준다. 사실은 바꾸지 않고 분류만
        # 결정적으로 보정한다.
        if _numbers(proposed):
            kind = "number_or_schedule"
        accepted.append(Proposal(
            kind=kind,
            current_text=current,
            proposed_text=proposed,
            evidence_quote=quote,
            evidence_at=matched.at,
            evidence_author=matched.author,
            evidence_locator=matched.locator,
            # 좌표도 **원문에서** 온다. 모델이 준 값은 쓰지 않는다 — 출처 링크가
            # 모델 출력이면 사람이 확인하러 간 자리에 그 문장이 없을 수 있다.
            evidence_message_ts=matched.message_ts,
            evidence_hash=_evidence_hash(matched),
            form=form,
        ))
    if stats is not None:
        stats.accepted = len(accepted)
    return accepted


def source_digest(lines: list[SourceLine]) -> str:
    body = "\n".join(f"{x.locator}|{x.at}|{x.author}|{x.text}" for x in lines)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _evidence_hash(line: SourceLine) -> str:
    """승인 당시 원문 한 줄의 지문. 부분 인용이 아니라 전체 원문을 묶는다."""
    from .evidence_refs import content_hash

    return content_hash(line.at, line.author, line.text)


def prompt_input(*, approved: list[str], source: list[SourceLine]) -> str:
    old = ("\n".join(f"- {x}" for x in approved) or "(기존 승인 요약 없음)")
    old = old[:MAX_APPROVED_CHARS]
    fresh = "\n".join(f"[{x.at}] {x.author}: {x.text}" for x in source)
    fresh = fresh[:MAX_NEW_SOURCE_CHARS]
    return f"기존 승인 요약:\n{old}\n\n새 원문:\n{fresh}"


class Store:
    def __init__(self, conn):
        self.conn = conn

    def cursor(self, workspace: str, channel_id: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute("SELECT watermark FROM summary_review_cursor WHERE workspace=%s AND channel_id=%s", (workspace, channel_id))
            row = cur.fetchone()
        if not row:
            return ""
        return str(row.get("watermark") if isinstance(row, dict) else row[0])

    def rewind(self, workspace: str, channel_id: str, watermark: str) -> None:
        """커서를 지정한 지점으로 되돌린다. 빈 값이면 **아카이브 처음부터**.

        소급 검토(B-58)만 쓴다. 커서는 원래 한 방향으로만 간다 — 뒤로 돌리는 것은
        「이미 본 것으로 친 구간을 다시 본다」 는 뜻이고, 운영자가 명시적으로
        요청했을 때만 해야 한다. 후보는 `ON CONFLICT DO NOTHING` 으로 막히므로
        같은 구간을 다시 읽어도 같은 후보가 두 번 생기지는 않는다.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO summary_review_cursor(workspace,channel_id,watermark,source_digest)
                VALUES (%s,%s,%s,'') ON CONFLICT(workspace,channel_id) DO UPDATE SET
                watermark=excluded.watermark, source_digest='',
                failed_digest='', retry_after=NULL, checked_at=now()""",
                (workspace, channel_id, watermark),
            )
        self.conn.commit()

    def form_stats(self, *, since: date, workspace: str = "",
                   channel_id: str = "") -> list[dict]:
        """`form`·`state` 별 후보 건수 (B-60 실측).

        생성 요약을 허용한 값어치는 **후보 수가 줄고 승인률이 유지되는가**로만
        판단할 수 있다. 그 수치를 볼 수단이 없으면 「실측 대기」 가 영원히 풀리지
        않는다.
        """
        where = ["run_date >= %s"]
        args: list = [since]
        if workspace:
            where.append("workspace = %s")
            args.append(workspace)
        if channel_id:
            where.append("channel_id = %s")
            args.append(channel_id)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""SELECT workspace, channel_id, max(channel_name) AS channel_name,
                       form, state, count(*) AS n
                  FROM summary_review_candidate
                 WHERE {" AND ".join(where)}
                 GROUP BY workspace, channel_id, form, state
                 ORDER BY workspace, channel_id, form, state""",
                tuple(args),
            )
            return [dict(row) for row in cur.fetchall()]

    def review_latency(self, *, since: date, workspace: str = "",
                       channel_id: str = "") -> list[dict]:
        """보여 준 뒤 사람이 결정하기까지 걸린 시간 (B-60 실측).

        **자동 폐기는 빼고 센다.** 사람이 결정하지 않아 시스템이 버린 것을 섞으면
        「검토에 얼마나 걸리는가」 가 아니라 「얼마나 방치되는가」 를 재게 된다.
        """
        where = [
            "run_date >= %s",
            "delivered_at IS NOT NULL",
            "decided_at IS NOT NULL",
            "coalesce(decided_by, '') <> 'system:expired'",
        ]
        args: list = [since]
        if workspace:
            where.append("workspace = %s")
            args.append(workspace)
        if channel_id:
            where.append("channel_id = %s")
            args.append(channel_id)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""SELECT workspace, channel_id, form, count(*) AS decided,
                       avg(extract(epoch FROM (decided_at - delivered_at))) AS avg_seconds,
                       max(extract(epoch FROM (decided_at - delivered_at))) AS max_seconds
                  FROM summary_review_candidate
                 WHERE {" AND ".join(where)}
                 GROUP BY workspace, channel_id, form
                 ORDER BY workspace, channel_id, form""",
                tuple(args),
            )
            return [dict(row) for row in cur.fetchall()]

    def source_run_seen(self, workspace: str, channel_id: str, digest: str) -> bool:
        """이 원문 구간으로 **이미 요약을 돌렸는가.**

        커서만으로는 모른다. 소급은 커서를 되돌리므로 다시 실행하면 같은 구간이
        또 LLM 으로 간다 — 후보는 중복 차단에 막히지만 돈은 두 번 나간다.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM summary_review_source_run
                WHERE workspace=%s AND channel_id=%s AND source_digest=%s""",
                (workspace, channel_id, digest),
            )
            return cur.fetchone() is not None

    def advance(self, workspace: str, channel_id: str, watermark: str,
                digest: str) -> None:
        """LLM 을 부르지 않고 커서만 옮긴다. 이미 돌린 구간을 지나갈 때 쓴다."""
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO summary_review_cursor(workspace,channel_id,watermark,source_digest)
                VALUES (%s,%s,%s,%s) ON CONFLICT(workspace,channel_id) DO UPDATE SET
                watermark=excluded.watermark, source_digest=excluded.source_digest,
                checked_at=now()""",
                (workspace, channel_id, watermark, digest),
            )
        self.conn.commit()

    def mark_delivered(self, candidate_ids: list) -> None:
        """이 후보들을 검토자에게 실제로 보여 줬다고 남긴다.

        폐기는 이 기록만 보고 판단한다 — 보여 준 적 없는 후보를 폐기하면 소급으로
        쌓은 검토 대기분이 다음 회차에 통째로 사라진다.
        """
        if not candidate_ids:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE summary_review_candidate SET delivered_at=now()
                WHERE id = ANY(%s) AND delivered_at IS NULL""",
                (list(candidate_ids),),
            )
        self.conn.commit()

    def start_at(self, workspace: str, channel_id: str) -> str:
        """최초 회차는 검토자를 지정한 뒤의 원문부터 본다."""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT to_char(min(set_at) AT TIME ZONE 'Asia/Seoul',
                'YYYY-MM-DD HH24:MI') AS start_at FROM channel_reviewer
                WHERE workspace=%s AND channel_id=%s AND enabled""",
                (workspace, channel_id),
            )
            row = cur.fetchone()
        if not row:
            return ""
        return str(row.get("start_at") if isinstance(row, dict) else row[0] or "")

    def approved(self, workspace: str, channel_id: str) -> list[str]:
        """살아 있는 승인 요약. **근거가 어긋난 항목은 빼고** 돌려준다(B-56).

        근거 원문이 사라졌거나 좌표가 바뀐 항목(`stale_at`)은 더 이상 사람이
        확인한 문장이 아니다. 그대로 두면 다음 회차가 그 문장을 「기존 승인」으로
        읽고 그 위에 후보를 얹는다 — 확인할 수 없는 것 위에 쌓는 셈이다.
        길잡이와 기존 승인 목록에서 제외하며, 같은 사실이 새 원문으로 수집된 경우에만
        정상 후보 생성 경로에서 다시 검토한다.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT body FROM approved_summary_item WHERE workspace=%s AND channel_id=%s
                AND superseded_at IS NULL AND stale_at IS NULL ORDER BY approved_at""",
                (workspace, channel_id),
            )
            rows = cur.fetchall()
        return [str(r.get("body") if isinstance(r, dict) else r[0]) for r in rows]

    def may_attempt(self, workspace: str, channel_id: str, digest: str,
                    now: datetime) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT failed_digest,retry_after FROM summary_review_cursor WHERE workspace=%s AND channel_id=%s",
                (workspace, channel_id),
            )
            row = cur.fetchone()
        if not row:
            return True
        failed, retry = ((row.get("failed_digest"), row.get("retry_after"))
                         if isinstance(row, dict) else row)
        return str(failed or "") != digest or retry is None or retry <= now

    def generated_on(self, workspace: str, channel_id: str, on: date) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM summary_review_cursor WHERE workspace=%s AND channel_id=%s
                AND source_digest<>'' AND (checked_at AT TIME ZONE 'Asia/Seoul')::date=%s""",
                (workspace, channel_id, on),
            )
            return cur.fetchone() is not None

    def mark_failed(self, workspace: str, channel_id: str, digest: str,
                    now: datetime) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO summary_review_cursor(workspace,channel_id,failed_digest,retry_after)
                VALUES (%s,%s,%s,%s) ON CONFLICT(workspace,channel_id) DO UPDATE SET
                failed_digest=excluded.failed_digest,retry_after=excluded.retry_after,checked_at=now()""",
                (workspace, channel_id, digest, now + timedelta(hours=1)),
            )
        self.conn.commit()

    def save_run(self, *, workspace: str, channel_id: str, channel_name: str,
                 watermark: str, digest: str, proposals: list[Proposal], on: date) -> int:
        with self.conn.cursor() as cur:
            for proposal in proposals:
                cur.execute(
                    """INSERT INTO summary_review_candidate
                    (id,workspace,channel_id,channel_name,run_date,source_digest,kind,current_text,
                    proposed_text,evidence_quote,evidence_at,evidence_author,evidence_locator,
                     evidence_message_ts,evidence_hash,form)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (uuid.uuid4(), workspace, channel_id, channel_name, on, digest,
                     proposal.kind, proposal.current_text, proposal.proposed_text,
                     proposal.evidence_quote, proposal.evidence_at, proposal.evidence_author,
                     proposal.evidence_locator, proposal.evidence_message_ts,
                     proposal.evidence_hash, proposal.form),
                )
            cur.execute(
                """INSERT INTO summary_review_cursor(workspace,channel_id,watermark,source_digest)
                VALUES (%s,%s,%s,%s) ON CONFLICT(workspace,channel_id) DO UPDATE SET
                watermark=excluded.watermark, source_digest=excluded.source_digest,
                failed_digest='', retry_after=NULL, checked_at=now()""",
                (workspace, channel_id, watermark, digest),
            )
            # 후보가 0건이어도 남긴다. 행이 없으면 「안 돌렸다」 와 구별되지 않아
            # 잡담뿐인 구간을 누를 때마다 다시 요약한다.
            cur.execute(
                """INSERT INTO summary_review_source_run
                (workspace,channel_id,source_digest,watermark,candidates)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (workspace,channel_id,source_digest) DO UPDATE SET
                watermark=excluded.watermark, candidates=excluded.candidates,
                ran_at=now()""",
                (workspace, channel_id, digest, watermark, len(proposals)),
            )
        self.conn.commit()
        return len(proposals)

    def pending(self, workspace: str, channel_id: str, on: date) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM summary_review_candidate WHERE workspace=%s AND channel_id=%s
                AND (state='pending' OR (state='deferred' AND defer_until<=%s))
                ORDER BY created_at LIMIT %s""", (workspace, channel_id, on, MAX_CANDIDATES),
            )
            return list(cur.fetchall())

    def expire_unconfirmed(self, workspace: str, channel_id: str, on: date) -> int:
        """지난 회차에서 확인하지 않은 후보를 승인 없이 폐기한다.

        보류한 후보는 약속한 날 한 번 더 보여 주고, 그날도 결정하지 않았을 때
        다음 검토일에 폐기한다. 폐기 후보는 `approved_summary_item`에 들어가지 않는다.

        **보여 준 적 없는 후보는 폐기하지 않는다**(B-58). 한 회차 DM 은 후보를 최대
        10건만 싣는다. 하루에 11건이 생기거나 소급 검토로 수백 건이 쌓이면 나머지는
        사람 앞에 나온 적이 없는데, 예전 규칙은 `run_date` 만 보고 그것들을 버렸다.
        「확인하지 않았다」 는 보여 준 뒤에만 할 수 있는 말이다.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE summary_review_candidate SET state='expired', decided_at=now(),
                decided_by='system:expired', correction='', defer_until=NULL
                WHERE workspace=%s AND channel_id=%s AND run_date<%s
                AND delivered_at IS NOT NULL
                AND (delivered_at AT TIME ZONE 'Asia/Seoul')::date < %s
                AND (state='pending' OR (state='deferred' AND defer_until<%s))
                RETURNING id""",
                (workspace, channel_id, on, on, on),
            )
            expired = list(cur.fetchall())
            if not expired:
                self.conn.commit()
                return 0
            cur.execute(
                """SELECT DISTINCT a.id FROM summary_review_artifact a
                JOIN summary_review_artifact_candidate m ON m.artifact_id=a.id
                JOIN summary_review_candidate c ON c.id=m.candidate_id
                WHERE a.workspace=%s AND a.channel_id=%s AND c.state='expired'
                AND a.state NOT IN ('failed','ambiguous','completed','expired')""",
                (workspace, channel_id),
            )
            artifacts = list(cur.fetchall())
        self.conn.commit()
        for row in artifacts:
            key = row.get("id") if isinstance(row, dict) else row[0]
            self.refresh_artifact_state(str(key))
        return len(expired)

    # --- Canvas 회차 (B-50) ---------------------------------------------------
    def reviewer_recipients(self, workspace: str, channel_id: str) -> list[str]:
        """이 회차를 받을 사람. **검토자만.**

        채널 담당자·개설자를 자동으로 넣지 않는다(설계 §6). 담당자도 보려면
        검토자로 등록한다 — 「담당이니까 당연히」 로 권한을 넓히면, 권한이 어디서
        생겼는지 아무도 설명할 수 없게 된다.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT reviewer_user FROM channel_reviewer
                WHERE workspace=%s AND channel_id=%s AND enabled ORDER BY reviewer_user""",
                (workspace, channel_id),
            )
            rows = cur.fetchall()
        out = [
            str(r.get("reviewer_user") if isinstance(r, dict) else r[0]) for r in rows
        ]
        return [user for user in dict.fromkeys(out) if user]

    def begin_artifact(self, *, workspace: str, channel_id: str, channel_name: str,
                       review_date: date, source_digest: str, digest: str,
                       rows: list[dict]) -> tuple[str, str]:
        """회차 행을 **Canvas 보다 먼저** 잡는다. `(artifact_id, state)`.

        순서를 뒤집으면 안 된다. Slack 이 Canvas 를 만들고 응답만 유실됐을 때,
        DB 에 아무 흔적이 없으면 다음 실행이 같은 회차의 Canvas 를 또 만든다.

        이미 있는 회차면 그 행을 그대로 돌려준다 — 재실행의 멱등성이 여기 걸려
        있다. 후보 매핑도 그때 만든 것을 유지한다(번호가 바뀌면 안 된다).
        """
        artifact_id = uuid.uuid4()
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO summary_review_artifact
                (id,workspace,channel_id,channel_name,review_date,source_digest,content_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (workspace,channel_id,review_date,source_digest) DO NOTHING
                RETURNING id""",
                (artifact_id, workspace, channel_id, channel_name, review_date,
                 source_digest, digest),
            )
            created = cur.fetchone()
            if created is None:
                cur.execute(
                    """SELECT id,state FROM summary_review_artifact
                    WHERE workspace=%s AND channel_id=%s AND review_date=%s
                    AND source_digest=%s""",
                    (workspace, channel_id, review_date, source_digest),
                )
                row = cur.fetchone()
                self.conn.commit()
                if row is None:
                    raise SummaryReviewError("회차 행을 찾지 못했습니다.")
                get = row.get if isinstance(row, dict) else None
                return (str(get("id") if get else row[0]),
                        str(get("state") if get else row[1]))
            for position, candidate in enumerate(ordered_rows(rows), start=1):
                cur.execute(
                    """INSERT INTO summary_review_artifact_candidate
                    (artifact_id,candidate_id,position) VALUES (%s,%s,%s)
                    ON CONFLICT DO NOTHING""",
                    (artifact_id, candidate.get("id"), position),
                )
        self.conn.commit()
        # DB 상태와 구별되는 호출 결과다. 기존 `creating` 행은 이전 실행이 Slack
        # 호출 전후에 끊긴 것일 수 있지만, `new` 는 이 호출이 방금 만든 행이라
        # 안전하게 Canvas 생성을 시작할 수 있다.
        return str(artifact_id), "new"

    def artifact_rows(self, artifact_id: str) -> list[dict]:
        """이 회차의 후보. **저장된 번호 순서**로 돌려준다.

        `pending()` 을 다시 조회하지 않는다 — 그 사이 상태가 바뀌면 번호가 밀리고,
        사람이 "3번" 이라고 한 것이 다른 후보를 가리키게 된다.
        """
        key = _as_uuid(artifact_id)
        if key is None:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT c.*, m.position FROM summary_review_artifact_candidate m
                JOIN summary_review_candidate c ON c.id=m.candidate_id
                WHERE m.artifact_id=%s ORDER BY m.position""",
                (key,),
            )
            return list(cur.fetchall())

    def mark_artifact(self, artifact_id: str, state: str, *, canvas_id: str = "",
                      permalink: str = "", error_code: str = "") -> None:
        if state not in ARTIFACT_STATES:
            raise SummaryReviewError(f"알 수 없는 회차 상태입니다: {state}")
        key = _as_uuid(artifact_id)
        if key is None:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE summary_review_artifact SET state=%s,
                canvas_id=COALESCE(NULLIF(%s,''),canvas_id),
                canvas_permalink=COALESCE(NULLIF(%s,''),canvas_permalink),
                error_code=NULLIF(%s,''),
                ready_at=CASE WHEN %s='ready' THEN now() ELSE ready_at END
                WHERE id=%s""",
                (state, canvas_id, permalink, error_code, state, key),
            )
        self.conn.commit()

    def artifact(self, artifact_id: str) -> dict | None:
        key = _as_uuid(artifact_id)
        if key is None:
            return None
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM summary_review_artifact WHERE id=%s", (key,))
            row = cur.fetchone()
        return dict(row) if row else None

    def record_delivery(self, artifact_id: str, recipient: str, *, dm_channel: str = "",
                        message_ts: str = "", state: str = "sent",
                        error_code: str = "") -> None:
        key = _as_uuid(artifact_id)
        if key is None:
            return
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO summary_review_delivery
                (artifact_id,recipient,dm_channel,message_ts,state,sent_at,error_code)
                VALUES (%s,%s,NULLIF(%s,''),NULLIF(%s,''),%s,
                        CASE WHEN %s='sent' THEN now() END, NULLIF(%s,''))
                ON CONFLICT (artifact_id,recipient) DO UPDATE SET
                dm_channel=COALESCE(excluded.dm_channel,summary_review_delivery.dm_channel),
                message_ts=COALESCE(excluded.message_ts,summary_review_delivery.message_ts),
                state=excluded.state, sent_at=COALESCE(excluded.sent_at,summary_review_delivery.sent_at),
                error_code=excluded.error_code""",
                (key, recipient, dm_channel, message_ts, state, state, error_code),
            )
        self.conn.commit()

    def deliveries(self, artifact_id: str) -> list[dict]:
        key = _as_uuid(artifact_id)
        if key is None:
            return []
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT recipient,dm_channel,message_ts,state FROM summary_review_delivery
                WHERE artifact_id=%s AND state='sent' ORDER BY recipient""",
                (key,),
            )
            return list(cur.fetchall())

    def delivery_sent(self, artifact_id: str, recipient: str) -> bool:
        """이 수신자에게 **이 회차** DM을 보냈는가."""
        key = _as_uuid(artifact_id)
        if key is None or not recipient:
            return False
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM summary_review_delivery
                WHERE artifact_id=%s AND recipient=%s AND state='sent'""",
                (key, recipient),
            )
            return cur.fetchone() is not None

    def may_decide(self, artifact_id: str, recipient: str, workspace: str) -> bool:
        """이 사람이 **이 회차를** 받았는가.

        날짜 단위 발송 이력이 아니라 회차 단위로 본다. 이력은 「그날 무언가를
        받았다」 만 알고 어느 Canvas 였는지 모른다 — 다른 회차의 권한이다.
        """
        key = _as_uuid(artifact_id)
        if key is None or not recipient:
            return False
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM summary_review_delivery d
                JOIN summary_review_artifact a ON a.id=d.artifact_id
                WHERE d.artifact_id=%s AND d.recipient=%s AND d.state='sent'
                AND a.workspace=%s""",
                (key, recipient, workspace),
            )
            return cur.fetchone() is not None

    def refresh_artifact_state(self, artifact_id: str) -> str:
        """후보 집계로 회차 상태를 다시 계산한다.

        **하나라도 `deferred` 면 완료가 아니다.** 보류는 결정이 아니라 미룬 것이다.
        """
        key = _as_uuid(artifact_id)
        if key is None:
            return ""
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS total,
                count(*) FILTER (WHERE c.state IN ('approved','rejected')) AS decided,
                count(*) FILTER (WHERE c.state='deferred') AS deferred,
                count(*) FILTER (WHERE c.state='expired') AS expired
                FROM summary_review_artifact_candidate m
                JOIN summary_review_candidate c ON c.id=m.candidate_id
                WHERE m.artifact_id=%s""",
                (key,),
            )
            row = cur.fetchone() or {}
            get = row.get if isinstance(row, dict) else None
            total = int((get("total") if get else row[0]) or 0)
            decided = int((get("decided") if get else row[1]) or 0)
            deferred = int((get("deferred") if get else row[2]) or 0)
            expired = int((get("expired") if get else row[3]) or 0)
            if total and expired == total:
                state = "expired"
            elif total and decided + expired == total and not deferred:
                state = "completed"
            elif not total or (not decided and not expired):
                state = "ready"
            else:
                state = "partial"
            cur.execute(
                """UPDATE summary_review_artifact SET state=%s,
                decided_at=CASE WHEN %s IN ('completed','expired') THEN now()
                ELSE decided_at END
                WHERE id=%s AND state NOT IN ('failed','ambiguous')""",
                (state, state, key),
            )
        self.conn.commit()
        return state

    def approve_all(
        self, artifact_id: str, *, workspace: str, actor: str
    ) -> tuple[list[str], int]:
        """아직 결정되지 않은 후보만 승인. `(승인한 후보 ID, 건너뜀)`.

        **이미 반려된 후보를 덮어쓰지 않는다.** 다른 검토자가 먼저 "틀리다" 라고
        한 것을 "전체 맞다" 한 번으로 뒤집으면, 그 사람이 적은 정정사항이 아무
        효력 없이 남는다.
        """
        approved: list[str] = []
        skipped = 0
        for row in self.artifact_rows(artifact_id):
            if str(row.get("state") or "") not in OPEN_STATES:
                skipped += 1
                continue
            if self.decide(str(row.get("id")), workspace=workspace, actor=actor,
                           decision="approved", artifact_id=artifact_id):
                approved.append(str(row.get("id")))
            else:
                skipped += 1
        return approved, skipped

    def candidate_channel(self, candidate_id: str, workspace: str) -> str:
        try:
            key = uuid.UUID(candidate_id)
        except ValueError:
            return ""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT channel_id FROM summary_review_candidate WHERE id=%s AND workspace=%s",
                (key, workspace),
            )
            row = cur.fetchone()
        return str((row.get("channel_id") if isinstance(row, dict) else row[0]) if row else "")

    def decide(self, candidate_id: str, *, workspace: str, actor: str, decision: str,
               correction: str = "", defer_until: date | None = None,
               artifact_id: str = "") -> bool:
        """후보 하나를 결정한다. 이미 누가 결정했으면 `False`.

        **조건부 UPDATE 한 문장이 잠금이다.** `state IN ('pending','deferred')` 가
        WHERE 에 있으므로, 두 검토자가 동시에 눌러도 행 잠금을 먼저 잡은 쪽만
        바꾸고 나머지는 0행을 받는다. 읽고 나서 쓰면 그 사이가 벌어진다.

        권한은 `artifact_id` 가 있으면 **그 회차를 받았는지**로 본다(설계 §4.3).
        없으면 예전 날짜 단위 발송 이력으로 본다 — Canvas 실패 폴백 DM 과 이미
        보낸 옛 DM 이 그 형식이다.
        """
        if decision not in {"approved", "rejected", "deferred"}:
            raise SummaryReviewError("지원하지 않는 검토 결정입니다.")
        try:
            key = uuid.UUID(candidate_id)
        except ValueError as exc:
            raise SummaryReviewError("잘못된 요약 후보 식별자입니다.") from exc
        if decision == "rejected" and len(correction.strip()) < MIN_CORRECTION:
            raise SummaryReviewError("반려할 때는 정정 사항을 입력해야 합니다.")
        artifact_key = _as_uuid(artifact_id) if artifact_id else None
        if artifact_id and artifact_key is None:
            raise SummaryReviewError("잘못된 요약 검토 회차 식별자입니다.")
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE summary_review_candidate SET state=%s, decided_at=CASE WHEN %s='deferred' THEN NULL ELSE now() END,
                decided_by=%s, correction=%s, defer_until=%s WHERE id=%s AND workspace=%s
                AND state IN ('pending','deferred') AND (
                    (%s::uuid IS NOT NULL AND EXISTS (
                        SELECT 1 FROM summary_review_delivery d
                        JOIN summary_review_artifact_candidate m
                          ON m.artifact_id=d.artifact_id
                        WHERE d.artifact_id=%s AND d.recipient=%s AND d.state='sent'
                        AND m.candidate_id=summary_review_candidate.id
                    ))
                    OR (%s::uuid IS NULL AND EXISTS (
                        SELECT 1 FROM review_digest_sent sent
                        WHERE sent.workspace=summary_review_candidate.workspace
                        AND sent.channel_id=summary_review_candidate.channel_id
                        AND sent.recipient=%s AND sent.kind='summary'
                    ))
                ) RETURNING *""",
                (decision, decision, actor, correction.strip(), defer_until, key, workspace,
                 artifact_key, artifact_key, actor, artifact_key, actor),
            )
            row = cur.fetchone()
            if row and decision == "approved":
                get = row.get if isinstance(row, dict) else None
                values = (
                    get("id"), get("workspace"), get("channel_id"), get("kind"), get("proposed_text"), actor
                ) if get else (row[0], row[1], row[2], row[6], row[8], actor)
                current = str(get("current_text") if get else row[7])
                if current:
                    cur.execute(
                        """UPDATE approved_summary_item SET superseded_at=now(), superseded_by=%s
                        WHERE workspace=%s AND channel_id=%s AND body=%s
                        AND superseded_at IS NULL""",
                        (values[0], values[1], values[2], current),
                    )
                cur.execute(
                    """INSERT INTO approved_summary_item(candidate_id,workspace,channel_id,kind,body,approved_by)
                    VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""", values,
                )
        self.conn.commit()
        return row is not None


def candidate_blocks(channel_name: str, rows: list[dict]) -> list[dict]:
    numeric_count = sum(
        str(row.get("kind") or "") == "number_or_schedule" for row in rows
    )
    heading = (
        f"*{channel_name} - 오늘 수집 내용 요약 검토 {len(rows)}건*\n"
        f"숫자·금액·비율·날짜 포함 {numeric_count}건 · 일반 핵심 내용 "
        f"{len(rows) - numeric_count}건\n"
        "특히 숫자는 후보와 근거 원문이 한 자리까지 같은지 확인해 주세요."
    )
    out = [{"type": "section", "text": {"type": "mrkdwn", "text": heading}}]
    labels = {"number_or_schedule": "숫자·일정", "new_issue": "새 쟁점", "closed_issue": "끝난 쟁점"}
    ordered = sorted(
        rows,
        key=lambda row: str(row.get("kind") or "") != "number_or_schedule",
    )
    for row in ordered:
        get = row.get
        cid = str(get("id"))
        body = (
            f"*{labels.get(str(get('kind')), '요약')}{form_note(row)}*\n"
            f"*후보* {get('proposed_text')}\n"
            f"*근거* {get('evidence_author')} · {get('evidence_at')}\n"
            f">{get('evidence_quote')}"
        )
        values = _number_values(str(get("proposed_text") or ""))
        if values:
            body = f"*확인할 값* `{'` · `'.join(values)}`\n" + body
        if get("current_text"):
            body = f"*현재* {get('current_text')}\n" + body
        out.extend([
            {"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}},
            {"type": "actions", "elements": [
                {"type": "button", "action_id": ACTION_APPROVE, "text": {"type": "plain_text", "text": "맞다"}, "style": "primary", "value": cid},
                {"type": "button", "action_id": ACTION_REJECT, "text": {"type": "plain_text", "text": "틀렸다"}, "style": "danger", "value": cid},
                {"type": "button", "action_id": ACTION_DEFER, "text": {"type": "plain_text", "text": "나중에"}, "value": cid},
            ]},
        ])
    out.append({"type": "context", "elements": [{"type": "mrkdwn", "text":
        "응답하지 않은 항목은 다음 검토일에 폐기됩니다. 승인 요약은 검색 길잡이이며 "
        "답변 출처는 연결된 원문입니다."}]})
    return out


# --- Canvas 렌더 (설계 §2.1) ---------------------------------------------------
#
# **LLM 을 다시 부르지 않는다.** 예상 요약본은 지금 승인된 항목과 이번 후보를
# 결정적 코드가 조합해 만든다. 여기서 모델을 부르면 사람이 검토하려는 문장이
# 검토 화면에서 또 바뀐다 — 무엇을 승인한 것인지 알 수 없게 된다.
KIND_LABELS = {
    "number_or_schedule": "숫자·일정",
    "new_issue": "새 쟁점",
    "closed_issue": "끝난 쟁점",
}


def ordered_rows(rows: list[dict]) -> list[dict]:
    """Canvas·DM 에 보일 순서. **숫자 후보가 먼저다.**

    숫자는 한 자리만 틀려도 답이 바뀌는데, 목록 아래쪽에 있으면 끝까지 안 보고
    닫는다. 같은 종류 안에서는 생성 순서를 지킨다 — 매번 순서가 바뀌면 사람이
    어제 본 것과 대조할 수 없다.
    """
    return sorted(rows, key=lambda row: str(row.get("kind") or "") != "number_or_schedule")


def projected_summary(approved: list[str], rows: list[dict]) -> list[str]:
    """이번 후보를 **가상 적용한** 전체 요약. 아직 승인된 문서가 아니다.

    - `current_text` 가 있는 후보: 그 문장을 `proposed_text` 로 교체
    - `current_text` 가 없는 후보: 끝에 추가
    - `closed_issue`: 기존 문장을 종결 문장으로 교체하되 **임의로 지우지 않는다**

    같은 `current_text` 가 두 번 나오거나 아예 없으면 `AmbiguousProjection`.
    """
    out = list(approved)
    for row in ordered_rows(rows):
        current = str(row.get("current_text") or "").strip()
        proposed = str(row.get("proposed_text") or "").strip()
        if not proposed:
            continue
        if not current:
            out.append(proposed)
            continue
        hits = [i for i, body in enumerate(out) if body.strip() == current]
        if len(hits) != 1:
            raise AmbiguousProjection(
                f"바꿀 기존 문장을 {len(hits)}개 찾았습니다(1개여야 합니다)."
            )
        out[hits[0]] = proposed
    return out


def content_hash(approved: list[str], rows: list[dict]) -> str:
    """이 회차의 지문. **렌더러 버전과 후보 순서까지** 넣는다.

    후보 내용이 같아도 순서가 다르면 다른 문서다 — 사람이 "3번" 이라고 부르는
    것이 달라지기 때문이다.
    """
    payload = {
        "renderer": RENDERER_VERSION,
        "approved": list(approved),
        "candidates": [
            [str(row.get("id")), str(row.get("kind") or ""),
             str(row.get("current_text") or ""), str(row.get("proposed_text") or "")]
            for row in ordered_rows(rows)
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def canvas_title(*, channel_label: str, review_date: date, artifact_id: str) -> str:
    """`2026-09-16 전산팀장보고 요약 검토 [a1b2c3d4]`.

    Artifact ID 앞 8자를 넣는다 — 같은 채널·같은 날 회차가 둘이 되는 상황(다른
    source digest)에서 사람이 어느 것을 보는지 구별할 수 있어야 한다.
    접미사 ` · TYBot` 은 `canvas_answer` 가 붙인다(수집 제외 표식).
    """
    short = str(artifact_id).replace("-", "")[:8]
    return f"{review_date} {channel_label} 요약 검토 [{short}]"


def _numbers_table(rows: list[dict]) -> list[str]:
    """숫자·금액·비율·날짜 확인표. **환산하거나 정밀도를 올리지 않는다.**"""
    out = [
        "| 번호 | 구분 | 확인할 값 | 요약 후보 | 근거 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for index, row in enumerate(ordered_rows(rows), start=1):
        values = _number_values(str(row.get("proposed_text") or ""))
        if not values:
            continue
        out.append(
            f"| {index} | {KIND_LABELS.get(str(row.get('kind')), '요약')}"
            f"{form_note(row)} "
            f"| {', '.join(values)} | {_cell(row.get('proposed_text'))} "
            f"| {_cell(row.get('evidence_author'))} · {_cell(row.get('evidence_at'))} |"
        )
    return out if len(out) > 2 else []


def form_of(row) -> str:
    """이 후보의 형식. 모르면 `quote` — 더 엄격한 쪽으로 읽는다."""
    value = str(row.get("form") or FORM_QUOTE)
    return value if value in FORMS else FORM_QUOTE


def form_note(row) -> str:
    """검토자에게 붙이는 한마디. **생성 문장은 그렇다고 말한다.**

    원문 그대로인 후보와 모델이 쓴 문장은 확인하는 방법이 다르다. 표시가 없으면
    검토자는 둘을 같게 읽고, 생성 문장을 「원문에 그렇게 적혀 있다」 로 믿는다.
    """
    return " · 정리 문장" if form_of(row) == FORM_ABSTRACT else ""


def _cell(value) -> str:
    """표 한 칸. 줄바꿈과 `|` 만 지운다 — **내용은 고치지 않는다.**"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text.replace("|", r"\|")


def message_link(channel_id: str, message_ts: str) -> str:
    """근거 메시지 한 건을 여는 링크. 좌표가 없으면 빈 문자열.

    좌표가 없을 때 채널 링크를 **여기서** 대신 돌려주지 않는다. 호출부가 두 링크를
    다른 문구로 보여야 한다 — 「이 메시지」 라고 적힌 링크가 채널 맨 위를 열면
    사람은 근거를 못 찾고도 찾았다고 생각한다.
    """
    ts = str(message_ts or "").strip()
    if not channel_id or not ts:
        return ""
    return f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def canvas_markdown(
    *, channel_label: str, review_date: date, rows: list[dict], approved: list[str],
    channel_id: str = "",
) -> str:
    """검토용 Canvas 본문. Disclaimer 는 `canvas_answer.markdown()` 이 붙인다.

    **후보에 없는 설명·평가·결론을 만들지 않는다.** 여기서 문장을 지어내면 사람이
    승인한 것과 문서에 적힌 것이 갈린다.
    """
    ordered = ordered_rows(rows)
    numeric = sum(str(row.get("kind") or "") == "number_or_schedule" for row in ordered)
    parts = [
        f"## {channel_label} · {review_date} 일일 요약 검토",
        f"요약 항목 {len(ordered)}건 — 숫자·일정 포함 {numeric}건 · 일반 핵심 내용 {len(ordered) - numeric}건",
        "",
        "## 오늘 수집 내용 요약 (검토 전)",
        "",
        "> 오늘 수집된 사람의 채팅과 변환 완료 첨부에서 뽑은 핵심 내용입니다. 아직"
        " 승인된 문서가 아니며, DM에서 승인한 항목만 파생 요약에 반영됩니다.",
        "",
    ]
    parts += [f"- {_cell(row.get('proposed_text'))}" for row in ordered] or ["- (없음)"]

    projected = projected_summary(approved, ordered)
    if approved:
        parts += ["", "## 승인 반영 후 누적 요약 (예상)", ""]
        parts += [f"- {line}" for line in projected]

    table = _numbers_table(ordered)
    if table:
        parts += ["", "## 숫자·날짜 확인", ""]
        parts += table

    rest = [row for row in ordered if not _number_values(str(row.get("proposed_text") or ""))]
    if rest:
        parts += ["", "## 나머지 쟁점", ""]
        parts += [
            f"{index}. {_cell(row.get('proposed_text'))}"
            for index, row in enumerate(ordered, start=1)
            if row in rest
        ]

    parts += ["", "## 항목별 근거 원문", ""]
    source_url = f"https://slack.com/archives/{channel_id}" if channel_id else ""
    if source_url:
        parts += [f"[Slack 채널 원문 열기]({source_url})", ""]
    for index, row in enumerate(ordered, start=1):
        parts.append(
            f"### {index}. {KIND_LABELS.get(str(row.get('kind')), '요약')}{form_note(row)}"
        )
        if form_of(row) == FORM_ABSTRACT:
            # 인용은 바로 아래에 그대로 붙는다. 두 줄을 나란히 읽고 판단한다.
            parts.append(
                "- 이 문장은 **원문 그대로가 아니라** 아래 인용을 정리한 것입니다."
                " 숫자·날짜가 인용과 같은지 먼저 확인하세요."
            )
        if row.get("current_text"):
            parts.append(f"- 현재: {_cell(row.get('current_text'))}")
        parts.append(f"- 후보: {_cell(row.get('proposed_text'))}")
        parts.append(
            f"- 근거: {_cell(row.get('evidence_author'))} · {_cell(row.get('evidence_at'))}"
        )
        # 후보마다 **그 근거 메시지**를 연다. 채널 링크만 주면 사람은 그날 대화를
        # 처음부터 뒤져야 하고, 실제로는 확인하지 않은 채 승인하게 된다(B-56).
        direct = message_link(channel_id, str(row.get("evidence_message_ts") or ""))
        if direct:
            parts.append(f"- 출처 링크: [이 근거 메시지 열기]({direct})")
        elif source_url:
            parts.append(f"- 출처 링크: [채널에서 원문 확인]({source_url})")
        # **검증을 통과한 인용만** 싣는다. 첨부에서 뽑은 문장이라도 XML·OCR 덤프를
        # 그대로 옮기지 않는다 — 그건 근거가 아니라 원본의 사본이다.
        parts.append(f"> {_cell(row.get('evidence_quote'))}")
        parts.append("")

    parts += [
        "## 검토 방법",
        "",
        "- 이 문서는 읽기용입니다. 결정은 TYBot DM 의 버튼에서 합니다.",
        "- 후보 하나가 부분적으로만 맞아도 **틀리다** 로 처리하고 올바른 전체 문장을 적어 주세요.",
        "- 응답하지 않은 항목은 다음 검토일에 폐기되며 승인 요약에 반영되지 않습니다.",
        "- 승인 요약은 검색 길잡이로만 사용하며 실제 답변은 연결된 원문을 다시 확인합니다.",
    ]
    return "\n".join(parts)


def canvas_review_blocks(
    *, channel_label: str, review_date: date, permalink: str,
    artifact_id: str, rows: list[dict],
) -> list[dict]:
    """DM 제어 화면. **후보·근거 원문을 다시 복제하지 않는다.**

    긴 내용은 Canvas 에 있다. 여기 또 실으면 두 화면이 어긋날 때 어느 쪽이 맞는지
    알 수 없고, DM 이 길어져 버튼이 화면 밖으로 밀린다.
    """
    ordered = ordered_rows(rows)
    numeric = sum(str(row.get("kind") or "") == "number_or_schedule" for row in ordered)
    head = (
        f"*{channel_label} · {review_date} 오늘 수집 내용 요약 {len(ordered)}건*\n"
        f"숫자 포함 {numeric}건 · 일반 핵심 내용 {len(ordered) - numeric}건\n"
        "Canvas에서 요약과 출처를 읽고 항목별로 확인해 주세요."
    )
    out: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": head}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "tybot_summary_review_open_canvas",
             "text": {"type": "plain_text", "text": "Canvas에서 전체 요약과 근거 읽기"},
             "url": permalink, "value": artifact_id},
            {"type": "button", "action_id": ACTION_APPROVE_ALL,
             "text": {"type": "plain_text", "text": "전체 맞다"},
             "style": "primary", "value": artifact_id},
        ]},
    ]
    for index, row in enumerate(ordered, start=1):
        value = _button_value(artifact_id, str(row.get("id")))
        out.extend([
            {"type": "section", "text": {"type": "mrkdwn",
                                         "text": _dm_headline(index, row)}},
            {"type": "actions", "block_id": f"decide_{index}", "elements": [
                {"type": "button", "action_id": ACTION_APPROVE, "value": value,
                 "text": {"type": "plain_text", "text": "맞다"}, "style": "primary"},
                {"type": "button", "action_id": ACTION_REJECT, "value": value,
                 "text": {"type": "plain_text", "text": "틀리다"}, "style": "danger"},
                {"type": "button", "action_id": ACTION_DEFER, "value": value,
                 "text": {"type": "plain_text", "text": "나중에"}},
            ]},
        ])
    out.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "미응답 항목은 다음 검토일에 폐기됩니다. 틀리면 정정사항을 입력해 주세요."}]})
    return out


def _dm_headline(index: int, row: dict) -> str:
    """DM 한 줄. 값이 있으면 값만, 없으면 문장 앞머리만 보인다."""
    label = KIND_LABELS.get(str(row.get("kind")), "요약")
    values = _number_values(str(row.get("proposed_text") or ""))
    tail = " · ".join(f"`{v}`" for v in values) if values else _cell(row.get("proposed_text"))[:80]
    return f"*{index}. {label}* · {tail}"


DECISION_LABELS = {
    "approved": "맞음",
    "rejected": "수정 필요",
    "deferred": "내일 다시 확인",
    "expired": "미응답 폐기",
}


def _with_decisions(blocks: list[dict], rows: list[dict]) -> list[dict]:
    """결정된 후보의 버튼을 상태 줄로 바꾼다(설계 §8).

    **정정 본문은 넣지 않는다.** 누가 무엇으로 결정했는지만 보인다 — 정정은 그
    사람의 판단이고, 다른 검토자에게 퍼뜨리면 다음 판단이 그 문장에 끌린다.
    """
    decided = {
        f"decide_{index}": row
        for index, row in enumerate(ordered_rows(rows), start=1)
        if str(row.get("state") or "") not in ("", "pending")
    }
    out: list[dict] = []
    for block in blocks:
        row = decided.get(str(block.get("block_id") or ""))
        if block.get("type") != "actions" or row is None:
            out.append(block)
            continue
        state = str(row.get("state") or "")
        who = str(row.get("decided_by") or "")
        label = DECISION_LABELS.get(state, state)
        tail = f" · <@{who}>" if who and state != "deferred" else ""
        out.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"{label}{tail}"}
        ]})
    return out


def _button_value(artifact_id: str, candidate_id: str) -> str:
    """버튼이 들고 다니는 좌표. **회차와 후보를 함께** 실어야 권한을 볼 수 있다.

    후보 ID 만 실으면 「이 사람이 이 회차를 받았는가」 를 확인할 수 없고, 날짜
    단위 발송 이력으로 대신 보게 된다 — 그건 다른 회차의 권한이다.
    """
    return json.dumps({"a": artifact_id, "c": candidate_id}, ensure_ascii=False)


def parse_button_value(raw: str) -> tuple[str, str]:
    """버튼 값 → `(artifact_id, candidate_id)`.

    옛 형식(후보 ID 문자열)도 받는다 — Canvas 실패 폴백 DM 이 그 형식을 쓰고,
    이미 보낸 DM 도 남아 있다.
    """
    text = str(raw or "").strip()
    if not text.startswith("{"):
        return "", text
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return "", ""
    return str(data.get("a") or ""), str(data.get("c") or "")


def reject_modal(
    candidate_id: str, *, artifact_id: str = "", position: int = 0, headline: str = ""
) -> dict:
    """반려 모달. `private_metadata` 는 **JSON 이다.**

    예전에는 후보 ID 문자열 하나였다. 회차까지 실어야 권한을 볼 수 있는데, 문자열을
    이어 붙이면 구분자가 값 안에 들어갔을 때 조용히 잘못 갈린다.
    """
    blocks: list[dict] = []
    if headline:
        # 무엇을 반려하는지 보이지 않으면 사람은 다른 후보에 정정을 적는다.
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": headline[:2900]}})
    blocks += [
        {"type": "input", "block_id": "wrong_part",
         "label": {"type": "plain_text", "text": "틀린 부분"},
         "element": {"type": "static_select", "action_id": "wrong_part",
                     "placeholder": {"type": "plain_text", "text": "고르세요"},
                     "options": [
                         {"text": {"type": "plain_text", "text": label}, "value": code}
                         for code, label in WRONG_PARTS
                     ]}},
        {"type": "input", "block_id": "correction",
         "label": {"type": "plain_text", "text": "올바른 내용/정정사항"},
         "element": {"type": "plain_text_input", "action_id": "correction",
                     "multiline": True, "min_length": MIN_CORRECTION,
                     "max_length": MAX_CORRECTION}},
    ]
    return {"type": "modal", "callback_id": REJECT_CALLBACK,
            "private_metadata": json.dumps(
                {"artifact_id": artifact_id, "candidate_id": candidate_id,
                 "position": position}, ensure_ascii=False),
            "title": {"type": "plain_text", "text": "요약 후보 반려"},
            "submit": {"type": "plain_text", "text": "반려"},
            "close": {"type": "plain_text", "text": "취소"},
            "blocks": blocks}


def reject_metadata(view: dict) -> dict:
    """모달의 `private_metadata`. 옛 형식(후보 ID 문자열)도 읽는다."""
    raw = str(view.get("private_metadata") or "").strip()
    if not raw.startswith("{"):
        return {"artifact_id": "", "candidate_id": raw, "position": 0}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"artifact_id": "", "candidate_id": "", "position": 0}
    return {
        "artifact_id": str(data.get("artifact_id") or ""),
        "candidate_id": str(data.get("candidate_id") or ""),
        "position": int(data.get("position") or 0),
    }


def correction_from_view(view: dict) -> str:
    return str(((view.get("state") or {}).get("values") or {}).get("correction", {}).get("correction", {}).get("value") or "").strip()


def wrong_part_from_view(view: dict) -> str:
    """고른 「틀린 부분」 코드. 안 골랐으면 빈 문자열이고, 그건 제출 거절 사유다."""
    block = ((view.get("state") or {}).get("values") or {}).get("wrong_part") or {}
    selected = (block.get("wrong_part") or {}).get("selected_option") or {}
    code = str(selected.get("value") or "")
    return code if code in {c for c, _ in WRONG_PARTS} else ""


def contract_prompt() -> str:
    path = Path(__file__).resolve().parents[2] / "subbots" / "hermes" / "contract" / "summary-review.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SummaryReviewError("Hermes 요약 검토 계약을 읽지 못했습니다.") from exc


def default_defer_date(today: date) -> date:
    return today + timedelta(days=1)


def _source_rows(archive, workspace: str, channel_id: str, watermark: str,
                 start_at: str = "") -> list[SourceLine]:
    """커서 이후의 원문 **전부**. 한 회차 분량으로 자르지 않는다.

    소급 대상이 얼마나 되는지 세려면 자르기 전 목록이 필요하다 — 잘린 목록으로
    세면 몇 달치가 늘 「한 회차」 로 보인다.
    """
    from .channel_lifecycle import include_retired, keep

    allow_retired = include_retired()
    rows: list[SourceLine] = []
    for doc in archive.docs():
        if doc.workspace != workspace or str(doc.channel_id or "") != channel_id:
            continue
        # 보관·삭제된 채널의 원문은 **요약 후보를 만들지 않는다**(B-51).
        # 없앤 채널의 이야기가 오늘 요약에 들어가면 검토자는 출처를 확인할 수 없다.
        if not keep(doc, allow_retired=allow_retired):
            continue
        for line in doc.raw_lines:
            source = line.source_path or doc.path
            locator = f"{source.name}:{line.lineno}"
            key = f"{line.ts}|{locator}"
            if watermark and key <= watermark:
                continue
            if not watermark and start_at and line.ts < start_at[:16]:
                continue
            if "tybot" in line.speaker.casefold():
                continue
            rows.append(SourceLine(
                line.ts, line.speaker, line.text, locator,
                message_ts=str(getattr(line, "message_ts", "") or ""),
            ))
    rows.sort(key=lambda item: (item.at, item.locator))
    return rows


def _line_size(item: SourceLine) -> int:
    return len(item.at) + len(item.author) + len(item.text) + 8


def channel_source(archive, workspace: str, channel_id: str, watermark: str,
                   start_at: str = "") -> list[SourceLine]:
    """한 채널의 새 원문 **한 회차분**. 승인 요약과 봇 출력은 읽지 않는다."""
    selected: list[SourceLine] = []
    used = 0
    for item in _source_rows(archive, workspace, channel_id, watermark, start_at):
        size = _line_size(item)
        if selected and used + size > MAX_NEW_SOURCE_CHARS:
            break
        selected.append(item)
        used += size
    return selected


# --- 소급 검토 (B-58) ---------------------------------------------------------
# 수집은 됐는데 검토 후보가 한 번도 만들어지지 않은 구간이 있다. 최초 회차가
# 「검토자 지정 시각 이후」와 「최근 하루」로 두 번 좁혀지고, 그 뒤로는 커서가
# 앞으로만 가기 때문이다. 과거 전체 수집으로 넣은 몇 달치는 그 셋에 모두 걸린다.
#
# 소급은 커서를 뒤로 돌려 같은 생성 경로를 다시 태우는 것이다. **새 경로를 만들지
# 않는다** — 후보 계약·원문 대조·승인 절차가 갈라지면 어느 쪽이 맞는지 알 수 없다.

# 아카이브 처음을 뜻하는 커서 값. 빈 문자열은 「커서 없음」이라 최초 회차 규칙
# (최근 하루)이 다시 걸리므로 쓸 수 없다.
EPOCH_WATERMARK = "0000-00-00 00:00|"

# 한 번의 소급이 도는 최대 회차. 회차마다 LLM 을 한 번 부른다 — 무제한이면
# 채널 하나가 일일 비용 한도를 다 쓴다.
DEFAULT_BACKFILL_ROUNDS = 10
MAX_BACKFILL_ROUNDS = 50


def backfill_watermark(since: str = "") -> str:
    """소급 시작점을 커서 값으로 옮긴다. 시작일은 **포함**한다."""
    since = (since or "").strip()
    if not since:
        return EPOCH_WATERMARK
    return f"{since[:10]} 00:00|"


@dataclass(frozen=True)
class BackfillEstimate:
    """LLM 을 부르지 않고 센 소급 대상. 비용을 보고 누르라고 있는 값이다."""

    lines: int = 0
    characters: int = 0
    rounds: int = 0
    first_at: str = ""
    last_at: str = ""


def backfill_estimate(archive, *, workspace: str, channel_id: str,
                      since: str = "") -> BackfillEstimate:
    rows = _source_rows(archive, workspace, channel_id, backfill_watermark(since))
    if not rows:
        return BackfillEstimate()
    characters = sum(_line_size(item) for item in rows)
    rounds = max(1, -(-characters // MAX_NEW_SOURCE_CHARS))
    return BackfillEstimate(
        lines=len(rows), characters=characters, rounds=rounds,
        first_at=rows[0].at, last_at=rows[-1].at,
    )


@dataclass
class BackfillResult:
    rounds: int = 0
    candidates: int = 0
    # 원문을 끝까지 읽었는가. 거짓이면 회차 상한에 걸린 것이고, 다시 누르면
    # **멈춘 지점부터** 이어 간다(커서가 그대로 남는다).
    exhausted: bool = False


def backfill_channel(store: Store, archive, *, workspace: str, channel_id: str,
                     channel_name: str, complete, now: datetime,
                     since: str = "", max_rounds: int = DEFAULT_BACKFILL_ROUNDS,
                     resume: bool = False) -> BackfillResult:
    """수집된 과거 원문으로 검토 후보를 만든다.

    `resume` 이면 커서를 그대로 두고 멈춘 지점부터 이어 간다. 아니면 `since` 로
    되돌린다 — 되돌리는 것은 「이미 본 것으로 친 구간을 다시 본다」 는 뜻이라
    운영자가 명시적으로 요청했을 때만 한다.

    후보는 `(워크스페이스, 채널, 원문 지문, 종류, 제안 문장)` 으로 유일하므로 같은
    구간을 다시 읽어도 같은 후보가 두 번 생기지 않는다.
    """
    rounds = max(1, min(int(max_rounds or 0) or DEFAULT_BACKFILL_ROUNDS,
                        MAX_BACKFILL_ROUNDS))
    if not resume:
        store.rewind(workspace, channel_id, backfill_watermark(since))
    result = BackfillResult()
    for _ in range(rounds):
        before = store.cursor(workspace, channel_id)
        result.candidates += generate_channel(
            store, archive, workspace=workspace, channel_id=channel_id,
            channel_name=channel_name, complete=complete, now=now, force=True,
        )
        result.rounds += 1
        if store.cursor(workspace, channel_id) == before:
            # 커서가 안 움직였다 = 읽을 원문이 없다. 회차를 더 돌아도 같다.
            result.exhausted = True
            break
    return result


def generate_channel(store: Store, archive, *, workspace: str, channel_id: str,
                     channel_name: str, complete, now: datetime,
                     force: bool = False,
                     stats: GenerateStats | None = None) -> int:
    watermark = store.cursor(workspace, channel_id)
    configured_at = store.start_at(workspace, channel_id)
    if not watermark:
        recent = (now.astimezone(KST) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        configured_at = max(configured_at, recent)
    source = channel_source(
        archive, workspace, channel_id, watermark,
        start_at=configured_at,
    )
    if not source:
        return 0
    if stats is not None:
        stats.lines = len(source)
    digest = source_digest(source)
    watermark_after = f"{source[-1].at}|{source[-1].locator}"
    if store.source_run_seen(workspace, channel_id, digest):
        # 같은 원문을 두 번 요약하지 않는다. 후보는 이미 DB 에 있고 중복 차단에
        # 막히지만 **돈은 다시 나간다**(B-59). 커서만 옮겨 다음 구간으로 간다.
        log.info(
            "이미 요약한 구간이라 건너뛴다 ws=%s ch=%s 줄=%d",
            workspace, channel_id, len(source),
        )
        if stats is not None:
            stats.reused = True
        store.advance(workspace, channel_id, watermark_after, digest)
        return 0
    # 예약 실행은 같은 실패 입력을 한 시간 뒤에 재시도한다. 콘솔에서 사람이 직접
    # 누른 즉시 실행은 그 백오프도 우회한다 — 장애를 고친 직후 확인하려고 누른
    # 버튼이 이전 실패 시각 때문에 아무 일도 하지 않으면 원인을 다시 숨긴다.
    if not force and not store.may_attempt(workspace, channel_id, digest, now):
        return 0
    try:
        approved = store.approved(workspace, channel_id)
        raw = complete(
            contract_prompt(),
            prompt_input(approved=approved, source=source),
            workspace,
        )
        proposals = parse_proposals(raw, source, approved, stats=stats)
    except Exception:
        with contextlib.suppress(Exception):
            store.conn.rollback()
        with contextlib.suppress(Exception):
            store.mark_failed(workspace, channel_id, digest, now)
        raise
    return store.save_run(
        workspace=workspace, channel_id=channel_id, channel_name=channel_name,
        watermark=watermark_after, digest=digest, proposals=proposals,
        on=now.astimezone(KST).date(),
    )


def _already_sent(conn, *, workspace: str, channel_id: str,
                  recipient: str, on: date) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM review_digest_sent WHERE workspace=%s AND channel_id=%s
            AND recipient=%s AND digest_date=%s AND kind='summary'""",
            (workspace, channel_id, recipient, on),
        )
        return cur.fetchone() is not None


def _mark_sent(conn, *, workspace: str, channel_id: str, recipient: str,
               on: date, count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO review_digest_sent
            (workspace,channel_id,recipient,digest_date,kind,item_count)
            VALUES (%s,%s,%s,%s,'summary',%s) ON CONFLICT DO NOTHING""",
            (workspace, channel_id, recipient, on, count),
        )
    conn.commit()


# --- 왜 안 갔는가 -------------------------------------------------------------
# 종료 코드 0 하나로는 「보냈다」 와 「보낼 것이 없었다」 가 구별되지 않았다. 콘솔은
# 그래서 아무도 DM 을 못 받은 회차에도 「실행 완료」 만 보였다(2026-09-17). 채널마다
# 무슨 일이 있었는지 코드로 남긴다 — 운영자가 로그를 읽지 않아도 알아야 한다.
OUTCOME_SENT = "sent"
OUTCOME_GENERATE_FAILED = "generate-failed"
OUTCOME_NO_CANDIDATES = "no-candidates"
# 「새 후보가 없다」 를 셋으로 가른다. 조치가 서로 다르기 때문이다 — 원문이 없으면
# 소급 검토, 원문은 읽었는데 다 떨어졌으면 소급해도 같은 결과, 이미 다 결정했으면
# 할 일이 없다.
OUTCOME_NO_SOURCE = "no-new-source"
OUTCOME_NOTHING_PENDING = "nothing-pending"
OUTCOME_NO_ACCEPTED = "no-accepted-candidate"
OUTCOME_NO_CLIENT = "no-client"
OUTCOME_NO_REVIEWER = "no-reviewer"
OUTCOME_ALREADY_SENT = "already-sent"
OUTCOME_DM_FAILED = "dm-failed"

OUTCOME_LABELS = {
    OUTCOME_SENT: "검토 DM 발송",
    OUTCOME_GENERATE_FAILED: "요약 후보를 만들지 못했습니다",
    OUTCOME_NO_CANDIDATES: "오늘 검토할 새 후보가 없습니다",
    OUTCOME_NO_SOURCE: "마지막 처리 이후 새로 읽을 원문이 없습니다 — 과거 자료는 소급 검토로 읽습니다",
    OUTCOME_NO_ACCEPTED: "원문은 읽었지만 원문 대조를 통과한 후보가 없습니다",
    OUTCOME_NOTHING_PENDING: "다시 보낼 대기 후보가 없습니다",
    OUTCOME_NO_CLIENT: "이 워크스페이스의 Slack 토큰이 없습니다",
    OUTCOME_NO_REVIEWER: "활성 검토자가 없습니다",
    OUTCOME_ALREADY_SENT: "오늘 이미 보낸 검토자뿐입니다",
    OUTCOME_DM_FAILED: "DM 발송이 실패했습니다",
}


# 결정으로 치는 상태. 보류(`deferred`)는 결정이 아니고, 자동 폐기(`expired`)는
# 사람이 한 일이 아니다. 둘을 분모에 넣으면 승인률이 사람의 판단과 무관해진다.
DECIDED_STATES = ("approved", "rejected")


@dataclass
class FormSummary:
    """한 `form` 의 실측치. 승인률은 **사람이 결정한 것만** 분모로 쓴다."""

    form: str = FORM_QUOTE
    total: int = 0
    approved: int = 0
    rejected: int = 0
    pending: int = 0
    deferred: int = 0
    expired: int = 0

    @property
    def decided(self) -> int:
        return self.approved + self.rejected

    @property
    def approval_rate(self) -> float | None:
        """승인 / 결정. **결정이 없으면 `None`** — 0% 와 다르다."""
        return self.approved / self.decided if self.decided else None


def summarize_forms(rows: list[dict]) -> dict[str, FormSummary]:
    """`Store.form_stats()` 행을 form 별로 접는다."""
    out: dict[str, FormSummary] = {}
    for row in rows:
        form = str(row.get("form") or FORM_QUOTE)
        if form not in FORMS:
            form = FORM_QUOTE
        found = out.setdefault(form, FormSummary(form=form))
        count = int(row.get("n") or 0)
        found.total += count
        state = str(row.get("state") or "")
        if state in {"approved", "rejected", "pending", "deferred", "expired"}:
            setattr(found, state, getattr(found, state) + count)
    return out


def _empty_reason(stats: GenerateStats | None, *, deliver_only: bool = False) -> str:
    """후보가 0건인 이유. **셋을 한 문장으로 뭉치지 않는다.**"""
    if deliver_only:
        # 생성을 아예 안 돌린 회차다. 「새 후보가 없다」 로 말하면 사람은 생성
        # 버튼을 다시 누르고, 같은 원문에 돈이 또 나간다.
        return OUTCOME_NOTHING_PENDING
    if stats is None:
        # 오늘 이미 생성했고 그 후보는 모두 결정됐다. 새로 만들 것이 없다.
        return OUTCOME_NO_CANDIDATES
    if not stats.lines:
        return OUTCOME_NO_SOURCE
    if not stats.accepted:
        return OUTCOME_NO_ACCEPTED
    # 만들긴 했는데 대기 목록이 비었다 = 같은 후보가 이미 있었다(중복 차단).
    return OUTCOME_NO_CANDIDATES


def _empty_detail(stats: GenerateStats | None) -> str:
    if stats is None or not stats.lines:
        return ""
    return f"원문 {stats.lines}줄 · 요약기 제안 {stats.proposed}건 · 대조 통과 {stats.accepted}건"


@dataclass
class ChannelOutcome:
    """한 채널 한 회차의 실제 결과. `code` 는 사람이 조치할 사유다."""

    workspace: str
    channel_id: str
    channel_name: str = ""
    code: str = OUTCOME_NO_CANDIDATES
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    # 이 회차가 들고 있던 검토 대기 후보. 0 이 아닌데 `sent` 가 0 이면 **이미 만든
    # 것이 아직 아무에게도 안 갔다** — LLM 을 다시 부르지 않고 보내기만 하면 된다.
    pending: int = 0
    # 사유를 뒷받침하는 수치. 운영자가 로그를 열지 않아도 판단할 수 있어야 한다.
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "workspace": self.workspace,
            "channelId": self.channel_id,
            "channelName": self.channel_name,
            "code": self.code,
            "reason": OUTCOME_LABELS.get(self.code, self.code),
            "detail": self.detail,
            "sent": self.sent,
            "skipped": self.skipped,
            "failed": self.failed,
            "pending": self.pending,
        }


@dataclass
class RunResult:
    generated: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    # Canvas 회차 수치(B-50). **폴백을 성공과 섞지 않는다** — 섞으면 Canvas 가
    # 계속 실패하는데도 「잘 보내지고 있다」 로 보인다.
    canvas_created: int = 0
    canvas_fallback: int = 0
    canvas_ambiguous: int = 0
    expired: int = 0
    # 발송 시각이 지난 채널만 들어간다. 아직 시각 전인 채널은 결과가 아니다.
    outcomes: list[ChannelOutcome] = field(default_factory=list)


def _canvas_round(
    store: Store, client, *, workspace: str, channel_id: str, channel_label: str,
    rows: list[dict], review_date: date, recipients: list[str], result: RunResult,
) -> dict | None:
    """이 회차의 Canvas 를 만들고 좌표를 돌려준다. 못 만들면 `None`(폴백).

    **순서를 지킨다**(설계 §5). Artifact 행 → Canvas 생성 → 권한 → 상태 기록.
    Canvas 를 먼저 만들면 응답이 유실됐을 때 다음 실행이 하나 더 만든다.
    """
    from . import canvas_answer

    digest = str(rows[0].get("source_digest") or "") if rows else ""
    approved = store.approved(workspace, channel_id)
    try:
        body_hash = content_hash(approved, rows)
        body = canvas_markdown(
            channel_label=channel_label, review_date=review_date,
            rows=rows, approved=approved, channel_id=channel_id,
        )
    except AmbiguousProjection as exc:
        # 예상본을 확정할 수 없다. **그려서 보여 주지 않는다** — 바뀌지 않을 문장이
        # 바뀐다고 읽히면 사람이 그것을 승인한다.
        log.warning("요약 예상본 모호 ws=%s ch=%s: %s", workspace, channel_id, exc)
        result.canvas_ambiguous += 1
        return None

    artifact_id, state = store.begin_artifact(
        workspace=workspace, channel_id=channel_id, channel_name=channel_label,
        review_date=review_date, source_digest=digest, digest=body_hash, rows=rows,
    )
    if state == "ambiguous":
        # 지난 실행이 API 와 DB 사이에서 죽었다. **자동으로 다시 만들지 않는다** —
        # Slack 이 이미 만들었을 수 있고, 그러면 같은 회차 Canvas 가 둘이 된다.
        log.warning("모호 상태 회차라 Canvas 를 다시 만들지 않는다 id=%s", artifact_id)
        result.canvas_ambiguous += 1
        return None
    existing = store.artifact(artifact_id) or {}
    if state in ("ready", "partial", "completed") and existing.get("canvas_id"):
        # 이미 만든 회차다. 재실행에서 **하나만** 존재해야 한다.
        return {
            "artifact_id": artifact_id,
            "canvas_id": str(existing.get("canvas_id") or ""),
            "permalink": str(existing.get("canvas_permalink") or ""),
        }
    if state == "creating":
        # 이 호출이 만든 행은 `new` 다. 기존 `creating` 은 이전 실행이 API 전후
        # 어디에서 끊겼는지 알 수 없다. 재생성하면 중복 Canvas가 될 수 있다.
        store.mark_artifact(artifact_id, "ambiguous", error_code="canvas-create-interrupted")
        result.canvas_ambiguous += 1
        return None
    if state == "failed" and existing.get("canvas_id"):
        # Canvas 생성은 끝났고 권한 부여만 실패한 회차. 새 문서를 만들지 않고
        # 같은 문서에 현재 검토자 권한을 다시 부여한다.
        try:
            canvas_answer.grant_users(client, str(existing["canvas_id"]), recipients)
        except Exception as exc:  # noqa: BLE001
            log.warning("검토 Canvas 권한 재부여 실패 ws=%s: %s", workspace, type(exc).__name__)
            return None
        store.mark_artifact(artifact_id, "ready")
        return {
            "artifact_id": artifact_id,
            "canvas_id": str(existing.get("canvas_id") or ""),
            "permalink": str(existing.get("canvas_permalink") or ""),
        }
    if state == "failed":
        # Slack이 명백히 생성을 거절해 Canvas ID가 없는 경우만 다시 만든다.
        store.mark_artifact(artifact_id, "creating")
    elif state != "new":
        # 정상 상태인데 Canvas 좌표가 없거나 알 수 없는 상태다.
        store.mark_artifact(artifact_id, "ambiguous", error_code="canvas-state-unknown")
        result.canvas_ambiguous += 1
        return None

    try:
        canvas = canvas_answer.create(
            client, body,
            title=canvas_title(
                channel_label=channel_label, review_date=review_date,
                artifact_id=artifact_id,
            ) + canvas_answer.TITLE_SUFFIX,
            provenance={
                "workspace": workspace,
                "channel_id": channel_id,
                "qa_record_id": f"summary-review:{artifact_id}",
            },
        )
    except Exception as exc:  # noqa: BLE001 - Slack 오류 종류가 여러 가지다
        # **만들어졌는지 확실하지 않으면 `ambiguous`.** 명백한 거절만 `failed` 로
        # 두고 다시 시도한다. 중복 Canvas 보다 발송 지연을 택한다.
        code, state_after = _canvas_failure(exc)
        store.mark_artifact(artifact_id, state_after, error_code=code)
        log.warning("검토 Canvas 생성 실패 ws=%s ch=%s code=%s", workspace, channel_id, code)
        if state_after == "ambiguous":
            result.canvas_ambiguous += 1
        return None

    try:
        canvas_answer.grant_users(client, canvas.canvas_id, recipients)
    except Exception as exc:  # noqa: BLE001
        # 권한을 못 줬으면 링크를 보내도 열리지 않는다. Canvas 는 이미 있으므로
        # ID 는 기록하고, 이번 회차는 후보 DM 으로 간다.
        store.mark_artifact(
            artifact_id, "failed", canvas_id=canvas.canvas_id,
            permalink=canvas.permalink, error_code="canvas-access-failed",
        )
        log.warning("검토 Canvas 권한 부여 실패 ws=%s: %s", workspace, type(exc).__name__)
        return None

    store.mark_artifact(
        artifact_id, "ready", canvas_id=canvas.canvas_id, permalink=canvas.permalink
    )
    result.canvas_created += 1
    return {
        "artifact_id": artifact_id,
        "canvas_id": canvas.canvas_id,
        "permalink": canvas.permalink,
    }


# Slack 이 **분명히 거절한** 경우. 이때만 Canvas 가 안 만들어진 것이 확실하다.
_CLEAR_REFUSALS = (
    "invalid_auth", "not_authed", "missing_scope", "account_inactive",
    "channel_not_found", "invalid_arguments", "invalid_canvas",
    "canvas_disabled", "free_team_not_allowed", "restricted_action",
)


def _canvas_failure(exc: Exception) -> tuple[str, str]:
    """`(오류 코드, 다음 상태)`.

    타임아웃·연결 끊김은 **만들어졌는지 모른다.** 그걸 `failed` 로 두고 재시도하면
    같은 회차의 Canvas 가 둘이 된다.
    """
    text = str(exc)
    code = next((name for name in _CLEAR_REFUSALS if name in text), "")
    if code:
        return code, "failed"
    return f"canvas-unknown:{type(exc).__name__}", "ambiguous"


def run(conn, clients: dict, *, archive, channels, complete, owners=None,
        now: datetime | None = None, resend: bool = False,
        force_generate: bool = False, deliver_only: bool = False) -> RunResult:
    """설정 시각이 지난 채널의 후보를 만들고 **검토자에게** 민다.

    `owners` 는 더 이상 수신자를 만들지 않는다. 호출부 호환으로만 남긴다 —
    담당자를 자동으로 넣으면 그 사람이 승인 권한을 갖는데, 아무도 그 권한을
    준 적이 없다(설계 §6). 담당자도 보려면 검토자로 등록한다.

    `resend` 는 **운영자가 콘솔에서 직접 누른 회차에만** 쓴다. 오늘 이미 보낸
    검토자에게 같은 회차를 다시 민다 — 타이머가 이걸 켜면 하루 종일 같은 DM 이
    간다. 후보 자체가 없으면 재발송도 보낼 것이 없다.

    `force_generate` 는 수동 실행에서만 오늘의 생성 잠금을 우회한다. 원문 워터마크는
    그대로 쓰므로 이미 처리한 대화를 다시 요약하지 않고, 마지막 처리 뒤 새로 수집된
    원문만 후보 생성기에 보낸다.

    `deliver_only` 는 **LLM 을 한 번도 부르지 않는다.** 이미 만들어 둔 후보를 다시
    민다 — 요약은 됐는데 DM 만 실패한 회차를 복구하는 경로다. 이게 없으면 사람은
    보내려고 생성 버튼을 다시 누르고, 같은 원문에 돈이 또 나간다(B-59).
    """
    del owners
    from .daily_review import due

    now = now or datetime.now(KST)
    on = now.astimezone(KST).date()
    db = Store(conn)
    result = RunResult()
    for workspace, channel_id, channel_name, send_at in channels:
        if not due(send_at, now):
            continue
        outcome = ChannelOutcome(
            workspace=workspace, channel_id=channel_id, channel_name=channel_name,
        )
        result.outcomes.append(outcome)
        try:
            result.expired += db.expire_unconfirmed(
                workspace, channel_id, on
            )
            stats = None
            if not deliver_only and (
                force_generate or not db.generated_on(workspace, channel_id, on)
            ):
                stats = GenerateStats()
                result.generated += generate_channel(
                    db, archive, workspace=workspace, channel_id=channel_id,
                    channel_name=channel_name, complete=complete, now=now,
                    force=force_generate, stats=stats,
                )
            rows = db.pending(workspace, channel_id, on)
        except Exception as exc:  # noqa: BLE001 - 한 채널 실패로 다음 채널을 막지 않는다
            with contextlib.suppress(Exception):
                conn.rollback()
            log.warning(
                "요약 후보 생성 실패 ws=%s ch=%s code=%s",
                workspace, channel_id, type(exc).__name__,
            )
            result.failed += 1
            outcome.failed += 1
            outcome.code = OUTCOME_GENERATE_FAILED
            continue
        if not rows:
            result.skipped += 1
            outcome.skipped += 1
            outcome.code = _empty_reason(stats, deliver_only=deliver_only)
            outcome.detail = _empty_detail(stats)
            continue
        outcome.pending = len(rows)
        client = clients.get(workspace)
        if client is None:
            result.failed += 1
            outcome.failed += 1
            outcome.code = OUTCOME_NO_CLIENT
            continue
        label = channel_name or channel_id
        # **검토자만** 받는다. 담당자를 자동으로 넣지 않는다(설계 §6) — 담당이라는
        # 이유로 권한이 넓어지면 그 권한이 어디서 왔는지 아무도 설명할 수 없다.
        targets = db.reviewer_recipients(workspace, channel_id)
        if not targets:
            result.skipped += 1
            outcome.skipped += 1
            outcome.code = OUTCOME_NO_REVIEWER
            continue
        # Canvas 를 **먼저 한 번** 만들고 모든 수신자가 같은 것을 본다.
        round_info = None
        try:
            round_info = _canvas_round(
                db, client, workspace=workspace, channel_id=channel_id,
                channel_label=label, rows=rows, review_date=on,
                recipients=targets, result=result,
            )
        except Exception as exc:  # noqa: BLE001 - Canvas 실패가 검토 전체를 막지 않는다
            with contextlib.suppress(Exception):
                conn.rollback()
            log.warning("검토 Canvas 처리 실패 ws=%s code=%s", workspace, type(exc).__name__)
        if round_info is None:
            result.canvas_fallback += 1
        artifact_rows = db.artifact_rows(round_info["artifact_id"]) if round_info else rows

        for recipient in targets:
            already_sent = not resend and (
                db.delivery_sent(round_info["artifact_id"], recipient)
                if round_info else
                _already_sent(conn, workspace=workspace, channel_id=channel_id,
                              recipient=recipient, on=on)
            )
            if already_sent:
                result.skipped += 1
                outcome.skipped += 1
                if outcome.code != OUTCOME_SENT:
                    outcome.code = OUTCOME_ALREADY_SENT
                continue
            try:
                opened = client.conversations_open(users=recipient)
                dm = (opened.get("channel") or {}).get("id")
                if not dm:
                    raise SummaryReviewError("DM 채널을 열지 못했습니다.")
                if round_info:
                    blocks = canvas_review_blocks(
                        channel_label=label, review_date=on,
                        permalink=round_info["permalink"],
                        artifact_id=round_info["artifact_id"], rows=artifact_rows,
                    )
                else:
                    # 폴백은 **요약 후보와 근거만**이다. 첨부 변환 상세 DM 으로
                    # 돌아가지 않는다(인계 문서).
                    blocks = candidate_blocks(label, rows)
                posted = client.chat_postMessage(
                    channel=dm, text=f"요약 검토 후보 {len(artifact_rows)}건",
                    blocks=blocks,
                )
                if round_info:
                    # 결정 권한의 근거다. **보낸 뒤에** 남긴다 — 먼저 남기면
                    # 못 받은 사람이 결정할 수 있게 된다.
                    db.record_delivery(
                        round_info["artifact_id"], recipient, dm_channel=str(dm),
                        message_ts=str((posted or {}).get("ts") or ""), state="sent",
                    )
                    # 옛 날짜 단위 이력은 호환용이다. 회차 발송 기록이 권한과
                    # 멱등성의 기준이므로 이 기록 실패로 정상 DM을 실패 처리하거나
                    # `sent` delivery를 덮어쓰지 않는다.
                    with contextlib.suppress(Exception):
                        _mark_sent(conn, workspace=workspace, channel_id=channel_id,
                                   recipient=recipient, on=on,
                                   count=len(artifact_rows))
                else:
                    _mark_sent(conn, workspace=workspace, channel_id=channel_id,
                               recipient=recipient, on=on, count=len(artifact_rows))
                # 폐기 판정의 기준이다(B-58). 이 줄이 빠지면 보여 준 후보가
                # 「한 번도 안 보여 준 것」 으로 남아 영원히 폐기되지 않는다.
                with contextlib.suppress(Exception):
                    db.mark_delivered([row["id"] for row in artifact_rows if row.get("id")])
                result.sent += 1
                outcome.sent += 1
                outcome.code = OUTCOME_SENT
            except Exception as exc:  # noqa: BLE001 - Slack 오류 종류가 여러 가지다
                with contextlib.suppress(Exception):
                    conn.rollback()
                log.warning(
                    "요약 검토 DM 실패 ws=%s ch=%s code=%s",
                    workspace, channel_id, type(exc).__name__,
                )
                if round_info:
                    with contextlib.suppress(Exception):
                        db.record_delivery(
                            round_info["artifact_id"], recipient, state="failed",
                            error_code=type(exc).__name__,
                        )
                result.failed += 1
                outcome.failed += 1
                if outcome.code != OUTCOME_SENT:
                    outcome.code = OUTCOME_DM_FAILED
    return result


def last_round(conn, *, workspace: str, channel_id: str) -> tuple[str, str]:
    """이 채널의 **마지막 회차** 상태와 실패 코드. 회차가 없으면 `("", "")`.

    진단이 읽는다. 표가 아직 없는 설치에서도 죽지 않아야 한다 — 스키마를 안 올린
    상태에서 `/채널 상태` 가 통째로 실패하면 진단이 가장 필요할 때 침묵한다.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.summary_review_artifact') AS present")
        row = cur.fetchone()
        present = (row.get("present") if isinstance(row, dict) else row[0]) if row else None
        if not present:
            return "", ""
        cur.execute(
            """SELECT state,error_code FROM summary_review_artifact
            WHERE workspace=%s AND channel_id=%s ORDER BY created_at DESC LIMIT 1""",
            (workspace, channel_id),
        )
        row = cur.fetchone()
    if not row:
        return "", ""
    get = row.get if isinstance(row, dict) else None
    return (str(get("state") if get else row[0] or ""),
            str((get("error_code") if get else row[1]) or ""))


def _connect():
    import os

    import psycopg

    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row)


def register_slack_handlers(app, *, workspace: str, feedback_log) -> None:
    """기존 Socket Mode 앱에 검토 버튼을 등록한다."""
    def record(candidate_id: str, actor: str, kind: str, channel_id: str,
               text: str = "", *, code: str = "", artifact_id: str = "") -> None:
        """정정사항은 **피드백 로그에만** 남긴다(설계 §3.1).

        감사 기록에는 회차·후보 ID, 분류 코드, 결정자만 간다 — 사람이 쓴 정정
        문장이 감사 metadata 로 복제되면 그 자리가 근거의 사본이 된다.
        """
        feedback_log.write(
            workspace=workspace, channel_id=channel_id,
            qa_record_id=f"summary:{candidate_id}",
            answer_ts="", actor=actor, kind=kind, action="submitted", text=text,
        )
        try:
            from .console import audit_store
            audit_store.record(
                actor=actor, category="summary-review",
                action=f"{kind}:{code}" if code else kind,
                target_type="summary-candidate",
                target_id=f"{artifact_id}/{candidate_id}" if artifact_id else candidate_id,
                workspace=workspace,
            )
        except Exception:  # noqa: BLE001 - 감사 실패가 이미 끝난 결정을 되돌리면 안 된다
            pass

    def refresh_dms(store: Store, client, artifact_id: str) -> None:
        """결정 뒤 모든 수신자 DM 을 같은 상태로 맞춘다(설계 §8).

        **정정 본문은 옮기지 않는다.** 다른 검토자의 DM 에는 결정 상태만 보인다 —
        정정은 그 사람이 쓴 판단이고, 그것을 퍼뜨리면 다음 검토가 그 문장에 끌린다.

        실패해도 **결정을 되돌리지 않는다.** 이미 DB 에 반영된 사실이다.
        """
        if not artifact_id:
            return
        try:
            artifact = store.artifact(artifact_id) or {}
            rows = store.artifact_rows(artifact_id)
            targets = store.deliveries(artifact_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("DM 갱신 준비 실패 code=%s", type(exc).__name__)
            return
        blocks = canvas_review_blocks(
            channel_label=str(artifact.get("channel_name") or artifact.get("channel_id") or ""),
            review_date=artifact.get("review_date"),
            permalink=str(artifact.get("canvas_permalink") or ""),
            artifact_id=artifact_id, rows=rows,
        )
        blocks = _with_decisions(blocks, rows)
        for target in targets:
            get = target.get if isinstance(target, dict) else None
            channel = str((get("dm_channel") if get else target[1]) or "")
            ts = str((get("message_ts") if get else target[2]) or "")
            if not channel or not ts:
                continue
            try:
                client.chat_update(channel=channel, ts=ts, blocks=blocks,
                                   text="요약 검토 상태가 갱신되었습니다")
            except Exception as exc:  # noqa: BLE001 - 갱신 실패가 결정을 되돌리면 안 된다
                log.warning("검토 DM 갱신 실패 code=%s", type(exc).__name__)

    def settle(cid: str, artifact_id: str, actor: str, kind: str, text: str = "",
               *, code: str = "") -> str:
        """결정 하나를 적용하고 DM 을 갱신한다. 사람에게 보일 한 줄을 돌려준다."""
        decision = {"positive": "approved", "negative": "rejected", "defer": "deferred"}[kind]
        with _connect() as conn:
            store = Store(conn)
            changed = store.decide(
                cid, workspace=workspace, actor=actor, decision=decision,
                correction=text,
                defer_until=(default_defer_date(datetime.now(KST).date())
                             if decision == "deferred" else None),
                artifact_id=artifact_id,
            )
            channel_id = store.candidate_channel(cid, workspace)
            if changed and artifact_id:
                store.refresh_artifact_state(artifact_id)
        if not changed:
            return "이미 다른 검토자가 처리했습니다."
        if kind != "defer":
            record(cid, actor, kind, channel_id, text, code=code, artifact_id=artifact_id)
        return {
            "positive": "승인했습니다.",
            "negative": ("반려했습니다. 정정사항은 검토 기록에 남겼으며 원문이나 "
                         "답변 근거를 자동으로 바꾸지 않습니다."),
            "defer": "내일 다시 알려드리겠습니다.",
        }[kind]

    @app.action("tybot_summary_review_open_canvas")
    def open_canvas(ack):
        # URL 버튼은 Slack 이 링크를 열어 준다. 우리가 할 일은 응답뿐이다.
        ack()

    @app.action(ACTION_APPROVE)
    def approve(ack, body, respond, client):
        ack()
        artifact_id, cid = parse_button_value(
            ((body.get("actions") or [{}])[0]).get("value")
        )
        actor = str((body.get("user") or {}).get("id") or "")
        reply = settle(cid, artifact_id, actor, "positive")
        if artifact_id:
            with _connect() as conn:
                refresh_dms(Store(conn), client, artifact_id)
        respond(text=reply, replace_original=False)

    @app.action(ACTION_APPROVE_ALL)
    def approve_all(ack, body, respond, client):
        ack()
        artifact_id = str(((body.get("actions") or [{}])[0]).get("value") or "")
        actor = str((body.get("user") or {}).get("id") or "")
        with _connect() as conn:
            store = Store(conn)
            if not store.may_decide(artifact_id, actor, workspace):
                respond(text="이 검토 회차의 수신자가 아닙니다.", replace_original=False)
                return
            rows = store.artifact_rows(artifact_id)
            approved_ids, skipped = store.approve_all(
                artifact_id, workspace=workspace, actor=actor
            )
            store.refresh_artifact_state(artifact_id)
            channels = {str(r.get("channel_id") or "") for r in rows}
        approved_set = set(approved_ids)
        for row in rows:
            if str(row.get("id") or "") in approved_set:
                record(
                    str(row.get("id")),
                    actor,
                    "positive",
                    next(iter(channels), ""),
                    artifact_id=artifact_id,
                )
        if artifact_id:
            with _connect() as conn:
                refresh_dms(Store(conn), client, artifact_id)
        tail = f" · 이미 처리된 {skipped}건은 건너뛰었습니다." if skipped else ""
        respond(text=f"{len(approved_ids)}건을 승인했습니다.{tail}", replace_original=False)

    @app.action(ACTION_DEFER)
    def defer(ack, body, respond, client):
        ack()
        artifact_id, cid = parse_button_value(
            ((body.get("actions") or [{}])[0]).get("value")
        )
        actor = str((body.get("user") or {}).get("id") or "")
        reply = settle(cid, artifact_id, actor, "defer")
        if artifact_id:
            with _connect() as conn:
                refresh_dms(Store(conn), client, artifact_id)
        respond(text=reply, replace_original=False)

    @app.action(ACTION_REJECT)
    def reject(ack, body, client):
        ack()
        artifact_id, cid = parse_button_value(
            ((body.get("actions") or [{}])[0]).get("value")
        )
        position, headline = 0, ""
        if artifact_id:
            # 무엇을 반려하는지 모달에 보여 준다. 없으면 다른 후보에 정정을 적는다.
            with contextlib.suppress(Exception), _connect() as conn:
                for row in Store(conn).artifact_rows(artifact_id):
                    if str(row.get("id")) == cid:
                        position = int(row.get("position") or 0)
                        headline = _dm_headline(position, row)
                        break
        client.views_open(
            trigger_id=body["trigger_id"],
            view=reject_modal(cid, artifact_id=artifact_id, position=position,
                              headline=headline),
        )

    @app.view(REJECT_CALLBACK)
    def reject_submit(ack, body, view, client):
        correction = correction_from_view(view)
        wrong_part = wrong_part_from_view(view)
        errors = {}
        if not wrong_part:
            errors["wrong_part"] = "틀린 부분을 골라 주세요."
        if len(correction) < MIN_CORRECTION:
            errors["correction"] = "올바른 내용을 구체적으로 적어 주세요."
        if errors:
            ack(response_action="errors", errors=errors)
            return
        ack()
        meta = reject_metadata(view)
        actor = str((body.get("user") or {}).get("id") or "")
        settle(meta["candidate_id"], meta["artifact_id"], actor, "negative", correction,
               code=wrong_part)
        if meta["artifact_id"]:
            with _connect() as conn:
                refresh_dms(Store(conn), client, meta["artifact_id"])
