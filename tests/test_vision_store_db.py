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
from tybot.gateway.base import Sensitivity

ROOT = Path(__file__).resolve().parents[1]

DSN = os.environ.get('TYBOT_SCHEMA_TEST_DSN', '').strip()
pytestmark = pytest.mark.skipif(not DSN, reason='TYBOT_SCHEMA_TEST_DSN absent: real DB locks unverified')
CHECKS = dict(capability_verified=True, credential_ready=True, source_verified=True,
              rendering_verified=True, access_verified=True, pii_blocked=False,
              catalog_max_sensitivity=Sensitivity.PUBLIC)
CONFIG = dict(model=APPROVAL.model, provider=APPROVAL.provider, max_image_bytes=1024,
              max_output_tokens=2000, timeout_seconds=30, daily_usd='0.15', job_usd='0.1',
              max_concurrency=4, input_usd_per_mtok='1', output_usd_per_mtok='10')


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
        # 확장(pg_trgm·pg_bigm)은 public 에 있다 — 운영과 같이 보이게 public 을 뒤에 둔다.
        conn.execute(sql.SQL('SET search_path TO {}, public').format(sql.Identifier(name)))
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


# --- B-80 운영 연결(2026-11-19): 파생 전사 revision · 재청구 금지 -----------------

def _result(page=PAGE, **over):
    from tybot.archive.vision_extract import STATUS_EXTRACTED, VisionPageResult, VisionUsage

    base = dict(
        status=STATUS_EXTRACTED, workspace=page.workspace, file_id=page.file_id,
        original_sha256=page.original_sha256, page_number=page.page_number,
        page_basis=page.page_basis, rendered_sha256=page.rendered_sha256 or page.original_sha256,
        renderer_version=page.renderer_version, markdown='| 항목 | 금액 |\n|---|---|\n| 기성 | 3.2억 |',
        usage=VisionUsage(model=APPROVAL.model, provider=APPROVAL.provider),
    )
    base.update(over)
    return VisionPageResult(**base)


def _sent(store, revision, page=PAGE, *, actual=None):
    approval = approve(store, revision, page)
    receipt = reserve(store, approval, page)
    store.begin_dispatch(receipt.reservation_id, page, checks=CHECKS)
    store.finish(receipt.reservation_id, actual_usd=actual)
    return approval, receipt


def test_a_timeout_page_is_recorded_as_failed_and_never_reserved_again(database):
    from tybot.archive.vision_extract import ERROR_MODEL_TIMEOUT, STATUS_FAILED

    store, connect, revision = database
    _approval, receipt = _sent(store, revision, actual=None)  # 영수증 없음 → unknown
    store.record_page(receipt.reservation_id,
                      _result(status=STATUS_FAILED, markdown='', error_code=ERROR_MODEL_TIMEOUT),
                      channel_id='C0TEST0001')
    conn = connect()
    job = conn.execute('SELECT state, actual_usd FROM archiver_vision_job WHERE id=%s',
                       (receipt.reservation_id,)).fetchone()
    assert job == {'state': 'unknown', 'actual_usd': None}
    row = conn.execute('SELECT status, body, codes, label FROM archiver_vision_page_revision').fetchone()
    assert row == {'status': 'failed', 'body': '', 'codes': [ERROR_MODEL_TIMEOUT],
                   'label': '[AI 문서 전사·파생]'}
    # 같은 쪽·같은 바이트는 승인을 새로 받아도 다시 예약되지 않는다(재청구 금지).
    with pytest.raises(VisionDenied, match='duplicate_job'):
        reserve(store, approve(store, revision), PAGE)
    assert conn.execute('SELECT count(*) AS n FROM archiver_vision_job').fetchone()['n'] == 1


def test_an_extracted_page_keeps_coordinates_and_masks_before_storing(database):
    store, connect, revision = database
    _, receipt = _sent(store, revision, actual=Decimal('0'))
    store.record_page(receipt.reservation_id,
                      _result(markdown='담당 연락처 010-1234-5678\n| 기성 | 3.2억 |'),
                      channel_id='C0TEST0001')
    row = connect().execute('SELECT * FROM archiver_vision_page_revision').fetchone()
    assert row['status'] == 'extracted' and '1234-5678' not in row['body']
    assert row['masked_count'] >= 1
    assert (row['workspace'], row['file_id'], row['page_number'], row['original_sha256']) == (
        PAGE.workspace, PAGE.file_id, PAGE.page_number, PAGE.original_sha256)
    assert row['renderer_version'] == APPROVAL.renderer_version


