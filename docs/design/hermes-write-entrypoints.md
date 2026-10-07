# Hermes 원문 쓰기 진입점 — 목록과 경계

> 작성: 2026-10-02
> 대상: `subbots/hermes/` (우리 저장소에 들어온 Hermes 구현)
> 고정: [`tests/test_hermes_write_boundary.py`](../../tests/test_hermes_write_boundary.py)

## 1. 왜 목록이 필요한가

`82c120f` 로 Hermes 전체 소스(277 파일)가 `subbots/hermes/` 에 들어왔다. 그 안에는
**Slack 에서 원문을 받아 아카이브에 쓰는 경로가 여럿** 있다 — 스케줄러, CLI, 그리고
Claude Code 스킬까지.

지금 이 경로들은 **아무도 실행하지 않는다.** TYBot 은 Hermes 소스를 돌리지 않고
`contract/prompt.md` 만 읽는다(§3). 그런데 「지금 안 돈다」 와 「돌 수 없다」 는 다르다.
`npm run ingest` 한 줄이면 돌고, 스킬은 **에이전트가 집어서** 돌릴 수도 있다.

그래서 목록을 만들고 시험으로 고정한다. **새 쓰기 경로가 생기면 시험이 깨진다** —
조용히 늘어나는 것을 막는 것이 이 문서의 전부다.

## 2. 쓰기·수집 진입점 전수

### 2.1 스케줄러 — `src/scheduler.js`

| 작업 | 하는 일 | 쓰나 |
|---|---|---|
| `ingest` · `ingestPre` | 자동 반영 (`ingest/index.js` → `runIngest`) | **예** |
| `daily` · `weekly` | 요약 생성·Slack 전송 | **예**(요약) |
| `health` · `healthPre` | 위생 점검 | 아니오 |

`npm start` → `src/index.js` → 이 스케줄러. **기동만으로 쓰기가 예약된다.**

### 2.2 CLI — `package.json` scripts

| 스크립트 | 쓰나 | 비고 |
|---|---|---|
| `ingest` | **예** | 수집·반영. `--dry` 로 억제 가능 |
| `backfill` | **예** | 소급 수집 |
| `init-archive` | **예** | 저장소 씨앗 생성 |
| `digest:daily` · `digest:weekly` | **예** | 요약 + Slack 전송 |
| `start` | **예**(간접) | 스케줄러 기동 |
| `ask` · `health` · `doctor` · `deployed` | 아니오 | 읽기·진단 |
| `check` · `check:offline` · `check:archive` · `check:live` | 아니오 | 점검 |
| `log:render` · `log:measure` | 아니오 | 로그 가공 |

### 2.3 스킬 — `.claude/skills/*/scripts/`

**가장 위험한 축이다.** 사람이 명령을 치지 않아도 에이전트가 고를 수 있다.

| 스킬 | 스크립트 | 하는 일 |
|---|---|---|
| `doc-archive` | `fetch_slack_files.py` | Slack 파일 **다운로드** |
| | `insert_entry.py` | 아카이브 **삽입** |
| | `join_channels.py` | 채널 **참여** |
| | `apply_approvals.py` · `review_batch.py` | 승인 반영·검토 기록 |
| | `check_against_libreoffice.py` · `xlsx_to_blocks.py` | 외부 프로세스 실행 |
| `slack-sync` | `insert_messages.py` | 원문 메시지 **삽입** |
| | `apply_edits.py` · `verify_archive.py` · `verify_backfill.py` | 편집 반영·검증 |
| `archive-inbox` | `decide_work.py` · `review_work.py` | 작업 판정·기록 |
| | `decision_export.py` (2026-10-06) | **검토 결정 기록만** 쓴다 — 원문·아카이브는 안 건드린다 |
| `archive-run` | `board.py` | 진행판 기록 |

`decision_export.py` 는 원문을 쓰지 않는다. 그래도 이 표에 있는 이유는 이 목록이
「원문 쓰기」 목록이 아니라 **쓰기 성격의 진입점** 목록이기 때문이다 — 예외를 하나
두면 다음 사람이 그 파일에 원문 쓰기를 더해도 관문(`test_hermes_write_boundary.py`)이
안 걸린다.

### 2.4 쓰기 원시 함수 — `src/`

| 모듈 | 호출 |
|---|---|
| `src/ingest/util.js` | `writeFile` · `mkdirSync` · `renameSync` · `rmSync` · `execFile` |
| `src/convo-log/store.js` | `appendFile` · `writeFile` · `mkdirSync` · `renameSync` |
| `src/slack-live.js` | `writeFile` · `mkdirSync` · `renameSync` |
| `src/ingest/git.js` · `verify.js` | `execFile` (git) |
| `src/ingest/slack-archive.js` · `pending-work.js` | `unlinkSync` |

## 3. TYBot 연동 모드 — 스위치 하나로 막는다 (2026-10-06 구현)

```bash
HERMES_MODE=pf             # PF 직접 실행. 기존 writer까지 유지하는 롤백 모드
HERMES_MODE=pf-archiver    # PF 질문·DM·요약 유지, 원문 쓰기만 전부 막는다
HERMES_MODE=tybot          # TYBot 계약 연동. 독립 Node 런타임도 막는다
                           # 미설정은 pf, 그 밖의 값은 기동 오류
```

판정은 **두 곳에서만** 읽는다 — 봇 쪽 [`src/mode.js`](../../subbots/hermes/src/mode.js),
스킬 쪽 [`.claude/skills/_shared/mode.py`](../../subbots/hermes/.claude/skills/_shared/mode.py).
여기저기서 읽으면 판정이 갈리고, 갈리면 오류가 아니라 **한쪽만 막힌 상태**로
나타난다 — 봇은 멈췄는데 스킬은 쓰고 있는 식이다.

### 막는 자리 — 네 축 전부

