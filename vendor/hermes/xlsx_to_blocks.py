#!/usr/bin/env python3
"""
엑셀 → 시트별 회차 블록 md.

  python xlsx_to_blocks.py --file <xlsx> --date 2026-08-05 [--out-dir <dir>] [--max-rows 1000]

`--out-dir` 을 생략하면 `<--file 경로>.blocks` 에 만든다.

왜 시트마다 블록인가: documents.js 의 clip 은 검색 히트를 블록 **앞에서부터** 자른다.
파일 하나를 블록 하나로 담으면 3번째 시트의 값은 4,000자 안에 안 들어와, 봇이
"검색에 걸렸다"고 알면서 값을 못 본다. 헤더는 archive.js 의 splitMessages 정규식
`^\\*\\*(\\d{4}-\\d{2}-\\d{2})[^*]*\\*\\*\\s*$` 에 맞춰 두었다 — 날짜 뒤는 자유 문자열이라
파서를 안 고치고 시트가 블록이 된다.
"""
import argparse
import collections
import datetime
import decimal
import html
import io
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# 화면 출력을 UTF-8 로 못박는다 (`insert_entry.py:34-38` 과 같은 이유·같은 방식).
# 윈도우 파이썬은 stdout 이 **파이프면** 로캘 인코딩(cp949)으로 쓴다. 직접 찍을 때는
# 멀쩡해서 안 드러나고, 받아 적는 순간에만 깨진다. 그러면 이 스크립트를
# `subprocess.run(..., encoding="utf-8")` 로 부르는 쪽에서 읽기 스레드가 죽고
# `r.stdout` 이 **None** 이 되어, 변환이 실패한 날 사유를 찍으려던 코드가 사유 대신
# AttributeError 로 터진다 — 정보가 가장 필요한 순간에 정보가 사라진다.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

try:
    import openpyxl
except ImportError:
    # 종료코드 2 = **못 쟀음** (1 = 실패와 가른다). `check_against_libreoffice.py` 가
    # LibreOffice 에 쓰는 것과 같은 규약이다.
    #
    # 1 을 내면 안 되는 이유: openpyxl 은 **VM 에 일부러 안 깐다**(`deploy/setup.sh` 의
    # 「3/9 기본 패키지」). 그런데 이 모듈을 끌어오는 시험 넷이 없을 때도 1 을 내서,
    # `npm run check` 가 「안 깔기로 한 것이 없다」를 「시험이 깨졌다」와 같은 ✗ 로 냈고
    # VM 의 점검은 **영구히 빨강**이었다 (2026-09-06 VM 실측). 영구히 빨간 검사는
    # 진짜 고장이 나도 화면이 그대로라 구별되지 않는다.
    # 이 규약을 지키는 검사는 `scripts/check-optional-dep-signal.js` 다.
    print(
        "openpyxl 이 없습니다. pip install -r "
        ".claude/skills/doc-archive/scripts/requirements.txt",
        file=sys.stderr,
    )
    sys.exit(2)


class EmptyFormulaError(RuntimeError):
    """수식 셀인데 엑셀이 캐시한 값이 없다 — 조용히 빈칸으로 두면 봇이 '값이 없다'고 답한다."""


HEADER_FMT = "**{date} · {source} — 시트 {i}/{n}: {name}**"

# 개인정보 열 판정 — 값 모양으로는 이름·주소를 못 찾으므로 **열 머리글**로 정한다.
# 부분 일치다: `주민등록 주소1` 은 `주소` 로 걸린다.
# **부분 문자열로 본다.** 그래서 글자가 조금만 달라도 안 걸린다 — `주민번호` 는
# `주민등록번호` 를 못 잡는다(둘은 서로의 부분 문자열이 아니다). 2026-08-25 에
# 사업장라 가압류신청 시트가 그것 때문에 적중 1개로 문턱(2)을 못 넘어 통째로 빠졌는데,
# **막힌 것이 운이었다** — 낱말 하나만 더 걸렸으면 시트가 실렸고, 그때는 `대상자`
# 가 아직 어느 목록에도 없어 실명이 그대로 나갔을 것이다(그 낱말은 지금
# `NAME_COLUMN_WORDS` 로 옮겨져 있고, 사람 이름은 이제 이 목록과 무관하게
# 안 가린다 — WHK 결정 2026-08-26). 변형을 볼 때마다 여기 더한다.
#
# **주소는 「주소」라고만 적히지 않는다** (2026-09-03). `거주지`·`실거주지` 는
# `주소` 의 부분 문자열이 아니라 열이 통째로 안 가려졌다. 이건 판정 방식 문제가
# 아니라 **다른 이름**이라 낱말을 더하는 것 말고 길이 없다 — 어떤 정규식도
# 「주소의 동의어」를 지어낼 수 없다. `실거주지` 는 `거주지` 가 부분 일치로 잡는다.
# **`주거지` 는 넣지 않는다** — `제2종일반주거지역` 같은 용도지역 표기가 부분
# 일치로 걸린다 (2026-09-03 실측: 아카이브 문서 5건 이상에 `주거지역` 이 있다).
# 사람이 사는 곳을 뜻하는 것만 넣고, 땅의 쓰임을 뜻하는 말은 안 넣는다.
MASK_COLUMN_WORDS = (
    "핸드폰", "휴대폰", "연락처", "전화",
    "EMAIL", "이메일",
    "주소", "거주지", "송달장소",
    "주민번호", "주민등록번호", "생년월일", "계좌",
)
# **낱말 사이에 다른 말이 끼어드는 열.** 위 목록은 부분 문자열이라 이것을 못 잡는다 —
# `주민(사업자)번호` 는 `주민번호` 의 부분 문자열도 `주민등록번호` 의 부분 문자열도
# 아니어서 `header_kind` 가 None 을 준다. 2026-09-03 재현: 머리글이
# `성명 · 주민(사업자)번호 · 거주지 · 연락처` 인 표에서 **연락처만 `***` 가 되고**
# 구분자 없는 주민번호와 주소가 그대로 실렸다. 더 나쁜 것은 `연락처` 하나로 머리글
# 행이 **정상적으로 찾아져** `sheet_drop_reason` 안전망이 아예 안 돈다는 것이다.
#
# **낱말을 더 넣는 것으로는 안 닫힌다.** `주민(사업자)번호` 를 넣으면 `주민(법인)번호`
# 가, 그걸 넣으면 `주민등록·사업자번호` 가 또 뚫린다 — 끼어드는 말은 닫힌 목록이
# 아니라 **자리**이기 때문이다. 그래서 이 갈래만 「앞말 … 뒷말」 꼴로 본다.
#
# 사이를 8자로 막는다: 실물에서 본 것 중 가장 긴 것이 `등록·사업자`(6자)이고,
# 머리글 라벨 자체가 30자 미만(`HEADER_LABEL_MAX`)이라 무한정 벌릴 이유가 없다.
#
# **사이에 줄바꿈도 받는다**(`.` 이 아니라 `[\s\S]`). 엑셀에서 머리글을 두 줄로
# 접어 적으면 값에 `\n` 이 그대로 들어오는데, 그 표기가 이 아카이브에 실제로 있다
# (2026-09-03: `물건\n번호` · `입찰\n번호` · `z 번\n(획지번호)`). `.` 은 줄바꿈을
# 안 받아 `주민\n등록번호` 를 놓친다. 재 보니 줄바꿈까지 받아서 **새로 걸리는 칸은
# 0개**였다 (고유 칸 44,965개).
#
# **넓히는 쪽이 안전한 자리다** — 주민번호는 구분자가 없으면 `mask_inline` 이
# 못 잡아, 열 판정이 마지막 그물이다 (WHK 결정 2026-08-27). 애먼 열이 걸리면
# `NOT_PERSONAL_HEADERS` 로 한 줄 열고 그 사실이 `masked.not_personal` 에 남는다.
# 2026-09-03 실측: 아카이브 고유 머리글 2,645개에 새로 걸리는 것은 0개다.
MASK_COLUMN_PATTERNS = (
    ("주민번호", re.compile(r"주민[\s\S]{0,8}번호")),
)
# 시트를 통째로 뺄 이유가 되는 낱말. **`MASK_COLUMN_WORDS` 의 부분집합이다** —
# 열 마스킹은 여전히 열세 개 전부로 한다. 바뀌는 것은 「머리글을 못 찾았을 때
# 시트를 버릴 이유가 되나」 하나뿐이다.
#
# 나머지 여섯(핸드폰·휴대폰·연락처·전화·EMAIL·이메일)은 **열 머리글이 없어도
# `mask_inline` 이 모양으로 잡는다.** 그래서 시트를 버릴 이유가 못 된다 — 버려도
# 얻는 안전이 없고, 실물에서는 상담 메모 문장 하나가 95행짜리 표를 통째로
# 버리고 있었다 (2026-08-26 측정 2-1·2-2절).
#
# 남은 일곱은 인라인이 못 잡거나 반만 잡는다: 주소는 모양이 없고(`거주지`·`송달장소`
# 도 같은 이유로 여기 든다 — 2026-09-03), 생년월일은 다른 날짜와 구별이 안 되고,
# 계좌는 **낱말이 같은 칸에 있을 때만** 잡힌다
# (비공개나 급여계좌에서 계좌번호 6개 중 0개가 걸리는 것이 그 실측이다).
#
# **주민번호는 구분자가 있을 때만 잡힌다** (WHK 결정 2026-08-27). 정규식이
# 구분자가 있는 형태는 잡지만 구분자 없이 이어진 형태는 잡지 않는다.
# 못 잡는데, 엑셀에 구분자 없이 적는 것이 흔하다. 인라인만 믿으면 「표가 10행
# 밖에서 시작해 머리글을 못 찾고, 머리글이 `성명 · 주민등록번호 · 연락처` 라
# 주소·계좌가 없는」 시트가 마스킹 없이 실린다. 좁은 경로지만 이 목록에서
# 가장 민감한 항목이라 안전 쪽을 골랐다. **구분자 없는 유선전화도 같은 모양의
# 구멍이지만 그것은 지금도 아카이브 전체에서 안 잡히는 것이라** 여기서 달라지는
# 것이 없어 넣지 않았다.
DROP_TRIGGER_WORDS = (
    "주소", "생년월일", "계좌", "주민번호", "주민등록번호",
)
# 주소를 뜻하는 **다른 이름**들. 이 둘만 **10자 이하 칸일 때** 빼는 이유가 된다
# (WHK 결정 2026-09-03). 다른 낱말과 달리 길이를 재는 이유는 실물로 갈렸기 때문이다 —
# 아카이브 전체에서 `거주지` 가 든 머리글 칸은 **0종**이고, 걸리는 것은 상담 메모·제출서류
# 목록 **3종(20·25·28자)** 뿐이다. 반면 진짜 주소 머리글은 전부 짧다
# (`주소` 2 · `도로명주소` 5 · `물건지 주소` 6 · `도로명상세주소` 7). 그래서 10자에서
# 자르면 둘이 여유 있게 갈린다. `is_label_cell` 의 30자 문턱만으로는 저 메모들이
# 통과해 **표 하나를 통째로 버린다** — 2026-08-26 에 실제로 났던 사고와 같은 모양이다.
# **열 마스킹에서는 빼지 않았다** — 거기서는 길이와 무관하게 `거주지` 열을 가린다.
DROP_TRIGGER_SHORT_ONLY = ("거주지", "송달장소")
DROP_TRIGGER_SHORT_MAX = 10
# 끼어드는 말이 있는 주민번호도 같은 이유로 시트를 뺀다. **`MASK_COLUMN_PATTERNS`
# 를 그대로 가리킨다** — 두 벌로 적으면 한쪽만 늘어나도 에러가 안 나고, 열은
# 가리는데 시트는 안 빠지는(또는 그 반대) 상태가 조용히 생긴다.
DROP_TRIGGER_PATTERNS = MASK_COLUMN_PATTERNS
# 계약자 이름 열. **가리지 않는다** (WHK 결정 2026-08-26) — 슬랙에서 이미 실명으로
# 논의 중이고, 글 문서(pdf·워드)에는 애초에 실명이 그대로 실려 있어 엑셀만 가리는
# 것이 어긋나 있었다. 열 머리글로는 계약자인지 직원인지 회사 대표인지 못 가르므로
# 사람 이름 열을 통째로 연다.
#
# **`MASK_COLUMN_WORDS` 에서 그냥 지우면 안 된다.** `find_header_row` 가 걸리는
# 낱말 2개로 머리글 행을 찾는데, 지우면 `대표자명 … 담당` 으로 겨우 문턱을 넘던
# 시트가 1개로 떨어져 머리글을 못 찾고 **시트가 통째로 안 실린다**(unmaskable).
# 그래서 `header_kind` 가 'keep' 을 주도록 여기로 옮긴다 — 세어지되 안 가려진다.
NAME_COLUMN_WORDS = ("대표자명", "성명", "이름", "계약자명", "대상자")
# 직원 열. **가릴지 말지를 정하는 데는 안 쓴다** — 머리글 행을 찾을 때 세기만 한다.
# 이름 낱말은 이제 열려 있어 `팀장 성명` 같은 충돌은 없다 — 사람 이름은
# 무엇이 걸리든 이미 안 가린다 (WHK 결정 2026-08-26).
KEEP_COLUMN_WORDS = ("담당", "관리담당", "팀")
# 가릴 낱말이 들었지만 사람의 것이 아닌 열. **머리글 전체가 정확히 일치할 때만** 걸린다 —
# 부분 일치가 아니다. `KEEP_COLUMN_WORDS` 를 제외에 쓰지 않는 이유(`팀` 이 `팀장 성명` 을
# 삼킨다)가 여기에는 없다. `주민등록 주소1`·`우편물주소1` 은 그대로 가려진다.
# `물건지 주소` = 감정평가 대상 부동산의 소재지이지 사람이 사는 곳이 아니다 (WHK 결정 2026-08-25).
# `신탁계좌가압류` = 「신탁계좌에 가압류를 걸었나」 여부(ㅇ/빈칸)이지 계좌번호가 아니다
# (2026-08-26 사업장파 원본 확인 — 148행 중 79행에 값이 있고 전부 'ㅇ' 하나뿐이며,
# 같은 모양의 법적조치 여부 열 `신협부동산가압류` 바로 옆에 있다). `계좌` 가 부분 일치로
# 걸려 118행이 통째로 가려져 있었다.
# `계좌잔액` = 그 계좌에 얼마가 들어 있나(금액)이지 계좌번호가 아니다 (WHK 결정 2026-08-28 —
# 사업장가 요약본 `비교` 시트 원본 확인. C열이 `계좌잔액 · 미납잔금 · 재판매수입금 · 상가수입금 ·
# 소계 · 중도금 · 공사비 · 재판매비 · 제세공과금` 인 **항목 이름 열**인데 맨 위 칸이 걸려
# 항목 이름 23칸이 `***` 가 됐다. 금액은 옆 열이라 남고 이름만 사라져, 어느 돈인지 알 수
# 없는 표가 됐다).
# **여기 한 줄을 더하는 것은 그 이름의 열을 앞으로 전부 여는 일이다.** 무엇을 열었는지는
# meta.json 의 `masked.not_personal` 과 7.5 점검표에 남아 조용히 열리지 않는다.
NOT_PERSONAL_HEADERS = ("물건지 주소", "신탁계좌가압류", "계좌잔액")
MASK_TEXT = "***"
HEADER_SCAN_ROWS = 10
MIN_HEADER_HITS = 2

# 머리글 라벨과 서술 문장을 가르는 길이. **실물이 이미 갈라 놓은 자리다** —
# 2026-08-27 에 아카이브에 실린 엑셀에서 주소·생년월일·계좌 낱말이 든 칸 44개를
# 재 보니 라벨은 최장 15자, 문장은 최단 37자(`주) 출자전환 주주 중 투자설명서 교부
# 원하는 고객에게 주소로 발송`)였고 **16~36자는 한 칸도 없었다.** 그 44개에는 안
# 들어가지만 실제 머리글로 확인된 것 중 가장 긴 것이 25자(사업장가 (축약) 반응도
# 4행의 `내용증명 발송 이후 계약자 반응도(6월 이후)`)라, 그것보다 넉넉한 30을 잡았다.
#
# **좁히려면 실물을 다시 재고 좁힌다.** 근거 없이 좁혔다가 실물을 놓친 자리가
# 이 파일에 이미 둘 있다(등록번호 뒷자리 `[1-8]` · 유선전화 `50[57]`).
# 넓히는 쪽은 안전하다 — 라벨이 아닌 것을 라벨로 보면 시트가 실리는 쪽이 아니라
# 2차 머리글 후보가 늘어나는 쪽이고, 그 뒤에 「값 3칸」 문턱이 한 번 더 있다.
HEADER_LABEL_MAX = 30

# 2차 머리글이 되려면 그 행에 값 있는 칸이 몇 개 있어야 하나.
# **이 문턱이 「급여계좌」를 지킨다** — 비공개나 8월5일 시트는 2행이 제목
# `□ 급여계좌`(값 1칸)이고 3행이 진짜 머리글 `구분·계좌번호·잔액·비고` 다.
# 문턱이 없으면 2행을 머리글로 잡아 은행명 열을 가리고 **계좌번호 6개가 그대로
# 나간다**(2026-08-26 측정 4절에서 실제로 돌려 확인한 경로다).
MIN_HEADER_CELLS_FALLBACK = 3

