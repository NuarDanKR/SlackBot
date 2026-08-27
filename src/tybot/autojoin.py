"""규칙에 맞는 채널 자동 참여.

`/invite` 를 사람이 하지 않아도, 채널 이름이 규칙에 맞으면 봇이 스스로 들어간다.
들어간 뒤에는 기존 실시간 수집 경로가 그대로 동작한다(대화·스레드 답글·첨부).

## Slack 제약 — 이것만은 못 우회한다
| | 초대 없이 가능? |
|---|---|
| **공개 채널** | **가능.** `conversations.join` 으로 봇이 스스로 참여 |
| **비공개 채널** | **불가.** 목록 조회조차 안 된다. 사람이 `/invite` 해야 한다 |

봇 토큰은 **참여하지 않은 채널의 내용을 읽을 수 없다.** 그래서 "초대 없이 수집"은
정확히는 "초대 대신 봇이 스스로 참여"다. 채널 멤버 목록에는 봇이 보인다.

## 왜 이름 규칙으로 거르나
전 채널을 무조건 담으면 잡담·개인·외부 협업 채널까지 아카이브에 들어간다.
이름 규칙은 "업무 채널"을 선언적으로 표시하는 장치이고, 조직코드까지 얻는다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .channels import parse

log = logging.getLogger("tybot.autojoin")


@dataclass
class JoinResult:
    joined: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    skipped_rule: list[str] = field(default_factory=list)  # 이름이 규칙과 다름
    need_invite: list[str] = field(default_factory=list)  # 비공개 - 사람이 초대해야 함
    failed: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"autojoin joined={len(self.joined)} already={len(self.already)} "
            f"skipped={len(self.skipped_rule)} need_invite={len(self.need_invite)} "
            f"failed={len(self.failed)}"
        )


def list_channels(client) -> list[dict]:
    """봇이 볼 수 있는 채널 전부. 비공개는 이미 멤버인 것만 보인다(Slack 설계)."""
    out: list[dict] = []
    cursor = None
    while True:
        res = client.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=True,
            limit=200,
            cursor=cursor,
        )
        out.extend(res.get("channels", []))
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def sweep(client, *, dry_run: bool = False) -> JoinResult:
    """규칙에 맞는 공개 채널에 참여한다."""
    result = JoinResult()
    for ch in list_channels(client):
        name = "#" + ch.get("name", "")
        spec = parse(name)

        if spec is None:
            result.skipped_rule.append(name)
            continue
        if ch.get("is_member"):
            result.already.append(name)
            continue
        if ch.get("is_private"):
            # 봇이 스스로 들어갈 수 없다. 사람이 초대해야 한다.
            result.need_invite.append(name)
            continue
        if dry_run:
            result.joined.append(name)
            continue
        try:
            client.conversations_join(channel=ch["id"])
        except Exception as e:  # noqa: BLE001 - 채널 하나 실패가 전체를 막지 않는다
            result.failed.append((name, str(e)))
            log.warning("자동 참여 실패 %s: %s", name, e)
            continue
        result.joined.append(name)
        log.info("자동 참여 %s (%s)", name, spec.label())
    return result


def on_channel_event(client, channel: dict, *, dry_run: bool = False) -> str | None:
    """채널 생성·이름 변경 이벤트 처리. 참여했으면 채널명을 돌려준다.

    새 채널이 규칙에 맞으면 그 즉시 들어간다 - 다음 정기 스윕까지 기다리지 않는다.
    """
    name = "#" + (channel.get("name") or "")
    spec = parse(name)
    if spec is None or channel.get("is_private") or channel.get("is_member"):
        return None
    if dry_run:
        return name
    try:
        client.conversations_join(channel=channel["id"])
    except Exception as e:  # noqa: BLE001
        log.warning("자동 참여 실패 %s: %s", name, e)
        return None
    log.info("새 채널 자동 참여 %s (%s)", name, spec.label())
    return name