| 축 | 어떻게 | 어디 |
|---|---|---|
| 스케줄러 | 쓰는 작업을 **예약하지 않는다** | `src/scheduler.js` |
| CLI | 종료 코드 **2** 로 멈춘다(실패 1과 구별) | `run-ingest` · `run-backfill` · `init-archive` |
| 스킬 | `main()` **첫 줄**에서 종료 코드 2 | 쓰기 스킬 10개 |
| 직접 모듈 호출 | `ArchiveWriteBlocked` 를 던진다 | `runIngest` · `ingestConversations` · `writeMonth` · `commitAndPush` |

관문은 **맨 앞**에 둔다. `runIngest` 는 아래에서 git sync 를 도는데, 관문이 그 뒤면
트리를 건드린 뒤에 막는 꼴이라 「막혔는데 작업 트리는 더러워진」 상태가 남는다.
스킬도 같다 — 인자 해석이나 Slack 호출이 먼저 돌면 막기 전에 네트워크로 나가고,
그건 되돌릴 수 없다.

`commitAndPush` 는 자료 저장소에 **실제로 쓰는 유일한 자리**다. 위 관문을 전부
우회해도 여기서 막힌다.

### 기본값이 PF 인 이유

켜는 쪽을 기본값으로 두면 환경변수를 안 넘긴 PF 운영이 어느 날 조용히 멈춘다.
**막는 쪽이 기본값**(원칙 3)과 반대로 보이지만 대상이 다르다 — 원칙 3 은 「자료
열람」의 기본값이고, 여기는 「이미 돌고 있는 운영」의 기본값이다. 운영을 끄는 결정은
명시적이어야 한다. 모르는 값(오타)도 PF 로 본다.

### 회귀 시험

[`tests/test_hermes_tybot_mode.py`](../../tests/test_hermes_tybot_mode.py) 가 **우회
진입점별로** 본다. `node_modules` 가 있으면 실제로 실행해 막히는지 보고, 없으면
정적으로 관문의 존재와 위치를 본다 — 둘 다 없으면 「설치 안 돼서 통과」 가 되고
그게 가장 나쁜 초록불이다.

## 3.1 왜 이것만으로 충분한가 — 연동의 원래 구조

연동 선언(`subbots/hermes/tybot-specialist.toml`)이 이미 이렇게 정한다.

```toml
execution_mode = "tools"
prompt = "contract/prompt.md"
```

- `tools` = Hermes 는 **우리 아카이브 위의 도구**(`search` · `read_channel` ·
  `read_document` · `fetch_recent_slack`)만 부른다. 그 도구는 전부
  `src/tybot/specialist_tools.py` 안에 있고 **읽기 전용**이다.
- 권한은 `RequestContext` 하나로 판정된다(원칙 3). Hermes 는 자기 권한이 없다.
- 우리 코드가 Hermes 에서 읽는 것은 **`contract/` 아래 마크다운뿐**이다
  (`specialist_adapters.py`, `summary_review.py`). 소스를 import 하거나 실행하는
  경로가 없다.
- 배포도 같다 — `deploy/install.sh` 는 `subbots/*/contract/prompt.md` 만 설치한다.

> 그래서 **TYBot 이 Hermes 를 부르는 경로로는** 원문이 써질 수 없었다. 그런데
> 그것은 「아무도 안 부른다」 이지 「부를 수 없다」 가 아니다 — `npm run ingest`
> 한 줄이면 돌고, 스킬은 에이전트가 집어서 돌릴 수도 있다. §3 의 스위치가 그
> 차이를 메운다.

## 4. PF 직접 호출은 건드리지 않는다

PF 는 자기 배포에서 Hermes 를 직접 돌린다. 질문·DM·요약이 거기서 나온다.
**이 저장소의 사본은 PF 운영에 쓰이지 않는다**(`subbots/pf-hermes-archive/` ·
`subbots/pf-hermes-source/` 는 `.gitignore` 로 막혀 있다).

PF 기능을 유지하면서 원문 writer만 끄는 전환 모드는 `pf-archiver`다. 코드는 준비됐지만
PF 운영 호스트에는 아직 적용하지 않는다. Archiving Bot 자료를 PF Hermes가 읽는 경로와
검색 결과의 동등성을 확인한 뒤 전환한다.

- [x] 끄고 켜는 **단일 스위치**: `HERMES_MODE` (기본값 `pf`)
- [x] `pf-archiver`에서 질문·DM·요약 역할을 유지하는 회귀 시험
- [x] `pf-archiver`에서 수집·소급·첨부 저장을 전부 거부하는 시험
- [x] 즉시 롤백 절차: `HERMES_MODE=pf`로 되돌리고 프로세스를 재기동
- [ ] PF Hermes가 Archiving Bot 정본을 읽는 어댑터와 검색 동등성 검증

## 5. 시험이 고정하는 것

두 파일이 **서로 다른 질문**을 본다. 하나만 있으면 반쪽이다 — 목록이 맞아도 관문이
안 걸려 있으면 아무것도 막히지 않고, 관문이 있어도 새 진입점이 생기면 그리로 샌다.

| 파일 | 보는 것 |
|---|---|
| `test_hermes_write_boundary.py` | 진입점이 **늘지 않는가** |
| `test_hermes_tybot_mode.py` | 각 진입점이 **실제로 막히는가** |

### 5.1 진입점이 늘지 않는다

[`tests/test_hermes_write_boundary.py`](../../tests/test_hermes_write_boundary.py)

1. **진입점 인벤토리가 늘지 않는다** — npm 스크립트·스킬 쓰기 스크립트·`src` 쓰기
   모듈이 위 표와 정확히 같다. 새 경로가 생기면 **이 문서를 고쳐야** 통과한다.
2. **TYBot 은 Hermes 소스를 실행하지 않는다** — 우리 `src/` 어디에도 `subbots/hermes`
   의 `.js`·`.py` 를 부르는 경로가 없다. 읽는 것은 `contract/` 뿐이다.
3. **배포가 소스를 싣지 않는다** — `install.sh` 는 계약 파일만 설치한다.
4. **연동 선언이 `tools` 모드를 유지한다** — `execution_mode` 가 바뀌면 깨진다.

시험이 깨졌을 때 할 일은 「시험을 고치는 것」이 아니라 **왜 쓰기 경로가 늘었는지
확인하는 것**이다. 늘려야 할 이유가 있으면 이 문서에 적고 시험을 같이 고친다.

