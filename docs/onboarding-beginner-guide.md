# TYBot 입문 가이드 — 개발을 처음 접하는 사람을 위한 전체 정리

> 작성 기준일: 2026-11-12 (초판) · **2026-11-24 갱신**. 코드·README·CLAUDE.md·docs/design 문서를 읽고 정리했다.
> 초판 이후 바뀐 내용은 **0-1장**에 모아 두었고, 본문 각 장에도 반영했다.
> 세부 수치(테스트 개수 등)와 구현 진행 상태는 계속 바뀌므로, 최종 기준은 항상
> [`BACKLOG.md`](../BACKLOG.md)와 각 설계 문서다.

---

## 0. 이 문서를 읽는 법

| 시간이 없다면 | 읽을 장 |
|---|---|
| 10분 | 1장(한 줄 요약) → 2장(비유) → 3장(질문 한 건의 여행) |
| 1시간 | 1~8장 |
| 초판을 읽은 적이 있다 | 0-1장(달라진 점)만 |
| 개발에 투입된다 | 전부 + 15장(개발 환경) + 18장(읽는 순서) |

모르는 단어는 **19장 용어집**에 모아 두었다.

### 0-1. 초판(2026-11-12) 이후 달라진 점 — 2026-11-24 갱신

> BACKLOG·CLAUDE.md·신규 설계 문서와 코드 일부를 읽고 반영했다. **서버 운영 상태는 확인하지 못했다.**
> 아래의 "구현됨"은 저장소에 코드가 있다는 뜻이고, 운영 적용 여부는 BACKLOG 의 상태 값을 따른다.

| 영역 | 달라진 점 | 항목 |
|---|---|---|
| **수집 소유권** | 2026-11-18 전 채널이 Archiver 소유가 됐다. Master 의 원문 쓰기는 소유 기록이 없거나 DB 조회에 실패하면 허용이 아니라 **거절**(fail-closed)한다. 11-20 에 Master 가 옛 `workspaces/` 디렉터리를 만들어 Archiver 가 10초마다 재시작하는 장애가 났고, 쓰기 차단·유닛 점검(`scripts/check_unit_drift.py`)은 구현됨(배포 대기). 섀도 모드 제거(2단계)는 대기 | B-99 |
| **VIPBot** | 별도 런타임과 `tybot-vipbot.service` 를 **삭제**했다. TYBot 프로세스 하나가 DB 에서 검증된 VIPBot 연결마다 Socket Mode 세션을 연다(워크스페이스별 권한 분리). 판단 Sonnet·Hermes Opus 로 한 단계 올리는 등급 분리는 오너 결정만 있고 대기 | B-95 완료 · B-96 |
| **대화 맥락·기억** | ① **개인 기억**: 선호·용어·사실 주장에 더해 열거형 밖 **자유 요청**(`instruction`, "결론부터" 등)을 본인 답변에만 반영. `내 기억 보여줘`·`기억 삭제 N` 으로 조회·철회 ② **사람 정정 공유**(`fact_correction`): "15억이 아니라 12억" 같은 정정을 원문을 볼 수 있는 사람의 답변 끝에 덧붙임 ③ **직전 답변 지칭 해석**(`prior_answer`): "위 Top 10 중…" 처럼 봇이 보여 준 목록을 가리킬 수 있게 직전 답변 문장을 *근거 아님* 용도로 전달 ④ 후속 질문이 이전 범위에 갇히지 않고 자기 낱말로 새로 찾아 합침. 서버 스키마 적용·운영 확인은 미완 | B-91 |
| **검색 폭** | 전수 탐색(보이는 원문 150,000줄 이하일 때)·문서당 4줄 상한·근거 줄 상한(TYBot 40, VIP 60)을 도입했다. 한 문서가 근거를 독차지해 핵심 문서가 밀려나던 VIPBot 사례가 계기. **채널×일 다이제스트**(검색 길잡이)의 생성 배치·콘솔 화면·`tybot-channel-digest` 타이머 코드가 들어왔다(BACKLOG 체크박스는 아직 미표시, 타이머는 설치만 하고 꺼둠). 서버 측정 후 문턱을 조정하는 일이 남음 | B-93 |
| **벡터 검색** | B-12 는 shadow 가 아니라 **보류**다(2026-11-09 오너: 임베딩 모델 사용 불가). 키워드 검색 보강으로 대체 | B-12 |
| **사용 제한·캐시** | 사용자별·KST 날짜별로 TYBot 10회, VIPBot 10회(합치지 않음) 성공 답변 제한. 정적 프롬프트 5분 캐시, 결정적 검색 캐시 | B-85 완료 |
| **첨부·이미지** | 변환 정책을 환경변수에서 **DB 정본**(`conversion_runtime_policy`)으로 이전, 모르면 꺼짐이 아니라 "모름"으로 보고 fail-closed. 새벽 재점검 타이머. 이미지 설명·PDF Vision 은 2026-11-19/20 오너 결정으로 일반 업무 문서의 전송을 허용(PII·민감 문서 차단은 그대로)하고 이미지의 건별 승인은 폐지. 인식 결과를 원 스레드에 알림 | B-89 · B-80 · B-81 · B-94 |
| **보안** | Slack 첨부 다운로드가 `files.slack.com` 외 호스트로 봇 토큰을 보내지 못하게 막았다(원격 파일은 `external_skipped`). Google Drive 승인 폴더를 근거로 삼는 기능은 **설계 단계** | B-97 |
| **콘솔** | 화면 추가: AnswerRecords, ChannelDigest, ConversionPolicy, ImageDescription 등(14장) | |
| **수치** | 테스트 파일 381개, `pilot.py` 약 5,200줄, SQL 스키마 47개, BACKLOG 약 310KB | |

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

