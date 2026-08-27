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


@dataclass
class Intent:
    kind: str
    days: int = DEFAULT_DAYS
    terms: list[str] = field(default_factory=list)
    source: str = "llm"  # llm | regex
    # 이 하위질문이 가리키는 원문 조각. 복합 질문을 나눴을 때 각 조각을 답변 생성에 넘긴다.
    # 비어 있으면 호출자가 전체 질문을 쓴다(기존 호출부 호환).
    question: str = ""

    @property
    def query(self) -> str:
        return " ".join(self.terms)


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


def plan(text: str, router: Router | None) -> list[Intent]:
    """복합 질문을 하위질문 목록으로 분해한다(1차 LLM). 실패하면 규칙으로 폴백한다.

    라벨 하나만 돌려주던 예전 구조에서는 "기억나? 그리고 전산팀은 무슨 일 있어?" 처럼
    두 가지를 물으면 **한쪽이 처리 경로에 도달조차 하지 못했다.** 분해를 분류기 책임으로
    옮겨 사람이 실제로 묻는 방식에 맞춘다.
    """
    if router is None:
        return plan_by_rule(text)

    messages = [Message("system", PLANNER_PROMPT), Message("user", text)]
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
            return plan_by_rule(text)
    else:
        return plan_by_rule(text)

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
        return plan_by_rule(text)

    writes = [x for x in tasks if x.kind in WRITE_KINDS]
    if writes:
        return [writes[0]]
    # 실행 계층이 상한을 적용하고 생략 안내를 만든다. planner는 전체 개수를 보존한다.
    return _dedupe(tasks)


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
