#!/usr/bin/env python3
"""
count_track_changes.py 시험 — 한글 문서의 변경 내용 추적 세기.

  python .claude/skills/doc-archive/scripts/test_count_track_changes.py

**임시 폴더에서만 돈다.** 가짜 hwp5(복합 파일)를 아래 `make_hwp` 로 손수 만들고,
가짜 hwpx 는 `zipfile` 로 만든다 (`test_extract_comments.py` 가 쓰는 방식과 같다).

**왜 있나** — 한글은 화면에 「최종본」만 보여 주므로 변경 추적이 켜진 채 저장된 문서를
**사람이 원본을 열어도 알 수 없다.** 그 상태로 변환하면 고치기 전 값과 고친 뒤 값이
한 칸에 붙어 나온다(`812804`, `15.611.2`). 자릿수가 이상한 것은 눈에 띄지만
`812804` 는 그냥 숫자로 보이고, 취소선 속성은 하나도 안 붙어 **취소선을 찾는
검사로는 못 잡는다.**

실물 대조 (2026-09-03 실측) — 이 시험이 도는 자리에 아래 파일이 있으면 함께 잰다.
없으면 그 항목만 건너뛴다. 통과/실패를 실물 유무에 걸지 않는다.

종료코드: 0 전부 통과 / 1 실패 있음
"""
import os
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
import count_track_changes as C  # noqa: E402

FAILED = 0
SKIPPED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


def skip(m):
    global SKIPPED
    SKIPPED += 1
    print(f"  – 건너뜀: {m}")


# ── 가짜 hwp5 만들기 ─────────────────────────────────────────────────────────
SEC = 512
MINI = 64
FATSECT = 0xFFFFFFFD
ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF


def _record(tag: int, level: int = 0, body: bytes = b"") -> bytes:
    """DocInfo 레코드 하나. 본문이 4,095바이트를 넘으면 확장 크기 칸을 쓴다."""
    if len(body) < 0xFFF:
        return struct.pack("<I", (len(body) << 20) | (level << 10) | tag) + body
    head = struct.pack("<I", (0xFFF << 20) | (level << 10) | tag)
    return head + struct.pack("<I", len(body)) + body


def _dir_entry(name: str, etype: int, start: int, size: int) -> bytes:
    e = bytearray(128)
    nb = name.encode("utf-16-le") + b"\x00\x00"
    e[: len(nb)] = nb
    struct.pack_into("<H", e, 64, len(nb))
    e[66] = etype
    e[67] = 1  # black
    struct.pack_into("<III", e, 68, FREESECT, FREESECT, FREESECT)  # 좌·우·자식 없음
    struct.pack_into("<I", e, 116, start)
    struct.pack_into("<Q", e, 120, size)
    return bytes(e)