### 위 흐름에 2026-11 이후 추가로 끼는 것

| 무엇 | 하는 일 | 코드 |
|---|---|---|
| **개인 기억 주입** | 같은 사용자의 선호·용어·자유 요청을 `<user_memory>` 블록으로 프롬프트에 넣는다. 블록 안은 *지시가 아니라 데이터*이고 근거가 아니라서 출처 줄이 붙지 않는다. 최대 5건·600바이트(자유 요청은 3건, 전체 1,200바이트) | `user_memory.py`, `user_memory_service.py` |
| **직전 답변 지칭 해석** | "위 목록의 3번"을 풀기 위해, 직전 답변이 읽은 원문 좌표를 지금도 전부 열 수 있을 때만 직전 답변을 `<직전_봇_답변 근거="아님">` 으로 함께 넘긴다 | `prior_answer.py` |
| **검색 길잡이** | 승인 요약·채널×일 다이제스트·PDF Vision 전사로 *어느 원문을 볼지*만 고른다. 답변과 출처는 항상 원문 줄이다 | `summary_guide.py`, `digest_guide.py`, `pdf_vision_guide.py` |
| **사람 정정 표시** | 정정이 달린 원문을 읽은 답변이면 끝에 `✏️ 사람이 남긴 정정` 을 코드가 붙인다. 보는 사람이 그 좌표를 지금 전부 열 수 있을 때만 | `fact_correction.py` |

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
| 1++ | 후속 질문에서 **직전 답변 문장은 "무엇을 가리키는지" 알아내는 용도로만** 함께 전달(근거 아님). 그 답변이 읽은 원문 좌표를 **지금 전부** 다시 열 수 있을 때만 | 2026-11-20 오너 결정 | `prior_answer.py` |
| 2 | **출처 강제** `출처: [조직]업무, 📄문서(날짜)` | 출처 없는 답 = 근거 못 찾은 답 | `answer.py`, `channels.source_label` |
| 3 | **권한은 막는 쪽이 기본값** | 공개 표시 없으면 비공개 | `access/` |
| 4 | **크로스 워크스페이스는 화이트리스트만** | 팀 간 자료 유출 방지 | `workspaces.py` (`CROSS_WS_READ`) |
| 5 | **PII는 가린 뒤 보관, 못 가리면 격리** | 주민번호 등. 2026-11-10 "있으면 전체 금지"에서 "가린 뒤 보관"으로 변경 | `sensitive_mask.py`, `pii_screen.py` |
| 6 | **시크릿은 저장소 밖** | 토큰 유출 방지 | `.env.example`만 커밋, `.gitignore`, 훅 |
| 7 | 사람 발언 ↔ 문서 분리, 엇갈리면 두 시점 병기. 사람이 남긴 **사실 정정**은 원문을 바꾸지 않고, 원문을 볼 수 있는 사람의 답변 끝에 `정정자·날짜·전 → 후` 로 덧붙임 | 2026-11-20 오너 결정 | `fact_correction.py` |
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