def test_one_result_per_job_and_only_after_sending(database):
    store, _connect, revision = database
    approval = approve(store, revision)
    receipt = reserve(store, approval)
    with pytest.raises(VisionDenied, match='job_not_finished'):
        store.record_page(receipt.reservation_id, _result(), channel_id='C0TEST0001')  # 보내지 않은 job
    store.begin_dispatch(receipt.reservation_id, PAGE, checks=CHECKS)
    store.finish(receipt.reservation_id, actual_usd=Decimal('0'))
    store.record_page(receipt.reservation_id, _result(), channel_id='C0TEST0001')
    import psycopg

    with pytest.raises(psycopg.errors.UniqueViolation):
        store.record_page(receipt.reservation_id, _result(), channel_id='C0TEST0001')


def test_a_result_for_another_page_cannot_be_attached(database):
    store, _connect, revision = database
    _, receipt = _sent(store, revision, actual=Decimal('0'))
    with pytest.raises(VisionDenied, match='result_scope_mismatch'):
        store.record_page(receipt.reservation_id, channel_id='C0TEST0001', result=_result(page_number=PAGE.page_number + 1))


def test_a_successful_page_is_projected_for_master_search_with_coordinates(database):
    from tybot.archive.vision_extract import STATUS_FAILED

    store, connect, revision = database
    _, ok = _sent(store, revision, actual=Decimal('0'))
    store.record_page(ok.reservation_id, _result(), channel_id='C0TEST0001')
    other = replace(PAGE, page_number=PAGE.page_number + 1)
    _, bad = _sent(store, revision, other, actual=None)
    store.record_page(bad.reservation_id, _result(other, status=STATUS_FAILED, markdown=''),
                      channel_id='C0TEST0001')
    rows = connect().execute('SELECT * FROM pdf_page_search_entry ORDER BY page_number').fetchall()
    assert len(rows) == 2
    row, tomb = rows
    # 실패 전사는 **본문 없는 묘비**로만 들어간다 — Master 에 본문이 가지 않는다.
    assert (tomb['status'], tomb['body'], tomb['unreadable']) == ('failed', '', [])
    assert (row['workspace'], row['channel_id'], row['file_id'], row['page_number'],
            row['original_sha256']) == (PAGE.workspace, 'C0TEST0001', PAGE.file_id,
                                        PAGE.page_number, PAGE.original_sha256)
    assert row['label'] == '[AI 문서 전사·파생]' and row['status'] == 'extracted'


def test_existing_revisions_can_be_projected_again_idempotently(database):
    store, connect, revision = database
    _, receipt = _sent(store, revision, actual=Decimal('0'))
    store.record_page(receipt.reservation_id, _result(), channel_id='C0TEST0001')
    conn = connect()
    conn.execute('DELETE FROM pdf_page_search_entry')  # 투영이 없던 옛 revision 을 흉내 낸다
    assert store.project_pages() == 1
    assert store.project_pages() == 0  # 다시 돌려도 같다
    assert conn.execute('SELECT count(*) AS n FROM pdf_page_search_entry').fetchone()['n'] == 1


def test_the_master_guide_reads_real_projection_rows_and_follow_up_coordinates(database):
    """B-80 답변 경로의 SQL 을 실제 투영 행으로 돈다(낱말·같은 쪽 최신·revision 좌표)."""
    from tybot import pdf_vision_guide as guide
    from tybot.archive.vision_extract import STATUS_UNCERTAIN

    store, connect, revision = database
    _, first = _sent(store, revision, actual=Decimal('0'))
    rev1 = store.record_page(first.reservation_id, _result(), channel_id='C0TEST0001')
    conn = connect()
    scopes = {(PAGE.workspace, 'C0TEST0001')}

    (entry,) = guide.active_entries(scopes, tokens=['기성'], conn=conn)
    assert (entry.revision_id, entry.file_id, entry.page_number, entry.original_sha256,
            entry.status) == (rev1, PAGE.file_id, PAGE.page_number, PAGE.original_sha256,
                              'extracted')
    assert guide.active_entries({(PAGE.workspace, 'C0OTHER001')}, tokens=['기성'], conn=conn) == []
    assert guide.active_entries(scopes, tokens=['없는낱말'], conn=conn) == []
    assert set(guide.entries_by_revision([rev1], conn=conn)) == {rev1}

    # 같은 쪽의 더 새 revision(uncertain)이 나오면 옛 extracted 는 근거도 좌표도 아니다.
    from hashlib import sha256

    newer = replace(PAGE, image=b'synthetic-page-v2', renderer_version='v2')
    approval = replace(APPROVAL, approval_id=uuid4().hex, policy_revision=revision,
                       image_sha256=sha256(newer.image).hexdigest(), renderer_version='v2',
                       expires_at=datetime.now(UTC) + timedelta(minutes=10))
    store.approve(approval, actor='test')
    second = reserve(store, approval, newer)
    store.begin_dispatch(second.reservation_id, newer, checks=CHECKS)
    store.finish(second.reservation_id, actual_usd=Decimal('0'))
    store.record_page(second.reservation_id,
                      _result(newer, status=STATUS_UNCERTAIN, markdown='판독 불명 기성'),
                      channel_id='C0TEST0001')
    assert guide.active_entries(scopes, tokens=['기성'], conn=conn) == []
    assert guide.entries_by_revision([rev1], conn=conn) == {}


