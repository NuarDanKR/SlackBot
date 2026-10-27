"""Durable Vision controls. Not wired to a collector, API or model worker.

Use a dedicated transaction-capable psycopg connection with dict_row and
READ COMMITTED isolation. No failure path substitutes env/default activation.
All writers must use this repository: the global row serializes controls,
reservations and settlements. No transaction is held during model I/O.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from .vision_policy import PageApproval, PageReservation, VisionPolicy, preflight

CONFIG_KEYS = frozenset({
    'model', 'provider', 'max_image_bytes', 'max_output_tokens', 'timeout_seconds',
    'daily_usd', 'job_usd', 'max_concurrency',
})


class VisionDenied(RuntimeError):
    """A safe, content-free control code; DB exceptions are not swallowed."""


@dataclass(frozen=True)
class VisionDispatch:
    """Committed permission and immutable settings for exactly one model call."""

    reservation: PageReservation
    model: str
    provider: str
    max_output_tokens: int
    timeout_seconds: float


def money(value) -> Decimal:
    if isinstance(value, bool):
        raise VisionDenied('invalid_amount')
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise VisionDenied('invalid_amount') from exc
    if not amount.is_finite() or amount < 0:
        raise VisionDenied('invalid_amount')
    return amount


def approval_scope(approval: PageApproval) -> dict:
    data = asdict(approval)
    for key in ('approval_id', 'expires_at', 'revoked'):
        data.pop(key)
    return data


def identity(scope: dict) -> str:
    # Approval renewals must not turn the same paid page into a new send.
    data = {k: v for k, v in scope.items() if k != 'policy_revision'}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class VisionStore:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def _transaction(self):
        # A nested savepoint is not a durable dispatch boundary. Refuse callers
        # that could return to model I/O before our transaction is committed.
        if not self.connection.autocommit or self.connection.info.transaction_status != 0:
            raise VisionDenied('connection_not_idle_autocommit')
        with self.connection.transaction():
            yield

    def _lock(self, cur):
        cur.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        cur.execute("SELECT * FROM archiver_vision_policy WHERE workspace = '' FOR UPDATE")
        global_policy = cur.fetchone()
        if not global_policy:
            raise VisionDenied('policy_unavailable')
        return global_policy

    def save_policy(self, workspace: str, *, expected_revision: str | None,
                    enabled: bool, config: dict, actor: str) -> str:
        if type(enabled) is not bool or not actor or set(config) - CONFIG_KEYS:
            raise VisionDenied('policy_invalid')
        # JSON encoding rejects nonfinite floats before touching DB.
        encoded = json.dumps(config, allow_nan=False)
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute('SELECT revision FROM archiver_vision_policy WHERE workspace=%s', (workspace,))
            row = cur.fetchone()
            if (row['revision'] if row else None) != expected_revision:
                raise VisionDenied('policy_conflict')
            revision = uuid4().hex
            cur.execute('''INSERT INTO archiver_vision_policy_audit
                (workspace,revision,enabled,config,updated_by) VALUES (%s,%s,%s,%s::jsonb,%s)''',
                        (workspace, revision, enabled, encoded, actor))
            cur.execute('''INSERT INTO archiver_vision_policy
                (workspace, revision, enabled, config, updated_by) VALUES (%s,%s,%s,%s::jsonb,%s)
                ON CONFLICT (workspace) DO UPDATE SET revision=EXCLUDED.revision,
                enabled=EXCLUDED.enabled, config=EXCLUDED.config,
                updated_by=EXCLUDED.updated_by, updated_at=now()''',
                        (workspace, revision, enabled, encoded, actor))
            return revision

    def _effective(self, cur, global_row, workspace, *, capability_verified=False,
                   credential_ready=False):
        cur.execute('SELECT * FROM archiver_vision_policy WHERE workspace=%s', (workspace,))
        local = cur.fetchone()
        if not local or not workspace:
            raise VisionDenied('policy_unavailable')
        config = global_row['config'] | local['config']
        policy = VisionPolicy(
            workspace=workspace, revision=global_row['revision'] + ':' + local['revision'],
            enabled=global_row['enabled'], workspace_enabled=local['enabled'],
            capability_verified=capability_verified, credential_ready=credential_ready,
            **{k: config[k] for k in (
                'model', 'provider', 'max_image_bytes', 'max_output_tokens', 'timeout_seconds'
            ) if k in config},
        )
        return policy, config

    def effective_policy(self, workspace: str):
        """Settings/provenance only. Capability/credential remain unverified.

        Console authorization and provider catalog validation belong to the
        future service layer, not caller-supplied booleans in a public API.
        """
        with self._transaction(), self.connection.cursor() as cur:
            global_row = self._lock(cur)
            policy, config = self._effective(cur, global_row, workspace)
            cur.execute('SELECT config FROM archiver_vision_policy WHERE workspace=%s', (workspace,))
            local = cur.fetchone()['config']
            sources = {key: 'workspace' if key in local else 'global' for key in config}
            return policy, config, sources

    def approve(self, approval: PageApproval, *, actor: str):
        if not actor or approval.revoked or not approval.approval_id:
            raise VisionDenied('approval_invalid')
        with self._transaction(), self.connection.cursor() as cur:
            global_row = self._lock(cur)
            policy, _ = self._effective(cur, global_row, approval.workspace)
            if (approval.policy_revision, approval.model, approval.provider) != (
                policy.revision, policy.model, policy.provider
            ):
                raise VisionDenied('approval_scope_mismatch')
            cur.execute('''INSERT INTO archiver_vision_approval
                (id, workspace, scope, expires_at, approved_by) VALUES (%s,%s,%s::jsonb,%s,%s)''',
                        (approval.approval_id, approval.workspace,
                         json.dumps(approval_scope(approval)), approval.expires_at, actor))

    def revoke(self, approval_id: str, *, actor: str):
        if not actor:
            raise VisionDenied('actor_required')
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute('''UPDATE archiver_vision_approval SET revoked_at=now(), revoked_by=%s
                WHERE id=%s AND revoked_at IS NULL''', (actor, approval_id))
            # Never release money for a possibly transmitted request.
            cur.execute('''UPDATE archiver_vision_job SET state='cancelled', finished_at=now()
                WHERE approval_id=%s AND state='reserved' ''', (approval_id,))

    def _approval(self, cur, approval_id):
        cur.execute('SELECT * FROM archiver_vision_approval WHERE id=%s', (approval_id,))
        row = cur.fetchone()
        if not row:
            raise VisionDenied('approval_missing')
        return PageApproval(approval_id=approval_id, expires_at=row['expires_at'],
                            revoked=row['revoked_at'] is not None, **row['scope'])

    def _receipt(self, job):
        return PageReservation(
            reservation_id=job['id'], approval_id=job['approval_id'],
            max_output_tokens=job['max_output_tokens'], amount_usd=job['reserved_usd'],
            expires_at=job['expires_at'], active=job['state'] == 'reserved', **job['scope'],
        )

    def reserve(self, page, *, approval_id: str, amount_usd: Decimal,
                expires_at: datetime, checks: dict) -> PageReservation:
        """checks are trusted fresh source/ACL/catalog facts, NEVER API parameters.

        amount_usd must be a conservative bound from verified model tariffs.
        Unknown tariffs cannot be replaced with zero/a caller-selected estimate.
        """
        amount = money(amount_usd)
        if amount <= 0:
            raise VisionDenied('invalid_amount')
        with self._transaction(), self.connection.cursor() as cur:
            global_row = self._lock(cur)
            cur.execute('SELECT clock_timestamp() AS now')
            now = cur.fetchone()['now']
            policy, config = self._effective(cur, global_row, page.workspace,
                                            **self._catalog(checks))
            approval = self._approval(cur, approval_id)
            # Check every gate before recording a financial reservation.
            verdict = preflight(page, policy=policy, approval=approval, reservation=None,
                                now=now, **self._source(checks))
            if verdict.code != 'reservation_unavailable':
                raise VisionDenied(verdict.code)
            if expires_at.utcoffset() is None or not now < expires_at <= approval.expires_at:
                raise VisionDenied('reservation_expiry_invalid')
            if amount > money(config.get('job_usd', 0)):
                raise VisionDenied('job_budget_exceeded')
            scope = approval_scope(approval)
            cur.execute('SELECT id FROM archiver_vision_job WHERE identity_sha256=%s',
                        (identity(scope),))
            if cur.fetchone():
                raise VisionDenied('duplicate_job')
            caps = (global_row['config'].get('max_concurrency'), config.get('max_concurrency'))
            if any(type(cap) is not int or cap <= 0 for cap in caps):
                raise VisionDenied('concurrency_unconfigured')
            cur.execute('''SELECT workspace, count(*) AS active FROM archiver_vision_job
                WHERE state IN ('reserved','dispatching','unknown') GROUP BY workspace''')
            active = {r['workspace']: r['active'] for r in cur.fetchall()}
            if sum(active.values()) >= caps[0] or active.get(page.workspace, 0) >= caps[1]:
                raise VisionDenied('concurrency_exceeded')
            # Unknown/in-flight charges remain held across midnight. Settled
            # charges count on settlement day (Asia/Seoul), not reservation day.
            cur.execute('''SELECT workspace, COALESCE(sum(CASE
                WHEN state IN ('reserved','dispatching','unknown') THEN reserved_usd
                WHEN state='settled' AND (finished_at AT TIME ZONE 'Asia/Seoul')::date
                    = (clock_timestamp() AT TIME ZONE 'Asia/Seoul')::date THEN actual_usd
                ELSE 0 END),0) AS charged FROM archiver_vision_job GROUP BY workspace''')
            charged = {r['workspace']: r['charged'] for r in cur.fetchall()}
            if (sum(charged.values()) + amount > money(global_row['config'].get('daily_usd', 0))
                    or charged.get(page.workspace, Decimal(0)) + amount
                    > money(config.get('daily_usd', 0))):
                raise VisionDenied('daily_budget_exceeded')
            job_id = uuid4().hex
            # Unique identity also covers cancelled/unknown jobs. Manual
            # reconciliation is required; renewal is not permission to resend.
            cur.execute('''INSERT INTO archiver_vision_job
                (id,identity_sha256,approval_id,workspace,policy_revision,scope,policy_snapshot,
                 max_output_tokens,reserved_usd,state,expires_at)
                VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,'reserved',%s) RETURNING *''',
                        (job_id, identity(scope), approval_id, page.workspace, policy.revision,
                         json.dumps(scope), json.dumps(dict(effective=config,
                                                            global_config=global_row['config'])),
                         policy.max_output_tokens, amount, expires_at))
            return self._receipt(cur.fetchone())

    @staticmethod
    def _catalog(checks):
        return {k: checks.get(k, False) for k in ('capability_verified', 'credential_ready')}

    @staticmethod
    def _source(checks):
        return {k: checks.get(k, k == 'pii_blocked') for k in (
            'source_verified', 'rendering_verified', 'access_verified', 'pii_blocked'
        )}

    def begin_dispatch(self, job_id: str, page, *, checks: dict) -> VisionDispatch:
        """Commit before I/O; crash after this point is NOT automatically retried.

        Revocation after this commit cannot recall an already authorized send.
        Future worker must send only immediately after a successful return.
        """
        with self._transaction(), self.connection.cursor() as cur:
            global_row = self._lock(cur)
            cur.execute('SELECT *, clock_timestamp() AS now FROM archiver_vision_job WHERE id=%s',
                        (job_id,))
            job = cur.fetchone()
            if not job or job['state'] != 'reserved':
                raise VisionDenied('job_not_reserved')
            policy, _ = self._effective(cur, global_row, job['workspace'], **self._catalog(checks))
            receipt = self._receipt(job)
            verdict = preflight(page, policy=policy, approval=self._approval(cur, job['approval_id']),
                                reservation=receipt, now=job['now'], **self._source(checks))
            if not verdict.ready:
                raise VisionDenied(verdict.code)
            cur.execute("UPDATE archiver_vision_job SET state='dispatching', dispatched_at=now() WHERE id=%s",
                        (job_id,))
            return VisionDispatch(
                reservation=receipt,
                model=policy.model,
                provider=policy.provider,
                max_output_tokens=policy.max_output_tokens,
                timeout_seconds=float(policy.timeout_seconds),
            )

    def finish(self, job_id: str, *, actual_usd: Decimal | None):
        """None means unknown charge, not zero. No response/body is stored here."""
        actual = None if actual_usd is None else money(actual_usd)
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute('''UPDATE archiver_vision_job SET state=%s, actual_usd=%s, finished_at=now()
                WHERE id=%s AND state IN ('dispatching','unknown') RETURNING id''',
                        ('unknown' if actual is None else 'settled', actual, job_id))
            if not cur.fetchone():
                raise VisionDenied('job_not_dispatched')

    def cancel(self, job_id: str):
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute('''UPDATE archiver_vision_job SET state='cancelled', finished_at=now()
                WHERE id=%s AND state='reserved' RETURNING id''', (job_id,))
            if not cur.fetchone():
                raise VisionDenied('job_not_reserved')
