"""보관 HWP 원본을 **여러 로컬 변환기로 읽어 비교**한다. 읽기 전용.

범위: [`docs/verification/2026-10-20-companion-app-rollout.md`](../../../docs/verification/2026-10-20-companion-app-rollout.md)
「Claude assignment: historical HWP recovery」

## 이 모듈이 하는 일

같은 원본 바이트를 **후보 변환기마다** 돌려 쪽·표·글자 수를 나란히 놓는다.
어느 경로로 복구할지 고르기 전에 **무엇이 더 많이 읽히는지 숫자로** 봐야 하고,
그 숫자가 없으면 「되는 것 같다」 로 정본이 정해진다.

| 한다 | 하지 않는다 |
|---|---|
| 후보 변환기 실행 · 산출 PDF 추출 비교 | metadata·정본·색인 쓰기 |
| 변환기 신원(실행 파일 해시·버전) 기록 | 원본·실패 기록 수정 |
| 쪽별 글자·이미지 개수 | 외부 전송 · 모델 호출 |

**아무것도 발행하지 않는다.** 임시 디렉터리에서 돌고 결과는 숫자와 해시뿐이다.
사람이 표·숫자를 눈으로 맞추려면 호출부가 따로 덤프를 내려받아야 한다.

## 로컬 변환기만 받는다

후보는 **이 서버 안에서 끝나는 것**만이다. 보관 HWP 는 사내 문서이고, 외부
변환 서버로 올리는 순간 그것은 변환이 아니라 **전송**이다 — 전송은 provider·
모델·쪽·예산·PII 승인을 거쳐야 한다(B-80 / WC-21 §3).

2026-10-20 확인: `rhwp` 는 **로컬 CLI 가 아니다.** `rhwp for Slack` 은 호스팅
앱이고 변환 서버가 문서를 내려받아 처리한다. `@rhwp/core` 는 Rust·WASM 렌더링
라이브러리다. 그래서 이 모듈은 **특정 변환기를 박지 않고** 설정으로 받는다 —
검증된 로컬 변환기가 정해지면 그때 후보로 넣는다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tybot.archive.hwp_candidates")

OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")

#: 후보 변환기 하나에 주는 시간. 파이프라인 상한과 같게 둔다.
CANDIDATE_TIMEOUT = 180

#: 변환기가 stderr 에 뱉는 것 중 **진단에 쓰는 표식만** 꺼낸다.
#:
#: 변환기는 처리 중인 문단·셀 값을 오류 메시지에 실어 보낸다. 꼬리를 남기면
#: 어디를 자르느냐가 운이 되고, 그 운이 운영 로그로 간다(원칙 5).
STDERR_MARKERS = (
    ("unsupported", ("Unsupported", "not supported", "Unknown format", "HWP3")),
    ("encrypted", ("encrypted", "password", "DRM")),
    ("missing_filter", ("no export filter", "Error: source file could not be loaded")),
    ("oom", ("std::bad_alloc", "Cannot allocate memory", "ENOMEM",
             "JavaScript heap out of memory")),
    ("crash", ("Segmentation fault", "V8_Fatal", "FATAL ERROR:")),
    ("permission", ("EACCES", "Permission denied")),
    ("timeout_hint", ("timed out", "Timeout")),
)


class CandidateUnavailable(RuntimeError):
    """그 후보 변환기가 이 서버에 없다. **실패가 아니라 미설치다.**"""


def stderr_markers(text: str) -> tuple[str, ...]:
    """허용 목록 표식만. 문서 본문은 통과하지 못한다."""
    found: list[str] = []
    for code, needles in STDERR_MARKERS:
        if any(needle in text for needle in needles) and code not in found:
            found.append(code)
    return tuple(found)


@dataclass(frozen=True)
class Candidate:
    """후보 변환기 하나의 **설정**. 코드에 명령을 박지 않는다.

    `command` 는 `{src}` 와 `{outdir}` 를 쓰는 템플릿이다. 새 변환기를 붙일 때
    이 모듈을 고치지 않아도 되고, 고치지 않으면 **다른 후보의 측정이 바뀌지
    않는다** — 비교는 같은 조건에서만 의미가 있다.
    """

    name: str
    command: str
    #: 신원 확인에 쓸 명령. 응답이 없으면 그 후보는 쓰지 않는다.
    version_command: str = ""
    #: 산출 PDF 이름. 비우면 출력 디렉터리에서 `*.pdf` 하나를 찾는다.
    output: str = ""


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _argv(template: str, *, src: Path | str = "", outdir: Path | str = "") -> list[str]:
    r"""명령 템플릿 → argv. **경로를 문자열로 끼워 넣지 않는다.**

    먼저 토큰으로 쪼개고 그 다음에 치환한다. 끼워 넣고 쪼개면 경로에 공백이나
    백슬래시가 있을 때 토큰이 갈라진다 — 개발 PC(Windows)에서 실제로 그랬다.
    `shlex` 는 기본이 POSIX 규칙이라 `C:\Users\…` 의 백슬래시를 먹는다.

    템플릿 자체는 **쉘 규칙**으로 읽는다(공백 있는 경로는 따옴표로 묶는다).
    쉘을 거치지는 않으므로 리다이렉션·파이프는 동작하지 않는다 — 의도한 것이다.
    """
    tokens = shlex.split(template)
    return [
        token.replace("{src}", str(src)).replace("{outdir}", str(outdir))
        for token in tokens
    ]


def _identity(candidate: Candidate, work: Path) -> dict:
    """무엇이 돌았나. **신원을 못 세우면 후보로 쓰지 않는다.**

    변환기가 바뀌면 같은 원본이 다른 결과를 낸다. 신원을 안 적으면 그 차이가
    어디서 왔는지 나중에 아무도 못 짚는다.
    """
    argv = _argv(candidate.version_command or candidate.command)
    if not argv:
        raise CandidateUnavailable(f"{candidate.name}: 실행할 명령이 없습니다")
    binary = _resolve(argv[0])
    # 실행 파일을 못 여는 경우가 있다 — 보호된 경로의 심 링크·reparse point 가
    # 그렇다. **그 하나 때문에 후보 전체를 못 재면 안 된다.** 못 읽었다는 사실을
    # 적고 다른 신원(버전 배너)으로 버틴다.
    out: dict = {"executable": argv[0]}
    try:
        out["executable_sha256"] = _hash_file(Path(binary))
    except OSError as exc:
        out["executable_sha256"] = ""
        out["executable_hash_error"] = type(exc).__name__
    if not candidate.version_command:
        if not out["executable_sha256"]:
            # 해시도 배너도 없다. **무엇이 돌았는지 전혀 모른다** — 쓰지 않는다.
            raise CandidateUnavailable(f"{candidate.name}: 변환기 신원을 세우지 못했습니다")
        return out
    try:
        result = subprocess.run(
            [binary, *_argv(candidate.version_command)[1:]],
            cwd=work, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, check=False,
        )
    except OSError as exc:
        raise CandidateUnavailable(f"{candidate.name}: {type(exc).__name__}") from exc
    banner = (result.stdout or result.stderr or "").strip()
    if not banner:
        raise CandidateUnavailable(f"{candidate.name}: 버전을 확인하지 못했습니다")
    # **배너 원문을 싣지 않는다.** 경로·사용자 이름이 섞여 나오는 변환기가 있다.
    out["version_sha256"] = hashlib.sha256(banner.encode()).hexdigest()
    out["version_line"] = banner.splitlines()[0][:80]
    return out


def _resolve(name: str) -> str:
    """실행 파일 위치. systemd 의 좁은 PATH 에서도 찾는다."""
    from .external_convert import _installed_binary

    if Path(name).is_file() and os.access(name, os.X_OK):
        return name
    found = _installed_binary(name)
    if not found:
        raise CandidateUnavailable(f"실행 파일을 찾지 못했습니다: {name}")
    return found


@dataclass
class CandidateResult:
    """후보 하나의 결과. **본문은 담지 않는다.**"""

    name: str
    available: bool = False
    skipped: str = ""
    exit_code: int | None = None
    signal_name: str = ""
    timed_out: bool = False
    seconds: float = 0.0
    stderr_markers: tuple[str, ...] = ()
    stderr_lines: int = 0
    identity: dict = field(default_factory=dict)
    #: 산출 PDF
    pdf_sha256: str = ""
    pdf_bytes: int = 0
    pages: int | None = None
    encrypted: bool | None = None
    #: 우리 PDF 경로(`convert("pdf")`)로 읽은 결과
    lines: int | None = None
    chars: int | None = None
    table_rows: int | None = None
    coverage_state: str = ""
    coverage_converted: int | None = None
    coverage_total: int | None = None
    missing_pages: tuple[int, ...] = ()
    page_chars: tuple[int, ...] = ()
    error: str = ""


def _run_candidate(candidate: Candidate, source: Path, outdir: Path,
                   work: Path, result: CandidateResult) -> Path | None:
    argv = _argv(candidate.command, src=source, outdir=outdir)
    argv[0] = _resolve(argv[0])
    env = os.environ.copy()
    env["HOME"] = str(work)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, cwd=work, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        result.error = type(exc).__name__
        return None
    try:
        _out, err = proc.communicate(timeout=CANDIDATE_TIMEOUT)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            import contextlib
            import signal as _signal

            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, _signal.SIGKILL)
        else:
            proc.kill()
        _out, err = proc.communicate()
        result.timed_out = True
    result.seconds = round(time.monotonic() - started, 1)
    result.exit_code = proc.returncode
    if proc.returncode is not None and proc.returncode < 0:
        import signal as _signal

        try:
            result.signal_name = _signal.Signals(-proc.returncode).name
        except ValueError:
            result.signal_name = f"signal {-proc.returncode}"
    raw = (err or "").strip()
    result.stderr_markers = stderr_markers(raw)
    result.stderr_lines = len(raw.splitlines())
    if candidate.output:
        made = outdir / candidate.output
        return made if made.is_file() else None
    found = sorted(outdir.glob("*.pdf"))
    return found[0] if len(found) == 1 else None


def _measure_pdf(pdf: bytes, result: CandidateResult) -> None:
    """산출 PDF 를 **우리 경로로** 읽는다.

    변환기가 PDF 를 냈다는 것과 우리가 그것을 읽는다는 것은 다르다. 복구에
    실제로 쓰일 경로로 재어야 비교가 의미를 갖는다.
    """
    import io

    from pypdf import PdfReader

    from .convert import ConvertError, convert_with_coverage

    result.pdf_sha256 = hashlib.sha256(pdf).hexdigest()
    result.pdf_bytes = len(pdf)
    try:
        reader = PdfReader(io.BytesIO(pdf))
        result.encrypted = bool(reader.is_encrypted)
        result.pages = len(reader.pages)
        per: list[int] = []
        for page in reader.pages:
            try:
                per.append(len((page.extract_text() or "").strip()))
            except Exception:  # noqa: BLE001 - 한 쪽이 실패해도 나머지를 센다
                per.append(0)
        result.page_chars = tuple(per)
    except Exception as exc:  # noqa: BLE001 - 읽지 못한 것도 결과다
        result.error = type(exc).__name__
        return
    try:
        lines, coverage = convert_with_coverage("pdf", pdf)
    except ConvertError as exc:
        result.error = type(exc).__name__
        return
    result.lines = len(lines)
    result.chars = sum(len(x) for x in lines)
    result.table_rows = sum(1 for ln in lines if ln.strip().startswith("|"))
    result.coverage_state = coverage.state
    result.coverage_converted = coverage.converted
    result.coverage_total = coverage.total
    result.missing_pages = tuple(coverage.missing[:50])


def run_candidate(candidate: Candidate, data: bytes, *,
                  keep_pdf: Path | None = None) -> CandidateResult:
    """후보 하나를 돌린다. **예외를 밖으로 내지 않는다.**

    미설치·실패·시간 초과도 결과 객체로 돌려줘야 비교표에서 그 자리가 빈 칸이
    아니라 사유로 남는다. 빈 칸은 「안 해 봤다」 와 「안 된다」 를 섞는다.
    """
    result = CandidateResult(name=candidate.name)
    if not data.startswith(OLE2_MAGIC):
        result.skipped = "OLE2 컨테이너가 아닙니다"
        return result
    with tempfile.TemporaryDirectory(prefix="tybot-hwp-cand-") as raw:
        work = Path(raw)
        outdir = work / "out"
        outdir.mkdir()
        source = work / "source.hwp"
        source.write_bytes(data)
        try:
            result.identity = _identity(candidate, work)
        except CandidateUnavailable as exc:
            result.skipped = str(exc)
            return result
        result.available = True
        made = _run_candidate(candidate, source, outdir, work, result)
        if made is None:
            if not result.error:
                result.error = "pdf_missing"
            return result
        pdf = made.read_bytes()
        _measure_pdf(pdf, result)
        if keep_pdf is not None:
            keep_pdf.parent.mkdir(parents=True, exist_ok=True)
            keep_pdf.write_bytes(pdf)
    logger.info(
        "hwp_candidate name=%s exit=%s signal=%s pages=%s chars=%s tables=%s "
        "coverage=%s/%s markers=%s",
        result.name, result.exit_code, result.signal_name or "-", result.pages,
        result.chars, result.table_rows, result.coverage_converted,
        result.coverage_total, ",".join(result.stderr_markers) or "-",
    )
    return result


def direct_extraction(data: bytes, filetype: str = "hwp") -> dict:
    """지금 파이프라인 경로(`convert("hwp")`). **비교의 기준선이다.**"""
    from .convert import ConvertError, convert_with_coverage

    try:
        lines, coverage = convert_with_coverage(filetype, data)
    except ConvertError as exc:
        return {"failed": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001
        return {"failed": type(exc).__name__}
    return {
        "lines": len(lines),
        "chars": sum(len(x) for x in lines),
        "table_rows": sum(1 for ln in lines if ln.strip().startswith("|")),
        "coverage_state": coverage.state,
        "coverage_converted": coverage.converted,
        "coverage_total": coverage.total,
    }


# --- 후보 설정 ------------------------------------------------------------------
#
# **코드에 변환기를 박지 않는다.** 검증된 로컬 변환기가 무엇인지는 서버마다
# 다르고, 박아 두면 새 후보를 넣을 때마다 이 파일이 바뀌어 **이전 측정과 조건이
# 달라진다.**

#: 기본 후보. LibreOffice 는 지금 복구 경로가 쓰는 것이라 기준선으로 늘 넣는다.
DEFAULT_CANDIDATES = (
    Candidate(
        name="libreoffice",
        command=(
            "soffice --headless --norestore --convert-to pdf --outdir {outdir} {src}"
        ),
        version_command="soffice --version",
        output="source.pdf",
    ),
)

#: 추가 후보를 넣는 환경변수. JSON 배열이다.
#:
#: ```json
#: [{"name": "h2orestart", "command": "soffice --headless --convert-to pdf …",
#:   "version_command": "soffice --version", "output": "source.pdf"}]
#: ```
#:
#: **로컬에서 끝나는 변환기만 넣는다.** 외부 서버로 올리는 것은 변환이 아니라
#: 전송이고, 전송은 승인 경계를 지나야 한다.
CANDIDATES_ENV = "TYBOT_HWP_CANDIDATES"


def configured_candidates(raw: str | None = None) -> tuple[Candidate, ...]:
    """기본 후보 + 설정에서 받은 후보. 설정이 깨졌으면 **기본만** 쓴다."""
    text = (raw if raw is not None else os.getenv(CANDIDATES_ENV, "")).strip()
    if not text:
        return DEFAULT_CANDIDATES
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("%s 를 읽지 못해 기본 후보만 씁니다", CANDIDATES_ENV)
        return DEFAULT_CANDIDATES
    if not isinstance(rows, list):
        logger.warning("%s 가 배열이 아닙니다. 기본 후보만 씁니다", CANDIDATES_ENV)
        return DEFAULT_CANDIDATES
    extra: list[Candidate] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name") or not row.get("command"):
            logger.warning("후보 설정 한 건을 건너뜁니다(name·command 필요)")
            continue
        extra.append(Candidate(
            name=str(row["name"]),
            command=str(row["command"]),
            version_command=str(row.get("version_command") or ""),
            output=str(row.get("output") or ""),
        ))
    return (*DEFAULT_CANDIDATES, *extra)


__all__ = [
    "CANDIDATES_ENV",
    "CANDIDATE_TIMEOUT",
    "DEFAULT_CANDIDATES",
    "Candidate",
    "CandidateResult",
    "CandidateUnavailable",
    "configured_candidates",
    "direct_extraction",
    "run_candidate",
    "stderr_markers",
]
