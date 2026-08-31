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

from .. import heartbeat
from ..access import RequestContext
from ..answer import Answer, AnswerEngine
from ..archive import writer
from ..archive.canvas import canvas_lines
from ..archive.files import file_lines
from ..archive.store import ArchiveStore
from ..archive.writer import KST
from ..audit import QALog, QARecord
from ..autojoin import on_channel_event, sweep
from ..channel_management import (
    ChannelNameError,
    ChannelOwnerStore,
    create_modal,
    rename_modal,
    request_from_view,
)
from ..channels import parse, should_collect
from ..compose import join_sections, truncated_notice, write_from_facts
from ..config import cost_state_path
from ..failures import failure_message
from ..feedback import FeedbackLog, correction_text, reaction_kind
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
from ..workspaces import ConfigError, WorkspaceConfig, load_workspaces

log = logging.getLogger("tybot.slack")

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
    "• 비공개 채널은 봇이 스스로 들어갈 수 없습니다. "
    "`/채널` 로 만들었거나 `/invite @{bot}` 한 채널만 수집됩니다.\n"
    "• 개인 인적사항·부동산 등본류·개인이 특정되는 목록은 올리지 마세요. "
    "아카이브에 저장되지 않도록 걸러지지만, "
    "Slack 대화에는 그대로 남습니다."
)


BLANK = chr(10) * 2  # 문단 구분


