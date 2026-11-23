# TYBot 입문 가이드 — 개발을 처음 접하는 사람을 위한 전체 정리

> 작성 기준일: 2026-11-12. 코드·README·CLAUDE.md·docs/design 문서를 읽고 정리했다.
> 세부 수치(테스트 개수 등)와 구현 진행 상태는 계속 바뀌므로, 최종 기준은 항상
> [`BACKLOG.md`](../BACKLOG.md)와 각 설계 문서다.

---

## 0. 이 문서를 읽는 법

| 시간이 없다면 | 읽을 장 |
|---|---|
| 10분 | 1장(한 줄 요약) → 2장(비유) → 3장(질문 한 건의 여행) |
| 1시간 | 1~8장 |
| 개발에 투입된다 | 전부 + 15장(개발 환경) + 16장(읽는 순서) |

모르는 단어는 **17장 용어집**에 모아 두었다.

---

## 1. TYBot 은 무엇인가 (한 줄 요약)

> **Slack 대화와 첨부파일을 "원문 그대로" 모아 두고, 사용자가 질문하면 _그 사람이 볼 권한이 있는 원문만_
> 근거로 답하는 사내 AI 봇.** 근거가 없으면 지어내지 않고 "없다"고 말한다.

태영건설 Slack AI TFT 프로젝트이고, 원래 단일 워크스페이스에서 돌던 봇(Hermes)을
**본부·팀별 여러 워크스페이스 + 중앙 아카이브 + 여러 LLM** 구조로 확장한 것이다.

핵심 가치는 세 가지다.

1. **환각(거짓 답) 방지** — 원문에 없는 건 답하지 않는다.
2. **권한 보호** — 내가 못 보는 채널의 내용이 답변으로 새어 나오면 안 된다.
3. **운영 가능성** — 개발자가 없어도 관리 콘솔에서 운영할 수 있어야 한다.

---

## 2. 비유로 이해하기: "회사 서기관 + 문서고 + 사서"

| 현실 비유 | TYBot 에서 | 코드 위치 |
|---|---|---|
| **서기관** — 회의를 받아 적는다. 요약하지 않고 말한 그대로 | 수집기(writer): Slack 메시지를 원문 MD 파일에 append | `archive/writer.py` |
| **문서고** — 날짜별 서류철, 수정 금지 | 아카이브(MD 파일 디렉터리) | `/var/lib/tybot/archive/...` |
| **출입 통제** — 신분증을 확인해 볼 수 있는 서류철만 꺼내 준다 | 권한 필터 | `access/__init__.py` |
| **사서** — 질문을 듣고 서류철에서 관련 줄을 찾아 읽어 준다 | 답변 엔진 | `answer.py` |
| **통역사/전문가** — 사서가 찾아 준 자료를 읽고 문장을 다듬는다 | LLM / 전문 봇(Hermes) | `gateway/`, `specialist_*.py` |
| **감사실** — 누가 뭘 물었고 어떤 근거로 답했는지 기록 | 감사 기록 | `audit.py` |
| **총무과 콘솔** — 서기관·사서의 상태를 보는 관리 화면 | 관리 콘솔 | `console/`, `console-web/` |

가장 중요한 규칙: **사서(AI)는 서류철에 없는 이야기를 하면 안 되고, 서기관은 사서가 한 말을 서류철에 다시
적으면 안 된다.** (봇이 자기 말을 근거로 삼아 점점 틀려지는 "자기 되먹임"을 막기 위해서다.)

---

## 3. 질문 한 건의 여행 (가장 중요한 그림)

사용자가 Slack 에서 `@tybot 김해외동 기성금 얼마야?` 라고 물었을 때:

```
① Slack ──(Socket Mode)──▶ slack/pilot.py        이벤트 수신 (app_mention)
② 의도 분류  intent.py     "이 질문은 search(검색)인가, summary(요약)인가…" (저가 LLM, 실패 시 정규식)
③ 요청자 권한 컨텍스트 생성  access.RequestContext  (어느 워크스페이스, 어느 채널, 어떤 역할)
④ 권한 필터  can_access()   ★ 답변 생성 "이전"에, 볼 수 있는 문서로만 범위를 줄인다
⑤ 원문 검색  archive/store.py + search_index.py   핵심어로 raw 줄을 찾는다
⑥ 근거 0건?  ──예──▶ "추측하지 않는다": 문서 목록 + 다음 행동 안내만 (LLM 호출 없음)
             └아니오─▶ ⑦
⑦ 답변 생성  gateway/router.py → Claude 등       근거 줄만 프롬프트에 넣는다
             (업무 질문은 전문 봇(Hermes)이 문장을 쓰고, 마스터는 권한·근거·출처를 소유)
⑧ 출처 부착  Answer.to_slack()   "출처: [조직명]업무명, 📄문서(날짜)" — 코드가 붙인다
⑨ PII 마스킹 sensitive_mask.py   전달 직전에도 한 번 더
⑩ 감사 기록  audit.py            질문·의도·권한범위·근거·모델·비용을 JSONL + 일자별 MD 로
```

