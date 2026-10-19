"""Opt-in real PostgreSQL tests. Never use an operational database.

Uses TYBOT_SCHEMA_TEST_DSN and the existing isolated-DSN refusal guard.
Creates only a generated private schema; no public tables or roles are removed.
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from test_vision_policy import APPROVAL, PAGE

from tybot.archive.vision_store import VisionDenied, VisionStore

ROOT = Path(__file__).resolve().parents[1]

DSN = os.environ.get('TYBOT_SCHEMA_TEST_DSN', '').strip()
pytestmark = pytest.mark.skipif(not DSN, reason='TYBOT_SCHEMA_TEST_DSN absent: real DB locks unverified')
CHECKS = dict(capability_verified=True, credential_ready=True, source_verified=True,
              rendering_verified=True, access_verified=True, pii_blocked=False)
CONFIG = dict(model=APPROVAL.model, provider=APPROVAL.provider, max_image_bytes=1024,
              max_output_tokens=2000, timeout_seconds=30, daily_usd='0.15', job_usd='0.1',
              max_concurrency=4)


@pytest.fixture
def database():
    sys.path.insert(0, str(ROOT / 'scripts'))
    from verify_schema_isolated import operational_db_refusal

    assert not operational_db_refusal(DSN), 'refusing a non-isolated DSN'
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    name = 'vision_test_' + uuid4().hex
    connections = []

    def connect():
        conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
        conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(name)))
        conn.execute("SET statement_timeout='10s'")
        connections.append(conn)
        return conn

    with psycopg.connect(DSN, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
        try:
            conn = connect()
            schema = (ROOT / 'deploy/sql/archiver_vision_schema.sql').read_text(encoding='utf-8')
            conn.execute(schema)
            conn.execute(schema)  # idempotence must preserve controls and records
            store = VisionStore(conn)
            global_rev = store.save_policy('', expected_revision='initial', enabled=True,
                                           config=CONFIG, actor='test')
            local_rev = store.save_policy('tyit', expected_revision=None, enabled=True,
                                          config={}, actor='test')
            yield store, connect, global_rev + ':' + local_rev
        finally:
            for conn in connections:
                conn.close()
            owner.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(name)))


def approve(store, revision, page=PAGE, *, approval_id=None):
    expiry = datetime.now(UTC) + timedelta(minutes=10)
    approval = replace(APPROVAL, approval_id=approval_id or uuid4().hex,
                       page_number=page.page_number, policy_revision=revision, expires_at=expiry)
    store.approve(approval, actor='test')
    return approval


def reserve(store, approval, page=PAGE):
    return store.reserve(page, approval_id=approval.approval_id,
                         amount_usd=Decimal('0.1'), expires_at=approval.expires_at, checks=CHECKS)


def test_concurrent_reservations_cannot_exceed_budget(database):
    store, connect, revision = database
    pages = [PAGE, replace(PAGE, page_number=4)]
    approvals = [approve(store, revision, p) for p in pages]
    stores = [VisionStore(connect()), VisionStore(connect())]
    barrier = Barrier(2)

    def run(i):
        barrier.wait(timeout=5)
        try:
            reserve(stores[i], approvals[i], pages[i])
            return 'reserved'
        except VisionDenied as exc:
            return str(exc)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, range(2)))
    assert sorted(results) == ['daily_budget_exceeded', 'reserved']


def test_concurrent_same_page_and_new_approval_cannot_resend(database):
    store, connect, revision = database
    approvals = [approve(store, revision), approve(store, revision)]
    stores = [VisionStore(connect()), VisionStore(connect())]
    barrier = Barrier(2)

    def run(i):
        barrier.wait(timeout=5)
        try:
            reserve(stores[i], approvals[i])
            return 'reserved'
        except VisionDenied as exc:
            return str(exc)

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(run, range(2))) == ['duplicate_job', 'reserved']


@pytest.mark.parametrize('control', ['off', 'revoke', 'changed_image', 'missing_checks'])
def test_fresh_controls_block_dispatch(database, control):
    store, _, revision = database
    approval = approve(store, revision)
    receipt = reserve(store, approval)
    page, checks = PAGE, CHECKS
    if control == 'off':
        store.save_policy('', expected_revision=revision.split(':')[0], enabled=False,
                          config=CONFIG, actor='test')
    elif control == 'revoke':
        store.revoke(approval.approval_id, actor='test')
    elif control == 'changed_image':
        page = replace(PAGE, image=b'changed-image')
    else:
        checks = {}
    with pytest.raises(VisionDenied):
        store.begin_dispatch(receipt.reservation_id, page, checks=checks)


def test_unknown_cost_stays_held_across_midnight_and_never_retries(database):
    store, _, revision = database
    approval = approve(store, revision)
    receipt = reserve(store, approval)
    store.begin_dispatch(receipt.reservation_id, PAGE, checks=CHECKS)
    store.finish(receipt.reservation_id, actual_usd=None)
    store.connection.execute("UPDATE archiver_vision_job SET created_at=now()-interval '2 days', "
                             "finished_at=now()-interval '2 days'")
    with pytest.raises(VisionDenied, match='job_not_reserved'):
        store.begin_dispatch(receipt.reservation_id, PAGE, checks=CHECKS)
    other = replace(PAGE, page_number=4)
    with pytest.raises(VisionDenied, match='daily_budget_exceeded'):
        reserve(store, approve(store, revision, other), other)


def test_cancel_releases_only_unsent_money_not_duplicate_identity(database):
    store, _, revision = database
    approval = approve(store, revision)
    receipt = reserve(store, approval)
    store.cancel(receipt.reservation_id)
    with pytest.raises(VisionDenied, match='duplicate_job'):
        reserve(store, approval)
    other = replace(PAGE, page_number=4)
    receipt = reserve(store, approve(store, revision, other), other)
    store.begin_dispatch(receipt.reservation_id, other, checks=CHECKS)
    with pytest.raises(VisionDenied, match='job_not_reserved'):
        store.cancel(receipt.reservation_id)


def test_settlement_uses_actual_cost_and_cannot_be_rewritten(database):
    store, _, revision = database
    approval = approve(store, revision)
    receipt = reserve(store, approval)
    store.begin_dispatch(receipt.reservation_id, PAGE, checks=CHECKS)
    store.finish(receipt.reservation_id, actual_usd=Decimal('0.01'))
    with pytest.raises(VisionDenied, match='job_not_dispatched'):
        store.finish(receipt.reservation_id, actual_usd=Decimal('0'))
    other = replace(PAGE, page_number=4)
    reserve(store, approve(store, revision, other), other)


def test_stale_policy_update_is_rejected(database):
    store, _, _ = database
    with pytest.raises(VisionDenied, match='policy_conflict'):
        store.save_policy('tyit', expected_revision='stale', enabled=True, config={}, actor='test')
