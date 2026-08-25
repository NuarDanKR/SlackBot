# AGENTS.md — TYBot (Codex / 에이전트 공통 규칙)

> 이 파일은 Codex 및 CLAUDE.md를 읽지 않는 도구가 참조하는 규칙이다.
> Claude Code용 상세는 `CLAUDE.md`, 도메인 표준은 `.claude/skills/`, `.claude/agents/` 참조.

## 시작하기 (에이전트·신규 참여자 공통)

```bash
# 1) 커밋 가드 활성화 — 도구와 무관하게 걸린다. 처음에 반드시 한 번
git config core.hooksPath .githooks

# 2) 환경
python3.11 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 3) 통과해야 시작할 수 있다
pytest                              # 154 tests
ruff check src tests scripts        # 0 errors
```

**작업 대상은 [`BACKLOG.md`](BACKLOG.md) 에서 고른다.** 착수 시 그 항목 상태를
`진행중 (담당/날짜)` 로 바꿔 커밋해 중복 작업을 막는다.

- 시크릿 없이도 테스트는 전부 돌아간다. `.env` 는 봇을 실제로 띄울 때만 필요하다.
- `.claude/hooks/guard.py` 는 **Claude Code 전용**이다. 다른 도구는 그 훅을 거치지 않으므로
  `.githooks/pre-commit`(도구 무관)이 실제 방어선이다. 1번을 건너뛰면 보호가 없다.

## 프로젝트
태영건설 Slack AI TFT. 본부/팀별 **멀티 워크스페이스 Slack 봇**이 대화·첨부를
수집→요약해 **중앙 MD 아카이브**에 쌓고, 모든 봇은 권한이 허용된 **중앙 아카이브의 원문만
근거로** 답한다. 멀티 LLM(Claude·GPT 등)을 게이트웨이로 라우팅하며 모델 선택을 지원한다.

## 절대 원칙 (위반 시 데이터 자산 오염 — 리뷰에서 반려)
1. **원문 보존·요약 재귀 금지** — 봇 답변/요약을 아카이브에 되먹이지 않는다. 원문만 저장.
2. **출처 강제** — 모든 답변에 `출처: #채널, 📄문서(날짜)`. 출처 없으면 근거 없는 답.
3. **권한은 막는 쪽이 기본값** — `visibility: public` 명시 없으면 비공개. 답변 생성 **이전에** 권한 필터.
4. **크로스 워크스페이스 조회는 화이트리스트만.**
5. **PII 제외** — 등기부등본·계약자 명단·주민번호 등은 아카이브 금지.
6. **시크릿은 저장소 밖** — 토큰/API키는 서버 시크릿 매니저에만. 저장소엔 `.env.example`만.
7. **사람 발언 ↔ 문서 분리**, 엇갈리면 두 시점 병기.
8. **봇 단일 인스턴스** — 같은 토큰으로 2곳 기동 금지(중복 답변·비용 2배). force push 금지.

## 코드 규칙
- 언어: Python 3.11+. 포맷/린트: `ruff`. 테스트: `pytest`.
- LLM 호출은 반드시 `src/tybot/gateway/` 게이트웨이 경유. 프로바이더 SDK를 봇 로직에 직접 사용 금지.
- Slack 연동은 Socket Mode 아웃바운드 전용.
- **봇이 자기 출력을 다시 학습·근거로 삼는 경로를 만들지 않는다.** 원래 "인바운드 금지"로 적었던
  규칙의 실제 의도가 이것이다(자가학습으로 편향이 굳는 것을 막는다). 원칙 1과 같은 축이다.
- 네트워크 노출은 **별개 축**이다. 인터넷 대상 인바운드는 여전히 금지.
  사내 VPN 내부에 한정하고 **사람이 관리·승인하는** 경로는 허용한다(2026-08-25 오너 승인,
  관리 콘솔 B-08). 조건: VPN 내부만 · 승인권자 1인 · 원문 편집 경로 없음.
- 레거시 그룹웨어 연동은 읽기 전용, 결재는 알림만(write 금지).
- 금액·날짜·기관명·사람 이름은 추론/반올림 금지, 원문 그대로.

## 커밋
- 시크릿·PII 커밋 금지. `.githooks/pre-commit` 이 차단한다(활성화는 위 1번).
- `archive/` · `qa-log/` 는 사내 대화 원문·감사기록이다. **코드 저장소에 커밋 금지.**
- `.env`는 커밋하지 않고 `.env.example`만. 예시 파일에도 **인라인 주석 금지**
  (`VAR=값  # 설명` 형태는 파서에 따라 주석이 값에 섞인다).
- 커밋/PR은 사람이 검토 후 진행. force push 금지.

## 설계 문서 지도
| 주제 | 문서 |
|---|---|
| 백로그·인계 | [`BACKLOG.md`](BACKLOG.md) |
| 구조 개요·도식 | [`README.md`](README.md) |
| 멀티 워크스페이스·권한 3층 | [`docs/multi-workspace.md`](docs/multi-workspace.md) |
| 에이전트 구조(하지 않을 것 포함) | [`docs/design/agent-architecture.md`](docs/design/agent-architecture.md) |
| DB 선택·조직 권한 | [`docs/design/db-and-acl.md`](docs/design/db-and-acl.md) |
| 그룹웨어 연동 | [`docs/design/oracle-sync.md`](docs/design/oracle-sync.md) |
| 서버 배치 | [`docs/deploy/rocky8.md`](docs/deploy/rocky8.md) |

## 판단이 필요할 때
- **원문(`## 원문` 블록)을 수정하는 변경**은 제안하지 말 것. 예외는 PII 삭제(사람 승인 + 묘비)뿐.
- **새로 포트를 여는 설계**는 오너 승인이 필요하다. 관리 콘솔(B-08)은 승인됨 —
  단 VPN 내부 한정·승인권자 1인·원문 편집 경로 없음이 조건이다.
  다른 노출 경로를 추가하려면 다시 승인을 받는다.
- 애매하면 **막는 쪽·안 하는 쪽**을 고르고 BACKLOG 에 결정 필요 항목으로 남긴다.
