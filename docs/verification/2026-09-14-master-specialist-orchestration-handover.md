# 마스터·전문 봇 오케스트레이션 구현 인계

_구현: Claude / 2026-09-14_
_검증: Codex_
_설계: [`master-specialist-orchestration.md`](../design/master-specialist-orchestration.md) ·
[`hermes-integration-fidelity.md`](../design/hermes-integration-fidelity.md)_
_근거: [`2026-09-13-master-specialist-routing-review.md`](2026-09-13-master-specialist-routing-review.md)_

## 0. 한 줄

**Hermes 는 서버에서 도구를 써 본 적이 없다.** DB 행의 `execution_mode` 가 런타임
객체에 전달되지 않아 모든 전문 봇이 `prompt` 로 돌았다. 그 한 줄을 고쳤고, 같은
고장이 다시 조용해지지 않도록 선언·실제·실패 사유를 전부 값으로 남겼다.

다른 부서 Hermes 와의 실행 동등성은 **여전히 확인되지 않았다**(§6).

## 1. 확인된 원인 (코드 대조 완료)

| 설계 문서의 주장 | 코드 | 상태 |
|---|---|---|
| `available()` 이 `execution_mode` 를 객체에 안 넘긴다 | `specialist_router.py` 행 매핑 | 사실 · 수정 |
| tools 인데 toolbox 없으면 조용히 prompt 로 강등 | `specialist_adapters.build()` | 사실 · 수정 |
| 세 문장 고정 | `subbots/hermes/contract/prompt.md` | 사실 · 수정(v4) |
| 입력 40,000자 단순 절단 | `MAX_EVIDENCE_CHARS` | 사실 · coverage 로 보완(기존) |
| 시각 근거가 있으면 전문 봇을 건너뜀 | `answer.py` `not visual.any` | 사실 · 수정 |
| 실패가 전부 마스터 직접 답변으로 | `answer.py` `_router.complete()` | 사실 · 수정 |
| DB 오류가 빈 후보 목록과 같음 | `available()` | 사실 · 수정 |

## 2. 변경 범위

```
src/tybot/master_planner.py        신규 — MasterDecision/MasterTask, 능력 열거형
src/tybot/specialist_router.py     serve()/select()/capabilities_of()/SpecialistOutcome
                                   RegistryUnavailable, route() 사용 중단
src/tybot/specialist_adapters.py   contract_meta()/supports_visual(), tools 강등 제거
src/tybot/specialist_contract.py   allow_empty_evidence, visual
src/tybot/specialist_tools.py      ToolBudget
src/tybot/answer.py                _ask_specialist()/_unavailable()/_specialist_no_hits()
                                   마스터 업무 답변 제거, advise() 를 전문 봇 경로로
src/tybot/intent.py                standalone_question/capability/specialist/confidence
src/tybot/audit.py                 decision_id/final_responder/attempted/error_code
src/tybot/slack/pilot.py           specialist_hook() 재작성, MasterDecision 배선
src/tybot/console/app.py           executionMode vs contractExecutionMode 등
subbots/hermes/contract/prompt.md  v2 → v4
scripts/measure_specialist.py      serve() 경로로 갱신
tests/test_master_orchestration.py 신규 28건
```

커밋: `4ffce04`(1~6단계) 이후 이 인계까지.

## 3. 실행 명령과 결과

```bash
pytest -q --ignore=tests/test_specialist_runtime_unit.py \
          --ignore=tests/test_specialist_runtime_store.py   # 1795 passed
ruff check src tests scripts                                # 0
```

`test_specialist_runtime_*` 의 33건은 이 개발 PC 에서 `bash` 가 WSL 런처로 잡혀
**이 변경 전부터** 실패한다(stash 후 재실행으로 확인). Linux 서버에서 별도 확인이
필요하며, **미검증으로 인계한다.**

### 되돌림 실험 11건 — 전부 테스트가 잡는다

| 되돌린 것 | 잡는 테스트 |
|---|---|
| `execution_mode` 행 매핑 제거(원래 고장) | `test_a_db_row_keeps_its_execution_mode_all_the_way_to_the_object` |
| toolbox 없을 때 prompt 강등 | `test_tools_mode_without_a_toolbox_is_refused` |
| 전문 봇 실패 → 마스터 답변 | `test_no_failure_becomes_a_master_business_answer` 외 |
| 권한 밖 출처 그대로 사용 | `test_a_source_outside_the_acl_is_discarded_without_a_master_rewrite` |
| 시각 지원 검사 제거 | `test_no_visual_capable_specialist_closes_...` |
| 후보 줄 끊기 | `test_a_failed_candidate_hands_off_to_the_next_approved_one` |
| 지어낸 부모 QA ID 수용 | `test_a_parent_record_id_outside_the_thread_is_refused` |
| 지어낸 능력 이름 수용 | `test_a_made_up_capability_falls_back_to_the_enum` |
| 목록 밖 전문 봇 제안 신뢰 | `test_selection_is_deterministic_and_ignores_invented_keys` |
| 예산 소진 = 자료 없음 | `test_the_tool_budget_stops_an_endless_search_without_claiming_absence` |
| DB 장애 = 빈 목록 | `test_a_database_failure_is_not_an_empty_roster` |

**첫 줄이 중요하다.** 처음 쓴 검사는 `available()` 을 monkeypatch 해서 행 매핑을
건너뛰었고, 그래서 원래 고장을 **못 잡았다.** 검증 문서가 지적한 그대로였다 —
가짜 psycopg 커서로 실제 경로를 지나게 고쳐야 잡혔다.

