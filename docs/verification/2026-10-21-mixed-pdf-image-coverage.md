# 혼합 PDF — 글자가 나왔다고 그림 안까지 읽은 것이 아니다 (B-80)

Written for: Codex, 운영. 작성: Claude, 2026-10-21.
읽은 것: `archive/vision_extract.py` · `archive/vision_policy.py` ·
`archive/vision_store.py` · `archive/convert.py::_pdf` 쪽별 coverage.

**커밋하지 않았습니다.** `specialist_tools.py` · 콘솔 환경설정 ·
`test_specialist_tool_setting.py` 는 열지 않았습니다. Vision 정책·원장은
**읽기만** 했습니다.

## 1. 무엇이 비어 있었나

`_pdf_text_layer()` 는 글자가 나오면 그 쪽을 읽은 것으로 셉니다. **이미지 안은
보지도 않습니다.**

업무보고 한 쪽이 이렇게 생겼다고 하면 —

```
[문단 두 줄]            ← 텍스트 레이어에 있다
[스캔해 붙인 기성 내역 표]  ← 이미지다. 아무 데도 안 남는다
```

그 쪽은 `converted` 로 닫힙니다. 표는 사라지고, **그 쪽은 「다 읽음」 이라 다시
보지도 않습니다.** 사람이 「기성금 얼마야」 라고 물으면 봇은 「자료에 없습니다」
라고 답하고, 그 답은 틀린 줄 아무도 모릅니다.

2026-10-19 Vision 분담 문서가 이미 짚어 둔 자리입니다.

> `_pdf_text_layer`: 글자가 나오면 이미지 본문의 누락을 별도로 확인하지 않는다.

## 2. 고친 방향 — 세 번째 갈래를 만들었습니다

전에는 쪽이 두 갈래였습니다. 둘로는 이 상황을 표현할 수 없습니다.

| 갈래 | 뜻 | 사람이 할 일 |
|---|---|---|
| `converted` | 읽었다 | 없음 |
| `missing` | **아무것도** 못 얻었다 | 재변환·OCR·Vision |
| **`unverified`**(신규) | **얻은 것이 전부가 아니다** | 그 쪽 그림을 따로 읽어야 한다 |

`missing` 에 넣지 않은 이유가 중요합니다. 그 쪽에는 **본문이 나왔고**, 못 읽은
쪽으로 세면 그 글자가 답변에서 빠집니다. 잃는 방향이 반대로 바뀔 뿐입니다.

`Coverage.unverified` 는 `state` 를 `partial` 로 만들고, `to_json()` 에
`unverified_units` 로 앞 50개가 실립니다 — **진단용 후보 미리보기**입니다.

## 3. 어떻게 판정하나

`archive/pdf_page_images.py`(신규)가 쪽마다 **이미지가 얼마나 차지하나**만 셉니다.

| 센다 | 세지 않는다 |
|---|---|
| 이미지 XObject 개수 | 그 안에 무엇이 그려져 있나 |
| 이미지가 덮은 넓이 ÷ 쪽 넓이 | 표인지 사진인지 로고인지 |
| 가장 큰 이미지 한 장의 비율 | 픽셀·OCR·모델 호출 |

**픽셀 크기가 아니라 쪽 위에 놓인 크기**를 봅니다. 콘텐츠 스트림의 `cm`/`Do` 를
따라가 현재 변환 행렬에서 `|a·d − b·c|` 로 그려진 넓이를 구합니다. 300dpi 스캔
표는 픽셀로는 거대하지만 쪽에서 차지하는 자리는 쪽을 넘지 않고, 반대로 작은
아이콘을 크게 늘려 놓은 쪽도 있습니다. 행렬을 못 읽으면 픽셀 비로 어림하되
**쪽 넓이로 묶습니다.**

기준은 `DEFAULT_IMAGE_AREA_RATIO = 0.10` 입니다. 머리글 로고는 보통 2~3%이고,
스캔해 붙인 표·도면은 쪽의 상당 부분을 덮습니다.

