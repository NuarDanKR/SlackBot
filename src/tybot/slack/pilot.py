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
import pathlib
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
from ..config import cost_state_path
from ..intent import INGEST_ALL_RE, INGEST_RE, Intent
from ..lock import AlreadyRunning, LockUnavailable, instance_lock
from ..managed_env import consume_restart_request
from ..workspaces import WorkspaceConfig, load_workspaces

log = logging.getLogger("tybot.slack")

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
HISTORY_LIMIT = 15  # 신규 앱 conversations.history / replies 요청당 상한
THREAD_FETCH_LIMIT = 5  # 한 번의 수집에서 답글까지 받아올 스레드 수(rate limit 고려)
# 상태 파일 갱신 주기. heartbeat.STALE_AFTER_SECONDS(180초)보다 짧아야 한다.
HEARTBEAT_SECONDS = 60


def _clean(text: str) -> str:
    return MENTION_RE.sub("", text or "").strip()


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _writable(path: str) -> str | None:
    """쓰기 가능 여부 점검. 문제가 있으면 사유 문자열을 반환한다.

    아카이브 쓰기 실패는 '조용한 고장'의 대표 사례다 - 봇은 정상 응답하는데
    원문이 하나도 쌓이지 않는다. 기동 시점에 잡아 로그와 `상태`에 드러낸다.
    """
    d = pathlib.Path(path)
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return None
    except Exception as e:
        return f"{e.__class__.__name__}: {e}"


def _scope_label(ctx: RequestContext | None) -> str:
    """감사 기록용 권한범위 표기 — 채널명은 남기지 않는다(로그 자체가 유출 경로가 되지 않게)."""
    if ctx is None:
        return "-"
    if ctx.role == "exec":
        return "exec(전체)"
    return f"채널 {len(ctx.channels)}개"


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
                self._handle(event, client, say, in_channel=False)
                return
            if ctype in ("channel", "group") and self.realtime:
                self._ingest_live(client, event)

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
            f"{visibility} 업무 채널 <#{channel_id}>을 만들었습니다."
            "\n`/채널 이름변경`으로 표준 이름 안에서 변경할 수 있습니다."
            "\nSlack 기본 관리 권한이 필요하면 채널 정보 → 관리자로 지정에서 추가하세요."
            + suffix,
        )
        with contextlib.suppress(Exception):
            client.chat_postMessage(
                channel=channel_id,
                text=(
                    "이 채널은 TYBot 수집 대상 표준으로 생성되었습니다. "
                    "채널명을 규칙 밖으로 변경하면 이후 대화는 수집되지 않습니다."
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
            if thread_ts:
                say(text=reply, thread_ts=thread_ts)
            else:
                say(text=reply)  # 채널 본문에 답한다
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
            )
            log.info("%s", rec.log_line())
            self.qa_log.write(rec)

        # 명시 명령은 LLM 을 거치지 않는다(비용·지연 절약). 그 외 표현은 분류기가 판단한다.
        if INGEST_ALL_RE.search(text):
            intent = Intent("ingest_all", source="cmd")
        elif INGEST_RE.search(text):
            intent = Intent("ingest", source="cmd")
        else:
            intent = self.engine.classify(text)

        if intent.kind == "ingest_all":
            finish(self._ingest_all(client), intent=intent, ans=None, ctx=None)
            return
        if intent.kind == "ingest":
            if not in_channel:
                finish(
                    "수집은 채널에서만 실행할 수 있습니다. 대상 채널에서 `수집` 이라고 불러주세요.",
                    intent=intent, ans=None, ctx=None,
                )
                return
            finish(self._ingest_channel(client, channel_id), intent=intent, ans=None, ctx=None)
            return
        if intent.kind == "status":
            finish(self._status(client), intent=intent, ans=None, ctx=None)
            return
        if intent.kind == "help":
            finish(self._help(), intent=intent, ans=None, ctx=None)
            return
        if intent.kind == "memory":
            finish(self._memory(user_id), intent=intent, ans=None, ctx=None)
            return

        ctx = self._context(client, user_id)
        ans = self.engine.respond(text, ctx, intent)
        finish(ans.to_slack(), intent=intent, ans=ans, ctx=ctx)

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


def check_paths(archive_dir: str, qa_dir: str) -> dict[str, str]:
    """아카이브·감사기록 쓰기 가능 여부. 조용한 고장을 기동 시점에 드러낸다."""
    problems: dict[str, str] = {}
    for label, path in (("아카이브", archive_dir), ("감사기록", qa_dir)):
        why = _writable(path)
        if why:
            problems[label] = f"{path} ({why})"
            log.error(
                "%s 디렉터리에 쓸 수 없습니다: %s - %s. "
                "tybot.env 의 ARCHIVE_DIR/QA_LOG_DIR 를 /var/lib/tybot 아래로 지정하세요.",
                label, path, why,
            )
    if not problems:
        log.info("경로 점검 통과 - archive=%s qa_log=%s", archive_dir, qa_dir)
    return problems


def build_bots() -> list[WorkspaceBot]:
    """설정을 읽어 워크스페이스별 봇을 만든다. 공유 자원은 한 번만 생성한다."""
    from ..gateway.router import Router

    archive_dir = os.getenv("ARCHIVE_DIR", "./archive")
    qa_log = QALog(
        os.getenv("QA_LOG_DIR", "./qa-log"), write_md=_truthy(os.getenv("QA_LOG_MD", "1"))
    )
    problems = check_paths(archive_dir, str(qa_log.root))

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
