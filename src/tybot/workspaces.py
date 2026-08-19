"""워크스페이스 설정 — 여러 Slack 워크스페이스를 한 프로세스에서 운영한다.

설치 방식(문서 `docs/multi-workspace.md` 참조):
- 워크스페이스마다 **앱을 따로 만든다**(봇 토큰 + 앱 토큰 각 1개).
  단일 앱 배포(distribution) 방식은 OAuth 리다이렉트용 **인바운드 엔드포인트**가 필요한데,
  우리 보안 경계는 인바운드 포트 0개다. 워크스페이스 수가 적을 때는 앱 분리가 더 단순하고 격리도 강하다.
- 각 워크스페이스는 Socket Mode 연결을 하나씩 갖는다(모두 아웃바운드).

환경변수:
    WORKSPACES=pilot,mgmt
    SLACK_BOT_TOKEN_PILOT=xoxb-...
    SLACK_APP_TOKEN_PILOT=xapp-...
    SLACK_BOT_TOKEN_MGMT=xoxb-...
    SLACK_APP_TOKEN_MGMT=xapp-...
    WORKSPACE_LABEL_MGMT=경영본부          # (선택) 사람이 읽는 이름
    CROSS_WS_READ=mgmt:pilot,pilot:        # 아래 참조

WORKSPACES 가 없으면 기존 단일 워크스페이스 설정(PILOT_WORKSPACE + SLACK_BOT_TOKEN)을 그대로 쓴다.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("tybot.workspaces")


class ConfigError(RuntimeError):
    """워크스페이스 설정 오류 — 기동을 막는다(조용히 반쪽만 뜨면 안 된다)."""


def env_suffix(key: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", key.upper()).strip("_")


@dataclass(frozen=True)
class WorkspaceConfig:
    key: str  # 아카이브 디렉터리·ACL 에 쓰는 식별자
    label: str  # 사람이 읽는 이름
    bot_token: str
    app_token: str
    # 이 워크스페이스에서 물었을 때 **추가로** 읽을 수 있는 다른 워크스페이스들.
    # 기본은 비어 있다 = 크로스 워크스페이스 차단(원칙 4).
    readable: frozenset[str] = field(default_factory=frozenset)

    def masked(self) -> str:
        return f"{self.key}({self.label}) bot={self.bot_token[:9]}… readable={sorted(self.readable) or '없음'}"


def parse_cross_read(spec: str | None, known: set[str]) -> dict[str, frozenset[str]]:
    """`CROSS_WS_READ` 파싱.

    형식: `읽는쪽:대상1|대상2, 읽는쪽2:대상3`
    - 모르는 키가 나오면 **설정 오류로 막는다.** 오타 하나가 조용히 권한을 없애거나
      반대로 열어두는 상황을 피한다.
    - `*` 는 전체 허용. 편의를 위해 지원하지만 사용을 권하지 않는다.
    """
    out: dict[str, frozenset[str]] = {}
    if not spec or not spec.strip():
        return out
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ConfigError(f"CROSS_WS_READ 형식 오류: {chunk!r} (읽는쪽:대상1|대상2)")
        reader, _, targets = chunk.partition(":")
        reader = reader.strip()
        if reader not in known:
            raise ConfigError(f"CROSS_WS_READ 의 '{reader}' 는 WORKSPACES 에 없습니다")
        allowed: set[str] = set()
        for t in targets.split("|"):
            t = t.strip()
            if not t:
                continue
            if t == "*":
                allowed |= known - {reader}
                continue
            if t not in known:
                raise ConfigError(f"CROSS_WS_READ 의 대상 '{t}' 는 WORKSPACES 에 없습니다")
            allowed.add(t)
        out[reader] = frozenset(allowed - {reader})
    return out


def load_workspaces(env: dict[str, str] | None = None) -> list[WorkspaceConfig]:
    """환경변수에서 워크스페이스 목록을 구성한다."""
    e = dict(os.environ if env is None else env)

    keys = [k.strip() for k in (e.get("WORKSPACES") or "").split(",") if k.strip()]
    if not keys:
        # 단일 워크스페이스(기존 설정) 호환 경로
        key = e.get("PILOT_WORKSPACE", "pilot")
        bot, app = e.get("SLACK_BOT_TOKEN"), e.get("SLACK_APP_TOKEN")
        if not bot or not app:
            raise ConfigError(
                "SLACK_BOT_TOKEN / SLACK_APP_TOKEN 이 없습니다. "
                "여러 워크스페이스를 쓰려면 WORKSPACES 를 설정하세요."
            )
        return [
            WorkspaceConfig(
                key=key, label=e.get("WORKSPACE_LABEL", key), bot_token=bot, app_token=app
            )
        ]

    if len(set(keys)) != len(keys):
        raise ConfigError(f"WORKSPACES 에 중복 키가 있습니다: {keys}")

    cross = parse_cross_read(e.get("CROSS_WS_READ"), set(keys))

    configs: list[WorkspaceConfig] = []
    for key in keys:
        sfx = env_suffix(key)
        bot = e.get(f"SLACK_BOT_TOKEN_{sfx}")
        app = e.get(f"SLACK_APP_TOKEN_{sfx}")
        missing = [n for n, v in ((f"SLACK_BOT_TOKEN_{sfx}", bot), (f"SLACK_APP_TOKEN_{sfx}", app)) if not v]
        if missing:
            raise ConfigError(f"워크스페이스 '{key}' 의 토큰 누락: {', '.join(missing)}")
        configs.append(
            WorkspaceConfig(
                key=key,
                label=e.get(f"WORKSPACE_LABEL_{sfx}", key),
                bot_token=bot,
                app_token=app,
                readable=cross.get(key, frozenset()),
            )
        )
    return configs
