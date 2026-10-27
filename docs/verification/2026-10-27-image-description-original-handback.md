# B-81 원본 이미지 bytes loader — Claude 인계 (2026-10-27)

신규 두 파일만 만들었다. Codex 파일과 미커밋 변경은 건드리지 않았다.

| 파일 | 내용 |
|---|---|
| `src/tybot/archive/image_description_original.py` | 로더 |
| `tests/test_image_description_original.py` | 시험 75건 |

## 계약

```python
load_original(candidate, *, archive_root, doc=None, max_image_bytes: int)
    -> OriginalLoad          # .image: ImageInput | None, .error_code: str, .ok
```

예외를 내지 않는다 — 입력 계약 위반(`max_image_bytes`·빈 `archive_root`)만
`ValueError` 다. 배치가 건당 사유를 세야 하고, **예외는 메시지에 경로를 끌고
다니기 쉽다.**

모델·DB·네트워크를 부르지 않는다. 시험이 소스에서 `requests`·`httpx`·`socket`·
`psycopg`·`router` 같은 이름이 없는 것까지 확인한다.

## 요구 10항목

| # | 요구 | 구현 |
|---|---|---|
| 1 | PNG/JPEG/WebP 만, 확장자 불신 | **매직 바이트**로 판별. 이름은 안 본다 |
| 2 | objects 밖·symlink·traversal 차단 | `resolve()` 후 뿌리 안 확인 + 링크 자체 거절 |
| 3 | metadata 경로 문자열 불신 | 경로를 **우리가 만든다**(`files.attachment_storage`) |
| 4 | SHA-256 재계산 대조 | 읽은 바이트로 다시 계산 |
| 5 | stat 크기 + 실제 읽은 크기 | 둘 다. stat 은 **열기 전에** 막는다 |
| 6 | 없거나 여럿이면 거절 | `original_missing` · `original_ambiguous` |
| 7 | 정본 provenance 일치 | ws·채널·file_id·sha256 전부 대조 |
| 8 | 본문·경로·예외 미기록 | 고정 코드 14종. 로그는 좌표+코드 |
| 9 | 모델·DB·네트워크 금지 | 없음(시험이 확인) |
| 10 | `human_upload_confirmed` 없으면 거절 | `is not True` → fail-closed |

## 시험이 잡은 실제 결함 — 좌표 중화

처음 구현은 경로를 `files._safe_component` 로 만들었다. 그 함수는
`../F0ABC` 를 **`F0ABC` 로 바꾼다.** 탈출은 막히는데 **더 나쁜 일이 남았다**:

```
file_id = "../F0TEST0001"
→ 바이트는 F0TEST0001 에서 읽히고
→ 결과 ImageInput.file_id 는 "../F0TEST0001"
→ 통과
```

좌표와 바이트 출처가 어긋난 채 성공한다. 나중에 그 출처를 누르면 **없는 자리**가
열리고, 파생 색인은 존재하지 않는 파일을 가리킨다.

**중화에 기대지 않기로 했다.** `_coordinate_code` 가 workspace·channel_id·
file_id 를 보고 **`_safe_component` 를 거쳐 값이 바뀌면 거절**한다
(`unsafe_coordinate`). 실제 Slack ID 와 workspace 이름은 그냥 지난다.

이 검사가 제일 세다 — 되돌리면 시험 **24건**이 빨개진다.

## 설계에서 설명이 필요한 자리

### 경로를 베끼지 않고 그 함수를 부른다

`files.attachment_storage(...)` 를 불러 objects 자리를 얻는다. 규칙을 여기 베껴
적으면 수집 쪽이 바뀐 날 **둘이 갈라지고**, 갈라진 쪽은 아무도 안 본다.

만든 경로조차 그대로 열지 않는다. `resolve(strict=True)` 로 링크를 다 푼 뒤
**그 결과가 여전히 뿌리 안인지** 확인한다.

### 링크는 안을 가리켜도 거절한다

밖을 가리키는 링크는 경로 검사가 잡는다. 그런데 **안을 가리키는 링크는 그
검사를 통과한다** — 확인한 순간과 여는 순간 사이에 대상이 바뀔 수 있다.
그래서 `is_symlink()` 를 따로 본다.

### 하나가 아니면 해시로 맞혀 고르지 않는다

맞혀 고르면 「맞는 것이 하나 있으면 통과」 가 되고, **같은 자리에 다른 파일을
끼워 넣는 길**이 열린다. 둘이면 거절하고 사람이 본다.

### MIME 이 메타데이터와 다르면 거절

실제가 JPEG 인데 메타데이터가 PNG 라면 **둘 중 무엇이 맞는지 여기서 알 수 없다.**
모르는 채로 보내지 않는다(`original_mime_mismatch`).

### `doc` 이 뿌리 밖에서 왔으면 거절

정본 문서 자체가 아카이브 밖에서 왔다면 그것이 가리키는 바이트를 믿을 이유가
없다. `source_path` 는 **경로를 얻는 데 쓰지 않고** 이 확인에만 쓴다.

