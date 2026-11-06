#!/usr/bin/env python3
"""VIPBot 을 **켜기 전에** 돌리는 읽기 전용 점검 (B-83).

    sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/check_vipbot_readiness.py
    … --skip-db        # DB 에 못 붙는 자리에서 파일·유닛만 본다
    … --json           # 콘솔·자동화에 물린다

## 왜 따로 필요한가

조각마다 시험이 있고 전부 초록이다. 그런데 **운영 서버의 실제 상태**는 시험이
모른다 — 역할이 안 만들어졌는지, 자격증명 파일이 0644 인지, 유닛이 필요한 설정을
안 주는지. 그 어긋남은 `systemctl start` 를 치는 순간에야 보이고, 그때는 이미
Slack 에 붙으려다 실패한 뒤다.

이 점검은 그 순간을 **앞으로 당긴다.** 돌려서 조용하면 켤 준비가 된 것이고,
무엇이든 나오면 그게 켜기 전에 할 일이다.

## 안 하는 것

- **고치지 않는다.** `systemctl start/enable` · `GRANT`/`ALTER` · 파일 권한 변경을
  하지 않는다. 읽기만 한다
- **Slack 에 붙지 않는다.** `auth_test` 도 안 부른다 — 그건 기동의 일이고,
  여기서 부르면 「점검이 봇을 깨웠다」 가 된다
- **비밀 값을 출력하지 않는다.** DSN·토큰·API 키는 **모양만** 보고 버린다.
  경로·권한비트·역할명·개수만 적는다. 이 출력은 티켓에 붙는다

종료 코드: `0` 이상 없음 · `1` 준비 안 됨 · `2` 입력·환경 오류
"""
from __future__ import annotations

import argparse
import os
import pathlib
import stat
import sys
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_INPUT = 2

GROUP_ROLE = "tybot_vipbot"
RUNTIME_ROLE = "tybot_vipbot_runtime"
DEFINER_ROLE = "tybot_vipbot_definer"

CONFIG_FN = "public.vipbot_connection_config(text)"
ACCESS_FN = "public.vipbot_authorization(text,text)"

#: 런타임이 **직접 읽으면 안 되는** 표. 창구 함수로만 닿아야 한다.
BASE_TABLES = (
    "bot_connection",
    "bot_connection_secret",
    "employee",
    "user_identity",
    "bot_access",
    "bot_source_channel",
)

#: 유닛이 절대 들이면 안 되는 공용 설정. 하나라도 있으면 전용 계정이 무의미하다.
SHARED_ENV = ("DATABASE_URL", "ARCHIVE_DIR", "QA_LOG_DIR", "TYBOT_ENV_FILE")

#: 이 스크립트는 `load_env_file()` 을 **일부러 부르지 않는다.**
#:
#: 그 함수는 `/etc/tybot/tybot.env` 를 읽고, 그 파일에는 Master 의
#: `DATABASE_URL` 이 있다. 점검이 그걸 들고 있으면 (1) VIPBot 전용 계정을 만든
#: 의미가 사라지고 (2) 위 `SHARED_ENV` 검사가 자기 프로세스의 오염을 못 본다.
#: `scripts/verify_schema_isolated.py` 의 `operational_db_refusal` 과 같은 축이다 —
#: 「DB 를 쓴다」 와 「DB 이름을 언급한다」 는 다르다.
REFUSES_SHARED_ENV = True

#: 런타임이 **반드시** 받아야 하는 설정. 하나라도 없으면 기동에서 멎는다.
REQUIRED_SETTINGS = (
    "VIPBOT_WORKSPACE",
    "VIPBOT_ARCHIVE_DIR",
    "VIPBOT_DAILY_LIMIT_USD",
)

#: 있어도 되는 것. 비우면 승인된 기본 모델을 쓴다.
OPTIONAL_SETTINGS = ("VIPBOT_MODEL",)

#: 설정 파일에 있으면 안 되는 모드 비트. 기대는 `root:tybot 0640` 이다.
#:
#: 그룹 쓰기(`0o020`)는 서비스 계정이 자기 예산을 고치는 길이고, other 의 어떤
#: 비트(`0o007`)든 근거 범위가 서버의 아무에게나 보이는 길이다. `0o022`(쓰기만)
#: 로 두면 **0644 가 통과한다** — 읽기는 막지 않는다는 뜻이 되고, 그건 기대한
#: 0640 이 아니다.
MODE_FORBIDDEN_BITS = 0o027