> **2026-11 갱신:** 위 경로가 코드에 남아 있어도, 채널이 Archiver 소유이면 Master 의 쓰기는 거절된다
> (`write_owner.decide` — 소유 기록 없음·DB 조회 실패도 거절). 운영 루트 아래에 옛 `workspaces/` 배치를
> 만드는 경로는 막혔다(B-99 1단계). 실제 수집의 본선은 Archiver 다(8-5).

### 8-3. 첨부파일

```
Slack 파일 → 다운로드 → (격리된) 변환 워커 → 텍스트 추출 → PII 검사/마스킹 → 첨부 정본 문서
   xlsx·docx·pptx·pdf·hwpx → 변환 성공           실패/미지원 → 격리 보관 + 알림
   스캔 PDF·구형 hwp·이미지 → OCR/Vision(선택, 승인·정책 게이트 뒤) 또는 목록만
```

- 변환 실패·누락은 채널 단위로 묶어 알림(`conversion_alerts.py`), 재처리 큐(`conversion_queue.py`),
  매일 새벽 재점검(`conversion_recheck.py`).
- **이미지 설명(B-81)**: 수집 시 원본 보관을 먼저 끝내고, 별도 비동기 worker 가 PNG/JPEG/WebP 를 승인된 무료 Vision 모델(Google)로
  설명·OCR 한다(`archive/image_description_*.py`). 운영자가 켠 Vision 정책 아래에서만 돌고 기본은 OFF 다. 2026-11-20 결정으로
  **전송 전 PII 판별·건별 승인은 폐지**했지만, 결과 텍스트는 저장·Slack 전달 전에 마스킹과 출력 가드를 거치고 얼굴 신원 식별·
  차량번호 전사는 금지다. API 키는 `.env` 가 아니라 Vision 전용 DB 비밀 항목(Fernet 암호문)에 둔다.
  인식 결과는 원본이 올라온 **스레드에 Archiver 가 답글**로 알린다(B-94, worker 는 Slack 토큰이 없고 outbox 로만 요청한다).
- **PDF 페이지 Vision(B-80)**: 로컬 변환에서 읽지 못한 쪽만 렌더링해 전사한다(`archive/vision_*.py`). 일반 업무 문서의 전송은 허용(2026-11-19)하되
  PII·민감 문서 차단, 원본 불변, 쪽별 중복 호출 금지는 그대로다. 근거 줄에는 `[AI 문서 전사·파생 · PDF N쪽]` 이 표시된다.
- 두 기능의 결과는 **근거가 아니라 검색용 파생 색인**이고, 답변에 쓸 때마다 현재 ACL·원본 해시를 다시 확인한다.
- 한글(HWP)은 변환이 어렵다 — 별도 복구 도구와 진단 스크립트가 있다(`hwp_*.py`, `scripts/diagnose_hwp_conversion.py`).
  운영에서는 Slack 의 외부 **rhwp 앱**이 HWP/HWPX 를 PDF 로 바꿔 올리고 Archiver 가 그 PDF 를 첨부로 보관한다. 자체 변환 worker 로
  내재화하는 B-92 는 오너 결정으로 **대기**(현 방식 유지)다.
- **변환 정책은 DB 정본**이다(`conversion_runtime_policy`, `conversion_policy.py`). rhwp 앱 ID·야간 소급 상한 등을 콘솔 「변환 정책」에서
  관리하고, DB 를 읽지 못하면 "꺼짐"이 아니라 **"모름"** 으로 보고 HWP 직접 변환·야간 `--apply` 를 막는다(fail-closed). 이전에는 값이 비면
  위임이 꺼진 것으로 읽혀 같은 문서가 두 벌의 근거가 될 수 있었다.
