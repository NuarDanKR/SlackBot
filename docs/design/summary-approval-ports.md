# 요약 승인 — 인터페이스를 갈아 끼울 수 있게

> 상태: **설계. 승인 전 구현 금지**
> 작성: 2026-10-06
> 전제: [`archiving-bot-separation-2026-09-23.md`](archiving-bot-separation-2026-09-23.md)
> 연결: [`hermes-write-entrypoints.md`](hermes-write-entrypoints.md) §6

## 1. 왜 다시 설계하나

오너 결정(2026-10-06): **TYBot 없이 Archiver + Hermes 만으로도 돌아야 한다.**
PF 팀은 TYBot 을 쓰지 않고, 요약 확인을 **로컬 Claude Code 스킬**에서 한다.

지금 구조는 승인 인터페이스가 TYBot 하나로 못 박혀 있다. `summary_review` 가 후보
생성·검증·Canvas·DM·승인·발송을 전부 들고 있어서, TYBot 을 빼면 **요약 흐름 자체가
사라진다.** 그래서 셋을 가른다.

| 무엇 | 지금 | 앞으로 |
|---|---|---|
| 근거 소유(원문 + **좌표 도출**) | TYBot | Archiver(목표) · Hermes(PF) · TYBot(사내) |
| 요약 생성·**재요약** | TYBot 이 Hermes 규칙으로 | **Hermes** |
| 승인 인터페이스 | TYBot DM/Canvas | TYBot DM/Canvas **또는 로컬 스킬** |

## 2. 세 구성이 전부 돌아야 한다

| 구성 | 수집 | 요약·재요약 | 승인 | 게시 |
|---|---|---|---|---|
| 사내 현재 | TYBot | Hermes 규칙 + TYBot 실행 | TYBot DM/Canvas | TYBot |
| 사내 목표 | Archiver | **Hermes** | TYBot DM/Canvas | TYBot |
| **PF** | Archiver(또는 Hermes 현행) | **Hermes** | **로컬 스킬** | 승인 뒤에만 |

가운데 열이 **어느 구성에서도 Hermes** 라는 것이 이 설계의 전부다. 승인 인터페이스는
갈아 끼우는 부품이고, 요약은 부품이 아니다.

## 3. 키스톤 — 공통 결정 기록

셋을 잇는 것은 **파일 하나의 형식**이다. 이게 있어야 승인 인터페이스를 갈아 끼워도
재요약과 중복 생략이 그대로 동작한다.

```json
{
  "schema": "summary-review-decisions/v1",
  "decisions": [{
    "candidate_id": "…",
    "workspace": "tyit",
    "channel_id": "C0BQ…",
    "evidence_locator": "2026-10-02.md:42",
    "evidence_hash": "<그 줄의 content hash>",
    "evidence_message_ts": "1759…",
    "kind": "number_or_schedule",
    "state": "approved",
    "generation": 1,
    "decided_at": "2026-10-02T09:00:00+09:00",
    "decided_by": "U0BQ… | local-skill",
    "source": "tybot-dm | archive-inbox"
  }]
}
```

읽는 쪽과 쓰는 쪽 모두 [`summary_review_reconcile.py`](../../src/tybot/summary_review_reconcile.py)
에 있다(`load_decisions` · `write_export`). 늘어나는 것은 **누가 쓰느냐**뿐이다.

`source` 의 실제 값은 `tybot-dm` 과 `hermes-archive-inbox` 다.

### 3.A 파일은 어디에 놓이나 (2026-10-06 구현)

```text
$SUMMARY_DECISION_DIR/<workspace>/<source>.json
   예:  /var/lib/tybot/decisions/tyit/tybot-dm.json
        /var/lib/tybot/decisions/tyit/hermes-archive-inbox.json
```

