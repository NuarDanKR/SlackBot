# PDF 이미지 조사 — 못 읽은 것을 0건으로 적지 않는다

Written for: Codex, 운영. 작성: Claude, 2026-10-22.
앞선 문서: [2026-10-21-nested-form-image-detection.md](2026-10-21-nested-form-image-detection.md)

**커밋하지 않았습니다.** `convert.py` · reader · 콘솔 · 정책·DB 는 열지
않았습니다. 바꾼 것은 `pdf_page_images.py` 와 그 시험 둘뿐입니다.

## 1. 삼키던 실패 셋

전 구현은 아래를 전부 `return 0.0` 으로 삼켰습니다.

| 상황 | 전 | 후 |
|---|---|---|
| `Do` 가 가리키는 이름이 `/XObject` 에 없다 | 0건 | `xobject_unreadable` |
| `/XObject` 자체를 못 연다 | 0건 | `xobject_unreadable` |
| Form 콘텐츠 스트림을 못 연다 | 0건 | `form_stream_unreadable` |
| `cm`·`/Matrix` 에 숫자가 아닌 값 | 무시하고 진행 | `matrix_unreadable` |
| `cm`·`/Matrix` 에 `inf`·`nan` | **조용히 0 처럼 행동** | `matrix_unreadable` |

`0건` 으로 적으면 그 쪽은 **「그림이 없는 쪽」** 으로 닫힙니다. 닫힌 쪽은
coverage 에서 `converted` 가 되고, **아무도 다시 보지 않습니다.**

`nan` 이 특히 나쁩니다. 모든 비교가 거짓이라 `max(nan, 0.0)` 도 `nan` 이고,
그 `nan` 이 넓이 합에 들어가면 기준 비교가 전부 거짓이 됩니다 — **그림이 큰 쪽이
그림 없는 쪽으로** 닫힙니다. 소리 없이.

### `Do` 가 있다는 것은 무언가를 그렸다는 뜻입니다

못 푼 이름을 0 으로 적는 것은 「거기 아무것도 없었다」 고 말하는 것입니다.
우리가 아는 것은 **「무언가를 그렸는데 그것이 무엇인지 모른다」** 뿐입니다.

반대로 **`Do` 가 아예 없는 쪽은 그대로 0건**입니다. 고친 쪽으로 넓히면 아무것도
안 그린 멀쩡한 쪽까지 실패가 됩니다(`test_a_page_with_no_draw_at_all_is_still_a_clean_zero`).

## 2. `MAX_OPERATIONS` 는 사전 파싱 비용을 **못 막습니다**

확인했습니다. pypdf 6.16.1 기준입니다.

```python
# pypdf/generic/_data_structures.py
@property
def operations(self):
    if not self._operations and self._data:
        self._parse_content_stream(BytesIO(self._data))   # ← 전체를 한 번에
        self._data = b""
    return self._operations
```

`ContentStream.__init__` 은 `set_data()` 만 합니다(디코드). **토큰화는
`.operations` 에 처음 닿을 때 일어나고, 스트림 전체를 한 번에 리스트로
만든 뒤** 돌려줍니다.

우리 `MAX_OPERATIONS` 는 **그 리스트를 걸어갈 때** 줄어듭니다. 즉 토큰화가
끝나기 전에는 **한 번도 안 불립니다.**

pypdf 의 디코드 상한은 `ZLIB_MAX_OUTPUT_LENGTH = 75,000,000` 입니다. 75MB
짜리 콘텐츠 스트림 하나가 Python 객체로 토큰화되는 동안 **변환 하나가 서버를
붙듭니다.** 연산 상한은 그걸 하나도 못 막습니다.

### 그래서 크기를 먼저 봅니다

```python
MAX_CONTENT_BYTES = 8 * 1024 * 1024        # 스트림 하나
MAX_PAGE_CONTENT_BYTES = 32 * 1024 * 1024  # 한 쪽의 합
```

`_decoded_size()` 로 **토큰화 전에** 디코드된 크기를 재고, 넘으면
`ContentStream` 을 **만들지도 않습니다.** 결과는 `content_too_large` —
「그림이 없다」 가 아니라 **미검사**입니다.

Form 이 여러 겹이면 하나하나는 작아도 합이 커지므로 쪽 단위 합도 봅니다.

