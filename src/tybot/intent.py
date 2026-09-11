"""질문 의도 분류 — LLM 이 판단하고, 실패하면 정규식으로 폴백한다.

정규식만으로 자연어를 가르면 표현이 바뀔 때마다 샌다("상태" vs "너의 상태" vs "잘 돌아가?").
분류는 LLM 에게 맡기고, 대신 **분류기는 답을 만들지 않는다** — 라우팅과 검색어 추출만 한다.
근거는 여전히 아카이브 원문뿐이다(환각방지 4겹 유지).

비용: 분류는 최저가 모델 + 짧은 출력으로 질문당 $0.001 미만.
"""
from __future__ import annotations

import json
import logging
import re
import string
from dataclasses import dataclass, field

from .gateway.base import Message, Sensitivity
from .gateway.router import ModelNotAllowed, Router, UnknownModel

logger = logging.getLogger("tybot.intent")

# 분류 전용 저가 모델. 레지스트리에 없으면 기본 모델로 폴백한다.
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

KINDS = (
    "status", "help", "summary", "search", "advice", "smalltalk", "out_of_scope",
    "ingest", "ingest_all", "memory",
)

CLASSIFIER_PROMPT = """너는 사내 Slack 아카이브 봇의 **라우터**다. 질문에 답하지 말고 분류만 한다.

kind 를 하나 고른다:
- status: 봇 자신의 상태를 **묻는** 질문 (연결·가동·수집 현황·사용 모델·설정)
  (예: "너 상태 어때", "연결됐어?", "잘 돌아가?", "무슨 모델 써?", "몇 건 모았어?")
- ingest: 지금 이 채널의 대화를 **수집하라는 지시** (질문이 아니라 명령)
  (예: "수집해", "내용 수집해", "이 채널 취합해줘", "대화 모아줘", "긁어와")
- ingest_all: **모든 채널**을 수집하라는 지시 (예: "전체 수집해", "모든 채널 수집", "다 모아줘")
  주의: "몇 건 수집했어?"처럼 **현황을 묻는** 것은 status 다. 수집을 **실행**하라는 것만 ingest 다.
- memory: **봇이 이전 대화·답변을 기억하는지** 묻는 질문
  (예: "이전에 네가 했던 답변 기억나?", "아까 뭐라고 했지?", "우리 대화 기억해?",
   "맥락 유지돼?", "내가 전에 물어본 거 알아?")
  주의: 봇의 연결·가동 상태(status)와 다르다. '기억·이전 답변·대화 맥락'을 묻는 것만 memory 다.
- help: **봇 자신의** 사용법·명령어·기능을 묻는 질문 (예: "뭘 할 수 있어?", "명령어 알려줘")
  주의: 업무 방식에 대한 조언 요청은 help 가 아니라 advice 다
- summary: 특정 키워드가 아니라 **범위 전체의 내용·진행 상황**을 알고 싶은 질문
  (예: "요약해줘", "이번주 어땠어", "무슨 일 있었어", "프로젝트 어디까지 갔어",
   "다른 워크스페이스 내용 알려줘", "어떤 자료 있어?")
- search: 아카이브 원문에서 **구체적 사실**을 찾는 질문
  (예: "김해외동 기성금 얼마야", "누가 승인했어", "착공일 언제야")
- advice: 사실 조회가 아니라 **판단·권고·설계 방향**을 묻는 업무 질문
  (예: "채널을 잘게 쪼개는 게 나아 하나로 묶는 게 나아?", "이 구성의 장단점 알려줘",
   "어느 방향을 추천해?", "이렇게 하면 문제 생길까?")
- smalltalk: 인사·감사·잡담 (예: "안녕", "고마워")
- out_of_scope: **사내 업무와 완전히 무관한** 질문만 (예: "내일 날씨", "야구 결과", 연예 소식)
  주의: 아카이브·워크스페이스·채널·수집 범위에 대한 질문은 out_of_scope 가 **아니다**.
  "다른 워크스페이스 내용 알려줘", "무슨 자료 있어?", "어떤 채널 있어?" 같은 질문은 summary 다.
  판단이 애매하면 out_of_scope 대신 summary 나 search 를 고른다.

days: summary 일 때만, 질문이 가리키는 기간을 일수로. 언급 없으면 7. "오늘"=1, "이번주"=7, "한달"=30.
terms: search 또는 advice 일 때, 아카이브 검색에 쓸 **핵심 명사·고유명사·숫자**만 골라 배열로.
  조사·서술어·"알려줘" 같은 요청 표현은 제외한다. 현장명·팀명·금액·문서명은 반드시 포함.

JSON 만 출력한다. 설명·코드펜스 금지.
{"kind": "...", "days": 7, "terms": ["..."]}"""