| 왜 그렇게 | 이유 |
|---|---|
| **출처마다 파일** | 담는 것이 한 출처의 **전체 스냅샷**이라 부분 병합이 성립하지 않는다. 한 파일을 둘이 쓰면 마지막 쓰기가 앞 결정을 통째로 덮고 **조용하다** |
| **워크스페이스마다 디렉터리** | 한 파일에 여러 워크스페이스의 채널 ID 를 담으면 읽는 쪽이 권한 없는 워크스페이스의 채널 구조를 알게 된다(원칙 4) |
| **환경변수 하나** | 양쪽이 같은 `SUMMARY_DECISION_DIR` 을 읽는다. 안 정한 설치에서는 내보내기가 **조용히 꺼진다** — 그때는 아무것도 생략되지 않을 뿐이고, 승인 버튼이 실패하지는 않는다 |

쓰기는 고유 임시 파일 + 원자적 교체이고, TYBot 쪽은 그 위에 OS 락과
`ConcurrentExportRefused` 로 **단일 writer 를 명시 거부**로 고정한다. 경로 조각이
`..` 이나 구분자를 담으면 `UnsafeExportTarget` 으로 던진다 — 걸러서 만든 이름은 다른
워크스페이스의 이름과 같아질 수 있다.

### 3.B 실제 호출부 (2026-10-06)

| 언제 | 어디 |
|---|---|
| TYBot DM 결정 1건 | `summary_review.register_slack_handlers.settle()` → `export_decisions()` |
| TYBot DM 「전체 승인」 | 같은 모듈 `approve_all` 핸들러 → `export_decisions()` |
| TYBot 후보 생성 **전** | `generate_channel()` → `_counterpart_decisions()`, 생성 후 `_without_settled()` |
| Hermes 반영·빼·나중에·취소 | `decide_work.py` 의 `approve`·`drop`·`later`·`clear` → `export_now()` |
| Hermes 목록 표시 | `review_work.load_items()` → `_settled_elsewhere()` |

**취소(`clear`)도 내보낸다.** 안 내보내면 풀린 결정이 상대편 파일에 남아, 그쪽이
이미 풀린 결정으로 후보를 생략한다 — 그 건은 어느 쪽에서도 사람 앞에 오지 않는다.

내보내기 실패는 **던지지 않는다.** 결정은 이미 권위 DB·`.sync-state.json` 에 들어가
있는데 버튼이 「실패」 를 돌려주면, 사람은 성공한 일을 다시 하려 하고 두 번째는
「이미 다른 검토자가 처리했습니다」 를 본다. 그 조합이면 자기 결정이 반영됐는지
화면에서 알 수가 없다.

관문: [`tests/test_summary_decision_export.py`](../../tests/test_summary_decision_export.py)
①~③ 이 **호출부가 없는 상태를 금지한다**. 2026-10-05 에 helper 만 들어가고 부르는
곳이 없었는데, 그 상태에서도 단위 시험은 전부 통과했다.
[`tests/test_archive_inbox_reconcile.py`](../../tests/test_archive_inbox_reconcile.py)
는 양쪽 구현이 **같은 스키마·키·사유 코드·파일 자리**를 쓰는지 맞대어 본다 — 두 벌이
갈려도 오류가 안 나고, 대조가 조용히 0건이 될 뿐이다.

### 3.0 본문을 싣지 않는다 (2026-10-06 수정)

처음 설계에는 `proposed_text` 가 있었다. **뺐다.** 이 파일은 다른 쪽이 읽는 것이고,
사내 요약 문장이 PF 로 또는 PF 문장이 사내로 건너가면 그건 크로스 워크스페이스
노출이다(원칙 4). 대조에 필요한 것은 **좌표·해시·상태**뿐이고 본문은 각자 자기
쪽에서 본다. 같은 이유로 `evidence_quote` 도 없다 — 해시가 「같은 줄인가」 를
말해 주므로 인용문을 옮길 이유가 없다.

내보내는 상태도 좁힌다. `approved`·`rejected`·`deferred` 만 보내고 `pending`·
`expired`(미응답 폐기)·`superseded`(대체됨)는 보내지 않는다. 뒤 셋은 **사람이 내린
판단이 아니라서**, 보내면 받는 쪽이 「끝났다」 로 읽을 수 있다.

### 3.1 좌표는 **원문을 가진 쪽이** 도출한다

Hermes 의 요약 스키마(`src/llm/summary-check.js`)는 `{type, where, was, now, evidence}`
만 낸다 — **evidence 는 인용문이고 좌표가 아니다.** 지금 TYBot `parse_proposals` 가
그 인용을 원문과 대조해 locator·hash·message_ts 를 **도출**한다.