### 5.2 각 진입점이 실제로 막힌다

[`tests/test_hermes_tybot_mode.py`](../../tests/test_hermes_tybot_mode.py)

- 네 축(스케줄러·CLI·스킬·직접 모듈 호출)을 **진입점마다 하나씩** 본다
- 관문이 **앞머리에** 있는지도 본다 — 뒤에 있으면 막기 전에 부작용이 나간다
- `HERMES_MODE` 를 읽는 곳이 `src/mode.js` · `_shared/mode.py` **둘뿐**인지 본다
- **PF 모드에서는 막히지 않는 것**도 같이 본다. 차단을 넣으면서 PF 운영을 조용히
  멈추는 것이 가장 나쁜 실패다

## 6. 실행 역할과 검토 대조 (2026-10-06 구현)

### 6.1 빠지는 것은 「요약」 이 아니라 「직접 게시」 다

> **2026-10-06 정정.** 처음에 이 절을 「요약 발송은 TYBot 이 맡는다」 로 적었다.
> 틀렸다 — 그렇게 읽으면 **요약의 주인이 TYBot** 이 되고, 목표 구조와 반대로 굳는다.

[`archiving-bot-separation-2026-09-23.md`](archiving-bot-separation-2026-09-23.md)
가 정한 목표 소유자는 이렇다.

| 책임 | 목표 소유자 |
|---|---|
| 원문 수집·첨부 변환·provenance | **Archiving Bot** |
| 검토 후보·정정·승인/만료, 정기 요약, 검색·Q&A | **Hermes** |
| 신원·권한, 전문가 선택, 요청/응답 전달·감사 | **TYBot Master** |

그래서 연동에서 Hermes 가 내려놓는 것은 **자기 스케줄로 Slack 에 직접 올리는 경로**
하나다. 요약을 만드는 규칙은 그대로 Hermes 것이고, TYBot 은 **승인 인터페이스**다.

```text
근거(권한 검사)  TYBot  ─┐
요약 규칙·후보   Hermes ─┤─▶ 사람 확인(DM·Canvas)  TYBot  ─▶ 승인 반영
재요약           Hermes ─┘
```

구조는 `archive-run` 과 같다. 상황판은 **무엇이 남았나·순서·마감**만 알고 절차는
하위 스킬이 쥔다. 여기서는 TYBot 이 상황판, Hermes 가 절차다. 절차를 상황판에
복사하지 않는 이유도 같다 — **복사하면 원본이 바뀔 때 에러 없이 낡는다.**

### 6.1.1 역할표 — 현재와 목표

| 역할 | PF 직접 실행 | TYBot 연동 (현재) | 목표 |
|---|---|---|---|
| `ingest` 수집·소급·첨부 | Hermes | 차단 | **Archiving Bot** (역할 자체가 사라짐) |
| `digest-publish` **직접 게시** | Hermes | 차단 | 차단 유지 — 게시는 승인 뒤에만 |
| `health` 위생 점검 발송 | Hermes | 차단 | Archiving Bot 자료 기준으로 재정의 |
| 독립 Node `answer` 질문·DM 응답 | Hermes | **차단** | TYBot이 Hermes 계약을 권한 도구로 호출 |

`HERMES_MODE=tybot` 은 Node Hermes를 TYBot 옆에 하나 더 띄우는 모드가 아니다.
TYBot 연동의 Hermes는 `tybot-specialist.toml`의 `execution_mode="tools"` 계약이다.
따라서 독립 `npm start`·`npm run ask`·직접 `answerQuestion()`도 막힌다. `npm start`는
의존성 없는 `scripts/run-server.js`가 설정·토큰·아카이브보다 먼저 판정한다. 읽기 전용이라도
Hermes 로컬 아카이브를 열면 TYBot의 `RequestContext` 권한 판정을 우회하기 때문이다.

이름이 `digest` 가 아니라 `digest-publish` 인 이유가 이 표다. 「요약을 안 맡는다」
가 아니라 「직접 올리지 않는다」 다.

막는 자리는 셋이다 — **스케줄 등록** · **발송 함수**(`runDigest`·`runHealth` 앞머리)
· **CLI**. 스케줄만 막으면 손으로 돌리는 길이 남고, 그게 중복 게시의 실제 경로다.

### 6.2 요약 후보 계약 — 이어져 있고, 한 군데가 비어 있다

`summary_review.contract_prompt()` 가 `subbots/hermes/contract/summary-review.md` 를
**런타임에** 읽는다. 규칙을 TYBot 안으로 복사하지 않는다 — 그 점은 §6.1 의 원칙대로다.

| 단계 | 지금 | 목표 |
|---|---|---|
| 근거 수집 | TYBot `_source_rows` (권한 검사) | 그대로 |
| 후보 생성 | TYBot 이 Hermes **규칙**으로 LLM 호출 | 그대로(규칙 주인은 Hermes) |
| 후보 검증 | TYBot `parse_proposals` | 그대로 — 근거 대조는 근거 주인이 한다 |
| 사람 확인 | TYBot Canvas·DM | 그대로 |
| **재요약** | TYBot `projected_summary()` — **결정적 조합** | **Hermes** |

마지막 줄이 비어 있는 자리다. 지금은 승인 항목을 코드가 문자열 치환으로 조합한다
(`summary-review-canvas.md` §2.1 — 「LLM 을 다시 호출해 만들지 않는다」). 그건 모호한
교체를 막으려는 **의도된 결정**이었고, 목표 구조(재요약은 Hermes)와 충돌한다.

바꾸려면 CLAUDE.md 원칙 1 의 파생 요약 다섯 조건을 전부 지켜야 한다 — 사람 승인
전제 · 원문 좌표 상속 · 묶음 상한 · 세대 상한 2 · 연쇄 stale·단일 채널.
**그 전환은 이 변경에 포함하지 않았다.**

### 6.3 이미 끝낸 검토를 생략하는 조건

구현: [`src/tybot/summary_review_reconcile.py`](../../src/tybot/summary_review_reconcile.py)

