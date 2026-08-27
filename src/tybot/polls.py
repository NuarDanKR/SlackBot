"""Slack 투표 — `/투표` 명령의 도메인 로직.

Slack 에는 기본 투표 기능이 없어서 직접 만든다. 이 모듈은 **Slack SDK 를 쓰지 않는다.**
투표를 만들고·집계하고·화면 블록을 조립하는 일만 하고, 실제 전송은 `slack/pilot.py` 가 한다.
그래야 투표 규칙을 Slack 없이 테스트할 수 있다.

## 저장 위치 — 아카이브가 아니다
`<STATE_DIR>/polls/<워크스페이스>/<투표id>.json`.

투표 결과는 **아카이브에 넣지 않는다**(원칙 1: 원문만 저장한다). 투표 메시지 자체도 봇이
올리는 것이라 수집 대상에서 제외된다. 즉 투표는 봇의 운영 상태이고, 답변 근거가 아니다.

## 익명 투표에서 누가 찍었는지 남지 않게 하는 방법
"익명"이라고 하면서 파일에 사용자 ID 를 그대로 적어 두면 익명이 아니다. 그런데 중복 투표를
막으려면 같은 사람인지 알아야 한다. 그래서 익명 투표는 **투표마다 다른 소금값(salt)을 섞은
해시**를 키로 쓴다.

- 같은 사람은 같은 해시가 되므로 중복 투표를 막을 수 있다
- 해시에서 사용자 ID 를 되돌릴 수 없고, 소금값이 투표마다 달라서 다른 투표와 대조할 수도 없다

공개 투표는 누가 무엇을 골랐는지 보여 주는 것이 목적이므로 ID 를 그대로 저장한다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("tybot.polls")

_LOCK = threading.Lock()

# 선택지 개수 상한. Slack 메시지 블록 수 제한(50)과 읽기 편한 길이를 함께 고려한다.
MAX_OPTIONS = 10
MIN_OPTIONS = 2
MAX_QUESTION = 300
MAX_OPTION_TEXT = 150

# 결과를 언제 보여 줄지
SHOW_ALWAYS = "always"  # 항상 — 진행 상황을 실시간으로 본다
SHOW_AFTER_VOTE = "after_vote"  # 내가 투표한 뒤부터
SHOW_AFTER_CLOSE = "after_close"  # 마감 후에만 — 앞선 표가 뒤 표를 끌고 가는 것을 막는다
SHOW_CHOICES = (SHOW_ALWAYS, SHOW_AFTER_VOTE, SHOW_AFTER_CLOSE)

_SPACE_RE = re.compile(r"[ \t]+")


class PollError(ValueError):
    """투표 생성·참여 오류. 모달의 특정 입력 블록에 표시할 수 있다."""

    def __init__(self, message: str, block_id: str = "") -> None:
        super().__init__(message)
        self.block_id = block_id


# ---------------------------------------------------------------------------
# 자료 구조
# ---------------------------------------------------------------------------

@dataclass
class Poll:
    id: str
    workspace: str
    channel_id: str
    creator: str
    question: str
    options: list[str]
    # 여러 항목을 고를 수 있는가 (중복 투표 허용)
    multi: bool = False
    # 누가 무엇을 골랐는지 감추는가
    anonymous: bool = False
    # 이미 투표한 사람이 선택을 바꿀 수 있는가
    allow_change: bool = True
    show_results: str = SHOW_ALWAYS
    closes_at: str | None = None
    closed: bool = False
    closed_by: str | None = None
    created_at: str = ""
    # 익명 투표에서 사용자 ID 를 가리는 데 쓰는 값. 투표마다 다르다.
    salt: str = ""
    # 투표자키 → 고른 선택지 번호들
    votes: dict[str, list[int]] = field(default_factory=dict)
    # Slack 메시지 위치(같은 메시지를 갱신하기 위해 기억한다)
    message_ts: str | None = None

    # --- 상태 판정 --------------------------------------------------------
    def voter_key(self, user_id: str) -> str:
        """투표자를 식별하는 키. 익명 투표에서는 되돌릴 수 없는 해시를 쓴다."""
        if not self.anonymous:
            return user_id
        return hashlib.sha256(f"{self.salt}:{user_id}".encode()).hexdigest()[:32]

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if not self.closes_at:
            return False
        try:
            deadline = datetime.fromisoformat(self.closes_at)
        except ValueError:
            return False
        return (now or datetime.now(UTC)) >= deadline

    def is_open(self, *, now: datetime | None = None) -> bool:
        return not self.closed and not self.is_expired(now=now)

    def has_voted(self, user_id: str) -> bool:
        return self.voter_key(user_id) in self.votes

    def selection(self, user_id: str) -> list[int]:
        return list(self.votes.get(self.voter_key(user_id), []))

    def may_see_results(self, user_id: str, *, now: datetime | None = None) -> bool:
        if self.show_results == SHOW_ALWAYS:
            return True
        if self.show_results == SHOW_AFTER_VOTE:
            return self.has_voted(user_id) or not self.is_open(now=now)
        return not self.is_open(now=now)

    # --- 집계 -------------------------------------------------------------
    def counts(self) -> list[int]:
        out = [0] * len(self.options)
        for picks in self.votes.values():
            for i in picks:
                if 0 <= i < len(out):
                    out[i] += 1
        return out

    @property
    def voter_count(self) -> int:
        return len(self.votes)

    def voters_by_option(self) -> list[list[str]]:
        """공개 투표에서 선택지별 참여자. 익명 투표는 빈 목록을 돌려준다."""
        out: list[list[str]] = [[] for _ in self.options]
        if self.anonymous:
            return out
        for key, picks in self.votes.items():
            for i in picks:
                if 0 <= i < len(out):
                    out[i].append(key)
        return out

    # --- 직렬화 -----------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace": self.workspace,
            "channel_id": self.channel_id,
            "creator": self.creator,
            "question": self.question,
            "options": self.options,
            "multi": self.multi,
            "anonymous": self.anonymous,
            "allow_change": self.allow_change,
            "show_results": self.show_results,
            "closes_at": self.closes_at,
            "closed": self.closed,
            "closed_by": self.closed_by,
            "created_at": self.created_at,
            "salt": self.salt,
            "votes": self.votes,
            "message_ts": self.message_ts,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> Poll:
        return cls(
            id=str(raw["id"]),
            workspace=str(raw.get("workspace", "")),
            channel_id=str(raw.get("channel_id", "")),
            creator=str(raw.get("creator", "")),
            question=str(raw.get("question", "")),
            options=[str(o) for o in raw.get("options", [])],
            multi=bool(raw.get("multi", False)),
            anonymous=bool(raw.get("anonymous", False)),
            allow_change=bool(raw.get("allow_change", True)),
            show_results=str(raw.get("show_results", SHOW_ALWAYS)),
            closes_at=raw.get("closes_at"),
            closed=bool(raw.get("closed", False)),
            closed_by=raw.get("closed_by"),
            created_at=str(raw.get("created_at", "")),
            salt=str(raw.get("salt", "")),
            votes={str(k): [int(i) for i in v] for k, v in (raw.get("votes") or {}).items()},
            message_ts=raw.get("message_ts"),
        )


# ---------------------------------------------------------------------------
# 입력 검증
# ---------------------------------------------------------------------------

def parse_options(text: str) -> list[str]:
    """줄바꿈으로 구분한 선택지 입력을 정리한다.

    빈 줄과 앞뒤 공백은 버리고, 같은 선택지가 두 번 들어오면 오류로 막는다
    — 같은 항목이 두 개면 표가 갈려서 결과가 뜻을 잃는다.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for raw in (text or "").splitlines():
        # 사람들이 "1. 항목" / "- 항목" 처럼 번호를 붙여 적는다. 그대로 두면 화면에서 두 번 매겨진다.
        line = re.sub(r"^\s*(?:[-*·]|\d+[.)])\s*", "", raw).strip()
        line = _SPACE_RE.sub(" ", line)
        if not line:
            continue
        if len(line) > MAX_OPTION_TEXT:
            raise PollError(
                f"선택지가 너무 깁니다({len(line)}자). {MAX_OPTION_TEXT}자 이내로 줄여 주세요.",
                "options",
            )
        key = line.casefold()
        if key in seen:
            raise PollError(f"같은 선택지가 두 번 있습니다: {line}", "options")
        seen[key] = 1
        out.append(line)

    if len(out) < MIN_OPTIONS:
        raise PollError(f"선택지를 {MIN_OPTIONS}개 이상 한 줄에 하나씩 적어 주세요.", "options")
    if len(out) > MAX_OPTIONS:
        raise PollError(
            f"선택지는 최대 {MAX_OPTIONS}개까지입니다. 지금 {len(out)}개입니다.", "options"
        )
    return out


