"""승인 하나를 끝까지 돌리는 러너 (B-81).

이 파일이 지키는 것 다섯.

1. **거절 순서를 건너뛰지 않는다.** 로드가 막히면 예약이 없고, 예약이 막히면
   모델이 안 불린다. 단계마다 그 사실이 남는다.
2. **`pii_blocked=False` 를 우리가 적지 않는다.** 호출자만 말할 수 있고,
   안 말하면 닫힌 기본값이 그대로 간다.
3. **증명한 것만 참으로 적는다.** `source_verified`·`bot_generated` 는 로더가
   해시와 `human_upload_confirmed` 를 확인한 **뒤에만** 이 자리에 온다.
4. **예외 본문이 밖으로 안 나간다.** 저장소 거절은 코드, 그 밖은 형만.
5. **실패가 재전송으로 바뀌지 않는다.** dispatch 뒤 실패는 worker 가 닫는다.

모델·DB·네트워크를 부르지 않는다. store·router 는 대역이고, 원본은 실제 임시
디렉터리에 깐 합성 바이트다.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from tybot.archive import image_description_runner as runner
from tybot.archive.attachment_doc import CONVERTED, AttachmentDoc
from tybot.archive.files import attachment_storage
from tybot.archive.image_description import ImageDescription, ImageUsage
from tybot.archive.image_description_policy import ImageDescriptionDispatch
from tybot.archive.image_description_source import ImageCandidate
from tybot.archive.image_description_store import ImageDescriptionDenied

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-pixels" * 4
WS, CH, FID = "tyit", "C0TEST0001", "F0TEST0001"
SHA = hashlib.sha256(PNG).hexdigest()
APPROVAL = "approval-1"
JOB = "job-1"
LATER = __import__("datetime").datetime(2026, 10, 28, tzinfo=__import__("datetime").UTC)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "archive"
    root.mkdir()
    objects = attachment_storage(root, WS, CH).objects_dir / FID
    objects.mkdir(parents=True, exist_ok=True)
    (objects / "photo.png").write_bytes(PNG)
    return root


def _candidate(**over) -> ImageCandidate:
    base = {
        "workspace": WS, "channel_id": CH, "file_id": FID,
        "original_sha256": SHA, "filetype": "png", "mime": "image/png",
    }
    base.update(over)
    return ImageCandidate(**base)


def _doc(**over) -> AttachmentDoc:
    base = {
        "workspace": WS, "channel_id": CH, "channel": "#팀_전산(ABB100)_공지",
        "file_id": FID, "name": "현장사진", "revision": "r1",
        "visibility": "private", "acl": frozenset({"U1"}),
        "filetype": "png", "sha256": SHA, "conversion_state": CONVERTED,
        "human_upload_confirmed": True,
    }
    base.update(over)
    return AttachmentDoc(**base)


def _result(**over) -> ImageDescription:
    base = {
        "status": "described", "workspace": WS, "channel": CH, "file_id": FID,
        "original_sha256": SHA, "description": "건설현장에 굴착기가 보인다.",
        "tags": ("건설현장",), "usage": ImageUsage(model="m", provider="p"),
    }
    base.update(over)
    return ImageDescription(**base)


class FakeStore:
    """저장소 대역. **무엇을 받았는지 그대로 센다.**"""

    def __init__(self, *, reserve_error=None, publish=("rev-1", "published")):
        self.reserve_error = reserve_error
        self.publish = publish
        self.reserved: list[dict] = []
        self.dispatched: list[str] = []
        self.published: list[str] = []
        self.unknown: list[str] = []

    def reserve(self, image, *, approval_id, prompt_revision, expires_at, checks):
        self.reserved.append({
            "image": image, "approval_id": approval_id,
            "prompt_revision": prompt_revision, "checks": dict(checks),
        })
        if self.reserve_error is not None:
            raise self.reserve_error
        return ImageDescriptionDispatch(
            dispatch_id=JOB, approval_id=approval_id, workspace=WS, channel=CH,
            file_id=FID, original_sha256=SHA, image_sha256=SHA, policy_revision="g:l",
            prompt_revision=prompt_revision, model="m", provider="p",
            max_output_tokens=256, expires_at=expires_at, active=True,
        )

    def begin_dispatch(self, job_id, image, *, checks):
        self.dispatched.append(job_id)
        from tybot.archive.image_description_store import ImageDescriptionGrant

        return ImageDescriptionGrant(
            dispatch=ImageDescriptionDispatch(
                dispatch_id=job_id, approval_id=APPROVAL, workspace=WS, channel=CH,
                file_id=FID, original_sha256=SHA, image_sha256=SHA, policy_revision="g:l",
                prompt_revision="p1", model="m", provider="p",
                max_output_tokens=256, expires_at=LATER, active=True,
            ),
            model="m", provider="p", max_output_tokens=256, timeout_seconds=20.0,
        )

    def finish_and_publish(self, job_id, result):
        self.published.append(job_id)
        revision, _ = self.publish
        if isinstance(revision, Exception):
            raise revision
        return revision

    def finish_without_call(self, job_id, *, code):
        self.unknown.append(f"{job_id}:{code}")

    def mark_unknown(self, job_id, *, code="result_unknown"):
        self.unknown.append(f"{job_id}:{code}")


class FakeRouter:
    """모델 대역. `describe_image` 가 실제로 타는 경로다."""

    def __init__(self):
        self.calls = 0

    def resolve(self, model, sensitivity):
        from tybot.gateway.base import ModelSpec, Sensitivity

        return ModelSpec(model, "p", 0.0, 0.0, Sensitivity.CONFIDENTIAL)

    def restricted_to(self, model, *, sensitivity):
        return self

    def complete(self, messages, **kw):
        self.calls += 1

        class _Resp:
            text = (
                '{"describable": true, "description": "건설현장에 굴착기가 보인다.",'
                ' "tags": ["건설현장"], "uncertainty": []}'
            )
            model, provider, stop_reason = "m", "p", "end_turn"
            input_tokens, output_tokens, cost_usd = 10, 5, 0.0

        return _Resp()


def _run(archive, *, store=None, router=None, candidate=None, doc=None,
         checks=None, limit=5_000_000):
    return runner.run_approved_image(
        candidate or _candidate(),
        store=store or FakeStore(),
        router=router or FakeRouter(),
        archive_root=archive,
        doc=doc if doc is not None else _doc(),
        approval_id=APPROVAL,
        prompt_revision="p1",
        expires_at=LATER,
        max_image_bytes=limit,
        checks=checks if checks is not None else {
            "access_verified": True, "pii_blocked": False,
        },
    )


# --- 끝까지 ------------------------------------------------------------------


def test_one_approved_photo_goes_all_the_way_through(archive):
    """조각은 다 있었다. **이 선이 없어 한 장도 안 돌았다.**"""
    store, router = FakeStore(), FakeRouter()
    out = _run(archive, store=store, router=router)

    assert out.stage == runner.STAGE_DONE
    assert out.dispatched is True
    assert out.job_id == JOB
    assert out.revision_id == "rev-1"
    assert out.state == "published"
    assert router.calls == 1, "모델은 한 번만 불린다"


def test_the_loaded_bytes_are_what_gets_reserved(archive):
    store = FakeStore()
    _run(archive, store=store)

    image = store.reserved[0]["image"]
    assert image.image == PNG
    assert image.original_sha256 == SHA
    assert (image.workspace, image.channel, image.file_id) == (WS, CH, FID)
    assert image.mime == "image/png"


def test_the_approval_and_prompt_revision_are_passed_through(archive):
    store = FakeStore()
    _run(archive, store=store)

    assert store.reserved[0]["approval_id"] == APPROVAL
    assert store.reserved[0]["prompt_revision"] == "p1"


# --- 안전은 우리가 정하지 않는다 ------------------------------------------------


def test_only_what_the_loader_proved_is_marked_true(archive):
    store = FakeStore()
    _run(archive, store=store, checks={"access_verified": True, "pii_blocked": False})

    said = store.reserved[0]["checks"]
    assert said["source_verified"] is True, "해시를 다시 잰 뒤다"
    assert said["bot_generated"] is False, "human_upload_confirmed 를 본 뒤다"
    assert said["access_verified"] is True
    assert said["pii_blocked"] is False


def test_a_missing_caller_check_is_not_filled_in(archive):
    """빠지면 메워 주지 않는다. **닫힌 기본값이 뜻을 잃는다.**"""
    store = FakeStore()
    _run(archive, store=store, checks={})

    said = store.reserved[0]["checks"]
    assert "pii_blocked" not in said
    assert "access_verified" not in said


def test_no_checks_at_all_still_reaches_the_store_closed(archive):
    """`checks` 를 아예 안 주면 **증명한 둘만** 간다. 나머지는 닫힌 채다."""
    store = FakeStore()
    runner.run_approved_image(
        _candidate(), store=store, router=FakeRouter(), archive_root=archive,
        doc=_doc(), approval_id=APPROVAL, prompt_revision="p1",
        expires_at=LATER, max_image_bytes=5_000_000,
    )

    said = store.reserved[0]["checks"]
    assert set(said) == {"source_verified", "bot_generated"}


@pytest.mark.parametrize("sneaky", [
    {"source_verified": True},
    {"bot_generated": False},
    {"pii_blocked": False, "source_verified": True},
    {"whatever": True},
])
def test_a_caller_cannot_assert_what_only_we_can_prove(archive, sneaky):
    """`pii_blocked=False` 가 호출자 아닌 곳에서 들어오는 길을 막는 것과 같은 축."""
    with pytest.raises(ValueError):
        _run(archive, checks=sneaky)


def test_nothing_is_reserved_when_the_checks_are_rejected(archive):
    store = FakeStore()
    with pytest.raises(ValueError):
        _run(archive, store=store, checks={"nope": True})

    assert store.reserved == []


# --- 거절 순서 --------------------------------------------------------------


def test_a_load_failure_never_reserves(archive):
    """로드가 막히면 **예약도 모델도 없다.**"""
    store, router = FakeStore(), FakeRouter()
    out = _run(archive, store=store, router=router,
               doc=_doc(human_upload_confirmed=False))

    assert out.stage == runner.STAGE_LOAD
    assert out.error_code == "human_upload_unconfirmed"
    assert store.reserved == []
    assert router.calls == 0


def test_a_hash_mismatch_stops_before_the_store(archive):
    store = FakeStore()
    out = _run(archive, store=store, candidate=_candidate(original_sha256="b" * 64),
               doc=_doc(sha256="b" * 64))

    assert out.stage == runner.STAGE_LOAD
    assert store.reserved == []


def test_a_reserve_refusal_never_calls_the_model(archive):
    store = FakeStore(reserve_error=ImageDescriptionDenied("quota_exhausted"))
    router = FakeRouter()
    out = _run(archive, store=store, router=router)

    assert out.stage == runner.STAGE_RESERVE
    assert out.error_code == "quota_exhausted"
    assert out.job_id == ""
    assert router.calls == 0


def test_the_stage_says_where_it_stopped(archive):
    assert _run(archive, doc=_doc(human_upload_confirmed=False)).stage == runner.STAGE_LOAD
    assert _run(
        archive, store=FakeStore(reserve_error=ImageDescriptionDenied("x_y")),
    ).stage == runner.STAGE_RESERVE
    assert _run(archive).stage == runner.STAGE_DONE


# --- 예외가 새지 않는다 --------------------------------------------------------


def test_a_store_exception_body_never_reaches_the_outcome(archive):
    """psycopg 오류는 `detail` 에 **실패한 행 전체**를 담는다."""
    boom = RuntimeError("Failing row contains (…, 주민등록증 사본 설명, …)")
    out = _run(archive, store=FakeStore(reserve_error=boom))

    assert out.error_code == "store_error"
    assert "주민등록증" not in repr(out)


def test_a_denied_code_is_kept_but_sanitised(archive):
    weird = ImageDescriptionDenied("행 내용: 김해외동 현장 사진")
    out = _run(archive, store=FakeStore(reserve_error=weird))

    assert out.error_code == "denied"
    assert "김해외동" not in repr(out)


def test_a_publish_failure_is_reported_without_losing_the_job(archive):
    """worker 가 `unknown` 으로 닫는다. **재전송되지 않는다.**"""
    store = FakeStore(publish=(RuntimeError("DB down"), ""))
    out = _run(archive, store=store)

    assert out.stage == runner.STAGE_DISPATCH
    assert out.error_code == "store_error"
    assert out.job_id == JOB, "어느 작업인지는 남는다"
    assert store.unknown == [f"{JOB}:publish_result_unknown"]
    assert "DB down" not in repr(out)


# --- 로그 -------------------------------------------------------------------


def test_the_log_carries_no_description_bytes_or_path(archive, caplog):
    caplog.set_level(logging.DEBUG, logger="tybot.archive.image_description_runner")
    _run(archive)

    said = "\n".join(r.getMessage() for r in caplog.records)
    assert said, "로그가 아예 안 남았다"
    assert "굴착기" not in said
    assert "synthetic-pixels" not in said
    assert str(archive) not in said
    assert FID in said and runner.STAGE_DONE in said


def test_every_stage_name_is_non_sensitive():
    for stage in runner.STAGES:
        assert stage.isascii() and stage.replace("_", "").isalnum(), stage


# --- 경계 -------------------------------------------------------------------


def test_the_runner_calls_no_model_db_or_network_itself():
    source = Path(runner.__file__).read_text(encoding="utf-8")

    # 주석·docstring 에 이름이 나오는 것은 괜찮다. **부르는지**를 본다.
    code = [
        line for line in source.splitlines()
        if line.strip().startswith(("import ", "from ")) or "(" in line
    ]
    said = "\n".join(code)
    for banned in ("requests", "httpx", "urllib", "socket", "psycopg.",
                   "describe_image(", "cur.execute"):
        assert banned not in said, banned