# 셀 안에서 모양으로 잡히는 개인정보. **여기가 유일한 원본이다** —
# `review_batch.py` 와 `screen_personal.py` 가 이 목록을 가져다 쓴다.
# 두 벌로 적어 두면 한쪽만 고쳐져도 에러가 안 나고, 변환기는 가리는데 점검표는
# ⚠ 를 내거나 그 반대가 된다 (2026-08-26 에 한 벌로 합쳤다).
#
# 앞뒤에 숫자가 붙어 있으면 개인정보가 아니다 — 스캔본의 금액 나열·차트 축 눈금·
# 등기부 발행번호 한가운데서 휴대폰 모양이 걸린다 (2026-08-15 전수: 휴대폰 ⚠
# 5건이 전부 이것이었다). 경계에 `.` 과 `-` 는 넣지 않는다 — 아카이브에
# `M.010-…` 표기가 실제로 있어서 진짜 휴대폰을 놓친다.
INLINE_PATTERNS = (
    # 셋을 갈라 봐야 셋 다 가리므로 한 패턴으로 둔다 (WHK 결정 2026-08-26).
    #
    # **뒷자리 첫 글자를 묶지 않는다.** 주민등록번호는 그 자리가 성별·국적 코드
    # (1~4 내국인 · 5~8 외국인)라 닫힌 목록이지만, **법인등록번호의 같은 자리는
    # 일련번호**라 0~9 아무 값이나 온다. 처음엔 `[1-8]` 로 적어 뒀다가 아카이브에서
    # 4건을 놓쳤다 (2026-08-26: 병정보 `110111-0111111` 2회 · 어느 사업장의
    # 공영개발 법인 `171211-0222222` 2회). 뒤엣것은 **같은 등기부 표에서 옆 줄의
    # 구분자가 있는 값만 잡히면 같은 표가 반쯤만 가려진다.
    # 근거는 같은 등기소(`110111`) 아래 법인 7곳의 그 자리가 {0,3,4,7,7,8,8}로
    # 흩어져 있다는 것이다 — 코드라면 같아야 한다.
    #
    # 넓혀도 오탐이 안 는다: 자리 수가 **정확히 6+7 이고 구분자가 있어야** 하며
    # 앞뒤 경계가 숫자를 막는다. 2026-08-26 전수에서 대화 아카이브에는 이 모양이
    # 한 건도 없고, 문서 아카이브 10건은 전부 진짜 등록번호였다.
    ("주민·법인·외국인등록번호",
     re.compile(r"(?<!\d)\d{6}\s*[-–]\s*\d{7}(?!\d)")),
    ("휴대폰",
     re.compile(r"(?<!\d)01[016-9][-–.\s]?\d{3,4}[-–.\s]?\d{4}(?!\d)")),
    # **유선전화만 구분자를 필수로 한다.** 휴대폰은 `010` 이라는 강한 머리가 있어
    # 구분자 없이도 안전하지만, 유선은 `02`·`031` 이라 구분자를 빼면
    # `0212345678` 같은 10자리 금액·번호가 전부 걸린다.
    # 지역번호는 **닫힌 목록**이다 — 02 · 031~033 · 041~044 · 051~055 ·
    # 061~064 · 070(인터넷전화) · 0500~0509(개인번호 서비스) · 080. 새 번호대가
    # 생기면 여기 더한다. 회사 대표번호도 함께 가려진다 (WHK 결정 2026-08-26).
    #
    # **050 은 넷째 자리를 묶지 않는다** (2026-08-26 에 `50[57]` 에서 넓혔다).
    # 050 은 국번 열 개가 통째로 개인번호 서비스라 0505·0507 만 적을 근거가 없었다.
    # 실물 신탁계약서의 `0502` 팩스번호를 그래서 놓쳤고, 계좌 판정이 그것을 계좌로
    # 잡아 **엉뚱한 라벨로 점검표에 떴다** (그 번호는 자료 저장소 `사고기록.md` 의
    # 「`050` 국번을 통째로 묶은 이유」 절). 목록 자체는 닫아 둔다 —
    # 그게 `0212345678` 같은 금액·번호를 막는 안전장치다.
    #
    # **050 은 세 자리로도 쓴다** (2026-09-03 에 `50\d` 에서 `50\d?` 로 넓혔다).
    # `50\d` 는 050 을 네 자리(`0505`·`0507`)로만 봐서 `(050)0000-0000` 도
    # `050-0000-0000` 도 어느 갈래에도 안 맞았다. 넷째 자리는 국번이지 번호대가
    # 아니라 세 자리 표기가 실물에 그대로 있다.
    #
    # **지역번호 뒤 구분자에 `)` 를 넣는다** (2026-09-03). 구분자 집합이
    # `[-–.\s]` 뿐이라 지역번호를 괄호로 닫는 표기를 **한 건도 못 잡았다** —
    # 공문·감정평가서의 담당자 줄이 `담당 : ○○○ 차장 ☎ (02)000-0000` 모양이라
    # 이름과 직통번호가 함께 아카이브에 실렸다 (2026-09-03 전수: 41건 / 22개 문서).
    # 여는 괄호가 없는 반쪽 표기(`02)000-0000`)와 `)` 뒤에 공백이 오는 것도 실물에
    # 있어 셋을 함께 받는다. 여는 괄호는 `\(?` 로 **매치에 포함해** 가린다 —
    # 빼 두면 `(***` 처럼 괄호 한 짝이 남는다.
    #
    # **`)` 앞의 공백도 함께 받는다** — pdf 의 칸 채움(`(02  )000-0000`)이 md 로
    # 그대로 넘어온다. 어느 사업장의 채권압류통지서에서는 그 자리가 **빈 줄**이라 문단이
    # 갈라져 있었고, 같은 번호가 같은 문서 안에서 한 번은 가려지고 한 번은 안
    # 가려질 뻔했다 (2026-09-03 실물 대조에서 나온 42번째 건). 줄바꿈을 넘는 것은
    # 새 위험이 아니다 — `[-–.\s]` 의 `\s` 가 이미 줄바꿈을 받고 있었다.
    #
    # **넓힌 것은 구분자 한 종류뿐이고 지역번호 목록과 「구분자 필수」는 그대로다.**
    # 둘째 구분자도 여전히 필수라 `(02)00000000` 은 안 걸린다.
    ("유선전화",
     re.compile(r"(?<!\d)\(?0(?:2|3[1-3]|4[1-4]|5[1-5]|6[1-4]|70|50\d?|80)"
                r"(?:[-–.\s]|\s*\)\s*)\d{3,4}[-–.\s]\d{4}(?!\d)")),
    # **꼬리 토막 하나를 선택으로 받는다** (2026-09-03 에 3토막 고정에서 넓혔다).
    # `ACCOUNT_NUM` 이 겪은 것과 **같은 갈래의 별개 결함**이었다 — 뒤 경계 `(?!\d)`
    # 는 다음 글자가 `-` 면 통과하므로, 이 패턴이 더 긴 번호의 앞 세 토막만 먹고
    # 꼬리를 남겼다. `mask_inline("301-12-34567-8")` 이 `"***-8"` 을 냈다.
    # **그 잔여가 자료 저장소에 실제로 들어가 있었다** — 어느 자금운영보고 md 의 표
    # 한 칸이 `<td>***-8</td>` 이었고 그 값은 은행 계좌다(2026-09-03 에 `***` 로 닫았다).
    # 그 칸에는 계좌 낱말이 없어 `ACCOUNT_WORD` 게이트가 안 열리므로
    # **계좌 정규식을 아무리 고쳐도 이 자리는 안 막힌다.**
    #
    # **넓히기만 하고 좁히지 않는다.** 선택 그룹이라 매치 **시작 위치는 하나도
    # 안 는다** — 자료 저장소 md 483개·줄 130,341 에 고치기 전 것과 나란히 돌려
    # 재 보면 새 시작 위치 0개, 가리던 구간을 전부 포함(상위집합), 구간이 늘어난
    # 줄 1개(그 실물 하나)다. 헛걸림 0건.
    #
    # **뒤 경계를 `(?![\d-–])` 로 막는 처방은 쓰면 안 된다.** 하이픈이 붙는 순간
    # 통째로 **안 가려져서** `123-45-67890-1` 이 원문 그대로 남는다 — 반쯤 가리던
    # 것보다 더 새는 쪽이라 마스킹을 느슨하게 만드는 것이다 (2026-09-03 실측 확인).
    ("사업자등록번호",
     re.compile(r"(?<!\d)\d{3}-\d{2}-\d{5}(?:[-–]\d{1,8})?(?!\d)")),
    ("이메일",
     re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
)
# 정규식만 뽑은 것. **이름을 바꾸지 않는다** — screen_personal.py 가 이 이름으로 쓴다.
_INLINE_MASK = tuple(p for _, p in INLINE_PATTERNS)

# 계좌는 숫자 모양만으로는 날짜와 구별이 안 된다 (`2026-08-26` 이 그대로 걸린다).
# 그래서 **낱말로 먼저 좁힌다.** 대가로 낱말 없이 덩그러니 적힌 계좌는 못 잡는다.
ACCOUNT_WORD = re.compile(r"계좌|예금주|입금계좌|가상계좌")
# **앞뒤에 숫자가 붙어 있으면 계좌번호가 아니다** (2026-08-26) — 위 다섯 패턴은 전부
# `(?<!\d)`·`(?!\d)` 경계가 있는데 이것만 없었다. 긴 숫자열 한가운데서 걸리면 앞뒤
# 진짜 숫자가 마스킹 결과에 그대로 남는다 — `mask_inline("계좌 1234567890-123-4567890123 확인")`
# 이 `"계좌 1234***23 확인"` 을 냈다(2026-08-26 재현, Task 1 리뷰에서 발견). 경계를
# 더하면 이 값은 어느 구간도 2~6·2~6·2~8 자리로 안 맞아 통째로 안 걸린다.
#
# **토막은 셋 또는 넷이다** (2026-09-03 에 셋 고정에서 넓혔다). 뒤 경계 `(?!\d)` 는
# 다음 글자가 `-` 면 통과하므로, 농협·기업식 네 토막 계좌는 앞 세 토막만 가려지고
# **마지막 토막이 그대로 남았다** — `mask_inline("입금계좌 301-1234-5678-90")` 이
# `"입금계좌 ***-90"` 을 냈다(2026-09-03 재현). 가려진 것처럼 보이는데 숫자가 남는
# 모양이라 눈으로도 안 걸린다.
#
# **구분자는 넓히지 않는다.** 2026-09-03 에 자료 저장소 md 483개를 **줄마다** 훑어
# (계좌 낱말이 그 줄에 있을 때만 세는, 이 함수와 같은 조건) 후보 넷을 재 봤다.
#
#   지금(`[-–]`·토막 3)            1건
#   구분자 그대로 · 토막 3~4        1건  ← 채택. 새로 걸린 자리 0
#   구분자 `[-–.\s]` · 토막 3       4건  ← 늘어난 3건 전부 헛걸림 (OCR 숫자표의
#                                        `500 00.000`·`00.00 0000` 같은 공백·소수점)
#   구분자 넓힘 · 토막 2~4        113건  ← 늘어난 112건 전부 헛걸림 (`40.1`·`88.5`
#                                        같은 소수와 `26.09` 같은 연월)
#
# 토막만 3~4 로 넓힌 지금은 **헛걸림이 한 건도 안 늘고**, 잘려서 잡히던 것이 통째로
# 잡힌다 — 파일 단위로 넓게 재면 그렇게 온전해진 값이 14건이다(실물
# `301-12-34567-8` 이 `301-12-34567` 로 잘리던 것이 그 예).
ACCOUNT_NUM = re.compile(
    r"(?<!\d)\d{2,6}[-–]\d{2,6}[-–]\d{2,8}(?:[-–]\d{1,8})?(?!\d)")
_DATE_SHAPE = re.compile(r"^(\d{2}|\d{4})[-–]\d{1,2}[-–]\d{1,2}$")


def looks_like_date(value):
    """`2026-08-26`·`26-8-26` 처럼 날짜로 읽히나. 계좌 낱말이 있는 줄의 날짜를 지키려는 것."""
    m = _DATE_SHAPE.match((value or "").strip())
    if not m:
        return False
    parts = re.split(r"[-–]", value.strip())
    return 1 <= int(parts[1]) <= 12 and 1 <= int(parts[2]) <= 31


def mask_inline(text):
    """가려지지 않은 셀·줄 안에서 모양으로 잡히는 것만 가린다. (가린 글자열, 건수)

    계좌는 **낱말이 같은 글자열 안에 있을 때만** 가리고, 날짜로 읽히는 것은 건너뛴다."""
    if not text:
        return text, 0
    n = 0
    for pat in _INLINE_MASK:
        text, k = pat.subn(MASK_TEXT, text)
        n += k
    if ACCOUNT_WORD.search(text):
        def _sub(m):
            nonlocal n
            if looks_like_date(m.group(0)):
                return m.group(0)
            n += 1
            return MASK_TEXT
        text = ACCOUNT_NUM.sub(_sub, text)
    return text, n


def column_word_hit(folded, words, patterns=()):
    """머리글 한 칸(**이미 casefold 된 글자**)에 걸리는 낱말·꼴이 있나.

    낱말은 부분 일치, 꼴은 정규식이다. **한 자리에서만 판정한다** — 낱말 목록과
    꼴 목록을 부르는 쪽마다 따로 훑으면 한 곳만 고쳐져도 에러가 안 나고,
    「가리는 열」과 「시트를 뺄 이유」가 조용히 갈린다."""
    if any(w.casefold() in folded for w in words):
        return True
    return any(p.search(folded) for _name, p in patterns)


def header_kind(text):
    """머리글 한 칸이 어느 목록에 걸리나. 'mask' / 'keep' / None"""
    if text is None:
        return None
    s = str(text).strip().casefold()
    if not s:
        return None
    # 예외가 먼저다. 'keep' 을 주므로 머리글 행을 찾을 때는 계속 세어진다 —
    # None 을 주면 이 열 하나 때문에 머리글 행을 못 찾아 시트가 통째로 빠질 수 있다.
    if any(s == w.casefold() for w in NOT_PERSONAL_HEADERS):
        return "keep"
    if column_word_hit(s, MASK_COLUMN_WORDS, MASK_COLUMN_PATTERNS):
        return "mask"
    if any(w.casefold() in s for w in KEEP_COLUMN_WORDS):
        return "keep"
    if any(w.casefold() in s for w in NAME_COLUMN_WORDS):
        return "keep"
    return None


def is_label_cell(value):
    """머리글 라벨처럼 짧은 칸인가. 서술 문장이면 False.

    상담 메모의 「전화주겠다고 함」·「중도금이자계좌 안됨」 같은 문장이 머리글로
    잡히거나 시트를 통째로 버리는 이유가 되지 않게 하려는 것이다."""
    if value is None:
        return False
    s = str(value).strip()
    return 0 < len(s) < HEADER_LABEL_MAX


def data_bounds(ws):
    """값이 있는 마지막 (행, 열). 값이 하나도 없으면 (0, 0).

    **`ws.max_row`·`ws.max_column` 을 쓰면 안 된다.** 그 둘은 *셀 객체가 있는*
    마지막 자리이지 *값이 있는* 마지막 자리가 아니다 — openpyxl 3.1.5 의
    `max_row` 는 `<dimension>` 태그를 안 읽고 `max(self._cells)[0]` 을 돌려준다.
    표 아래·오른쪽에 값도 서식도 없는 `<c/>` 가 **하나만** 있어도 openpyxl 이
    그 자리에 셀 객체를 만들고 max_row 가 거기로 뛴다.

    2026-08-26 실측: `<c r="A1048505"/>` 하나를 끼운 4,942바이트 파일에서
    `_cells` 는 7개인데 `max_row` 는 1,048,505 였다. 그 상태로
    `range(1, max_row+1)` 을 돌면 `ws.cell()` 이 셀 객체를 새로 만들어
    2,097,003개(껍데기만 218MB)가 생기고 4.8초가 든다 — 열이 2개뿐인
    파일에서다. 실측 두 건이 6.4GB 를 먹은 것이 이 기전이고, 그때는
    상한 판정에 걸려 **보증 포지션 전체를 담은 머리 표가 통째로 빠졌다**
    (에러도 경고도 안 났다).

    빈 문자열을 「값 없음」으로 보는 것은 이 파일의 다른 자리와 같은 기준이다
    (`build_blocks` 의 숨긴 행 판정·마스킹 행 판정). **7차에서 정한 「빈
    문자열도 계산된 값이다」와 부딪히지 않는다** — 그쪽은 「수식이 계산됐나」를
    가르고(`_missing_formula_cells`), 여기는 「표에 그릴 것이 있나」를 가른다.

    `_cells` 는 크지 않다 — 항목 수가 시트 XML 의 `<c>` 개수와 같아서 유령 행
    하나는 항목 하나다. 그래서 이 훑기는 실측 0.00초이고, 값을 물려 다니지
    않고 필요한 자리에서 그때그때 부른다 (물리면 `masked_columns` 처럼 지금
    혼자 시험되는 함수가 인자를 하나씩 더 받아야 한다).
    """
    last_row = last_col = 0
    for (r, c), cell in ws._cells.items():
        if cell.value not in (None, ""):
            if r > last_row:
                last_row = r
            if c > last_col:
                last_col = c
    return last_row, last_col


def find_header_row_detail(ws, scan=HEADER_SCAN_ROWS):
    """머리글 행과 「어떻게 잡았나」. (행|None, 'strict'|'fallback'|None)

    1차 — 걸리는 낱말이 MIN_HEADER_HITS 개 이상인 첫 행. 여기는 안 바뀐다.

    2차 — 1차가 실패했을 때만. 낱말이 1개인 행 중, **그 낱말이 라벨 칸에 있고**
    **값 있는 칸이 MIN_HEADER_CELLS_FALLBACK 개 이상**인 첫 행.

    2차가 필요한 이유: 머리글이 `구분·계좌번호·잔액·비고`(비공개나 급여계좌 3행)
    나 `동호수·타입·계약자명·…`(사업장가 요약본 5행)처럼 **걸리는 낱말이 하나뿐인
    표가 실물에 있다.** 문턱 2 만 두면 그런 시트는 머리글을 못 찾고, 그러면
    「가릴 방법이 없다」로 통째로 빠진다 — 사업장가 95행은 가릴 열이 0개인데
    빠졌고, 급여계좌는 가려야 할 계좌번호 열이 있는데 빠져서 **안전한 것이
    아니라 우연히 안 실린 상태**였다 (2026-08-26 측정).

    머리글 행이 시트마다 다르다 — 2026-08-23 실측으로 일보의 `호실별관리대장` 은 3행,
    `상황판` 은 4행, `상담(0710)` 은 2행이었다. 그래서 고정할 수 없다."""
    last_row, last_col = data_bounds(ws)
    last_row = min(scan, last_row)
    fallback = None
    for r in range(1, last_row + 1):
        hits = 0
        label_hits = 0
        filled = 0
        for c in range(1, last_col + 1):
            v = ws.cell(row=r, column=c).value
            # **여기만 `.strip()` 을 본다.** 이 파일의 다른 자리(`data_bounds`·
            # 마스킹 행 세기)는 빈 문자열만 「값 없음」으로 보고 공백 한 칸은
            # 값으로 센다. 여기서 같은 기준을 쓰면 `["□ 급여계좌", " ", " "]`
            # 같은 제목 줄이 3칸을 채워 문턱을 넘고, 그 줄이 머리글이 되어
            # **계좌번호 열이 안 가려진다** — 이 문턱이 막으려던 바로 그 사고다.
            # 기준이 갈린 것은 실수가 아니라 그 이유로 고른 것이다.
            if v not in (None, "") and str(v).strip():
                filled += 1
            if header_kind(v) is not None:
                hits += 1
                if is_label_cell(v):
                    label_hits += 1
        if hits >= MIN_HEADER_HITS:
            return r, "strict"
        if (
            fallback is None
            and label_hits >= 1
            and filled >= MIN_HEADER_CELLS_FALLBACK
        ):
            fallback = r
    if fallback is not None:
        return fallback, "fallback"
    return None, None


def find_header_row(ws, scan=HEADER_SCAN_ROWS):
    """머리글 행. 없으면 None. 「어떻게 잡았나」까지 필요하면
    `find_header_row_detail` 을 쓴다."""
    return find_header_row_detail(ws, scan)[0]


def sheet_drop_reason(ws):
    """머리글을 못 찾은 시트를 통째로 뺄 이유. 없으면 None.

    **라벨 칸에 있을 때만** 이유가 된다 — 상담 메모의 「중도금이자계좌 안됨」
    같은 서술 문장은 가려야 할 열이 있다는 뜻이 아니다."""
    for cell in ws._cells.values():
        v = cell.value
        if header_kind(v) != "mask" or not is_label_cell(v):
            continue
        s = str(v).strip()
        if column_word_hit(s.casefold(), DROP_TRIGGER_WORDS, DROP_TRIGGER_PATTERNS):
            return f"머리글 행을 못 찾아 '{s}' 열을 가릴 수 없었다"
        # 거주지·송달장소는 짧을 때만 — 위 상수의 설명 참조.
        if len(s) <= DROP_TRIGGER_SHORT_MAX and column_word_hit(s.casefold(), DROP_TRIGGER_SHORT_ONLY):
            return f"머리글 행을 못 찾아 '{s}' 열을 가릴 수 없었다"
    return None


# 시트 이름 끝의 `(MMDD)` 또는 `(MDD)`. 실물에 `상담(0710)` 과 `상담(529)` 가 함께 있다.
_SHEET_DATE_RE = re.compile(r"^(.*?)\s*\((\d{3,4})\)\s*$")


def sheet_date_key(title):
    """`상담(0710)` → ('상담', '0710'). 날짜로 안 읽히면 None.

    같은 파일 안에 날짜별로 쌓인 시트를 가려내려고 쓴다. **못 읽으면 None 을 주고,
    부르는 쪽은 그때 전부 남긴다** — 모르면 안 버린다."""
    m = _SHEET_DATE_RE.match(title or "")
    if not m:
        return None
    stem, digits = m.group(1), m.group(2).zfill(4)
    if not stem:
        return None
    month, day = int(digits[:2]), int(digits[2:])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return stem, digits


def masked_columns(ws, header_row):
    """가릴 열 번호 → 그 열의 머리글 글자. 머리글 행이 None 이면 빈 dict."""
    if not header_row:
        return {}
    out = {}
    _last_row, last_col = data_bounds(ws)
    for c in range(1, last_col + 1):
        text = ws.cell(row=header_row, column=c).value
        if header_kind(text) == "mask":
            out[c] = str(text).strip()
    return out


def not_personal_columns(ws, header_row):
    """가릴 낱말이 들었지만 `NOT_PERSONAL_HEADERS` 로 안 가린 열 → 머리글 글자.

    무엇을 열었는지 메타에 남기려고 쓴다. 안 남기면 「가릴 열이 없었다」와
    「가릴 수 있었는데 안 가리기로 했다」가 구별되지 않는다."""
    if not header_row:
        return {}
    out = {}
    _last_row, last_col = data_bounds(ws)
    for c in range(1, last_col + 1):
        text = ws.cell(row=header_row, column=c).value
        if text is None:
            continue
        s = str(text).strip()
        if not s or not column_word_hit(
            s.casefold(), MASK_COLUMN_WORDS, MASK_COLUMN_PATTERNS
        ):
            continue
        if any(s.casefold() == w.casefold() for w in NOT_PERSONAL_HEADERS):
            out[c] = s
    return out

# 재무 자료에서 실제로 쓰이는 것만 해석한다. 모르는 서식은 원값을 쓰고 세어서 메타에 남긴다 —
# 지어낸 값을 담는 것보다 "해석 못 했다"가 낫다.
_INT = re.compile(r"^[#,0]*0(;.*)?$")
_DEC = re.compile(r"^[#,0]*0\.(0+)(;.*)?$")
_PCT = re.compile(r"^[#,0]*0(\.(0+))?%(;.*)?$")

# 날짜 서식에서 아는 토큰만 이 넷 + 요일이다. 나머지(시·분·초, 영문 요일 전체
# 표기 등)는 모르는 것으로 두고 해석 못 함으로 센다.
_DATE_TOKEN_RE = re.compile(r"yyyy|yy|mm|dd|aaa")

# **엑셀 내장 서식 14번 — 「간단한 날짜」다.** openpyxl 은 이것을 `mm-dd-yy` 라는
# 글자로 돌려주지만 그건 규격에 적힌 이름일 뿐이고, 엑셀은 **보는 사람의 지역 설정**
# 으로 찍는다 — 한국에서 열면 `2024-01-28` 이지 `01-28-24` 가 아니다. 셀 서식을
# 그냥 "날짜"로 고르면 붙는 것이 이 번호라 실물에 아주 많다(실물 대조 390셀).
# 글자 그대로 읽으면 미국식으로 뒤집힌 날짜가 known=True 로 나가므로 해석하지 않는다.
# 보는 사람의 지역을 우리가 알 수 없다는 것이 이유이고, 그래서 세는 쪽이 맞다.
_LOCALE_DATE_FORMATS = {"mm-dd-yy"}
_DATE_ALLOWED_SEP = set("-/.() ")
_KOREAN_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]  # datetime.weekday(): 월=0 ... 일=6