def parse_deadline(value: str | None, *, now: datetime | None = None) -> str | None:
    """마감 설정값(`1h`, `3h`, `1d`, `없음`)을 실제 시각으로 바꾼다."""
    if not value or value in ("none", "없음", ""):
        return None
    hours = {"30m": 0.5, "1h": 1, "3h": 3, "6h": 6, "1d": 24, "3d": 72, "7d": 168}.get(value)
    if hours is None:
        raise PollError(f"마감 시간을 알 수 없습니다: {value}", "deadline")
    return ((now or datetime.now(UTC)) + timedelta(hours=hours)).isoformat(timespec="seconds")


def create_poll(
    *,
    workspace: str,
    channel_id: str,
    creator: str,
    question: str,
    options_text: str,
    multi: bool = False,
    anonymous: bool = False,
    allow_change: bool = True,
    show_results: str = SHOW_ALWAYS,
    deadline: str | None = None,
    now: datetime | None = None,
) -> Poll:
    question = _SPACE_RE.sub(" ", (question or "").strip())
    if not question:
        raise PollError("무엇을 물을지 적어 주세요.", "question")
    if len(question) > MAX_QUESTION:
        raise PollError(
            f"질문이 너무 깁니다({len(question)}자). {MAX_QUESTION}자 이내로 줄여 주세요.",
            "question",
        )
    if show_results not in SHOW_CHOICES:
        raise PollError(f"결과 공개 설정을 알 수 없습니다: {show_results}", "show_results")

    options = parse_options(options_text)
    closes_at = parse_deadline(deadline, now=now)

    # 마감 후 공개인데 마감이 없으면 결과를 영원히 볼 수 없다. 만들 때 막는다.
    if show_results == SHOW_AFTER_CLOSE and closes_at is None:
        logger.info("마감 없는 '마감 후 공개' 투표 — 만든 사람이 직접 마감해야 결과가 보인다")

    return Poll(
        id=secrets.token_urlsafe(8),
        workspace=workspace,
        channel_id=channel_id,
        creator=creator,
        question=question,
        options=options,
        multi=multi,
        anonymous=anonymous,
        allow_change=allow_change,
        show_results=show_results,
        closes_at=closes_at,
        created_at=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
        salt=secrets.token_hex(16),
    )


