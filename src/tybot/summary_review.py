"""Hermes 요약 후보 생성, 검토 DM, 승인 상태 전이.

후보와 승인본은 파생 DB에만 둔다. 원문 아카이브와 답변 검색에는 넣지 않는다.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KINDS = frozenset({"number_or_schedule", "new_issue", "closed_issue"})
OPEN_STATES = ("pending", "deferred")
ACTION_APPROVE = "tybot_summary_review_approve"
ACTION_REJECT = "tybot_summary_review_reject"
ACTION_DEFER = "tybot_summary_review_defer"
REJECT_CALLBACK = "tybot_summary_review_reject_modal"
MAX_CANDIDATES = 10
MAX_SOURCE_CHARS = 40_000
MAX_APPROVED_CHARS = 8_000
MAX_NEW_SOURCE_CHARS = MAX_SOURCE_CHARS - MAX_APPROVED_CHARS - 500
MIN_CORRECTION = 5
KST = timezone(timedelta(hours=9))
log = logging.getLogger("tybot.summary_review")


class SummaryReviewError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceLine:
    at: str
    author: str
    text: str
    locator: str


@dataclass(frozen=True)
class Proposal:
    kind: str
    current_text: str
    proposed_text: str
    evidence_quote: str
    evidence_at: str
    evidence_author: str
    evidence_locator: str


def _normalized(value: str) -> str:
    value = re.sub(r"[`*_\"'“”‘’]", "", value or "")
    value = re.sub(r"^[\s>•◦∙]+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _numbers(value: str) -> set[str]:
    return set(re.findall(r"(?<![가-힣A-Za-z])\d[\d,.]*(?:%|억|만|원|개월|일|년)?", value or ""))


def parse_proposals(raw: str, source: list[SourceLine],
                    approved: list[str] | None = None) -> list[Proposal]:
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
    source_norm = [(_normalized(line.text), line) for line in source]
    accepted: list[Proposal] = []
    approved_norm = {_normalized(item) for item in (approved or [])}
    for item in rows[:MAX_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        proposed = str(item.get("proposed_text") or "").strip()
        quote = str(item.get("evidence_quote") or "").strip()
        quote_norm = _normalized(quote)
        if kind not in KINDS or not proposed or len(quote_norm) < 5:
            continue
        matched = next((line for text, line in source_norm if quote_norm in text), None)
        if matched is None:
            continue
        current = str(item.get("current_text") or "").strip()
        if current and _normalized(current) not in approved_norm:
            continue
        # 승인 후보는 요약기의 창작물이 아니라 원문에서 뽑은 검토 단위다.
        if _normalized(proposed) != quote_norm:
            continue
        # 새로 만든 숫자는 원문 인용이나 기존 승인 문장에 실제로 있어야 한다.
        if not _numbers(proposed) <= (_numbers(quote) | _numbers(current)):
            continue
        accepted.append(Proposal(
            kind=kind,
            current_text=current,
            proposed_text=proposed,
            evidence_quote=quote,
            evidence_at=matched.at,
            evidence_author=matched.author,
            evidence_locator=matched.locator,
        ))
    return accepted


def source_digest(lines: list[SourceLine]) -> str:
    body = "\n".join(f"{x.locator}|{x.at}|{x.author}|{x.text}" for x in lines)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


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
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT body FROM approved_summary_item WHERE workspace=%s AND channel_id=%s
                AND superseded_at IS NULL ORDER BY approved_at""",
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
                     proposed_text,evidence_quote,evidence_at,evidence_author,evidence_locator)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (uuid.uuid4(), workspace, channel_id, channel_name, on, digest,
                     proposal.kind, proposal.current_text, proposal.proposed_text,
                     proposal.evidence_quote, proposal.evidence_at, proposal.evidence_author,
                     proposal.evidence_locator),
                )
            cur.execute(
                """INSERT INTO summary_review_cursor(workspace,channel_id,watermark,source_digest)
                VALUES (%s,%s,%s,%s) ON CONFLICT(workspace,channel_id) DO UPDATE SET
                watermark=excluded.watermark, source_digest=excluded.source_digest,
                failed_digest='', retry_after=NULL, checked_at=now()""",
                (workspace, channel_id, watermark, digest),
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
               correction: str = "", defer_until: date | None = None) -> bool:
        if decision not in {"approved", "rejected", "deferred"}:
            raise SummaryReviewError("지원하지 않는 검토 결정입니다.")
        try:
            key = uuid.UUID(candidate_id)
        except ValueError as exc:
            raise SummaryReviewError("잘못된 요약 후보 식별자입니다.") from exc
        if decision == "rejected" and len(correction.strip()) < MIN_CORRECTION:
            raise SummaryReviewError("반려할 때는 정정 사항을 입력해야 합니다.")
        with self.conn.cursor() as cur:
            cur.execute(
                """UPDATE summary_review_candidate SET state=%s, decided_at=CASE WHEN %s='deferred' THEN NULL ELSE now() END,
                decided_by=%s, correction=%s, defer_until=%s WHERE id=%s AND workspace=%s
                AND state IN ('pending','deferred') AND EXISTS (
                    SELECT 1 FROM review_digest_sent sent
                    WHERE sent.workspace=summary_review_candidate.workspace
                    AND sent.channel_id=summary_review_candidate.channel_id
                    AND sent.recipient=%s AND sent.kind='summary'
                ) RETURNING *""",
                (decision, decision, actor, correction.strip(), defer_until, key, workspace, actor),
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
    out = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{channel_name} - 요약 검토 {len(rows)}건*\n근거 원문과 후보를 확인해 주세요."}}]
    labels = {"number_or_schedule": "숫자·일정", "new_issue": "새 쟁점", "closed_issue": "끝난 쟁점"}
    for row in rows:
        get = row.get
        cid = str(get("id"))
        body = f"*{labels.get(str(get('kind')), '요약')}*\n*후보* {get('proposed_text')}\n*근거* {get('evidence_author')} · {get('evidence_at')}\n>{get('evidence_quote')}"
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
    out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "승인 요약은 파생 문서이며 원문이나 답변 검색 근거를 변경하지 않습니다."}]})
    return out


