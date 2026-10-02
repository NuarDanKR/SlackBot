#!/usr/bin/env python3
"""
커밋 전 건별 점검표 — 이번 회차에 무엇이 들어갔고, 무엇을 고쳐야 하나.

**왜 있나.** doc-archive 는 「전부 반영」밖에 없었다. 변환이 끝나면 곧바로 커밋 승인을
물었고, 사람이 볼 수 있는 것은 「변환 12건」 같은 합계뿐이었다. 그런데 이 파이프라인이
**조용히 틀리는 자리**는 전부 건별이다 — 한 건의 열람 판정이 어긋나고, 한 건이 이미지만
있고, 한 건의 검토의견이 통째로 빠진다. 에러는 하나도 안 난다.

그 자리들이 지금까지 SKILL.md 산문에 흩어져 **사람 기억에 의존**하고 있었다.
여기서 한 표로 뽑는다. 축 여덟:

  열람      메타 `열람`+`공개승인` ↔ 채널의 비공개 여부 3자 대조
            (2026-08-05: `#비공개나` 이 privateChannels 에 없어 문서 5건이 공개로 들어갔다)
  본문      `![image](...)` 를 지운 뒤의 글자 수
            (2026-08-04: 계약서 4건이 파일 길이로는 414~3,166자였는데 실제 본문은 0자였다)
  주석      원본 docx 에 `word/comments.xml` 이 있는데 md 에 「문서 주석」 절이 없나
            (2026-08-05: 검토의견 886자·1,026자가 통째로 빠질 뻔했다. 본문이 길어
            글자 수 검사는 통과한다)
  마스킹    주민번호·휴대폰·계좌 패턴이 남아 있나 (5단계 규칙 4 — 검사가 없었다)
  시리즈    3건째면 승격, 같은 날짜 회차가 이미 있으면 교체 여부 (6단계 판정)
  엑셀      시트 개수 불일치 · 상한 초과로 뺀 시트 · 숨긴 시트/행 · 서식 미해석 ·
            본문의 엑셀 오류 문자열(`#REF!` 등) — `시트` 메타가 없는 문서는 판정 안 함
  뭉침      표 칸에 숫자 둘이 붙은 것 (`13,4731,076`). **이번에 들어온 줄만** 본다
            (2026-09-21: 검사가 없어 `2868,373`·`7901,076` 이 그대로 들어갔고,
            어느 사업장에서는 사람이 `1,000900` 을 1,000억으로 읽어 봇이 인용했다)
  계약검사  `verify_format.py` 의 `check()` 를 그대로 부른다 (7단계)

표 아래에 **「봇 색인 문장」 절**이 따로 붙는다 (2026-09-04). 메타 `주요 항목` 과
회차 요약 표의 칸은 **모델이 쓴 문장**인데 위 어느 축에도 안 실려서, 사람이
한 번도 안 읽은 문장이 그대로 커밋됐다 — 그 문장이 곧 `documents.js` 의
`buildDocumentsBrief` 가 봇 프롬프트에 싣는 색인 줄이라, 숫자 하나가 틀리면 봇이
그 숫자를 근거로 답한다(2026-09-04 한 회차에서만 새 `주요 항목` 3건 + 회차 요약 표
2칸이 이렇게 들어갔다). 절은 **이번에 새로 생기거나 바뀐 문장만 전문으로** 보인다 —
전부 다 보이면 표가 길어져 관문이 성가셔지고, 성가신 관문은 꺼진다(이 저장소의
기존 판단). 강제는 기존 도장이 그대로 한다 — 도장은 md 전체 해시라, 이 절이 화면에
나온 뒤 문장을 또 고치면 해시가 갈려 커밋이 막힌다. 새 도장은 안 만들었다.

**대상은 git 작업 트리에서 찾는다.** 이번 회차에 생기거나 고쳐진 md 가 곧 「아직 사람이
승인하지 않은 제안」이다. 별도 장부가 필요 없고, `apply_approvals.py` 로 메타 두 줄만
고친 경우까지 같은 표에 실린다.

**`verify_format.py` 는 건드리지 않는다.** VM 의 대화 자동 반영이 커밋 직전 관문
(`ingest/verify.js` 의 `runGate`)에서 그것을 그대로 돌리므로, 검사 항목이나 종료코드가
바뀌면 그날 대화 반영까지 통째로 롤백된다. 여기서는 `check()` 를 읽어 쓰기만 한다.

**10단계(원본 정리) 전에 돌려야 한다** — 주석 대조에 `~/.doc-cache` 의 원본이 필요하다.

**표를 낸 사실을 기록으로 남긴다** (`documents/.review-stamp.json`). 이 관문은 사람이
손으로 부르는 것뿐이라 **건너뛰어도 아무 흔적이 없었다** — 나중에 확인할 방법이 커밋
메시지에 적혔나뿐이었다. 그래서 표를 낼 때 그 md 의 해시를 적어 두고, `--gate` 가
커밋에 담긴 문서 md 중 그 기록에 없는 것을 찾아 **커밋을 멈춘다**(git pre-commit 훅).
점검 뒤에 고치면 해시가 달라져 다시 막히므로, 「고쳐서 반영」도 다시 돌아야 통과한다.
기록은 저장소에 안 들어간다(`.gitignore`) — 이 PC 에서 표를 봤다는 사실이지 문서가 아니다.

사용:
  python review_batch.py                    # 작업 트리 전체
  python review_batch.py --project 사업장나   # 그 사업장만
  python review_batch.py --verbose          # ⚠ 의 사유를 자세히
  python review_batch.py --gate             # 표 없이 검사만 (훅 자리. 안 거친 것 있으면 1)
  python review_batch.py --mask-sweep       # 자료 저장소의 md 전부(문서 + 대화 + 운영 노트)를 훑는다

이 스크립트는 아무것도 고치지 않는다. 고치는 것은 사람이고, 빼는 것은 `decide.py --undo`.

종료코드: 0 (판정이 아니라 표다 — 커밋 여부는 사람이 정한다)
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
from paths import DATA_ROOT, DOCS_DIR  # noqa: E402
from fetch_slack_files import safe_name  # noqa: E402
from xlsx_to_blocks import (  # noqa: E402
    ACCOUNT_NUM, ACCOUNT_WORD, INLINE_PATTERNS, looks_like_date,
)
from verify_format import (  # noqa: E402
    ENTRY_RE, IMG_RE, check, meta_lines, parse_meta, private_channels,
)
# 뭉침 잣대는 **한 벌만 있어야 한다.** 정규식을 여기 베끼면 한쪽만 고쳐도 에러가 안
# 난 채 조용히 갈린다 — 마스킹 패턴이 실제로 그렇게 갈렸다 (2026-08-26).
import detect_merged_numbers as merged  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# `ROOT` 는 **자료 저장소**다 — 문서 md 가 있고 git 을 돌리는 자리.
ROOT = DATA_ROOT
DOCS_REL = "documents/projects"
CACHE = Path.home() / ".doc-cache"
# 표를 낸 사실만 담는다. 저장소에 안 들어간다 — 다른 PC·VM 의 기록이 섞이면
# 「여기서 사람이 봤다」는 뜻이 사라진다.
STAMP = DOCS_DIR / ".review-stamp.json"

# 본문이 이만큼도 안 되면 OCR 을 빠뜨린 스캔본으로 본다 (SKILL.md 4단계와 같은 값).
MIN_BODY = 200

# 마스킹 패턴은 `xlsx_to_blocks.py` 가 유일한 원본이다 (2026-08-26). 예전에는
# 여기에 한 벌 더 적혀 있었고 "같아야 한다"는 주석에 기대고 있었다 — 한쪽만
# 고쳐도 에러가 안 나서, 변환기는 가리는데 점검표는 ✓ 를 찍는 상태가 조용히 났다.
COMMENT_SECTION = "문서 주석"

# 엑셀은 오류 셀을 문자열 그대로 싣는다(화면에 그렇게 보이므로 옳다) — 그래서
# meta.json 이 아니라 **본문**에서 찾는다. 값을 지어내지 않은 정직한 결과지만,
# 산정 내역에 오류가 있다는 사실은 사람이 봐야 한다.
EXCEL_ERROR_TOKENS = ["#REF!", "#N/A", "#DIV/0!", "#VALUE!", "#NAME?", "#NUM!", "#NULL!"]
EXCEL_ERROR_RE = re.compile("|".join(re.escape(t) for t in EXCEL_ERROR_TOKENS))

# 회차 헤더의 `· ` 뒤(원본명 자리)에 시트 표기가 붙었는지. 메타 `시트` 줄이 빠졌는지를
# **본문으로** 판정하려고 쓴다 — 메타만 보면 빠뜨린 것을 영영 못 본다.
SHEET_SRC_RE = re.compile(r" — 시트 \d+/\d+: ")

# 시리즈 정규화명 — SKILL.md 6단계와 같은 규칙 (날짜·회차 표기를 빼고 특수문자를 압축)
DATE_TOKENS = re.compile(r"\d{4}[.\-_]?\d{2}[.\-_]?\d{2}|\d{6}|\d{2}년\s*\d+차|\d+차")
NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]+")


# ── 표 그리기 (한글 폭 계산) ─────────────────────────────────────────────

def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in str(s))


def pad(s: str, n: int) -> str:
    """폭 `n` 에 맞춘다. **`⚠` 로 끝나는 값은 잘려도 그 표시를 남긴다** — 안 그러면
    긴 사유가 표 칸에서 잘리면서, 정작 사람 눈을 끌어야 할 `⚠` 까지 함께 사라진다
    (2026-08-24: `unmaskable_sheets` 사유를 더하고 나서 실물 표에서 실제로 이렇게
    잘려 「가릴 수 없어 뺀 시…」만 남고 ⚠ 는 안 보였다)."""
    s = str(s)
    if width(s) <= n:
        return s + " " * (n - width(s))
    warn = s.endswith("⚠")
    tail = "…⚠" if warn else "…"
    body = s[:-1].rstrip() if warn else s
    out = ""
    for c in body:
        if width(out) + width(c) > n - width(tail):
            break
        out += c
    out += tail
    return out + " " * max(0, n - width(out))


def table(headers, rows, caps):
    """열마다 상한을 따로 준다 — 사업장/문서 칸은 날짜까지 보여야 해서 넓어야 하고,
    나머지는 좁아야 한 줄에 들어온다."""
    cols = [
        min(caps[i], max(width(h), *(width(r[i]) for r in rows)) if rows else width(h))
        for i, h in enumerate(headers)
    ]
    out = ["  ".join(pad(h, w) for h, w in zip(headers, cols)).rstrip()]
    for r in rows:
        out.append("  ".join(pad(c, w) for c, w in zip(r, cols)).rstrip())
    return "\n".join(out)


# ── git ──────────────────────────────────────────────────────────────────

class GitError(RuntimeError):
    """git 이 실패했다. **빈 문자열로 돌려주지 않는다** — 그러면 관문이 「볼 것 없음」과
    「못 봄」을 같은 0 으로 답하고, 훅이 걸려 있는데 아무것도 안 지키게 된다
    (2026-08-12: 스킬을 다른 깊이에 깔면 ROOT 가 어긋나 실제로 이 길로 간다)."""


# **바깥의 git 환경변수를 끊는다** (2026-09-22 — `.githooks/test-push-gate.sh` 의
# 7aa2397 과 같은 처리). 코드 저장소에서 `git commit -- <경로>` 로 담으면 git 이
# 임시 인덱스를 만들어 훅에 `GIT_INDEX_FILE` 로 물려주는데, 이 관문은 **자료
# 저장소**에서 git 을 돌리므로 그 값을 물려받으면 남의(코드 저장소의) 임시 인덱스를
# 읽는다 — 자료 저장소가 완전히 깨끗한데도 문서가 전부 「바뀐 것」으로 잡히고
# `git show :<경로>` 가 「path exists on disk, but not in the index」로 죽는다
# (2026-09-07 에 실제로 막혀서 드러남 — 막힌 사유가 자기 변경과 무관했다).
_FOREIGN_GIT_ENV = ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE",
                    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_COMMON_DIR")


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in _FOREIGN_GIT_ENV}


def git(*args) -> str:
    """`core.quotepath=false` 를 늘 붙인다 — 없으면 git 이 한글 경로를 8진수로 감싸
    출력해서 이 아카이브(파일명이 거의 다 한글)의 경로를 하나도 못 읽는다."""
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_clean_env(),
    )
    if r.returncode != 0:
        head = (r.stderr or "").strip().splitlines()
        raise GitError(f"git {' '.join(args[:2])} — "
                       + (head[0] if head else f"종료코드 {r.returncode}"))
    return r.stdout


def repo_check():
    """`ROOT`(자료 저장소) 가 진짜 이 저장소의 뿌리인가.

    **2026-08-31 부터 ROOT 는 추측이 아니다.** 전에는 각 스크립트가
    `Path(__file__).parents[4]` 로 파일 위치에서 추측했고, 스킬을 다른 깊이(개인
    `~/.claude/skills/` 등)에 깔면 엉뚱한 저장소에서 git 을 돌리게 됐다 — 그러면 관문이
    「담긴 문서 없음」이라며 통과시킨다. 훅은 걸려 있는데 아무것도 안 지킨다.

    지금은 `_shared/paths.py` **한 곳**이 정하고(환경변수 → `.env` → 이웃 폴더),
    봇의 `src/config.js` 와 갈렸는지는 `scripts/check-roots.js` 가 본다.
    그래도 이 검사는 남긴다 — 자료 저장소를 짚었는데 그 자리가 git 저장소가
    아니거나 다른 저장소의 하위 폴더인 경우는 여전히 있을 수 있다.
    """
    top = Path(git("rev-parse", "--show-toplevel").strip())
    if top.resolve() != ROOT.resolve():
        raise GitError(f"관문이 계산한 위치와 저장소가 다릅니다 (계산 {ROOT} · 실제 {top}). "
                       "스킬은 <저장소>/.claude/skills/<이름>/scripts/ 에 있어야 합니다")


def changed_docs(project: str | None):
    """(경로, 새 파일인가) — 작업 트리에서 이번에 생기거나 고쳐진 문서 md."""
    out = []
    # `-uall` 이 없으면 **새 사업장 폴더가 폴더 한 줄로만 보고된다** — 그 안의 md 가
    # 통째로 안 잡혀 새 사업장의 첫 문서가 점검을 건너뛴다.
    for line in git("status", "--porcelain", "-uall", "--", DOCS_REL).splitlines():
        if not line.strip():
            continue
        status, path = line[:2], line[3:].strip()
        # 이름 변경은 `R  옛경로 -> 새경로` 로 온다. 그대로 두면 경로가
        # `"옛경로 -> 새경로"` 라는 한 덩어리 문자열이 되어 없는 파일을 읽으러 간다.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if not path.endswith(".md"):
            continue
        if project and f"/{project}/" not in "/" + path:
            continue
        md = ROOT / path
        # **없는 파일은 뺀다.** 삭제(` D`)도 여기 실려 오는데, 그게 흔한 순서다 —
        # 7.5 관문에서 ⚠ 를 보고 `decide.py --undo` 로 물려 md 가 지워진 뒤
        # (decide.py 의 `md.unlink()`) ⚠ 가 사라졌는지 이 관문을 다시 돌린다.
        # 거르지 않으면 표 대신 FileNotFoundError 가 뜬다.
        if not md.exists():
            continue
        out.append((md, status.strip() in ("??", "A", "AM")))
    return sorted(out)


def staged_docs():
    """커밋에 담긴 문서 md. **작업 트리가 아니라 스테이징을 본다** — 변환하다 만 md 를
    옆에 둔 채 다른 일을 커밋하는 것까지 막으면 관문이 곧 성가신 것이 되어 꺼진다."""
    out = []
    for path in git("diff", "--cached", "--name-only", "--", DOCS_REL).splitlines():
        path = path.strip().strip('"')
        if not path.endswith(".md"):
            continue
        md = ROOT / path
        # 삭제(`decide.py --undo` 로 물린 것)는 점검할 내용이 없다.
        if md.exists():
            out.append(md)
    return sorted(set(out))


# ── 점검 기록 ────────────────────────────────────────────────────────────

def _digest(data: bytes) -> str:
    """**줄끝을 맞춘 뒤 센다.** git 은 인덱스에 LF 로 넣고 작업 트리에는 CRLF 로 꺼내므로
    (이 저장소가 그렇다), 바이트를 그대로 대면 같은 내용인데 해시가 갈려 관문이 늘 막는다.
    줄끝 차이는 여기서 판정할 대상이 아니다 — 사람이 본 내용이 같은지만 본다."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:16]


