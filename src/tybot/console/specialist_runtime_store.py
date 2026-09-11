"""전문 봇 2단계 런타임 — 제출·빌드·배포 상태 저장소.

설계: [`../../../docs/design/specialist-runtime-v2.md`](../../../docs/design/specialist-runtime-v2.md)

## 상태 전이를 코드가 소유한다

수명주기가 문자열 비교로 흩어지면, 「빌드 실패인데 배포 가능」 같은 조합이 어디선가
만들어진다. 여기서 전이표 하나를 두고 그 밖의 이동은 거부한다.

## 요청자와 승인자는 다르다

관리자가 직접 등록해야 할 때도 **다른 관리자**가 승인한다. 한 사람이 둘 다 하면
승인이 형식이 되고, 그러면 승인 기록은 있는데 아무도 안 본 상태가 된다.
비상 운영은 별도의 break-glass 감사 이벤트로만 남긴다.

## 저장하지 않는 것

질문·근거·응답 본문·HMAC 평문·provider key 평문·업로드 원본 경로.
남기는 것은 식별자, digest, 상태, 오류 코드, 시각이다.
"""
from __future__ import annotations

import json
import os
import re

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

# 전문가를 **어떻게 부르는가.** 이 목록과 DB 의 CHECK 제약이 갈리면, 코드가
# 아는 값을 DB 가 거부한다 — 2026-09-11 에 `tools` 로 그렇게 막혔다. 스키마를
# 올리지 않은 설치에서 나는 일이고, 오류 문구가 「제약 위반」 이라 무엇을
# 적용해야 하는지는 말해 주지 않는다.
#
# 테스트가 이 목록과 스키마 파일을 대조한다.
EXECUTION_MODES = ("prompt", "tools", "http")

# 소스 상태와 런타임 상태를 섞지 않는다(설계 §배포 수명주기).
SOURCE_STATES = ("uploaded", "source_verified", "source_rejected")
BUILD_STATES = ("building", "build_failed", "image_ready", "contract_failed")
DEPLOY_STATES = ("standby", "active", "retired", "failed")

# 허용된 전이만. 그 밖은 거부한다 — 「빌드 실패인데 배포 가능」 같은 조합이
# 어디선가 만들어지는 것을 막는 자리다.
_BUILD_NEXT: dict[str, tuple[str, ...]] = {
    "building": ("image_ready", "build_failed", "contract_failed"),
    "image_ready": (),
    "build_failed": (),
    "contract_failed": (),
}
_DEPLOY_NEXT: dict[str, tuple[str, ...]] = {
    "standby": ("active", "failed", "retired"),
    "active": ("retired", "failed"),
    "failed": ("retired",),
    "retired": (),
}


class RuntimeStoreError(RuntimeError):
    """런타임 저장소 작업 실패. 문구가 콘솔에 그대로 보인다."""


def _connect():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeStoreError("DATABASE_URL 이 없습니다.")
    try:
        import psycopg

        return psycopg.connect(url, row_factory=psycopg.rows.dict_row)
    except RuntimeStoreError:
        raise
    except Exception as exc:
        raise RuntimeStoreError(f"런타임 DB 연결 실패: {exc}") from exc


