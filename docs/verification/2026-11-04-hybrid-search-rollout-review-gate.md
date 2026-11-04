# B-12 hybrid search 운영 검토 게이트

작성: 2026-11-04 · Codex

## 목적

`hybrid_search_rollout.assess_rollout()`은 shadow 측정과 선택한 RRF 가중치의 집계를
운영자가 정한 기준과 대조한다. 결과가 `review_ready`여도 실제 답변 경로를 바꾸거나
벡터 검색을 활성화하지 않는다. 사람이 운영 표본을 검토할 최소 조건만 만족했다는 뜻이다.

## 명시해야 하는 기준

- 전체 비교 표본과 정답 근거가 있는 표본의 최소 수
- 평균 recall@k 개선 하한
- 벡터 조회 불가율 상한
- 평균 벡터 지연과 호출 비용 상한
- 표본당 밀려난 키워드 후보 수 상한

기본 임계값은 없다. 값이 없거나 비용·지연을 모르면 통과시키지 않는다. 질문·답변·본문·
workspace/channel/path 좌표는 입력 집계와 결과 객체에 넣지 않는다.

## 고정 차단 코드

`insufficient_comparisons`, `insufficient_labeled_comparisons`,
`recall_unverified`, `recall_gain_below_policy`,
`vector_unavailable_rate_above_policy`, `vector_latency_unknown`,
`vector_latency_above_policy`, `embedding_cost_unknown`,
`embedding_cost_above_policy`, `keyword_displacement_above_policy`.

## 검증

```text
pytest tests/test_hybrid_search_rollout.py tests/test_hybrid_search_tuning.py
       tests/test_hybrid_search_eval.py tests/test_hybrid_search_rank.py
       tests/test_hybrid_search_shadow.py
65 passed

ruff check src/tybot/hybrid_search_rollout.py tests/test_hybrid_search_rollout.py
All checks passed
```