def fingerprint(md: Path) -> str:
    """작업 트리의 내용. **도장을 찍을 때 쓴다** — 사람이 표에서 실제로 본 것이 이것이다."""
    return _digest(md.read_bytes())


def staged_fingerprint(md: Path) -> str:
    """**스테이징(인덱스)의 내용.** 관문은 이쪽을 본다.

    도장은 작업 트리에서 찍는데 관문이 그것을 그대로 대면, `git add -p` 로 조각만
    골라 담았을 때 **사람이 표에서 못 본 조합이 커밋된다** — 작업 트리 해시는 그대로라
    통과하고, 뚫려도 화면에 아무 표시가 없다. 이 관문이 지키는 것은 「사람이 본 내용과
    커밋된 내용이 같다」이므로 대는 쪽은 인덱스여야 한다 (2026-08-12 리뷰).

    `git show :<경로>` 가 인덱스의 blob 을 낸다. **바이트로 받는다** — 텍스트로 받으면
    파이썬이 또 한 번 줄끝을 손대서 무엇을 견주는지가 흐려진다.
    """
    rel = md.relative_to(ROOT).as_posix()
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "show", f":{rel}"],
        cwd=ROOT, capture_output=True, env=_clean_env(),
    )
    if r.returncode != 0:
        head = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise GitError(f"git show :{rel} 실패 — {head[0] if head else f'종료코드 {r.returncode}'}")
    return _digest(r.stdout)


def load_stamp() -> dict:
    """망가졌으면 빈 것으로 본다 — 기록이 없으면 막는 쪽이 안전한 방향이다."""
    try:
        return json.loads(STAMP.read_text(encoding="utf-8")).get("files", {})
    except (OSError, ValueError):
        return {}