def make_hwp(path: Path, *, changes: int, authors: int,
             compressed: bool = True, track_flag: bool = True) -> None:
    """변경 기록 `changes` 건 · 작성자 `authors` 명인 최소 hwp5 를 만든다."""
    props = (1 if compressed else 0) | ((1 << 14) if track_flag else 0)
    file_header = bytearray(256)
    file_header[:17] = b"HWP Document File"
    struct.pack_into("<I", file_header, 32, 0x05000000)
    struct.pack_into("<I", file_header, 36, props)

    doc = b"".join(
        [_record(16, 0, b"\x00" * 26)]                       # 문서 속성
        + [_record(21, 0, b"\x00" * 8) for _ in range(3)]    # 글자 모양 몇 개
        + [_record(C.HWPTAG_TRACK_CHANGE, 0, b"\x01\x02\x03\x04") for _ in range(changes)]
        + [_record(C.HWPTAG_TRACK_CHANGE_AUTHOR, 0, "홍길동".encode("utf-16-le"))
           for _ in range(authors)]
    )
    doc_info = zlib.compress(doc)[2:-4] if compressed else doc
    if compressed:  # 헤더 없는 deflate 로 다시 정확히 만든다
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        doc_info = co.compress(doc) + co.flush()

    # 미니 스트림에 두 스트림을 이어 붙인다 (실물 hwp 도 작아서 여기 들어간다)
    def pad(b):
        return b + b"\x00" * (-len(b) % MINI)

    fh_pad, di_pad = pad(bytes(file_header)), pad(doc_info)
    fh_mini, di_mini = len(fh_pad) // MINI, len(di_pad) // MINI
    mini_stream = fh_pad + di_pad

    minifat = []
    for i in range(fh_mini):
        minifat.append(ENDOFCHAIN if i == fh_mini - 1 else i + 1)
    for i in range(di_mini):
        minifat.append(ENDOFCHAIN if i == di_mini - 1 else fh_mini + i + 1)
    minifat += [FREESECT] * (SEC // 4 - len(minifat))

    # 섹터 0 = FAT · 1 = 디렉터리 · 2 = 미니FAT · 3.. = 미니 스트림 본체
    n_ms = max(1, -(-len(mini_stream) // SEC))
    fat = [FATSECT, ENDOFCHAIN, ENDOFCHAIN]
    for i in range(n_ms):
        fat.append(ENDOFCHAIN if i == n_ms - 1 else 3 + i + 1)
    fat += [FREESECT] * (SEC // 4 - len(fat))

    directory = (
        _dir_entry("Root Entry", 5, 3, len(mini_stream))
        + _dir_entry("FileHeader", 2, 0, 256)
        + _dir_entry("DocInfo", 2, fh_mini, len(doc_info))
        + bytes(128)
    )

    header = bytearray(512)
    header[:8] = C.CFB_MAGIC
    struct.pack_into("<HH", header, 24, 0x003E, 3)
    struct.pack_into("<H", header, 28, 0xFFFE)
    struct.pack_into("<HH", header, 30, 9, 6)          # 섹터 512 · 미니섹터 64
    struct.pack_into("<I", header, 40, 1)              # 디렉터리 섹터 수
    struct.pack_into("<I", header, 44, 1)              # FAT 섹터 수
    struct.pack_into("<I", header, 48, 1)              # 첫 디렉터리 섹터
    struct.pack_into("<I", header, 56, 4096)           # 미니 스트림 경계
    struct.pack_into("<I", header, 60, 2)              # 첫 미니FAT 섹터
    struct.pack_into("<I", header, 64, 1)              # 미니FAT 섹터 수
    struct.pack_into("<I", header, 68, ENDOFCHAIN)     # 첫 DIFAT 섹터
    struct.pack_into("<I", header, 72, 0)              # DIFAT 섹터 수
    difat = [0] + [FREESECT] * 108
    struct.pack_into("<109I", header, 76, *difat)

    def sector(b):
        return b + b"\x00" * (-len(b) % SEC)

    path.write_bytes(
        bytes(header)
        + sector(struct.pack(f"<{len(fat)}I", *fat))
        + sector(directory)
        + sector(struct.pack(f"<{len(minifat)}I", *minifat))
        + sector(mini_stream)
    )


# ── 가짜 hwpx 만들기 ─────────────────────────────────────────────────────────
def make_hwpx(path: Path, *, header_xml: str, body_xml: str = "") -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("version.xml", "<hv:HCFVersion/>")
        z.writestr("Contents/header.xml", header_xml)
        z.writestr("Contents/section0.xml", f"<hs:sec>{body_xml}</hs:sec>")


BASELINE_HEADER = '<hh:head><hh:trackchageConfig flags="56"/></hh:head>'


# ═════════════════════════════════════════════════════════════════════════════
print("[1/9] 변경 기록과 작성자를 태그 96·97 로 센다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "추적켜짐.hwp"
    make_hwp(p, changes=138, authors=2)
    r = C.inspect(p)
    if r["changes"] == 138:
        ok("변경 기록 138건 — 2026-08-28 실물에서 나온 수와 같은 자리")
    else:
        bad(f"변경 기록이 {r['changes']}건으로 세어졌습니다")
    if r["authors"] == 2:
        ok("작성자 2명")
    else:
        bad(f"작성자가 {r['authors']}명으로 세어졌습니다")
    if r["header_flag"] is True:
        ok("FileHeader 의 「변경 추적 문서」 비트(14)를 읽는다")
    else:
        bad("헤더 플래그를 못 읽습니다")

print("[2/9] 추적이 없는 문서는 0건 — 대부분이 이쪽이라 여기서 죽으면 안 된다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "평범.hwp"
    make_hwp(p, changes=0, authors=0, track_flag=False)
    r = C.inspect(p)
    if r["changes"] == 0 and r["authors"] == 0 and r["header_flag"] is False:
        ok("0건 · 플래그 꺼짐")
    else:
        bad(f"평범한 문서가 {r!r} 로 나옵니다")

print("[3/9] 종료코드로도 알린다 — 사람이 눈으로 안 봐도 걸린다")
with tempfile.TemporaryDirectory() as d:
    hit = Path(d) / "추적켜짐.hwp"
    miss = Path(d) / "평범.hwp"
    make_hwp(hit, changes=3, authors=1)
    make_hwp(miss, changes=0, authors=0, track_flag=False)
    if C.main([str(miss)]) == 0:
        ok("없으면 0")
    else:
        bad("추적 없는 파일에 0 이 아닌 값이 옵니다")
    if C.main([str(hit)]) == 1:
        ok("있으면 1")
    else:
        bad("추적 있는 파일에 1 이 아닌 값이 옵니다")
    if C.main([str(miss), str(hit)]) == 1:
        ok("여러 개 중 하나만 걸려도 1 — 배치로 돌려도 놓치지 않는다")
    else:
        bad("배치에서 1 이 안 옵니다")

print("[4/9] 압축 안 된 DocInfo 도 읽는다 (압축 비트가 0 인 hwp 가 있다)")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "무압축.hwp"
    make_hwp(p, changes=7, authors=1, compressed=False)
    r = C.inspect(p)
    if r["changes"] == 7:
        ok("7건")
    else:
        bad(f"무압축 문서가 {r['changes']}건으로 세어졌습니다")

print("[5/9] 플래그만 켜져 있고 기록이 0건이어도 사람에게 넘긴다")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "플래그만.hwp"
    make_hwp(p, changes=0, authors=0, track_flag=True)
    if C.main([str(p)]) == 1:
        ok("종료코드 1 — 「켜 놓고 아직 안 고친」 문서도 사람이 본다")
    else:
        bad("플래그만 켜진 문서를 그냥 통과시킵니다")

print("[6/9] 4,095바이트를 넘는 레코드(확장 크기 칸)를 건너뛰지 않는다")
with tempfile.TemporaryDirectory() as d:
    # 큰 레코드 뒤에 변경 기록을 둔다 — 확장 크기를 잘못 읽으면 뒤가 통째로 어긋난다
    p = Path(d) / "큰레코드.hwp"
    make_hwp(p, changes=0, authors=0)
    raw = bytearray()
    raw += _record(21, 0, b"\x5a" * 5000)
    raw += b"".join(_record(C.HWPTAG_TRACK_CHANGE, 0, b"\x01") for _ in range(4))
    got = [t for t, _ in C._walk_records(bytes(raw))]
    if got.count(C.HWPTAG_TRACK_CHANGE) == 4:
        ok("큰 레코드 뒤의 변경 기록 4건이 그대로 세어진다")
    else:
        bad(f"태그가 {got!r} 로 읽혔습니다")

print("[7/9] hwpx — 한컴 오타 `trackchageConfig` 를 본다 (`trackChange` 로는 0 이 나온다)")
with tempfile.TemporaryDirectory() as d:
    base = Path(d) / "평범.hwpx"
    make_hwpx(base, header_xml=BASELINE_HEADER)
    r = C.inspect(base)
    if r["has_config"] and r["flags"] == "56":
        ok('실측 기준선 flags="56" 을 읽는다')
    else:
        bad(f"기준선 hwpx 가 {r!r} 로 나옵니다")
    if C.main([str(base)]) == 0:
        ok("기준선과 같으면 통과시킨다")
    else:
        bad("평범한 hwpx 를 붙잡습니다")

    other = Path(d) / "다른설정.hwpx"
    make_hwpx(other, header_xml='<hh:head><hh:trackchageConfig flags="57"/></hh:head>')
    if C.main([str(other)]) == 1:
        ok("flags 가 기준선과 다르면 사람에게 넘긴다 — 뜻을 모르니 삼키지 않는다")
    else:
        bad("기준선과 다른 flags 를 그냥 통과시킵니다")

    marked = Path(d) / "본문표시.hwpx"
    make_hwpx(marked, header_xml=BASELINE_HEADER,
              body_xml='<hp:trackChange id="1"/><hp:trackChangeTag id="2"/>')
    r = C.inspect(marked)
    if r["changes"] == 2 and C.main([str(marked)]) == 1:
        ok("본문 변경표시 2개를 세고 1 을 낸다")
    else:
        bad(f"본문 표시가 {r['changes']}개로 세어졌습니다")

    # 이 스킬이 「없다」고 단정하지 않는 이유를 시험으로 못 박는다.
    if C.inspect(base)["uncertain"] is True:
        ok("hwpx 결과에는 「확인 못 함」 표가 붙는다 — 켜진 실물을 못 구했다")
    else:
        bad("hwpx 결과가 확정처럼 나옵니다")

print("[8/9] 못 읽는 것은 조용히 넘기지 않는다")
with tempfile.TemporaryDirectory() as d:
    junk = Path(d) / "깨진.hwp"
    junk.write_bytes(b"not a compound file at all" * 40)
    try:
        C.inspect(junk)
        bad("깨진 파일에 예외가 안 납니다")
    except C.NotReadable:
        ok("NotReadable 을 던진다")
    if C.main([str(junk)]) == 2:
        ok("종료코드 2 — 0(없음)과 구별된다. 0 이면 「깨끗하다」로 읽힌다")
    else:
        bad("못 읽은 것이 0 이나 1 로 나옵니다")
    missing = Path(d) / "없는파일.hwp"
    if C.main([str(missing)]) == 2:
        ok("없는 파일도 2")
    else:
        bad("없는 파일이 2 가 아닙니다")
    wrong = Path(d) / "문서.docx"
    wrong.write_bytes(b"PK\x03\x04")
    if C.main([str(wrong)]) == 2:
        ok("다루지 않는 확장자도 2 — docx 는 4단계의 `w:ins`·`w:del` 쪽이다")
    else:
        bad("확장자 밖 파일이 2 가 아닙니다")

# ── 실물 대조 (있을 때만) ────────────────────────────────────────────────────
print("[실물] 2026-08-28 현황보고 원본이 있으면 함께 잰다")
# **파일 이름은 이 저장소에 안 남긴다** (2026-09-05). 이 저장소는 팀끼리 나눠 쓰고,
# 그 이름은 사업장을 가리킨다. 날짜 앞머리로만 찾으므로 대조는 그대로 돈다 —
# 원본은 자료 저장소의 2026-08-28 현황보고 문서이고, 기대값(138건·2명·켜짐)은
# 2026-09-03 실측이다.
REAL = next(iter(sorted((Path.home() / "Downloads").glob("260828_*.hwp"))), None)
if REAL is not None and REAL.exists():
    r = C.inspect(REAL)
    if r["changes"] == 138 and r["authors"] == 2 and r["header_flag"]:
        ok(f"실물에서 변경 기록 {r['changes']}건 · 작성자 {r['authors']}명 · 플래그 켜짐")
    else:
        bad(f"실물이 {r!r} 로 나옵니다 (2026-09-03 실측은 138건·2명·켜짐)")
else:
    skip("2026-08-28 현황보고 원본이 Downloads 에 없다 — 가짜 문서 시험만 돌았다")

# ═════════════════════════════════════════════════════════════════════════════
print("[9/9] 한글 윈도우 콘솔(cp949)에서 죽지 않는다")
#
# 이 스크립트의 출력에는 줄표(`—`, U+2014)가 들어 있는데 **cp949 에 그 글자가 없다.**
# 출력 인코딩을 안 맞추면 한글 윈도우의 기본 콘솔에서 그 줄을 찍다 UnicodeEncodeError
# 로 죽는다 — 2026-09-04 에 실제로 hwp 1건을 재고 다음 hwpx 에서 죽었다.
#
# **그냥 죽는 것보다 나쁘다.** 죽을 때의 종료코드 1 은 이 스크립트에서 「변경 기록
# 있음」이라, 「못 쟀다」와 「추적이 있다」가 같은 값이 된다. 여러 파일을 한 번에 재다
# 중간에 죽으면 앞 몇 줄만 보고 「0건」으로 메타에 적을 수 있고, 그 0건은 아카이브를
# 거쳐 봇 답변까지 간다.
#
# stderr 는 기본이 `backslashreplace` 라 죽지는 않지만 `—` 라는 글자가 그대로
# 찍힌다 — 그것도 함께 본다.
#
# 하위 프로세스로 돌리는 이유: 이 시험 파일 자신은 위에서 stdout 을 utf-8 로 맞춰
# 두었으므로, 같은 프로세스 안에서는 그 조건을 만들 수 없다.
with tempfile.TemporaryDirectory() as d:
    good = Path(d) / "기준선.hwpx"
    make_hwpx(good, header_xml=BASELINE_HEADER)
    bad_zip = Path(d) / "가짜.hwpx"
    bad_zip.write_bytes(b"not a zip")

    env = {**os.environ, "PYTHONIOENCODING": "cp949", "PYTHONDONTWRITEBYTECODE": "1"}
    script = str(Path(__file__).parent / "count_track_changes.py")

    p = subprocess.run([sys.executable, script, str(good)],
                       env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", check=False)
    if p.returncode == 0 and "Traceback" not in p.stderr:
        ok("cp949 출력에서도 기준선 hwpx 를 끝까지 찍는다 (종료코드 0)")
    else:
        bad(f"cp949 에서 죽습니다 — 종료코드 {p.returncode}\n"
            f"      {p.stderr.strip().splitlines()[-1] if p.stderr.strip() else '(stderr 없음)'}")

    p2 = subprocess.run([sys.executable, script, str(bad_zip)],
                        env=env, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", check=False)
    if "\\u" not in p2.stderr:
        ok("못 읽는 파일의 사유도 글자가 안 깨진다")
    else:
        bad(f"stderr 에 이스케이프가 그대로 찍힙니다: {p2.stderr.strip()}")

print(f"\n{'실패 있음' if FAILED else '전부 통과'}"
      f"{f' (건너뜀 {SKIPPED})' if SKIPPED else ''}")
sys.exit(1 if FAILED else 0)