그 구조를 유지한다. 모델이 준 좌표를 믿으면 사람이 확인하러 간 자리에 그 문장이
없을 수 있다(원칙 2). 그래서:

| 구성 | 좌표를 도출하는 쪽 |
|---|---|
| 사내 | TYBot (`parse_proposals`) — 그대로 |
| PF | **로컬 스킬** (`review_work.py` 가 이미 하는 탐색을 좌표로 굳힌다) |
| 목표 | Archiver provenance 를 받아 그대로 상속 |

PF 에 Archiver 를 바로 넣지 않는다(오너 결정 2026-10-06) — 사내에서 안정화된 뒤
도입한다. 그래서 가운데 줄은 **버려질 코드가 아니라 상당 기간 쓰이는 경로**다.

**모델은 어느 구성에서도 좌표를 만들지 않는다.**

## 3.2 좌표계가 둘이다 — 사내·PF 사이 생략은 지금 불가능하다

> **2026-10-06 조사 결과.** 「archive-inbox 에서 끝낸 검토를 TYBot DM 에서 생략한다」
> 는 **두 아카이브가 하나가 되기 전에는 성립하지 않는다.**

| | 파일 | 메시지 한 건 | 좌표 |
|---|---|---|---|
| TYBot | `workspaces/<ws>/channels/<id>__<name>/raw/<날짜>.md` | **한 줄** | `파일:줄` |
| Hermes | `slack-export/<채널>.md` | **블록**(`**날짜 · 작성자**` + 본문) | 블록 머리 줄 |

같은 Slack 메시지라도 **파일도 모양도 다르다.** 줄 해시와 블록 해시는 비교 대상이
아니다. 그래서 지금 대조는 늘 `no_match` 로 끝난다 — §6.3 의 막는 쪽 기본값이
이 경우를 이미 올바르게 처리한다(생략하지 않는다).

**이것을 억지로 맞추지 않는다.** 두 좌표를 서로 변환하는 코드를 쓰면, 변환이 어긋난
날 「다른 메시지를 같은 것으로」 보게 되고 그건 조용히 틀린다.

제대로 풀리는 자리는 **수집 단일화**다. Archiver 가 수집을 맡으면 두 서비스가 같은
자료를 보고 좌표계도 하나가 된다. 목표 구조가 주는 또 하나의 이득이고, 그때까지는
사내·PF 사이 생략을 **하지 않는 것이 맞다.**

### 그래서 2단계가 무엇을 만드나

사내·PF 사이가 아니라 **PF 안에서** 성립시킨다.

```text
Hermes 요약 후보 ──(인용)──▶ 로컬 스킬이 원문에서 찾음 ──▶ 좌표로 굳혀 결정 기록
                                                            ▲
                                         다음 회차 후보가 그 좌표와 대조
```

PF 는 TYBot 이 없으므로 이 고리만 닫히면 **중복 확인이 실제로 생략된다.** 사내는
Archiver 통합 뒤에 같은 고리가 좌표계 하나 위에서 돈다.

### 탐색을 두 벌 만들지 않는다

`review_work.py` 는 이미 인용을 원문에서 찾는다 — `_fold()`(정규화) · `_needles()`
(`...` 생략·최소 길이·상한) · `context_for()`(메시지 블록). 좌표를 얻자고 **탐색을
새로 짜면 안 된다.** 같은 인용이 한쪽은 걸리고 한쪽은 안 걸리는 순간, 그건 에러가
아니라 **다른 결과**로 나타난다.

정규화(`_fold`)는 이미 JS↔Python 교차 검증을 받고 있다(`scripts/check-shared-rules.js`).
좌표 도출도 같은 대우를 받아야 한다 — 한 곳에서 찾고, 그 결과를 좌표로 쓴다.

## 4. 재요약을 Hermes 로 — 다섯 조건 매핑

CLAUDE.md 원칙 1 의 파생 요약 다섯 조건을 하나씩 어디에 박을지 적는다. 조건을
못 지키는 설계면 하지 않는다.