_NUMERIC_CORE_RE = re.compile(r"[#,0.%]+")

# 리터럴(따옴표 구간 · 백슬래시 이스케이프 한 글자)을 숫자 스캔이 시작되기 전에
# 통째로 들어낼 때 쓰는 자리표시자 — 유니코드 사용자 영역(Private Use Area)이라
# `#`·`,`·`0`·`.`·`%` 는 물론 대괄호·별표·밑줄 어느 서식 문자와도 절대 안 겹친다.
_LITERAL_RE = re.compile(r'"[^"]*"|\\.')
_LITERAL_BASE = 0xE000

# 서식 문자열에 **이미** 사용자 영역(Private Use Area) 문자가 들어 있으면
# `_lift_literals` 가 심는 자리표시자와 구별할 방법이 없다 — `_lower_literals` 가
# 남의 글자를 리터럴로 잘못 되돌려 `'"단위"' + chr(0xE000) + '#,##0'` 이
# `단위단위1,520` 이 된다. 엑셀 UI 는 이런 문자를 서식에 쓰지 않아 실제로 닿기
# 어려운 자리지만, 자리표시자 방식이 스스로 만든 구멍이라 막아 둔다. 이스케이프해서
# 살리려 들지 않고 **해석하지 않는다** — 값을 지어내는 것보다 "모른다"가 늘 낫다.
_PUA_RE = re.compile("[\ue000-\uf8ff]")

# \ub300\uad04\ud638 \uadf8\ub8f9 \ub450 \uc885\ub958. \ub098\uba38\uc9c0([Red]\u00b7[$-412] \ub4f1)\ub294 \ud654\uba74\uc5d0 \uc548 \ubcf4\uc5ec \uc9c0\uc6b4\ub2e4.
_CONDITION_RE = re.compile(r"^[<>=]")  # [<0] [>=1000] \u2014 \uad6c\uc5ed \uc870\uac74\uc774\uc9c0 \uc7a5\uc2dd\uc774 \uc544\ub2c8\ub2e4
_CURRENCY_RE = re.compile(r"^\$([^-]*)(?:-.*)?$", re.DOTALL)  # [$$-409] [$\u20a9-412] [$-412]
_DBNUM_RE = re.compile(r"^DBNum", re.IGNORECASE)  # [DBNum1] \u2014 \uc22b\uc790 \uae00\ub9ac\ud504\ub97c \ubc14\uafbc\ub2e4

# \uc544\uc8fc \ud070 \uc218\uae4c\uc9c0 \uc790\ub9ac\uc218\ub97c \uc548 \uc783\uace0 \ubc18\uc62c\ub9bc\ud558\ub824\uace0 \ub109\ub109\ud788 \uc7a1\uc740 \uc2ed\uc9c4 \uc5f0\uc0b0 \uc790\ub9ac\uc218.
_ROUND_CTX = decimal.Context(prec=60)


def _raw_text(v):
    """\ud574\uc11d\ud558\uc9c0 \ubabb\ud55c \uac12\uc758 \uc6d0\ubb38 \ud45c\uae30.

    **\uc790\ub9ac\uc218\ub97c \uc798\ub77c \uba39\uc9c0 \uc54a\ub294\ub2e4.** \uc774 \uc790\ub9ac\ub294 \ucc38\uac12\uc744 \uadf8\ub300\ub85c \ub0a8\uae30\ub824\uace0 \uc788\ub294 \uacf3\uc778\ub370,
    \uc608\uc804\uc5d0 \uc4f0\ub358 `f"{v:g}"` \ub294 \uc720\ud6a8\uc22b\uc790 6\uc790\ub9ac\uc5d0\uc11c \ub04a\uc5b4 `39660821185.75` \ub97c
    `3.96608e+10` \uc73c\ub85c \ubc14\uafd4 \ub193\uc558\ub2e4 \u2014 \uc6d0\uac12 \ubcf4\uc874\uc774\ub77c\ub294 \ubaa9\uc801 \uc790\uccb4\uac00 \uae68\uc9c4\ub2e4.
    \ud30c\uc774\uc36c\uc758 `str(float)` \uc740 \ub2e4\uc2dc \uc77d\uc73c\uba74 \uac19\uc740 \uac12\uc774 \ub418\ub294 \uac00\uc7a5 \uc9e7\uc740 \ud45c\uae30\ub77c \uc548 \uc798\ub9b0\ub2e4.
    """
    return str(v)


def _to_decimal(v):
    """\uc22b\uc790 \uc140 \uac12\uc744 \uc2ed\uc9c4\uc218\ub85c. \ubb34\ud55c\ub300\u00b7NaN \ucc98\ub7fc \ud654\uba74 \ud45c\uae30\ub97c \ubaa8\ub974\ub294 \uac12\uc740 None.

    **`str(v)` \ub97c \uac70\uce58\ub294 \uac83\uc774 \uc694\uc810\uc774\ub2e4.** `decimal.Decimal(2.675)` \ub294 \uc774\uc9c4\uc218 \uc6d0\uac12
    `2.67499999999999982\u2026` \ub77c \ub450 \uc790\ub9ac\ub85c \uc904\uc774\uba74 2.67 \uc774 \ub418\ub294\ub370, \uc5d1\uc140\uc740 \uc720\ud6a8\uc22b\uc790
    15\uc790\ub9ac\ub85c \uba3c\uc800 \ub2e4\ub4ec\uace0 \ubcf4\ubbc0\ub85c 2.68 \uc744 \ucc0d\ub294\ub2e4. `str(float)` \uc740 \uc655\ubcf5\uc774 \ub418\ub294 \uac00\uc7a5
    \uc9e7\uc740 \ud45c\uae30\ub77c \uadf8 15\uc790\ub9ac \ub2e4\ub4ec\uae30\uc640 \uac19\uc740 \uc790\ub9ac\uc5d0\uc11c \ub04a\uaca8 \uc5d1\uc140 \ud654\uba74\uacfc \ub9de\ub294\ub2e4.
    """
    try:
        d = decimal.Decimal(str(v))
    except (ValueError, ArithmeticError):
        return None
    return d if d.is_finite() else None


# ── General 서식 ────────────────────────────────────────────────────────
#
# 엑셀은 유효숫자 15자리까지만 보여준다. 그 밖에 **평문(0.00001·4561.2)으로 찍는
# 구간**과 **지수 표기로 넘어가는 구간**이 갈리는데, 그 경계는 추론하지 않고
# LibreOffice 로 재서 정했다(`soffice --convert-to csv` 의 「보이는 대로 저장」).
# 잰 결과: 위쪽은 십진 지수 **14 까지 평문**이고 16 이상은 언제나 지수 표기,
# **15 는 갈린다**(1e15~9e15 는 평문인데 9.9e15 부터 지수 표기)라 15 는 세는 쪽으로.
#
# **아래쪽 경계는 지수 하나로 안 정해진다 — 유효숫자 개수와 함께 움직인다.**
# 처음에 지수만 훑어 -9 로 잡았는데, 지수 × 유효숫자 개수로 격자를 만들어 다시 재니
# 15자리까지 평문인 것은 지수 -4 까지였고 그 아래로는 자릿수가 줄어든다
# (-5 는 12자리 · -6 은 11 · -7 은 10 · -8 은 9 · -9 는 8자리까지만 평문).
# 그래서 `1.2345678901234e-05`(연이율을 365로 나눈 값 같은 것)가 화면에는
# `1.2345678901234E-05` 인데 우리는 `0.000012345678901234` 를 known=True 로 냈다.
# **면이 둘인 경계는 규칙을 하나 더 붙이지 않고 안전한 쪽 끝을 쓴다** — `-4`.
# 이 가지에서 나온 결함 아홉 중 여럿이 「규칙 하나만 더」 자리에서 나왔다.
# 억 단위를 다루는 자료에서 1e-4 보다 작은 값은 드물어 세는 양도 거의 안 는다.
#
# 지수 표기 구간을 아예 안 그리는 이유: LibreOffice 는 `1E+016` 으로 찍고 엑셀은
# `1E+16` 으로 찍는다 — 재는 도구와 맞춰야 할 대상이 서로 다른 글자를 내므로
# **우리가 그 모양을 확정할 방법이 없다.** 지어내는 것보다 세어서 사람이 보는 편이 낫다.
_GENERAL_CTX = decimal.Context(prec=15, rounding=decimal.ROUND_HALF_UP)
_GENERAL_CTX16 = decimal.Context(prec=16, rounding=decimal.ROUND_HALF_UP)
_GENERAL_MIN_EXP = -4
_GENERAL_MAX_EXP = 14


def _general_is_ambiguous(v, d, rounded):
    """15자리로 줄이는 자리가 아슬아슬해서 **렌더러마다 답이 갈리는** 값인가.

    LibreOffice 로 3,299개를 대조하다 9종이 마지막 한 자리에서 어긋났다. 전부 같은
    모양이었다 — 16번째 유효숫자가 `4` 이고 그다음이 `7`~`9` 인 값
    (`1256.5289256198348` → 우리 `…983` · LibreOffice `…984`). LibreOffice 는 16자리로
    한 번 줄인 뒤 다시 15자리로 줄여서(이중 반올림) `…47` 이 `…5` 가 되고 그게 다시
    올라간다. 이진수 원값에서 곧바로 15자리로 줄이면 내려간다.

    **어느 쪽이 엑셀인지 우리는 모른다** — 엑셀로 재 볼 수단이 없다. 그래서 두 길의
    답이 갈리는 값은 그리지 않고 센다. 재 보니 이 구간은 3,299개 중 27개(0.8%)라
    계수기를 덮을 양이 아니다.
    """
    if _GENERAL_CTX.plus(_GENERAL_CTX16.plus(d)) != rounded:
        return True  # 16자리를 거치면 답이 바뀐다 (LibreOffice 가 가는 길)
    if isinstance(v, float):
        try:
            if _GENERAL_CTX.plus(decimal.Decimal(v)) != rounded:
                return True  # 이진수 원값에서 줄이면 답이 바뀐다
        except ArithmeticError:
            return True
    return False


# ── 「해석 못 함」의 사유 ─────────────────────────────────────────────────
#
# **왜 개수만으로는 모자란가.** 점검표는 오래 「서식 해석 못 함 N셀」만 적었다. 한 파일이
# 14.69% 였을 때 그것이 **전부 한 원인**(openpyxl 이 못 푼 내장 서식)이었는데, 읽는 사람은
# 그 사실을 알 방법이 없어 무엇을 봐야 할지 정할 수 없었다.
#
# 사유는 **판정하는 그 순간에만** 있다. `known=False` 라는 bool 로 접히고 나면 세는 자리
# (`sheet_to_table`)에서는 복구할 수 없다 — 그래서 표시 쪽만 고쳐서는 안 되고 여기서부터
# 들고 나가야 한다. 셀에서 사유를 **다시 알아내는** 길은 일부러 안 만들었다. 그러면 판정이
# 두 벌이 되어 서로 갈리고, 이 저장소가 여러 번 겪은 그 고장이 조용히 다시 생긴다.
#
# 같은 꼴을 점검표가 이미 둘 쓰고 있다 — `숨긴 행 65(내용 있는 것 46)` · `오류 셀 12개
# (#DIV/0! · #N/A)`. 개수 옆에 「무엇이」를 붙이는 자리다.
R_GENERAL_UNREADABLE = "General — 값을 십진수로 못 읽음"
R_GENERAL_EXPONENT = "General — 지수 표기 구간"
R_GENERAL_AMBIGUOUS = "General — 15자리에서 갈림"
R_ROUND_AMBIGUOUS = "반올림이 렌더러마다 갈림"
R_FMT_PUA = "서식에 사용자 영역 문자"
R_FMT_SECTIONS = "구역이 넷을 넘음"
R_FMT_CONDITION = "서식에 조건·숫자 치환"
R_FMT_LITERAL = "리터럴만인 구역을 못 읽음"
R_NUM_UNREADABLE = "값을 십진수로 못 읽음"
R_NUM_PLACEHOLDER = "숫자 자리표시자를 못 읽음"
R_TEXT_SECTION = "문자 구역 — 구역 초과·조건"
R_TEXT_AT = "문자 구역에 @ 자리가 없거나 둘 이상"
R_UNKNOWN_BUILTIN = "openpyxl 이 못 푼 내장 서식"
R_BOOL_FORMAT = "참거짓인데 General·@ 서식이 아님"
R_TIMEDELTA = "경과시간 셀 (범위 밖)"
R_TIME = "시간만 있는 셀 (범위 밖)"
R_DATE_FORMAT = "날짜인데 서식을 못 그림"
R_UNKNOWN_TYPE = "모르는 값 형"


