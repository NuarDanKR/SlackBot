# B-81 이미지 설명 정책 변경 경계 (2026-10-28)

Claude 구현분. **커밋하지 않았다.** 독립 리뷰·라우트 연결·커밋은 Codex.

정책은 OFF 그대로다. 모델 호출·소급 작업·worker·systemd 변경은 하지 않았다.
Codex 소유 파일(`image_description_store.py`, clearance API, 스키마, 유닛)은
건드리지 않았다.

| 파일 | 내용 |
|---|---|
| `src/tybot/console/image_description_policy_admin.py` | 정책 변경 경계 |
| `tests/test_image_description_policy_admin.py` | 98건 |

---

## 계약

```python
PolicyChangeRequest(workspace, expected_revision, enabled, config, reason, actor)

preview_policy_change(request, *, now, connect=None) -> PolicyChangePreview
save_policy_change(request, *, now, connect=None)   -> PolicyChangeOutcome
```

`workspace=""` 가 전역 행이다. `connect` 는 기본으로
`image_description_admin._connect` — 즉 **Vision 전용 DSN** 으로만 붙는다.
Master/Archiver 자격으로 정책을 고치는 길은 없다.

---

## 1. 저장은 허가가 아니다

이 경계가 하는 일은 정책 행 하나를 고쳐 쓰는 것뿐이다. 저장했다고 모델을 부르지
않고, 이미 쌓인 파일을 소급해 돌리지도 않는다. 호출 권한은 전송 직전에
`preflight_image_description` 과 `ImageDescriptionStore.reserve` 가 **현재 사실로
다시** 판정한다.

그래서 여기서 통과한 저장이 나중에 거절될 수 있고, 그게 맞다. 이 모듈의 TOCTOU
가 위험하지 않은 이유도 같다 — 읽어 본 값이 낡았더라도 **호출 직전 판정이 다시
본다.**

시험이 거는 것:
- 모듈 소스에 `router`·`describe_image`·`coordinator`·`dispatch`·`reserve`·
  `load_original`·`backfill`·`requests`·`httpx`·`subprocess` 가 **한 줄도 없다**
- 저장은 `save_policy` 를 정확히 한 번 부르고 다른 저장소 메서드를 부르지 않는다
  (가짜 저장소의 `__getattr__` 이 다른 호출을 모두 실패로 만든다)
- 활성화가 막히면 **아무것도 쓰지 않는다**

### 그런데 왜 미리 막나

화면에서 「켜짐」 으로 저장해 놓고 실제로는 아무것도 안 도는 상태가 가장 나쁘다.
운영자는 켰다고 믿고, 증상은 몇 주 뒤 **「왜 사진이 하나도 안 잡히나」** 로
나타난다 — 요약 검토의 「대기 31건·처리 0건」 과 같은 모양이다.

---

## 2. expectedRevision — 읽기 API 가 주는 값은 **합성**이다

이번에 찾은 함정이다. **라우트를 붙이기 전에 알아야 한다.**

`effective_policy` 가 내려 주는 `revision` 은 `전역revision:워크스페이스revision`
합성이다(`_effective`). 그런데 `save_policy` 는 **쓰는 행 하나**의 revision 과
비교한다. 화면이 읽은 값을 그대로 돌려주면 비교가 영원히 어긋나고,
**저장이 항상 `policy_conflict` 로 떨어진다.**

그래서 여기서 갈라 쓴다.

| 저장 대상 | `expectedRevision` 이 합성일 때 |
|---|---|
| 워크스페이스 행 | 뒤쪽(워크스페이스 revision)을 쓴다 |
| 전역 행 | 앞쪽(전역 revision)을 쓴다 |
| 합성이 아니면 | 그대로 넘긴다 |

충돌 **판정 자체는 하지 않는다.** `save_policy` 가 전역 행을 `FOR UPDATE` 로
잠근 뒤에 하므로, 여기서 한 번 더 비교하면 잠금 밖 비교가 하나 늘 뿐이고 그것은
틀릴 수 있다.

> **남은 틈 (의도적).** 워크스페이스 행을 저장할 때 **전역 행이 그 사이 바뀌면**
> 잡히지 않는다. 쓰는 행의 revision 은 그대로이기 때문이다. 잠금 안에서 두 행을
> 함께 보려면 `save_policy` 를 고쳐야 하는데 그 파일은 Codex 소유다.
> 실질 위험은 낮다 — 잘못 병합된 설정은 전송 직전 판정이 다시 거절한다.
> 고칠지는 Codex 판단.

---

## 3. 전역과 워크스페이스

덮어쓰기 규칙은 저장소(`_effective`)가 소유하므로 베끼지 않고 같은 방식으로
합친다 — `global.config | workspace.config | 이번_요청`.

**쓰는 것은 요청한 키뿐이다.** 합친 값을 써 넣으면 전역을 고친 날 워크스페이스가
따라오지 않는다. 시험이 그것을 건다.

### 상한 둘은 합치지 않는다

