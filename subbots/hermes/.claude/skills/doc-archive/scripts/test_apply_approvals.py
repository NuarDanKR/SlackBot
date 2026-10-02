#!/usr/bin/env python3
"""
apply_approvals.py 의 `--only` 고르기 시험.

  python .claude/skills/doc-archive/scripts/test_apply_approvals.py

**슬랙도 파일도 안 부른다** — `_split_only()`·`select()` 는 주어진 값만 보는 순수
함수라 임시 폴더조차 필요 없다.

**왜 있나** — `--only` 는 쉼표를 늘 구분자로 봤다. 그래서 이름에 쉼표가 든 문서
(`보고서(1,2월).pdf`)는 조각 둘로 갈려 각각 아무것도 못 맞추고 「맞는 항목이
없습니다」만 나왔다 — 그 문서는 **이름으로 영영 못 고른다.** 번호로 고르는 길
(`--only 2,4`)은 남겨야 하므로, 값이 번호 형태일 때만 쪼갠다.

이름으로 고르는 것을 권하는 이유는 `select()` 의 독스트링에 있다 — 번호는 실행할
때마다 다시 매겨져 같은 번호가 다른 문서를 가리킬 수 있다.

종료코드: 0 전부 통과 / 1 실패 있음
"""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).parent))
import apply_approvals as A

FAILED = 0


def ok(m):
    print(f"  ✓ {m}")


def bad(m):
    global FAILED
    FAILED += 1
    print(f"  ✗ {m}", file=sys.stderr)


def item(channel, file):
    """승인 미반영 한 건 — 고르기가 보는 칸만 담는다.

    `md` 는 실제 코드가 넘기는 것과 같게 `Path` 로 둔다 (`_keys` 가 `relative_to` 를 쓴다).
    """
    return {"channel": channel, "file": file,
            "md": A.DOCS_DIR / channel / f"{file}.md"}


ITEMS = [
    item("사업장가", "보고서(1,2월).pdf"),
    item("사업장나", "잔금수금.pdf"),
    item("비공개나", "이슈사항.xlsm"),
]

print("[1/4] 이름에 쉼표가 있어도 한 토큰으로 남는다")
got = A._split_only("보고서(1,2월).pdf")
if got == ["보고서(1,2월).pdf"]:
    ok("안 쪼갠다 — 쪼개면 그 문서는 이름으로 영영 못 고른다")
else:
    bad(f"쪼개졌습니다: {got!r}")

print("[2/4] 번호는 여전히 쉼표로 쪼개진다")
got = A._split_only("2,4")
if got == ["2", "4"]:
    ok("`--only 2,4` 가 둘로 쪼개진다")
else:
    bad(f"안 쪼개졌습니다: {got!r}")
got = A._split_only(" 1 , 3 ")
if got == ["1", "3"]:
    ok("사이에 공백이 있어도 번호로 본다")
else:
    bad(f"공백 든 번호가 {got!r} 입니다")

print("[3/4] 쉼표 든 이름으로 실제로 그 한 건이 골라진다")
# **`채널/파일명` 형태로 준다** — DM 과 목록이 실제로 안내하는 형태이고
# (`--only "사업장가/보고서(1,2월).pdf"`), 쉼표로 쪼개지면 앞 조각
# `사업장가/보고서(1` 이 아무것도 못 맞춰 `unmatched` 에 남는다.
# 파일명만 주면 쪼갠 조각도 그 이름의 부분 문자열이라 우연히 같은 것이 걸려
# 어긋남이 안 드러난다 — 그래서 이 시험은 조각이 어긋나는 형태로 잰다.
picked, unmatched, ambiguous = A.select(ITEMS, ["사업장가/보고서(1,2월).pdf"])
if len(picked) == 1 and picked[0]["file"] == "보고서(1,2월).pdf" and not unmatched:
    ok("그 문서 하나만 골렸다 — 못 맞춘 조각이 없다")
else:
    bad(f"골린 것이 {[p['file'] for p in picked]!r}, 못 맞춘 토큰 {unmatched!r}")

print("[4/4] 번호로 고르는 길이 안 막혔다")
picked, unmatched, ambiguous = A.select(ITEMS, ["1,3"])
if [p["file"] for p in picked] == ["보고서(1,2월).pdf", "이슈사항.xlsm"]:
    ok("1·3번이 골라진다 — 번호 경로가 살아 있다")
else:
    bad(f"번호로 골린 것이 {[p['file'] for p in picked]!r} 입니다")

sys.exit(1 if FAILED else 0)
