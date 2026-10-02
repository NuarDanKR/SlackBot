#!/usr/bin/env python3
"""
review_batch.py 시험. **임시 파일에서만 돌고 저장소·캐시는 안 건드린다.**

  python .claude/skills/doc-archive/scripts/test_review_batch.py

종료코드: 0 전부 통과 / 1 실패 있음

── 왜 남겨 두는가 ──

7.5 점검표의 ⚠ 는 **사람이 볼 것을 고르는 자리**다. 그래서 ⚠ 에 붙는 사유가 틀리면
사람이 엉뚱한 곳을 본다. 2026-08-12 에 실제로 그랬다 — 원본이 캐시에 **있는데도**
「원본이 캐시에 없어 확인하지 못했습니다」가 붙었다. 원본은 PDF 였고, 워드 주석은
애초에 존재할 수 없는 포맷이었다.

원인은 `comment_cell` 이 **서로 다른 두 상태를 한 분기로 묶은 것**이다.

  · origin is None            → 캐시에 없다 (판정 불가)
  · origin 있고 워드가 아님    → 워드 주석이 존재할 수 없다 (판정 불필요)

같은 파일의 `cache_origin` 은 반대로 「같은 이름의 캐시 원본이 N건이라…」처럼 무엇을
몰랐는지 정확히 말하고, 주석에 "모르면 모른다고 말한다" 라고 적혀 있다. 그 원칙이
여기서만 깨져 있었다. 사유가 틀린 ⚠ 는 고칠 수 없는 ⚠ 라서, 쌓이면 사람이 ⚠ 전체를
안 보게 된다 — 이 축을 만든 이유(검토의견 886자 누락, 2026-08-05)가 그때 무너진다.
"""

import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_batch as R  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok &= bool(cond)
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))