# --- 폴백용 정규식 (LLM 실패 시에만 쓴다) ---
STATUS_RE = re.compile(
    r"(^\s*(상태|status)\b|연결\s*(상태|상황|확인|됐|되었|잘)|접속\s*(상태|상황|확인)|"
    r"살아\s*있|정상\s*(작동|동작|이야|인가)|헬스\s*체크|health\s*check|\bping\b|"
    # '현재/지금 상태' 처럼 주체를 생략한 표현도 잡는다. LLM 분류기가 죽은 상황(키 만료·장애)에서
    # 이 규칙만으로 상태 질문에 답할 수 있어야 한다 - 하필 그때 가장 필요한 질문이다.
    r"(봇|너|자기|현재|지금|시스템|서버)\s*(의)?\s*(상태|상황)|"
    # 문장 앞에 고정한다. 안 하면 "이번주 진행 상황 알려줘"(요약 질문)까지 삼킨다.
    r"^\s*(현재|지금)?\s*(상태|상황)\s*(는|은|이|가|를|을)?\s*"
    r"(어때|어떠|어떤|어떻|알려|보여|확인|점검|출력)|"
    r"버전\s*(확인|알려|뭐)|어떤\s*모델|무슨\s*모델|설정\s*확인|"
    # '수집 현황을 묻는' 표현 - 수집 '지시'보다 먼저 걸러야 한다(아래 INGEST_RE 보다 우선 검사).
    r"(수집|취합)\s*(현황|상태|건수|얼마나|몇)|몇\s*건|수집(했|됐|된)|취합(했|됐|된))"
)
HELP_RE = re.compile(r"(도움말|사용법|명령어|뭘\s*할\s*수|어떻게\s*써|\bhelp\b)")
# 봇의 '기억'을 묻는 질문. STATUS_RE 보다 먼저 검사한다 —
# "이전에 네가 했던 답변" 류가 '(너) ... 상태' 패턴에 걸려 status 로 새는 일이 있었다.
MEMORY_RE = re.compile(
    r"(기억(나|해|하|되|할|은|을|이)|까먹|잊었|"
    r"이전\s*(에)?\s*(한|했던|말한|답변|대화)|아까\s*(뭐|한|했던|말)|방금\s*(뭐|한|말)|"
    r"previous\s*(answer|reply)|"
    r"(대화|답변|맥락|컨텍스트|context)\s*(를|을|이)?\s*(유지|기억|저장|남|알)|"
    r"세션\s*(유지|기억))"
)
# 수집 '지시'만 잡는다. "몇 건 수집했어?"(현황 질문)는 STATUS_RE 가 먼저 잡도록 순서를 둔다.
INGEST_ALL_RE = re.compile(
    r"((전체|모든|전부|다)\s*(채널\s*)?(수집|취합)|(수집|취합)\s*(전체|모두)|ingest\s*all)"
)
INGEST_RE = re.compile(
    r"((수집|취합)\s*(해|해줘|하자|해라|시작|좀)|^\s*(수집|취합|ingest)\s*$|"
    r"(대화|내용|채팅|기록)\s*(를|을)?\s*(수집|취합|모아|긁어)|모아\s*줘|긁어\s*와)"
)
ADVICE_RE = re.compile(
    r"(추천|권장|의견|조언|어느\s*(쪽|방향|게)|어떤\s*(쪽|방향|방법)|"
    r"좋을까|나을까|낫나|낫니|낫을까|장단점|비교해|괜찮을까|문제\s*(될|있을|생길)|"
    r"어떻게\s*(하는\s*게|해야|가는\s*게)|바람직)"
)
SUMMARY_RE = re.compile(
    r"(요약|브리핑|정리해|정리 좀|진행\s*상황|진행\s*현황|현재\s*상황|현황|"
    r"어디까지|어떻게\s*돼가|무슨\s*일|summary|summarize|status\s*update)"
)
PERIOD_RE = re.compile(r"(\d+)\s*(일|주|개월|달)")
PERIOD_WORDS = {
    "오늘": 1, "어제": 2, "이번주": 7, "금주": 7, "이번 주": 7,
    "지난주": 14, "저번주": 14, "이번달": 30, "이번 달": 30, "한달": 30, "지난달": 60,
}
DEFAULT_DAYS = 7
# 검색어에서 걸러낼 요청 표현 (폴백 경로용 최소 스톱워드)
STOPWORDS = {
    "알려줘", "알려", "말해줘", "말해", "궁금", "궁금해", "확인", "확인해줘", "해줘", "주세요",
    "뭐야", "무엇", "어떻게", "어때", "지금", "현재", "우리", "저기", "그거", "이거",
}
TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]{2,}")
CLAUSE_SPLIT_RE = re.compile(
    r"(?:[?!.。！？]+|\s+(?:그리고|그런데|근데|또한|또)\s+)"
)