- **다운로드 호스트 가드(B-97 0단계)**: 첨부 다운로드는 https `files.slack.com` 에만 봇 토큰을 보낸다. 외부(원격) 파일은
  `[첨부:외부파일]`·`external_skipped` 로 남기고, 리다이렉트로 호스트가 바뀌면 헤더를 빼고 실패시킨다.
- Google Drive 링크의 본문은 아직 근거가 아니다. 콘솔에서 승인한 폴더만 읽는 설계(`docs/design/google-drive-evidence.md`)가 있고 구현은 1단계 이전이다.

### 8-4. 수정·삭제·보관

- 메시지 수정/삭제는 revision 으로 따로 보존하고 일반 근거에서 뺀다(`revision_store.py`, `revision_reader.py`).
- 채널이 보관/삭제되면 근거에서 제외(`channel_lifecycle.py`). 보존기간(90일 등) 집행은 `retention.py`.

### 8-5. Archiving Bot 분리 (Archiver 가 수집 정본, 섀도 제거 진행 중)

수집은 **수집 전담 "Archiving Bot"(Archiver)** 이라는 별도 Slack 앱이 맡는다
([`docs/design/archiving-bot-separation-2026-09-23.md`](design/archiving-bot-separation-2026-09-23.md)).
초판 시점에는 Master 가 직접 수집하고 Archiver 는 비교용 "그림자"였지만, 2026-11-18 에 전 채널이 Archiver 소유가 됐다.

| 책임 | 목표 소유자 |
|---|---|
| 채널 원문 수집·첨부 변환·원본 저장 | Archiving Bot (`archiving_bot.py`, `archive/supervisor.py`) |
| 검토 후보·승인·정기 요약·검색·Q&A | Hermes |
| 신원·권한 범위·전문가 선택·요청/응답 전달·감사 | TYBot Master |
| 보고서 작성 | Clio |

채널별로 "누가 지금 운영 원문을 쓰는가"를 `archive/write_owner.py` 가 판정한다. 소유 기록이 없거나 DB 조회에 실패하면
Master 는 **쓰지 않는다**(fail-closed).

**B-99 (2026-11-20 장애에서 시작).** Archiver 는 운영 루트에 옛 `workspaces/<ws>` 가 있으면 기동을 거부하는데, Master 가 그 디렉터리를
계속 만들었다. 두 쓰기 경로가 서로를 죽이는 구조라서 오너가 "섀도는 없앤다"고 결정했다.

| 단계 | 내용 | 상태 |
|---|---|---|
| 1 | Master DM 을 새 배치로 쓰기, Master 채널 쓰기 fail-closed, 변환 재처리의 옛 배치 분기 차단, 설치 유닛 대조(`check_unit_drift.py`) | 구현됨 · 배포 대기 |
| 2 | `shadow` 모드·섀도 루트 제거, 유닛 이름 `tybot-archiving-shadow@` → `tybot-archiver@`, 격리한 옛 디렉터리(`archive-legacy-quarantine-…`) 병합 | 대기 |

---

## 9. 검색 — 원문을 어떻게 찾나

