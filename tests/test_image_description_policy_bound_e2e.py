"""B-81 — 2026-11-20 오너 결정: 이미지는 건별 PII clearance·approval 없이 보낸다.

정본 이미지 → 선별 → 원본 재검증 → **정책 결속 예약** → 모델 1회 → 출력 가드 →
파생 revision·검색 투영 → B-94 스레드 알림까지 `run_once` 를 그대로 돌린다.

DB 는 `ImageDescriptionStore` 가 내는 SQL 을 **문장 단위로** 받아 메모리 표에 쓰는
대역이다. 저장소 코드는 실제 것이 돈다 — 대역은 행을 돌려줄 뿐 판정하지 않는다.
CHECK·외래키는 `test_image_description_store_db.py`(실제 PostgreSQL)가 잰다.

거는 것:
- 정책 ON 정상 이미지: 승인·clearance 행이 하나도 없어도 **정확히 1회** 호출, 승인 행을
  만들지 않고(가짜 `pii_cleared` 포함), 작업의 `approval_id` 는 NULL 이다
- 정책 OFF · ACL 모름 · 원본 해시 변경 · 봇 생성 이미지 · quota 소진: 외부 호출 0회
- 같은 원본을 다시 돌려도 두 번 보내지 않는다
- 반환 텍스트는 마스킹·출력 가드를 지난다. 차량번호 전사는 저장되지 않는다
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_image_transcript_e2e as e2e

from tybot.archive import image_description_job as job
from tybot.archive import image_description_notification as notification

NOW = datetime.now(UTC)


class MemoryDB:
    """`ImageDescriptionStore` 의 SQL 을 받아 메모리 표에 쓰는 대역."""

    autocommit = True
    info = SimpleNamespace(transaction_status=0)

    def __init__(self, *, enabled=True, quota_remaining=10, quota_state="available"):
        self.global_policy = {
            "workspace": "",
            "revision": "g1",
            "enabled": enabled,
            "config": {"max_concurrency": 2, "daily_calls": 50},
        }
        self.local_policy = {
            "workspace": e2e.WS,
            "revision": "w1",
            "enabled": enabled,
            "config": {
                "model": e2e.MODEL,
                "provider": e2e.PROVIDER,
                "region": "global",
                "sensitivity": "internal",
                "catalog_verification_ttl_seconds": 86400,
                "max_image_bytes": 1024 * 1024,
                "max_output_tokens": 1024,
                "timeout_seconds": 20,
                "max_concurrency": 1,
                "daily_calls": 20,
            },
        }
        self.catalog = {
            "model": e2e.MODEL,
            "provider": e2e.PROVIDER,
            "supports_vision": True,
            "zero_cost_verified": True,
            "verified_at": NOW - timedelta(minutes=5),
            "verification_revision": "cat-1",
            "max_sensitivity": "internal",
            "allowed_regions": ["global"],
            "credential_ready": True,
            "quota_state": quota_state,
            "quota_remaining": quota_remaining,
            "quota_resets_at": None,
        }
        self.jobs: dict[str, dict] = {}
        self.descriptions: dict[str, dict] = {}
        self.transcripts: dict[str, dict] = {}
        self.entries: list[dict] = []
        self.approvals_written = 0
        self.clearances_written = 0
        self.sql: list[str] = []
        self._result = None

    # --- psycopg 모양 ---------------------------------------------------------

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    @contextmanager
    def transaction(self):
        yield

    @contextmanager
    def cursor(self):
        yield self

    def fetchone(self):
        result, self._result = self._result, None
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def fetchall(self):
        result, self._result = self._result, None
        return result or []

    # --- SQL --------------------------------------------------------------------

    def execute(self, sql, args=None):
        text = " ".join(sql.split())
        self.sql.append(text)
        args = tuple(args or ())
        self._result = None
        if text.startswith("SET TRANSACTION"):
            return
        if "archiver_image_description_approval" in text and text.startswith("INSERT"):
            self.approvals_written += 1
            return
        if "archiver_image_description_clearance" in text and text.startswith("INSERT"):
            self.clearances_written += 1
            return
        if text.startswith("SELECT * FROM archiver_image_description_policy WHERE workspace=''"):
            self._result = dict(self.global_policy)
        elif text.startswith("SELECT * FROM archiver_image_description_policy WHERE workspace=%s"):
            self._result = dict(self.local_policy) if args[0] == e2e.WS else None
        elif text.startswith("SELECT * FROM archiver_image_model_catalog"):
            self._result = dict(self.catalog)
        elif text.startswith("SELECT clock_timestamp() AS now"):
            self._result = {"now": datetime.now(UTC)}
        elif text.startswith("UPDATE archiver_image_description_job SET state='unknown'"):
            self._result = []
        elif "FROM archiver_image_description_revision WHERE" in text and "UNION" in text:
            rows = [*self.descriptions.values(), *self.transcripts.values()]
            self._result = [
                {k: r[k] for k in ("workspace", "channel_id", "original_sha256")}
                for r in rows
                if r["workspace"] == args[0]
            ]
        elif text.startswith("SELECT id FROM archiver_image_description_job WHERE identity_sha256"):
            hit = [j for j in self.jobs.values() if j["identity_sha256"] == args[0]]
            self._result = [{"id": hit[0]["id"]}] if hit else None
        elif "count(*) AS active" in text:
            active = [
                j
                for j in self.jobs.values()
                if j["state"] in ("reserved", "dispatching", "unknown")
            ]
            self._result = [{"workspace": e2e.WS, "active": len(active)}] if active else []
        elif "count(*) AS calls" in text:
            self._result = [{"workspace": e2e.WS, "calls": len(self.jobs)}] if self.jobs else []
        elif "count(*) AS used" in text:
            self._result = {"used": len(self.jobs)}
        elif text.startswith("INSERT INTO archiver_image_description_job"):
            (
                job_id,
                identity,
                approval_id,
                workspace,
                policy_revision,
                prompt_revision,
                scope,
                policy_snapshot,
                catalog_snapshot,
                max_tokens,
                expires_at,
            ) = args
            row = {
                "id": job_id,
                "identity_sha256": identity,
                "approval_id": approval_id,
                "workspace": workspace,
                "policy_revision": policy_revision,
                "prompt_revision": prompt_revision,
                "scope": json.loads(scope),
                "policy_snapshot": json.loads(policy_snapshot),
                "catalog_snapshot": json.loads(catalog_snapshot),
                "max_output_tokens": max_tokens,
                "state": "reserved",
                "expires_at": expires_at,
            }
            self.jobs[job_id] = row
            self._result = dict(row)
        elif text.startswith(
            "SELECT *,clock_timestamp() AS now FROM archiver_image_description_job"
        ):
            self._result = dict(self.jobs[args[0]]) | {"now": datetime.now(UTC)}
        elif text.startswith("SELECT * FROM archiver_image_description_job WHERE id=%s"):
            self._result = dict(self.jobs[args[0]])
        elif text.startswith("UPDATE archiver_image_description_job SET state='dispatching'"):
            self.jobs[args[0]]["state"] = "dispatching"
        elif text.startswith("UPDATE archiver_image_description_job SET state=%s"):
            self.jobs[args[-1]]["state"] = args[0]
            self.jobs[args[-1]]["error_code"] = args[1]
        elif text.startswith("UPDATE archiver_image_model_catalog"):
            self.catalog["zero_cost_verified"] = False
        elif text.startswith("INSERT INTO archiver_image_description_revision"):
            keys = (
                "id",
                "identity_sha256",
                "job_id",
                "workspace",
                "channel_id",
                "file_id",
                "original_sha256",
                "image_sha256",
                "prompt_revision",
                "model",
                "provider",
                "status",
                "guard_verdict",
                "guard_codes",
                "description",
                "tags",
                "uncertainty",
            )
            row = dict(zip(keys, args, strict=True))
            self.descriptions[row["id"]] = row
        elif text.startswith("INSERT INTO archiver_image_transcript_revision"):
            keys = (
                "id",
                "identity_sha256",
                "job_id",
                "description_revision_id",
                "workspace",
                "channel_id",
                "file_id",
                "original_sha256",
                "image_sha256",
                "prompt_revision",
                "model",
                "provider",
                "status",
                "body",
                "line_count",
                "masked_count",
                "uncertainty",
                "guard_codes",
            )
            row = dict(zip(keys, args, strict=True))
            self.transcripts[row["id"]] = row
        elif text.startswith("INSERT INTO image_search_entry"):
            keys = (
                "id",
                "kind",
                "revision_id",
                "workspace",
                "channel_id",
                "file_id",
                "original_sha256",
                "provider",
                "model",
                "prompt_revision",
                "status",
                "label",
                "body",
                "uncertainty",
            )
            self.entries.append(dict(zip(keys, args, strict=True)))
        elif text.startswith(
            "SELECT status, description, tags FROM archiver_image_description_revision"
        ):
            self._result = self.descriptions.get(args[0])
        elif text.startswith("SELECT status, body FROM archiver_image_transcript_revision"):
            hit = [t for t in self.transcripts.values() if t["description_revision_id"] == args[0]]
            self._result = hit[0] if hit else None
        else:  # pragma: no cover - 새 SQL 이 생기면 대역이 조용히 넘기지 않는다
            raise AssertionError(f"unexpected SQL: {text[:120]}")


def _run(tmp_path, db, router, *, revisions=None, **archive_kw):
    root = (tmp_path / "archive").resolve()
    if not root.exists():
        e2e._archive(root, **archive_kw)
    return job.run_once(
        e2e.WS,
        dsn="postgresql://unused",
        archive_root=root,
        runtime_root=tmp_path / "state",
        connect=lambda _dsn: db,
        router_factory=lambda *_a, **_k: router,
        secret_resolver=lambda *_a: "test-key",
        **({"revisions": revisions} if revisions else {}),
    )


def _drain(tmp_path):
    seen = []
    notification.drain(e2e.WS, lambda n: seen.append(n) or True, root=tmp_path / "state")
    return seen


# --- 정책 ON · 정상 이미지 ------------------------------------------------------


def test_policy_on_image_is_described_once_without_clearance_or_approval(tmp_path):
    db = MemoryDB()
    router = e2e._Router(e2e._Resp(e2e._model_json()))

    status = _run(tmp_path, db, router)

    assert router.calls == 1
    assert status.published == 1 and status.pending_approval == 0
    # 승인·clearance 를 만들지도, 조회하지도 않는다.
    assert db.approvals_written == 0 and db.clearances_written == 0
    assert not any("approval" in s and s.startswith("SELECT") for s in db.sql)
    assert not any("clearance" in s for s in db.sql)
    (row,) = db.jobs.values()
    assert row["approval_id"] is None
    assert row["state"] == "described"
    assert row["scope"]["policy_revision"] == "g1:w1"

    # 파생 revision · 검색 투영
    (described,) = db.descriptions.values()
    assert described["description"] == e2e.DESCRIPTION
    (transcribed,) = db.transcripts.values()
    assert "010-1234-5678" not in transcribed["body"]
    kinds = sorted(entry["kind"] for entry in db.entries)
    assert kinds == ["description", "transcript"]
    assert all("010-1234-5678" not in entry["body"] for entry in db.entries)

    # B-94 스레드 알림: 원 메시지 좌표와 발행된 인식 결과
    (notice,) = _drain(tmp_path)
    assert notice.channel_id == e2e.CH and notice.message_ts == "1794000000.000100"
    assert notice.description == e2e.DESCRIPTION
    assert "010-1234-5678" not in notice.text
    assert "안전제일" in notice.text


def test_the_same_original_is_never_sent_twice(tmp_path):
    db = MemoryDB()
    router = e2e._Router(e2e._Resp(e2e._model_json()))
    _run(tmp_path, db, router)
    _run(tmp_path, db, router)
    assert router.calls == 1
    assert len(db.jobs) == 1


# --- 외부 호출 0회 ------------------------------------------------------------


def _never_called():
    return e2e._Router(AssertionError("외부 호출이 일어나면 안 된다"))


def test_policy_off_makes_no_external_call(tmp_path):
    db = MemoryDB(enabled=False)
    router = _never_called()
    status = _run(tmp_path, db, router)
    assert status.state == "disabled"
    assert router.calls == 0 and not db.jobs


def test_unknown_acl_makes_no_external_call(tmp_path):
    from dataclasses import replace

    from tybot.archive.attachment_reader import all_revisions

    db = MemoryDB()
    router = _never_called()
    status = _run(
        tmp_path, db, router,
        revisions=lambda root: tuple(replace(d, acl=frozenset()) for d in all_revisions(root)),
    )
    assert router.calls == 0 and not db.jobs
    assert status.published == 0


def test_changed_original_bytes_make_no_external_call(tmp_path):
    db = MemoryDB()
    router = _never_called()
    status = _run(tmp_path, db, router, sha="f" * 64)
    assert router.calls == 0 and not db.jobs
    assert status.published == 0


def test_bot_generated_image_makes_no_external_call(tmp_path):
    """우리 봇 산출물(답변 Canvas 표식)은 수집 입구를 지났어도 후보가 아니다."""
    from dataclasses import replace

    from tybot.archive.attachment_reader import all_revisions
    from tybot.archive.canvas import GENERATED_TITLE

    db = MemoryDB()
    router = _never_called()
    status = _run(
        tmp_path, db, router,
        revisions=lambda root: tuple(
            replace(d, name=f"{GENERATED_TITLE} 사진.png") for d in all_revisions(root)
        ),
    )
    assert router.calls == 0 and not db.jobs
    assert status.published == 0


@pytest.mark.parametrize(
    "catalog",
    [
        {"quota_state": "exhausted", "quota_remaining": 0},
        {"quota_remaining": 0},
    ],
)
def test_exhausted_quota_makes_no_external_call(tmp_path, catalog):
    db = MemoryDB()
    db.catalog.update(catalog)
    router = _never_called()
    _run(tmp_path, db, router)
    assert router.calls == 0
    assert all(j["state"] not in ("dispatching", "described") for j in db.jobs.values())


# --- 반환 텍스트 가드 -----------------------------------------------------------


def test_a_transcribed_vehicle_plate_is_not_stored_or_shown(tmp_path):
    db = MemoryDB()
    body = e2e._model_json(
        transcript={"present": True, "lines": ["출입차량 12가3456"], "uncertainty": []}
    )
    router = e2e._Router(e2e._Resp(body))
    _run(tmp_path, db, router)
    assert router.calls == 1
    assert not db.transcripts
    assert all("12가3456" not in entry["body"] for entry in db.entries)
    (notice,) = _drain(tmp_path)
    assert "12가3456" not in notice.text


def test_a_description_that_identifies_a_person_is_not_indexed(tmp_path):
    db = MemoryDB()
    body = e2e._model_json(description="홍길동 소장이 현장에 서 있다. 010-2222-3333")
    router = e2e._Router(e2e._Resp(body))
    _run(tmp_path, db, router)
    assert router.calls == 1
    assert all("010-2222-3333" not in entry["body"] for entry in db.entries)
    assert all("010-2222-3333" not in str(row["description"]) for row in db.descriptions.values())