# --- 2026-11-19 보정: 묘비·원장 시각 ---------------------------------------------

CH = 'C0TEST0001'


def _page_revision(store, revision, *, version, status, markdown='', error_code=''):
    """같은 쪽(PAGE)의 다른 렌더 판 하나를 보내고 결과를 적는다. revision id 를 돌려준다."""
    from hashlib import sha256

    page = replace(PAGE, image=f'synthetic-page-{version}'.encode(), renderer_version=version)
    approval = replace(APPROVAL, approval_id=uuid4().hex, policy_revision=revision,
                       image_sha256=sha256(page.image).hexdigest(), renderer_version=version,
                       expires_at=datetime.now(UTC) + timedelta(minutes=10))
    store.approve(approval, actor='test')
    receipt = reserve(store, approval, page)
    store.begin_dispatch(receipt.reservation_id, page, checks=CHECKS)
    store.finish(receipt.reservation_id, actual_usd=Decimal('0'))
    return store.record_page(
        receipt.reservation_id,
        _result(page, status=status, markdown=markdown, error_code=error_code),
        channel_id=CH,
    )


class _NoDocs:
    """resolve_refs 가 DB 단계에서 끝나는지 보는 대역 — 정본은 없다."""

    root = Path('.')

    def visible_channel_docs(self, ctx):
        return []


def _old_ref(rev):
    from tybot.evidence_refs import vision_ref

    return vision_ref(PAGE.workspace, CH, revision_id=rev, file_id=PAGE.file_id,
                      page_number=PAGE.page_number, original_sha256=PAGE.original_sha256)


def _newer(store, revision, status, monkeypatch):
    from tybot.archive.vision_extract import ERROR_MODEL_TIMEOUT, STATUS_EXTRACTED, STATUS_FAILED

    if status == 'failed':
        return _page_revision(store, revision, version='v2', status=STATUS_FAILED,
                              error_code=ERROR_MODEL_TIMEOUT)
    # 보류(withheld)는 마스킹 뒤 최종 관문이 막을 때 생긴다. 그 관문을 표식 줄로 흉내 낸다.
    import tybot.pii_screen as pii_screen

    original = pii_screen.screen_line
    monkeypatch.setattr(pii_screen, 'screen_line',
                        lambda line: 'synthetic' if 'BLOCKME' in line else original(line))
    return _page_revision(store, revision, version='v2', status=STATUS_EXTRACTED,
                          markdown='기성 3.2억 BLOCKME 원본 민감 줄')


@pytest.mark.parametrize('status', ['failed', 'withheld'])
def test_a_newer_failed_or_withheld_revision_blocks_the_old_extracted_everywhere(
    database, monkeypatch, status
):
    from tybot import pdf_vision_guide as guide
    from tybot.access import RequestContext
    from tybot.archive.vision_extract import STATUS_EXTRACTED

    store, connect, revision = database
    rev1 = _page_revision(store, revision, version='v1', status=STATUS_EXTRACTED,
                          markdown='| 기성 | 3.2억 |')
    conn = connect()
    scopes = {(PAGE.workspace, CH)}
    found = guide.active_entries(scopes, tokens=['기성'], conn=conn)
    assert [e.revision_id for e in found] == [rev1]

    rev2 = _newer(store, revision, status, monkeypatch)

    tomb = conn.execute('SELECT * FROM pdf_page_search_entry WHERE revision_id=%s',
                        (rev2,)).fetchone()
    ledger = conn.execute('SELECT status, created_at FROM archiver_vision_page_revision '
                          'WHERE id=%s', (rev2,)).fetchone()
    assert tomb['status'] == ledger['status'] == status
    assert (tomb['body'], tomb['unreadable']) == ('', [])  # 민감 본문은 Master 로 안 간다
    projected = conn.execute('SELECT * FROM pdf_page_search_entry').fetchall()
    assert 'BLOCKME' not in str(projected)
    assert tomb['created_at'] == ledger['created_at']
    # 최초 검색 0건
    assert guide.active_entries(scopes, tokens=['기성'], conn=conn) == []
    # 옛 EvidenceRef 복원 0건 — DB 단계에서 projection_missing 으로 닫힌다
    assert guide.entries_by_revision([rev1], conn=conn) == {}
    ctx = RequestContext(workspace=PAGE.workspace, channels=frozenset({'#x'}))
    hits, dropped = guide.resolve_refs(_NoDocs(), ctx, [_old_ref(rev1)], conn=conn)
    assert hits == [] and dropped == [guide.DROP_PROJECTION]