| 계층 | 하는 일 | 코드 |
|---|---|---|
| 키워드 색인 | 원문 줄을 `raw_line` 에 넣고 **pg_bigm**(한글 2-gram) 인덱스로 검색 | `search_index.py`, `tybot-index.timer`(10분) |
| 파일 직접 읽기 | 색인이 늦어도 원문 줄을 반환 | `archive/store.py` |
| 승인 요약 길잡이 | 사람이 승인한 요약을 "어디를 볼지" 힌트로만 사용 (사실 근거 아님) | `summary_guide.py` |
| **전수 탐색 (B-93 1단계)** | 요청자에게 보이는 원문이 `TYBOT_EXHAUSTIVE_MAX_LINES`(기본 150,000줄) 이하면 색인 후보 창에 의존하지 않고 전체를 훑고, 넘으면 DB 색인을 쓴다. 한 문서가 근거를 독차지하지 않도록 **문서당 4줄 상한**을 둔다. 요청 표현("정리·표·설명")은 검색어에서 뺀다 | `archive/store.py`, `answer.focus_query` |
| **채널×일 다이제스트 (B-93 2단계)** | 채널·하루 단위로 만든 요약과 "같은 뜻 다른 낱말"(재무목표↔경영목표)을 질문과 대조해 **어느 날짜·첨부의 원문을 볼지** 고른다. 다이제스트 문장은 반환값에 없고 원문 줄만 돌려준다. 원문 해시가 달라지면 그 다이제스트는 쓰지 않는다(`stale`). 매일 04:10 배치(`tybot-channel-digest`, 설치만 하고 꺼둠), 전용 DB 계정 | `channel_digest*.py`, `digest_guide.py` |
| **벡터/하이브리드 검색 (B-12, 보류)** | pgvector 후보 → **현재 권한으로 원문 재검증** → 키워드와 RRF 융합 | `embedding_*.py`, `hybrid_search_*.py` |

> B-12 는 **보류**다(2026-11-09 오너 결정: 임베딩 모델 사용 불가). 키워드 검색 보강으로 대체했고, 코드·`tybot-embedding-index`
> 타이머·콘솔 EmbeddingIndex 화면은 남아 있다. 벡터 DB의 결과는 절대 답변 근거가 아니고
> "원문 좌표를 가리키는 후보"일 뿐이며, 열 때마다 현재 권한을 다시 확인한다(fail-closed).
> 임베딩 호출은 인덱싱용과 질문용 승인 정책이 **따로**다.
>
> B-93 의 원칙: **측정 없이 전수 범위를 넓히지 않는다.** 서버 로그(`검색 경로 전수|색인 …`, `answer 전수 탐색 hits= docs=`)와
> 응답 시간을 일주일 본 뒤 문턱을 조정하는 일이 남아 있다.

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
| `/피드백`, 👍👎 | 답변 품질 피드백. 직전 답변 뒤의 정정·선호는 자동으로 개인 기억 후보가 된다 | `feedback.py`, `answer_issues.py` |
| `내 기억 보여줘` / `기억 삭제 17` | 본인 활성 기억 조회·철회 (DB 장애를 "기억 없음"으로 오인하지 않게 구분) | `user_memory_service.py` |
| `정정 삭제 17` | 본인이 남긴 사실 정정 철회. 관리자는 콘솔 「질문·답변 원본 › 사람이 남긴 정정」에서 | `fact_correction.py` |
| `근거 보기` 버튼 | 답변의 근거 원문 확인 | `evidence_view.py` |
| `@tybot --model=... 질문` | 모델 지정 | `answer.parse_model_flag` |
| 긴 답변 | Canvas 로 전달 | `canvas_answer.py`, `canvas_harness.py` |

영문 별칭도 있다(`/ty-channel`, `/ty-scope`, `/ty-schedule`, `/ty-poll`, `/ty-feedback`, `/ty-collection`).

**후속 질문**("방금 그 문서 다시")은 이전 답변 **문장**이 아니라 그 답이 읽은 **원문 좌표(EvidenceRef)** 를 이어받고,
매번 현재 권한으로 다시 연다(`evidence_refs.py`, `thread_followup.py`).
2026-11-12 오너 결정으로 후속 질문이 이전 범위에 **갇히지 않는다** — 질문에 자기 낱말이 있으면 현재 권한으로 새로 찾아 이전 근거와 합친다
(`AnswerEngine._widen_followup`). 이전 범위로 되돌아가는 폴백은 여전히 채널 전체로 넓히지 않는다.
"위 목록 중 3번" 같은 지칭은 직전 답변 문장을 *근거 아님* 용도로 함께 넘겨 푼다(`prior_answer.py`, 원칙 1++).

