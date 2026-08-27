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

from .channels import COLLECT_PREFIXES, ChannelSpec, parse

_OWNER_LOCK = threading.Lock()
_SPACE_RE = re.compile(r"\s+")
_CODE_RE = re.compile(r"^[0-9A-Za-z]+$")


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

    name = f"{prefix}-{org_name}_{org_code}-{task}"
    if len(name) > 80:
        raise ChannelNameError("채널명이 80자를 넘습니다. 조직명이나 업무명을 줄여 주세요.", "task")
    if parse(name) is None:
        raise ChannelNameError("표준 채널명으로 만들 수 없는 입력입니다.", "task")
    return name


def _selected(state: dict, block_id: str, action_id: str) -> dict:
    return state.get(block_id, {}).get(action_id, {})


def request_from_view(view: dict, *, include_channel_options: bool) -> ChannelRequest:
    """Slack view_submission의 state.values를 ChannelRequest로 바꾼다."""
    state = (view.get("state") or {}).get("values") or {}
    prefix = (_selected(state, "prefix", "prefix").get("selected_option") or {}).get(
        "value", ""
    )
    org_name = _selected(state, "org_name", "org_name").get("value", "")
    org_code = _selected(state, "org_code", "org_code").get("value", "")
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


def _name_inputs(spec: ChannelSpec | None = None) -> list[dict]:
    prefix_options = [
        {"text": {"type": "plain_text", "text": p}, "value": p}
        for p in ("본부", "실", "팀", "현장", "프로젝트")
    ]
    selected_prefix = spec.prefix if spec else "팀"
    return [
        {
            "type": "input",
            "block_id": "prefix",
            "label": {"type": "plain_text", "text": "조직 구분"},
            "element": {
                "type": "static_select",
                "action_id": "prefix",
                "options": prefix_options,
                "initial_option": next(o for o in prefix_options if o["value"] == selected_prefix),
            },
        },
        {
            "type": "input",
            "block_id": "org_name",
            "label": {"type": "plain_text", "text": "조직명"},
            "element": {
                "type": "plain_text_input",
                "action_id": "org_name",
                "placeholder": {"type": "plain_text", "text": "예: 전산"},
                **({"initial_value": spec.org_name} if spec else {}),
            },
        },
        {
            "type": "input",
            "block_id": "org_code",
            "label": {"type": "plain_text", "text": "조직코드"},
            "element": {
                "type": "plain_text_input",
                "action_id": "org_code",
                "placeholder": {"type": "plain_text", "text": "예: ABB110"},
                **({"initial_value": spec.org_code} if spec else {}),
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


def create_modal(private_metadata: str) -> dict:
    """전역 바로가기와 `/채널 생성`이 공유하는 생성 모달."""
    blocks = _name_inputs()
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
        "blocks": _name_inputs(spec),
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