def test_reprojecting_keeps_the_ledger_revision_order(database):
    """투영을 지우고 소급해도 판 순서는 원장 시각 — 투영 실행 시각이 아니다."""
    from tybot import pdf_vision_guide as guide
    from tybot.archive.vision_extract import (
        ERROR_MODEL_TIMEOUT,
        STATUS_EXTRACTED,
        STATUS_FAILED,
    )

    store, connect, revision = database
    conn = connect()
    scopes = {(PAGE.workspace, CH)}
    # 옛 판 = 실패, 새 판 = 성공. 소급 한 문장 안에서 투영되면 now() 가 같아진다.
    _page_revision(store, revision, version='v1', status=STATUS_FAILED,
                   error_code=ERROR_MODEL_TIMEOUT)
    rev2 = _page_revision(store, revision, version='v2', status=STATUS_EXTRACTED,
                          markdown='| 기성 | 3.2억 |')
    conn.execute('DELETE FROM pdf_page_search_entry')
    assert store.project_pages() == 2
    rows = conn.execute(
        """SELECT e.created_at AS projected, r.created_at AS ledger
             FROM pdf_page_search_entry e JOIN archiver_vision_page_revision r
               ON r.id = e.revision_id"""
    ).fetchall()
    assert rows and all(r['projected'] == r['ledger'] for r in rows)
    found = guide.active_entries(scopes, tokens=['기성'], conn=conn)
    assert [e.revision_id for e in found] == [rev2]
    assert set(guide.entries_by_revision([rev2], conn=conn)) == {rev2}

    # 반대로 새 판이 실패면, 소급 뒤에도 옛 성공은 살아나지 않는다.
    _page_revision(store, revision, version='v3', status=STATUS_FAILED,
                   error_code=ERROR_MODEL_TIMEOUT)
    conn.execute('DELETE FROM pdf_page_search_entry')
    assert store.project_pages() == 3
    assert guide.active_entries(scopes, tokens=['기성'], conn=conn) == []
    assert guide.entries_by_revision([rev2], conn=conn) == {}


def test_schema_reapply_upgrades_an_old_projection_in_place(database):
    """처음 판(성공만·투영 시각) 표를 스키마 재적용이 멱등으로 바로잡는다."""
    import psycopg

    from tybot.archive.vision_extract import (
        ERROR_MODEL_TIMEOUT,
        STATUS_EXTRACTED,
        STATUS_FAILED,
    )

    store, connect, revision = database
    conn = connect()
    ok = _page_revision(store, revision, version='v1', status=STATUS_EXTRACTED,
                        markdown='| 기성 | 3.2억 |')
    bad = _page_revision(store, revision, version='v2', status=STATUS_FAILED,
                         error_code=ERROR_MODEL_TIMEOUT)
    # 처음 판 모양으로 되돌린다: 묘비 없음 · 이름 없는 좁은 제약 · 투영 시각
    table = 'pdf_page_search_entry'
    conn.execute(f'DELETE FROM {table} WHERE revision_id=%s', (bad,))
    conn.execute(f'ALTER TABLE {table} DROP CONSTRAINT {table}_status_known')
    conn.execute(f'ALTER TABLE {table} DROP CONSTRAINT {table}_body_by_status')
    conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {table}_status_check "
                 "CHECK (status IN ('extracted', 'uncertain'))")
    conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {table}_body_check "
                 "CHECK (body <> '' AND char_length(body) <= 20000)")
    conn.execute(f"UPDATE {table} SET created_at = now() + interval '1 hour'")

    schema = (ROOT / 'deploy/sql/archiver_vision_schema.sql').read_text(encoding='utf-8')
    conn.execute(schema)
    conn.execute(schema)

    rows = {
        r['revision_id']: r for r in conn.execute(
            """SELECT e.revision_id, e.status, e.body, e.created_at = r.created_at AS same_time
                 FROM pdf_page_search_entry e JOIN archiver_vision_page_revision r
                   ON r.id = e.revision_id""").fetchall()
    }
    assert set(rows) == {ok, bad}
    assert rows[bad]['status'] == 'failed' and rows[bad]['body'] == ''
    assert all(r['same_time'] for r in rows.values())
    names = {r['conname'] for r in conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'pdf_page_search_entry'::regclass"
    ).fetchall()}
    assert {f'{table}_status_known', f'{table}_body_by_status'} <= names
    assert not names & {f'{table}_status_check', f'{table}_body_check'}
    # 묘비에 본문을 넣는 쓰기는 DB 가 거절한다.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(f"UPDATE {table} SET body='새는 본문' WHERE revision_id=%s", (bad,))