# 한 질문에서 처리할 하위질문 상한. 비용·지연을 묶는다 - 사람이 한 번에 묻는 질문은
# 보통 2개, 많아도 3개다. 넘치면 앞의 것부터 답하고 나머지는 다시 묻게 안내한다.
MAX_TASKS = 3

# --- 후속 질문 (설계: thread-follow-up-evidence.md §8) ------------------------
#
# 같은 스레드에서 이어 묻는 말. **이 표현이 있다고 곧바로 후속 질문은 아니다** —
# 실행 동사가 함께 있어야 한다. "아까 뭐라고 했지?" 는 기억을 묻는 것이고,
# "아까 그거 다시 정리해줘" 는 실행 요청이다. 둘을 같은 칸에 넣으면 실행 요청이
# 기억 확인으로 새고, 사용자는 "기억한다" 는 답만 받는다(2026-09-11 실제 발생).
REFERENCE_RE = re.compile(
    r"(방금|아까|직전|위에서|위의|앞서|이전\s*(답변|대화|내용|질문)|"
    r"그\s*(문서|파일|자료|내용|건)|해당\s*(문서|파일|자료|건)|"
    r"관련\s*(파일|문서|자료)|저\s*문서|"
    r"(너와|우리가?)\s*나눈\s*대화|우리\s*대화|"
    r"처리\s*(가\s*)?(안\s*된|되지\s*않은|실패한|못한)\s*(문서|파일)?)"
)
# 무언가를 **하라는** 말. 기억 여부를 묻는 문장과 가르는 기준이다.
ACTION_RE = re.compile(
    r"(요약|정리|확인|알려|보여|찾아|검색|비교|뽑아|추려|말해|설명|"
    r"다시\s*(봐|보|확인|정리|요약|알려))"
)
# 기억 **여부 자체**를 묻는 표현. 실행 동사가 없을 때만 memory 다.
MEMORY_ONLY_RE = re.compile(
    r"(기억(나|해|하니|하고|되|할\s*수|력)|까먹|잊었|"
    r"물어본\s*적|말한\s*적|한\s*적\s*있)"
)
# 첨부 변환 상태를 묻는 표현. 이 신호가 있으면 같은 참조 범위에
# 현재 첨부 메타데이터를 결합한다 — 별도의 채널 전체 검색으로 쪼개지 않는다.
ATTACHMENT_STATUS_RE = re.compile(
    r"(첨부|변환|파일\s*(상태|처리|변환)|"
    r"처리\s*(가\s*)?(안\s*된|되지\s*않은|실패|못한)|미처리|변환\s*실패)"
)
# 주제어에서 걸러낼 지칭·요청 표현. 주제가 아니라 **가리키는 말**이다.
REFERENCE_STOPWORDS = frozenset(
    {
        "방금", "아까", "직전", "위에서", "이전", "앞서", "다시", "대화", "내용",
        "요약", "정리", "확인", "알려", "보여", "검색", "관련", "문서", "파일",
        "자료", "첨부", "변환", "실패", "처리", "너와", "우리", "나눈", "해당",
        "상태", "목록", "부탁", "한번", "좀더", "하나", "한개", "이것", "그것",
    }
)

REFERENCE_MODES = ("none", "prior_turn", "prior_topic", "prior_attachments")