`max_concurrency` 와 `daily_calls` 는 `reserve` 가 전역 행과 워크스페이스 행에서
**따로** 읽어 둘 다 만족해야 통과시킨다(전체 상한과 워크스페이스 상한이다).
합쳐서 보면 한쪽만 채워도 통과해 버리고, 그 설정은 저장은 되는데 호출은 안 된다.

```python
PER_ROW_KEYS = {"max_concurrency", "daily_calls"}
MERGED_KEYS  = frozenset(CONFIG_KEYS) - PER_ROW_KEYS   # 목록을 베끼지 않는다
```

`CONFIG_KEYS` 를 import 하므로 저장소가 키를 늘리면 따라온다. 시험이 모든 키에
검사기가 있는지 확인한다.

---

## 4. 필수 상한 누락 시 활성화 거절

`enabled=True` 일 때만 본다. **끄는 것은 언제나 할 수 있어야 한다** — 막으면
사고가 났을 때 못 끈다.

| 코드 | 뜻 |
|---|---|
| `activation_requires_<키>` | 합친 설정에 그 키가 없거나 값이 틀렸다 |
| `activation_requires_global_<키>` | 전역 행에 상한이 없다 |
| `activation_requires_workspace_<키>` | 워크스페이스 행에 상한이 없다 |
| `model_<blocker>` | 카탈로그 판정이 막았다 |

상한이 하나라도 비면 **모델 판정은 하지 않는다.** 둘을 섞어 내보내면 무엇부터
고칠지 흐려지고, 애초에 민감도·리전이 없으면 모델을 평가할 수 없다.

전역 행을 켤 때는 **혼자 완전해야 한다** — 상속받을 곳이 없다.

이번 저장으로 채워 넣는 값도 센다. 아니면 상한을 넣는 저장 한 번, 켜는 저장 한
번으로 두 번 해야 한다.

---

## 5. 서버 카탈로그에 있는 무료 Vision 모델만

판정은 `image_model_catalog.select_free_vision_model` 이 한다. 규칙을 여기 다시
쓰지 않는다 — 베끼면 카탈로그 쪽이 바뀐 날 **갈라진 쪽이 없는 자격을 있다고**
읽는다. 행 → 항목 변환도 저장소의 `_catalog_entry` 를 쓴다.

막히는 경우를 전부 시험한다: Vision 미확인 · 무료 미검증 · credential 미준비 ·
리전 불일치 · 민감도 초과 · quota 소진 · quota 모름 · 검증 만료 · 검증 기록 없음 ·
카탈로그에 없는 모델 · provider 불일치.

**막힌 모델을 다른 모델로 바꿔 주지 않는다.** 바꿔 주는 순간 유료로 조용히
넘어가는 길이 거기서 생긴다. 카탈로그에 쓸 수 있는 다른 모델이 있어도 거절한다.

---

## 6. 사유와 실행자

`save_policy` 는 실행자만 원장에 남긴다. **사유 칸이 스키마에 없다.** 스키마는
Codex 소유라 여기서 고치지 않았다.

대신 콘솔 감사(`console_audit_event`)에 넣을 레코드를 만들어 돌려준다.
라우트가 `audit_store.record(**outcome.audit)` 로 기록한다.

```python
{"actor": …, "category": "archiver",
 "action": "image_description_policy_save",
 "target_type": "image_description_policy",
 "target_id": workspace or "global", "workspace": workspace or None,
 "outcome": "succeeded",
 "metadata": {"reason", "enabled", "changed_keys",
              "previous_revision", "revision"}}
```

시험이 이 모양이 `audit_store.record` 시그니처에 **실제로 맞는지** 확인한다.

사유가 없거나 비었으면 **저장 자체를 거절한다**(`reason_required`). 500자를
넘으면 `reason_too_long` — 감사 칸이 감당할 크기로 둔다.

`metadata` 에는 **무엇이 바뀌었는지만** 적는다. 값(모델 이름·상한)은 원장이 들고
있고, 감사에 베끼면 두 곳이 갈라진다.

---

## 7. 값 검사

```python
config_invalid_<키>    # 어느 키가 틀렸는지 코드가 말한다
config_key_unknown     # 모르는 키는 **버리지 않고 거절**한다
```

모르는 키를 조용히 버리면 운영자는 저장됐다고 믿는다.

`bool` 은 `int` 의 하위형이라 **먼저 거른다** — 안 거르면 `max_image_bytes=True`
가 1 로 통과한다. 시험이 세 키에서 그것을 건다.

값 검사 기준은 전송 직전 판정과 **같아야 한다.** 여기서 통과시킨 값이 거기서
거절되면 운영자는 켰다고 믿고 아무것도 안 도는 상태를 본다. 시험이 두 기준을
대조한다 — 키마다 값을 하나씩 망가뜨려 `preflight_image_description` 이
`policy_invalid` 를 내는지, 그리고 올바른 설정이 정책 단계를 지나
`catalog_unavailable` 까지 가는지 확인한다.

---

## 8. 미리보기

