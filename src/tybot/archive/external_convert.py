"""Optional high-fidelity converters owned by the TYBot ingestion layer.

The bundled Hermes reference used ``kordoc`` and LibreOffice operationally.  We
keep those programs outside the bot process and invoke only binaries already
installed on the server.  Runtime package downloads (for example ``npx -y``)
are deliberately not allowed in the ingestion path.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


class ExternalConverterUnavailable(RuntimeError):
    """The optional converter is not installed on this host."""
    code = "converter_missing"
    retryable = False


class ExternalConversionError(RuntimeError):
    """An installed converter could not produce a usable document."""

    def __init__(self, message: str, *, code="conversion_failed", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def failure_details(exc: BaseException, *, default="conversion_failed") -> tuple[str, bool]:
    """Preserve typed failures through ConvertError wrappers without storing stderr."""
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        code = getattr(cause, "code", None)
        if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code):
            return code, bool(getattr(cause, "retryable", False))
        cause = cause.__cause__
    return default, False


PROJECT_ROOT = Path(__file__).resolve().parents[3]
HERMES_XLSX = PROJECT_ROOT / "vendor" / "hermes" / "xlsx_to_blocks.py"
COMMAND_TIMEOUT = 120
MIN_DOCUMENT_CHARS = 200
IMAGE_MARK = re.compile(r"!\[image\]\([^)]*\)")
SYSTEM_BINARY_DIRS = (
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/bin"),
)


def _installed_binary(name: str) -> str | None:
    """Find an installed converter even when sudo/systemd supplies a narrow PATH."""
    found = shutil.which(name)
    if found:
        return found
    if Path(name).name != name:
        return None
    for directory in SYSTEM_BINARY_DIRS:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _binary(env_name: str, *names: str) -> str:
    configured = os.getenv(env_name, "").strip()
    if configured:
        path = Path(configured)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        found = _installed_binary(configured)
        if found:
            return found
        raise ExternalConverterUnavailable(f"{env_name} 실행 파일을 찾지 못했습니다")
    for name in names:
        found = _installed_binary(name)
        if found:
            return found
    raise ExternalConverterUnavailable(f"실행 파일을 찾지 못했습니다: {', '.join(names)}")


def _run(
    command: list[str],
    *,
    cwd: Path,
    home: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if home is not None:
        environment["HOME"] = str(home)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise ExternalConversionError(
            "문서 변환기를 실행하지 못했습니다",
            code="converter_start_failed",
        ) from exc
    try:
        stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.communicate()
        raise ExternalConversionError(
            f"문서 변환이 {COMMAND_TIMEOUT}초를 초과했습니다",
            code="converter_timeout", retryable=True,
        ) from exc
    if process.returncode != 0:
        code = "converter_crashed" if process.returncode < 0 else "conversion_failed"
        # A V8 fatal message is a crash, not proof of OOM or a particular policy.
        if "Fatal error" in stderr or "V8_Fatal" in stderr:
            code = "converter_crashed"
        raise ExternalConversionError(
            f"변환기 비정상 종료 code={code} exit={process.returncode}", code=code,
        )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


# 시트 하나가 이보다 크면 벤더 스크립트가 **그 시트를 통째로 버린다.**
# 사실상 무제한으로 둔다 — 잘린 것보다 통째로 없어지는 쪽이 훨씬 나쁘고,
# 없어진 사실은 답변에 드러나지 않는다.
XLSX_MAX_ROWS_DEFAULT = 10_000_000


def _xlsx_max_rows() -> int:
    """벤더 변환기에 넘길 행 상한. 0 이나 읽을 수 없는 값이면 기본값."""
    import os

    raw = (os.getenv("TYBOT_XLSX_MAX_ROWS") or "").strip()
    if not raw:
        return XLSX_MAX_ROWS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return XLSX_MAX_ROWS_DEFAULT
    return value if value > 0 else XLSX_MAX_ROWS_DEFAULT


def xlsx_lines(data: bytes) -> list[str]:
    """Run the reviewed Hermes spreadsheet renderer and return searchable lines."""
    if not HERMES_XLSX.is_file():
        raise ExternalConverterUnavailable("TYBot Excel 정밀 변환기가 설치되지 않았습니다")

    with tempfile.TemporaryDirectory(prefix="tybot-xlsx-") as raw:
        work = Path(raw)
        source = work / "attachment.xlsx"
        output = work / "out"
        source.write_bytes(data)
        result = _run(
            [
                sys.executable,
                str(HERMES_XLSX),
                "--file",
                str(source),
                "--date",
                "1970-01-01",
                "--source",
                "attachment.xlsx",
                "--out-dir",
                str(output),
                # **1,000 은 자르는 값이 아니라 버리는 값이었다.** 벤더 스크립트는
                # 행 수가 넘으면 그 시트를 통째로 건너뛴다(`build_blocks`). 우리
                # 정산서·기성 표는 대부분 그보다 크고, 그래서 시트가 통째로
                # 사라진 채 「변환 성공」 이 됐다(2026-09-14 확인).
                #
                # 변환 단계에서는 자르지 않는다 — 자르면 아카이브에 영구히
                # 없어진다. 값은 비상용으로만 환경변수에 남긴다.
                "--max-rows",
                str(_xlsx_max_rows()),
            ],
            cwd=work,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:300]
            raise ExternalConversionError(f"Excel 정밀 변환 실패: {detail or result.returncode}")

        metadata_path = output / "meta.json"
        sheets = sorted(output.glob("sheet-*.md"))
        if not sheets or not metadata_path.is_file():
            raise ExternalConversionError("Excel 정밀 변환 결과가 없습니다")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        names = list(metadata.get("sheets") or [])

        lines: list[str] = []
        for index, path in enumerate(sheets):
            name = names[index] if index < len(names) else path.stem
            lines.append(f"[시트] {name}")
            body = path.read_text(encoding="utf-8").splitlines()
            if body and body[0].startswith("**1970-01-01"):
                body = body[1:]
            lines.extend(line.strip() for line in body if line.strip())

        for key, label in (
            ("hidden_sheets", "숨긴 시트 제외"),
            # **이 줄이 뜨면 시트가 통째로 없어진 것이다.** 잘린 것이 아니다.
            ("skipped_sheets", "행 상한 초과로 시트 통째 제외"),
            ("unmaskable_sheets", "개인정보 안전 판정 불가 시트 제외"),
            ("folded_date_sheets", "이전 날짜 시트 접음"),
        ):
            value = metadata.get(key)
            if value:
                lines.append(f"[변환 안내] {label}: {json.dumps(value, ensure_ascii=False)}")
        return lines


def kordoc_lines(data: bytes, suffix: str, *, force_ocr: bool = False) -> list[str]:
    """Convert one document with a preinstalled kordoc CLI."""
    kordoc = _binary("KORDOC_BIN", "kordoc")
    with tempfile.TemporaryDirectory(prefix="tybot-kordoc-") as raw:
        work = Path(raw)
        source = work / f"source.{suffix.lstrip('.').lower()}"
        output = work / "out"
        output.mkdir()
        source.write_bytes(data)
        command = [kordoc, "--silent", "-d", str(output)]
        if force_ocr:
            command.append("--ocr-force")
        command.append(str(source))
        state_dir = Path(os.getenv("STATE_DIR", "").strip() or "/var/lib/tybot")
        home = state_dir if state_dir.is_dir() and os.access(state_dir, os.W_OK) else work
        result = _run(command, cwd=work, home=home)
        made = sorted(output.glob("*.md"))
        if result.returncode != 0 or len(made) != 1:
            detail = (result.stderr or result.stdout).strip()[:300]
            raise ExternalConversionError(f"kordoc 변환 실패: {detail or '출력 문서 없음'}")
        text = made[0].read_text(encoding="utf-8", errors="replace")
        visible = IMAGE_MARK.sub("", text).strip()
        if not visible:
            raise ExternalConversionError("kordoc 결과에 읽을 수 있는 본문이 없습니다")
        lines = [line.strip() for line in visible.splitlines() if line.strip()]
        if force_ocr:
            lines.insert(0, "[변환 안내] OCR 사용 - 금액·날짜·조문은 원본 확인 필요")
        return lines


def office_pdf_lines(data: bytes, suffix: str) -> list[str]:
    """Render an office document to PDF, then parse its visual representation."""
    soffice = _binary("LIBREOFFICE_BIN", "soffice", "libreoffice")
    with tempfile.TemporaryDirectory(prefix="tybot-office-") as raw:
        work = Path(raw)
        source = work / f"source.{suffix.lstrip('.').lower()}"
        output = work / "out"
        profile = work / "profile"
        output.mkdir()
        profile.mkdir()
        source.write_bytes(data)
        result = _run(
            [
                soffice,
                f"-env:UserInstallation={profile.resolve().as_uri()}",
                "--headless",
                "--norestore",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output),
                str(source),
            ],
            cwd=work,
            home=work,
        )
        pdf = output / "source.pdf"
        if result.returncode != 0 or not pdf.is_file():
            detail = (result.stderr or result.stdout).strip()[:300]
            raise ExternalConversionError(f"LibreOffice PDF 변환 실패: {detail or '출력 없음'}")
        pdf_data = pdf.read_bytes()
        try:
            lines = kordoc_lines(pdf_data, "pdf")
        except ExternalConversionError:
            return kordoc_lines(pdf_data, "pdf", force_ocr=True)
        chars = len(IMAGE_MARK.sub("", "\n".join(lines)).strip())
        return kordoc_lines(pdf_data, "pdf", force_ocr=True) if chars < MIN_DOCUMENT_CHARS else lines
