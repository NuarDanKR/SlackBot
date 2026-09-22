"""PF 콘솔 격리 — 코드 차원에서 TYBot 에 닿지 않는가.

## 무엇을 고정하나
오너 계획 §10 의 완료 게이트 중 코드로 확인할 수 있는 것들이다.

- PF 사용자가 TYBot API 와 DB 에 접근하면 모두 거부된다
- PF 콘솔에 시크릿·질문·답변·문서 본문이 없다

「PF 는 별도 프로세스입니다」 는 배포 문서의 문장이고, 문장은 리팩터링을 견디지
못한다. 누군가 편의로 `from tybot.console import reader` 한 줄을 넣으면 그 순간
PF 프로세스가 TYBot 의 `DATABASE_URL` 과 아카이브 경로를 들고 있게 되는데,
**화면에는 아무 변화도 없다.** 그래서 시험으로 박는다.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PF_ROOT = Path(__file__).resolve().parents[1] / "src" / "tybot_pf"


def _modules() -> list[Path]:
    return sorted(PF_ROOT.glob("*.py"))


def _directives(unit_text: str) -> str:
    """systemd unit 에서 **주석을 뺀 실제 지시문**만.

    왜 필요한가: 이 파일들은 「왜 이렇게 두었나」 를 주석으로 길게 적는다. 옛 설정을
    설명하려면 그 값을 적어야 하고(`--host 0.0.0.0` 이 그랬다), 문자열만 찾는 검사는
    그 설명을 위반으로 읽는다. **고정하려는 것은 설정이지 우리가 무엇을 쓸 수
    있느냐가 아니다.**
    """
    return "\n".join(
        line
        for line in unit_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _code(path: Path) -> str:
    """파이썬 소스에서 주석과 docstring 을 뺀 부분.

    같은 이유다. 이 패키지의 docstring 은 「TYBot 경로를 읽지 않는다」 를 설명하려고
    그 경로를 적는다. 설명을 금지하면 남는 것은 이유 없는 규칙뿐이다.

    `ast.unparse` 가 주석을 떨어뜨리므로 docstring 만 따로 걷어 내면 된다.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


def test_the_package_exists_and_is_not_empty():
    assert _modules(), "src/tybot_pf 에 모듈이 없습니다."


# --- 임포트 경계 --------------------------------------------------------------
def test_pf_never_imports_tybot():
    """한 줄이면 격리가 사라진다. 그런데 화면에는 아무 변화도 없다."""
    offenders: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # `from . import x` 는 level>0 이라 module 이 None 이다 — 우리 것이다.
                names = [node.module] if node.module and node.level == 0 else []
            else:
                continue
            for name in names:
                if name == "tybot" or name.startswith("tybot."):
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, (
        "PF 패키지가 TYBot 을 임포트합니다 — 그 순간 TYBot env·DB·아카이브가 이 "
        f"프로세스로 들어옵니다: {offenders}"
    )


PROBE = """
import json, sys
sys.path.insert(0, %r)
import tybot_pf.app, tybot_pf.auth, tybot_pf.health, tybot_pf.store
leaked = sorted(k for k in sys.modules if k == "tybot" or k.startswith("tybot."))
print(json.dumps(leaked))
"""


