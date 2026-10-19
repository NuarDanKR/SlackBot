# HWP `converter_crashed` 진단 — 무엇을 모르는지부터

Written for: Codex, TYBot 팀. 작성: Claude, 2026-10-19.
대상: tyit `C0C9A86JUSJ` · `F0C9LHFTHLM`(2026.09) · `F0C9VH39NTW`(2025.12)
보드 B-80 · 분담 [2026-10-19-archiver-vision-claude-handoff.md](2026-10-19-archiver-vision-claude-handoff.md)

**DB·정책·gateway 를 수정하지 않았습니다.** 새 파일 둘만 만들었습니다.

## 1. 저장된 것만으로는 원인을 알 수 없습니다

`converter_crashed` 는 **서로 다른 두 일**에 붙습니다
(`archive/external_convert.py::_run`).

```python
code = "converter_crashed" if process.returncode < 0 else "conversion_failed"
# A V8 fatal message is a crash, not proof of OOM or a particular policy.
if "Fatal error" in stderr or "V8_Fatal" in stderr:
    code = "converter_crashed"
```

| 갈래 | 뜻 | 사람이 할 일 |
|---|---|---|
| `returncode < 0` · **SIGKILL** | 신호로 죽었다. 대개 OOM killer | 메모리 상한·격리 |
| `returncode < 0` · **SIGSEGV** | 파서가 터졌다 | 파일 구조·변환기 버그 |
| `returncode < 0` · **SIGABRT** | 변환기가 스스로 중단 | stderr 추적 |
| stderr 에 `V8_Fatal` | kordoc 은 Node 다. JS 힙 의심 | `--max-old-space-size` 로 재현 |

**그런데 신호 번호도 stderr 도 어디에도 저장되지 않습니다.** metadata 가 들고
있는 것은 `error_code` 하나뿐입니다. 예외 메시지에 `exit=` 이 들어가지만 그
메시지는 저장되지 않습니다(`failure_details()` 는 코드만 보존).

그래서 **지금 있는 자료로는 넷 중 어느 것인지 영원히 알 수 없고, 다시 돌려 봐야
합니다.** 그 일을 하는 스크립트를 만들었습니다.

> 받은 전제 그대로입니다 — 「변환기 비정상 종료까지 확인된 것이며, OCR 오류·OOM·
> 파일 손상으로 확정된 것은 아니다」. 이 문서는 그 미확정을 **확정으로 바꾸는 절차**이지
> 원인을 주장하는 문서가 아닙니다.

## 2. 또 하나 — 확장자를 믿을 수 없습니다

`convert.py::_hwp` 는 kordoc 단일 경로이고, kordoc 에는 파일을
`source.<filetype>` 으로 써서 넘깁니다. **`filetype` 은 Slack 이 준 확장자입니다.**

`.hwp` 로 올라온 것이 실제로는 HWPX(zip)이거나 PDF 면 kordoc 은 엉뚱한 파서로
들어가고, 거기서 터지면 그것도 `converter_crashed` 입니다. **이름만 바꾼 것은
변환이 아닙니다.**

그래서 진단 1순위는 재현이 아니라 **실제 컨테이너 확인**입니다.

| 머리 바이트 | 판정 |
|---|---|
| `D0 CF 11 E0 A1 B1 1A E1` + `HWP Document File` | HWP 5.x (OLE2) |
| `D0 CF 11 E0 …` · 서명 없음 | OLE2 이지만 HWP 아님 — doc·xls 일 수 있다 |
| `PK\x03\x04` + `Contents/` | HWPX — **확장자가 틀린 것** |
| `HWP Document File V3.00` | HWP 3.0 구형. kordoc 지원이 다르다 |
| `%PDF-` | PDF 를 hwp 로 올린 것 |

HWP 5 는 FileHeader 속성 비트도 봅니다 — **암호·배포용·DRM 문서는 어떤 변환기도
열지 못합니다.** 반대로 비트가 깨끗하다는 것이 **파일 내부가 온전하다는 뜻은
아닙니다** — 서명과 속성만 본 것입니다. 크래시 원인과 **별개로** 복구 불가이고, 그러면 Vision 경로로
가도 소용없습니다. 먼저 알아야 하는 사실입니다.

> 이 비트 읽기는 **정식 OLE2 파싱이 아닙니다.** `olefile` 이 의존성에 없어 서명
> 위치를 찾아 그 자리를 읽는 어림입니다. 「의심된다」 까지만 말하고, 확정은 사람이
> 원본을 열어 합니다.

## 3. 만든 것

| 파일 | 내용 |
|---|---|
| `scripts/diagnose_hwp_conversion.py` | **조회 전용** 진단. 아카이브·metadata·DB 를 쓰지 않는다 |
| `tests/test_diagnose_hwp_conversion.py` | 판정 로직 30건 |

진단 단계:

1. **원본 찾기** — staging metadata 로 좌표를 맞춥니다. **파일 이름으로 찾지
   않습니다** — 같은 이름의 보고서가 기준월마다 올라오고, 이름으로 묶으면 두 달치가
   한 건으로 보입니다. 원본이 안 남아 있으면 **거기서 끝냅니다**(추측하지 않음)
