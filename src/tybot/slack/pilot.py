"""워크스페이스 봇 — Socket Mode(아웃바운드 전용). 여러 워크스페이스를 한 프로세스에서 운영한다.

수집 경로 두 가지:
1. **실시간**(기본) — message.channels / message.groups 이벤트로 들어오는 즉시 원문 append.
   Slack 신규 비-마켓플레이스 앱은 conversations.history 가 분당 1요청/15건으로 제한되므로
   이 경로가 본선이다. 비공개 채널은 봇이 초대된 곳만 이벤트가 온다.
2. **백필**(`수집`) — 과거 대화 보충용. rate limit 때문에 느리다.

멀티 워크스페이스: 워크스페이스마다 앱을 따로 만들고(봇 토큰 + 앱 토큰), 각각 Socket Mode 연결을
연다. 아카이브·감사기록·LLM 게이트웨이는 공유하되 **조회 권한은 워크스페이스 경계로 분리**한다
(`docs/multi-workspace.md`).

실행: python -m tybot.slack.pilot
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime

from .. import (
    daily_review,
    evidence_view,
    heartbeat,
    reviewers,
    schedule_dm,
    specialist_router,
)
from ..access import RequestContext
from ..answer import Answer, AnswerEngine
from ..archive import writer
from ..archive.canvas import canvas_lines
from ..archive.files import (
    AttachmentOrigin,
    attachment_storage,
    stage_attachments,
)
from ..archive.store import ArchiveStore
from ..archive.writer import KST
from ..attachment_trace import confirm_archived
from ..audit import QALog, QARecord
from ..autojoin import on_channel_event, sweep
from ..canvas_answer import create as create_answer_canvas
from ..canvas_answer import grant_channel as grant_canvas_channel
from ..canvas_answer import grant_user as grant_canvas_user
from ..canvas_answer import parse_request as parse_canvas_request
from ..channel_health import HealthFacts
from ..channel_health import report as health_report
from ..channel_management import (
    EDIT_CALLBACK,
    ChannelNameError,
    ChannelOwnerStore,
    action_prefix,
    create_modal,
    edit_from_view,
    edit_modal,
    request_from_view,
    requests_from_view,
    selected_prefix,
    typed_task,
)
from ..channels import parse, should_collect
from ..collection_status import ChannelFacts
from ..collection_status import report as collection_report
from ..compose import join_sections, truncated_notice, write_from_facts
from ..config import cost_state_path
from ..db import connect as db_connect
from ..evidence_refs import attachment_refs_to_json, refs_to_json
from ..failures import failure_message
from ..feedback import (
    SLASH_HELP,
    FeedbackLog,
    correction_text,
    feedback_modal,
)
from ..feedback import (
    from_view as feedback_from_view,
)
from ..feedback import (
    thanks as feedback_thanks,
)
from ..feedback import (
    validation_errors as feedback_errors,
)
from ..identity import backfill as identity_backfill
from ..identity import ensure as ensure_identity
from ..intent import (
    INGEST_ALL_RE,
    INGEST_RE,
    MAX_TASKS,
    SELF_KINDS,
    WRITE_KINDS,
    Intent,
)
from ..lock import AlreadyRunning, LockUnavailable, instance_lock
from ..managed_env import consume_restart_request
from ..orgsearch import NO_MATCH as ORG_NO_MATCH
from ..orgsearch import UNAVAILABLE as ORG_UNAVAILABLE
from ..orgsearch import defaults_by_prefix, my_org_chain, notice_option
from ..orgsearch import options as org_options
from ..orgsearch import search as org_search
from ..paths import check_paths
from ..poll_view import (
    ACTION_CLOSE,
    ACTION_RESULTS,
    ACTION_VOTE,
    private_results,
)
from ..poll_view import (
    MODAL_CALLBACK as POLL_MODAL,
)
from ..poll_view import (
    create_modal as poll_create_modal,
)
from ..poll_view import (
    fallback_text as poll_fallback,
)
from ..poll_view import (
    help_text as poll_help,
)
from ..poll_view import (
    message_blocks as poll_blocks,
)
from ..poll_view import (
    read_modal as read_poll_modal,
)
from ..polls import PollError, apply_vote, close_poll, create_poll
from ..polls import load as load_poll
from ..polls import save as save_poll
from ..schedule import HELP as SCHEDULE_HELP
from ..schedule import (
    UNAVAILABLE as SCHEDULE_UNAVAILABLE,
)
from ..schedule import (
    fetch as schedule_fetch,
)
from ..schedule import (
    format_reply as schedule_reply,
)
from ..schedule import (
    last_sync as schedule_last_sync,
)
from ..schedule import (
    parse_window,
    unknown_window,
)
from ..scope_report import WELCOME, ScopeFacts
from ..scope_report import report as scope_text
from ..status_tree import build_tree, render_tree, totals
from ..workspaces import ConfigError, WorkspaceConfig, load_workspaces

log = logging.getLogger("tybot.slack")

# 응답 문구를 여러 줄로 잇는다. 소스에 `\n` 을 직접 쓰면 이 파일을 다루는
# 스크립트·heredoc 에서 자꾸 망가진다(실제로 여러 번 깨졌다).
NEWLINE = chr(10)

# 슬래시 명령 본문의 사람 멘션. Slack 은 `<@U123>` 또는 `<@U123|이름>` 으로 보낸다.
# **이름으로 받지 않는다** — 동명이인이 있고, 이름은 바뀐다.
#
# 아래 `MENTION_RE` 와 이름을 달리 둔다. 그쪽은 본문에서 봇 멘션을 지우는 용도라
# 캡처 그룹이 없다 — 같은 이름을 쓰면 **나중 정의가 이겨서** 여기서 사용자 ID 대신
# 멘션 전체가 잡히고, 검토자 저장이 조용히 엉뚱한 값을 넣는다. 실제로 그랬다.
SLASH_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
# `09:00`·`9시`·`0900`. 멘션 안의 숫자를 집지 않도록 멘션을 먼저 지운 뒤 찾는다.
SLASH_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2}|\d{4}|\d{1,2})(?:시)?(?![\d:])")


def _mentioned_users(text: str) -> list[str]:
    """멘션된 사용자 ID. 순서를 지키고 중복은 없앤다."""
    out: list[str] = []
    for uid in SLASH_MENTION_RE.findall(text or ""):
        if uid not in out:
            out.append(uid)
    return out


def _asks_to_clear(text: str) -> bool:
    """검토자를 없애겠다는 뜻인가. 인수 없이 부른 것과 구별해야 한다 —

    인수가 없으면 **현재 상태를 보여주는 것**이 맞고, 해제는 명시해야 한다.
    실수로 `/채널 검토자` 만 쳐서 검토가 멈추면 아무 표시도 없이 요약이 안 된다.
    """
    return (text or "").strip() in {"없음", "해제", "없애", "삭제", "none"}


def _time_token(text: str) -> str:
    """인수에서 시각만 꺼낸다. 멘션 안의 숫자를 시각으로 읽으면 안 된다."""
    stripped = SLASH_MENTION_RE.sub(" ", text or "")
    found = SLASH_TIME_RE.search(stripped)
    return found.group(1) if found else ""


MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
HISTORY_LIMIT = 15  # 신규 앱 conversations.history / replies 요청당 상한
THREAD_FETCH_LIMIT = 5  # 한 번의 수집에서 답글까지 받아올 스레드 수(rate limit 고려)
# 상태 파일 갱신 주기. heartbeat.STALE_AFTER_SECONDS(180초)보다 짧아야 한다.
HEARTBEAT_SECONDS = 60


# 채널 생성 직후 채널에 남기는 안내. 참여자 전원이 보므로 **사실만** 적는다.
#
# 자주 나오는 오해: "TYBot 으로 만든 채널만 수집된다". 사실이 아니다 -
# 수집 여부는 `channels.should_collect()`, 즉 **채널 이름**이 정한다(생성 경로가 아니라).
# 다만 비공개 채널은 봇이 스스로 들어갈 수 없다는 Slack 제약이 있어서,
# 결과적으로 `/채널` 로 만들거나 사람이 초대해야 수집이 시작된다. 그 차이를 그대로 적는다.
CHANNEL_CREATED_NOTICE = (
    "이 채널은 TYBot 수집 대상 표준({visibility})으로 만들어졌습니다. "
    "여기 올라오는 대화·스레드·첨부는 중앙 아카이브에 원문 그대로 쌓이고, "
    "권한이 있는 사람의 질문에 근거로 쓰입니다.\n"
    "• 수집 여부는 **채널 이름**이 정합니다. 규칙 밖 이름으로 바꾸면 그 시점부터 멈춥니다.\n"
    "• 두문자: 본부 > 팀 > 현장, 또는 실 > 팀. "
    "`업무` 는 다른 팀과 협업할 때 쓰는 채널이며 주관 팀의 조직코드를 씁니다.\n"
    "• 비공개 채널은 봇이 스스로 들어갈 수 없습니다. "
    "`/채널` 로 만들었거나 `/invite @{bot}` 한 채널만 수집됩니다.\n"
    "• 개인 인적사항·부동산 등본류·개인이 특정되는 목록은 올리지 마세요. "
    "아카이브에 저장되지 않도록 걸러지지만, "
    "Slack 대화에는 그대로 남습니다."
)


# 여러 채널을 한 번에 만든 뒤에만 붙인다.
#
# **사이드바 섹션은 봇이 만들어 줄 수 없다.** 섹션은 서버에 공유되는 값이 아니라
# 사람마다 자기 사이드바를 정리하는 개인 설정이고, 공개 API 에 그 메서드가 없다.
# 그래서 "생성 시 섹션 지정" 옵션을 두지 않고 방법만 알린다 —
# 동작하지 않는 옵션을 화면에 두는 것보다 한 줄 안내가 실제로 쓸모 있다.
SECTION_TIP = (
    "묶어서 보시려면 사이드바에서 채널을 우클릭 → *섹션으로 이동* → *새 섹션* 으로 "
    "한 번만 정리해 두시면 됩니다. "
    "섹션은 **사람마다 따로**라 각자 해야 하고, 플랜에 따라 메뉴가 없을 수 있습니다. "
    "이름 규칙이 같아 정리하지 않아도 사이드바에서 이웃해 보입니다."
)


BLANK = chr(10) * 2  # 문단 구분

# 처리한 메시지를 기억하는 개수. Slack 재전송은 몇 초 안에 오므로 이 정도면 넉넉하다.
SEEN_EVENTS = 500


def _slack_error(e: Exception) -> str:
    """Slack API 가 돌려준 오류 코드만 뽑는다.

    `SlackApiError` 는 문자열로 만들면 URL 과 응답 전문이 딸려 와 로그 한 줄이
    지저분해진다. 정작 필요한 건 `restricted_action_read_only_channel` 같은
    코드 하나다.
    """
    response = getattr(e, "response", None)
    if response is None:
        return "-"
    try:
        return str(response.get("error") or "-")
    except (AttributeError, TypeError):
        return "-"


def _clean(text: str) -> str:
    return MENTION_RE.sub("", text or "").strip()


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def schedule_blocks(body: str, *, share_payload: str = "") -> list[dict]:
    """일정 응답 블록. 본문 + '채널에 공유' 버튼.

    기본은 ephemeral 이고 공유는 사용자가 누를 때만 일어난다 - 일정 제목·장소가
    채널 대화에 섞이는 것을 기본값으로 두지 않는다.
    """
    out: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}}]
    if "보여드릴 수 있는 일정이 없습니다" in body or "준비되지 않았습니다" in body:
        return out
    button: dict = {
        "type": "button",
        "action_id": "tybot_schedule_share",
        "text": {"type": "plain_text", "text": "채널에 공유"},
    }
    # **빈 문자열을 넣으면 안 된다.** Slack 은 value 가 빈 버튼을 거부하고
    # 메시지 전체를 버린다. `/일정` 을 인수 없이 치면 share_payload 가 "" 라
    # 그 경우에만 답이 안 오는데, 서버 로그에는 조회 성공만 남아 원인을 찾기 어렵다.
    # 값이 없으면 키 자체를 넣지 않는다 — 받는 쪽은 없으면 기본 기간으로 읽는다.
    if share_payload.strip():
        button["value"] = share_payload[:200]
    out.append({"type": "actions", "elements": [button]})
    return out


def _scope_label(ctx: RequestContext | None) -> str:
    """감사 기록용 권한범위 표기 — 채널명은 남기지 않는다(로그 자체가 유출 경로가 되지 않게)."""
    if ctx is None:
        return "-"
    if ctx.channel_id or ctx.channel:
        return "현재 채널"
    if ctx.role == "exec":
        return "exec(전체)"
    return f"채널 {len(ctx.channels)}개"


CHANNEL_SCOPE_NOTICE = (
    "_조회 범위: 현재 채널만 · 여러 채널 통합 조회는 TYBot 개인 DM에서 요청하세요._"
)
MAX_THREAD_CONTEXT_CHARS = 6000


def _response_ts(response) -> str:
    try:
        return str(response.get("ts") or "")
    except (AttributeError, TypeError):
        return ""


class WorkspaceBot:
    """워크스페이스 1개에 대응하는 Socket Mode 봇.

    아카이브·감사기록·LLM 게이트웨이는 **모든 워크스페이스가 공유**한다.
    (아카이브는 디렉터리로, 조회 권한은 RequestContext 로 분리된다.)
    """

    def __init__(
        self,
        cfg: WorkspaceConfig,
        *,
        store: ArchiveStore,
        engine: AnswerEngine,
        qa_log: QALog,
        archive_dir: str,
    ) -> None:
        from slack_bolt import App

        self.cfg = cfg
        self.workspace = cfg.key
        self.archive_dir = archive_dir
        self.bot_name = os.getenv("BOT_NAME", "tybot")
        self.realtime = _truthy(os.getenv("REALTIME_INGEST", "1"))
        # 스레드 답글 대신 채널 본문에 답할지. 스레드가 기본인 이유는 채널 소음과
        # 자기 답변 재수집(요약 재귀) 위험을 줄이기 때문이다.
        self.reply_in_thread = _truthy(os.getenv("REPLY_IN_THREAD", "1"))
        # 채널 이름이 규칙에 맞으면 초대 없이 봇이 스스로 참여한다(공개 채널만).
        self.autojoin = _truthy(os.getenv("AUTOJOIN_CHANNELS", "1"))
        # 전 채널·전 워크스페이스 통합조회 허용 사용자. 채널 멤버십과 워크스페이스 경계를 모두 우회한다.
        self.exec_users = {
            u.strip() for u in (os.getenv("EXEC_USERS") or "").split(",") if u.strip()
        }
        # 봇이 가진 채널 관리 권한을 대신 사용할 수 있는 최소 별도 화이트리스트.
        # EXEC_USERS(전 자료 열람)와 섞지 않는다.
        self.channel_admin_users = {
            u.strip()
            for u in (os.getenv("CHANNEL_ADMIN_USERS") or "").split(",")
            if u.strip()
        }
        # 첫 사용 안내를 이미 받은 사람. 프로세스 안에서만 기억한다 -
        # 재시작 후 한 번 더 오는 것이, 파일을 새로 만들어 관리하는 것보다 낫다.
        self._welcomed: set[str] = set()
        self.app = App(token=cfg.bot_token)
        self.store = store
        self.engine = engine
        self.qa_log = qa_log
        self.feedback_log = FeedbackLog(qa_log.root)
        self._started = datetime.now(UTC)
        self._last_ingest_at: datetime | None = None
        self._ingested = 0
        self._user_cache: dict[str, str] = {}
        self._chan_cache: dict[str, str] = {}
        self.channel_owners = ChannelOwnerStore(
            heartbeat.state_dir() / "channel-owners.json"
        )
        self.path_problems: dict[str, str] = {}
        self._register()

    # --- Slack 조회 헬퍼 ---------------------------------------------------
    def _user_name(self, client, user_id: str) -> str:
        if user_id not in self._user_cache:
            try:
                info = client.users_info(user=user_id)["user"]
                self._user_cache[user_id] = (
                    info.get("profile", {}).get("real_name") or info.get("name") or user_id
                )
            except Exception:
                self._user_cache[user_id] = user_id
        return self._user_cache[user_id]

    def _channel_name(self, client, channel_id: str) -> str:
        if channel_id not in self._chan_cache:
            try:
                self._chan_cache[channel_id] = (
                    "#" + client.conversations_info(channel=channel_id)["channel"]["name"]
                )
            except Exception:
                return channel_id
        return self._chan_cache[channel_id]

    def _context(self, client, user_id: str) -> RequestContext:
        """권한 컨텍스트 — 답변 생성 **이전에** 검색 범위를 좁힌다."""
        if user_id in self.exec_users:
            log.info("exec 통합조회 user=%s", user_id)
            return RequestContext(workspace=self.workspace, role="exec")
        channels: set[str] = set()
        try:
            res = client.users_conversations(
                user=user_id, types="public_channel,private_channel", limit=1000
            )
            channels = {"#" + c["name"] for c in res.get("channels", [])}
        except Exception as e:
            log.warning("users.conversations 실패(%s) — 권한 범위 축소 폴백", e)
        return RequestContext(
            workspace=self.workspace,
            channels=frozenset(channels),
            readable_workspaces=self.cfg.readable,
            is_root=self.cfg.is_root,
        )

    def _request_context(
        self,
        client,
        user_id: str,
        *,
        channel_id: str = "",
        in_channel: bool = False,
    ) -> RequestContext:
        """질문 위치까지 포함한 권한 컨텍스트.

        채널 질문은 현재 채널 하나로 고정하고, DM만 사용자가 볼 수 있는 전체 범위를
        유지한다. exec/root도 채널 안에서는 이 제한을 우회하지 않는다.
        """
        base = self._context(client, user_id)
        if not in_channel:
            return base
        channel = self._channel_name(client, channel_id) if channel_id else ""
        return RequestContext(
            workspace=str(getattr(base, "workspace", self.workspace) or self.workspace),
            channels=frozenset(getattr(base, "channels", ()) or ()),
            role=str(getattr(base, "role", "member") or "member"),
            readable_workspaces=frozenset(
                getattr(base, "readable_workspaces", ()) or ()
            ),
            is_root=bool(getattr(base, "is_root", False)),
            channel_id=channel_id,
            channel=channel,
        )

    def _followup_resolver(self):
        """후속 질문 해석기. 스토어 하나를 공유하므로 봇당 하나만 만든다."""
        got = getattr(self, "_followup_resolver_obj", None)
        if got is None:
            from ..thread_followup import ThreadFollowupResolver

            got = ThreadFollowupResolver(self.store, archive_dir=self.archive_dir)
            self._followup_resolver_obj = got
        return got

    def _thread_turns(self, event: dict) -> list[dict]:
        """같은 스레드의 이전 TYBot 문답. 구조화된 turn 목록이다."""
        thread_ts = str(event.get("thread_ts") or "")
        channel_id = str(event.get("channel") or "")
        reader = getattr(self.qa_log, "context_for_thread", None)
        if not thread_ts or not channel_id or not callable(reader):
            return []
        try:
            return list(reader(self.workspace, channel_id, thread_ts) or [])
        except Exception as exc:
            log.warning("[%s] 스레드 문맥 조회 실패: %s", self.workspace, exc)
            return []

    @staticmethod
    def _thread_conversation_context(turns: list[dict]) -> str:
        """planner 에게 줄 **지칭 해석용** 문맥.

        예전에는 이전 봇 답변 전문을 최대 6,000자까지 이어 붙였다. 그 구조는 긴
        답변 하나가 앞선 관련 문답을 밀어내고, 밀려난 것은 **오류 없이** 사라졌다.
        더 나쁘게는 답변 문장이 다음 답의 재료가 됐다 — 요약을 근거로 요약하는
        길이다(원칙 1).

        지금 싣는 것은 질문과 작은 메타데이터다. 실제 근거는 좌표(`evidence_refs`)
        로 잇고, 그 좌표는 `ThreadFollowupResolver` 가 현재 권한으로 다시 연다.
        답변 문장은 좌표가 없는 **구형 레코드**에만 남아 있고, 그 값도 지칭어를
        푸는 데만 쓴다.
        """
        blocks: list[str] = []
        used = 0
        for turn in reversed(turns):
            bits = [f"이전 질문: {turn.get('question', '')}".strip()]
            topics = [str(t) for t in (turn.get("subject_terms") or []) if str(t).strip()]
            if topics:
                bits.append(f"이전 주제: {', '.join(topics[:6])}")
            refs = turn.get("evidence_refs") or []
            files = turn.get("attachment_refs") or []
            if refs or files:
                bits.append(f"이전 근거: 원문 {len(refs)}줄, 관련 파일 {len(files)}건")
            legacy = str(turn.get("legacy_answer") or "").strip()
            if legacy:
                bits.append(f"이전 봇 답변(지칭 해석 전용): {legacy}")
            block = "\n".join(b for b in bits if b)
            if not block:
                continue
            if used + len(block) > MAX_THREAD_CONTEXT_CHARS:
                break
            blocks.insert(0, block)
            used += len(block)
        return "\n\n".join(blocks)

    def autojoin_sweep(self) -> None:
        """규칙에 맞는 공개 채널에 자동 참여. 기동 시 1회."""
        if not self.autojoin:
            log.info("[%s] 자동 참여 비활성(AUTOJOIN_CHANNELS=0)", self.workspace)
            return
        try:
            r = sweep(self.app.client)
        except Exception as e:
            log.exception("[%s] 자동 참여 스윕 실패: %s", self.workspace, e)
            return
        log.info("[%s] %s", self.workspace, r.summary())

    # --- 핸들러 -----------------------------------------------------------
    def _register(self) -> None:
        @self.app.shortcut("create_work_channel")
        def on_create_shortcut(ack, body, client):
            ack()
            self._open_create_modal(
                client, body["trigger_id"], (body.get("user") or {}).get("id", "")
            )

        @self.app.command("/채널")
        @self.app.command("/ty-channel")
        def on_channel_command(ack, command, client, respond):
            ack()
            raw = (command.get("text") or "").strip()
            action = raw.replace(" ", "")
            # 인수를 받는 하위 명령은 **첫 낱말로** 판정한다. 공백을 지운 `action` 으로는
            # `검토자 @홍길동 09:00` 이 `검토자@홍길동09:00` 이 되어 맞지 않는다.
            head, _, args = raw.partition(" ")
            if action in ("", "생성", "만들기"):
                self._open_create_modal(client, command["trigger_id"], command["user_id"])
                return
            if action in ("상태", "점검", "health", "status"):
                self._respond_channel_health(client, command, respond)
                return
            # 이름 변경은 **수정으로 합쳤다.** 이름만 고치려는 사람도, 검토자만
            # 고치려는 사람도 같은 화면에서 한다 — 어디서 무엇을 고치는지 기억하게
            # 만들면 사람은 둘 다 안 고친다.
            if action in ("수정", "설정", "변경", "이름변경", "이름바꾸기", "고치기"):
                self._open_edit_modal(
                    client,
                    command["trigger_id"],
                    command["user_id"],
                    command.get("channel_id", ""),
                    respond,
                )
                return
            if head in ("검토자", "검토자지정", "요약검토자"):
                self._handle_reviewer_command(command, args, respond)
                return
            if head in ("담당자", "수정담당자", "채널담당자"):
                self._handle_channel_manager_command(command, args, respond)
                return
            respond(
                "사용법: `/채널 상태`, `/채널 수정`, `/채널 생성`, `/채널 담당자`, `/채널 도움말`\n"
                "`상태` 는 이름·봇 참여·수집·검토자·첨부를 한 번에 점검합니다.\n"
                "`수정` 에서 이름·검토자·수정 담당자를 함께 고칩니다.\n"
                "`담당자 @사람`은 기존 자동화용 예비 명령입니다.\n"
                "명령어 없이 `/채널`만 입력해도 생성 화면이 열립니다.",
                response_type="ephemeral",
            )

        @self.app.action(evidence_view.ACTION_SHOW)
        def on_show_evidence(ack, body, client, respond):
            """답변이 실제로 읽은 원문 줄을 보여준다.

            저장해 둔 것을 꺼내는 게 아니라 **같은 검색어로 지금 다시 찾는다.**
            그래서 권한도 지금 다시 판정된다 - 답변 뒤에 채널에서 나간 사람에게는
            근거가 보이지 않는다. 저장 방식이었다면 그대로 보였을 것이다.
            """
            ack()
            query = ((body.get("actions") or [{}])[0]).get("value") or ""
            user_id = (body.get("user") or {}).get("id", "")
            channel_id = str((body.get("channel") or {}).get("id") or "")
            respond(
                self._evidence_text(client, user_id, query, channel_id=channel_id),
                response_type="ephemeral",
            )

        @self.app.event("app_home_opened")
        def on_home_opened(event, client):
            """봇을 처음 여는 사람에게 예시 셋을 보낸다.

            `도움말` 은 명령 목록이고, 그것만으로는 "내 업무에 왜 쓸모 있나" 가
            답되지 않는다. 처음 만나는 사람에게 필요한 것은 예시다.

            **한 사람에게 한 번만** 보낸다 - 홈 탭은 자주 열리고, 매번 오면 소음이다.

            **홈 탭에서만 보낸다.** 이 이벤트는 `tab` 이 `messages` 일 때도 온다 —
            즉 사람이 봇 DM 을 **읽으려고 열 때도** 발생한다. 그래서 일정 알림 DM 을
            보러 들어간 사람에게 봇 소개가 따라붙었다(2026-09-11 실측). 알림을 읽으러
            온 사람에게 소개를 읽히는 것은 방해다.
            """
            if str(event.get("tab") or "") != "home":
                return
            user_id = str(event.get("user") or "")
            if not user_id or user_id in self._welcomed:
                return
            self._welcomed.add(user_id)
            self._notify_user(client, user_id, WELCOME.format(bot=self.bot_name))

        @self.app.command("/권한")
        @self.app.command("/ty-scope")
        def on_scope_command(ack, command, client, respond):
            """"내가 받는 답은 무엇을 근거로 하나" 를 사용자가 직접 확인한다.

            `/수집상태` 는 채널 하나를 답한다. 이건 사람 기준의 범위다.
            ephemeral 로만 답한다 - 남의 권한 범위가 채널에 보일 이유가 없다.
            """
            ack()
            respond(
                self._scope_report(client, command.get("user_id", "")),
                response_type="ephemeral",
            )

        @self.app.command("/수집상태")
        @self.app.command("/ty-collection")
        def on_collection_command(ack, command, client, respond):
            """이 채널이 수집되는지, 아니면 왜 안 되는지 그 자리에서 답한다.

            ephemeral 로만 답한다 - 채널에 봇 발언을 남기지 않는다(원칙 1과 같은 축).
            """
            ack()
            respond(
                self._collection_status(client, command.get("channel_id", "")),
                response_type="ephemeral",
            )

        @self.app.command("/피드백")
        @self.app.command("/ty-feedback")
        def on_feedback_command(ack, command, client, respond):
            """피드백의 **주 입구**.

            인수 없이 실행하면 선택 화면(모달)을 연다. 답변 메시지에 마우스를 올려
            이모지를 찾는 동작은 번거롭고 모바일에서는 더 그렇다. 리액션·`정정:` 답글도
            계속 동작하며, 셋 다 **같은 로그**에 쌓는다 - 입구마다 저장소가 갈라지면
            품질 검토에서 한쪽을 놓친다.
            """
            ack()
            text = (command.get("text") or "").strip()
            channel_id = command.get("channel_id", "")
            user_id = command.get("user_id", "")

            if text in ("도움말", "help", "?"):
                respond(SLASH_HELP.format(bot=self.bot_name), response_type="ephemeral")
                return
            if text:
                # 이미 적어서 보냈으면 한 번에 접수한다. 모달을 또 띄우지 않는다.
                respond(
                    self._record_slash_feedback(
                        user_id=user_id, channel_id=channel_id, text=text
                    ),
                    response_type="ephemeral",
                )
                return

            row = self.qa_log.last_answer_for_user(self.workspace, channel_id, user_id)
            asked = str((row or {}).get("question") or "").strip()
            metadata = json.dumps(
                {
                    "channel_id": channel_id,
                    "qa_record_id": str((row or {}).get("record_id") or ""),
                    "answer_ts": str((row or {}).get("response_ts") or ""),
                    "question": asked[:80],
                },
                ensure_ascii=False,
            )
            try:
                client.views_open(
                    trigger_id=command["trigger_id"],
                    view=feedback_modal(
                        metadata, target=f"`{asked[:80]}`" if asked else ""
                    ),
                )
            except Exception as e:
                log.warning("[%s] 피드백 모달 열기 실패: %s", self.workspace, e)
                respond(SLASH_HELP.format(bot=self.bot_name), response_type="ephemeral")

        @self.app.view("tybot_feedback")
        def on_feedback_submission(ack, body, client, view):
            kind, detail = feedback_from_view(view)
            # 👎·🔍 는 정정 사항이 있어야 접수한다. 내용 없는 신고는 만족도 숫자만
            # 낮추고 고칠 거리를 남기지 않는다. **ack 전에** 막아야 모달이 닫히지 않는다.
            errors = feedback_errors(kind, detail)
            if errors:
                ack(response_action="errors", errors=errors)
                return
            ack()
            meta = self._modal_metadata(view)
            user_id = (body.get("user") or {}).get("id", "")
            self.feedback_log.write(
                workspace=self.workspace,
                channel_id=str(meta.get("channel_id") or ""),
                qa_record_id=str(meta.get("qa_record_id") or ""),
                answer_ts=str(meta.get("answer_ts") or ""),
                actor=user_id,
                kind=kind,
                action="submitted",
                text=detail,
            )
            asked = str(meta.get("question") or "")
            self._notify_user(
                client,
                user_id,
                feedback_thanks(
                    kind,
                    linked=f"직전 질문(`{asked}`)에 연결했습니다." if asked else "",
                ),
            )

        # --- 일정 (/일정) ----------------------------------------------------
        @self.app.command("/일정")
        @self.app.command("/ty-schedule")
        def on_schedule_command(ack, command, client, respond):
            """그룹웨어 팀 일정 조회.

            ephemeral 로만 답한다. 채널에 뿌리면 일정 제목·장소가 대화에 섞이고,
            그 채널이 수집 대상이면 아카이브 금지 규칙(원칙 1·5)과 충돌한다.
            공유가 필요하면 아래 버튼으로 사용자가 명시적으로 올린다.
            """
            ack()
            text = (command.get("text") or "").strip()
            if text in ("도움말", "help", "?"):
                respond(SCHEDULE_HELP, response_type="ephemeral")
                return
            if text.replace(" ", "") in ("알림", "알림설정", "reminder", "dm"):
                self._schedule_reminder_panel(
                    respond, command.get("user_id", ""), client=client
                )
                return
            self._ensure_identity(client, command.get("user_id", ""))
            body = self._schedule_text(
                text,
                channel_id=command.get("channel_id", ""),
                user_id=command.get("user_id", ""),
            )
            # 블록이 거부되면 Slack 은 메시지를 통째로 버린다. 그러면 사용자에게는
            # "아무 반응 없음" 으로만 보이고 서버에는 조회 성공 로그만 남는다.
            # 최소한 글은 가게 물러선다.
            try:
                respond(
                    text=body,
                    response_type="ephemeral",
                    blocks=schedule_blocks(body, share_payload=text),
                )
            except Exception as e:
                log.warning("[%s] 일정 블록 응답 실패 — 글만 보냅니다: %s", self.workspace, e)
                respond(text=body, response_type="ephemeral")

        @self.app.action(schedule_dm.ACTION_ENABLE)
        def on_schedule_dm_enable(ack, body, respond):
            ack()
            self._set_schedule_dm(body, respond, minutes=None, turn_on=True)

        @self.app.action(schedule_dm.ACTION_OFF)
        def on_schedule_dm_off(ack, body, respond):
            ack()
            self._set_schedule_dm(body, respond, minutes=None, turn_on=False)

        @self.app.action(re.compile(rf"^{schedule_dm.ACTION_MINUTES}:"))
        def on_schedule_dm_minutes(ack, body, respond):
            """분 선택은 곧 '켜기' 다. 고른 뒤 다시 켜라고 시키면 한 동작이 늘어난다."""
            ack()
            raw = ((body.get("actions") or [{}])[0]).get("value") or ""
            picked = [int(x) for x in raw.split("-") if x.isdigit()]
            self._set_schedule_dm(body, respond, minutes=picked, turn_on=True)

        @self.app.options("org")
        def on_org_options(ack, payload, body):
            prefix = selected_prefix((body or {}).get("view") or {})
            ack(options=self._org_options(payload.get("value", ""), prefix=prefix))

        @self.app.action("tybot_schedule_share")
        def on_schedule_share(ack, body, client):
            """사용자가 누른 경우에만 채널에 올린다.

            봇 발언은 수집에서 제외되므로(`writer.ingest` 의 skipped_bot) 아카이브에는
            남지 않는다. 다만 채널 사람들에게는 보이므로 누른 사람을 함께 표기한다.
            """
            ack()
            user_id = (body.get("user") or {}).get("id", "")
            channel_id = ((body.get("channel") or {}).get("id")) or ""
            payload = ((body.get("actions") or [{}])[0]).get("value") or ""
            self._ensure_identity(client, user_id)
            shared = self._schedule_text(payload, channel_id=channel_id, user_id=user_id)
            try:
                client.chat_postMessage(
                    channel=channel_id,
                    text=f"<@{user_id}> 님이 공유한 일정\n{shared}",
                )
            except Exception as e:
                log.warning("[%s] 일정 공유 실패: %s", self.workspace, e)

        @self.app.action("prefix")
        def on_prefix_change(ack, body, client):
            """구분을 고르면 그 층의 **내 조직**으로 조직명·조직코드를 다시 채운다.

            `views_update` 로 화면을 다시 그린다. Slack 모달은 다른 입력을 보고 스스로
            바뀌지 못하므로, 서버가 새 화면을 만들어 보내는 것 말고 방법이 없다.
            이미 입력한 업무명은 그대로 되살린다.
            """
            ack()
            view = body.get("view") or {}
            user_id = (body.get("user") or {}).get("id", "")
            try:
                client.views_update(
                    view_id=view.get("id"),
                    hash=view.get("hash"),
                    view=create_modal(
                        view.get("private_metadata") or "{}",
                        prefix=action_prefix(body),
                        defaults=self._org_defaults(user_id, client=client),
                        task=typed_task(view),
                        # 다시 그릴 때도 기본값을 넘긴다. Slack 은 block_id 가 같으면
                        # 고른 값을 보존하므로 사람이 바꾼 검토자는 그대로 남는다.
                        default_reviewer=user_id,
                    ),
                )
            except Exception as e:
                log.warning("[%s] 구분 변경 반영 실패: %s", self.workspace, e)

        @self.app.view("tybot_create_channel")
        def on_create_submission(ack, body, client, view):
            user_id = (body.get("user") or {}).get("id", "")
            try:
                # 업무명을 여러 줄 적으면 그만큼 만든다. 조직·공개범위·참여자는 같다.
                requests = requests_from_view(view)
            except ChannelNameError as e:
                ack(response_action="errors", errors={e.block_id: str(e)})
                return
            ack()
            self._create_channels(client, user_id, requests)

        @self.app.view(EDIT_CALLBACK)
        def on_edit_submission(ack, body, client, view):
            """`/채널 수정` 제출. 채운 것만 바꾼다."""
            try:
                edit = edit_from_view(view)
            except ChannelNameError as e:
                ack(response_action="errors", errors={e.block_id: str(e)})
                return
            ack()
            metadata = self._modal_metadata(view)
            self._apply_channel_edit(
                client,
                (body.get("user") or {}).get("id", ""),
                metadata.get("channel_id", ""),
                edit,
            )

        # 이 화면은 더 이상 열리지 않는다 — `/채널 이름변경` 은 수정 모달로 간다.
        # 배포 순간에 이미 떠 있던 옛 모달의 제출만 받는다. 지우면 그 사람은
        # 저장을 눌렀는데 오류만 보게 된다.
        @self.app.view("tybot_rename_channel")
        def on_rename_submission(ack, body, client, view):
            try:
                request = request_from_view(view, include_channel_options=False)
            except ChannelNameError as e:
                ack(response_action="errors", errors={e.block_id: str(e)})
                return
            ack()
            metadata = self._modal_metadata(view)
            user_id = (body.get("user") or {}).get("id", "")
            self._rename_channel(client, user_id, metadata.get("channel_id", ""), request.name)

        # --- 투표 (/투표) ---------------------------------------------------
        @self.app.command("/투표")
        @self.app.command("/ty-poll")
        def on_poll_command(ack, command, client, respond):
            ack()
            text = (command.get("text") or "").strip()
            if text.replace(" ", "") in ("도움말", "help", "?"):
                respond(poll_help(), response_type="ephemeral")
                return
            channel_id = command.get("channel_id", "")
            channel_error = self._ensure_poll_channel(client, channel_id)
            if channel_error:
                respond(channel_error, response_type="ephemeral")
                return
            # 명령 뒤에 적은 문장은 질문 칸에 미리 채워 준다. 두 번 입력하지 않게.
            try:
                client.views_open(
                    trigger_id=command["trigger_id"],
                    view=poll_create_modal(
                        channel_id=channel_id, prefill_question=text
                    ),
                )
            except Exception as e:
                log.warning("[%s] 투표 모달 열기 실패: %s", self.workspace, e)
                respond(
                    "투표 화면을 열지 못했습니다. 잠시 후 다시 시도해 주세요.",
                    response_type="ephemeral",
                )

        @self.app.view(POLL_MODAL)
        def on_poll_submission(ack, body, client, view):
            fields = read_poll_modal(view)
            user_id = (body.get("user") or {}).get("id", "")
            try:
                poll = create_poll(workspace=self.workspace, creator=user_id, **fields)
            except PollError as e:
                # 입력 오류는 모달 안에서 알려 준다. 창이 닫히면 적은 내용이 사라진다.
                ack(response_action="errors", errors={e.block_id or "question": str(e)})
                return
            ack()
            self._publish_poll(client, poll)

        @self.app.action(re.compile(rf"^{ACTION_VOTE}:\d+$"))
        def on_poll_vote(ack, body, client, action):
            ack()
            self._handle_vote(client, body, action)

        @self.app.action(ACTION_RESULTS)
        def on_poll_results(ack, body, client, action):
            ack()
            poll = load_poll(self.workspace, str(action.get("value") or ""))
            user_id = (body.get("user") or {}).get("id", "")
            if poll is None:
                self._poll_notice(client, body, user_id, "투표를 찾을 수 없습니다.")
                return
            self._poll_notice(client, body, user_id, private_results(poll, user_id))

        @self.app.action(ACTION_CLOSE)
        def on_poll_close(ack, body, client, action):
            ack()
            user_id = (body.get("user") or {}).get("id", "")
            poll = load_poll(self.workspace, str(action.get("value") or ""))
            if poll is None:
                self._poll_notice(client, body, user_id, "투표를 찾을 수 없습니다.")
                return
            try:
                close_poll(poll, user_id, is_admin=user_id in self.channel_admin_users)
            except PollError as e:
                self._poll_notice(client, body, user_id, str(e))
                return
            save_poll(poll)
            self._refresh_poll(client, poll)
            self._poll_notice(client, body, user_id, "투표를 마감했습니다.")

        @self.app.event("channel_created")
        def on_channel_created(event, client):
            joined = on_channel_event(
                client, event.get("channel", {}), enabled=self.autojoin
            )
            if joined:
                log.info("[%s] 새 채널 수집 시작: %s", self.workspace, joined)

        @self.app.event("channel_rename")
        def on_channel_renamed(event, client):
            # 이름을 규칙에 맞게 고친 순간부터 수집 대상이 된다.
            joined = on_channel_event(
                client, event.get("channel", {}), enabled=self.autojoin
            )
            if joined:
                log.info("[%s] 이름 변경으로 수집 시작: %s", self.workspace, joined)

        @self.app.event("app_mention")
        def on_mention(event, client, say):
            if event.get("bot_id"):
                return
            if self._handle_correction(event, say):
                return
            self._handle(event, client, say, in_channel=True)

        @self.app.event("message")
        def on_message(event, client, say):
            if event.get("bot_id"):
                return  # 1겹: 봇 출력은 아카이브 대상 아님
            # 첨부만 올린 메시지는 subtype=file_share 로 온다 - 이건 수집한다.
            if event.get("subtype") not in (None, "file_share"):
                return  # 입퇴장·핀 등 시스템 메시지 제외
            ctype = event.get("channel_type")
            if ctype == "im":
                if self._handle_correction(event, say):
                    return
                self._handle(event, client, say, in_channel=False)
                return
            if ctype in ("channel", "group") and self.realtime:
                self._ingest_live(client, event)

    def _schedule_text(self, text: str, *, channel_id: str, user_id: str) -> str:
        """`/일정` 본문. 제목·장소는 여기서만 다루고 로그에는 남기지 않는다."""
        window = parse_window(text)
        if window is None:
            return unknown_window(text)

        with db_connect() as conn:
            if conn is None:
                return SCHEDULE_UNAVAILABLE
            try:
                rows, scope = schedule_fetch(
                    conn,
                    workspace=self.workspace,
                    channel_id=channel_id,
                    slack_user=user_id,
                    window=window,
                )
                synced_at = schedule_last_sync(conn)
            except Exception as e:
                log.warning("[%s] 일정 조회 실패: %s", self.workspace, e)
                return (
                    "일정을 조회하지 못했습니다. 잠시 뒤 다시 시도해 주세요.\n"
                    "계속되면 관리자에게 알려 주세요."
                )

        # 건수·범위만 남긴다. 제목·장소는 로그에 넣지 않는다(설계 문서 §2).
        log.info(
            "[%s] 일정 조회 scope=%s window=%s rows=%d folders=%s",
            self.workspace,
            scope,
            window.label,
            len(rows),
            sorted({r.source_folder_id for r in rows}),
        )
        return schedule_reply(rows, window=window, scope=scope, synced_at=synced_at)

    def _schedule_reminder_panel(self, respond, user_id: str, *, client=None) -> None:
        """현재 개인 일정 알림 설정을 ephemeral 화면으로 보여준다."""
        self._ensure_identity(client, user_id)
        with db_connect() as conn:
            if conn is None:
                respond(schedule_dm.UNAVAILABLE, response_type="ephemeral")
                return
            try:
                emp_no = schedule_dm.resolve_emp_no(
                    conn, workspace=self.workspace, slack_user=user_id
                )
                if not emp_no:
                    respond(schedule_dm.NEED_IDENTITY, response_type="ephemeral")
                    return
                pref = schedule_dm.get_preference(conn, emp_no)
            except Exception as e:
                log.warning("[%s] 일정 알림 설정 조회 실패: %s", self.workspace, e)
                respond(schedule_dm.UNAVAILABLE, response_type="ephemeral")
                return

        respond(
            text="일정 알림 설정",
            blocks=schedule_dm.settings_blocks(
                pref, workspace_label=pref.workspace if pref and pref.enabled else ""
            ),
            response_type="ephemeral",
        )

    def _set_schedule_dm(self, body: dict, respond, *, minutes, turn_on: bool) -> None:
        """버튼으로 개인 일정 알림 설정을 바꾸고 같은 메시지를 갱신한다."""
        user_id = str((body.get("user") or {}).get("id") or "")
        entitled: int | None = None
        with db_connect() as conn:
            if conn is None:
                respond(text=schedule_dm.UNAVAILABLE, replace_original=True)
                return
            try:
                emp_no = schedule_dm.resolve_emp_no(
                    conn, workspace=self.workspace, slack_user=user_id
                )
                if not emp_no:
                    respond(text=schedule_dm.NEED_IDENTITY, replace_original=True)
                    return

                previous = schedule_dm.get_preference(conn, emp_no)
                if turn_on:
                    selected = minutes or (previous.minutes if previous else schedule_dm.DEFAULT_MINUTES)
                    pref = schedule_dm.enable(
                        conn,
                        emp_no=emp_no,
                        workspace=self.workspace,
                        slack_user=user_id,
                        minutes=selected,
                    )
                else:
                    schedule_dm.disable(conn, emp_no=emp_no, actor=user_id)
                    pref = schedule_dm.get_preference(conn, emp_no)
            except Exception as e:
                log.warning("[%s] 일정 알림 설정 변경 실패: %s", self.workspace, e)
                respond(text=schedule_dm.UNAVAILABLE, replace_original=True)
                return

            # 켰다고 오는 것이 아니다. 승인된 폴더가 없으면 영원히 0건인데, 화면은
            # 켜졌다고만 말한다 — 그 사람은 오지 않는 알림을 기다리고 봇을 안 믿게
            # 된다. 같은 연결 안에서 자격을 확인해 사실을 함께 말한다.
            if turn_on:
                try:
                    entitled = schedule_dm.entitled_folders(conn, emp_no)
                except Exception as e:
                    log.warning("[%s] 일정 폴더 자격 확인 실패: %s", self.workspace, e)
                    entitled = None

        blocks = schedule_dm.settings_blocks(
            pref, workspace_label=pref.workspace if pref and pref.enabled else ""
        )
        if (
            turn_on
            and previous
            and previous.enabled
            and previous.workspace != self.workspace
        ):
            blocks.insert(1, {
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": schedule_dm.moved_notice(previous.workspace),
                }],
            })
        if turn_on and entitled == 0:
            blocks.insert(1, {
                "type": "section",
                "text": {"type": "mrkdwn", "text": schedule_dm.NOT_ENTITLED},
            })
        respond(text="일정 알림 설정", blocks=blocks, replace_original=True)

    def _evidence_lines(
        self, client, user_id: str, query: str, *, channel_id: str = ""
    ):
        """지금 이 사람 권한으로 다시 찾은 근거 줄."""
        ctx = self._request_context(
            client,
            user_id,
            channel_id=channel_id,
            in_channel=bool(channel_id and not channel_id.startswith("D")),
        )
        return [
            evidence_view.EvidenceLine(
                channel=h.doc.channel,
                ts=h.line.ts,
                speaker=h.line.speaker,
                text=h.line.text,
                workspace=h.doc.workspace,
            )
            for h in self.store.search(query, ctx, limit=40)
        ]

    def _evidence_text(
        self, client, user_id: str, query: str, *, channel_id: str = ""
    ) -> str:
        """`근거 보기` 본문. 원문은 손대지 않고 그대로 보여준다."""
        if not (query or "").strip():
            return evidence_view.NO_EVIDENCE
        try:
            lines = self._evidence_lines(
                client, user_id, query, channel_id=channel_id
            )
        except Exception as e:
            log.warning("[%s] 근거 조회 실패: %s", self.workspace, e)
            return "근거를 다시 찾지 못했습니다. 잠시 뒤 다시 시도해 주세요."
        # 검색어와 건수만 남긴다. 원문 줄은 로그에 넣지 않는다.
        log.info(
            "[%s] 근거 보기 user=%s q=%r lines=%d",
            self.workspace, user_id, query[:40], len(lines),
        )
        return evidence_view.report(lines, query=query, own_workspace=self.workspace)

    def _scope_report(self, client, user_id: str) -> str:
        """`/권한` 본문. 범위와 건수만 보여주고 채널 이름은 보여주지 않는다.

        채널명에는 조직·업무·현장이 들어 있어 그 자체가 노출이다. 여기서 필요한 것은
        "얼마나 넓은가" 이지 "무엇이 있나" 가 아니다.
        """
        facts = ScopeFacts(
            workspace_label=self.cfg.label,
            workspace_key=self.workspace,
            is_root=bool(self.cfg.is_root),
            is_exec=user_id in self.exec_users,
            readable=sorted(self.cfg.readable or ()),
        )
        try:
            res = client.users_conversations(
                user=user_id, types="public_channel,private_channel", limit=1000
            )
            names = ["#" + c["name"] for c in res.get("channels", [])]
        except Exception as e:
            log.warning("[%s] 권한 범위 조회 실패: %s", self.workspace, e)
            facts.lookup_failed = True
            return scope_text(facts)

        facts.collected_channels = sum(1 for n in names if should_collect(n))
        facts.uncollected_channels = len(names) - facts.collected_channels

        # 실제로 답변 근거가 될 수 있는 양. 권한 필터를 그대로 통과시킨다 -
        # 여기 숫자와 실제 답변의 근거가 다르면 이 화면이 거짓말이 된다.
        ctx = self._context(client, user_id)
        docs = self.store.visible_docs(ctx)
        facts.visible_docs = len(docs)
        facts.visible_lines = sum(len(d.raw_lines) for d in docs)
        log.info(
            "[%s] 권한 범위 조회 user=%s 채널=%d 문서=%d",
            self.workspace, user_id, facts.collected_channels, facts.visible_docs,
        )
        return scope_text(facts)

    def _collection_status(self, client, channel_id: str) -> str:
        """`/수집상태` 가 보여줄 문장. 사실 수집만 하고 판단은 collection_status 가 한다."""
        channel = self._channel_name(client, channel_id)
        info = {}
        try:
            info = (client.conversations_info(channel=channel_id) or {}).get("channel", {})
        except Exception as e:
            log.warning("[%s] 채널 정보 조회 실패 ch=%s: %s", self.workspace, channel_id, e)

        doc = next((d for d in self.store.docs() if d.channel == channel), None)
        return collection_report(
            ChannelFacts(
                channel=channel,
                is_private=bool(info.get("is_private")),
                is_member=bool(info.get("is_member")),
                is_dm=bool(info.get("is_im")) or channel_id.startswith("D"),
                autojoin_enabled=self.autojoin,
                realtime_enabled=self.realtime,
                bot_name=self.bot_name,
                raw_lines=len(doc.raw_lines) if doc else 0,
                last_ingested=doc.last_ingested if doc else None,
                write_problems=dict(self.path_problems),
            )
        )

    def _record_slash_feedback(self, *, user_id: str, channel_id: str, text: str) -> str:
        """`/피드백 <내용>` 을 기록하고 사용자에게 확인을 돌려준다."""
        if not text:
            return SLASH_HELP.format(bot=self.bot_name)

        row = self.qa_log.last_answer_for_user(self.workspace, channel_id, user_id)
        record_id = str(row.get("record_id") or "") if row else ""
        self.feedback_log.write(
            workspace=self.workspace,
            channel_id=channel_id,
            qa_record_id=record_id,
            answer_ts=str(row.get("response_ts") or "") if row else "",
            actor=user_id,
            kind="correction",
            action="submitted",
            text=text,
        )
        # 연결할 답변이 없으면 그 사실을 말한다 - 검토자가 재현하지 못하기 때문이다.
        asked = str(row.get("question") or "").strip() if row else ""
        linked = f"직전 질문(`{asked[:60]}`)에 연결했습니다." if record_id and asked else ""
        return feedback_thanks("correction", linked=linked)

    def _handle_correction(self, event: dict, say) -> bool:
        text = correction_text(_clean(event.get("text", "")))
        if text is None:
            return False
        thread_ts = str(event.get("thread_ts") or "")
        if not thread_ts:
            say(text="정정할 TYBot 답변의 스레드에서 `@tybot 정정: 올바른 내용`으로 남겨주세요.")
            return True
        if not text:
            say(text="`정정:` 뒤에 올바른 내용을 적어주세요.", thread_ts=thread_ts)
            return True
        channel_id = str(event.get("channel") or "")
        row = self.qa_log.find_answer(
            self.workspace, channel_id, thread_ts=thread_ts
        )
        if not row or not row.get("record_id"):
            say(text="이 스레드에서 연결할 TYBot 답변 기록을 찾지 못했습니다.", thread_ts=thread_ts)
            return True
        self.feedback_log.write(
            workspace=self.workspace,
            channel_id=channel_id,
            qa_record_id=str(row["record_id"]),
            answer_ts=str(row.get("response_ts") or ""),
            actor=str(event.get("user") or ""),
            kind="correction",
            action="submitted",
            text=text,
        )
        say(text="정정 의견을 기록했습니다. 답변 품질 검토에 반영하겠습니다.", thread_ts=thread_ts)
        return True

    def _modal_metadata(self, view: dict) -> dict:
        try:
            value = json.loads(view.get("private_metadata") or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _ensure_identity(self, client, user_id: str) -> str | None:
        """회사 이메일로 Slack 사용자와 사번을 지연 연결한다."""
        if client is None or not user_id:
            return None
        with db_connect() as conn:
            if conn is None:
                return None
            try:
                return ensure_identity(
                    conn, client, workspace=self.workspace, slack_user=user_id
                )
            except Exception as e:
                log.warning("[%s] 이메일 사번 매핑 실패 user=%s: %s", self.workspace, user_id, e)
                return None

    def _org_defaults(self, user_id: str, *, client=None) -> dict:
        """구분 → 그 층의 내 조직. 모달의 조직명·조직코드 **기본값**이다.

        강제하지 않는다. 조직도를 못 읽거나 계정 연결이 없으면 빈 값으로 두고,
        사람이 직접 입력한다 — 채널 생성이 DB 가용성에 묶이면 안 된다.
        """
        if not user_id:
            return {}
        self._ensure_identity(client, user_id)
        with db_connect() as conn:
            if conn is None:
                return {}
            try:
                chain = my_org_chain(
                    conn, workspace=self.workspace, slack_user=user_id
                )
            except Exception as e:
                log.warning("[%s] 소속 조직 조회 실패: %s", self.workspace, e)
                return {}
        if not chain:
            log.info("[%s] 소속 조직을 찾지 못했다 user=%s", self.workspace, user_id)
            return {}
        return defaults_by_prefix(chain)

    def _org_options(self, query: str, *, prefix: str) -> list[dict]:
        """조직 검색 결과를 선택한 구분에 맞게 좁힌다."""
        with db_connect() as conn:
            if conn is None:
                return [notice_option(ORG_UNAVAILABLE)]
            try:
                hits = org_search(conn, query)
            except Exception as e:
                log.warning("[%s] 조직 검색 실패: %s", self.workspace, e)
                return [notice_option(ORG_UNAVAILABLE)]

        if prefix == "업무":
            hits = [hit for hit in hits if hit.prefix in ("본사팀", "현장")]
        elif prefix:
            hits = [hit for hit in hits if hit.prefix == prefix]
        if not hits:
            return [notice_option(ORG_NO_MATCH)]
        return org_options(hits)

    def _open_create_modal(self, client, trigger_id: str, user_id: str) -> None:
        metadata = json.dumps({"user_id": user_id}, ensure_ascii=False)
        try:
            client.views_open(
                trigger_id=trigger_id,
                view=create_modal(
                    metadata,
                    defaults=self._org_defaults(user_id, client=client),
                    # 기본 검토자는 **만든 사람 자신**이다. 빈 칸으로 열면 사람이
                    # 누구를 넣어야 할지 몰라 아무나 넣거나 그냥 닫는다.
                    default_reviewer=user_id,
                ),
            )
        except Exception as e:
            log.warning("[%s] 채널 생성 모달 열기 실패: %s", self.workspace, e)

    # --- 투표 -------------------------------------------------------------
    def _ensure_poll_channel(self, client, channel_id: str) -> str:
        """투표를 게시할 채널에 접근할 수 있게 하고, 불가능하면 사용자 안내를 반환한다."""
        if not channel_id:
            return "현재 채널을 확인하지 못했습니다. 채널에서 다시 실행해 주세요."
        try:
            result = client.conversations_info(channel=channel_id)
            channel = result.get("channel") or {}
            if channel.get("is_member"):
                return ""
            if channel.get("is_private"):
                return (
                    "이 비공개 채널에 TYBot이 참여하지 않았습니다. "
                    f"`/invite @{self.bot_name}` 후 다시 실행해 주세요."
                )
            client.conversations_join(channel=channel_id)
            return ""
        except Exception as e:
            # 비공개 채널의 비멤버에게 conversations.info는 channel_not_found를 돌려준다.
            log.warning(
                "[%s] 투표 대상 채널 접근 실패 channel=%s: %s",
                self.workspace,
                channel_id,
                e,
            )
            return (
                "TYBot이 이 채널에 접근할 수 없습니다. 비공개 채널이면 "
                f"`/invite @{self.bot_name}` 후 다시 실행해 주세요."
            )

    def _publish_poll(self, client, poll) -> None:
        """투표를 채널에 올리고 메시지 위치를 기억한다.

        메시지 위치(ts)를 저장하는 이유: 누가 투표할 때마다 **같은 메시지를 갱신**해야 한다.
        새 메시지를 계속 올리면 채널이 투표 알림으로 도배된다.
        """
        try:
            posted = client.chat_postMessage(
                channel=poll.channel_id,
                text=poll_fallback(poll),
                blocks=poll_blocks(poll),
            )
            poll.message_ts = posted.get("ts")
        except Exception as e:
            log.warning("[%s] 투표 게시 실패: %s", self.workspace, e)
            self._notify_user(
                client,
                poll.creator,
                (
                    "투표를 올리지 못했습니다. 이 채널에 TYBot이 참여 중인지 확인한 뒤 "
                    f"필요하면 `/invite @{self.bot_name}` 해 주세요."
                ),
            )
            return
        try:
            save_poll(poll)
        except PollError as e:
            log.error("[%s] 투표 저장 실패: %s", self.workspace, e)

    def _refresh_poll(self, client, poll) -> None:
        """올려 둔 투표 메시지를 새 결과로 바꿔 그린다."""
        if not poll.message_ts:
            return
        try:
            client.chat_update(
                channel=poll.channel_id,
                ts=poll.message_ts,
                text=poll_fallback(poll),
                blocks=poll_blocks(poll),
            )
        except Exception as e:
            log.warning("[%s] 투표 메시지 갱신 실패: %s", self.workspace, e)

    def _poll_notice(self, client, body, user_id: str, message: str) -> None:
        """누른 사람에게만 보이는 안내. 채널을 어지럽히지 않는다."""
        channel = ((body.get("channel") or {}).get("id")) or ""
        if not channel or not user_id:
            return
        with contextlib.suppress(Exception):
            client.chat_postEphemeral(channel=channel, user=user_id, text=message)

    def _handle_vote(self, client, body, action) -> None:
        user_id = (body.get("user") or {}).get("id", "")
        raw = str(action.get("value") or "")
        poll_id, _, index_text = raw.partition(":")
        poll = load_poll(self.workspace, poll_id)
        if poll is None:
            self._poll_notice(client, body, user_id, "투표를 찾을 수 없습니다. 이미 지워졌을 수 있습니다.")
            return
        try:
            message = apply_vote(poll, user_id, int(index_text))
        except (PollError, ValueError) as e:
            self._poll_notice(client, body, user_id, str(e))
            # 마감된 투표를 눌렀다면 메시지가 낡은 것이므로 다시 그려 준다
            if isinstance(e, PollError) and not poll.is_open():
                self._refresh_poll(client, poll)
            return
        try:
            save_poll(poll)
        except PollError as e:
            self._poll_notice(client, body, user_id, str(e))
            return
        self._refresh_poll(client, poll)
        self._poll_notice(client, body, user_id, message)

    def _can_manage_channel(self, channel_id: str, user_id: str) -> bool:
        """이 채널을 고칠 수 있는가.

        ## Slack 생성자까지 본다 (2026-09-08)

        전에는 **TYBot 이 만든 채널**만 고칠 수 있었다(`channel_owners` 는 우리
        생성 기록이다). 그래서 A 가 Slack 에서 직접 만든 채널을 A 가 고치려 하면
        「TYBot 이 만든 게 아니라서 안 된다」 고 막혔다. 자기가 만든 채널을
        자기가 못 고치는 것은 권한이 아니라 고장이다.

        Slack 이 알려 주는 `channel.creator` 는 우리 JSON 기록보다 더 확실한
        사실이다. 그것을 마지막 근거로 쓴다 — 권한을 넓히는 것이 아니라, 우리가
        기록을 놓친 자리를 Slack 에게 되묻는 것이다.
        """
        if user_id in self.channel_admin_users:
            return True
        if self.channel_owners.is_manager(self.workspace, channel_id, user_id):
            return True
        if bool(user_id) and self._slack_creator(channel_id) == user_id:
            return True
        return self._is_workspace_admin(user_id)

    def _can_delegate_channel_manager(self, channel_id: str, user_id: str) -> bool:
        """이 채널의 TYBot 수정 담당자를 지정할 수 있는가.

        위임받은 담당자에게 재위임 권한까지 주면 권한이 연쇄 확장된다. 채널을
        고칠 권한과 다른 사람에게 그 권한을 줄 권한을 분리한다.
        """
        if user_id in self.channel_admin_users:
            return True
        if self.channel_owners.is_owner(self.workspace, channel_id, user_id):
            return True
        if bool(user_id) and self._slack_creator(channel_id) == user_id:
            return True
        return self._is_workspace_admin(user_id)

    def _is_workspace_admin(self, user_id: str) -> bool:
        """Slack이 공개 API로 확인해 주는 Workspace Admin/Owner인가.

        채널별 Channel Manager 목록은 bot Web API로 조회할 수 없다. 그 역할은
        `/채널 담당자`로 TYBot에 한 번 위임한다. 반면 workspace admin/owner는
        `users.info`의 안정된 필드로 확인할 수 있으므로 별도 환경변수가 필요 없다.
        """
        if not user_id:
            return False
        cache = getattr(self, "_workspace_admin_cache", None)
        if cache is None:
            cache = self._workspace_admin_cache = {}
        if user_id in cache:
            return cache[user_id]
        try:
            user = (self.app.client.users_info(user=user_id) or {}).get("user") or {}
            allowed = bool(
                user.get("is_admin")
                or user.get("is_owner")
                or user.get("is_primary_owner")
            )
        except Exception as e:  # 조회 실패가 권한을 넓히면 안 된다
            log.warning("[%s] 워크스페이스 관리자 조회 실패 user=%s: %s", self.workspace, user_id, e)
            return False
        cache[user_id] = allowed
        return allowed

    def _slack_creator(self, channel_id: str) -> str:
        """Slack 이 기록한 채널 생성자. 못 읽으면 빈 문자열.

        권한 판정마다 API 를 부르지 않게 캐시한다. 생성자는 바뀌지 않는다.
        """
        if not channel_id:
            return ""
        cache = getattr(self, "_creator_cache", None)
        if cache is None:
            cache = self._creator_cache = {}
        if channel_id in cache:
            return cache[channel_id]
        try:
            info = (
                self.app.client.conversations_info(channel=channel_id) or {}
            ).get("channel") or {}
            creator = str(info.get("creator") or "")
        except Exception as e:  # 못 읽으면 권한을 넓히지 않는다
            log.warning("[%s] 채널 생성자 조회 실패 ch=%s: %s", self.workspace, channel_id, e)
            # 실패는 캐시하지 않는다. 일시 오류가 그 채널을 영구히 잠근다.
            return ""
        cache[channel_id] = creator
        return creator

    def _handle_reviewer_command(self, command: dict, args: str, respond) -> None:
        """`/채널 검토자 @사람 [@사람2] [09:00]` — 이 채널의 요약 검토자를 정한다.

        요약은 봇이 후보만 만들고 이 사람이 확정한다.
        설계: docs/design/summary-review.md

        **채널 소유자만** 바꿀 수 있다. 아무나 자기를 검토자로 넣으면, 요약 반영이
        곧 그 사람 재량이 된다 — 이름변경과 같은 권한선이다.
        """
        channel_id = str(command.get("channel_id") or "")
        user_id = str(command.get("user_id") or "")
        if not channel_id:
            respond("채널 안에서 실행해 주세요.", response_type="ephemeral")
            return

        users = _mentioned_users(args)
        if not users and not _asks_to_clear(args):
            self._show_reviewers(channel_id, respond)
            return

        if not self._can_manage_channel(channel_id, user_id):
            respond(
                "이 채널의 최초 생성 요청자 또는 TYBot 채널 관리자만 검토자를 정할 수 있습니다.",
                response_type="ephemeral",
            )
            return

        try:
            send_at = reviewers.parse_send_at(_time_token(args))
            rows = reviewers.set_reviewers(
                workspace=self.workspace,
                channel_id=channel_id,
                channel_name=str(command.get("channel_name") or ""),
                reviewer_users=users,
                send_at=send_at,
                set_by=user_id,
            )
        except reviewers.ReviewerError as e:
            respond(f"검토자를 저장하지 못했습니다: {e}", response_type="ephemeral")
            return

        if not rows:
            respond(
                "검토자를 모두 해제했습니다. **이 채널은 요약을 반영하지 않습니다.**"
                + NEWLINE
                + "원문 수집은 그대로 계속됩니다.",
                response_type="ephemeral",
            )
            return
        names = ", ".join(f"<@{r.reviewer_user}>" for r in rows)
        respond(
            f"검토자: {names}" + NEWLINE
            + f"매일 {send_at.strftime('%H:%M')} 에 요약 후보를 DM 으로 보냅니다." + NEWLINE
            + "검토자가 확인한 것만 요약에 반영됩니다.",
            response_type="ephemeral",
        )

    def _show_reviewers(self, channel_id: str, respond) -> None:
        """지금 누가 검토자인지. 인수 없이 부르면 이것이 나온다."""
        try:
            rows = reviewers.reviewers_for(self.workspace, channel_id)
        except reviewers.ReviewerError as e:
            respond(f"검토자를 읽지 못했습니다: {e}", response_type="ephemeral")
            return
        if not rows:
            respond(
                "이 채널에는 요약 검토자가 없습니다 — **요약을 반영하지 않습니다.**" + NEWLINE
                + "정하기: `/채널 검토자 @사람 09:00`" + NEWLINE
                + "여러 명도 됩니다. 해제: `/채널 검토자 없음`",
                response_type="ephemeral",
            )
            return
        names = ", ".join(f"<@{r.reviewer_user}>" for r in rows)
        respond(
            f"검토자: {names} (매일 {rows[0].send_at.strftime('%H:%M')})" + NEWLINE
            + "바꾸기: `/채널 검토자 @사람 09:00`",
            response_type="ephemeral",
        )

    def _handle_channel_manager_command(self, command: dict, args: str, respond) -> None:
        """`/채널 담당자 @사람` — TYBot의 채널 수정 권한을 위임한다.

        Slack의 채널별 Channel Manager는 bot token에 권한이 위임되지 않고 그 목록도
        공개 Web API로 조회할 수 없다. 개설자, Workspace Admin/Owner 또는 전역 TYBot
        채널 관리자가 이 명령으로 같은 권한을 명시적으로 연결한다.
        """
        channel_id = str(command.get("channel_id") or "")
        user_id = str(command.get("user_id") or "")
        if not channel_id:
            respond("채널 안에서 실행해 주세요.", response_type="ephemeral")
            return

        users = _mentioned_users(args)
        clearing = _asks_to_clear(args)
        if args.strip() and not users and not clearing:
            respond(
                "사용자를 식별하지 못했습니다. `/채널 수정`을 열어 "
                "`채널 수정 담당자`에서 사용자를 선택해 주세요. "
                "예비 명령을 쓸 때는 Slack 자동완성 목록에서 사람을 선택해야 합니다.",
                response_type="ephemeral",
            )
            return
        if not users and not clearing:
            owner = self.channel_owners.owner_of(self.workspace, channel_id)
            managers = self.channel_owners.managers_of(self.workspace, channel_id)
            lines = [f"개설자: <@{owner}>" if owner else "개설자 기록: 없음"]
            lines.append(
                "TYBot 수정 담당자: " + " ".join(f"<@{uid}>" for uid in managers)
                if managers
                else "TYBot 수정 담당자: 없음"
            )
            lines.append("변경: `/채널 수정` · 예비 명령: `/채널 담당자 @사람`")
            respond(NEWLINE.join(lines), response_type="ephemeral")
            return

        if not self._can_delegate_channel_manager(channel_id, user_id):
            respond(
                "이 채널의 개설자 또는 Workspace Admin만 "
                "담당자를 지정할 수 있습니다. Slack의 채널별 Channel Manager 역할은 "
                "봇 API에 전달되지 않으므로 개설자나 Workspace Admin에게 연결을 요청해 주세요.",
                response_type="ephemeral",
            )
            return

        managers = self.channel_owners.set_managers(
            self.workspace,
            channel_id,
            [] if clearing else users,
            set_by=user_id,
        )
        if not managers:
            respond(
                "TYBot 수정 담당자를 모두 해제했습니다. 개설자와 Workspace Admin의 권한은 유지됩니다.",
                response_type="ephemeral",
            )
            return
        respond(
            "TYBot 수정 담당자: " + " ".join(f"<@{uid}>" for uid in managers)
            + NEWLINE
            + "이제 해당 사용자는 `/채널 수정`을 사용할 수 있습니다.",
            response_type="ephemeral",
        )

    # --- `/채널 상태` · `/채널 수정` -------------------------------------
    def _health_facts(self, client, channel_id: str, viewer: str = "") -> HealthFacts:
        """상태 화면이 쓸 사실을 모은다. **판단은 `channel_health` 가 한다.**

        한 조각을 못 읽었다고 화면 전체를 포기하지 않는다 — 못 읽은 것은
        `None` 으로 넘겨 「모른다」 로 표시된다. 「없음」 과 다른 사실이다.
        """
        channel = self._channel_name(client, channel_id)
        info = {}
        try:
            info = (client.conversations_info(channel=channel_id) or {}).get("channel", {})
        except Exception as e:
            log.warning("[%s] 채널 정보 조회 실패 ch=%s: %s", self.workspace, channel_id, e)

        doc = next((d for d in self.store.docs() if d.channel == channel), None)

        found = None
        send_at = ""
        try:
            rows = reviewers.reviewers_for(self.workspace, channel_id)
            found = [r.reviewer_user for r in rows]
            if rows:
                send_at = rows[0].send_at.strftime("%H:%M")
        except reviewers.ReviewerError as e:
            log.warning("[%s] 검토자 조회 실패 ch=%s: %s", self.workspace, channel_id, e)

        # 검토 DM 이 실제로 나가는지. 검토자 지정과 따로 고장나므로 따로 본다.
        last_digest = None
        reviewer_since = None
        if found:
            try:
                with db_connect() as conn:
                    if conn is not None:
                        last_digest = daily_review.last_sent(
                            conn, workspace=self.workspace, channel_id=channel_id
                        )
            except Exception as e:
                log.warning(
                    "[%s] 검토 DM 이력 조회 실패 ch=%s: %s", self.workspace, channel_id, e
                )
            try:
                reviewer_since = reviewers.since(self.workspace, channel_id)
            except Exception as e:
                log.warning(
                    "[%s] 검토자 지정 시각 조회 실패 ch=%s: %s",
                    self.workspace, channel_id, e,
                )

        waiting = None
        try:
            waiting = len(daily_review.blocked(
                self.archive_dir,
                workspace=self.workspace,
                channel_id=channel_id,
                extracted=self._extracted_names(channel_id),
            ))
        except Exception as e:
            log.warning("[%s] 첨부 대기 집계 실패 ch=%s: %s", self.workspace, channel_id, e)

        return HealthFacts(
            channel=channel,
            channel_id=channel_id,
            is_private=bool(info.get("is_private")),
            is_member=bool(info.get("is_member")),
            is_dm=bool(info.get("is_im")) or channel_id.startswith("D"),
            autojoin_enabled=self.autojoin,
            realtime_enabled=self.realtime,
            bot_name=self.bot_name,
            raw_lines=len(doc.raw_lines) if doc else 0,
            last_ingested=doc.last_ingested if doc else None,
            write_problems=dict(self.path_problems),
            reviewers=found,
            send_at=send_at,
            last_digest=last_digest,
            reviewer_since=reviewer_since,
            waiting_attachments=waiting,
            # **권한 판정은 `/채널 수정` 과 같은 함수를 쓴다.** 갈리면 수정은
            # 거절되는데 화면은 초록으로 뜬다(2026-09-08 실측). 화면이 자기
            # 모순이면 사람은 화면을 안 믿는다.
            viewer_can_edit=self._can_manage_channel(channel_id, viewer),
            owner=self._human_owner(channel_id),
            admin_exists=bool(self.channel_admin_users),
        )

    def _human_owner(self, channel_id: str) -> str:
        """이 채널을 고칠 수 있는 **사람**. 없으면 빈 문자열.

        TYBot 이 만든 채널은 Slack 상 생성자가 **봇 자신**이다. 그 값을 개설자로
        보이면 「<@TYBot> 에게 요청하세요」 가 되어 막다른 안내가 된다.
        우리 생성 기록이 먼저고, Slack 생성자는 그것이 사람일 때만 쓴다.
        """
        recorded = self.channel_owners.owner_of(self.workspace, channel_id)
        if recorded:
            return recorded
        creator = self._slack_creator(channel_id)
        return "" if creator == self._bot_user_id() else creator

    def _bot_user_id(self) -> str:
        """봇 자신의 사용자 ID. 못 읽으면 빈 문자열(그때는 아무것도 걸러지지 않는다)."""
        cached = getattr(self, "_bot_uid", None)
        if cached is not None:
            return cached
        try:
            self._bot_uid = str((self.app.client.auth_test() or {}).get("user_id") or "")
        except Exception as e:
            log.warning("[%s] 봇 사용자 ID 조회 실패: %s", self.workspace, e)
            return ""
        return self._bot_uid

    def _extracted_names(self, channel_id: str) -> set[str]:
        """이 채널 문서에 변환본이 들어간 첨부 이름.

        **채널명이 아니라 ID 로 고른다.** 이름은 바뀌고, 바뀌면 이 집계가 조용히
        0건이 되어 이미 읽고 있는 파일까지 「확인 필요」 로 올라온다.
        """
        from ..answer import EXTRACTED_ATTACHMENT_RE as pattern

        return {
            m.group("name")
            for doc in self.store.docs()
            if doc.channel_id == channel_id
            for line in doc.raw_lines
            if (m := pattern.match((line.text or "").strip()))
        }

    def _respond_channel_health(self, client, command: dict, respond) -> None:
        """`/채널 상태` — 넷 다 조용히 고장 나는 것들을 한 화면에 모은다."""
        channel_id = str(command.get("channel_id") or "")
        if not channel_id:
            respond("채널 안에서 실행해 주세요.", response_type="ephemeral")
            return
        try:
            text = health_report(
                self._health_facts(client, channel_id, str(command.get("user_id") or ""))
            )
        except Exception as e:
            log.warning("[%s] 채널 상태 실패 ch=%s: %s", self.workspace, channel_id, e)
            respond("채널 상태를 읽지 못했습니다.", response_type="ephemeral")
            return
        respond(text, response_type="ephemeral")

    def _open_edit_modal(
        self, client, trigger_id: str, user_id: str, channel_id: str, respond
    ) -> None:
        """`/채널 수정` — 이름과 검토자를 한 화면에서.

        **현재 이름이 표준 형식이 아니어도 연다.** 전에는 여기서 막았는데, 그건
        정확히 이름을 고쳐야 하는 채널을 못 고치게 하는 것이었다.
        """
        if not channel_id:
            respond("채널 안에서 실행해 주세요.", response_type="ephemeral")
            return
        if not self._can_manage_channel(channel_id, user_id):
            respond(
                "이 채널의 개설자, TYBot 수정 담당자 또는 Workspace Admin만 수정할 수 있습니다. "
                "Slack 채널별 관리자는 개설자나 Workspace Admin이 `/채널 수정`에서 연결해야 합니다.",
                response_type="ephemeral",
            )
            return
        name = self._channel_name(client, channel_id)
        spec = parse(name)
        current: tuple[str, ...] = ()
        send_at = "08:00"
        try:
            rows = reviewers.reviewers_for(self.workspace, channel_id)
            current = tuple(r.reviewer_user for r in rows)
            if rows:
                send_at = rows[0].send_at.strftime("%H:%M")
        except reviewers.ReviewerError as e:
            # 검토자를 못 읽었다고 이름 수정까지 막지 않는다. 다만 그 상태로
            # 저장하면 기존 검토자를 지울 수 있으므로 화면에서 말한다.
            log.warning("[%s] 검토자 조회 실패 ch=%s: %s", self.workspace, channel_id, e)
        try:
            client.views_open(
                trigger_id=trigger_id,
                view=edit_modal(
                    json.dumps({"channel_id": channel_id}, ensure_ascii=False),
                    spec=spec,
                    current_name=name,
                    reviewers=current,
                    send_at=send_at,
                    managers=(
                        self.channel_owners.managers_of(self.workspace, channel_id)
                        if self._can_delegate_channel_manager(channel_id, user_id)
                        else None
                    ),
                ),
            )
        except Exception as e:
            log.warning("[%s] 채널 수정 모달 열기 실패: %s", self.workspace, e)
            respond("채널 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")

    def _apply_channel_edit(self, client, user_id: str, channel_id: str, edit) -> None:
        """수정 제출을 적용한다. 채운 것만 바꾸고, **무엇을 바꿨는지 되돌려 준다.**

        아무것도 안 바뀌었는데 「저장했습니다」 라고 하면, 사람은 바뀐 줄 알고 나간다.
        """
        if not channel_id or not self._can_manage_channel(channel_id, user_id):
            self._notify_user(client, user_id, "이 채널을 수정할 권한이 없습니다.")
            return

        done: list[str] = []
        failed: list[str] = []

        current = self._channel_name(client, channel_id).lstrip("#")
        if edit.renames and edit.name.lstrip("#").lower() != current.lower():
            try:
                result = client.conversations_rename(
                    channel=channel_id, name=edit.name.lstrip("#").lower()
                )
                actual = (result.get("channel") or {}).get("name") or edit.name
                self._chan_cache[channel_id] = "#" + actual
                done.append(f"이름 → <#{channel_id}>")
                log.info(
                    "[%s] 채널 이름 변경 channel=%s name=%s requester=%s",
                    self.workspace, channel_id, actual, user_id,
                )
            except Exception as e:
                log.warning("[%s] 채널 이름 변경 실패 ch=%s: %s", self.workspace, channel_id, e)
                failed.append(f"이름 변경 실패 (`{edit.name}`)")

        if edit.reviewers or edit.clear_reviewers:
            try:
                rows = reviewers.set_reviewers(
                    workspace=self.workspace,
                    channel_id=channel_id,
                    channel_name=self._channel_name(client, channel_id),
                    reviewer_users=list(edit.reviewers),
                    send_at=reviewers.parse_send_at(edit.send_at),
                    set_by=user_id,
                )
                if rows:
                    who = " ".join(f"<@{r.reviewer_user}>" for r in rows)
                    done.append(f"검토자 → {who} · 매일 {rows[0].send_at:%H:%M}")
                else:
                    done.append(
                        "검토자 → 전부 해제. **이 채널은 요약을 반영하지 않고, "
                        "읽지 못한 첨부도 아무에게도 가지 않습니다.**"
                    )
            except reviewers.ReviewerError as e:
                failed.append(f"검토자 저장 실패 — {e}")

        if edit.managers or edit.clear_managers:
            if not self._can_delegate_channel_manager(channel_id, user_id):
                failed.append("수정 담당자 저장 실패 — 담당자를 지정할 권한이 없습니다.")
            else:
                before = self.channel_owners.managers_of(self.workspace, channel_id)
                if tuple(edit.managers) != before:
                    managers = self.channel_owners.set_managers(
                        self.workspace,
                        channel_id,
                        edit.managers,
                        set_by=user_id,
                    )
                    if managers:
                        who = " ".join(f"<@{uid}>" for uid in managers)
                        done.append(f"수정 담당자 → {who}")
                    else:
                        done.append("수정 담당자 → 전부 해제")

        if not done and not failed:
            self._notify_user(
                client, user_id,
                "바뀐 것이 없습니다. 이름을 바꾸려면 조직과 업무명을 채우고, "
                "검토자를 바꾸려면 사람을 고르세요.",
            )
            return

        lines = [f"✅ {row}" for row in done] + [f"⚠️ {row}" for row in failed]
        lines.append("")
        lines.append("확인: `/채널 상태`")
        self._notify_user(client, user_id, NEWLINE.join(lines))


    def _notify_user(self, client, user_id: str, text: str) -> None:
        if not user_id:
            return
        try:
            opened = client.conversations_open(users=user_id)
            dm_channel = str((opened.get("channel") or {}).get("id") or "")
            if not dm_channel:
                raise RuntimeError("DM channel missing")
            client.chat_postMessage(channel=dm_channel, text=text)
        except Exception as e:
            log.warning("[%s] 채널 관리 결과 DM 실패 user=%s: %s", self.workspace, user_id, e)

    def _create_channel(
        self, client, user_id: str, request, *, notify: bool = True
    ) -> tuple[str, str] | None:
        """채널 하나를 만든다. 성공하면 (channel_id, 실제이름), 실패하면 None.

        `notify=False` 는 여러 개를 만들 때 쓴다. 채널마다 DM 을 보내면 8개를
        만들 때 DM 이 8통 오고, 무엇이 실패했는지가 오히려 묻힌다.
        """
        try:
            # Slack 채널명의 영문은 소문자만 허용한다. 조직코드 입력은 대소문자를 받되
            # 실제 채널명에서는 Slack 규칙에 맞게 소문자로 보낸다.
            api_name = request.name.lower()
            result = client.conversations_create(
                name=api_name,
                is_private=request.visibility == "private",
            )
            channel = result["channel"]
            channel_id = channel["id"]
            actual_name = channel.get("name") or request.name
        except Exception as e:
            log.warning("[%s] 채널 생성 실패 name=%s: %s", self.workspace, request.name, e)
            if notify:
                self._notify_user(
                    client,
                    user_id,
                    f"채널을 만들지 못했습니다: `{request.name}`\n"
                    "Slack 앱 권한과 채널명을 확인해 주세요.",
                )
            return None

        try:
            self.channel_owners.record(self.workspace, channel_id, user_id, actual_name)
        except OSError as e:
            # 소유권을 기록하지 못하면 이름 변경 권한은 막히지만, 만들어진 채널은 유지한다.
            log.error("[%s] 채널 소유권 기록 실패 channel=%s: %s", self.workspace, channel_id, e)

        # 검토자를 **여기서 저장한다.** 나중에 정하게 두면 안 정한 채널이 쌓이고,
        # 그 채널은 요약이 반영되지 않고 읽지 못한 첨부도 아무에게도 가지 않는다.
        # 저장 실패는 삼키지 않는다 - 채널은 만들어졌는데 검토가 안 물린 상태다.
        reviewer_error = ""
        if request.reviewers:
            try:
                reviewers.set_reviewers(
                    workspace=self.workspace,
                    channel_id=channel_id,
                    channel_name="#" + actual_name,
                    reviewer_users=list(request.reviewers),
                    send_at=reviewers.parse_send_at(request.send_at),
                    set_by=user_id,
                )
            except reviewers.ReviewerError as e:
                reviewer_error = str(e)
                log.warning(
                    "[%s] 검토자 저장 실패 channel=%s: %s", self.workspace, channel_id, e
                )

        members = sorted({user_id, *request.members} - {""})
        invite_error = False
        if members:
            try:
                client.conversations_invite(channel=channel_id, users=",".join(members))
            except Exception as e:
                invite_error = True
                log.warning("[%s] 채널 참여자 초대 실패 channel=%s: %s", self.workspace, channel_id, e)

        visibility = "비공개" if request.visibility == "private" else "공개"
        suffix = "\n일부 참여자 초대에 실패했습니다. 채널에서 직접 초대해 주세요." if invite_error else ""
        if reviewer_error:
            suffix += (
                "\n⚠️ **요약 검토자를 저장하지 못했습니다** — " + reviewer_error
                + " 지금은 요약이 반영되지 않고 읽지 못한 첨부도 가지 않습니다. "
                "`/채널 수정` 으로 다시 지정해 주세요."
            )
        elif request.reviewers:
            who = " ".join(f"<@{u}>" for u in request.reviewers)
            suffix += f"\n요약 검토자 {who} · 매일 {request.send_at} DM."
        if notify:
            self._notify_user(
                client,
                user_id,
                f"{visibility} 업무 채널 <#{channel_id}>을 만들었습니다. "
                f"이름이 수집 규칙에 맞아 **이 채널의 대화는 아카이브에 쌓입니다.**"
                "\n`/채널 수정`으로 이름과 요약 검토자를 함께 고칠 수 있고, "
                "`/채널 상태`로 수집·검토자·첨부가 제대로 물렸는지 봅니다. "
                "규칙 밖 이름으로 바꾸면 그 시점부터 수집이 멈춥니다."
                "\nSlack 기본 관리 권한이 필요하면 채널 정보 → 관리자로 지정에서 추가하세요."
                + suffix,
            )
        with contextlib.suppress(Exception):
            client.chat_postMessage(
                channel=channel_id,
                text=CHANNEL_CREATED_NOTICE.format(
                    bot=self.bot_name,
                    visibility="비공개" if request.visibility == "private" else "공개",
                ),
            )
        log.info(
            "[%s] 업무 채널 생성 channel=%s name=%s private=%s requester=%s",
            self.workspace,
            channel_id,
            actual_name,
            request.visibility == "private",
            user_id,
        )
        return channel_id, actual_name

    def _create_channels(self, client, user_id: str, requests: list) -> None:
        """여러 채널을 만들고 **결과를 한 통으로** 알린다.

        한 개면 기존 문구를 그대로 쓴다. 여러 개일 때 채널마다 DM 을 보내면
        성공만 잔뜩 오고 실패가 그 사이에 묻힌다. 무엇이 만들어졌고 무엇이
        빠졌는지는 한눈에 보여야 한다.

        중간에 실패해도 멈추지 않는다. 앞의 몇 개는 이미 만들어졌으므로 멈추면
        되돌릴 수도, 이어서 할 수도 없는 상태가 된다.
        """
        if len(requests) == 1:
            self._create_channel(client, user_id, requests[0])
            return

        made: list[str] = []
        failed: list[str] = []
        for request in requests:
            result = self._create_channel(client, user_id, request, notify=False)
            if result:
                made.append(f"<#{result[0]}>")
            else:
                failed.append(f"`{request.name}`")

        base = requests[0]
        lines = [f"업무 채널 {len(made)}개를 만들었습니다."]
        if made:
            lines.append(" ".join(made))
            lines.append(
                "이름이 수집 규칙에 맞아 **이 채널들의 대화는 아카이브에 쌓입니다.**"
            )
            if base.reviewers:
                who = " ".join(f"<@{u}>" for u in base.reviewers)
                lines.append(f"요약 검토자 {who} · 매일 {base.send_at} DM.")
            # 검토자 저장은 채널마다 따로 실패할 수 있다. 한 통으로 알릴 때는
            # 확인 경로만 준다 - 어느 채널이 빠졌는지는 상태가 답한다.
            lines.append("검토자가 제대로 물렸는지: 각 채널에서 `/채널 상태`")
            lines.append(SECTION_TIP)
        if failed:
            lines.append(
                f"만들지 못한 것 {len(failed)}개: " + ", ".join(failed)
                + "\n이미 있는 이름이거나 Slack 권한 문제일 수 있습니다. "
                "이름을 바꿔 다시 시도해 주세요."
            )
        self._notify_user(client, user_id, "\n".join(lines))

    def _rename_channel(self, client, user_id: str, channel_id: str, name: str) -> None:
        if not channel_id or not self._can_manage_channel(channel_id, user_id):
            self._notify_user(client, user_id, "이 채널의 이름을 변경할 권한이 없습니다.")
            return
        try:
            result = client.conversations_rename(channel=channel_id, name=name.lower())
            actual_name = result.get("channel", {}).get("name") or name
            self._chan_cache[channel_id] = "#" + actual_name
        except Exception as e:
            log.warning("[%s] 채널 이름 변경 실패 channel=%s: %s", self.workspace, channel_id, e)
            self._notify_user(
                client, user_id, f"채널 이름을 변경하지 못했습니다: `{name}`"
            )
            return
        self._notify_user(client, user_id, f"채널 이름을 <#{channel_id}>으로 변경했습니다.")
        log.info(
            "[%s] 업무 채널 이름 변경 channel=%s name=%s requester=%s",
            self.workspace,
            channel_id,
            actual_name,
            user_id,
        )

    def _already_handled(self, event) -> bool:
        """같은 메시지를 두 번 처리하지 않는다.

        **Slack 은 3초 안에 응답이 없으면 같은 이벤트를 다시 보낸다.** 우리 처리에는
        LLM 호출이 들어가 그보다 오래 걸리는 일이 흔하다. 그러면 한 번의 질문이
        네 번 처리되어 **비용이 네 배 나가고, 답도 네 번 달린다.**
        실제로 그렇게 돌던 기록이 있다(2026-09-02, 재시도마다 LLM 재호출).

        메시지 `ts` 는 채널 안에서 유일하고 재전송에도 같은 값이 온다. 그것으로 거른다.
        """
        key = f"{event.get('channel') or ''}:{event.get('ts') or ''}"
        if key == ":":
            return False
        seen = getattr(self, "_seen_events", None)
        if seen is None:
            seen = self._seen_events = {}
            self._seen_lock = threading.Lock()
        with self._seen_lock:
            if key in seen:
                return True
            seen[key] = None
            # 메모리를 무한정 쓰지 않는다. 재전송은 몇 초 안에 오므로 이 정도면 충분하다.
            while len(seen) > SEEN_EVENTS:
                seen.pop(next(iter(seen)))
        return False

    def _handle(self, event, client, say, *, in_channel: bool) -> None:
        """요청 처리 진입점. 어떤 예외가 나도 **사람에게 무슨 일인지 알린다.**

        예전에는 예외가 여기서 조용히 사라져 👀 만 붙고 답이 없었다. 사용자는 봇이
        무시했다고 생각하고, 원인은 서버 로그를 보는 사람만 알 수 있었다.
        """
        if self._already_handled(event):
            log.info(
                "[%s] 재전송 무시 ch=%s ts=%s",
                self.workspace, event.get("channel"), event.get("ts"),
            )
            return

        started = time.monotonic()
        try:
            self._handle_request(event, client, say, in_channel=in_channel)
        except Exception as e:
            # Slack 이 거부한 이유를 첫 줄에 남긴다. 트레이스백 30줄을 읽어야
            # 'restricted_action_read_only_channel' 을 찾는 상황을 만들지 않는다.
            log.exception(
                "요청 처리 실패 ws=%s ch=%s slack_error=%s",
                self.workspace, event.get("channel"), _slack_error(e),
            )
            reply = failure_message(e)
            response_ts = ""
            with contextlib.suppress(Exception):
                ts = event.get("thread_ts") or event.get("ts")
                if self.reply_in_thread or event.get("thread_ts"):
                    response_ts = _response_ts(say(text=reply, thread_ts=ts))
                else:
                    response_ts = _response_ts(say(text=reply))
            rec = QARecord.build(
                workspace=self.workspace,
                channel=self._chan_cache.get(event.get("channel", ""), event.get("channel", "")),
                channel_id=str(event.get("channel") or ""),
                user=str(event.get("user") or ""),
                user_name=self._user_name(client, str(event.get("user") or "")),
                question=_clean(event.get("text", "")),
                intent_kind="error",
                intent_source="runtime",
                reason="error",
                hits=0,
                scope="-",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                answer=reply,
                request_ts=str(event.get("ts") or ""),
                response_ts=response_ts,
                thread_ts=str(event.get("thread_ts") or event.get("ts") or ""),
                channel_type=str(event.get("channel_type") or ("channel" if in_channel else "im")),
                error=type(e).__name__,
            )
            self.qa_log.write(rec)

    def _handle_request(self, event, client, say, *, in_channel: bool) -> None:
        raw_text = _clean(event.get("text", ""))
        canvas_requested, text = parse_canvas_request(raw_text)
        if canvas_requested and not text:
            text = "최근 업무 내용을 정리해줘"
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        # 스레드 안에서 부른 경우에는 설정과 무관하게 그 스레드에 답한다(대화 맥락 유지).
        in_existing_thread = bool(event.get("thread_ts"))
        thread_ts = (
            (event.get("thread_ts") or event.get("ts"))
            if (self.reply_in_thread or in_existing_thread)
            else None
        )
        started = time.monotonic()

        # 👀 표시는 부가 기능이다. 실패해도 답변은 계속한다.
        with contextlib.suppress(Exception):
            client.reactions_add(channel=channel_id, timestamp=event["ts"], name="eyes")

        def finish(reply: str, *, intent: Intent, ans: Answer | None, ctx: RequestContext | None):
            """모든 응답 경로가 여기로 모인다 — 경로마다 로그가 달라지지 않게."""
            if ans is not None and ctx is not None and (ctx.channel_id or ctx.channel):
                reply = f"{reply}\n\n{CHANNEL_SCOPE_NOTICE}"
            # 아카이브 근거로 답한 경우에만 '근거 보기' 를 붙인다. 버튼이 있는데
            # 눌러도 아무것도 안 나오면 없는 것만 못하다.
            fallback_kw = {"text": reply}
            if thread_ts:
                fallback_kw["thread_ts"] = thread_ts
            kw = dict(fallback_kw)
            canvas = None
            if canvas_requested and ans is not None:
                try:
                    canvas = create_answer_canvas(client, reply)
                    kw = {
                        "text": f"정식 답변을 Canvas로 작성했습니다: <{canvas.permalink}|Canvas 열기>"
                    }
                    if thread_ts:
                        kw["thread_ts"] = thread_ts
                    if not channel_id.startswith("D"):
                        grant_canvas_channel(client, canvas.canvas_id, channel_id)
                except Exception as exc:
                    log.exception("[%s] Canvas 답변 생성 실패: %s", self.workspace, exc)
                    canvas = None
                    kw = fallback_kw
            elif ans is not None and ans.terms:
                kw["blocks"] = evidence_view.blocks(
                    reply, ans.terms, workspace=self.workspace
                )
            response = say(**kw)
            if canvas is not None and channel_id.startswith("D"):
                try:
                    # Slack은 Canvas를 먼저 DM으로 보낸 뒤 사용자 접근을 부여하도록 요구한다.
                    grant_canvas_user(client, canvas.canvas_id, user_id)
                except Exception as exc:
                    log.exception("[%s] Canvas DM 권한 부여 실패: %s", self.workspace, exc)
                    # 접근할 수 없는 링크만 남기지 않고 원래 메시지 답변도 전달한다.
                    response = say(**fallback_kw)
            rec = QARecord.build(
                workspace=self.workspace,
                channel=self._chan_cache.get(channel_id, channel_id),
                channel_id=channel_id,
                user=user_id,
                user_name=self._user_name(client, user_id) if user_id else "unknown",
                question=raw_text,
                intent_kind=intent.kind,
                intent_source=intent.source,
                reason=ans.reason if ans else intent.kind,
                hits=ans.hit_count if ans else 0,
                scope=_scope_label(ctx),
                citations=list(ans.citations) if ans else [],
                model=ans.model if ans else None,
                cost_usd=ans.cost_usd if ans else 0.0,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                answer=reply,
                request_ts=str(event.get("ts") or ""),
                response_ts=_response_ts(response),
                thread_ts=str(event.get("thread_ts") or event.get("ts") or ""),
                channel_type=str(event.get("channel_type") or ("channel" if in_channel else "im")),
                # 다음 질문이 이어 갈 **좌표**. 답변 문장이 아니다 —
                # 문장을 이어 가면 요약을 근거로 요약하게 된다(원칙 1).
                evidence_refs=refs_to_json(ans.evidence_refs) if ans else [],
                attachment_refs=attachment_refs_to_json(ans.attachment_refs) if ans else [],
                subject_terms=list(ans.subject_terms) if ans else [],
                context_parent_ids=list(ans.context_parent_ids) if ans else [],
                context_resolution=(ans.context_resolution if ans else "none"),
            )
            log.info("%s", rec.log_line())
            self.qa_log.write(rec)

        # 명시 명령은 LLM 을 거치지 않는다(비용·지연 절약).
        # 그 외에는 1차 LLM 이 **하위질문 목록**으로 분해한다 - 사람은 한 번에 여러 가지를
        # 묻는데, 라벨 하나만 고르던 예전 구조에서는 그중 하나만 처리 경로에 도달했다.
        turns: list[dict] = []
        if INGEST_ALL_RE.search(text):
            tasks = [Intent("ingest_all", source="cmd", question=text)]
        elif INGEST_RE.search(text):
            tasks = [Intent("ingest", source="cmd", question=text)]
        else:
            turns = self._thread_turns(event)
            tasks = self.engine.plan(
                text,
                conversation_context=self._thread_conversation_context(turns),
                # 좌표가 있으면 옛 답변 문구를 정규식으로 다시 파싱하지 않는다.
                thread_has_refs=any(
                    turn.get("evidence_refs") or turn.get("attachment_refs") for turn in turns
                ),
            )
        if not tasks:
            tasks = [Intent("search", source="regex", question=text)]

        dropped = len(tasks) - MAX_TASKS if len(tasks) > MAX_TASKS else 0
        tasks = tasks[:MAX_TASKS]
        first = tasks[0]

        # 쓰기 동작은 **단독으로만** 실행한다. 무엇을 실행하는지 모호하면 실행하지 않는다.
        if first.kind in WRITE_KINDS:
            if first.kind == "ingest_all":
                finish(self._ingest_all(client), intent=first, ans=None, ctx=None)
                return
            if not in_channel:
                finish(
                    "수집은 채널에서만 실행할 수 있습니다. 대상 채널에서 `수집` 이라고 불러주세요.",
                    intent=first, ans=None, ctx=None,
                )
                return
            finish(self._ingest_channel(client, channel_id), intent=first, ans=None, ctx=None)
            return

        sections: list[str] = []
        ctx: RequestContext | None = None
        last: Answer | None = None
        for task in tasks:
            q = task.question or text
            if task.kind in SELF_KINDS:
                sections.append(self._self_reply(task.kind, q, client=client, user_id=user_id))
                continue
            # 아카이브 근거 답변은 엔진 출력을 **그대로** 쓴다 - 출처가 붙어 있으므로
            # 문장을 다시 만들면 본문과 출처가 어긋날 수 있다(원칙 2).
            if ctx is None:
                ctx = self._request_context(
                    client,
                    user_id,
                    channel_id=channel_id,
                    in_channel=in_channel,
                )
            # 후속 질문이면 **이전 결과의 좌표를 현재 권한으로 되살린 범위**만
            # 쓴다. 되살리지 못하면 못했다고 답한다 — 채널 전체로 넓히지 않는다.
            followup = None
            if task.is_followup and turns:
                try:
                    followup = self._followup_resolver().resolve(
                        task, ctx, turns=turns, channel_id=channel_id if in_channel else ""
                    )
                except Exception as exc:
                    log.exception("[%s] 후속 질문 해석 실패: %s", self.workspace, exc)
                    followup = None
            ans = self.engine.respond(q, ctx, task, followup=followup)
            last = ans
            sections.append(ans.to_slack())

        if dropped:
            sections.append(truncated_notice(dropped))

        # 감사기록에는 처리한 의도를 전부 남긴다(예: "memory+summary").
        merged = Intent(
            kind="+".join(dict.fromkeys(x.kind for x in tasks)),
            source=first.source,
            question=raw_text,
        )
        finish(join_sections(sections), intent=merged, ans=last, ctx=ctx)

    # --- 수집 -------------------------------------------------------------
    def _messages_from(self, client, event: dict) -> list:
        """Slack 메시지 1건 → 원문 라인들(본문 + 첨부).

        첨부는 텍스트 형식만 본문을 넣고, 나머지는 목록만 남긴다(files.py 참조).
        """
        ts = datetime.fromtimestamp(float(event["ts"]), tz=UTC)
        speaker = self._user_name(client, event.get("user", "unknown"))
        # 이전 메시지의 첨부가 남아 있으면 그 상태를 엉뚱한 원문으로 확인하게 된다.
        self._pending_attachments = []
        out = []
        body = (event.get("text") or "").strip()
        if body:
            out.append(writer.IncomingMessage(ts=ts, speaker=speaker, text=body))
        if event.get("files"):
            channel_id = event.get("channel", "unknown")
            storage = attachment_storage(self.archive_dir, self.workspace, channel_id)
            staged = stage_attachments(
                event["files"], self.cfg.bot_token, storage,
                origin=AttachmentOrigin(
                    workspace=self.workspace,
                    channel_id=channel_id,
                    message_ts=str(event.get("ts") or ""),
                    thread_ts=str(event.get("thread_ts") or ""),
                ),
            )
            # 파일↔줄 연결을 들고 있어야 writer 뒤에 반영을 확인할 수 있다.
            self._pending_attachments = staged
            for item in staged:
                for ln in item.lines:
                    out.append(writer.IncomingMessage(ts=ts, speaker=speaker, text=ln))
                for w in item.warnings:
                    log.warning("첨부 처리 경고 ch=%s: %s", channel_id, w)
        return out

    def _ingest_live(self, client, event) -> None:
        """실시간 원문 append. 실패해도 봇은 계속 살아 있어야 한다."""
        channel = self._channel_name(client, event.get("channel", ""))
        if not should_collect(channel):
            log.debug("채널 규칙 밖이라 실시간 수집 생략 ch=%s", channel)
            return
        msgs = self._messages_from(client, event)
        if not msgs:
            return
        try:
            r = writer.ingest(
                self.archive_dir,
                workspace=self.workspace,
                channel=channel,
                messages=msgs,
                acl=[channel],
            )
        except Exception as e:
            log.exception("실시간 수집 실패 ch=%s: %s", channel, e)
            return
        if r.written:
            self._ingested += r.written
            self._last_ingest_at = datetime.now(UTC)
        if r.refused:
            log.warning("제외 대상으로 미저장 ch=%s 사유=%s", channel, r.refused[0][1])
        self._confirm_attachments(event.get("channel", ""))

    def _confirm_attachments(self, channel_id: str) -> None:
        """첨부 줄이 **원문에 실제로 들어갔는지** 확인해 metadata 에 남긴다.

        `r.written` 을 근거로 쓰지 않는다 — 이미 같은 줄이 있으면 0 이고, 그건 멱등
        성공이다(설계 §6). 원문에 줄이 있는지가 유일한 근거다.

        실패해도 수집을 막지 않는다. 원문이 진실이고 metadata 는 그 사본이다. 기록에
        실패하면 진단에서 `pending` 으로 남아 「모른다」 로 보이는데, 그게 사실이다.
        """
        staged = getattr(self, "_pending_attachments", None)
        if not staged:
            return
        self._pending_attachments = []
        try:
            confirm_archived(
                self.store, staged,
                workspace=self.workspace, channel_id=channel_id,
            )
        except Exception as e:
            log.warning("첨부 원문 반영 확인 실패 ch=%s: %s", channel_id, e)

    def _ingest_channel(self, client, channel_id: str) -> str:
        channel = self._channel_name(client, channel_id)
        if not should_collect(channel):
            return f"{channel}: 채널 이름이 수집 규칙과 달라 건너뛰었습니다."
        try:
            res = client.conversations_history(channel=channel_id, limit=HISTORY_LIMIT)
        except Exception as e:
            return (
                f"채널 히스토리를 읽지 못했습니다: {e}\n"
                f"`/invite @{self.bot_name}` 와 `channels:history` 권한을 확인하세요."
            )

        msgs = []
        thread_parents = []
        for m in reversed(res.get("messages", [])):
            if m.get("bot_id") or m.get("subtype") not in (None, "file_share"):
                continue
            msgs.extend(self._messages_from(client, m))
            # conversations.history 는 스레드 답글을 주지 않는다. 답글이 있으면 따로 받는다.
            if int(m.get("reply_count") or 0) > 0:
                thread_parents.append(m["ts"])

        replies = 0
        for parent in thread_parents[:THREAD_FETCH_LIMIT]:
            try:
                rr = client.conversations_replies(
                    channel=channel_id, ts=parent, limit=HISTORY_LIMIT
                )
            except Exception as e:
                log.warning("스레드 답글 조회 실패 ch=%s ts=%s: %s", channel, parent, e)
                continue
            for m in rr.get("messages", [])[1:]:  # 첫 건은 부모 메시지
                if m.get("bot_id") or m.get("subtype") not in (None, "file_share"):
                    continue
                got = self._messages_from(client, m)
                msgs.extend(got)
                replies += len(got)

        # 채널 캔버스 스냅샷. 없으면 조용히 넘어간다(정상).
        canvas_note = ""
        canvas = canvas_lines(client, channel_id, self.cfg.bot_token)
        if canvas.lines:
            now = datetime.now(UTC)
            msgs.extend(
                writer.IncomingMessage(
                    ts=now,
                    speaker="캔버스",
                    text=ln,
                    dedupe_key=canvas.dedupe_key,
                )
                for ln in canvas.lines
            )
            canvas_note = f"캔버스 {len(canvas.lines)}줄 포함"
        for w in canvas.warnings:
            log.warning("[%s] %s", self.workspace, w)
            canvas_note = f"캔버스 처리 경고: {w}"

        try:
            r = writer.ingest(
                self.archive_dir,
                workspace=self.workspace,
                channel=channel,
                messages=msgs,
                acl=[channel],
            )
        except Exception as e:
            return f"형식 검사 실패로 이번 취합을 롤백했습니다: {e}"

        out = [f"{channel}: 원문 {r.written}건 저장 (봇 발언 {r.skipped_bot}건 제외)"]
        if canvas_note:
            out.append(canvas_note)
        if thread_parents:
            out.append(
                f"스레드 {min(len(thread_parents), THREAD_FETCH_LIMIT)}개의 답글 {replies}건 포함"
                + (f" (답글 있는 스레드 {len(thread_parents)}개 중)" if len(thread_parents) > THREAD_FETCH_LIMIT else "")
            )
        if r.refused:
            out.append(f"제외 대상 {len(r.refused)}건(개인정보/등기부 등)은 아카이브하지 않았습니다.")
        out.append(
            f"백필은 신규 앱 제한으로 회당 {HISTORY_LIMIT}건까지입니다. "
            "이후 대화는 실시간으로 자동 수집됩니다."
        )
        return "\n".join(out)

    def _ingest_all(self, client) -> str:
        """봇이 볼 수 있는 채널 중 이름 규칙에 맞는 채널만 백필."""
        try:
            channels = []
            cursor = None
            while True:
                res = client.conversations_list(
                    types="public_channel,private_channel",
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor,
                )
                channels.extend(res.get("channels", []))
                cursor = (res.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as e:
            return f"채널 목록 조회 실패: {e} (`channels:read`, `groups:read` 확인)"

        joined, done, skipped_rule, skipped_disabled, skipped_private, failed = (
            [],
            0,
            [],
            [],
            [],
            [],
        )
        for ch in channels:
            name = "#" + ch["name"]
            if not should_collect(name):
                skipped_rule.append(name)
                continue
            if not ch.get("is_member"):
                if ch.get("is_private"):
                    skipped_private.append(name)  # 봇은 비공개 채널에 자가참여 불가
                    continue
                if not self.autojoin:
                    skipped_disabled.append(name)
                    continue
                try:
                    client.conversations_join(channel=ch["id"])
                    joined.append(name)
                except Exception as e:
                    failed.append(f"{name}(참여실패: {e})")
                    continue
            self._chan_cache[ch["id"]] = name
            msg = self._ingest_channel(client, ch["id"])
            if "저장" in msg:
                done += 1
            else:
                failed.append(f"{name}({msg.splitlines()[0]})")

        lines = [f"백필 완료: {done}개 채널"]
        if joined:
            lines.append(f"자가참여: {', '.join(joined)}")
        if skipped_rule:
            lines.append(f"채널 규칙 밖이라 생략: {len(skipped_rule)}개")
        if skipped_disabled:
            lines.append(f"자동 참여가 꺼져 있어 미가입 채널 생략: {len(skipped_disabled)}개")
        if skipped_private:
            lines.append(
                f"⚠️ 비공개 채널 {len(skipped_private)}개는 봇이 스스로 들어갈 수 없습니다 — "
                f"해당 채널에서 `/invite @{self.bot_name}` 필요: {', '.join(skipped_private)}"
            )
        if failed:
            lines.append(f"실패 {len(failed)}건: {'; '.join(failed[:5])}")
        lines.append(
            f"각 채널 과거 대화는 회당 {HISTORY_LIMIT}건 제한(Slack 신규 앱). "
            "앞으로의 대화는 실시간 수집됩니다."
        )
        return "\n".join(lines)

    # --- 봇 자신에 대한 답변 -------------------------------------------------
    def _memory_facts(self, user_id: str) -> dict:
        """기억 정책을 '사실'로 넘긴다. 문장은 LLM 이 질문에 맞춰 쓴다.

        예전에는 이 내용이 고정 문단이어서, 같은 메시지에 붙은 다른 질문을 반영할 수
        없었다. 정책 자체는 코드가 정한다 - 모델이 기억 여부를 창작하면 안 된다.
        """
        recent = self.qa_log.recent_for_user(self.workspace, user_id)
        return {
            "스레드_밖_지속_기억": False,
            "같은_스레드_맥락": (
                "이전 질문과 답변으로 '그 문서' 같은 지칭어만 해석하고, "
                "실제 답은 권한 내 원문에서 다시 찾는다."
            ),
            "이유": [
                "봇 답변을 다시 근거로 쓰면 틀린 내용이 사실처럼 굳는다(요약 재귀).",
                "근거는 사람이 쓴 원문이어야 출처를 붙이고 검증할 수 있다.",
            ],
            "매_질문마다": "아카이브 원문에서 처음부터 다시 찾는다.",
            "본인_최근_질문": [
                {"시각": ts[5:16].replace("T", " "), "질문": q} for ts, q in recent
            ],
            "최근_질문_주의": "감사 기록이며 답변 생성에는 쓰이지 않는다. 본인 질문만 보인다.",
        }

    def _visible_workspaces(self) -> frozenset[str]:
        """상태에 보여줄 워크스페이스. 자기 것 + 크로스 열람 허용분.

        예전에는 아카이브 전체 문서를 나열해서, 자기 워크스페이스만 볼 수 있는 봇도
        다른 워크스페이스의 채널명을 보여줬다. 채널명에는 조직·업무가 들어 있어
        그 자체가 노출이다(원칙 3).
        """
        return frozenset({self.workspace, *(self.cfg.readable or ())})

    def _status_tree(self):
        return build_tree(
            self.store.docs(),
            visible=self._visible_workspaces(),
            labels=getattr(self, "workspace_labels", None) or {self.workspace: self.cfg.label},
            own=self.workspace,
        )

    def _status_facts(self, client=None) -> dict:
        """상태 요약을 사실로 넘긴다. 상세 목록은 코드가 만든 블록을 그대로 붙인다."""
        tree_totals = totals(self._status_tree())
        up = datetime.now(UTC) - self._started
        hours, rem = divmod(int(up.total_seconds()), 3600)
        return {
            "워크스페이스": f"{self.cfg.label} ({self.workspace})",
            "등급": "상위(root)" if self.cfg.is_root else "일반",
            "크로스_열람_허용": sorted(self.cfg.readable) or "없음",
            "가동시간": f"{hours}시간 {rem // 60}분",
            "실시간_수집": self.realtime,
            "자동참여": self.autojoin,
            "모델": self.engine.model_info(),
            "오늘_사용액_USD": round(self.engine.spent_today(), 4),
            "아카이브_문서수": tree_totals["문서수"],
            "아카이브_원문줄수": tree_totals["원문줄수"],
            "워크스페이스별_수집": tree_totals["워크스페이스별"],
            "형식위반_건수": len(self.store.broken()),
            "쓰기_불가_경로": self.path_problems or "없음",
        }

    def _self_reply(self, kind: str, question: str, *, client, user_id: str) -> str:
        """봇 자신에 대한 답변. 사실은 코드가, 문장은 LLM 이 만든다.

        데이터가 촘촘한 답변(status/help)은 결정적 블록을 유지한다 - 모델이 다시 쓰면
        채널별 줄 수나 명령어 목록이 조용히 빠지거나 없는 명령이 생길 수 있다.
        LLM 은 '질문에 대한 첫 문장'만 쓴다.
        """
        router = getattr(self.engine, "router", None)
        if kind == "memory":
            return write_from_facts(
                router,
                question=question,
                facts=self._memory_facts(user_id),
                fallback=self._memory(user_id),
            )
        if kind == "status":
            block = self._status(client)
            lead = write_from_facts(
                router,
                question=question,
                facts=self._status_facts(client),
                fallback="",
                max_tokens=300,
            )
            return lead + BLANK + block if lead else block
        if kind == "help":
            return self._help()
        if kind == "smalltalk":
            return write_from_facts(
                router,
                question=question,
                facts={
                    "역할": "사내 Slack 아카이브 봇. 수집된 원문만 근거로 답한다.",
                    "할_수_있는_것": ["기간 요약", "원문 검색", "판단·권고", "수집", "상태 확인"],
                    "도움말_명령": "도움말",
                },
                fallback="네, 대기 중입니다. `도움말` 로 사용법을 볼 수 있습니다.",
                max_tokens=200,
            )
        return (
            "사내 아카이브에 쌓인 원문만 근거로 답하는 봇입니다. "
            "그 범위를 벗어난 질문에는 답하지 않습니다. `도움말` 을 참고하세요."
        )

    def _memory(self, user_id: str) -> str:
        """"이전 답변 기억나?" — 설계상 기억하지 않는다는 것을 그대로 말한다.

        매번 아카이브 원문에서 처음부터 찾는 것이 요약 재귀를 막는 장치다(원칙 1).
        대신 감사 기록에 남은 **본인 질문**은 보여준다.
        """
        lines = [
            "*이전 답변을 스레드 밖까지 기억하지 않습니다.* "
            "질문마다 아카이브 원문에서 다시 찾습니다.",
            "",
            "그렇게 만든 이유:",
            "• 제 답변을 다시 근거로 쓰면 틀린 내용이 사실처럼 굳습니다(요약 재귀).",
            "• 근거는 사람이 쓴 원문뿐이어야 출처를 붙이고 검증할 수 있습니다.",
            "",
            "같은 스레드에서 이어 물으면 이전 문답으로 `그 문서` 같은 지칭어를 "
            "해석합니다. 이전 봇 답변 자체를 근거로 쓰지는 않고 원문에서 다시 검증합니다.",
        ]
        recent = self.qa_log.recent_for_user(self.workspace, user_id)
        if recent:
            lines += ["", "*참고 — 감사 기록에 남은 회원님의 최근 질문*"]
            lines += [f"• {ts[5:16].replace('T', ' ')}  {q}" for ts, q in recent]
            lines.append("(기록용이며 답변 생성에는 쓰이지 않습니다. 본인 질문만 표시됩니다.)")
        return "\n".join(lines)

    def _help(self) -> str:
        return "\n".join(
            [
                f"*@{self.bot_name} 사용법* — 아카이브에 쌓인 원문만 근거로 답합니다.",
                "• `요약` / `이번주 진행상황` / `30일 요약` — 기간별 정리",
                "• `<키워드> 얼마야?` 같은 구체 질문 — 원문 검색 + 출처",
                "• `어느 방향이 나을까?` 같은 판단·권고 요청 — 원문이 있으면 근거로, 없으면 일반 판단으로 답합니다",
                "• `수집` — 이 채널 과거 대화 백필 / `전체수집` — 규칙에 맞는 전 채널",
                "• `상태` — 연결·수집 상태 / `도움말` — 이 안내",
                "• `/일정` — 그룹웨어 팀 일정 (본인에게만 표시)",
                "• `/수집상태` — 지금 이 채널이 수집되는지, 아니면 왜 아닌지",
                "• `/권한` — 내 질문이 근거로 삼는 범위",
                "• `/피드백` — 답변 품질 신고. 선택 화면이 열립니다 "
                "(`/피드백 <내용>` 으로 바로 보내거나, 답변에 :+1:/:-1: 도 가능)",
                "• `이전 답변 기억나?` — 기억 여부와 그 이유(매번 원문에서 다시 찾습니다)",
                "• `--model=claude-opus-4-8 질문` — 모델 지정",
                "*사실*은 아카이브 원문만 근거로 답합니다. 근거가 없으면 추측하지 않습니다.",
            ]
        )

    def _status(self, client=None) -> str:
        """봇 자체 상태 — 아카이브 질의가 아니므로 LLM 을 호출하지 않는다(비용 0)."""
        nodes = self._status_tree()
        broken = self.store.broken()
        up = datetime.now(UTC) - self._started
        hours, rem = divmod(int(up.total_seconds()), 3600)
        conn = "Socket Mode 연결됨"
        who = ""
        if client is not None:
            try:
                a = client.auth_test()
                who = f" · 봇 {a.get('user')} / 워크스페이스 {a.get('team')}"
            except Exception as e:
                conn = f"Slack API 응답 이상: {e}"
        last = (
            self._last_ingest_at.astimezone(KST).strftime("%m-%d %H:%M")
            if self._last_ingest_at
            else "없음"
        )
        cross = (
            ", ".join(sorted(self.cfg.readable)) if self.cfg.readable else "없음(자기 워크스페이스만)"
        )
        role = "상위(root) - 산하 자료 전량 열람" if self.cfg.is_root else "일반 - 소속 채널만"
        lines = [
            f"*워크스페이스*: {self.cfg.label} (`{self.workspace}`) · 등급: {role}",
            f"*크로스 열람 허용*: {cross}",
            f"*연결*: {conn}{who}",
            f"*가동*: {hours}시간 {rem // 60}분 · 실시간 수집 {'ON' if self.realtime else 'OFF'}"
            f" · 답변 위치 {'스레드' if self.reply_in_thread else '채널'}"
            f" · 자동참여 {'ON' if self.autojoin else 'OFF'}"
            f" · 이번 세션 수집 {self._ingested} 건 (마지막 {last})",
            f"*모델*: {self.engine.model_info()} · 오늘 사용액 ${self.engine.spent_today():.3f}",
            f"*감사기록*: `{self.qa_log.root}`",
            f"*아카이브*: `{self.archive_dir}` — 문서 {sum(n.docs for n in nodes)}건, "
            f"원문 {sum(n.lines for n in nodes)}줄 · 워크스페이스 {len(nodes)}개",
        ]
        # 워크스페이스 → 채널 트리. 평평한 목록은 어느 조직의 채널인지 알 수 없다.
        lines.extend(render_tree(nodes))
        if broken:
            lines.append(f"⚠️ 형식 위반 {len(broken)}건: " + ", ".join(p.name for p, _ in broken))
        for label, why in self.path_problems.items():
            lines.append(f"🛑 *{label} 쓰기 불가* — {why}. 수집이 저장되지 않습니다.")
        if not any(n.docs for n in nodes) and not self.path_problems:
            lines.append("ℹ️ 아직 수집된 원문이 없습니다. 채널에서 `수집` 또는 대화가 쌓이길 기다리세요.")
        return "\n".join(lines)

    def recent_messages(self, channel_id: str, limit: int = 20) -> list[dict]:
        """아직 아카이브에 없는 최근 대화. **권한 판정은 하지 않는다.**

        부르는 쪽(`specialist_tools.ToolBox._fetch_recent`)이 이미 그 채널을
        `visible_docs` 로 확인했다. 여기서 또 판정하면 규칙이 두 곳에 생긴다.

        **봇 발언에 표시를 붙여 보낸다.** 거르는 것은 도구가 하지만, 여기서
        `is_bot` 을 안 실으면 도구가 거를 근거가 없다 — 우리 답이 다음 답의
        근거가 되고 잘못 말한 숫자가 굳는다(원칙 1).
        """
        if not channel_id:
            return []
        try:
            got = self.app.client.conversations_history(
                channel=channel_id, limit=max(1, min(limit, 100))
            )
        except Exception as e:
            log.warning("[%s] 실시간 조회 실패 ch=%s: %s", self.workspace, channel_id, e)
            return []
        out: list[dict] = []
        # Slack 은 최신부터 준다. 읽는 순서는 시간 순이 자연스럽다.
        for message in reversed(got.get("messages") or []):
            ts = str(message.get("ts") or "")
            is_bot = bool(message.get("bot_id")) or message.get("subtype") == "bot_message"
            out.append({
                "ts": _kst_stamp(ts),
                "speaker": self._user_name(self.app.client, str(message.get("user") or "")),
                "text": str(message.get("text") or ""),
                "is_bot": is_bot,
                # permalink 를 따로 부르면 메시지마다 API 호출이 하나씩 는다.
                # 링크 모양은 안정적이라 우리가 만든다.
                "permalink": (
                    f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}"
                    if ts else ""
                ),
            })
        return out

    def connect(self) -> None:
        """Socket Mode 연결을 비동기로 연다(블로킹하지 않는다).

        워크스페이스마다 연결이 하나씩이므로, 여러 개를 띄우려면 블로킹하면 안 된다.
        """
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        self._handler = SocketModeHandler(self.app, self.cfg.app_token)
        self._backfill_channel_owners()
        self._handler.connect()
        self.autojoin_sweep()
        self.identity_sweep()
        log.info(
            "워크스페이스 연결 — %s / 실시간수집=%s / 크로스열람=%s",
            self.cfg.masked(),
            self.realtime,
            sorted(self.cfg.readable) or "없음",
        )
        self.publish_status(connected=True)

    def identity_sweep(self) -> None:
        """워크스페이스 멤버를 이메일로 사번에 이어 둔다. 기동 시 1회.

        `ensure` 는 **사람이 봇을 쓸 때** 불린다. 그래서 봇을 써 볼 이유가 없던 사람은
        매핑이 없고, 매핑이 없으면 일정 DM 같은 자동 발송에서 조용히 빠진다 — 이메일이
        맞아도 그렇다. 실제로 8명 팀에서 4명만 잡혀 있었다(2026-09-11).

        기동을 막지 않는다. Slack 이 느리거나 권한이 없어도 봇은 떠야 한다 — 매핑이
        없으면 그 사람만 알림을 못 받고, 그건 다음 기동이나 첫 명령에서 이어진다.
        """
        with db_connect() as conn:
            if conn is None:
                log.info("[%s] DB 가 없어 사번 매핑 훑기를 건너뛴다", self.workspace)
                return
            try:
                result = identity_backfill(
                    conn, self.app.client, workspace=self.workspace
                )
            except Exception as e:
                log.warning("[%s] 사번 매핑 훑기 실패: %s", self.workspace, e)
                return
        log.info("[%s] %s", self.workspace, result.summary())
        if result.unmatched:
            # 사람이 손으로 확인할 목록. 이메일·이름은 남기지 않는다.
            log.warning(
                "[%s] 사번을 못 찾은 멤버 %d명 — Slack 프로필 이메일과 인사 이메일이"
                " 같은지 확인하라: %s",
                self.workspace, len(result.unmatched),
                ", ".join(result.unmatched[:20]),
            )

    def _backfill_channel_owners(self) -> None:
        """내부 기록이 없는 봇 가시 채널에 Slack 개설자를 보충한다.

        공개 채널과 봇이 초대된 비공개 채널만 bot token으로 볼 수 있다. Slack상
        creator가 봇 자신이면 실제 생성 요청자를 알 수 없으므로 기록하지 않는다.
        조회나 파일 기록 하나가 실패해도 봇 연결은 유지한다.
        """
        client = self.app.client
        bot_user_id = self._bot_user_id()
        cursor = None
        found = added = skipped_bot = skipped_missing = 0
        try:
            while True:
                response = client.conversations_list(
                    types="public_channel,private_channel",
                    exclude_archived=True,
                    limit=200,
                    cursor=cursor,
                )
                for channel in response.get("channels", []):
                    found += 1
                    channel_id = str(channel.get("id") or "")
                    creator = str(channel.get("creator") or "")
                    if not channel_id or not creator:
                        skipped_missing += 1
                        continue
                    if creator == bot_user_id:
                        skipped_bot += 1
                        continue
                    if self.channel_owners.record_if_missing(
                        self.workspace,
                        channel_id,
                        creator,
                        str(channel.get("name") or ""),
                    ):
                        added += 1
                cursor = str(
                    (response.get("response_metadata") or {}).get("next_cursor") or ""
                )
                if not cursor:
                    break
        except Exception as exc:
            log.warning("[%s] 채널 개설자 역채움 실패: %s", self.workspace, exc)
            return
        log.info(
            "[%s] 채널 개설자 역채움 visible=%d added=%d bot_creator=%d missing=%d",
            self.workspace,
            found,
            added,
            skipped_bot,
            skipped_missing,
        )

    def close(self) -> None:
        """재시작·종료 전에 Socket Mode 연결과 작업 스레드를 정리한다."""
        handler = getattr(self, "_handler", None)
        if handler is not None:
            with contextlib.suppress(Exception):
                handler.close()

    def publish_status(self, *, connected: bool) -> None:
        """관리 콘솔이 읽을 상태 파일을 남긴다.

        연결 상태·채널 수처럼 **Slack 만 아는 것**은 봇이 적어 둔다. 콘솔이 직접 Slack 을
        호출하면 콘솔에도 토큰이 필요해지고 rate limit 을 나눠 쓰게 된다.
        """
        channels = uninvited = 0
        try:
            client = self.app.client
            res = client.conversations_list(
                types="public_channel,private_channel", exclude_archived=True, limit=1000
            )
            found = res.get("channels", [])
            channels = sum(1 for c in found if c.get("is_member"))
            uninvited = len(found) - channels
        except Exception as e:
            log.debug("채널 수를 세지 못했습니다(상태 파일): %s", e)

        heartbeat.write(
            heartbeat.BotStatus(
                workspace=self.workspace,
                connected=connected,
                realtime=self.realtime,
                channels=channels,
                uninvited_channels=uninvited,
                spend_today_usd=round(self.engine.spent_today(), 6),
                limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
                started_at=self._started.astimezone(KST).isoformat(timespec="seconds"),
                updated_at=heartbeat.now_iso(),
                write_problem="; ".join(
                    f"{label}: {why}" for label, why in self.path_problems.items()
                )
                or None,
            )
        )


def enforce_archive_writable(problems: dict[str, str]) -> None:
    """아카이브에 못 쓰면 기동을 막는다.

    답은 하면서 원문을 버리는 상태가 가장 위험하다 — 사람은 봇이 정상이라 믿고,
    Slack 백필은 분당 1요청 제한이라 그 기간 원문은 사실상 복구가 안 된다.
    락(`_resolve_lock_path`)과 달리 이건 봇의 존재 이유라서 경고로 넘기지 않는다.

    감사기록(qa-log)은 경고만 한다 — 없어도 원문 자산이 사라지지는 않는다.
    """
    if "아카이브" not in problems:
        return
    if _truthy(os.getenv("ALLOW_READONLY_ARCHIVE", "0")):
        log.warning(
            "아카이브 쓰기 불가 상태로 기동합니다(ALLOW_READONLY_ARCHIVE=1) — %s. "
            "수집은 되지 않고 조회만 됩니다.",
            problems["아카이브"],
        )
        return
    raise ConfigError(
        f"아카이브에 쓸 수 없어 기동하지 않습니다 - {problems['아카이브']}. "
        "tybot.env 의 ARCHIVE_DIR 을 쓰기 가능한 경로(예: /var/lib/tybot/archive)로 "
        "지정하세요. 조회 전용으로 띄우려면 ALLOW_READONLY_ARCHIVE=1."
    )


def _kst_stamp(ts: str) -> str:
    """Slack ts → `2026-09-11 14:03` (KST). 못 읽으면 원값.

    아카이브 줄과 **같은 모양**이어야 한다. 모양이 다르면 모델이 두 근거를
    서로 다른 종류로 읽는다.
    """
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).astimezone(KST).strftime(
            "%Y-%m-%d %H:%M"
        )
    except (TypeError, ValueError):
        return ts


def specialist_hook(router, store=None, live_fetch=None):
    """엔진이 근거를 모은 뒤 부를 훅을 만든다. 전문가 문장 또는 `None`.

    **엔진은 전문가를 모른다.** 라우팅·DB·계약이 여기 있고 엔진은 결과만 받는다.
    `None` 이면 마스터가 그대로 답한다.

    워크스페이스는 `ctx` 에서 읽는다 — 엔진은 워크스페이스마다 하나가 아니라
    **전체에 하나**라, 봇 인스턴스에 매어 두면 마지막 봇의 것만 남는다.

    **이 호출이 답변을 막는 일은 없어야 한다.** 라우팅도 전문가도 없어도 되는
    기능이라, 예외를 통째로 삼키고 마스터로 넘긴다.
    """

    def hook(question: str, ctx, evidence: str):
        workspace = getattr(ctx, "workspace", "") or ""
        if not workspace:
            return None
        try:
            decision = specialist_router.route(question, workspace, router)
            if decision.went_to_master:
                # 후보가 아예 없으면 기록하지 않는다 — 질문마다 `none` 행을 쌓으면
                # 표가 잡음으로 차고, 정작 라우팅을 켰을 때 무엇이 새 판정인지
                # 구별할 수 없다.
                if specialist_router.available(workspace):
                    specialist_router.record(decision, workspace=workspace, elapsed_ms=0)
                return None
            # 근거는 이미 `visible_docs` 를 통과한 것뿐이다(원칙 3).
            # `authorization_id` 는 그 판정을 가리키고, 나중에 「무엇이 전문가에게
            # 갔나」 를 되짚는 근거가 된다.
            #
            # 도구를 쓰는 전문가에게는 **요청마다 새 묶음**을 만든다. `ctx` 를
            # 안에 가둬야 도구가 누구 권한으로 읽는지를 바꿀 수 없다.
            toolbox = None
            uses_tools = (
                decision.specialist is not None
                and decision.specialist.execution_mode == "tools"
            )
            if uses_tools and store is not None:
                from ..specialist_tools import ToolBox

                # `live_fetch` 는 워크스페이스를 받아야 한다 — 엔진은 전체에
                # 하나지만 Slack 클라이언트는 워크스페이스마다 다르다.
                # 여기서 묶어 두면 도구는 채널 ID 만 알면 된다.
                bound = (
                    (lambda cid, n: live_fetch(workspace, cid, n))
                    if live_fetch else None
                )
                toolbox = ToolBox(
                    store=store, ctx=ctx, live_fetch=bound,
                    here=getattr(ctx, "channel", "") or "",
                )
            return specialist_router.ask(
                decision,
                question=question,
                workspace=workspace,
                evidence=[evidence],
                router=router,
                toolbox=toolbox,
                # 실시간 조회는 Slack 클라이언트가 있을 때만 준다. 도구를 안 주면
                # 모델이 못 부른다 — 프롬프트로 막는 것보다 확실하다.
                live=bool(live_fetch),
                # 전문가가 못 답하면 빈 문자열. 호출부가 그것을 「마스터가 답한다」
                # 로 읽는다 — 여기서 마스터 답변을 만들면 답이 두 번 만들어진다.
                fallback=lambda: "",
                authorization_id=f"{workspace}:{getattr(ctx, 'role', '-') or '-'}",
            )
        except Exception as e:
            log.warning("[%s] 전문가 호출 실패: %s", workspace, e)
            return None

    return hook


def build_bots() -> list[WorkspaceBot]:
    """설정을 읽어 워크스페이스별 봇을 만든다. 공유 자원은 한 번만 생성한다."""
    from ..gateway.router import Router

    archive_dir = os.getenv("ARCHIVE_DIR", "./archive")
    qa_log = QALog(
        os.getenv("QA_LOG_DIR", "./qa-log"), write_md=_truthy(os.getenv("QA_LOG_MD", "1"))
    )
    problems = check_paths(archive_dir, str(qa_log.root))
    enforce_archive_writable(problems)

    store = ArchiveStore(archive_dir)
    router = Router.from_default_registry(
        daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
        default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
        # 재시작해도 당일 누적이 유지되어야 상한이 실제로 상한 역할을 한다.
        cost_state_path=cost_state_path(str(qa_log.root)),
    )
    # 실시간 조회는 워크스페이스별 클라이언트가 필요한데 엔진은 전체에 하나다.
    # 등록부를 먼저 만들고 봇이 생긴 뒤 채운다 — 훅이 만들어질 때는 아직 봇이 없다.
    live_bots: dict[str, WorkspaceBot] = {}

    def live_fetch(workspace: str, channel_id: str, limit: int) -> list[dict]:
        bot = live_bots.get(workspace)
        return bot.recent_messages(channel_id, limit) if bot else []

    engine = AnswerEngine(
        store, router,
        specialist=specialist_hook(router, store=store, live_fetch=live_fetch),
    )

    configs = load_workspaces()
    # 상태 트리가 다른 워크스페이스 이름을 표시하려면 키만으로는 부족하다.
    # 각 봇은 자기 cfg 만 알기 때문에 여기서 지도를 만들어 넘긴다.
    labels = {c.key: c.label for c in configs}

    bots = []
    for cfg in configs:
        bot = WorkspaceBot(
            cfg, store=store, engine=engine, qa_log=qa_log, archive_dir=archive_dir
        )
        bot.path_problems = problems
        bot.workspace_labels = labels
        live_bots[cfg.key] = bot
        bots.append(bot)
    return bots


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # systemd EnvironmentFile 과 파싱 규칙이 어긋나는 문제를 피하려고 직접 읽는다.
    from ..envfile import load_env_file

    log.info("환경설정 출처: %s", load_env_file())

    # 봇이 두 곳에서 뜨면 같은 질문에 두 번 답하고 LLM 비용이 두 배가 된다.
    # Slack 에 연결하기 **전에** 막는다 — 연결한 뒤에 알면 이미 중복 응답이 나간다.
    lock = instance_lock("bot")
    try:
        lock.acquire()
    except AlreadyRunning as e:
        log.error(
            "봇이 이미 실행 중입니다. 이 프로세스는 종료합니다. %s "
            "이미 뜬 프로세스를 끄려면: systemctl stop tybot",
            e,
        )
        return 1
    except LockUnavailable as e:
        log.error("단일 실행 락을 만들 수 없어 기동을 멈춥니다 — %s", e)
        return 1

    bots: list[WorkspaceBot] = []
    try:
        bots = build_bots()
        connected: list[WorkspaceBot] = []
        for bot in bots:
            try:
                bot.connect()
            except Exception as exc:
                log.exception(
                    "워크스페이스 연결 실패 — %s만 제외하고 나머지는 계속 기동합니다",
                    bot.workspace,
                )
                if os.getenv("DATABASE_URL"):
                    with contextlib.suppress(Exception):
                        from ..console.workspace_store import record_runtime_result

                        record_runtime_result(bot.workspace, str(exc))
                continue
            connected.append(bot)
            if os.getenv("DATABASE_URL"):
                with contextlib.suppress(Exception):
                    from ..console.workspace_store import record_runtime_result

                    record_runtime_result(bot.workspace, None)
        if not connected:
            log.error("연결에 성공한 워크스페이스가 없어 기동을 중단합니다")
            return 1
        bots = connected
        log.info("기동 완료 — 워크스페이스 %d개: %s", len(bots), [b.workspace for b in bots])

        # 연결은 백그라운드 스레드가 유지한다. 메인 스레드는 종료 신호를 기다리면서
        # 주기적으로 상태 파일을 갱신한다. 갱신이 멈추면 콘솔이 '상태 모름'으로 표시하므로,
        # 봇이 죽었는데 화면만 멀쩡해 보이는 상황이 생기지 않는다.
        stop = threading.Event()
        while not stop.wait(HEARTBEAT_SECONDS):
            restart = consume_restart_request()
            if restart is not None:
                log.warning(
                    "환경변수 설정 변경으로 봇을 재시작합니다 — actor=%s changed=%s",
                    restart.get("actor", "-"),
                    restart.get("changed", []),
                )
                break
            for bot in bots:
                bot.publish_status(connected=True)
    finally:
        for bot in bots:
            bot.close()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
