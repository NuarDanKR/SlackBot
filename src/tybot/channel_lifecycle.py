"""보관·삭제된 채널 기록과 근거 제외 판정(B-51).

## 무엇이 문제였나

Slack 채널을 보관(archive)하거나 지워도 **우리 아카이브에는 원문이 그대로 남는다.**
그게 설계대로다 — 원문은 지우지 않는다(원칙 1). 그런데 답변 경로가 그 원문을
지금도 살아 있는 자료처럼 근거로 쓰고 요약에 넣었다.

사람 입장에서는 **없앤 채널의 이야기가 오늘 답에 섞여 나오는 것**이다. 출처를
눌러도 채널이 없으니 확인할 수도 없다. 「자료가 틀렸다」 보다 나쁜 상태다 —
확인할 방법이 없는 자료가 답에 들어간다.

## 어떻게 고쳤나

채널이 보관·삭제되면 **그 사실을 기록**하고, 기본적으로 근거에서 뺀다.
원문은 그대로 둔다 — 빼는 것은 **근거로 쓰는 것**뿐이다.

포함할지는 운영이 정한다(`INCLUDE_RETIRED_CHANNELS`, 콘솔 환경설정).
**기본값은 제외**다(원칙 3 — 막는 쪽이 기본값). 포함으로 켜면 출처에
`(보관 채널)` 이 붙는다 — 조용히 섞어 넣지 않는다.

## 기록은 어디서 오나

1. Slack 이벤트 — `channel_archive`·`channel_deleted` 등. 즉시 반영된다.
2. 대조(`reconcile`) — 봇이 꺼져 있는 동안 생긴 변화를 따라잡는다.

**둘 다 필요하다.** 이벤트만 쓰면 봇이 내려간 사이의 변화를 영영 놓치고,
대조만 쓰면 다음 대조까지 삭제된 채널이 답에 계속 나온다.

## 판정은 열리는 쪽으로 실패한다

기록을 못 읽으면 **아무것도 제외하지 않는다.** 여기서 막는 쪽으로 기울면
디스크 오류 하나로 멀쩡한 채널의 자료가 통째로 사라지고, 봇은 "자료가 없다" 고
답한다 — 사람은 그것을 수집 실패로 읽는다. 권한이 아니라 **선도** 문제라서
기본값의 방향이 반대다.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("tybot.channel_lifecycle")

STATE_FILE = "retired-channels.json"
SCHEMA_VERSION = 1

# 왜 근거에서 뺐는가. **비민감 코드만** 남긴다.
ARCHIVED = "archived"
DELETED = "deleted"
REASONS = (ARCHIVED, DELETED)
REASON_LABELS = {ARCHIVED: "보관", DELETED: "삭제"}

# 출처에 붙는 표시. 포함으로 켠 경우에만 보인다 — **조용히 섞지 않는다.**
RETIRED_MARK = "보관 채널"

_lock = threading.Lock()
# path -> (stat 지문, 파싱 결과). 답변 경로에서 문서마다 불리므로 캐시한다.
_cache: dict[Path, tuple[tuple[int, int], dict]] = {}


def state_path() -> Path:
    from .heartbeat import state_dir

    return state_dir() / STATE_FILE


def include_retired() -> bool:
    """보관·삭제된 채널의 원문을 **근거로 쓸 것인가**.

    기본은 아니다. 켜는 것은 운영 판단이고, 켜면 출처에 표시가 붙는다.
    """
    raw = (os.environ.get("INCLUDE_RETIRED_CHANNELS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Retired:
    """기록 한 건. **채널 이름과 좌표만** 담는다 — 원문은 여기 오지 않는다."""

    workspace: str
    channel_id: str
    channel: str
    reason: str
    at: str

    @property
    def label(self) -> str:
        return REASON_LABELS.get(self.reason, self.reason)


@dataclass
class Registry:
    """한 번 읽은 기록. 조회는 ID 우선, 이름은 폴백이다."""

    by_id: dict[tuple[str, str], Retired] = field(default_factory=dict)
    by_name: dict[tuple[str, str], Retired] = field(default_factory=dict)

    def find(self, workspace: str, channel_id: str | None, channel: str | None) -> Retired | None:
        """이 문서의 채널이 보관·삭제됐는가.

        **ID 를 먼저 본다.** 이름은 바뀔 수 있고, 같은 이름이 나중에 다시 만들어질
        수 있다 — 이름만 보면 **새로 만든 채널의 자료까지** 제외된다.

        만들어 낸 ID(`legacy-…`)는 신원이 아니다. 그 경우에만 이름으로 찾는다.
        """
        got = str(channel_id or "")
        if got and not got.startswith("legacy-"):
            return self.by_id.get((workspace, got))
        name = str(channel or "")
        return self.by_name.get((workspace, name)) if name else None

    def all(self, workspace: str = "") -> list[Retired]:
        rows = list(self.by_id.values()) + [
            row for row in self.by_name.values() if not row.channel_id
        ]
        if workspace:
            rows = [row for row in rows if row.workspace == workspace]
        return sorted(rows, key=lambda r: (r.workspace, r.channel or r.channel_id))


def _empty() -> dict:
    return {"schema_version": SCHEMA_VERSION, "channels": []}


def _read() -> dict:
    """기록 파일. 없거나 못 읽으면 **빈 기록**이다(열리는 쪽으로 실패)."""
    path = state_path()
    try:
        stat = path.stat()
    except OSError:
        return _empty()
    key = (stat.st_mtime_ns, stat.st_size)
    with _lock:
        cached = _cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("보관 채널 기록을 읽지 못했습니다: %s", exc)
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        log.warning("보관 채널 기록 형식이 다릅니다 — 빈 기록으로 둡니다")
        return _empty()
    with _lock:
        _cache[path] = (key, data)
    return data


def registry() -> Registry:
    """지금 기록. 파일 지문이 같으면 다시 파싱하지 않는다."""
    out = Registry()
    for row in _read().get("channels", []):
        if not isinstance(row, dict):
            continue
        reason = str(row.get("reason") or "")
        if reason not in REASONS:
            continue
        item = Retired(
            workspace=str(row.get("workspace") or ""),
            channel_id=str(row.get("channel_id") or ""),
            channel=str(row.get("channel") or ""),
            reason=reason,
            at=str(row.get("at") or ""),
        )
        if item.channel_id:
            out.by_id[(item.workspace, item.channel_id)] = item
        if item.channel:
            out.by_name[(item.workspace, item.channel)] = item
    return out


def _write(data: dict) -> bool:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
    except OSError as exc:
        log.warning("보관 채널 기록을 쓰지 못했습니다: %s", exc)
        return False
    with _lock:
        _cache.pop(path, None)
    return True


def mark(workspace: str, channel_id: str, *, channel: str = "", reason: str = ARCHIVED) -> bool:
    """이 채널을 보관·삭제로 기록한다. 이미 있으면 사유와 시각을 갱신한다.

    **되돌릴 수 있게 둔다** — `restore()` 로 지운다. 기록이 원문을 지우지 않는다.
    """
    if reason not in REASONS:
        raise ValueError(f"알 수 없는 사유입니다: {reason}")
    if not workspace or not (channel_id or channel):
        return False
    data = _read()
    rows = [
        row for row in data.get("channels", [])
        if isinstance(row, dict) and not _same(row, workspace, channel_id, channel)
    ]
    rows.append({
        "workspace": workspace,
        "channel_id": channel_id,
        # 이름도 남긴다. 사람이 콘솔에서 읽을 때 ID 만으로는 어느 채널인지 모른다.
        "channel": channel,
        "reason": reason,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    })
    return _write({"schema_version": SCHEMA_VERSION, "channels": rows})


def restore(workspace: str, channel_id: str, *, channel: str = "") -> bool:
    """보관 해제. 기록에서 지운다 — 그 순간부터 다시 근거가 된다."""
    data = _read()
    rows = [
        row for row in data.get("channels", [])
        if isinstance(row, dict) and not _same(row, workspace, channel_id, channel)
    ]
    if len(rows) == len(data.get("channels", [])):
        return False
    return _write({"schema_version": SCHEMA_VERSION, "channels": rows})


def _same(row: dict, workspace: str, channel_id: str, channel: str) -> bool:
    if str(row.get("workspace") or "") != workspace:
        return False
    if channel_id and str(row.get("channel_id") or "") == channel_id:
        return True
    return bool(channel) and str(row.get("channel") or "") == channel


def is_retired(workspace: str, channel_id: str | None, channel: str | None) -> bool:
    return registry().find(workspace, channel_id, channel) is not None


def keep(doc, *, allow_retired: bool | None = None) -> bool:
    """이 문서를 근거로 써도 되는가.

    `allow_retired` 를 주면 설정을 읽지 않는다 — 호출부가 한 요청 안에서 값을
    한 번만 읽고 문서마다 넘기라고 둔 자리다.
    """
    if allow_retired if allow_retired is not None else include_retired():
        return True
    return not is_retired(
        str(getattr(doc, "workspace", "") or ""),
        getattr(doc, "channel_id", None),
        getattr(doc, "channel", None),
    )


def mark_for(workspace: str, channel_id: str | None, channel: str | None) -> str:
    """출처 뒤에 붙일 표시. 해당 없으면 빈 문자열.

    **포함으로 켠 경우에만** 의미가 있다 — 제외돼 있으면 애초에 출처에 안 나온다.
    """
    found = registry().find(workspace, channel_id, channel)
    return f" ({found.label} 채널)" if found else ""


def reconcile(client, workspace: str, *, known: list[tuple[str, str]]) -> dict:
    """Slack 의 현재 채널 목록과 대조해 기록을 갱신한다.

    `known` 은 우리 아카이브가 들고 있는 `(channel_id, channel)` 목록이다.
    **아카이브에 있는 채널만** 본다 — 워크스페이스 전체를 훑어 기록을 만들면
    수집 대상이 아닌 채널까지 기록에 쌓인다.

    비공개 채널이 목록에서 사라지는 이유는 둘이다 — 지워졌거나, **봇이 나갔거나.**
    둘을 구별할 수 없으므로 비공개는 `deleted` 로 기록하지 않는다. 봇이 나간 것을
    삭제로 적으면, 다시 초대했을 때 그 채널 자료가 근거에서 빠진 채로 남는다.
    """
    result = {"archived": 0, "deleted": 0, "restored": 0, "checked": 0, "failed": 0}
    try:
        live = _live_channels(client)
    except Exception as exc:  # noqa: BLE001 - 조회 실패로 기록을 지우면 안 된다
        log.warning("채널 목록 대조 실패 ws=%s: %s", workspace, exc)
        result["failed"] = 1
        return result

    current = registry()
    for channel_id, channel in known:
        result["checked"] += 1
        info = live.get(channel_id)
        was = current.find(workspace, channel_id, channel)
        if info is None:
            # 목록에 아예 없다. 공개 채널이면 지워진 것이다.
            if (was is None and not _looks_private(channel_id)
                    and mark(workspace, channel_id, channel=channel, reason=DELETED)):
                result["deleted"] += 1
            continue
        if info.get("is_archived"):
            if was is None and mark(workspace, channel_id, channel=channel, reason=ARCHIVED):
                result["archived"] += 1
            continue
        # 살아 있다. 보관 해제된 채널의 기록을 남겨 두면 자료가 계속 빠진다.
        if was is not None and restore(workspace, channel_id, channel=channel):
            result["restored"] += 1
    return result


def _looks_private(channel_id: str) -> bool:
    """비공개 채널 ID 인가. Slack 은 비공개에 `G`/`C` 를 섞어 쓴다.

    확신할 수 없으므로 **만들어 낸 ID 는 비공개로 본다** — 삭제로 적지 않는다.
    """
    got = str(channel_id or "")
    return not got or got.startswith(("G", "legacy-"))


def _live_channels(client) -> dict[str, dict]:
    """보관된 것까지 **포함해서** 가져온다. 빼면 보관과 삭제를 구별할 수 없다."""
    out: dict[str, dict] = {}
    cursor = None
    while True:
        res = client.conversations_list(
            types="public_channel,private_channel",
            exclude_archived=False,
            limit=200,
            cursor=cursor,
        )
        for item in res.get("channels") or []:
            if item.get("id"):
                out[str(item["id"])] = item
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return out


__all__ = [
    "ARCHIVED",
    "DELETED",
    "REASONS",
    "REASON_LABELS",
    "RETIRED_MARK",
    "Registry",
    "Retired",
    "include_retired",
    "is_retired",
    "keep",
    "mark",
    "mark_for",
    "reconcile",
    "registry",
    "restore",
    "state_path",
]