#: 전용 설정 파일. **유닛이 값을 박지 않는 대신** 여기 하나만 읽는다.
SETTINGS_FILE = "/etc/tybot/tybot-vipbot.conf"

#: `EnvironmentFile=` 로 들일 수 있는 **유일한** 경로.
ALLOWED_ENVIRONMENT_FILES = (SETTINGS_FILE,)

#: 이 점검이 낼 수 있는 **전부.** 고정 낱말이라 그대로 세고 티켓에 붙인다.
FINDING_CODES = frozenset(
    {
        "role_missing",
        "role_can_login",
        "role_unsafe_attributes",
        "role_membership_unexpected",
        "role_admin_option",
        "function_missing",
        "function_owner_wrong",
        "function_not_executable",
        "function_public_executable",
        "base_table_readable",
        "connection_missing",
        "connection_ambiguous",
        "connection_incomplete",
        "credential_missing",
        "credential_mode_open",
        "credential_invalid",
        "archive_unconfigured",
        "archive_unreadable",
        "workspace_unconfigured",
        "budget_unconfigured",
        "budget_invalid",
        "model_not_approved",
        "unit_missing",
        "unit_shared_env",
        "unit_credential_missing",
        "unit_settings_missing",
        "unit_settings_hardcoded",
        "settings_file_missing",
        "settings_file_unreadable",
        "settings_file_mode_open",
        "settings_file_owner",
        "settings_file_invalid",
        "settings_key_missing",
        "settings_key_forbidden",
        "database_unreachable",
        "state_directory_missing",
    }
)


@dataclass(frozen=True)
class Finding:
    """준비가 안 된 자리 하나. `detail` 에 **비밀 값은 넣지 않는다.**"""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.code not in FINDING_CODES:
            raise ValueError("unknown readiness finding")

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail else self.code


@dataclass(frozen=True)
class Facts:
    """서버에서 **읽은 사실**만. 판정은 :func:`evaluate` 가 한다."""

    roles: dict = field(default_factory=dict)
    functions: dict = field(default_factory=dict)
    base_tables: dict = field(default_factory=dict)
    connection: dict = field(default_factory=dict)
    credentials: tuple = ()
    archive: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    settings_file: dict = field(default_factory=dict)
    unit: dict = field(default_factory=dict)
    database_checked: bool = True
    database_error: str = ""


# ---------------------------------------------------------------------------
# 판정 — 순수 함수. DB 도 파일도 안 건드리므로 시험이 전부 돌릴 수 있다.
# ---------------------------------------------------------------------------


def evaluate(facts: Facts) -> list[Finding]:
    """사실 묶음 하나를 보고 **켤 수 있는지**를 말한다."""
    found: list[Finding] = []
    found.extend(_roles(facts))
    found.extend(_functions(facts))
    found.extend(_connection(facts))
    found.extend(_credentials(facts))
    found.extend(_archive(facts))
    found.extend(_settings(facts))
    found.extend(_settings_file(facts))
    found.extend(_unit(facts))
    return found


