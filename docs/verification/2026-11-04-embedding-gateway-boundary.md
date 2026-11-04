# B-12 임베딩 게이트웨이 호출 경계

작성 2026-11-04 · 신규 3 + 시험 2. Codex 소유 파일(`search_embedding_schema.sql` ·
`embedding_index_policy.py` · `embedding_index_job.py` 와 각 시험,
`test_search_embedding_schema.py`)은 **안 건드렸다.** 커밋하지 않았다.

## 무엇이 없었나

게이트웨이에는 동사가 **하나**뿐이었다 — `complete`. 임베딩은 다른 일이다.
짧은 본문을 한 번에 여럿 보내고, 산문이 아니라 **고정 폭 벡터**를 받고,
샘플링도 도구도 stop reason 도 없다.

그래서 `complete` 를 돌려쓰는 길이 열려 있었다. 그게 **가장 쉽고 가장 나쁜
쪽**이다 — 채팅 모델에게 산문으로 「임베딩을 돌려줘」 하면 **숫자처럼 생긴
글**이 온다. 그 글은 진짜 벡터와 구별되지 않는다. 그대로 저장되면 근거 좌표가
되고, 검색 순위를 매긴다. 오류는 끝까지 안 난다.

## 새 파일 셋

| 파일 | |
|---|---|
| `src/tybot/gateway/embedding.py` | 좁은 `EmbeddingProvider` 프로토콜 · `EmbeddingModel` · `ProviderEmbeddings` · `ordered_vectors` · `EmbeddingUnsupported` |
| `src/tybot/gateway/providers/openai_embeddings.py` | 실제 어댑터 하나. SDK 는 **호출 안에서** lazy import |
| `src/tybot/embedding_gateway.py` | `embedding_index_job.Embedder` 구현체 `GatewayEmbedder` |

시험: `tests/test_embedding_gateway.py`(73) · `tests/test_gateway_openai_embeddings.py`(19).

## 경계가 지키는 것

### 동사 하나. 대체 없음

`EmbeddingProvider` 에는 `embed` 뿐이다. 샘플링·도구·스트리밍 없다.
임베딩 API 가 없는 프로바이더는 `EmbeddingUnsupported` 로 **멈춘다** —
`complete` 로 우회하지 않는다.

`build_embedding_providers()` 에 **anthropic 이 없다.** 완성(completion)을 파는
곳이지 임베딩을 파는 곳이 아니기 때문이고, 그 한 줄을 넣는 순간 위의 재앙이
들어온다. 시험이 그 부재를 붙들고 있다.

### 아무것도 스스로 켜지 않는다

`GatewayEmbedder` 는 프로바이더 지도를 **주입받는다.** 탐색도 기본값도 없다.
`providers/` 에 파일을 하나 더 놓는 것만으로는 아카이브 글이 어디로도 안 나간다.

어떤 모델을 쓸지도 **안 고른다.** provider·model·dimensions 는 전부
`search_embedding_model_revision` 에서 읽는다(`db_bindings`). 그 행이
`state='active'` 가 아니거나 revision 이 안 맞으면 `embedding_model_unsupported`
로 **부르기 전에** 거절한다.

### 한 번 부른다

재시도도 폴백도 없다.

- **재시도**는 절반쯤 성공했을지 모르는 호출에 **청구를 두 배로** 한다
- **폴백**은 매니페스트가 적을 모델과 **다른 모델의 벡터**를 넣는다. 한 색인 안에
  두 판이 섞이는데, 표에는 그걸 드러내는 칸이 없다

복구는 바깥이다 — 다음 실행이 정본을 다시 열고 다시 자르고 다시 임베딩한다.

`timeout` 은 **SDK 에 실제로 건다.** 재는 것만으로는 느린 호출 하나가 바깥 예산을
통째로 먹고, 취소할 수 없는 스레드만 남는다. 남은 예산이 0 이면 **부르지 않는다** —
아무도 안 기다리는 결과에 돈을 쓰는 일이다.

### 답을 질문과 대조한다

| 보는 것 | 안 맞으면 |
|---|---|
| provider · model 이 요청과 같은가 | `embedding_identity_mismatch` |
| 벡터 개수가 입력 개수와 같은가 | `embedding_result_incomplete` |
| 각 벡터의 차원이 레지스트리 값과 같은가 | `embedding_dimension_mismatch` |
| 값이 전부 유한한 수인가(bool·문자열 배제) | `embedding_vector_invalid` |
| 응답 index 가 정확히 `0..n-1` 인가 | `embedding_result_incomplete` |

