"""A request-owned progress message, replaced by its final response."""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


class RequestProgress:
    def __init__(self, client, say, *, channel: str, root_ts: str, delay: float = 3):
        self.client = client
        self.say = say
        self.channel = channel
        self.root_ts = root_ts
        self.ts = ""
        self.done = False
        self.lock = threading.Lock()
        self.timer = threading.Timer(delay, self.show)
        self.timer.daemon = True
        self.slow_timer = threading.Timer(30, self.show_slow)
        self.slow_timer.daemon = True

    def start(self):
        self.timer.start()
        self.slow_timer.start()

    def show_slow(self):
        with self.lock:
            if self.done or not self.ts:
                return
            try:
                self.client.chat_update(
                    channel=self.channel, ts=self.ts,
                    text="아직 요청을 처리하고 있습니다. 완료되면 이 메시지를 답변으로 갱신하겠습니다.",
                )
            except Exception:  # noqa: BLE001 - progress is best effort
                log.warning("지연 안내 갱신 실패 ch=%s", self.channel)

    def show(self):
        # Serializing post and completion prevents a late orphan progress message.
        with self.lock:
            if self.done or self.ts:
                return
            try:
                response = self.say(
                    text="요청을 확인하고 있습니다. 자료 확인과 답변 작성에 시간이 걸리고 있습니다.",
                    thread_ts=self.root_ts,
                )
                self.ts = str(response.get("ts") or "")
            except Exception:  # noqa: BLE001 - progress failure must not block answers
                log.warning("진행 안내 게시 실패 ch=%s", self.channel)

    def send(self, **kwargs):
        with self.lock:
            self.done = True
            self.timer.cancel()
            self.slow_timer.cancel()
            if self.ts:
                update = dict(kwargs)
                update.pop("thread_ts", None)
                update.setdefault("blocks", [])
                try:
                    return self.client.chat_update(channel=self.channel, ts=self.ts, **update)
                except Exception:  # noqa: BLE001 - final posting remains available
                    log.warning("진행 안내 갱신 실패 ch=%s", self.channel)
                    kwargs["thread_ts"] = self.root_ts
            return self.say(**kwargs)

    def close(self):
        with self.lock:
            self.done = True
            self.timer.cancel()
            self.slow_timer.cancel()
