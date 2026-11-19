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

from tybot.gateway.base import Sensitivity

from .vision_policy import PageApproval, PageReservation, VisionPolicy, preflight
from .vision_sensitivity import GENERAL_BUSINESS, request_sensitivity

CONFIG_KEYS = frozenset({
    'model', 'provider', 'max_image_bytes', 'max_output_tokens', 'timeout_seconds',
    'daily_usd', 'job_usd', 'max_concurrency', 'sensitivity',
    'input_usd_per_mtok', 'output_usd_per_mtok',
})

#: 운영자가 확인해 적은 모델 요금표(USD / 1M tokens). **정산은 이 값으로만 한다.**
#:
#: gateway 응답의 `cost_usd` 는 쓰지 않는다. 그 값은 Router 에 넣은 ModelSpec 단가로
#: 계산되는데, Vision 경로는 단가 0 으로 Router 를 만들고(Google provider 는 늘 0.0)
#: 그러면 실제 청구와 무관하게 0 원으로 정산되어 일일 예산이 소진되지 않는다.
TARIFF_KEYS = ('input_usd_per_mtok', 'output_usd_per_mtok')


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
    #: Router·provider 경계에 넘길 계약 값(`vision_sensitivity.request_sensitivity`).
    sensitivity: Sensitivity = GENERAL_BUSINESS
    #: 예약 시점에 묶인 요금표. 없으면(옛 예약) 정산은 「모름」 이다 — 0 이 아니다.
    input_usd_per_mtok: Decimal | None = None
    output_usd_per_mtok: Decimal | None = None

    def charge_usd(self, input_tokens, output_tokens) -> Decimal | None:
        """토큰 × 요금표. 토큰이나 요금표 하나라도 모르면 None(모름)."""
        if self.input_usd_per_mtok is None or self.output_usd_per_mtok is None:
            return None
        if not all(type(t) is int and t >= 0 for t in (input_tokens, output_tokens)):
            return None
        if input_tokens == 0 or output_tokens == 0:
            # 쪽 이미지를 보낸 호출은 입력이 0 일 수 없고, 응답이 있으면 출력도 0 이 아니다.
            # provider 는 usage 가 빠지면 0 으로 채운다 — 그 0 을 「공짜」 로 읽지 않는다.
            return None
        return (Decimal(input_tokens) * self.input_usd_per_mtok
                + Decimal(output_tokens) * self.output_usd_per_mtok) / Decimal(1_000_000)