**「로컬 스킬이 돌았다」 는 생략의 근거가 아니다.** 그것은 어떤 항목을 어떤 원문으로
끝냈는지 아무것도 말해 주지 않는다. 보는 것은 둘이다 — **같은 원문 좌표**인가,
**그 좌표의 해시가 그대로**인가. 그 위에 **확정된 결정**(승인·거절)만 쓴다.

아래 중 하나라도 걸리면 생략하지 않는다.

| 사유 코드 | 뜻 |
|---|---|
| `records_unreadable` | 기록을 못 읽었다 (없는 것과 다르다) |
| `no_match` | 그 좌표에 대한 결정이 없다 |
| `not_final` | 보류 — 아직 끝난 것이 아니다 |
| `source_mismatch` | 워크스페이스·채널이 다르다 |
| `evidence_changed` | 결정 이후 원문이 바뀌었다 |
| `no_coordinate` | 좌표·해시가 없어 대조할 수 없다 |

묻는 쪽으로 틀리면 사람이 한 번 더 볼 뿐이지만, **생략하는 쪽으로 틀리면 아무도
모른다.**

### 6.4 지금은 아무것도 생략되지 않는다 — 그리고 그게 맞다

Hermes 의 결정 기록(`.sync-state.json` 의 `applied`·`dismissed`)은 **항목 id 와 요약
섹션 해시**만 담는다. 원문 줄 좌표도, 그 줄의 해시도 없다. §6.3 의 조건을 채울 수
없으므로 대조는 늘 `no_match`·`source_mismatch` 로 끝난다.

생략하려면 Hermes 가 결정 시점에 아래를 함께 적어야 한다.

```json
{"decisions": [{
  "workspace": "tyit", "channel_id": "C0BQ…",
  "evidence_locator": "2026-10-02.md:42",
  "evidence_hash": "<그 줄의 content hash>",
  "state": "approved",
  "decided_at": "2026-10-02T09:00:00+09:00", "decided_by": "…"
}]}
```

### 6.5 좌표를 채웠다 — 그리고 사내·PF 사이는 여전히 안 맞는다 (2026-10-06, 2단계)

위 §6.4 의 「하지 않았다」 를 **PF 경로에 한해** 채웠다. `decide_work.py` 가 결정하는
순간 `workspace` · `channel_id` · `evidence_locator` · `evidence_hash` · `state` 를
`.sync-state.json` 에 함께 적고, `decision_export.py` 가 그것을
`summary-review-decisions/v1` 로 내보낸다. 자리와 형식은
[`summary-approval-ports.md`](summary-approval-ports.md) §3.A·§3.B.

| 무엇 | Hermes 쪽 값 |
|---|---|
| `workspace` | `config.json` 의 `workspace`. **비어 있으면 아무것도 내보내지 않는다** — 빈 값으로 적으면 받는 쪽에서 아무 결정에도 안 걸리고, 그 상태는 「상대편이 아직 안 했다」 와 화면에서 같다 |
| `channel_id` | `.sync-state.json` 의 `channels[<id>]` 를 뒤집어 얻는다. **한 파일에 ID 가 둘이면 안 쓴다**(개명 이력) |
| `evidence_locator` | `slack-export/channels/<파일>.md#L<첫줄>-L<끝줄>` — **메시지 블록** |
| `evidence_hash` | 그 블록의 sha256(앞 32자). 줄끝은 지문에 안 넣는다 |

**좌표는 결정하는 순간에 적는다.** 나중에 다시 재면 그 뒤 바뀐 원문의 지문이 나오고,
그러면 사람이 보고 정한 것이 아닌 것에 승인이 붙는다.

**좌표를 지어내지 않는다.** 아래 중 하나면 좌표를 비워 두고, 받는 쪽은
`no_coordinate` 로 **다시 묻는다.**

| 비우는 경우 | 왜 |
|---|---|
| 인용을 원문에서 못 찾았다 | 어느 메시지인지 모른다 |
| 근거가 **여러 메시지**에 걸쳐 있다 | 블록 하나를 적으면 사람이 본 근거의 일부만 가리킨다 |
| 원문에 없는 인용 조각이 섞여 있다 | 그 사실이 기록에서 사라진다 |
| 요약 계열이 아니다(새 채널·개명·note·파생값) | 근거 인용이 아예 없다 |
| 채널 ID 나 워크스페이스를 못 냈다 | 어느 자료인지 특정되지 않는다 |

탐색은 **한 자리에서만** 한다. `review_work.evidence_block()` 을 화면(`context_for`)과
좌표 도출이 함께 쓴다 — 따로 찾으면 사람이 본 원문과 기록의 좌표가 다른 메시지를
가리킬 수 있고, 그때 승인과 기록이 어긋난다. 관문:
`tests/test_archive_inbox_reconcile.py` ⑫.

**사내↔PF 사이는 여전히 `no_match` 다.** 좌표계가 둘이기 때문이다(§6.4 위의 표,
`summary-approval-ports.md` §3.2). 변환층은 **만들지 않았다** — 변환이 어긋난 날 다른
메시지를 같은 것으로 보게 되고, 그건 조용히 틀린다. 지금 닫힌 고리는 **PF 안**
(한 PC 에서 정한 것을 다른 PC·VM 이 읽는다)이고, 두 좌표계가 하나가 되는 것은
아카이브가 Archiver 아래로 모일 때다.

## 7. PF Archiver reader 어댑터 (2026-10-07 구현)

> `HERMES_MODE=pf-archiver` 에서 **근거를 Archiver 정본만** 읽는다. 질문·DM·요약은
> 그대로 돌고, Hermes 의 원문 writer 는 계속 막혀 있다. `pf` 는 손대지 않은 롤백 모드다.

### 7.1 왜 reader 를 갈아 끼우나

§3 의 모드 스위치는 **쓰기**를 막았다. 그런데 읽는 쪽이 그대로면 Hermes 는 자기
`slack-export/` 를 계속 본다 — 그 자료는 writer 가 막힌 날부터 **자라지 않는다.**
오류는 한 줄도 안 나고, 「요즘 봇이 옛 얘기만 한다」 로 몇 주 뒤에 드러난다.