> **순서는 바꾸지 말 것** — `answer.py` 첫 docstring 에 명시돼 있다.
> 권한 필터를 LLM 호출 뒤에 두면, 이미 권한 밖 내용을 LLM 이 읽은 뒤라 막을 수 없다.

### 의도(kind) 종류

| kind | 의미 | LLM 답변 생성? |
|---|---|---|
| `search` | 특정 사실 검색 ("기성금 얼마야?") | 예 (근거 있을 때) |
| `summary` | 기간 요약 ("이번주 진행상황") | 예 |
| `advice` | 판단·권고 ("쪼개는 게 나아?") | 예 + `일반적인 판단` 라벨을 **코드가** 붙임 |
| `status` | 봇 상태 ("잘 돌아가?") | 결정적 블록 + 짧은 문장 |
| `help` | 사용법 | 아니오 (없는 명령을 지어내지 않도록) |
| `memory` | "이전 답변 기억나?" | 기억 정책을 사실로 설명 |
| `smalltalk` / `out_of_scope` | 잡담 / 범위 밖 | 간단 안내 / 거절 |
| `ingest`, `ingest_all` | `수집`, `전체수집` 명시 명령 | **LLM 이 분류로 트리거하지 않음** (쓰기는 명시 명령만) |

복합 질문은 최대 3개 하위질문으로 쪼개서 각각 답한다. 분해가 실패하면 정규식으로 폴백한다.

---

## 4. 시스템 전체 구조

```
┌─ Slack ─────────────────────────────┐
│ 워크스페이스마다 앱 1개 (Socket Mode) │   ← 봇이 Slack 으로 "나가는" WebSocket
└───────────────┬─────────────────────┘     인바운드 포트 0개
                ▼
┌─ 서버 (Rocky Linux 8) ───────────────────────────────────────────┐
│ tybot.service       : WorkspaceBot × N   (python -m tybot.slack.pilot)
│ 정기 잡(systemd timer): 점검·백필·색인·변환·검토DM·일정동기화 ...
│ tybot-console.service: 관리 콘솔 API (FastAPI) + React 화면, nginx TLS 뒤
│ 변환 워커            : 첨부 → 텍스트 (격리된 별도 계정/venv)
│ 전문 봇(Hermes 등)   : Unix socket 으로 호출 (tybot-specialist@.service)
└───────┬────────────────────┬──────────────────┬─────────────────┘
        ▼                    ▼                  ▼
  /var/lib/tybot        PostgreSQL          LLM API (Anthropic 등)
  archive/ (원문 MD)    (색인·설정·ACL·      ※ 호출은 전부 gateway 경유
  qa-log/ (감사)         계정·큐·승인)
```

### 저장소 3종과 역할

