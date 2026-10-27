"""후보 → 원본 이미지 바이트 로더 (B-81).

이 파일이 지키는 것 여섯.

1. **승인된 그 바이트인가.** 읽은 것의 SHA-256 을 후보 값과 다시 대조한다.
   메타데이터가 맞다는 것과 디스크의 바이트가 그것이라는 것은 다른 말이다.
2. **정말 이미지인가.** 매직 바이트를 본다 — 확장자는 이름일 뿐이다.
3. **우리 자리에 있나.** 경로는 우리가 만들고, 링크를 푼 뒤에도 뿌리 안이어야 한다.
4. **하나가 아니면 고르지 않는다.** 해시로 맞혀 고르면 끼워 넣기가 가능해진다.
5. **사람이 올린 것인가.** `human_upload_confirmed` 가 없으면 아니다.
6. **오류에 아무것도 담지 않는다.** 경로·파일명·바이트·예외 본문 전부.

모델·DB·네트워크를 부르지 않는다. 실제 임시 디렉터리에 합성 바이트를 깔고
경로 공격까지 실제로 건다 — 가짜 파일시스템으로는 링크·탈출을 못 잰다.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from tybot.archive import image_description_original as orig
from tybot.archive.attachment_doc import CONVERTED, AttachmentDoc
from tybot.archive.files import attachment_storage
from tybot.archive.image_description_source import ImageCandidate

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-pixels" * 4
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg" * 4
WEBP = b"RIFF" + (0).to_bytes(4, "little") + b"WEBP" + b"synthetic-webp" * 4
PDF = b"%PDF-1.7\n" + b"not-a-photo" * 4

WS, CH, FID = "tyit", "C0TEST0001", "F0TEST0001"
LIMIT = 5_000_000


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """`files.attachment_storage` 가 쓰는 것과 **같은 자리**를 만든다."""
    root = tmp_path / "archive"
    root.mkdir()
    return root


def _objects(archive: Path) -> Path:
    where = attachment_storage(archive, WS, CH).objects_dir / FID
    where.mkdir(parents=True, exist_ok=True)
    return where


def _put(archive: Path, data: bytes, *, name: str = "photo.png") -> Path:
    path = _objects(archive) / name
    path.write_bytes(data)
    return path


def _candidate(data: bytes = PNG, **over) -> ImageCandidate:
    base = {
        "workspace": WS,
        "channel_id": CH,
        "file_id": FID,
        "original_sha256": _sha(data),
        "filetype": "png",
        "mime": "image/png",
    }
    base.update(over)
    return ImageCandidate(**base)


def _doc(data: bytes = PNG, **over) -> AttachmentDoc:
    base = {
        "workspace": WS,
        "channel_id": CH,
        "channel": "#팀_전산(ABB100)_공지",
        "file_id": FID,
        "name": "현장사진",
        "revision": "r1",
        "visibility": "private",
        "acl": frozenset({"U1"}),
        "filetype": "png",
        "sha256": _sha(data),
        "conversion_state": CONVERTED,
        "human_upload_confirmed": True,
    }
    base.update(over)
    return AttachmentDoc(**base)


def _load(archive: Path, candidate=None, doc=None, limit: int = LIMIT):
    return orig.load_original(
        candidate if candidate is not None else _candidate(),
        archive_root=archive,
        doc=doc if doc is not None else _doc(),
        max_image_bytes=limit,
    )


# --- 정상 -------------------------------------------------------------------


def test_a_matching_original_becomes_an_image_input(archive):
    _put(archive, PNG)
    out = _load(archive)

    assert out.ok is True
    assert out.error_code == ""
    got = out.image
    assert (got.workspace, got.channel, got.file_id) == (WS, CH, FID)
    assert got.original_sha256 == _sha(PNG)
    assert got.image == PNG
    assert got.mime == "image/png"


@pytest.mark.parametrize("data,mime,filetype", [
    (PNG, "image/png", "png"),
    (JPEG, "image/jpeg", "jpg"),
    (WEBP, "image/webp", "webp"),
])
def test_every_allowed_format_is_recognised_by_its_magic_bytes(
    archive, data, mime, filetype
):
    _put(archive, data, name=f"photo.{filetype}")
    out = _load(
        archive,
        _candidate(data, filetype=filetype, mime=mime),
        doc=_doc(data, filetype=filetype),
    )

    assert out.ok, out.error_code
    assert out.image.mime == mime


def test_the_file_name_on_disk_does_not_decide_the_format(archive):
    """확장자는 이름일 뿐이다. 내용이 PNG 면 PNG 다."""
    _put(archive, PNG, name="사진")
    out = _load(archive)

    assert out.ok, out.error_code
    assert out.image.mime == "image/png"


# --- MIME 위장 --------------------------------------------------------------


def test_a_pdf_named_png_is_refused(archive):
    """`.png` 로 올라온 PDF 가 그대로 가면 **사진이라 믿은 것이 문서**가 된다."""
    _put(archive, PDF, name="photo.png")
    out = _load(archive, _candidate(PDF), doc=_doc(PDF))

    assert out.ok is False
    assert out.error_code == orig.ERROR_NOT_AN_IMAGE


@pytest.mark.parametrize("data", [
    b"GIF89a" + b"x" * 20,
    b"\x00\x00\x00\x20ftypheic" + b"x" * 20,
    b"RIFF" + (0).to_bytes(4, "little") + b"WAVE" + b"x" * 20,
    b"\x89PNG",                      # 머리가 모자라다
    b"x" * 40,
])
def test_a_non_image_payload_is_refused(archive, data):
    _put(archive, data)
    out = _load(archive, _candidate(data), doc=_doc(data))

    assert out.error_code == orig.ERROR_NOT_AN_IMAGE


def test_metadata_claiming_the_wrong_image_format_is_refused(archive):
    """둘 중 무엇이 맞는지 **여기서 알 수 없다.** 모르는 채로 보내지 않는다."""
    _put(archive, JPEG, name="photo.png")
    out = _load(
        archive,
        _candidate(JPEG, filetype="png", mime="image/png"),
        doc=_doc(JPEG),
    )

    assert out.error_code == orig.ERROR_MIME_MISMATCH


# --- 해시 -------------------------------------------------------------------


def test_bytes_that_do_not_match_the_approved_hash_are_refused(archive):
    """승인은 **그 바이트**에 묶여 있다. 다른 바이트면 승인이 없는 것이다."""
    _put(archive, PNG + b"tampered")
    out = _load(archive, _candidate(PNG), doc=_doc(PNG))

    assert out.error_code == orig.ERROR_HASH_MISMATCH


def test_the_hash_is_recomputed_not_trusted(archive):
    """메타데이터가 맞다는 것과 디스크의 바이트가 그것이라는 것은 다르다."""
    _put(archive, JPEG)
    lying = _candidate(PNG, filetype="jpg", mime="image/jpeg")

    out = _load(archive, lying, doc=_doc(PNG))

    assert out.error_code == orig.ERROR_HASH_MISMATCH


# --- 원본이 없거나 여럿 ---------------------------------------------------------


def test_a_missing_original_is_refused(archive):
    _objects(archive)
    out = _load(archive)

    assert out.error_code == orig.ERROR_MISSING


def test_a_missing_directory_is_refused(archive):
    out = _load(archive)

    assert out.error_code == orig.ERROR_OUTSIDE_ROOT


def test_two_originals_are_never_guessed_between(archive):
    """해시로 맞혀 고르면 **같은 자리에 다른 파일을 끼워 넣는 길**이 열린다."""
    _put(archive, PNG, name="photo.png")
    _put(archive, JPEG, name="photo.jpg")
    out = _load(archive)

    assert out.error_code == orig.ERROR_AMBIGUOUS


def test_the_right_hash_does_not_rescue_an_ambiguous_directory(archive):
    _put(archive, PNG, name="a.png")
    _put(archive, PNG + b"x", name="b.png")
    out = _load(archive)

    assert out.error_code == orig.ERROR_AMBIGUOUS


# --- 경로 ------------------------------------------------------------------


def test_a_symlinked_original_is_refused(archive, tmp_path):
    """가리키는 곳이 지금은 안이라도 **나중에 밖으로 바뀔 수 있다.**"""
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    link = _objects(archive) / "photo.png"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다")

    out = _load(archive)

    assert out.error_code in (orig.ERROR_SYMLINK, orig.ERROR_OUTSIDE_ROOT)


def test_a_symlink_pointing_inside_the_root_is_still_refused(archive):
    """**뿌리 안을 가리켜도 링크는 링크다.**

    경로를 확인한 순간과 여는 순간 사이에 링크 대상이 바뀔 수 있다. 바깥을
    가리키는 링크는 경로 검사가 잡지만, 이 경우는 **그 검사를 통과한다** —
    링크 자체를 거절하지 않으면 그 틈이 남는다.
    """
    objects = _objects(archive)
    real = objects / "sub"
    real.mkdir()
    (real / "real.png").write_bytes(PNG)
    link = objects / "photo.png"
    try:
        link.symlink_to(real / "real.png")
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다")

    out = _load(archive)

    assert out.error_code == orig.ERROR_SYMLINK


def test_a_symlinked_objects_directory_is_refused(archive, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "photo.png").write_bytes(PNG)
    parent = attachment_storage(archive, WS, CH).objects_dir
    parent.mkdir(parents=True, exist_ok=True)
    try:
        (parent / FID).symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다")

    out = _load(archive)

    # 링크 체인 검사가 **경로 검사보다 먼저** 잡는다 — 더 정확한 사유다.
    assert out.error_code == orig.ERROR_SYMLINK


@pytest.mark.parametrize("field", ["file_id", "workspace", "channel_id"])
@pytest.mark.parametrize("value", [
    "../../etc",
    "..",
    "../F0TEST0001",
    "/etc/passwd",
    "F0TEST0001/../../..",
    "F0TEST0001/photo.png",
    "",
    "   ",
])
def test_a_coordinate_that_is_not_a_single_path_segment_is_refused(
    archive, field, value
):
    """**중화에 기대지 않는다.**

    `_safe_component` 는 `../F0ABC` 를 `F0ABC` 로 바꾼다. 탈출은 막히지만 바이트는
    `F0ABC` 에서 오고 결과에 적히는 좌표는 `../F0ABC` 다 — 나중에 그 출처를
    누르면 **없는 자리**가 열린다. 바뀌어야 하는 좌표면 거절한다.
    """
    _put(archive, PNG)
    out = _load(archive, _candidate(**{field: value}), doc=_doc(**{field: value}))

    assert out.ok is False
    assert out.error_code == orig.ERROR_UNSAFE_COORDINATE


def test_a_real_slack_coordinate_passes_the_segment_check(archive):
    """실제 Slack ID·workspace 이름은 이 검사를 그냥 지나야 한다."""
    _put(archive, PNG)

    assert _load(archive).ok is True


def test_a_directory_in_place_of_the_original_is_refused(archive):
    (_objects(archive) / "photo.png").mkdir()
    out = _load(archive)

    assert out.error_code == orig.ERROR_MISSING


# --- 두 배치 ---------------------------------------------------------------
#
# 아카이브는 옛 배치와 shadow 배치가 공존하고 **objects 자리가 다르다.**
# 운영 수집기는 shadow 쪽을 쓴다(`archiving_bot` 이 `channel_root` 를 넘긴다).


def _shadow(archive: Path) -> tuple[Path, Path]:
    """shadow 배치의 (정본 문서 경로, objects 디렉터리)."""
    channel_root = archive / WS / f"{CH}__공지"
    doc_path = (
        channel_root / "archive" / "attachments" / FID / "r1.md"
    )
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text("x", encoding="utf-8")
    objects = attachment_storage(
        archive, WS, CH, channel_root=channel_root,
    ).objects_dir / FID
    objects.mkdir(parents=True, exist_ok=True)
    return doc_path, objects


def test_the_shadow_layout_is_resolved_from_the_doc(archive):
    """옛 배치만 가정하면 새 배치의 사진이 전부 **「없음」** 이 된다."""
    doc_path, objects = _shadow(archive)
    (objects / "photo.png").write_bytes(PNG)

    out = _load(archive, doc=_doc(source_path=doc_path))

    assert out.ok is True, out.error_code
    assert out.image.image == PNG


def test_the_legacy_layout_still_works_without_a_source_path(archive):
    _put(archive, PNG)

    assert _load(archive).ok is True


def test_a_shadow_doc_does_not_read_the_legacy_objects_dir(archive):
    """배치를 섞어 읽으면 **다른 채널의 바이트**를 집을 수 있다."""
    _put(archive, PNG)
    doc_path, _ = _shadow(archive)

    out = _load(archive, doc=_doc(source_path=doc_path))

    assert out.ok is False
    assert out.error_code == orig.ERROR_MISSING


def test_a_symlinked_segment_on_the_way_down_is_refused(archive, tmp_path):
    """마지막 칸만 보면 `<workspace>` 가 **남의 자리**를 가리켜도 통과한다."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "objects").mkdir(parents=True)
    legacy_objects = attachment_storage(archive, WS, CH).objects_dir
    workspace_dir = legacy_objects.parents[2]
    workspace_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        workspace_dir.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("이 환경에서는 심볼릭 링크를 만들 수 없습니다")

    out = _load(archive)

    assert out.error_code in (orig.ERROR_SYMLINK, orig.ERROR_OUTSIDE_ROOT)


