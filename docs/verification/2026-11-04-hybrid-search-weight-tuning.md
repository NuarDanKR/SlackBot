# B-12 shadow RRF 가중치 비교 경계

작성: 2026-11-04 · Codex

## 범위

`tybot.hybrid_search_tuning.evaluate_weights()`는 같은 ACL·원문 snapshot에서 얻은
키워드/벡터 후보를 여러 RRF 가중치로 다시 정렬한다. 결과에는 아래 집계만 남는다.

- 표본 수와 정답 근거가 있는 표본 수
- 키워드/혼합 recall@k와 차이
- 상위 k에 새로 들어온 벡터 단독 후보 수
- 상위 k에서 밀려난 키워드 후보 수

질문, 답변, 본문, 경로, workspace/channel 좌표는 결과 객체에 없다. 입력의
`SearchHit`은 계산 중에만 들고 있고 `repr`에서도 제외한다.

## 의도적으로 하지 않는 것

- 가장 높은 recall만 보고 가중치를 자동 선택하지 않는다.
- 실제 답변 순서를 바꾸지 않는다.
- 정답 근거가 없는 표본에 recall 0을 붙이지 않는다.
- ACL 또는 원문 revision이 다른 두 결과를 비교하지 않는다.
- 지연과 비용은 가중치로 달라지는 값이 아니므로 이 모듈에서 다시 만들지 않는다.
  실제 호출의 지연·비용은 `hybrid_search_shadow` 측정값을 별도로 본다.

운영 전환은 표본 수, recall 변화, 벡터 단독 후보와 키워드 후보 교체량, 지연, 비용을
함께 본 뒤 사람이 결정한다. 이 코어가 있다는 사실은 B-12 검색 활성화를 뜻하지 않는다.

## 검증

```text
pytest tests/test_hybrid_search_tuning.py tests/test_hybrid_search_rank.py
       tests/test_hybrid_search_eval.py tests/test_hybrid_search_shadow.py
58 passed

ruff check src/tybot/hybrid_search_tuning.py tests/test_hybrid_search_tuning.py
All checks passed
```