### 7.2 구조

```text
<root>/<workspace>/<channel-id>__<채널명>/archive/raw/<YYYY-MM-DD>.md
<root>/<workspace>/<channel-id>__<채널명>/archive/attachments/<file-id>/<판>.md
<root>/<workspace>/dm/<user-id>/archive/…      ← 절대 읽지 않는다
<root>/<다른 workspace>/…                       ← 절대 읽지 않는다
```

구현: [`subbots/hermes/src/archive-reader/archiver.js`](../../subbots/hermes/src/archive-reader/archiver.js)
— `node:fs`·`node:path`·`node:crypto` 만 쓰는 **leaf** 다. `config.js` 를 가져가지 않는다:
가져가면 맞물린 import 가 되고, 그때 `config` 가 초기화 전 `undefined` 로 보여
「설정이 없다」 로 조용히 갈린다.

| 설정 | 값 |
|---|---|
| 루트 | `config.json` 의 `archiver.root`(자료 저장소 기준) 또는 `HERMES_ARCHIVER_ROOT` |
| workspace 키 | `archiver.workspace` 또는 `HERMES_ARCHIVER_WORKSPACE` |

**`config.workspace` 를 경로 키로 추정하지 않는다.** 그 값은 사람이 읽는 이름이고
(그 줄의 주석이 「동작에 안 씁니다」 라고 적고 있다) 실제로 `태영건설 재무팀` 같은
문장이 들어온다 — 경로로 쓰면 디렉터리를 못 찾아 **빈 아카이브**가 되고, 빈 아카이브는
오류가 아니라 「자료가 없습니다」 로 나가서 아무도 못 알아챈다. 키 모양이 아니면 던진다.

설정이 없는데 `pf-archiver` 면 **기동을 막는다.** 조용히 slack-export 로 물러서면
writer 를 끈 상태의 낡은 자료를 최신으로 읽는다.

### 7.3 사본을 만들지 않는다 — 투영이다

디스크에 Hermes 모양 md 를 깔면 `archive.js` 를 한 줄도 안 고쳐도 된다. **안 한다.**
그 파일은 권한 판정을 거치지 않은 **원문 사본**이고, 이 모드가 막으려는 것이 그것이다
(§3). 두 자료가 디스크에 함께 있으면 어느 날 양쪽이 함께 읽혀 **같은 메시지가 두 번**
나온다.

그래서 투영은 메모리에만 있고, 파일 경로 대신 **가상 키**(`archiver:ch/<id>` ·
`archiver:doc/<id>/<file-id>`)를 돌려준다. `config.readCached()` 가 그 키를 받아
reader 에게 넘기므로 `metaBlock`·`channelBrief`·`readChannel`·`scanArchive`·
`documents/store.js` 를 **한 줄도 고치지 않았다** — 고치면 그 자리마다 모드 분기가
하나씩 생기고, 하나를 빼먹는 날 그 자리만 slack-export 를 읽는다.

갈아 끼운 자리는 넷뿐이다.

| 자리 | 무엇 |
|---|---|
| `config.readCached()` | 가상 키를 reader 로 넘긴다. 실제 경로는 그대로 통과 |
| `config.loadSyncState()` | 개명 지도를 정본 디렉터리에서 만든다(`{name, file}` 같은 모양) |
| `archive.listArchivedChannels()` · `channelPath()` · `assertArchive()` · `getIndexText()` | 채널 목록과 본문 출처 |
| `documents.hasDocuments()` · `listProjects()` · `listDocuments()` · `projectDir()` | 첨부 정본을 문서로 |

### 7.4 채널 ID 가 정체성이다

디렉터리 이름에는 **처음 이름**이 남고 지금 이름은 가장 최근 raw 의 프론트매터
`channel:` 에서 읽는다. 그래서 개명해도 같은 채널이고, 개명 지도는
`{name: 지금 이름, file: 처음 이름}` 으로 그대로 성립한다.

이름이 **겹치면 둘 다 뺀다.** 개명 직후 두 채널이 같은 이름을 가질 수 있고, 그때
어느 채널인지 모르는 이름으로 답하면 출처가 틀린다.

### 7.5 좌표가 TYBot 과 **같다** — §6.5 의 전제가 바뀐다

§6.5 는 「좌표계가 둘이라 사내↔PF 대조가 안 된다」 고 적었다. 이 모드에서는 **양쪽이
같은 파일을 읽으므로** 그 전제가 사라진다.

| | 값 |
|---|---|
| locator | `<상대경로>:<줄번호>` — TYBot `_source_rows` 와 같은 모양 |
| `evidence_hash` | `sha256(at \0 author \0 text)` — TYBot `evidence_refs.content_hash` 와 **같은 공식** |
| `message_ts` | 정본 raw 의 `|ts|` 를 그대로 |

실측(2026-10-07): 같은 fixture 에서 Hermes(JS)와 TYBot(Python)이 낸 locator·ts·hash 가
**4건 전부 일치**했다. 관문은 `tests/test_hermes_archiver_reader.py` ②.

한 블록이 두 raw 줄일 수 있다(본문 + 첨부 표시는 같은 ts·작성자로 두 줄에 적힌다).
TYBot 은 그 둘을 **각각** 세므로, Hermes 의 블록 좌표는 **첫 줄**에 맞춘다.

**여전히 추정하지 않는다.** 투영 줄 범위가 블록 하나에 정확히 안 맞으면(두 블록에
걸침·범위 어긋남·`message_ts` 없는 옛 줄) 좌표를 안 내고, 받는 쪽은 다시 묻는다.

### 7.6 격리는 세 겹이다

경로로 거르고 **내용으로 두 번 더** 거른다.

1. `<id>__<이름>` 디렉터리만 읽는다 — `dm/<user-id>` 는 모양이 달라 **경로의 성질로**
   빠진다(B-68 과 같은 축). 다른 워크스페이스는 디렉터리를 아예 안 연다
