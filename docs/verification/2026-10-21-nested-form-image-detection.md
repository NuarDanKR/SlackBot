# Form XObject 안에 중첩된 이미지 탐지 (B-80 후속)

Written for: Codex, 운영. 작성: Claude, 2026-10-21.
앞선 문서: [2026-10-21-mixed-pdf-image-coverage.md](2026-10-21-mixed-pdf-image-coverage.md)

**커밋하지 않았습니다.** `convert.py` · `attachment_reader.py` · 변환기 판 ·
콘솔은 열지 않았습니다. 바꾼 것은 둘뿐입니다.

## 1. 전 구현이 틀렸던 두 가지

전에는 쪽의 `/XObject` **목록을 훑고**, 이름마다 쪽 콘텐츠에서 `Do` 를 찾았습니다.

### (가) 안 그려지는 리소스를 셌습니다

`/XObject` 에 있다는 것은 **쓸 수 있다**는 뜻이지 이 쪽에 그렸다는 뜻이
아닙니다. 템플릿에서 물려받은 미사용 이미지는 `Do` 가 없어 넓이를 못 구하는데,
그때 **픽셀 크기로 어림하는 폴백**이 돌았습니다. 아무것도 안 그린 쪽이
의심으로 올라갔습니다.

### (나) Form XObject 안의 이미지를 못 봤습니다

한글·워드가 내보낸 PDF 는 표·도장을 Form 으로 한 겹 싸는 일이 흔합니다.

```
쪽 콘텐츠:  q 500 0 0 400 20 20 cm /Fm1 Do Q     ← 여기엔 이미지가 없다
  /Fm1:     /Im1 Do                              ← 표 이미지는 여기 있다
```

쪽의 `/XObject` 에는 `/Fm1`(Form)만 있습니다. 이미지가 **없는 쪽**으로 보였고,
스캔해 붙인 표가 그대로 묻혔습니다. **이번 작업이 고치려던 바로 그 쪽입니다.**

## 2. 고친 방향 — 그리기 연산을 따라갑니다

콘텐츠 스트림을 **한 번 걸어 내려가며** `Do` 를 만날 때만 셉니다.

| 만난 것 | 하는 일 |
|---|---|
| `q` / `Q` | 현재 변환 행렬(CTM)을 밀고 꺼낸다 |
| `cm` | `새 행렬 × CTM` 으로 갱신 |
| `Do` → Image | `\|a·d − b·c\|` ÷ 쪽 넓이를 더한다 |
| `Do` → Form | `/Matrix × CTM` 으로 **그 안으로 들어간다** |

Form 의 `/Resources` 가 있으면 그것을, 없으면 물려받은 것을 씁니다.

**픽셀 폴백을 없앴습니다.** 그려지지 않은 것은 넓이가 0 입니다. 추측으로 채우던
자리가 사라졌습니다.

### `images` 의 뜻이 바뀌었습니다

전: 쪽 리소스에 있는 이미지 XObject 수.
후: **이 쪽에 실제로 그려진** 서로 다른 이미지 수.

같은 이미지를 두 번 그리면 **넓이는 두 번 더하고 장수는 하나**입니다. 장수와
넓이는 다른 질문입니다.

> 기존 시험 `test_an_unused_image_resource_is_not_a_drawn_image` 가 미사용
> 리소스에 `images == 1` 을 기대하고 있었습니다. 「미사용 리소스는 세지 않는다」
> 와 맞지 않아 **`images == 0` 으로 고쳤습니다.** 그 시험이 함께 보던
> `area_ratio == 0` · `image_heavy False` 는 그대로입니다.

### `/BBox` 로 과대평가를 깎습니다

Form 서브트리는 `/BBox` 밖으로 나갈 수 없습니다. 안쪽에서 쪽 전체를 그리는
`cm` 을 써도 실제로 덮는 자리는 BBox 안입니다. 서브트리가 더한 값이 변환된
BBox 넓이를 넘으면 **BBox 값으로 깎습니다.**

클리핑(`W n`)을 제대로 계산하지는 않습니다. 그건 여전히 과대평가 방향으로
남아 있고, 그 방향이 **놓치는 쪽보다 쌉니다.**

## 3. 안전하게 멈춥니다

사용자가 올린 PDF 입니다. 세 가지로 묶었습니다.

| 묶는 것 | 어떻게 | 상수 |
|---|---|---|
| 순환 참조 | 지금 내려온 **경로**의 Form 객체 번호를 들고 다닌다. 다시 나오면 안 들어간다 | — |
| 깊이 | 넘으면 그 가지를 더 안 판다 | `MAX_FORM_DEPTH = 12` |
| 전체 분량 | 연산 수가 넘으면 멈춘다 | `MAX_OPERATIONS = 200_000` |

