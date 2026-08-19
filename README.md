# TYBot (Hermes Hub)

태영건설 Slack AI TFT — 본부/팀별 멀티 워크스페이스 Slack 봇이 대화·첨부를 수집해
중앙 MD 아카이브로 자산화하고, 권한 허용 범위의 원문만 근거로 답한다. 멀티 LLM 라우팅 지원.

- 설계 원칙·컨벤션: [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md)
- 아키텍처: [`docs/architecture.md`](docs/architecture.md)
- Claude Code 셋업: [`.claude/`](.claude/) (agents / skills / commands / hooks)
- **파일럿 런북(지금 여기부터)**: [`docs/pilot/README.md`](docs/pilot/README.md) — 워크스페이스 1개 + `@tybot`
- 서버 배치(Rocky Linux 8): [`docs/deploy/rocky8.md`](docs/deploy/rocky8.md)
- **멀티 워크스페이스 · 크로스 열람 권한**: [`docs/multi-workspace.md`](docs/multi-workspace.md)
- 에이전트 구조(마스터봇·정리봇 검토): [`docs/design/agent-architecture.md`](docs/design/agent-architecture.md)
- DB 선택·조직 권한 설계: [`docs/design/db-and-acl.md`](docs/design/db-and-acl.md)
- PostgreSQL vs MariaDB 비교: [`docs/design/postgres-vs-mariadb.md`](docs/design/postgres-vs-mariadb.md)

## 개발 환경
```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env    # 실제 값은 서버 시크릿 매니저에서 주입, .env 는 커밋 금지
pytest
```

## 구조
```
src/tybot/
├── config.py           # 환경설정
├── answer.py           # 질의응답 파이프라인 (환각방지 4겹)
├── intent.py           # 의도 분류 (LLM + 규칙 폴백)
├── audit.py            # 질의응답 감사 기록
├── workspaces.py       # 멀티 워크스페이스 설정 · 크로스 열람 화이트리스트
├── gateway/            # LLM 게이트웨이 (모델 선택·민감도 라우팅·비용 가드)
├── slack/pilot.py      # 파일럿 봇 (Socket Mode, 단일 워크스페이스)
├── archive/            # store.py=원문 검색 / writer.py=원문 수집
└── access/             # ACL / 권한 필터
```

## 우선 구현: LLM 게이트웨이
```python
from tybot.gateway import Router, Message, Sensitivity

router = Router.from_default_registry()
resp = router.complete(
    model="claude-sonnet-5",
    messages=[Message("user", "이 채널 요약해줘")],
    sensitivity=Sensitivity.INTERNAL,
)
print(resp.text, resp.model, resp.cost_usd)
```