SINGULAR_FOLLOW_UP_RE = re.compile(
    r"(하나(?:의|인)?\s*문서|한\s*개(?:의)?\s*문서|그\s*문서|해당\s*문서|"
    r"처리\s*(?:가\s*)?(?:안\s*된|되지\s*않은|실패한)\s*문서)"
)
FAILED_ATTACHMENT_NOTE_RE = re.compile(
    r"자동 변환 실패로 내용을 읽지 못한 첨부:\s*(?P<name>[^\n]+)"
)


@dataclass
class Intent:
    kind: str
    days: int = DEFAULT_DAYS
    terms: list[str] = field(default_factory=list)
    source: str = "llm"  # llm | regex
    # 이 하위질문이 가리키는 원문 조각. 복합 질문을 나눴을 때 각 조각을 답변 생성에 넘긴다.
    # 비어 있으면 호출자가 전체 질문을 쓴다(기존 호출부 호환).
    question: str = ""
    # --- 후속 질문 (설계: thread-follow-up-evidence.md §8) --------------------
    #
    # **범위를 넓히는 필드가 아니라 좁히는 필드다.** 값이 `none` 이 아니면 이
    # 질문의 근거는 「현재 권한 ∩ 현재 채널 ∩ 이전 답변의 원문 참조 ∩ 현재 주제」
    # 의 교집합뿐이다. 복원에 실패해도 채널 전체로 되돌아가지 않는다.
    reference_mode: str = "none"  # none | prior_turn | prior_topic | prior_attachments
    topic_terms: list[str] = field(default_factory=list)
    include_attachment_status: bool = False
    # 어느 QA 레코드를 이어 가는가. **LLM 이 정하지 않는다** — 같은 워크스페이스·
    # 채널·스레드 안에서 코드가 고른다.
    referenced_record_ids: list[str] = field(default_factory=list)

    @property
    def query(self) -> str:
        return " ".join(self.terms)

    @property
    def is_followup(self) -> bool:
        return self.reference_mode != "none"


# 아카이브 원문을 근거로 답하는 의도. 이 의도의 답변은 **답변 엔진 출력을 그대로** 쓴다 -
# 출처가 붙어 있으므로 다시 생성하면 원칙 2(출처 강제)가 깨진다.
ARCHIVE_KINDS = ("summary", "search", "advice")
# 봇 자신에 대한 답변. 사실은 코드가 만들고 문장은 LLM 이 쓴다(compose.py).
SELF_KINDS = ("status", "help", "memory", "smalltalk", "out_of_scope")
# 쓰기 동작. 절대 다른 의도와 섞지 않는다 - 무엇을 실행하는지 모호하면 실행하지 않는다.
WRITE_KINDS = ("ingest", "ingest_all")


def parse_period(text: str, *, default: int = DEFAULT_DAYS) -> int:
    m = PERIOD_RE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return min(n * {"일": 1, "주": 7, "개월": 30, "달": 30}[unit], 365)
    for word, days in PERIOD_WORDS.items():
        if word in text:
            return days
    return default


def classify_by_rule(text: str) -> Intent:
    """LLM 없이 판단. 분류기 장애 시 폴백 경로."""
    if MEMORY_RE.search(text):
        return Intent("memory", source="regex")
    if STATUS_RE.search(text):
        return Intent("status", source="regex")
    if INGEST_ALL_RE.search(text):
        return Intent("ingest_all", source="regex")
    if INGEST_RE.search(text):
        return Intent("ingest", source="regex")
    if HELP_RE.search(text):
        return Intent("help", source="regex")
    if SUMMARY_RE.search(text):
        return Intent("summary", days=parse_period(text), source="regex")
    terms = [t for t in TOKEN_RE.findall(text) if t not in STOPWORDS]
    if ADVICE_RE.search(text):
        return Intent("advice", terms=terms, source="regex")
    return Intent("search", terms=terms or TOKEN_RE.findall(text), source="regex")