def _clean(text: str) -> str:
    return MENTION_RE.sub("", text or "").strip()


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _scope_label(ctx: RequestContext | None) -> str:
    """감사 기록용 권한범위 표기 — 채널명은 남기지 않는다(로그 자체가 유출 경로가 되지 않게)."""
    if ctx is None:
        return "-"
    if ctx.role == "exec":
        return "exec(전체)"
    return f"채널 {len(ctx.channels)}개"


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

    def autojoin_sweep(self) -> None:
        """규칙에 맞는 공개 채널에 자동 참여. 기동 시 1회."""
        if not self.autojoin:
            log.info("[%s] 자동 참여 비활성(AUTOJOIN_CHANNELS=0)", self.workspace)
            return
        try:
            r = sweep(self.app.client)
        except Exception as e:
            log.error("[%s] 자동 참여 스윕 실패: %s", self.workspace, e)
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
            action = (command.get("text") or "").strip().replace(" ", "")
            if action in ("", "생성", "만들기"):
                self._open_create_modal(client, command["trigger_id"], command["user_id"])
                return
            if action in ("이름변경", "이름바꾸기"):
                self._open_rename_modal(
                    client,
                    command["trigger_id"],
                    command["user_id"],
                    command.get("channel_id", ""),
                    respond,
                )
                return
            respond(
                "사용법: `/채널 생성`, `/채널 이름변경`, `/채널 도움말`\n"
                "명령어 없이 `/채널`만 입력해도 생성 화면이 열립니다.",
                response_type="ephemeral",
            )

        @self.app.view("tybot_create_channel")
        def on_create_submission(ack, body, client, view):
            try:
                request = request_from_view(view, include_channel_options=True)
            except ChannelNameError as e:
                ack(response_action="errors", errors={e.block_id: str(e)})
                return
            ack()
            user_id = (body.get("user") or {}).get("id", "")
            self._create_channel(client, user_id, request)

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
            # 명령 뒤에 적은 문장은 질문 칸에 미리 채워 준다. 두 번 입력하지 않게.
            try:
                client.views_open(
                    trigger_id=command["trigger_id"],
                    view=poll_create_modal(
                        channel_id=command.get("channel_id", ""), prefill_question=text
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

        @self.app.event("reaction_added")
        def on_reaction_added(event):
            self._handle_feedback_reaction(event, action="added")

        @self.app.event("reaction_removed")
        def on_reaction_removed(event):
            self._handle_feedback_reaction(event, action="removed")

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

    def _handle_feedback_reaction(self, event: dict, *, action: str) -> None:
        kind = reaction_kind(str(event.get("reaction") or ""))
        item = event.get("item") or {}
        if kind is None or item.get("type") != "message":
            return
        channel_id = str(item.get("channel") or "")
        answer_ts = str(item.get("ts") or "")
        row = self.qa_log.find_answer(
            self.workspace, channel_id, response_ts=answer_ts
        )
        if not row or not row.get("record_id"):
            return  # TYBot 답변이 아닌 메시지의 반응은 수집하지 않는다.
        self.feedback_log.write(
            workspace=self.workspace,
            channel_id=channel_id,
            qa_record_id=str(row["record_id"]),
            answer_ts=answer_ts,
            actor=str(event.get("user") or ""),
            kind=kind,
            action=action,
        )

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

    def _open_create_modal(self, client, trigger_id: str, user_id: str) -> None:
        metadata = json.dumps({"user_id": user_id}, ensure_ascii=False)
        try:
            client.views_open(trigger_id=trigger_id, view=create_modal(metadata))
        except Exception as e:
            log.warning("[%s] 채널 생성 모달 열기 실패: %s", self.workspace, e)

    # --- 투표 -------------------------------------------------------------
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
            with contextlib.suppress(Exception):
                client.chat_postEphemeral(
                    channel=poll.channel_id,
                    user=poll.creator,
                    text=(
                        "투표를 올리지 못했습니다. 이 채널에 봇이 초대되어 있는지 확인해 주세요"
                        f" (`/invite @{self.bot_name}`)."
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
        return user_id in self.channel_admin_users or self.channel_owners.is_owner(
            self.workspace, channel_id, user_id
        )

    def _open_rename_modal(
        self, client, trigger_id: str, user_id: str, channel_id: str, respond
    ) -> None:
        if not channel_id or not self._can_manage_channel(channel_id, user_id):
            respond(
                "이 채널의 최초 생성 요청자 또는 TYBot 채널 관리자만 이름을 변경할 수 있습니다.",
                response_type="ephemeral",
            )
            return
        try:
            channel = client.conversations_info(channel=channel_id)["channel"]
            spec = parse(channel.get("name", ""))
            if spec is None:
                respond(
                    "현재 채널명이 TYBot 표준 형식이 아니어서 이 화면에서 변경할 수 없습니다.",
                    response_type="ephemeral",
                )
                return
            metadata = json.dumps({"channel_id": channel_id}, ensure_ascii=False)
            client.views_open(
                trigger_id=trigger_id,
                view=rename_modal(metadata, spec),
            )
        except Exception as e:
            log.warning("[%s] 채널 이름 변경 모달 열기 실패: %s", self.workspace, e)
            respond("채널 정보를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.")

    def _notify_user(self, client, user_id: str, text: str) -> None:
        if not user_id:
            return
        try:
            client.chat_postMessage(channel=user_id, text=text)
        except Exception as e:
            log.warning("[%s] 채널 관리 결과 DM 실패 user=%s: %s", self.workspace, user_id, e)

    def _create_channel(self, client, user_id: str, request) -> None:
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
            self._notify_user(
                client,
                user_id,
                f"채널을 만들지 못했습니다: `{request.name}`\nSlack 앱 권한과 채널명을 확인해 주세요.",
            )
            return

        try:
            self.channel_owners.record(self.workspace, channel_id, user_id, actual_name)
        except OSError as e:
            # 소유권을 기록하지 못하면 이름 변경 권한은 막히지만, 만들어진 채널은 유지한다.
            log.error("[%s] 채널 소유권 기록 실패 channel=%s: %s", self.workspace, channel_id, e)

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
        self._notify_user(
            client,
            user_id,
            f"{visibility} 업무 채널 <#{channel_id}>을 만들었습니다. "
            f"이름이 수집 규칙에 맞아 **이 채널의 대화는 아카이브에 쌓입니다.**"
            "\n`/채널 이름변경`으로 표준 이름 안에서 변경할 수 있습니다. "
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

    def _handle(self, event, client, say, *, in_channel: bool) -> None:
        """요청 처리 진입점. 어떤 예외가 나도 **사람에게 무슨 일인지 알린다.**

        예전에는 예외가 여기서 조용히 사라져 👀 만 붙고 답이 없었다. 사용자는 봇이
        무시했다고 생각하고, 원인은 서버 로그를 보는 사람만 알 수 있었다.
        """
        started = time.monotonic()
        try:
            self._handle_request(event, client, say, in_channel=in_channel)
        except Exception as e:
            log.exception("요청 처리 실패 ws=%s ch=%s", self.workspace, event.get("channel"))
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
        text = _clean(event.get("text", ""))
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
            response = (
                say(text=reply, thread_ts=thread_ts) if thread_ts else say(text=reply)
            )
            rec = QARecord.build(
                workspace=self.workspace,
                channel=self._chan_cache.get(channel_id, channel_id),
                channel_id=channel_id,
                user=user_id,
                user_name=self._user_name(client, user_id) if user_id else "unknown",
                question=text,
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
            )
            log.info("%s", rec.log_line())
            self.qa_log.write(rec)

        # 명시 명령은 LLM 을 거치지 않는다(비용·지연 절약).
        # 그 외에는 1차 LLM 이 **하위질문 목록**으로 분해한다 - 사람은 한 번에 여러 가지를
        # 묻는데, 라벨 하나만 고르던 예전 구조에서는 그중 하나만 처리 경로에 도달했다.
        if INGEST_ALL_RE.search(text):
            tasks = [Intent("ingest_all", source="cmd", question=text)]
        elif INGEST_RE.search(text):
            tasks = [Intent("ingest", source="cmd", question=text)]
        else:
            tasks = self.engine.plan(text)
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
                ctx = self._context(client, user_id)
            ans = self.engine.respond(q, ctx, task)
            last = ans
            sections.append(ans.to_slack())

        if dropped:
            sections.append(truncated_notice(dropped))

        # 감사기록에는 처리한 의도를 전부 남긴다(예: "memory+summary").
        merged = Intent(
            kind="+".join(dict.fromkeys(x.kind for x in tasks)),
            source=first.source,
            question=text,
        )
        finish(join_sections(sections), intent=merged, ans=last, ctx=ctx)

    # --- 수집 -------------------------------------------------------------
    def _messages_from(self, client, event: dict) -> list:
        """Slack 메시지 1건 → 원문 라인들(본문 + 첨부).

        첨부는 텍스트 형식만 본문을 넣고, 나머지는 목록만 남긴다(files.py 참조).
        """
        ts = datetime.fromtimestamp(float(event["ts"]), tz=UTC)
        speaker = self._user_name(client, event.get("user", "unknown"))
        out = []
        body = (event.get("text") or "").strip()
        if body:
            out.append(writer.IncomingMessage(ts=ts, speaker=speaker, text=body))
        if event.get("files"):
            lines, warns = file_lines(event["files"], self.cfg.bot_token)
            for ln in lines:
                out.append(writer.IncomingMessage(ts=ts, speaker=speaker, text=ln))
            for w in warns:
                log.warning("첨부 처리 경고 ch=%s: %s", event.get("channel"), w)
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
            log.error("실시간 수집 실패 ch=%s: %s", channel, e)
            return
        if r.written:
            self._ingested += r.written
            self._last_ingest_at = datetime.now(UTC)
        if r.refused:
            log.warning("제외 대상으로 미저장 ch=%s 사유=%s", channel, r.refused[0][1])

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
            "이전_답변_기억": False,
            "이유": [
                "봇 답변을 다시 근거로 쓰면 틀린 내용이 사실처럼 굳는다(요약 재귀).",
                "근거는 사람이 쓴 원문이어야 출처를 붙이고 검증할 수 있다.",
            ],
            "예외": "스레드 안에서 이어 물으면 그 스레드에 답한다. "
                    "다만 이전 답변 내용을 근거로 삼지는 않는다.",
            "매_질문마다": "아카이브 원문에서 처음부터 다시 찾는다.",
            "본인_최근_질문": [
                {"시각": ts[5:16].replace("T", " "), "질문": q} for ts, q in recent
            ],
            "최근_질문_주의": "감사 기록이며 답변 생성에는 쓰이지 않는다. 본인 질문만 보인다.",
        }

    def _status_facts(self, client=None) -> dict:
        """상태 요약을 사실로 넘긴다. 상세 목록은 코드가 만든 블록을 그대로 붙인다."""
        docs = self.store.docs()
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
            "아카이브_문서수": len(docs),
            "아카이브_원문줄수": sum(len(d.raw_lines) for d in docs),
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
            "*이전 답변을 기억하지 않습니다.* 질문마다 아카이브 원문에서 처음부터 찾습니다.",
            "",
            "그렇게 만든 이유:",
            "• 제 답변을 다시 근거로 쓰면 틀린 내용이 사실처럼 굳습니다(요약 재귀).",
            "• 근거는 사람이 쓴 원문뿐이어야 출처를 붙이고 검증할 수 있습니다.",
            "",
            "다만 스레드 안에서 이어 물으시면 그 스레드에 답합니다. "
            "이전 답변 내용을 근거로 삼지는 않습니다.",
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
                "• `이전 답변 기억나?` — 기억 여부와 그 이유(매번 원문에서 다시 찾습니다)",
                "• `--model=claude-opus-4-8 질문` — 모델 지정",
                "*사실*은 아카이브 원문만 근거로 답합니다. 근거가 없으면 추측하지 않습니다.",
            ]
        )

    def _status(self, client=None) -> str:
        """봇 자체 상태 — 아카이브 질의가 아니므로 LLM 을 호출하지 않는다(비용 0)."""
        docs = self.store.docs()
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
            f"*아카이브*: `{self.archive_dir}` — 문서 {len(docs)}건, "
            f"원문 {sum(len(d.raw_lines) for d in docs)}줄",
        ]
        for d in sorted(docs, key=lambda d: -len(d.raw_lines))[:10]:
            lines.append(f"  • {d.channel} — {len(d.raw_lines)}줄 (최근 {d.last_ingested or '-'})")
        if broken:
            lines.append(f"⚠️ 형식 위반 {len(broken)}건: " + ", ".join(p.name for p, _ in broken))
        for label, why in self.path_problems.items():
            lines.append(f"🛑 *{label} 쓰기 불가* — {why}. 수집이 저장되지 않습니다.")
        if not docs and not self.path_problems:
            lines.append("ℹ️ 아직 수집된 원문이 없습니다. 채널에서 `수집` 또는 대화가 쌓이길 기다리세요.")
        return "\n".join(lines)

    def connect(self) -> None:
        """Socket Mode 연결을 비동기로 연다(블로킹하지 않는다).

        워크스페이스마다 연결이 하나씩이므로, 여러 개를 띄우려면 블로킹하면 안 된다.
        """
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        self._handler = SocketModeHandler(self.app, self.cfg.app_token)
        self._handler.connect()
        self.autojoin_sweep()
        log.info(
            "워크스페이스 연결 — %s / 실시간수집=%s / 크로스열람=%s",
            self.cfg.masked(),
            self.realtime,
            sorted(self.cfg.readable) or "없음",
        )
        self.publish_status(connected=True)

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
    engine = AnswerEngine(
        store,
        Router.from_default_registry(
            daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
            default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
            # 재시작해도 당일 누적이 유지되어야 상한이 실제로 상한 역할을 한다.
            cost_state_path=cost_state_path(str(qa_log.root)),
        ),
    )

    bots = []
    for cfg in load_workspaces():
        bot = WorkspaceBot(
            cfg, store=store, engine=engine, qa_log=qa_log, archive_dir=archive_dir
        )
        bot.path_problems = problems
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
        for bot in bots:
            bot.connect()
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
