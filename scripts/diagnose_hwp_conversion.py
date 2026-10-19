"""HWP 변환 실패 진단 — `converter_crashed` 가 실제로 **무엇이었나** (B-80).

2026-10-19 tyit `C0C9A86JUSJ` 의 업무보고 두 건이 `conversion_state=failed` ·
`error_code=converter_crashed` 로 남았다. 기준월 사이의 진척을 비교할 수 없었다.

## 왜 metadata 만 보면 알 수 없나

`converter_crashed` 는 **두 가지 전혀 다른 일**에 붙는다
(`archive/external_convert.py::_run`).

```python
code = "converter_crashed" if process.returncode < 0 else "conversion_failed"
if "Fatal error" in stderr or "V8_Fatal" in stderr:
    code = "converter_crashed"
```

1. **신호로 죽었다**(`returncode < 0`). SIGSEGV 면 파서가 터진 것이고,
   SIGKILL 이면 대개 OOM killer 다. 둘은 사람이 할 일이 전혀 다르다.
2. **stderr 에 V8 fatal 이 있었다.** kordoc 은 Node 프로그램이라 JS 힙이 모자라면
   이 문구가 난다. 다만 코드 주석이 적어 둔 대로, **그 문구가 OOM 의 증거는 아니다.**

그런데 **신호 번호도 stderr 도 어디에도 저장되지 않는다.** metadata 에는
`error_code` 만 남는다. 그래서 저장된 것만으로는 어느 쪽인지 영원히 모르고,
**다시 돌려 봐야** 한다. 이 스크립트가 그 일을 한다.

## 무엇을 하나

| 단계 | 보는 것 |
|---|---|
| 1. 원본 찾기 | staging metadata 로 좌표를 맞춘다. 원본이 안 남아 있으면 거기서 끝 |
| 2. 실제 컨테이너 | 확장자를 믿지 않는다. OLE2(HWP 5) · ZIP(HWPX) · HWP 3.0 · 엉뚱한 형식 |
| 3. 암호·배포용 | HWP 5 FileHeader 속성 비트. 암호 문서면 어떤 변환기도 못 연다 |
| 4. kordoc 재현 | 파이프라인과 **같은 명령**으로 돌려 종료 신호·stderr 꼬리·최대 RSS 를 본다 |
| 5. LibreOffice 렌더 | HWP → PDF 가 되는가. 된 PDF 의 쪽 수와 텍스트 레이어까지 확인 |

## 하지 않는 것

**아무것도 고치지 않는다.** 아카이브·metadata·DB 를 쓰지 않고, 변환본을 발행하지
않는다. 작업 파일은 전부 임시 디렉터리에 두고 끝나면 지운다.

LibreOffice 가 설치돼 있다는 사실만으로 **그 HWP 판의 지원을 단정하지 않는다** —
실제로 돌려서 PDF 가 나왔는지, 나온 PDF 에 쪽과 글자가 있는지까지 본다.
이름만 바꾼 것은 변환이 아니다.

## 서버에서

```bash
sudo -u tybot TYBOT_ENV_FILE=/etc/tybot/tybot.env \\
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/diagnose_hwp_conversion.py \\
  --archive /var/lib/tybot/archive --file-id F0C9LHFTHLM --file-id F0C9VH39NTW
```

`--render` 를 붙이면 LibreOffice 렌더링까지 돌린다(오래 걸릴 수 있다).
`--json` 은 기계가 읽을 모양으로 찍는다. **본문은 어느 모양에도 넣지 않는다.**
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 파이프라인이 kordoc 에 거는 상한과 같다. 다르게 두면 진단이 다른 조건을 잰다.
COMMAND_TIMEOUT = 120

# 변환기 stderr 에서 **이 목록에 있는 것만** 꺼낸다.
#
# 꼬리 몇 줄만 남기는 것으로는 부족하다. 변환기는 처리 중인 문단·셀 값을 오류
# 메시지에 그대로 실어 보내고, 그 줄이 어디에 올지는 우리가 정할 수 없다.
# 꼬리를 자르면 **어디를 자르느냐가 운이 된다.**
#
# 그래서 원시 stderr 를 출력에 싣지 않는다. 진단에 쓰는 표식만 **있나 없나**로
# 바꿔 싣는다. 이름은 전부 변환기·런타임이 내는 고정 문자열이라 문서 내용이
# 섞일 수 없다.
DIAGNOSTIC_MARKERS = (
    ("v8_fatal", ("V8_Fatal", "FATAL ERROR:")),
    ("js_heap_oom", ("JavaScript heap out of memory",)),
    ("segfault", ("Segmentation fault", "SIGSEGV")),
    ("bad_alloc", ("std::bad_alloc", "Cannot allocate memory", "ENOMEM")),
    ("permission", ("EACCES", "Permission denied")),
    ("missing_file", ("ENOENT", "No such file")),
    ("unsupported", ("Unsupported", "not supported", "Unknown format")),
    ("node_stack", ("node:internal", "at Object.<anonymous>")),
    ("java_exception", ("Exception in thread", "java.lang.")),
)


def stderr_codes(text: str) -> tuple[str, ...]:
    """stderr 에서 **알고 있는 표식만** 고른다. 본문은 통과하지 못한다."""
    found: list[str] = []
    for code, needles in DIAGNOSTIC_MARKERS:
        if any(needle in text for needle in needles) and code not in found:
            found.append(code)
    return tuple(found)

# --- 컨테이너 판별 ------------------------------------------------------------
#
# **확장자를 믿지 않는다.** `.hwp` 로 올라온 것이 실제로는 HWPX(zip)이거나 PDF 인
# 경우가 있고, 그때 kordoc 은 엉뚱한 파서로 들어가 터진다. 이름만 바꾼 것은
# 변환이 아니다(분담 §7).

OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
HWP5_SIGNATURE = b"HWP Document File"
HWP3_SIGNATURE = b"HWP Document File V3.00"

CONTAINERS = {
    "hwp5": "HWP 5.x (OLE2 복합 문서)",
    "hwpx": "HWPX (OOXML 계열 zip)",
    "hwp3": "HWP 3.0 (구형 단일 파일)",
    "zip": "zip 이지만 HWPX 구조가 아님",
    "ole2": "OLE2 이지만 HWP 서명 없음 (doc·xls 일 수 있음)",
    "pdf": "PDF (확장자만 hwp)",
    "unknown": "알 수 없음",
}

# HWP 5.0 FileHeader 속성 비트. 서명 뒤 버전 4바이트 다음이 속성 4바이트다.
HWP5_PROPERTY_OFFSET = 36
HWP5_FLAGS = (
    (0, "compressed", "압축됨"),
    (1, "password", "**암호 설정** — 어떤 변환기도 열 수 없다"),
    (2, "distribution", "**배포용 문서** — 내용이 잠겨 있다"),
    (3, "script", "스크립트 포함"),
    (4, "drm", "**DRM 보안 문서**"),
    (5, "xml_template", "XML 템플릿 포함"),
    (6, "history", "문서 이력 포함"),
    (7, "signed", "전자 서명 있음"),
    (8, "certificate_encrypted", "**공인 인증서 암호화**"),
)


def sniff_container(head: bytes, whole: bytes | None = None) -> str:
    """앞부분 바이트로 실제 컨테이너를 고른다. 모르면 `unknown`."""
    if head.startswith(HWP3_SIGNATURE):
        return "hwp3"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(OLE2_MAGIC):
        body = whole if whole is not None else head
        return "hwp5" if HWP5_SIGNATURE in body else "ole2"
    if head.startswith(b"PK\x03\x04"):
        body = whole if whole is not None else head
        return "hwpx" if (b"Contents/" in body or b"hwpml" in body.lower()) else "zip"
    return "unknown"


def hwp5_flags(data: bytes) -> tuple[dict[str, bool], str]:
    """HWP 5 FileHeader 속성. 못 읽으면 빈 표와 사유.

    **정식 OLE2 파싱이 아니다.** `olefile` 이 의존성에 없어서 서명 위치를 찾아
    그 자리를 읽는 어림이다 — 그래서 결과를 「확정」 으로 쓰지 않고, 암호·배포용이
    **의심되면** 그때 사람이 원본을 열어 확인한다.
    """
    at = data.find(HWP5_SIGNATURE)
    if at == -1:
        return {}, "HWP 5 서명을 찾지 못했습니다"
    start = at + HWP5_PROPERTY_OFFSET
    if start + 4 > len(data):
        return {}, "FileHeader 가 잘려 있습니다"
    value = int.from_bytes(data[start : start + 4], "little")
    return {name: bool(value >> bit & 1) for bit, name, _ in HWP5_FLAGS}, ""


# --- 외부 변환기 실행 ----------------------------------------------------------


@dataclass
class RunReport:
    """한 번 돌린 결과. **본문은 담지 않는다** — 종료 상태와 계기판뿐이다."""

    ran: bool = False
    skipped: str = ""
    exit_code: int | None = None
    signal_name: str = ""
    timed_out: bool = False
    #: stderr 에서 고른 **진단 표식 이름**. 원시 문자열은 싣지 않는다.
    stderr_codes: tuple[str, ...] = ()
    #: stderr 의 **모양**만. 표식이 하나도 안 걸렸을 때 「뭔가 나오긴 했다」 를
    #: 알 수 있어야 사람이 직접 떠 볼지 판단한다.
    stderr_lines: int = 0
    stderr_chars: int = 0
    #: **이 프로세스가 띄운 자식 전체의 최대 RSS** 다(`RUSAGE_CHILDREN`).
    #: 한 호출에 묶인 값이 아니고 누적 최대라, **이 호출이 그만큼 썼다는 뜻이
    #: 아니다.** 여러 건을 연달아 돌리면 앞 건의 최대치가 그대로 남는다.
    children_max_rss_mb: float | None = None
    seconds: float = 0.0
    outputs: list[str] = field(default_factory=list)
    verdict: str = ""


def _max_rss_mb() -> float | None:
    """`RUSAGE_CHILDREN` 의 최대 RSS. POSIX 만.

    **한 호출의 사용량이 아니다.** 이 프로세스가 띄운 자식 전체의 누적 최대라,
    여러 건을 연달아 돌리면 앞 건의 최대치가 그대로 남는다. 한 건에 묶인 값을
    원하면 그 건만 단독 실행해야 한다.
    """
    try:
        import resource
    except ImportError:
        return None
    try:
        raw = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    except (OSError, ValueError):
        return None
    # Linux 는 KB, macOS 는 byte 로 돌려준다. 큰 쪽을 byte 로 본다.
    return round(raw / 1024 if raw < 10**9 else raw / 1024 / 1024, 1)


def _verdict(report: RunReport) -> str:
    """이번 실행이 **어떻게 끝났나.** 과거 실패의 원인을 말하지 않는다.

    이 스크립트는 **지금** 돌린 결과만 봅니다. 그때 무엇이었는지는 그때 로그에만
    있고, 여기서 그것을 추정해 적으면 그 추정이 사실처럼 인계됩니다.
    """
    if report.timed_out:
        return f"{COMMAND_TIMEOUT}초 안에 끝나지 않아 이쪽에서 중단했다"
    if report.exit_code is None:
        return "실행하지 못했다"
    if report.exit_code == 0:
        return "이번 실행은 exit 0 으로 끝났다. 과거 실패의 원인은 알려 주지 않는다"
    name = report.signal_name
    codes = ", ".join(report.stderr_codes) or "없음"
    if name == "SIGKILL":
        return (
            "SIGKILL 로 끝났다. 바깥에서 죽인 것이고 **누가 죽였는지는 여기서 알 수 "
            "없다** — `journalctl -k` 의 같은 시각을 함께 봐야 한다. "
            f"stderr 표식: {codes}"
        )
    if name == "SIGSEGV":
        return f"SIGSEGV 로 끝났다. 변환기 프로세스가 비정상 종료했다. stderr 표식: {codes}"
    if name == "SIGABRT":
        return f"SIGABRT 로 끝났다. 변환기가 스스로 중단했다. stderr 표식: {codes}"
    if name:
        return f"{name} 신호로 끝났다. stderr 표식: {codes}"
    return f"exit={report.exit_code} 로 끝났다(신호 아님). stderr 표식: {codes}"


def run_converter(command: list[str], *, cwd: Path, env: dict,
                  collect: Path | None = None) -> RunReport:
    """돌리고 **종료 상태를 그대로** 가져온다.

    `external_convert._run` 을 쓰지 않는다. 그쪽은 실패를 예외로 바꾸면서
    **신호 번호와 stderr 를 버린다** — 지금 알고 싶은 것이 바로 그 둘이다.
    """
    import time

    report = RunReport(ran=True)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        report.ran = False
        report.skipped = f"실행하지 못했습니다: {type(exc).__name__}"
        report.verdict = report.skipped
        return report
    try:
        _out, err = proc.communicate(timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            import signal as _signal

            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, _signal.SIGKILL)
        else:
            proc.kill()
        _out, err = proc.communicate()
        report.timed_out = True
    report.seconds = round(time.monotonic() - started, 1)
    report.exit_code = proc.returncode
    if proc.returncode is not None and proc.returncode < 0:
        try:
            import signal as _signal

            report.signal_name = _signal.Signals(-proc.returncode).name
        except (ValueError, AttributeError):
            report.signal_name = f"signal {-proc.returncode}"
    # **원시 stderr 를 싣지 않는다.** 변환기는 처리 중인 문단·셀 값을 오류
    # 메시지에 그대로 실어 보내고, 어디를 잘라도 그것이 남을 수 있다. 알고 있는
    # 표식만 있나 없나로 바꾸고, 나머지는 **모양**만 센다.
    raw = (err or "").strip()
    report.stderr_codes = stderr_codes(raw)
    report.stderr_lines = len(raw.splitlines())
    report.stderr_chars = len(raw)
    report.children_max_rss_mb = _max_rss_mb()
    if collect is not None and collect.is_dir():
        report.outputs = sorted(p.name for p in collect.iterdir())
    report.verdict = _verdict(report)
    return report


# --- 추출 손실 대조 ---------------------------------------------------------------
#
# **「변환 성공」 이 「다 읽었다」 가 아니다.** 2026-10-19 두 건에서 kordoc 직접
# 추출은 4,074자·4,206자였고, 같은 파일을 PDF 로 렌더해 읽으면 47,140자·46,782자가
# 나왔다 — **11배**다. 표는 10행 대 395행이었다.
#
# 그 상태로 재변환하면 **11%짜리 문서가 「변환 성공」 으로 정본이 된다.** 지금의
# 실패보다 나쁘다 — 실패는 보이지만 11% 성공은 아무도 모른다.
#
# 그래서 이 대조를 스크립트에 둔다. 한 번 쓰고 버리는 명령으로 두면 **다른 HWP 의
# 손실 규모를 아무도 재지 않는다.**


def _stats(lines: list[str]) -> dict:
    """줄 목록의 **모양**만 센다. 본문은 담지 않는다."""
    return {
        "lines": len(lines),
        "chars": sum(len(x) for x in lines),
        "table_rows": sum(1 for ln in lines if ln.strip().startswith("|")),
        "page_marks": sum(1 for ln in lines if ln.strip().startswith("[") and "쪽]" in ln),
    }


def _page_kinds(pdf_bytes: bytes, missing) -> dict:
    """미확인 쪽이 **어떤 쪽인가.** 글자 수와 이미지 개수만 센다.

    「이미지만 있는 쪽」 과 「추출이 실패한 쪽」 은 사람이 할 일이 다르다 —
    앞은 Vision 후보이고 뒤는 렌더러 문제다. 다만 여기서 **단정하지 않는다.**
    세는 것은 글자 수와 리소스 개수뿐이고, 무엇이 그려져 있는지는 모른다.
    """
    import io

    out: dict = {}
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:  # noqa: BLE001 - 분류 실패가 진단을 막지 않는다
        return {"error": type(exc).__name__}
    for number in list(missing)[:50]:
        if not 1 <= number <= len(reader.pages):
            out[str(number)] = "쪽 범위 밖"
            continue
        page = reader.pages[number - 1]
        try:
            chars = len((page.extract_text() or "").strip())
        except Exception as exc:  # noqa: BLE001
            out[str(number)] = f"추출 예외({type(exc).__name__})"
            continue
        images = 0
        try:
            resources = page.get("/Resources") or {}
            xobject = resources.get("/XObject")
            if xobject is not None:
                obj = xobject.get_object()
                images = sum(
                    1 for key in obj
                    if str(obj[key].get_object().get("/Subtype", "")) == "/Image"
                )
        except Exception:  # noqa: BLE001 - 개수를 못 세는 것이 실패는 아니다
            images = -1
        out[str(number)] = f"글자 {chars}자 · 이미지 {images if images >= 0 else '?'}개"
    return out


def dump_for_review(dump_dir: Path, fid: str, *, direct, via_pdf, rendered: bytes | None) -> list[str]:
    """사람이 **원본과 대조**할 수 있게 산출물을 파일로 내놓는다.

    표·숫자가 맞는지는 글자 수로 알 수 없다. 사람이 한글 원본을 열고 눈으로
    맞춰야 하고, 그러려면 추출 결과가 **파일로** 있어야 한다.

    **화면에 찍지 않는다.** 이 파일들은 사내 문서 본문이라 운영 로그·인계 문서·
    티켓에 붙이면 안 된다(원칙 5). 경로만 알려 주고 열람은 사람이 한다.
    """
    import re

    if not re.fullmatch(r"F[A-Z0-9]+|attachment", fid):
        raise ValueError("invalid dump file ID")
    if any(path.is_symlink() for path in (dump_dir, *dump_dir.parents)):
        raise ValueError("symlinked dump directories are not allowed")
    dump_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        info = dump_dir.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise PermissionError("dump directory must be owner-only (0700)")

    def write_private(path: Path, data: bytes) -> None:
        # O_EXCL rejects existing files and symlinks; never truncate evidence.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)

    made: list[str] = []
    for name, lines in (("direct", direct), ("via-pdf", via_pdf)):
        if not lines:
            continue
        path = dump_dir / f"{fid}.{name}.md"
        write_private(path, ("\n".join(lines) + "\n").encode("utf-8"))
        made.append(str(path))
    if rendered:
        path = dump_dir / f"{fid}.rendered.pdf"
        write_private(path, rendered)
        made.append(str(path))
    return made


def compare_extraction(data: bytes, suffix: str, work: Path,
                       *, dump_dir: Path | None = None, fid: str = "") -> dict:
    """같은 원본을 **두 경로로** 읽어 비교한다. 아무것도 발행하지 않는다.

    1. 지금 파이프라인 경로(`convert("hwp")` → kordoc)
    2. 복구 후보 경로(LibreOffice → PDF → `convert("pdf")`)

    두 번째가 크게 많으면 **1번이 조용히 잃고 있다**는 뜻이고, 그때 재변환 대상은
    「실패한 파일」 이 아니라 **「성공으로 닫힌 파일 전부」** 가 된다.
    """
    import hashlib

    from tybot.archive.convert import ConvertError, convert_with_coverage

    out: dict = {}
    direct_lines: list[str] = []
    via_lines: list[str] = []
    try:
        lines, cov = convert_with_coverage(suffix, data)
        direct_lines = list(lines)
        out["direct"] = _stats(lines)
        out["direct"]["coverage_state"] = cov.state
        out["direct"]["coverage"] = f"{cov.converted}/{cov.total}{cov.unit or ''}"
    except ConvertError as exc:
        out["direct"] = {"failed": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001 - 대조 실패가 진단을 막지 않는다
        out["direct"] = {"failed": type(exc).__name__}

    report, detail = check_libreoffice(data, suffix, work)
    out["render"] = {"exit_code": report.exit_code, "verdict": report.verdict, **detail}
    pdf = work / "lo-out" / "render.pdf"
    if not pdf.is_file():
        return out
    blob = pdf.read_bytes()
    out["render"]["sha256"] = hashlib.sha256(blob).hexdigest()
    try:
        lines, cov = convert_with_coverage("pdf", blob)
        via_lines = list(lines)
        out["via_pdf"] = _stats(lines)
        out["via_pdf"]["coverage_state"] = cov.state
        out["via_pdf"]["coverage"] = f"{cov.converted}/{cov.total}{cov.unit or ''}"
        out["via_pdf"]["missing_page_count"] = len(cov.missing)
        # **번호를 남긴다.** 개수만 남기면 사람이 어느 쪽을 열어 봐야 하는지
        # 모르고, 그러면 미확인 쪽은 영원히 미확인으로 남는다.
        out["via_pdf"]["missing_pages"] = list(cov.missing[:50])
        out["via_pdf"]["flags"] = list(cov.flags)
        out["via_pdf"]["page_kinds"] = _page_kinds(blob, cov.missing)
    except ConvertError as exc:
        out["via_pdf"] = {"failed": type(exc).__name__}
        if dump_dir is not None:
            out["dumped"] = dump_for_review(
                dump_dir, fid or "attachment",
                direct=direct_lines, via_pdf=[], rendered=blob,
            )
        return out
    direct_chars = out["direct"].get("chars") or 0
    if direct_chars:
        # **추출량 차이다. 누락률이 아니다.**
        #
        # 어느 쪽이 맞는지 이 스크립트는 모른다. PDF 경로는 Markdown 표의 `|` 와
        # 구분선을 더하므로 같은 내용이어도 글자가 는다. 반대로 지금 경로가
        # 중복을 접고 있을 수도 있다. **정확성은 사람이 원본과 대조해야 한다.**
        out["extracted_ratio"] = round(out["via_pdf"]["chars"] / direct_chars, 1)
    if dump_dir is not None:
        out["dumped"] = dump_for_review(
            dump_dir, fid or "attachment",
            direct=direct_lines, via_pdf=via_lines, rendered=blob,
        )
    return out


# --- 단계 -----------------------------------------------------------------------


def _binary(env_name: str, *names: str) -> tuple[str, str]:
    """파이프라인과 **같은 방식**으로 실행 파일을 찾는다. 못 찾으면 사유."""
    try:
        from tybot.archive.external_convert import _binary as find

        return find(env_name, *names), ""
    except Exception as exc:  # noqa: BLE001 - 미설치도 진단 결과다
        return "", f"{type(exc).__name__}: {exc}"


def _kordoc_home(work: Path) -> Path:
    """파이프라인이 kordoc 에 주는 HOME. 다르면 다른 조건을 재게 된다."""
    state = Path(os.getenv("STATE_DIR", "").strip() or "/var/lib/tybot")
    return state if state.is_dir() and os.access(state, os.W_OK) else work


def check_kordoc(data: bytes, suffix: str, work: Path) -> RunReport:
    """`kordoc_lines()` 와 같은 명령으로 재현한다."""
    binary, why = _binary("KORDOC_BIN", "kordoc")
    if not binary:
        return RunReport(skipped=f"kordoc 미설치: {why}",
                         verdict="변환기가 없다. 크래시가 아니라 **미설치**다")
    out = work / "kordoc-out"
    out.mkdir(exist_ok=True)
    source = work / f"source.{suffix}"
    source.write_bytes(data)
    env = os.environ.copy()
    env["HOME"] = str(_kordoc_home(work))
    return run_converter(
        [binary, "--silent", "-d", str(out), str(source)],
        cwd=work, env=env, collect=out,
    )


def check_libreoffice(data: bytes, suffix: str, work: Path) -> tuple[RunReport, dict]:
    """HWP → PDF. **나온 PDF 가 진짜인지까지** 본다.

    LibreOffice 가 exit 0 으로 끝나고도 PDF 를 안 만들거나, 만들어도 쪽이 비는
    경우가 있다. 「설치돼 있다」 와 「이 판을 읽는다」 는 다르다.
    """
    binary, why = _binary("LIBREOFFICE_BIN", "soffice", "libreoffice")
    if not binary:
        return RunReport(skipped=f"LibreOffice 미설치: {why}",
                         verdict="렌더러가 없다"), {}
    out = work / "lo-out"
    profile = work / "lo-profile"
    out.mkdir(exist_ok=True)
    profile.mkdir(exist_ok=True)
    source = work / f"render.{suffix}"
    source.write_bytes(data)
    env = os.environ.copy()
    env["HOME"] = str(work)
    report = run_converter(
        [
            binary, f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--headless", "--norestore", "--convert-to", "pdf",
            "--outdir", str(out), str(source),
        ],
        cwd=work, env=env, collect=out,
    )
    pdf = out / "render.pdf"
    detail = inspect_pdf(pdf)
    if report.exit_code == 0 and not detail.get("exists"):
        report.verdict = (
            "exit 0 인데 **PDF 가 안 나왔다.** LibreOffice 가 이 HWP 판을 "
            "열지 못한 것이다 — 설치 여부로 지원을 단정하면 안 되는 자리다"
        )
    elif detail.get("exists") and detail.get("pages") == 0:
        report.verdict = "PDF 는 나왔는데 **쪽이 없다.** 렌더링본으로 쓸 수 없다"
    elif detail.get("exists") and not detail.get("text_pages"):
        report.verdict = (
            f"PDF {detail['pages']}쪽이 나왔다. **텍스트 레이어는 없다** — "
            "스캔본처럼 OCR 또는 Vision 경로가 필요하다"
        )
    elif detail.get("exists"):
        report.verdict = (
            f"PDF {detail['pages']}쪽, 글자가 있는 쪽 {detail['text_pages']}개. "
            "로컬 추출로 복구 가능성이 있다"
        )
    return report, detail


def inspect_pdf(path: Path) -> dict:
    """렌더링본이 쓸 만한가. 쪽 수와 **글자가 나오는 쪽 수**를 센다."""
    if not path.is_file():
        return {"exists": False}
    detail: dict = {"exists": True, "bytes": path.stat().st_size}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        detail["encrypted"] = bool(getattr(reader, "is_encrypted", False))
        detail["pages"] = len(reader.pages)
        got = 0
        for page in reader.pages:
            try:
                if (page.extract_text() or "").strip():
                    got += 1
            except Exception:  # noqa: BLE001 - 한 쪽 실패가 진단을 막지 않는다
                continue
        detail["text_pages"] = got
    except Exception as exc:  # noqa: BLE001
        detail["error"] = type(exc).__name__
    return detail


# --- 대상 찾기 -------------------------------------------------------------------


def find_targets(archive: Path, file_ids: list[str], workspace: str) -> list:
    """staging metadata 에서 좌표를 맞춘다. **파일 이름으로 찾지 않는다.**

    같은 이름의 보고서가 기준월마다 올라온다. 이름으로 묶으면 두 달치가 한 건으로
    보이고, 한쪽의 실패가 다른 쪽의 성공에 가려진다(분담 §7).
    """
    from tybot import attachment_review

    wanted = {fid.strip() for fid in file_ids if fid.strip()}
    out = []
    for item in attachment_review.scan(archive):
        if wanted and item.file_id not in wanted:
            continue
        if workspace and item.workspace != workspace:
            continue
        if not wanted and item.filetype.lower() not in ("hwp", "hwpx"):
            continue
        out.append(item)
    return out


def diagnose(item, *, render: bool, compare: bool = False,
             dump_dir: Path | None = None) -> dict:
    """한 건. 원본이 없으면 거기서 끝난다 — 없는 것을 추측하지 않는다."""
    row: dict = {
        "workspace": item.workspace,
        "channel_id": item.channel_id,
        "file_id": item.file_id,
        "filetype": item.filetype,
        "declared_size": item.size,
        "sha256": item.sha256,
        "conversion_state": item.conversion_state,
        "error_code": item.error_code,
        "original_state": item.original_state,
    }
    path = Path(item.object_path) if item.object_path else None
    if path is None or not path.is_file():
        row["blocked"] = "원본이 남아 있지 않습니다. 재현할 수 없습니다"
        return row

    data = path.read_bytes()
    row["actual_bytes"] = len(data)
    container = sniff_container(data[:4096], data[: 1 << 20])
    row["container"] = container
    row["container_label"] = CONTAINERS.get(container, container)
    row["extension_matches"] = (
        (container == "hwp5" and item.filetype.lower() == "hwp")
        or (container == "hwpx" and item.filetype.lower() == "hwpx")
    )
    if container == "hwp5":
        flags, why = hwp5_flags(data[: 1 << 20])
        row["hwp5_flags"] = flags
        if why:
            row["hwp5_flags_error"] = why
        locked = [
            label for bit, name, label in HWP5_FLAGS
            if name in ("password", "distribution", "drm", "certificate_encrypted")
            and flags.get(name)
        ]
        if locked:
            row["locked"] = locked

    with tempfile.TemporaryDirectory(prefix="tybot-hwp-diag-") as raw:
        work = Path(raw)
        suffix = item.filetype.lower() or "hwp"
        row["kordoc"] = asdict(check_kordoc(data, suffix, work))
        if compare:
            # 대조는 렌더링을 포함한다. 따로 또 돌리지 않는다.
            row["compare"] = compare_extraction(
                data, suffix, work, dump_dir=dump_dir, fid=item.file_id,
            )
        elif render:
            report, pdf = check_libreoffice(data, suffix, work)
            row["libreoffice"] = asdict(report)
            row["rendered_pdf"] = pdf
    return row


# --- 출력 -------------------------------------------------------------------------


def _print_run(title: str, data: dict) -> None:
    print(f"  {title}")
    if data.get("skipped"):
        print(f"    건너뜀: {data['skipped']}")
    else:
        bits = [f"exit={data.get('exit_code')}"]
        if data.get("signal_name"):
            bits.append(f"signal={data['signal_name']}")
        if data.get("timed_out"):
            bits.append("시간초과")
        bits.append(f"{data.get('seconds', 0)}초")
        print(f"    {' · '.join(bits)}")
        if data.get("children_max_rss_mb") is not None:
            # **한 호출의 사용량이 아니다.** 라벨을 안 붙이면 그 숫자가 이 건의
            # 메모리로 읽히고, 그대로 OOM 판단에 쓰인다.
            print(f"    자식 전체 누적 최대 RSS={data['children_max_rss_mb']}MB "
                  "(이 호출 단독 사용량 아님)")
        if data.get("outputs"):
            print(f"    산출물: {', '.join(data['outputs'])}")
        codes = data.get("stderr_codes") or []
        if data.get("stderr_chars"):
            print(f"    stderr: {data['stderr_lines']}줄 {data['stderr_chars']}자 · "
                  f"표식={', '.join(codes) or '걸린 것 없음'}")
            if not codes:
                print("      (알고 있는 표식이 없다. 내용은 싣지 않는다 — "
                      "필요하면 변환기를 직접 띄워 보라)")
    print(f"    판정: {data.get('verdict') or '-'}")


def _print_compare(data: dict) -> None:
    """두 경로의 **분량** 대조. 「성공」 과 「다 읽었다」 를 가르는 자리다."""
    direct = data.get("direct") or {}
    via = data.get("via_pdf") or {}
    print("  추출 대조")
    if direct.get("failed"):
        print(f"    지금 경로: 실패({direct['failed']})")
    else:
        print(f"    지금 경로: {direct.get('chars')}자 · 표 {direct.get('table_rows')}행 "
              f"· coverage {direct.get('coverage_state')} {direct.get('coverage')}")
    render = data.get("render") or {}
    if not render.get("exists"):
        print(f"    렌더링: {render.get('verdict') or '안 됨'}")
        return
    print(f"    렌더 PDF: {render.get('pages')}쪽 · 글자 있는 쪽 "
          f"{render.get('text_pages')} · sha256={str(render.get('sha256') or '')[:16]}")
    if via.get("failed"):
        print(f"    PDF 경로: 실패({via['failed']})")
        return
    print(f"    PDF 경로: {via.get('chars')}자 · 표 {via.get('table_rows')}행 "
          f"· 쪽표시 {via.get('page_marks')} · coverage {via.get('coverage_state')} "
          f"{via.get('coverage')} · 미확인 {via.get('missing_pages')}쪽")
    if via.get("missing_pages"):
        print(f"    미확인 쪽: {via['missing_pages']}")
        for number, kind in (via.get("page_kinds") or {}).items():
            print(f"      {number}쪽 — {kind}")
    if data.get("dumped"):
        print("    대조용 저장:")
        for path in data["dumped"]:
            print(f"      {path}")
    ratio = data.get("extracted_ratio")
    if ratio:
        print(f"    ⇒ 추출량 차이: PDF 경로가 {ratio}배 더 많은 글자를 냈다")
        print("       **어느 쪽이 맞는지는 이 숫자로 알 수 없다** — 누락률이 아니다.")
        print("       정확성은 사람이 원본과 대조해야 한다")


def report(rows: list[dict]) -> None:
    for row in rows:
        print("=" * 72)
        print(f"{row['workspace']}/{row['channel_id']}/{row['file_id']} "
              f"({row['filetype']}, {row.get('actual_bytes', row['declared_size'])} bytes)")
        print(f"  저장된 상태: {row['conversion_state']} · {row['error_code'] or '-'}")
        if row.get("blocked"):
            print(f"  ⛔ {row['blocked']}")
            continue
        print(f"  컨테이너 서명: {row['container_label']}")
        print("    (서명이 맞다는 것이 **파일 내부가 온전하다는 뜻은 아니다**)")
        if not row["extension_matches"]:
            print("  ⚠ **확장자와 실제 형식이 다릅니다.** 이름만 바뀐 파일일 수 있습니다")
        if row.get("locked"):
            print("  🔒 " + " / ".join(row["locked"]))
            print("     암호·배포용 문서는 어떤 변환기도 열지 못합니다. 크래시 원인과 별개로 복구 불가입니다")
        if row.get("hwp5_flags_error"):
            print(f"  (속성 비트 확인 불가: {row['hwp5_flags_error']})")
        _print_run("kordoc", row.get("kordoc") or {})
        if row.get("compare"):
            _print_compare(row["compare"])
        if row.get("libreoffice"):
            _print_run("LibreOffice → PDF", row["libreoffice"])
            pdf = row.get("rendered_pdf") or {}
            if pdf.get("exists"):
                print(f"    PDF: {pdf.get('pages')}쪽 · 글자 있는 쪽 {pdf.get('text_pages')}개"
                      f" · {pdf.get('bytes')} bytes"
                      + (" · 암호" if pdf.get("encrypted") else ""))
    print("=" * 72)
    print(f"대상 {len(rows)}건. **아무것도 고치지 않았습니다** — 조회 전용입니다.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="HWP 변환 실패 진단 (조회 전용)")
    parser.add_argument("--archive", default=os.getenv("ARCHIVE_DIR", "./archive"))
    parser.add_argument("--file-id", action="append", default=[],
                        help="진단할 Slack file ID. 여러 번 줄 수 있다")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--render", action="store_true",
                        help="LibreOffice 로 PDF 렌더링까지 시도한다")
    parser.add_argument("--compare-render", action="store_true",
                        help="지금 경로와 「렌더 PDF → PDF 경로」 의 추출 분량을 대조한다")
    parser.add_argument("--dump-dir", default="",
                        help="추출 결과와 렌더 PDF 를 이 디렉터리에 저장한다. "
                             "사람이 원본과 표·숫자를 대조하는 용도이며, "
                             "**사내 문서 본문이라 로그·문서·티켓에 붙이면 안 된다**")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        from tybot.envfile import load_env_file

        load_env_file()
    except Exception:  # noqa: BLE001 - 설정이 없어도 아카이브는 볼 수 있다
        pass

    archive = Path(args.archive)
    if not archive.is_dir():
        print(f"아카이브 디렉터리가 없습니다: {archive}", file=sys.stderr)
        return 2

    targets = find_targets(archive, args.file_id, args.workspace)
    if not targets:
        print("대상을 찾지 못했습니다. file ID 와 --archive 경로를 확인하세요.",
              file=sys.stderr)
        return 1

    dump_dir = Path(args.dump_dir) if args.dump_dir else None
    if dump_dir is not None and not args.compare_render:
        print("--dump-dir 는 --compare-render 와 함께 씁니다.", file=sys.stderr)
        return 2
    rows = [diagnose(item, render=args.render, compare=args.compare_render,
                     dump_dir=dump_dir)
            for item in targets]
    if dump_dir is not None:
        print(f"\n저장 위치: {dump_dir}")
        print("**사내 문서 본문입니다.** 원본과 대조한 뒤 지우고, "
              "로그·인계 문서·티켓에 내용을 붙이지 마십시오.")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
    else:
        report(rows)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
