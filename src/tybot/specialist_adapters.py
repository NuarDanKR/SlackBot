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

## 규칙은 두 곳에서 온다

| 우선 | 어디 | 누가 고치나 |
|---|---|---|
| 1 | `specialist_bot.rules` (DB) | 팀 개발자가 콘솔에서 요청 → 관리자 승인 |
| 2 | `specialist_prompts/<key>.md` | 우리가 저장소에서. 코드 리뷰를 거친다 |

처음에는 「콘솔에서 편집하지 않는다」 로 설계했다. 답변 규칙이 **코드 리뷰 없이
바뀌는 길**이 생긴다는 이유였다. 그 걱정은 이미 해소돼 있다 — 콘솔 변경은
`specialist_change_request` 를 지나 승인을 받고 감사 기록에 남는다. 리뷰의 자리가
git 에서 콘솔로 옮겨간 것이지 없어진 것이 아니다.

파일을 남겨 두는 이유는 **콘솔이 비어 있어도 돌아야** 하기 때문이다. 콘솔에서 규칙을
지우면 파일로 되돌아가고, 둘 다 없으면 거부한다.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("tybot.specialist_adapters")

# 프롬프트 계약이 사는 두 자리.
#
# **`subbots/<key>/contract/prompt.md` 가 정식**이다. 콘솔이 버전·검사·승인을
# 관리하는 전문 봇은 거기 둔다 — 다른 팀에게서 **받은 계약**이고, 우리 코드 옆에
# 두면 "우리가 만든 프롬프트" 처럼 보인다. 받은 것은 받은 자리에 둔다
# (`subbots/README.md`).
#
# `src/tybot/specialist_prompts/` 는 남겨 둔다. 그 경로가 패키지 안이라 어떤
# 설치 형태에서도 따라오고, 저장소 밖에서 도는 경우의 마지막 보루다.
# **두 곳에 같은 key 가 있으면 안 된다** — 어느 쪽이 도는지 아무도 모르게 되고,
# 고친 쪽이 안 도는 상태가 조용히 생긴다. 테스트가 그것을 막는다.
PROMPT_DIR = Path(__file__).parent / "specialist_prompts"
SUBBOTS_DIR = Path(__file__).resolve().parents[2] / "subbots"


def contract_path(key: str) -> Path | None:
    """이 전문가의 프롬프트 계약 파일. 없으면 `None`.

    key 를 경로에 붙이기 전에 **형식을 본다.** DB 나 화면을 통해 온 값이라도
    그대로 붙이면 그 자리가 곧 경로 탈출이다.
    """
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", key or ""):
        return None
    official = SUBBOTS_DIR / key / "contract" / "prompt.md"
    if official.is_file():
        return official
    legacy = PROMPT_DIR / f"{key}.md"
    return legacy if legacy.is_file() else None

# 근거를 프롬프트에 실을 때의 상한. 넘으면 앞에서 자른다 —
# 자르는 것이 나은 이유: 컨텍스트를 넘겨 호출이 통째로 실패하면 답이 아예 안 나간다.
MAX_EVIDENCE_CHARS = 40_000

# 전문 봇별 계약보다 상위에 있는 마스터 출력 정책. 전문 봇 소스와 버전을 바꾸지 않고
# 모든 어댑터에 동일하게 적용한다. 출처 링크는 여전히 마스터가 별도로 붙인다.
MASTER_OUTPUT_POLICY = """

## TYBot 마스터 출력 정책

사람의 평가·의견·판단을 옮길 때는 근거에 적힌 발언자와 날짜를 함께 씁니다.
누군가의 평가를 전문 봇 자신의 평가처럼 바꾸어 쓰지 않습니다. 근거에 발언자가
없으면 평가 주체를 알 수 없다고 밝힙니다.
"""


def _governed_prompt(prompt: str) -> str:
    return prompt.rstrip() + MASTER_OUTPUT_POLICY


class AdapterError(Exception):
    """어댑터를 만들 수 없다. 호출부는 마스터 답변으로 넘어간다."""