def _render_general(v):
    """서식이 `General`(또는 `@`)인 숫자 한 칸을 (표시 문자열, 아는 값인가) 로.

    **`_raw_text` 와 계약이 정반대라 함수를 따로 둔다.** `_raw_text` 는
    known=False 자리에서 **저장된 참값**을 한 자리도 안 잘리게 남기는 것이 일이고
    (Finding 9), 여기는 **엑셀 화면 글자**를 내는 것이 일이다. 예전엔 한 식이
    두 자리를 다 맡아서, `4561.200000000001`(1520.4×3 처럼 흔한 계산 결과)이
    파이썬 repr 그대로 known=True 로 나갔다 — 엑셀 화면은 `4561.2` 다.
    `1e-05`·`1e+16` 같은 파이썬 표기도 그대로 새어 나갔다.
    """
    d = _to_decimal(v)
    if d is None:
        return _raw_text(v), False, R_GENERAL_UNREADABLE
    # 0 을 따로 받는 분기는 두지 않는다. `Context.plus` 가 `-0.0` 의 부호를 떼 주고
    # (0 + -0 = +0), 남은 길이 그대로 `0` 을 낸다 — 분기를 두었다가 되돌려 봤는데
    # 시험이 아무 반응도 안 했다. 증명할 수 없는 분기는 안 남긴다(라운드 2와 같은 판단).
    try:
        rounded = _GENERAL_CTX.plus(d)  # 유효숫자 15자리로 (엑셀이 보여주는 한계)
    except ArithmeticError:
        return _raw_text(v), False, R_GENERAL_UNREADABLE
    if not (_GENERAL_MIN_EXP <= rounded.adjusted() <= _GENERAL_MAX_EXP):
        # 지수 표기 구간이거나 갈리는 15자리 — 화면 글자를 확정할 수 없다.
        return _raw_text(v), False, R_GENERAL_EXPONENT
    if _general_is_ambiguous(v, d, rounded):
        return _raw_text(v), False, R_GENERAL_AMBIGUOUS
    return f"{rounded.normalize():f}", True, None


# 반올림 자리가 앞에서 몇 번째 유효숫자에 닿으면 「모른다」로 세는가.
#
# **왜 15가 아니라 14인가.** 엑셀이 지키는 마지막 자리가 15번째다. 소수 n자리로
# 반올림할 때 올릴지 내릴지를 **결정하는 자리는 그 바로 다음 자리**이므로, 반올림
# 자리가 14번째면 결정하는 자리가 15번째 — 엑셀이 겨우 붙잡고 있고 이진수 오차가
# 앉는 자리다. 그 자리를 근거로 판정하지 않는다.
#
# 추론만이 아니라 재서 정했다. 2026-08-23 에 LibreOffice 로 2,500셀을 대조한 결과:
#   경계 16 → 어긋남 3건 · 15 → 1건 · **14 → 0건** · 13 → 0건 (안 그리는 셀만 늘었다)
#
# **비용은 자료 모양에 달렸다 — 「0이다」로 적으면 거짓말이 된다.**
#   · 아카이브에 실린 엑셀 숫자 셀 7,128개: 표시 자릿수가 최대 12자리라 **닿는 셀 0개**
#   · 조 단위 값에 소수 두 자리 서식을 붙인 합성 표본: 10.9%가 안 그려진다
# 즉 「정수부 + 소수 자리」가 14자리에 닿는 자료(조 단위를 소수 둘째 자리까지 적는 표)를
# 새로 넣으면 그 표는 상당수가 known=False 로 세어진다. 점검표의 「서식 해석 못 함」이
# 갑자기 뛰면 여기를 먼저 보라 — 고장이 아니라 이 규칙이 일하는 중이다.
_QUANT_SIG_LIMIT = 14


def _quantize(d, digits):
    """\uc18c\uc218 `digits` \uc790\ub9ac\ub85c **\uc5d1\uc140\uacfc \uac19\uc740 \ubc29\ud5a5\uc73c\ub85c** \ubc18\uc62c\ub9bc\ud55c\ub2e4. \ubabb \ud558\uba74 None.

    **\uc65c f-string \uc744 \uc548 \uc4f0\ub294\uac00.** `f"{v:,.0f}"` \ub294 \ud30c\uc774\uc36c \uae30\ubcf8\uc778 \u300c\uc9dd\uc218 \ucabd\uc73c\ub85c
    \ubc18\uc62c\ub9bc\u300d(round-half-even)\uc774\ub77c 20336.5 \ub97c 20,336 \uc73c\ub85c \ucc0d\ub294\ub370, \uc5d1\uc140\uc740 \u300c0\uc5d0\uc11c \uba3c
    \ucabd\uc73c\ub85c\u300d \uc62c\ub824 20,337 \ub85c \ucc0d\ub294\ub2e4. `.5` \ub294 \uc774\uc9c4\uc218\ub85c \uc815\ud655\ud788 \ud45c\ud604\ub418\ubbc0\ub85c \uc774 \ucc28\uc774\ub294
    \ubd80\ub3d9\uc18c\uc218\uc810 \uc7a1\uc74c\uc774 \uc544\ub2c8\ub77c \uac19\uc740 \uc785\ub825\uc5d0 \ub298 \uac19\uac8c \ub098\uc624\ub294 \uacb0\uc815\uc801 \ucc28\uc774\ub2e4 \u2014 \uc2e4\ubb3c 32\uac1c
    \ud30c\uc77c 218,992\uc140\uc744 \uc7ac \ubcf4\ub2c8 494\uc140\uc774 \uc774 \ud558\ub098\ub85c \uc5b4\uae0b\ub0ac\uace0 \uc804\ubd80 known=True \ub85c
    \ub098\uac14\ub2e4(\uac00\uc7a5 \ub9ce\uc740 \uc790\ub9ac\uac00 \ud55c\uad6d \ud68c\uacc4 \uc11c\uc2dd `_-* #,##0_-`).
    """
    try:
        q = d.quantize(
            decimal.Decimal(1).scaleb(-digits),
            rounding=decimal.ROUND_HALF_UP,
            context=_ROUND_CTX,
        )
    except ArithmeticError:
        # \uc790\ub9ac\uc218\uac00 60\uc744 \ub118\ub294 \uac12. \uc9c0\uc5b4\ub0b4\uc9c0 \uc54a\uace0 known=False \ub85c \ubcf4\ub0b8\ub2e4.
        return None
    if q.is_zero():
        # **\ubc18\uc62c\ub9bc\ud574\uc11c 0 \uc774 \ub418\uba74 \ub9c8\uc774\ub108\uc2a4\ub97c \ub5bc\uc5b4\uc57c \ud55c\ub2e4.** -0.125 \ub97c `#,##0` \uc73c\ub85c \uadf8\ub9ac\uba74
        # Decimal \uc774 \ubd80\ud638\ub97c \uc9c0\ucf1c `-0` \uc774 \ub098\uc624\ub294\ub370 \ud654\uba74\uc740 `0` \uc774\ub2e4(2026-08-23 LibreOffice
        # \ub300\uc870\uc5d0\uc11c \ubc1c\uacac, \uc544\uce74\uc774\ube0c\uc5d0 \uc774\ubbf8 1\uc140 \ub4e4\uc5b4\uac00 \uc788\uc5c8\ub2e4). General \uacbd\ub85c\ub294 `Context.plus`
        # \uac00 `-0.0` \uc758 \ubd80\ud638\ub97c \ub5bc \uc8fc\uc5b4 \ucc98\uc74c\ubd80\ud130 `0` \uc774\uc5c8\uc73c\ub2c8, \uc774\uac74 \ub450 \uacbd\ub85c\ub97c \ub9de\ucd94\ub294 \uac83\uc774\uae30\ub3c4 \ud558\ub2e4.
        # \uc74c\uc218 **\uad6c\uc5ed**\uc774 \ub530\ub85c \uc788\ub294 \uc11c\uc2dd\uc740 \uc5ec\uae30 \uc548 \uc628\ub2e4 \u2014 \uadf8\ucabd\uc740 \uac12\uc744 \uc808\ub313\uac12\uc73c\ub85c \ubc14\uafd4 \ubd80\ub978\ub2e4.
        q = q.copy_abs()
    if _quantize_is_ambiguous(d, digits, q):
        return None
    return q


def _quantize_is_ambiguous(d, digits, q):
    """\uc694\uccad\ud55c \uc18c\uc218 \uc790\ub9ac\uac00 **\uc720\ud6a8\uc22b\uc790 15\uc790\ub9ac \ub108\uba38**\ub77c \ub80c\ub354\ub7ec\ub9c8\ub2e4 \ub2f5\uc774 \uac08\ub9ac\ub294 \uac12\uc778\uac00.

    \uc5d1\uc140\uc740 \uc720\ud6a8\uc22b\uc790 15\uc790\ub9ac\uae4c\uc9c0\ub9cc \ubcf4\uc5ec\uc900\ub2e4. \uadf8 \ub108\uba38\uc5d0 \ubc18\uc62c\ub9bc \uc790\ub9ac\uac00 \uac78\ub9ac\uba74 \u300c\uc6d0\uac12\uc5d0\uc11c
    \ud55c \ubc88\uc5d0 \ubc18\uc62c\ub9bc\u300d\uacfc \u300c15\uc790\ub9ac\ub85c \uc904\uc778 \ub4a4 \ubc18\uc62c\ub9bc\u300d\uc758 \ub2f5\uc774 \ub2ec\ub77c\uc9c4\ub2e4 \u2014 `_general_is_ambiguous`
    \uac00 General \uc11c\uc2dd\uc5d0\uc11c \ub9c9\ub294 \uac83\uacfc **\uac19\uc740 \ubaa8\uc591\uc758 \ubb38\uc81c**\uc774\uace0, \uc5ec\uae30\ub294 \uc790\ub9ac\ud45c\uc2dc\uc790 \uc11c\uc2dd \ucabd\uc774\ub2e4.

    **\uc5b4\ub290 \ucabd\uc774 \uc5d1\uc140\uc778\uc9c0 \uc6b0\ub9ac\ub294 \ubaa8\ub978\ub2e4.** 2026-08-23 \uc5d0 LibreOffice \ub85c \uac08\ub9ac\ub294 218\uc140\uc744
    \uc7ac \ubcf4\ub2c8 15\uc790\ub9ac \ucabd\uc774 \ub9de\uc740 \uac83 124 \u00b7 \ud55c \ubc88\uc5d0 \ubc18\uc62c\ub9bc\ud55c \ucabd\uc774 \ub9de\uc740 \uac83 79 \u00b7 **\ub458 \ub2e4 \ud2c0\ub9b0 \uac83 5**
    \uc600\ub2e4. \uac19\uc740 \uc790\ub9bf\uc218\u00b7\uac19\uc740 \uc18c\uc218\uc790\ub9ac \uc548\uc5d0\uc11c\ub3c4 \uac12\ub9c8\ub2e4 \uac08\ub824\uc11c(\uc815\uc218 11\uc790\ub9ac\u00b7\uc18c\uc218 1\uc790\ub9ac = 6 \ub300 4)
    \uc790\ub9bf\uc218\ub85c \uac00\ub974\ub294 \uaddc\uce59\uc744 \uc138\uc6b8 \uc218\ub3c4 \uc5c6\ub2e4. \uadf8\ub798\uc11c \ud55c\ucabd\uc73c\ub85c \uc815\ud558\uc9c0 \uc54a\uace0 **\uc548 \uadf8\ub9ac\uace0 \uc13c\ub2e4.**
    \uc774 \ud30c\uc77c\uc774 \uc9c0\uc218 \ud45c\uae30 \uad6c\uac04\uacfc \uac08\ub9ac\ub294 General \uac12\uc5d0 \uc774\ubbf8 \uc4f0\uace0 \uc788\ub294 \uaddc\uce59 \uadf8\ub300\ub85c\ub2e4.

    \ub9c9\ub294 \uc790\ub9ac\uac00 **\ub458**\uc774\ub2e4.

    \u2460 **\ub450 \uae38\uc758 \ub2f5\uc774 \uac08\ub9b4 \ub54c.** \uac12\uc774 15\uc790\ub9ac\ub97c \ub118\uac8c \uc800\uc7a5\ub3fc \uc788\uc73c\uba74, \uc904\uc778 \ub4a4 \ubc18\uc62c\ub9bc\ud558\ub294 \uac83\uacfc
       \uace7\ubc14\ub85c \ubc18\uc62c\ub9bc\ud558\ub294 \uac83\uc774 \ub2ec\ub77c\uc9c4\ub2e4(2109901986.749998 + `0.0` \u2192 .7 \ub300 .8).

    \u2461 **\ubc18\uc62c\ub9bc\uc744 \uacb0\uc815\ud558\ub294 \uc790\ub9ac\uac00 \uc5d1\uc140\uc774 \uaca8\uc6b0 \ubd99\uc7a1\uace0 \uc788\ub294 15\ubc88\uc9f8 \uc790\ub9ac\uc77c \ub54c**
       (`_QUANT_SIG_LIMIT` \uc8fc\uc11d \ucc38\uc870). \uc5ec\uae30\uc11c\ub294 \ub450 \uae38\uc774 \uc6b0\uc5f0\ud788 \uac19\uc544\ub3c4 \ubabb \ubbff\ub294\ub2e4 \u2014
       \uc7ac\ub294 \ub3c4\uad6c\uac00 **\uc790\uae30\ubaa8\uc21c**\uc744 \uc77c\uc73c\ud0a8\ub2e4. 2026-08-23 \uc2e4\uce21: 68601575893949.84 \ub97c
       LibreOffice \uac00 `0.0` \uc73c\ub85c\ub294 `\u20269`, `0.00` \uc73c\ub85c\ub294 `\u202680` \uc73c\ub85c \uadf8\ub838\ub2e4. `.80` \uc744 \ud55c
       \uc790\ub9ac\ub85c \uc904\uc774\uba74 `.8` \uc774\ub77c \ub458\uc774 \uc11c\ub85c \uc548 \ub9de\ub294\ub2e4. \uc5b4\ub290 \ucabd\uc774 \uc5d1\uc140\uc778\uc9c0 \uac00\ub9b4 \uadfc\uac70\uac00 \uc5c6\ub2e4.

    \uc9c0\uae08 \uc544\uce74\uc774\ube0c\uc5d0 \uc2e4\ub9b0 \uc5d1\uc140 \uc22b\uc790 \uc140 7,128\uac1c\ub294 **\ud558\ub098\ub3c4 \uc5ec\uae30 \uc548 \uac78\ub9b0\ub2e4**(2026-08-23,
    \ud45c\uc2dc \uc790\ub9bf\uc218 \ucd5c\ub300 12\uc790\ub9ac). \ub2e4\ub9cc \ube44\uc6a9\uc740 \uc790\ub8cc \ubaa8\uc591\uc5d0 \ub2ec\ub838\ub2e4 \u2014 `_QUANT_SIG_LIMIT` \uc8fc\uc11d \ucc38\uc870.
    """
    # \u2461 \ubc18\uc62c\ub9bc \uc790\ub9ac\uac00 \uc5d1\uc140\uc774 \ubcf4\uc5ec\uc904 \uc218 \uc788\ub294 \uc790\ub9ac \uc218\uc5d0 \ub2ff\ub294\uac00
    if not d.is_zero() and d.adjusted() + 1 + digits >= _QUANT_SIG_LIMIT:
        return True
    step = decimal.Decimal(1).scaleb(-digits)
    # \u2460 15\uc790\ub9ac\ub85c \uc904\uc778 \ub4a4 \ubc18\uc62c\ub9bc\ud558\uba74 \ub2f5\uc774 \ubc14\ub00c\ub294\uac00
    # \u2462 \uc800\uc7a5\ub41c \uc774\uc9c4\uc218 \uc6d0\uac12\uc5d0\uc11c \ubc18\uc62c\ub9bc\ud558\uba74 \ub2f5\uc774 \ubc14\ub00c\ub294\uac00 (\uadf8 \uac12\ub3c4 15\uc790\ub9ac\ub85c \uc904\uc5ec \ud55c \ubc88 \ub354)
    # **이진수 원값(`decimal.Decimal(float)`)을 따로 보는 갈래는 두지 않는다.** 한 번
    # 넣었다가 되돌려 보니 **시험이 아무 반응도 안 했고**, ①·② 를 안 건드리면서 그 갈래만
    # 걸리는 입력을 300만 회 뽑아도 0건이었다(2026-08-23). `str(float)` 은 왕복이 되는
    # 가장 짧은 표기라, 15자리로 줄이면 이진수 원값을 줄인 것과 같은 값이 되기 때문이다.
    # 증명할 수 없는 분기는 안 남긴다 — 이 파일이 이미 두 자리에서 같은 판단을 했다.
    #
    # (엑셀은 15자리로 먼저 다듬고 보여주므로 `1.005`(저장은 1.00499…)는 화면에 `1.01`
    # 이다. 줄이지 않은 원값을 그냥 대면 그 멀쩡한 값까지 삼킨다 — 실제로 그렇게 넣었다가
    # 시험 두 항목이 빨개져 되돌렸다.)
    try:
        if _GENERAL_CTX.plus(d).quantize(
            step, rounding=decimal.ROUND_HALF_UP, context=_ROUND_CTX
        ) != q:
            return True
    except ArithmeticError:
        return True
    return False