| 저장소 | 무엇이 들어있나 | 진실의 원천(source of truth)? |
|---|---|---|
| **MD 파일 아카이브** | Slack 원문, 일자별 | **예.** 원문 = 진실 |
| **PostgreSQL** | 검색 색인(pg_bigm, pgvector), 워크스페이스·권한·계정·큐·승인 상태, 조직/인사 스냅샷 | 아니오(재구성 가능한 파생 + 설정) |
| **qa-log/** | 질문·답변·근거·비용 감사 기록 | 감사 자료일 뿐 **아카이브가 아님** (근거로 쓰지 않음) |

### 아카이브 디렉터리 (v2)

```
/var/lib/tybot/
├─ archive/workspaces/<ws>/channels/<채널ID>__<이름>/raw/2026-08-31.md   ← 답변의 유일한 근거
├─ archive/workspaces/<ws>/dm/<사용자ID>/ ...                            ← 봇과의 DM (본인 전용)
├─ staging/ ... attachments/<file-id>/{metadata.json, extracted.md}      ← 첨부 변환 중간물
├─ objects/ ... 첨부 원본 바이트 (검색 안 함)
└─ derived/summaries/ ...                                                 ← 요약(파생, 후보 탐색용)
```

- **채널 ID가 소유 키**다. 채널 이름이 바뀌어도 디렉터리는 그대로.
- 원문 파일 형식(요지):
  ```yaml
  ---
  schema_version: 2
  workspace: tyit
  channel: "#팀-전산_ABB110-회의"
  channel_id: C0123456789
  visibility: private     # 기본값은 항상 private
  share_with: []
  ---
  ## 원문 (자동 취합, 편집 금지)
  > [2026-08-31 09:15] 홍길동: ...
  ```

---

## 5. 절대 원칙 (코드 리뷰에서 반려되는 사유)

[`CLAUDE.md`](../CLAUDE.md) / [`AGENTS.md`](../AGENTS.md) 에 있는 규칙을 초보자 눈높이로 풀었다.

| # | 원칙 | 왜 | 지켜지는 곳 |
|---|---|---|---|
| 1 | **원문만 저장, 봇 출력은 근거 금지** | 봇이 자기 글을 근거로 삼으면 오류가 증폭된다 | `archive/writer.py`(봇 발언 배제) |
| 1+ | 단, **사람이 승인한** 요약은 다음 단계 입력 가능 (5조건: 사람 승인, 원문 좌표 상속, 묶음 상한, 세대 상한 2, 단일 채널) | 2026-09-17 오너 결정 | `summary_review.py` 등 |
| 2 | **출처 강제** `출처: [조직]업무, 📄문서(날짜)` | 출처 없는 답 = 근거 못 찾은 답 | `answer.py`, `channels.source_label` |
| 3 | **권한은 막는 쪽이 기본값** | 공개 표시 없으면 비공개 | `access/` |
| 4 | **크로스 워크스페이스는 화이트리스트만** | 팀 간 자료 유출 방지 | `workspaces.py` (`CROSS_WS_READ`) |
| 5 | **PII는 가린 뒤 보관, 못 가리면 격리** | 주민번호 등. 2026-11-10 "있으면 전체 금지"에서 "가린 뒤 보관"으로 변경 | `sensitive_mask.py`, `pii_screen.py` |
| 6 | **시크릿은 저장소 밖** | 토큰 유출 방지 | `.env.example`만 커밋, `.gitignore`, 훅 |
| 7 | 사람 발언 ↔ 문서 분리, 엇갈리면 두 시점 병기 | | |
| 8 | **봇 단일 인스턴스** | 2곳에서 켜면 중복 답변 + 비용 2배 | `lock.py` |

> 초보자 체크포인트: 새 기능을 만들다가 "이 결과를 아카이브에 저장하면 편한데…" 싶으면 **멈추고** 원칙 1을 본다.

---

## 6. 환각 방지 4겹

| 겹 | 규칙 | 코드 |
|---|---|---|
| 1 | 근거 오염 금지 (봇 발언·PII·qa-log 를 아카이브에 넣지 않음) | `archive/writer.py` |
| 2 | 답 전에 **원문 줄**을 연다 (요약 섹션은 근거 제외) | `archive/store.py` |
| 3 | "없다" 남용 금지 — 0건이면 문서 목록과 다음 행동 안내, **LLM 호출 없음** | `answer.py` (`_no_evidence`) |
| 4 | 사람이 잡을 수 있게 — 질문·근거·권한·비용 전부 기록, 출처 강제 | `audit.py` |

추가로 `근거 보기` 버튼(`evidence_view.py`)이 있어 사용자가 답변이 실제로 읽은 원문을 직접 열어 볼 수 있다.

---

## 7. 권한 모델 — 3개의 독립된 축

```
문서 열람 요청
  │
  ├─ 소유 워크스페이스 ≠ 요청자 워크스페이스
  │     ├─ CROSS_WS_READ 화이트리스트에 없다 → 차단 (채널명조차 노출 안 함)
  │     └─ 있다 → root 워크스페이스면 허용 / 아니면 문서의 share_with 에 요청자 WS가 있어야 허용
  │
  └─ 같은 워크스페이스
        ├─ root → 허용
        ├─ visibility: public → 허용
        └─ 그 채널 멤버 → 허용, 아니면 차단
```

| 축 | 통제 | 누가 정하나 |
|---|---|---|
| 채널 멤버십 | 같은 워크스페이스 내 어느 채널을 보나 | Slack 초대 |
| `share_with` | 이 문서를 어느 동등 워크스페이스에 넘길지 | 자료 소유 쪽 |
| root 워크스페이스 | 산하 자료를 취합 열람하는 상위 조직 (임원용) | 콘솔 관리자 |

추가 규칙 (코드 `can_access()` 에서 **맨 앞**에 처리):

- **DM 은 본인 전용** — 봇과의 DM 에 쌓인 기록은 본인이 DM 에서 물을 때만 근거가 된다. exec·root 도 예외 없음.
  채널에서 물으면 본인 것이라도 나오지 않는다(답을 채널 사람들이 함께 보기 때문).
- **채널에서 온 질문**은 그 워크스페이스의 **그 채널**만 근거로 삼는다. 다른 채널로 넓히지 않는다.
- **VIPBot 용** `evidence_sources`: 사람의 멤버십이 아니라 관리자가 고른 정확한 (워크스페이스, 채널ID) 쌍만 읽는다.
- 멤버십 확인에 실패하면(`permission_unverified`) 더 좁게 동작하고, "자료가 없다"와 "범위를 모른다"를 다르게 안내한다.

---

## 8. 데이터가 들어오는 길 (수집 파이프라인)

### 8-1. 무엇이 수집되나

- **채널 이름 규칙**에 맞고 **봇이 초대된** 채널만. 규칙: `#<두문자>-<조직명>_<조직코드>-<업무>`
  (예: `#팀-전산_ABB110-주간회의`). 이름 규칙 로직은 `channels.py`, 검증 스킬은 `channel-naming`.
- 공개 채널는 규칙에 맞으면 봇이 **스스로 참여**(`autojoin.py`). 비공개 채널은 초대 필수.
- `/채널 생성`(업무 채널 만들기)으로 만들면 봇이 직접 만들어서 처음부터 멤버가 된다(`channel_management.py`).

### 8-2. 네 가지 수집 경로

| 경로 | 무엇 | 비고 |
|---|---|---|
| **실시간**(본선) | message 이벤트가 오는 즉시 원문 append | `slack/pilot.py` `_ingest_live` |
| **정기 백필** | 업무시간 매시 정시, 단절 구간 보충 | `collect.py`, `tybot-collect.timer` |
| **수동 `수집`** | 최근 15건 + 스레드 답글 | 봇 멘션 명령 |
| **관리자 소급** | 초대 이전 과거 전체 | `scripts/backfill_channel_history.py` 또는 콘솔 |

Slack 신규 앱은 `conversations.history` 호출이 분당 1요청/15건 수준으로 제한되기 때문에 실시간 이벤트가 본선이다.

### 8-3. 첨부파일

```
Slack 파일 → 다운로드 → (격리된) 변환 워커 → 텍스트 추출 → PII 검사/마스킹 → 첨부 정본 문서
   xlsx·docx·pptx·pdf·hwpx → 변환 성공           실패/미지원 → 격리 보관 + 알림
   스캔 PDF·구형 hwp·이미지 → OCR/Vision(선택, 승인·정책 게이트 뒤) 또는 목록만
```

- 변환 실패·누락은 채널 단위로 묶어 알림(`conversion_alerts.py`), 재처리 큐(`conversion_queue.py`),
  매일 새벽 재점검(`conversion_recheck.py`).
- **이미지 설명(B-81)** / **PDF 페이지 Vision(B-80)** 은 외부 모델로 이미지가 나가므로 승인·정책·안전판정(fail-closed)을
  여러 겹으로 두었다(`archive/image_description_*.py`, `archive/vision_*.py`). 이것은 **근거가 아니라 검색용 파생 색인**이다.
- 한글(HWP)은 변환이 어렵다 — 별도 복구 도구와 진단 스크립트가 있다(`hwp_*.py`, `scripts/diagnose_hwp_conversion.py`).

### 8-4. 수정·삭제·보관

- 메시지 수정/삭제는 revision 으로 따로 보존하고 일반 근거에서 뺀다(`revision_store.py`, `revision_reader.py`).
- 채널이 보관/삭제되면 근거에서 제외(`channel_lifecycle.py`). 보존기간(90일 등) 집행은 `retention.py`.

### 8-5. Archiving Bot 분리 (진행 중)

현재 TYBot 이 직접 수집하지만, 합의된 목표는 **수집 전담 "Archiving Bot"** 을 별도 Slack 앱으로 분리하는 것이다
([`docs/design/archiving-bot-separation-2026-09-23.md`](design/archiving-bot-separation-2026-09-23.md)).

| 책임 | 목표 소유자 |
|---|---|
| 채널 원문 수집·첨부 변환·원본 저장 | Archiving Bot (`archiving_bot.py`, `archive/supervisor.py`) |
| 검토 후보·승인·정기 요약·검색·Q&A | Hermes |
| 신원·권한 범위·전문가 선택·요청/응답 전달·감사 | TYBot Master |
| 보고서 작성 | Clio |

현재는 **그림자(shadow) 수집** 단계로, 운영 검색에 연결하지 않는다. 채널별로 "누가 지금 운영 원문을 쓰는가"를
`archive/write_owner.py` 가 판정하고, `archive/handover.py` 가 Master→Archiver 인계 순서를 정한다.

---

## 9. 검색 — 원문을 어떻게 찾나

| 계층 | 하는 일 | 코드 |
|---|---|---|
| 키워드 색인 | 원문 줄을 `raw_line` 에 넣고 **pg_bigm**(한글 2-gram) 인덱스로 검색 | `search_index.py`, `tybot-index.timer`(10분) |
| 파일 직접 읽기 | 색인이 늦어도 원문 줄을 반환 | `archive/store.py` |
| 승인 요약 길잡이 | 사람이 승인한 요약을 "어디를 볼지" 힌트로만 사용 (사실 근거 아님) | `summary_guide.py` |
| **벡터/하이브리드 검색 (B-12)** | pgvector 후보 → **현재 권한으로 원문 재검증** → 키워드와 RRF 융합 | `embedding_*.py`, `hybrid_search_*.py` |

> B-12 는 아직 **shadow(비교 측정만)** 단계다. 벡터 DB의 결과는 절대 답변 근거가 아니고
> "원문 좌표를 가리키는 후보"일 뿐이며, 열 때마다 현재 권한을 다시 확인한다(fail-closed).
> 임베딩 호출은 인덱싱용과 질문용 승인 정책이 **따로**다.

---

## 10. 마스터 봇 + 전문 봇 (Master / Specialist)

```
사용자 질문 ─▶ TYBot(마스터)
                ① 판정 (저가 LLM, master_planner.py) : 어느 전문가에게 물을지, 독립형 질문
                ② 권한 필터 (코드)                    : LLM 은 관여하지 않는다
                ③ 근거 추출 (코드)                    : 아카이브 원문 줄
                └─▶ 전문 봇 (Hermes 등)               : 문장만 돌려준다
                ④ 출처는 마스터가 붙인다
```

핵심 설계: **LLM 은 "누구에게 물을지"만 정하고, 권한·근거·출처는 코드가 소유**한다. 그래서 프롬프트 인젝션으로
라우팅을 흔들 수는 있어도 권한을 넘을 수는 없다. 최악은 "엉뚱한 전문가라 답이 부실"이며 되돌릴 수 있다.

전문 봇 계약(요지): 아웃바운드 호출만 / 자기 데이터 조회 금지 / 입력은 우리가 필터한 텍스트 / 출력은 문장만 /
출력 저장 금지 / 장애 시 마스터가 대신 업무 답변하지 않고 **명시적 unavailable** / 읽기 전용.

| 개념 | 파일 |
|---|---|
| 판정 | `master_planner.py`, `specialist_router.py` |
| 계약·와이어(v2/v3) | `specialist_contract.py`, `specialist_wire*.py` |
| v3 도구 호출(Hermes→TYBot 검색 도구) | `specialist_v3_*.py`, `specialist_tools.py` |
| 프롬프트 계약 어댑터 | `specialist_adapters.py` |
| 실행 격리 | `deploy/tybot-specialist@.service`, `tybot-specialist-build/run/deploy` |
| 샘플/실제 전문 봇 | `templates/specialist-bot/`, `subbots/hermes/` |
| 롤아웃 게이트 | `master_v2_rollout.py` (워크스페이스별 점진 적용) |

전문 봇 등록·배포·승인은 콘솔에서 한다(`console/specialist_*.py`).

---

## 11. 요약 검토 루프 (사람이 확정한다)

봇이 요약을 **후보**로 만들고 → 채널 검토자가 **확정** → 승인본만 "검색 길잡이"로 사용.
(자동 반영 금지가 기본. B-88 이후, 유예시간(기본 72h)이 지난 미검토 후보는 다음 화·금 전역 회차에서
"자동 봇 요약"으로 라벨을 달아 전환.)

| 단계 | 코드 |
|---|---|
| 후보 생성·검토 Canvas·DM·상태 전이 | `summary_review.py` |
| 검토자 지정 | `reviewers.py` (`/채널 수정`) |
| 검토자에게 밀어주기 | `daily_review.py`, `tybot-review-dm.timer` (화·금 17:00 KST) |
| 이미 끝난 검토 생략 판정 | `summary_review_reconcile.py` |

"사람이 찾아오게 하지 않는다"가 설계 철학이다 — 기능이 있어도 사람이 모르면 쓰이지 않기 때문(실제로 "대기 31건·처리 0건" 사고가 있었다).

---

## 12. 사용자 기능 (Slack 에서 보이는 것)

| 입력 | 기능 | 코드 |
|---|---|---|
| `@tybot 질문` / 봇에게 DM | 질의응답 | `slack/pilot.py` → `answer.py` |
| `@tybot 수집` / `전체수집` | 백필 | |
| `@tybot 상태` | 수집 현황 트리 | `status_tree.py`, `compose.py` |
| `/채널 [생성\|수정\|상태]` | 표준 채널 생성·수정·점검 | `channel_management.py`, `channel_health.py` |
| `/수집상태` | 이 채널이 수집되는지/왜 안 되는지 | `collection_status.py` |
| `/권한` | 내 답변이 무엇을 근거로 하는지 | `scope_report.py` |
| `/일정` | 그룹웨어 팀 일정 조회 (본인에게만 표시) | `schedule.py`, `schedulesync.py` |
| `/일정 알림` | 일정 개인 DM 알림 | `schedule_dm.py` |
| `/투표` | 직접 만든 투표(익명·마감 등) | `polls.py`, `poll_view.py` |
| `/피드백`, 👍👎 | 답변 품질 피드백 | `feedback.py`, `answer_issues.py` |
| `근거 보기` 버튼 | 답변의 근거 원문 확인 | `evidence_view.py` |
| `@tybot --model=... 질문` | 모델 지정 | `answer.parse_model_flag` |
| 긴 답변 | Canvas 로 전달 | `canvas_answer.py`, `canvas_harness.py` |

영문 별칭도 있다(`/ty-channel`, `/ty-scope`, `/ty-schedule`, `/ty-poll`, `/ty-feedback`, `/ty-collection`).

**후속 질문**("방금 그 문서 다시")은 이전 답변 **문장**이 아니라 그 답이 읽은 **원문 좌표(EvidenceRef)** 를 이어받고,
매번 현재 권한으로 다시 연다(`evidence_refs.py`, `thread_followup.py`).

**사용 제한**: 사용자별 성공 답변 쿼터(`answer_quota.py`), 봇 사용 권한(`bot_access.py`), 워크스페이스별 일일 비용 상한(`gateway/budget.py`).

---

## 13. LLM 게이트웨이와 비용

모든 LLM 호출은 `gateway/` 를 통과한다 (프로바이더 SDK 직접 호출 금지).

```python
from tybot.gateway import Router, Message, Sensitivity
router = Router.from_default_registry(daily_limit_usd=5)
resp = router.complete([Message("user", "...")], model="claude-sonnet-5-5",
                       sensitivity=Sensitivity.CONFIDENTIAL)
```

- **모델 선택**: 기본 모델 / 라우터(저가) 모델 / 폴백 모델은 환경변수(`DEFAULT_MODEL`, `ROUTER_MODEL`, `LLM_FALLBACK_MODELS`).
- **민감도 라우팅**: 모델 등급이 아니라 **벤더 계약(DPA)** 단위로 허용 여부를 판정.
- **비용 가드**: 전 워크스페이스 합산 일일 상한, 워크스페이스별 상한도 있다. 누적은 파일(`cost-state.json`)에 남아 재시작해도 유지.
- 응답 캐시, 임베딩 전용 게이트웨이(`gateway/embedding.py`)가 따로 있다.
- API 키는 `.env`(평문) 대신 콘솔의 **암호화된 LLM 시크릿 저장소**에 둘 수 있다(`console/llm_secret_store.py`).

---

## 14. 관리 콘솔

`/opt/tybot/.venv/bin/python` 기반 FastAPI(`src/tybot/console/app.py`, 라우트 80여 개) + React/Vite SPA(`console-web/`).
nginx 가 443 TLS 를 끝내고 `127.0.0.1:8787` 로 넘긴다. 로그인은 회사 이메일 + 비밀번호(HttpOnly 쿠키).

> **원칙(2026-09-18)**: 새 운영 기능은 **콘솔에서 쓸 수 있어야 한다.** SSH 로 명령 치는 것을 전제로 하지 않는다.
> 서버 명령을 쓸 수 있는 사람이 한 명이면, 그 사람이 없을 때 기능이 없는 것과 같기 때문이다. (진단 스크립트는 예외)

주요 화면(`console-web/src/pages/`):

| 화면 | 용도 |
|---|---|
| Home / Dashboard / HealthCheck | 수집이 멈추지 않았나, 봇이 "돌고는 있는데 일을 못 하는" 상태인가 |
| Collected | 수집된 문서 열람, 첨부 변환 결과 |
| Usage | API 사용량·비용 |
| Questions | 질문·답변 기록, 피드백, 문제 패킷 내려받기 |
| Specialists | 전문 봇 등록·배포·규칙 QA |
| Harness | 답변 하네스·규칙 |
| ServiceLogs | `tybot.service` 로그 |
| BatchTimers | systemd 타이머 조회·실행·주기 변경 |
| Deploy | 배포 요청 → **다른 관리자 승인** → 전체 테스트 통과 시에만 배포 |
| ConsoleUsers | 콘솔 계정/권한(게스트·개발자·관리자) |
| AuditEvents | 모든 승인·열람 감사 |
| LifecycleDashboards | 채널 수명주기 |

`/pf/` 는 **다른 프로세스·다른 DB role** 의 읽기 전용 콘솔(PF 팀 Hermes 용)이다. 같은 앱의 라우트가 아니다 — 권한 격리를 위해 일부러 분리.

---

## 15. 개발 환경과 일하는 법

### 15-1. 로컬 세팅 (Windows 개발 PC)

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env             # 값은 직접 채움. 절대 커밋 금지, 채팅에 붙이지 않기
git config core.hooksPath .githooks   # ★ 처음에 한 번: 시크릿·PII·원문 커밋 차단
python scripts/check_env.py        # 설정 점검 (값은 마스킹되어 출력됨)
pytest                             # 시크릿 없이도 전부 돌아감
ruff check src tests scripts       # 0건 유지
python -m tybot.slack.pilot        # 봇 기동 (한 곳에서만!)
```

- `.env` 에는 `SLACK_BOT_TOKEN`(xoxb-), `SLACK_APP_TOKEN`(xapp-), `ANTHROPIC_API_KEY` 등이 들어간다.
  `.env` 는 인라인 주석(`VAR=값 # 설명`) 금지 — 설명은 줄 앞에 단독으로.
- 테스트 파일은 365개 정도 있고(README 의 "154 tests"는 오래된 수치), 실제 Slack/DB 대신 **가짜(fake)** 저장소(`tests/fake_*_repo.py`)를 쓴다.

### 15-2. 서버 명령은 항상 venv 기준

```bash
sudo -u tybot /opt/tybot/.venv/bin/python -m tybot.search_index
```

서버에는 `python` 이 PATH 에 없다. 개발 PC 명령과 서버 명령을 문서에 섞지 않는다.

### 15-3. 규칙

- **작업은 [`BACKLOG.md`](../BACKLOG.md) 에서 고른다.** 착수 시 상태를 `진행중` 으로 바꿔 커밋 (중복 작업 방지). BACKLOG 는 "하지 않기로 한 것"도 담고 있다.
- Claude Code + Codex 병행 개발. 커밋·PR 은 사람이 검토.
- 절대 하지 말 것: 봇 2곳 동시 기동, 중앙 저장소 force push, 원문 편집, 봇 출력을 아카이브에 쓰기, 시크릿 커밋.
- `.claude/` 에는 이 프로젝트용 에이전트(slack-integration, llm-router, md-archiver, legacy-connector),
  스킬(access-control, channel-naming, hallucination-guard, md-archive-schema, qa-issue-fix),
  명령(`/new-workspace`, `/archive-review`), 훅(`guard.py` 시크릿 차단, `format.py`)이 있다.

---

## 16. 운영(배포) 구조

### 정기 잡 (systemd timer)

| 타이머 | 주기 | 하는 일 |
|---|---|---|
| `tybot-tidy` | 15분 | 아카이브 건강 점검 (리포트만, 수정 안 함) |
| `tybot-index` | 10분 | 검색 색인 갱신 |
| `tybot-collect` | 업무시간 매시 정시 | 정기 백필 |
| `tybot-review-dm` | 화·금 17:00 KST | 검토자 DM |
| `tybot-convert-*` | 재시도/야간/알림 | 첨부 변환 재처리 |
| `tybot-schedule-sync/dm/reconcile` | | 그룹웨어 일정 동기화·알림 |
| `tybot-embedding-index` | | 벡터 색인 |
| `tybot-file-audit`, `tybot-account-sync`, `tybot-image-description@` | | 파일 감사, 콘솔 계정 동기화, 이미지 설명 |
| `tybot-update` / `tybot-deploy.path` | | 자동·승인 기반 배포 |

### 배포 안전장치

콘솔에서 개발자가 배포 **요청** → **다른** 관리자가 **승인** → `update.sh` 가 전체 테스트를 돌려 통과해야 코드 교체 → 실패하면 운영 코드는 그대로.
요청자와 승인자를 분리한 것이 핵심이다(`console/deploy_approval_store.py`, `deploy/deploy-runner.sh`).

### 외부 연동 (읽기 전용·내부망)

- **그룹웨어 Oracle**: 내부망에서 `scripts/oracle_export.py` 가 JSONL + manifest(sha256)로 밀어 넣고(push),
  봇 서버의 `orgsync.py` 가 PostgreSQL 로 반영. 봇 서버는 Oracle 을 import 하지 않는다. (조직·사번·일정)
- **사번 ↔ Slack 매핑**: Slack 회사 이메일 ↔ 직원 이메일 정확 일치로 사번 연결(`identity.py`). 이후 권한은 확정된 사번으로.

---

## 17. 파일 지도 (어디에 뭐가 있나)

```
SlackBot/
├─ src/tybot/
│  ├─ slack/pilot.py        ★ 봇 본체: 이벤트·슬래시 명령·모달·버튼 핸들러 (약 5,000줄)
│  ├─ answer.py             ★ 질의응답 엔진 (검색/요약/판단/전문봇 호출/출처)
│  ├─ intent.py             의도 분류 + 질문 분해
│  ├─ access/               ★ 권한 판정 can_access, RequestContext
│  ├─ archive/              수집·저장·첨부 변환·검색(store)·Vision·이미지설명·소급·Supervisor
│  ├─ gateway/              LLM 라우터·비용·민감도·임베딩
│  ├─ console/              관리 콘솔 백엔드 (FastAPI)
│  ├─ specialist_*.py       전문 봇 계약·라우팅·도구·HTTP
│  ├─ vip*.py               VIPBot (별도 런타임, 상세 답변은 요청자 DM 으로만)
│  ├─ summary_*.py          요약 후보·검토·승인
│  ├─ embedding_*.py, hybrid_search_*.py   벡터/하이브리드 검색(B-12, shadow)
│  ├─ sensitive_mask.py, pii_screen.py     PII 마스킹·마지막 관문
│  ├─ audit.py, feedback.py, answer_issues.py   감사·피드백·문제 패킷
│  ├─ schedule*.py, polls.py, channel_*.py      일정·투표·채널 관리
│  ├─ workspaces.py, config.py, envfile.py      설정 로딩
│  └─ lock.py, heartbeat.py, db.py              락·상태 파일·DB 연결
├─ console-web/             React + Vite 관리 콘솔 프론트 (src/pages 가 화면)
├─ deploy/                  systemd 서비스·타이머, nginx, install/update 스크립트, sql/ (스키마 40여 개)
├─ scripts/                 운영·진단·마이그레이션 CLI (check_env.py 부터 보면 됨)
├─ tests/                   pytest (fake 저장소 사용)
├─ docs/                    architecture / multi-workspace / pilot(런북) / deploy / design(설계 결정 50+) / verification(검증·인계)
├─ subbots/                 전문 봇 연동 계약 (hermes 는 소스 포함)
├─ templates/specialist-bot 전문 봇 템플릿
├─ vendor/                  오프라인 설치용 번들(추정; 용도 미확인)
├─ .claude/ .claude-plugin/ Claude Code 에이전트·스킬·훅 설정
├─ BACKLOG.md               ★ 다음 할 일·인계 (매우 큼, 260KB)
├─ CLAUDE.md / AGENTS.md    ★ 원칙·컨벤션
└─ .env.example             설정 항목 전체 목록 (.env 는 커밋 금지)
```

### VIPBot 이란

같은 답변 엔진을 **별도 Slack 앱·별도 DB 자격·별도 비용 상한**으로 돌리는 임원용 봇. 사람의 채널 멤버십 대신
**관리자가 고른 근거 채널**만 읽고, 상세 답변은 채널에 남기지 않고 **요청자 DM 으로만** 보낸다(`vip_*.py`, `vipbot_*.py`).

---

## 18. 추천 읽는 순서 (초보 개발자용)

1. [`README.md`](../README.md) — 다이어그램 4장 (구조 / 질문흐름 / 권한 / 환각방지)
2. [`CLAUDE.md`](../CLAUDE.md) — 절대 원칙과 오너 결정들
3. [`docs/pilot/README.md`](pilot/README.md) — 실제 사용법·런북
4. `src/tybot/access/__init__.py` — 짧고, 보안의 핵심
5. `src/tybot/archive/writer.py` → `archive/store.py` — 데이터가 어떻게 쌓이고 읽히나
6. `src/tybot/intent.py` → `answer.py` → `gateway/router.py` — 질문이 답이 되는 길 (answer.py 는 크니 `AnswerEngine.respond` 부터)
7. `src/tybot/slack/pilot.py` — 위 모듈들이 Slack 이벤트에 어떻게 붙는지
8. [`docs/design/bot-hierarchy.md`](design/bot-hierarchy.md), [`summary-review.md`](design/summary-review.md), [`dm-workspace.md`](design/dm-workspace.md)
9. [`docs/design/console.md`](design/console.md) 와 `console-web/src/pages/Dashboard.tsx`
10. 테스트(`tests/test_answer.py`, `test_archive_invariants.py`, `test_answer_invariants.py`) — "무엇이 절대 깨지면 안 되는지"가 코드로 적혀 있다

---

## 19. 용어집

| 용어 | 뜻 |
|---|---|
| **워크스페이스(ws)** | Slack 조직 단위. 본부/팀/현장별로 따로 있고 봇 앱도 각각 설치 |
| **root 워크스페이스** | 모든 산하 자료를 볼 수 있는 상위(경영본부) |
| **Socket Mode** | Slack 이 봇에게 접속하는 게 아니라, 봇이 Slack 으로 먼저 WebSocket 을 여는 방식. 서버에 열어둘 인바운드 포트가 필요 없다 |
| **아카이브** | 원문 MD 파일 모음. 답변의 유일한 근거 |
| **원문(raw)** | 사람이 실제로 쓴 메시지 그대로. 편집 금지 |
| **파생(derived)** | 요약·이미지설명·벡터 등 원문에서 만든 것. 근거가 아니라 길잡이 |
| **ACL** | 접근 제어 목록(누가 볼 수 있나) |
| **visibility / share_with** | 문서의 공개 표시 / 넘겨줄 워크스페이스 목록 |
| **PII** | 개인정보(주민번호·계좌 등) |
| **EvidenceRef(원문 좌표)** | "어느 문서·어느 줄·어느 시점·해시" 를 가리키는 참조. 후속 질문이 이것을 이어받는다 |
| **fail-closed** | 확인이 안 되면 열지 않고 막는 설계 (반대: fail-open) |
| **shadow** | 운영에 영향 없이 옆에서 돌려 결과만 비교하는 방식 |
| **백필(backfill)** | 놓친/과거 구간을 나중에 채워 넣는 수집 |
| **RAG** | 검색한 자료를 프롬프트에 넣어 LLM 이 답하게 하는 기법 (TYBot 의 기본 방식) |
| **환각(hallucination)** | LLM 이 근거 없이 지어낸 답 |
| **Hermes** | 내부 기록(회의록·업무 파악·요약)을 담당하는 첫 전문 봇. 합병 대상이자 전문가 |
| **Clio** | 보고서 작성 전문 봇 |
| **VIPBot** | 임원용 별도 봇(DM 전달 전용) |
| **마스터(Master)** | TYBot 본체. 권한·근거·출처·라우팅을 소유 |
| **B-NN** | BACKLOG 항목 번호 (예: B-12 벡터검색, B-86 PII 마스킹) |
| **systemd timer** | 리눅스의 정기 실행기 (cron 과 비슷) |
| **pg_bigm / pgvector** | PostgreSQL 한글 부분검색 / 벡터 검색 확장 |
| **오너 결정** | 프로젝트 오너가 날짜와 함께 확정한 정책. 문서마다 "(2026-09-17 오너 결정)"처럼 기록 |

---

## 20. 이 문서가 확인하지 못한 것 (솔직한 한계)

- `slack/pilot.py`(5천 줄), `answer.py`(2.6천 줄), `summary_review.py` 는 **함수 목록·docstring·핵심 흐름만** 읽었고 전체를 줄 단위로 검증하지 않았다.
- `console-web` 화면별 상세 동작, `vendor/` 의 정확한 용도, `subbots/hermes/` 내부 구현(Node 기반 추정)은 상세 확인하지 않았다.
- 구현 진행 상태(Archiving Bot 분리, B-12 벡터 검색, Vision, 마스터 v2 롤아웃)는 문서 작성 시점 기준이며 BACKLOG 가 더 최신일 수 있다.
- `.env` 는 보안 규칙상 읽지 않았다. 설정 항목은 `.env.example` 의 변수 이름만 참고했다.