def is_ready() -> bool:
    """2단계 스키마가 적용됐는가. **읽기 실패는 「아니다」** — 막는 쪽이 기본값이다."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.specialist_deployment') IS NOT NULL AS ready"
            )
            return bool(cur.fetchone()["ready"])
    except Exception:  # noqa: BLE001 - 기능 탐지는 닫는 쪽으로 실패한다
        return False


# --- 전이 규칙 ----------------------------------------------------------------
def check_build_transition(current: str, nxt: str) -> None:
    if nxt not in _BUILD_NEXT.get(current, ()):
        raise RuntimeStoreError(f"빌드 상태를 {current} → {nxt} 로 바꿀 수 없습니다.")


def check_deploy_transition(current: str, nxt: str) -> None:
    if nxt not in _DEPLOY_NEXT.get(current, ()):
        raise RuntimeStoreError(f"배포 상태를 {current} → {nxt} 로 바꿀 수 없습니다.")


def check_separation(requester: str, approver: str) -> None:
    """요청자와 승인자는 달라야 한다.

    한 사람이 둘 다 하면 승인이 형식이 되고, 승인 기록은 있는데 아무도 안 본
    상태가 된다. 비상 운영은 break-glass 로 따로 남긴다.
    """
    if not requester.strip() or not approver.strip():
        raise RuntimeStoreError("요청자와 승인자를 모두 남겨야 합니다.")
    if requester.strip().lower() == approver.strip().lower():
        raise RuntimeStoreError(
            "요청한 사람이 스스로 승인할 수 없습니다. 다른 관리자가 승인해야 합니다."
        )


# --- 제출물 -------------------------------------------------------------------
def create_source(
    *,
    specialist: str,
    source_type: str,
    submitted_by: str,
    repository_url: str = "",
    release_ref: str = "",
    source_commit: str = "",
    source_name: str = "",
    bundle_sha256: str = "",
    quarantine_key: str = "",
) -> int:
    if not KEY_RE.match(specialist):
        raise RuntimeStoreError("전문가 key 형식이 올바르지 않습니다.")
    if source_type not in ("git", "zip"):
        raise RuntimeStoreError("source_type 은 git|zip 이어야 합니다.")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO specialist_source
                       (specialist, source_type, repository_url, release_ref,
                        source_commit, source_name, bundle_sha256, quarantine_key,
                        submitted_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (specialist, source_type, repository_url, release_ref, source_commit,
                 source_name, bundle_sha256, quarantine_key, submitted_by),
            )
            new_id = int(cur.fetchone()["id"])
            conn.commit()
    except RuntimeStoreError:
        raise
    except Exception as exc:
        raise RuntimeStoreError(f"제출물 저장 실패: {exc}") from exc
    return new_id


def set_source_status(source_id: int, status: str, *, error_code: str = "") -> None:
    if status not in SOURCE_STATES:
        raise RuntimeStoreError(f"알 수 없는 제출물 상태: {status}")
    _execute(
        "UPDATE specialist_source SET status = %s, error_code = %s WHERE id = %s",
        (status, error_code[:80], source_id),
    )


def list_sources(specialist: str = "", limit: int = 50) -> list[dict]:
    where = "WHERE specialist = %s" if specialist else ""
    params: tuple = (specialist, limit) if specialist else (limit,)
    return _query(
        f"""
        SELECT id, specialist, source_type, repository_url, release_ref,
               source_commit, source_name, bundle_sha256, status, error_code,
               submitted_by, submitted_at
          FROM specialist_source {where}
         ORDER BY submitted_at DESC LIMIT %s
        """,
        params,
    )


# --- 빌드 ---------------------------------------------------------------------
def create_build(*, source_id: int, runtime: str) -> int:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO specialist_build (source_id, runtime) VALUES (%s, %s) "
                "RETURNING id",
                (source_id, runtime),
            )
            new_id = int(cur.fetchone()["id"])
            conn.commit()
    except Exception as exc:
        raise RuntimeStoreError(f"빌드 기록 생성 실패: {exc}") from exc
    return new_id


def finish_build(
    build_id: int,
    *,
    status: str,
    image_digest: str = "",
    sbom_sha256: str = "",
    checks: list | None = None,
    error_code: str = "",
) -> None:
    """빌드 종료. **성공이면 digest 가 반드시 있어야 한다.**

    없으면 무엇을 배포할지 모르는 채로 `image_ready` 가 되고, 그 행을 승인하는
    사람은 존재하지 않는 이미지를 승인하게 된다.
    """
    if status not in BUILD_STATES:
        raise RuntimeStoreError(f"알 수 없는 빌드 상태: {status}")
    if status == "image_ready" and not DIGEST_RE.match(image_digest or ""):
        raise RuntimeStoreError("성공한 빌드에는 sha256 digest 가 필요합니다.")
    current = _one(
        "SELECT status FROM specialist_build WHERE id = %s", (build_id,)
    )
    if current is None:
        raise RuntimeStoreError("빌드 기록을 찾지 못했습니다.")
    check_build_transition(str(current["status"]), status)
    _execute(
        """
        UPDATE specialist_build
           SET status = %s, image_digest = %s, sbom_sha256 = %s,
               checks = %s, error_code = %s, finished_at = now()
         WHERE id = %s
        """,
        (status, image_digest, sbom_sha256,
         json.dumps(checks or [], ensure_ascii=False), error_code[:80], build_id),
    )


def get_build(build_id: int) -> dict | None:
    return _one(
        """
        SELECT b.*, s.specialist, s.source_type, s.repository_url, s.release_ref,
               s.source_commit, s.bundle_sha256, s.quarantine_key
          FROM specialist_build b JOIN specialist_source s ON s.id = b.source_id
         WHERE b.id = %s
        """,
        (build_id,),
    )


# --- 배포 ---------------------------------------------------------------------
def create_deployment(*, specialist: str, build_id: int, deployed_by: str) -> int:
    """후보를 `standby` 로 만든다. **바로 active 로 가지 않는다** — smoke 가 남았다."""
    build = get_build(build_id)
    if build is None:
        raise RuntimeStoreError("빌드 기록을 찾지 못했습니다.")
    if str(build["status"]) != "image_ready":
        raise RuntimeStoreError(
            f"빌드가 준비되지 않았습니다(현재 {build['status']})."
        )
    if str(build["specialist"]) != specialist:
        # digest 는 맞는데 남의 전문가 빌드를 이 자리에 붙이는 경우다.
        raise RuntimeStoreError("이 전문가의 빌드가 아닙니다.")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO specialist_deployment (specialist, build_id, deployed_by) "
                "VALUES (%s, %s, %s) RETURNING id",
                (specialist, build_id, deployed_by),
            )
            new_id = int(cur.fetchone()["id"])
            conn.commit()
    except Exception as exc:
        raise RuntimeStoreError(f"배포 기록 생성 실패: {exc}") from exc
    return new_id


def activate(deployment_id: int, *, actor: str) -> None:
    """후보를 active 로. **이전 active 는 같은 트랜잭션에서 retired 로 내린다.**

    나눠서 하면 그 사이에 active 가 둘인 순간이 생기고, 그때 온 질문은 어느
    digest 가 답했는지 알 수 없다. DB 의 부분 유니크 인덱스도 그 순간에 터진다.
    """
    row = _one("SELECT specialist, state FROM specialist_deployment WHERE id = %s",
               (deployment_id,))
    if row is None:
        raise RuntimeStoreError("배포 기록을 찾지 못했습니다.")
    check_deploy_transition(str(row["state"]), "active")
    specialist = str(row["specialist"])
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE specialist_deployment SET state = 'retired' "
                " WHERE specialist = %s AND state = 'active'",
                (specialist,),
            )
            cur.execute(
                "UPDATE specialist_deployment "
                "   SET state = 'active', deployed_by = %s, deployed_at = now() "
                " WHERE id = %s",
                (actor, deployment_id),
            )
            cur.execute(
                "UPDATE specialist_bot SET active_deployment_id = %s, updated_at = now(), "
                "       updated_by = %s WHERE key = %s",
                (deployment_id, actor, specialist),
            )
            conn.commit()
    except Exception as exc:
        raise RuntimeStoreError(f"활성화 실패: {exc}") from exc


def disable(specialist: str, *, actor: str) -> None:
    """라우팅을 즉시 닫는다. **컨테이너 중지를 기다리지 않는다.**

    남의 코드가 이상하게 답할 때 우리가 기다릴 수 있는 시간은 몇 분이 아니라
    몇 초다. 컨테이너 정리는 뒤따르고, 그것이 실패해도 라우팅은 이미 닫혀 있다.
    """
    if not KEY_RE.match(specialist):
        raise RuntimeStoreError("전문가 key 형식이 올바르지 않습니다.")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE specialist_bot SET state = 'disabled', active_deployment_id = NULL, "
                "       updated_at = now(), updated_by = %s WHERE key = %s",
                (actor, specialist),
            )
            cur.execute(
                "UPDATE specialist_deployment SET state = 'retired' "
                " WHERE specialist = %s AND state = 'active'",
                (specialist,),
            )
            conn.commit()
    except Exception as exc:
        raise RuntimeStoreError(f"중지 실패: {exc}") from exc


def rollback_target(specialist: str) -> dict | None:
    """직전 승인 digest. 없으면 `None` — 되돌릴 곳이 없다는 사실을 말해야 한다."""
    return _one(
        """
        SELECT d.id, d.build_id, b.image_digest, d.deployed_at
          FROM specialist_deployment d JOIN specialist_build b ON b.id = d.build_id
         WHERE d.specialist = %s AND d.state = 'retired' AND b.status = 'image_ready'
         ORDER BY d.deployed_at DESC LIMIT 1
        """,
        (specialist,),
    )


def set_health(deployment_id: int, *, health: str, error_code: str = "") -> None:
    if health not in ("unknown", "ok", "error"):
        raise RuntimeStoreError(f"알 수 없는 health: {health}")
    _execute(
        "UPDATE specialist_deployment SET health = %s, error_code = %s, "
        "       last_checked_at = now() WHERE id = %s",
        (health, error_code[:80], deployment_id),
    )


def active_deployment(specialist: str) -> dict | None:
    """라우팅이 쓸 배포. **health 가 정상이 아니면 돌려주지 않는다.**

    설계: `execution_mode=http` 인데 active 가 없거나 health 가 정상이 아니면
    라우터 후보에서 제외한다.
    """
    return _one(
        """
        SELECT d.id, d.specialist, d.health, b.image_digest, b.id AS build_id,
               s.source_commit, s.bundle_sha256
          FROM specialist_deployment d
          JOIN specialist_build b ON b.id = d.build_id
          JOIN specialist_source s ON s.id = b.source_id
         WHERE d.specialist = %s AND d.state = 'active' AND d.health = 'ok'
        """,
        (specialist,),
    )


def list_deployments(specialist: str = "", limit: int = 50) -> list[dict]:
    where = "WHERE d.specialist = %s" if specialist else ""
    params: tuple = (specialist, limit) if specialist else (limit,)
    return _query(
        f"""
        SELECT d.id, d.specialist, d.state, d.health, d.error_code,
               d.deployed_by, d.deployed_at, d.last_checked_at,
               b.image_digest, b.runtime, s.source_type, s.release_ref,
               s.source_commit, s.bundle_sha256
          FROM specialist_deployment d
          JOIN specialist_build b ON b.id = d.build_id
          JOIN specialist_source s ON s.id = b.source_id
          {where}
         ORDER BY d.deployed_at DESC LIMIT %s
        """,
        params,
    )


# --- 자격 ---------------------------------------------------------------------
def mask(secret: str) -> str:
    """콘솔에 보일 값. **복호화 API 를 만들지 않는다** — 화면으로 꺼낼 수 있으면
    그 순간 브라우저·로그·스크린샷이 전부 보관 장소가 된다."""
    tail = secret[-4:] if len(secret) >= 8 else ""
    return f"••••{tail}"


def put_secret(
    *, specialist: str, kind: str, ciphertext: str, mask_value: str, actor: str
) -> None:
    if kind not in ("hmac", "provider"):
        raise RuntimeStoreError("kind 는 hmac|provider 여야 합니다.")
    _execute(
        """
        INSERT INTO specialist_runtime_secret
               (specialist, kind, ciphertext, mask, updated_by)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (specialist, kind) DO UPDATE
           SET previous_ciphertext = specialist_runtime_secret.ciphertext,
               -- 교체 시 구/신 키를 잠깐 겹친다. 없으면 교체 순간의 요청이 전부
               -- 서명 불일치로 떨어진다.
               previous_until = now() + interval '5 minutes',
               ciphertext = excluded.ciphertext,
               mask = excluded.mask,
               updated_at = now(),
               updated_by = excluded.updated_by
        """,
        (specialist, kind, ciphertext, mask_value, actor),
    )


def secret_status(specialist: str) -> list[dict]:
    """마스크와 교체일만. 평문도 암호문도 돌려주지 않는다."""
    return _query(
        "SELECT kind, mask, enabled, updated_at, updated_by, previous_until "
        "  FROM specialist_runtime_secret WHERE specialist = %s ORDER BY kind",
        (specialist,),
    )


# --- 작은 도우미 --------------------------------------------------------------
def _execute(sql: str, params: tuple) -> None:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
    except RuntimeStoreError:
        raise
    except Exception as exc:
        raise RuntimeStoreError(f"저장 실패: {exc}") from exc


def _query(sql: str, params: tuple) -> list[dict]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except RuntimeStoreError:
        raise
    except Exception as exc:
        raise RuntimeStoreError(f"조회 실패: {exc}") from exc


def _one(sql: str, params: tuple) -> dict | None:
    rows = _query(sql, params)
    return rows[0] if rows else None


__all__ = [
    "BUILD_STATES",
    "DEPLOY_STATES",
    "DIGEST_RE",
    "EXECUTION_MODES",
    "SOURCE_STATES",
    "RuntimeStoreError",
    "activate",
    "active_deployment",
    "check_build_transition",
    "check_deploy_transition",
    "check_separation",
    "create_build",
    "create_deployment",
    "create_source",
    "disable",
    "finish_build",
    "get_build",
    "is_ready",
    "list_deployments",
    "list_sources",
    "mask",
    "put_secret",
    "rollback_target",
    "secret_status",
    "set_health",
    "set_source_status",
]
