#!/usr/bin/env python3
"""한글 문서(.hwp·.hwpx)에 켜져 있는 변경 내용 추적을 센다.

왜 필요한가 — 한글은 화면에 「최종본」만 보여 주므로 **사람이 원본을 열어도 안 보인다.**
그 상태로 kordoc 에 넣으면 고치기 전 값과 고친 뒤 값이 한 칸에 붙어 나온다
(2026-08-28 어느 사업장의 현황보고 hwp: `3,100845`(→3,845억) ·
`812804` · `15.611.2` 처럼 9곳). `3,100845` 는 자릿수가 이상해 눈에 띄지만
`812804` 는 **그냥 숫자로 보인다.** 취소선 속성은 하나도 안 붙으므로
「취소선을 찾는」 검사로는 못 잡는다.

쓰는 법 (doc-archive 4단계, docx 의 `w:ins`·`w:del` 세기와 같은 자리):

    python count_track_changes.py <파일>
    python count_track_changes.py <폴더>/*.hwp

종료코드
    0  변경 추적 흔적 없음
    1  있음 — 사람이 원본을 봐야 한다
    2  읽지 못함 (형식 아님·깨진 파일)

의존성 없음 — 표준 라이브러리만 쓴다.
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
import zipfile
import zlib
from pathlib import Path

# 이 파일의 출력에는 줄표(`—`, U+2014)가 들어 있는데 **cp949 에 그 글자가 없다.**
# 안 맞추면 한글 윈도우의 기본 콘솔에서 그 줄을 찍다 UnicodeEncodeError 로 죽는데,
# 그때의 종료코드 1 은 이 스크립트에서 「변경 기록 있음」이라 **「못 쟀다」와 「추적이
# 있다」가 같은 값이 된다.** 여러 파일을 재다 중간에 죽으면 앞 몇 줄만 보고 「0건」으로
# 메타에 적게 되고, 그 0건은 아카이브를 거쳐 봇 답변까지 간다 (2026-09-04 에 겪었다).
# 같은 폴더의 다른 스크립트 20개는 처음부터 이 처리를 갖고 있었고 이 파일만 없었다.
# 모양은 decide.py 와 같다 — pythonw 처럼 stdout 이 없으면 조용히 넘어가고,
# errors="replace" 라 utf-8 로도 못 찍는 글자가 있어도 죽지 않는다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ── HWP 5.0 DocInfo 레코드 태그 (HWPTAG_BEGIN = 16) ───────────────────────────
HWPTAG_TRACK_CHANGE = 96  # HWPTAG_BEGIN + 80 — 변경 기록 하나하나
HWPTAG_TRACK_CHANGE_AUTHOR = 97  # HWPTAG_BEGIN + 81 — 그 기록을 남긴 사람

# FileHeader 의 문서 속성 DWORD (오프셋 36) 안의 비트
PROP_COMPRESSED = 1 << 0
PROP_TRACK_CHANGE_DOC = 1 << 14  # 「변경 추적 문서」 플래그

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF


class NotReadable(Exception):
    """파일을 열지 못했다 — 형식이 아니거나 깨졌다."""


# ── 최소 CFB(복합 파일) 리더 ─────────────────────────────────────────────────
class Cfb:
    """hwp5 를 담고 있는 복합 파일에서 스트림 하나를 꺼내는 데 필요한 만큼만."""

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or data[:8] != CFB_MAGIC:
            raise NotReadable("복합 파일(CFB)이 아니다")
        self.data = data
        self.sector_size = 1 << struct.unpack_from("<H", data, 30)[0]
        self.mini_sector_size = 1 << struct.unpack_from("<H", data, 32)[0]
        self.mini_cutoff = struct.unpack_from("<I", data, 56)[0]
        first_dir = struct.unpack_from("<I", data, 48)[0]
        first_minifat = struct.unpack_from("<I", data, 60)[0]
        first_difat = struct.unpack_from("<I", data, 68)[0]
        n_difat = struct.unpack_from("<I", data, 72)[0]

        self.fat = self._read_fat(first_difat, n_difat)
        self.minifat = self._sector_ints(first_minifat)
        self.entries = self._read_directory(first_dir)
        root = self.entries[0]
        self.mini_stream = self._read_chain(root["start"], root["size"], mini=False)

    # -- 낮은 층 --------------------------------------------------------------
    def _sector_bytes(self, sec: int) -> bytes:
        off = (sec + 1) * self.sector_size
        chunk = self.data[off: off + self.sector_size]
        if len(chunk) < self.sector_size:
            raise NotReadable(f"섹터 {sec} 가 파일 끝을 넘는다")
        return chunk

    def _read_fat(self, first_difat: int, n_difat: int) -> list[int]:
        per = self.sector_size // 4
        difat = list(struct.unpack_from("<109I", self.data, 76))
        sec = first_difat
        for _ in range(n_difat):
            if sec in (ENDOFCHAIN, FREESECT):
                break
            block = self._sector_bytes(sec)
            vals = list(struct.unpack_from(f"<{per}I", block, 0))
            difat.extend(vals[:-1])
            sec = vals[-1]
        fat: list[int] = []
        for fs in difat:
            if fs in (ENDOFCHAIN, FREESECT):
                continue
            fat.extend(struct.unpack_from(f"<{per}I", self._sector_bytes(fs), 0))
        return fat

    def _sector_ints(self, first: int) -> list[int]:
        per = self.sector_size // 4
        out: list[int] = []
        sec = first
        guard = 0
        while sec not in (ENDOFCHAIN, FREESECT) and sec < len(self.fat):
            out.extend(struct.unpack_from(f"<{per}I", self._sector_bytes(sec), 0))
            sec = self.fat[sec]
            guard += 1
            if guard > 1_000_000:
                raise NotReadable("섹터 사슬이 끝나지 않는다")
        return out

    def _read_chain(self, start: int, size: int, mini: bool) -> bytes:
        table = self.minifat if mini else self.fat
        unit = self.mini_sector_size if mini else self.sector_size
        out = bytearray()
        sec = start
        guard = 0
        while sec not in (ENDOFCHAIN, FREESECT) and sec < len(table):
            if mini:
                off = sec * unit
                out += self.mini_stream[off: off + unit]
            else:
                out += self._sector_bytes(sec)
            sec = table[sec]
            guard += 1
            if guard > 1_000_000:
                raise NotReadable("스트림 사슬이 끝나지 않는다")
        return bytes(out[:size]) if size else bytes(out)

    def _read_directory(self, first_dir: int) -> list[dict]:
        raw = bytearray()
        sec = first_dir
        guard = 0
        while sec not in (ENDOFCHAIN, FREESECT) and sec < len(self.fat):
            raw += self._sector_bytes(sec)
            sec = self.fat[sec]
            guard += 1
            if guard > 100_000:
                raise NotReadable("디렉터리 사슬이 끝나지 않는다")
        entries = []
        for i in range(len(raw) // 128):
            e = raw[i * 128: (i + 1) * 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            name = e[: max(0, nlen - 2)].decode("utf-16-le", "ignore")
            entries.append({
                "name": name,
                "type": e[66],
                "start": struct.unpack_from("<I", e, 116)[0],
                "size": struct.unpack_from("<Q", e, 120)[0],
            })
        if not entries:
            raise NotReadable("디렉터리가 비어 있다")
        return entries

    # -- 쓰는 쪽 --------------------------------------------------------------
    def stream(self, name: str) -> bytes:
        for e in self.entries:
            if e["name"] == name and e["type"] == 2:
                mini = e["size"] < self.mini_cutoff
                return self._read_chain(e["start"], e["size"], mini=mini)
        raise NotReadable(f"스트림 `{name}` 이 없다")


# ── hwp5 ────────────────────────────────────────────────────────────────────
def _inflate(blob: bytes) -> bytes:
    """hwp5 는 헤더 없는 deflate 로 압축한다."""
    return zlib.decompress(blob, -15)


def _walk_records(stream: bytes):
    """DocInfo 레코드를 훑어 (태그ID, 본문) 을 하나씩 낸다."""
    pos, n = 0, len(stream)
    while pos + 4 <= n:
        (header,) = struct.unpack_from("<I", stream, pos)
        pos += 4
        tag = header & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > n:
                break
            (size,) = struct.unpack_from("<I", stream, pos)
            pos += 4
        body = stream[pos: pos + size]
        if len(body) < size:  # 잘린 꼬리 — 여기서 멈춘다
            break
        pos += size
        yield tag, body


def count_hwp(path: Path) -> dict:
    cfb = Cfb(path.read_bytes())
    header = cfb.stream("FileHeader")
    if len(header) < 40 or not header[:17].startswith(b"HWP Document File"):
        raise NotReadable("hwp5 FileHeader 가 아니다")
    props = struct.unpack_from("<I", header, 36)[0]
    doc_info = cfb.stream("DocInfo")
    if props & PROP_COMPRESSED:
        doc_info = _inflate(doc_info)

    changes = authors = 0
    for tag, _body in _walk_records(doc_info):
        if tag == HWPTAG_TRACK_CHANGE:
            changes += 1
        elif tag == HWPTAG_TRACK_CHANGE_AUTHOR:
            authors += 1
    return {
        "kind": "hwp5",
        "changes": changes,
        "authors": authors,
        "header_flag": bool(props & PROP_TRACK_CHANGE_DOC),
    }


# ── hwpx ────────────────────────────────────────────────────────────────────
# hwpx 는 zip(OWPML) 이다. 설정은 `Contents/header.xml` 의 **`hh:trackchageConfig`**
# 에 적힌다 — **한컴의 오타라 `change` 가 아니라 `chage` 다.** 그래서 「trackChange」
# 로 찾으면 **켜져 있든 아니든 0건이 나온다** (2026-09-02 에 hwpx 1건이 0 으로 나온
# 것은 이 때문이고, 그 0 은 「추적이 없다」를 뜻하지 않았다).
#
# 실측 기준선 (2026-09-03, 이 PC 의 hwpx 33개) — 전부 `flags="56"` 이고 본문
# 변경표시는 0개였다. **추적이 켜진 hwpx 실물을 못 구해서 「켜지면 무엇이 달라지나」
# 는 확인하지 못했다.** 그래서 아래는 값을 그대로 보이고 기준선과 다르면 사람에게
# 넘길 뿐, 「없다」고 단정하지 않는다.
HWPX_BASELINE_FLAGS = "56"
HWPX_CONFIG_RE = re.compile(r"<[a-zA-Z]+:trackchageConfig\b[^>]*>")
HWPX_FLAGS_RE = re.compile(r'flags\s*=\s*"([^"]*)"')
HWPX_BODY_MARK_RE = re.compile(r"<[a-zA-Z]+:(trackChange[A-Za-z]*)\b")


def count_hwpx(path: Path) -> dict:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise NotReadable("zip 이 아니다 — hwpx 가 아닌 듯하다") from exc
    with z:
        names = z.namelist()
        if "Contents/header.xml" not in names:
            raise NotReadable("`Contents/header.xml` 이 없다 — hwpx 가 아닌 듯하다")
        header = z.read("Contents/header.xml").decode("utf-8", "ignore")
        body = "".join(
            z.read(n).decode("utf-8", "ignore")
            for n in names if n.startswith("Contents/section")
        )
    cfg = HWPX_CONFIG_RE.search(header)
    flags = None
    if cfg:
        fm = HWPX_FLAGS_RE.search(cfg.group(0))
        flags = fm.group(1) if fm else ""
    marks = HWPX_BODY_MARK_RE.findall(body)
    off_baseline = cfg is not None and flags == HWPX_BASELINE_FLAGS and not marks
    return {
        "kind": "hwpx",
        "changes": len(marks),
        "authors": 0,
        # 기준선과 똑같을 때만 「아닌 듯」으로 넘긴다. 그 밖은 전부 사람에게.
        "header_flag": not off_baseline,
        "flags": flags,
        "has_config": cfg is not None,
        "uncertain": True,
    }


def inspect(path: Path) -> dict:
    if not path.is_file():
        raise NotReadable("파일이 없다")
    suffix = path.suffix.lower()
    if suffix == ".hwpx":
        return count_hwpx(path)
    if suffix == ".hwp":
        return count_hwp(path)
    raise NotReadable(f"다루지 않는 확장자 `{suffix}` — .hwp · .hwpx 만 본다")


def describe(path: Path, r: dict) -> str:
    if r["kind"] == "hwp5":
        flag = "켜짐" if r["header_flag"] else "꺼짐"
        return (f"변경 기록 {r['changes']}건 · 작성자 {r['authors']}명 · "
                f"헤더 플래그 {flag}   {path.name}")
    cfg = f'flags="{r["flags"]}"' if r["has_config"] else "설정 원소 없음"
    tail = "" if r["header_flag"] else "  (실측 기준선과 같다 — 켜진 실물로 확인한 적은 없다)"
    return (f"본문 변경표시 {r['changes']}개 · trackchageConfig {cfg}"
            f"   {path.name}{tail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="hwp·hwpx 의 변경 내용 추적을 센다")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--quiet", action="store_true",
                    help="흔적이 있는 파일만 찍는다")
    args = ap.parse_args(argv)

    found = unreadable = False
    for path in args.files:
        try:
            r = inspect(path)
        except NotReadable as exc:
            unreadable = True
            print(f"못 읽음: {exc}   {path.name}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001 — 어떤 파일이 왜 죽었는지 보이고 계속 간다
            unreadable = True
            print(f"못 읽음: {type(exc).__name__}: {exc}   {path.name}", file=sys.stderr)
            continue
        hit = r["changes"] > 0 or r["header_flag"]
        found = found or hit
        if hit or not args.quiet:
            print(("추적 있음  " if hit else "           ") + describe(path, r))

    if found:
        return 1
    return 2 if unreadable else 0


if __name__ == "__main__":
    sys.exit(main())
