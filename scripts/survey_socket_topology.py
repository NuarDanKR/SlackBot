#!/usr/bin/env python3
"""Archiver 연결이 **Socket 을 몇 개 열어야 하는지** 센다.

파일 이름에 `_token` 을 넣지 않는다 — `.gitignore` 의 시크릿 위생 규칙(`*_token*`)에
걸려 저장소에 안 들어간다. 규칙을 푸는 것보다 이름을 피하는 편이 안전하다.

    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/survey_socket_topology.py

## 왜 먼저 세는가

Slack 은 같은 앱의 Socket Mode 연결이 여러 개면 payload 를 어느 연결로 보낼지
보장하지 않는다(supervisor 지시서 §2.1). workspace 마다 WebSocket 을 하나씩 열면
같은 앱을 두 곳에 설치한 순간부터 이벤트가 둘 중 아무 데나 가고, 한쪽은 조용하다 —
오류가 아니라 **누락**이다.

supervisor 를 만들기 전에 지금 토큰이 어떻게 묶여 있는지부터 확인한다. 몇 개 묶음인지
모르면 worker 를 몇 개 열어야 하는지도 모른다.

## 무엇을 보여 주나

- 열어야 하는 Socket 연결 수와 각 묶음의 workspace 목록
- App Token 이 없어 Socket 을 못 여는 연결
- **같은 Bot Token 을 쓰는 워크스페이스**(설치 실수 — 남의 대화를 수집한다)

## 무엇을 안 보여 주나

토큰 평문, 토큰 해시, 묶음 표시. 묶음 표시는 실행할 때마다 새 소금으로 만들어
**저장해도 다음 실행과 대조할 수 없다**(`token_topology.fingerprint`).

읽기 전용이다. DB 에 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tybot.archive import token_topology  # noqa: E402


def load(bot_key: str = "archiver") -> list[token_topology.Connection]:
    """연결과 토큰을 **메모리로만** 읽는다.

    복호화는 여기서 한 번 하고 `Connection` 안에서 끝난다. 반환되는 요약에는
    값이 없다.
    """
    from cryptography.fernet import InvalidToken

    from tybot.console.workspace_store import _connect, _fernet

    cipher = _fernet()
    out: list[token_topology.Connection] = []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.workspace, c.bot_key, c.state, c.team_id,
                   max(s.ciphertext) FILTER (WHERE s.kind = 'bot') AS bot_cipher,
                   max(s.ciphertext) FILTER (WHERE s.kind = 'app') AS app_cipher
              FROM bot_connection c
              LEFT JOIN bot_connection_secret s ON s.connection_id = c.id
             WHERE c.connector_type = 'slack_socket'
               AND (%s = '' OR c.bot_key = %s)
               AND c.state <> 'retired'
             GROUP BY c.workspace, c.bot_key, c.state, c.team_id
             ORDER BY c.workspace
            """,
            (bot_key, bot_key),
        )
        rows = cur.fetchall()

    for row in rows:
        def _plain(value) -> str:
            if value is None:
                return ""
            try:
                return cipher.decrypt(bytes(value)).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError):
                # 못 읽는 토큰은 **없는 것으로 센다.** 그 사실만 요약에 나온다.
                return ""

        out.append(token_topology.Connection(
            workspace=str(row["workspace"]),
            bot_key=str(row["bot_key"]),
            bot_token=_plain(row["bot_cipher"]),
            app_token=_plain(row["app_cipher"]),
            state=str(row["state"]),
            team_id=str(row["team_id"] or ""),
        ))
    return out


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--bot", default="archiver", help="기본: archiver · 전부는 ''")
    args = parser.parse_args()

    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    from tybot.envfile import load_env_file

    load_env_file()
    try:
        connections = load(args.bot.strip())
    except Exception as exc:  # noqa: BLE001 - 연결 실패 종류가 여럿이다
        print(f"연결 정보를 읽지 못했습니다: {type(exc).__name__}")
        return 2

    if not connections:
        print("등록된 Slack 연결이 없습니다.")
        return 0

    topology = token_topology.survey(connections)
    for line in token_topology.summarize(topology):
        print(line)
    print()
    print("이 출력에는 토큰도 토큰 해시도 없습니다. 묶음 표시는 실행마다 바뀝니다.")
    return 1 if topology.shared_bot_tokens or topology.without_app_token else 0


if __name__ == "__main__":
    raise SystemExit(main())
