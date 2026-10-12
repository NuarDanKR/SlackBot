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

import json
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
    ("check-rpc-unit.js", "배치 — 소켓 그룹·enable 가능·설치 helper 멱등"),
    ("check-rpc-shared.js", "공유 — 명부·workspace 선택·교차 인용·취소·429·health"),
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
        #
        # 넘기는 것은 **비밀이 아닌 것뿐**이다. 임시 디렉터리 변수(Windows 의
        # TEMP/TMP, POSIX 의 TMPDIR)를 빼면 `os.tmpdir()` 가 `undefined` 를 내놓고,
        # 검사는 자기 fixture 도 못 만든 채 죽는다 — 그건 「계약이 깨졌다」 가 아니라
        # 「검사를 못 돌렸다」 인데 둘이 같은 모양으로 보인다.
        env=_clean_env(),
    )


def _clean_env() -> dict[str, str]:
    """검사에 줄 환경. **API 키·토큰은 한 개도 안 넘긴다.**"""
    import os

    keep = (
        "PATH", "TEMP", "TMP", "TMPDIR", "HOME",
        # Windows 가 경로를 푸는 데 쓰는 것들. 빼면 Node 와 그 자식이 `%SystemDrive%`
        # 같은 **안 풀린 문자열을 디렉터리 이름으로** 쓰고, 그 디렉터리가 저장소
        # 작업 트리에 생긴다(2026-10-12 실제로 그랬다 — `subbots/hermes/%SystemDrive%/`).
        # 비밀이 아니다.
        "SystemRoot", "SystemDrive", "windir",
        "ProgramData", "ALLUSERSPROFILE", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
    )
    env = {name: os.environ[name] for name in keep if name in os.environ}
    env["PYTHON"] = sys.executable
    return env


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
    assert "RuntimeDirectoryMode=0750" in live
    assert not [
        line
        for line in live
        if line.startswith("ExecStartPre=")
        and any(command in line for command in ("chgrp", "chmod"))
        and "/run/tybot-subbots/hermes-%i" in line
    ]
    # `[Install]` 이 없으면 `systemctl enable` 이 「no installation config」 로 실패한다.
    assert "[Install]" in live
    assert "WantedBy=multi-user.target" in live

    runner = (HERMES / "scripts" / "run-rpc-worker.js").read_text(encoding="utf-8")
    server = (HERMES / "src" / "rpc" / "server.js").read_text(encoding="utf-8")
    assert "alignSocketDirectoryGroup(cfg.socketPath, TOOL_SOCKET_PATH)" in runner
    assert "export function alignSocketDirectoryGroup" in server


def test_rpc_unit_uses_a_systemd_239_compatible_secret_file():
    unit = (HERMES / "deploy" / "hermes-rpc@.service").read_text(encoding="utf-8")
    live = [
        line.strip()
        for line in unit.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert not any(line.startswith("LoadCredential=") for line in live)
    assert "Environment=HERMES_RPC_HMAC_FILE=/etc/hermes-rpc-%i/hmac" in live
    assert "ReadOnlyPaths=/opt/tybot/subbots/hermes /etc/hermes-rpc-%i" in live


def test_shared_worker_keeps_the_individual_one() -> None:
    """공유 worker 가 개별 worker 를 **대체하지 않는다**(2026-10-12 인계).

    OS 수준 격리가 필요한 workspace 는 개별 worker 를 계속 쓴다. 공유 쪽을 만들면서
    개별 unit·entrypoint 를 건드리면 그 선택지가 사라지는데, 사라진 것은 쓰려고
    할 때야 안다.
    """
    deploy = HERMES / "deploy"
    assert (deploy / "hermes-rpc@.service").is_file(), "개별 unit 이 사라졌다"
    assert (deploy / "hermes-rpc-shared.service").is_file(), "공유 unit 이 없다"
    assert (SCRIPTS / "run-rpc-worker.js").is_file(), "개별 entrypoint 가 사라졌다"
    assert (SCRIPTS / "run-rpc-shared.js").is_file(), "공유 entrypoint 가 없다"
    # helper 도 따로다 — 한 스크립트에 섞으면 「공유를 깔려다 개별 설정을 덮었다」 가 생긴다.
    assert (deploy / "install-rpc.sh").is_file()
    assert (deploy / "install-rpc-shared.sh").is_file()

    scripts = json.loads((HERMES / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert scripts["rpc"] == "node scripts/run-rpc-worker.js"
    assert scripts["rpc:shared"] == "node scripts/run-rpc-shared.js"


def test_shared_worker_takes_its_allowlist_from_the_server() -> None:
    """허용 목록이 **서버 설정**에 있다(인계 §6).

    요청 본문이 domain·모델·키 파일을 고르면, 요청 하나로 남의 키를 쓰게 할 수 있고
    그 요청은 실제로 답을 받아 가므로 아무 경보도 안 울린다.
    """
    profiles = (HERMES / "src" / "rpc" / "profiles.js").read_text(encoding="utf-8")
    # 전역 fallback 키가 없다 — 있으면 미등록 workspace 가 그냥 돈다.
    assert "ANTHROPIC_API_KEY" not in profiles
    assert "wildcard" in profiles
    shared_cfg = (HERMES / "src" / "rpc" / "config.js").read_text(encoding="utf-8")
    # 공유 설정은 workspace·domain·모델 키를 env 에서 안 읽는다.
    shared_block = shared_cfg.split("loadSharedRpcConfig", 1)[1]
    for forbidden in ("HERMES_RPC_WORKSPACE", "HERMES_DOMAIN", "ANTHROPIC_API_KEY"):
        assert forbidden not in shared_block, f"공유 설정이 {forbidden} 를 읽는다"


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
