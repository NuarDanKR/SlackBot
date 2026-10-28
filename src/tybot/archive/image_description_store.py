"""B-81 이미지 설명의 영속 정책·승인·1회 dispatch·파생 revision 저장소.

전용 psycopg connection은 autocommit + dict_row여야 한다. 모델 I/O 동안 DB
transaction을 잡지 않는다. 수집기나 Master의 DSN으로 이 저장소를 열지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from tybot.gateway.base import Sensitivity

from .image_description import ImageDescription, ImageInput
from .image_description_clearance import SafetyAssessment
from .image_description_output_guard import guard_index_write
from .image_description_policy import (
    ImageDescriptionApproval,
    ImageDescriptionDispatch,
    ImageDescriptionPolicy,
    preflight_image_description,
)
from .image_model_catalog import FreeQuotaState, ImageModelCatalogEntry

CONFIG_KEYS = frozenset({
    "model",
    "provider",
    "region",
    "sensitivity",
    "catalog_verification_ttl_seconds",
    "max_image_bytes",
    "max_output_tokens",
    "timeout_seconds",
    "max_concurrency",
    "daily_calls",
})


class ImageDescriptionDenied(RuntimeError):
    """본문·시크릿을 포함하지 않는 운영 제어 코드."""


@dataclass(frozen=True)
class ImageDescriptionGrant:
    dispatch: ImageDescriptionDispatch
    model: str
    provider: str
    max_output_tokens: int
    timeout_seconds: float


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


def _catalog_snapshot(entry: ImageModelCatalogEntry) -> dict:
    """JSON 원장용 비민감 사실. set/datetime을 임의 문자열 한 덩어리로 만들지 않는다."""
    return {
        "model": entry.model,
        "provider": entry.provider,
        "supports_vision": entry.supports_vision,
        "zero_cost_verified": entry.zero_cost_verified,
        "verified_at": entry.verified_at.isoformat() if entry.verified_at else None,
        "verification_revision": entry.verification_revision,
        "max_sensitivity": entry.max_sensitivity.value,
        "allowed_regions": sorted(entry.allowed_regions),
        "credential_ready": entry.credential_ready,
        "quota_state": entry.quota_state.value,
        "quota_remaining": entry.quota_remaining,
        "quota_resets_at": entry.quota_resets_at.isoformat() if entry.quota_resets_at else None,
    }


def _money(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ImageDescriptionDenied("invalid_cost")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ImageDescriptionDenied("invalid_cost") from exc
    if not amount.is_finite() or amount < 0:
        raise ImageDescriptionDenied("invalid_cost")
    return amount


def approval_scope(approval: ImageDescriptionApproval) -> dict:
    data = asdict(approval)
    for key in ("approval_id", "expires_at", "revoked"):
        data.pop(key)
    return data


def dispatch_identity(scope: dict) -> str:
    """승인 갱신·정책 저장만으로 같은 bytes를 다시 보내지 않는다."""
    stable = {key: value for key, value in scope.items() if key != "policy_revision"}
    body = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


class ImageDescriptionStore:
    def __init__(self, connection) -> None:
        self.connection = connection

    @contextmanager
    def _transaction(self):
        if not self.connection.autocommit or self.connection.info.transaction_status != 0:
            raise ImageDescriptionDenied("connection_not_idle_autocommit")
        with self.connection.transaction():
            yield

    @staticmethod
    def _lock(cur):
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cur.execute(
            "SELECT * FROM archiver_image_description_policy WHERE workspace='' FOR UPDATE"
        )
        row = cur.fetchone()
        if not row:
            raise ImageDescriptionDenied("policy_unavailable")
        return row

    def save_policy(
        self,
        workspace: str,
        *,
        expected_revision: str | None,
        enabled: bool,
        config: dict,
        actor: str,
    ) -> str:
        if type(enabled) is not bool or not actor or set(config) - CONFIG_KEYS:
            raise ImageDescriptionDenied("policy_invalid")
        encoded = _json(config)
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                "SELECT revision FROM archiver_image_description_policy WHERE workspace=%s",
                (workspace,),
            )
            row = cur.fetchone()
            if (row["revision"] if row else None) != expected_revision:
                raise ImageDescriptionDenied("policy_conflict")
            revision = uuid4().hex
            cur.execute(
                """INSERT INTO archiver_image_description_policy_audit
                (workspace,revision,enabled,config,updated_by) VALUES (%s,%s,%s,%s::jsonb,%s)""",
                (workspace, revision, enabled, encoded, actor),
            )
            cur.execute(
                """INSERT INTO archiver_image_description_policy
                (workspace,revision,enabled,config,updated_by) VALUES (%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (workspace) DO UPDATE SET revision=EXCLUDED.revision,
                enabled=EXCLUDED.enabled, config=EXCLUDED.config,
                updated_by=EXCLUDED.updated_by, updated_at=now()""",
                (workspace, revision, enabled, encoded, actor),
            )
            return revision

    def save_catalog(
        self,
        entry: ImageModelCatalogEntry,
        *,
        expected_revision: str | None,
        actor: str,
    ) -> None:
        if not actor:
            raise ImageDescriptionDenied("actor_required")
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """SELECT verification_revision FROM archiver_image_model_catalog
                WHERE provider=%s AND model=%s FOR UPDATE""",
                (entry.provider, entry.model),
            )
            row = cur.fetchone()
            if (row["verification_revision"] if row else None) != expected_revision:
                raise ImageDescriptionDenied("catalog_conflict")
            cur.execute(
                """INSERT INTO archiver_image_model_catalog
                (provider,model,supports_vision,zero_cost_verified,verified_at,
                 verification_revision,max_sensitivity,allowed_regions,credential_ready,
                 quota_state,quota_remaining,quota_resets_at,updated_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (provider,model) DO UPDATE SET
                supports_vision=EXCLUDED.supports_vision,
                zero_cost_verified=EXCLUDED.zero_cost_verified,
                verified_at=EXCLUDED.verified_at,
                verification_revision=EXCLUDED.verification_revision,
                max_sensitivity=EXCLUDED.max_sensitivity,
                allowed_regions=EXCLUDED.allowed_regions,
                credential_ready=EXCLUDED.credential_ready,
                quota_state=EXCLUDED.quota_state,
                quota_remaining=EXCLUDED.quota_remaining,
                quota_resets_at=EXCLUDED.quota_resets_at,
                updated_by=EXCLUDED.updated_by,updated_at=now()""",
                (
                    entry.provider,
                    entry.model,
                    entry.supports_vision,
                    entry.zero_cost_verified,
                    entry.verified_at,
                    entry.verification_revision,
                    entry.max_sensitivity.value,
                    sorted(entry.allowed_regions),
                    entry.credential_ready,
                    entry.quota_state.value,
                    entry.quota_remaining,
                    entry.quota_resets_at,
                    actor,
                ),
            )

    @staticmethod
    def _catalog_entry(row) -> ImageModelCatalogEntry:
        return ImageModelCatalogEntry(
            model=row["model"],
            provider=row["provider"],
            supports_vision=row["supports_vision"],
            zero_cost_verified=row["zero_cost_verified"],
            verified_at=row["verified_at"],
            verification_revision=row["verification_revision"],
            max_sensitivity=Sensitivity(row["max_sensitivity"]),
            allowed_regions=frozenset(row["allowed_regions"]),
            credential_ready=row["credential_ready"],
            quota_state=FreeQuotaState(row["quota_state"]),
            quota_remaining=row["quota_remaining"],
            quota_resets_at=row["quota_resets_at"],
        )

    @staticmethod
    def _effective(cur, global_row, workspace: str):
        cur.execute(
            "SELECT * FROM archiver_image_description_policy WHERE workspace=%s",
            (workspace,),
        )
        local = cur.fetchone()
        if not local or not workspace:
            raise ImageDescriptionDenied("policy_unavailable")
        config = global_row["config"] | local["config"]
        try:
            policy = ImageDescriptionPolicy(
                workspace=workspace,
                revision=global_row["revision"] + ":" + local["revision"],
                enabled=global_row["enabled"],
                workspace_enabled=local["enabled"],
                model=config["model"],
                provider=config["provider"],
                region=config["region"],
                sensitivity=Sensitivity(config["sensitivity"]),
                catalog_verification_ttl_seconds=config["catalog_verification_ttl_seconds"],
                max_image_bytes=config["max_image_bytes"],
                max_output_tokens=config["max_output_tokens"],
                timeout_seconds=config["timeout_seconds"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ImageDescriptionDenied("policy_invalid") from exc
        return policy, config, local

    @staticmethod
    def _catalog(cur, policy: ImageDescriptionPolicy) -> ImageModelCatalogEntry:
        cur.execute(
            """SELECT * FROM archiver_image_model_catalog
            WHERE provider=%s AND model=%s""",
            (policy.provider, policy.model),
        )
        row = cur.fetchone()
        if not row:
            raise ImageDescriptionDenied("catalog_unavailable")
        return ImageDescriptionStore._catalog_entry(row)

    def effective_policy(self, workspace: str):
        with self._transaction(), self.connection.cursor() as cur:
            global_row = self._lock(cur)
            policy, config, local = self._effective(cur, global_row, workspace)
            sources = {
                key: "workspace" if key in local["config"] else "global" for key in config
            }
            return policy, config, sources

    def indexed_sources(
        self, workspace: str, *, channel_id: str = ""
    ) -> frozenset[tuple[str, str, str]]:
        """이미 파생 revision이 생긴 원본 좌표.

        설명 본문을 보류한 revision도 포함한다. 사람 검토 대상으로 이미 기록된
        사진을 자동 배치가 다시 모델에 보내면 보류가 재전송 통로가 된다.
        """
        if not workspace:
            raise ImageDescriptionDenied("workspace_required")
        with self._transaction(), self.connection.cursor() as cur:
            params: tuple[str, ...] = (workspace,)
            where = "workspace=%s"
            if channel_id:
                where += " AND channel_id=%s"
                params = (workspace, channel_id)
            cur.execute(
                "SELECT DISTINCT workspace,channel_id,original_sha256 "
                "FROM archiver_image_description_revision WHERE " + where,
                params,
            )
            return frozenset(
                (
                    str(row["workspace"]),
                    str(row["channel_id"]),
                    str(row["original_sha256"]),
                )
                for row in cur.fetchall()
            )

    def approve(self, approval: ImageDescriptionApproval, *, actor: str) -> None:
        if not actor or approval.revoked or not approval.approval_id:
            raise ImageDescriptionDenied("approval_invalid")
        with self._transaction(), self.connection.cursor() as cur:
            global_row = self._lock(cur)
            policy, _, _ = self._effective(cur, global_row, approval.workspace)
            if (
                approval.policy_revision,
                approval.model,
                approval.provider,
            ) != (policy.revision, policy.model, policy.provider):
                raise ImageDescriptionDenied("approval_scope_mismatch")
            cur.execute(
                """INSERT INTO archiver_image_description_approval
                (id,workspace,scope,expires_at,approved_by) VALUES (%s,%s,%s::jsonb,%s,%s)""",
                (
                    approval.approval_id,
                    approval.workspace,
                    _json(approval_scope(approval)),
                    approval.expires_at,
                    actor,
                ),
            )

    def active_approval_id(
        self,
        *,
        workspace: str,
        channel: str,
        file_id: str,
        original_sha256: str,
        provider: str,
        model: str,
        policy_revision: str,
        prompt_revision: str,
    ) -> str:
        """현재 외부 전송 경로와 정확히 결속된 유효 승인 ID."""
        values = (
            workspace,
            channel,
            file_id,
            original_sha256,
            provider,
            model,
            policy_revision,
            prompt_revision,
        )
        if any(not isinstance(value, str) or not value for value in values):
            raise ImageDescriptionDenied("approval_scope_invalid")
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """SELECT id FROM archiver_image_description_approval
                WHERE workspace=%s AND scope->>'channel'=%s
                  AND scope->>'file_id'=%s AND scope->>'original_sha256'=%s
                  AND scope->>'provider'=%s AND scope->>'model'=%s
                  AND scope->>'policy_revision'=%s
                  AND scope->>'prompt_revision'=%s
                  AND revoked_at IS NULL AND expires_at > clock_timestamp()
                ORDER BY created_at DESC,id DESC LIMIT 1""",
                values,
            )
            row = cur.fetchone()
            return str(row["id"]) if row else ""

    def revoke(self, approval_id: str, *, actor: str) -> None:
        if not actor:
            raise ImageDescriptionDenied("actor_required")
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """UPDATE archiver_image_description_approval
                SET revoked_at=now(),revoked_by=%s WHERE id=%s AND revoked_at IS NULL""",
                (actor, approval_id),
            )
            # 전송 여부가 모호한 dispatching/unknown은 취소하거나 quota를 돌려주지 않는다.
            cur.execute(
                """UPDATE archiver_image_description_job
                SET state='cancelled',finished_at=now()
                WHERE approval_id=%s AND state='reserved'""",
                (approval_id,),
            )

    def save_clearance(
        self,
        clearance_id: str,
        assessment: SafetyAssessment,
        *,
        reason: str,
        actor: str,
    ) -> None:
        """사진 하나의 사람 안전 판정을 append-only 원장에 남긴다."""
        scope = (
            assessment.workspace,
            assessment.channel_id,
            assessment.file_id,
            assessment.provider,
            assessment.model,
            assessment.policy_revision,
            assessment.visibility,
        )
        if (
            not isinstance(clearance_id, str)
            or not clearance_id.strip()
            or clearance_id != clearance_id.strip()
            or not isinstance(actor, str)
            or not actor.strip()
            or actor != actor.strip()
            or not isinstance(reason, str)
            or not reason.strip()
            or any(
                not isinstance(value, str) or not value.strip() or value != value.strip()
                for value in scope
            )
            or not isinstance(assessment.original_sha256, str)
            or re.fullmatch(r"[a-f0-9]{64}", assessment.original_sha256) is None
            or not isinstance(assessment.acl, (set, frozenset))
            or assessment.assessed_by != actor
            or assessment.revoked is not False
            or type(assessment.pii_cleared) is not bool
            or not isinstance(assessment.assessed_at, datetime)
            or not isinstance(assessment.expires_at, datetime)
            or assessment.assessed_at.utcoffset() is None
            or assessment.expires_at.utcoffset() is None
            or assessment.expires_at <= assessment.assessed_at
            or not assessment.acl
        ):
            raise ImageDescriptionDenied("clearance_invalid")
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """INSERT INTO archiver_image_description_clearance
                (id,workspace,channel_id,file_id,original_sha256,provider,model,
                 policy_revision,visibility,acl,pii_cleared,assessed_by,assessed_at,
                 expires_at,reason)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    clearance_id,
                    assessment.workspace,
                    assessment.channel_id,
                    assessment.file_id,
                    assessment.original_sha256,
                    assessment.provider,
                    assessment.model,
                    assessment.policy_revision,
                    assessment.visibility,
                    sorted(assessment.acl),
                    assessment.pii_cleared,
                    actor,
                    assessment.assessed_at,
                    assessment.expires_at,
                    reason.strip(),
                ),
            )

    def active_clearance(
        self,
        *,
        workspace: str,
        channel_id: str,
        file_id: str,
        original_sha256: str,
        provider: str,
        model: str,
        policy_revision: str,
    ) -> SafetyAssessment | None:
        """정확한 원본·외부 경로·정책에 묶인 현재 판정만 읽는다."""
        values = (
            workspace,
            channel_id,
            file_id,
            original_sha256,
            provider,
            model,
            policy_revision,
        )
        if any(not isinstance(value, str) or not value for value in values):
            raise ImageDescriptionDenied("clearance_scope_invalid")
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """SELECT * FROM archiver_image_description_clearance
                WHERE workspace=%s AND channel_id=%s AND file_id=%s
                  AND original_sha256=%s AND provider=%s AND model=%s
                  AND policy_revision=%s AND revoked_at IS NULL
                  AND expires_at > clock_timestamp()
                ORDER BY assessed_at DESC,created_at DESC LIMIT 1""",
                values,
            )
            row = cur.fetchone()
            if not row:
                return None
            return SafetyAssessment(
                workspace=row["workspace"],
                channel_id=row["channel_id"],
                file_id=row["file_id"],
                original_sha256=row["original_sha256"],
                provider=row["provider"],
                model=row["model"],
                policy_revision=row["policy_revision"],
                visibility=row["visibility"],
                acl=frozenset(row["acl"]),
                assessed_by=row["assessed_by"],
                assessed_at=row["assessed_at"],
                expires_at=row["expires_at"],
                pii_cleared=row["pii_cleared"],
                revoked=False,
            )

    def revoke_clearance(
        self,
        clearance_id: str,
        *,
        actor: str,
        reason: str,
    ) -> None:
        if (
            not isinstance(clearance_id, str)
            or not clearance_id.strip()
            or clearance_id != clearance_id.strip()
            or not isinstance(actor, str)
            or not actor.strip()
            or actor != actor.strip()
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise ImageDescriptionDenied("clearance_revoke_invalid")
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """UPDATE archiver_image_description_clearance
                SET revoked_at=now(),revoked_by=%s,revoke_reason=%s
                WHERE id=%s AND revoked_at IS NULL RETURNING id""",
                (actor, reason.strip(), clearance_id),
            )
            if not cur.fetchone():
                raise ImageDescriptionDenied("clearance_not_active")

    @staticmethod
    def _approval(cur, approval_id: str) -> ImageDescriptionApproval:
        cur.execute(
            "SELECT * FROM archiver_image_description_approval WHERE id=%s",
            (approval_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ImageDescriptionDenied("approval_missing")
        return ImageDescriptionApproval(
            approval_id=approval_id,
            expires_at=row["expires_at"],
            revoked=row["revoked_at"] is not None,
            **row["scope"],
        )

    @staticmethod
    def _receipt(job) -> ImageDescriptionDispatch:
        return ImageDescriptionDispatch(
            dispatch_id=job["id"],
            approval_id=job["approval_id"],
            max_output_tokens=job["max_output_tokens"],
            expires_at=job["expires_at"],
            active=job["state"] == "reserved",
            **job["scope"],
        )

    @staticmethod
    def _checks(checks: dict) -> dict:
        return {
            key: checks.get(key, key in ("pii_blocked", "bot_generated"))
            for key in ("source_verified", "access_verified", "pii_blocked", "bot_generated")
        }

    def reserve(
        self,
        image: ImageInput,
        *,
        approval_id: str,
        prompt_revision: str,
        expires_at: datetime,
        checks: dict,
    ) -> ImageDescriptionDispatch:
        with self._transaction(), self.connection.cursor() as cur:
            global_row = self._lock(cur)
            cur.execute("SELECT clock_timestamp() AS now")
            now = cur.fetchone()["now"]
            policy, config, _ = self._effective(cur, global_row, image.workspace)
            catalog = self._catalog(cur, policy)
            approval = self._approval(cur, approval_id)
            verdict = preflight_image_description(
                image,
                policy=policy,
                catalog=catalog,
                approval=approval,
                dispatch=None,
                prompt_revision=prompt_revision,
                now=now,
                **self._checks(checks),
            )
            if verdict.code != "dispatch_unavailable":
                raise ImageDescriptionDenied(verdict.code)
            if expires_at.utcoffset() is None or not now < expires_at <= approval.expires_at:
                raise ImageDescriptionDenied("dispatch_expiry_invalid")

            scope = approval_scope(approval) | {
                "image_sha256": hashlib.sha256(image.image).hexdigest()
            }
            identity = dispatch_identity(scope)
            cur.execute(
                "SELECT id FROM archiver_image_description_job WHERE identity_sha256=%s",
                (identity,),
            )
            if cur.fetchone():
                raise ImageDescriptionDenied("duplicate_job")
            caps = (global_row["config"].get("max_concurrency"), config.get("max_concurrency"))
            if any(type(cap) is not int or cap <= 0 for cap in caps):
                raise ImageDescriptionDenied("concurrency_unconfigured")
            cur.execute(
                """SELECT workspace,count(*) AS active FROM archiver_image_description_job
                WHERE state IN ('reserved','dispatching','unknown') GROUP BY workspace"""
            )
            active = {row["workspace"]: row["active"] for row in cur.fetchall()}
            if sum(active.values()) >= caps[0] or active.get(image.workspace, 0) >= caps[1]:
                raise ImageDescriptionDenied("concurrency_exceeded")

            limits = (global_row["config"].get("daily_calls"), config.get("daily_calls"))
            if any(type(limit) is not int or limit <= 0 for limit in limits):
                raise ImageDescriptionDenied("daily_calls_unconfigured")
            cur.execute(
                """SELECT workspace,count(*) AS calls FROM archiver_image_description_job
                WHERE (created_at AT TIME ZONE 'Asia/Seoul')::date =
                      (clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date
                GROUP BY workspace"""
            )
            calls = {row["workspace"]: row["calls"] for row in cur.fetchall()}
            if sum(calls.values()) >= limits[0] or calls.get(image.workspace, 0) >= limits[1]:
                raise ImageDescriptionDenied("daily_calls_exceeded")
            if catalog.quota_remaining is not None:
                cur.execute(
                    """SELECT count(*) AS used FROM archiver_image_description_job
                    WHERE (catalog_snapshot->>'provider')=%s
                      AND (catalog_snapshot->>'model')=%s AND created_at >= %s""",
                    (catalog.provider, catalog.model, catalog.verified_at),
                )
                if cur.fetchone()["used"] >= catalog.quota_remaining:
                    raise ImageDescriptionDenied("quota_exhausted")

            job_id = uuid4().hex
            cur.execute(
                """INSERT INTO archiver_image_description_job
                (id,identity_sha256,approval_id,workspace,policy_revision,prompt_revision,
                 scope,policy_snapshot,catalog_snapshot,max_output_tokens,state,expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,'reserved',%s)
                RETURNING *""",
                (
                    job_id,
                    identity,
                    approval_id,
                    image.workspace,
                    policy.revision,
                    prompt_revision,
                    _json(scope),
                    _json({"effective": config, "global": global_row["config"]}),
                    _json(_catalog_snapshot(catalog)),
                    policy.max_output_tokens,
                    expires_at,
                ),
            )
            return self._receipt(cur.fetchone())

    def begin_dispatch(
        self,
        job_id: str,
        image: ImageInput,
        *,
        checks: dict,
    ) -> ImageDescriptionGrant:
        with self._transaction(), self.connection.cursor() as cur:
            global_row = self._lock(cur)
            cur.execute(
                """SELECT *,clock_timestamp() AS now FROM archiver_image_description_job
                WHERE id=%s FOR UPDATE""",
                (job_id,),
            )
            job = cur.fetchone()
            if not job or job["state"] != "reserved":
                raise ImageDescriptionDenied("job_not_reserved")
            policy, _, _ = self._effective(cur, global_row, job["workspace"])
            catalog = self._catalog(cur, policy)
            receipt = self._receipt(job)
            verdict = preflight_image_description(
                image,
                policy=policy,
                catalog=catalog,
                approval=self._approval(cur, job["approval_id"]),
                dispatch=receipt,
                prompt_revision=job["prompt_revision"],
                now=job["now"],
                **self._checks(checks),
            )
            if not verdict.ready:
                raise ImageDescriptionDenied(verdict.code)
            cur.execute(
                """UPDATE archiver_image_description_job
                SET state='dispatching',dispatched_at=now() WHERE id=%s""",
                (job_id,),
            )
            return ImageDescriptionGrant(
                dispatch=receipt,
                model=policy.model,
                provider=policy.provider,
                max_output_tokens=policy.max_output_tokens,
                timeout_seconds=float(policy.timeout_seconds),
            )

    def finish_and_publish(self, job_id: str, result: ImageDescription) -> str:
        """안전 게이트 뒤 파생 revision만 저장한다. 모델 원문 응답은 저장하지 않는다."""
        decision = guard_index_write(result)
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                "SELECT * FROM archiver_image_description_job WHERE id=%s FOR UPDATE",
                (job_id,),
            )
            job = cur.fetchone()
            if not job or job["state"] != "dispatching":
                raise ImageDescriptionDenied("job_not_dispatched")
            scope = job["scope"]
            if (
                result.workspace,
                result.channel,
                result.file_id,
                result.original_sha256,
                result.prompt_revision,
            ) != (
                scope["workspace"],
                scope["channel"],
                scope["file_id"],
                scope["original_sha256"],
                job["prompt_revision"],
            ):
                raise ImageDescriptionDenied("result_scope_mismatch")

            usage = result.usage
            if bool(usage.model) != bool(usage.provider):
                raise ImageDescriptionDenied("result_identity_invalid")
            expected_identity = (
                job["catalog_snapshot"]["model"],
                job["catalog_snapshot"]["provider"],
            )
            if decision.storable and (usage.model, usage.provider) != expected_identity:
                raise ImageDescriptionDenied("result_identity_invalid")
            for value in (usage.input_tokens, usage.output_tokens):
                if value is not None and (type(value) is not int or value < 0):
                    raise ImageDescriptionDenied("invalid_usage")
            actual_usd = _money(usage.cost_usd)
            cost_code = (
                "cost_unverified"
                if actual_usd is None
                else "unexpected_charge"
                if actual_usd > 0
                else ""
            )
            publish = decision.storable and not cost_code
            state = (
                decision.index_status
                if publish
                else "failed" if result.status == "failed"
                else "blocked"
            )
            codes = (
                (cost_code,)
                if cost_code
                else decision.codes
                if decision.storable
                else decision.codes or ((result.error_code or "unsafe_output"),)
            )
            revision_id = ""
            if publish:
                revision_id = uuid4().hex
                cur.execute(
                    """INSERT INTO archiver_image_description_revision
                    (id,identity_sha256,job_id,workspace,channel_id,file_id,original_sha256,
                     image_sha256,prompt_revision,model,provider,status,guard_verdict,
                     guard_codes,description,tags,uncertainty)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        revision_id,
                        job["identity_sha256"],
                        job_id,
                        result.workspace,
                        result.channel,
                        result.file_id,
                        result.original_sha256,
                        scope["image_sha256"],
                        result.prompt_revision,
                        job["catalog_snapshot"]["model"],
                        job["catalog_snapshot"]["provider"],
                        decision.index_status,
                        decision.verdict,
                        list(decision.codes),
                        decision.description,
                        list(decision.tags),
                        list(decision.uncertainty),
                    ),
                )
            if cost_code:
                # 한 건만 막고 다음 사진을 또 보내면 「무료라서 켠 기능」이 계속 과금된다.
                # 사람이 카탈로그를 다시 검증하기 전까지 같은 모델을 fail-closed 한다.
                cur.execute(
                    """UPDATE archiver_image_model_catalog
                    SET zero_cost_verified=false,quota_state='unknown',quota_remaining=NULL,
                        updated_by='runtime-cost-guard',updated_at=now()
                    WHERE provider=%s AND model=%s""",
                    (expected_identity[1], expected_identity[0]),
                )
            cur.execute(
                """UPDATE archiver_image_description_job SET state=%s,error_code=%s,
                actual_model=%s,actual_provider=%s,input_tokens=%s,output_tokens=%s,
                actual_usd=%s,finished_at=now() WHERE id=%s""",
                (
                    state,
                    ",".join(codes),
                    usage.model or None,
                    usage.provider or None,
                    usage.input_tokens,
                    usage.output_tokens,
                    actual_usd,
                    job_id,
                ),
            )
            return revision_id

    def mark_unknown(self, job_id: str, *, code: str = "result_unknown") -> None:
        if not re.fullmatch(r"[a-z0-9_]+", code or ""):
            raise ImageDescriptionDenied("invalid_error_code")
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """UPDATE archiver_image_description_job
                SET state='unknown',error_code=%s,finished_at=now()
                WHERE id=%s AND state='dispatching' RETURNING id""",
                (code, job_id),
            )
            if not cur.fetchone():
                raise ImageDescriptionDenied("job_not_dispatched")

    def finish_without_call(self, job_id: str, *, code: str) -> None:
        """dispatch commit 뒤에도 외부 호출 전에 닫힌 경우. 전송 0회가 확정이다."""
        if not re.fullmatch(r"[a-z0-9_]+", code or ""):
            raise ImageDescriptionDenied("invalid_error_code")
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """UPDATE archiver_image_description_job
                SET state='blocked',error_code=%s,finished_at=now()
                WHERE id=%s AND state='dispatching' RETURNING id""",
                (code, job_id),
            )
            if not cur.fetchone():
                raise ImageDescriptionDenied("job_not_dispatched")

    def cancel(self, job_id: str) -> None:
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """UPDATE archiver_image_description_job
                SET state='cancelled',finished_at=now()
                WHERE id=%s AND state='reserved' RETURNING id""",
                (job_id,),
            )
            if not cur.fetchone():
                raise ImageDescriptionDenied("job_not_reserved")

    def reconcile_stale_dispatches(self, *, older_than_seconds: int) -> tuple[str, ...]:
        """오래된 dispatching을 `unknown`으로 닫는다. **재시도 가능 상태가 아니다.**"""
        if type(older_than_seconds) is not int or older_than_seconds <= 0:
            raise ImageDescriptionDenied("invalid_reconcile_age")
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute(
                """UPDATE archiver_image_description_job
                SET state='unknown',error_code='dispatch_stale',finished_at=now()
                WHERE state='dispatching' AND dispatched_at <
                      clock_timestamp() - make_interval(secs => %s)
                RETURNING id""",
                (older_than_seconds,),
            )
            return tuple(str(row["id"]) for row in cur.fetchall())
