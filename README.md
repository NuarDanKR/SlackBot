# Hermes Hub

태영건설 Slack AI TFT — 본부/팀별 멀티 워크스페이스 Slack 봇이 대화·첨부를 수집해
중앙 MD 아카이브로 자산화하고, 권한 허용 범위의 원문만 근거로 답한다. 멀티 LLM 라우팅 지원.

- 설계 원칙·컨벤션: [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md)
- 아키텍처: [`docs/architecture.md`](docs/architecture.md)
- Claude Code 셋업: [`.claude/`](.claude/) (agents / skills / commands / hooks)

## 개발 환경
```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env    # 실제 값은 서버 시크릿 매니저에서 주입, .env 는 커밋 금지
pytest
```

## 구조
```
src/hermes_hub/
├── config.py          # 환경설정
├── gateway/           # LLM 게이트웨이 (모델 선택·민감도 라우팅·비용 가드)
├── slack/             # Slack 연동 (Socket Mode) — 뼈대
├── archive/           # MD 수집/검색 파이프라인 — 뼈대
└── access/            # ACL / 권한 필터 — 뼈대
```

## 우선 구현: LLM 게이트웨이
```python
from hermes_hub.gateway import Router, Message, Sensitivity

router = Router.from_default_registry()
resp = router.complete(
    model="claude-sonnet-5",
    messages=[Message("user", "이 채널 요약해줘")],
    sensitivity=Sensitivity.INTERNAL,
)
print(resp.text, resp.model, resp.cost_usd)
```