def load_prompt(key: str) -> str:
    """전문가 프롬프트. 없으면 예외 — **조용히 빈 프롬프트로 돌지 않는다.**

    빈 프롬프트로 돌면 전문가가 아니라 그냥 모델이 답하는데, 겉으로는 전문가가
    답한 것처럼 기록된다. 그러면 판정이 맞는지 보는 일 자체가 무의미해진다.
    """
    path = contract_path(key)
    if path is None:
        raise AdapterError(f"전문가 프롬프트가 없습니다: {key}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AdapterError(f"전문가 프롬프트를 읽지 못했습니다: {key}") from exc
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
    path = contract_path(key)
    if path is None:
        return ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[:10]:
            if line.startswith("version:"):
                return line.partition(":")[2].strip()
    except OSError:
        return ""
    return ""


def deployed_keys(console_keys: set[str] | None = None) -> set[str]:
    """규칙이 있는 전문가. 파일 **또는** 콘솔.

    콘솔에서 규칙을 넣은 전문가는 파일이 없어도 돈다 — 그것이 「팀이 자기 규칙으로
    답한다」 의 뜻이다. 두 곳을 합쳐야 화면의 「배포됨」 이 사실과 맞는다.
    """
    return available_keys() | (console_keys or set())


def available_keys() -> set[str]:
    """프롬프트가 실제로 배포된 전문가. `ALLOWED_ADAPTERS.available` 의 근거다.

    목록을 손으로 적으면 프롬프트를 지워도 화면은 「배포됨」 이라고 말한다.
    파일이 곧 사실이다.
    """
    keys: set[str] = set()
    if SUBBOTS_DIR.is_dir():
        # 정식 자리. `subbots/<key>/contract/prompt.md` 가 있어야 배포된 것이다 —
        # 디렉터리만 있고 계약이 없으면 「등록했는데 답을 못 한다」 가 된다.
        keys |= {
            path.parents[1].name
            for path in SUBBOTS_DIR.glob("*/contract/prompt.md")
        }
    if PROMPT_DIR.is_dir():
        keys |= {p.stem for p in PROMPT_DIR.glob("*.md")}
    return keys


class PromptSpecialist:
    """프롬프트 + 지정 모델로 답하는 전문가.

    `specialist_contract.SpecialistAdapter` 를 만족한다. **출처를 붙이지 않는다** —
    계약 검사가 그것을 거부하고, 그 자리는 마스터 몫이다.
    """

    def __init__(
        self, key: str, router, *, model: str = "", rules: str = ""
    ) -> None:
        self.key = key
        # 콘솔에서 넣은 규칙이 있으면 그것을, 없으면 저장소의 프롬프트 파일을 쓴다.
        # **둘 다 없으면 거부한다** — 빈 지시문으로 돌면 전문가가 아니라 그냥 모델이
        # 답하는데 기록에는 전문가로 남는다.
        self.prompt = rules.strip() or load_prompt(key)
        self.source = "console" if rules.strip() else "file"
        self._router = router
        # 비면 게이트웨이 기본 모델. 간단한 분야에 무거운 모델을 쓸 이유가 없다.
        self._model = model or ""
        # 계약(`SpecialistAdapter`)은 문장만 돌려준다. 어느 모델이 얼마에 답했는지는
        # 감사기록과 사용량에 남아야 하므로 여기에 둔다 — 호출부가 뒤에 읽는다.
        self.last_model = ""
        self.last_cost_usd = 0.0

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
                Message("system", _governed_prompt(self.prompt)),
                # 근거를 먼저, 질문을 뒤에. 캐시는 접두사 일치라 이 순서라야
                # 같은 채널을 다시 물을 때 근거 부분이 캐시된다.
                Message("user", f"근거:\n{evidence}\n\n질문: {request.question}"),
            ],
            model=self._model or None,
            # 사내 근거가 실린다. 전문가라고 민감도를 낮추지 않는다.
            sensitivity=Sensitivity.CONFIDENTIAL,
            # **thinking 이 이 예산을 함께 쓴다.** 현재 모델들은 사고가 기본으로
            # 켜져 있고(Opus 5 는 끌 수도 없는 기본값), 그 토큰이 `max_tokens` 에서
            # 나간다. 1024 로 두면 사고하다 예산이 끝나 본문이 비고, 계약이 그것을
            # 「빈 응답」 으로 막아 **매번 마스터로 폴백**한다 — 오류는 안 나고
            # 전문가만 조용히 안 쓰인다.
            #
            # 실제로 쓴 만큼만 과금되므로 상한을 올리는 것 자체의 비용은 없다.
            max_tokens=8192,
        )
        self.last_model = response.model
        self.last_cost_usd = response.cost_usd
        return response.text


def build(
    key: str,
    router,
    *,
    model: str = "",
    rules: str = "",
    execution_mode: str = "prompt",
    toolbox=None,
    live: bool = False,
):
    """어댑터 하나 만들기. 호출부는 어느 갈래인지 몰라도 된다.

    `tools` 인데 도구 묶음이 없으면 **프롬프트로 내려간다.** 여기서 예외를 내면
    도구를 못 만든 사정(스토어 없음 등) 하나가 전문가를 통째로 끄는데, 그보다는
    마스터가 고른 근거로라도 답하는 편이 낫다.
    """
    if execution_mode == "tools":
        if toolbox is None:
            log.warning("도구 묶음이 없어 프롬프트로 내려간다 key=%s", key)
        else:
            return ToolSpecialist(
                key, router, toolbox=toolbox, model=model, rules=rules, live=live
            )
    return PromptSpecialist(key, router, model=model, rules=rules)


