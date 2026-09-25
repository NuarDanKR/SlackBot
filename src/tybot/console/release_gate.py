"""스키마 검증 게이트 — **검증 안 된 스키마로 운영에 쓰지 않는다.**

결정: 2026-09-25 오너 §9·§10. 검증 절차: `scripts/verify_schema_isolated.py`.

## 왜 필요한가

회사 PostgreSQL 의 DBA 권한이 없어 격리 DB 검증을 못 하고 있다. 그렇다고 개발을
멈추지 않기로 했으므로, **검증이 끝나지 않았다는 사실이 코드에 남아 있어야 한다.**

안 남기면 두 가지가 일어난다. 사람은 언젠가 검증을 했다고 기억하고, 콘솔은
`active` 버튼을 그냥 눌러 준다. 그 순간 운영 원문의 주인이 검증 안 된 표를 보고
바뀐다.

## 지문에 묶는다

게이트는 「검증했다」 가 아니라 **「이 스키마를 검증했다」** 를 기록한다.
`archiving_schema.sql` 을 한 글자라도 고치면 지문이 달라지고 게이트가 다시 닫힌다.

이게 요점이다. 「한 번 검증했으니 됐다」 로 두면, 검증 뒤에 고친 부분은 **아무도
확인하지 않은 채로** 운영에 간다. 그리고 고치는 것은 늘 검증 뒤다.

## 표식은 DB 밖에 둔다

검증 대상이 DB 인데 결과를 그 DB 에 적으면, 표가 잘못 섰을 때 결과도 못 읽는다.
`STATE_DIR` 아래 파일로 둔다.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: 지문에 넣는 스키마. `verify_schema_isolated.TARGET_FILES` 와 같아야 한다 —
#: 검증한 것과 지문을 잰 것이 다르면 게이트가 엉뚱한 것을 지킨다.
GATED_SQL = (
    "index_schema.sql",
    "console_schema.sql",
    "archiving_schema.sql",
    "workspace_service_schema.sql",
)

ROOT = Path(__file__).resolve().parents[3]
SQL_DIR = ROOT / "deploy" / "sql"

MARKER_NAME = "schema-verified.json"


def state_dir() -> Path:
    return Path(os.getenv("STATE_DIR", "").strip() or "/var/lib/tybot")


def marker_path() -> Path:
    return state_dir() / "state" / MARKER_NAME


def schema_fingerprint(sql_dir: Path | None = None) -> str:
    """검증 대상 스키마의 지문.

    경로와 내용 사이에 길이를 끼운다 — 없으면 파일을 갈라 붙인 다른 조합이 같은
    지문을 낸다(`archive_layout_convert.snapshot_digest` 와 같은 이유).

    파일이 없으면 그 사실도 지문에 넣는다. 조용히 건너뛰면 **파일이 사라진 것과
    검증된 것이 구분되지 않는다.**
    """
    base = sql_dir or SQL_DIR
    digest = hashlib.sha256()
    for name in sorted(GATED_SQL):
        raw = name.encode("utf-8")
        digest.update(f"{len(raw)}:".encode("ascii"))
        digest.update(raw)
        path = base / name
        body = path.read_bytes() if path.is_file() else b"<missing>"
        digest.update(hashlib.sha256(body).hexdigest().encode("ascii"))
    return digest.hexdigest()


@dataclass(frozen=True)
class GateStatus:
    """게이트 상태. **화면이 그대로 보여 줄 수 있는 모양**이다."""

    verified: bool
    reason: str
    current_fingerprint: str
    verified_fingerprint: str = ""
    verified_at: str = ""
    verified_by: str = ""
    verified_dsn_label: str = ""

    def as_json(self) -> dict:
        return {
            "verified": self.verified,
            "reason": self.reason,
            "currentFingerprint": self.current_fingerprint[:12],
            "verifiedFingerprint": self.verified_fingerprint[:12],
            "verifiedAt": self.verified_at,
            "verifiedBy": self.verified_by,
            "verifiedDsnLabel": self.verified_dsn_label,
        }


def gate_status() -> GateStatus:
    """지금 스키마가 검증된 상태인가.

    **닫힌 이유를 문장으로 들고 있다.** 「검증 안 됨」 만 보여 주면 사람이 무엇을
    해야 하는지 모르고, 모르면 아무도 안 한다.
    """
    current = schema_fingerprint()
    path = marker_path()
    if not path.is_file():
        return GateStatus(
            verified=False,
            reason=(
                "격리 DB 검증을 아직 하지 않았습니다. DBA 가 만든 전용 DB 에서 "
                "`scripts/verify_schema_isolated.py` 를 통과시키면 열립니다."
            ),
            current_fingerprint=current,
        )
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return GateStatus(
            verified=False,
            reason=f"검증 기록을 읽지 못했습니다({type(exc).__name__}). 다시 검증하세요.",
            current_fingerprint=current,
        )

    recorded = str(marker.get("fingerprint") or "")
    if recorded != current:
        return GateStatus(
            verified=False,
            reason=(
                "검증한 뒤 스키마가 바뀌었습니다. 고친 부분은 아무도 확인하지 않은 "
                f"상태입니다 — 검증 {recorded[:12] or '?'} · 지금 {current[:12]}. "
                "다시 검증하세요."
            ),
            current_fingerprint=current,
            verified_fingerprint=recorded,
            verified_at=str(marker.get("at") or ""),
            verified_by=str(marker.get("by") or ""),
        )

    return GateStatus(
        verified=True,
        reason="",
        current_fingerprint=current,
        verified_fingerprint=recorded,
        verified_at=str(marker.get("at") or ""),
        verified_by=str(marker.get("by") or ""),
        verified_dsn_label=str(marker.get("dsn_label") or ""),
    )


def _write_marker(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    )
    os.replace(temporary, path)
    return path


def record_pass(
    *, by: str, dsn_label: str, destination: Path | None = None
) -> Path:
    """검증 통과를 남긴다. **통과했을 때만 부른다.**

    `dsn_label` 은 DB **이름**이다. DSN 전체를 적으면 비밀번호가 상태 파일에
    남고, 상태 파일은 로그처럼 복사된다.
    """
    path = destination or marker_path()
    payload = {
        "fingerprint": schema_fingerprint(),
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "by": by,
        "dsn_label": dsn_label,
        "files": list(sorted(GATED_SQL)),
    }
    return _write_marker(path, payload)


def install_verified_artifact(source: Path, *, installed_by: str) -> Path:
    """Install a portable verification result only for this deployed schema."""
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateClosed(f"검증 artifact를 읽지 못했습니다: {type(exc).__name__}") from exc

    recorded = str(payload.get("fingerprint") or "")
    current = schema_fingerprint()
    if recorded != current:
        raise GateClosed(
            "검증 artifact와 배포된 스키마가 다릅니다: "
            f"artifact {recorded[:12] or '?'} · 서버 {current[:12]}"
        )
    if not str(payload.get("at") or "") or not str(payload.get("by") or ""):
        raise GateClosed("검증 artifact에 검증 시각 또는 실행자가 없습니다.")
    dsn_label = str(payload.get("dsn_label") or "")
    if not dsn_label or any(token in dsn_label for token in ("://", "@", "password")):
        raise GateClosed("검증 artifact의 DB 표시는 이름이어야 하며 DSN이면 안 됩니다.")

    installed = dict(payload)
    installed["installed_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    installed["installed_by"] = installed_by.strip() or "unknown"
    return _write_marker(marker_path(), installed)


class GateClosed(RuntimeError):
    """검증 전에는 못 하는 일이다. **사유를 사람 말로 들고 있다.**"""


def require_verified_schema(what: str) -> None:
    """검증 안 됐으면 막는다.

    `what` 은 막힌 동작의 이름이다. 「게이트가 닫혔습니다」 만 보여 주면 사람이
    무엇을 하려다 막혔는지 화면에서 못 읽는다.
    """
    status = gate_status()
    if status.verified:
        return
    raise GateClosed(f"{what}: {status.reason}")