> **판정은 한쪽으로만 틀립니다.** 로고만 큰 쪽도 의심으로 잡힙니다. 반대 방향
> (표를 놓치는 것)보다 이쪽이 쌉니다 — 의심은 사람이 지우면 되고, 놓친 표는
> 아무도 모릅니다.

조사 자체가 실패한 쪽은 **의심으로 바꾸지 않고** `image_scan_failed` 플래그로
따로 셉니다. 「그림이 없다」 와 「조사를 못 했다」 는 다릅니다.

## 4. 승인·예산 없이 아무것도 보내지 않습니다

**coverage 는 판정이지 전송이 아닙니다.** 여기서 Vision 을 부르면 승인·예산·
provider 검사를 전부 건너뛴 전송이 됩니다.

지키는 방법 둘:

- `convert.py` 는 `vision_extract`·`vision_policy`·`vision_store`·`extract_page`
  를 **import 하지 않습니다.** 시험이 소스를 훑어 확인합니다 — 변환기가 승인
  경로를 들고 있으면 **언젠가 거기서 부릅니다**
- `pdf_page_images.py` 에 `requests`·`urllib`·`socket`·`http`·`anthropic`·
  `router` 가 없는지도 시험합니다

쪽을 **후보로 표시만** 하고, 보내는 일은 승인을 쥔 worker 몫입니다.

## 5. 실패 기록을 덮지 않습니다

- `unverified` 가 하나라도 있으면 `state` 는 **절대 `succeeded` 가 아닙니다**
- 그 쪽의 **글자는 버리지 않습니다.** `[N쪽]` 과 본문이 그대로 남습니다
- 본문 상단에 `[변환 안내] 그림 안을 읽지 못한 쪽: 2, 5` 가 붙습니다 —
  **쪽 번호만** 담고 문서 내용은 담지 않습니다

## 6. 변경 파일

| 파일 | 내용 |
|---|---|
| `src/tybot/archive/pdf_page_images.py` | **신규.** 쪽별 이미지 조사. 순수 함수, I/O·네트워크 없음 |
| `src/tybot/archive/convert.py` | `Coverage.unverified` · `IMAGE_CONTENT_UNREAD` · `IMAGE_SCAN_FAILED` · `_pdf` 훅 (+54 −3) |
| `tests/test_pdf_mixed_pages.py` | **신규 24건** |
| `tests/test_conversion_coverage.py` | metadata 키 계약에 `unverified_units` 추가 |

### 계약 변경

- `Coverage.unverified: tuple[int, ...] = ()` — 기본값이 있어 **기존 생성자
  호출은 그대로** 동작합니다
- `Coverage.to_json()` 에 `unverified_units` 추가. metadata·목록 API 소비자가
  모르는 키를 받습니다
- `Coverage.summary()` 에 「그림 미해독 N쪽」 추가
- `_record(..., unverified=())` 인자 추가

### 판단이 필요한 자리 — `config_snapshot("pdf")` 에 넣지 않았습니다

문턱값을 pdf config 에 넣으면 **모든 PDF revision 이 바뀌어 전량 재변환**이
걸립니다. 그런데 이 변경으로 **추출 본문은 달라지지 않습니다** — 달라지는 것은
coverage metadata 의 정직함뿐입니다.

그래서 넣지 않았습니다. 대가는 같은 revision 의 두 문서가 변환 시점에 따라 다른
`coverage_state` 를 가질 수 있다는 것입니다(OCR 가용성에서 이미 그렇습니다).
**revision 은 Codex 담당이라 결론을 적지 않고 올려 둡니다.**

## 7. 시험 결과

개발 PC(Windows)에서 돌렸습니다. 서버 절차가 아니라 결과 보고입니다.