def _roles(facts: Facts) -> list[Finding]:
    if not facts.database_checked:
        return []
    found: list[Finding] = []
    for name in (GROUP_ROLE, RUNTIME_ROLE, DEFINER_ROLE):
        role = facts.roles.get(name)
        if not role:
            found.append(Finding("role_missing", name))
            continue
        # 그룹과 definer 는 **접속하지 않는다.** 접속하는 것은 runtime 하나다.
        if name != RUNTIME_ROLE and role.get("can_login"):
            found.append(Finding("role_can_login", name))
        if name == RUNTIME_ROLE and not role.get("can_login"):
            found.append(Finding("role_missing", f"{name} (LOGIN 아님)"))
        if role.get("unsafe"):
            found.append(
                Finding("role_unsafe_attributes", f"{name}: {role['unsafe']}")
            )
    # 계약은 **한 변**이 아니라 그래프 전체다.
    #   runtime 은 group 하나의 멤버 · group 의 멤버는 runtime 하나
    #   definer 는 아무의 멤버도 아니고 아무도 definer 의 멤버가 아니다
    #   ADMIN OPTION 은 어디에도 없다 — 있으면 그 역할이 멤버십을 남에게 준다
    expected_parents = {GROUP_ROLE: (), RUNTIME_ROLE: (GROUP_ROLE,), DEFINER_ROLE: ()}
    expected_members = {GROUP_ROLE: (RUNTIME_ROLE,), RUNTIME_ROLE: (), DEFINER_ROLE: ()}
    for name in (GROUP_ROLE, RUNTIME_ROLE, DEFINER_ROLE):
        role = facts.roles.get(name)
        if not role:
            continue
        for direction, expected in (
            ("memberships", expected_parents[name]),
            ("members", expected_members[name]),
        ):
            edges = tuple(role.get(direction) or ())
            names = tuple(_edge_name(edge) for edge in edges)
            if names != expected:
                label = "상위" if direction == "memberships" else "멤버"
                found.append(
                    Finding(
                        "role_membership_unexpected",
                        f"{name} {label}: {','.join(names) or '없음'}",
                    )
                )
            for edge in edges:
                if _edge_admin(edge):
                    found.append(
                        Finding("role_admin_option", f"{name}-{_edge_name(edge)}")
                    )
    return found


def _edge_name(edge) -> str:
    return str(edge[0] if isinstance(edge, (tuple, list)) else edge)


def _edge_admin(edge) -> bool:
    return bool(edge[1]) if isinstance(edge, (tuple, list)) and len(edge) > 1 else False


def _functions(facts: Facts) -> list[Finding]:
    if not facts.database_checked:
        return []
    found: list[Finding] = []
    for signature in (CONFIG_FN, ACCESS_FN):
        window = facts.functions.get(signature)
        if not window:
            found.append(Finding("function_missing", signature))
            continue
        if window.get("owner") != DEFINER_ROLE:
            found.append(
                Finding(
                    "function_owner_wrong",
                    f"{signature}: {window.get('owner') or '알 수 없음'}",
                )
            )
        if not window.get("runtime_execute"):
            found.append(Finding("function_not_executable", signature))
        if window.get("public_execute"):
            found.append(Finding("function_public_executable", signature))
    for table in BASE_TABLES:
        if facts.base_tables.get(table):
            # 창구를 우회해 표를 직접 읽을 수 있으면, 창구의 조건은 장식이다.
            found.append(Finding("base_table_readable", table))
    return found


def _connection(facts: Facts) -> list[Finding]:
    if not facts.database_checked:
        return []
    rows = facts.connection.get("rows")
    if rows is None:
        return []
    if rows == 0:
        return [Finding("connection_missing", str(facts.connection.get("workspace") or "-"))]
    if rows > 1:
        return [Finding("connection_ambiguous", f"{rows}건")]
    if not facts.connection.get("complete"):
        return [Finding("connection_incomplete", "team_id·bot_user_id·토큰쌍")]
    return []