# 절 경계에서 떼어낼 공백·구두점(전각 쉼표 포함).
PLANNER_PROMPT = CLASSIFIER_PROMPT.replace(
    "kind 를 하나 고른다:",
    "각 하위질문마다 kind 를 하나 고른다:",
).replace(
    """JSON 만 출력한다. 설명·코드펜스 금지.
{"kind": "...", "days": 7, "terms": ["..."]}""",
    """사람은 한 번에 여러 가지를 묻는다. **질문을 하위질문으로 나눠라.**
- "이전 답변 기억나? 그리고 전산팀은 무슨 일 있어?" -> memory 1개 + summary 1개
- "상태 어때? 그리고 김해외동 기성금 얼마야?" -> status 1개 + search 1개
- 질문이 하나면 task 도 하나다. 억지로 쪼개지 말 것.
- 최대 3개. 각 task 의 question 에는 그 하위질문의 원문 조각을 그대로 넣는다.
- 수집 지시(ingest/ingest_all)가 섞여 있으면 그것만 남긴다 —
  무엇을 실행하는지 모호한 상태로 실행해서는 안 된다.
- `<이전_스레드>`가 있으면 현재 질문의 "그 문서", "하나", "아까 것" 같은
  지칭어만 해석하는 데 쓴다. 거기 적힌 것은 **질문과 주제 목록**이고, 사실 근거가
  아니다. 실제 원문은 시스템이 좌표로 다시 연다.
- 후속 질문의 task.question과 terms는 지칭 대상을 정확히 넣어 독립적으로 이해되게
  만든다. 하나를 가리키면 이전 답변의 전체 목록으로 넓히지 않는다.
- "방금 그거 다시 정리해줘"처럼 **실행 동사가 있는** 후속 질문은 memory 가 아니다.
  memory 는 "기억나?", "전에 물어본 적 있어?"처럼 기억 여부 자체를 묻는 것뿐이다.
- 요약과 첨부 변환 상태를 한 문장에서 함께 물으면 **하나의 task 로 둔다.** 쪼개면
  한쪽은 좁은 범위로, 다른 쪽은 채널 전체로 가서 서로 다른 범위의 답이 붙는다.

JSON 만 출력한다. 설명·코드펜스 금지.
{"tasks": [{"kind": "...", "question": "...", "days": 7, "terms": ["..."]}]}""",
)

_CLAUSE_TRIM = string.whitespace + ',，'


def _clamp_days(v) -> int:
    """모델이 준 기간을 안전 범위로 자른다. 없거나 이상하면 기본값."""
    try:
        return min(max(int(v), 1), 365)
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def split_clauses(text: str) -> list[str]:
    """질문을 절 단위로 나눈다. 복합 질문의 규칙 기반 분해에 쓴다."""
    parts = [c.strip(_CLAUSE_TRIM) for c in CLAUSE_SPLIT_RE.split(text)]
    return [c for c in parts if c]


def _dedupe(tasks: list[Intent]) -> list[Intent]:
    """같은 의도가 여러 번 나오면 하나로 합친다. 검색어는 합집합으로 모은다.

    "전산팀 상황이랑 자금팀 상황" 같은 질문을 요약 두 번 돌리지 않기 위한 것이다.
    """
    out: list[Intent] = []
    for task in tasks:
        same = next((x for x in out if x.kind == task.kind), None)
        if same is None:
            out.append(task)
            continue
        for term in task.terms:
            if term not in same.terms:
                same.terms.append(term)
        if task.question and task.question not in same.question:
            same.question = f"{same.question} {task.question}".strip()
    return out


def plan_by_rule(text: str) -> list[Intent]:
    """LLM 없이 복합 질문을 분해한다. 분류기 장애 시 폴백 경로.

    절 단위로 나눠 각각 분류한다. 절이 하나뿐이거나 분해해도 같은 의도면 1개로 돌아간다.
    """
    clauses = split_clauses(text)
    if len(clauses) < 2:
        one = classify_by_rule(text)
        one.question = text
        return [one]

    tasks: list[Intent] = []
    for clause in clauses:
        task = classify_by_rule(clause)
        task.question = clause
        # "기억나? 왜?" 의 "왜" 처럼 검색어가 없는 조각은 질문이 아니다. 남겨두면
        # 0건 검색 답변이 붙어 "물어본 적 없는 것에 답을 못했다" 는 문장이 나간다.
        if task.kind == "search" and not task.terms:
            continue
        tasks.append(task)
    if not tasks:
        one = classify_by_rule(text)
        one.question = text
        return [one]

    # 쓰기 동작이 섞이면 실행 대상이 모호하다. 쓰기 하나만 남긴다.
    writes = [x for x in tasks if x.kind in WRITE_KINDS]
    if writes:
        writes[0].question = text
        return [writes[0]]

    # 절을 나눴더니 전부 search 로 흩어지는 경우가 많다 - 그럴 땐 원문 전체로 한 번 분류한다.
    tasks = _dedupe(tasks)
    if len(tasks) == 1:
        tasks[0].question = text
    # 실행 계층이 MAX_TASKS까지만 처리하고 초과 개수를 사용자에게 알린다. 여기서 먼저
    # 자르면 몇 개가 생략됐는지 알 수 없어 질문을 조용히 버리게 된다.
    return tasks