def make_docx(path: Path, comment_text: str):
    """주석이 든 최소 docx. 표준 라이브러리만 쓴다 (SKILL.md 전제)."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body/></w:document>")
        if comment_text:
            z.writestr(
                "word/comments.xml",
                f'<w:comments><w:comment w:author="김검토"><w:p><w:r><w:t>'
                f"{comment_text}</w:t></w:r></w:p></w:comment></w:comments>",
            )


with tempfile.TemporaryDirectory() as d:
    cache = Path(d)
    (cache / "사업장다").mkdir()
    (cache / "비공개가").mkdir()

    # **덧붙임 이름의 PDF** — 워드에서 의견을 달고 PDF 로 내보낸 판. 주석은 이 검사가
    # 못 읽으므로 아래 「사유」 분기가 실제로 쓰이는 자리다.
    pdf = cache / "사업장다" / "F0BQ_260811_임대차 계약서(안)_검토의견 추가.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    plain = cache / "사업장다" / "F0BR_260811_주간보고.pdf"
    plain.write_bytes(b"%PDF-1.4 fake")
    docx = cache / "비공개가" / "F0BS_260808_검토의견.docx"
    make_docx(docx, "위약벌 기준은 재검토가 필요합니다")

    R.CACHE = cache
    R._cache_index = None

    print("\ncomment_cell — 캐시에 없는 원본")

    miss, why = R.comment_cell("없는파일_합의서_의견추가.pdf", "본문", "사업장다")
    check("판정하지 못하면 ⚠", miss == "? ⚠", repr(miss))
    check("사유가 「캐시에 없다」다", "캐시에 없" in (why or ""), repr(why))

    print("\ncomment_cell — 캐시에 있는데 워드가 아닌 원본  ← 2026-08-12 에 틀렸던 자리")

    cell, why2 = R.comment_cell(
        "260811_임대차 계약서(안)_검토의견 추가.pdf", "본문", "사업장다")
    # 원본이 눈앞에 있는데 「없다」고 말하면, 사람은 캐시를 뒤지거나 다시 받아온다.
    check("사유가 「캐시에 없다」라고 말하지 않는다", "캐시에 없" not in (why2 or ""), repr(why2))
    # 무엇 때문에 못 보는지는 말해야 한다 — 워드가 아니라 워드 주석이 있을 수 없다는 것.
    check("워드가 아니라는 것이 사유에 있다",
          any(w in (why2 or "") for w in ("워드가 아니", "pdf", "PDF")), repr(why2))
    check("두 상태의 사유가 서로 다르다", (why or "") != (why2 or ""))

    print("\ncomment_cell — 워드 원본 (여기는 실제로 셀 수 있다)")

    have, why3 = R.comment_cell("260808_검토의견.docx", "본문만 있고 절이 없다", "비공개가")
    check("주석이 있는데 md 에 절이 없으면 ⚠", have.endswith("⚠"), repr(have))
    check("몇 자인지 센다", "자" in have, repr(have))
    ok3, why4 = R.comment_cell(
        "260808_검토의견.docx", f"### {R.COMMENT_SECTION} (검토의견)\n- 위약벌…", "비공개가")
    check("md 에 절이 있으면 ✓", ok3.endswith("✓") and not why4, repr(ok3))

    print("\ncomment_cell — 이름에 '의견' 이 없으면 조용하다")

    quiet, why5 = R.comment_cell("260811_주간보고.pdf", "본문", "사업장다")
    check("워드가 아니고 이름도 평범하면 —", quiet == "—" and not why5, repr(quiet))

    # ── 이름 판정 — 아카이브 실물의 **이름 모양**으로 (2026-08-12 전수)
    #
    # **사업장·회사·법무법인 이름은 지어낸 것으로 바꿨다.** 이 저장소는 팀끼리 나눠 쓰고,
    # 실물 파일명을 적으면 사본이 하나 더 생겨 이력에 남는다. 판정에 필요한 것은
    # 「덧붙임 표기가 있나」뿐이라 이름을 바꿔도 같은 것을 잰다 (아래 주민번호와 같은 이유).
    #
    # 검사가 지키려는 것은 **「원본 문서 + 거기 붙인 의견」에서 그 의견이 사라지는 것**이다
    # (2026-08-05: 계약서에 붙은 검토의견 886자·1,026자). 「의견서」류는 본문이 곧 의견이라
    # 잃을 것이 없다. 낱말 `의견` 만으로 걸면 뒤엣것까지 전부 ⚠ 가 되고, **고칠 수 없는 ⚠**
    # 는 사람이 ⚠ 자체를 안 보게 만든다.
    print("\nlooks_like_review — 「의견을 덧붙인 문서」만 걸린다")

    ADDED = [  # 걸려야 하는 것 — 원본에 의견을 덧붙인 판
        "260626 - [갑사] 임대차 계약서(안_ v.3.2)_을사 검토의견 추가.doc",
        "사업장나 B동_ 양해각서(MOU)_markup_문구 수정.docx",
        "3. 채권양도 관련 합의서(수정)_의견추가-PFV 확인.docx",
    ]
    OPINION_DOC = [  # 안 걸려야 하는 것 — 의견 자체가 그 문서
        "법률검토의견서_사업장차.pdf",
        "법률검토의견_사업장차 현안 관련.pdf",
        "[사업장다] 대위변제 관련_갑법인의견.pdf",
        "사업장라 자금보충 관련 갑법인의견_20260729-1.pdf",
        "26. 06. 10. 연체이자 소송 관련 법률검토의견서.hwpx",
        "20260706_대출 기한연기에 대한 검토 및 의견 요청.pdf",
        "20260707_대출기한 연장 요청에 대한 검토 의견 통보.pdf",
        "260811_사업장마 자금수지, 현안 및 처분(안)의견 요청의 건.pdf",
    ]
    for n in ADDED:
        check(f"걸린다: {n[:34]}…", R.looks_like_review(n))
    for n in OPINION_DOC:
        check(f"안 걸린다: {n[:34]}…", not R.looks_like_review(n))

# ── 마스킹 판정 (2026-08-15 전수 조사) ────────────────────────────────────
#
# 443회차를 전수로 재 보니 ⚠ 16건 중 **휴대폰 5건이 전부 오탐**이고 주민번호 6건 중
# 진짜는 1건뿐이었다. 그런데 문제는 오탐 자체가 아니라 **오탐이 진짜를 밀어낸 것**이다 —
# `mask_cell` 이 라벨당 첫 건만 보고해서, 같은 회차 앞쪽의 법인등록번호가 먼저 걸리자
# 진짜 주민등록번호가 화면에서 사라졌다 (사업장바 투자계약서 날인본).
#
# 원인 셋: A 앞뒤 숫자 경계가 없다 · B 법인등록번호와 구별이 안 된다 · C 첫 건만 본다.
#
# **아래 주민번호는 전부 지어낸 값이다.** 아카이브의 실물을 시험에 적으면 사본이 하나
# 더 생기고 그것도 이력에 남는다. 판정에 필요한 것은 날짜 유효성과 검증번호뿐이라
# 지어낸 값으로 같은 것을 잰다. 법인등록번호 둘도 지어낸 값이다 (앞 6자리 등기소 코드만 실물 모양이다).
print("\nmask_cell — 긴 숫자열 한가운데를 개인정보로 보지 않는다 (원인 A)")

MASK_PASS = [  # 넘겨야 하는 것 — 아카이브 실물에서 뽑았다
    ("연도 나열 (사업장바 시장보고서)", "차트 1: 실질 GDP 성장률, 2016 2017 2018 2019 2020"),
    ("차트 축 눈금 (사업장바 시장보고서2)", '<tr><td rowspan="6">1301802300000</td></tr>'),
    ("등기부 발행번호 (사업장카)",
     "발행번호404535GL43553930734356330333336626433347181445445"),
    ("표 금액 나열 (비공개나 PF60개)", "<td>230230230230230180</td>"),
    ("ISO 날짜", "납부일 2026-05-28 · 2026-08-14"),
    ("이미 마스킹됨", "연락처 010-****-****"),
]
for label, text in MASK_PASS:
    cell, why = R.mask_cell(text)
    check(f"넘긴다: {label}", cell == "✓" and not why, f"{cell!r} {why!r}")

print("\nmask_cell — 진짜 휴대폰은 놓치지 않는다")

MASK_HIT_PHONE = [
    ("하이픈", "홍길동 부장 / 010-1234-5678 / 02-1234-5678"),
    ("붙여쓰기", "연락처 01012345678 로 주세요"),
    ("공백", "M. 010 1234 5678"),
    # 경계에 `.` 이나 `-` 를 넣으면 이 표기를 놓친다. 아카이브에 실제로 있다.
    ("앞에 마침표", "M.010-1234-5678"),
]
for label, text in MASK_HIT_PHONE:
    cell, why = R.mask_cell(text)
    check(f"잡는다: 휴대폰 {label}", cell.startswith("⚠") and "휴대폰" in cell, f"{cell!r}")

print("\nmask_cell — 법인등록번호도 이제 가린다 (2026-08-26 마스킹 규칙 2판)")

# 예전에는 `is_rrn` 이 이것들을 "법인 것이니 주민번호가 아니다"로 걸러 넘겼다.
# 이제는 법인등록번호·외국인등록번호·식별번호를 함께 가리므로 전부 ⚠ 다.
MASK_HIT_ID = [
    ("갑자산운용 법인등록번호", "법인등록번호 : 110111-1111111"),
    ("을자산신탁 법인등록번호", "법인등록번호 110111-2222222"),
    ("식별번호 — 앞 6자리가 날짜가 아님", "인식번호 309923-1234567"),
    ("외국인등록번호", "등록 900101-5123456"),
]
for label, text in MASK_HIT_ID:
    cell, why = R.mask_cell(text)
    check(f"잡는다: {label}", cell.startswith("⚠"), f"{cell!r} {why!r}")

# 지어낸 값. 1990-01-01 은 유효한 날짜다. 문맥은 사업장바 실물과 같다 (인감 절).
FAKE_RRN = "900101-1234568"
cell, why = R.mask_cell(f"대표이사 홍길동 ( {FAKE_RRN})")
check("잡는다: 주민번호", cell.startswith("⚠"), f"{cell!r}")
check("이름은 사유에 안 뜬다", "홍길동" not in (why or ""), repr(why))

print("\nmask_cell — 앞에 걸린 것이 진짜를 밀어내지 않는다 (원인 C)")

SEAL_SHAPE = (
    "법 인퉁록번호 : 110111-1111111\n"
    "주소 : 서울특별시 영등포구\n"
    "대표이사 홍길동\n"
    f"( {FAKE_RRN})\n"
)
cell, why = R.mask_cell(SEAL_SHAPE)
# **값까지 본다.** 이제 둘 다 걸리므로 둘 다 사유에 있어야 한다 —
# 라벨만 보면 하나가 밀려난 상태에서도 시험이 통과한다.
check("법인등록번호가 사유에 있다", "110111" in (why or ""), repr(why))
check("진짜 주민번호도 사유에 있다", "900101" in (why or ""), repr(why))

cell, why = R.mask_cell("담당 010-1234-5678\n대리 010-9911-2233\n")
check("같은 라벨의 둘째 값도 사유에 나온다",
      "010-12" in (why or "") and "010-99" in (why or ""), repr(why))

print("\nmask_cell — 신설 항목 (사업자등록번호·유선전화·계좌)")

MASK_HIT_NEW = [
    ("사업자등록번호", "등록 123-45-67890 발급", "사업자등록번호"),
    ("유선전화 서울", "대표 02-1234-5678", "유선전화"),
    ("유선전화 경기", "사무실 031-123-4567", "유선전화"),
    ("안심번호", "연락 0507-1234-5678", "유선전화"),
    ("낱말이 있는 계좌", "납부계좌 : 갑은행 393301-04-04999", "계좌"),
]
for label, text, want in MASK_HIT_NEW:
    cell, why = R.mask_cell(text)
    check(f"잡는다: {label}", cell.startswith("⚠") and want in (why or ""),
          f"{cell!r} {why!r}")

MASK_PASS_NEW = [
    ("낱말 없는 숫자", "코드 301-0123-4567 참조"),
    ("계좌 낱말 옆의 날짜", "계좌 개설일 2026-08-26"),
    ("계약자 이름", "계약자 홍길동 님 상담 완료"),
    ("금액과 날짜", "공급금액 1,170,110,000 · 납부일 2026-07-10"),
]
for label, text in MASK_PASS_NEW:
    cell, why = R.mask_cell(text)
    check(f"넘긴다: {label}", cell == "✓", f"{cell!r} {why!r}")

# 사업자등록번호와 유선전화는 가운데 자릿수가 달라 서로 안 겹친다.
cell, why = R.mask_cell("사업자 031-81-12345")
check("031-81-12345 는 사업자등록번호로만 걸린다",
      "사업자등록번호" in (why or "") and "유선전화" not in (why or ""), repr(why))

# 두 벌로 갈리는 함정을 구조적으로 막았는지 — 같은 객체인지 본다.
import xlsx_to_blocks as X  # noqa: E402
check("점검표와 변환기가 같은 정규식 객체를 쓴다",
      R.INLINE_PATTERNS is X.INLINE_PATTERNS,
      f"{id(R.INLINE_PATTERNS)} vs {id(X.INLINE_PATTERNS)}")

print("\nmask_cell — 계좌 경계 (Task 1 리뷰에서 발견, 2026-08-26 xlsx_to_blocks.py 수정)")

# `ACCOUNT_NUM` 은 다섯 인라인 패턴과 달리 앞뒤 숫자 경계(`(?<!\d)`/`(?!\d)`)가 없었다.
# 그래서 긴 숫자열 한가운데서 걸려 `mask_inline` 이 "계좌 1234***23 확인" 처럼
# 앞뒤 진짜 숫자를 남긴 채 가운데만 지웠다(재현: `xlsx_to_blocks.mask_inline` 직접
# 호출). 경계를 더한 지금은 이 값이 2~6·2~6·2~8 자리 어느 조합으로도 안 맞아
# **통째로 안 걸린다** — `mask_hits` 가 이 값을 부분 문자열로도 잡지 않아야 한다
# (부분만 먹혔다가 아니라 전부 넘겨야 한다).
_LONG_DIGIT_RUN = "1234567890-123-4567890123"
cell, why = R.mask_cell(f"계좌 {_LONG_DIGIT_RUN} 확인")
check("긴 숫자열 한가운데를 부분만 계좌로 잡지 않는다",
      cell == "✓" and not why, f"{cell!r} {why!r}")
_account_hits = [v for label, v in R.mask_hits(f"계좌 {_LONG_DIGIT_RUN} 확인") if label == "계좌"]
check("부분 문자열조차 계좌로 걸리지 않는다 (경계 없이는 '890-123-456…' 식으로 걸렸었다)",
      _account_hits == [], _account_hits)

# ── 이메일 마스킹 그물 (Task 7) ──────────────────────────────────────────
#
# 엑셀 변환기(xlsx_to_blocks.py 의 _INLINE_MASK)가 이메일을 가리게 됐다. 점검표가
# 같은 패턴을 안 보면 변환기는 가리는데 점검표는 계속 ✓ 를 찍거나, 그 반대로
# 변환기가 놓친 것을 점검표도 놓친다 — 두 곳이 갈리는 것이 이 시험이 막는 것이다.
print("\n[이메일 마스킹 그물]")
_hit, _why = R.mask_cell("연락처 gildong@naver.com 로 회신")
check("가려지지 않은 이메일을 ⚠ 로 세운다",
      _hit.startswith("⚠") and "이메일" in (_why or ""), f"{_hit!r} / {_why!r}")

_hit2, _why2 = R.mask_cell("메일 *** 로 회신")
check("이미 가려진 것은 안 잡는다", _hit2 == "✓", f"{_hit2!r} / {_why2!r}")

_hit3, _why3 = R.mask_cell("공급금액 1,170,110,000 · 납부일 2026-07-10")
check("금액·날짜를 이메일로 오인하지 않는다", _hit3 == "✓", f"{_hit3!r} / {_why3!r}")

# ── pad — 잘려도 ⚠ 는 남는다 (2026-08-24, unmaskable_sheets 를 표에 태우다 발견) ──
#
# 긴 사유는 표 칸 폭(엑셀 칸 20)을 넘기기 일쑤다. 앞부분만 자르면 끝의 `⚠` 가
# 통째로 잘려 나가 「가릴 수 없어 뺀 시…」처럼 **표만 보고는 이 줄에 볼 것이
# 있는지조차 알 수 없다** — ⚠ 가 사람 눈을 끄는 유일한 표식이라 이게 없어지면
# 이 축이 있으나 마나다.
print("\npad — 잘려도 ⚠ 는 남는다")

_long_warn = "가릴 수 없어 뺀 시트: 계약자명부(머리글 행을 못 찾아 개인정보 열을 가릴 수 없었다) ⚠"
_padded = R.pad(_long_warn, 20)
check("잘린 칸에도 ⚠ 가 끝에 남는다", _padded.rstrip().endswith("⚠"), repr(_padded))
check("칸 폭을 넘지 않는다", R.width(_padded) == 20, f"{R.width(_padded)} {_padded!r}")

_short = "✓"
check("짧은 값은 그대로 오른쪽만 채운다", R.pad(_short, 20) == "✓" + " " * 19,
      repr(R.pad(_short, 20)))

_long_plain = "그냥 긴 글자열인데 경고 표시는 없는 경우입니다"
_padded_plain = R.pad(_long_plain, 20)
check("경고가 없는 값은 …로만 잘린다", _padded_plain.rstrip().endswith("…"), repr(_padded_plain))
check("⚠ 를 새로 만들어 붙이지 않는다", "⚠" not in _padded_plain, repr(_padded_plain))


print("\ngate — git 이 실패하면 「볼 것 없음」이 아니다")

# 관문은 「담긴 문서 md 가 없다」와 「저장소를 못 읽었다」를 **같은 0** 으로 답하고 있었다.
# 그러면 훅이 걸려 있는데 아무것도 안 지키는 상태가 되고, 그 사실이 화면에 안 나온다.
# 스킬을 다른 깊이에 깔아 ROOT 가 어긋나면 실제로 이 길로 간다.


class _FakeRun:
    def __init__(self, rc=128, out="", err="fatal: not a git repository"):
        self.returncode, self.stdout, self.stderr = rc, out, err


_keep_run = R.subprocess.run
try:
    R.subprocess.run = lambda *a, **k: _FakeRun()
    _code = R.gate()
finally:
    R.subprocess.run = _keep_run
check("git 이 실패하면 관문이 1 을 낸다", _code == 1, repr(_code))

# 스킬을 `<저장소>/.claude/skills/<이름>/scripts/` 가 아닌 곳에 깔면 ROOT 가 엉뚱한 곳을
# 가리킨다. 그때 「통과」라고 답하면 안 된다 — 무엇을 봤는지 모르는 상태다.
with tempfile.TemporaryDirectory() as _d:
    _keep_root = R.ROOT
    try:
        R.ROOT = Path(_d)
        _code2 = R.gate()
    finally:
        R.ROOT = _keep_root
check("저장소가 아닌 곳에서는 통과시키지 않는다", _code2 == 1, repr(_code2))

print("\n해시는 스테이징을 본다 — 작업 트리가 아니다")

# 도장은 작업 트리에서 찍는데 관문이 그것을 그대로 대면, `git add -p` 로 조각만 골라
# 담았을 때 **사람이 표에서 못 본 조합이 커밋된다.** 작업 트리 해시는 그대로라 통과하고,
# 뚫려도 화면에 아무 표시가 없다 (2026-08-12 리뷰).
#
# **여기서 만드는 것은 완전히 별개인 임시 저장소다** — WHK 저장소를 건드리지 않는다.
with tempfile.TemporaryDirectory() as _d:
    _repo = Path(_d)
    _sub = lambda *a: subprocess.run(["git", *a], cwd=_repo, capture_output=True, text=True)
    _sub("init", "-q")
    _sub("config", "user.email", "t@t"); _sub("config", "user.name", "t")
    _f = _repo / "doc.md"
    _f.write_text("본 것\n", encoding="utf-8")
    _sub("add", "doc.md")
    # 담은 뒤에 작업 트리만 고친다 = `git add -p` 로 일부만 담은 것과 같은 상태.
    _f.write_text("안 본 것\n", encoding="utf-8")

    _keep_root = R.ROOT
    try:
        R.ROOT = _repo
        _staged = R.staged_fingerprint(_f)
        _work = R.fingerprint(_f)
    finally:
        R.ROOT = _keep_root

    check("두 해시가 갈린다", _staged != _work, f"{_staged} vs {_work}")
    # 인덱스에 담긴 것은 「본 것」이므로, 그때 찍힌 도장과 같아야 한다.
    check("인덱스 해시는 담긴 내용의 것이다",
          _staged == R._digest("본 것\n".encode("utf-8")), _staged)
    check("작업 트리 해시는 지금 파일의 것이다",
          _work == R._digest("안 본 것\n".encode("utf-8")), _work)

# 줄끝만 다른 것은 같은 내용으로 본다. git 은 인덱스에 LF 로 넣고 작업 트리에는 CRLF 로
# 꺼내므로, 안 맞추면 관문이 늘 막아 곧 `--no-verify` 로 꺼진다.
check("줄끝 차이는 해시를 가르지 않는다",
      R._digest(b"a\r\nb\r\n") == R._digest(b"a\nb\n"))

# 없는 경로를 대면 조용히 통과시키지 않는다 — 「볼 것 없음」과 「못 봄」은 다르다.
with tempfile.TemporaryDirectory() as _d:
    _repo = Path(_d)
    subprocess.run(["git", "init", "-q"], cwd=_repo, capture_output=True)
    _keep_root = R.ROOT
    try:
        R.ROOT = _repo
        R.staged_fingerprint(_repo / "없는파일.md")
        _raised = False
    except R.GitError:
        _raised = True
    finally:
        R.ROOT = _keep_root
check("인덱스에 없으면 예외를 낸다", _raised)

# ── norm_name — 이름을 「경로」로 읽지 않는다 (2026-08-15) ─────────────────
#
# 받는 값은 `md.stem` 이라 **이미 확장자가 없다.** 그런데 거기에 `Path(name).stem` 을
# 한 번 더 걸고 있었고, 그러면 이름 가운데의 점 뒤가 통째로 날아간다. 업무 표기에
# 점이 든 것이 실제로 있다 — `사업장타 Tr.A 000억`. 그런 이름 둘이 같은 값으로 접히면
# `series_cell` 이 서로 다른 문서를 「같은 정규화명 3건째」로 잘못 ⚠ 한다.
#
# 지금 문서 md 311개 중 이름에 점이 든 것은 0개라 **잠복이다.** 사고를 고치는 것이
# 아니라 닿으면 조용히 틀리는 자리를 닫는 것이다. (같은 함정을 `fetch_slack_files.py`
# 의 `name_key` 에서 먼저 만났다 — 거기는 슬랙이 주는 이름이라 통제 밖이었다.)
print("\nnorm_name — 점 뒤를 버리지 않는다")

check("점 뒤가 남는다", R.norm_name("사업장타 Tr.A 000억") == "사업장타tra000억",
      R.norm_name("사업장타 Tr.A 000억"))
check("점 뒤만 다른 이름이 서로 갈린다",
      R.norm_name("사업장타 Tr.A 000억") != R.norm_name("사업장타 Tr.B 111억"),
      f'{R.norm_name("사업장타 Tr.A 000억")} vs {R.norm_name("사업장타 Tr.B 111억")}')
# 지금 쓰는 이름들은 그대로여야 한다 — 고침이 기존 묶음을 바꾸면 안 된다.
check("점 없는 이름은 그대로", R.norm_name("260812_사업장나 잔금수금") == "사업장나잔금수금",
      R.norm_name("260812_사업장나 잔금수금"))
check("날짜 토큰은 계속 지운다", R.norm_name("2026-08-12 주간보고") == "주간보고",
      R.norm_name("2026-08-12 주간보고"))

# ── series_cell — norm_name 을 실제로 쓰는 자리까지 통과시킨다 ─────────────
#
# 위 단위 시험은 `norm_name` 만 본다. 화면에 나오는 것은 `series_cell` 의 셀이고,
# **그 사이에 `md.stem` 을 넘기는 배선이 있다** — 함수만 고치고 배선을 안 보면
# 시험은 통과하는데 표는 그대로다 (`active-todos.md` 에 같은 교훈이 적혀 있다).
#
# 「단발」 갈래로 가려면 `all_entries` 가 1건 이하여야 한다 (2건부터는 회차 표시).
print("\nseries_cell — 다른 문서를 같은 이름으로 묶지 않는다")

with tempfile.TemporaryDirectory() as _d:
    _proj = Path(_d) / "사업장타"
    _proj.mkdir()
    # 점 뒤만 다른 세 문서. 옛 판정에서는 셋 다 `사업장타tr` 로 접혔다.
    # **이름과 금액은 지어낸 것이다** — 이 시험이 재는 것은 「점 뒤가 날아가나」이지
    # 어느 사업장의 얼마인가가 아니다 (`review_batch.norm_name` 의 주석과 같은 표기).
    for _n in ("사업장타 Tr.A 000억", "사업장타 Tr.B 111억", "사업장타 Tr.C 222억"):
        (_proj / f"{_n}.md").write_text("x", encoding="utf-8")
    _md = _proj / "사업장타 Tr.A 000억.md"
    _cell, _why = R.series_cell(_md, "2026-08-12", "원본.pdf", [("2026-08-12", "원본.pdf")])
    check("점 뒤만 다르면 단발로 남는다", _cell == "단발", f"{_cell!r} {_why!r}")

with tempfile.TemporaryDirectory() as _d:
    _proj = Path(_d) / "_주간모음"
    _proj.mkdir()
    # 진짜로 같은 이름인 셋 — 날짜만 다르다. 이건 계속 ⚠ 여야 한다.
    for _n in ("260801_주간보고", "260808_주간보고", "260815_주간보고"):
        (_proj / f"{_n}.md").write_text("x", encoding="utf-8")
    _md = _proj / "260815_주간보고.md"
    _cell, _why = R.series_cell(_md, "2026-08-15", "원본.pdf", [("2026-08-15", "원본.pdf")])
    check("진짜 같은 이름 3건째는 그대로 ⚠", _cell == "단발 (같은 이름 3건) ⚠", f"{_cell!r}")
    check("그 사유가 붙는다", bool(_why) and "3건째" in _why, f"{_why!r}")

# ── series_cell — 엑셀 시트는 「같은 날짜의 다른 원본」이 아니다 ───────────
#
# 엑셀 한 파일은 시트마다 회차 블록이 되고, 그 회차들은 **일부러 같은 날짜에
# 서로 다른 `source`** 를 쓴다 (`<원본명> — 시트 n/N: <이름>`, SKILL.md 「엑셀 변환」).
# `--source` 에 시트 이름을 안 넣으면 2번째 시트부터 조용히 SKIP 되므로 이 형태는
# 바꿀 수 없다. 그런데 「교체?」 판정은 날짜가 같고 source 가 다르면 개정본으로 보아,
# 손대지 않으면 **엑셀 문서는 시트 수만큼 가짜 ⚠ 가 뜬다** — 어느 사업장의 산정내역
# 엑셀 실물에서 4시트 전부가 걸렸고 「그대로 둬도 되는 것 0건」이 됐다. 13건이면
# 50줄 가까이 되어
# 점검표의 취지(깨끗한 건은 ✓ 한 줄)가 무너지고 진짜 경고가 묻힌다.
print("\nseries_cell — 같은 엑셀의 다른 시트는 개정본이 아니다")

# **파일명·시트 이름은 지어낸 것이다.** 시험이 재는 것은 `— 시트 n/N: 이름` 표기와
# 버전 꼬리(v10/v11)뿐이라, 이름을 바꿔도 같은 것을 잰다.
_SHEETS = [
    ("2026-08-05", "260805_사업장가 산정 내역_v10.xlsx — 시트 1/4: 중후순위 정산"),
    ("2026-08-05", "260805_사업장가 산정 내역_v10.xlsx — 시트 2/4: 대출이자"),
    ("2026-08-05", "260805_사업장가 산정 내역_v10.xlsx — 시트 3/4: CP(91)"),
    ("2026-08-05", "260805_사업장가 산정 내역_v10.xlsx — 시트 4/4: 약정 관련 문서 정리"),
]

with tempfile.TemporaryDirectory() as _d:
    _proj = Path(_d) / "사업장가"
    _proj.mkdir()
    _md = _proj / "20260805-사업장가-산정-내역-v10.md"
    _md.write_text("x", encoding="utf-8")
    for _i, (_dt, _src) in enumerate(_SHEETS, 1):
        _cell, _why = R.series_cell(_md, _dt, _src, _SHEETS)
        check(f"시트 {_i}/4 에 「교체?」가 안 붙는다", "교체?" not in _cell, f"{_cell!r} {_why!r}")
    check("회차 수는 그대로 4건으로 센다",
          R.series_cell(_md, _SHEETS[0][0], _SHEETS[0][1], _SHEETS)[0] == "회차 4건",
          repr(R.series_cell(_md, _SHEETS[0][0], _SHEETS[0][1], _SHEETS)[0]))

# 같은 날짜에 **진짜 다른 파일**이 오면 여전히 ⚠ 여야 한다 — 엑셀 개정본을 같은 날
# 두 번 올리는 일이 실제로 있다(8/5 에 v10 과 v8_갑사의견 반영이 각각 다른 채널에 올랐다).
with tempfile.TemporaryDirectory() as _d:
    _proj = Path(_d) / "사업장가"
    _proj.mkdir()
    _md = _proj / "20260805-사업장가-산정-내역.md"
    _md.write_text("x", encoding="utf-8")
    _mixed = _SHEETS[:2] + [("2026-08-05", "260805_사업장가 산정 내역_v11.xlsx — 시트 1/2: 요약")]
    _cell, _why = R.series_cell(_md, _mixed[0][0], _mixed[0][1], _mixed)
    check("다른 엑셀 파일이 같은 날짜에 있으면 여전히 ⚠", "교체?" in _cell, f"{_cell!r}")
    check("그 사유에 상대 원본이 찍힌다", bool(_why) and "v11" in _why, f"{_why!r}")

# 엑셀이 아닌 문서는 손대지 않는다 — 같은 날짜의 다른 pdf 는 예전처럼 ⚠ 다.
with tempfile.TemporaryDirectory() as _d:
    _proj = Path(_d) / "사업장나"
    _proj.mkdir()
    _md = _proj / "20260805-보고.md"
    _md.write_text("x", encoding="utf-8")
    _pdfs = [("2026-08-05", "260805_보고.pdf"), ("2026-08-05", "260805_보고_수정.pdf")]
    _cell, _why = R.series_cell(_md, _pdfs[0][0], _pdfs[0][1], _pdfs)
    check("엑셀이 아니면 판정이 그대로다", "교체?" in _cell, f"{_cell!r}")

# ── excel_cell — Task 6: doc-archive 가 엑셀 시트를 점검표에 태운다 ────────
#
# `시트` 메타가 없으면(엑셀 문서가 아니면) 아무것도 판정하지 않는다 — `comment_cell` 과
# 같은 원칙("모르면 모른다"가 아니라 여기서는 "대상이 아니면 조용히 넘어간다"). 있으면
# meta.json(캐시)·본문을 대조해 다섯 가지 중 무엇이 걸리는지 정확한 문구로 낸다.
print("\nexcel_cell — 엑셀이 아닌 문서(본문에도 시트 헤더가 없다)는 판정하지 않는다")

_no_xlsx, _why_a, _why_b = R.excel_cell({}, [("2026-08-20", "260811_현황.xlsx")], "본문")
check("엑셀 문서가 아니면 —", _no_xlsx == "—" and not _why_a and not _why_b, repr(_no_xlsx))

print("\nexcel_cell — 본문에 시트 헤더가 있는데 '시트' 메타가 없으면 ⚠")
# 봇의 isSheetDoc 은 이 한 줄로만 엑셀을 가른다. 빠뜨리면 시트가 회차로 세어져
# 그 문서가 6,000자 색인에서 영원히 안 접히는 줄이 되는데, 예전에는 여기서도
# verify_format 에서도 키가 없으면 아무 검사도 안 돌아 어디에서도 안 드러났다.
_missing, _mwhy, _ = R.excel_cell(
    {},
    [("2026-08-20", "260811_현황.xlsx — 시트 1/2: 산정내역"),
     ("2026-08-20", "260811_현황.xlsx — 시트 2/2: 이자계산")],
    "본문",
)
check("'시트 메타 없음 ⚠' 이 칸에 나온다", _missing == "시트 메타 없음 ⚠", repr(_missing))
check("사유에 몇 건인지와 이유가 적힌다",
      "시트 헤더가 2건" in (_mwhy or "") and "안 접힙니다" in (_mwhy or ""), repr(_mwhy))

print("\nexcel_cell — meta.json 을 못 읽으면 ✓ 가 아니라 '점검 못 함' 이다")
# 「보고 깨끗했다」와 「아무것도 못 봤다」가 화면에서 같은 모양이면 안 된다 —
# 상한 초과로 뺀 시트·숨김·서식 미해석은 전부 그 파일에만 있다.

with tempfile.TemporaryDirectory() as _d:
    _cache = Path(_d)
    R.CACHE = _cache
    R._cache_index = None

    # `.blocks` 는 이제 캐시 루트가 아니라 **원본 파일 바로 옆**이다 — 원본이
    # 캐시에 없으면 excel_meta_json 이 「원본이 캐시에 없음」으로 먼저 멈춘다.
    _ch = _cache / "사업장가"
    _ch.mkdir(parents=True, exist_ok=True)
    _orig = _ch / "F0TEST1_260811_현황.xlsx"
    _orig.write_bytes(b"x")
    _blocks = _orig.with_name(_orig.name + ".blocks")

    _entries = [
        ("2026-08-20", "260811_현황.xlsx — 시트 1/3: 산정내역"),
        ("2026-08-20", "260811_현황.xlsx — 시트 2/3: 이자계산"),
        ("2026-08-20", "260811_현황.xlsx — 시트 3/3: 상환스케줄"),
    ]
    _meta = {"시트": "산정내역 · 이자계산 · 상환스케줄"}

    _cell, _fwhy, _ewhy = R.excel_cell(_meta, _entries, "<table><tr><td>1,520</td></tr></table>")
    check("캐시가 비었으면 ✓ 가 아니다", _cell != "✓", repr(_cell))
    check("칸에 'meta.json 없음 — 점검 못 함' 이 그대로 나온다",
          _cell == "meta.json 없음 — 점검 못 함 ⚠", repr(_cell))
    check("파일 단위 사유에도 같은 문구가 있다",
          "meta.json 없음 — 점검 못 함" in (_fwhy or ""), repr(_fwhy))

    # 파일은 있는데 JSON 이 깨진 경우도 조용히 넘어가지 않는다.
    _blocks.mkdir()
    (_blocks / "meta.json").write_text("{깨진", encoding="utf-8")
    _cellb, _fwhyb, _ = R.excel_cell(_meta, _entries, "본문")
    check("깨진 meta.json 도 ✓ 가 아니다",
          _cellb == "meta.json 을 읽지 못함 — 점검 못 함 ⚠", repr(_cellb))

    print("\nexcel_cell — 시트 개수가 메타와 본문에서 다르다")

    _meta_bad = {"시트": "산정내역 · 이자계산"}  # 메타는 2개, 본문(all_entries)은 3개
    _cell2, _fwhy2, _ewhy2 = R.excel_cell(_meta_bad, _entries, "본문")
    check("칸에 '시트 메타 2 ≠ 본문 3' 이 그대로 나온다",
          "시트 메타 2 ≠ 본문 3" in _cell2 and _cell2.endswith("⚠"), repr(_cell2))
    check("파일 단위 사유에도 같은 문구가 있다", "시트 메타 2 ≠ 본문 3" in (_fwhy2 or ""), repr(_fwhy2))

    print("\nexcel_cell — 깨끗하면 ✓ (meta.json 을 읽었고 걸린 것이 없다)")

    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
            "unformatted_cells": 0, "skipped_sheets": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _cellc, _fwhyc, _ewhyc = R.excel_cell(_meta, _entries, "<table><tr><td>1,520</td></tr></table>")
    check("읽었고 걸린 것이 없으면 ✓", _cellc == "✓", repr(_cellc))
    check("사유가 없다", not _fwhyc and not _ewhyc)

    print("\nexcel_cell — meta.json 의 skipped_sheets · hidden · unformatted")

    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": ["부록"],
            "hidden_rows": 14,
            "hidden_rows_with_content": 9,
            "unformatted_cells": 3,
            "skipped_sheets": [{"name": "backdata", "rows": 4180}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    _cell3, _fwhy3, _ewhy3 = R.excel_cell(_meta, _entries, "본문")
    check("상한 초과로 뺀 시트 문구가 그대로 나온다",
          "상한 초과로 뺀 시트: backdata(4,180행)" in (_fwhy3 or ""), repr(_fwhy3))
    # 숨긴 행 수와 「내용 있는 것」은 뜻이 다르다 — 이 줄의 용도가 「버린 것 중 진짜
    # 내용이 얼마나 되나」이므로 둘 다 나와야 한다. 개수만 적혀 있던 동안은 65개가
    # 전부 빈 줄인 경우와 46개에 내용이 있는 경우를 표에서 가릴 수 없었다.
    check("숨긴 시트·행 문구에 「내용 있는 것」까지 나온다",
          "숨긴 시트 1 · 숨긴 행 14(내용 있는 것 9)" in (_fwhy3 or ""), repr(_fwhy3))
    check("서식 해석 못 함 문구가 그대로 나온다",
          "서식 해석 못 함 3셀" in (_fwhy3 or ""), repr(_fwhy3))
    check("칸도 ⚠ 다", _cell3.endswith("⚠"), repr(_cell3))

    # ── 사유 내역 (2026-08-23) ──
    #
    # 개수만 적혀 있던 동안, 한 파일이 14.69% 였을 때 그것이 전부 한 원인이었는데도
    # 읽는 사람은 무엇을 봐야 할지 알 수 없었다. 사유는 `xlsx_to_blocks` 가 판정하는
    # 순간에 meta.json 에 적어 보내고, 여기서는 그것을 읽어 보이기만 한다 —
    # 셀에서 다시 알아내면 판정이 두 벌이 되어 조용히 갈린다.
    print("\nexcel_cell — 「해석 못 함」에 사유 내역이 붙는다")

    def _write_meta(reasons, cells):
        (_blocks / "meta.json").write_text(
            json.dumps({
                "sheets": ["산정내역", "이자계산", "상환스케줄"],
                "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
                "unformatted_cells": cells, "skipped_sheets": [],
                **({"unformatted_reasons": reasons} if reasons is not None else {}),
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    _write_meta({"openpyxl 이 못 푼 내장 서식": 390, "반올림이 렌더러마다 갈림": 2}, 392)
    _, _fwhy5, _ = R.excel_cell(_meta, _entries, "본문")
    check("사유와 건수가 개수 옆에 나온다",
          "서식 해석 못 함 392셀 (openpyxl 이 못 푼 내장 서식 390 · 반올림이 렌더러마다 갈림 2)"
          in (_fwhy5 or ""), repr(_fwhy5))

    # 표의 한 줄이라 길면 안 읽힌다 — 셋까지만 펼치고 나머지는 갈래 수로 접는다.
    _write_meta({"가": 5, "나": 4, "다": 3, "라": 2, "마": 1}, 15)
    _, _fwhy6, _ = R.excel_cell(_meta, _entries, "본문")
    check("넷째부터는 갈래 수로 접힌다",
          "서식 해석 못 함 15셀 (가 5 · 나 4 · 다 3 · 그 외 2갈래)" in (_fwhy6 or ""), repr(_fwhy6))

    # 옛 회차의 meta.json 에는 사유 키가 없다. 없는 것을 있는 척하지 않는다 —
    # 「사유가 없다」와 「사유를 안 남긴 변환분이다」는 다른 사실이다.
    _write_meta(None, 3)
    _, _fwhy7, _ = R.excel_cell(_meta, _entries, "본문")
    check("사유 키가 없으면 그렇다고 말한다",
          "서식 해석 못 함 3셀 (사유 미기록 — 옛 변환분)" in (_fwhy7 or ""), repr(_fwhy7))

    print("\nexcel_cell — 본문의 엑셀 오류 문자열은 meta.json 이 아니라 md 본문에서 센다")

    _block_err = "<table><tr><td>#REF!</td><td>1,520</td><td>#DIV/0!</td></tr></table>"
    _cell4, _fwhy4, _ewhy4 = R.excel_cell(_meta, _entries, _block_err)
    check("오류 셀 개수와 종류가 그대로 나온다",
          _ewhy4 == "오류 셀 2개 (#DIV/0! · #REF!)", repr(_ewhy4))
    check("회차 단위 사유(파일 단위와 분리)로 나온다", _fwhy4 is None or "오류 셀" not in _fwhy4,
          repr(_fwhy4))

    # ── excel_cell — unmaskable_sheets (Task 4 가 「가릴 방법이 없어 뺀 시트」를
    #    남기고, 아무도 그걸 안 보여줬다는 리뷰가 Task 7 에 이 조각을 더했다) ──
    #
    # 상한 초과로 뺀 시트(skipped_sheets)와 문구가 갈려야 한다 — 같은 「뺐다」인데
    # 이유가 다르고, 사람이 할 일도 다르다(하나는 상한을 올릴지, 하나는 머리글을
    # 손으로 넣거나 원본을 직접 열어 볼지).
    print("\nexcel_cell — 가릴 수 없어 뺀 시트(unmaskable_sheets)를 보여준다")

    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
            "unformatted_cells": 0, "skipped_sheets": [],
            "unmaskable_sheets": [
                {"name": "계약자명부", "why": "머리글 행을 못 찾아 개인정보 열을 가릴 수 없었다"}
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _cell8, _fwhy8, _ = R.excel_cell(_meta, _entries, "본문")
    check("칸이 ⚠ 다", _cell8.endswith("⚠"), repr(_cell8))
    check("사유에 시트 이름과 왜 못 가렸는지가 나온다",
          "가릴 수 없어 뺀 시트: 계약자명부(머리글 행을 못 찾아" in (_fwhy8 or ""), repr(_fwhy8))
    check("상한 초과 문구(상한 초과로 뺀 시트)와는 다른 문구다",
          "상한 초과로 뺀 시트" not in (_fwhy8 or ""), repr(_fwhy8))

    # ── excel_cell — masked 요약 (변환기가 가린 것을 사람이 볼 수 있게 한다) ──
    print("\nexcel_cell — 변환기가 마스킹한 요약을 보여준다")

    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
            "unformatted_cells": 0, "skipped_sheets": [], "unmaskable_sheets": [],
            "masked": {"columns": ["연락처", "이메일"], "rows": 12,
                       "comments_dropped": 1, "inline": 2},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _cell9, _fwhy9, _ = R.excel_cell(_meta, _entries, "본문")
    check("마스킹이 있으면 ⚠ 로 보인다(사람이 확인할 자리다)", _cell9.endswith("⚠"), repr(_cell9))
    check("가린 열 이름·행 수·인라인·메모 건수가 사유에 나온다",
          "마스킹함: 열 2개(연락처 · 이메일) · 행 12개 · 인라인 2건 · 메모 1건 뺌"
          in (_fwhy9 or ""), repr(_fwhy9))

    # 마스킹이 전혀 없으면(값 전부 0/빈 목록) 조용해야 한다 — 그래야 깨끗한 문서가
    # 계속 ✓ 한 줄로 남는다.
    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
            "unformatted_cells": 0, "skipped_sheets": [], "unmaskable_sheets": [],
            "masked": {"columns": [], "rows": 0, "comments_dropped": 0, "inline": 0},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _cell10, _fwhy10, _ewhy10 = R.excel_cell(_meta, _entries, "본문")
    check("가린 것이 없으면 ✓", _cell10 == "✓" and not _fwhy10 and not _ewhy10, repr(_cell10))

    # `masked` 키가 아예 없는 옛 변환분도 조용해야 한다 — 없는 것을 있는 척하지 않는다.
    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
            "unformatted_cells": 0, "skipped_sheets": [], "unmaskable_sheets": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _cell11, _fwhy11, _ewhy11 = R.excel_cell(_meta, _entries, "본문")
    check("masked 키가 없어도 ✓", _cell11 == "✓" and not _fwhy11 and not _ewhy11, repr(_cell11))

    # ── excel_cell — trimmed_sheets · untrimmed_sheets (Task 4, 유령 행) ──
    #
    # 값 없는 꼬리 행을 잘랐다는 사실과, 잘라야 하는데 못 잘랐다는 사실은 사람이
    # 봐야 할 것 중 하나다 — 사유는 `xlsx_to_blocks` 가 판정하는 그 순간에 적어 보낸다.
    print("\nexcel_cell — trimmed_sheets(자른 것)를 보여준다")

    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
            "unformatted_cells": 0, "skipped_sheets": [], "unmaskable_sheets": [],
            "trimmed_sheets": [{"name": "정산", "kept": 71, "dropped": 1047731}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _cell12, _fwhy12, _ = R.excel_cell(_meta, _entries, "본문")
    # `kept` 는 **행 번호**(그 행까지 남겼다)다 — 성긴 시트에서는 남은 행 수와 다르므로
    # 「71행 남기고」로 읽히면 틀린 숫자가 된다.
    check("자른 시트 문구가 그대로 나온다 (kept 는 '…행까지')",
          "값 없는 꼬리 행을 잘람: 정산(71행까지 남기고 1,047,731행)" in (_fwhy12 or ""), repr(_fwhy12))
    # **자른 것만으로는 ⚠ 가 안 뜬다.** 자르기는 평상시에 일어나고(실물 53MB 파일에서
    # 5시트 중 4시트) SKILL.md 도 사람이 할 일은 없다고 적는다 — 여기서 ⚠ 를 띄우면
    # 「사람이 봐야 한다」는 표시가 매번 뜨고 곧 아무도 안 본다.
    check("자른 것만 있으면 ⚠ 가 아니다", not _cell12.endswith("⚠"), repr(_cell12))
    check("칸에는 자른 사실이 보인다", "꼬리 행 잘람" in _cell12, repr(_cell12))

    # 자른 것 + 정말 봐야 할 것이 함께 있으면 **헤드라인은 봐야 할 쪽**이다.
    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
            "unformatted_cells": 0, "unmaskable_sheets": [],
            "skipped_sheets": [{"name": "계약자list", "rows": 1379}],
            "trimmed_sheets": [{"name": "정산", "kept": 71, "dropped": 1047731}],
            "masked": {"columns": ["연락처"], "rows": 12, "comments_dropped": 0, "inline": 0},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _cell12b, _fwhy12b, _ = R.excel_cell(_meta, _entries, "본문")
    check("헤드라인은 자른 것이 아니라 상한 초과로 뺀 시트다",
          _cell12b.startswith("상한 초과로 뺀 시트") and _cell12b.endswith("⚠"), repr(_cell12b))
    check("자른 사실은 사유에 그대로 남는다",
          "값 없는 꼬리 행을 잘람: 정산" in (_fwhy12b or ""), repr(_fwhy12b))
    check("꼬리의 「외 N건」이 자른 줄까지 센다", "외 2건" in _cell12b, repr(_cell12b))

    print("\nexcel_cell — untrimmed_sheets(못 자른 것)를 보여준다")

    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
            "unformatted_cells": 0, "skipped_sheets": [], "unmaskable_sheets": [],
            "untrimmed_sheets": [{"name": "부록", "ghost_rows": 900000,
                                  "why": "표에 안 보이는 참조가 1047802행까지 걸쳐 있다"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _cell13, _fwhy13, _ = R.excel_cell(_meta, _entries, "본문")
    check("못 자른 유령 행 문구가 그대로 나온다",
          "못 자른 유령 행: 부록(900,000행 — 표에 안 보이는 참조가 1047802행까지 걸쳐 있다)"
          in (_fwhy13 or ""), repr(_fwhy13))
    # **이쪽은 사람 눈이 필요하다** — 그 시트는 유령 행을 그대로 안고 로드되므로 다음에
    # 또 터지고, 사유가 「되돌렸다」면 자르는 코드의 결함이다. 자른 것과 갈라 두는 선이
    # 여기다.
    check("못 자른 것은 ⚠ 로 뜨고 헤드라인을 갖는다",
          _cell13.startswith("못 자른 유령 행") and _cell13.endswith("⚠"), repr(_cell13))

    # 두 키가 다 없는 것은 옛 변환분(자르는 기능이 없던 시절)과 「자를 것이 없었다」를
    # 구분하지 않는다 — 이 함수는 그 구분을 하지 않기로 정했다(Task 4 계약). 미래에
    # 누군가 빈 목록을 위해 빈 줄을 찍기 시작해도 여기서 잡아야 한다.
    (_blocks / "meta.json").write_text(
        json.dumps({
            "sheets": ["산정내역", "이자계산", "상환스케줄"],
            "hidden_sheets": [], "hidden_rows": 0, "hidden_rows_with_content": 0,
            "unformatted_cells": 0, "skipped_sheets": [], "unmaskable_sheets": [],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _cell14, _fwhy14, _ewhy14 = R.excel_cell(_meta, _entries, "본문")
    check("두 키가 다 없으면 자른 것도 못 자른 것도 안 나온다",
          "값 없는 꼬리 행을 잘람" not in (_fwhy14 or "") and "못 자른 유령 행" not in (_fwhy14 or ""),
          repr(_fwhy14))
    check("두 키가 다 없으면 ✓", _cell14 == "✓" and not _fwhy14 and not _ewhy14, repr(_cell14))

    # ── excel_note_listed — 조용한 사실은 「손볼 곳」 목록의 자리를 안 먹는다 ──
    #
    # 목록은 `--verbose` 가 아니면 12줄에서 잘린다. 자르기는 평상시에 일어나므로
    # (실물 53MB 파일에서 5시트 중 4시트) 자른 파일이 열둘을 넘으면 그 줄들이
    # **진짜 지적을 12줄 밖으로 민다** — 칸에서는 ⚠ 를 안 띄우게 이미 갈라 놓았는데
    # 목록에서는 안 갈라져 있었다.
    print("\nexcel_note_listed — 조용한 사실은 목록에 안 싣는다")
    check("자른 것만 있는 칸은 목록에 안 실린다",
          not R.excel_note_listed("✓ · 꼬리 행 잘람", "값 없는 꼬리 행을 잘람: 정산(71행까지 남기고 1,047,731행)"))
    check("⚠ 인 칸은 목록에 실린다",
          R.excel_note_listed("상한 초과로 뺀 시트: 계약자list(1,379행) 외 2건 ⚠",
                              "상한 초과로 뺀 시트: 계약자list(1,379행) / 값 없는 꼬리 행을 잘람: 정산"))
    check("사유가 없으면 ⚠ 든 아니든 안 실린다",
          not R.excel_note_listed("✓", None) and not R.excel_note_listed("✓", ""))
    # `--verbose` 는 자르지 않으므로 조용한 사실도 다 보여야 한다 — 어느 시트를 몇
    # 행까지 남겼나를 볼 수 있어야 한다는 것이 `detail` 을 만든 이유다.
    check("--verbose 면 조용한 사실도 실린다",
          R.excel_note_listed("✓ · 꼬리 행 잘람", "값 없는 꼬리 행을 잘람: 정산", verbose=True))

    # ── 2차 머리글이 md 메타에 남나 ─────────────────────────────────────────
    #
    # header_fallback_sheets 는 캐시 meta.json 에만 있고 prune_cache 가 커밋 뒤에 지운다.
    # 그래서 메타에 안 적으면 「2차로 잡았다」가 그 회차와 함께 사라지고, 「몇 주 보고
    # 잦으면 판정을 좁힌다」를 나중에 셀 재료가 없어진다.
    check("2차 시트가 없으면 아무 말도 안 한다",
          R.fallback_meta_note([], None) is None)
    check("적혀 있으면 조용하다",
          R.fallback_meta_note(["급여계좌"], "급여계좌") is None)
    check("순서가 달라도 같은 것으로 본다",
          R.fallback_meta_note(["갑", "을"], "을 · 갑") is None)
    check("메타 줄이 아예 없으면 적을 문장을 준다",
          (R.fallback_meta_note(["급여계좌"], None) or "").find("**2차 머리글**: 급여계좌") >= 0)
    check("적힌 것이 실물과 다르면 지금 적힌 것도 함께 보여 준다",
          "고정계좌" in (R.fallback_meta_note(["급여계좌"], "고정계좌") or ""))
    # 사람이 넣은 공백·빈 항목이 「다르다」로 잡히면 고칠 방법이 없는 거짓 실패가 된다.
    check("공백과 빈 항목은 무시한다",
          R.fallback_meta_note(["갑", "을"], "  갑 ·  · 을  ") is None)


# ── verify_format 5-1 검사 — 엑셀 `시트` 메타 ────────────────────────────
#
# 이 검사가 여기 있는 이유: 7.5 점검표의 「계약」 칸이 verify_format.check 를 그대로
# 부르므로, 그쪽이 조용하면 점검표도 조용하다. 두 가지를 본다 — 메타 줄이 아예 없는
# 경우(예전에는 아무 검사도 안 돌았다)와, 시트 이름 끝의 공백(양쪽을 같은 기준으로
# 떼지 않으면 사람이 고칠 방법이 없는 거짓 실패가 된다).
import verify_format as VF  # noqa: E402

def sheet_md(path: Path, sheet_line: str, second: str):
    """시트 2개짜리 엑셀 문서 md. `sheet_line` 이 빈 문자열이면 '시트' 메타를 안 넣는다."""
    lines = [
        "# [산정내역] 시험 엑셀",
        "",
        "> **사업장**: 시험 · **종류**: 산정내역",
        "> **열람**: 공개",
    ]
    if sheet_line:
        lines.append(sheet_line)
    lines += [
        "> **값 표기**: 엑셀 화면 표시 그대로 (서식으로 반올림된 자리가 있음)",
        "",
        "---",
        "",
        "## 2026-08",
        "",
        "**2026-08-05 · 260805_시험.xlsx — 시트 1/2: 산정내역**",
        "",
        "<table><tr><td>1,520</td></tr></table>",
        "",
        f"**2026-08-05 · 260805_시험.xlsx — 시트 2/2: {second}**",
        "",
        "<table><tr><td>3,120</td></tr></table>",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


print("\nverify_format 5-1 — '시트' 메타 줄이 없으면 ✗ (색인에서 영원히 안 접힌다)")

with tempfile.TemporaryDirectory() as _d:
    _p = Path(_d) / "20260805-시험-산정내역.md"

    sheet_md(_p, "", "이자계산")
    _probs, _, _ = VF.check(_p)
    _hit = [x for x in _probs if "시트" in x and "메타 줄이 없습니다" in x]
    check("메타 줄이 없으면 ✗ 가 난다", bool(_hit), repr(_probs))

    sheet_md(_p, "> **시트**: 산정내역 · 이자계산", "이자계산")
    _probs2, _, _ = VF.check(_p)
    check("제대로 적으면 문제 없다", not _probs2, repr(_probs2))

    print("\nverify_format 5-1 — 시트 이름 끝의 공백은 양쪽 다 떼고 견준다")
    # `산정 정리 `(끝에 공백)는 어느 사업장의 산정내역 엑셀에 실재하는
    # 모양이다. parse_meta 가
    # 메타 값을 이미 떼기 때문에, 본문 쪽을 안 떼면 두 목록이 화면에 똑같이 보이면서
    # 영영 안 맞는다 — 사람이 md 를 고쳐도 벗어날 길이 없는 거짓 실패다.
    sheet_md(_p, "> **시트**: 산정내역 · 산정 정리", "산정 정리 ")
    _probs3, _, _ = VF.check(_p)
    check("끝 공백만 다른 이름은 통과한다", not _probs3, repr(_probs3))

    # 반대쪽 — 진짜로 다른 이름은 여전히 잡혀야 한다 (떼기가 검사를 무디게 만들지 않았나)
    sheet_md(_p, "> **시트**: 산정내역 · 이자계산", "상환스케줄")
    _probs4, _, _ = VF.check(_p)
    check("이름이 진짜 다르면 여전히 ✗", any("≠" in x for x in _probs4), repr(_probs4))


# ── cache_origin — 엑셀 회차의 원본을 찾는다 ───────────────────────────────
#
# 엑셀 회차의 `source` 에는 `— 시트 n/N: 이름` 이 붙어 있다(SKILL.md 「엑셀 변환」).
# 캐시에 저장된 파일 이름에는 그런 꼬리가 없으므로, 떼지 않고 찾으면 **원본이 눈앞의
# 캐시에 있는데도 영영 못 찾는다.** 그러면 `comment_cell` 이 「원본이 캐시에 없어
# 확인하지 못했습니다」라는 **틀린 사유**를 낸다 — 2026-08-23 에 실제로 그랬고,
# 사유가 틀리면 다음 사람이 캐시를 뒤지거나 원본을 다시 받아오는 엉뚱한 곳을 본다.
# `series_cell` 이 시트를 개정본으로 오탐하던 것과 **같은 원인**이라 같은 함수로 뗀다.
print("\ncache_origin — 엑셀 회차의 시트 표기를 떼고 캐시를 찾는다")

with tempfile.TemporaryDirectory() as _d:
    _cache = Path(_d) / "사업장가"
    _cache.mkdir(parents=True)
    (_cache / "F0BMPFFTA5V_260805_산정 내역_v10.xlsx").write_text("x", encoding="utf-8")
    _saved, R.CACHE, R._cache_index = R.CACHE, Path(_d), None
    try:
        _src = "260805_산정 내역_v10.xlsx — 시트 3/4: CP(91)"
        _origin, _why = R.cache_origin(_src, "사업장가")
        check("시트 표기가 붙은 회차도 캐시 원본을 찾는다",
              _origin is not None and _origin.name.endswith("260805_산정 내역_v10.xlsx"),
              f"{_origin!r} {_why!r}")
        _plain, _ = R.cache_origin("260805_산정 내역_v10.xlsx", "사업장가")
        check("시트 표기가 없는 회차도 그대로 찾는다", _plain is not None, repr(_plain))
        _none, _ = R.cache_origin("없는 파일.xlsx", "사업장가")
        check("정말 없으면 여전히 None", _none is None, repr(_none))
    finally:
        R.CACHE, R._cache_index = _saved, None

# ── changed_entries — 헤더가 그대로여도 본문이 바뀌면 잡는다 (2026-08-23) ──
#
# `added_entries` 는 diff 의 **추가된 줄에 회차 헤더가 있을 때만** 회차를 잡는다.
# 그래서 이미 커밋된 문서의 표 안 값 하나를 고치면 회차가 0건이 되고, 표가 「못 찾음 ⚠」
# 한 줄만 내면서 마스킹·엑셀·본문 축을 통째로 건너뛰었다(어느 사업장의 산정내역
# 엑셀을 재변환할 때 실제로 발생).
#
# **별개 저장소를 임시로 만들어 잰다.** WHK 저장소에 검증용 커밋을 만들지 않는다.
print("\nchanged_entries — 본문만 바뀐 회차를 되찾는다")

_DOC = "\n".join([
    "# [산정내역] 시험",
    "",
    "> **사업장**: 시험 · **종류**: 산정내역",
    "> **열람**: 공개",
    "",
    "---",
    "",
    "## 2026-06",
    "",
    "**2026-06-11 · 시험.xlsx — 시트 1/2: 요약**",
    "",
    "<table><tr><td>선순위</td><td>150</td></tr></table>",
    "",
    "**2026-06-11 · 시험.xlsx — 시트 2/2: 내역**",
    "",
    "<table><tr><td>중순위</td><td>-0</td></tr></table>",
    "",
])

with tempfile.TemporaryDirectory() as _d:
    _repo = Path(_d)
    _run = lambda *a: subprocess.run(["git", *a], cwd=_repo, capture_output=True, text=True)
    _run("init", "-q")
    _run("config", "user.email", "t@t")
    _run("config", "user.name", "t")
    _md = _repo / "50-resources" / "documents" / "projects" / "시험" / "20260611-시험.md"
    _md.parent.mkdir(parents=True, exist_ok=True)
    _md.write_text(_DOC, encoding="utf-8")
    _run("add", "-A")
    _run("commit", "-qm", "첫 판")

    _keep = R.ROOT
    try:
        R.ROOT = _repo
        # ① 아무것도 안 바꿨으면 둘 다 빈손이어야 한다
        check("안 바꾸면 0건", R.changed_entries(_md) == [], R.changed_entries(_md))

        # ② 두 번째 블록의 값 하나만 고친다 — 헤더는 그대로다
        _md.write_text(_DOC.replace("<td>-0</td>", "<td>0</td>"), encoding="utf-8")
        _added = R.added_entries(_md, False)
        _changed = R.changed_entries(_md)
        check("added_entries 는 못 잡는다 (이게 옛 사각지대다)", _added == [], _added)
        check("changed_entries 가 **바뀐 그 블록만** 잡는다",
              _changed == [("2026-06-11", "시험.xlsx — 시트 2/2: 내역")], _changed)
        check("메타 변경으로 오인하지 않는다", R.meta_changed(_md, False) is False)

        # ③ 메타만 고치면 회차로 안 잡혀야 한다 — 그래야 「메타만」 갈래가 산다
        _md.write_text(_DOC.replace("> **열람**: 공개", "> **열람**: 비공개"), encoding="utf-8")
        check("메타만 고치면 changed_entries 는 0건", R.changed_entries(_md) == [],
              R.changed_entries(_md))
        check("그때는 meta_changed 가 참", R.meta_changed(_md, False) is True)

        # ④ 표에서 한 행을 **지운** 것도 잡아야 한다 (덩이 줄 수가 0 으로 온다)
        _md.write_text(_DOC.replace("<table><tr><td>중순위</td><td>-0</td></tr></table>\n", ""),
                       encoding="utf-8")
        check("행을 지운 것도 잡는다", R.changed_entries(_md) != [], R.changed_entries(_md))
    finally:
        R.ROOT = _keep

# ── meta_changed — `2차 머리글` 줄도 메타 변경으로 잡는다 ──────────────────
#
# 안 잡으면 이 줄만 고친 커밋이 회차도 못 찾고 메타 변경도 못 찾아 7.5 점검표가
# 매번 「못 찾음 ⚠」를 낸다 — 2차 머리글을 md 에 적어 두라고 만든 규칙인데,
# 그 줄을 적을 때마다 사람이 git diff 로 손수 확인해야 하는 상태가 된다.
print("\nmeta_changed — `2차 머리글` 줄도 메타 변경으로 잡는다")

with tempfile.TemporaryDirectory() as _d2:
    _repo2 = Path(_d2)
    _run2 = lambda *a: subprocess.run(["git", *a], cwd=_repo2, capture_output=True, text=True)
    _run2("init", "-q")
    _run2("config", "user.email", "t@t")
    _run2("config", "user.name", "t")
    _md2 = _repo2 / "50-resources" / "documents" / "projects" / "시험" / "20260611-시험.md"
    _md2.parent.mkdir(parents=True, exist_ok=True)
    _md2.write_text(_DOC, encoding="utf-8")
    _run2("add", "-A")
    _run2("commit", "-qm", "첫 판")

    _keep2 = R.ROOT
    try:
        R.ROOT = _repo2
        _md2.write_text(_DOC.replace(
            "> **열람**: 공개",
            "> **열람**: 공개\n> **2차 머리글**: 급여계좌",
        ), encoding="utf-8")
        check("`2차 머리글` 줄만 늘어도 changed_entries 는 0건",
              R.changed_entries(_md2) == [], R.changed_entries(_md2))
        check("meta_changed 가 `2차 머리글` 을 메타 변경으로 본다",
              R.meta_changed(_md2, False) is True)
    finally:
        R.ROOT = _keep2

# ── meta_changed — 메타 블록 전체를 견준다, 다섯 줄짜리 목록이 아니다 (2026-09-03) ──
#
# **재현.** `review_batch.py` 의 `meta_changed` 가 보던 것은 열람·공개승인·비공개 사유·2차
# 머리글 넷(변경추적까지 다섯)뿐이었다. 그 목록에 없는 칸(`주요 항목`·`수록 범위`)만
# 고치면 changed_entries 도 0건(회차 헤더는 그대로니까)·meta_changed 도 거짓이 나서
# 표가 「새 회차도 메타 변경도 못 찾았습니다 ⚠」를 냈다 — 사람이 매번 git diff HEAD 를
# 손으로 열어야 했다. 2026-09-03 전수(431개 md)로 실제 쓰이는 칸이 27종임을 확인했고,
# 목록을 스물여덟째로 늘리는 대신 메타 블록 전체(`meta_lines`)를 견주는 쪽으로 고쳤다.
print("\nmeta_changed — 메타 블록 전체를 견준다 (목록에 없던 칸도 잡힌다)")

with tempfile.TemporaryDirectory() as _d3:
    _repo3 = Path(_d3)
    _run3 = lambda *a: subprocess.run(["git", *a], cwd=_repo3, capture_output=True, text=True)
    _run3("init", "-q")
    _run3("config", "user.email", "t@t")
    _run3("config", "user.name", "t")
    _md3 = _repo3 / "50-resources" / "documents" / "projects" / "시험" / "20260611-시험.md"
    _md3.parent.mkdir(parents=True, exist_ok=True)
    _md3.write_text(_DOC, encoding="utf-8")
    _run3("add", "-A")
    _run3("commit", "-qm", "첫 판")

    _keep3 = R.ROOT
    try:
        R.ROOT = _repo3
        check("아무것도 안 바꾸면 meta_changed 는 거짓",
              R.meta_changed(_md3, False) is False)

        # `주요 항목`·`수록 범위` — 옛 다섯 줄짜리 목록에 없던 칸. 이게 이 결함의
        # 재현이다: 옛 코드로는 이 변경이 changed_entries 도 meta_changed 도 못 잡는다.
        for _field in ("주요 항목", "수록 범위"):
            _md3.write_text(_DOC.replace(
                "> **열람**: 공개",
                f"> **열람**: 공개\n> **{_field}**: 새로 적은 값",
            ), encoding="utf-8")
            check(f"`{_field}` 줄만 늘어도 changed_entries 는 0건 (회차 헤더는 그대로다)",
                  R.changed_entries(_md3) == [], R.changed_entries(_md3))
            check(f"`{_field}` 처럼 옛 목록에 없던 칸도 meta_changed 가 참으로 잡는다",
                  R.meta_changed(_md3, False) is True)
            _md3.write_text(_DOC, encoding="utf-8")  # 다음 반복을 위해 원복

        # 메타가 아니라 회차 헤더 뒤 본문만 바뀌면 여전히 거짓이어야 한다 — 넓힌 것이
        # 「메타 블록 밖」까지 삼키면 changed_entries 와 meta_changed 가 항상 같이
        # 참이 되어 「메타만」 갈래가 없어진다.
        _md3.write_text(_DOC.replace("<td>-0</td>", "<td>0</td>"), encoding="utf-8")
        check("본문만 바뀌면 meta_changed 는 여전히 거짓 (메타 블록 밖이다)",
              R.meta_changed(_md3, False) is False)
    finally:
        R.ROOT = _keep3

# ── excel_meta_json — `.blocks` 를 캐시 루트가 아니라 원본 옆에서 찾는다 (Task 3) ──
#
# 2026-08-25 까지는 `~/.doc-cache/<원본명>.xlsx.blocks/` 라 채널이 이름에 없었다.
# 같은 이름 파일이 두 채널에 올라오면 나중에 변환한 쪽이 앞엣것을 **에러 없이
# 덮었다.** 원본 경로가 이미 채널과 파일 ID 를 갖고 있어(`<채널>/<파일ID>_<이름>`)
# `.blocks` 를 그 옆에 두면 겹칠 자리가 없다.
print("\nexcel_meta_json — .blocks 를 캐시 루트가 아니라 원본 옆에서 찾는다")

with tempfile.TemporaryDirectory() as _d:
    _cache = Path(_d)
    R.CACHE = _cache
    R._cache_index = None

    _ch = _cache / "사업장가"
    _ch.mkdir(parents=True, exist_ok=True)
    _orig = _ch / "F0AAA_260811_현황.xlsx"
    _orig.write_bytes(b"x")
    _bl = _ch / "F0AAA_260811_현황.xlsx.blocks"
    _bl.mkdir(exist_ok=True)
    (_bl / "meta.json").write_text('{"시트": 2}', encoding="utf-8")
    _mj, _why = R.excel_meta_json("260811_현황.xlsx", "사업장가")
    check("원본 옆의 meta.json 을 읽었다", _mj and _mj.get("시트") == 2, repr((_mj, _why)))

    print("\nexcel_meta_json — 같은 이름이 두 채널에 있어도 제 것을 연다")
    _ch2 = _cache / "사업장바"
    _ch2.mkdir(parents=True, exist_ok=True)
    _orig2 = _ch2 / "F0BBB_260811_현황.xlsx"
    _orig2.write_bytes(b"y")
    _bl2 = _ch2 / "F0BBB_260811_현황.xlsx.blocks"
    _bl2.mkdir(exist_ok=True)
    (_bl2 / "meta.json").write_text('{"시트": 9}', encoding="utf-8")
    R._cache_index = None
    _mj2, _why2 = R.excel_meta_json("260811_현황.xlsx", "사업장바")
    check("사업장바 것을 열었다 (사업장가 것이 아니다)", _mj2 and _mj2.get("시트") == 9,
          repr((_mj2, _why2)))

    print("\nexcel_meta_json — 채널을 모르고 후보가 여럿이면 짐작하지 않고 사유를 낸다")
    R._cache_index = None
    _mj3, _why3 = R.excel_meta_json("260811_현황.xlsx", None)
    check("짐작하지 않고 멈추고 사유를 낸다", _mj3 is None and bool(_why3), repr((_mj3, _why3)))

# ── sweep_masks — 이미 커밋된 옛 문서까지 전수로 훑는다 (Task 5, 2026-08-26) ──
#
# review_batch 의 다른 축은 전부 git 작업 트리(이번에 바뀐 것)만 본다. 구간 D 에서
# 400여건의 옛 문서에 남은 개인정보를 찾으려면 그 경계 밖으로 나가는 모드가 있어야
# 한다 — "훑은 파일 수"를 함께 주는 것이 핵심이다. 안 주면 "0건"과 "안 셌다"가
# 화면에서 같은 모양이 된다.
print("\n[전수 훑기]")
import tempfile as _tf2  # noqa: E402
from pathlib import Path as _P  # noqa: E402

with _tf2.TemporaryDirectory() as _d:
    _root = _P(_d)
    (_root / "가").mkdir()
    (_root / "가" / "a.md").write_text(
        "# 문서\n\n담당 홍길동 부장\n대표 02-333-4444 입니다\n", encoding="utf-8")
    (_root / "가" / "b.md").write_text(
        "# 문서\n\n계약자 홍길동 · 납부일 2026-08-26\n", encoding="utf-8")
    _hits, _n, _fails = R.sweep_masks(_root)
    check("훑은 파일 수를 함께 준다", _n == 2, f"{_n}")
    check("남은 유선전화를 찾는다",
          any(h["label"] == "유선전화" and h["line"] == 4 for h in _hits), repr(_hits))
    check("깨끗한 파일에서는 아무것도 안 찾는다",
          not any(h["file"].endswith("b.md") for h in _hits), repr(_hits))
    check("읽기 실패가 없으면 빈 목록이다", _fails == [], repr(_fails))

# ── 읽기 실패는 "잔여 개인정보" 가 아니다 (리뷰 지적, 2026-08-26) ──────────
#
# 처음 구현은 읽기 실패를 `hits` 안에 라벨만 다르게 넣어 담았다. 그러면
# `len(hits)` 가 진짜 개인정보 잔여 건수와 읽기 실패 건수를 합산해 세는 꼴이 되고,
# 파일 하나만 못 읽어도 "N건" 이 실제 잔여보다 부풀려진다. 구간 D 가 이 N 을
# 전후 비교 기준값으로 쓰므로, 라벨로 걸러도 안 걸러지는 오염은 그대로 "조용한
# 0건"의 다른 모양이다. 그래서 `hits` 와 `read_failures` 를 아예 다른 목록으로
# 나눈다 — 개인정보 1건과 읽기 실패 1건이 섞인 디렉터리를 돌려 **각각 1건씩**임을
# 증명한다(2 로 뭉치지 않는다).
print("\n[전수 훑기 — 읽기 실패와 개인정보 잔여는 서로 다른 집계다]")

with _tf2.TemporaryDirectory() as _d2:
    _root2 = _P(_d2)
    (_root2 / "나").mkdir()
    _bad = _root2 / "나" / "bad.md"
    _bad.write_bytes(b"\xff\xfe\x00broken")  # utf-8 로 못 여는 바이트열
    (_root2 / "나" / "good.md").write_text(
        "# 문서\n\n대표 031-123-4567 입니다\n", encoding="utf-8")  # 진짜 잔여 1건(유선전화)
    _hits2, _n2, _fails2 = R.sweep_masks(_root2)
    check("읽지 못해도 훑은 파일 수에는 들어간다 (시도는 했다)", _n2 == 2, f"{_n2}")
    check("개인정보 잔여는 정확히 1건이다 (읽기 실패와 안 섞인다)",
          len(_hits2) == 1 and _hits2[0]["label"] == "유선전화", repr(_hits2))
    check("읽기 실패도 정확히 1건이다", len(_fails2) == 1, repr(_fails2))
    check("읽기 실패 항목은 bad.md 를 가리킨다",
          _fails2[0]["file"].endswith("bad.md"), repr(_fails2))
    check("읽기 실패 항목에는 라벨/값이 아니라 사유만 있다 (잔여 목록이 아니다)",
          "reason" in _fails2[0] and "label" not in _fails2[0], repr(_fails2))

# ── `--mask-sweep` 의 범위 — 도움말이 말하는 「아카이브 md 전체」와 같은가 ──
#
# 도움말과 SKILL.md 는 「아카이브 md 전체」라고 적는데 실제로 훑던 것은
# `documents/projects` 하나였다. **대화 아카이브 md 는 이 도구로 한 번도 안 훑렸고**,
# 거기에 사람이 슬랙에 직접 적은 미마스킹 휴대폰이 실제로 남아 있었다 (2026-09-03).
# 「전체」라고 말하면서 한 갈래만 보는 것은 조용한 0 의 전형이다 — 화면은 깨끗한데
# 안 본 자리가 있다.
#
# `sweep_masks` 는 뿌리를 인자로 받으므로 함수만 보면 멀쩡하다. 갈리는 자리는
# **`main` 이 어느 뿌리를 주느냐**뿐이라, 실제로 스크립트를 돌려서 본다.
print("\n[전수 훑기 — 범위가 자료 저장소 전체인가]")

import os as _os  # noqa: E402

with _tf2.TemporaryDirectory() as _d3:
    _fake = _P(_d3)
    (_fake / "documents" / "projects" / "가").mkdir(parents=True)
    (_fake / "documents" / "projects" / "가" / "문서.md").write_text(
        "# 문서\n\n대표 02-333-4444 입니다\n", encoding="utf-8")
    # 대화 아카이브 — 사람이 슬랙에 직접 적은 휴대폰. 여기가 안 훑기던 자리다.
    (_fake / "slack-export" / "channels").mkdir(parents=True)
    (_fake / "slack-export" / "channels" / "나.md").write_text(
        "# 채널\n\n> **└ 아무개** — 유선으로 설명해주셔도 됩니다 010-1234-5678\n",
        encoding="utf-8")
    # `documents/` 바로 아래(색인·제외 목록)와 저장소 뿌리의 운영 노트도 md 다.
    (_fake / "documents" / "index.md").write_text(
        "# 색인\n\n문의 gildong@naver.com\n", encoding="utf-8")

    _env = dict(_os.environ, HERMES_DATA_ROOT=str(_fake), PYTHONDONTWRITEBYTECODE="1")
    _p = subprocess.run(
        [sys.executable, str(_P(__file__).resolve().parent / "review_batch.py"),
         "--mask-sweep"],
        capture_output=True, text=True, encoding="utf-8", env=_env)
    _out = _p.stdout + _p.stderr

    check("문서 아카이브의 잔여를 찾는다", "문서.md" in _out, repr(_out))
    check("대화 아카이브(slack-export)의 잔여도 찾는다 — 「전체」라고 말하는 자리다",
          "나.md" in _out, repr(_out))
    check("documents/ 바로 아래의 md 도 훑는다", "index.md" in _out, repr(_out))
    check("훑은 md 개수가 세 갈래를 다 센다", "md 3개" in _out, repr(_out))
    # 경로를 어느 뿌리 기준으로 찍든, **훑은 뿌리 밖으로 나가면 죽는다.**
    # 예전에는 `documents/` 기준으로 상대경로를 만들어서, 범위를 넓히는 순간
    # `slack-export/...` 에서 ValueError 로 터진다.
    check("뿌리 밖 경로에서 죽지 않는다 (종료코드 0)", _p.returncode == 0,
          f"rc={_p.returncode} {_out!r}")
    check("어느 뿌리를 훑었는지 화면에 적는다", str(_fake) in _out, repr(_out))

# ── sweep_footer — 훑기가 자기 한계를 말하나 ────────────────────────────
import re  # noqa: E402
from review_batch import sweep_footer  # noqa: E402

_f0 = sweep_footer(406, 0, 0, {})
_f1 = sweep_footer(406, 3, 1, {"계좌": 2, "이메일": 1})

check("잔여 0건에도 훑은 파일 수와 건수를 찍는다",
      "md 406개" in _f0 and "0건" in _f0, repr(_f0))

# **잔여 건수(n_hits)가 실제로 찍히는지**를 본다. "0건" 이 본문에 있다는 것만으로는
# 부족하다 — n_hits 를 아예 안 찍는 판도 "읽기 실패 0건" 문구 때문에 통과한다
# (2026-08-27 리뷰어가 실측). "훑어 N건" 자리에서 n_hits 값을 직접 뽑아 맞춘다.
_m0 = re.search(r"훑어 (\d+)건", _f0)
_m1 = re.search(r"훑어 (\d+)건", _f1)
check("잔여 0건일 때 '훑어 N건' 이 n_hits(0) 값을 찍는다",
      _m0 is not None and _m0.group(1) == "0", repr(_f0))
check("잔여가 있을 때 '훑어 N건' 이 n_hits(3) 값을 찍는다",
      _m1 is not None and _m1.group(1) == "3", repr(_f1))

# **0건일 때도 한계를 말해야 한다** — 정확히 그때 오해가 난다.
check("잔여 0건에도 「줄 단위라 표에 걸친 계좌는 못 본다」를 말한다",
      "줄 단위" in _f0 and "점검표" in _f0, repr(_f0))

check("잔여가 있을 때도 같은 한계 문장을 말한다",
      "줄 단위" in _f1 and "점검표" in _f1, repr(_f1))

check("라벨별 개수와 읽기 실패를 함께 찍는다",
      "계좌 2" in _f1 and "이메일 1" in _f1 and "읽기 실패 1건" in _f1, repr(_f1))

# ── 변경추적 (WHK 결정 2026-09-03: 막지 말고 보이게 한다) ──────────────────
_tc = R.track_change_note
check("엑셀·pdf 는 변경추적을 안 본다 (docx 는 4단계에 이미 세는 자리가 있다)",
      _tc("현황.xlsx", None) is None and _tc("검토.pdf", None) is None)
check("hwp 인데 메타가 없으면 「안 쟀다」고 말한다 (0건과 같은 화면이면 안 된다)",
      "안 쟀다" in (_tc("보고.hwp", None) or ""), repr(_tc("보고.hwp", None)))
check("hwpx 도 같다", "안 쟀다" in (_tc("보고.hwpx", None) or ""))
check("대소문자가 달라도 hwp 로 본다", _tc("보고.HWP", None) is not None)
check("0건이라고 적혀 있으면 볼 것이 없다", _tc("보고.hwp", "0건") is None)
check("켜져 있으면 겹친 숫자를 보라고 말한다",
      "138건" in (_tc("보고.hwp", "138건 · 작성자 2명") or ""),
      repr(_tc("보고.hwp", "138건 · 작성자 2명")))
# **`10건`·`130건` 이 0 으로 읽히면 켜진 회차가 조용히 통과한다.**
check("`10건` 을 0건으로 읽지 않는다", _tc("보고.hwp", "10건") is not None, repr(_tc("보고.hwp", "10건")))
check("`130건` 을 0건으로 읽지 않는다", _tc("보고.hwp", "130건") is not None)
check("한글 화면에 안 보인다는 것을 말한다", "한글 화면" in (_tc("보고.hwp", "9건") or ""))

# 메타만 고친 것을 관문이 알아보나 — 안 그러면 매번 `git diff` 를 손으로 열게 된다.
# **문구가 아니라 동작을 본다** (2026-09-03) — 예전에는 `inspect.getsource` 로 함수 안에
# `"**변경추적**"` 이라는 글자가 있나만 봤는데, `meta_changed` 가 다섯 줄짜리 목록에서
# 메타 블록 전체를 견주는 쪽으로 바뀌면서 그 글자 자체가 소스에서 사라졌다. 아래
# 「meta_changed — 메타 블록 전체」 절이 실제 동작(목록에 없던 칸도 잡히나)을 잰다.

# ── 봇 색인 문장 (brief_lines) — 모델 문장이 사람 눈을 거치나 ─────────────
# `주요 항목` 과 회차 요약 표 칸은 봇 프롬프트·답변 근거에 그대로 실리는 모델 문장인데
# 어느 축에도 안 실려 사람이 안 읽은 채 커밋됐다 (2026-09-04). 순수 함수라 git 없이 잰다.

_doc_v1 = [
    "# 사업장나 잔금수금 업무보고",
    "",
    "> **사업장**: 사업장나 · **종류**: 시리즈",
    "> **열람**: 공개",
    "> **주요 항목**: 잔금수금 누계 호실·금액·비율",
    "",
    "---",
    "",
    "## 회차 요약",
    "",
    "비율이 두 가지다 — 호실 비율과 금액 비율.",
    "",
    "| 작성일 | 원본 파일 | 수금 누계 (호실) |",
    "|---|---|---|",
    "| 2026-09-02 | 260902_보고.pdf | 347실 (27.0%) |",
    "",
    "---",
    "",
    "## 2026-09",
    "",
    "**2026-09-02 · 260902_보고.pdf**",
    "",
    "본문",
]

# 새 파일 — 전부 이번에 쓴 문장이므로 전부 나온다. 구조 줄(제목·메타·구분선)은 안 나온다.
_b = R.brief_lines(_doc_v1)
_texts = [t for _, t in _b]
check("새 파일이면 주요 항목이 전문으로 나온다",
      ("주요 항목", "잔금수금 누계 호실·금액·비율") in _b, repr(_b))
check("새 파일이면 회차 요약의 표 행이 나온다",
      any("347실 (27.0%)" in t for t in _texts), repr(_texts))
check("회차 요약의 산문도 나온다 (모델이 쓴 문장이다)",
      any("비율이 두 가지다" in t for t in _texts))
check("표 구분선·제목·메타 줄은 안 나온다",
      not any(t == "|---|---|" or t.startswith("#") or t.startswith(">") for t in _texts),
      repr(_texts))
check("본문(첫 회차 헤더 뒤)은 이 절의 대상이 아니다 — 본문 축이 따로 본다",
      not any(t == "본문" for t in _texts))

# 기존 파일에 회차 한 줄 추가 — **바뀐 줄만** 나온다. 옛 행이 다시 나오면 표가 붇는다.
_doc_v2 = list(_doc_v1)
_doc_v2.insert(14, "| 2026-09-03 | 260903_보고.pdf | 348실 (27.1%) |")
_b2 = R.brief_lines(_doc_v2, head_lines=_doc_v1)
check("바뀐 줄만 낸다 — 새 표 행 1건",
      len(_b2) == 1 and "348실 (27.1%)" in _b2[0][1], repr(_b2))

# 표 안 값 하나만 고침 (재변환·손수정) — 그 행의 새 판이 걸린다.
_doc_v3 = [l.replace("347실 (27.0%)", "346실 (26.9%)") for l in _doc_v1]
_b3 = R.brief_lines(_doc_v3, head_lines=_doc_v1)
check("표 칸 하나만 고쳐도 그 행이 걸린다",
      len(_b3) == 1 and "346실 (26.9%)" in _b3[0][1], repr(_b3))

# 주요 항목만 고침 — 새 판과 전 판이 함께 나온다 (무엇에서 무엇으로 갔는지가 대조 재료다).
_doc_v4 = [l.replace("호실·금액·비율", "호실·금액·비율 · 납부 접수현황") for l in _doc_v1]
_b4 = R.brief_lines(_doc_v4, head_lines=_doc_v1)
check("주요 항목이 바뀌면 새 판과 전 판이 함께 나온다",
      any(lb == "주요 항목" and "납부 접수현황" in t for lb, t in _b4)
      and any(lb == "주요 항목 (전 판)" for lb, _ in _b4), repr(_b4))

# 모델 문장이 안 바뀐 변경(열람 등 메타·본문)은 이 절에 안 나온다 — 다른 축의 자리다.
_doc_v5 = [l.replace("**열람**: 공개", "**열람**: 비공개") for l in _doc_v1]
check("모델 문장이 안 바뀌면 아무것도 안 낸다 (열람 변경은 열람 축이 본다)",
      R.brief_lines(_doc_v5, head_lines=_doc_v1) == [],
      repr(R.brief_lines(_doc_v5, head_lines=_doc_v1)))

print("\n[뭉침 축 — 여덟째 축 (2026-09-21)]")

# 유산 뭉침이 있는 시리즈 md 를 흉내낸다. 옛 회차에 뭉친 칸이 있고, 새 회차는 깨끗하다.
_MG_LINES = (
    "# 문서\n"
    "> **열람**: 공개\n"
    "---\n"
    "**2026-09-20 · 새것.pdf**\n"                      # 4행 — 새 회차 시작
    "<tr><td>깨끗</td><td>13,473</td></tr>\n"          # 5행
    "**2026-08-01 · 옛것.pdf**\n"                      # 6행 — 옛 회차
    "<tr><td>유산</td><td>2868,373</td></tr>\n"        # 7행 — 유산 뭉침
).split("\n")

_new_span = R.entry_span(_MG_LINES, "2026-09-20", "새것.pdf")
_old_span = R.entry_span(_MG_LINES, "2026-08-01", "옛것.pdf")

# ① 이번에 들어온 줄이 새 회차(4·5행)뿐이면 옛 회차의 유산은 **안 울린다**
_cell, _why = R.merged_cell(_MG_LINES, _old_span, {4, 5})
check("유산이 있는 옛 회차는 이번에 안 건드렸으면 ⚠ 가 안 뜬다", _cell == "—", f"{_cell!r} {_why!r}")
_cell, _why = R.merged_cell(_MG_LINES, _new_span, {4, 5})
check("새로 들어온 깨끗한 회차는 ✓", _cell == "✓", f"{_cell!r} {_why!r}")

# ② 같은 파일에 뭉친 블록을 넣으면 뜬다 (7행이 이번에 들어온 줄일 때)
_cell, _why = R.merged_cell(_MG_LINES, _old_span, {6, 7})
check("이번에 들어온 줄에 뭉침이 있으면 ⚠", _cell.endswith("⚠"), f"{_cell!r}")
check("사유에 걸린 값이 보인다", "2868,373" in (_why or ""), repr(_why))

# ③ 새 파일(=전부 새것)이면 범위 한정이 없다 — 유산도 이번에 들어온 것이다
_cell, _why = R.merged_cell(_MG_LINES, _old_span, None)
check("새 파일이면 전부 본다", _cell.endswith("⚠"), f"{_cell!r}")

# ④ 갈래 ① — 실사고 꼴. ② 전용 잣대로는 안 걸린다
_LED = "**2026-09-01 · 분석.pdf**\n<tr><td>본PF</td><td>1,000900</td></tr>\n".split("\n")
_cell, _why = R.merged_cell(_LED, R.entry_span(_LED, "2026-09-01", "분석.pdf"), None)
check("원장 쉼표형(1,000900)도 잡는다 — 실사고가 이 꼴이었다",
      _cell.endswith("⚠") and "1,000900" in (_why or ""), f"{_cell!r} {_why!r}")

# ⑤ 그래프 눈금 사다리는 **말만 덧붙이고 ⚠ 는 그대로 둔다**
_AX = "**2026-09-01 · 전망.pdf**\n<tr><td>10,0005,0000</td></tr>\n".split("\n")
_cell, _why = R.merged_cell(_AX, R.entry_span(_AX, "2026-09-01", "전망.pdf"), None)
check("눈금 사다리도 ⚠ 다", _cell.endswith("⚠"), f"{_cell!r}")
check("눈금일 수 있다는 말이 덧붙는다", "눈금" in (_why or ""), repr(_why))

# ⑥ 여러 줄에 걸친 칸 — 가운데 줄 하나만 고쳐도 그 칸이 걸려야 한다.
#    **이 자리가 실제로 뚫려 있었다** (2026-09-21 회의적 검증이 재현). 추가된 줄만
#    이어 붙여 검사하면 `<td>` 여는 태그가 그 줄에 없어 정규식이 0건을 내고, 칸이
#    `—` 가 아니라 **`✓`** 로 찍혔다 — 「검사했고 깨끗하다」는 거짓 단언이다.
_MULTI = (
    "**2026-09-01 · 계약.pdf**\n"      # 1행
    "<table><tr><td>\n"                # 2행 — 칸 시작
    "잔금\n"                            # 3행
    "271,000,0000\n"                   # 4행 — 뭉친 값이 칸 가운데 줄에 있다
    "</td></tr></table>\n"             # 5행 — 칸 끝
).split("\n")
_span = R.entry_span(_MULTI, "2026-09-01", "계약.pdf")
_cell, _why = R.merged_cell(_MULTI, _span, {4})
check("여러 줄에 걸친 칸도 가운데 줄만 고치면 걸린다", _cell.endswith("⚠"), f"{_cell!r} {_why!r}")
_cell, _why = R.merged_cell(_MULTI, _span, {1})
check("그 칸을 안 건드린 회차에서는 안 울린다 (「—」 = 볼 것이 없었다)",
      _cell == "—", f"{_cell!r}")

# ⑦ 그래프 눈금 꼴이어도 **⚠ 를 안 내린다** — 진짜 계약 금액이 이 꼴에 걸린다
_REAL = "**2026-09-01 · 계약.pdf**\n<tr><td>262,500,000268,0000</td></tr>\n".split("\n")
_cell, _why = R.merged_cell(_REAL, R.entry_span(_REAL, "2026-09-01", "계약.pdf"), None)
check("눈금 꼴로 보여도 ⚠ 는 그대로다 (매매계약서 실물)", _cell.endswith("⚠"), f"{_cell!r}")
check("눈금일 수 있다는 말은 덧붙는다", "눈금" in (_why or ""), repr(_why))

# ⑧ `added_lines` 자체를 실물 git 저장소에서 잰다.
#    **이 함수에 시험이 없어서, 죽여 놔도 묶음이 파랬다** (사보타주로 실증됨).
#    범위가 좁게 틀리면 뭉침이 조용히 안 걸린다 — 이 축을 만든 이유와 정반대다.
print("\nadded_lines — 실물 git 저장소로 잰다")
import subprocess as _sp  # noqa: E402

with tempfile.TemporaryDirectory() as _d:
    _repo = Path(_d) / "repo"
    (_repo / "documents" / "projects" / "사업장").mkdir(parents=True)
    _md = _repo / "documents" / "projects" / "사업장" / "문서.md"

    def _git(*a):
        return _sp.run(["git", *a], cwd=_repo, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")

    _git("init", "-q")
    _git("config", "user.email", "t@t")
    _git("config", "user.name", "t")
    _md.write_text("# 문서\n\n**2026-08-01 · 옛것.pdf**\n<td>2868,373</td>\n",
                   encoding="utf-8", newline="\n")
    _git("add", "-A")
    _git("commit", "-qm", "first")

    _saved_root = R.ROOT
    try:
        R.ROOT = _repo
        check("안 고쳤으면 추가된 줄이 없다", R.added_lines(_md, False) == set(),
              repr(R.added_lines(_md, False)))
        check("새 파일이면 None(=전부 새것)", R.added_lines(_md, True) is None)

        _md.write_text("# 문서\n\n**2026-09-01 · 새것.pdf**\n<td>1,000900</td>\n\n"
                       "**2026-08-01 · 옛것.pdf**\n<td>2868,373</td>\n",
                       encoding="utf-8", newline="\n")
        _added = R.added_lines(_md, False)
        check("새로 넣은 줄만 잡는다", _added == {3, 4, 5}, repr(_added))
        check("옛 회차의 유산 줄(7행)은 안 들어 있다", 7 not in _added, repr(_added))
        # 그 유산 줄이 범위 밖이라는 것이 곧 「유산이 안 울린다」의 근거다
        _lines = _md.read_text(encoding="utf-8").split("\n")
        _oldspan = R.entry_span(_lines, "2026-08-01", "옛것.pdf")
        check("그래서 옛 회차는 안 울린다", not R.merged_cell(_lines, _oldspan, _added)[0].endswith("⚠"),
              R.merged_cell(_lines, _oldspan, _added))
        _newspan = R.entry_span(_lines, "2026-09-01", "새것.pdf")
        check("새 회차의 뭉침은 ⚠", R.merged_cell(_lines, _newspan, _added)[0].endswith("⚠"),
              R.merged_cell(_lines, _newspan, _added))

        # `git add` 뒤에도 같아야 한다 — 관문은 스테이징 뒤에 도는 일이 흔하다
        _git("add", "-A")
        check("git add 뒤에도 같은 줄을 잡는다", R.added_lines(_md, False) == _added,
              repr(R.added_lines(_md, False)))
    finally:
        R.ROOT = _saved_root

# ⑨ 잣대가 한 벌인가 — 정규식을 베끼면 조용히 갈린다
check("점검표와 잣대가 같은 모듈을 쓴다", R.merged.MIN_DIGITS == 6, R.merged.__file__)

print()
print("전부 통과" if _ok else "실패 있음")
sys.exit(0 if _ok else 1)
