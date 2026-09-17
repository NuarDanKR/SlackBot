"""B-17 rule QA: compare deployed and draft specialist rules in memory."""
from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ..access import RequestContext
from ..archive.store import ArchiveStore
from ..config import cost_state_path
from ..gateway import cost
from ..gateway.budget import WorkspaceLimits
from ..gateway.router import Router
from ..paths import archive_dir
from ..specialist_router import Specialist, preview_rules
from ..specialist_tools import ToolBox
from ..workspaces import load_workspaces

KST = timezone(timedelta(hours=9))
MAX_QUESTION_CHARS = 2000
MAX_OUTPUT_CHARS = 20_000


class RuleTestError(RuntimeError):
    pass


def rules_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _request_context(email: str, workspace: str) -> RequestContext:
    """Resolve the console actor to their real Slack channel memberships."""
    cfg = next((item for item in load_workspaces() if item.key == workspace), None)
    if cfg is None:
        raise RuleTestError("활성 워크스페이스 설정을 찾지 못했습니다.")
    try:
        from slack_sdk import WebClient

        client = WebClient(token=cfg.bot_token)
        found = client.users_lookupByEmail(email=email)
        slack_user = str((found.get("user") or {}).get("id") or "")
        if not slack_user:
            raise RuleTestError("콘솔 이메일과 일치하는 Slack 사용자를 찾지 못했습니다.")
        channels: set[str] = set()
        cursor = ""
        while True:
            response = client.users_conversations(
                user=slack_user,
                types="public_channel,private_channel",
                limit=200,
                cursor=cursor or None,
            )
            channels.update(
                "#" + str(item.get("name") or "")
                for item in response.get("channels", [])
                if item.get("name")
            )
            cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
    except RuleTestError:
        raise
    except Exception as exc:
        raise RuleTestError(
            "Slack 사용자 권한을 확인하지 못해 시험을 중단했습니다."
        ) from exc
    return RequestContext(
        workspace=workspace,
        channels=frozenset(channels),
        readable_workspaces=cfg.readable,
        is_root=cfg.is_root,
    )


def _specialist(row: dict) -> Specialist:
    return Specialist(
        key=str(row["key"]),
        name=str(row["name"]),
        domain=str(row["domain"]),
        routing_hint=str(row.get("routing_hint") or ""),
        adapter=str(row["adapter"]),
        model=str(row.get("model") or ""),
        min_confidence=float(row.get("min_confidence") or 0.6),
        rules=str(row.get("rules") or ""),
        execution_mode=str(row.get("execution_mode") or "prompt"),
    )


def _sources(answer) -> list[str]:
    if answer is None:
        return []
    labels: list[str] = []
    for doc in answer.documents:
        path = getattr(doc, "path", None)
        channel = str(getattr(doc, "channel", "") or "")
        name = getattr(path, "name", "") or ""
        label = " · ".join(part for part in (channel, name) if part)
        if label and label not in labels:
            labels.append(label)
    for link in answer.live_links:
        if link and link not in labels:
            labels.append(link)
    return labels[:20]


def _seed_evidence(store: ArchiveStore, ctx: RequestContext, question: str) -> list[str]:
    hits = store.search(question, ctx, limit=20)
    return [
        f"[{hit.line.ts}] ({hit.doc.channel}) {hit.line.speaker}: {hit.line.text}"
        for hit in hits
    ]


def _run_variant(
    specialist: Specialist,
    *,
    variant: str,
    question: str,
    workspace: str,
    ctx: RequestContext,
    store: ArchiveStore,
    router: Router,
    evidence: list[str],
) -> dict:
    before = router.spent_today_for(workspace)
    with cost.attribute_to(workspace):
        outcome = preview_rules(
            specialist,
            question=question,
            workspace=workspace,
            evidence=evidence,
            router=router,
            authorization_id=(
                f"rule-test:{workspace}:"
                f"{rules_hash(chr(0).join(sorted(ctx.channels)))[:12]}"
            ),
            toolbox_factory=lambda: ToolBox(store=store, ctx=ctx),
            variant=variant,
        )
    charged = max(0.0, router.spent_today_for(workspace) - before)
    answer = outcome.answer
    return {
        "status": outcome.status,
        "answer": (answer.text if answer else "")[:MAX_OUTPUT_CHARS],
        "model": answer.model if answer else "",
        "costUsd": round(charged, 6),
        "errorCode": outcome.error_code,
        "sources": _sources(answer),
    }


def run(
    row: dict,
    *,
    draft_rules: str,
    question: str,
    workspace: str,
    actor: str,
    ctx: RequestContext | None = None,
    store: ArchiveStore | None = None,
    router: Router | None = None,
) -> dict:
    question = question.strip()
    draft_rules = draft_rules.strip()
    workspace = workspace.strip().lower()
    if not question or len(question) > MAX_QUESTION_CHARS:
        raise RuleTestError(f"시험 질문은 1~{MAX_QUESTION_CHARS:,}자여야 합니다.")
    if len(draft_rules) > 8000:
        raise RuleTestError("편집 중인 규칙은 8,000자를 넘을 수 없습니다.")
    if workspace not in set(row.get("workspaces") or []):
        raise RuleTestError("이 전문 봇이 승인된 워크스페이스가 아닙니다.")

    ctx = ctx or _request_context(actor, workspace)
    store = store or ArchiveStore(archive_dir())
    router = router or Router.from_default_registry(
        daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
        default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
        cost_state_path=cost_state_path(os.getenv("QA_LOG_DIR", "./qa-log")),
        workspace_limits=WorkspaceLimits(),
    )
    current = _specialist(row)
    draft = replace(current, rules=draft_rules)
    evidence = _seed_evidence(store, ctx, question) if current.execution_mode == "prompt" else []
    current_result = _run_variant(
        current, variant="current", question=question, workspace=workspace,
        ctx=ctx, store=store, router=router, evidence=evidence,
    )
    draft_result = _run_variant(
        draft, variant="draft", question=question, workspace=workspace,
        ctx=ctx, store=store, router=router, evidence=evidence,
    )
    created = datetime.now(KST)
    return {
        "id": str(uuid.uuid4()),
        "specialist": current.key,
        "workspace": workspace,
        "requester": actor.lower(),
        "question": question,
        "currentRulesHash": rules_hash(current.rules),
        "draftRulesHash": rules_hash(draft.rules),
        "current": current_result,
        "draft": draft_result,
        "createdAt": created.isoformat(),
        "expiresAt": (created + timedelta(days=30)).isoformat(),
    }