def _credentials(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    for credential in facts.credentials:
        name = str(credential.get("name") or "?")
        if not credential.get("present"):
            found.append(Finding("credential_missing", name))
            continue
        if credential.get("mode_open"):
            found.append(
                Finding("credential_mode_open", f"{name} ({credential['mode_open']})")
            )
        if credential.get("invalid"):
            # **값은 적지 않는다.** 모양이 틀렸다는 사실만 남긴다.
            found.append(Finding("credential_invalid", name))
    return found


def _archive(facts: Facts) -> list[Finding]:
    if not facts.archive:
        return []
    if not facts.archive.get("configured"):
        return [Finding("archive_unconfigured", "VIPBOT_ARCHIVE_DIR")]
    if not facts.archive.get("readable"):
        return [Finding("archive_unreadable", str(facts.archive.get("path") or "-"))]
    return []


def _settings(facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    if not facts.settings:
        return found
    if not facts.settings.get("workspace"):
        found.append(Finding("workspace_unconfigured", "VIPBOT_WORKSPACE"))
    budget = facts.settings.get("budget")
    if budget == "missing":
        found.append(Finding("budget_unconfigured", "VIPBOT_DAILY_LIMIT_USD"))
    elif budget == "invalid":
        found.append(Finding("budget_invalid", "VIPBOT_DAILY_LIMIT_USD"))
    if facts.settings.get("model_not_approved"):
        found.append(Finding("model_not_approved", "VIPBOT_MODEL"))
    if facts.settings.get("state_directory_missing"):
        found.append(Finding("state_directory_missing", "StateDirectory=tybot-vipbot"))
    return found


def _unit(facts: Facts) -> list[Finding]:
    if not facts.unit:
        return []
    if not facts.unit.get("present"):
        return [Finding("unit_missing", str(facts.unit.get("path") or "-"))]
    found: list[Finding] = []
    for name in facts.unit.get("shared_env") or ():
        found.append(Finding("unit_shared_env", name))
    for name in facts.unit.get("missing_credentials") or ():
        found.append(Finding("unit_credential_missing", name))
    if not facts.unit.get("settings_file"):
        found.append(Finding("unit_settings_missing", SETTINGS_FILE))
    for name in facts.unit.get("hardcoded_settings") or ():
        found.append(Finding("unit_settings_hardcoded", name))
    return found


def _settings_file(facts: Facts) -> list[Finding]:
    settings_file = facts.settings_file
    if not settings_file:
        return []
    if not settings_file.get("present"):
        return [Finding("settings_file_missing", str(settings_file.get("path") or "-"))]
    if settings_file.get("unreadable"):
        return [
            Finding("settings_file_unreadable", str(settings_file.get("path") or "-"))
        ]
    found: list[Finding] = []
    if settings_file.get("mode_open"):
        found.append(Finding("settings_file_mode_open", settings_file["mode_open"]))
    if settings_file.get("owner") is not None:
        found.append(
            Finding("settings_file_owner", f"uid={settings_file['owner']} (root 아님)")
        )
    if settings_file.get("invalid_lines"):
        found.append(
            Finding("settings_file_invalid", f"{settings_file['invalid_lines']}줄")
        )
    for name in settings_file.get("missing") or ():
        found.append(Finding("settings_key_missing", name))
    for name in settings_file.get("forbidden") or ():
        # **이름만** 적는다. 값은 읽지 않았다.
        found.append(Finding("settings_key_forbidden", name))
    return found


# ---------------------------------------------------------------------------
# 사실 수집 — 여기만 바깥 세계를 만진다. 전부 **읽기**다.
# ---------------------------------------------------------------------------


def unit_facts(path: pathlib.Path) -> dict:
    """유닛 파일을 읽는다. 주석은 **무엇을 안 넣었는지** 설명하므로 걷어낸다."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"present": False, "path": str(path)}
    directives = [
        line.strip()
        for line in (row.split("#", 1)[0] for row in raw.splitlines())
        if line.strip()
    ]
    shared = [
        name
        for name in SHARED_ENV
        if any(name in line for line in directives if "=" in line)
    ]
    # `EnvironmentFile=` 자체는 금지가 아니다. **어느 파일이냐**가 전부다 —
    # `/etc/tybot/tybot.env` 에는 Master 의 DSN 이 있고, VIPBot 전용 파일에는
    # 네 키만 있다. 전자를 들이면 전용 계정이 무의미해진다.
    environment_files = tuple(
        line.split("=", 1)[1].strip().lstrip("-")
        for line in directives
        if line.startswith("EnvironmentFile=")
    )
    shared.extend(
        f"EnvironmentFile={name}"
        for name in environment_files
        if name not in ALLOWED_ENVIRONMENT_FILES
    )
    loaded = {
        line.split("=", 1)[1].split(":", 1)[0]
        for line in directives
        if line.startswith("LoadCredential=")
    }
    declared = {
        line.split("=", 1)[1].split("=", 1)[0]
        for line in directives
        if line.startswith("Environment=")
    }
    return {
        "present": True,
        "path": str(path),
        "shared_env": tuple(dict.fromkeys(shared)),
        "credentials": tuple(sorted(loaded)),
        "credential_paths": {
            line.split("=", 1)[1].split(":", 1)[0]: line.split(":", 1)[1]
            for line in directives
            if line.startswith("LoadCredential=") and ":" in line
        },
        # 유닛은 설정 **파일을 가리키기만** 한다. 값을 박으면 서버마다 다른
        # 유닛이 생기고, 저장소의 유닛과 갈린 사실이 아무 데도 안 보인다.
        "hardcoded_settings": tuple(
            name
            for name in (*REQUIRED_SETTINGS, *OPTIONAL_SETTINGS)
            if name in declared
        ),
        "environment_files": environment_files,
        "settings_file": SETTINGS_FILE in environment_files,
        "enablable": "[Install]" in directives,
    }


def settings_file_facts(path: pathlib.Path) -> dict:
    """전용 설정 파일을 읽는다. **키 이름만 들고 나온다 — 값은 버린다.**

    `EnvironmentFile=` 은 이 파일의 모든 줄을 환경변수로 만든다. 그래서 이것은
    「설정」 이 아니라 **「프로세스의 환경 전체」** 다. 허용 목록 밖의 키는 전부
    거절한다 — `DATABASE_URL` 한 줄이면 전용 DB 계정이 무의미해지고, 토큰 한
    줄이면 0640 파일에 시크릿이 눕는다.
    """
    allowed = {*REQUIRED_SETTINGS, *OPTIONAL_SETTINGS}
    facts: dict = {"path": str(path), "present": False}
    try:
        info = path.stat()
        raw = path.read_text(encoding="utf-8")
    except OSError:
        facts["unreadable"] = path.exists()
        return facts
    except UnicodeError:
        facts["present"] = True
        facts["unreadable"] = True
        return facts

    facts["present"] = True
    if os.name == "posix":
        mode = stat.S_IMODE(info.st_mode)
        # 0640 root:tybot 을 기대한다. 그룹 쓰기·other 비트가 있으면 남이
        # 아카이브 경로와 예산을 바꿔 끼울 수 있다.
        if mode & MODE_FORBIDDEN_BITS:
            facts["mode_open"] = oct(mode)
        # **소유자가 root 여야 한다.** `tybot` 소유면 쓰기 비트가 없어도
        # 서비스 프로세스가 `chmod` 한 줄로 자기 근거 범위와 예산을 고칠 수
        # 있다. 봇이 자기 권한을 넓힐 수 있으면 그건 권한이 아니다.
        if info.st_uid != 0:
            facts["owner"] = info.st_uid

    keys: list[str] = []
    invalid = 0
    for row in raw.splitlines():
        line = row.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, _value = line.partition("=")
        name = name.strip()
        if not separator or not name:
            invalid += 1
            continue
        keys.append(name)
    facts["invalid_lines"] = invalid
    facts["missing"] = tuple(name for name in REQUIRED_SETTINGS if name not in keys)
    # **이름만** 적는다. 값은 읽지도 들고 있지도 않는다.
    facts["forbidden"] = tuple(
        dict.fromkeys(name for name in keys if name not in allowed)
    )
    return facts


def allowed_values(path: pathlib.Path) -> dict:
    """설정 파일에서 **허용된 네 키의 값만** 읽는다. 다른 줄은 쳐다보지 않는다.

    값이 필요한 이유는 하나다 — 아카이브 경로가 **실제로 있는지** 보려면 그
    경로를 알아야 한다. 읽은 값은 판정에만 쓰고 출력에는 넣지 않는다.
    """
    allowed = {*REQUIRED_SETTINGS, *OPTIONAL_SETTINGS}
    values: dict = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return values
    for row in raw.splitlines():
        line = row.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator and name.strip() in allowed:
            values[name.strip()] = value.strip().strip("'\"")
    return values


def credential_facts(unit: dict) -> tuple[dict, ...]:
    """유닛이 적은 자격증명 **경로 그대로** 본다. 값은 안 들고 나온다."""
    found: list[dict] = []
    for name, path in sorted((unit.get("credential_paths") or {}).items()):
        entry = {"name": name, "path": path, "present": False}
        target = pathlib.Path(path)
        try:
            info = target.stat()
        except OSError:
            found.append(entry)
            continue
        entry["present"] = True
        if os.name == "posix":
            open_bits = stat.S_IMODE(info.st_mode) & 0o077
            if open_bits:
                entry["mode_open"] = oct(stat.S_IMODE(info.st_mode))
        entry["invalid"] = not _credential_shape_ok(name, target)
        found.append(entry)
    return tuple(found)


def _credential_shape_ok(name: str, path: pathlib.Path) -> bool:
    """모양만 본다. **값은 돌려주지도 적지도 않는다.**"""
    try:
        if name.endswith("database-url"):
            from tybot.vipbot_dsn import resolve_vipbot_database_url

            # 이 해석기는 인자를 안 받고 `os.environ` 만 본다. 베껴 쓰면 두
            # 검증이 갈라지므로, **자기 프로세스의 변수 하나만** 잠시 세웠다
            # 되돌린다. 돌려받은 DSN 은 쓰지 않고 바로 버린다.
            previous = os.environ.get("VIPBOT_DATABASE_URL_FILE")
            os.environ["VIPBOT_DATABASE_URL_FILE"] = str(path)
            try:
                resolve_vipbot_database_url()
            finally:
                if previous is None:
                    os.environ.pop("VIPBOT_DATABASE_URL_FILE", None)
                else:
                    os.environ["VIPBOT_DATABASE_URL_FILE"] = previous
            return True
        if name.endswith("anthropic-api-key"):
            from tybot.vipbot_anthropic_credential import resolve_anthropic_api_key

            resolve_anthropic_api_key({"CREDENTIALS_DIRECTORY": str(path.parent)})
            return True
        if name.endswith("secret-key"):
            from cryptography.fernet import Fernet

            Fernet(path.read_text(encoding="utf-8").strip().encode("ascii"))
            return True
    except Exception:  # noqa: BLE001 - 이유는 모양 하나로 충분하다
        return False
    return True


def settings_facts(environ=None) -> tuple[dict, dict]:
    """운영자가 들고 있는 환경에서 설정을 읽는다. 공용 이름은 안 본다."""
    values = os.environ if environ is None else environ
    workspace = str(values.get("VIPBOT_WORKSPACE", "") or "").strip()
    raw_archive = str(values.get("VIPBOT_ARCHIVE_DIR", "") or "").strip()
    raw_budget = str(values.get("VIPBOT_DAILY_LIMIT_USD", "") or "").strip()
    model = str(values.get("VIPBOT_MODEL", "") or "").strip()

    budget = "missing" if not raw_budget else "ok"
    if raw_budget:
        try:
            from tybot.vip_profile import load_profile

            load_profile({"VIPBOT_DAILY_LIMIT_USD": raw_budget})
        except Exception:  # noqa: BLE001 - 고정 낱말로만 말한다
            budget = "invalid"

    from tybot.vip_profile import APPROVED_MODEL

    settings = {
        "workspace": workspace,
        "budget": budget,
        "model_not_approved": bool(model) and model != APPROVED_MODEL,
        "state_directory_missing": False,
    }
    archive: dict = {"configured": bool(raw_archive), "path": raw_archive}
    if raw_archive:
        candidate = pathlib.Path(raw_archive)
        archive["configured"] = candidate.is_absolute()
        archive["readable"] = candidate.is_dir()
    return settings, archive


def database_facts(workspace: str) -> Facts:
    """전용 DSN 으로 **그 계정이 되어** 묻는다. 변경 문장은 한 줄도 없다."""
    import psycopg

    from tybot.vipbot_dsn import resolve_vipbot_database_url

    roles: dict = {}
    functions: dict = {}
    base_tables: dict = {}
    connection: dict = {"workspace": workspace}
    with psycopg.connect(resolve_vipbot_database_url()) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT rolname, rolcanlogin,
                      concat_ws(' ',
                        CASE WHEN rolsuper THEN 'SUPERUSER' END,
                        CASE WHEN rolcreatedb THEN 'CREATEDB' END,
                        CASE WHEN rolcreaterole THEN 'CREATEROLE' END,
                        CASE WHEN rolreplication THEN 'REPLICATION' END,
                        CASE WHEN rolbypassrls THEN 'BYPASSRLS' END)
                 FROM pg_roles WHERE rolname = ANY(%s)""",
            ([GROUP_ROLE, RUNTIME_ROLE, DEFINER_ROLE],),
        )
        for name, can_login, unsafe in cur.fetchall():
            roles[name] = {"can_login": bool(can_login), "unsafe": unsafe or ""}
        # **양방향으로 읽는다.** child 쪽만 보면 「우리 역할이 누구의 멤버인가」
        # 만 보이고, 「그룹에 누가 들어와 있는가」 는 안 보인다. 낯선 역할 하나가
        # `tybot_vipbot` 의 멤버가 되면 그 역할도 창구를 실행할 수 있는데,
        # 한쪽만 읽는 점검은 끝까지 초록이다.
        cur.execute(
            """SELECT parent.rolname, child.rolname, a.admin_option
                 FROM pg_auth_members a
                 JOIN pg_roles parent ON parent.oid = a.roleid
                 JOIN pg_roles child ON child.oid = a.member
                WHERE parent.rolname = ANY(%s) OR child.rolname = ANY(%s)
                ORDER BY 1, 2""",
            (
                [GROUP_ROLE, RUNTIME_ROLE, DEFINER_ROLE],
                [GROUP_ROLE, RUNTIME_ROLE, DEFINER_ROLE],
            ),
        )
        for parent, child, admin_option in cur.fetchall():
            if child in (GROUP_ROLE, RUNTIME_ROLE, DEFINER_ROLE):
                roles.setdefault(child, {}).setdefault("memberships", [])
                roles[child]["memberships"].append((parent, bool(admin_option)))
            if parent in (GROUP_ROLE, RUNTIME_ROLE, DEFINER_ROLE):
                roles.setdefault(parent, {}).setdefault("members", [])
                roles[parent]["members"].append((child, bool(admin_option)))

        for signature in (CONFIG_FN, ACCESS_FN):
            cur.execute("SELECT to_regprocedure(%s) IS NOT NULL", (signature,))
            if not cur.fetchone()[0]:
                continue
            # PUBLIC 은 **`pg_roles` 의 행이 아니다.** `has_function_privilege`
            # 에 `'public'` 을 넣으면 `role "public" does not exist` 로 터지거나,
            # 터지지 않으면 엉뚱한 역할을 물어본 것이다. ACL 을 직접 편다 —
            # PUBLIC 은 grantee oid **0** 이다.
            #
            # `proacl` 이 NULL 이면 「아무에게도 안 줬다」 가 아니라 **기본값**이고,
            # 함수의 기본값은 `EXECUTE TO PUBLIC` 이다. `acldefault` 로 그 기본을
            # 펴서 같이 센다.
            cur.execute(
                """SELECT r.rolname,
                          has_function_privilege(%s, %s, 'EXECUTE'),
                          EXISTS (
                            SELECT 1
                              FROM aclexplode(
                                     COALESCE(p.proacl, acldefault('f', p.proowner))
                                   ) acl
                             WHERE acl.grantee = 0
                               AND acl.privilege_type = 'EXECUTE'
                          )
                     FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
                    WHERE p.oid = %s::regprocedure""",
                (RUNTIME_ROLE, signature, signature),
            )
            owner, runtime_execute, public_execute = cur.fetchone()
            functions[signature] = {
                "owner": owner,
                "runtime_execute": bool(runtime_execute),
                "public_execute": bool(public_execute),
            }

        for table in BASE_TABLES:
            cur.execute(
                "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
            )
            if not cur.fetchone()[0]:
                continue
            cur.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (RUNTIME_ROLE, f"public.{table}"),
            )
            base_tables[table] = bool(cur.fetchone()[0])

        if workspace:
            # 창구 함수만 부른다. 결과는 **세기만** 하고 버린다 — 암호문은
            # 돌아오지만 어디에도 적지 않는다.
            cur.execute(
                "SELECT state, team_id, bot_user_id,"
                "       bot_ciphertext IS NOT NULL, app_ciphertext IS NOT NULL"
                "  FROM public.vipbot_connection_config(%s)",
                (workspace,),
            )
            rows = cur.fetchall()
            connection["rows"] = len(rows)
            connection["complete"] = bool(rows) and all(
                row[1] and row[2] and row[3] and row[4] for row in rows
            )
    return Facts(
        roles=roles,
        functions=functions,
        base_tables=base_tables,
        connection=connection,
    )