## 4. DB 마이그레이션

**필요 없다.** 능력·시각 지원은 계약 파일 프론트매터에 있고, `execution_mode` 열은
이미 있다(`deploy/sql/specialist_runtime_schema.sql`, 2026-09-11 적용 완료).

다만 운영 DB 확인이 필요하다.

```sql
SELECT key, state, health, execution_mode, version FROM specialist_bot;
```

`hermes` 의 `execution_mode` 가 `tools` 인지 확인한다. `prompt` 면 이번 수정의
효과가 없다.

```sql
UPDATE specialist_bot SET execution_mode = 'tools' WHERE key = 'hermes';
```

`rules` 열이 비어 있지 않으면 **DB 규칙이 계약 파일보다 우선한다.** 계약을 v4 로
올렸는데 답이 안 바뀌면 여기를 먼저 본다. 콘솔이 이제 `rulesSource` 로 보여 준다.
**기존 DB 규칙을 자동으로 덮어쓰지 않았다** — 지울지는 오너 판단이다.

```sql
SELECT key, length(rules) FROM specialist_bot WHERE rules <> '';
```

## 5. 운영 전 확인 (서버에서만 가능)

1. 배포 후 `journalctl -u tybot@<ws> | grep specialist_result` — `selected=hermes`
   가 실제로 찍히는지. `attempted=-` 면 후보를 못 골랐다는 뜻이다.
2. 같은 로그의 `tool_calls=` — **0 이면 도구를 한 번도 안 불렀다.** 그러면
   `execution_mode` 가 아직 `prompt` 다.
3. 콘솔 전문 봇 화면에서 `executionMode` 와 `contractExecutionMode` 가 같은지.
4. 미수금 후속 질문 3단계를 실제 스레드에서 재현(설계 §9.1).
5. 이미지 첨부 질문에서 `final_responder` 가 `none` 이 아닌지.
6. 전문 봇을 일부러 끄고(`state='disabled'`) 물었을 때 **마스터 문장이 아니라**
   `사유 코드: no-specialist-registered` 가 나오는지.

## 6. 하지 못한 것 (미검증으로 인계)

- **다른 부서 Hermes 와의 동등성 비교.** 그쪽 모델·프롬프트·자료·실행 기록을
  가져오지 않았다. `scripts/measure_specialist.py` 는 경로 2(어댑터 직접)와
  경로 3(전체 경로)만 비교한다. 경로 1은 **비교 미실시**다.
- **Linux 지원 환경 전체 테스트.** 위 33건은 Windows 셸 문제로 미확인.
- **골든셋 일관성 측정.** 같은 의미의 질문을 여러 표현으로 반복해 라우팅이
  흔들리지 않는지는 실사용 질문이 쌓여야 잴 수 있다(`measure_specialist.py` 는
  지어낸 질문을 쓰지 않는다).
- **`ANALYSIS` 작업 종류는 아직 분류기가 만들지 않는다.** `KIND_MAP` 에 자리는
  있지만 `intent.KINDS` 에 대응 항목이 없어 실제로는 `factual`/`summary` 로 간다.
- **HTTP 실행 모드는 이번 변경에서 손대지 않았다.** `serve()` 는 `http` 를
  `prompt` 와 같은 길로 보낸다 — 격리 컨테이너 전송은 `specialist_http.py` 가
  붙을 때 연결한다. 지금 `http` 로 등록하면 프롬프트형으로 돈다(**남은 위험**).

## 7. 남은 위험

| 위험 | 영향 | 완화 |
|---|---|---|
| Hermes 가 유일한 `internal_document_qa` 봇이다 | Hermes 장애 = 업무 답변 0건 | 의도된 정책(설계 §5.2). 두 번째 승인 봇을 두면 체인이 작동한다 |
| `http` 모드가 조용히 prompt 로 돈다 | 선언과 실제가 갈린다 | 위 §6. `http` 등록 전에 연결 필요 |
| 도구 예산 기본값이 실측이 아니다 | 긴 종합에서 일찍 끊길 수 있다 | `tool_calls=`/`budget=` 로그로 조정 |
| planner 가 `standalone_question` 을 안 주면 결정적 폴백이 이전 질문을 앞에 붙인다 | 묻지 않은 주제가 섞일 수 있다 | 지칭 표현이 남아 있을 때만 붙인다. 로그의 `standalone` 으로 확인 |

## 8. Codex 검증 순서 (설계 §11)

1. 변경 커밋과 새 테스트가 설계의 각 요구를 **실제 경로로** 검사하는지 본다.
   특히 monkeypatch 로 검사 대상 경로를 건너뛴 곳이 없는지.
2. `rg "self\._router\.complete" src/tybot/answer.py` — 남은 세 곳이 전부
   「전문 봇 계층이 없는 설치」 경로인지 확인(확인 완료: 1070·1156·1541행).
   그 상태로 기동하면 `AnswerEngine` 이 기동 로그에 경고를 남긴다
   (`test_an_engine_without_a_specialist_layer_says_so_at_startup`).
3. 미수금 후속, 주간 보고, 시각 첨부, 전문 봇 장애 시나리오를 집중 실행.
4. 전체 pytest, ruff, 콘솔 빌드.
5. §5·§6 을 실제 서버에서 확인하고 결과를 이 문서에 덧붙인다.