**사용 제한**: 사용자별 성공 답변 쿼터(`answer_quota.py`) — **KST 날짜별로 TYBot 10회, VIPBot 10회**(서로 합치지 않음). 답변 생성과 전달이 모두
성공한 회차만 세고(캐시 적중 포함), 시작 전에 DB 에서 예약했다가 실패하면 환급한다. 그 밖에 봇 사용 권한(`bot_access.py`),
워크스페이스별 일일 비용 상한(`gateway/budget.py`)이 있다.

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
- 응답 캐시(B-85): 정적 시스템 프롬프트 5분 캐시와 결정적 검색·변환 캐시를 쓴다. 최종 답변 캐시는 봇 종류·사용자 권한 지문·정규화 질문·
  원문 revision·모델/프롬프트 판이 **모두 같을 때만** 적중하고, 적중해도 현재 ACL 과 원문 revision 을 다시 연다. 개인 기억이 다른 요청끼리는 공유하지 않는다.
- 임베딩 전용 게이트웨이(`gateway/embedding.py`)가 따로 있다(B-12 보류). 이미지 설명·Vision 용 Google 프로바이더(`gateway/providers/google_provider.py`)도 있다.
- **봇 등급별 모델(B-96, 대기)**: 오너 결정은 "VIPBot 은 TYBot 보다 모든 단계가 한 단계 위" — 판단 Haiku→Sonnet, Hermes 답변 Sonnet→Opus.
  현재 코드에는 판단 모델을 바꿀 손잡이가 없어 아직 적용 전이다.
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
| AnswerRecords | 질문·답변 원본(VIPBot 포함), 사람이 남긴 정정 조회·철회 (B-91) |
| ChannelDigest | 채널×일 다이제스트 — 설계상 분량 미리보기·소급 실행 (B-93 2단계) |
| ConversionPolicy | 변환 정책(rhwp 앱 ID·야간 소급 상한·대기 시간) 조회·저장, 감사 이력 (B-89) |
| ImageDescription | 이미지 설명 정책·모델·자격 관리 (B-81) |
| EmbeddingIndex | 임베딩 색인 상태 (B-12, 보류) |
| Bots, BotsRouting, Workspaces, SummaryReviews, Licenses, Diagnostics | 초판에서 빠져 있던 화면. 화면 이름만 확인했고 세부 동작은 읽지 않았다 |

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
- 테스트 파일은 381개 정도 있고(README 의 "154 tests"는 오래된 수치), 실제 Slack/DB 대신 **가짜(fake)** 저장소(`tests/fake_*_repo.py`)를 쓴다.

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
| `tybot-convert-*` | 재시도 / 야간 03:20(±10분) / 알림 | 첨부 변환 재처리. 야간은 변환 정책이 DB 정본일 때만 `--apply` 하고, 배포가 타이머를 자동으로 켜지 않는다(B-89) |
| `tybot-channel-digest` | 매일 04:10 (설치만 하고 꺼둠) | 채널×일 다이제스트 생성. LLM 비용이 들어 오너가 dry-run 분량을 본 뒤 직접 켠다(B-93) |
| `tybot-schedule-sync/dm/reconcile` | | 그룹웨어 일정 동기화·알림 |
| `tybot-embedding-index` | | 벡터 색인 |
| `tybot-file-audit`, `tybot-account-sync`, `tybot-image-description@` | | 파일 감사, 콘솔 계정 동기화, 이미지 설명 |
| `tybot-update` / `tybot-deploy.path` | | 자동·승인 기반 배포 |