def gather(
    *,
    unit_path: pathlib.Path,
    skip_db: bool,
    environ=None,
    settings_path: str | None = None,
) -> Facts:
    """서버 상태 한 장. **읽기만** 한다."""
    unit = unit_facts(unit_path)
    credentials = credential_facts(unit)
    conf_path = pathlib.Path(settings_path or SETTINGS_FILE)
    conf = settings_file_facts(conf_path)
    # 설정은 **그 파일이 정본**이다. 운영자가 셸에서 덮어쓴 값이 있으면 그걸
    # 쓰되(점검을 돌려 보는 흔한 방법이다), 없으면 파일에서 읽는다.
    values = dict(allowed_values(conf_path))
    values.update(
        {
            name: value
            for name, value in (os.environ if environ is None else environ).items()
            if name in {*REQUIRED_SETTINGS, *OPTIONAL_SETTINGS} and value
        }
    )
    settings, archive = settings_facts(values)
    if "tybot-vipbot-database-url" not in (unit.get("credentials") or ()):
        unit.setdefault("missing_credentials", [])
        unit["missing_credentials"] = (
            *(unit.get("missing_credentials") or ()),
            "tybot-vipbot-database-url",
        )

    if skip_db:
        return Facts(
            credentials=credentials,
            archive=archive,
            settings=settings,
            settings_file=conf,
            unit=unit,
            database_checked=False,
        )
    try:
        database = database_facts(settings.get("workspace") or "")
    except Exception as exc:  # noqa: BLE001 - 연결 실패도 「준비 안 됨」 이다
        return Facts(
            credentials=credentials,
            archive=archive,
            settings=settings,
            settings_file=conf,
            unit=unit,
            database_checked=False,
            database_error=type(exc).__name__,
        )
    return Facts(
        roles=database.roles,
        functions=database.functions,
        base_tables=database.base_tables,
        connection=database.connection,
        credentials=credentials,
        archive=archive,
        settings=settings,
        settings_file=conf,
        unit=unit,
    )


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------