경로 기반 집합이라 **형제 가지에서 같은 Form 을 여러 번 쓰는 정상 PDF 는
막지 않습니다.** 자기 조상만 막습니다.

### 멈춘 것을 「다 봤다」 로 적지 않습니다

상한에 걸렸을 때 두 갈래입니다.

- 그때까지 찾은 넓이가 **이미 기준을 넘었다** → 더 봐도 줄지 않으므로 그대로 씁니다
- **기준에 못 미친다** → `error = "traversal_budget"` 입니다. 더 봤으면 넘었을
  수도 있고, **모르는 것을 「괜찮다」 로 적지 않습니다**

`inspected is False` 가 되므로 `image_heavy()` 는 `False` 이고, `convert.py` 가
`image_scan_failed` 플래그로 받아 문서를 `partial` 로 만듭니다 — 기존 배선
그대로이고 `convert.py` 를 고치지 않았습니다.

`inspect()` 도 쪽마다 감쌌습니다. 한 쪽이 터져도 나머지를 세고, **터진 쪽을
목록에서 빼지 않습니다** — 빼면 「이미지 없음」 으로 읽힙니다.

## 4. 변경 파일

| 파일 | 내용 |
|---|---|
| `src/tybot/archive/pdf_page_images.py` | 그리기 연산 추적으로 재작성 (+238 −85) |
| `tests/test_pdf_mixed_pages.py` | **신규 15건** 추가 · 기존 2건 계약 갱신 |

### 계약 변경

- `PageImages.max_depth: int = 0` 추가 — Form 을 몇 겹까지 들어갔나. 숫자뿐이라
  본문이 섞이지 않습니다. `test_the_inspection_carries_no_image_content` 의 필드
  목록을 함께 고쳤습니다
- `PageImages.images` 의 뜻이 **그려진 이미지 수**로 바뀌었습니다(§2)
- `ERROR_BUDGET` · `MAX_FORM_DEPTH` · `MAX_OPERATIONS` 공개
- `PageImages`·`image_heavy`·`inspect` 의 시그니처는 **그대로**입니다 —
  `convert.py` 는 고칠 필요가 없었습니다

## 5. 시험 결과

개발 PC(Windows)에서 돌렸습니다. 서버 절차가 아니라 결과 보고입니다.

| 무엇 | 결과 |
|---|---|
| `ruff check` (`src` · `tests` · `scripts`) | All checks passed |
| 전체 (`test_external_convert.py` 제외) | **5,394 passed · 43 skipped** (225s) |
| `tests/test_pdf_mixed_pages.py` 단독 | **42 passed** |

합성 PDF 만 씁니다. Form 중첩·순환을 직접 조립하는 빌더(`_nested_pdf`)를 더했고,
객체 번호를 **먼저 잡고** 본문을 씁니다 — Form 끼리 서로를 가리켜야 하기 때문입니다.
모델도 OCR 도 네트워크도 부르지 않습니다.

| 시험 | 지키는 것 |
|---|---|
| `test_an_image_inside_a_form_is_found` | **이번에 고친 그 자리.** 쪽엔 `/Fm1 Do` 만 있다 |
| `test_images_two_forms_deep_are_found` | Form 안의 Form. 한 겹만 보면 또 놓친다 |
| `test_the_form_matrix_scales_the_nested_image` | `/Matrix` 를 안 곱하면 넓이가 틀린다(1/10 → 1/100) |
| `test_a_logo_inside_a_form_still_does_not_make_the_page_suspect` | 중첩을 본다고 작은 그림까지 의심하면 신호가 소음이 된다 |
| `test_an_unused_resource_inside_a_form_is_not_counted` | Form 안에서도 **그린 것만** |
| `test_the_same_image_drawn_twice_counts_once_but_adds_area` | 장수와 넓이는 다른 질문 |
| `test_a_self_referencing_form_does_not_hang` | `max_depth == 1` — **깊이 상한이 아니라 순환 방어가 멈췄다** |
| `test_a_mutual_form_cycle_does_not_hang` | Fm1→Fm2→Fm1. `max_depth == 2` |
| `test_the_depth_limit_stops_the_descent` | 상한에 걸리면 **「이미지 없음」 이 아니라 실패** |
| `test_an_exhausted_budget_that_already_found_a_big_image_is_kept` | 더 봐도 넓이는 안 줄어든다 |
| `test_an_exhausted_budget_without_enough_area_is_a_failure` | 못 봤고 모자라다 → **모르는 것** |
| `test_the_form_bbox_caps_an_overstated_area` | BBox 밖은 안 그려진다 |
| `test_a_missing_xobject_only_loses_that_branch` | 없는 이름을 그려도 쪽 조사를 안 막는다 |
| `test_the_nested_inspection_carries_no_image_content` | 중첩을 따라가도 **숫자뿐** |
| `test_the_traversal_makes_no_network_or_model_call` | 소스에 `http`·`socket`·`anthropic`·`vision_`·`subprocess` 가 없다 |

