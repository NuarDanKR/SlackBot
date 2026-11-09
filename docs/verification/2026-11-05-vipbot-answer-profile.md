# VIPBot 답변 프로필 인계

## 이번에 고정한 것

- 프로필 revision: `vip-executive-v2`
- v2는 ACL 검증을 마친 전문봇 성공 답변도 VIP 경영진 형식으로 표시한다. 전문봇
  원문을 근거로만 재구성하고, 시스템이 보유한 출처와 EvidenceRef는 후처리에
  전달하지 않으며 그대로 보존한다. 후처리 실패 시 전문봇 원문으로 복귀한다.
  렌더링 전후 숫자·금액·날짜·비율·건수 토큰이 정확히 같지 않거나 모델이
  `출처:` 줄을 만들면 형식 변환을 폐기하고 원문으로 복귀한다.
- 모델: `claude-opus-5-5`만 허용
- 사용자 `--model=...` 지정은 VIP 런타임에서 모두 제거
- 출력 상한: 네 답변 경로 모두 1,800 tokens. TYBot 기본 경로는 기존 상한을 그대로 유지
- 일일 비용 상한: `VIPBOT_DAILY_LIMIT_USD`를 반드시 별도로 지정. 없거나 0 이하이면
  `vip_cost_limit_unconfigured` 또는 `vip_cost_limit_invalid`로 기동 전 차단
- 비용 누적 원장: `/var/lib/tybot-vipbot/cost-state.json`. TYBot 원장과 공유하지 않으며
  재시작 뒤에도 당일 누적 비용을 복원한다.
- 일반 답변·요약·판단·임시 판단 모두 같은 경영진 계약을 받는다

경영진 계약은 결론 우선(BLUF), 날짜·담당 조직 병기, 사실과 분석 분리, 재무 영향·운영
리스크·대안 비교, 2~3개 의사결정안, 미확인 범위 명시를 요구한다. 숫자·날짜·인과관계는
원문 밖에서 만들지 않고 출처는 기존 원문 좌표 코드만 붙인다.

## 코드 경계

- `src/tybot/vip_profile.py`: 모델·revision·프롬프트·비용/출력 상한 계약
- `src/tybot/answer.py`: 기본값을 바꾸지 않는 `AnswerPrompts` 주입점과 출력 상한
- `tests/test_vip_profile.py`: 약한/미승인 모델, 비용 상한 누락, 사용자 모델 override,
  경영진 프롬프트 규칙, provider/DB/Slack 직접 의존 금지

## 아직 연결하지 않은 것

별도 Socket Mode 런타임이 생길 때 다음 값을 그대로 사용해야 한다.

```python
profile = load_profile()
router = Router.from_default_registry(
    default_model=profile.model,
    daily_limit_usd=profile.daily_limit_usd,
    cost_state_path=profile.cost_state_path,
)
engine = AnswerEngine(
    ...,
    prompts=profile.prompts,
    max_output_tokens=profile.max_output_tokens,
)
question = question_for_engine(question)
```

이 연결 전에는 프로필이 운영 호출에 적용됐다고 표시하면 안 된다. 특히 전문봇이 직접
완성한 답변에는 `AnswerEngine`의 시스템 프롬프트가 적용되지 않는다. VIP 런타임에서
전문봇 결과를 그대로 전달할지, 같은 원문으로 VIP compose 단계를 둘지 결정해야 한다.
AI 답변 문장을 다시 근거로 넣는 방식은 원문 재귀 금지 원칙 때문에 허용하지 않는다.