> **남은 비용 하나.** `_decoded_size()` 가 `get_data()` 를 부르므로 **압축 해제
> 비용은 여전히 치릅니다.** pypdf 의 75MB 상한 안쪽이고, 토큰화(Python 객체
> 수백만 개)보다 훨씬 쌉니다. 압축 해제 전에 막으려면 `/Length`(압축 크기)로
> 거르는 수밖에 없는데, 그건 압축률에 따라 멀쩡한 문서를 자릅니다. **재지 않고
> 자르는 쪽보다 재고 자르는 쪽**을 골랐습니다(§5-3).

## 3. 이미 의심인 쪽은 결론이 바뀌지 않습니다

실패가 있어도 **그때까지 찾은 넓이가 이미 기준을 넘었으면 그대로 씁니다.**

못 본 부분은 넓이를 **늘리기만** 하지 줄이지 않습니다. 그러니 「이 쪽은
의심이다」 라는 결론은 더 봐도 안 바뀝니다. 여기서 실패로 뒤집으면
`image_heavy()` 가 `False` 가 되어 **덜 의심하게** 됩니다 — 방향이 반대입니다.

```python
if state.total < DEFAULT_IMAGE_AREA_RATIO and (state.exhausted or state.failures):
    code = state.failures[0] if state.failures else ERROR_BUDGET
    return PageImages(..., error=code)
```

## 4. API 는 그대로입니다

- `PageImages` 의 필드 **추가 없음**. `error` 에 새 코드가 들어갈 뿐입니다
- `inspect` · `inspect_page` · `image_heavy` 시그니처 **그대로**
- `convert.py` 는 `error` 가 비었는지만 보므로(`inspected`) **고칠 필요가
  없었습니다** — 새 실패도 기존 `image_scan_failed` 플래그로 받습니다

추가된 공개 이름(전부 상수): `ERROR_XOBJECT` · `ERROR_FORM_STREAM` ·
`ERROR_MATRIX` · `ERROR_TOO_LARGE` · `MAX_CONTENT_BYTES` ·
`MAX_PAGE_CONTENT_BYTES`.

## 5. 시험 결과

개발 PC(Windows)에서 돌렸습니다. 서버 절차가 아니라 결과 보고입니다.

| 무엇 | 결과 |
|---|---|
| `ruff check` (`src` · `tests` · `scripts`) | All checks passed |
| 전체 (`test_external_convert.py` 제외) | **5,412 passed · 43 skipped** (278s) |
| `tests/test_pdf_mixed_pages.py` 단독 | **56 passed** (신규 14) |

합성 PDF 만 씁니다. 빌더에 손잡이 둘을 더했습니다.

- `broken_filter` — **모르는 필터**(`/TyBotBrokenDecode`)를 적어 `get_data()` 를
  터뜨린다
- `matrix_raw` — `/Matrix` 배열을 **그대로** 적어 숫자 아닌 값을 넣는다

> 처음에는 깨진 `/FlateDecode` 로 만들려 했는데, **pypdf 가 조용히 복구해**
> 빈 데이터를 돌려줬습니다. 그 시험은 통과하면서 아무것도 안 지켰습니다.
> 모르는 필터로 바꿨고, 그 사실을 빌더 주석에 적어 뒀습니다.

### 주요 시험

| 시험 | 지키는 것 |
|---|---|
| `test_a_missing_xobject_name_is_an_inspection_failure` | 못 푼 `Do` 를 0건으로 적지 않는다 |
| `test_a_page_with_no_draw_at_all_is_still_a_clean_zero` | 안 그린 쪽까지 실패로 만들지 않는다 |
| `test_a_missing_name_beside_a_big_image_keeps_the_verdict` | 이미 의심이면 결론이 안 바뀐다(§3) |
| `test_an_unreadable_form_stream_is_an_inspection_failure` | Form 스트림 파싱 실패 |
| `test_an_unreadable_form_matrix_is_an_inspection_failure` | `/Matrix` 를 항등으로 가정하지 않는다 |
| `test_a_non_numeric_cm_operand_is_an_inspection_failure` | 쪽 콘텐츠의 `cm` 도 같다 |
| `test_a_non_finite_matrix_value_is_not_treated_as_zero` | `inf`·`nan` |
| `test_an_oversized_stream_is_not_parsed_at_all` | **`ContentStream` 을 만들지도 않는다** — 생성자를 세어 확인 |
| `test_a_normal_stream_is_still_parsed` | 상한을 넣었다고 멀쩡한 쪽을 미검사로 만들지 않는다 |
| `test_the_page_content_budget_adds_up_across_forms` | Form 합도 본다 |
| `test_an_oversized_stream_beside_a_big_image_keeps_the_verdict` | §3 |
| `test_failure_results_carry_no_document_text` | 실패 경로도 숫자·코드뿐(원칙 5) |
| `test_every_failure_code_is_distinct` | 코드가 겹치면 원인 둘이 한 줄로 합쳐진다 |

