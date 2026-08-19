"""파일럿 봇 — 단일 워크스페이스, Socket Mode(아웃바운드 전용).

수집 경로 두 가지:
1. **실시간**(기본) — message.channels / message.groups 이벤트로 들어오는 즉시 원문 append.
   Slack 신규 비-마켓플레이스 앱은 conversations.history 가 분당 1요청/15건으로 제한되므로
   이 경로가 본선이다. 비공개 채널은 봇이 초대된 곳만 이벤트가 온다.
2. **백필**(`수집`) — 과거 대화 보충용. rate limit 때문에 느리다.

실행: python -m tybot.slack.pilot
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone

from ..access import RequestContext
from ..answer import Answer, AnswerEngine
from ..audit import QALog, QARecord
from ..intent import Intent
from ..archive import writer
from ..archive.store import ArchiveStore
from ..archive.writer import KST

log = logging.getLogger("tybot.slack")

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
INGEST_ALL_RE = re.compile(r"^\s*(전체\s*수집|수집\s*전체|ingest\s*all)\b")
INGEST_RE = re.compile(r"^\s*(수집|취합|ingest)\b")
HISTORY_LIMIT = 15  # 신규 앱 conversations.history 상한


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


class PilotBot:
    def __init__(self) -> None:
        from slack_bolt import App

        self.workspace = os.getenv("PILOT_WORKSPACE", "pilot")
        self.archive_dir = os.getenv("ARCHIVE_DIR", "./archive")
        self.bot_name = os.getenv("BOT_NAME", "tybot")
        self.realtime = _truthy(os.getenv("REALTIME_INGEST", "1"))
        # 전 채널 통합조회 허용 사용자(Slack user id, 콤마 구분). 채널 멤버십 필터를 우회한다.
        self.exec_users = {
            u.strip() for u in (os.getenv("EXEC_USERS") or "").split(",") if u.strip()
        }
        self.app = App(token=os.environ["SLACK_BOT_TOKEN"])
        self.store = ArchiveStore(self.archive_dir)
        # 감사 기록은 아카이브 밖에 둔다 - 봇 답변이 근거로 재사용되면 요약 재귀가 된다(원칙 1).
        self.qa_log = QALog(
            os.getenv("QA_LOG_DIR", "./qa-log"),
            write_md=_truthy(os.getenv("QA_LOG_MD", "1")),
        )
        self.engine = AnswerEngine(self.store, self._router())
        self._started = datetime.now(timezone.utc)
        self._last_ingest_at: datetime | None = None
        self._ingested = 0
        self._user_cache: dict[str, str] = {}
        self._chan_cache: dict[str, str] = {}
        self._register()

    def _router(self):
        from ..gateway.router import Router

        return Router.from_default_registry(
            daily_limit_usd=float(os.getenv("DAILY_COST_LIMIT_USD", "50")),
            default_model=os.getenv("DEFAULT_MODEL", "claude-sonnet-5"),
        )

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
        return RequestContext(workspace=self.workspace, channels=frozenset(channels))

    # --- 핸들러 -----------------------------------------------------------
    def _register(self) -> None:
        @self.app.event("app_mention")
        def on_mention(event, client, say):
            if event.get("bot_id"):
                return
            self._handle(event, client, say, in_channel=True)

        @self.app.event("message")
        def on_message(event, client, say):
            if event.get("bot_id") or event.get("subtype"):
                return  # 1겹: 봇 출력·시스템 메시지는 아카이브 대상 아님
            ctype = event.get("channel_type")
            if ctype == "im":
                self._handle(event, client, say, in_channel=False)
                return
            if ctype in ("channel", "group") and self.realtime:
                self._ingest_live(client, event)

    def _handle(self, event, client, say, *, in_channel: bool) -> None:
        text = _clean(event.get("text", ""))
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        thread_ts = event.get("thread_ts") or event.get("ts")
        started = time.monotonic()

        try:
            client.reactions_add(channel=channel_id, timestamp=event["ts"], name="eyes")
        except Exception:
            pass

        def finish(reply: str, *, intent: Intent, ans: Answer | None, ctx: RequestContext | None):
            """모든 응답 경로가 여기로 모인다 — 경로마다 로그가 달라지지 않게."""
            say(text=reply, thread_ts=thread_ts)
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

        if INGEST_ALL_RE.match(text):
            finish(self._ingest_all(client), intent=Intent("ingest_all", source="cmd"), ans=None, ctx=None)
            return
        if in_channel and INGEST_RE.match(text):
            finish(
                self._ingest_channel(client, channel_id),
                intent=Intent("ingest", source="cmd"),
                ans=None,
                ctx=None,
            )
            return

        # 의도 분류는 LLM 이 한다(표현이 바뀌어도 새지 않게). 실패 시 규칙 기반 폴백.
        intent = self.engine.classify(text)

        if intent.kind == "status":
            finish(self._status(client), intent=intent, ans=None, ctx=None)
            return
        if intent.kind == "help":
            finish(self._help(), intent=intent, ans=None, ctx=None)
            return

        ctx = self._context(client, user_id)
        ans = self.engine.respond(text, ctx, intent)
        finish(ans.to_slack(), intent=intent, ans=ans, ctx=ctx)

    # --- 수집 -------------------------------------------------------------
    def _ingest_live(self, client, event) -> None:
        """실시간 원문 1건 append. 실패해도 봇은 계속 살아 있어야 한다."""
        channel = self._channel_name(client, event.get("channel", ""))
        msg = writer.IncomingMessage(
            ts=datetime.fromtimestamp(float(event["ts"]), tz=timezone.utc),
            speaker=self._user_name(client, event.get("user", "unknown")),
            text=event.get("text", ""),
        )
        try:
            r = writer.ingest(
                self.archive_dir,
                workspace=self.workspace,
                channel=channel,
                messages=[msg],
                acl=[channel],
            )
        except Exception as e:
            log.error("실시간 수집 실패 ch=%s: %s", channel, e)
            return
        if r.written:
            self._ingested += r.written
            self._last_ingest_at = datetime.now(timezone.utc)
        if r.refused:
            log.warning("제외 대상으로 미저장 ch=%s 사유=%s", channel, r.refused[0][1])

    def _ingest_channel(self, client, channel_id: str) -> str:
        channel = self._channel_name(client, channel_id)
        try:
            res = client.conversations_history(channel=channel_id, limit=HISTORY_LIMIT)
        except Exception as e:
            return (
                f"채널 히스토리를 읽지 못했습니다: {e}\n"
                f"`/invite @{self.bot_name}` 와 `channels:history` 권한을 확인하세요."
            )

        msgs = []
        for m in reversed(res.get("messages", [])):
            if m.get("bot_id") or m.get("subtype"):
                continue
            msgs.append(
                writer.IncomingMessage(
                    ts=datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc),
                    speaker=self._user_name(client, m.get("user", "unknown")),
                    text=m.get("text", ""),
                )
            )

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
        if r.refused:
            out.append(f"제외 대상 {len(r.refused)}건(개인정보/등기부 등)은 아카이브하지 않았습니다.")
        out.append(
            f"백필은 신규 앱 제한으로 회당 {HISTORY_LIMIT}건까지입니다. "
            "이후 대화는 실시간으로 자동 수집됩니다."
        )
        return "\n".join(out)

    def _ingest_all(self, client) -> str:
        """봇이 볼 수 있는 모든 채널 백필. 공개 채널은 자가참여 시도."""
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

        joined, done, skipped_private, failed = [], 0, [], []
        for ch in channels:
            name = "#" + ch["name"]
            if not ch.get("is_member"):
                if ch.get("is_private"):
                    skipped_private.append(name)  # 봇은 비공개 채널에 자가참여 불가
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

    def _help(self) -> str:
        return "\n".join(
            [
                f"*@{self.bot_name} 사용법* — 아카이브에 쌓인 원문만 근거로 답합니다.",
                "• `요약` / `이번주 진행상황` / `30일 요약` — 기간별 정리",
                "• `<키워드> 얼마야?` 같은 구체 질문 — 원문 검색 + 출처",
                "• `어느 방향이 나을까?` 같은 판단·권고 요청 — 원문이 있으면 근거로, 없으면 일반 판단으로 답합니다",
                "• `수집` — 이 채널 과거 대화 백필 / `전체수집` — 전 채널",
                "• `상태` — 연결·수집 상태 / `도움말` — 이 안내",
                "• `--model=claude-opus-4-8 질문` — 모델 지정",
                "*사실*은 아카이브 원문만 근거로 답합니다. 근거가 없으면 추측하지 않습니다.",
            ]
        )

    def _status(self, client=None) -> str:
        """봇 자체 상태 — 아카이브 질의가 아니므로 LLM 을 호출하지 않는다(비용 0)."""
        docs = self.store.docs()
        broken = self.store.broken()
        up = datetime.now(timezone.utc) - self._started
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
        lines = [
            f"*연결*: {conn}{who}",
            f"*가동*: {hours}시간 {rem // 60}분 · 실시간 수집 {'ON' if self.realtime else 'OFF'}"
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
        return "\n".join(lines)

    def start(self) -> None:
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        log.info(
            "파일럿 봇 기동 — workspace=%s archive=%s realtime=%s exec=%d명",
            self.workspace,
            self.archive_dir,
            self.realtime,
            len(self.exec_users),
        )
        SocketModeHandler(self.app, os.environ["SLACK_APP_TOKEN"]).start()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    PilotBot().start()


if __name__ == "__main__":
    main()