### 시험이 실제로 무는지 확인했습니다

네 가지를 각각 되돌려 돌렸습니다.

| 되돌린 것 | red |
|---|---|
| Form 재귀 | **8건** |
| `/Matrix` 곱 | 1건 |
| 순환 방어(단독) | **2건** |
| 깊이·예산 판정 | 2건 |

순환 방어는 처음에 깊이 상한에 **가려져 있었습니다** — 방어를 빼도 12겹에서
멈추니 결과가 같았습니다. `max_depth` 를 단언에 넣어 **순환 방어가 멈춘 것인지
상한이 멈춘 것인지** 구별되게 고쳤고, 그러고 나서 단독으로 2건이 빨개집니다.

## 6. 미검증

1. **실제 사내 PDF 로 돌리지 않았습니다.** 합성 자료뿐입니다. 한글이 내보낸
   실문서의 Form 중첩 모양이 제 빌더와 같은지는 서버에서 봐야 합니다
2. **탐지 건수가 얼마나 늘어나는지 모릅니다.** 전에 안 보이던 Form 중첩 이미지가
   이제 잡히므로 `unverified` 쪽이 **늘어납니다.** 그것이 이 변경의 의도지만
   규모는 미측정입니다
3. **클리핑(`W n`)은 계산하지 않습니다.** 보이는 부분만 작게 잘린 이미지는
   여전히 과대평가됩니다. BBox 로 일부만 깎았습니다
4. **Soft mask·패턴·타일링 패턴 안의 이미지는 안 봅니다.** `/Pattern` 으로
   표를 깔아 둔 PDF 가 있으면 그 쪽은 여전히 안 잡힙니다
5. `MAX_OPERATIONS = 200_000` 이 실문서에서 충분한지 재지 않았습니다. 예산
   소진이 자주 나면 그 값을 올리기 전에 **왜 그렇게 긴지**부터 봐야 합니다

## 7. 서버 확인 절차

원본·metadata 를 쓰지 않습니다. 조회뿐이고 **쪽 번호와 숫자만** 찍습니다.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python - <<'PY'
import json
from pathlib import Path
from tybot import attachment_review
from tybot.archive import pdf_page_images as pi
root = Path("/var/lib/tybot/archive")
nested = heavy = failed = total = 0
for item in attachment_review.scan(root):
    if item.filetype.lower() != "pdf" or not item.object_path:
        continue
    path = Path(item.object_path)
    if not path.is_file():
        continue
    total += 1
    census = pi.inspect(path.read_bytes())
    deep = [n for n, g in census.items() if g.inspected and g.max_depth > 0]
    hot = [n for n, g in census.items() if pi.image_heavy(g)]
    bad = [n for n, g in census.items() if not g.inspected]
    nested += bool(deep)
    heavy += bool(hot)
    failed += bool(bad)
    if deep or hot or bad:
        print(json.dumps({"ws": item.workspace, "ch": item.channel_id,
                          "file": item.file_id, "nested_pages": deep[:10],
                          "heavy_pages": hot[:10], "failed_pages": bad[:10]}))
print(f"PDF {total}건 · Form 중첩 {nested} · 그림 큰 쪽 있음 {heavy} · 조사 실패 {failed}")
PY
```

확인할 것:

- **`nested_pages` 가 실제로 나오는가.** 0 이면 이 서버의 PDF 에는 Form 중첩이
  없다는 뜻이고, 그러면 이번 변경의 효과도 없습니다
- `failed_pages` 가 많으면 `traversal_budget` 입니다. 상한을 올리기 전에
  그 문서가 왜 그렇게 긴지 먼저 봅니다
- 사람이 `heavy_pages` 중 한 쪽을 열어 **실제로 못 읽은 표가 있는지** 확인

## 8. Codex 에게

- `convert.py` 는 **한 줄도 안 고쳤습니다.** `image_heavy()` 와 `PageImages` 의
  모양이 그대로라 기존 배선이 그대로 동작합니다
- `images` 의 뜻이 바뀌었으니(§2) 그 값을 읽는 코드가 있으면 봐 주십시오.
  저장소 안에서는 `convert.py` 와 제 시험뿐입니다
- `max_depth` 는 진단용입니다. 「Form 중첩이 있는 문서인가」 를 세는 데 쓸 수
  있고, 원장에 실을지는 그쪽 판단입니다
- §6-4(패턴·soft mask)는 **아직 구멍입니다.** 실문서에서 그 모양이 나오면
  알려 주시면 이어서 보겠습니다