def render(facts: Facts, findings: list[Finding]) -> str:
    lines = ["== VIPBot 활성화 준비 점검 (읽기 전용)"]
    lines.append(f"유닛        : {facts.unit.get('path') or '-'}")
    lines.append(
        f"DB 점검     : {'했음' if facts.database_checked else '건너뜀'}"
        + (f" ({facts.database_error})" if facts.database_error else "")
    )
    lines.append(f"자격증명    : {len(facts.credentials)}개 확인")
    lines.append(f"설정 파일   : {facts.settings_file.get('path') or '-'}")
    lines.append(f"워크스페이스: {facts.settings.get('workspace') or '(미설정)'}")
    lines.append("")
    if not findings:
        lines.append("준비됨: 막는 것이 없습니다. 활성화 절차 문서의 마지막 단계로 가세요.")
        if facts.unit.get("present") and not facts.unit.get("enablable"):
            lines.append("   (유닛에는 아직 [Install] 이 없습니다. 그게 마지막 단계입니다)")
        return "\n".join(lines)
    lines.append(f"준비 안 됨: 켜기 전에 할 일 {len(findings)}건:")
    lines.extend(f"  - {finding}" for finding in findings)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    default_unit = (
        pathlib.Path(__file__).resolve().parent.parent
        / "deploy"
        / "tybot-vipbot.service"
    )
    parser = argparse.ArgumentParser(
        description="VIPBot 활성화 준비 점검(읽기 전용). 아무것도 바꾸지 않는다.",
    )
    parser.add_argument("--unit", default=str(default_unit))
    parser.add_argument(
        "--settings",
        default=SETTINGS_FILE,
        help=f"전용 설정 파일 (기본 {SETTINGS_FILE})",
    )
    parser.add_argument(
        "--skip-db", action="store_true", help="DB 에 못 붙는 자리에서 쓴다"
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    facts = gather(
        unit_path=pathlib.Path(args.unit),
        skip_db=args.skip_db,
        settings_path=args.settings,
    )
    findings = evaluate(facts)
    if args.json:
        import json

        print(
            json.dumps(
                {
                    "ready": not findings,
                    "database_checked": facts.database_checked,
                    "findings": [
                        {"code": item.code, "detail": item.detail} for item in findings
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        print(render(facts, findings))
    return EXIT_OK if not findings else EXIT_PROBLEM


if __name__ == "__main__":
    raise SystemExit(main())