2. 프론트매터 `workspace` 가 설정 키와 다르면 그 파일을 버린다
3. 프론트매터에 `dm_user` 가 있거나 `channel_id` 가 디렉터리의 ID 와 다르면 버린다

경로 체인의 **모든 칸**이 심볼릭 링크가 아닌지도 본다(`shadow_paths.
refuse_symlinked_chain` 과 같은 판정) — 마지막 칸만 보면 `<workspace>` 가 남의 자리를
가리킬 때 통과한다.

실측: 셋을 **전부** 끄면 샌다(잘못 놓인 정본 파일을 fixture 가 만든다). 하나만 끄면
남은 둘이 잡는다 — 그게 겹의 뜻이고, 그래서 「하나를 지우면 빨개지는」 시험은 만들 수
없다. 대신 세 판정이 **소스에 남아 있는지**를 시험이 본다. 겹 하나가 조용히 사라지는
쪽이 이 자리의 실패 방식이다.

### 7.7 비공개 판정은 바꾸지 않았다 — 남은 전제

Hermes 의 공개·비공개는 `config.json` 의 `privateChannels` 가 쥔다(`isPrivateChannel`).
정본의 `visibility` 는 기본값이 `private` 이라, 그것으로 갈아치우면 PF 의 거의 모든
채널이 비공개가 되어 답변이 통째로 닫힌다. **그래서 권위를 옮기지 않았다.**

대신 그 값을 투영 본문에 **적지 않는다** — 적으면 모델이 공개 채널을 비공개로 읽고
「말할 수 없다」 로 닫는다. 값은 `channelMeta().visibility` 로 꺼내 진단에서 대본다.

> **운영 전제**: `pf-archiver` 로 전환해도 `privateChannels` 는 **계속 `config.json` 에
> 선언해야 한다.** 정본의 `visibility`·`acl` 을 권위로 올리는 것은 별건이고, 올릴
> 때는 PF 의 모든 채널이 한 번에 닫히지 않는지 먼저 세어야 한다.

첨부의 `열람` 은 정본 `visibility` 에서만 온다. **`공개승인` 은 만들지 않는다** —
그건 사람이 Slack 스레드의 `[공개]` 를 옮겨 적은 기록이고 정본에는 그 개념이 없다.
없는 승인을 지어내면 비공개 채널 자료가 팀 앞에 나간다(Hermes 의 두 줄 규칙).

### 7.8 fixture 는 실제 수집기가 만든다

[`scripts/make_archiver_fixture.py`](../../scripts/make_archiver_fixture.py) 가
`archiving_bot.ShadowCollector` + `backfill.run` 을 그대로 돌린다. Slack 클라이언트만
가짜다 — 경로는 `shadow_paths`, 줄 모양은 `archive.writer`, 첨부 정본은
`attachment_writer` 가 만든다.

손으로 적은 fixture 는 **적은 사람이 생각한 모양**만 고정하고, 정본이 바뀌어도 시험은
통과한다. 그때 reader 는 운영에서만 깨진다.

### 7.9 관문

| 무엇 | 어디 |
|---|---|
| 기능·격리·좌표를 봇이 쓰는 함수로 | `subbots/hermes/scripts/check-archiver-reader.js` |
| 위를 CI 에 들이고 TYBot 좌표와 대조 | `tests/test_hermes_archiver_reader.py` |
| `pf` 롤백이 slack-export 를 읽는다 | 같은 파일 ④ |
| 설정 없음·사람 이름 키는 기동 실패 | 같은 파일 ⑤⑥ |
| `pf-archiver` 에서 쓰기가 계속 막힌다 | 같은 파일 ⑦ · `tests/test_hermes_tybot_mode.py` |
| fixture 가 실제 writer 에서 나왔다 | 같은 파일 ⑧ |

### 7.10 캐시 지문에 첨부가 들어간다 (2026-10-07)

처음 판은 지문에 **raw 만** 넣었다. 그러면 raw 를 안 건드린 채 첨부가 생기거나
재변환되거나 지워져도 캐시가 안 갈린다 — 오류가 아니라 **「문서가 안 바뀐다」** 로만
드러나고, 사람은 변환이 실패한 줄 안다.

지문에 들어가는 것이 셋이다.

| 무엇 | 무엇을 잡나 |
|---|---|
| `attachments/` 의 file-id 목록 | 신규 첨부, 통째 삭제 |
| file-id 마다 판 파일 목록 | 재변환(새 판 파일), 마지막 판 삭제 |
| 판 파일의 mtime·크기 | 같은 판을 덮어쓴 경우 |

목록 자체를 넣는 이유는 **빈 디렉터리**와 **사라진 디렉터리**를 가르기 위해서다.
파일 stat 만 모으면 마지막 판을 지운 것과 처음부터 없던 것이 같은 지문을 내고, 그때
지워진 문서가 캐시에 남는다.

관문은 **한 프로세스 안에서** 신규·재변환·삭제를 차례로 일으키고 `documents()` ·
`readKey()` · `listDocuments()` 가 즉시 따라오는지 본다. 프로세스를 다시 띄우면 캐시가
어차피 비어 있어 지문이 틀려도 통과한다 — 그러면 그 시험은 아무것도 안 지킨다.
raw 의 mtime 이 그대로인 것도 함께 확인한다(`tests/test_hermes_archiver_reader.py` ⑨).

### 7.11 순회하는 모든 칸에서 링크를 거부한다

§7.6 의 체인 검사는 `<root>/<workspace>` 까지였다. 그 아래는 안 봤다. 이제
**실제로 여는 칸을 전부** 본다.

```text
<workspace>/<channel-id>__<이름>/   ← 채널 디렉터리
  archive/                          ← archive
    raw/                            ← raw 디렉터리
      <날짜>.md                     ← raw 파일
    attachments/                    ← attachments 디렉터리
      <file-id>/                    ← file-id 디렉터리
        <판>.md                     ← 판 파일
```