# 토큰 끝에 붙어 오는 조사·어미. 한국어는 「문서」와 「문서도」가 다른 토큰이라,
# 떼지 않으면 지칭어가 주제어로 남는다 — 그러면 "처리 안 된 문서도 확인해줘" 가
# 「문서도」 라는 주제를 가진 질문이 되고, 이전 근거에서 그 낱말을 찾다 0건이 된다.
JOSA_SUFFIXES = (
    "하고", "해줘", "합니다", "한다", "으로", "에서", "까지", "부터", "에게",
    "이나", "라도", "에는", "도", "은", "는", "이", "가", "을", "를", "의",
    "에", "만", "와", "과", "로", "나",
)


def _stem(token: str) -> str:
    for suffix in JOSA_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 2:
            return token[: -len(suffix)]
    return token


def topic_terms_of(text: str, terms: list[str] | None = None) -> list[str]:
    """이 후속 질문이 한정한 **주제어**.

    "미수금 관련 내용을 다시 요약" 에서 남겨야 할 것은 `미수금` 하나다. `다시`,
    `요약`, `관련` 은 가리키는 말이지 주제가 아닌데, 그대로 두면 이전 근거를
    좁히는 게 아니라 아무 줄에나 걸린다.

    조사가 붙은 형태도 같은 낱말로 본다. 「문서도」를 주제로 남기면 그 질문은
    **첨부 상태 질문이 아니라 「문서도」 라는 주제의 질문**이 되어, 참조 범위에서
    0건이 나오고 사용자는 근거가 없다는 답을 받는다.
    """
    out: list[str] = []
    for raw in list(terms or []) + TOKEN_RE.findall(text or ""):
        token = str(raw).strip()
        if not token:
            continue
        stem = _stem(token)
        if token in STOPWORDS or token in REFERENCE_STOPWORDS:
            continue
        if stem in STOPWORDS or stem in REFERENCE_STOPWORDS:
            continue
        if stem not in out:
            out.append(stem)
    return out[:8]


def followup_hint(text: str) -> tuple[str, bool]:
    """지칭 표현만 보고 `(reference_mode, include_attachment_status)` 를 정한다.

    같은 스레드에 이전 문답이 있을 때만 의미가 있다 — 호출자가 그것을 확인한다.
    분류 우선순위는 설계 §8 그대로다.
    """
    if not REFERENCE_RE.search(text or ""):
        return "none", False
    has_action = bool(ACTION_RE.search(text))
    # 2순위: 기억 여부 자체를 묻는 것이면 기존 memory 동작을 유지한다.
    if MEMORY_ONLY_RE.search(text) and not has_action:
        return "none", False
    if not has_action:
        return "none", False
    attachments = bool(ATTACHMENT_STATUS_RE.search(text))
    topics = topic_terms_of(text)
    if attachments and not topics:
        return "prior_attachments", True
    if topics:
        return "prior_topic", attachments
    return "prior_turn", attachments


def _followup_kind(tasks: list[Intent]) -> str:
    """복합 후속 질문 하나를 어떤 의도로 처리할지.

    요약과 첨부 상태 확인이 한 문장에 있으면 **둘로 쪼개지 않는다**(설계 §8-5).
    쪼개면 요약은 좁은 참조 범위로, 첨부 확인은 채널 전체로 가서 서로 다른
    범위의 답이 한 메시지에 붙는다 — 사용자는 어느 쪽이 무엇인지 알 수 없다.
    """
    kinds = [t.kind for t in tasks]
    if "summary" in kinds:
        return "summary"
    if "advice" in kinds:
        return "advice"
    return "search"