| 무엇 | 결과 |
|---|---|
| `ruff check` (`src` · `tests` · `scripts`) | All checks passed |
| 전체 (`test_external_convert.py` 제외) | **5,375 passed · 43 skipped** (252s) |
| `tests/test_pdf_mixed_pages.py` 단독 | **24 passed** |

합성 PDF 로만 돌립니다 — 글자와 이미지 XObject 를 직접 조립합니다. 실제 사내
PDF 는 fixture 에 넣지 않고, 모델도 OCR 도 부르지 않습니다.

주요 시험:

| 시험 | 지키는 것 |
|---|---|
| `test_a_mixed_page_is_unverified_not_converted` | **B-80 의 그 일.** 문단 읽힘 + 표 안 읽힘 → `converted=1` · `unverified=(2,)` · `missing=()` |
| `test_a_clean_text_pdf_stays_succeeded` | 멀쩡한 문서를 전부 미확인으로 만들지 않는다 |
| `test_a_logo_on_every_page_does_not_turn_the_document_partial` | 로고 2~3%는 신호가 아니다 |
| `test_a_failed_inspection_is_not_called_suspect` | 조사 실패 ≠ 그림이 크다 |
| `test_marking_a_page_unverified_calls_no_model` | `extract_page` 를 폭탄으로 바꿔 놓고 돌린다 |
| `test_the_converter_never_imports_the_vision_worker` | 소스에 승인 경로가 없다 |
| `test_the_notice_carries_no_document_text` | 안내 줄·metadata 에 본문이 없다 |
| `test_an_unverified_page_never_becomes_succeeded` | 부분을 완료로 올리지 않는다 |

**시험이 실제로 무는지 확인했습니다.** 혼합 쪽 판정을 끄고 조사 실패를 의심으로
뒤집어 돌렸습니다 → **6건 red.** 복구 → 24건 green.

## 8. 서버 검증 절차

원본·metadata 를 쓰지 않습니다. 조회뿐입니다.

```bash
# 1) 이미 변환된 PDF 첨부에서 혼합 쪽이 몇 건 나오나 — 영향 범위
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python - <<'PY'
import json
from pathlib import Path
from tybot import attachment_review
from tybot.archive import pdf_page_images as pi
root = Path("/var/lib/tybot/archive")
heavy = total = 0
for item in attachment_review.scan(root):
    if item.filetype.lower() != "pdf" or not item.object_path:
        continue
    path = Path(item.object_path)
    if not path.is_file():
        continue
    total += 1
    census = pi.inspect(path.read_bytes())
    pages = [n for n, got in census.items() if pi.image_heavy(got)]
    if pages:
        heavy += 1
        print(json.dumps({"ws": item.workspace, "ch": item.channel_id,
                          "file": item.file_id, "pages": pages[:20],
                          "coverage": item.conversion_state}))
print(f"PDF {total}건 중 그림 큰 쪽이 있는 문서 {heavy}건")
PY
```

> 이 출력에는 **쪽 번호와 좌표만** 있습니다. 문서 내용은 나오지 않습니다.

```bash
# 2) 대표 문서 한 건을 새 coverage 로 다시 재어 본다 (metadata 안 건드림)
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python -c "
import sys
from pathlib import Path
from tybot.archive.convert import convert_with_coverage
data = Path(sys.argv[1]).read_bytes()
lines, cov = convert_with_coverage('pdf', data)
print(cov.to_json())
print(cov.summary())
" /var/lib/tybot/objects/.../<파일>.pdf
```

확인할 것:

- `unverified_units` 에 **실제로 그림이 큰 쪽**이 들어가는가
- `coverage_state` 가 `succeeded` 에서 `partial` 로 **얼마나 많이** 바뀌는가.
  너무 많으면 문턱값(0.10)을 올리는 것이 아니라 **왜 그런지**를 먼저 봐야 합니다
- 사람이 그중 한 쪽을 열어 **실제로 못 읽은 내용이 있는지** 확인

## 9. 미검증