### 시험이 실제로 무는지 확인했습니다

다섯 자리를 각각 되돌려 돌렸습니다.

| 되돌린 것 | red |
|---|---|
| `/XObject` 실패 기록 | 1건 |
| Form 스트림 실패 기록 | 1건 |
| 행렬 유한성 검사 | 1건 |
| `cm` 실패 기록 | 2건 |
| Form `/Matrix` 실패 기록 | 1건 |
| 크기 상한 | 2건 |

## 6. 미검증

1. **실제 사내 PDF 로 돌리지 않았습니다.** 새 실패 코드가 실문서에서 얼마나
   자주 나오는지 모릅니다. 많이 나오면 그만큼 `coverage_state` 가 `partial` 로
   갑니다 — 의도이긴 하지만 규모는 미측정입니다
2. **8MB·32MB 가 실문서에서 넉넉한지 재지 않았습니다.** `content_too_large` 가
   자주 나오면 상한을 올리기 전에 **그 문서가 왜 그렇게 큰지**부터 봐야 합니다
3. **압축 해제 비용은 여전히 치릅니다**(§2). 압축 폭탄은 pypdf 의 75MB 상한에
   기대고 있습니다
4. **`_decoded_size()` 가 `get_data()` 를 두 번 부르게 합니다** — 한 번은 크기를
   재려고, 한 번은 `ContentStream` 안에서. pypdf 가 캐시하는지 확인하지
   않았습니다. 8MB 상한 안쪽이라 큰 비용은 아니지만 중복입니다
5. 클리핑(`W n`)·`/Pattern`·soft mask 는 여전히 안 봅니다(앞 문서 §6-3·§6-4)

## 7. 서버 확인

앞 문서 §7 의 명령에 실패 코드별 집계만 더하면 됩니다. 조회 전용이고
**쪽 번호와 코드만** 찍습니다.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python - <<'PY'
from collections import Counter
from pathlib import Path
from tybot import attachment_review
from tybot.archive import pdf_page_images as pi
root = Path("/var/lib/tybot/archive")
codes, docs = Counter(), 0
for item in attachment_review.scan(root):
    if item.filetype.lower() != "pdf" or not item.object_path:
        continue
    path = Path(item.object_path)
    if not path.is_file():
        continue
    docs += 1
    for got in pi.inspect(path.read_bytes()).values():
        if not got.inspected:
            codes[got.error] += 1
print(f"PDF {docs}건")
for code, count in codes.most_common():
    print(f"  {code}: {count}쪽")
PY
```

- `content_too_large` 가 많으면 §6-2
- `xobject_unreadable` 가 많으면 그 PDF 들이 어떤 도구로 만들어졌는지 봐야
  합니다. 특정 생성기의 버릇일 수 있습니다
- `matrix_unreadable` 는 드물어야 정상입니다. 많으면 제 파서가 못 읽는
  정상 표기가 있다는 뜻입니다

## 8. Codex 에게

- `convert.py` 는 **한 줄도 안 고쳤습니다.** 새 실패도 기존
  `image_scan_failed` 로 받습니다
- 실패 코드를 원장·목록에 **각각 세고 싶으면** `PageImages.error` 를 그대로
  쓰면 됩니다. 전부 짧은 고정 문자열이고 본문이 섞이지 않습니다
- §6-2 의 상한 조정은 실측 뒤 판단할 일입니다. 제 쪽에서 값을 올리지
  않았습니다 — 재기 전에 올리면 막으려던 것을 그대로 통과시킵니다