def _lift_literals(section):
    """따옴표 리터럴과 백슬래시 이스케이프 글자를 **숫자 스캔보다 먼저** 불투명한
    자리표시자 한 글자씩으로 바꾼다. 실제 글자는 반환하는 목록에 순서대로 챙겨 둔다.

    **왜 스캔 전에 먼저 들어내는가 — 구조로 막는 지점.** 지금까지 리뷰 세 번이 잡은
    문제가 전부 같은 모양이었다: 리터럴을 서식 문자와 한 문자열에 남겨 둔 채 정규식
    으로 걸러내려 했고, 리터럴 안에 우연히 서식 문자(회계 서식의 `0`·`.`, 이번엔
    `%`)가 섞이면 그 뒤 어떤 스캔도 리터럴과 진짜 자리표시자를 구별하지 못했다.
    `0.0"%"` 는 "곱하지 말고 % 기호만 보여라"는 서식인데, 따옴표를 먼저 벗기고
    스캔하면 그 `%` 가 진짜 퍼센트 자리표시자와 안 갈라져 값을 100배로 올렸다.
    리터럴을 먼저 이 함수가 불투명한 자리로 바꿔 두면, 그 뒤에 어떤 정규식을
    걸어도 리터럴 속 글자는 다시 나타나지 않는다 — 다음에 또 어떤 문자가 리터럴에
    섞여도 이 순서 자체가 막아 준다.
    """
    literals = []

    def _replace(m):
        text = m.group(0)
        text = text[1:-1] if text[0] == '"' else text[1:]  # "억" → 억 · \₩ → ₩
        literals.append(text)
        return chr(_LITERAL_BASE + len(literals) - 1)

    lifted = _LITERAL_RE.sub(_replace, section)
    return lifted, literals


def _lower_literals(s, literals):
    """`_lift_literals` 가 심어 둔 자리표시자 한 글자를 원래 리터럴 글자로 되돌린다."""
    return "".join(
        literals[ord(ch) - _LITERAL_BASE]
        if _LITERAL_BASE <= ord(ch) < _LITERAL_BASE + len(literals)
        else ch
        for ch in s
    )


def _strip_invisible_decorations(section, literals):
    """리터럴을 이미 자리표시자로 들어낸 문자열에서, 화면에 **안 보이는** 장식만
    걷어낸다 — `_x` 정렬 패딩(`_)`·`_-` 등) · `* ` 채움 · `[Red]`·`[$-412]` 류
    대괄호 그룹. 리터럴은 이미 자리표시자라 이 정규식들이 리터럴 속 글자를 볼
    일이 자체가 없다(예전엔 리터럴이 아직 원문 그대로라 `"[단위]"` 같은 리터럴
    안의 대괄호까지 지워질 수 있었다).

    **대괄호를 전부 지우던 것이 두 자리에서 화면 글자를 잃었다.**

    ① 통화 기호 — `[$$-409]`·`[$€-2]`·`[$₩-412]` 에서 `[$` 와 `-` 사이 글자는
       화면에 **찍힌다**. 그 자리가 비어 있는 `[$-412]` 만 순수한 지역 표시라
       안 보인다. 지우고 known=True 를 달면 `$1,520.40` 이 `1,520.40` 이 되어
       나가는데, 이건 라운드 2 에서 `억` 을 지웠던 것과 같은 결함이다. 그래서
       기호가 있으면 `_lift_literals` 와 같은 자리표시자로 바꿔 **제자리에 남긴다**.

    ② 조건(`[<0]`)과 한자·한글 숫자(`[DBNum1]`)는 여기 오기 전에 걸러진 상태다 —
       `_unhandled_bracket` 이 **모든 구역**을 먼저 훑어 그런 서식을 통째로
       known=False 로 보낸다. 여기서도 한 번 더 보게 두었다가 되돌려 봤는데
       시험이 아무 반응도 안 했다(닿지 않는 분기였다). 증명할 수 없는 분기는
       안 남긴다 — 판정은 `_unhandled_bracket` 한 곳이다.

    반환: 걷어낸 문자열.
    """
    out, pos = [], 0
    for m in re.finditer(r"\[([^\]]*)\]", section):
        out.append(section[pos : m.start()])
        content = m.group(1)
        cm = _CURRENCY_RE.match(content)
        if cm and cm.group(1):
            literals.append(cm.group(1))
            out.append(chr(_LITERAL_BASE + len(literals) - 1))
        # 그 밖([Red]·[$-412]·[h] 등)은 정말로 화면에 안 보인다 — 그냥 뺀다
        pos = m.end()
    out.append(section[pos:])

    s = "".join(out)
    s = re.sub(r"\*.", "", s)  # * 채움 — 안 보인다 (채움 문자 하나까지 함께)
    s = re.sub(r"_.", "", s)  # _) _- 등 패딩 — 안 보인다 (패딩 문자 하나까지 함께)
    return s


def _split_sections(lifted):
    """리터럴을 이미 자리표시자로 들어낸 서식을 구역(`;`)으로 가른다.

    **가르는 곳은 여기 하나뿐이다.** 리터럴이 이미 자리표시자라 따옴표 안의
    세미콜론(`#,##0"원;부가세별도"`)은 구분자로 안 읽힌다 — 원문에 대고 `split(";")`
    하면 첫 구역이 `#,##0"원` 이 되어 `1,520"원` 이 known=True 로 나갔었다.
    """
    return lifted.split(";")


def _unhandled_bracket(lifted_section):
    """이 구역에 우리가 못 다루는 대괄호가 있나 — 조건(`[<0]`)과 숫자 치환(`[DBNum1]`).

    구역 고르기는 값의 **부호**로 한다. 조건이 하나라도 붙어 있으면 엑셀이 구역을
    고르는 규칙 자체가 부호가 아니라 그 조건이 되므로, 부호로 고른 구역은 엉뚱한
    구역이다 — 그래서 **어느 구역에 있든** 그 서식은 통째로 해석하지 않는다.
    """
    return any(
        _CONDITION_RE.match(m.group(1)) or _DBNUM_RE.match(m.group(1))
        for m in re.finditer(r"\[([^\]]*)\]", lifted_section)
    )


def _is_literal_only(normalized, literals):
    """장식을 걷어낸 구역에 **리터럴과 자리 채움 말고는 아무것도 안 남았나.**

    남은 글자가 서식 토큰이면(`General`·`@`·`yyyy` 등) 그건 리터럴이 아니라 우리가
    아직 안 다루는 지시어다. 그걸 리터럴로 보고 되돌리면 **서식 글자가 그대로 셀
    값이 되어** 나간다 — `[$$-409]General` 이 `$General` 로, 날짜 서식이 붙은 숫자
    셀이 `yyyy-mm-dd` 로. 아홉 번째 사례가 정확히 이 모양이었다. 그래서 리터럴
    자리표시자와 `?`(채울 숫자가 없는 자리)·공백만 통과시키고 나머지는 세는 쪽으로.
    """
    return all(
        _LITERAL_BASE <= ord(ch) < _LITERAL_BASE + len(literals) or ch == "?" or ch.isspace()
        for ch in normalized
    )


def _extract_numeric_core(normalized):
    """리터럴을 자리표시자로 들어내고 장식까지 걷어낸 문자열에서 자리표시자 뭉치를
    하나 찾아 (접두, 핵심, 접미) 로 가른다. 뭉치가 없거나 둘 이상이거나, 그 뭉치가
    `_INT`/`_DEC`/`_PCT` 어느 것과도 안 맞으면 `None` — 어느 쪽에도 놓을 자리를
    믿을 수 없다는 뜻이라 known=False 로 떨어뜨려야 한다(분수 서식 `# ?/?` 가 이
    경우: `#` 하나만 뭉치인데 '0' 으로 안 끝나 세 정규식 어디에도 안 맞는다.
    `#,##0,` 도 이 경우: 뭉치는 하나지만 끝의 `,` 때문에 세 정규식 다 안 맞는다)."""
    runs = list(_NUMERIC_CORE_RE.finditer(normalized))
    if len(runs) != 1:
        return None
    m = runs[0]
    core = m.group(0)
    if not (_PCT.match(core) or _DEC.match(core) or _INT.match(core)):
        return None
    return normalized[: m.start()], core, normalized[m.end() :]


def _render_date(v, number_format):
    """엑셀 날짜 서식을 화면 그대로 렌더한다. 아는 토큰(yyyy·yy·mm·dd·aaa)과 구분자
    (-·/·.·()·공백), 그리고 리터럴로만 이뤄진 서식만 해석하고, 그 밖의 문자가
    섞여 있으면 None(모른다)을 돌려준다.

    **숫자 쪽과 같은 순서를 쓴다 — 리터럴을 먼저 들어내고 그다음에 훑는다.** 예전에는
    이 함수만 따옴표를 먼저 벗기고 토큰을 훑어서, `yyyy-mm-dd"(dd)"` 가
    `2026-04-21(21)` 이 됐다 — 리터럴 속 `dd` 가 진짜 자리표시자와 안 갈렸다.
    라운드 3 이 숫자 쪽에서 구조로 막은 바로 그 결함인데, 날짜 경로만 그 수정이
    닿지 않은 채 남아 있었다.
    """
    raw_fmt = number_format or ""
    if raw_fmt in _LOCALE_DATE_FORMATS:
        return None  # 보는 사람의 지역 설정을 따르는 내장 서식 (위 _LOCALE_DATE_FORMATS 설명)
    if _PUA_RE.search(raw_fmt):
        return None  # 자리표시자와 같은 영역의 문자가 이미 서식에 있다 (숫자 쪽과 같은 이유)

    lifted_all, literals = _lift_literals(raw_fmt)
    section = lifted_all.split(";")[0]

    def _is_literal(ch):
        return _LITERAL_BASE <= ord(ch) < _LITERAL_BASE + len(literals)

    residual = _DATE_TOKEN_RE.sub("", section)
    if any(ch not in _DATE_ALLOWED_SEP and not _is_literal(ch) for ch in residual):
        return None

    out = []
    pos = 0
    for m in _DATE_TOKEN_RE.finditer(section):
        out.append(section[pos : m.start()])
        tok = m.group(0)
        if tok == "yyyy":
            out.append(f"{v.year:04d}")
        elif tok == "yy":
            out.append(f"{v.year % 100:02d}")
        elif tok == "mm":
            out.append(f"{v.month:02d}")
        elif tok == "dd":
            out.append(f"{v.day:02d}")
        elif tok == "aaa":
            out.append(_KOREAN_WEEKDAYS[v.weekday()])
        pos = m.end()
    out.append(section[pos:])
    return _lower_literals("".join(out), literals)


def _render_number(v, number_format):
    """숫자 셀(int·float) 한 칸을 (표시 문자열, 서식을 해석했나) 로. 서식을 못 읽으면
    원값 + known=False 를 돌려주고, 그 셀은 메타의 `unformatted_cells` 로 세어진다."""
    raw_fmt = number_format or "General"
    fallback = _raw_text(v)

    if _PUA_RE.search(raw_fmt):
        # 자리표시자와 같은 영역의 문자가 서식에 이미 있다 — 되돌릴 때 어느 글자가
        # 리터럴이었는지 가릴 수 없으므로 아예 해석하지 않는다 (위 _PUA_RE 설명 참조).
        return fallback, False, R_FMT_PUA

    # **구역(`;`)을 가르기 전에** 리터럴을 들어낸다 (`_split_sections` 설명 참조).
    lifted_all, literals = _lift_literals(raw_fmt)  # 리터럴을 먼저 불투명한 자리로 뺀다
    sections = _split_sections(lifted_all)
    if len(sections) > 4:
        return fallback, False, R_FMT_SECTIONS  # 엑셀의 구역은 최대 넷이다
    if any(_unhandled_bracket(s) for s in sections):
        return fallback, False, R_FMT_CONDITION  # 조건·숫자 치환 — 통째로 해석하지 않는다

    # **엑셀은 구역을 값의 부호로 고른다**: ① 양수 ② 음수 ③ 0 ④ 문자.
    # 없는 구역은 1번으로 떨어진다 — 구역이 둘뿐이면 0 은 1번을 쓰고, 하나뿐이면
    # 음수도 1번을 써서 빼기 기호가 자동으로 붙는다.
    #
    # 늘 1번 구역만 쓰던 것이 이 결함의 일곱 번째 사례였다. 조건 대괄호(`[<0]`)를
    # 막은 것과 같은 이유인데, **조건을 안 쓴 쪽이 훨씬 흔하다** — 회계 서식
    # `_-* #,##0_-;-* #,##0_-;_-* "-"_-;_-@_-` 의 0 이 `0` 으로(엑셀은 `-`),
    # 괄호 음수 `#,##0_);\(#,##0\)` 의 -1520.4 가 `-1,520` 으로(엑셀은 `(1,520)`)
    # 나갔고 전부 known=True 였다. 그 둘이 실측에서 각각 5만·1만 셀대이고 0 은
    # 재무 자료 어디에나 있다. 그래서 음수·0 을 전부 known=False 로 미루지 않고
    # 제대로 고른다 — 늘 울리는 계수기는 신호가 아니다.
    idx = 0
    if v < 0 and len(sections) >= 2:
        idx = 1
    elif v == 0 and len(sections) >= 3:
        idx = 2
    section = sections[idx]

    # **음수 구역은 절댓값에 적용한다.** 부호는 그 구역이 글자로 갖고 있다 —
    # `\(#,##0\)` 는 괄호로, `-* #,##0_-` 는 앞의 빼기 글자로. 값에 붙은 부호를
    # 그대로 두면 `(-1,520)` 이 되고, 두 구역이 같은 `#,##0;#,##0` 은 엑셀이
    # 일부러 부호를 감춘 서식인데 `-1,520` 이 나간다.
    value = abs(v) if idx == 1 else v

    # **장식을 걷어낸 **뒤에** General·@ 인지 본다.** 앞에서는 원문과 글자 그대로
    # 견주었는데, `[Red]General` 처럼 안 보이는 대괄호가 앞에 붙으면 그 비교가 어긋나
    # 아래 「리터럴만인 구역」으로 떨어졌다 — 그러면 **서식 글자 자체가 셀 값으로**
    # 나갔다(`[Red]General` → `General`, known=True). 구역 고르기를 넣으면서 생긴
    # 후퇴다: 그전에는 known=False 로 세어지던 자리다.
    normalized = _strip_invisible_decorations(section, literals)
    if normalized in ("General", "@"):
        return _render_general(value)

    if not _NUMERIC_CORE_RE.search(normalized):
        # 숫자 자리표시자가 하나도 없는 구역 — 화면에는 리터럴만 나온다. 회계 서식의
        # 0 구역 `_-* "-"_-` 가 그렇다(화면은 `-` 한 글자다). `?` 는 채울 숫자가
        # 없으니 자리만 잡는 것이라 `_` 패딩과 같이 뺀다(`_-* "-"??_-` → `-`).
        #
        # **빈 구역(`#,##0;;` 의 음수·0)도 여기로 온다** — 리터럴이 하나도 없는
        # 리터럴 구역이라 빈 문자열이 나오고, 그게 곧 화면이다. 따로 분기를 두었다가
        # 되돌려 봤는데 결과가 같아 뺐다. 증명할 수 없는 분기는 안 남긴다.
        if not _is_literal_only(normalized, literals):
            return fallback, False, R_FMT_LITERAL
        return _lower_literals(normalized.replace("?", ""), literals), True, None

    core = _extract_numeric_core(normalized)

    if core is not None:
        prefix, core_str, suffix = core
        prefix = _lower_literals(prefix, literals)
        suffix = _lower_literals(suffix, literals)
        d = _to_decimal(value)
        if d is None:
            return fallback, False, R_NUM_UNREADABLE
        # **천단위 쉼표는 서식이 시킬 때만 찍는다.** 자리표시자 뭉치에 `,` 가 있어야
        # 묶는 것인데(`#,##0`), 예전에는 `f"{q:,.0f}"` 로 늘 찍었다 — 그래서 `0` 이나
        # `0.0` 처럼 쉼표가 없는 서식에서도 `38417` 이 `38,417` 으로, `31682.4` 가
        # `24,947.3` 으로 나갔다(실물 대조에서 14셀). 화면에 없는 글자를 넣은 것이라
        # 값을 지어낸 것과 같다.
        group = "," if "," in core_str else ""
        m = _PCT.match(core_str)
        if m:
            digits = len(m.group(2) or "")
            # 100을 곱한 **뒤에** 반올림한다 — 순서가 바뀌면 0.25 가 0.2%→25% 로 어긋난다.
            # scaleb 은 지수만 옮기므로 곱셈에서 자리수를 잃지 않는다.
            q = _quantize(d.scaleb(2), digits)
            if q is None:
                return fallback, False, R_ROUND_AMBIGUOUS
            return f"{prefix}{q:{group}.{digits}f}%{suffix}", True, None
        m = _DEC.match(core_str)
        if m:
            digits = len(m.group(1))
            q = _quantize(d, digits)
            if q is None:
                return fallback, False, R_ROUND_AMBIGUOUS
            return f"{prefix}{q:{group}.{digits}f}{suffix}", True, None
        if _INT.match(core_str):
            q = _quantize(d, 0)
            if q is None:
                return fallback, False, R_ROUND_AMBIGUOUS
            return f"{prefix}{q:{group}.0f}{suffix}", True, None
    return fallback, False, R_NUM_PLACEHOLDER


def _is_unknown_builtin(cell):
    """서식 번호는 0 이 아닌데 openpyxl 이 `'General'` 로 돌려준 셀인가.

    **`number_format` 이 `General` 이라는 말은 믿을 수 없을 때가 있다.** openpyxl 은
    자기 표에 있는 내장 서식만 글자로 풀어 주고 **모르는 번호는 조용히 `'General'`
    로 돌려준다.** 그런데 27~36·50~58번은 한국·일본어 로케일 전용 내장 날짜 서식이라
    그 표에 없다 — 31번이 `yyyy"년" mm"월" dd"일"` 이다.

    그대로 믿으면 날짜 일련번호가 그냥 숫자로 나간다. 실물에서 실제로 그랬다:
    어느 사업장의 산정내역 엑셀 `대출이자` 시트 P13 은 화면이 `2025년 03월 14일`
    인데 우리는 `45730` 을 known=True 로 냈다. 서식 번호가 0(진짜 General)이
    아니면 우리가 그 서식을 모르는 것이므로 해석하지 않는다.
    """
    if (cell.number_format or "General") != "General":
        return False  # openpyxl 이 글자로 풀어 준 서식 — 그건 그대로 믿는다
    try:
        return cell._style.numFmtId != 0
    except AttributeError:
        return False  # 스타일을 못 읽는 셀(StubCell 등)은 예전대로


