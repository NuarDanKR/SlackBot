# B-12 질의 임베딩 실행 경계 — 섀도 전용

작성 2026-11-05 · 신규 2. **기존 파일 수정 0.** 커밋하지 않았다.

| 파일 | |
|---|---|
| `src/tybot/embedding_query_run.py` | 실행 경계 |
| `tests/test_embedding_query_run.py` | 39건 |

Codex 파일(`embedding_index_inventory.py` · `embedding_index_runner.py` ·
`embedding_index_runtime.py`) 은 건드리지 않았다.

## 사슬

```
QueryRequest → 정책 조회 → require_query_authorization
             → 임베딩 1회 → candidates(승인된 scope)
             → resolve_candidates(현재 권한) → evidence_for(정본 재대조)
             → ShadowQueryResult
```

B-12 조각들이 **처음으로 끝까지 이어진** 자리다. 그런데 답변 **옆에** 이었다.

## 왜 섀도인가

벡터 결과를 키워드 순위와 섞는 것은 별도 결정이고, 재현율·지연·비용을 **재고
나서** 해야 한다(B-12 완료 조건). 그 측정을 하려면 경로가 돌아야 하는데, 돌리면서
답변을 바꾸면 측정 대상이 사라진다.

그래서 결과 객체에 **섞을 것 자체를 안 뒀다.**

- `SearchHit` 없음 · `score` 없음 · 본문 없음
- 들고 나오는 것은 **좌표**(`EvidenceRef`)와 **수**뿐
- 시험이 `src/` 전체를 훑어 **이 모듈을 부르는 제품 코드가 없음**을 고정한다

## 정책이 통과하기 전에는 질문이 안 나간다

정책을 먼저 읽는다. **읽지 못하면 거절이다** — 못 읽은 승인은 승인이 아니다
(`db_unavailable` / `policy_unreadable`, 호출 0회).

요청이 모양부터 안 맞아도 보내지 않는다. `scope.workspace` 가 정책을 읽을
workspace 와 다르면 `scope_mismatch` 로 멈춘다 — 한 워크스페이스를 **다른
워크스페이스의 승인으로** 뒤지는 일이 없어야 한다(원칙 3·4).

## 호출은 정확히 한 번

주입된 임베더를 `_Once` 로 감싼다. 「재시도 안 함」 은 리뷰는 통과하고 **다음
수정에서 죽는** 종류의 규칙이라, 약속이 아니라 **구조**로 둔다. 두 번째 호출은
`query_embedder_called_twice` 로 터진다.

질문 재시도는 청구만 두 배가 아니다. **그 문장의 사본이 남의 로그에 하나 더
생긴다.** 아무도 기다리지 않는 섀도 실행에서.

provider·model·transport 는 **호출자가 준다.** 이 모듈은 레지스트리를 뒤져
모델을 고르지 않고, 프로바이더를 import 하지 않는다. 폴백도 없다.

## 본문·벡터·오류 본문

| | |
|---|---|
| 질문 | `EmbeddingInput` 하나 만드는 데만 쓰고 버린다. 결과·로그·`repr` 어디에도 없다 |
| 벡터 | `candidates()` 에 넘기고 `del`. 결과에 칸이 없다 |
| 프로바이더 오류 | 예외 타입 이름만 로그에 남는다. 임베딩 4xx 본문은 **거절한 입력을 되돌려 준다** — 그 입력이 질문이다 |

`detail` 은 고정 낱말 집합(`DETAIL_WORDS`)으로 검증한다. 집합 밖 값이 들어오면
`ValueError` — 프로바이더 메시지나 경로가 그 칸으로 새는 길을 막는다.

## 권한은 벡터 조회 **뒤에** 다시 본다

색인은 **어제 권한으로 쓴 재생성 가능한 캐시**다. 그래서 후보가 나온 뒤:

1. `resolve_candidates(open_visible=sources.open)` — 지금 권한으로 다시 열고
   `source_revision` · 구간 · 청크 해시를 대조
2. `evidence_for(..., sources, root)` — **같은 창구**가 들고 있는 정본과 한 번 더

창구(`VisibleSources`)는 호출자가 만든 **그 요청의 것**을 받는다. 여기서 ACL 을
다시 계산하지 않는다 — 판정이 두 곳으로 갈라지면 갈려도 오류가 안 난다.

## 결과 코드

| outcome | 뜻 | 호출 |
|---|---|---|
| `ok` | 좌표가 나왔다 | 1 |
| `policy_denied` | 정책·요청이 막았다 (detail = 판정 낱말) | **0** |
| `embedding_failed` | 임베딩이 실패했다 (detail = 고정 실패 코드) | 1 |
| `db_unavailable` | 정책 또는 벡터 색인을 못 읽었다 | 0 또는 1 |
| `no_candidates` | 후보가 없거나, 지금 권한으로 안 보인다 | 1 |
| `snapshot_changed` | 원문이 색인 뒤에 바뀌었다 | 1 |

**「안 보인다」 를 따로 구분하지 않는 것은 일부러다.** 구분해 주면 벡터 캐시가
「그 문서가 있느냐」 를 묻는 창구가 된다 — resolver 가 `source_not_visible` 로
문서 없음과 권한 없음을 합쳐 두는 이유와 같다. 반대로 `snapshot_changed` 는
**볼 수 있는 문서가 바뀐** 경우라 알려 줘도 새지 않는다.

## 시험

39건, DB·네트워크·프로바이더 없이 돈다. 아카이브는 합성 MD 를 `tmp_path` 에
만들어 **실제 `ArchiveStore` · `VisibleSources` · resolver · `evidence_for`** 를
그대로 통과시킨다. 가짜는 DB 커서와 임베더 둘뿐이다.

**돌연변이 11종 전부 잡힘**(생존 0): 실패 시 재시도, 정책 전에 임베딩,
snapshot 을 no_candidates 로 접기, `source_not_visible` 을 snapshot 으로 올리기,
벡터 여러 개 허용, scope 에 채널 추가, 빈 질문 허용, 질문을 결과 칸에 넣기,
프로바이더 본문을 로그에 찍기, 벡터·질문을 로그에 찍기, `detail` 검증 제거.

전체 스위트 **7172 passed · 152 skipped · 0 failed**, ruff 0건.

## 아직 안 한 것

1. **아무도 안 부른다.** 답변 경로에 물리는 것은 다음 결정이다. 물릴 때는
   「섀도로 N건 돌려 재현율·지연·비용을 재고」 가 먼저다
2. **섀도 결과를 어디에도 적지 않는다.** 로그 한 줄이 전부다. 집계가 필요하면
   표를 따로 만들고, 질문·좌표를 쌓는 것이 맞는지부터 결정해야 한다
3. **콘솔 화면 없음.** 켜는 스위치는 `search_embedding_query_policy` 고,
   그 표를 만지는 화면은 아직 없다(2026-11-05 질의 정책 인계 문서 §안 한 것)