**상시 서비스(타이머 아님)**: `tybot.service`(Master·VIPBot 공유 프로세스), `tybot-console.service`, 워크스페이스별 Archiver
`tybot-archiving-shadow@<ws>.service`(섀도 시절 이름 그대로이며 B-99 2단계에서 `tybot-archiver@` 로 바꿀 예정), 전문 봇
`tybot-specialist@.service`, 이미지 설명 worker `tybot-image-description@<ws>`(15분 타이머). **`tybot-vipbot.service` 는 삭제됐다** —
서버에 남아 있으면 중지·제거해야 같은 Slack 토큰의 두 번째 소비자가 뜨지 않는다.

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
│  ├─ slack/pilot.py        ★ 봇 본체: 이벤트·슬래시 명령·모달·버튼 핸들러 (약 5,200줄)
│  ├─ answer.py             ★ 질의응답 엔진 (검색/요약/판단/전문봇 호출/출처)
│  ├─ intent.py             의도 분류 + 질문 분해
│  ├─ access/               ★ 권한 판정 can_access, RequestContext
│  ├─ archive/              수집·저장·첨부 변환·검색(store)·Vision·이미지설명·소급·Supervisor
│  ├─ gateway/              LLM 라우터·비용·민감도·임베딩
│  ├─ console/              관리 콘솔 백엔드 (FastAPI)
│  ├─ specialist_*.py       전문 봇 계약·라우팅·도구·HTTP
│  ├─ vip_*.py              VIPBot 정책 어댑터 (공유 런타임에 권한·모델·근거 채널만 끼움. 별도 런타임은 삭제됨)
│  ├─ summary_*.py          요약 후보·검토·승인
│  ├─ embedding_*.py, hybrid_search_*.py   벡터/하이브리드 검색(B-12, shadow)
│  ├─ sensitive_mask.py, pii_screen.py     PII 마스킹·마지막 관문
│  ├─ audit.py, feedback.py, answer_issues.py   감사·피드백·문제 패킷
│  ├─ user_memory*.py, fact_correction.py, prior_answer.py   개인 기억·사람 정정·직전 답변 지칭 해석 (B-91)
│  ├─ channel_digest*.py, digest_guide.py       채널×일 다이제스트 생성·검색 길잡이 (B-93 2단계)
│  ├─ conversion_policy.py  변환 정책 DB 정본 (B-89)
│  ├─ question_files.py     질문에 함께 첨부한 파일을 즉시 근거로 (B-84, files.slack.com 만 허용)
│  ├─ schedule*.py, polls.py, channel_*.py      일정·투표·채널 관리
│  ├─ workspaces.py, config.py, envfile.py      설정 로딩
│  └─ lock.py, heartbeat.py, db.py              락·상태 파일·DB 연결
├─ console-web/             React + Vite 관리 콘솔 프론트 (src/pages 가 화면)
├─ deploy/                  systemd 서비스·타이머, nginx, install/update 스크립트, sql/ (스키마 47개)
├─ scripts/                 운영·진단·마이그레이션 CLI (check_env.py 부터 보면 됨)
├─ tests/                   pytest (fake 저장소 사용)
├─ docs/                    architecture / multi-workspace / pilot(런북) / deploy / design(설계 결정 50+) / verification(검증·인계)
├─ subbots/                 전문 봇 연동 계약 (hermes 는 소스 포함)
├─ templates/specialist-bot 전문 봇 템플릿
├─ vendor/                  오프라인 설치용 번들(추정; 용도 미확인)
├─ .claude/ .claude-plugin/ Claude Code 에이전트·스킬·훅 설정
├─ BACKLOG.md               ★ 다음 할 일·인계 (매우 큼, 약 310KB — 통째로 읽지 말고 상단 "우선순위 요약" 표에서 항목을 골라 해당 구간만 읽는다)
├─ CLAUDE.md / AGENTS.md    ★ 원칙·컨벤션
└─ .env.example             설정 항목 전체 목록 (.env 는 커밋 금지)
```

### VIPBot 이란

TYBot 과 **같은 답변 코드**를 쓰되 **별도 Slack 앱·별도 사용 권한·별도 모델/프롬프트·별도 비용 상한**을 가진 임원용 봇. 사람의 채널
멤버십 대신 **관리자가 콘솔에 등록한 근거 채널**(`bot_source_channel`)만 읽고, 호출 권한은 `bot_access` 의 개인 권한만 인정한다. 채널에서
불러도 상세 답변은 **요청자 DM 으로만** 보내고 채널에는 전달 상태만 남긴다(`vip_*.py`).

- 2026-11-12 에 독립 런타임과 전용 systemd 유닛을 삭제했다(두 구현이 갈라지는 것을 막기 위해). 이제 TYBot 프로세스가 DB 의
  `bot_connection`(`bot_key='vipbot'`, 활성, 신원 검증됨, 토큰 쌍 완전) 마다 Socket Mode 세션을 연다. `.env` 의 `VIPBOT_WORKSPACE` 는 읽지 않는다(B-95).
- 등록 근거가 없거나 DB 판정을 못 하면 검색·분류·LLM 호출 전에 종료한다. 질문 첨부는 임시로만 읽고 아카이브에 쓰지 않는다(B-84).
- 후속 질문은 일반 봇과 같은 경로(원문 좌표를 현재 VIP 권한으로 다시 열기)를 쓴다. 한때 있던 `vipbot_history.py`(VIP 전용 30일 기록 로더)는 위 삭제와 함께 사라졌다.

---

## 18. 추천 읽는 순서 (초보 개발자용)

1. [`README.md`](../README.md) — 다이어그램 4장 (구조 / 질문흐름 / 권한 / 환각방지)
2. [`CLAUDE.md`](../CLAUDE.md) — 절대 원칙과 오너 결정들
3. [`docs/pilot/README.md`](pilot/README.md) — 실제 사용법·런북
4. `src/tybot/access/__init__.py` — 짧고, 보안의 핵심
5. `src/tybot/archive/writer.py` → `archive/store.py` — 데이터가 어떻게 쌓이고 읽히나
6. `src/tybot/intent.py` → `answer.py` → `gateway/router.py` — 질문이 답이 되는 길 (answer.py 는 크니 `AnswerEngine.respond` 부터)
7. `src/tybot/slack/pilot.py` — 위 모듈들이 Slack 이벤트에 어떻게 붙는지
8. [`docs/design/bot-hierarchy.md`](design/bot-hierarchy.md), [`summary-review.md`](design/summary-review.md), [`dm-workspace.md`](design/dm-workspace.md),
   맥락 관련은 [`thread-follow-up-evidence.md`](design/thread-follow-up-evidence.md) → [`personal-memory-threat-model.md`](design/personal-memory-threat-model.md)
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
| **VIPBot** | 임원용 별도 봇. TYBot 과 같은 답변 코드, 다른 권한·근거 채널·모델이고 상세 답변은 DM 으로만 |
| **Archiver(Archiving Bot)** | 수집 전담 Slack 앱. 원문·첨부 수집과 변환·보관의 정본 쓰기 주체 |
| **섀도(shadow)** 수집 | Archiver 가 운영에 영향 없이 비교용으로 먼저 돌던 단계. B-99 로 제거 중 |
| **다이제스트(digest)** | 채널·하루 단위 검색용 요약. 어느 원문을 볼지 고르는 길잡이일 뿐 근거·출처가 아니다 |
| **개인 기억** | 사용자별 선호·용어·사실 주장·자유 요청을 DB(`user_memory`)에 두고 프롬프트에 데이터로 넣는 것. 모델 학습이 아니다 |
| **정정(fact_correction)** | 사람이 남긴 "전 → 후" 사실 정정. 원문은 그대로 두고 원문을 볼 수 있는 사람의 답변 끝에 덧붙인다 |
| **Vision** | 이미지·PDF 쪽을 외부 비전 모델로 설명·전사하는 보완 경로(B-80/B-81). 결과는 파생 색인이다 |
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
- **2026-11-24 갱신 한계:** 신규·변경 파일은 BACKLOG 의 해당 항목, CLAUDE.md, 설계 문서와 일부 코드 앞머리(docstring)만 읽고 반영했다.
  서버 운영 상태(배포 여부, 타이머 활성화, 스키마 적용)는 확인하지 못했다. 코드가 저장소에 있어도 BACKLOG 체크박스와 어긋날 수 있다
  (예: 채널×일 다이제스트는 코드·타이머·콘솔 화면이 있으나 B-93 체크박스는 미표시). 14장의 신규 화면 설명은 BACKLOG 기준이고
  화면 코드를 직접 열어 본 것은 아니다.
- 초판 17장 표기(용어집·읽는 순서의 장 번호)가 실제 번호와 어긋나 있던 것을 바로잡았다(읽는 순서 18장, 용어집 19장).