1. **실제 사내 PDF 로 돌리지 않았습니다.** 합성 자료뿐입니다. 문턱값 0.10 이
   실문서에서 맞는지는 §8-1 의 분포를 봐야 압니다
2. **`coverage_state` 가 바뀌는 건수를 모릅니다.** `succeeded` → `partial` 로
   넘어가는 문서가 늘어납니다. 그것이 이 변경의 **의도**지만, 규모는 미측정입니다
3. **표가 실제로 그 그림 안에 있는지 확인하지 않았습니다.** 이 판정은 「봐야 할
   것이 있나」 까지입니다. 무엇이 그려져 있는지는 읽어야 알고, 읽는 것은 OCR·
   Vision 의 일입니다
4. **`unverified_units` 소비자를 확인하지 못했습니다.** 콘솔 목록·답변 경로가
   모르는 키를 어떻게 다루는지는 그쪽 코드라 확인 밖입니다
5. 회전·클리핑된 이미지의 넓이 계산은 단위 사각형 변환만 봅니다. 클리핑으로
   실제 보이는 부분이 작은 경우 **과대평가**될 수 있습니다

## 10. Codex 에게

- `unverified_units` 는 Vision 후보의 진단용 앞 50개입니다. 원장은 전체
  페이지 범위를 다시 확인한 뒤 승인·예산을 거쳐야 합니다. 제 쪽에서는 전송을
  시작하지 않습니다
- `missing` 과 `unverified` 를 **합치지 말아 주십시오.** 합치면 본문이 나온 쪽이
  못 읽은 쪽으로 세어지고, 그 글자가 답변에서 빠집니다
- §6 의 revision 판단(`config_snapshot` 에 문턱값을 넣을지)이 그쪽 결정입니다
- Vision 전사가 성공한 쪽을 `unverified` 에서 빼는 자리는 **원장 쪽**입니다.
  변환기는 승인을 모르므로 여기서 지울 수 없습니다

## 11. Codex 검수 (2026-10-21)

- `partial` 정본에는 추출한 글자가 남지만, 일반 검색의 `attachment_reader.evidence()`는
  `succeeded`만 채택한다. **현재 답변 근거에는 그 글자가 나오지 않는다.**
  Vision 승인·처리 경로가 연결되기 전까지 혼합 PDF 탐지는 검색 품질 개선이 아니라
  불완전한 문서를 표시하고 차단하는 조치다. 기존 검색 가능 PDF가 `partial`로
  재변환되면 답변 가능 범위가 줄 수 있으므로 영향 건수를 먼저 측정한다.
- reader가 상태를 먼저 거른 뒤 최신 판을 골라 오래된 `succeeded`를 다시 노출하던
  순서를 수정했다. 최신 판이 `partial`이면 구판도 일반 근거로 쓰지 않는다.
- 판정 상태가 달라지면 정본 프론트매터도 달라진다. PDF 변환기 판을 3에서 4로
  올려 기존 revision에 덮어쓰지 않는다. **기존 PDF의 소급 재변환은 자동 실행되지
  않는다.** 별도 범위·비용 검토 후 `--outdated`를 사용해야 한다.
- 이미지 조사가 빈 결과이거나 개별 이미지 배치 계산에 실패하면
  `image_scan_failed`로 `partial` 처리한다. 리소스에만 등록되고 그려지지 않은
  이미지는 면적 0으로 센다.
- 이미지가 Form XObject 안에 중첩되거나 벡터 도형으로 그려진 경우는 아직
  정확히 판정하지 못한다. 실제 사내 PDF 표본으로 오탐·미탐을 검증해야 한다.
- `unverified_units`는 앞 50쪽 번호만 직렬화한다. 전체 Vision 작업 목록으로
  그대로 사용하면 51번째 이후를 놓친다. worker는 원본의 전체 coverage를 다시
  계산하거나, 승인된 범위의 페이지 목록을 별도로 보존해야 한다.
