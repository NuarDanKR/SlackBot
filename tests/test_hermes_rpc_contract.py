"""Hermes 전문봇 RPC worker — v3 계약·경계 회귀.

인계: `docs/verification/2026-10-08-hermes-external-router-claude-handoff.md`
계약: `docs/design/specialist-v3-tool-contract.md`

## 여기서 Python 이 하는 일

실체는 Node 검사 셋이고(`subbots/hermes/scripts/check-rpc-*.js`), 이 파일은 그것을
**pytest 한 바퀴에 들어오게** 묶는다. 묶지 않으면 `npm run check` 를 따로 돌리는
사람만 보게 되고, 그 사람은 보통 Hermes 를 고치는 사람뿐이다 — 정작 깨뜨리는 쪽은
`specialist_wire_v3.py` 를 고치는 Python 작업이다.

## 계약 상수를 여기서도 본다

Node 쪽 거울이 Python 원본과 같은지는 `check-rpc-wire.js` 가 Python 소스를 읽어
대조한다. 그 검사가 **빠지거나 이름이 바뀌어도** 아무것도 안 빨개지는 것을 막으려고,
아래에서 그 파일이 실제로 대조하고 있는지를 한 번 더 본다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "subbots" / "hermes"
SCRIPTS = HERMES / "scripts"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None or not HERMES.is_dir(), reason="node 또는 subbots/hermes 가 없다"
)
needs_deps = pytest.mark.skipif(
    not (HERMES / "node_modules" / "@anthropic-ai").is_dir(),
    reason="subbots/hermes/node_modules 가 설치되지 않았다",
)

CHECKS = (
    ("check-rpc-wire.js", "v3 계약 거울이 Python 원본·서명과 맞나"),
    ("check-rpc-server.js", "worker 왕복 — 근거·서명·재생·상한·health·취소"),
    ("check-rpc-isolation.js", "경계 — Slack·아카이브 미접근과 요청 간 토큰 격리"),
    ("check-rpc-unit.js", "배치 — 소켓 그룹(SGID)·enable 가능·설치 helper 멱등"),
)


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [NODE, str(SCRIPTS / script)],
        cwd=HERMES,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
        # 이 검사들은 모델도 Slack 도 안 부른다. 키가 환경에 있어도 **쓰지 않는다**는
        # 것을 여기서 못박는다 — 빈 값으로 넘겨 두면 실수로 붙는 날 바로 터진다.
        env={"PATH": __import__("os").environ.get("PATH", ""), "PYTHON": sys.executable},
    )


@needs_node
@needs_deps
@pytest.mark.parametrize("script,what", CHECKS, ids=[c[0] for c in CHECKS])
def test_rpc_check_passes(script: str, what: str) -> None:
    done = _run(script)
    assert done.returncode == 0, f"{what}\n--- stdout ---\n{done.stdout}\n--- stderr ---\n{done.stderr}"


@needs_node
@needs_deps
def test_wire_check_actually_compares_python_source() -> None:
    """거울 검사가 **Python 소스를 읽고** 있나.

    손으로 적은 기대값만 비교하면 원본이 바뀌어도 통과한다 — 그 상태의 증상은
    운영에서 400 한 줄뿐이고, 무엇이 달라졌는지는 어디에도 안 적힌다.
    """
    source = (SCRIPTS / "check-rpc-wire.js").read_text(encoding="utf-8")
    assert "specialist_wire_v3.py" in source
    assert "specialist_wire.py" in source
    done = _run("check-rpc-wire.js")
    # 파이썬이 있으면 서명 교차 검증이 **실제로 돌아야** 한다. 건너뛴 회차가
    # 통과와 같은 모양이면 「맞았다」 와 「안 쟀다」 를 가릴 수 없다.
    assert "못 쟀다" not in done.stdout, done.stdout


@needs_node
@needs_deps
def test_worker_graph_has_no_slack_or_archive() -> None:
    """경계 검사가 **실제 파일 목록**을 찍나.

    목록이 비면 「Slack 이 없다」 가 공짜로 참이 된다. 찍힌 목록에 worker 진입점과
    도구 클라이언트가 들어 있는지를 본다.
    """
    done = _run("check-rpc-isolation.js")
    assert done.returncode == 0, done.stdout + done.stderr
    for must in ("scripts/run-rpc-worker.js", "src/rpc/tool-client.js", "src/rpc/server.js"):
        assert must in done.stdout, f"{must} 가 그래프에 없다\n{done.stdout}"
    assert "src/archive.js" not in done.stdout
    assert "src/slack-live.js" not in done.stdout


def test_socket_group_contract_is_written_down() -> None:
    """소켓 그룹이 틀리면 **오류가 안 난다.**

    worker 는 멀쩡히 뜨고, TYBot 은 연결에 실패하고, 사람은 「전문가가 조용히 답을
    안 함」 만 본다. 양쪽 어디에도 볼 로그가 없다. 그래서 unit 의 이 네 줄은
    주석이 아니라 계약이다.
    """
    unit = (HERMES / "deploy" / "hermes-rpc@.service").read_text(encoding="utf-8")
    live = [
        line.strip()
        for line in unit.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # SGID 가 없으면 소켓은 프로세스 기본 그룹(`hermes-rpc-<ws>`)을 받는다.
    assert "RuntimeDirectoryMode=2750" in live
    assert "ExecStartPre=+/usr/bin/chgrp tybot-specialist-rpc /run/tybot-subbots/hermes-%i" in live
    assert "ExecStartPre=+/usr/bin/chmod 2750 /run/tybot-subbots/hermes-%i" in live
    # 소유자를 root 로 돌리면 2750 에서 group 이 `r-x` 라 worker 가 소켓을 못 만든다.
    assert not [line for line in live if line.startswith("ExecStartPre=") and "chown" in line]
    # `[Install]` 이 없으면 `systemctl enable` 이 「no installation config」 로 실패한다.
    assert "[Install]" in live
    assert "WantedBy=multi-user.target" in live


def test_installer_prepares_nothing_it_cannot_verify() -> None:
    """설치 helper 가 계정·비밀을 **만들지 않는다.**

    특히 HMAC 비밀: 양쪽이 같은 값을 가져야 하는데 한쪽만 새로 만들면 전부 401 이
    되고, 증상은 「전문가가 조용히 답을 안 함」 이다. 소켓 그룹 오류와 구별이 안 된다.
    """
    sh = (HERMES / "deploy" / "install-rpc.sh").read_text(encoding="utf-8")
    # **실행과 출력을 가른다.** helper 는 사람이 칠 명령을 `warn "  useradd …"` 로
    # 보여 준다 — 그 줄은 따옴표 안이다. 실행하는 줄은 명령으로 시작한다.
    runs = [
        line.strip()
        for line in sh.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    executed = re.compile(r"^(sudo\s+)?(useradd|groupadd|usermod|openssl)\b")
    offenders = [line for line in runs if executed.match(line)]
    assert not offenders, f"설치 helper 가 준비물을 직접 만든다: {offenders}"
    assert "openssl rand" not in "\n".join(
        line for line in runs if not line.startswith(("warn ", "say ", "ok "))
    ), "설치 helper 가 HMAC 비밀을 만든다"
    # 실행하지 않을 뿐 **알려는 준다.** 안 알려 주면 설치하고 아무 일도 안 일어난다.
    assert "useradd" in sh and "systemctl enable" in sh


@needs_node
@needs_deps
def test_checks_are_registered_in_catalog() -> None:
    """`npm run check` 가 이 셋을 실제로 돌리나.

    등록이 빠지면 그 검사는 **어디서도 안 돌면서 아무것도 안 빨개진다.**
    """
    catalog = (SCRIPTS / "check-catalog.js").read_text(encoding="utf-8")
    for script, _ in CHECKS:
        assert f'"scripts/{script}"' in catalog, f"{script} 가 카탈로그에 없다"
