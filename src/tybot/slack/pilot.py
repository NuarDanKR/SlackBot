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

import logging
import os
import pathlib
import re
import threading
import time
from datetime import datetime, timezone

from ..access import RequestContext
from ..answer import Answer, AnswerEngine
from ..audit import QALog, QARecord
from ..intent import INGEST_ALL_RE, INGEST_RE, Intent
from ..archive import writer
from ..archive.files import file_lines
from ..archive.store import ArchiveStore
from ..archive.writer import KST
from ..workspaces import WorkspaceConfig, load_workspaces

log = logging.getLogger("tybot.slack")

MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
HISTORY_LIMIT = 15  # 신규 앱 conversations.history / replies 요청당 상한
THREAD_FETCH_LIMIT = 5  # 한 번의 수집에서 답글까지 받아올 스레드 수(rate limit 고려)


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
        # 전 채널·전 워크스페이스 통합조회 허용 사용자. 채널 멤버십과 워크스페이스 경계를 모두 우회한다.
        self.exec_users = {
            u.strip() for u in (os.getenv("EXEC_USERS") or "").split(",") if u.strip()
        }
        self.app = App(token=cfg.bot_token)
        self.store = store
        self.engine = engine
        self.qa_log = qa_log
        self._started = datetime.now(timezone.utc)
        self._last_ingest_at: datetime | None = None
        self._ingested = 0
        self._user_cache: dict[str, str] = {}
        self._chan_cache: dict[str, str] = {}
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

    # --- 핸들러 -----------------------------------------------------------
    def _register(self) -> None:
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

        try:
            client.reactions_add(channel=channel_id, timestamp=event["ts"], name="eyes")
        except Exception:
            pass

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

        ctx = self._context(client, user_id)
        ans = self.engine.respond(text, ctx, intent)
        finish(ans.to_slack(), intent=intent, ans=ans, ctx=ctx)

    # --- 수집 -------------------------------------------------------------
    def _messages_from(self, client, event: dict) -> list:
        """Slack 메시지 1건 → 원문 라인들(본문 + 첨부).

        첨부는 텍스트 형식만 본문을 넣고, 나머지는 목록만 남긴다(files.py 참조).
        """
        ts = datetime.fromtimestamp(float(event["ts"]), tz=timezone.utc)
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
        log.info(
            "워크스페이스 연결 — %s / 실시간수집=%s / 크로스열람=%s",
            self.cfg.masked(),
            self.realtime,
            sorted(self.cfg.readable) or "없음",
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


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # systemd EnvironmentFile 과 파싱 규칙이 어긋나는 문제를 피하려고 직접 읽는다.
    from ..envfile import load_env_file

    log.info("환경설정 출처: %s", load_env_file())

    bots = build_bots()
    for bot in bots:
        bot.connect()
    log.info("기동 완료 — 워크스페이스 %d개: %s", len(bots), [b.workspace for b in bots])
    # 연결은 백그라운드 스레드가 유지한다. 메인 스레드는 종료 신호를 기다린다.
    threading.Event().wait()


if __name__ == "__main__":
    main()