# ---------------------------------------------------------------------------
# 투표 참여
# ---------------------------------------------------------------------------

def apply_vote(poll: Poll, user_id: str, option_index: int, *, now: datetime | None = None) -> str:
    """선택지 하나를 눌렀을 때의 처리. 사람에게 보여 줄 안내 문구를 돌려준다.

    - 단일 선택: 같은 항목을 다시 누르면 취소, 다른 항목을 누르면 갈아탄다
    - 중복 허용: 누를 때마다 켜지고 꺼진다
    """
    if not poll.is_open(now=now):
        raise PollError("이미 마감된 투표입니다.")
    if not (0 <= option_index < len(poll.options)):
        raise PollError("없는 선택지입니다.")

    key = poll.voter_key(user_id)
    current = list(poll.votes.get(key, []))
    already_voted = key in poll.votes

    if already_voted and not poll.allow_change:
        raise PollError("이 투표는 한 번 고르면 바꿀 수 없습니다.")

    label = poll.options[option_index]
    if poll.multi:
        if option_index in current:
            current.remove(option_index)
            message = f"‘{label}’ 선택을 취소했습니다."
        else:
            current.append(option_index)
            message = f"‘{label}’ 을(를) 선택했습니다."
    else:
        if current == [option_index]:
            current = []
            message = f"‘{label}’ 선택을 취소했습니다."
        else:
            current = [option_index]
            message = f"‘{label}’ 에 투표했습니다."

    if current:
        poll.votes[key] = sorted(current)
    else:
        # 아무것도 고르지 않았으면 참여 기록 자체를 지운다 — 참여자 수가 부풀지 않게.
        poll.votes.pop(key, None)
    return message


def close_poll(poll: Poll, user_id: str, *, is_admin: bool = False) -> None:
    """투표를 마감한다. 만든 사람이나 관리자만 할 수 있다."""
    if poll.closed:
        raise PollError("이미 마감된 투표입니다.")
    if user_id != poll.creator and not is_admin:
        raise PollError("투표를 만든 사람만 마감할 수 있습니다.")
    poll.closed = True
    poll.closed_by = user_id


# ---------------------------------------------------------------------------
# 저장
# ---------------------------------------------------------------------------

def state_dir() -> Path:
    explicit = os.getenv("STATE_DIR")
    if explicit:
        return Path(explicit)
    return Path(os.getenv("ARCHIVE_DIR", "./archive")).parent


def poll_path(workspace: str, poll_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", poll_id)
    return state_dir() / "polls" / (workspace or "default") / f"{safe}.json"


def save(poll: Poll) -> None:
    """원자적으로 덮어쓴다. 반쯤 쓰인 파일을 읽는 일이 없게."""
    path = poll_path(poll.workspace, poll.id)
    with _LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(poll.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)
        except OSError as e:
            # 투표는 부가 기능이다. 저장 실패로 봇을 죽이지 않지만 조용히 넘기지도 않는다.
            logger.error("투표 저장 실패 (%s): %s", path, e)
            raise PollError("투표를 저장하지 못했습니다. 관리자에게 알려 주세요.") from e


def load(workspace: str, poll_id: str) -> Poll | None:
    path = poll_path(workspace, poll_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        logger.error("투표를 읽지 못했습니다 (%s): %s", path, e)
        return None
    return Poll.from_dict(raw)


def list_polls(workspace: str, *, limit: int = 20) -> list[Poll]:
    """최근 투표 목록. 운영 확인용."""
    base = state_dir() / "polls" / (workspace or "default")
    if not base.is_dir():
        return []
    out: list[Poll] = []
    for path in sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            out.append(Poll.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return out