## 시험 75건 · Ruff

개발 PC(Windows) 기준 `pytest tests/test_image_description_original.py` →
**75 passed**. `ruff check` 두 파일 **0건**.

실제 임시 디렉터리에 합성 바이트를 깔고 **경로 공격을 실제로 건다** — 가짜
파일시스템으로는 링크·탈출을 못 잰다. 링크를 못 만드는 환경에서는 그 두 건이
`skip` 된다.

### 돌연변이 검사

| 되돌린 것 | red |
|---|---|
| 좌표 검사 제거 | **24** |
| shadow 배치 무시 | 3 |
| 링크 체인 미검사 | 1 |
| 배치 되짚기 실패를 삼킴 | 1 |
| 확장자만 믿기 | 9 |
| 해시 재계산 제거 | 2 |
| 다중 원본 첫 번째 선택 | 2 |
| 뿌리 밖 검사 제거 | 2 |
| `human_upload_confirmed` 미확인 허용 | 2 |
| MIME 불일치 허용 | 1 |
| symlink 허용 | 1 |
| stat 크기 미검사 | 1 |
| 실제 읽은 길이 미검사 | 1 |

**처음엔 symlink·stat 두 건이 살아남았다.** 시험이 느슨해서였다 — symlink 시험이
`OUTSIDE_ROOT` 도 받아 주고 있었고, stat 상한은 읽기 상한에 가려져 있었다.
「뿌리 안을 가리키는 링크」 와 「상한 초과 파일은 열지도 않는다」 두 시험을
더해 둘 다 물리게 했다.

`doc is None` 분기 하나는 돌연변이에 안 물린다. `_coordinate_code` 가 빈 좌표를
먼저 막아 그 분기가 **중복 방어**이기 때문이다. 지우지 않았다 — 지우면
`getattr(None, …)` 이 유일한 방어가 되어 읽는 사람이 의도를 모른다.

## 오류 코드 14종

```
original_missing · original_ambiguous · original_outside_root
original_not_a_regular_file · original_symlink · original_unreadable
original_too_large · original_empty · original_hash_mismatch
original_not_an_image · original_mime_mismatch
provenance_mismatch · human_upload_unconfirmed · unsafe_coordinate
```

전부 ASCII. 경로·파일명·바이트·예외 본문을 담지 않는다.

## 두 배치 — 처음에 틀렸던 자리

첫 구현은 **옛 배치만** 가정했다. 확인해 보니 운영 수집기가 shadow 배치를 쓴다
(`archiving_bot` 이 `channel_root` 를 넘긴다).

```
옛:    <root>/workspaces/<ws>/channels/<id>/attachments/<file>/<rev>.md
       objects → <root>/../objects/...        ← 아카이브 뿌리의 **형제**
shadow: <root>/<ws>/<id>__<이름>/archive/attachments/<file>/<rev>.md
       objects → <channel_root>/objects/...   ← 뿌리 **안**
```

옛 배치만 가정하면 shadow 사진이 전부 「없음」 이 되고, 그 사실이 원장에는
**파일이 사라진 것**처럼 보인다.

`doc.source_path` 로 배치를 되짚는다(`ack_reconcile._storage_for` 와 같은 규칙).
그쪽은 private 이고 실패를 `None` 으로 삼키므로 여기서 다시 적되, **되짚기에
실패하면 거절한다** — 어느 자리인지 모르는 채 고르면 틀린 자리를 연다.

### 링크 체인

`shadow_paths.refuse_symlinked_chain` 을 재사용한다. 마지막 칸만 보면
`<workspace>` 가 남의 자리를 가리킬 때 통과한다.

**기준이 배치마다 다르다.** 옛 배치의 objects 는 뿌리의 형제라 뿌리를 기준으로
삼으면 `relative_to` 가 터진다 — 첫 수정에서 실제로 그랬고 시험 28건이
빨개졌다. 옛 배치는 `root.parent`, shadow 는 `root` 가 기준이다.

## 미검증 · 남은 것

- **호출부가 없다.** 선별기(`image_description_source`) → 이 로더 →
  작업 원장(`image_description_store.reserve`) 을 잇는 잡이 아직 없다.
  `already_indexed` 는 Codex 가 `store.indexed_sources()` 로 열어 뒀고,
  `human_file_ids` 는 `source.confirmed_human_file_ids()` 가 준다 — 둘 다
  준비됐고 **잇는 코드만 없다**
- 실제 아카이브 원본으로 안 돌려 봤다. 합성 바이트뿐이다
- **WebP 를 provider 가 실제로 받는지는 여전히 미확인.** 코어
  `ALLOWED_MIME` 에 있어 통과시키지만 이 저장소에서 확인한 적 없다
- TOCTOU 를 완전히 닫지는 못한다. 링크 거절과 크기 재확인으로 좁혔을 뿐,
  열고 읽는 사이의 교체는 **해시 대조가 마지막 방어**다
