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
from dataclasses import dataclass, field

from .gateway.base import Message, Sensitivity
from .gateway.router import ModelNotAllowed, Router, UnknownModel

logger = logging.getLogger("tybot.intent")

# 분류 전용 저가 모델. 레지스트리에 없으면 기본 모델로 폴백한다.
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

KINDS = (
    "status", "help", "summary", "search", "advice", "smalltalk", "out_of_scope",
    "ingest", "ingest_all",
)

CLASSIFIER_PROMPT = """너는 사내 Slack 아카이브 봇의 **라우터**다. 질문에 답하지 말고 분류만 한다.

kind 를 하나 고른다:
- status: 봇 자신의 상태를 **묻는** 질문 (연결·가동·수집 현황·사용 모델·설정)
  (예: "너 상태 어때", "연결됐어?", "잘 돌아가?", "무슨 모델 써?", "몇 건 모았어?")
- ingest: 지금 이 채널의 대화를 **수집하라는 지시** (질문이 아니라 명령)
  (예: "수집해", "내용 수집해", "이 채널 취합해줘", "대화 모아줘", "긁어와")
- ingest_all: **모든 채널**을 수집하라는 지시 (예: "전체 수집해", "모든 채널 수집", "다 모아줘")
  주의: "몇 건 수집했어?"처럼 **현황을 묻는** 것은 status 다. 수집을 **실행**하라는 것만 ingest 다.
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
    r"(봇|너|자기)\s*(의)?\s*(상태|상황)|버전\s*(확인|알려|뭐)|어떤\s*모델|무슨\s*모델|설정\s*확인|"
    # '수집 현황을 묻는' 표현 - 수집 '지시'보다 먼저 걸러야 한다(아래 INGEST_RE 보다 우선 검사).
    r"(수집|취합)\s*(현황|상태|건수|얼마나|몇)|몇\s*건|수집(했|됐|된)|취합(했|됐|된))"
)
HELP_RE = re.compile(r"(도움말|사용법|명령어|뭘\s*할\s*수|어떻게\s*써|\bhelp\b)")
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


@dataclass
class Intent:
    kind: str
    days: int = DEFAULT_DAYS
    terms: list[str] = field(default_factory=list)
    source: str = "llm"  # llm | regex

    @property
    def query(self) -> str:
        return " ".join(self.terms)


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