`preview_policy_change` 는 같은 검사를 하고 **쓰지 않는다.** 화면이 저장 단추를
막고 이유를 보여 주려면 저장을 시도해 보기 전에 알아야 한다 — 저장해 보고
실패로 배우게 하면 그 실패가 감사에 남는다.

---

## 9. 시험 98건 · Ruff

`python -m ruff check src tests scripts` **0건**.

---

## 10. 라우트 (2026-11-02 추가)

`src/tybot/console/image_description_policy_routes.py` · 시험 68건.

```
POST /api/archiver/image-description/policy/preview
PUT  /api/archiver/image-description/policy
```

`build_router(current_user, require_admin, check_write_request, audit_event, now=None)`
— Codex 의 clearance 라우트와 같은 의존성 주입이다. **`app` 을 import 하지
않는다.** 모듈 수준에서 서로를 부르면 순환 import 가 되고, 실제로 그렇게 터진
적이 있다(2026-10-28, `router` 속성이 아직 없다는 `AttributeError`).

`app.py` 에는 등록 3줄만 더했다 — clearance 라우터 바로 뒤, 정적 마운트 앞.

### 본문

```json
{"workspace": "tyit", "expectedRevision": "rev-global:rev-tyit",
 "enabled": true, "config": {...}, "reason": "..."}
```

`extra="forbid"` 다. **`actor` 를 본문에서 받지 않는다** — 적어 보내면 422.
조용히 버리면 그 칸이 무시된다는 사실을 모른 채 다른 사람 이름으로 감사가
남는 줄 안다. 실행자는 로그인한 `ConsoleUser.email` 로만 정한다.

`workspace` 는 서버가 다듬고(`strip().lower()`) 가시성을 검사한다.
**전역 행(`""`)은 `all_workspaces` 인 관리자만** 고칠 수 있다 — 전역 기본값은
모든 워크스페이스에 걸리므로, 일부만 보는 사람이 고치면 자기가 못 보는 곳의
설정을 바꾸는 것이 된다(원칙 3).

### 상태 코드

| 코드 | HTTP |
|---|---|
| `policy_conflict` | **409** — 고칠 것은 입력이 아니라 화면의 낡은 값이다 |
| `policy_invalid`·`reason_*`·`config_*`·`activation_blocked` | 422 |
| `policy_unavailable`·`connection_not_idle_autocommit`·`vision_database_*` | 503 |
| 권한·CSRF | 403 |

`detail` 은 **언제나 같은 모양**이다 — `{"code": str, "blockers": [str]}`.
차단 사유가 없을 때도 빈 목록을 준다. 모양이 둘이면 화면이 두 갈래로 갈라진다.

이번에 `image_description_policy_admin` 쪽도 하나 고쳤다. Vision 자격 코드
(`vision_database_*`)가 `policy_unavailable` 로 **뭉개지고 있었다.** DSN 이 없는
것과 DB 가 죽은 것이 같은 글자가 되면 운영자는 고칠 자리를 못 찾는다. 이제
ASCII 고정 코드만 확인하고 그대로 올려 보낸다.

### 미리보기도 CSRF 를 지난다

쓰지는 않지만 **설정과 차단 사유를 읽는** 경로다. 로그인 세션으로만 열려야 한다.

### 감사 실패는 보이게 한다

저장이 끝난 뒤 감사 기록이 실패해도 500 을 내지 않는다. 내면 운영자가 다시
누르고 **같은 변경이 revision 두 개로** 남는다. 대신 응답에 `"audited": false`
로 적는다 — 조용히 성공으로 보이면 감사 공백을 아무도 모른다.

### 시험이 거는 것

권한(관리자 아님 403) · 워크스페이스 범위 · 전역 행과 `all_workspaces` ·
CSRF 헤더 없음 · 교차 출처 · `actor` 본문 거절 · 모르는 칸 거절 ·
미리보기 무쓰기(저장 경로 미호출 + 감사 미기록) · 13개 코드별 상태 ·
`detail` 모양 일정 · DSN·예외 본문 비노출 · 실패 시 감사 없음 ·
감사 폭발해도 저장 유지 + `audited: false` · 소스에 worker·모델 경로 없음 ·
경계 호출 1회 · 시계 주입.

---

## 11. 미검증 · 남은 것

- **실제 DB 에서 안 돌렸다.** 가짜 커서로만 검증했다. `save_policy` 의 잠금·충돌
  동작은 Codex 의 실제 DB 시험이 덮는다
- **전역 행 동시 변경 틈**(§2). 잠금 안에서 두 행을 함께 보려면 `save_policy`
  수정이 필요하다
- **콘솔 화면 없음.** API 는 섰지만 WC-30 탭에 사유 입력·차단 사유 표시·미리보기를
  붙여야 한다. 그때까지는 「쓸 수 있는 사람이 한 명」 이다(CLAUDE.md)
- 사유를 원장에 남기려면 `archiver_image_description_policy_audit` 에 `reason`
  칸이 필요하다. 지금은 콘솔 감사에만 남는다 — 두 곳을 맞출지는 Codex 결정