| 조건 | 어디에 |
|---|---|
| 1. 사람 승인이 전제 | 입력은 `state == "approved"` 인 기록만. 미승인은 읽지 않는다 |
| 2. 원문 좌표 상속 | 파생 문장이 묶은 항목들의 `evidence_locator`·`evidence_hash`·`evidence_message_ts` **집합**을 그대로 들고 다닌다. 좌표를 잃은 파생은 만들지 않는다 |
| 3. 묶음 상한 | `MAX_BUNDLE`(예: 8). 사람이 한 화면에서 확인할 수 없는 분량이면 승인이 형식이 되고 1번이 무의미해진다 |
| 4. 세대 상한 2 | `generation` 컬럼 + **스키마 제약**. 2차의 입력은 항상 1차이며 다른 2차가 아니다. 코드 규칙이 아니라 DB 제약으로 박는다 |
| 5. 연쇄 stale · 단일 채널 | 물고 있는 1차가 `stale_at` 이면 파생도 재검토. 한 채널 안에서만 묶는다(여러 채널을 묶으면 일부 원문에 권한 없는 사람이 파생 문장으로 내용을 안다 — 원칙 3) |

### 4.1 지금 있는 것을 무엇으로 바꾸나

`summary_review.projected_summary()` 는 승인 문장을 **결정적 치환**으로 조합한다.
그건 「바뀌지 않을 문장이 바뀐다고 읽히는 것」(`AmbiguousProjection`)을 막으려던
의도된 결정이었다(`summary-review-canvas.md` §2.1).

그 안전장치를 버리지 않는다. 바뀌는 것은 **누가 만드느냐**이고, 모호할 때의 동작은
그대로다 — 바꿀 문장을 못 찾거나 두 번 찾으면 **파생을 만들지 않고** 사람에게 묻는다.

## 5. 승인 인터페이스 두 벌

| | TYBot DM/Canvas | 로컬 스킬 |
|---|---|---|
| 누가 | 사내 사용자 | PF 담당자 |
| 어디서 | Slack | Claude Code |
| 쓰는 기록 | `approved_summary_item` + §3 형식으로 내보내기 | §3 형식을 직접 쓴다 |
| 이미 있는 것 | `summary_review` 전부 | `archive-inbox` 의 반영/빼/나중에 |

로컬 스킬 쪽은 **처음부터 만들지 않는다.** `archive-inbox` 가 이미 「건별로 원문과
함께 보이고 반영/빼/나중에를 정한다」 를 한다. 모자란 것은 **그 결정에 좌표를 붙여
§3 형식으로 쓰는 것** 하나다.

## 6. 하지 않는 것

- **PF 의 현재 동작을 끄지 않는다.** 기본값은 지금 그대로다
- **모델에게 좌표를 만들게 하지 않는다**(§3.1)
- **재요약을 모호할 때 강행하지 않는다**(§4.1)
- **두 인터페이스가 같은 채널을 동시에 맡게 하지 않는다.** 한 채널의 승인 주체는
  하나다 — 둘이 각자 승인하면 같은 원문에 두 개의 「확정」 이 생긴다

## 7. 구현 순서와 관문

각 단계는 **앞 단계의 시험이 서 있을 때만** 들어간다.

1. **공통 기록 형식 고정** — 스키마 + 읽기(이미 있음) + 쓰기 양쪽의 왕복 시험
2. **좌표 도출을 Hermes 에도** — PF 구성에서 Hermes 가 자기 아카이브로 대조.
   사내 경로는 건드리지 않는다
3. **`archive-inbox` 가 §3 형식으로 결정을 쓴다** — 그때부터 중복 생략이 실제로 동작
4. **재요약을 Hermes 로** — 다섯 조건을 스키마 제약과 함께. DB 변경이 여기 들어온다
5. **TYBot 쪽 전환** — `projected_summary()` 를 Hermes 호출로 바꾼다. 되돌릴 수 있게
   환경변수 하나로 가르고, 양쪽 결과를 한동안 **비교만** 한다

1~3 은 PF 가 TYBot 없이 도는 데 필요한 전부다. 4~5 는 사내 전환이고, 3 이 서기
전에는 시작하지 않는다.