def test_an_unresolvable_layout_is_refused_not_guessed(archive, tmp_path):
    """되짚기에 실패하면 **어느 자리인지 모르는 것**이다. 고르지 않는다."""
    shallow = archive / "a.md"
    shallow.write_text("x", encoding="utf-8")
    _put(archive, PNG)

    out = _load(archive, doc=_doc(source_path=shallow))

    assert out.ok is False
    assert out.error_code in (orig.ERROR_OUTSIDE_ROOT, orig.ERROR_MISSING)


def test_a_doc_from_outside_the_root_is_refused(archive, tmp_path):
    """정본 문서가 뿌리 밖에서 왔다면 그것이 가리키는 바이트도 믿을 수 없다."""
    _put(archive, PNG)
    stray = tmp_path / "stray.md"
    stray.write_text("x", encoding="utf-8")
    out = _load(archive, doc=_doc(source_path=stray))

    assert out.error_code == orig.ERROR_OUTSIDE_ROOT


def test_a_doc_from_inside_the_root_passes(archive):
    _put(archive, PNG)
    inside = archive / "workspaces" / WS / "doc.md"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("x", encoding="utf-8")

    out = _load(archive, doc=_doc(source_path=inside))

    assert out.ok, out.error_code


# --- 크기 ------------------------------------------------------------------


