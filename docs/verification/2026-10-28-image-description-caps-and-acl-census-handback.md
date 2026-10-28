# B-81 후속 둘 — 상한 대조 · 권한 분포 진단 (2026-10-28)

신규 세 파일만 만들었다. Codex 파일과 미커밋 변경은 건드리지 않았다.
`image_description_store.py` · 배포 역할 스크립트 · clearance 스키마도 그대로다.

| 파일 | 내용 |
|---|---|
| `tests/test_image_description_schema_caps.py` | 상한 대조 시험 5건 |
| `src/tybot/archive/image_description_acl_census.py` | 권한 분포 진단기 |
| `tests/test_image_description_acl_census.py` | 시험 46건 |

---

## 1. 상한 대조 — 숫자를 파싱해 상수와 비교한다

### 무엇이 문제였나

`tests/test_image_description_schema.py:45` 는 SQL 에 문자열
`char_length(description) <= 200` 이 있는지 본다. **상수가 바뀌어도 통과한다** —
리터럴 `200` 이 SQL 과 코어에 따로 적혀 있어 둘이 갈라져도 아무도 모른다.

실제로 묶는 대조는 `tests/test_image_description_store_db.py` 에 하나 있는데
`TEST_DATABASE_URL` 이 있어야 돌고, 실제 DB 검증을 패키지 밖에서 돌리면
`skip` 된다(2026-10-28 실측). **평상시에는 아무도 안 재고 있었다.**

### 어떻게 했나

```python
_CAP = re.compile(r"char_length\s*\(\s*description\s*\)\s*<=\s*(\d+)", re.IGNORECASE)
assert _caps()[0] == MAX_DESCRIPTION_CHARS
```

DB·DSN 없이 돈다. 리터럴을 양쪽에 반복하지 않는다 — **숫자는 SQL 에서만** 오고
비교 대상은 코어 상수다.

세 가지를 함께 건다.

| 시험 | 막는 것 |
|---|---|
| 상한이 **있는가** | CHECK 가 통째로 사라지면 「대조할 것이 없어 통과」 가 된다 |
| **한 군데뿐**인가 | 두 군데면 갈라질 수 있다 |
| 정규식이 **실제로 무는가** | 아무것도 못 잡으면 빈 목록끼리 비교해 조용히 지나간다 |

마지막 줄이 핵심이다. 대조 시험은 **자기가 작동하는지**를 같이 증명해야 한다 —
그러지 않으면 「초록인데 아무것도 안 잼」 이 또 생긴다.

대상 칸 이름(`description`)은 정규식에 고정했다. `label` 이나 다른 칸의 상한을
집어 오면 엉뚱한 숫자를 보증한다.

---

## 2. 권한 분포 진단기

### 왜 필요한가

전송 직전 판정은 빈 `acl` 을 거절한다 — 빈 값은 「공개」 가 아니라 **모름**이기
때문이다(원칙 3). 옳은 선택이지만 **대가를 재지 않았다.** ACL 이 빈 문서가
많다면 그 사진들은 영원히 후보로만 남고, 운영자는 「왜 하나도 안 나오나」 만
보게 된다.

### 계약

```python
take_census(docs) -> AclCensus      # .counts · .total · .image_total
                                    # .blocked_by_empty_acl · .rows
```

순수 읽기 전용이다. 원본 바이트를 열지 않고 네트워크·DB 를 부르지 않는다.
예외를 내지 않는다 — **진단이 한 줄 때문에 멈추면 나머지 분포를 못 본다.**

### 칸 7개 — 하나의 문서는 정확히 한 칸

| 칸 | 뜻 |
|---|---|
| `not_an_image` | 선별기가 안 받는 filetype |
| `unknown_visibility` | 빈 값·모르는 값. **「공개」 로 내려 읽지 않는다** |
| `public_empty_acl` · `public_with_acl` | |
| `private_empty_acl` · `private_with_acl` | |
| `unreadable_acl` | `acl` 이 집합이 아닌 경우 — **최소 집계를 넘는 칸** |

`unreadable_acl` 을 따로 둔 이유가 설계의 핵심이다. 문자열이 `acl` 로 들어오면
파이썬은 그것을 순회해 **글자 하나하나를 멤버로** 만든다. 「ACL 이 있다」 로
세면 **없는 권한을 있다고 보고**하고, 「비어 있다」 로 세면 **고치면 열리는 수를
부풀린다.** 둘 다 운영 판단을 틀리게 한다.

