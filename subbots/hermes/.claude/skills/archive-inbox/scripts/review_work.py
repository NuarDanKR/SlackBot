#!/usr/bin/env python3
"""
07:00 자동 반영이 남긴 「사람이 손대야 하는 것」을 건별로 보인다.

목록은 VM 이 쓴다 (`src/ingest/pending-work.js` → `.pending-work.json`).
이 스크립트는 **읽고 보이기만 한다** — 아무것도 고치지 않는다. 고치는 것은 사람이고,
결정을 기록하는 것은 `decide_work.py` 다.

사용:
  python review_work.py              # 목록 (요약 항목에는 원문을 붙인다)
  python review_work.py --json       # 기계용 (decide_work.py 가 쓴다)

종료코드: 0

── 왜 원문을 붙이나 ──

DM 이 지목한 요약 불일치는 **후보이지 근거가 아니다.** 모델이 요약과 대화를 읽고
"어긋난다"고 말한 것이고, 그 문구가 원문과 다른 일이 실제로 있었다 — 2026-08-12 에
DM 은 「내부 승인 진행」이라 적었으나 원문은 「진행예정」이었다(5건 중 2건). DM 은
120자에서 잘려 그 자리에서는 확인할 방법도 없다. 그래서 판정 전에 원문을 붙인다.

── 화면을 두 덩이로 나누는 이유 ──

doc-archive 7.5 점검표의 ⚠ 는 셀 수 있는 것(글자 수·마스킹 누락)이라 표만 보면 판정이
끝난다. 요약 불일치는 원문을 열어야 끝난다. 같은 표에 ✓ 와 함께 두면 **원문 확인이
필요한 줄이 필요 없는 줄처럼 보인다.** 그래서 요약 쪽에는 ✓ 를 쓰지 않는다 —
그 블록은 전 항목이 「원문 볼 것」이다.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1] / "slack-sync" / "scripts"))
sys.path.insert(0, str(_HERE.parents[1] / "doc-archive" / "scripts"))
sys.path.insert(0, str(_HERE.parents[1] / "_shared"))

from paths import CODE_ROOT  # noqa: E402
from sync_index import ARCHIVE, CHANNELS, ROOT, STATE  # noqa: E402,F401
# 만기 판정은 **문서·수정삭제 쪽과 같은 함수를 그대로 쓴다.** 옮겨 적으면 갈리고,
# 갈리면 한쪽은 「N건 남음」 한쪽은 「0건」이라고 알린다 (fetch_slack_files.py 참조).
from fetch_slack_files import active_deferred, today_kst  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

PENDING_WORK = ARCHIVE / ".pending-work.json"
PENDING_EDITS = ARCHIVE / ".pending-edits.json"
# 「이 PC 에서 사람이 보고 정했다」는 기록. 저장소에 안 들어간다 (.gitignore).
STAMP = ARCHIVE / ".decision-stamp.json"
CHANNELS_REL = "slack-export/channels"

# 아카이브 md 포맷은 계약이다 (`src/archive.js` 가 같은 것을 판다).
HEADER_RE = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) · (.+?)\*\*\s*$")
MONTH_RE = re.compile(r"^##\s+\d{4}-\d{2}\s*$")
# `derive.js`·`sync_index.py` 가 자동으로 갱신하는 건수 줄
COUNT_RE = re.compile(r"^>\s*\*\*기간\*\*:.*\*\*실제 메시지\*\*:")

_Q = "\"“”‘’'"
QUOTE_RE = re.compile(f"[{_Q}]([^{_Q}]{{8,}})[{_Q}]")
# 모델이 **원문에 없는 글자로 이어 붙인 자리.** 인용을 여기서 쪼갠다 (`_needles` 참조).
#
# 둘이다 — ① 가운데를 건너뛴 표시 `...`·`…` ② 여러 줄을 한 인용으로 이을 때 줄바꿈과
# 글머리표 자리에 들어가는 ` / `. **둘 다 원문에는 없다.**
#
# ②를 2026-09-10 에 더했다. 슬랙에서 항목을 나열하면 줄마다 `· ` 로 시작하는데, 모델이 두
# 줄을 이어 인용하면 그 자리가 ` / ` 가 된다. 원문의 그 자리는 `·`(U+00B7)이고 그것은
# **문장 구분자라 접기에서 일부러 안 지운다**(`_fold` 참조) — 그래서 접기만으로는 영영
# 안 걸리고 항목이 「근거 못 찾아 뺀 후보」로 밀린다. SKILL 이 그 문구를 「대개 빼」로
# 읽으라고 안내하므로 **멀쩡한 근거가 버려진다.** `#사업장가` 회차에서 실제로 그랬고
# 사람이 Grep 해서야 알았다. 다섯째 원인(백슬래시 따옴표)과 마찬가지로 **파이썬·JS 가
# 함께 틀려서 `check-shared-rules.js` 로는 안 잡혔다.**
#
# **`/` 는 앞뒤가 공백일 때만 이음매로 본다.** 붙여 쓴 것은 날짜(`10/23`)나 `채권/채무`
# 처럼 **원문의 글자**라, 쪼개면 조각이 8자 밑으로 부서져 되레 못 찾는다.
#
# **JS `summary.js` 의 `JOIN_RE` 와 같아야 한다** — 갈리면 `check-shared-rules.js` 가 잡는다.
JOIN_RE = re.compile(r"\.{3,}|…+|\s+/\s+")

# 실제로 찾을 문자열의 최소 길이 — JS `summary.js` 의 `MIN_FOLDED` 와 같아야 한다
MIN_FOLDED = 6

SUMMARY_KINDS = {"summary", "dropped"}

KIND_LABEL = {
    "summary": "요약 불일치",
    "dropped": "근거 못 찾아 뺀 후보",
    "derive": "파생값 — 기계가 못 고침",
    "new-channel": "새 채널",
    "renamed": "채널 개명",
    "note": "확인할 것",
    "error": "반영 실패 — 자동 반영이 못 한 것",
    "undeclared-private": "비공개인데 선언 안 됨",
}


def is_count_line(line: str) -> bool:
    """자동으로 갱신되는 건수 줄인가.

    `derive.js`·`sync_index.py` 가 회차마다 다시 세어 고치는 값이라 사람 승인의 대상이
    아니다. 관문에서 이걸 안 빼면 `sync_index.py` 를 돌린 뒤 커밋이 매번 막힌다."""
    return bool(COUNT_RE.match(line))


def _fold(s: str) -> str:
    """비교용 정규화 — 공백·강조 기호·곧은 따옴표를 지우고 소문자로 맞춘다.

    **생산 쪽 `summary.js` 의 `fold` 와 글자 하나까지 같아야 한다.** 저쪽은 관문이다 —
    `verifyEvidence` 가 이 판정으로 「근거가 원문에 있나」를 보고 없으면 항목을 아예
    내보내지 않는다. 이쪽은 그 항목의 원문을 화면에 찾아 준다. 둘이 갈리면
    **관문은 통과시켰는데 화면은 「원문에서 찾지 못했습니다」라고 하는** 조합이 생기고,
    스킬 문서는 그 문구를 「대개 빼」로 읽으라고 적고 있어 멀쩡한 항목이 버려진다.

    2026-08-13 까지 이쪽은 **공백만** 지웠다. 아카이브 전량으로 재 보니 모델이 표시를
    다듬어 인용하는 경우에 백틱 440건 · 별표 141건 · 따옴표 25건이 그 상태였다.
    맞춘 뒤 0건이 됐고, 대신 첨부 파일명처럼 30자 안에서 구별이 안 되는 인용 1건에서
    3분 차이 메시지가 잡힌다 (WHK 결정 2026-08-13 — 606건 대 1건).

    지우는 따옴표는 **곧은 것(`"`·`'`)뿐이다.** JS 쪽 문자 집합을 코드포인트로 확인했다
    (`22 22 27 27`) — 둥근 따옴표는 양쪽 다 남긴다. `QUOTE_RE` 의 `_Q` 는 인용을 **끊는**
    글자라 둥근 것까지 넣으며, 그것과 이것은 다른 판정이다.

    **글머리표 `•`·`◦`·`∙` 도 지운다** (2026-08-20). 슬랙에서 항목을 나열하면 `• ` 로
    시작하는 줄이 되는데, 모델이 앞 줄과 이어 인용하면 그 표시가 빠진 문장이 된다 —
    `<회의와 별도 내용>` 다음 줄이 `• 레지던스 위탁운영계약에…` 인 자리에서 실제로
    밀렸다(#사업장사, 2026-08-20 07:00 회차. 인용이 하나뿐이라 그대로 `dropped` 로
    갔고 사람이 Grep 해서야 원문이 있는 것을 알았다).

    **`·`(U+00B7)는 안 지운다.** 아카이브 전량 실측으로 `•` 는 305회 중 261회(86%)가
    줄머리라 사실상 글머리표뿐이지만, `·` 는 2,295회 중 줄머리가 9회(0.4%)뿐인 **문장
    구분자**다. 지우면 서로 다른 문장이 같은 값으로 접혀 엉뚱한 원문을 보여준다 —
    「못 찾음」보다 나쁜 쪽이다. 같은 이유로 `-`(5,268회)도 안 지운다.

    **백슬래시 `\\` 도 지운다** (2026-09-11, 할 일 185 의 다섯째 원인). 모델은 인용을
    `\\"확인\\"` 처럼 이스케이프해 적는데 그 백슬래시는 원문에 없어서, 근거가 원문에
    멀쩡히 있어도 접은 값이 갈려 **관문이 항목을 버린다.**

    아카이브 전량 실측(2026-09-11): 5,895회가 전부 `documents/` 에 있고 슬랙 채널
    md 에는 0회다. 5,779회(98.0%)는 별표·백틱·물결·밑줄 **바로 앞**에 붙은 것인데
    그 네 글자는 접기가 이미 지우므로, 백슬래시만 찌꺼기로 남아 **지금도** 값을
    가르고 있었다.

    **공짜가 아니다.** 앞뒤가 둘 다 접기에서 살아남는 `A\\B` 모양이 **95회(1.6%)**
    있고, 그 자리의 백슬래시는 지우면 뜻이 사라진다. 내역은 **원화 기호 75회** ·
    표 구분자 `\\|` 6회 · 수식 깨짐(`$\\frac}$`) 9회 · 그 밖 5회다. 원화 기호는 한글
    코드페이지가 `₩` 을 U+005C 로 저장해서 생긴 것으로, 감정평가서가
    `육백오십억이천삼백만원정 (\\65,023,000,000.-)` 처럼 적고 있다(원문 코드포인트로
    확인). **그래서 `₩65,023,000,000` 과 `65,023,000,000` 이 이제 같은 값으로 접힌다.**
    윈도우 경로(`C:\\`)는 0회다 — 그건 `A\\B` 의 한 종류가 없다는 뜻일 뿐 위험이
    없다는 뜻이 아니다. 98% 를 고치려고 1.6% 의 통화 기호 구별을 내준 맞바꿈이다.

    갈렸는지는 `scripts/check-shared-rules.js` 가 같은 입력을 양쪽에 먹여 본다."""
    return re.sub(r"[\s`*_~\"'•◦∙\\]", "", str(s or "")).lower()


def _needles(quote: str) -> list:
    """인용 하나에서 원문에 있을 법한 조각들을 뽑는다.

    **모델은 인용 가운데를 `...`·`…` 로 건너뛰어 적는다.** 그 표시는 「여기서 중간을
    뺐다」는 뜻이라 **원문에는 없다.** 인용이 통째로 이어져 있다고 치고 찾으면 원문
    어디에도 없어 「못 찾음」이 되고, 스킬 문서는 그 문구를 「근거가 원문에 없으니 대개
    빼」로 읽으라고 적고 있다 — 즉 멀쩡한 근거가 버려진다. 인용이 둘 이상인 항목에서는
    다른 인용이 걸려 가려지므로 **인용이 하나뿐인 항목에서만 드러난다**
    (실측: 과거 목록 12건·인용 18건 중 이 모양 2건, 안 걸린 인용 1건, 화면에 드러난 것 0건).

    **조각이 짧으면 버린다.** 짧은 조각은 흔한 낱말이라 엉뚱한 메시지가 걸리는데,
    「못 찾음」보다 나쁜 것은 **틀린 원문을 원문이라고 보여주는 것**이다. 기준은
    `QUOTE_RE` 가 인용으로 인정하는 길이(8자)와 같다 — 같은 판정을 두 값으로 적으면 갈린다.
    조각이 전부 짧으면 아무것도 안 돌려줘 「못 찾음」으로 간다.

    **그 8자는 `_fold` 하기 전에 센다.** `QUOTE_RE` 가 공백을 지우기 전 원문에서 세기
    때문이다. 접은 뒤로 재면 띄어쓴 짧은 인용이 통째로 버려진다 — `약정 체결 업무` 는
    8자라 인용으로 뽑히는데 공백을 지우면 6자다. 아카이브 전량 대조에서 이것 때문에
    3건이 퇴행했고, 그동안 시험은 통과하고 있었다 (시험 ⑮ 가 지금 그것을 지킨다).

    잘라내는 상한 30자는 조각마다 건다. 이어 붙인 본문에서 찾을 때 이 길이가
    「아무거나 걸리지 않게」 하는 제동이라(`context_for` 참조), 조각도 같은 제동을 받아야 한다.

    **그 8자와 별개로 「실제로 찾을 문자열」에도 바닥(`MIN_FOLDED`)을 둔다.** 8자는 접기
    전에 세는데 찾는 것은 접은 뒤라 사이가 벌어진다 — `**공문을** 확인` 은 8자 검사를
    통과하고 5자짜리로 찾는다. 아카이브 전량 실측으로 3~5자는 **같은 채널의 남의
    메시지**에도 22~33% 있어서 엉뚱한 원문을 보여줄 수 있다(8자 17.2% · 20자 6.6%).
    6인 것은 `약정 체결 업무` 가 접으면 6자이기 때문이고, 더 올리면 위 퇴행 3건이 돌아온다."""
    parts = [p for p in JOIN_RE.split(str(quote or "")) if len(p) >= 8]
    return [n for n in (_fold(p)[:30] for p in parts) if len(n) >= MIN_FOLDED]


def md_path(item: dict) -> Path:
    """그 항목이 가리키는 채널 md.

    **채널 이름이 아니라 `file` 을 먼저 본다** — 채널을 개명해도 md 파일명은 그대로
    두기 때문이다 (`report.js` 의 「채널 개명」 블록)."""
    name = item.get("file") or item.get("channel") or ""
    return CHANNELS / f"{name}.md"


def context_for(md: Path, evidence: str, span: int = 10) -> str:
    """근거 인용이 든 메시지 블록을 헤더째 돌려준다.

    **못 찾으면 빈 문자열이 아니라 못 찾았다고 적는다.** 빈 값은 「원문에 없다」와
    「검색이 어긋났다」를 화면에서 같은 모양으로 만든다 — 사람은 근거가 없는 줄 모르고
    DM 문구만 보고 정하게 된다.

    **인용이 걸린 줄은 반드시 화면에 넣는다.** 전에는 창을 메시지 헤더에 고정하고
    `span` 줄에서 잘랐는데, 긴 메시지에서는 **정작 근거 문장이 창 밖으로 밀려났다**
    (2026-08-12 실사용: 22줄짜리 메시지에서 근거 2건이 둘 다 안 보였다). 화면에는 같은
    메시지의 앞부분이 멀쩡히 보여서 사람은 그게 전부인 줄 알고 후보 문구만 보고 정하게
    된다 — 이 도구가 하려는 일과 정반대다. 그래서 `span` 은 이제 「길이 상한」이 아니라
    **「생략 없이 통째로 보여주는 길이」**이고, 넘으면 인용 줄만 남기고 **생략한 줄 수를
    적는다.** 잘렸다는 표시가 없는 것이 잘린 것보다 나쁘다.

    **인용이 여럿이면 전부 찾는다.** 처음 걸린 것에서 멈추면 안 된다 — 모델은 대개
    메시지 제목을 먼저 인용하고 실제 근거를 뒤에 붙여서, 앞엣것만 보면 늘 제목만 보인다.

    **찾는 것은 줄 단위가 아니라 이어 붙인 본문에서다.** 슬랙 원문은 한 문장이 여러 줄에
    걸치는 일이 흔하고 모델은 그것을 이어 한 문장으로 인용한다. 줄 하나씩 보면 그런 인용이
    어느 줄에도 통째로 없어 「못 찾음」이 된다 — 아래 본문의 주석이 그 경위다. 그렇게 찾은
    인용은 **걸친 줄을 전부** 화면에 넣는다. 시작 줄만 넣으면 뒷줄이 「생략」 안으로 접혀,
    바로 위 문단의 사고가 원인만 바꿔 되살아난다."""
    quotes = QUOTE_RE.findall(evidence or "")
    if not quotes:
        return f"(근거에 원문 인용이 없습니다 — 적힌 것: {evidence or '없음'})"
    if not md.exists():
        return f"(채널 md 를 찾지 못했습니다: {md.name})"

    lines = md.read_text(encoding="utf-8").split("\n")
    folded = [_fold(x) for x in lines]

    # **요약 자리는 건너뛴다 — 거기 있는 문장은 원문이 아니다.** 「반영」은 원문 표현을
    # 그대로 요약에 옮겨 적는 일이라, 한 번 반영한 채널에서는 같은 문장이 요약에도 있다.
    # 파일 전체에서 찾으면 요약이 먼저 걸리고, 그 위에는 메시지 헤더가 없어 「원문」이라며
    # 요약과 구분선을 보여주게 된다 (2026-08-12 첫 반영 직후 실제로 발생).
    body_from = _summary_end(lines)

    # **줄을 이어 붙여 찾는다 — 줄 하나씩 보면 안 된다.** 슬랙에서는 항목과 그 설명이
    # 다른 줄에 있는 것이 흔한데(`- 을상사 : 이영희 대표 미팅` 다음 줄에
    # `＊월세 및 보증금 조정 요청에 따른 협의`), 모델은 그 둘을 이어 한 문장으로 인용한다.
    # 그러면 인용이 어느 한 줄에도 통째로 들어 있지 않아 검색이 어긋나고, 화면에는
    # 「원문에서 찾지 못했습니다」가 뜬다. 스킬 문서는 그 문구를 「근거가 원문에 없으니
    # 대개 빼」로 읽으라고 적고 있어서, **멀쩡한 항목이 버려진다**
    # (2026-08-13 실사용: 8건 중 3건이 이렇게 나왔고 셋 다 원문이 있었다).
    #
    # 메시지 경계를 넘어 엉뚱하게 걸릴 걱정은 없다 — 메시지 사이에는 반드시 헤더 줄
    # (`**날짜 시각 · 이름**`)이 있고 그 글자도 이어 붙은 줄기에 함께 들어가므로,
    # 인용이 경계를 넘으려면 헤더 문자열까지 품고 있어야 한다. **헤더를 줄기에서 빼면
    # 이 보호가 사라진다** — 시험 ⑫ 가 그것을 지킨다.
    # 여기에 더해 needle 이 30자로 잘리는 것이 제동을 건다(아래). 인용이 넘을 수 있는
    # 줄 경계가 한둘로 묶여서 「이어 붙이면 아무거나 걸린다」가 안 된다 — 30 을 크게
    # 올리려는 사람은 이 맞바꿈이 여기 있다는 것을 알고 올려야 한다.
    body_join = "".join(folded[body_from:])
    line_of: list = []              # 이어 붙인 글자 위치 → 줄 번호
    for i in range(body_from, len(folded)):
        line_of.extend([i] * len(folded[i]))

    # `hits` 는 인용마다 **시작 줄** 하나다 — 아래 `elsewhere` 가 이것을 「인용 건수」로
    # 세므로 걸친 줄을 여기 늘려 담으면 그 문장이 줄 수를 세는 말로 바뀐다. 걸친 범위는
    # 화면 창을 잡을 때만 쓰므로 `spans` 에 따로 둔다.
    hits: list = []
    spans: list = []                # 인용 하나가 걸친 줄 범위 (첫 줄, 끝 줄)
    # **조각마다 「원문에 있었나」를 따로 기억한다.** 관문(`summary.js` 의 `verifyEvidence`)은
    # 조각 하나만 걸려도 인용을 통과시키는데(`needles(q).some(...)`), 화면은 걸린 쪽의 원문
    # 창만 보여준다. 그래서 **앞 절반은 실존하고 뒤 절반은 지어낸 인용**이 들어오면, 사람이
    # 아침 보고에서 보는 것은 멀쩡한 원문뿐이고 못 찾은 조각이 있다는 사실 자체가 화면에
    # 안 나온다 — 관문도 사람도 못 거른다. 관문 규칙을 어떻게 정하든 **못 찾은 조각은 사람
    # 눈에 보여야 한다** (WHK 2026-09-11, 할 일 185-곁).
    #
    # **다만 이 표시는 관문 기준의 「하한」이다 — 건초더미가 서로 다르다.** 여기는 채널 md
    # 전체를 뒤지고 관문은 **그 회차에 들어온 대화**만 본다(`pending-work.js` 의 `buildItems`
    # 가 같은 비대칭을 적어 뒀다 — 「뺐다」가 「원문에 없다」와 같은 말이 아닌 이유다).
    # 그래서 관문이 부분 일치로 통과시킨 인용이라도 그 조각이 **같은 채널의 딴 날 메시지**에
    # 있으면 여기서는 경고가 안 붙는다. **「경고가 없다」를 「조각이 다 실존한다」로 읽으면
    # 안 된다** (SKILL.md 에 같은 한계를 적어 뒀다).
    missing: list = []
    for q in quotes:
        claimed = False
        for needle in _needles(q):
            at = 0
            seen = False                # 이 조각이 본문 어딘가에서 한 번이라도 걸렸나
            while True:
                pos = body_join.find(needle, at)
                if pos < 0:
                    break
                i = line_of[pos]
                if MONTH_RE.match(lines[i]):
                    # 월 헤딩은 원문이 아니다. 줄 하나씩 찾던 때는 헤딩을 넘는 needle 을
                    # 만들 수 없어 닿지 않았는데, 줄을 이어 붙이면서 닿게 됐다. 걸리면
                    # 블록이 헤딩 한 줄로 줄어드는 데서 안 끝난다 — 헤딩은 본문 맨 앞줄이라
                    # `first = min(hits)` 가 그것을 고르고 **같은 항목의 진짜 근거까지 덮는다.**
                    at = pos + 1
                    continue
                # 월 헤딩이 아닌 자리에서 걸렸다 = 이 조각은 원문에 있다. 아래에서 `hits`
                # 에 안 담길 수도 있지만(같은 줄을 다른 인용이 이미 차지했을 때) 그것은
                # 「화면 창을 누가 잡나」의 문제이지 「원문에 있나」의 답이 아니다.
                seen = True
                end_i = line_of[min(pos + len(needle) - 1, len(line_of) - 1)]
                if not claimed and i not in hits:
                    # 인용 하나당 `hits` 는 한 줄 — 아래 `elsewhere` 가 이것을 인용 건수로 센다
                    hits.append(i)
                    spans.append((i, end_i))
                    claimed = True
                    break
                if claimed:
                    # 같은 인용의 뒷조각. 줄은 새로 안 담고 창만 넓힌다
                    spans.append((i, end_i))
                    break
                at = pos + 1
            if not seen and needle not in missing:
                missing.append(needle)
    if not hits:
        return f"(원문에서 찾지 못했습니다 — 근거: {quotes[0][:40]}…)"

    # 블록 = 첫 인용이 든 메시지의 헤더부터 다음 헤더 앞까지.
    # 헤더까지 거슬러 올라간다 — 언제·누가가 있어야 원문으로 쓸 수 있다.
    first = min(hits)
    start = next((j for j in range(first, -1, -1) if HEADER_RE.match(lines[j])), None)
    if start is None:
        # 위로 올라가도 메시지 헤더가 없다 = 첫 메시지보다 앞이다. 언제·누가가 없는 토막을
        # 「원문」이라며 보여주면 사람은 그것을 근거로 삼는다. 못 찾은 것으로 둔다.
        return f"(원문에서 찾지 못했습니다 — 근거: {quotes[0][:40]}…)"
    end = start + 1
    while end < len(lines) and not HEADER_RE.match(lines[end]):
        end += 1
    # 메시지 사이 구분선까지 딸려 오면 원문이 어디서 끝나는지가 흐려진다
    while end - 1 > start and lines[end - 1].strip() in ("", "---"):
        end -= 1

    inside = [i for i in hits if start <= i < end]
    if end - start <= span:
        keep = set(range(start, end))
    else:
        # 헤더 + 인용이 걸친 줄 전부 + 그 앞뒤 한 줄. 인용은 문장 중간에서 끊기는 일이 많다.
        # **걸친 줄을 전부 넣는다** — 시작 줄만 넣으면 여러 줄에 걸친 인용의 뒷줄이
        # 「생략」 안으로 접혀, ⑤ 와 같은 사고가 원인만 바꿔 되살아난다: 화면에는 앞부분이
        # 멀쩡히 보여서 사람은 그게 인용 전부인 줄 안다.
        keep = {start}
        for a, b in spans:
            if start <= a < end:
                keep.update(j for j in range(a - 1, b + 2) if start <= j < end)

    out = []
    prev = None
    for i in sorted(keep):
        if prev is not None and i > prev + 1:
            out.append(f"… ({i - prev - 1}줄 생략)")
        out.append(lines[i])
        prev = i
    if prev is not None and prev < end - 1:
        out.append(f"… ({end - 1 - prev}줄 생략)")

    # 다른 메시지에서 걸린 인용은 조용히 버리지 않는다 — 근거가 여러 메시지에 걸쳐 있다는
    # 사실 자체가 판정에 쓰인다
    elsewhere = len(hits) - len(inside)
    if elsewhere:
        out.append(f"(근거 인용 {elsewhere}건은 다른 메시지에 있습니다 — 채널 md 를 여세요)")

    # **못 찾은 조각은 끝에 밝힌다.** 기존 줄은 하나도 안 바꾸고 뒤에 덧붙이기만 한다 —
    # SKILL 이 위쪽 문구를 읽는다. 적는 값은 **접힌 조각**(공백·강조 기호를 지우고 30자로
    # 자른 것)이라 원문 그대로의 모양이 아니다. 그래서 «» 로 감싸 구별한다.
    if missing:
        out.append("")
        for n in missing:
            out.append(f"⚠️ 이 조각은 원문에 없음: «{n}»")
    return "\n".join(out)


def summary_hash(name: str) -> str:
    """그 채널 요약 자리의 지금 해시.

    **파이썬으로 다시 구현하지 않고 JS 의 같은 함수를 부른다**
    (`src/ingest/pending-work.js` 의 `summaryHash`). 이 값은 「뺌」을 언제 풀지
    판정하는 데 쓰이는데, 판정하는 쪽은 JS(`suppress`)이고 적는 쪽은 파이썬이다.
    두 구현이 한 글자라도 어긋나면 해시가 영영 안 맞아 **「뺌」이 조용히 무효가 된다** —
    에러가 아니라 「뺀 항목이 매일 다시 뜬다」로만 드러난다.

    node 를 못 찾으면 빈 문자열이 아니라 예외를 낸다. 안 맞는 해시를 적는 것보다
    적지 못했다고 멈추는 쪽이 낫다."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        raise RuntimeError("node 를 찾지 못했습니다 — 요약 해시를 낼 수 없습니다.")
    js = (CODE_ROOT / "src" / "ingest" / "pending-work.js").resolve()
    code = "const m = await import(process.argv[2]); process.stdout.write(m.summaryHash(process.argv[1]));"
    r = subprocess.run(
        [node, "--input-type=module", "-e", code, "--", name, js.as_uri()],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        raise RuntimeError(f"요약 해시를 내지 못했습니다: {r.stderr.strip().splitlines()[-1:] or ''}")
    return r.stdout.strip()


def bot_visible_lines(md: Path) -> set:
    """그 md 에서 **봇 프롬프트에 실리는** 줄들 (정규화한 집합).

    **파이썬으로 다시 구현하지 않고 JS 의 같은 함수를 부른다**
    (`src/archive.js` 의 `metaBlock`). 그 규칙은 단순하지 않다 — 상단
    40줄 제한, 시작한 뒤 `>` 아닌 줄에서 중단, 중간 빈 줄은 건너뜀. 흉내 내면
    **봇 가시성 판정이 두 언어로 갈리고, 갈려도 에러가 안 난다.** `summary_hash` 가
    같은 이유로 같은 방식을 쓴다.

    node 를 못 찾으면 빈 집합이 아니라 예외를 낸다 — 표시가 없는 것과 「봇에게 안
    간다」가 화면에서 같은 모양이 되면 안 된다.

    **경로는 `ROOT` 가 아니라 `CODE_ROOT` 기준이다.** `ROOT` 는 자료 저장소를 가리키고
    시험이 그것을 임시 폴더로 갈아끼우는데, 이 JS 는 자료가 아니라 스킬과 함께 깔린
    소스다. **2026-08-31 에 저장소가 둘로 갈리면서 그 둘은 실제로 다른 자리가 됐다** —
    전에는 같은 워크스페이스 안이라 어느 쪽으로 세도 같은 폴더가 나왔다.
    지금은 `_shared/paths.py` 가 둘을 따로 정한다."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        raise RuntimeError("node 를 찾지 못했습니다 — 봇에게 가는 줄을 가릴 수 없습니다.")
    js = (CODE_ROOT / "src" / "archive.js").resolve()
    code = ("const m = await import(process.argv[2]); "
            "process.stdout.write(m.metaBlock(process.argv[1]));")
    r = subprocess.run(
        [node, "--input-type=module", "-e", code, "--", str(md), js.as_uri()],
        capture_output=True, text=True, encoding="utf-8",
    )
    if r.returncode != 0:
        head = (r.stderr or "").strip().splitlines()
        raise RuntimeError(f"metaBlock 호출 실패 — {head[0] if head else r.returncode}")
    return {ln.strip() for ln in r.stdout.split("\n") if ln.strip()}


def norm_summary_line(line: str) -> str:
    """화면에 낸 줄을 `bot_visible_lines` 의 집합과 맞춰 보기 위한 정규화.
    `metaBlock` 이 `>` 와 뒤따르는 공백 하나를 벗기고 `trim()` 하는 것과 맞춘다."""
    return line[1:].strip() if line.startswith(">") else line.strip()


def _still_dismissed(item: dict, rec: dict) -> bool:
    """「뺌」이 아직 유효한가. **JS 의 `suppress` 와 같은 규칙이어야 한다.**

    해시가 안 적힌 것(요약이 아닌 항목)은 되돌아올 근거가 없으므로 계속 뺀다."""
    if rec.get("summaryHash") in (None, ""):
        return True
    return rec["summaryHash"] == summary_hash(item.get("file") or item.get("channel") or "")


def applied_after(rec: dict, generated: str) -> bool:
    """「반영」으로 정한 것이 **이 목록보다 나중**인가.

    반영한 항목은 목록에서 빠져야 한다. 전에는 `.decision-stamp.json` 을 목록이 아예
    안 읽어서, 반영·커밋·push 를 끝낸 4건이 그대로 다시 보였다 (2026-08-12 첫 실사용).

    **무조건 감추면 안 된다** — 내일 같은 자리가 다시 어긋나도 영영 안 보이게 된다.
    그래서 목록이 만들어진 시각과 견준다: 목록보다 나중에 정한 것이면 그 목록에 대한
    결정이니 감추고, VM 이 목록을 새로 만들면(그때도 어긋나 있으면) 다시 보인다.
    「빼」가 요약 해시로 되돌아오는 것과 같은 생각이고, 여기서는 새 값을 안 만들어도
    이미 있는 두 시각으로 갈린다.

    **못 읽으면 감추지 않는다.** 안 보이는 쪽으로 틀리면 사람이 영영 모른다."""
    from datetime import datetime

    def parse(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            return None

    at, gen = parse((rec or {}).get("at")), parse(generated)
    if at is None or gen is None or at.tzinfo is None or gen.tzinfo is None:
        return False
    return at > gen


def confirmed_at(item: dict, generated: str) -> str:
    """그 항목을 **마지막으로 다시 본 시각**. 위 `applied_after` 에 넘길 값이다.

    `applied_after` 가 목록 전체의 `generated` 와 견주던 것이 **요약 항목에만 맞았다.**
    그 판정의 전제는 「도장이 새로워졌다 = 그 항목을 새 증거로 다시 봤다」인데,
    요약이 아닌 항목(새 채널·개명·note·파생값)은 `pending-work.js` 의 `mergeItems` 에서
    **무조건 이월된다** — 다시 안 잡히므로 치우면 그대로 사라지기 때문이다. 그런데
    07:00 회차는 대조를 했으므로 도장을 **정당하게** 앞당긴다. 그러면 반영을 끝낸
    항목이 다음 날 아침에 그대로 다시 뜬다. **에러는 안 난다.**

    `30655cf`(대조를 안 한 회차가 도장을 찍던 것)로는 안 덮이는 별개 원인이다.
    거기는 도장이 **부당하게** 앞당겨진 것이고, 여기는 도장이 정당한데 **견주는 대상이
    틀린 것**이다.

    뒤집힌 유인이 이 구멍의 실체다 — 「빼」는 `dismissed`, 「나중에」는 `deferred` 로
    저장소에 남아 걸러지는데 **옳은 답인 「반영」만 안 붙었다.**

    그래서 항목의 `lastSeen`(그 회차에 실제로 검출됐을 때만 찍힌다)과 견준다. 그러면
    일회성 항목(새 채널·개명)은 반영이 붙어 있고, 조건이 남아 매 회차 다시 잡히는
    항목(note·파생값)은 정당하게 다시 뜬다.

    **없으면 `generated` 로 물러선다** — 옛 형식 파일에는 `lastSeen` 이 없다. 물러서는
    쪽이 `generated` 라야 **보이는 쪽으로** 틀린다. 안 보이는 쪽으로 틀리면 사람이
    영영 모른다 (`applied_after` 가 못 읽으면 감추지 않는 것과 같은 방향)."""
    return (item or {}).get("lastSeen") or generated


def load_items() -> list:
    """할 일 목록. 사람이 이미 정한 것은 뺀다.

    VM 쪽(`pending-work.js` 의 `suppress`)도 같은 것을 거른다. 여기서 한 번 더 거르는
    이유는 사람이 정한 직후 다음 07:00 전까지의 구간 때문이다 — 그 사이에 목록을
    다시 열면 방금 정한 것이 그대로 보인다."""
    data = read_pending_work()
    items = (data or {}).get("items", [])
    generated = (data or {}).get("generated", "")
    state = _read_json(STATE) or {}
    held = set(active_deferred(state, today_kst()))
    dismissed = state.get("dismissed") or {}
    # 「반영」도 상태 파일에 있다 — 「빼·나중에」와 같은 자리이고 저장소에 들어가 VM 도
    # 읽는다 (2026-09-07 부터. 그전에는 로컬 전용 `.decision-stamp.json` 이라 VM 이 못 봤다).
    approved = state.get("applied") or {}

    out = []
    for it in items:
        if it.get("id") in held:
            continue
        rec = dismissed.get(it.get("id"))
        if rec and _still_dismissed(it, rec):
            continue
        if applied_after(approved.get(it.get("id")), confirmed_at(it, generated)):
            continue
        out.append(it)
    # **번호의 원본은 이 목록의 순서다** — 화면(render)·`--only`(decide_work)·`--json`
    # 셋 다 여기서 받은 순서로 번호를 만든다. 파일 순서는 이월분(비요약)이 앞이라
    # (`mergeItems` 가 이월을 앞에 쌓는다), 그대로 주면 요약을 먼저 보이는 화면의 1번과
    # `--only 1` 이 **다른 항목**이 된다 (2026-09-04). 정렬은 안정적이라 같은 계열끼리는
    # 파일 순서가 유지된다.
    out.sort(key=lambda it: it.get("kind") not in SUMMARY_KINDS)
    return out


def _read_json(p: Path):
    """없는 것과 못 읽는 것을 같은 `None` 으로 뭉갠다.

    **`.pending-work.json` 에는 쓰지 말 것** — 아래 `read_pending_work` 가 따로 있다.
    여기 남은 세 곳(`.sync-state.json`·`.decision-stamp.json`·`.pending-edits.json`)은
    없는 것이 정상이고 못 읽어도 화면이 「0건」이라고 단정하지 않는 자리다.
    """
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class PendingWorkUnreadable(Exception):
    """`.pending-work.json` 이 있는데 못 읽는다. **없는 것과 다른 사건이다.**"""


def read_pending_work(path: Path = None) -> dict:
    """할 일 목록. **없으면 0건, 있는데 못 읽으면 예외.**

    `_read_json` 처럼 둘을 같은 `None` 으로 뭉개면 `load_items()` 가 `[]` 를 돌려주고
    종료코드 0 으로 끝나서, 상황판에 「아침 보고 0건 · 새 채널 0건」이 찍힌다. 파일이
    있으니 `board.py` 의 `probe_work` 도 실패 경로로 안 간다 — 아무 데서도 안 드러난다.
    이 파일은 **추적되는 파일**이라 `git pull` 충돌 표식 하나로 바로 그 길로 간다.

    `board.py` 의 `probe_edits` 가 `.pending-edits.json` 에 대해 하는 구분과 같다.
    """
    path = path or PENDING_WORK
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PendingWorkUnreadable(f"{rel_to_root(path)} 을 읽지 못했습니다: {e}") from e


def rel_to_root(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def age_of(item: dict) -> int:
    from datetime import date
    try:
        y, m, d = (int(x) for x in str(item.get("firstSeen", "")).split("-"))
        return (today_kst() - date(y, m, d)).days
    except (ValueError, TypeError):
        return 0


def summary_block(it: dict, n: int, days: int = 0) -> list:
    """요약 항목 한 건의 화면 줄.

    **후보가 여럿이면 전부 적는다.** 같은 채널·같은 자리를 가리키는 후보들은 한 항목으로
    합쳐지는데(`pending-work.js` 의 `buildItems`), 화면이 첫 번째만 그리면 파일에만 있고
    사람은 못 보게 된다 — 잃은 것과 같다. 고칠 요약 줄은 하나이므로 항목은 그대로 두고
    후보만 번호를 매겨 나란히 보인다."""
    cands = [it] + list(it.get("more") or [])
    head = f"{n}. #{it.get('channel','')}  [{it.get('type','')}]  {it.get('where','') or ''}"
    if len(cands) > 1:
        head += f"   (같은 자리에 후보 {len(cands)}건)"
    out = [head + (f"        {days}일째" if days else "")]

    if it.get("kind") != "dropped":
        out.append(f"   요약: {it.get('was') or '(없음)'}")
    for i, c in enumerate(cands, 1):
        # 셀 것이 없는데 번호를 매기면 시끄럽다 — 후보가 하나면 그냥 「후보」
        label = f"후보 {i}" if len(cands) > 1 else "후보"
        if it.get("kind") == "dropped":
            out.append(f"   {label}: {c.get('now','')}")
            out.append(f"   뺀 이유: {c.get('why','')}")
        else:
            out.append(f"   {label}: {c.get('now') or '(종결)'}")
        out.append("   원문:")
        out += [f"     {line}"
                for line in context_for(md_path(it), c.get("evidence", "")).split("\n")]
    return out


def render() -> None:
    items = load_items()
    if not items:
        print("할 일 목록이 없습니다 — 07:00 회차가 아직 안 돌았거나, 정할 것이 없습니다.")
        print(f"  ({PENDING_WORK.relative_to(ROOT).as_posix()})")
    else:
        summary = [it for it in items if it.get("kind") in SUMMARY_KINDS]
        other = [it for it in items if it.get("kind") not in SUMMARY_KINDS]
        n = 0

        if summary:
            print("\n── 상단 요약과 다른 것 (전부 원문 확인이 필요합니다) ──\n")
            for it in summary:
                n += 1
                for line in summary_block(it, n, age_of(it)):
                    print(line)
                print()

        if other:
            print("── 그 밖에 사람이 정할 것 ──\n")
            for it in other:
                n += 1
                days = age_of(it)
                label = KIND_LABEL.get(it.get("kind"), it.get("kind"))
                where = it.get("channel") or it.get("where") or ""
                detail = it.get("detail") or ""
                print(f"{n}. [{label}] {where}  {detail}".rstrip()
                      + (f"   {days}일째" if days else ""))
            print()

    # 수정·삭제는 **목록만 잇고 집행은 잇지 않는다.** 매일 도는 경로라 건드리면 매일 깨진다.
    edits = (_read_json(PENDING_EDITS) or {}).get("items", [])
    if edits:
        print("── 슬랙에서 고쳐지거나 지워진 것 ──\n")
        print(f"  {len(edits)}건. 이쪽은 따로 도는 장치가 반영합니다:")
        print("  python .claude/skills/slack-sync/scripts/apply_edits.py")
        print()


# ── 관문 ────────────────────────────────────────────────────────────────
#
# 요약 문장을 새로 쓰는 것은 판단이라 스크립트가 대신 못 한다. 그 말은 **스크립트를 안
# 거치고 md 를 바로 고치는 길이 늘 열려 있다**는 뜻이고, 지금 문제가 정확히 그것이다.
# 그래서 여기서는 관문이 유일한 자물쇠다 — doc-archive 7.5 는 집행이 `decide.py` 라
# 스크립트를 안 쓰면 애초에 아무 일도 안 일어나지만, 이쪽은 다르다.

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


class GitError(RuntimeError):
    """git 이 실패했다. **빈 문자열로 돌려주지 않는다** — 그러면 관문이 「볼 것 없음」과
    「못 봄」을 같은 0 으로 답하고, 훅이 걸려 있는데 아무것도 안 지키게 된다
    (2026-08-12: 스킬을 다른 깊이에 깔면 ROOT 가 어긋나 실제로 이 길로 간다).

    **`doc-archive/review_batch.py` 에도 같은 것이 따로 있다.** 스킬끼리 임포트를 늘리면
    한쪽만 가져간 저장소에서 훅이 ImportError 로 죽어 남의 커밋을 전부 막는다.
    """


def git(*args) -> str:
    """`core.quotepath=false` 를 늘 붙인다 — 없으면 git 이 한글 경로를 8진수로 감싸
    출력해서 이 아카이브(파일명이 거의 다 한글)의 경로를 하나도 못 읽는다."""
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        head = (r.stderr or "").strip().splitlines()
        raise GitError(f"git {' '.join(args[:2])} — "
                       + (head[0] if head else f"종료코드 {r.returncode}"))
    return r.stdout


def repo_check():
    """`ROOT`(자료 저장소) 가 진짜 이 저장소의 뿌리인가.

    **2026-08-31 부터 ROOT 는 추측이 아니다.** 전에는 `sync_index` 가 파일 위치로 추측했고,
    스킬을 다른 깊이에 깔면 엉뚱한 저장소에서 git 을 돌려 관문이 「담긴 채널 md 없음」이라며
    통과시켰다. 지금은 `_shared/paths.py` 한 곳이 정한다. 그래도 이 검사는 남긴다 —
    자료 저장소를 짚었는데 그 자리가 git 저장소가 아닌 경우는 여전히 있을 수 있다.
    """
    top = Path(git("rev-parse", "--show-toplevel").strip())
    if top.resolve() != Path(ROOT).resolve():
        raise GitError(f"관문이 계산한 위치와 저장소가 다릅니다 (계산 {ROOT} · 실제 {top}). "
                       "스킬은 <저장소>/.claude/skills/<이름>/scripts/ 에 있어야 합니다")


def _digest(data: bytes) -> str:
    """**줄끝을 맞춘 뒤 센다.** git 은 인덱스에 LF 로 넣고 작업 트리에는 CRLF 로 꺼내므로
    (이 저장소가 그렇다), 바이트를 그대로 대면 같은 내용인데 해시가 갈려 관문이 늘 막는다."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()[:16]


def fingerprint(md: Path) -> str:
    """작업 트리의 내용. **도장을 찍을 때 쓴다** — 사람이 고쳐 놓은 것이 이것이다."""
    return _digest(md.read_bytes())


def staged_fingerprint(md: Path) -> str:
    """**스테이징(인덱스)의 내용.** 관문은 이쪽을 본다.

    도장은 작업 트리에서 찍는데 관문이 그것을 그대로 대면, `git add -p` 로 조각만
    골라 담았을 때 **화면에 보인 것과 다른 조합이 커밋된다** — 작업 트리 해시는 그대로라
    통과하고, 뚫려도 화면에 아무 표시가 없다 (2026-08-12 리뷰. 문서 쪽 `review_batch.py`
    도 같다).
    """
    rel = md.relative_to(ROOT).as_posix()
    r = subprocess.run(
        ["git", "-c", "core.quotepath=false", "show", f":{rel}"],
        cwd=ROOT, capture_output=True,
    )
    if r.returncode != 0:
        head = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise GitError(f"git show :{rel} 실패 — {head[0] if head else f'종료코드 {r.returncode}'}")
    return _digest(r.stdout)


def load_stamp() -> dict:
    """망가졌으면 빈 것으로 본다 — 기록이 없으면 막는 쪽이 안전한 방향이다."""
    d = _read_json(STAMP)
    return d if isinstance(d, dict) else {}


def staged_channel_mds() -> list:
    """커밋에 담긴, **이번에 고쳐진** 채널 md.

    · **작업 트리가 아니라 스테이징을 본다** — 고치다 만 md 를 옆에 둔 채 다른 일을
      커밋하는 것까지 막으면 관문이 곧 성가신 것이 되어 꺼진다.
    · **새로 만든 파일(`A`)은 뺀다**(`--diff-filter=M`). 이 관문이 지키는 것은 「이미
      있던 요약이 화면을 안 거치고 바뀌는 것」이다. 새 채널 md 에는 지킬 이전 요약이
      없다 — 빼지 않으면 `slack-sync` 3.5단계(새 채널 md 만들기)가 영영 커밋이 안 된다.
      (문서 쪽 관문은 반대로 새 파일을 본다. 거기는 새로 만든 것이 곧 점검 대상이다.)"""
    out = []
    for path in git("diff", "--cached", "--name-only", "--diff-filter=M",
                    "--", CHANNELS_REL).splitlines():
        path = path.strip().strip('"')
        if not path.endswith(".md"):
            continue
        md = ROOT / path
        if md.exists():
            out.append(md)
    return sorted(set(out))


def summary_end(md: Path) -> int:
    """요약 자리의 끝 (1-based 로 「이 줄까지가 요약」).

    범위를 `summary.js` 의 `extractSummary` 와 맞춘다 — `>` 메타 블록과 첫 `## YYYY-MM`
    앞까지가 사람이 쓰는 요약이다. **봇이 요약으로 읽는 자리와 관문이 지키는 자리가
    갈리면 안 된다.** 월 헤딩이 없으면 파일 전체를 요약으로 본다(보수적으로)."""
    return _summary_end(md.read_text(encoding="utf-8").split("\n"))


def _summary_end(lines: list) -> int:
    """같은 판정을 줄 목록에서 한다 — `context_for` 도 이걸 쓴다.

    범위를 두 곳에 따로 적으면 「관문이 지키는 요약 자리」와 「원문을 찾을 때 건너뛰는
    요약 자리」가 갈린다. 이 파일이 요약 해시를 파이썬으로 다시 구현하지 않는 것과 같은
    이유다."""
    for i, line in enumerate(lines):
        if MONTH_RE.match(line):
            return i
    return len(lines)


def summary_changed_in(diff: str, end: int) -> bool:
    """diff 안에 요약 자리의 변경이 있나.

    **건수 줄은 뺀다** — `derive.js`·`sync_index.py` 가 회차마다 다시 세어 고치는 값이라
    사람 승인의 대상이 아니다. 이걸 안 빼면 `sync_index.py` 를 돌린 뒤 커밋이 매번 막힌다.

    지운 줄은 새 파일의 줄 번호로 정확히 못 맞춘다 — 그때는 그 자리(hunk 시작)로 보고
    **요약 변경으로 친다.** 덜 막는 쪽보다 더 막는 쪽이 안전한 방향이다."""
    cur = 0
    for line in diff.splitlines():
        m = HUNK_RE.match(line)
        if m:
            cur = int(m.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            if cur <= end and not is_count_line(line[1:]):
                return True
            cur += 1
        elif line.startswith("-"):
            if cur <= end and not is_count_line(line[1:]):
                return True
    return False


def summary_changed(md: Path) -> bool:
    rel = md.relative_to(ROOT).as_posix()
    diff = git("diff", "--cached", "-U0", "--", rel)
    return bool(diff.strip()) and summary_changed_in(diff, summary_end(md))


def head_text(md: Path) -> str:
    """HEAD 판 전문. **HEAD 에 없는 새 파일이면 빈 문자열**을 돌려준다 —
    그때는 「전」이 없는 것이 사실이고, 관문도 새 파일은 안 본다."""
    rel = md.relative_to(ROOT).as_posix()
    try:
        return git("show", f"HEAD:{rel}")
    except GitError:
        return ""


def summary_changed_vs_head(md: Path) -> bool:
    """요약 자리가 **HEAD 와** 달라졌나.

    관문(`summary_changed`)은 인덱스를 보지만 `--show` 는 `git add` **전**에 돈다.
    HEAD 와 견주면 스테이징된 것과 안 된 것을 둘 다 잡는다. 건수 줄을 빼는 판정은
    같은 `summary_changed_in` 을 그대로 쓴다 — 두 곳에 따로 적으면 갈린다."""
    rel = md.relative_to(ROOT).as_posix()
    diff = git("diff", "HEAD", "-U0", "--", rel)
    return bool(diff.strip()) and summary_changed_in(diff, summary_end(md))


def gate() -> int:
    """커밋 직전 검사. 화면에 보인 내용으로 찍은 도장이 없는 요약 변경이 있으면 멈춘다.

    보는 것은 **승인 기록(`applied`)이 아니라 도장(`files`)**이다 — `--show` 가 바뀐
    요약을 화면에 내고 그 내용의 지문을 찍고, 여기는 스테이징된 내용의 지문이 그것과
    같은지만 본다. 승인이 하나도 없어도 도장이 맞으면 통과하고, 승인이 있어도 도장을
    찍은 뒤에 또 고치면 막힌다 (2026-08-15, `show_edits` 참조).

    **저장소를 못 읽은 것은 「볼 것 없음」이 아니다** — 그 경우도 멈춘다.
    """
    try:
        repo_check()
        mds = staged_channel_mds()
    except GitError as e:
        print(f"\n커밋을 멈춥니다 — 관문이 저장소를 읽지 못했습니다.\n  · {e}", file=sys.stderr)
        print("  이 검사를 뺄 것이면 git config --unset core.hooksPath 하세요.", file=sys.stderr)
        return 1
    if not mds:
        return 0
    stamp = load_stamp().get("files", {})
    blocked = []
    for md in mds:
        try:
            changed = summary_changed(md)
        except GitError as e:
            print(f"\n커밋을 멈춥니다 — 관문이 {md.name} 의 변경을 읽지 못했습니다.\n  · {e}",
                  file=sys.stderr)
            return 1
        if not changed:
            continue
        rel = md.relative_to(ROOT).as_posix()
        # **인덱스를 댄다 — 작업 트리가 아니다** (위 `staged_fingerprint` 참조).
        try:
            staged = staged_fingerprint(md)
        except GitError as e:
            print(f"\n커밋을 멈춥니다 — 관문이 {md.name} 의 스테이징된 내용을 읽지 못했습니다.\n  · {e}",
                  file=sys.stderr)
            return 1
        if stamp.get(rel) != staged:
            blocked.append((rel, rel in stamp))
    if not blocked:
        return 0

    print("\n커밋을 멈춥니다 — 화면에 보인 내용으로 찍은 도장이 없는 요약 변경이 있습니다.",
          file=sys.stderr)
    for rel, shown_before in blocked:
        print(f"  · {rel}" + ("  (보여준 뒤에 또 고쳐졌습니다)" if shown_before else ""),
              file=sys.stderr)
    print("\n  python .claude/skills/archive-inbox/scripts/review_work.py", file=sys.stderr)
    print("  (요약을 고친 뒤) python .claude/skills/archive-inbox/scripts/decide_work.py --show",
          file=sys.stderr)
    print("  (반영하기로 정한 것을 목록에서 걸러내려면 — 관문 통과 조건은 아닙니다)",
          file=sys.stderr)
    print("    python .claude/skills/archive-inbox/scripts/decide_work.py --only <번호> --apply",
          file=sys.stderr)
    print("바뀐 요약을 --show 로 화면에 보이고 도장을 찍은 다음에 커밋하세요 (archive-inbox SKILL.md).",
          file=sys.stderr)
    return 1


def main(argv=None) -> int:
    # 연동 모드에서는 원문을 쓰지 않는다. **맨 앞이어야** 한다.
    from mode import exit_if_blocked
    exit_if_blocked("수집 작업 기록(review_work)")
    ap = argparse.ArgumentParser(description="07:00 자동 반영이 남긴 할 일을 건별로 보인다")
    ap.add_argument("--json", action="store_true", help="기계용 출력")
    ap.add_argument("--gate", action="store_true", help="목록 없이 검사만 (훅 자리)")
    args = ap.parse_args(argv)

    # **관문은 목록을 안 읽는다.** `gate()` 는 스테이징된 채널 md 와 도장만 본다 —
    # 아침 목록이 깨진 것과 요약을 안 보이고 고친 것은 다른 사건이라, 앞의 것 때문에
    # 커밋이 막히면 안 된다.
    if args.gate:
        return gate()

    try:
        if args.json:
            print(json.dumps(load_items(), ensure_ascii=False, indent=2))
            return 0
        render()
    except PendingWorkUnreadable as e:
        # **빈 목록으로 끝내지 않는다.** 0 으로 끝나면 상황판이 「아침 보고 0건」이라고 적는다.
        print(f"할 일 목록을 읽지 못해 멈춥니다.\n  · {e}", file=sys.stderr)
        print("  파일이 깨졌으면(대개 git pull 충돌 표식) 고치거나, 지우고 다음 07:00 을 기다리세요.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    # 목록을 만드는 도중에도 git 을 부른다. 거기서 실패하면 트레이스백 대신 사유 한 줄로
    # 멈춘다 — **빈 목록으로 끝나지 않는 것**이 요점이다.
    try:
        sys.exit(main())
    except GitError as _e:
        print(f"저장소를 읽지 못해 멈춥니다.\n  · {_e}", file=sys.stderr)
        sys.exit(1)