2. **실제 컨테이너** + HWP 5 속성 비트 (§2)
3. **kordoc 재현** — 파이프라인과 **같은 명령·같은 HOME·같은 120초 상한**으로
   돌려 **종료 신호 · stderr 꼬리 · 최대 RSS** 를 가져옵니다
4. **LibreOffice 렌더링**(`--render`) — HWP → PDF 가 되는가. 되면 그 PDF 의
   **쪽 수와 글자가 나오는 쪽 수**까지 셉니다

`external_convert._run()` 을 쓰지 않습니다. 그쪽은 실패를 예외로 바꾸면서
**신호 번호와 stderr 를 버립니다** — 지금 알고 싶은 것이 바로 그 둘입니다.

### 「설치돼 있다」 와 「이 판을 읽는다」 를 가릅니다

LibreOffice 가 exit 0 으로 끝나고도 PDF 를 안 만들거나, 만들어도 쪽이 비는 경우가
있습니다. 그래서 exit code 로 판정하지 않고 **나온 PDF 를 열어 셉니다.**

| 결과 | 판정 |
|---|---|
| exit 0 · PDF 없음 | 이 HWP 판을 **열지 못한 것** |
| PDF 있음 · 0쪽 | 렌더링본으로 쓸 수 없음 |
| PDF 있음 · 글자 있는 쪽 0 | 스캔본처럼 **OCR 또는 Vision 경로가 필요** |
| PDF 있음 · 글자 있는 쪽 N | **로컬 추출로 복구 가능성** |

마지막 줄이 중요합니다 — 로컬 추출로 되면 Vision 을 부를 이유가 없습니다
(복구 순서: 로컬 HWP 추출 → PDF 렌더링 → 텍스트 추출/OCR → **승인된 경우에만** Vision).

## 4. 아무것도 고치지 않습니다

- 아카이브·metadata·DB 에 쓰지 않습니다. 변환본을 발행하지 않습니다
- 작업 파일은 임시 디렉터리에만 두고 끝나면 지웁니다
- 결과에 **문서 본문이 들어가지 않습니다.** 원시 stderr 를 싣지 않고, 허용 목록
  표식(`v8_fatal`·`segfault`·`bad_alloc` …)과 줄 수·글자 수만 남깁니다 —
  변환기는 처리 중인 문단·셀 값을 오류에 실어 보내고, 꼬리를 자르면 **어디를
  자르느냐가 운이 됩니다**
- `--dump-dir` 를 주면 대조용 산출물을 **파일로만** 내놓습니다. 화면에 찍지
  않고, 그 파일은 사내 문서 본문이라 로그·문서·티켓에 붙이면 안 됩니다
- 원본이 없으면 거기서 멈춥니다

## 5. 서버 절차

```bash
# 1) 원본이 남아 있는가 — 이것부터다. 없으면 나머지가 의미 없다
sudo -u tybot find /var/lib/tybot/archive \
  \( -path '*F0C9LHFTHLM*' -o -path '*F0C9VH39NTW*' \) -printf '%s\t%p\n'

# 2) 컨테이너·암호 여부 + kordoc 재현 (렌더링 없이, 빠르다)
sudo -u tybot TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/diagnose_hwp_conversion.py \
  --archive /var/lib/tybot/archive \
  --file-id F0C9LHFTHLM --file-id F0C9VH39NTW

# 3) SIGKILL 이 나왔다면 — 같은 시각 OOM killer 기록과 대조
sudo journalctl -k --since '2026-10-19' | grep -i 'killed process'

# 4) 렌더링까지 (오래 걸릴 수 있다)
sudo -u tybot TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/diagnose_hwp_conversion.py \
  --archive /var/lib/tybot/archive \
  --file-id F0C9LHFTHLM --file-id F0C9VH39NTW --render
```

`--json` 을 붙이면 기계가 읽을 모양으로 나옵니다. 어느 모양에도 본문은 없습니다.

## 5-1. 실측 결과는 따로 있습니다

이 문서는 **절차와 도구**입니다. 2026-10-19 서버 실측값과 그 한계는
[2026-10-19-hwp-findings.md](2026-10-19-hwp-findings.md) 에 있습니다.

## 6. 시험 결과

개발 PC(Windows)에서 돌렸습니다. 서버 절차가 아니라 결과 보고입니다.

| 무엇 | 결과 |
|---|---|
| `ruff check` (`src` · `tests` · `scripts`) | All checks passed |
| 전체 (`test_external_convert.py` 제외) | **5,256 passed · 41 skipped** (160s) |
| `tests/test_diagnose_hwp_conversion.py` 단독 | **38 passed** |

판정 로직을 들어내고 돌려 **5건이 빨간 것**도 확인했습니다 — 컨테이너 판별 2건
(HWPX 오인·OLE2 오인), 신호 구분 1건, 원본 없을 때 멈추기 2건.