def test_a_file_over_the_cap_is_refused_by_stat(archive):
    _put(archive, PNG)
    out = _load(archive, limit=len(PNG) - 1)

    assert out.error_code == orig.ERROR_TOO_LARGE


def test_a_file_at_the_cap_is_read(archive):
    _put(archive, PNG)
    out = _load(archive, limit=len(PNG))

    assert out.ok, out.error_code


def test_an_oversized_file_is_never_opened(archive, monkeypatch):
    """`stat` 이 먼저 막는다. 상한을 넘는 파일은 **열지도 않는다.**"""
    _put(archive, PNG)

    def _boom(*args, **kw):
        raise AssertionError("상한을 넘는 파일을 열었다")

    monkeypatch.setattr(Path, "open", _boom)
    out = _load(archive, limit=len(PNG) - 1)

    assert out.error_code == orig.ERROR_TOO_LARGE


def test_the_bytes_actually_read_are_capped_too(archive, monkeypatch):
    """stat 과 실제 길이가 다를 수 있다 — **그 사이에 파일이 자라면** 못 막는다."""
    _put(archive, PNG)
    real = Path.lstat

    def _small(self):
        info = real(self)

        class _Lie:
            st_mode = info.st_mode
            st_size = 1
        return _Lie()

    monkeypatch.setattr(Path, "lstat", _small)
    out = _load(archive, limit=len(PNG) - 1)

    assert out.error_code == orig.ERROR_TOO_LARGE


