"""소급 engine 을 **실시간과 같은 writer** 에 붙인다.

결정: 2026-09-29 supervisor 작업지시서 §4.3-6 · 4단계 지시 2·4

## 왜 어댑터가 따로 있나

`backfill` 은 Slack 을 읽고 계획을 세운다. 쓰는 일은 `ShadowCollector.ingest_message`
가 한다 — 실시간 이벤트가 지나는 **바로 그 메서드**다. 둘을 잇는 얇은 함수만
여기 둔다.

규칙을 복제하지 않는 것이 요점이다. PII 검사·ACL·revision·ACK·첨부 처리·중복
방지가 두 벌이 되면, 한쪽만 고치는 날 **같은 대화가 들어온 길에 따라 다르게
남는다.** 그 차이는 오류를 내지 않는다.

## 첨부도 같은 설정을 따른다

`separate_attachments` 는 워크스페이스 런타임 설정이고, 소급은 그것을 **그대로**
쓴다(지시 4). 소급만 분리하면 같은 채널에 두 모양이 섞이고, 나중에 정본 전환을
할 때 어느 것이 어느 경로로 들어왔는지 알 수 없다.

## 목적지는 collector 가 고른다

이 어댑터는 경로를 고르지 않는다. collector 가 채널 모드와 운영 쓰기 게이트를
확인한 뒤 shadow 또는 live 루트를 고른다. 소급도 실시간과 같은 판정을 지난다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .backfill import Found, Target

log = logging.getLogger("tybot.archive.backfill_adapter")

#: 소급이 「그 채널을 지금 모아도 되나」 를 다시 묻지 않게, 한 번 확인한 결과를 든다.
#: 메시지마다 물으면 Slack 호출이 메시지 수만큼 늘고 rate limit 에 걸린다.
ChannelResolver = Callable[[object, str], tuple[str, bool] | None]


def make_ingest(
    collector, client, *, resolver: ChannelResolver | None = None,
) -> Callable[[Target, Found], str]:
    """`backfill.run(ingest=...)` 에 넘길 함수를 만든다.

    돌려주는 값은 engine 이 세는 이름이다 — `written` · `duplicate` · `refused`
    · 그 외(실패). `ingest_message` 가 이미 그 이름을 돌려주므로 여기서 다시
    분류하지 않는다. 분류를 두 곳에 두면 한쪽이 「부분 성공」 을 성공으로 센다.
    """
    resolve = resolver or collector._collection_channel
    known: dict[str, tuple[str, bool] | None] = {}

    def ingest(target: Target, item: Found) -> str:
        channel_id = target.channel_id
        if channel_id not in known:
            known[channel_id] = resolve(client, channel_id)
        resolved = known[channel_id]
        if resolved is None:
            # 지금 모으면 안 되는 채널이다(초대가 없거나 사람이 꺼 뒀다). 소급이
            # 그 판정을 우회하면, 끈 채널이 소급으로 되살아난다.
            log.info("소급 대상이 아닌 채널이라 건너뛴다 ch=%s", channel_id)
            return "refused"
        channel, is_private = resolved
        return collector.ingest_message(
            client, item.payload,
            channel=channel, channel_id=channel_id, is_private=is_private,
        )

    return ingest


def targets_from(rows: list[dict], workspace: str) -> list[Target]:
    """DB 채널 목록을 소급 대상으로. **모을 수 있는 채널만.**

    `channel_membership.is_collectible` 과 같은 판정을 여기서 다시 쓰지 않는다 —
    행이 그 판정을 이미 거친 것을 받는다. 두 곳에서 판정하면 한 곳만 고치는 날이
    오고, 그날 끈 채널이 소급으로 들어온다.
    """
    out: list[Target] = []
    for row in rows:
        channel_id = str(row.get("channel_id") or "")
        if not channel_id:
            continue
        name = str(row.get("channel_name") or "")
        out.append(Target(
            workspace=workspace,
            channel_id=channel_id,
            channel="#" + name.removeprefix("#") if name else f"#{channel_id}",
            cursor=str(row.get("last_history_ts") or ""),
        ))
    return out