def reject_modal(candidate_id: str) -> dict:
    return {"type": "modal", "callback_id": REJECT_CALLBACK,
            "private_metadata": candidate_id,
            "title": {"type": "plain_text", "text": "요약 후보 반려"},
            "submit": {"type": "plain_text", "text": "반려"},
            "close": {"type": "plain_text", "text": "취소"},
            "blocks": [{"type": "input", "block_id": "correction",
                        "label": {"type": "plain_text", "text": "정정 사항"},
                        "element": {"type": "plain_text_input", "action_id": "correction", "multiline": True, "min_length": MIN_CORRECTION, "max_length": 2000}}]}


def correction_from_view(view: dict) -> str:
    return str(((view.get("state") or {}).get("values") or {}).get("correction", {}).get("correction", {}).get("value") or "").strip()


def contract_prompt() -> str:
    path = Path(__file__).resolve().parents[2] / "subbots" / "hermes" / "contract" / "summary-review.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SummaryReviewError("Hermes 요약 검토 계약을 읽지 못했습니다.") from exc


def default_defer_date(today: date) -> date:
    return today + timedelta(days=1)


def channel_source(archive, workspace: str, channel_id: str, watermark: str,
                   start_at: str = "") -> list[SourceLine]:
    """한 채널의 새 원문만 가져온다. 승인 요약과 봇 출력은 읽지 않는다."""
    rows: list[SourceLine] = []
    for doc in archive.docs():
        if doc.workspace != workspace or str(doc.channel_id or "") != channel_id:
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
            rows.append(SourceLine(line.ts, line.speaker, line.text, locator))
    rows.sort(key=lambda item: (item.at, item.locator))
    selected: list[SourceLine] = []
    used = 0
    for item in rows:
        size = len(item.at) + len(item.author) + len(item.text) + 8
        if selected and used + size > MAX_NEW_SOURCE_CHARS:
            break
        selected.append(item)
        used += size
    return selected