def save_stamp(reviewed):
    """표로 보인 md 의 해시를 적는다. **작업 트리에서 사라진 것은 지운다** —
    커밋되거나 물려서 없어진 기록을 그대로 두면, 나중에 같은 경로에 다른 문서가
    들어왔을 때 옛 해시가 남아 있어 점검을 건너뛴 것이 통과할 수 있다."""
    live = {md.relative_to(ROOT).as_posix() for md, _ in changed_docs(None)}
    files = {p: h for p, h in load_stamp().items() if p in live}
    for md, _ in reviewed:
        files[md.relative_to(ROOT).as_posix()] = fingerprint(md)
    try:
        STAMP.write_text(json.dumps(
            {"reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
             "files": files},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        # 기록에 실패해도 표는 이미 사람이 봤다. 커밋이 막힐 뿐이니 사유만 알린다.
        print(f"\n! 점검 기록을 남기지 못했습니다 ({e}) — 커밋이 막히면 이 줄이 이유입니다",
              file=sys.stderr)


def gate() -> int:
    """커밋 직전 검사. 표를 안 거친 문서 md 가 담겨 있으면 멈춘다.

    **저장소를 못 읽은 것은 「볼 것 없음」이 아니다** — 그 경우도 멈춘다.
    """
    try:
        repo_check()
        docs = staged_docs()
    except GitError as e:
        print(f"\n커밋을 멈춥니다 — 관문이 저장소를 읽지 못했습니다.\n  · {e}", file=sys.stderr)
        print("  이 검사를 뺄 것이면 git config --unset core.hooksPath 하세요.", file=sys.stderr)
        return 1
    if not docs:
        return 0
    stamp = load_stamp()
    # **인덱스를 댄다 — 작업 트리가 아니다.** `git add -p` 로 조각만 담으면 작업 트리는
    # 표에서 본 그대로인데 커밋에는 다른 조합이 들어간다.
    try:
        stale = [md for md in docs
                 if stamp.get(md.relative_to(ROOT).as_posix()) != staged_fingerprint(md)]
    except GitError as e:
        print(f"\n커밋을 멈춥니다 — 관문이 스테이징된 내용을 읽지 못했습니다.\n  · {e}", file=sys.stderr)
        return 1
    if not stale:
        return 0
    print("\n커밋을 멈춥니다 — 7.5 건별 점검을 안 거친 문서 md 가 있습니다.", file=sys.stderr)
    for md in stale:
        rel = md.relative_to(ROOT).as_posix()
        after = "  (점검한 뒤에 또 고쳐졌습니다)" if rel in stamp else ""
        print(f"  · {rel}{after}", file=sys.stderr)
    print("\n  python .claude/skills/doc-archive/scripts/review_batch.py", file=sys.stderr)
    print("표를 사람에게 보이고 승인받은 다음에 커밋하세요 (SKILL.md 7.5단계).",
          file=sys.stderr)
    return 1


def added_entries(md: Path, is_new: bool):
    """이번에 들어간 회차 [(날짜, 원본파일명)]. 새 파일이면 전부, 아니면 diff 의 추가 줄."""
    if is_new:
        lines = md.read_text(encoding="utf-8").split("\n")
    else:
        # **`HEAD` 를 붙인다.** 그냥 `git diff` 는 스테이징 안 된 것만 본다 —
        # 이미 `git add` 한 뒤에 이 관문을 돌리면 회차를 하나도 못 찾고 **빈 표에
        # 「회차 0건」**을 내는데, 그것이 「볼 것 없음」과 같은 모양이라 그대로 커밋된다.
        rel = md.relative_to(ROOT).as_posix()
        lines = [l[1:] for l in git("diff", "HEAD", "-U0", "--", rel).splitlines()
                 if l.startswith("+") and not l.startswith("+++")]
    out = []
    for line in lines:
        m = ENTRY_RE.match(line)
        if m:
            # 헤더는 `**YYYY-MM-DD · 원본파일명**`. 가운뎃점 뒤가 원본이다.
            rest = line.strip().strip("*").split("·", 1)
            out.append((m.group(1), rest[1].strip() if len(rest) > 1 else ""))
    return out


# `git diff -U0` 의 덩이 머리 `@@ -a,b +c,d @@` 에서 **새 파일 쪽** 시작 줄과 줄 수.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_entries(md: Path):
    """헤더는 그대로인데 **본문이 바뀐** 회차 [(날짜, 원본파일명)].

    **왜 따로 있나 — 관문이 눈감던 자리다.** `added_entries` 는 diff 의 **추가된 줄에
    회차 헤더가 있을 때만** 회차를 잡는다. 그래서 이미 커밋된 문서의 표 안 값 하나를
    고치면(재변환·손수정) 헤더는 안 바뀌므로 회차가 0건이 되고, 표가 「못 찾음 ⚠」 한 줄만
    내면서 **마스킹·엑셀·본문 축을 통째로 건너뛰었다.** 어느 사업장의 산정내역 엑셀을
    재변환할 때 실제로 그랬고, 그때는 사람이 마스킹 검사를 따로 손으로 돌렸다.

    바뀐 줄 번호를 회차 헤더 줄 번호와 견주어 **그 줄이 속한 블록**을 되찾는다.
    메타 블록은 첫 회차 헤더보다 앞이라 여기서 안 잡힌다 — 그건 `meta_changed` 몫이고,
    그래야 「메타만 고친 것」과 「본문이 바뀐 것」이 계속 갈린다.
    """
    rel = md.relative_to(ROOT).as_posix()
    touched = set()
    for line in git("diff", "HEAD", "-U0", "--", rel).splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = 1 if m.group(2) is None else int(m.group(2))
        # 줄 수 0 은 **순수 삭제**다. 지워진 자리는 새 파일에 없으므로 그 앞 줄을 본다 —
        # 안 그러면 「표에서 한 행을 지운 것」이 아무 회차에도 안 걸린다.
        touched.update(range(start, start + count) if count else [start])
    if not touched:
        return []

    headers = []
    for i, line in enumerate(md.read_text(encoding="utf-8").split("\n"), start=1):
        if ENTRY_RE.match(line):
            rest = line.strip().strip("*").split("·", 1)
            headers.append((i, ENTRY_RE.match(line).group(1),
                            rest[1].strip() if len(rest) > 1 else ""))
    out = []
    for ln in sorted(touched):
        before = [h for h in headers if h[0] <= ln]
        if not before:
            continue  # 첫 회차보다 앞 — 메타 자리다
        entry = (before[-1][1], before[-1][2])
        if entry not in out:
            out.append(entry)
    return out


def meta_changed(md: Path, is_new: bool) -> bool:
    """메타 블록(파일 머리의 `>` 인용 줄) 전체가 HEAD 판과 지금 판 사이에 바뀌었나.

    **필드 목록이 아니라 블록 전체를 대는 이유** (WHK 결정 2026-09-03). 전에는
    열람·공개승인·비공개 사유·2차 머리글·변경추적 다섯 줄만 봤다. 그 목록에
    없는 칸(`주요 항목`·`수록 범위`·`마스킹`·`값 표기`…)만 고치면
    `changed_entries` 도 0건(회차 헤더는 안 바뀌었으니)·이 함수도 거짓이 나서,
    표가 「새 회차도 메타 변경도 못 찾았습니다 ⚠」를 내고 **매번 사람이
    `git diff HEAD` 를 손으로 열어야 했다.** 2026-09-03 에 자료 저장소 431개 md 의
    메타 블록을 전수로 뽑아 보니 실제로 쓰이는 칸이 27종이었다 — 목록을 늘리는
    쪽으로 고치면 스물여덟째 칸이 또 빠진다. 그래서 목록을 버리고 `meta_lines`
    (이 파일의 다른 곳·`verify_format.py`·`archive.js` 가 메타 블록을 가르는 것과
    같은 규칙)로 자른 블록 자체를 HEAD 판과 통째로 견준다. 새 칸이 늘어도 이
    함수는 안 고쳐도 된다.

    `git()` 이 실패하면(예: 저장소가 갈렸다) `GitError` 를 그대로 올린다 —
    `main()` 이 그 자리에서 잡아 트레이스백 대신 사유 한 줄로 멈춘다.
    """
    if is_new:
        return False
    rel = md.relative_to(ROOT).as_posix()
    head_meta = meta_lines(git("show", f"HEAD:{rel}").split("\n"))
    now_meta = meta_lines(md.read_text(encoding="utf-8").split("\n"))
    return head_meta != now_meta


# ── 봇 색인 문장 (모델이 쓴 요약 — 사람이 읽어야 커밋되는 자리) ──────────

# 회차 요약 표의 구분선(`|---|---|`). 구조 줄이라 사람이 대조할 내용이 없다.
_TABLE_SEP_RE = re.compile(r"^\|[\s|:\-]+\|$")

BRIEF_KEY = "주요 항목"


def _zone_line(line: str) -> bool:
    """머리 구역(첫 회차 헤더 앞)의 줄 중 **사람이 읽을 내용이 있는 줄**인가.

    메타(`>`)는 다른 축이 보고, 제목(`#`)·빈 줄·`---`·표 구분선은 구조다.
    남는 것이 회차 요약 표의 데이터 행과 그 절의 산문 — 둘 다 모델이 쓴다."""
    s = line.strip()
    if not s or s.startswith(">") or s.startswith("#") or s == "---":
        return False
    return not _TABLE_SEP_RE.match(s)


def _head_zone(lines):
    """첫 회차 헤더 앞의 읽을 줄들. 회차 요약 표·산문이 전부 여기 산다
    (`changed_entries` 가 「첫 회차보다 앞 — 메타 자리다」로 건너뛰는 바로 그 구역이다)."""
    out = []
    for line in lines:
        if ENTRY_RE.match(line):
            break
        if _zone_line(line):
            out.append(line.strip())
    return out


def brief_lines(now_lines, head_lines=None):
    """봇 색인·요약에 실리는 모델 문장 중 **이번에 새로 생기거나 바뀐 것**. [(라벨, 문장)].

    왜 이 함수가 있나 — 메타 `주요 항목` 은 일일 요약의 문서 소개(gist)에 쓰이고
    (색인 줄에는 2026-09-10 부터 안 실린다), 회차 요약 표의 칸은 봇이 추이 질문의
    근거로 읽는다. 둘 다 모델이 쓴 문장인데 어느 축에도 안 실려 **사람 눈을 한 번도 안
    거치고 커밋됐다** (2026-09-04 발견 — 한 회차에서 새 `주요 항목` 3건 + 표 2칸).

    **바뀐 줄만 낸다.** 전부 내면 손대지 않은 문서의 옛 문장까지 회차마다 반복돼 표가
    붇고, 긴 관문은 꺼진다. 실측(자료 저장소 최근 15커밋, 2026-09-04)으로 바뀐 줄만이면
    회차당 대개 0~21줄이고, 단발들을 시리즈로 묶어 요약 표를 새로 쓴 재편 회차 하나만
    54줄이었다 — 그때는 그 54줄이 전부 처음 사람 앞에 오는 문장이라 다 보이는 것이 맞다.

    **diff 가 아니라 줄 집합으로 견준다.** 지금 판의 머리 구역 줄 중 HEAD 판의 머리
    구역에 없는 줄이 「새로 생기거나 바뀐 것」이다 — 바뀐 칸은 그 행의 새 판이 여기
    걸린다. 훅 없이 순수 함수라 시험이 git 없이 돈다. 지워지기만 한 줄은 안 보인다 —
    이 절이 막는 것은 「틀린 문장이 들어가는 것」이고, 지워진 문장은 들어가지 않는다.

    `head_lines=None` 은 새 파일이다 — 전부 이번에 쓴 문장이므로 전부 낸다.
    """
    out = []
    key_now = parse_meta("\n".join(meta_lines(now_lines))).get(BRIEF_KEY)
    if head_lines is None:
        if key_now:
            out.append((BRIEF_KEY, key_now))
        out.extend(("회차 요약", l) for l in _head_zone(now_lines))
        return out
    key_old = parse_meta("\n".join(meta_lines(head_lines))).get(BRIEF_KEY)
    if key_now != key_old:
        if key_now:
            out.append((BRIEF_KEY, key_now))
        if key_old:
            # 옛 문장도 함께 — 무엇에서 무엇으로 갔는지 없이는 대조가 안 된다.
            out.append((f"{BRIEF_KEY} (전 판)", key_old))
    old_zone = _head_zone(head_lines)
    out.extend(("회차 요약", l) for l in _head_zone(now_lines) if l not in old_zone)
    return out


def brief_for(md: Path, is_new: bool):
    """이 md 의 봇 색인 문장. HEAD 판을 못 읽으면(스테이징된 이름 변경 등) 새 파일로 본다 —
    그 경로의 문장은 전부 이 자리에서 처음 사람 앞에 오는 것이 맞다."""
    now_lines = md.read_text(encoding="utf-8").split("\n")
    if is_new:
        return brief_lines(now_lines)
    rel = md.relative_to(ROOT).as_posix()
    try:
        head_lines = git("show", f"HEAD:{rel}").split("\n")
    except GitError:
        return brief_lines(now_lines)
    return brief_lines(now_lines, head_lines)


# ── 축별 판정 ────────────────────────────────────────────────────────────

def read_meta(md: Path) -> dict:
    """메타 블록을 `{키: 값}` 으로. **파서를 다시 짜지 않는다** —
    `verify_format` 의 것을 그대로 쓴다(그것이 `archive.js` 의 `metaBlock` 과 맞춰져 있다).
    여기서 한 벌 더 만들면 봇과 열람 판정이 갈린다."""
    lines = md.read_text(encoding="utf-8").split("\n")
    return parse_meta("\n".join(meta_lines(lines)))


def access_cell(md: Path, meta: dict):
    """열람 판정. 메타 두 줄과 **채널의 비공개 여부**를 함께 본다."""
    access = meta.get("열람") or "없음"
    approved = bool(meta.get("공개승인"))
    is_private_ch = md.parent.name in private_channels()

    if access == "없음":
        return "없음 ⚠", "메타에 `열람` 이 없습니다 — fail-closed 로 비공개가 되지만 '정한 것'과 구분이 안 됩니다"
    if is_private_ch and access == "공개" and not approved:
        return "공개 ⚠", "비공개 채널 문서인데 `공개승인` 이 없습니다 — 봇은 계속 막습니다"
    if is_private_ch and access == "공개" and approved:
        return "공개(승인)", None
    if not is_private_ch and approved:
        return f"{access} ⚠", "공개 채널 문서에 `공개승인` 이 붙어 있습니다 — 검사가 ✗ 를 냅니다"
    if is_private_ch and access == "비공개":
        return "비공개", None
    return access, None


def entry_span(lines, entry_date: str, source: str):
    """그 회차 본문의 [시작, 끝) — 0부터 세는 줄 인덱스. 못 찾으면 None."""
    start = next(
        (i for i, l in enumerate(lines)
         if (m := ENTRY_RE.match(l)) and m.group(1) == entry_date and (not source or source in l)),
        None,
    )
    if start is None:
        return None
    end = next((j for j in range(start + 1, len(lines))
                if ENTRY_RE.match(lines[j]) or lines[j].startswith("## ")), len(lines))
    return start + 1, end


def entry_block(md: Path, entry_date: str, source: str) -> str:
    lines = md.read_text(encoding="utf-8").split("\n")
    span = entry_span(lines, entry_date, source)
    return "\n".join(lines[span[0]:span[1]]) if span else ""


def added_lines(md: Path, is_new: bool):
    """이번에 **새로 들어온** 줄 번호(1부터). 새 파일이면 None = 「전부 새것」.

    회차 본문 전체가 아니라 이 줄들만 뭉침 검사에 먹인다. 왜냐하면
    `changed_docs` 의 판정 단위가 **파일**이라, 시리즈 md 에 새 회차 하나를 넣을
    때마다 **그 파일의 옛 회차에 남은 유산 뭉침까지** ⚠ 로 올라오기 때문이다.
    미처리 유산이 아카이브 전체에 254칸이라 그러면 회차마다 되풀이되는, 그 자리에서
    못 고치는 ⚠ 가 된다 — **성가신 관문은 꺼진다**(이 저장소의 기존 판단, 이 파일
    위쪽 「봇 색인 문장」 절에도 같은 이유가 적혀 있다). 범위를 좁히는 것이 이 축의
    **성립 조건**이다.

    `-U0` 덩이 머리의 새 파일 쪽 시작·줄 수를 쓴다(`changed_entries` 와 같은 방식).
    **줄 수 0 은 순수 삭제**라 새로 들어온 줄이 없다 — 여기서는 건너뛴다
    (`changed_entries` 는 「지운 자리가 어느 회차인가」를 찾느라 앞 줄을 잡지만,
    지워진 줄에는 검사할 내용이 없다).
    """
    if is_new:
        return None
    rel = md.relative_to(ROOT).as_posix()
    out = set()
    for line in git("diff", "HEAD", "-U0", "--", rel).splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        count = 1 if m.group(2) is None else int(m.group(2))
        if count:
            out.update(range(start, start + count))
    return out


def merged_cell(lines, span, touched):
    """(칸, 사유) — 이번에 들어온 줄 안에 숫자가 뭉친 표 칸이 있나.

    **갈래 ① 까지 본다** (`with_ledger=True`). 잣대의 기본값은 ② 전용인데,
    그것은 옛 판정표의 숫자를 재현하려는 값이지 사람이 볼 범위가 아니다 —
    실사고 값 `1,000900` 이 바로 ① 꼴이라 ② 만 보면 그 건을 놓친다
    (2026-09-21, 픽스처 시험이 이 전제를 깨뜨려 알게 됐다).

    **막지 않는다.** 이 관문의 종료코드는 0 그대로다. 표 칸의 값이 진짜 뭉친
    것인지 아닌지는 원본과 대보는 사람만 가를 수 있고, 7.5 는 **10단계(원본 정리)
    전**이라 그 원본이 아직 `~/.doc-cache` 에 있다. 가를 수 없으면 값을 고치는
    대신 메타에 경고를 다는 출구도 있다 (강릉 Tr.A 소계 전례, WHK 결정 2026-09-21).
    """
    if span is None:
        return "—", None
    # **글자는 회차 본문을 통째로 먹인다** — 추가된 줄만 이어 붙이면 여러 줄에 걸친
    # 칸이 정규식에 안 잡혀 표가 `✓`(=「검사했고 깨끗하다」)를 찍는다. 그 칸이
    # 아카이브에 71파일·1,114칸 있고, 한 칸이 134줄인 것도 있다 (2026-09-21 실측).
    # 좁히는 일은 아래에서 **칸이 걸친 줄 범위**로 한다.
    base = span[0] + 1  # 본문 첫 줄의 파일 줄 번호 (1부터)
    if touched is not None and not any(n in touched for n in range(base, span[1] + 1)):
        # 이 회차에는 이번에 들어온 줄이 없다. **`✓` 가 아니라 `—` 다** —
        # 「봤는데 깨끗하다」와 「볼 것이 없었다」는 다른 말이다.
        return "—", None
    hits = merged.scan_text("\n".join(lines[span[0]:span[1]]), with_ledger=True)
    if touched is not None:
        hits = [h for h in hits
                if any((base + n - 1) in touched for n in range(h[0], h[1] + 1))]
    if not hits:
        return "✓", None
    toks = [t for _, _, ts in hits for t in ts]
    shown = " · ".join(toks[:3]) + (f" 외 {len(toks) - 3}건" if len(toks) > 3 else "")
    # 그래프 눈금 꼴이면 그 말을 덧붙이되 **⚠ 는 그대로 둔다.** 이 판정으로 ⚠ 를
    # 내렸더니 어느 매매계약서의 진짜 금액 뭉침이 「눈금」으로 묻혔다 (회의적 검증).
    axis = " (그래프 눈금 꼴이 섞여 있습니다 — 표의 금액이 아니면 그대로 둡니다)" \
        if any(merged.looks_like_chart_axis(t) for t in toks) else ""
    return f"{len(hits)}칸 ⚠", (
        f"표 칸에 숫자가 붙어 있습니다: {shown}{axis} — 원본이 아직 `~/.doc-cache` 에 "
        "있을 때 대조해 가르세요. 가를 수 없으면 값을 고치지 말고 메타 `수록 범위` 에 "
        "경고를 다세요 (kordoc 4.14.0 도 이 뭉침을 만듭니다 — 다시 변환해도 안 고쳐집니다)")


def body_cell(block: str):
    text = IMG_RE.sub("", block).strip()
    imgs = len(IMG_RE.findall(block))
    n = len(text)
    if imgs and n < MIN_BODY:
        return f"{n:,}자 ⚠", f"이미지 {imgs}개뿐이고 본문이 {n}자입니다 — kordoc 에 `--ocr` 를 붙여 다시 변환하세요"
    if n < MIN_BODY:
        return f"{n:,}자 ⚠", f"본문이 {n}자뿐입니다 — 변환이 반쯤 실패했는지 원본과 대조하세요"
    return f"{n:,}자", None


_cache_index = None


def cache_index():
    """캐시 원본을 `safe_name` 으로 찾을 수 있게 훑어 둔다. 저장 이름은 `<파일ID>_<안전한이름>`.

    캐시는 **채널 폴더별**로 쌓인다 (`fetch_slack_files.py` 의
    `out_dir / safe_name(채널) / f"{id}_{safe_name(이름)}"`). 파일명 하나만 키로 쓰면
    `주간보고.docx` 처럼 사업장마다 있는 이름에서 **먼저 걸린 채널이 이겨**, 두 번째
    문서를 남의 사업장 원본과 대보게 된다. 그래서 이름 하나에 후보를 전부 담아 두고,
    고르는 일은 `cache_origin` 이 채널로 한다.
    """
    global _cache_index
    if _cache_index is None:
        _cache_index = {}
        if CACHE.exists():
            # `.blocks` 가 캐시 루트에서 채널 폴더 **안**으로 옮겨진 뒤로, 이 rglob 은
            # `.blocks` 산출물까지 걸어 들어간다. 지금은 무해하다 — 산출물 이름이
            # `sheet-01.md`·`meta.json` 이라 아래 `"_" in p.name` 을 안 통과해 색인에
            # 안 들어간다. `xlsx_to_blocks.py` 가 언젠가 `_` 가 든 파일명을 쓰기
            # 시작하면 그 산출물이 「원본 후보」로 잘못 색인되고, 그때 `p.parent.name`
            # 은 채널이 아니라 `<파일ID>_<원본명>.xlsx.blocks` 라 `cache_origin` 이
            # 채널로 못 좁혀 허위 「원본을 못 골랐습니다」를 낸다.
            for p in CACHE.rglob("*"):
                if p.is_file() and "_" in p.name:
                    _cache_index.setdefault(p.name.split("_", 1)[1], []).append(p)
    return _cache_index


def cache_origin(source: str, channel: str | None):
    """(원본 경로, 못 고른 이유). 여럿인데 채널로 못 좁히면 **아무것도 안 고른다.**

    짐작으로 고르면 남의 사업장 원본과 대보고 「원본 주석 N자가 md 에 없습니다」라는
    허위 경고를 내거나, 반대로 진짜 누락을 조용한 `—` 로 덮는다. 후자가 이 검사 축을
    만든 이유 그 자체라, 모르면 모른다고 말한다.
    """
    # 엑셀 회차의 `source` 에는 `— 시트 n/N: 이름` 이 붙어 있는데 캐시에 저장된 이름에는
    # 없다. 떼지 않으면 원본이 눈앞의 캐시에 있어도 못 찾고, `comment_cell` 이
    # 「원본이 캐시에 없어」라는 **틀린 사유**를 낸다 (2026-08-23 실제 발생).
    hits = cache_index().get(safe_name(source_base(source))) or []
    if not hits:
        return None, None
    if len(hits) == 1:
        return hits[0], None
    same = [p for p in hits if p.parent.name == safe_name(channel or "")]
    if len(same) == 1:
        return same[0], None
    where = ", ".join(sorted({p.parent.name for p in hits}))
    return None, (f"같은 이름의 캐시 원본이 {len(hits)}건이라 어느 사업장 것인지 "
                  f"정하지 못했습니다 ({where}) — 슬랙 원본을 열어 확인하세요")


def looks_like_review(source: str) -> bool:
    """이름이 **「원본 문서에 의견을 덧붙인 판」**이라고 말하나.

    이 검사가 지키려는 것은 **덧붙인 의견이 변환에서 사라지는 것**이다
    (2026-08-05: 계약서에 붙은 검토의견 886자·1,026자가 통째로 빠질 뻔했다).
    그래서 걸어야 할 이름은 `계약서…_검토의견 추가` 처럼 **바탕 문서 + 덧붙임**이다.

    **낱말 `의견` 만으로 걸면 안 된다.** `법률검토의견서`·`<법무법인>의견`·`의견 요청의 건` 은
    의견 자체가 그 문서라 본문에 다 있고 잃을 것이 없는데, 전부 ⚠ 가 된다. 그리고 이
    판정은 **워드가 아닐 때만** 쓰이므로(워드는 `comments.xml` 을 직접 읽는다) 그 ⚠ 는
    도구가 영영 해소하지 못한다. 고칠 수 없는 ⚠ 가 쌓이면 사람이 ⚠ 자체를 안 본다 —
    이 축을 만든 이유가 거기서 무너진다.

    2026-08-12 에 아카이브 전수(원본 371건)로 맞춰 좁혔다. 그때 이름에 `의견`류가 든
    것이 11건이었고, 덧붙임은 3건(전부 워드), 나머지 8건은 의견서·의견 요청 문서였다.
    """
    s = source.replace(" ", "").lower()
    return any(w in s for w in ("의견추가", "의견반영", "markup", "코멘트", "comment"))


def comment_cell(source: str, block: str, channel: str | None = None):
    """원본 워드의 주석이 md 에 실렸나. 원본이 캐시에 없으면 판정하지 않고 '—' 를 낸다."""
    if not source:
        return "—", None
    origin, ambiguous = cache_origin(source, channel)
    if ambiguous:
        return "? ⚠", ambiguous
    if origin is None or origin.suffix.lower() not in (".docx", ".doc"):
        # 캐시가 이미 비워졌거나 워드가 아니면 볼 수 있는 것이 없다. **모른다고 말한다.**
        if not (looks_like_review(source) and COMMENT_SECTION not in block):
            return "—", None
        # **두 상태를 갈라서 말한다.** 전에는 한 문장으로 묶여 있어서, 원본이 눈앞의
        # 캐시에 있는데도 「캐시에 없어 확인하지 못했습니다」가 붙었다 (2026-08-12,
        # 사업장다 의 pdf). 사유가 틀리면 사람이 캐시를 뒤지거나 원본을 다시
        # 받아오는 엉뚱한 곳을 본다 — 고칠 수 없는 ⚠ 가 쌓이면 ⚠ 자체를 안 보게 된다.
        if origin is None:
            why = "원본이 캐시에 없어 확인하지 못했습니다. 슬랙 원본을 열어 보세요"
        else:
            why = (f"원본이 워드가 아니라({origin.suffix.lower()}) 워드 주석은 있을 수 "
                   "없습니다. 그 포맷의 주석은 이 검사가 못 읽으니 슬랙 원본을 열어 보세요")
        return "? ⚠", f"이름에 '검토의견'이 들어 있는데 md 에 주석 절이 없습니다 — {why}"
    try:
        with zipfile.ZipFile(origin) as z:
            if "word/comments.xml" not in z.namelist():
                return "—", None
            raw = z.read("word/comments.xml").decode("utf-8", "ignore")
        n = len(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip())
    except (zipfile.BadZipFile, KeyError, OSError):
        return "—", None
    if n == 0:
        return "—", None
    if COMMENT_SECTION in block:
        return f"{n:,}자 ✓", None
    return f"{n:,}자 ⚠", (f"원본 주석 {n:,}자가 md 에 없습니다 — 그 파일을 올린 이유가 대개 여기 있습니다. "
                          "`w:author` 와 함께 뽑아 회차 끝에 「### 문서 주석 (검토의견)」 절로 붙이세요")


def mask_hits(block: str):
    """마스킹 안 된 값들. `(라벨, 값)` 목록을 **나온 순서대로** 돌려준다.

    **라벨당 첫 건만 보고하면 안 된다.** 같은 라벨로 둘이 걸리면 뒤엣것이
    화면에서 통째로 사라진다 — 2026-08-15 에 사업장바 인감 절의 진짜
    주민등록번호가 앞 법인등록번호에 밀려 안 보였다."""
    out = []
    for label, pat in INLINE_PATTERNS:
        seen = []
        for m in pat.finditer(block):
            v = m.group(0)
            if "*" in v or v in seen:
                continue
            seen.append(v)
            out.append((label, v))
    if ACCOUNT_WORD.search(block):
        seen = []
        for m in ACCOUNT_NUM.finditer(block):
            v = m.group(0)
            if "*" in v or v in seen or looks_like_date(v):
                continue
            seen.append(v)
            out.append(("계좌", v))
    return out


# 화면에 읽기 실패를 알릴 때 쓰는 표시 이름. `mask_hits` 가 쓰는 여섯 라벨과는
# 다른 성격이다 — 이건 hits 항목의 라벨이 아니라 read_failures 항목을 찍을 때만 쓴다.
READ_FAIL_LABEL = "읽기 실패"


def sweep_masks(root):
    """`root` 아래 모든 md 를 줄 단위로 훑어 마스킹 안 된 값을 찾는다.

    `(hits, files_scanned, read_failures)` 를 돌려준다.

    **훑은 파일 수를 함께 돌려준다.** 안 그러면 "0건"과 "안 셌다"가 같은
    모양이 되는데, 그게 이 저장소가 반복해서 겪은 조용한 실패다.

    **읽기 자체가 실패해도(인코딩·권한 등) 조용히 건너뛰지 않는다** (2026-08-26).
    다만 **`hits` 에는 안 담는다** — `hits` 는 "잔여 개인정보" 목록이고, 읽기 실패는
    개인정보가 아니다. 섞으면 `len(hits)` 가 진짜 잔여 건수를 부풀리고, 다음 작업이
    전후 비교 기준값으로 이 수를 쓸 때 라벨로 걸러도 안 걸러진다 — 리뷰에서 지적된
    바로 그 문제(2026-08-26). 그래서 `read_failures` 를 따로 돌려주고, 그 파일도
    "훑은 파일 수"에는 들어간다 — 시도는 했으니까."""
    hits, failures, n = [], [], 0
    for md in sorted(Path(root).rglob("*.md")):
        n += 1
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            failures.append({"file": str(md), "reason": type(e).__name__})
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            for label, value in mask_hits(line):
                hits.append({"file": str(md), "line": i, "label": label, "value": value})
    return hits, n, failures


def sweep_footer(n_files: int, n_hits: int, n_failures: int, by_label: dict) -> str:
    """`--mask-sweep` 의 마지막 줄들. **한계 문장은 잔여가 0건이어도 찍는다.**

    0건일 때 안 찍으면 정확히 그때 오해가 난다 — 훑기가 「없다」고 말한 것을
    「안전하다」로 읽고 커밋한다. 2026-08-26 에 훑기가 「잔여 3건」이라 한 상태에서
    7.5 점검표가 **진짜 은행 계좌 11건**을 더 찾았다. 같은 `mask_hits` 인데
    훑기는 **한 줄**씩, 점검표는 **회차 블록 전체**를 먹여서 나는 차이다.
    표에서는 「계좌」 낱말이 머리글 칸에 있고 값은 다른 칸·다른 줄에 있다.
    """
    labels = " · ".join(f"{k} {v}" for k, v in sorted(by_label.items())) or "없음"
    return (
        f"\n■ md {n_files}개를 훑어 {n_hits}건 · 읽기 실패 {n_failures}건 — {labels}\n"
        "  이 훑기는 줄 단위다 — 표에서 「계좌」 낱말이 머리글 칸에 있고 값이 다른 칸에\n"
        "  있으면 못 본다. 커밋 전에는 이 값이 아니라 7.5 점검표를 봐라."
    )


def mask_cell(block: str):
    hits = []
    by_label = {}
    for label, v in mask_hits(block):
        by_label.setdefault(label, []).append(v)
    for label, vals in by_label.items():
        for v in vals[:3]:
            # 앞 6자리까지만 적는다 — 화면·로그에 남는 사본을 늘리지 않는다.
            hits.append(f"{label}({v[:6]}…)")
        if len(vals) > 3:
            hits.append(f"{label} 외 {len(vals) - 3}건")
    if not hits:
        return "✓", None
    return "⚠ " + hits[0].split("(")[0], "마스킹 안 된 값: " + " · ".join(hits) + " (5단계 규칙 4)"


def norm_name(name: str) -> str:
    """회차 이름에서 날짜·기호를 걷어낸 비교용 이름. **확장자 없는 이름을 받는다.**

    `Path(name).stem` 을 쓰지 않는다 (2026-08-15). 받는 값이 이미 `md.stem` 이라
    한 번 더 걸면 **이름 가운데의 점 뒤가 통째로 날아간다** — `사업장타 Tr.A 000억` 과
    `사업장타 Tr.B 111억` 이 둘 다 `사업장타tr` 이 되어, `series_cell` 이 서로 다른 문서를
    「같은 정규화명 3건째」로 잘못 ⚠ 한다. 점이 든 업무 표기(Tr.A)는 실제로 쓴다.
    `fetch_slack_files.py` 의 `stem` 에서 만난 것과 같은 함정이다 — 이름은 경로가 아니다.
    """
    return NON_WORD.sub("", DATE_TOKENS.sub("", name)).lower()


def source_base(source: str) -> str:
    """회차 원본명에서 시트 표기(`— 시트 n/N: 이름`)를 뗀 이름.

    엑셀은 시트마다 회차 블록이 되고, 그 회차들은 **일부러 같은 날짜에 서로 다른
    `source`** 를 쓴다 (SKILL.md 「엑셀 변환」 — `--source` 에 시트 이름을 안 넣으면
    `insert_entry.py` 의 중복 판정이 2번째 시트부터 조용히 SKIP 한다). 그래서
    「같은 날짜의 다른 원본 = 개정본?」 판정은 **시트 표기를 뗀 뒤에** 견준다.
    안 그러면 엑셀 문서마다 시트 수만큼 가짜 ⚠ 가 떠서 「깨끗한 건은 ✓ 한 줄」이
    무너지고 진짜 경고가 묻힌다 (2026-08-23 어느 사업장의 산정내역 엑셀 실물 4시트
    전부가 걸렸다).

    엑셀이 아닌 회차는 시트 표기가 없어 원본명 그대로 돌아온다 — 판정이 안 바뀐다.
    """
    return SHEET_SRC_RE.split(source, maxsplit=1)[0]


def series_cell(md: Path, entry_date: str, source: str, all_entries):
    base = source_base(source)
    same_date = [s for d, s in all_entries if d == entry_date and source_base(s) != base]
    n = len(all_entries)
    if n >= 2:
        cell = f"회차 {n}건"
    else:
        # 단발. 같은 사업장에 같은 정규화명이 더 있으면 3건째부터 시리즈로 묶는다.
        kin = [p for p in md.parent.glob("*.md") if p != md and norm_name(p.stem) == norm_name(md.stem)]
        cell = f"단발 (같은 이름 {len(kin) + 1}건) ⚠" if len(kin) + 1 >= 3 else "단발"
    if same_date:
        return cell + " · 교체? ⚠", (f"같은 날짜에 다른 원본의 회차가 있습니다: {same_date[0]} — "
                                     "개정본이면 옛 회차를 `decide.py --undo` 로 빼세요")
    if cell.endswith("⚠"):
        return cell, "같은 정규화명이 3건째입니다 — 시리즈 md 로 묶을 자리입니다 (6단계)"
    return cell, None


# ── 엑셀 시트 판정 ───────────────────────────────────────────────────────

def fallback_meta_note(fallback_sheets, meta_value):
    """2차로 잡은 머리글이 md 메타에 적혀 있나. 안 적혔으면 적을 문장을 돌려준다.

    **적어 두지 않으면 이 사실이 그 회차와 함께 사라진다.** `header_fallback_sheets`
    는 캐시 `meta.json` 에만 있고 `prune_cache` 가 커밋 뒤에 지운다. 그래서 「몇 주
    보고 잦으면 2차 판정을 좁힐지 정한다」를 나중에 셀 재료가 md 밖에는 없다.

    **순서와 공백은 안 본다.** 사람이 손으로 적는 줄이라 순서가 흔들리는데, 그걸
    「다르다」로 잡으면 고칠 방법이 없는 거짓 실패가 된다.
    """
    if not fallback_sheets:
        return None
    said = [s.strip() for s in (meta_value or "").split("·") if s.strip()]
    if sorted(said) == sorted(fallback_sheets):
        return None
    want = " · ".join(fallback_sheets)
    now = f" (지금 적힌 것: {' · '.join(said)})" if said else ""
    return f"메타에 적어 두세요 — `> **2차 머리글**: {want}`{now}"


def track_change_note(source: str, meta_value):
    """변경추적이 켜진 hwp 원본을 사람 눈으로 보내는 줄. 볼 것 없으면 None.

    **왜 막지 않고 보이기만 하나** (WHK 결정 2026-09-03, 선택지 ⓑ). 변환을 막으면
    그 문서가 통째로 아카이브에 안 들어가고 봇은 「자료가 없습니다」로 답한다 —
    같은 날 kordoc 건에서 본 실패 모양 그대로다. 그래서 넣되 사람이 반드시 보게 한다.

    **왜 hwp 만 보나.** docx 는 4단계에 `w:ins`·`w:del` 을 세는 자리가 원래 있었고,
    hwp·hwpx 에만 그 자리가 없어서 2026-08-28 에 겹친 숫자 9곳이 그대로 실렸다
    (`3,100845` → 3,845억 · `812804` → 804). **한글 화면은 「최종본」으로 감춰 보여주므로
    사람이 원본을 열어도 안 보이고, 취소선 속성도 아니라 취소선을 찾는 검사로는 못 잡는다.**

    **「안 쟀다」와 「없다」를 가른다.** 메타가 없으면 그렇게 말한다 — 0건과 같은
    화면으로 두면 안 센 회차가 통과로 읽힌다. 이 관문은 **이번 배치에 새로 들어오거나
    다시 변환된 회차**만 보므로, 이미 들어가 있는 hwp 문서 104개가 매번 울리지는 않는다.
    """
    if not re.search(r"\.hwpx?$", source, re.I):
        return None
    if not meta_value:
        return ("원본이 hwp 인데 `변경추적` 메타가 없습니다 — 4단계에서 "
                "`python .claude/skills/doc-archive/scripts/count_track_changes.py <파일>` 을 "
                "돌리고 그 결과를 `> **변경추적**: …` 로 적으세요. 「안 쟀다」와 「없다」는 다릅니다")
    # 「0건」이라고 적혀 있으면 볼 것이 없다. `10건`·`130건` 이 0 으로 읽히지 않게 앞자리를 막는다.
    if re.search(r"(?<!\d)0\s*건", str(meta_value)):
        return None
    return (f"변경추적이 켜진 원본입니다 (`변경추적`: {meta_value}) — 지운 값과 새 값이 붙은 "
            "숫자가 본문에 있는지 봅니다 (`3,100845` 처럼 자릿수가 이상한 것도, "
            "`812804` 처럼 그냥 숫자로 보이는 것도 있습니다). **한글 화면에는 안 보이고 "
            "취소선도 아닙니다**")


def excel_meta_json(source: str, channel: str | None):
    """`xlsx_to_blocks.py` 가 남긴 `meta.json` 을 (내용, 못 읽은 사유) 로.

    자리는 **원본 파일 바로 옆**이다 — `<원본경로>.blocks/meta.json`.
    원본 경로가 이미 채널과 파일 ID 를 갖고 있어(`<채널>/<파일ID>_<이름>`)
    같은 이름 파일이 두 채널에 있어도 안 겹친다. 2026-08-25 까지는
    `~/.doc-cache/<원본명>.blocks/` 라 채널이 이름에 없었고, 그래서
    나중에 변환한 쪽이 앞엣것을 **에러 없이 덮었다.**

    원본을 찾는 일은 `cache_origin` 에 맡긴다 — 이름+채널로 고르고,
    여럿이라 못 고르면 짐작하지 않고 사유를 낸다. 같은 판정을 두 벌
    만들면 조용히 갈린다.

    **못 찾으면 조용히 넘어가지 않는다.** 10단계(원본 정리) 뒤거나 다른
    세션에서 변환했으면 이미 지워졌을 수 있는데, 그때 `✓` 를 찍으면
    「보고 깨끗했다」와 「아무것도 못 봤다」가 화면에서 같은 모양이 된다.
    상한 초과로 뺀 시트·숨김·서식 미해석은 전부 이 파일에만 있으므로,
    못 읽었으면 그 셋을 **판정 못 한 것**이다.
    """
    orig, why = cache_origin(source, channel)
    if orig is None:
        return None, (why or "원본이 캐시에 없음 — 점검 못 함")
    p = orig.with_name(orig.name + ".blocks") / "meta.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")), None
    except OSError:
        return None, "meta.json 없음 — 점검 못 함"
    except ValueError:
        return None, "meta.json 을 읽지 못함 — 점검 못 함"


