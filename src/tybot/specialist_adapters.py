"""전문가 어댑터 — 프롬프트 계약 방식 (B-36/B-38).

## 왜 이 모양인가

전문 봇은 팀마다 개발자와 저장소가 다르다. 그런데 **코드를 받을 필요는 없다.**
Hermes 소스를 읽어 보면 값이 모델 가중치가 아니라 **프롬프트와 판정 규칙**에 있다.
글과 규칙은 우리 모델로 우리 근거에 대고 돌려도 같은 판정이 나온다.

그래서 전문가 = **프롬프트 파일 + 그 전문가에게 지정한 모델** 이다.
설계: [`docs/design/specialist-deployment.md`](../../docs/design/specialist-deployment.md)

얻는 것 셋.

- **런타임 의존이 없다.** 그 팀이 배포 중이어도 우리 봇은 답한다
- **근거가 우리 밖으로 나가지 않는다.** 남에게 보낼 일이 없다
- **비용이 우리 상한에 걸린다.** HTTP 로 부르면 그쪽 비용은 우리가 못 본다.
  전문가별로 싼 모델을 고를 수 있는 것도 이쪽이라야 뜻이 있다

## 프롬프트는 코드다

`specialist_prompts/*.md` 는 저장소에 있고 리뷰를 거쳐 배포된다. 콘솔에서 편집하지
않는다 — 콘솔이 프롬프트를 쓰게 하면, 답변 규칙이 코드 리뷰 없이 바뀌는 길이 생긴다.
콘솔이 정하는 것은 **어느 프롬프트를 어느 모델로, 어느 워크스페이스에서** 쓸지다.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("tybot.specialist_adapters")

PROMPT_DIR = Path(__file__).parent / "specialist_prompts"

# 근거를 프롬프트에 실을 때의 상한. 넘으면 앞에서 자른다 —
# 자르는 것이 나은 이유: 컨텍스트를 넘겨 호출이 통째로 실패하면 답이 아예 안 나간다.
MAX_EVIDENCE_CHARS = 40_000


class AdapterError(Exception):
    """어댑터를 만들 수 없다. 호출부는 마스터 답변으로 넘어간다."""


def load_prompt(key: str) -> str:
    """전문가 프롬프트. 없으면 예외 — **조용히 빈 프롬프트로 돌지 않는다.**

    빈 프롬프트로 돌면 전문가가 아니라 그냥 모델이 답하는데, 겉으로는 전문가가
    답한 것처럼 기록된다. 그러면 판정이 맞는지 보는 일 자체가 무의미해진다.
    """
    path = PROMPT_DIR / f"{key}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AdapterError(f"전문가 프롬프트가 없습니다: {key}") from exc
    # 프론트매터(--- ... ---)는 출처·버전 기록이라 모델에 보내지 않는다.
    if text.startswith("---"):
        _, _, rest = text.partition("---")
        _, _, body = rest.partition("---")
        text = body
    body = text.strip()
    if not body:
        raise AdapterError(f"전문가 프롬프트가 비어 있습니다: {key}")
    return body


def prompt_version(key: str) -> str:
    """콘솔에 보일 프롬프트 버전. 읽지 못하면 빈 문자열."""
    path = PROMPT_DIR / f"{key}.md"
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:10]:
            if line.startswith("version:"):
                return line.partition(":")[2].strip()
    except OSError:
        return ""
    return ""


def available_keys() -> set[str]:
    """프롬프트가 실제로 배포된 전문가. `ALLOWED_ADAPTERS.available` 의 근거다.

    목록을 손으로 적으면 프롬프트를 지워도 화면은 「배포됨」 이라고 말한다.
    파일이 곧 사실이다.
    """
    if not PROMPT_DIR.is_dir():
        return set()
    return {p.stem for p in PROMPT_DIR.glob("*.md")}


class PromptSpecialist:
    """프롬프트 + 지정 모델로 답하는 전문가.

    `specialist_contract.SpecialistAdapter` 를 만족한다. **출처를 붙이지 않는다** —
    계약 검사가 그것을 거부하고, 그 자리는 마스터 몫이다.
    """

    def __init__(self, key: str, router, *, model: str = "") -> None:
        self.key = key
        self.prompt = load_prompt(key)
        self._router = router
        # 비면 게이트웨이 기본 모델. 간단한 분야에 무거운 모델을 쓸 이유가 없다.
        self._model = model or ""

    def complete(self, request) -> str:
        from .gateway.base import Message, Sensitivity

        evidence = "\n\n".join(item.text for item in request.evidence)
        if len(evidence) > MAX_EVIDENCE_CHARS:
            log.warning(
                "근거가 %d자라 %d자로 자른다 key=%s",
                len(evidence),
                MAX_EVIDENCE_CHARS,
                self.key,
            )
            evidence = evidence[:MAX_EVIDENCE_CHARS]

        response = self._router.complete(
            [
                Message("system", self.prompt),
                # 근거를 먼저, 질문을 뒤에. 캐시는 접두사 일치라 이 순서라야
                # 같은 채널을 다시 물을 때 근거 부분이 캐시된다.
                Message("user", f"근거:\n{evidence}\n\n질문: {request.question}"),
            ],
            model=self._model or None,
            # 사내 근거가 실린다. 전문가라고 민감도를 낮추지 않는다.
            sensitivity=Sensitivity.CONFIDENTIAL,
            max_tokens=1024,
        )
        return response.text


def build(key: str, router, *, model: str = ""):
    """어댑터 하나 만들기. 지금은 프롬프트 방식뿐이다.

    HTTP 전송이 필요해지면(그쪽 모델·색인에 값이 있을 때) 여기에 한 갈래를 더한다.
    호출부는 어느 쪽인지 몰라도 된다 — 그게 계약을 좁혀 둔 이유다.
    """
    return PromptSpecialist(key, router, model=model)
