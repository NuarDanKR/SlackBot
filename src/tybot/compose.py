"""봇 자신에 대한 답변을 **사실 데이터에서 문장으로** 만든다.

## 왜 필요한가
예전에는 `status`·`memory`·`help` 답변이 파이썬 문자열 템플릿이었다. 그래서
"이전 답변 기억나? 그리고 전산팀은 무슨 일 있어?" 처럼 두 가지를 물으면 정해진 문단이
그대로 나가고 **질문의 나머지 절반은 답변에 반영될 수 없었다.** LLM 을 쓰는데도
답변이 기계적으로 느껴진 이유가 이것이다.

## 경계 — 여기서 문장을 만드는 대상
| 대상 | 처리 |
|---|---|
| 봇 자신(`status`/`help`/`memory`/`smalltalk`) | 코드가 **사실**을 만들고 LLM 이 문장을 쓴다 |
| 아카이브 근거 답변(`search`/`summary`/`advice`) | 답변 엔진 출력을 **그대로** 쓴다 |

아카이브 답변을 다시 생성하지 않는 이유: 그 답변에는 검증된 `출처:` 가 붙어 있다.
문장을 다시 쓰면 출처와 본문이 어긋날 수 있고 그건 원칙 2(출처 강제) 위반이다.
같은 이유로 이 모듈은 아카이브 원문을 입력으로 받지 않는다 — 요약 재귀와 무관하다.

## LLM 이 죽어도 답은 나간다
호출이 실패하면 호출자가 넘긴 `fallback` 문장을 그대로 쓴다. 오늘(401) 같은 상황에서
`상태` 질문이 침묵하면 안 된다 — 하필 그때 가장 필요한 질문이다.
"""
from __future__ import annotations

import json
import logging

from .gateway.base import Message, Sensitivity
from .gateway.router import Router

logger = logging.getLogger("tybot.compose")

# 문장만 쓰는 작업이라 저가 모델로 충분하다. 없으면 라우터가 기본 모델로 폴백한다.
WRITER_MODEL = "claude-haiku-4-5-20251001"

WRITER_PROMPT = """너는 사내 Slack 아카이브 봇의 **대변인**이다. 아래 <사실> 만 근거로
사용자 질문에 답하는 문장을 쓴다.

규칙:
- <사실> 에 없는 내용을 만들지 않는다. 숫자·이름·경로를 추측하지 않는다.
- 사용자가 물은 것에 먼저 답한다. 묻지 않은 정보를 늘어놓지 않는다.
- Slack 메시지다. 3~6문장. 굵게(*표시*)는 꼭 필요할 때만.
- 존댓말. 사족·인사·자기소개 없이 바로 내용부터.
- 사실이 부족해 답할 수 없으면 그 사실을 명확히 말한다.

출력은 답변 문장만. JSON·코드펜스·머리말 금지."""


def write_from_facts(
    router: Router | None,
    *,
    question: str,
    facts: dict,
    fallback: str,
    max_tokens: int = 500,
) -> str:
    """사실 dict + 질문 -> 답변 문장. 실패하면 `fallback` 을 돌려준다.

    `facts` 는 코드가 만든 값만 담는다(가동시간·문서 수·정책 문구 등). 아카이브 원문이나
    이전 답변은 넣지 않는다 — 넣으면 요약 재귀와 권한 우회의 통로가 된다.
    """
    if router is None:
        return fallback
    body = json.dumps(facts, ensure_ascii=False, indent=2, default=str)
    messages = [
        Message("system", WRITER_PROMPT),
        Message("user", f"질문: {question}\n\n<사실>\n{body}\n</사실>"),
    ]
    for model in (WRITER_MODEL, None):
        try:
            resp = router.complete(
                messages,
                model=model,
                sensitivity=Sensitivity.CONFIDENTIAL,
                max_tokens=max_tokens,
            )
        except Exception as e:  # noqa: BLE001 - 문장 생성 실패로 답을 못 보내면 안 된다
            logger.warning("답변 문장 생성 실패(%s) - 기본 문구로 대체", e)
            return fallback
        text = (resp.text or "").strip()
        if text:
            logger.info("compose model=%s cost=$%.5f", resp.model, resp.cost_usd)
            return text
        logger.warning("답변 문장이 비어 있음(model=%s) - 다음 후보 시도", model)
    return fallback


def join_sections(sections: list[str]) -> str:
    """하위질문별 답변을 하나의 Slack 메시지로 합친다.

    구분선을 넣는 이유: 출처가 붙은 아카이브 답변과 봇 자신에 대한 설명이 한 덩어리로
    섞이면, 어느 문장에 어느 출처가 걸린 것인지 사람이 구별할 수 없다.
    """
    clean = [s.strip() for s in sections if s and s.strip()]
    if not clean:
        return "답변을 만들지 못했습니다."
    if len(clean) == 1:
        return clean[0]
    return "\n\n───\n\n".join(clean)


def truncated_notice(dropped: int) -> str:
    """상한을 넘겨 처리하지 않은 하위질문을 사용자에게 알린다.

    조용히 버리면 '물었는데 무시당했다' 가 된다 - 이번 개편의 출발점이 그 문제였다.
    """
    return (
        f"_질문이 여러 개라 앞의 것부터 답했습니다. 나머지 {dropped}건은 따로 물어봐 주세요._"
    )