def excel_cell(meta: dict, all_entries, block: str, channel: str | None = None):
    """엑셀 시트 판정. (칸, 파일 단위 사유, 회차 단위 사유)

    문서에 메타 `시트` 도 없고 본문에 시트 헤더도 없으면(엑셀 문서가 아니면) 판정하지
    않는다 — `comment_cell` 과 같은 원칙이다. 파일 단위 사실(시트 개수 불일치·상한
    초과로 뺀 시트·숨김·서식 미해석)은 회차마다 똑같으므로 **호출부가 첫 회차에서만
    notes 에 적는다**(`file_reported`) — 칸 값은 매 회차 행에 그대로 보인다.

    회차(오류 셀) 는 그 시트의 본문에서만 찾으므로 회차마다 다를 수 있어 매번 본다.

    **본문에 시트 헤더가 있는데 메타 `시트` 줄이 없으면 그 자체가 ⚠ 다.** 봇의
    `isSheetDoc`(documents.js)이 그 한 줄로만 엑셀을 가르기 때문에, 없으면 색인이
    시트를 회차로 세어 그 문서가 **영원히 안 접히는 줄**이 된다(6,000자 색인에서
    다른 문서의 자리를 밀어낸다). 예전에는 여기서도 `verify_format.py` 에서도 키가
    없으면 아무 검사도 안 돌아, 빠뜨려도 어디에서도 안 드러났다.
    """
    sheet_meta_val = meta.get("시트")
    body_sheets = [s for _, s in all_entries if SHEET_SRC_RE.search(s)]
    if not sheet_meta_val:
        if body_sheets:
            why = (f"본문에 시트 헤더가 {len(body_sheets)}건인데 메타 '**시트**' 줄이 "
                   "없습니다 — 봇이 시트를 회차로 세어 색인에서 영원히 안 접힙니다")
            return "시트 메타 없음 ⚠", why, None
        return "—", None, None

    # `file_notes` 는 **⚠ 를 띄우는 사유**(사람이 봐야 하는 것)이고, `quiet_notes` 는
    # 사실이지만 사람이 할 일이 없는 것이다. 갈라 두는 이유는 아래 「자른 시트」
    # 자리에 적어 두었다.
    file_notes = []
    quiet_notes = []
    want = [s.strip() for s in sheet_meta_val.split("·") if s.strip()]
    got = len(all_entries)
    if len(want) != got:
        file_notes.append(f"시트 메타 {len(want)} ≠ 본문 {got}")

    base = re.split(r" — 시트 \d+/\d+: ", all_entries[0][1], maxsplit=1)[0] if all_entries else ""
    mj, mj_why = excel_meta_json(base, channel) if base else (None, "회차가 없어 원본명을 못 찾음 — 점검 못 함")
    if mj is None:
        file_notes.append(mj_why)
    else:
        # **못 자른 것이 더 봐야 할 쪽이다** — 그 시트는 유령 행을 그대로 안고
        # 로드되므로, 파일이 크면 다음번에 또 터진다. 사유가 「되돌렸다」면
        # 자르는 코드의 결함이니 넘어가지 말고 원인을 찾는다.
        untrimmed = mj.get("untrimmed_sheets") or []
        if untrimmed:
            names = " · ".join(
                f"{s['name']}({s['ghost_rows']:,}행 — {s['why']})" for s in untrimmed
            )
            file_notes.append(f"못 자른 유령 행: {names}")
        skipped = mj.get("skipped_sheets") or []
        if skipped:
            names = " · ".join(f"{s['name']}({s['rows']:,}행)" for s in skipped)
            file_notes.append(f"상한 초과로 뺀 시트: {names}")
        # **상한 초과로 뺀 시트와는 다른 실패다.** 저건 「너무 커서 못 담았다」고 이건
        # 「개인정보가 있는데 머리글을 못 찾아 가릴 방법이 없어 통째로 뺐다」다 — Task 4 가
        # 막으려던 「조용히 실림」이 「조용히 빠짐」으로 자리만 옮긴 것이라, 안 보이면
        # 그 사이 아무도 모른다. 문구를 갈라야 사람이 다른 행동을 한다: 상한 초과는
        # `--max-rows` 를 올릴지 말지를 정하는 문제고, 이건 머리글을 손으로 넣거나
        # 그 시트를 직접 열어 개인정보를 확인하는 문제다.
        unmaskable = mj.get("unmaskable_sheets") or []
        if unmaskable:
            names = " · ".join(f"{s['name']}({s['why']})" for s in unmaskable)
            file_notes.append(f"가릴 수 없어 뺀 시트: {names}")
        # **뺀 것과 다른 종류다.** 저건 「안 실렸다」고 이건 「실렸는데 머리글을
        # 낱말 하나로 잡았다」다 — 진짜 머리글이 그 아래에 있으면 주소·계좌 열이
        # 안 가려진 채 실린다. 막지 않고 보이기만 한다(이 도구는 판정하지 않는다).
        fallback_sheets = mj.get("header_fallback_sheets") or []
        if fallback_sheets:
            file_notes.append(
                "낱말 1개짜리로 머리글을 잡은 시트: " + " · ".join(fallback_sheets)
                + " — 그 시트의 머리글 줄이 맞는지 열어 본다"
            )
        note = fallback_meta_note(fallback_sheets, meta.get("2차 머리글"))
        if note:
            file_notes.append(note)
        hidden_sheets, hidden_rows = mj.get("hidden_sheets") or [], mj.get("hidden_rows") or 0
        if hidden_sheets or hidden_rows:
            # **숨긴 행 수와 「내용 있는 것」은 뜻이 다르다.** 이 줄의 용도가 「버린 것 중
            # 진짜 내용이 얼마나 되나」를 사람에게 알리는 것이라, 개수만 적으면 판단할
            # 근거가 없다 — 65개가 전부 빈 줄인 것과 46개에 내용이 있는 것은 다른 상황이다.
            with_content = mj.get("hidden_rows_with_content")
            rows_txt = f"숨긴 행 {hidden_rows}"
            if hidden_rows and with_content is not None:
                rows_txt += f"(내용 있는 것 {with_content})"
            file_notes.append(f"숨긴 시트 {len(hidden_sheets)} · {rows_txt}")
        unformatted = mj.get("unformatted_cells") or 0
        if unformatted:
            # **개수만 적으면 무엇을 봐야 할지 알 수 없다.** 한 파일이 14.69% 였을 때
            # 그것이 전부 한 원인(openpyxl 이 못 푼 내장 서식)이었는데 표에는 안 보였다.
            # 위 「숨긴 행 65(내용 있는 것 46)」·아래 「오류 셀 N개 (#DIV/0!)」와 같은 자리다.
            #
            # 사유는 `xlsx_to_blocks` 가 판정하는 그 순간에 적어 보낸다 — 여기서 셀을 보고
            # 다시 알아내지 않는다. 그러면 판정이 두 벌이 되어 조용히 갈린다.
            why = mj.get("unformatted_reasons") or {}
            if why:
                # 많은 사유부터. 셋을 넘으면 접는다 — 표의 한 줄이라 길면 안 읽힌다.
                top = list(why.items())[:3]
                txt = " · ".join(f"{name} {n}" for name, n in top)
                if len(why) > 3:
                    txt += f" · 그 외 {len(why) - 3}갈래"
                file_notes.append(f"서식 해석 못 함 {unformatted}셀 ({txt})")
            else:
                # 옛 회차의 meta.json 에는 사유가 없다. 없는 것을 있는 척하지 않는다.
                file_notes.append(f"서식 해석 못 함 {unformatted}셀 (사유 미기록 — 옛 변환분)")
        # 변환기가 실제로 얼마나 가렸는지. 이걸 안 보이면 Task 4 가 고친 것이 조용히
        # 고쳐진 채로 남는다 — 마스킹이 됐다는 사실도 사람이 볼 것 중 하나다.
        # 열 이름·건수까지만 보인다 — 값 자체는 이미 md 에서 `***` 로 지워져 있다.
        masked = mj.get("masked") or {}
        m_cols, m_rows = masked.get("columns") or [], masked.get("rows") or 0
        m_inline, m_comments = masked.get("inline") or 0, masked.get("comments_dropped") or 0
        if m_cols or m_rows or m_inline or m_comments:
            parts = []
            if m_cols:
                parts.append(f"열 {len(m_cols)}개({' · '.join(m_cols)})")
            if m_rows:
                parts.append(f"행 {m_rows}개")
            if m_inline:
                parts.append(f"인라인 {m_inline}건")
            if m_comments:
                parts.append(f"메모 {m_comments}건 뺌")
            file_notes.append("마스킹함: " + " · ".join(parts))
        # **안 가리기로 한 열은 가린 열보다 더 봐야 한다.** 가린 것은 md 에서 `***` 로
        # 눈에 띄지만, 안 가린 것은 평범한 값으로 실려 표에서 아무 표시가 없다.
        # `xlsx_to_blocks.py` 의 `NOT_PERSONAL_HEADERS` 가 정하고 여기서만 보인다.
        m_open = masked.get("not_personal") or []
        if m_open:
            file_notes.append(
                f"안 가림(예외): 열 {len(m_open)}개({' · '.join(m_open)}) — 개인정보 열 낱말이 들었으나 사람의 것이 아니라고 정한 열"
            )
        # **자른 것은 「빠진 것」이 아니라 「없던 것을 없앤 것」이다.** 값 없는 꼬리
        # 행이라 내용이 안 줄고, SKILL.md 도 사람이 할 일은 없다고 적는다. 그래서
        # `file_notes` 가 아니라 `quiet_notes` 이고, **맨 마지막에 붙는다**:
        # ① 이것만 있으면 ⚠ 를 안 띄운다(⚠ 는 「사람이 봐야 한다」는 뜻이라야 한다)
        # ② 다른 사유와 함께 있으면 헤드라인을 안 뺏는다
        # 실물 53MB 파일에서 5시트 중 4시트가 잘렸다 — 평상시에 뜨는 줄이라 앞에
        # 두면 「상한 초과로 뺀 시트」·「가릴 수 없어 뺀 시트」·「마스킹함」이 매번
        # 「외 N건」 꼬리로 밀린다.
        #
        # 사유는 `xlsx_to_blocks` 가 판정하는 그 순간에 적어 보낸다 — 여기서 XML 을
        # 다시 열어 알아내지 않는다.
        #
        # `kept` 는 **행 번호**(그 행까지 남겼다)이지 개수가 아니다. 성긴 시트에서는
        # 남은 행 수보다 크므로 「71행 남기고」로 읽히면 틀린 숫자가 된다.
        trimmed = mj.get("trimmed_sheets") or []
        if trimmed:
            names = " · ".join(
                f"{s['name']}({s['kept']:,}행까지 남기고 {s['dropped']:,}행)" for s in trimmed
            )
            quiet_notes.append(f"값 없는 꼬리 행을 잘람: {names}")

    hits = EXCEL_ERROR_RE.findall(block)
    err_note = None
    if hits:
        uniq = sorted(set(hits))
        err_note = f"오류 셀 {len(hits)}개 ({' · '.join(uniq)})"

    all_notes = file_notes + ([err_note] if err_note else [])
    # 사유 문장은 조용한 것까지 다 적는다 — 어느 시트를 몇 행까지 남겼나는
    # `--verbose` 로 볼 수 있어야 한다. 갈리는 것은 **칸의 모양**뿐이다.
    detail = " / ".join(file_notes + quiet_notes) or None
    if not all_notes:
        # ⚠ 를 띄울 것이 없다. 조용한 사실만 있으면 칸에 그 사실을 덧붙이되 ⚠ 는
        # 안 붙인다 — 표에서 보이기는 하고, 「볼 것」 수에는 안 들어간다.
        return ("✓ · 꼬리 행 잘람" if quiet_notes else "✓"), detail, None
    others = len(all_notes) - 1 + len(quiet_notes)
    extra = f" 외 {others}건" if others else ""
    cell = all_notes[0] + extra + " ⚠"
    return cell, detail, err_note