def _is_plain_general(number_format):
    """서식이 「구역 하나짜리 General·@」인가 — 즉 값을 감추거나 꾸미지 않는가."""
    raw_fmt = number_format or "General"
    if _PUA_RE.search(raw_fmt):
        return False
    lifted_all, literals = _lift_literals(raw_fmt)
    sections = _split_sections(lifted_all)
    if len(sections) != 1 or _unhandled_bracket(sections[0]):
        return False
    return _strip_invisible_decorations(sections[0], literals) in ("General", "@")


def _render_text(v, number_format):
    """문자 셀 한 칸을 (표시 문자열, 서식을 해석했나) 로.

    서식의 **네 번째 구역**이 문자 구역이다. 없으면 화면은 그 글자 그대로라 예전처럼
    원문을 돌려주면 되지만, 있으면 그 구역이 글자를 감싸거나 감춘다 —
    `#,##0;;;"[["@"]]"` 는 화면에 `[[본문]]` 을 찍는데 우리는 `본문` 만 내보내면서
    known=True 를 달았다. `@` 가 글자가 들어갈 자리다.
    """
    text = v.strip()
    raw_fmt = number_format or "General"
    if _PUA_RE.search(raw_fmt):
        return text, False, R_FMT_PUA

    lifted_all, literals = _lift_literals(raw_fmt)
    sections = _split_sections(lifted_all)
    if len(sections) < 4:
        return text, True, None  # 문자 구역이 없다 — 화면은 글자 그대로다
    if len(sections) > 4 or any(_unhandled_bracket(s) for s in sections):
        return text, False, R_TEXT_SECTION

    section = sections[3]
    if section == "":
        return "", True, None  # 문자를 안 보이게 한 것 — 화면이 정말로 빈칸이다
    normalized = _strip_invisible_decorations(section, literals)
    if normalized.count("@") != 1:
        # 글자가 들어갈 자리가 없거나 둘 이상이다 — 어디에 놓을지 믿을 수 없다.
        return text, False, R_TEXT_AT
    at = normalized.index("@")
    prefix = _lower_literals(normalized[:at], literals)
    suffix = _lower_literals(normalized[at + 1 :], literals)
    return f"{prefix}{text}{suffix}", True, None


def render_value_reason(cell):
    """(표시 문자열, 서식을 해석했나, 못 했다면 그 사유)

    **known=True 는 닫힌 목록이다.** 아래에 형(type)이 이름으로 적힌 것만 「화면에
    보이는 값 그대로」로 인정하고, 그 밖의 무엇이든 맨 아래 기본 분기에서
    known=False 로 떨어진다.

    **왜 이 방향인가.** 예전에는 반대였다 — 마지막 줄이 «못 알아본 것은 전부
    `str(v)` + known=True» 여서, openpyxl 이 돌려줄 수 있는데 아무도 생각 못 한
    형이 전부 조용히 「맞는 값」으로 선언됐다. 같은 결함이 다섯 번 모습만 바꿔
    나왔다: 날짜(`2026-04-21 00:00:00`) · 회계 서식 · 단위 접미사 · 서식 문자로
    된 리터럴 · 경과시간과 참거짓(`1 day, 12:00:00` · `True`). known=True 로
    나간 틀린 값은 봇이 「확인된 값」으로 인용하고 아무 데서도 안 걸린다. 반대로
    known=False 는 `unformatted_cells` 로 세어져 사람이 아카이브 전에 본다.
    그래서 **모르는 것은 세어지는 쪽으로** 기울여 둔다.
    """
    v = cell.value

    if v is None:
        return "", True, None  # 빈 셀은 화면에서도 빈칸이다 — 이건 정말로 아는 값
    if _is_unknown_builtin(cell):
        # openpyxl 이 못 푼 내장 서식이라 `General` 로 위장돼 있다 (위 설명 참조).
        # 무슨 서식인지 모르므로 어떤 형이든 여기서 세는 쪽으로 보낸다.
        return (v.strip() if isinstance(v, str) else _raw_text(v)), False, R_UNKNOWN_BUILTIN
    if isinstance(v, str):
        return _render_text(v, cell.number_format)  # 서식의 네 번째 구역이 문자 구역이다
    if isinstance(v, bool):
        # bool 은 int 의 하위 클래스라 숫자 분기보다 **먼저** 걸러야 한다. 엑셀 화면은
        # 대문자 TRUE/FALSE 로 찍는다 — 파이썬의 'True'/'False' 가 아니다.
        #
        # **다만 서식을 안 보고 돌려주면 안 된다.** `;;;` 은 화면을 빈칸으로 만드는
        # 서식인데 여기서 그냥 나가면 `TRUE` 가 known=True 로 찍힌다. 참거짓에 숫자
        # 서식이 어떻게 먹는지는 다루지 않으므로, **구역이 하나이고 그것이
        # General·@ 일 때만** 아는 값으로 본다.
        plain = _is_plain_general(cell.number_format)
        return ("TRUE" if v else "FALSE"), plain, None if plain else R_BOOL_FORMAT
    if isinstance(v, datetime.timedelta):
        # 경과 시간(`[h]:mm:ss` 류). **일부러 known=False 로 둔다.** 엑셀은 24시간을
        # 넘겨 누적해 `36:00:00` 으로 찍는데 파이썬은 `1 day, 12:00:00` 으로 찍어
        # 둘이 다르고, 경과시간 서식은 `[h]:mm`·`[mm]:ss`·`[h]:mm:ss.00` 처럼 변종이
        # 여럿이라 제대로 하려면 known=True 분기를 또 늘려야 한다. 이 변환기가 다루는
        # 재무 자료에 기간 셀이 나온 적이 없어, 쓸 일 없는 분기를 늘려 틀릴 자리를
        # 만드는 것보다 세어서 사람에게 보이는 편이 낫다.
        return str(v).strip(), False, R_TIMEDELTA
    if isinstance(v, datetime.time) and not isinstance(v, datetime.date):
        # 시간만 있는 셀 — 지원 범위 밖(요구사항은 날짜 패턴까지). 원값 + 해석 못 함.
        return str(v).strip(), False, R_TIME
    if isinstance(v, datetime.date):  # datetime.datetime 도 datetime.date 의 하위 클래스다
        rendered = _render_date(v, cell.number_format)
        if rendered is not None:
            return rendered, True, None
        return str(v).strip(), False, R_DATE_FORMAT
    if isinstance(v, (int, float)):
        return _render_number(v, cell.number_format)

    # ── 기본값은 **일부러** known=False 다. 넓히지 말 것. ──
    # 여기까지 왔다는 것은 위 목록에 없는 형이 왔다는 뜻이고, 그 형을 화면에 어떻게
    # 찍는지 우리는 모른다. `str(v)` 는 파이썬이 찍는 모양이지 엑셀 화면이 아니다.
    # **새 형을 known=True 로 올리려면 그 형의 시험을 함께 넣는다** — 「엑셀 화면이
    # 정확히 이 글자」임을 시험이 박아 두지 않은 채 분기만 늘리면, 지금까지 다섯 번
    # 그랬듯 틀린 값이 「확인된 값」 표를 달고 나간다. 이 기본 방향 자체를 지키는
    # 시험이 test_xlsx_to_blocks.py 에 따로 있다(「기본값 방향」).
    return str(v), False, R_UNKNOWN_TYPE


def render_value(cell):
    """(표시 문자열, 서식을 해석했나) — 사유가 필요 없는 자리를 위한 껍데기.

    사유까지 필요하면 `render_value_reason` 을 쓴다. 이 이름을 그대로 둔 것은 시험·검사가
    38곳에서 두 값으로 받고 있어서다 — 사유를 넣자고 그 자리를 전부 흔들 이유가 없다."""
    text, known, _ = render_value_reason(cell)
    return text, known


def _spans(ws):
    """병합 시작 셀 → (colspan, rowspan) · 병합에 먹힌 셀 좌표 집합"""
    starts, covered = {}, set()
    for rng in ws.merged_cells.ranges:
        starts[(rng.min_row, rng.min_col)] = (
            rng.max_col - rng.min_col + 1,
            rng.max_row - rng.min_row + 1,
        )
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if (r, c) != (rng.min_row, rng.min_col):
                    covered.add((r, c))
    return starts, covered