일곱 칸 중 하나라도 심볼릭 링크면 **source 전체를 거부한다.** 건너뛰지 않는다 —
건너뛰면 「그 채널만 안 보인다」 가 되고 그건 자료가 없는 것과 화면에서 같다. 더 나쁜
쪽은 링크가 **밖을 가리키는** 경우다: 그때 읽는 본문은 정본이 아닌데 출처는 정본
경로로 찍힌다. 중간 칸(디렉터리)만 보면 `raw/<날짜>.md` 한 장을 갈아 끼우는 수법이
지나가고, 파일만 보면 `archive` 를 통째로 돌려놓는 수법이 지나간다.

**거부를 「비었다」 로 읽지 않는다.** `config.archiveHasContent()` 가 그 예외를 삼키면
「신규 설치」 로 판정되고, 그 판정은 개명 지도를 살아 있는 것으로 본다 — 비공개 채널의
옛 이름이 공개로 판정되는 자리다(`deadOrFresh` 주석). 링크 하나로 권한이 열리는 길이라
그 `try` 를 좁혔다. 관문: 같은 파일 ⑩⑪.

### 7.12 공개·비공개 — 권위는 `archive_channel_mode.is_private` 다

> **2026-10-07.** 정본의 `visibility` 로는 공개 여부를 알 수 없다. 권위를 DB 의
> `archive_channel_mode.is_private` 로 못 박고, 그 값만 담은 **manifest** 로 넘긴다.

#### 왜 정본으로는 안 되나

| 자리 | 값 |
|---|---|
| raw 의 `visibility` | **전 채널 `private`.** 수집기가 그 값을 안 넘겨 `writer.ingest` 기본값이 박힌다 |
| 첨부 정본의 `visibility` | 진짜 값(`is_private` 에서 왔다). 다만 **첨부 없는 채널에는 없다** |

raw 로 대조하면 모든 채널이 「비공개인데 선언이 없다」 로 나오고, 늘 빨개지는 관문은
사람이 끄는 법부터 배운다. 첨부로만 대조하면 첨부 없는 채널이 영영 「모름」 이다.

진짜 권위는 Slack 의 `conversations.list` 가 주는 `is_private` 이고, 그 값은
`channel_membership.sync` 가 `archive_channel_mode` 에 적어 둔다.

#### manifest — 담는 것이 다섯뿐

```json
{
  "schema": "channel-privacy-manifest/v1",
  "workspace": "tyit",
  "generated_at": "2026-10-07T00:00:00+00:00",
  "channels": [{ "channel_id": "C…", "channel_name": "팀_…", "is_private": false }]
}
```

이 파일은 **조직 경계를 넘어 다닌다**(사내 DB → PF 파일). 그래서 원문·토큰·DSN 은
물론이고 `note`·`updated_by`·`cutover_ts` 같은 운영 흔적도 안 담는다 — 공개 여부를
판정하는 데 필요 없고, 필요 없는 것을 담으면 그 파일이 언젠가 다른 용도로 쓰인다.
행을 dict 그대로 싣지 않고 키를 코드에 박는 이유도 같다: 표에 열이 늘면 그 열이 조용히
건너간다.

만드는 쪽: [`src/tybot/archive/privacy_manifest.py`](../../src/tybot/archive/privacy_manifest.py)

```bash
sudo -u tybot /opt/tybot/.venv/bin/python -m tybot.archive.privacy_manifest \
    --workspace tyit --out /var/lib/tybot/state/privacy-manifest.json
```

**읽기 전용이다.** 질의는 `SELECT` 하나뿐이고 그 앞에 `SET TRANSACTION READ ONLY` 를
건다 — 「쓸 리 없다」 와 「쓸 수 없다」 는 다르고, 운영 DB 에 붙는 코드라 후자로 둔다.
못 거는 드라이버에서는 조용히 넘어간다(보호막이지 전제가 아니다).

#### 확인된 행만 담는다

`is_private` 의 기본값은 `false` 다. **한 번도 동기화되지 않은 행은 「공개」 라고 적혀
있다.** 그 값을 내보내면 비공개 채널이 공개로 선언된다.

그래서 `membership_checked_at IS NOT NULL` 인 행만 담는다 — 그 칸은 `is_private` 와
**같은 UPDATE 문**에서 채워지므로(`archiver_save_membership`), 값이 있다는 것은 Slack
에서 실제로 받아 적었다는 뜻이다. 빠진 채널은 받는 쪽에서 「미확인」 이 되어 전환을
막는다.

#### 전환 관문

`subbots/hermes/scripts/check-archiver-privacy.js` — manifest 와 정본 채널 ID 를
**전수 대조**한다.

| 갈래 | 종료코드 |
|---|---|
| manifest 가 없다·못 읽는다·형식이 다르다·남의 워크스페이스다 | **2** |
| `is_private` 가 불리언이 아니다 | **2** |
| 정본에 있는데 manifest 에 없다(미확인) | **1** |
| manifest 가 비공개인데 `privateChannels` 에 없다 | **1** |
| manifest 가 공개인데 선언은 비공개 | 0 (경고) |
| manifest 에 있는데 정본에 아직 없다 | 0 (경고) |
| 첨부 정본의 값과 manifest 가 다르다 | 0 (경고) |

「모른다」 에서 막는 것이 요점이다. 모르는 채널을 공개로 다루면 오류가 아니라 **평범한
답변**으로 내용이 나가고, 내용을 아는 사람만 알아챈다.

닫히는 쪽으로 틀린 것(공개인데 비공개 선언)으로는 막지 않는다 — 막으면 선언이 과한
설치가 영영 전환 못 하고, 그 상태에서 사람이 배우는 것은 이 관문을 끄는 법이다.

#### Hermes 는 DB 에 안 붙는다

이 관문은 **파일만 읽는다.** 자격증명을 Hermes 쪽에 두면 PF 에 운영 DB 로 가는 길이
하나 더 생기고, 그 길은 읽기 전용이라는 보장이 없다. `tests/test_hermes_privacy_gate.py`
⑫ 가 Hermes 소스와 `package.json` 양쪽에서 DB 클라이언트를 금지한다 — import 를 안
해도 깔려 있으면 다음 사람이 쓴다.

#### ACL 권위는 여전히 `config.privateChannels` 다