마지막 줄이 조용한 쪽이다. 프로바이더는 행을 **아무 순서로나** 돌려줄 수 있고
각 행에 입력 index 를 붙여 준다. 정렬만으로는 부족하다 — **한 index 가 중복되고
다른 하나가 빠진 응답**은 개수가 맞은 채로 **짝만 어긋난다.** 그 뒤의 모든 청크가
다른 글의 벡터를 달고, 좌표도 차원도 멀쩡해서 아래쪽에서는 아무도 못 본다.
그래서 index 집합이 정확히 `range(n)` 이어야 한다(`ordered_vectors`).

보내기 전에도 본다 — 각 입력의 `text_sha256` 을 **다시 계산해 대조**한다. 그
지문이 **나가는 본문과 저장될 청크 좌표를 잇는 유일한 끈**이고, 둘이 어긋나면
그 문서의 모든 출처가 **임베딩된 적 없는 글**을 가리킨다.

### 비용은 모르면 `None`

`0.0` 은 **측정값**이다. 실행 합계에 더해지고 「이 실행은 공짜였다」 로 읽힌다 —
외부 프로바이더로 나간 배치에 대해 믿어서는 안 되는 바로 그것이다.

- 프로바이더가 usage 를 안 주면 `None`
- 단가를 모르면 `None`(레지스트리에 단가 칸이 없다. 주입받고, 기본은 비어 있다)
- usage 값이 음수·bool·문자열이면 `None`

### 본문은 어디에도 안 남는다

- 모든 거절은 `EMBEDDING_FAILURE_CODES` 의 **고정 낱말 하나**다. 요청에 따라
  변하지 않으므로 그대로 카운터와 화면에 쓸 수 있다
- `ProviderEmbeddings.vectors` 는 `repr=False`. 벡터는 본문에서 나온 값이고,
  수천 개가 로그 한 줄에 찍히면 유출이면서 동시에 못 읽는 로그다
- 프로바이더 예외는 **`from None` 으로 끊는다.** 임베딩 엔드포인트의 4xx 본문은
  거절한 입력을 **되돌려 주는 일이 있고**, 그 입력이 아카이브 원문이다. 연결된
  traceback 은 그 예외를 찍는 모든 로그로 그걸 나른다
- 타임아웃 메시지도 고정 문구다(`"embedding call timed out"`)

## 시험

92건. **돌연변이 15종 전부 잡힘**(생존 0) — 모르는 provider 를 아무거나 고르기,
실패 시 한 번 더 부르기, identity·개수·차원 대조 빼기, 비용 0 으로 만들기,
draft 모델 부르기, 지문 재확인 빼기, 오류 원인 그대로 매달기, timeout 을 일반
실패로 뭉개기, index 무시하고 받은 순서대로, 벡터를 repr 에 노출, 예산 0 에도
호출.

SDK 는 **가짜 모듈**을 `sys.modules` 에 넣어 돌린다. lazy import 는 `ast` 로
확인한다 — 최상위에 `import openai` 가 없고, 함수 안에만 있다.

## 아직 안 한 것 · 알아 둘 것

1. **실제 프로바이더/모델을 고르거나 켜지 않았다.** 레지스트리에 행을 넣고
   워크스페이스 정책이 그 revision 을 승인해야 비로소 닿는다
2. **실제 OpenAI 호출로 검증한 적 없다.** 어댑터는 가짜 SDK 로만 돌았다.
   첫 실호출 때 `embeddings.create` 응답 모양을 한 번 확인할 것
3. **고정 오류 코드는 batch 결과까지 보존한다.** `EmbeddingFailure`만 명시적으로
   받아 `embedding_call_timeout`·`embedding_identity_mismatch` 등을 별도 집계한다.
   등록되지 않은 예외는 기존처럼 `embedding_failed`로 닫혀 provider 본문이
   결과에 섞이지 않는다.
4. **단가는 모델 revision 행이 정본이다.** `input_price_per_mtok` 가 비어 있으면
   비용은 `None`이고, 명시된 `0`만 실제 무료 호출로 센다. 음수는 DB가 거절한다.
