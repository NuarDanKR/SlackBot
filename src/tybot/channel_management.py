"""표준 Slack 업무 채널 생성·이름 변경 지원.

채널 메시지 원문과 섞이지 않도록 생성 요청자 정보는 STATE_DIR 아래에 둔다. Slack의
Channel Manager를 대체하는 권한이 아니라, 봇의 이름 변경 API를 누가 사용할 수 있는지
제한하는 최소 권한 기록이다.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .channels import COLLECT_PREFIXES, PREFIX_ALIASES, ChannelSpec, parse
from .orgsearch import OrgHit, decode_value, option

_OWNER_LOCK = threading.Lock()
_SPACE_RE = re.compile(r"\s+")
_CODE_RE = re.compile(r"^[0-9A-Za-z]+$")
_CHANNEL_PREFIX = {"본사팀": "팀"}


class ChannelNameError(ValueError):
    """모달의 특정 입력 블록에 표시할 채널명 오류."""

    def __init__(self, message: str, block_id: str) -> None:
        super().__init__(message)
        self.block_id = block_id


@dataclass(frozen=True)
class ChannelRequest:
    prefix: str
    org_name: str
    org_code: str
    task: str
    visibility: str = "private"
    members: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return build_channel_name(self.prefix, self.org_name, self.org_code, self.task)


def _piece(value: str) -> str:
    return _SPACE_RE.sub("-", (value or "").strip())


def build_channel_name(prefix: str, org_name: str, org_code: str, task: str) -> str:
    """사람이 입력한 네 항목을 수집 규칙에 맞는 Slack 채널명으로 조립한다."""
    prefix = (prefix or "").strip()
    org_name = _piece(org_name)
    org_code = (org_code or "").strip().upper()
    task = _piece(task)

    if prefix not in COLLECT_PREFIXES:
        raise ChannelNameError("조직 구분을 선택해 주세요.", "prefix")
    if not org_name:
        raise ChannelNameError("조직명을 입력해 주세요.", "org_name")
    if "_" in org_name or "#" in org_name:
        raise ChannelNameError("조직명에는 밑줄(_)이나 #을 사용할 수 없습니다.", "org_name")
    if not _CODE_RE.fullmatch(org_code):
        raise ChannelNameError("조직코드는 영문과 숫자만 입력해 주세요.", "org_code")
    if not task:
        raise ChannelNameError("업무명을 입력해 주세요.", "task")
    if "#" in task:
        raise ChannelNameError("업무명에는 #을 사용할 수 없습니다.", "task")

    channel_prefix = _CHANNEL_PREFIX.get(prefix, prefix)
    name = f"{channel_prefix}-{org_name}_{org_code}-{task}"
    if len(name) > 80:
        raise ChannelNameError("채널명이 80자를 넘습니다. 조직명이나 업무명을 줄여 주세요.", "task")
    if parse(name) is None:
        raise ChannelNameError("표준 채널명으로 만들 수 없는 입력입니다.", "task")
    return name


def _selected(state: dict, block_id: str, action_id: str) -> dict:
    return state.get(block_id, {}).get(action_id, {})


def request_from_view(view: dict, *, include_channel_options: bool) -> ChannelRequest:
    """Slack view_submission을 채널 요청으로 바꾼다.

    조직코드는 사용자가 입력하지 않는다. 조직 검색 선택값에서 코드와 이름을 함께 읽는다.
    """
    state = (view.get("state") or {}).get("values") or {}
    prefix = (_selected(state, "prefix", "prefix").get("selected_option") or {}).get(
        "value", ""
    )
    selected_org = _selected(state, "org", "org").get("selected_option") or {}
    org_code, _derived_prefix, org_name = decode_value(selected_org.get("value", ""))
    if not org_code or not org_name:
        raise ChannelNameError("조직명을 검색해 목록에서 선택해 주세요.", "org")
    task = _selected(state, "task", "task").get("value", "")
    visibility = "private"
    members: tuple[str, ...] = ()
    if include_channel_options:
        visibility = (
            _selected(state, "visibility", "visibility").get("selected_option") or {}
        ).get("value", "private")
        members = tuple(
            _selected(state, "members", "members").get("selected_users") or ()
        )
    request = ChannelRequest(prefix, org_name, org_code, task, visibility, members)
    _ = request.name
    return request


def selected_prefix(view: dict, *, default: str = "본사팀") -> str:
    """지금 고른 구분. 구분이 바뀔 때 화면을 다시 그리는 데 쓴다."""
    state = (view.get("state") or {}).get("values") or {}
    picked = (_selected(state, "prefix", "prefix").get("selected_option") or {}).get("value")
    return picked or default


def action_prefix(body: dict, *, default: str = "본사팀") -> str:
    """block_actions의 방금 선택한 구분을 읽는다.

    Slack은 action payload와 view.state를 함께 보내지만 view.state에는 변경 전 값이
    남을 수 있다. 화면을 다시 그릴 때는 actions의 새 값을 우선해야 한다.
    """
    actions = body.get("actions") or []
    selected = (actions[0].get("selected_option") or {}) if actions else {}
    picked = str(selected.get("value") or "")
    if picked in COLLECT_PREFIXES:
        return picked
    return selected_prefix(body.get("view") or {}, default=default)


def typed_task(view: dict) -> str:
    """다시 그릴 때 이미 입력한 업무명을 잃지 않게 한다."""
    state = (view.get("state") or {}).get("values") or {}
    return _selected(state, "task", "task").get("value", "") or ""


def _name_inputs(
    spec: ChannelSpec | None = None,
    *,
    prefix: str = "본사팀",
    defaults: dict | None = None,
    dispatch_prefix: bool = True,
) -> list[dict]:
    """구분·조직 검색·업무명을 받는다. 조직코드는 검색 선택값 안에 숨긴다."""
    options = [
        {"text": {"type": "plain_text", "text": p}, "value": p}
        for p in ("본부", "실", "본사팀", "현장", "업무")
    ]
    # 예전 이름으로 만들어진 채널을 이름 변경할 때 목록에 없는 값이 오지 않게 옮긴다.
    picked = PREFIX_ALIASES.get(prefix, prefix)
    if picked not in {o["value"] for o in options}:
        picked = "본사팀"

    hit = (defaults or {}).get(picked)
    selected_org = OrgHit(spec.org_code, spec.org_name) if spec else hit

    return [
        {
            "type": "input",
            "block_id": "prefix",
            # 고르는 즉시 아래 두 칸을 다시 채운다. 이 값이 없으면 Slack 이
            # 선택 사실을 서버에 보내지 않아 자동 채움이 동작하지 않는다.
            "dispatch_action": dispatch_prefix,
            "label": {"type": "plain_text", "text": "조직 구분"},
            "element": {
                "type": "static_select",
                "action_id": "prefix",
                "options": options,
                "initial_option": next(o for o in options if o["value"] == picked),
            },
            "hint": {
                "type": "plain_text",
                "text": "본부 > 본사팀 > 현장, 또는 실 > 본사팀. "
                        "업무는 조직이 아니라 다른 팀과 협업할 때 쓰는 채널이며, "
                        "주관 팀의 조직코드를 그대로 씁니다.",
            },
        },
        {
            "type": "input",
            "block_id": "org",
            "label": {"type": "plain_text", "text": "조직명"},
            "element": {
                "type": "external_select",
                "action_id": "org",
                "min_query_length": 1,
                "placeholder": {"type": "plain_text", "text": "조직명 또는 코드로 검색"},
                **({"initial_option": option(selected_org)} if selected_org else {}),
            },
            "hint": {
                "type": "plain_text",
                "text": "조직코드는 선택한 조직에서 자동으로 적용됩니다.",
            },
        },
        {
            "type": "input",
            "block_id": "task",
            "label": {"type": "plain_text", "text": "업무명"},
            "element": {
                "type": "plain_text_input",
                "action_id": "task",
                "placeholder": {"type": "plain_text", "text": "예: 주간회의"},
                **({"initial_value": spec.task} if spec else {}),
            },
        },
    ]


def create_modal(
    private_metadata: str,
    *,
    prefix: str = "본사팀",
    defaults: dict | None = None,
    task: str = "",
) -> dict:
    """전역 바로가기와 `/채널 생성`이 공유하는 생성 모달.

    구분이 바뀔 때 이 함수로 다시 만들어 `views_update` 한다. 그래서 이미 입력한
    업무명(`task`)을 인자로 받아 되살린다 — 다시 그렸다고 입력이 사라지면 안 된다.
    """
    blocks = _name_inputs(prefix=prefix, defaults=defaults)
    if task:
        blocks[-1]["element"]["initial_value"] = task
    blocks.extend(
        [
            {
                "type": "input",
                "block_id": "visibility",
                "label": {"type": "plain_text", "text": "공개 범위"},
                "element": {
                    "type": "radio_buttons",
                    "action_id": "visibility",
                    "options": [
                        {
                            "text": {"type": "plain_text", "text": "비공개"},
                            "value": "private",
                        },
                        {
                            "text": {"type": "plain_text", "text": "공개"},
                            "value": "public",
                        },
                    ],
                    "initial_option": {
                        "text": {"type": "plain_text", "text": "비공개"},
                        "value": "private",
                    },
                },
            },
            {
                "type": "input",
                "block_id": "members",
                "optional": True,
                "label": {"type": "plain_text", "text": "추가 참여자"},
                "element": {
                    "type": "multi_users_select",
                    "action_id": "members",
                    "placeholder": {"type": "plain_text", "text": "나중에 초대해도 됩니다"},
                },
            },
        ]
    )
    return {
        "type": "modal",
        "callback_id": "tybot_create_channel",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "업무 채널 만들기"},
        "submit": {"type": "plain_text", "text": "만들기"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": blocks,
    }


def rename_modal(private_metadata: str, spec: ChannelSpec) -> dict:
    return {
        "type": "modal",
        "callback_id": "tybot_rename_channel",
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "채널 이름 변경"},
        "submit": {"type": "plain_text", "text": "변경"},
        "close": {"type": "plain_text", "text": "취소"},
        # 이름변경은 제출 시 선택값을 읽으면 된다. 여기서 prefix 액션을 보내면
        # 생성 모달 전용 views_update 핸들러가 화면을 생성 모달로 바꿔 버린다.
        "blocks": _name_inputs(spec, dispatch_prefix=False),
    }


class ChannelOwnerStore:
    """TYBot 생성 채널의 최초 요청자를 원자적으로 기록한다."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "channels": {}}
        if not isinstance(data.get("channels"), dict):
            return {"version": 1, "channels": {}}
        return data

    def record(self, workspace: str, channel_id: str, owner_user_id: str, name: str) -> None:
        with _OWNER_LOCK:
            data = self._read()
            key = f"{workspace}:{channel_id}"
            data["channels"][key] = {
                "owner_user_id": owner_user_id,
                "name": name,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def is_owner(self, workspace: str, channel_id: str, user_id: str) -> bool:
        with _OWNER_LOCK:
            row = self._read()["channels"].get(f"{workspace}:{channel_id}") or {}
        return bool(user_id) and row.get("owner_user_id") == user_id