def test_importing_pf_does_not_pull_tybot_into_the_process():
    """정적 검사를 우회하는 지연 임포트까지 잡는다.

    **깨끗한 프로세스에서 확인한다.** 이 프로세스의 `sys.modules` 를 비우면 이미
    `tybot.*` 를 임포트해 둔 다른 시험들이 중복 모듈을 다시 만들게 되고, 그러면
    monkeypatch 와 모듈 수준 캐시가 서로 다른 객체를 보게 된다 — 엉뚱한 시험이
    실행 순서에 따라 실패한다.
    """
    src = str(PF_ROOT.parent)
    done = subprocess.run(
        [sys.executable, "-c", PROBE % src],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        # Windows 개발 PC 의 기본 콘솔 인코딩(cp949)으로 디코딩하면 깨진다.
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert done.returncode == 0, f"PF 임포트가 실패했습니다: {done.stderr}"
    leaked = json.loads(done.stdout.strip().splitlines()[-1])
    assert not leaked, f"PF 를 임포트했더니 TYBot 모듈이 따라 들어왔습니다: {leaked}"


# --- 환경변수 경계 ------------------------------------------------------------
def test_pf_reads_only_its_own_environment_variables():
    """TYBot 의 `DATABASE_URL` 을 읽으면 분리해 둔 DB role 이 의미를 잃는다.

    코드는 PF role 로 붙는 줄 알지만 실제로는 TYBot role 로 붙고, 그때는 질문·원문
    표까지 전부 열린다.
    """
    banned = {
        "DATABASE_URL",
        "CONSOLE_SECRET",
        "CONSOLE_DIST",
        "ARCHIVE_DIR",
        "QA_LOG_DIR",
        "STATE_DIR",
        "TYBOT_ENV_FILE",
        "ANTHROPIC_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "WORKSPACE_SECRET_KEY",
    }
    offenders: list[str] = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_getenv = (
                isinstance(func, ast.Attribute)
                and func.attr in {"getenv", "environ"}
            ) or (isinstance(func, ast.Name) and func.id == "getenv")
            if not is_getenv or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value in banned:
                offenders.append(f"{path.name}: {first.value}")
    assert not offenders, f"PF 가 TYBot 환경변수를 읽습니다: {offenders}"


def test_every_environment_variable_pf_reads_starts_with_pf():
    """규칙을 하나로 둔다 — 「PF_ 로 시작하는 것만」. 목록을 관리하면 빠진다."""
    found: set[str] = set()
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                found.add(str(node.args[0].value))
    assert found, "환경변수를 하나도 안 읽는다면 이 시험이 무의미해졌습니다."
    stray = sorted(name for name in found if not name.startswith("PF_"))
    assert not stray, f"PF_ 로 시작하지 않는 환경변수를 읽습니다: {stray}"


# --- 경로 경계 ----------------------------------------------------------------
def test_pf_code_never_points_at_a_tybot_runtime_path():
    """`/etc/tybot`·`/var/lib/tybot` 은 systemd 에서도 막지만 코드에도 안 적는다.

    주석·docstring 은 보지 않는다 — 거기서는 「이 경로를 읽지 않는다」 를 설명해야 한다.
    """
    banned = ("/etc/tybot/", "/var/lib/tybot/archive", "/var/lib/tybot/qa-log")
    offenders = []
    for path in _modules():
        code = _code(path)
        for needle in banned:
            if needle in code:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"PF 코드가 TYBot 경로를 가리킵니다: {offenders}"


# --- 세션 쿠키 경계 ------------------------------------------------------------
def test_the_two_consoles_use_different_cookie_names():
    """같은 이름이면 한쪽 로그인이 다른 쪽 쿠키를 덮어쓴다 — 사용자는 이유 없이
    로그아웃되고, 더 나쁘게는 한쪽 세션이 다른 쪽에서 해석될 여지가 생긴다."""
    from tybot.console.auth import SESSION_COOKIE as TYBOT_COOKIE
    from tybot_pf.config import SESSION_COOKIE as PF_COOKIE

    assert PF_COOKIE != TYBOT_COOKIE


def test_the_pf_cookie_is_scoped_to_its_own_path():
    """`Path=/pf` 라야 TYBot 콘솔 요청에 PF 세션이 실려 가지 않는다."""
    from tybot_pf.config import SESSION_COOKIE_PATH

    assert SESSION_COOKIE_PATH == "/pf"


# --- 배포 구성 ----------------------------------------------------------------
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def test_the_console_is_no_longer_open_to_the_whole_network():
    """B-35. 평문으로 사내망 전체에 열려 있던 상태를 끝낸다."""
    unit = _directives((DEPLOY / "tybot-console.service").read_text(encoding="utf-8"))
    assert "--host 127.0.0.1" in unit
    assert "--host 0.0.0.0" not in unit


def test_the_pf_service_runs_as_its_own_account_and_binds_to_loopback():
    unit = _directives((DEPLOY / "pf-hermes-console.service").read_text(encoding="utf-8"))
    assert "User=tybot-pf" in unit
    assert "--host 127.0.0.1" in unit
    assert "EnvironmentFile=/etc/tybot-pf/console.env" in unit
    # TYBot 설정 파일을 읽지 않는다.
    assert "/etc/tybot/tybot.env" not in unit


def test_the_pf_service_cannot_reach_tybot_paths():
    """파일 권한만 믿지 않는다. 계정 설정이 틀려도 여기서 막힌다."""
    unit = _directives((DEPLOY / "pf-hermes-console.service").read_text(encoding="utf-8"))
    assert "InaccessiblePaths=/etc/tybot" in unit
    assert "InaccessiblePaths=/var/lib/tybot" in unit


def test_nginx_terminates_tls_and_keeps_both_backends_on_loopback():
    conf = (DEPLOY / "nginx" / "tybot-console.conf").read_text(encoding="utf-8")
    assert "listen 443 ssl" in conf
    assert "127.0.0.1:8787" in conf   # TYBot 콘솔
    assert "127.0.0.1:8788" in conf   # PF 콘솔
    # 평문 80 은 처리하지 않고 넘긴다.
    assert "return 308 https://" in conf


def test_the_pf_schema_is_applied_by_the_deploy_script():
    """스키마 파일을 만들고 목록에 안 넣으면 한 번도 적용되지 않는다(2026-09-14)."""
    script = (DEPLOY / "apply-schema.sh").read_text(encoding="utf-8")
    assert "pf_console_schema.sql" in script


def _executed_statements(sql: str) -> list[str]:
    """`DO $$ ... EXECUTE '...' '...'; ... $$` 안에서 **실제로 실행되는 문장**.

    왜 줄 단위로 안 보나: 한 GRANT 가 여러 줄에 걸쳐 있다. 파이썬처럼 인접한 문자열
    리터럴이 이어 붙는 형태라, 줄 하나만 보면 `console_user` 는 뒷줄에 있고 컬럼
    목록은 앞줄에 있다. 2026-09-22 운영 배포에서 이 시험이 그렇게 틀렸다 —
    **SQL 이 아니라 시험이 틀렸는데 배포가 막혔다.**

    고정하려는 것은 「어떤 권한이 나가는가」 이지 그 문장을 몇 줄로 적었는가가 아니다.
    """
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    out: list[str] = []
    for match in re.finditer(r"EXECUTE\s+((?:'[^']*'\s*)+);", body):
        joined = "".join(re.findall(r"'([^']*)'", match.group(1)))
        out.append(" ".join(joined.split()))
    return out


def test_the_pf_role_gets_no_privilege_on_a_tybot_table():
    """PF role 에 TYBot 표 권한이 한 줄이라도 있으면 DB 쪽 방어가 사라진다.

    코드가 `tybot.*` 를 안 부르는 것(위 시험들)과 별개다. 코드는 고쳐 쓸 수 있지만
    DB 권한은 그 자리에서 거절한다 — 둘 중 하나가 틀렸을 때 나머지가 막는다.
    """
    sql = (DEPLOY / "sql" / "pf_console_schema.sql").read_text(encoding="utf-8")
    statements = [s for s in _executed_statements(sql) if "tybot_pf_console" in s]
    assert statements, "PF role 권한 구문을 찾지 못했습니다(형식이 바뀌었나 봅니다)."

    forbidden = ("usage_call", "archive_doc", "specialist_", "raw_line", "workspace")
    for statement in statements:
        for name in forbidden:
            assert name not in statement, (
                f"PF role 에 TYBot 표 권한이 있습니다: {statement}"
            )


def test_the_pf_role_reads_console_user_by_column_not_by_table():
    """표 전체를 주면 TYBot 쪽 역할·워크스페이스 배정까지 읽힌다.

    PF 가 알 필요가 없고, 알면 조직 구조가 드러난다(원칙 3).
    """
    sql = (DEPLOY / "sql" / "pf_console_schema.sql").read_text(encoding="utf-8")
    statements = [
        s
        for s in _executed_statements(sql)
        if "tybot_pf_console" in s and re.search(r"\bconsole_user\b", s)
    ]
    assert statements, "console_user 권한 구문을 찾지 못했습니다."
    for statement in statements:
        assert "(email, name, password_hash, active)" in statement, (
            f"console_user 를 표 단위로 주고 있습니다: {statement}"
        )


def test_the_pf_role_can_only_append_to_the_audit_table():
    """감사는 지울 수 없어야 한다. UPDATE·DELETE 권한이 있으면 그 전제가 무너진다."""
    sql = (DEPLOY / "sql" / "pf_console_schema.sql").read_text(encoding="utf-8")
    statements = [
        s
        for s in _executed_statements(sql)
        if "tybot_pf_console" in s and "pf_audit_event" in s and "SEQUENCE" not in s
    ]
    assert statements, "pf_audit_event 권한 구문을 찾지 못했습니다."
    for statement in statements:
        assert "UPDATE" not in statement, f"감사 표에 UPDATE 권한이 있습니다: {statement}"
        assert "DELETE" not in statement, f"감사 표에 DELETE 권한이 있습니다: {statement}"


# --- 배포가 콘솔을 잠그지 않는다 ------------------------------------------------
#
# 2026-09-22 사고. 콘솔 unit 을 루프백으로 바꾼 커밋이 배포되면서 **문을 닫는 쪽만**
# 적용됐다. nginx 배치는 수동 단계라 따라오지 않았고, 배포 버튼 한 번에 콘솔에 아무도
# 접속할 수 없게 됐다. 화면에는 「API 서버가 응답하지 않습니다」 만 남았다.
#
# 「실패한 배포가 현재 서비스를 중지해서는 안 된다」(오너 계획 §9).

def _guard_body() -> str:
    """안전장치 함수의 본문. 함수 이름이 아니라 **하는 일**을 고정한다."""
    script = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    start = script.index("console_bind_guard_for() {")
    return script[start : script.index("\n}\n", start)]


def test_the_installer_keeps_both_consoles_reachable_until_nginx_is_ready():
    """nginx 가 없으면 예전 바인딩을 유지한다. 유지하되 **조용히** 하지는 않는다."""
    script = (DEPLOY / "install.sh").read_text(encoding="utf-8")

    assert "console_bind_guard" in script, "콘솔 바인딩 안전장치가 없습니다."
    # 판단 근거 넷 — 하나라도 빠지면 nginx 가 요청을 못 받는데 문을 닫게 된다.
    for needle in (
        "command -v nginx",
        "systemctl is-active --quiet nginx",
        "/etc/nginx/conf.d/tybot-console.conf",
        "nginx -t",
    ):
        assert needle in script, f"nginx 준비 확인이 빠졌습니다: {needle}"


def test_the_guard_covers_the_pf_console_too():
    """PF 콘솔만 루프백에 남으면 `/pf/` 가 브라우저에서 안 닿는다.

    TYBot 콘솔만 챙기면 「콘솔은 되는데 /pf/ 만 안 된다」 가 되고, 원인이 바인딩이라는
    것은 화면 어디에도 안 나온다.
    """
    script = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    start = script.index("console_bind_guard() {")
    body = script[start : script.index("\n}\n", start)]

    assert "tybot-console" in body and "8787" in body
    assert "pf-hermes-console" in body and "8788" in body


def test_the_guard_skips_units_that_are_not_installed():
    """PF 콘솔은 나중에 붙는다. 없는 unit 에 drop-in 을 만들면 그 파일만 남는다."""
    assert '[[ -f "/etc/systemd/system/$unit.service" ]] || return 0' in _guard_body()


def test_the_fallback_binding_is_announced_not_silent():
    """조용히 평문으로 되돌리면 임시 상태가 영구 상태가 된다 — B-35 가 그랬다."""
    body = _guard_body()

    assert "평문" in body
    assert "B-35" in body
    # 방화벽으로 좁히라는 말을 같이 한다. 평문 + 전면 개방은 다른 이야기다.
    assert "방화벽" in body
    # 경고는 표준오류로. 성공 로그에 섞이면 배포 출력에서 묻힌다.
    assert ">&2" in body


def test_the_guard_removes_itself_once_nginx_takes_over():
    """목표 상태는 루프백이다. 안전장치가 남아 있으면 그게 새로운 영구 상태가 된다."""
    body = _guard_body()

    assert 'rm -f "$dropin"' in body
    assert "systemctl daemon-reload" in body
    # 바인딩이 바뀌었으면 재시작해야 실제로 반영된다.
    assert 'systemctl restart "$unit"' in body


def test_the_console_error_message_gives_no_broken_command():
    """틀린 명령은 안내가 아니라 한 단계 더 헤매게 만드는 것이다.

    서버에는 `uvicorn` 이 PATH 에 없다 — venv 절대경로라야 한다
    (CLAUDE.md 「서버 명령은 가상환경 기준으로 적는다」).
    """
    client = (
        Path(__file__).resolve().parents[1] / "console-web" / "src" / "api" / "client.ts"
    ).read_text(encoding="utf-8")
    start = client.index("const SERVER_DOWN")
    message = client[start : client.index(".join(", start)]

    assert "uvicorn tybot.console.app" not in message, (
        "화면이 서버에서 실패하는 명령을 안내합니다."
    )


def test_a_missing_optional_component_does_not_stop_the_deploy():
    """부가 구성 요소가 배포를 끊으면, 고치려고 누른 배포가 서비스를 세운다.

    2026-09-22. 문서 변환기(kordoc) 실행 파일을 못 찾자 `install.sh` 가 1/6 단계에서
    `exit 1` 했다. 코드 배치도, 서비스 재시작도, 바인딩 안전장치도 돌지 않았다 —
    콘솔이 죽은 채로 남았고 복구하려면 SSH 가 필요했다.

    「실패한 배포가 현재 서비스를 중지해서는 안 된다」(오너 계획 §9).
    """
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")

    # 변환기 **확인 블록**만 본다. 변수 선언부터 자르면 root 검사(`exit 1`)까지
    # 딸려 들어와 엉뚱한 것을 잡는다. 주석도 뺀다 — 거기서는 옛 `exit 1` 을 설명한다.
    start = install.index('if [[ "$INSTALL_DOCUMENT_CONVERTERS" == "1" ]]; then')
    block = _directives(install[start : install.index("== 2/6", start)])

    assert "exit 1" not in block, (
        "문서 변환기 확인이 배포를 즉시 끊습니다 — 서비스 재시작이 안 돕니다."
    )
    assert "DEFERRED_FAILURES" in block, "실패를 미뤄 둘 자리가 없습니다."


def test_a_deferred_failure_still_ends_as_a_failure():
    """조용히 넘어가면 콘솔 배포 화면이 성공으로 표시하고, 아무도 안 고친다.

    HWP/HWPX·스캔 PDF 가 변환되지 않는 것은 오류 없이 「첨부가 비어 있음」 으로만
    보인다 — 그래서 종료 코드로라도 말해야 한다.
    """
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    update = (DEPLOY / "update.sh").read_text(encoding="utf-8")

    assert "exit 4" in install, "미뤄 둔 실패가 성공으로 끝납니다."
    # update.sh 는 4 를 받고도 **계속 가고**, 끝에서 4 로 끝낸다.
    assert "INSTALL_RC" in update
    assert 'bash "$SRC/deploy/install.sh" || INSTALL_RC=$?' in update, (
        "update.sh 가 install.sh 의 실패에서 곧바로 멈춥니다."
    )
    assert "exit 4" in update


def test_the_restart_happens_before_the_deploy_gives_up():
    """순서가 전부다. 재시작이 실패 보고보다 뒤에 있으면 고친 것이 아니다."""
    update = (DEPLOY / "update.sh").read_text(encoding="utf-8")

    restart = update.index("systemctl restart tybot")
    give_up = update.rindex("exit 4")
    assert restart < give_up, "서비스 재시작보다 먼저 포기합니다."
