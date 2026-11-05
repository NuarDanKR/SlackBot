# B-12 질의 임베딩 정책 경계 — 색인 승인과 분리

작성 2026-11-05 · 신규 3 + 기존 1줄 수정(`deploy/apply-schema.sh`). 커밋하지 않았다.
**프로바이더 호출 0 · 검색 연결 0 · 활성화된 워크스페이스 0.**

| 파일 | |
|---|---|
| `deploy/sql/search_embedding_query_schema.sql` | 신규 표 `search_embedding_query_policy` |
| `src/tybot/embedding_query_policy.py` | 정책 판정 |
| `tests/test_embedding_query_policy.py` | 정책 단위시험 |
| `tests/test_search_embedding_query_schema.py` | 스키마·드리프트 시험 |
| `deploy/apply-schema.sh` | FILES 목록에 한 줄 추가 (수정한 유일한 기존 파일) |

Codex 가 가진 `embedding_index_inventory.py` · `preview_embedding_index.py` ·
`search_embedding_schema.sql` · `embedding_index_policy.py` 는 **건드리지 않았다.**

## 왜 승인을 새로 만드나

색인이 외부로 보내는 것은 **아카이브에 이미 들어온 원문**이다. PII 규칙을 통과했고,
사람이 파일을 열어 볼 수 있고, 안 된다 싶으면 되돌릴 수 있다.

질의가 보내는 것은 **방금 사람이 Slack 에 친 문장**이다. 아무것도 통과하지 않았고,
대개 그 대화에서 제일 민감한 문장이다 — 사람은 **모르는 것**을 질문에 적는다.
「김 과장 연봉 얼마야」 는 아카이브에 없지만 질문에는 있다.

그래서 색인 승인을 질의 승인으로 재사용하지 않는다. 재사용하면 **색인을 켠 날
질문이 같이 나가기 시작하고, 그것을 보여 주는 행 변화가 어디에도 없다.**

## 어떻게 분리했나

표를 따로 만들었다. 같은 행에 열만 더하면 색인 승인 UPDATE 하나가 질의 승인까지
건드릴 수 있고, revision 하나를 둘이 나눠 쓰게 된다.

| | 색인 | 질의 |
|---|---|---|
| 표 | `search_embedding_workspace_policy` | `search_embedding_query_policy` |
| 스위치 | `enabled` | `allow_query` — **기본 false** |
| 외부 전송 | `external_transfer_approved` | `query_external_transfer_approved` — **기본 false** |
| 승인자·사유·revision | 각자 | 각자 |
| 판정 | `authorize_index_request` | `authorize_query_request` |
| 결정 타입 | `IndexAuthorization` | `QueryAuthorization` (다른 타입) |
| policy_revision | `workspace:<ws>:r<n>` | `query:<ws>:r<n>` |

열 이름을 `query_` 로 시작하게 둔 것은 취향이 아니다. 두 표에 같은 이름의 boolean
이 있으면 **엉뚱한 표를 읽어도 boolean 이 나오고**, 코드도 시험도 멀쩡해 보인다.

모델 쪽 `external_approved` 는 질의 경로에서도 **필요조건으로만** 쓴다 — 그 모델로
우리 글을 보내도 되는가는 용도와 무관하므로 둘 다 요구하는 쪽이 더 닫힌다.
**충분조건은 아니다.** 외부로 나가려면 `query_external_transfer_approved` 가 따로
true 여야 한다.

## 질문 본문은 판정에 들어가지 않는다

`authorize_query_request(connection, request)` 가 전부다. `QueryRequest` 는
`workspace` · `model_revision` · `transport` 셋뿐이고, 셋 다 식별자 정규식
(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)으로 막는다. 공백·줄바꿈·한글이 들어간
문장은 **어느 칸으로도 못 들어온다** — 호출부가 인자를 잘못 엮어도 ValueError 다.

본문을 받지 않는 이유는 「필요 없어서」 가 아니다. 받는 함수는 결국 **로그에 찍는다.**

`transport` 는 호출부가 **실제로 쓰려는** 경로다(모델 바인딩에서 온다). 정책 행과
다르면 `transport_mismatch` 로 거절한다 — 레지스트리는 외부 모델로 옮겼는데 정책
행은 아직 `local` 인 상태가 조용히 통과하지 않는다.

## 판정 결과 (고정 낱말)

| 코드 | 뜻 |
|---|---|
| `allowed` | 통과 |
| `unconfigured` | 워크스페이스 행이 없음 |
| `query_disabled` | `allow_query` 가 false |
| `model_mismatch` | 정책이 가리키는 revision 과 요청이 다름 |
| `model_inactive` | 모델 state 가 active 아님 |
| `transport_mismatch` | 쓰려는 경로와 정책 경로가 다름 |
| `query_external_not_approved` | 외부인데 승인 둘 중 하나라도 없음 |

`require_query_authorization(auth, model_revision=…, transport=…)` 가 호출 직전
게이트다. `QueryAuthorization` 이 아니면 **TypeError** — 색인 결정을 들고 오면
여기서 멈춘다. 통과한 결정도 모델·transport 를 한 번 더 대조한다.

## 안 한 것

1. **프로바이더 호출 없음.** 이 모듈은 `embedding_gateway` · `embedding_search` ·
   SDK 를 import 하지 않는다(시험으로 고정)
2. **검색 연결 없음.** `embedding_search.py` 는 그대로다. 게이트는 만들어 뒀지만
   아직 아무도 부르지 않는다
3. **활성화 없음.** 스키마에 INSERT·UPDATE 가 없다. 적용해도 켜지는 워크스페이스는
   0개다(시험으로 고정)
4. **콘솔 화면 없음 — CLI 도 없다.** CLAUDE.md 「운영 기능은 콘솔에 둔다」 를
   어기지 않으려고 **둘 다 안 만들었다.** 켜는 수단을 CLI 로 먼저 내면 「나중에
   콘솔에 붙인다」 가 되고, 그게 금지된 순서다. 켜려면 콘솔 화면이 먼저다 —
   워크스페이스·모델 revision·transport 를 고르고, 외부 전송은 사유를 적게 한다

## 시험

52건, DB·네트워크 없이 돈다(가짜 커서).

**돌연변이 7종 전부 잡힘**(생존 0):
`allow_query` 기본값 true, 외부 승인 둘을 AND 대신 OR, transport 대조 제거,
게이트의 타입 검사 제거, 요청 필드 검증 제거, 질의 대신 색인 표 읽기,
`allow_query` 검사 제거.

`python -m pytest tests/test_embedding_query_policy.py
tests/test_search_embedding_query_schema.py tests/test_embedding_index_policy.py
tests/test_search_embedding_schema.py -q` → **78 passed**.

## 서버에 적용할 때

표만 생긴다. 어떤 워크스페이스도 켜지지 않는다.

```bash
sudo /opt/tybot/deploy/apply-schema.sh
```