def test_an_empty_original_is_refused(archive):
    _put(archive, b"")
    out = _load(archive, _candidate(b""), doc=_doc(b""))

    assert out.error_code == orig.ERROR_EMPTY


@pytest.mark.parametrize("limit", [0, -1, 1.5, "100", None])
def test_a_bad_cap_is_refused(archive, limit):
    _put(archive, PNG)
    with pytest.raises(ValueError):
        orig.load_original(_candidate(), archive_root=archive, doc=_doc(),
                           max_image_bytes=limit)


def test_an_empty_archive_root_is_refused(archive):
    with pytest.raises(ValueError):
        orig.load_original(_candidate(), archive_root="  ", doc=_doc(),
                           max_image_bytes=LIMIT)


# --- provenance --------------------------------------------------------------


@pytest.mark.parametrize("over", [
    {"workspace": "tyhq"},
    {"channel_id": "C0OTHER"},
    {"file_id": "F0OTHER"},
    {"sha256": "b" * 64},
])
def test_a_doc_that_disagrees_with_the_candidate_is_refused(archive, over):
    """후보는 선별 시점의 사실이다. 그 뒤 재변환·복구가 있었을 수 있다."""
    _put(archive, PNG)
    out = _load(archive, doc=_doc(**over))

    assert out.error_code == orig.ERROR_PROVENANCE


