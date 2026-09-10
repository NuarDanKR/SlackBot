"""Validate uploaded specialist prompt-contract ZIP bundles without extracting them."""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import stat
import time
import urllib.parse
import zipfile
from pathlib import PurePosixPath

from .specialist_git import SpecialistGitError, validate_contract

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 30
MAX_UNCOMPRESSED_BYTES = 512 * 1024
MAX_COMPRESSION_RATIO = 100
RECEIPT_TTL_SECONDS = 30 * 60
_PROCESS_RECEIPT_KEY = secrets.token_bytes(32)
_PROTECTED_FIELDS = (
    "sourceType", "sourceName", "bundleSha256", "repositoryUrl", "releaseRef",
    "sourceCommit", "artifactHashes", "key", "name", "domain", "adapter",
    "version", "contractVersion", "rules",
)


class SpecialistZipError(RuntimeError):
    """An uploaded ZIP is not a safe TYBot specialist contract bundle."""


def _receipt_key() -> bytes:
    configured = os.getenv("CONSOLE_SECRET", "").encode("utf-8")
    return hashlib.sha256(configured).digest() if configured else _PROCESS_RECEIPT_KEY


def _safe_filename(value: str) -> str:
    name = urllib.parse.unquote(value).strip()
    if (
        not name
        or len(name) > 150
        or not name.lower().endswith(".zip")
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        raise SpecialistZipError("파일명은 경로가 없는 150자 이하의 .zip 이름이어야 합니다.")
    return name


def _member_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        raise SpecialistZipError("ZIP 내부 경로 형식이 안전하지 않습니다.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise SpecialistZipError(f"ZIP 내부 경로가 묶음 밖을 가리킵니다: {value}")
    return path


def import_bundle(content: bytes, filename: str) -> dict:
    """Read a contract-only ZIP in memory and return the canonical import result."""
    safe_name = _safe_filename(filename)
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise SpecialistZipError("계약 ZIP은 비어 있지 않은 2MB 이하 파일이어야 합니다.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SpecialistZipError("올바른 ZIP 파일이 아닙니다.") from exc

    with archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_ENTRIES:
            raise SpecialistZipError(f"ZIP 항목은 1~{MAX_ARCHIVE_ENTRIES}개여야 합니다.")
        files: dict[str, zipfile.ZipInfo] = {}
        manifest_candidates: list[tuple[PurePosixPath, zipfile.ZipInfo]] = []
        total_size = 0
        for info in infos:
            path = _member_path(info.filename)
            if info.flag_bits & 0x1:
                raise SpecialistZipError("암호화된 ZIP은 업로드할 수 없습니다.")
            if info.is_dir():
                continue
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type not in (0, stat.S_IFREG) or mode & 0o111:
                raise SpecialistZipError(f"일반 비실행 파일만 허용됩니다: {info.filename}")
            total_size += info.file_size
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise SpecialistZipError("ZIP 압축 해제 크기는 512KB를 넘을 수 없습니다.")
            if (
                info.file_size > 4096
                and info.file_size > max(info.compress_size, 1) * MAX_COMPRESSION_RATIO
            ):
                raise SpecialistZipError(f"비정상적으로 압축률이 높은 항목입니다: {info.filename}")
            normalized = str(path)
            if normalized in files:
                raise SpecialistZipError(f"ZIP에 중복 경로가 있습니다: {info.filename}")
            files[normalized] = info
            if path.name == "tybot-specialist.toml":
                manifest_candidates.append((path, info))

        if len(manifest_candidates) != 1:
            raise SpecialistZipError("ZIP에는 tybot-specialist.toml이 정확히 하나 있어야 합니다.")
        manifest_path, manifest_info = manifest_candidates[0]
        root = manifest_path.parent
        relative_files: dict[str, zipfile.ZipInfo] = {}
        for path_text, info in files.items():
            path = PurePosixPath(path_text)
            try:
                relative = path.relative_to(root)
            except ValueError as exc:
                raise SpecialistZipError(f"계약 묶음 밖의 파일이 있습니다: {path_text}") from exc
            relative_text = str(relative)
            if relative_text != "tybot-specialist.toml" and relative.parts[0] != "contract":
                raise SpecialistZipError(f"계약과 무관한 파일이 포함됐습니다: {relative_text}")
            if relative_text in relative_files:
                raise SpecialistZipError(f"ZIP에 중복 계약 경로가 있습니다: {relative_text}")
            relative_files[relative_text] = info

        def read(info: zipfile.ZipInfo) -> bytes:
            try:
                with archive.open(info) as stream:
                    return stream.read(info.file_size + 1)
            except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
                raise SpecialistZipError(f"ZIP 항목을 읽지 못했습니다: {info.filename}") from exc

        manifest_bytes = read(manifest_info)
        try:
            contract = validate_contract(
                manifest_bytes,
                lambda path: read(relative_files[path]),
            )
        except (KeyError, SpecialistGitError) as exc:
            detail = str(exc) if not isinstance(exc, KeyError) else f"선언된 계약 파일이 없습니다: {exc.args[0]}"
            raise SpecialistZipError(detail) from exc
        unexpected = set(relative_files) - {"tybot-specialist.toml", *contract["artifactHashes"]}
        if unexpected:
            raise SpecialistZipError(
                f"매니페스트에 선언하지 않은 계약 파일이 있습니다: {sorted(unexpected)[0]}"
            )
        digest = hashlib.sha256(content).hexdigest()
        return {
            **contract,
            "sourceType": "zip",
            "sourceName": safe_name,
            "bundleSha256": digest,
            "repositoryUrl": "",
            "releaseRef": "",
            "sourceCommit": "",
            "checks": [
                {"id": "upload", "state": "pass", "detail": f"{safe_name} · {digest[:12]}"},
                *contract["checks"],
            ],
        }


def _receipt_message(imported: dict, actor: str, expires: int) -> bytes:
    protected = {field: imported.get(field) for field in _PROTECTED_FIELDS}
    payload = {"actor": actor.lower(), "expires": expires, "imported": protected}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def issue_receipt(imported: dict, actor: str) -> str:
    expires = int(time.time()) + RECEIPT_TTL_SECONDS
    signature = hmac.new(_receipt_key(), _receipt_message(imported, actor, expires), "sha256")
    return f"{expires}.{signature.hexdigest()}"


def verify_receipt(proposal: dict, actor: str, receipt: str) -> bool:
    try:
        expires_text, supplied = receipt.split(".", 1)
        expires = int(expires_text)
    except (AttributeError, TypeError, ValueError):
        return False
    if expires < int(time.time()) or expires > int(time.time()) + RECEIPT_TTL_SECONDS:
        return False
    expected = hmac.new(_receipt_key(), _receipt_message(proposal, actor, expires), "sha256")
    return hmac.compare_digest(expected.hexdigest(), supplied)
