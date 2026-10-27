"""후보 → **원본 이미지 바이트**. 승인된 그 바이트인지 매번 다시 증명한다.

## 왜 다시 증명하나

선별기(`image_description_source`)는 메타데이터만 본다. 메타데이터가 맞다는 것과
**디스크에 있는 바이트가 그것**이라는 것은 다른 말이다. 그 사이에 재변환·복구·
재업로드가 끼어들 수 있고, 그때 바뀌는 것은 파일이지 메타데이터가 아니다.

그래서 여기서는 적힌 것을 믿지 않고 **다시 잰다.**

| 묻는 것 | 어떻게 |
|---|---|
| 승인된 그 파일인가 | 읽은 바이트의 SHA-256 을 후보 값과 대조 |
| 정말 이미지인가 | **매직 바이트**를 본다. 확장자는 이름일 뿐이다 |
| 우리 자리에 있나 | 경로를 **우리가 만든다.** 메타데이터의 문자열을 안 쓴다 |
| 사람이 올린 것인가 | 정본의 `human_upload_confirmed` 가 **참일 때만** |

## 경로를 메타데이터에서 받지 않는다

`object_path` 같은 문자열을 받아 열면, 그 문자열 한 줄만 바뀌어도 **아카이브 밖
아무 파일이나** 모델로 나간다. 그래서 받는 것은 **신뢰하는 아카이브 뿌리**와
좌표뿐이고, 실제 경로는 수집이 쓴 것과 **같은 규칙**(`files.attachment_storage`)
으로 여기서 만든다. 규칙을 베껴 적지 않고 그 함수를 부른다 — 베끼면 갈라진다.

만든 경로조차 그대로 열지 않는다. `resolve()` 로 심볼릭 링크를 다 푼 뒤
**그 결과가 여전히 objects 뿌리 안인지** 확인한다. 밖이면 거절이다.

## 하나가 아니면 고르지 않는다

한 `file_id` 자리에 파일이 둘 있으면 어느 쪽이 승인된 것인지 **모른다.** 해시로
맞혀 볼 수는 있지만, 그러면 「맞는 것이 하나 있으면 통과」 가 되어 같은 자리에
다른 파일을 끼워 넣는 길이 열린다. 하나가 아니면 거절하고 사람이 본다.

## 오류에 아무것도 담지 않는다

돌려주는 것은 **고정 코드 하나**다. 경로·파일명·바이트·예외 본문을 담지 않는다 —
이 값은 작업 원장과 운영 화면으로 그대로 나가고, 거기서 한 번 새면 되돌릴 수 없다.

모델·DB·네트워크를 부르지 않는다. 파일 하나를 읽는 것이 전부다.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from .image_description import ALLOWED_MIME, ImageInput

logger = logging.getLogger(__name__)

#: 매직 바이트 → MIME. **확장자를 믿지 않는다.**
#:
#: `.png` 로 올라온 PDF 가 그대로 모델에 가면, 우리가 사진이라고 믿은 것이
#: 문서가 된다 — 설명 프롬프트는 문서를 막지 못한다.
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_RIFF = b"RIFF"
_WEBP = b"WEBP"

#: 형식을 알아보는 데 필요한 머리 길이. WebP 가 12바이트로 제일 길다.
MAGIC_BYTES = 12

ERROR_MISSING = "original_missing"
ERROR_AMBIGUOUS = "original_ambiguous"
ERROR_OUTSIDE_ROOT = "original_outside_root"
ERROR_NOT_A_FILE = "original_not_a_regular_file"
ERROR_SYMLINK = "original_symlink"
ERROR_UNREADABLE = "original_unreadable"
ERROR_TOO_LARGE = "original_too_large"
ERROR_EMPTY = "original_empty"
ERROR_HASH_MISMATCH = "original_hash_mismatch"
ERROR_NOT_AN_IMAGE = "original_not_an_image"
ERROR_MIME_MISMATCH = "original_mime_mismatch"
ERROR_PROVENANCE = "provenance_mismatch"
ERROR_NOT_HUMAN = "human_upload_unconfirmed"
#: 좌표가 경로 한 칸으로 **그대로** 쓰일 수 없다. 아래 `_coordinate_code` 참고.
ERROR_UNSAFE_COORDINATE = "unsafe_coordinate"

ERROR_CODES = (
    ERROR_MISSING,
    ERROR_AMBIGUOUS,
    ERROR_OUTSIDE_ROOT,
    ERROR_NOT_A_FILE,
    ERROR_SYMLINK,
    ERROR_UNREADABLE,
    ERROR_TOO_LARGE,
    ERROR_EMPTY,
    ERROR_HASH_MISMATCH,
    ERROR_NOT_AN_IMAGE,
    ERROR_MIME_MISMATCH,
    ERROR_PROVENANCE,
    ERROR_NOT_HUMAN,
    ERROR_UNSAFE_COORDINATE,
)


@dataclass(frozen=True)
class OriginalLoad:
    """읽기 결과. **성공이면 `image`, 아니면 코드 하나.**

    예외를 쓰지 않는 이유는 둘이다. 배치가 건당 사유를 세야 하고, 예외는
    메시지에 경로를 끌고 다니기 쉽다.
    """

    image: ImageInput | None = None
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return self.image is not None


def _sniff(head: bytes) -> str:
    """머리 바이트로 본 MIME. 모르면 빈 문자열."""
    if head.startswith(_PNG):
        return "image/png"
    if head.startswith(_JPEG):
        return "image/jpeg"
    if head.startswith(_RIFF) and head[8:12] == _WEBP:
        return "image/webp"
    return ""


def _channel_root(archive_root: Path, doc) -> Path | None:
    """정본이 어느 배치에서 왔나. 옛 배치면 `None`.

    아카이브는 두 배치가 공존한다.

    ```
    옛: <root>/workspaces/<ws>/channels/<id>/attachments/<file>/<rev>.md
    새: <root>/<ws>/<id>__<이름>/archive/attachments/<file>/<rev>.md
    ```

    objects 자리가 **배치마다 다르다.** 옛 배치만 가정하면 새 배치의 사진은 전부
    「없음」 이 되고, 그 사실이 원장에는 **파일이 사라진 것**처럼 보인다.

    `ack_reconcile._storage_for` 와 같은 규칙이다. 그쪽은 private 이고 실패를
    `None` 으로 삼키므로 여기서 다시 적되, 되짚기에 실패하면 **거절**한다.
    """
    source = getattr(doc, "source_path", None)
    if source is None:
        return None
    path = Path(source).resolve(strict=False)
    if path.is_relative_to((archive_root / "workspaces").resolve(strict=False)):
        return None
    return path.parents[3]


def _objects_dir(archive_root: Path, candidate, channel_root: Path | None) -> Path:
    """수집이 쓴 것과 **같은 규칙**으로 만든다. 베끼지 않고 그 함수를 부른다."""
    from .files import _safe_component, attachment_storage

    storage = attachment_storage(
        archive_root, candidate.workspace, candidate.channel_id,
        channel_root=channel_root,
    )
    return storage.objects_dir / _safe_component(candidate.file_id)


def _coordinate_code(candidate) -> str:
    """좌표가 경로 한 칸으로 **그대로** 쓰일 수 있나.

    `_safe_component` 는 `../F0ABC` 를 `F0ABC` 로 **바꿔 버린다.** 탈출은 막히지만
    더 나쁜 일이 남는다 — 바이트는 `F0ABC` 에서 오고 결과에 적히는 좌표는
    `../F0ABC` 라, 나중에 그 출처를 누르면 **없는 자리**가 열린다.

    그래서 중화에 기대지 않고, **바뀌어야 하는 좌표면 거절한다.** 실제 Slack
    ID 와 workspace 이름은 이 검사를 그냥 지난다.
    """
    from .files import _safe_component

    for value in (candidate.workspace, candidate.channel_id, candidate.file_id):
        said = str(value or "")
        if not said or _safe_component(said) != said:
            return ERROR_UNSAFE_COORDINATE
    return ""


def _inside(path: Path, root: Path) -> bool:
    """`path` 가 링크를 다 푼 뒤에도 `root` 안인가."""
    try:
        return path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        # `RuntimeError` 는 링크 고리, `OSError` 는 없는 경로다.
        return False


def _provenance_code(candidate, doc) -> str:
    """정본과 후보가 **같은 것을 가리키나.** 어긋나면 첫 사유.

    선별 이후 재변환·복구가 끼어들 수 있다. 그때 바뀌는 것은 정본 쪽이고,
    후보는 예전 사실을 들고 있다 — 그대로 보내면 **승인받지 않은 바이트**가 간다.
    """
    if doc is None:
        return ERROR_PROVENANCE
    if (
        (getattr(doc, "workspace", "") or "") != candidate.workspace
        or (getattr(doc, "channel_id", "") or "") != candidate.channel_id
        or (getattr(doc, "file_id", "") or "") != candidate.file_id
    ):
        return ERROR_PROVENANCE
    if (getattr(doc, "sha256", "") or "").strip().lower() != candidate.original_sha256:
        return ERROR_PROVENANCE
    if getattr(doc, "human_upload_confirmed", False) is not True:
        # 옛 자료는 이 값이 없다. **없으면 아니다** — 봇이 만든 그림을 봇이
        # 설명하고 그 설명이 검색 결과가 되는 고리를 여기서 끊는다(원칙 1).
        return ERROR_NOT_HUMAN
    return ""


def _one_file(objects: Path, root: Path) -> tuple[Path | None, str]:
    """그 자리에 있는 **단 하나의** 일반 파일. 아니면 사유."""
    if not _inside(objects, root):
        return None, ERROR_OUTSIDE_ROOT
    try:
        entries = sorted(objects.iterdir())
    except OSError:
        return None, ERROR_MISSING
    found = [p for p in entries if not p.is_dir()]
    if not found:
        return None, ERROR_MISSING
    if len(found) > 1:
        # 해시로 맞혀 고르면 「맞는 것이 하나 있으면 통과」 가 되고, 같은 자리에
        # 다른 파일을 끼워 넣는 길이 열린다.
        return None, ERROR_AMBIGUOUS
    return found[0], ""


def _read(path: Path, limit: int) -> tuple[bytes, str]:
    """상한 안에서 읽는다. **stat 과 실제 길이를 둘 다** 본다."""
    try:
        if path.is_symlink():
            # 링크 자체를 거절한다. 가리키는 곳이 지금은 안이라도 **나중에
            # 밖으로 바뀔 수 있다** — 검사와 읽기 사이가 그 틈이다.
            return b"", ERROR_SYMLINK
        info = path.lstat()
    except OSError:
        return b"", ERROR_UNREADABLE
    from stat import S_ISREG

    if not S_ISREG(info.st_mode):
        return b"", ERROR_NOT_A_FILE
    if info.st_size > limit:
        return b"", ERROR_TOO_LARGE
    try:
        with path.open("rb") as handle:
            # 한 바이트 더 읽어 **실제 길이**를 확인한다. stat 과 다를 수 있다 —
            # 그 사이에 파일이 자라면 stat 만으로는 못 막는다.
            data = handle.read(limit + 1)
    except OSError:
        return b"", ERROR_UNREADABLE
    if len(data) > limit:
        return b"", ERROR_TOO_LARGE
    if not data:
        return b"", ERROR_EMPTY
    return data, ""


def load_original(
    candidate,
    *,
    archive_root,
    doc=None,
    max_image_bytes: int,
) -> OriginalLoad:
    """후보의 원본 바이트를 읽어 `ImageInput` 을 만든다.

    `archive_root` 는 **신뢰하는 아카이브 뿌리**다. 메타데이터에서 오지 않는다.
    `doc` 은 **지금의** 정본이다. 후보와 어긋나면 거절한다 — 후보는 선별 시점의
    사실이고, 그 뒤에 재변환·복구가 있었을 수 있다.

    예외를 내지 않는다. 입력 계약 위반만 `ValueError` 다.
    """
    if type(max_image_bytes) is not int or max_image_bytes <= 0:
        raise ValueError("max_image_bytes must be a positive integer")
    root = Path(archive_root)
    if not str(archive_root).strip():
        raise ValueError("archive_root must not be empty")

    code = _coordinate_code(candidate) or _provenance_code(candidate, doc)
    if code:
        return _fail(candidate, code)

    source = getattr(doc, "source_path", None)
    if source is not None and not _inside(Path(source), root):
        # 정본 문서 자체가 뿌리 밖에서 왔다. 그 문서가 가리키는 바이트를 믿을
        # 이유가 없다.
        return _fail(candidate, ERROR_OUTSIDE_ROOT)

    try:
        channel_root = _channel_root(root, doc)
        objects = _objects_dir(root, candidate, channel_root)
    except (OSError, ValueError, IndexError):
        # 되짚기에 실패했다. `ack_reconcile` 은 `None` 으로 삼키지만 여기서는
        # **거절한다** — 어느 자리인지 모르는 채 고르면 틀린 자리를 열 수 있다.
        return _fail(candidate, ERROR_OUTSIDE_ROOT)
    try:
        # 뿌리부터 objects 까지 **모든 칸**이 링크가 아니어야 한다. 마지막 칸만
        # 보면 `<workspace>` 가 남의 자리를 가리킬 때 통과한다(`shadow_paths`).
        #
        # 기준이 배치마다 다르다. **옛 배치의 objects 는 아카이브 뿌리의
        # 형제**(`<root>/../objects/...`)라 뿌리를 기준으로 삼으면 relative 가
        # 아니다. shadow 배치는 뿌리 안이다.
        from .shadow_paths import ShadowPathError, refuse_symlinked_chain

        refuse_symlinked_chain(root if channel_root is not None else root.parent, objects)
    except ShadowPathError:
        return _fail(candidate, ERROR_SYMLINK)
    except (OSError, ValueError):
        return _fail(candidate, ERROR_OUTSIDE_ROOT)

    path, code = _one_file(objects, objects.parent)
    if code:
        return _fail(candidate, code)
    if not _inside(path, objects):
        return _fail(candidate, ERROR_OUTSIDE_ROOT)

    data, code = _read(path, max_image_bytes)
    if code:
        return _fail(candidate, code)

    if hashlib.sha256(data).hexdigest() != candidate.original_sha256:
        # 승인은 **그 바이트**에 묶여 있다. 다른 바이트면 승인이 없는 것이다.
        return _fail(candidate, ERROR_HASH_MISMATCH)

    mime = _sniff(data[:MAGIC_BYTES])
    if not mime or mime not in ALLOWED_MIME:
        return _fail(candidate, ERROR_NOT_AN_IMAGE)
    if mime != candidate.mime:
        # 메타데이터가 말한 형식과 실제가 다르다. 둘 중 무엇이 맞는지는 여기서
        # 알 수 없고, **모르는 채로 보내지 않는다.**
        return _fail(candidate, ERROR_MIME_MISMATCH)

    logger.info(
        "image_original ws=%s ch=%s file=%s mime=%s bytes=%d",
        candidate.workspace, candidate.channel_id, candidate.file_id, mime, len(data),
    )
    return OriginalLoad(
        image=ImageInput(
            workspace=candidate.workspace,
            channel=candidate.channel_id,
            file_id=candidate.file_id,
            original_sha256=candidate.original_sha256,
            image=data,
            mime=mime,
        )
    )


def _fail(candidate, code: str) -> OriginalLoad:
    """**좌표와 코드만.** 경로·파일명·바이트·예외 본문을 남기지 않는다."""
    logger.info(
        "image_original 거절 ws=%s ch=%s file=%s code=%s",
        candidate.workspace, candidate.channel_id, candidate.file_id, code,
    )
    return OriginalLoad(error_code=code)


__all__ = [
    "ERROR_AMBIGUOUS",
    "ERROR_CODES",
    "ERROR_EMPTY",
    "ERROR_HASH_MISMATCH",
    "ERROR_MIME_MISMATCH",
    "ERROR_MISSING",
    "ERROR_NOT_AN_IMAGE",
    "ERROR_NOT_A_FILE",
    "ERROR_NOT_HUMAN",
    "ERROR_OUTSIDE_ROOT",
    "ERROR_PROVENANCE",
    "ERROR_SYMLINK",
    "ERROR_TOO_LARGE",
    "ERROR_UNREADABLE",
    "ERROR_UNSAFE_COORDINATE",
    "MAGIC_BYTES",
    "OriginalLoad",
    "load_original",
]