def test_a_missing_doc_is_refused(archive):
    _put(archive, PNG)
    out = orig.load_original(_candidate(), archive_root=archive, doc=None,
                             max_image_bytes=LIMIT)

    assert out.error_code == orig.ERROR_PROVENANCE


def test_an_unconfirmed_human_upload_is_refused(archive):
    """옛 자료는 이 값이 없다. **없으면 아니다**(fail-closed)."""
    _put(archive, PNG)
    out = _load(archive, doc=_doc(human_upload_confirmed=False))

    assert out.error_code == orig.ERROR_NOT_HUMAN


def test_provenance_is_checked_before_the_file_is_opened(archive):
    """어긋난 좌표로 **디스크를 뒤지지 않는다.**"""
    out = _load(archive, doc=_doc(human_upload_confirmed=False))

    assert out.error_code == orig.ERROR_NOT_HUMAN


# --- 새지 않는다 -------------------------------------------------------------


@pytest.mark.parametrize("make,expected", [
    (lambda a: None, orig.ERROR_OUTSIDE_ROOT),
    (lambda a: _objects(a), orig.ERROR_MISSING),
    (lambda a: _put(a, PDF, name="비밀문서.png"), orig.ERROR_NOT_AN_IMAGE),
])
def test_a_refusal_carries_only_a_code(archive, make, expected):
    make(archive)
    out = _load(archive, _candidate(PDF), doc=_doc(PDF))

    assert out.image is None
    assert out.error_code in orig.ERROR_CODES
    said = repr(out)
    assert str(archive) not in said
    assert "비밀문서" not in said
    assert "%PDF" not in said


def test_the_log_carries_no_path_name_or_bytes(archive, caplog):
    caplog.set_level(logging.DEBUG, logger="tybot.archive.image_description_original")
    _put(archive, PDF, name="주민등록증사본.png")
    _load(archive, _candidate(PDF), doc=_doc(PDF))

    said = "\n".join(r.getMessage() for r in caplog.records)
    assert said, "로그가 아예 안 남았다"
    assert "주민등록증" not in said
    assert str(archive) not in said
    assert "%PDF" not in said
    assert orig.ERROR_NOT_AN_IMAGE in said
    assert FID in said, "좌표는 남는다"


def test_a_success_log_carries_no_bytes(archive, caplog):
    caplog.set_level(logging.DEBUG, logger="tybot.archive.image_description_original")
    _put(archive, PNG, name="현장전경.png")
    _load(archive)

    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "현장전경" not in said
    assert "synthetic-pixels" not in said
    assert str(archive) not in said


def test_every_error_code_is_non_sensitive():
    for code in orig.ERROR_CODES:
        assert code.isascii() and code.replace("_", "").isalnum(), code


# --- 경계 ------------------------------------------------------------------


def test_the_loader_touches_no_model_db_or_network():
    source = Path(orig.__file__).read_text(encoding="utf-8")

    for banned in ("requests", "httpx", "urllib", "socket", "psycopg",
                   "router", "complete(", "describe_image"):
        assert banned not in source, banned


def test_the_mime_table_matches_the_core():
    from tybot.archive.image_description import ALLOWED_MIME

    for data, mime in ((PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")):
        assert orig._sniff(data[:orig.MAGIC_BYTES]) == mime
        assert mime in ALLOWED_MIME
