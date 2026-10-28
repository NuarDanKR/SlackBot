"""B-81 이미지 설명 운영 현황의 본문 없는 콘솔 조회 경계."""
from __future__ import annotations

import os
import re

from ..archive.image_description_store import ImageDescriptionDenied, ImageDescriptionStore

_CODE = re.compile(r"^[a-z0-9_]*$")


class ImageDescriptionAdminError(RuntimeError):
    """시크릿·DB 예외 본문을 포함하지 않는 콘솔 오류."""


def _connect():
    url = os.getenv("TYBOT_VISION_DATABASE_URL", "").strip()
    if not url:
        raise ImageDescriptionAdminError("vision_database_unconfigured")
    try:
        import psycopg

        return psycopg.connect(
            url,
            autocommit=True,
            row_factory=psycopg.rows.dict_row,
        )
    except Exception as exc:
        raise ImageDescriptionAdminError("vision_database_unavailable") from exc


def _safe_code(value: object) -> str:
    code = str(value or "")
    return code if _CODE.fullmatch(code) else "status_unavailable"


def _policy_response(policy, config: dict, sources: dict) -> dict:
    return {
        "revision": policy.revision,
        "globalEnabled": policy.enabled,
        "workspaceEnabled": policy.workspace_enabled,
        "effectiveEnabled": policy.enabled and policy.workspace_enabled,
        "provider": policy.provider,
        "model": policy.model,
        "region": policy.region,
        "sensitivity": policy.sensitivity.value,
        "maxImageBytes": policy.max_image_bytes,
        "maxOutputTokens": policy.max_output_tokens,
        "timeoutSeconds": policy.timeout_seconds,
        "catalogVerificationTtlSeconds": policy.catalog_verification_ttl_seconds,
        "dailyCalls": config.get("daily_calls") if type(config.get("daily_calls")) is int else None,
        "maxConcurrency": (
            config.get("max_concurrency")
            if type(config.get("max_concurrency")) is int else None
        ),
        "sources": dict(sources),
        "configuredKeys": sorted(config),
        # Worker service is not installed yet. Unknown must not look applied.
        "workerAppliedRevision": None,
        "applicationState": "not_configured",
    }


def _catalog_response(row: dict) -> dict:
    return {
        "provider": row["provider"],
        "model": row["model"],
        "supportsVision": row["supports_vision"],
        "zeroCostVerified": row["zero_cost_verified"],
        "verifiedAt": row["verified_at"],
        "verificationRevision": row["verification_revision"],
        "maxSensitivity": row["max_sensitivity"],
        "allowedRegions": list(row["allowed_regions"]),
        "credentialReady": row["credential_ready"],
        "quotaState": row["quota_state"],
        "quotaRemaining": row["quota_remaining"],
        "quotaResetsAt": row["quota_resets_at"],
        "updatedAt": row["updated_at"],
    }


def _job_response(row: dict) -> dict:
    return {
        "id": row["id"],
        "state": row["state"],
        "errorCode": _safe_code(row.get("error_code")),
        "policyRevision": row["policy_revision"],
        "promptRevision": row["prompt_revision"],
        "channelId": row["channel_id"],
        "fileId": row["file_id"],
        "originalSha256Mask": row["original_sha256_mask"],
        "actualModel": row["actual_model"],
        "actualProvider": row["actual_provider"],
        "inputTokens": row["input_tokens"],
        "outputTokens": row["output_tokens"],
        "actualUsd": row["actual_usd"],
        "createdAt": row["created_at"],
        "dispatchedAt": row["dispatched_at"],
        "finishedAt": row["finished_at"],
        "expiresAt": row["expires_at"],
    }


def snapshot(workspace: str, *, limit: int = 50, connect=None) -> dict:
    """정책·카탈로그·작업 좌표만 반환한다. 설명/원본/시크릿은 조회하지 않는다."""
    workspace = workspace.strip().lower()
    if not workspace or type(limit) is not int or not 1 <= limit <= 200:
        raise ImageDescriptionAdminError("invalid_request")
    connector = connect or _connect
    try:
        with connector() as conn:
            policy, config, sources = ImageDescriptionStore(conn).effective_policy(workspace)
            with conn.cursor() as cur:
                cur.execute("SELECT clock_timestamp() AS generated_at")
                generated_at = cur.fetchone()["generated_at"]
                cur.execute(
                    """SELECT provider,model,supports_vision,zero_cost_verified,
                              verified_at,verification_revision,max_sensitivity,
                              allowed_regions,credential_ready,quota_state,
                              quota_remaining,quota_resets_at,updated_at
                         FROM archiver_image_model_catalog
                        ORDER BY provider,model"""
                )
                catalog = [_catalog_response(dict(row)) for row in cur.fetchall()]
                cur.execute(
                    """SELECT id,state,error_code,policy_revision,prompt_revision,
                              scope->>'channel' AS channel_id,
                              scope->>'file_id' AS file_id,
                              left(scope->>'original_sha256',12) AS original_sha256_mask,
                              actual_model,actual_provider,input_tokens,output_tokens,
                              CASE WHEN actual_usd IS NULL THEN NULL
                                   ELSE actual_usd::text END AS actual_usd,
                              created_at,dispatched_at,finished_at,expires_at
                         FROM archiver_image_description_job
                        WHERE workspace=%s
                        ORDER BY created_at DESC LIMIT %s""",
                    (workspace, limit),
                )
                jobs = [_job_response(dict(row)) for row in cur.fetchall()]
                cur.execute(
                    """SELECT
                         (SELECT count(*) FROM archiver_image_description_approval
                           WHERE workspace=%s AND revoked_at IS NULL
                             AND expires_at > clock_timestamp()) AS active_approvals,
                         (SELECT count(*) FROM archiver_image_description_clearance
                           WHERE workspace=%s AND revoked_at IS NULL
                             AND expires_at > clock_timestamp()) AS active_clearances""",
                    (workspace, workspace),
                )
                counts = dict(cur.fetchone())
    except ImageDescriptionDenied as exc:
        code = _safe_code(exc.args[0] if exc.args else "")
        raise ImageDescriptionAdminError(code or "policy_unavailable") from exc
    except ImageDescriptionAdminError:
        raise
    except Exception as exc:
        raise ImageDescriptionAdminError("vision_status_unavailable") from exc

    return {
        "workspace": workspace,
        "generatedAt": generated_at,
        "policy": _policy_response(policy, config, sources),
        "catalog": catalog,
        "jobs": jobs,
        "counts": {
            "activeApprovals": int(counts["active_approvals"]),
            "activeClearances": int(counts["active_clearances"]),
        },
    }


__all__ = ["ImageDescriptionAdminError", "snapshot"]