def excel_note_listed(cell, why, verbose=False):
    """이 엑셀 사유를 `손볼 곳` 목록에 실을 것인가.

    **칸에서 이미 갈라 놓은 것을 목록에서도 갈라야 한다.** `excel_cell` 은 조용한
    사실(자른 꼬리 행)만 있으면 ⚠ 를 안 붙이고 「볼 것」 수에도 안 넣는데, 목록은
    `detail` 이 비었나만 보고 있어서 그 파일도 한 줄을 차지했다. 목록은
    `--verbose` 가 아니면 12줄에서 잘리고 자르기는 평상시에 일어나므로(실물
    53MB 파일에서 5시트 중 4시트), 자른 파일이 열둘을 넘으면 **진짜 지적이 12줄
    밖으로 밀린다.**

    `--verbose` 면 그대로 다 싣는다 — 그때는 자르지 않고, 어느 시트를 몇 행까지
    남겼나를 볼 수 있어야 하는 것이 `detail` 을 만든 이유다.
    """
    if not why:
        return False
    return verbose or cell.endswith("⚠")


# ── main ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="사업장(채널)명. 생략하면 작업 트리 전체")
    ap.add_argument("--verbose", action="store_true", help="⚠ 의 사유를 모두 펼친다")
    ap.add_argument("--gate", action="store_true",
                    help="표 없이 검사만 — 점검 안 거친 문서 md 가 커밋에 있으면 1 (훅 자리)")
    ap.add_argument("--mask-sweep", action="store_true",
                    help="자료 저장소의 md 전부(문서 + 대화 + 운영 노트)에서 "
                         "마스킹 안 된 값을 찾는다 (표는 안 낸다)")
    args = ap.parse_args()

    # git 작업 트리·점검 기록 어느 것에도 기대지 않는다 — 인자 파싱 바로 뒤,
    # 다른 어떤 일도 하기 전에 처리한다. 이미 커밋된 옛 문서(작업 트리 밖)까지
    # 봐야 하고, 이 모드는 검토가 아니라서 `.review-stamp.json` 도 남기지 않는다.
    if args.mask_sweep:
        # **뿌리는 자료 저장소 전체다** (2026-09-03 에 `documents/projects` 에서 넓혔다).
        # 도움말과 SKILL.md 는 처음부터 「아카이브 md 전체」라고 적고 있었는데 실제
        # 대상은 문서 트리 하나였다 — **대화 아카이브 md 는 이 도구로 한 번도 안
        # 훑렸고**, 거기에 사람이 슬랙에 직접 적은 미마스킹 휴대폰이 남아 있었다.
        # 문서 트리 밖도 개인정보가 git 에 영구 기록되는 것은 똑같다.
        # 넓혀서 새로 걸린 것은 전부 진짜 값이었다 (2026-09-03 실측: md 431개·2건 →
        # 483개·7건. 늘어난 5건에 정규식 잡음 0건).
        root = ROOT
        print(f"■ 훑는 뿌리: {root}")
        hits, n, failures = sweep_masks(root)
        # by_label 은 `hits`(잔여 개인정보)만 센다 — 읽기 실패를 섞으면 다음 작업이
        # 전후 비교 기준값으로 쓸 이 수가 부풀거나, 라벨로 걸러도 안 걸러진다.
        by_label = {}
        for h in hits:
            by_label[h["label"]] = by_label.get(h["label"], 0) + 1
        # 상대경로는 **훑은 뿌리 기준**이다. `DOCS_DIR` 기준으로 만들면 문서 트리
        # 밖(대화 아카이브·운영 노트)에서 `ValueError` 로 죽는다 — 범위를 넓히는
        # 순간 같이 옮겨야 하는 자리다.
        for h in hits:
            rel = Path(h["file"]).relative_to(root)
            # 값은 앞 6자리까지만 — 화면·로그에 남는 사본을 늘리지 않는다.
            print(f"{rel}:{h['line']}  [{h['label']}] {h['value'][:6]}…")
        for f in failures:
            rel = Path(f["file"]).relative_to(root)
            # 예외 종류 이름 + 경로만 — 예외 메시지 전문은 값이 아니지만, 한 화면-출력
            # 규칙("앞 6자만")에 여기만 예외를 두지 않는다. 원인은 이 정도로 충분하다.
            print(f"{rel}  [{READ_FAIL_LABEL}] {f['reason']}")
        # "읽기 실패 0건" 도 반드시 찍는다 — 그래야 "실패 없음"과 "안 셌다"가 계속 다르다.
        print(sweep_footer(n, len(hits), len(failures), by_label))
        return 0

    if args.gate:
        return gate()

    # 표 쪽도 같다 — git 을 못 읽었는데 **빈 표**를 내면 「볼 것 없음」과 같은 모양이라
    # 그대로 커밋된다 (`added_entries` 주석이 같은 함정을 이미 적어 두었다).
    try:
        repo_check()
        docs = changed_docs(args.project)
    except GitError as e:
        print(f"저장소를 읽지 못해 표를 낼 수 없습니다.\n  · {e}", file=sys.stderr)
        return 1
    if not docs:
        print("작업 트리에 새로 생기거나 고쳐진 문서 md 가 없습니다.")
        print("(이미 커밋했다면 점검할 것이 없습니다 — 이 관문은 커밋 전 자리입니다)")
        return 0

    rows, notes, n_warn = [], [], 0
    briefs = []  # (행 번호, 라벨, 문장) — 표 아래 「봇 색인 문장」 절

    for md, is_new in docs:
        rel = f"{md.parent.name}/{md.stem}"
        # 행 번호는 이 md 의 첫 행 — 회차가 여럿이어도 문장은 파일 단위라 한 번만 단다.
        anchor = len(rows) + 1
        for label, text in brief_for(md, is_new):
            briefs.append((anchor, label, text))
        meta = read_meta(md)
        acc, acc_why = access_cell(md, meta)
        problems, _, _ = check(md)
        contract = "✓" if not problems else f"✗ {len(problems)}건"
        all_entries = [(m.group(1), l.strip().strip("*").split("·", 1)[-1].strip())
                       for l in md.read_text(encoding="utf-8").split("\n")
                       if (m := ENTRY_RE.match(l))]
        entries = added_entries(md, is_new)
        # 새 회차가 없으면 **이미 있던 회차의 본문이 바뀐 것**을 되찾는다. 이걸 안 하면
        # 표 안 값 하나를 고친 커밋이 검사를 통째로 건너뛴다 (`changed_entries` 주석 참조).
        reconverted = set()
        if not entries and not is_new:
            entries = changed_entries(md)
            reconverted = set(entries)

        # 열람·계약검사는 **파일 단위**다. 회차마다 같은 지적을 되풀이하면 표가 곧 안 읽힌다 —
        # 그 파일의 첫 줄에만 적고 나머지 줄에서는 셀의 ⚠ · `✗ N건` 으로만 보인다.
        file_reported = False

        if not entries:
            # 회차가 안 잡히는 경우가 셋이다. 둘은 정상 — 메타만 고친 것과, 회차 요약
            # 절만 고친 것(그 전문은 아래 「봇 색인 문장」 절에 실린다. 여기에 ⚠ 를
            # 남기면 관문이 실제로 본 것에 「못 봤다」고 말하는 거짓 ⚠ 가 된다 —
            # 고칠 수 없는 ⚠ 가 쌓이면 사람이 ⚠ 자체를 안 본다). 나머지 하나가
            # **이 관문이 눈이 먼 것**이다. 셋을 같은 모양으로 넘기면 안 된다 —
            # 빈 줄은 「볼 것 없음」과 구별이 안 되어 그대로 커밋된다.
            meta_only = meta_changed(md, is_new)
            has_brief = any(a == anchor for a, _, _ in briefs)
            unseen = not meta_only and not has_brief
            cell = "메타만" if meta_only else ("요약만" if has_brief else "못 찾음 ⚠")
            rows.append([str(len(rows) + 1), rel, acc, cell, "—", "—", "—", "—", "—", contract])
            if acc_why:
                notes.append((len(rows), "열람", acc_why))
            for p in problems:
                notes.append((len(rows), "계약검사", p))
            if unseen:
                notes.append((len(rows), "점검", (
                    "이 md 가 바뀌었는데 새 회차도 메타 변경도 못 찾았습니다 — "
                    "이 관문이 내용을 못 본 것이니 `git diff HEAD -- <파일>` 을 직접 보세요"
                )))
            n_warn += bool(acc_why or problems or unseen)
            continue

        # 뭉침 축의 범위 — 이번에 들어온 줄만. 파일당 한 번만 재고 회차마다 나눠 쓴다.
        md_lines = md.read_text(encoding="utf-8").split("\n")
        touched_new = added_lines(md, is_new)

        for entry_date, source in entries:
            block = entry_block(md, entry_date, source)
            body, body_why = body_cell(block)
            # 사업장 폴더 이름을 넘긴다 — 같은 파일명이 사업장 둘에 있을 때
            # 캐시에서 **이 사업장 원본**을 고르는 유일한 단서다.
            com, com_why = comment_cell(source, block, md.parent.name)
            mask, mask_why = mask_cell(block)
            ser, ser_why = series_cell(md, entry_date, source, all_entries)
            xl, xl_file_why, xl_err_why = excel_cell(meta, all_entries, block, md.parent.name)
            mg, mg_why = merged_cell(md_lines, entry_span(md_lines, entry_date, source), touched_new)
            # 새로 들어온 회차인지, 있던 회차의 **본문이 고쳐진** 것인지 한눈에 갈라 준다 —
            # 볼 자리가 다르다(고침은 「무엇이 왜 바뀌었나」를 diff 로 봐야 한다).
            tag = " · 본문 고침" if (entry_date, source) in reconverted else ""
            rows.append([str(len(rows) + 1), f"{rel} ({entry_date}{tag})", acc, body, com, mask, ser, xl, mg, contract])
            i = len(rows)
            for label, why in (("본문", body_why), ("주석", com_why),
                               ("마스킹", mask_why), ("시리즈", ser_why), ("엑셀", xl_err_why),
                               ("뭉침", mg_why)):
                if why:
                    notes.append((i, label, why))
            if not file_reported:
                if acc_why:
                    notes.append((i, "열람", acc_why))
                for p in problems:
                    notes.append((i, "계약검사", p))
                if excel_note_listed(xl, xl_file_why, args.verbose):
                    notes.append((i, "엑셀", xl_file_why))
                tc_why = track_change_note(source, meta.get("변경추적"))
                if tc_why:
                    notes.append((i, "변경추적", tc_why))
                file_reported = True
            # 엑셀 칸은 **`⚠` 로 끝날 때만** 「볼 것」이다. `✓ · 꼬리 행 잘람` 처럼
            # 사실만 적힌 칸은 사람이 할 일이 없어 여기 세면 안 된다 — 평상시에
            # 뜨는 줄이라 세기 시작하면 「볼 것 N건」이 늘 부풀고 곧 안 읽힌다.
            n_warn += bool(acc_why or body_why or com_why or mask_why or ser_why or problems
                           or xl.endswith("⚠") or mg.endswith("⚠"))

    print("\n[변환 후 점검표 — 커밋 전]\n")
    print(table(
        ["#", "사업장/문서", "열람", "본문", "주석", "마스킹", "시리즈", "엑셀", "뭉침", "계약검사"],
        rows,
        caps=[3, 52, 10, 12, 10, 12, 22, 20, 10, 10],
    ))

    if briefs:
        # **자르지 않는다** — 이 절이 곧 「사람이 그 문장을 읽었다」의 전부라, 12줄에서
        # 자르면 잘린 문장은 또 아무도 안 읽은 채 커밋된다(이 절을 만든 이유 그대로).
        # 바뀐 줄만 내므로 평상시 몇 줄이고, 길다면 그만큼이 전부 새 문장이라는 뜻이다.
        print(f"\n봇 색인 문장 {len(briefs)}건 — 모델이 쓴 요약이라 여기 말고는 사람 눈을 거치지 않습니다.")
        print("  `주요 항목` 은 일일 요약의 문서 소개에, 회차 요약 표는 추이 답변의 근거에 그대로 실립니다 (색인 줄에는 2026-09-10 부터 안 실립니다).")
        print("  숫자·이름이 본문과 맞는지 읽고 답하세요:")
        for i, label, text in briefs:
            print(f"  {i}번 [{label}] {text}")

    # 색인 계기판 (2026-09-10) — 시리즈 메타를 만진 반입에서 커밋 전에 바로 보이게.
    # node 가 없거나 실패해도 점검표는 계속 간다 — 계기판은 정보이지 관문이 아니다.
    try:
        code_root = Path(__file__).resolve().parents[4]  # .claude/skills/doc-archive/scripts → 코드 저장소 뿌리
        r = subprocess.run(
            ["node", str(code_root / "scripts" / "floor-gauge.js")],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            # node 는 있는데 스크립트가 죽은 경우 — stdout 이 비어 `if out:` 만으로는
            # 조용히 넘어간다. 훅(.githooks/pre-commit)과 같은 문구로 알린다.
            print("\n(계기판 실행 실패 — npm run check 로 확인하세요)")
        elif out:
            print("\n" + out)
    except Exception:
        print("\n(색인 계기판 실행 실패 — npm run check 로 확인하세요)")

    if notes:
        print(f"\n손볼 곳 {len(notes)}건:")
        shown = notes if args.verbose else notes[:12]
        for i, label, why in shown:
            print(f"  {i}번 [{label}] {why}")
        if len(notes) > len(shown):
            print(f"  … 그 밖 {len(notes) - len(shown)}건 (--verbose)")

    clean = len(rows) - n_warn
    print(f"\n회차 {len(rows)}건 · 그대로 둬도 되는 것 {clean}건 · 볼 것 {n_warn}건")
    print("빼려면: python decide.py --undo --file <md> --date <날짜> --source <원본명>")
    print("나중에: python decide.py <선택자> --later 14 --reason \"…\"")
    print("\n커밋·push 는 이 표를 승인받은 **다음 턴**에 따로 받는다.")
    # **표를 낸 다음에 적는다** — 여기까지 왔다는 것이 곧 사람이 볼 표가 나왔다는 뜻이다.
    save_stamp(docs)
    return 0


if __name__ == "__main__":
    # 표를 만드는 도중에도 git 을 부른다(`added_entries`·`meta_changed`). 거기서 실패하면
    # 트레이스백 대신 사유 한 줄로 멈춘다 — 어느 쪽이든 **빈 표로 끝나지 않는 것**이 요점이다.
    try:
        sys.exit(main())
    except GitError as _e:
        print(f"저장소를 읽지 못해 멈춥니다.\n  · {_e}", file=sys.stderr)
        sys.exit(1)