# --- 도구를 갖춘 전문가 (A+) --------------------------------------------------
#
# `PromptSpecialist` 는 마스터가 **고른 근거**로 한 번 답한다. Hermes 는 그렇게
# 동작하지 않는다 — 검색하고, 읽고, 모자라면 다시 검색한다(`ref/hermes` 소스,
# 2026-09-11 확인). 그 루프가 그 봇의 값이고, 프롬프트 한 장으로는 못 옮긴다.
#
# 그래서 도구는 우리가 만들고(`specialist_tools`) 설명문과 규칙만 가져온다.
# 권한은 도구 안에서 `RequestContext` 로 한 번만 판정된다.

# 루프 상한. **없으면 모델이 검색을 무한히 돈다** — 비용도 지연도 상한이 없어진다.
MAX_TOOL_ROUNDS = 8
# 루프 전체 예산. 사고가 켜져 있는 모델은 한 회차가 크다.
TOOL_MAX_TOKENS = 8192


class ToolSpecialist:
    """도구를 부르며 스스로 근거를 찾는 전문가.

    `specialist_contract.SpecialistAdapter` 를 만족한다. **출처를 붙이지 않는다** —
    무엇을 실제로 읽었는지는 `toolbox.touched` 에 남고, 출처는 마스터가 그것으로
    만든다. 모델이 본문에 적은 것을 믿고 출처를 만들면 그게 곧 환각이다.
    """

    def __init__(
        self,
        key: str,
        router,
        *,
        toolbox,
        model: str = "",
        rules: str = "",
        live: bool = False,
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self.key = key
        self.prompt = rules.strip() or load_prompt(key)
        self.source = "console" if rules.strip() else "file"
        self._router = router
        self._model = model or ""
        self._toolbox = toolbox
        self._live = live
        self._max_rounds = max_rounds
        self.last_model = ""
        self.last_cost_usd = 0.0
        self.rounds = 0

    @property
    def touched(self):
        """무엇을 읽었나. 출처를 만드는 쪽이 읽는다."""
        return self._toolbox.touched

    def complete(self, request) -> str:
        from .gateway.base import Message, Sensitivity
        from .specialist_tools import specs

        tools = specs(live=self._live)
        # 마스터가 이미 고른 근거가 있으면 함께 준다. 없어도 된다 —
        # 도구로 스스로 찾는 것이 이 어댑터의 전제다.
        seed = "\n\n".join(item.text for item in request.evidence)[:MAX_EVIDENCE_CHARS]
        opening = f"질문: {request.question}"
        if seed:
            opening = f"이미 찾아 둔 근거:\n{seed}\n\n{opening}"

        messages: list = [
            Message("system", _governed_prompt(self.prompt)),
            Message("user", opening),
        ]

        for _ in range(self._max_rounds):
            self.rounds += 1
            response = self._router.complete(
                messages,
                model=self._model or None,
                sensitivity=Sensitivity.CONFIDENTIAL,
                max_tokens=TOOL_MAX_TOKENS,
                tools=tools,
            )
            self.last_model = response.model
            self.last_cost_usd += response.cost_usd

            if not response.wants_tools:
                return response.text

            # 모델이 만든 블록을 **그대로** 되돌려 넣는다. 텍스트만 넣으면
            # tool_use 와 tool_result 의 짝이 깨져 다음 호출이 400 이다.
            messages.append(Message("assistant", _assistant_blocks(response)))
            messages.append(Message("user", [
                {
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": self._toolbox.run(call.name, call.input),
                }
                for call in response.tool_calls
            ]))

        # 상한에 걸렸다. **도구 없이 한 번 더 물어 지금까지 읽은 것으로 답하게 한다.**
        # 도구를 계속 주면 또 부르고, 상한이 상한이 아니게 된다.
        #
        # 그래도 비면 빈 문자열을 돌려준다 — 계약 검사가 그것을 위반으로 보고
        # 마스터가 답한다. 모자란 채로 억지 문장을 만드는 것보다 낫다.
        log.warning("전문가 도구 루프 상한 key=%s rounds=%d", self.key, self.rounds)
        messages.append(Message(
            "user",
            "더 찾지 말고 지금까지 읽은 것으로 답하세요. 모자라면 모자라다고 쓰세요.",
        ))
        final = self._router.complete(
            messages,
            model=self._model or None,
            sensitivity=Sensitivity.CONFIDENTIAL,
            max_tokens=TOOL_MAX_TOKENS,
        )
        self.last_model = final.model
        self.last_cost_usd += final.cost_usd
        return final.text


def _assistant_blocks(response) -> list[dict]:
    """모델 turn 을 대화에 되돌려 넣을 블록으로. 텍스트와 tool_use 둘 다 필요하다."""
    blocks: list[dict] = []
    if response.text.strip():
        blocks.append({"type": "text", "text": response.text})
    blocks.extend(
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
        for call in response.tool_calls
    )
    return blocks