def apply_followup(text: str, tasks: list[Intent], *, has_prior: bool) -> list[Intent]:
    """후속 질문이면 **하나의 참조 범위를 공유하는 한 건**으로 합친다."""
    if not has_prior or not tasks:
        return tasks
    mode, attachments = followup_hint(text)
    if mode == "none":
        return tasks
    kind = _followup_kind(tasks)
    terms: list[str] = []
    for task in tasks:
        for term in task.terms:
            if term not in terms:
                terms.append(term)
    # 분해기가 지칭을 이미 풀었으면(하위질문 하나) 그 문장을 쓴다 — "그 문서" 보다
    # "가정산서.pdf" 가 낫다. 여러 개로 쪼개진 복합 질문은 원문 전체를 쓴다.
    question = tasks[0].question if len(tasks) == 1 and tasks[0].question else text
    merged = Intent(
        kind=kind,
        days=max((t.days for t in tasks), default=DEFAULT_DAYS),
        terms=terms,
        source=tasks[0].source,
        question=question,
        reference_mode=mode,
        topic_terms=topic_terms_of(text, terms),
        include_attachment_status=attachments,
    )
    if mode == "prior_topic" and not merged.topic_terms:
        # 주제를 못 뽑았으면 「직전 결과」 로 내려간다. 빈 주제로 교집합을 잡으면
        # 아무것도 안 남고, 그건 근거가 없는 게 아니라 우리가 못 고른 것이다.
        merged.reference_mode = "prior_turn"
    return [merged]


def _referenced_failed_attachment(text: str, conversation_context: str) -> str:
    """후속 질문이 이전 답변의 실패 첨부 한 건을 가리키면 그 파일명을 돌려준다."""
    if not conversation_context or not SINGULAR_FOLLOW_UP_RE.search(text):
        return ""
    matches = FAILED_ATTACHMENT_NOTE_RE.findall(conversation_context)
    if not matches:
        return ""
    name = matches[-1].strip().strip("`*_ ")
    # 여러 건을 줄여 표시한 문구는 어느 하나인지 결정할 수 없다.
    if "," in name or re.search(r"\s외\s+\d+건", name):
        return ""
    return name


def _context_fallback(
    text: str, conversation_context: str, *, thread_has_refs: bool = False
) -> list[Intent]:
    if not thread_has_refs:
        # 구형 레코드에만 남은 길이다. 좌표가 있으면 문장을 다시 파싱하지 않는다 —
        # 문구가 바뀌거나 파일이 여러 개면 이 정규식은 조용히 어긋난다(설계 §8).
        name = _referenced_failed_attachment(text, conversation_context)
        if name:
            return [
                Intent(
                    "search",
                    terms=[name],
                    source="context",
                    question=f"{name} 내용을 다시 확인해줘",
                )
            ]
    return apply_followup(
        text, plan_by_rule(text), has_prior=bool(conversation_context.strip())
    )