def generate_channel(store: Store, archive, *, workspace: str, channel_id: str,
                     channel_name: str, complete, now: datetime) -> int:
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
    digest = source_digest(source)
    if not store.may_attempt(workspace, channel_id, digest, now):
        return 0
    try:
        approved = store.approved(workspace, channel_id)
        raw = complete(
            contract_prompt(),
            prompt_input(approved=approved, source=source),
            workspace,
        )
        proposals = parse_proposals(raw, source, approved)
    except Exception:
        with contextlib.suppress(Exception):
            store.conn.rollback()
        with contextlib.suppress(Exception):
            store.mark_failed(workspace, channel_id, digest, now)
        raise
    watermark = f"{source[-1].at}|{source[-1].locator}"
    return store.save_run(
        workspace=workspace, channel_id=channel_id, channel_name=channel_name,
        watermark=watermark, digest=digest, proposals=proposals,
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


@dataclass
class RunResult:
    generated: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0


def run(conn, clients: dict, *, archive, channels, owners, complete,
        now: datetime | None = None) -> RunResult:
    """설정 시각이 지난 채널의 후보를 만들고 검토자에게 민다."""
    from .daily_review import due, recipients

    now = now or datetime.now(KST)
    on = now.astimezone(KST).date()
    db = Store(conn)
    result = RunResult()
    for workspace, channel_id, channel_name, send_at in channels:
        if not due(send_at, now):
            continue
        try:
            if not db.generated_on(workspace, channel_id, on):
                result.generated += generate_channel(
                    db, archive, workspace=workspace, channel_id=channel_id,
                    channel_name=channel_name, complete=complete, now=now,
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
            continue
        if not rows:
            result.skipped += 1
            continue
        client = clients.get(workspace)
        if client is None:
            result.failed += 1
            continue
        for recipient in recipients(
            workspace, channel_id, owner=owners.get((workspace, channel_id), "")
        ):
            if _already_sent(conn, workspace=workspace, channel_id=channel_id,
                             recipient=recipient, on=on):
                result.skipped += 1
                continue
            try:
                opened = client.conversations_open(users=recipient)
                dm = (opened.get("channel") or {}).get("id")
                if not dm:
                    raise SummaryReviewError("DM 채널을 열지 못했습니다.")
                client.chat_postMessage(
                    channel=dm, text=f"요약 검토 후보 {len(rows)}건",
                    blocks=candidate_blocks(channel_name or channel_id, rows),
                )
                _mark_sent(conn, workspace=workspace, channel_id=channel_id,
                           recipient=recipient, on=on, count=len(rows))
                result.sent += 1
            except Exception as exc:  # noqa: BLE001 - Slack 오류 종류가 여러 가지다
                with contextlib.suppress(Exception):
                    conn.rollback()
                log.warning(
                    "요약 검토 DM 실패 ws=%s ch=%s code=%s",
                    workspace, channel_id, type(exc).__name__,
                )
                result.failed += 1
    return result


def _connect():
    import os

    import psycopg

    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=psycopg.rows.dict_row)


def register_slack_handlers(app, *, workspace: str, feedback_log) -> None:
    """기존 Socket Mode 앱에 검토 버튼을 등록한다."""
    def record(candidate_id: str, actor: str, kind: str, channel_id: str,
               text: str = "") -> None:
        feedback_log.write(
            workspace=workspace, channel_id=channel_id,
            qa_record_id=f"summary:{candidate_id}",
            answer_ts="", actor=actor, kind=kind, action="submitted", text=text,
        )
        try:
            from .console import audit_store
            audit_store.record(
                actor=actor, category="summary-review", action=kind,
                target_type="summary-candidate", target_id=candidate_id,
                workspace=workspace,
            )
        except Exception:  # noqa: BLE001 - 감사 실패가 이미 끝난 결정을 되돌리면 안 된다
            pass

    @app.action(ACTION_APPROVE)
    def approve(ack, body, respond):
        ack()
        cid = str(((body.get("actions") or [{}])[0]).get("value") or "")
        actor = str((body.get("user") or {}).get("id") or "")
        with _connect() as conn:
            store = Store(conn)
            changed = store.decide(
                cid, workspace=workspace, actor=actor, decision="approved"
            )
            channel_id = store.candidate_channel(cid, workspace)
        if changed:
            record(cid, actor, "positive", channel_id)
        respond(text="승인했습니다." if changed else "이미 다른 검토자가 처리한 후보입니다.", replace_original=False)

    @app.action(ACTION_DEFER)
    def defer(ack, body, respond):
        ack()
        cid = str(((body.get("actions") or [{}])[0]).get("value") or "")
        actor = str((body.get("user") or {}).get("id") or "")
        with _connect() as conn:
            changed = Store(conn).decide(
                cid, workspace=workspace, actor=actor, decision="deferred",
                defer_until=default_defer_date(datetime.now(KST).date()),
            )
        respond(text="내일 다시 알려드리겠습니다." if changed else "이미 처리한 후보입니다.", replace_original=False)

    @app.action(ACTION_REJECT)
    def reject(ack, body, client):
        ack()
        cid = str(((body.get("actions") or [{}])[0]).get("value") or "")
        client.views_open(trigger_id=body["trigger_id"], view=reject_modal(cid))

    @app.view(REJECT_CALLBACK)
    def reject_submit(ack, body, view):
        correction = correction_from_view(view)
        if len(correction) < MIN_CORRECTION:
            ack(response_action="errors", errors={"correction": "올바른 내용을 구체적으로 적어 주세요."})
            return
        ack()
        cid = str(view.get("private_metadata") or "")
        actor = str((body.get("user") or {}).get("id") or "")
        with _connect() as conn:
            store = Store(conn)
            changed = store.decide(
                cid, workspace=workspace, actor=actor, decision="rejected",
                correction=correction,
            )
            channel_id = store.candidate_channel(cid, workspace)
        if changed:
            record(cid, actor, "negative", channel_id, correction)