이 관문은 **두 기록이 엇갈리는지 세는 자리**다. 정본·manifest 를 권한 판정의 권위로
올리는 것은 별건 결정이고, 그때는 닫히는 채널 수를 먼저 세야 한다.

#### 관문

| 무엇 | 어디 |
|---|---|
| exporter 가 읽기만 하고, 다섯 가지만 담는다 | `tests/test_privacy_manifest.py` (합성 DB) |
| 전환 관문의 일곱 갈래 | `tests/test_hermes_privacy_gate.py` |
| Hermes 가 DB 에 안 붙는다 | 같은 파일 ⑫ |
| 두 쪽이 같은 형식 이름을 쓴다 | 같은 파일 ⑪ |

### 7.13 manifest 는 **실행 중 ACL** 이다 (2026-10-07 후속)

§7.12 의 manifest 는 **전환 관문**에만 쓰였다. 통과한 뒤 봇이 뜨고 나면, 그 사이에

- 공개 채널이 Slack 에서 **비공개로 바뀌어도**
- **새 비공개 채널**이 생겨도

Hermes 는 `config.privateChannels` 만 보므로 그 둘을 **공개로** 다룬다. 사람이
`config.json` 을 고치고 봇을 다시 띄우기 전까지다. 그동안 새는 것은 오류가 아니라
평범한 답변이고, 내용을 아는 사람만 알아챈다.

#### 닫는 쪽으로만 합친다

| 갈래 | 실행 중 판정 |
|---|---|
| manifest 가 **비공개** | **비공개.** `privateChannels` 에 없어도 닫는다 |
| manifest 가 **공개** | 선언이 비공개면 **비공개.** manifest 가 열지는 않는다 |
| 정본은 아는데 manifest 에 **없다** | **비공개**(미확인). 수집이 새 채널에 닿은 뒤 다음 내보내기 전까지의 구간이다 |
| manifest 를 **못 읽거나 오래됐다** | 전체 권한이 아닌 접근을 **전부** 닫는다 |

여는 쪽을 manifest 에 맡기지 않는 이유는, manifest 가 낡거나 틀렸을 때 **열리는**
방향으로 틀리기 때문이다. 여는 실수는 되돌릴 수 없다.

마지막 줄은 **「공개로 후퇴」 가 아니다.** 개명 지도가 죽었을 때와 같은 자리, 같은
모양이다(`isPrivateWith` 의 `map-dead` 분기). 전체 권한은 살린다 — 점검·복구까지
막으면 고칠 수가 없다.

#### 갈아 끼우면 그 자리에서 반영된다

`privacyState()` 가 manifest 파일의 **수정 시각·크기**로 캐시한다. 지도 캐시
(`currentChannelNames`)에 묻지 않는다 — `pf-archiver` 에는 `.sync-state.json` 이 없어
그쪽이 60초 캐시로 도는데, 그 60초는 **공개 채널이 비공개로 바뀐 뒤의 60초**다.

오래됨 판정은 **캐시하지 않는다.** 파일이 그대로여도 시간은 흐르고, 허용 기간을
넘기는 순간 닫혀야 한다.

#### 검증은 한 자리

[`src/archive-reader/privacy-manifest.js`](../../subbots/hermes/src/archive-reader/privacy-manifest.js)
— 관문과 런타임이 **같은 함수**를 쓴다. 두 벌이면 관문은 통과시키고 런타임은 닫는
(또는 그 반대) 조합이 생기고, 그 상태는 「전환했는데 봇이 아무것도 못 본다」 로만
드러난다.

| 거부 사유 | 왜 |
|---|---|
| 경로 없음·못 읽음·JSON 아님 | 「없음」 을 「비공개가 없음」 으로 읽으면 안 된다 |
| 형식·워크스페이스 불일치 | 짐작해 읽으면 공개 여부를 잘못 판정한다 |
| `generated_at` 없음·파싱 실패 | **얼마나 낡았는지 모른다.** 「방금」 과 「석 달 전」 이 같아진다 |
| `generated_at` 이 미래 | 못 재는 값으로 기간을 판정하면 **영원히 신선한** manifest 가 된다 |
| 허용 기간 초과(기본 26시간) | 그 사이 공개 채널이 비공개로 바뀌었을 수 있다 |
| 빈 `channel_id` | 대조에서 조용히 빠진다. 빠진 줄을 모르면 전수 대조가 아니다 |
| 중복 `channel_id` | 뒤엣것이 앞엣것을 덮는다. `true` 뒤에 `false` 면 비공개가 공개가 된다 |
| `is_private` 가 불리언 아님 | `"false"` 를 참으로 읽거나 그 반대로 읽는 쪽이 둘 다 조용히 틀린다 |

전부 관문에서 **rc 2** 이고, 런타임에서는 **접근 차단**이다. 중복은 **전부** 적는다 —
하나만 적으면 사람이 그것만 고치고 다시 돌린다.

미래 시각은 2분까지 봐 준다(`FUTURE_SKEW_MS`). 시계는 조금씩 어긋나고, 1초 차이로
빨개지는 관문은 꺼진다. 그 이상은 손으로 적었거나 시계가 크게 틀린 것이다.

허용 기간은 `archiver.privacyManifestMaxAgeHours` 로 바꾼다. 기본 26시간 — 하루 한 번
내보내는 운영에서 한 회차를 걸러도 살아남는 길이다.

#### 관문

| 무엇 | 어디 |
|---|---|
| `generated_at` 없음·파싱 실패·미래·기간 초과 | `tests/test_hermes_privacy_runtime.py` ①②③ |
| 빈 ID·중복 ID(전부 적기) | 같은 파일 ④⑤ |
| 공개→비공개, 새 비공개 채널, manifest 교체 | 같은 파일 ⑥⑦ |
| 선언이 비공개면 manifest 가 공개라도 비공개 | 같은 파일 ⑧ |
| 못 읽음·오래됨·삭제 → 닫는다 (전체 권한은 산다) | 같은 파일 ⑨⑩ |
| 관문과 런타임이 같은 loader 를 쓴다 | 같은 파일 ⑪ |
