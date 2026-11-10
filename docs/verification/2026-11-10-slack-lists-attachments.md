# Slack Lists 행 첨부 파일 누락 조사와 구현 계약

작성 2026-11-10. **아직 제품 코드를 고치거나 실제 Slack 데이터를 읽지 않았다.**
운영 제보를 재현하기 전에 현재 코드와 Slack 공식 API 계약을 대조한 기록이다.

## 결론

현재 TYBot 수집 경로는 일반 메시지 첨부, 채널별 `files.list`, Canvas가 가리키는
파일 ID를 처리한다. 그러나 Slack List의 행을 여는 API 호출은 없고 앱 manifest에도
`lists:read`가 없다. List 행의 첨부 파일이 기존 경로로 우연히 노출되지 않는다면
아카이브·변환·검색에서 빠질 수밖에 없는 구조다.

이것은 모델의 "학습" 문제가 아니다. 모델에 전달할 원문 좌표가 수집 단계에서 생기지
않는 문제다.

## 공식 API에서 확인한 사실

- [`slackLists.items.list`](https://docs.slack.dev/reference/methods/slackLists.items.list/)는
  List의 행을 페이지 단위로 읽으며 bot token의 `lists:read`가 필요하다.
- 행 필드에는 파일 ID 배열인 `attachment`가 올 수 있고, 링크 필드에는
  `link[].file.file_id`가 올 수 있다. 텍스트에서 파일 URL을 추측할 필요가 없다.
- Slack List 자체는 [`slackLists.items.info`](https://docs.slack.dev/reference/methods/slackLists.items.info/)
  응답에서 `filetype=list`, `mimetype=application/vnd.slack-list`인 파일로 표현된다.
- Lists API는 [2025년 9월 공개 API로 추가](https://docs.slack.dev/changelog/2025/09/02/list-api/)됐다.
  현재 저장소에는 이 API를 호출하는 코드가 없다.

## 현재 코드와의 차이

`src/tybot/archive/channel_files.py`는 의도적으로 `files.list(channel=...)`만 호출해
등록 채널 밖을 훑지 않는다. `referenced_files()`도 이미 알고 있는 파일 ID에 대해
`files.info`를 호출할 뿐이다. List 자체가 채널 파일 목록에 보이더라도 그 행을 열어
첨부 ID를 추출하는 단계가 없다.

따라서 기존 B-45/B-46을 넓혀 암묵적으로 처리하지 않고 B-90으로 별도 관리한다.
List는 일반 파일과 호출 방식, 페이지네이션, scope, rate limit, provenance가 다르다.

## 닫힌 수집 절차

1. 승인된 관리 채널 하나에서 `files.list(channel=...)`로 `filetype=list`를 찾는다.
2. 각 List에 `slackLists.items.list(list_id=..., cursor=...)`를 호출해 마지막 페이지까지
   읽는다. 한 페이지라도 실패하면 그 List 전체를 이번 회차에 적용하지 않는다.
3. 각 행 필드의 `attachment[]`와 `link[].file.file_id`만 추출한다. 행 텍스트와 일반
   URL은 파일 좌표로 해석하지 않는다.
4. 중복 파일 ID마다 `files.info`를 호출하고 현재 관리 채널에서의 가시성을 다시 확인한다.
   확인할 수 없으면 다운로드하지 않는다.
5. 검증된 파일 객체만 기존 `stage_files`와 attachment writer로 보낸다. 별도 다운로드,
   변환기, 정본 형식을 만들지 않는다.
6. provenance에 workspace, channel_id, list_id, row_id, column_id, file_id를 남긴다.
   감사 로그에는 ID와 건수만 남기고 행 본문이나 파일 내용을 남기지 않는다.

## 먼저 해야 할 읽기 전용 실측

- ~~SDK 에 편의 메서드가 없다~~ — **틀렸다(2026-11-10 확인).** 고정된
  `slack_sdk==3.43.0` 의 `WebClient` 에 `slackLists_items_list` 가 **있다.**

  ```
  slackLists_items_list(self, *, list_id, limit=None, cursor=None,
                        archived=None, **kwargs) -> SlackResponse
  ```

  같은 판에 `slackLists_items_info` · `slackLists_create` ·
  `slackLists_items_update` · `slackLists_access_set` 도 있다. 따라서 **SDK
  업그레이드도, 문자열 `api_call` 어댑터도 필요 없다.** 둘 다 하지 않는다 —
  업그레이드는 기존 Slack 경로 전체의 회귀 위험을 지불하는 일이고, 문자열 호출은
  오타가 런타임까지 가는 길이다.

  대신 **쓰기 메서드가 같은 클라이언트에 있다**는 사실이 새 위험이다.
  `slackLists_items_update` 는 `lists:write` 가 없으면 Slack 이 거절하지만,
  그 거절은 **런타임에** 온다. 수집 코드가 그 이름에 닿지 않는 것을 시험으로
  고정한다(`lists:write` 금지와 같은 축).
- 실제 응답에서 List ID, row ID, column ID와 첨부 필드 모양을 비식별 fixture로 만든다.
- `files.list(channel=...)` 결과의 List가 정말 해당 관리 채널에 연결됐는지 확인한다.
- 행 첨부 파일의 `files.info`가 `channels`/`groups`에 같은 채널을 싣는지 확인한다.
  싣지 않는다면 List 접근권한과 파일 접근권한을 결합하는 공식 계약을 추가 조사한 뒤
  구현을 멈춘다. 가시성을 추측해서 통과시키지 않는다.
- API pagination과 rate limit 응답을 기록한다. 일부 행만 읽고 완료로 표시하지 않는다.

실측 명령은 기본적으로 응답 구조와 건수만 출력해야 한다. 파일 다운로드, archive 쓰기,
행 본문 출력은 하지 않는다.

## 시험 계약

- attachment와 link-file 두 형식이 같은 기존 staging 경로에 도달한다.
- 메시지/파일 탭/Canvas/List에 중복 노출된 파일은 한 revision만 쓴다.
- 관리 밖 채널의 List, 비활성 채널, 파일 가시성 미확인, malformed ID는 모두 거절한다.
- 중간 페이지 실패와 rate limit은 `없음`이 아니라 재개 가능한 실패다.
- dry-run은 Slack 읽기 외 파일 다운로드·DB·archive 쓰기가 0건이다.
- Archiver에는 `lists:read`만 있고 `lists:write`는 없다. TYBot/VIPBot에는 수집 scope를
  넣지 않는다.
- B-86 마스킹 이전의 본문이 staging metadata, 로그, spill, 감사 기록에 남지 않는다.
- 실제 Slack 검증에서 첨부 정본, 검색 적중, 출처 좌표가 같은 파일 ID로 이어진다.

## 범위 밖

- List 행의 일반 텍스트를 원문으로 아카이빙하는 것
- List를 만들거나 수정하는 것
- 워크스페이스 전체 List 스캔
- VIPBot의 별도 수집기
- List 행 변경 이벤트가 있다고 가정한 실시간 동기화

이 항목들은 별도 오너 결정과 권한·보존 계약 없이는 추가하지 않는다.

---

## 실측 도구 (2026-11-10, Claude)

`scripts/probe_slack_lists.py` · 시험 `tests/test_probe_slack_lists.py` (44건).

```bash
sudo -u tybot /opt/tybot/.venv/bin/python /opt/tybot/scripts/probe_slack_lists.py \
    --workspace tyit --channel C0ABCDEF123 \
    --fixture /var/lib/tybot/reports/lists-probe-2026-11-10.json
```

### 1차 실측 (2026-11-10) — **결론 없음, 도구 결함**

```json
{"workspace": "5a9b3946", "channel": "648c9768",
 "list_count": 0, "list_lookup_failure": "", "lists": []}
```

**이 0 을 「List 가 없다」 로 읽으면 안 된다.** 도구가 틀렸다.

`files.list` 에 `types="list"` 를 보냈는데 Slack 문서의 `types` 값은
`all · spaces · snippets · images · gdocs · zips · pdfs · canvas` 다 —
**`list` 는 그 목록에 없다.** 알 수 없는 값으로 서버에서 거른 결과가 0건이고,
실패 칸은 비어 있었다.

이것은 이 도구가 막으려고 만든 바로 그 잘못이다. **못 본 것을 「없음」 으로
적었고, 읽는 사람이 구분할 길이 없었다.** 반쪽 페이지와 API 오류는 조심스럽게
다뤘으면서 정작 첫 호출의 필터를 확인하지 않았다.

고친 것 둘.

1. `types` 를 **보내지 않는다**(기본 `all`). 고르는 일은 우리가 한다 —
   응답의 `filetype == "list"` 로 거른다
2. `files_seen` 과 `filetypes` 를 함께 보고한다. 「파일이 0건」 과 「파일은
   많은데 List 가 없다」 는 다음에 할 일이 다르다

**다시 실행해야 한다.** 1차 결과는 버린다.

### 2차 실측 (2026-11-10) — **도구는 작동, 질문은 미해결**

```json
{"files_seen": 18,
 "filetypes": {"binary":1,"docx":1,"pdf":5,"png":6,"pptx":1,"quip":1,"xlsx":2,"zip":1},
 "list_count": 0, "list_lookup_failure": "", "lists": []}
```

파일 18건이 보인다 — **채널 지정·토큰·`files:read` 는 작동한다.** 1차와 달리 이
0 은 「아무것도 못 봤다」 가 아니다.

그런데 **핵심 질문은 아직 안 풀렸다.** 이 결과는 둘을 구분하지 못한다.

| | 뜻 | 다음에 할 일 |
|---|---|---|
| (가) | 이 채널에 Slack List 가 정말 없다 | List 가 붙은 등록 채널로 다시 잰다 |
| (나) | **Slack List 는 `files.list` 에 아예 안 나온다** | 시작점 설계를 다시 한다 |

(나)이면 이 문서 상단의 전제 — 「List 는 `files.list` 응답에서 `filetype=list` 인
파일로 표현된다」 — 가 틀린 것이고, B-90 수집 절차 1단계를 통째로 바꿔야 한다.

**공식 문서는 그 전제를 말하지 않는다**(2026-11-10 확인).
[`files.list`](https://docs.slack.dev/reference/methods/files.list/) 는 `files:read`
만 요구하고 List 를 돌려준다는 말이 없다.
[`lists:read`](https://api.slack.com/scopes/lists:read) 는 `slackLists.*` 메서드에만
붙는다. 둘을 잇는 문장이 없다.

`lists:read` 가 없어서 `files.list` 가 List 를 **숨기는** 것일 수도 있다. Slack 은
권한 없는 객체를 조용히 빼는 일이 흔하다. 그러면 scope 를 먼저 받아야 재볼 수 있고,
그건 「확인 전에는 권한을 넓히지 않는다」 와 맞부딪친다.

### 막혔다 — 사람이 한 번 봐야 한다

추측으로 메우지 않는다(계약 §가시성을 추측해서 통과시키지 않는다).

**확인할 것:** Slack UI 에서 그 채널(`C0BTU5LJD7A`)에 **Slack List 가 실제로
붙어 있는가.** 채널 상단 탭 또는 북마크에서 보인다.

| UI 에 List 가 | 결론 | 다음 |
|---|---|---|
| **없다** | (가). 도구는 정상 | List 가 있는 등록 채널로 다시 잰다 |
| **있다** | (나). `files.list` 는 시작점이 못 된다 | 아래 대안으로 설계 변경 |

### (나)일 때의 대안 (설계만, 구현 전)

1. **채널 북마크 후보 실측**(`bookmarks.list`). List 탭이 북마크 응답에 실제로
   나타나는지는 아직 확인하지 않았다. 나타날 때만 시작점으로 쓴다. 추가 scope
   `bookmarks:read`
2. **메시지 이벤트**에서 List 공유를 잡는다. 이미 수집 중인 경로라 scope 가 늘지 않지만,
   과거에 붙은 List 는 못 잡는다(소급 불가)
3. 콘솔에 **List ID 를 사람이 등록**한다. scope 가 가장 적게 늘고 범위가 가장
   명확하다. 대신 사람이 빠뜨리면 그대로 빠진다

셋 다 `slackLists.items.list` 에는 `lists:read` 가 필요하다. 바뀌는 것은 **무엇을
시작점으로 삼는가**뿐이다.

---

**유효한 실측은 아직 없다.** 위 확인 한 번이 다음 단계를 가른다.

### 토큰은 콘솔 DB 에서 온다 (2026-11-10 수정)

처음에는 `SLACK_BOT_TOKEN_<WS>` 환경변수를 읽었고, 서버에서 바로
`봇 토큰을 찾지 못했습니다` 로 멈췄다. **이 저장소에서 Slack 토큰의 정본은
DB 다**(`bot_connection` · `bot_connection_secret`, 콘솔이 관리).

env 를 따로 읽는 것은 못 찾는 것보다 나쁜 결말을 가진다 — 콘솔에서 토큰을 갈아
끼운 날 이 스크립트만 옛 토큰으로 돌고, 그 결과를 「그 봇이 List 를 못 본다」 로
읽게 된다. 실측이 **틀린 결론**을 내고, 그 결론으로 구현을 멈추거나 시작한다.

`console.bot_identity.runtime_tokens(workspace, bot_key)` 를 쓴다. 그 함수는
`state='enabled'` 이고 신원 확인을 통과한 연결만 돌려준다 — 꺼 둔 연결의 토큰으로
재면 운영에서 실제로 쓰이지 않을 권한을 보게 된다.

기본 봇은 **`archiver`** 다(`--bot` 으로 바꿀 수 있다). Lists 수집은 Archiver 의
일이므로 실측도 같은 토큰으로 해야 「이 봇이 볼 수 있나」 가 실제 답이 된다.
TYBot 토큰으로 재면 Archiver 에 없는 권한을 있는 것으로 읽는다.

`DATABASE_URL` 만 `/etc/tybot/tybot.env` 에서 읽는다.

### 이 도구가 하지 않는 것

| | |
|---|---|
| 파일 다운로드 | `url_private`·`requests`·`urlopen` 이 소스에 없다 |
| archive·DB 쓰기 | writer·store 를 들여오지 않는다 |
| Slack 쓰기 | 쓰기 메서드 **이름**이 소스에 없다(아래) |
| 워크스페이스 스캔 | `--channel` 하나뿐이고 `--all` 이 없다 |
| 본문·파일명·사용자 출력 | 건수와 해시만 출력한다 |

### 쓰기 메서드를 이름으로 막는 이유

`slack_sdk==3.43.0` 의 **같은 `WebClient`** 에 `slackLists_items_update` ·
`slackLists_access_set` 같은 쓰기 메서드가 함께 있다. `lists:write` 가 없으면
Slack 이 거절하지만 **그 거절은 요청이 나간 뒤에 온다.** 이름이 소스에 없으면
오타로도 부를 수 없다. 시험이 금지 목록 8개를 하나씩 센다.

### ID 를 해시로 남기는 이유

`files.info` 가시성 확인에는 실제 ID 가 필요하다. 그러나 **기록**에는 짧은
해시(SHA-256 앞 8자)로 적는다. 같은 ID 는 같은 값이 되어 대조는 되고 복원은 안
된다. 칸 이름도 해시한다 — 「담당자」 같은 이름은 사람이 쓴 말이고, List 의
구조에서 조직과 업무가 읽힌다.

검증 문서는 사람이 복사해 옮긴다. 저장소가 비공개라는 것은 그 복사본까지
보증하지 않는다.

### 시험이 고정하는 계약

| 계약 | 시험 |
|---|---|
| `lists:write` 금지 | 쓰기 메서드 8종 이름이 소스에 없음 · SDK 에 실재하는 이름인지 확인 |
| 전체 페이지 완독 | cursor 를 끝까지 따라감 · 끝나지 않는 cursor 는 **실패**이지 결과가 아님 |
| 부분 실패 시 미적용 | 한 쪽 실패 → `complete=False`, 그 List 는 가시성도 묻지 않음 |
| 파일 ID 명시 필드만 | `attachment[]` · `link[].file.file_id` 만 · 행 텍스트·URL 은 무시 |
| 가시성 확인 | 같은 채널 공유만 통과 · 타 채널만 공유는 불가 · 조회 실패는 `unverified` |
| 등록 채널 1개 | `filetype=list` 만 시작점 · 채널 목록 조회 실패는 「없음」 아님 |
| 기록에 본문 없음 | 보고서에 원문·파일명·ID 가 없음 · `print` 가 응답 필드를 안 꺼냄 |
| 토큰은 DB | `runtime_tokens` 사용 · env 미사용 · 기본 봇 `archiver` · 실패 시 예외 클래스만 출력 |

돌연변이 12종(`types="list"` 부활 · filetypes 집계 제거 · 토큰을 env 에서 읽기 · 기본 봇을 TYBot 으로 · 예외를 그대로
출력 · 반쪽 목록을 완료로 · 미완에 가시성 조회 · 가시성 실패를 통과로 ·
타 채널 공유를 통과로 · ID 평문 기록 · 문자열 `attachment` 허용 · `list` 아닌
filetype 수집) **전부 잡힘.**

### 작성 중 잡은 결함 하나

`attachment` 가 배열이 아니라 **문자열**로 오면 `for` 가 글자 하나하나를 돌려주고,
그 글자가 전부 파일 ID 가 된다. 그 다음 `files.info` 가 쓰레기 ID 로 나가고 응답은
「없는 파일」 이다. Slack 응답 모양이 바뀌는 날 조용히 그렇게 된다. `_as_list` 가
목록만 목록으로 본다 — 문자열은 목록이 아니다.

## 실측 뒤에 정해야 할 것

아래 둘은 **실제 응답을 보기 전에는 못 정한다.** 추측으로 메우지 않는다(계약).

1. 행 첨부의 `files.info` 가 `channels`/`groups` 에 **그 관리 채널을 싣는가.**
   싣지 않으면 List 접근권과 파일 접근권을 결합하는 공식 계약을 더 조사한 뒤
   구현을 멈춘다. 실측 보고서의 `other_channel_only` · `unverified` 가 그 답이다.
2. rate limit 응답의 모양과 재개 가능 여부. 지금은 실패를 사유 코드로만 남긴다.