def sheet_to_table(ws, unformatted=None, reasons=None, mask_cols=None, inline=None):
    """숨긴 행·열을 빼고 <table> 로. 앞뒤 빈 행·열은 트림한다.

    `reasons` 에 `collections.Counter` 를 주면 「해석 못 함」을 사유별로도 센다.
    `mask_cols` 에 열 번호 집합을 주면 그 열의 **값만** MASK_TEXT 로 바꾼다 —
    머리글 행은 안 바꾼다. 어떤 정보가 있었는지는 남아야 봇이 "가려져 있습니다"로
    답할 수 있다 (WHK 결정 2026-08-23).
    `inline` 에 `unformatted` 와 같은 방식의 한 칸짜리 리스트를 주면, 열 마스킹을
    안 탄 셀에서 `mask_inline` 이 가린 **건수**(가려진 칸 수가 아니라 바꿔치기한
    횟수 — 한 칸에 이메일과 전화가 함께 있으면 2로 센다, 값이 궁금한 것은 "몇 군데
    지웠나"이지 "몇 칸을 건드렸나"가 아니라서다)를 누적한다. 안 줘도 마스킹 자체는
    그대로 되고, 개수만 못 세는 상태로 이전과 똑같이 동작한다 (오너 결정 2026-08-23)."""
    mask_cols = mask_cols or set()
    header_row = find_header_row(ws) if mask_cols else None
    starts, covered = _spans(ws)
    hidden_rows = {i for i, d in ws.row_dimensions.items() if d.hidden}
    hidden_cols = {
        c
        for letters, d in ws.column_dimensions.items()
        if d.hidden
        for c in range(d.min or 0, (d.max or 0) + 1)
    }

    last_row, last_col = data_bounds(ws)
    rows = []
    for r in range(1, last_row + 1):
        if r in hidden_rows:
            continue
        cells = []
        for c in range(1, last_col + 1):
            if c in hidden_cols or (r, c) in covered:
                continue
            cell = ws.cell(row=r, column=c)
            text, known, why = render_value_reason(cell)
            if c in mask_cols and r != header_row and text:
                text, known, why = MASK_TEXT, True, None
            elif text:
                text, n = mask_inline(text)
                if inline is not None:
                    inline[0] += n
            if not known:
                if unformatted is not None:
                    unformatted[0] += 1
                if reasons is not None and why:
                    reasons[why] += 1
            cspan, rspan = starts.get((r, c), (1, 1))
            cells.append((text, cspan, rspan))
        if any(t for t, _, _ in cells):
            rows.append(cells)

    if not rows:
        return ""

    out = ["<table>"]
    for i, cells in enumerate(rows):
        tag = "th" if i == 0 else "td"
        parts = []
        for text, cspan, rspan in cells:
            attrs = ""
            if cspan > 1:
                attrs += f' colspan="{cspan}"'
            if rspan > 1:
                attrs += f' rowspan="{rspan}"'
            parts.append(f"<{tag}{attrs}>{html.escape(text)}</{tag}>")
        out.append("<tr>" + "".join(parts) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def _missing_formula_cells(path):
    """수식인데 엑셀이 캐시한 값이 없는 셀 수.

    **openpyxl 값으로는 판정할 수 없다.** `data_only=True` 는 서로 다른 두 상태에
    똑같이 None 을 돌려준다 — ① 캐시가 아예 없는 것과 ② **계산했는데 결과가 빈
    문자열**인 것. ②는 `IF(A="","",…)` 나 빈 칸을 찾아온 VLOOKUP 이 내는 정상적인
    값이라, 값 하나로 판정하면 멀쩡한 파일이 통째로 막힌다 (2026-08-25 실측: 막혀
    있던 8건 364셀이 **전부** ②였다).

    그래서 시트 XML 을 직접 본다. 가르는 것은 `<v>` 의 유무와 셀 타입이다:

      <f/> 만 있고 <v> 없음        → 캐시 없음
      <v> 에 내용 있음             → 값 있음
      <v> 가 비었는데 t="str"      → 계산된 빈 문자열 (정상)
      <v> 가 비었는데 t 가 숫자    → 캐시 없음 (openpyxl 이 쓰는 자리표시가 이 모양)

    마지막 줄이 중요하다 — openpyxl 로 만든 수식 셀은 `<v />` 를 이미 달고 나오므로
    「<v> 가 있으면 값이 있다」로 두면 진짜 캐시 없는 파일을 놓친다.
    """
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    n = 0
    with zipfile.ZipFile(path) as z:
        sheets = [x for x in z.namelist()
                  if x.startswith("xl/worksheets/sheet") and x.endswith(".xml")]
        for name in sheets:
            # iterparse + clear 로 훑는다 — 큰 시트에서 트리를 통째로 들고 있지 않으려고.
            with z.open(name) as fh:
                for _, el in ET.iterparse(fh, events=("end",)):
                    if el.tag != f"{ns}c":
                        continue
                    if el.find(f"{ns}f") is not None:
                        v = el.find(f"{ns}v")
                        if v is None or ((v.text or "") == "" and el.get("t") != "str"):
                            n += 1
                    el.clear()
    return n


# 유령 행 자르기 --------------------------------------------------------------
#
# 값 없이 서식만 있는 <row> 가 100만 줄 붙은 파일이 실물에 있다 —
# `사업장가 요약본-260824.xlsx` 의 `정산` 시트가 1,047,802행인데 값이 있는 행은
# 71행까지뿐이고, 압축을 풀면 456MB · <c> 1,677만 개라 load_workbook 이 6GB 를
# 먹는다. 그 시트를 로드 전에 잘라 낸다.
#
# **2026-08-26 에 고친 `data_bounds` 의 유령 셀과 다른 고장이다.** 그쪽은 유령 셀
# 하나가 `max_row` 를 끌어올려 시트가 상한에 걸리던 것이고 고친 자리는 「재는
# 자리」다. `data_bounds` 는 `ws` 를 받으므로 로드가 끝난 뒤에만 도는데, 이번에
# 터지는 자리는 **로드 그 자체**라 닿지 못한다.

# **대소문자를 가리지 않는다.** ECMA-376 은 열 글자를 대문자로 적지만 `a1` 처럼
# 소문자로 쓴 파일이 실제로 돌아다닌다(엑셀 아닌 도구가 쓴 것). 못 알아보면 그
# 참조가 구조물 바닥 셈에서 빠져 **바닥이 낮게 나오고**, 낮은 바닥은 아직 쓰이는
# 행을 잘라 버린다 — 틀리는 방향이 안전한 쪽이 아니라서 여기만은 넓게 잡는다.
_ROW_IN_REF = re.compile(r"[A-Z]+(\d+)", re.IGNORECASE)


def _row_refs(text):
    """`"A1:BR71"`·`"AO8 B2"` 같은 참조 문자열에서 행 번호를 뽑는다."""
    return [int(n) for n in _ROW_IN_REF.findall(text or "")]


def _sheet_bottoms(z, sheet_name):
    """(값 바닥, 구조물 바닥, 마지막 <row> 의 r, `r` 없는 <row> 가 있나).

    **값 바닥** — `<v>` 나 `<is>` 가 든 `<row>` 중 가장 아래.
    **구조물 바닥** — 표에 안 보이지만 행을 가리키는 것 중 가장 아래. 셋에서 모은다:
      ① 시트 XML 의 `<sheetData>` 바깥에서 `ref`·`sqref` 를 가진 태그 **전부**
      ② 시트 rels 가 가리키는 `xl/comments*.xml` 의 `<comment ref="…">`
      ③ 같은 rels 가 가리키는 `xl/tables/table*.xml` 의 `ref`

    **①에서 태그를 이름으로 열거하지 않는다.** 병합셀·하이퍼링크·데이터유효성·
    조건부서식·자동필터를 목록으로 두면 새 태그가 생겼을 때 조용히 빠지고, 그러면
    아직 쓰이는 자리를 잘라 버린다. 전부 보고 `dimension` 하나만 뺀다 — 그것은
    「여기까지 있다」는 신고일 뿐이고 지금 고치려는 대상이다.

    **네 번째 값(`r` 없는 <row>)을 여기서 함께 본다.** 이 시트를 어차피 한 번
    훑으므로 따로 한 벌 더 돌 이유가 없고, **자르기 전에** 알아야 헛된 자르기를
    안 한다. 무엇을 막는 값인지는 `trim_ghost_rows` 의 거절 자리에 적어 두었다.
    """
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    last_value = last_anchor = last_row = 0
    unnumbered = False

    with z.open(sheet_name) as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            tag = el.tag
            if tag == f"{ns}row":
                # `r` 이 없거나 숫자가 아니면 **위치로 번호가 정해지는 행**이다.
                # 0 으로 치고 넘어가면 「가장 위」로 잘못 읽히므로 사실만 적어 둔다.
                ra = (el.get("r") or "").strip()
                r = int(ra) if ra.isdigit() else 0
                if not ra.isdigit():
                    unnumbered = True
                if r > last_row:
                    last_row = r
                for c in el:
                    if c.find(f"{ns}v") is not None or c.find(f"{ns}is") is not None:
                        if r > last_value:
                            last_value = r
                        break
                el.clear()
                continue
            if tag in (f"{ns}c", f"{ns}v", f"{ns}f", f"{ns}is", f"{ns}t"):
                continue
            if tag == f"{ns}dimension":
                el.clear()
                continue
            for attr in ("ref", "sqref"):
                for r in _row_refs(el.get(attr)):
                    if r > last_anchor:
                        last_anchor = r
            el.clear()

    for target, part in _sheet_related_parts(z, sheet_name):
        try:
            body = z.read(part).decode("utf-8", "ignore")
        except KeyError:
            # 못 찾았다고 조용히 넘어가면 안 된다 — 이 자리를 놓치면 구조물 바닥이
            # 조용히 낮게 나오고, 그러면 실제 값이 있는 행까지 잘려 나간다.
            # 여기서 멈추자는 것은 아니다: rels 가 가리키는 부속 하나가 없다고
            # 변환 전체를 세울 이유는 없다. 대신 무엇을 못 찾았는지 stderr 에 남겨
            # 「셈에 안 들어갔다」가 보이게 한다.
            print(
                f"경고: {sheet_name} 의 rels 가 가리키는 부속을 못 찾았습니다 — "
                f"Target={target!r} → {part!r} (zip 에 없음)",
                file=sys.stderr,
            )
            continue
        for m in re.finditer(r'\bref="([^"]+)"', body):
            for r in _row_refs(m.group(1)):
                if r > last_anchor:
                    last_anchor = r

    return last_value, last_anchor, last_row, unnumbered


def _sheet_related_parts(z, sheet_name):
    """이 시트의 rels 가 가리키는 comments·tables 부속의 (원문 Target, zip 항목 이름)."""
    base = sheet_name.rsplit("/", 1)[-1]
    rels = f"xl/worksheets/_rels/{base}.rels"
    if rels not in z.namelist():
        return []
    try:
        root = ET.fromstring(z.read(rels))
    except ET.ParseError as e:
        # 못 읽었다고 조용히 넘어가면 안 된다 — 이 rels 가 가리키는 메모·표를
        # **하나도** 못 보게 되어 구조물 바닥이 조용히 낮게 나오고, 그러면 아직
        # 쓰이는 행까지 잘려 나간다. 아래 KeyError 자리와 같은 고장이고, 거기서
        # 멈추지 않는 이유도 같다: 부속 하나가 깨졌다고 변환 전체를 세울 이유는
        # 없다. 대신 무엇을 못 읽었는지 stderr 에 남겨 「셈에 안 들어갔다」가
        # 보이게 한다.
        print(
            f"경고: {rels} 를 못 읽어 {sheet_name} 의 메모·표 참조를 셈에 "
            f"못 넣습니다 — {e!r} — 구조물 바닥이 낮게 나와 아직 쓰이는 행이 "
            f"잘릴 수 있습니다",
            file=sys.stderr,
        )
        return []
    out = []
    for rel in root:
        target = (rel.get("Target") or "").replace("\\", "/")
        if "comments" not in target and "/tables/" not in target:
            continue
        # Target 은 대개 시트 기준 상대경로다 — `../comments1.xml` → `xl/comments1.xml`.
        # 다만 `/` 로 시작하면 OOXML 스펙상 패키지 루트 기준이라 (openpyxl 이 이렇게
        # 쓴다 — `/xl/comments/comment1.xml`), 그럴 땐 앞에 시트 경로를 붙이지 않는다.
        parts = target.split("/") if target.startswith("/") else ("xl/worksheets/" + target).split("/")
        stack = []
        for p in parts:
            if p == "..":
                if stack:
                    stack.pop()
            elif p not in ("", "."):
                stack.append(p)
        out.append((target, "/".join(stack)))
    return out


def _count_value_cells_stream(fh):
    """열린 파일류에서 값이 있는 `<c>` 의 개수를 센다 — 자르기 전후 대조의 본체.

    **아래 두 이름은 이 함수의 얇은 껍데기여야 하고, 절대 두 벌로 갈리면 안 된다.**
    안전망(`trim_ghost_rows` 의 `before != after`)은 **두 셈이 정확히 같은 규칙**일
    때만 성립한다. 한쪽만 고쳐 규칙이 갈리면 개수가 늘 어긋나거나 늘 맞아떨어져
    **에러 없이 안전망만 조용히 꺼진다** — 이 저장소는 같은 모양(봇 메시지 판정이
    두 곳에 살다 갈린 일)에 이미 한 번 데었다. 그래서 셈은 여기 한 벌만 둔다.

    `<row>` 도 비운다 — `<c>` 만 `clear()` 하면 다 센 자식이 부모에 붙은 채 쌓여
    456MB 짜리 시트에서 그것이 곧 메모리다(`_sheet_bottoms` 가 이미 이렇게 한다).
    """
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    n = 0
    for _, el in ET.iterparse(fh, events=("end",)):
        if el.tag == f"{ns}c":
            if el.find(f"{ns}v") is not None or el.find(f"{ns}is") is not None:
                n += 1
            el.clear()
        elif el.tag == f"{ns}row":
            el.clear()
    return n


def _count_value_cells(z, sheet_name):
    """그 시트에서 값이 있는 `<c>` 의 개수. 자르기 전후 대조에 쓴다."""
    with z.open(sheet_name) as fh:
        return _count_value_cells_stream(fh)


def _count_value_cells_bytes(raw):
    """`_count_value_cells` 와 **같은 셈**을 바이트열에서 한다 (자른 결과 대조용)."""
    return _count_value_cells_stream(io.BytesIO(raw))


_ROW_START = re.compile(rb"<row\b")
_ROW_R = re.compile(rb'\br="(\d+)"')
# ECMA-376 은 불리언을 `1`/`0` 또는 `true`/`false` 어느 쪽으로도 적는다. `"1"`만
# 찾으면 `hidden="true"` 로 저장한 파일에서 숨긴 꼬리 행이 세어지지 않은 채로
# 잘려 나가 `hidden_rows` 가 조용히 준다 — 바로 WHK 판정이 막으려던 그 드리프트다.
_ROW_HIDDEN = re.compile(rb'\bhidden="(?:1|true)"')


def _strip_rows(raw, keep_upto):
    """`r > keep_upto` 인 `<row>` 만 버린 시트 XML. `<dimension>` 끝 행도 고친다.

    돌려주는 것은 `(자른 바이트열, 버린 행 수, 그중 숨긴 행 수)` 다. **버린 행
    수를 `last_row - keep_upto` 처럼 행 번호 차로 셈하지 않는다** — 시트는
    성기어서(유령 행이 4~49행처럼 번호가 안 이어질 수 있다), 그 차는 실제로
    지운 `<row>` 개수보다 커질 수 있다. 여기서 하나씩 세면서 직접 센 개수만
    정확하다.

    **`<row r="N+1"` 을 찾아 `</sheetData>` 까지 통째로 건너뛰지 않는다.**
    그 방식은 행이 번호순이라고 가정하는데, 어긋난 파일이 오면 **값을 조용히
    버린다.** 행 하나씩 보는 것은 순서와 무관하게 안전하다.

    **메모리를 안 쓰는 것은 아니다.** 이 함수는 시트 XML 을 통째로 `bytes` 로
    받고(실물 456MB) 남길 것을 `bytearray` 에 다시 쌓으므로, 한때 원본과 결과를
    함께 들고 있다. 결과는 버린 행만큼 작아지지만 최대치는 원본의 두 배 남짓이다.
    같은 파일에 `load_workbook` 이 6GB 를 쓰는 것에 비하면 감당할 수 있는 값이라
    이렇게 둔다 — 「메모리를 안 쓴다」고 읽고 더 큰 파일을 이 길로 보내면 안 된다.

    **`r` 이 없는 <row> 를 만나는 일은 여기서 없다.** 그런 시트는 `trim_ghost_rows`
    가 아예 안 자르고 되돌려 보낸다(그 자리의 주석에 이유가 있다). 아래
    `r = ... if rm else 0` 은 그 관문이 뚫렸을 때의 마지막 보루라 **남기는 쪽**으로
    둔 것이지, 이 함수가 그 경우를 제대로 다룰 수 있다는 뜻이 아니다.
    """
    out = bytearray()
    pos = 0
    dropped = 0
    dropped_hidden = 0
    while True:
        m = _ROW_START.search(raw, pos)
        if not m:
            out += raw[pos:]
            break
        out += raw[pos:m.start()]
        # 이 <row> 의 끝을 찾는다 — 빈 행은 `<row .../>`, 아니면 `</row>`.
        gt = raw.index(b">", m.start())
        if raw[gt - 1:gt] == b"/":
            end = gt + 1
        else:
            end = raw.index(b"</row>", gt) + len(b"</row>")
        chunk = raw[m.start():end]
        head_end = chunk.index(b">") + 1
        rm = _ROW_R.search(chunk, 0, head_end)
        r = int(rm.group(1)) if rm else 0
        if r <= keep_upto:
            out += chunk
        else:
            dropped += 1
            if _ROW_HIDDEN.search(chunk, 0, head_end):
                dropped_hidden += 1
        pos = end

    body = bytes(out)
    # `<dimension>` 이 없는 시트도 있다. 그때는 만들지 않는다 — openpyxl 은 그
    # 태그를 안 읽고, 고치는 것은 다른 도구가 이 파일을 열 때를 위한 예의다.
    body = re.sub(
        rb'(<dimension ref="[A-Z]+\d+:[A-Z]+)\d+(")',
        lambda mm: mm.group(1) + str(keep_upto).encode() + mm.group(2),
        body,
        count=1,
    )
    return body, dropped, dropped_hidden


def _sheet_titles(z):
    """{zip 항목 이름: 사람이 읽는 시트 이름}.

    자르기는 `xl/worksheets/sheet2.xml` 로 도는데 meta.json 과 점검표는 `정산`
    을 써야 한다. 못 찾으면 zip 항목 이름을 그대로 쓴다 — 지어내지 않는다.
    """
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {
            r.get("Id"): (r.get("Target") or "").replace("\\", "/")
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        }
    except (KeyError, ET.ParseError) as e:
        # workbook.xml 이 없거나 깨진 파일은 이름 말고도 문제가 더 있을 것이다 —
        # 그래도 변환을 세우지 않고 zip 항목 이름으로 내려가되, 그 이름이
        # 「지어낸」 정상 이름이 아니라 낮춘 이름이라는 것은 반드시 알려야 한다.
        print(
            f"경고: xl/workbook.xml 을 못 읽어 시트 이름을 못 되찾습니다 — "
            f"{e!r} — 이후 시트 이름은 zip 항목 이름(예: sheet2.xml)으로 대신합니다",
            file=sys.stderr,
        )
        return {}
    out = {}
    # `find()` 결과가 없으면 `None`, 있으면 자식 없는 빈 엘리먼트일 수도 있어
    # 엘리먼트 자체의 참거짓으로 판정하면(미래 파이썬에서 늘 True) 잘못 걸린다 —
    # `is not None` 으로 명시한다.
    sheets_el = wb.find(f"{ns}sheets")
    for s in (sheets_el if sheets_el is not None else []):
        target = rels.get(s.get(f"{rns}id"), "")
        if not target:
            continue
        name = target.lstrip("/")
        if not name.startswith("xl/"):
            name = "xl/" + name
        out[name] = s.get("name") or name
    return out


def _count_rows_over(z, sheet_name, floor):
    """그 시트에서 `r > floor` 인 `<row>` 의 실제 개수.

    `untrimmed_sheets` 의 `ghost_rows` 에 쓴다 — **부르는 쪽은 `floor` 에 늘 값
    바닥을 준다.** 그 한 가지 뜻(=「값 바닥 아래에 있는 행 수」)으로 못 박은
    이유는 `trim_ghost_rows` 의 두 갈래가 붙는 자리에 적어 두었다.

    **`last_row - floor`(행 번호
    차)를 그대로 쓰지 않는다** — `dropped`(자른 시트의 실제로 지운 행 수)와
    똑같은 실수를 반복하게 된다: 시트가 성기면(예: 유령 행이 4~49행처럼 번호가
    안 이어지면) 번호 차가 실제 행 수보다 커진다. 같은 이름(`ghost_rows`)이
    자른 시트의 `dropped`·`hidden_dropped`처럼 「실제로 존재하는 <row> 개수」를
    뜻해야 사람이 두 표를 나란히 읽어도 헷갈리지 않는다.

    `_sheet_bottoms` 는 바닥(가장 큰 r)만 재고 개수는 안 세므로, 이미 한 번
    지나간 시트 XML 을 한 번 더 스트리밍으로 훑는다. 자를 수 없다고 판정된
    시트(대개 파일에서 가장 큰 시트)에서만 부르므로 시간은 배로 들지만, 메모리는
    `_sheet_bottoms` 와 똑같이 한 행씩 보고 바로 버려(`el.clear()`) 늘지 않는다.
    """
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    n = 0
    with z.open(sheet_name) as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            if el.tag == f"{ns}row":
                if int(el.get("r") or 0) > floor:
                    n += 1
                el.clear()
    return n


def trim_ghost_rows(path, tmp_dir):
    """값 없는 꼬리 `<row>` 를 잘라낸 사본을 만든다.

    돌려주는 것은 `(로드할 경로, trimmed, untrimmed, hidden_dropped)` 다.
    **자를 것이 한 시트도 없으면 사본을 안 만들고 원본 경로를 그대로 돌려준다**
    — 평상시 엑셀은 지금과 완전히 같은 경로로 돈다.

    안 자르기로 하는 갈래가 셋이다 — `r` 없는 `<row>` 가 있는 시트, 구조물이
    꼬리 끝까지 걸친 시트, 자른 뒤 값 셈이 어긋나 되돌린 시트. 셋 다
    `untrimmed_sheets` 에 사유와 함께 남는다.

    `hidden_dropped` 는 `{시트 이름: 잘려 나간 숨긴 행 수}` 다. 잘려 나가는 행은
    정의상 값이 없지만 **숨김(hidden) 표시만 있는 행**일 수 있다 — 그 행이
    잘리면 나중에 `ws.row_dimensions` 에서도 사라져 `build_blocks` 가 「숨긴 행」
    개수를 셀 때 못 본다. 자르기 전 개수를 지키려면 여기서 잘려 나간 숨긴 행
    수를 세어 돌려줘야 한다(WHK 판정 2026-08-27).
    """
    trimmed, untrimmed, plan = [], [], {}
    hidden_dropped = {}

    with zipfile.ZipFile(path) as z:
        titles = _sheet_titles(z)
        sheets = [n for n in z.namelist()
                  if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
        for name in sheets:
            last_value, last_anchor, last_row, unnumbered = _sheet_bottoms(z, name)
            if last_row <= last_value:
                continue  # 유령 행이 없다
            keep = max(last_value, last_anchor)
            title = titles.get(name, name)
            # **`untrimmed_sheets` 의 `ghost_rows` 는 아래 세 갈래에서 뜻이 하나다 —
            # 「값 바닥 아래에 있는 <row> 개수」.** 갈래마다 「자를 수 있었던 행 수」로
            # 재면 같은 표의 같은 칸이 갈래에 따라 다른 것을 세게 되고, 표를 읽는
            # 사람은 어느 쪽인지 알 방법이 없다. 그래서 늘 값 바닥을 기준으로 센다.
            if unnumbered:
                # **`r` 이 없는 <row> 가 하나라도 있으면 이 시트는 아예 안 자른다.**
                # `r` 은 스펙상 선택이고, 없는 행은 **앞에 몇 개가 있었나로 번호가
                # 정해진다**. 그래서 앞쪽 행을 지우면 그 행이 **다른 행 번호로
                # 옮겨간다** — 값은 그대로라 개수 대조(4절 안전망)에 안 걸린다.
                # 개수 대조가 증명하는 것은 「값이 안 지워졌다」뿐이고 「값이 안
                # 옮겨졌다」는 증명하지 못한다. 숫자가 조용히 다른 줄로 가는 것은
                # 이 변환기가 낼 수 있는 가장 나쁜 결과이고(봇이 그 표를 인용한다),
                # 안 자르면 지금까지와 똑같은 상태일 뿐이라 잃는 것이 없다.
                # 그러니 「r 이 없으면 0 으로 쳐서 남기면 되지 않나」로 되돌리지 말 것 —
                # 남기는 것만으로는 **남은 행이 밀리는 것**을 못 막는다.
                untrimmed.append({
                    "name": title,
                    "ghost_rows": _count_rows_over(z, name, last_value),
                    "why": "`r` 이 없는 <row> 가 있다 — 앞에 몇 행이 있나로 번호가 "
                           "정해지는 행이라, 자르면 그 행이 다른 행 번호로 밀린다",
                })
                continue
            if keep >= last_row:
                untrimmed.append({
                    "name": title,
                    "ghost_rows": _count_rows_over(z, name, last_value),
                    "why": f"표에 안 보이는 참조가 {last_anchor}행까지 걸쳐 있다",
                })
                continue
            plan[name] = (keep, title, last_value)

        if not plan:
            return Path(path), trimmed, untrimmed, hidden_dropped

        out = Path(tmp_dir) / Path(path).name
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
            for info in z.infolist():
                if info.filename not in plan:
                    w.writestr(info, z.read(info.filename))
                    continue
                keep, title, last_value = plan[info.filename]
                raw = z.read(info.filename)
                cut, n_dropped, n_hidden_dropped = _strip_rows(raw, keep)
                before = _count_value_cells(z, info.filename)
                after = _count_value_cells_bytes(cut)
                if before != after:
                    # **걸리면 결함이다.** 값 없는 행만 버리므로 개수는 늘 같아야
                    # 한다. 그 시트만 원본으로 되돌리고 사실을 남긴다 — 파일 전체를
                    # 포기하면 멀쩡한 시트까지 로드가 못 버티는 상태로 돌아간다.
                    untrimmed.append({
                        # `ghost_rows` 는 위 두 갈래와 **같은 것**을 센다 — 값 바닥
                        # 아래의 <row> 개수. 여기서 `n_dropped`(실제로 자른 행 수)를
                        # 쓰면 스캔은 아끼지만 같은 칸이 갈래마다 다른 것을 세게
                        # 된다(자른 행은 구조물 바닥 아래만이라 값 바닥 기준보다
                        # 적다). 이 갈래는 애초에 결함일 때만 밟으므로 스캔 한 번이
                        # 아깝지 않다.
                        "name": title,
                        "ghost_rows": _count_rows_over(z, info.filename, last_value),
                        "why": f"자른 뒤 값 있는 셀 수가 달라져 되돌렸다 "
                               f"(원본 {before} · 자른 뒤 {after})",
                    })
                    w.writestr(info, raw)
                    continue
                trimmed.append({
                    "name": title, "kept": keep, "dropped": n_dropped,
                })
                if n_hidden_dropped:
                    hidden_dropped[title] = n_hidden_dropped
                w.writestr(info.filename, cut)

    if not trimmed:
        # 계획은 있었는데 전부 되돌아갔다 — 사본을 쓸 이유가 없다.
        return Path(path), trimmed, untrimmed, hidden_dropped
    return out, trimmed, untrimmed, hidden_dropped


def sheet_comments(ws, mask_cols=None):
    """이 시트에서 **실제로 실리는 자리**의 셀 메모만 [(셀주소, 작성자, 본문), …] · 버린 개수.

    엑셀 셀 메모는 `<table>` 어디에도 안 나오는데, 내용은 값이 **왜 그 값인지**다
    (실측: 「회수가능액= 예상토지매각가 + PFV 잔존시재 전체」·「EOD 2/3 초일산입,
    말일불산입」·「26.05.21. 기준 (26.05.22. 대체공휴일)」). SKILL.md 가 워드 주석에
    대해 적어 둔 함정과 같은 자리다 — 본문은 멀쩡한데 그 파일을 올린 이유가 빠진다.

    거르는 기준은 `sheet_to_table` 과 **같아야 한다**. 숨긴 행·숨긴 열은 표에서 빠지므로
    거기 달린 메모도 뺀다 — 안 그러면 표에 없는 줄의 주석만 떠다니고, 읽는 사람은
    그 값이 어디 있는지 영영 못 찾는다.

    `mask_cols` 에 열 번호 집합을 주면 그 열에 달린 메모는 통째로 뺀다 — 표 값은
    `***` 로 가렸는데 메모에 원문이 그대로 남으면 가린 의미가 없다(WHK 결정
    2026-08-23). 버린 개수는 두 번째 값으로 돌려준다.

    본문은 엑셀이 `"작성자:\\n내용"` 으로 저장해 작성자가 한 번 더 들어 있다. 그대로
    쓰면 「심현준 · 심현준: …」 이 되므로 접두어를 뗀다.
    """
    mask_cols = mask_cols or set()
    hidden_rows = {i for i, d in ws.row_dimensions.items() if d.hidden}
    hidden_cols = {
        c
        for _letters, d in ws.column_dimensions.items()
        if d.hidden
        for c in range(d.min or 0, (d.max or 0) + 1)
    }
    out = []
    dropped = 0
    for cell in ws._cells.values():
        if cell.comment is None or cell.row in hidden_rows or cell.column in hidden_cols:
            continue
        if cell.column in mask_cols:
            dropped += 1
            continue
        author = (cell.comment.author or "").strip()
        text = " ".join((cell.comment.text or "").split())
        if author and text.startswith(f"{author}:"):
            text = text[len(author) + 1:].strip()
        if text:
            out.append((cell.coordinate, author, text))
    return out, dropped


def comments_section(items):
    """메모를 회차 본문 끝에 붙일 절로. 없으면 빈 문자열(절 자체를 안 만든다)."""
    if not items:
        return ""
    lines = [
        "",
        "### 시트 메모",
        "",
        "원본 엑셀 셀에 달린 메모다. 표 안에는 보이지 않는다.",
        "",
    ]
    for coord, author, text in items:
        who = f" [{html.escape(author)}]" if author else ""
        lines.append(f"- **{coord}**{who} {html.escape(text)}")
    return "\n".join(lines) + "\n"


def build_blocks(path, date, source, max_rows):
    # **원본을 본다.** 자른 사본을 주면 잘려나간 자리의 「수식인데 캐시 값이 없는
    # 셀」이 안 세어져 EmptyFormulaError 판정이 조용히 달라진다.
    empty = _missing_formula_cells(path)
    # 임시 파일은 이 `with` 안에서만 산다 — load_workbook 이 끝나면 사라지므로
    # `.blocks/` 에 낯선 파일이 안 남고, 중간에 터져도 안 남는다.
    with tempfile.TemporaryDirectory() as _td:
        load_path, trimmed_sheets, untrimmed_sheets, hidden_dropped = trim_ghost_rows(path, _td)
        wb = openpyxl.load_workbook(load_path, data_only=True)

    if empty:
        raise EmptyFormulaError(
            f"수식인데 엑셀이 캐시한 값이 없는 셀 {empty}개입니다. "
            "LibreOffice 로 한 번 다시 저장한 뒤 변환하세요"
        )

    visible = [ws for ws in wb.worksheets if ws.sheet_state == "visible"]
    hidden_sheets = [ws.title for ws in wb.worksheets if ws.sheet_state != "visible"]

    kept, skipped, unmaskable = [], [], []
    for ws in visible:
        n_rows, _n_cols = data_bounds(ws)
        if n_rows > max_rows:
            skipped.append({"name": ws.title, "rows": n_rows})
            continue
        # 머리글을 1차·2차 다 못 찾았는데 **라벨 칸에** 주소·생년월일·계좌가
        # 있으면 가릴 방법이 없다. 마스킹 없이 싣는 것보다 빼는 쪽이 낫다 —
        # 조용히 실리는 것이 가장 나쁜 실패다.
        if find_header_row(ws) is None:
            why = sheet_drop_reason(ws)
            if why:
                unmaskable.append({"name": ws.title, "why": why})
                continue
        kept.append(ws)

    # 같은 앞말의 날짜 시트가 2장 이상이면 가장 최근 1장만 남긴다. 파일 사이에서
    # 「최신 1건만」을 쓰는 것과 같은 규칙을 파일 안에도 적용하는 것이다
    # (설계 2026-08-23). 2026-08-23 실측: 일보 한 건에 `상담(MMDD)` 가 30장 있었다.
    #
    # **연도는 `sheet_date_key` 가 모른다 — 여기서 파일 날짜를 기준으로 붙인다.**
    # `상담(MMDD)` 는 연도가 없는 표기라, 해를 넘겨 쌓인 파일(예: 파일 날짜가
    # 2026-01-07 인데 `상담(1230)` 과 `상담(0105)` 이 함께 있는 경우)에서 zero-padded
    # 문자열만으로 정렬하면 `"1230" > "0105"` 라 12월이 최신으로 잘못 뽑히고, 실제로
    # 더 최근인 1월 5일 시트가 접혀 사라진다. 시트의 MMDD 가 파일 날짜의 MMDD 보다
    # **미래**면 그 시트는 전년도로 본다(WHK 판정 2026-08-24). 지금 아카이브된 일보는
    # 4~7월 안에서만 움직여 아직 이 경계를 안 밟았지만, 하루 한 장씩 계속 쌓이는
    # 자료라 해를 넘기는 것은 시간 문제였다.
    #
    # 이 판정을 `sheet_date_key` 에 넣지 않고 호출부에 둔 이유: `sheet_date_key` 는
    # 파일 날짜 없이 시트 이름 하나만 보고 혼자 시험할 수 있어야 한다(실제로 위
    # 시험이 그렇게 부른다) — 파일 날짜를 그 함수 인자로 만들면 그 독립성이 깨진다.
    file_year = int(date[:4])
    file_mmdd = date[5:7] + date[8:10]

    # **숫자로만 된 이름은 날짜가 아니라도 이 규칙을 통과한다** — `단가(0715)` 같은
    # 가격·버전 표기가 실제 날짜 시트와 구별할 방법이 없어 함께 접힌다(WHK 판정
    # 2026-08-24, 리뷰 지적 accepted·코드로 막지 않음). 조용한 유실은 아니다 — 접힌
    # 시트는 전부 `meta["folded_date_sheets"]` 에 이름과 「무엇으로 대체됐는지」가
    # 남아, 문서를 커밋하기 전에 사람이 그 목록을 보고 판단한다. 판별 규칙을 더
    # 추가하지 않는다 — 추측으로 가려내면 진짜 날짜 시트를 놓치는 쪽이 더 위험하다.
    by_stem = collections.defaultdict(list)
    for ws in kept:
        key = sheet_date_key(ws.title)
        if key:
            stem, mmdd = key
            year = file_year - 1 if mmdd > file_mmdd else file_year
            by_stem[stem].append(((year, mmdd), ws))
    folded = []
    drop = set()
    for stem, items in by_stem.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda t: t[0], reverse=True)
        keep_title = items[0][1].title
        for _yd, ws in items[1:]:
            drop.add(ws.title)
            folded.append({"name": ws.title, "kept": keep_title})
    if drop:
        kept = [ws for ws in kept if ws.title not in drop]

    unformatted = [0]
    reasons = collections.Counter()
    hidden_rows = 0
    hidden_with_content = 0
    n_comments = 0
    blocks = []
    masked_columns_seen = set()
    not_personal_seen = set()
    masked_rows = 0
    masked_comments_dropped = 0
    masked_inline = [0]
    header_fallback = []
    for i, ws in enumerate(kept, start=1):
        # 숨긴 행은 「빈 줄」과 「내용이 있는데 접어둔 줄」이 섞여 있고 뜻이 다르다.
        # 2026-08-22 실측: 어느 사업장의 본 시트는 숨긴 행 65개 중 46개에 내용이 있었고,
        # 그 46개의 가장 큰 덩이가 **폐기된 옛 일정**이었다. 그래서 빼는 것이 맞지만,
        # 몇 줄을 버렸는지는 메타에 적어야 봇이 "없다"가 아니라 "안 실렸다"로 답한다.
        sheet_rows, sheet_cols = data_bounds(ws)
        hid = {r for r, dm in ws.row_dimensions.items() if dm.hidden}
        # **자르기 전 숫자를 지킨다(WHK 판정 2026-08-27).** 유령 행 자르기가 값
        # 없는 숨긴 꼬리 행을 지우면 `ws.row_dimensions` 에서도 사라져 여기서
        # 못 센다. 잘려 나간 만큼을 되돌려 더한다 — 7.5 점검표의 「숨긴 행
        # N(내용 있는 것 M)」은 사람이 보는 숫자라, 자르기를 시작했다고 그 뜻이
        # 조용히 바뀌면 안 된다. 「내용 있는 것」쪽은 안 건드린다 — 잘리는 행은
        # 정의상 값이 없어(그래서 잘렸다) 내용 있는 쪽으로 셀 일이 없다.
        hidden_rows += len(hid) + hidden_dropped.get(ws.title, 0)
        for r in hid:
            if any(
                ws.cell(row=r, column=c).value not in (None, "")
                for c in range(1, sheet_cols + 1)
            ):
                hidden_with_content += 1
        # 2차로 잡은 것은 이름을 남긴다 — 낱말 하나로 잡은 머리글이라
        # 커밋 전에 사람이 그 시트만 열어 보게 하려는 것이다 (7.5 점검표).
        header_row, header_how = find_header_row_detail(ws)
        if header_how == "fallback":
            header_fallback.append(ws.title)
        mask_map = masked_columns(ws, header_row)
        for name in mask_map.values():
            masked_columns_seen.add(name)
        for name in not_personal_columns(ws, header_row).values():
            not_personal_seen.add(name)
        if mask_map:
            # 표를 다시 그려 세지 않는다 — 마스킹 열만 훑으면 된다(831행 시트에서
            # 표 렌더를 두 번 하는 비용을 없앤다). 「가려진 행」은 **마스킹 열에
            # 실제 값이 있는 행**만이다 — 마스킹 열이 전부 비어 있으면 그 행은
            # 안 가려진 채 그대로 실리므로 세면 안 된다 (WHK 결정 2026-08-23).
            for r in range(1, sheet_rows + 1):
                if r == header_row or r in hid:
                    continue
                if any(
                    ws.cell(row=r, column=c).value not in (None, "")
                    for c in mask_map
                ):
                    masked_rows += 1
        memos, dropped = sheet_comments(ws, mask_cols=set(mask_map))
        masked_comments_dropped += dropped
        n_comments += len(memos)
        blocks.append(
            {
                "index": i,
                "total": len(kept),
                "name": ws.title,
                "header": HEADER_FMT.format(
                    date=date, source=source, i=i, n=len(kept), name=ws.title
                ),
                "body": sheet_to_table(ws, unformatted, reasons, set(mask_map), masked_inline)
                        + comments_section(memos),
            }
        )

    # **이름을 못 되찾은 시트 몫도 반드시 더한다.** `_sheet_titles` 가 실패하면
    # (workbook.xml 이 깨졌거나 그 시트의 r:id 가 rels 에 없으면) `hidden_dropped`
    # 의 키가 사람이 읽는 이름이 아니라 zip 항목 이름(`xl/worksheets/sheet1.xml`)
    # 이라, 위 루프의 `ws.title` 과 **영영 안 맞는다.** 그러면 잘려 나간 숨긴 행이
    # 통째로 안 세어져 `hidden_rows` 가 조용히 준다 — 자르기가 사람이 보는 숫자를
    # 바꾸지 않기로 한 약속(WHK 판정 2026-08-27)이 경고 한 줄 뒤에서 깨진다.
    # 어느 시트 것인지는 못 밝히더라도 **개수는 지킨다.**
    _titles_seen = {ws.title for ws in wb.worksheets}
    hidden_rows += sum(v for k, v in hidden_dropped.items() if k not in _titles_seen)

    meta = {
        "sheets": [b["name"] for b in blocks],
        "hidden_sheets": hidden_sheets,
        "hidden_rows": hidden_rows,
        "hidden_rows_with_content": hidden_with_content,
        "unformatted_cells": unformatted[0],
        # 개수 옆의 「무엇이」. 많은 사유부터 적어 사람이 위에서부터 본다.
        "unformatted_reasons": dict(reasons.most_common()),
        "skipped_sheets": skipped,
        "unmaskable_sheets": unmaskable,
        # 낱말 1개짜리로 잡은 머리글. **에러가 아니라 「봐 달라」다** —
        # 진짜 머리글이 이 행보다 아래에 있으면 그 열을 못 가린 채 실린다.
        "header_fallback_sheets": header_fallback,
        "folded_date_sheets": folded,
        # **실은 것만 센다.** 숨긴 시트·숨긴 행의 메모는 표와 같은 기준으로 빠지므로
        # 여기 넣으면 「담았다」와 「원본에 있다」가 섞여 점검표가 거짓말을 한다.
        "comments": n_comments,
        "masked": {
            "columns": sorted(masked_columns_seen),
            "rows": masked_rows,
            "comments_dropped": masked_comments_dropped,
            # 열 마스킹을 안 탄 셀 안에서 mask_inline 이 가린 건수 — 전 시트 합계.
            "inline": masked_inline[0],
            # 가릴 낱말이 들었지만 `NOT_PERSONAL_HEADERS` 로 일부러 안 가린 열.
            "not_personal": sorted(not_personal_seen),
        },
    }
    # **빈 배열이면 키를 안 만든다.** 「자를 것이 없었다」와 「이 변환기는 자르기를
    # 모른다(옛 변환분)」를 구별하기 위해서다 — 7.5 점검표가 옛 meta.json 에
    # 「사유 미기록 — 옛 변환분」이라고 적는 것과 같은 이유다.
    if trimmed_sheets:
        meta["trimmed_sheets"] = trimmed_sheets
    if untrimmed_sheets:
        meta["untrimmed_sheets"] = untrimmed_sheets
    return blocks, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--date", required=True, help="회차 날짜 (YYYY-MM-DD)")
    ap.add_argument("--source", help="원본 파일명. 생략하면 --file 의 이름")
    # 안 주면 **원본 파일 바로 옆**에 만든다. 원본 경로가 이미 채널과 파일 ID 를
    # 갖고 있어(`~/.doc-cache/<채널>/<파일ID>_<이름>.xlsx`) 같은 이름 파일이 두
    # 채널에 있어도 안 겹친다. 사람이 경로를 안 쳐도 되고, 쳐서 틀릴 일도 없다 —
    # 7.5 점검표가 여기서 meta.json 을 찾으므로 한 글자만 달라도 점검이 통째로
    # 눈이 먼다. 시험은 임시 폴더를 명시적으로 준다.
    ap.add_argument("--out-dir", help="생략하면 <--file 경로>.blocks")
    # 실측(docs/superpowers/plans/2026-08-21-hermes-excel-실측.md 118~126행)이 정한 값이다.
    # 1,000행이면 사업장가 산정내역의 모든 시트(최대 541행)가 남고, 사업장나 5.1MB 파일에서도
    # 집계 시트인 `통합`(816행)·`3.호실별TM`(809행)은 남되 계약자 전원을 나열한 원장
    # `1-1.계약자list`(1,379행)만 빠진다. 400 으로 두면 그 집계 시트들이 함께 떨어진다.
    ap.add_argument("--max-rows", type=int, default=1000)
    a = ap.parse_args()

    src = a.source or Path(a.file).name
    blocks, meta = build_blocks(Path(a.file), a.date, src, a.max_rows)

    out = Path(a.out_dir) if a.out_dir else Path(str(a.file) + ".blocks")
    out.mkdir(parents=True, exist_ok=True)
    # 줄바꿈을 **LF 로 못박는다.** `write_text` 를 그냥 쓰면 윈도우에서 `\r\n` 이 되고,
    # SKILL.md 의 삽입 루프가 `head -n 1 | sed` 로 헤더에서 뽑는 `--source` 끝에 `\r` 이
    # 딸려 붙는다. 그러면 `insert_entry.py` 의 중복 판정(`already_has`, 119행)이 영영
    # 안 맞아 **루프를 다시 돌릴 때마다 같은 시트가 통째로 다시 들어간다** — 에러도
    # 경고도 없이 회차만 두 배가 된다. `insert_entry.py:47` 이 같은 함정을 이미
    # 바이트로 읽고 쓰는 방식으로 막아 두었고, 여기서도 같은 방식을 쓴다.
    for b in blocks:
        (out / f"sheet-{b['index']:02d}.md").write_bytes(
            f"{b['header']}\n\n{b['body']}\n".encode("utf-8")
        )
    (out / "meta.json").write_bytes(
        json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
    )
    print(f"시트 {len(blocks)}개 → {out}")
    for k, v in meta.items():
        if v:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