def tariff(config: dict) -> tuple[Decimal, Decimal] | None:
    """설정의 요금표. 둘 다 있고 유효할 때만. 모르면 None — 0 으로 메우지 않는다."""
    if not all(key in config for key in TARIFF_KEYS):
        return None
    try:
        return tuple(money(config[key]) for key in TARIFF_KEYS)  # type: ignore[return-value]
    except VisionDenied:
        return None


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
        try:
            # 일반 업무가 기본(오너 결정 2026-11-19). 틀린 값은 짐작하지 않고 정책 무효다.
            sensitivity = Sensitivity(config.get('sensitivity', GENERAL_BUSINESS.value))
        except ValueError:
            raise VisionDenied('policy_invalid') from None
        policy = VisionPolicy(
            sensitivity=sensitivity,
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
            rates = tariff(config)
            if rates is None:
                # 요금표 없이 보내면 정산할 수 없다. 모르는 비용을 0 으로 적지 않는다.
                raise VisionDenied('tariff_unconfigured')
            if amount < Decimal(policy.max_output_tokens or 0) * rates[1] / Decimal(1_000_000):
                # 출력 상한만으로도 넘는 예약은 보수적인 상한이 아니다.
                raise VisionDenied('reservation_below_tariff')
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
        source = {k: checks.get(k, k == 'pii_blocked') for k in (
            'source_verified', 'rendering_verified', 'access_verified', 'pii_blocked'
        )}
        # 서버 카탈로그가 확인한 모델 민감도 상한. 없으면 preflight 가 막는다.
        source['catalog_max_sensitivity'] = checks.get('catalog_max_sensitivity')
        return source

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
            # 정산 요금표는 **예약 때 묶인 값**이다. 그 사이 정책이 바뀌어도 이 job 은 그대로.
            snapshot = (job.get('policy_snapshot') or {}).get('effective') or {}
            rates = tariff(snapshot)
            return VisionDispatch(
                reservation=receipt,
                model=policy.model,
                provider=policy.provider,
                max_output_tokens=policy.max_output_tokens,
                timeout_seconds=float(policy.timeout_seconds),
                sensitivity=request_sensitivity(policy.sensitivity),
                input_usd_per_mtok=rates[0] if rates else None,
                output_usd_per_mtok=rates[1] if rates else None,
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

    def record_page(self, job_id: str, result, *, channel_id: str) -> str:
        """한 job 의 전사 결과를 **파생 revision** 으로 남긴다(원본·원문 MD 는 그대로).

        - 정산된(`settled`)·결과 미확정(`unknown`) job 만 받는다 — 보내지 않은 job 의 결과는 없다
        - 결과 좌표가 job 좌표와 다르면 거절한다(다른 쪽 결과가 이 쪽에 붙지 않게)
        - 본문은 `sensitive_mask` 로 가린 뒤 `pii_screen` 최종 관문을 지난 것만. 관문에 걸리면
          본문 없이 `withheld` 로 남긴다(원칙 5)
        - job 하나에 결과 하나(`job_id` UNIQUE). 같은 페이지를 다시 적지 않는다
        - `channel_id` 는 호출부가 **방금 수집 범위를 조회해 맞춘** 채널이다(`vision_pdf_batch.access_code`)
        - 같은 트랜잭션에서 검색 투영(`pdf_page_search_entry`)에도 들어간다. 실패·보류는
          본문 없는 묘비다 — 옛 성공 전사가 최신 판인 척하지 않게
        """
        from ..pii_screen import screen_line
        from ..sensitive_mask import mask_sensitive

        if not isinstance(channel_id, str) or not channel_id.strip():
            raise VisionDenied('channel_required')
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute('SELECT * FROM archiver_vision_job WHERE id=%s FOR UPDATE', (job_id,))
            job = cur.fetchone()
            if not job or job['state'] not in ('settled', 'unknown'):
                raise VisionDenied('job_not_finished')
            scope = job['scope']
            coordinates = (
                result.workspace, result.file_id, result.original_sha256, result.page_number,
                result.page_basis, result.rendered_sha256 or result.original_sha256,
                result.renderer_version,
            )
            if coordinates != (
                scope['workspace'], scope['file_id'], scope['original_sha256'],
                scope['page_number'], scope['page_basis'], scope['rendered_sha256'],
                scope['renderer_version'],
            ):
                raise VisionDenied('result_scope_mismatch')
            status, body, codes, masked = result.status, '', [], 0
            if result.ok and result.markdown.strip():
                masked_result = mask_sensitive(result.markdown)
                masked = sum(masked_result.counts.values())
                lines = masked_result.text.splitlines()
                if any(screen_line(line) for line in lines):
                    status, codes = 'withheld', ['pii_screen_blocked']
                else:
                    body = masked_result.text.strip()
            elif result.ok:
                status, codes = 'failed', ['empty_output']
            else:
                status = 'failed'
            if result.error_code:
                codes.append(result.error_code)
            revision_id = uuid4().hex
            cur.execute(
                """INSERT INTO archiver_vision_page_revision
                (id,job_id,workspace,channel_id,file_id,original_sha256,page_number,page_basis,
                 rendered_sha256,renderer_version,image_sha256,model,provider,status,body,
                 unreadable,codes,masked_count)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (revision_id, job_id, scope['workspace'], channel_id.strip(), scope['file_id'],
                 scope['original_sha256'], scope['page_number'], scope['page_basis'],
                 scope['rendered_sha256'], scope['renderer_version'], scope['image_sha256'],
                 scope['model'], scope['provider'], status, body,
                 list(result.unreadable), codes, masked),
            )
            self._project(cur, revision_id=revision_id)
            return revision_id

    #: 전사 revision → 검색 투영. 이미 있으면 건너뛴다(revision_id UNIQUE) — 소급을 몇 번
    #: 돌려도 같다.
    #:
    #: - 실패·보류(failed·withheld)도 **본문 없는 묘비**로 들어간다. 그래야 Master 가 그 쪽의
    #:   최신 판이 실패·보류임을 알고 옛 extracted 를 쓰지 않는다. 묘비의 body·unreadable 은
    #:   여기서 비운다 — 원장 제약과 별개로 민감 본문이 Master 로 넘어갈 길을 두지 않는다
    #: - created_at 은 **원장 revision 시각**을 복사한다. 소급 투영 시각이 들어가면 나중에
    #:   투영된 옛 판이 새 판보다 새것으로 보인다
    _PROJECT = '''INSERT INTO pdf_page_search_entry
        (id, revision_id, workspace, channel_id, file_id, page_number, original_sha256,
         renderer_version, provider, model, status, body, unreadable, created_at)
        SELECT md5(r.id || ':search'), r.id, r.workspace, r.channel_id, r.file_id,
               r.page_number, r.original_sha256, r.renderer_version, r.provider, r.model,
               r.status,
               CASE WHEN r.status IN ('extracted', 'uncertain') THEN r.body ELSE '' END,
               CASE WHEN r.status IN ('extracted', 'uncertain') THEN r.unreadable
                    ELSE '{}'::text[] END,
               r.created_at
          FROM archiver_vision_page_revision r
         WHERE (r.status IN ('failed', 'withheld')
                OR (r.status IN ('extracted', 'uncertain') AND r.body <> ''))
           AND (%(revision_id)s::text IS NULL OR r.id = %(revision_id)s::text)
        ON CONFLICT (revision_id) DO NOTHING'''

    @classmethod
    def _project(cls, cur, *, revision_id: str | None) -> int:
        cur.execute(cls._PROJECT, {'revision_id': revision_id})
        return cur.rowcount or 0

    def project_pages(self) -> int:
        """이미 저장된 전사 revision(묘비 포함)을 검색 투영에 소급한다. 멱등 — 새 줄 수."""
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            return self._project(cur, revision_id=None)

    def cancel(self, job_id: str):
        with self._transaction(), self.connection.cursor() as cur:
            self._lock(cur)
            cur.execute('''UPDATE archiver_vision_job SET state='cancelled', finished_at=now()
                WHERE id=%s AND state='reserved' RETURNING id''', (job_id,))
            if not cur.fetchone():
                raise VisionDenied('job_not_reserved')