`blocked_by_empty_acl` 이 이 모듈을 만든 이유다 — **ACL 만 채우면 열리는 수.**
`unknown_visibility` 와 `unreadable_acl` 은 여기 안 센다. 그것들은 ACL 을 채워서
열리지 않는다.

### 결과에 좌표를 담지 않는다

돌려주는 것은 칸 이름과 개수뿐이다. 워크스페이스·채널·파일 ID 도 없다.

편의가 아니다. 「어느 채널이 비어 있나」 를 알려면 채널 멤버십을 아는 사람이
봐야 하는데, 집계는 그 권한을 거치지 않고 만들어진다. 집계에 좌표를 붙이는
순간 **권한 없는 사람이 집계로 채널 구성을 안다**(원칙 3).

고칠 자리를 찾는 것은 복구 도구(`attachment_repair`)의 몫이고, 그쪽은 권한을
지나서 돈다.

담지 않는 것으로는 부족해서, 시험이 **모듈 소스에 `doc.text`·`doc.name`·
`source_path`·`permalink`·`channel`·`workspace`·`file_id` 가 없는 것까지** 센다.
읽지 않으면 샐 수 없다.

### 「이미지인가」 는 베끼지 않는다

`image_description_source.FILETYPE_MIME` 을 그대로 쓴다. 규칙을 여기 베껴 적으면
선별 쪽이 바뀐 날 둘이 갈라지고, **갈라진 집계는 없는 문제를 가리킨다.**
시험이 선별기가 받는 모든 filetype 을 돌며 확인한다.

### 빈 목록도 칸을 0 으로 채운다

「0 건」 과 「안 세어 봤다」 가 같아 보이면 안 된다. 돌려주는 `counts` 는 언제나
칸 7개를 전부 들고 있다.

---

## 시험 51건 · Ruff

두 파일 합쳐 **51 passed**(개발 PC). `ruff check src tests scripts` **0건**.
전체 스위트도 돌렸다(아래 「확인」).

### 돌연변이 검사 — 생존 0

| 되돌린 것 | red |
|---|---|
| 칸 0 초기화 제거 | **44** |
| 이미지 판정 제거 | 8 |
| `unreadable` 을 `empty` 로 흡수 | 7 |
| 문자열 acl 을 집합으로 | 7 |
| 빈 visibility 를 public 으로 | 5 |
| public/private 뒤바뀜 | 4 |
| 확장자 표기 미정리 | 3 |
| visibility 표기 미정리 | 3 |
| SQL 상한 제거 | 3 |
| `blocked` 가 `unknown` 까지 셈 | 1 |
| `rows` 를 개수 순 정렬 | 1 |
| `image_total` 이 전체 | 1 |
| 결과에 좌표를 담음 | 1 |
| SQL 상한만 바꿈 | 1 |
| 상한을 두 군데 적음 | 1 |
| 코어 상수만 바꿈 | 1 |

마지막 세 줄이 1번 과제의 답이다 — **SQL 만 바꿔도, 코어만 바꿔도 빨개진다.**
전에는 둘 중 어느 쪽을 바꿔도 조용했다.

> 돌연변이 실행이 `deploy/sql/archiver_image_description_schema.sql` 과
> `image_description.py` 의 **줄바꿈을 LF 로 바꿔 놓았다**(내용 차이는 없었고
> `git diff --stat` 도 비어 있었다). 인덱스에서 그대로 되돌렸고, 두 파일은
> Codex 가 올린 내용 그대로다.

## 미검증 · 남은 것

- **진단기를 실제 아카이브에 안 돌려 봤다.** 숫자를 내려면 `AttachmentDoc` 을
  읽어 오는 호출부가 필요한데, 그 자리는 권한을 지나는 쪽에 있어야 한다
- **콘솔 화면이 없다.** 운영 기능은 콘솔에 둔다(CLAUDE.md) — 집계는 운영자가
  보려고 만든 것이므로 CLI 만으로는 「쓸 수 있는 사람이 한 명」 이 된다
- `attachment_reader` 는 `visibility` 어휘를 `("public","private")` 로 검증한다.
  **clearance 는 그 어휘를 보지 않는다** — 승인 당시와 같기만 하면 통과한다.
  이 집계기는 어휘 밖 값을 `unknown_visibility` 로 센다. 세 곳의 기준이 조금씩
  다르다는 뜻이고, 맞출지 말지는 결정이 필요하다
