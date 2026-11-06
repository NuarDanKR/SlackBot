"""Fail-closed boundary for files attached to one bot question."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from .archive.files import SlackFile, file_lines

MAX_QUESTION_FILES = 4
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_QUESTION_CHARS = 120_000

_FILE_ID = re.compile(r"^F[A-Z0-9]{6,}$")
_DOWNLOAD_HOSTS = frozenset({"files.slack.com"})


@dataclass(frozen=True)
class QuestionFileEvidence:
    lines: tuple[str, ...] = ()
    citations: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()
    files_read: int = 0


def _https_url(value: object, *, hosts: frozenset[str]) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or parsed.username or parsed.password or port:
        return ""
    if (parsed.hostname or "").lower() not in hosts:
        return ""
    return raw


def _permalink(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username or parsed.password or port:
        return ""
    if host != "slack.com" and not host.endswith(".slack.com"):
        return ""
    return raw


def read_question_files(
    files: object,
    bot_token: str,
    *,
    loader: Callable[[list[dict], str], tuple[list[str], list[str]]] = file_lines,
) -> QuestionFileEvidence:
    """Read bounded event attachments without storing them in the archive.

    The caller must pass only the ``files`` array from the already validated
    human question event. TYBot and VIPBot share this boundary. URLs are
    restricted before the bot token reaches the existing download/convert
    helper.
    """
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        return QuestionFileEvidence(codes=("files_invalid",))
    if not files:
        return QuestionFileEvidence()
    if not bot_token:
        return QuestionFileEvidence(codes=("file_token_missing",))
    if len(files) > MAX_QUESTION_FILES:
        return QuestionFileEvidence(codes=("file_count_exceeded",))

    accepted: list[dict] = []
    citations: list[str] = []
    seen_ids: set[str] = set()
    total = 0
    for row in files:
        if not isinstance(row, Mapping):
            return QuestionFileEvidence(codes=("file_metadata_invalid",))
        file_id = str(row.get("id") or "").strip().upper()
        if not _FILE_ID.fullmatch(file_id):
            return QuestionFileEvidence(codes=("file_identity_invalid",))
        if file_id in seen_ids:
            return QuestionFileEvidence(codes=("file_identity_duplicate",))
        seen_ids.add(file_id)
        try:
            size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            return QuestionFileEvidence(codes=("file_size_invalid",))
        if size < 1 or size > MAX_FILE_BYTES:
            return QuestionFileEvidence(codes=("file_size_exceeded",))
        total += size
        if total > MAX_TOTAL_BYTES:
            return QuestionFileEvidence(codes=("file_total_exceeded",))
        download = _https_url(
            row.get("url_private_download") or row.get("url_private"),
            hosts=_DOWNLOAD_HOSTS,
        )
        if not download:
            return QuestionFileEvidence(codes=("file_url_invalid",))
        normalized = dict(row)
        normalized["id"] = file_id
        normalized["size"] = size
        normalized["url_private_download"] = download
        normalized.pop("url_private", None)
        parsed_file = SlackFile.from_event(normalized)
        if not (parsed_file.is_text or parsed_file.is_convertible):
            return QuestionFileEvidence(codes=("file_type_unsupported",))
        citation = _permalink(row.get("permalink"))
        if not citation:
            return QuestionFileEvidence(codes=("file_permalink_invalid",))
        accepted.append(normalized)
        if citation not in citations:
            citations.append(citation)

    if not accepted:
        return QuestionFileEvidence()
    try:
        lines, warnings = loader(accepted, bot_token)
    except Exception:  # noqa: BLE001 - downloader details must not escape
        return QuestionFileEvidence(codes=("file_read_failed",))
    clean_lines = tuple(line for line in lines if isinstance(line, str) and line.strip())
    text_chars = sum(len(line) for line in clean_lines) + max(0, len(clean_lines) - 1)
    if text_chars > MAX_QUESTION_CHARS:
        return QuestionFileEvidence(codes=("file_text_exceeded",))
    codes = ("file_read_partial",) if warnings else ()
    if not clean_lines:
        codes = ("file_read_empty",)
    return QuestionFileEvidence(
        lines=clean_lines,
        citations=tuple(citations),
        codes=codes,
        files_read=len(accepted),
    )


__all__ = [
    "MAX_FILE_BYTES",
    "MAX_QUESTION_CHARS",
    "MAX_QUESTION_FILES",
    "MAX_TOTAL_BYTES",
    "QuestionFileEvidence",
    "read_question_files",
]