def plan(
    text: str,
    router: Router | None,
    *,
    conversation_context: str = "",
    thread_has_refs: bool = False,
) -> list[Intent]:
    """복합 질문을 하위질문 목록으로 분해한다(1차 LLM). 실패하면 규칙으로 폴백한다.

    라벨 하나만 돌려주던 예전 구조에서는 "기억나? 그리고 전산팀은 무슨 일 있어?" 처럼
    두 가지를 물으면 **한쪽이 처리 경로에 도달조차 하지 못했다.** 분해를 분류기 책임으로
    옮겨 사람이 실제로 묻는 방식에 맞춘다.
    """
    if router is None:
        return _context_fallback(text, conversation_context, thread_has_refs=thread_has_refs)

    user_text = text
    if conversation_context.strip():
        user_text = (
            f"<이전_스레드>\n{conversation_context.strip()}\n</이전_스레드>\n\n"
            f"<현재_질문>\n{text}\n</현재_질문>"
        )
    messages = [Message("system", PLANNER_PROMPT), Message("user", user_text)]
    for model in (CLASSIFIER_MODEL, None):
        try:
            resp = router.complete(
                messages,
                model=model,
                sensitivity=Sensitivity.CONFIDENTIAL,
                max_tokens=500,
            )
            break
        except (UnknownModel, ModelNotAllowed) as e:
            logger.info("분류 모델 %s 사용 불가(%s) - 다음 후보 시도", model, e)
        except Exception as e:
            logger.warning("분해 호출 실패(%s) - 규칙 기반으로 폴백", e)
            return _context_fallback(text, conversation_context, thread_has_refs=thread_has_refs)
    else:
        return _context_fallback(text, conversation_context, thread_has_refs=thread_has_refs)

    try:
        raw = _extract_json(resp.text)
        items = raw.get("tasks")
        if not isinstance(items, list) or not items:
            raise ValueError(f"tasks 없음: {raw!r}")
        tasks: list[Intent] = []
        for item in items:
            kind = str(item.get("kind", "")).strip()
            if kind not in KINDS:
                logger.info("알 수 없는 kind 무시: %r", kind)
                continue
            terms = [str(x) for x in (item.get("terms") or []) if str(x).strip()]
            tasks.append(
                Intent(
                    kind=kind,
                    days=_clamp_days(item.get("days")),
                    terms=terms,
                    source="llm",
                    question=str(item.get("question") or "").strip() or text,
                )
            )
        if not tasks:
            raise ValueError("유효한 task 없음")
    except Exception as e:
        logger.warning("분해 파싱 실패(%s) - 규칙 기반으로 폴백. raw=%r", e, resp.text[:200])
        return _context_fallback(text, conversation_context, thread_has_refs=thread_has_refs)

    writes = [x for x in tasks if x.kind in WRITE_KINDS]
    if writes:
        return [writes[0]]
    if not thread_has_refs:
        referenced = _referenced_failed_attachment(text, conversation_context)
        if referenced:
            # 구형 레코드 전용 폴백. "정리해서 알려줘"가 summary로 분류되면 채널
            # 전체 실패 목록이 다시 나오므로, 이전 답변이 한 건을 명시한 경우에만
            # 그 파일 검색으로 좁힌다. 좌표가 있으면 이 길로 오지 않는다.
            return [
                Intent(
                    "search",
                    terms=[referenced],
                    source="context",
                    question=f"{referenced} 내용을 다시 확인해줘",
                )
            ]
    # 실행 계층이 상한을 적용하고 생략 안내를 만든다. planner는 전체 개수를 보존한다.
    return apply_followup(
        text, _dedupe(tasks), has_prior=bool(conversation_context.strip())
    )


def _extract_json(raw: str) -> dict:
    """코드펜스나 앞뒤 설명이 붙어도 첫 JSON 객체를 꺼낸다."""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\n?|\n?```$", "", s).strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"JSON 없음: {raw[:120]!r}")
    return json.loads(s[start : end + 1])


def classify(text: str, router: Router | None) -> Intent:
    """LLM 분류. 실패하면 조용히 정규식으로 폴백한다(봇은 멈추지 않는다)."""
    if router is None:
        return classify_by_rule(text)

    messages = [Message("system", CLASSIFIER_PROMPT), Message("user", text)]
    for model in (CLASSIFIER_MODEL, None):  # 저가 모델 → 없으면 기본 모델
        try:
            resp = router.complete(
                messages,
                model=model,
                sensitivity=Sensitivity.CONFIDENTIAL,
                max_tokens=200,
            )
            break
        except (UnknownModel, ModelNotAllowed) as e:
            logger.info("분류 모델 %s 사용 불가(%s) — 다음 후보 시도", model, e)
        except Exception as e:
            logger.warning("분류 호출 실패(%s) — 규칙 기반으로 폴백", e)
            return classify_by_rule(text)
    else:
        return classify_by_rule(text)

    try:
        data = _extract_json(resp.text)
        kind = str(data.get("kind", "")).strip()
        if kind not in KINDS:
            raise ValueError(f"알 수 없는 kind: {kind!r}")
        days = int(data.get("days") or DEFAULT_DAYS)
        terms = [str(t).strip() for t in (data.get("terms") or []) if str(t).strip()]
    except Exception as e:
        logger.warning("분류 파싱 실패(%s) — 규칙 기반으로 폴백. raw=%r", e, resp.text[:200])
        return classify_by_rule(text)

    if kind in ("search", "advice") and not terms:
        # 검색인데 핵심어를 못 뽑았으면 원문 전체를 토큰화해 시도한다.
        terms = [t for t in TOKEN_RE.findall(text) if t not in STOPWORDS]

    logger.info(
        "intent kind=%s days=%s terms=%s cost=$%.5f", kind, days, terms, resp.cost_usd
    )
    return Intent(kind, days=min(max(days, 1), 365), terms=terms, source="llm")
