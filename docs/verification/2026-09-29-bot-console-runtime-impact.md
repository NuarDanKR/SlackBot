# 봇 관리 화면의 변경이 **지금 돌고 있는 것**에 무엇을 하나

작성: 2026-09-29 · 기준 커밋 `1aa59fc` 이후 · 설계 [§9·§12.2](../design/workspace-service-console-redesign.md)

대상: 콘솔에서 봇 연결을 다루는 운영자와, 이 화면을 이어서 만드는 개발자

## 한 줄

**지금은 기록이고 적용이 아니다.** 새 화면에서 토큰을 넣고 신원을 확인하고 연결을
꺼도, 돌고 있는 Master·Archiver·Hermes 프로세스는 **아무것도 달라지지 않는다.**

이 사실이 화면에 안 보이면 제일 비싼 오해가 난다 — 연결을 끈 사람은 수집이 멈춘
줄 알고 자리를 뜨고, 다른 사람은 봇이 계속 도는 것을 보고 「콘솔이 고장났다」 로
읽는다. 그리고 서버에 들어가 프로세스를 죽인다. 아무 오류도 안 나기 때문에
둘 다 자기가 맞다고 믿는다.

## 왜 그런가 — 읽는 쪽이 아직 옛 표다

| 무엇 | 지금 읽는 것 | 새 표를 읽나 |
|---|---|---|
| Master 워크스페이스·토큰 | `workspace_secret` (`workspaces.load_workspaces`) | 아니오 |
| Archiver 기동 설정·토큰 | `workspace_service` (`archiver_runtime_config`) | 아니오 |
| Master 의 전문 봇 라우팅 | `specialist_workspace` + `specialist_bot` (`specialist_router`) | 아니오 |

credential reader 전환은 §12.2 **8단계**다. 그전까지 `bot_connection` ·
`bot_catalog` · `specialist_route` 는 **콘솔만 보는 표**다.

이 표를 코드가 지킨다. `bot_admin.RUNTIME_READS_NEW_TABLES` 가 `False` 이고,
`tests/test_bot_admin.py::test_no_runtime_reader_reads_the_new_tables_yet` 이 위 세
파일을 훑어 새 표 이름이 나타나면 실패한다. 전환을 시작하는 사람은 그 시험에서
막히고, 값을 함께 바꾸게 된다 — 안 그러면 화면이 거짓말을 한다.

## 화면이 말해야 하는 것

읽기 API 세 개(`/api/bots`, `/api/workspaces/{ws}/bot-connections`,
`/api/workspaces/{ws}/bot-routes`)가 모두 `runtimeEffect` 를 싣는다. 화면이 잊을 수
없게 **응답마다** 들어간다.

```json
{"appliesNow": false,
 "summary": "지금은 설정만 기록됩니다. 돌고 있는 봇에는 반영되지 않습니다.",
 "details": ["…", "…", "…"]}
```

## 무엇이 무엇을 바꾸지 **않는가**

| 콘솔에서 한 일 | 지금 일어나는 일 | 일어나지 않는 일 |
|---|---|---|
| 토큰 저장 | 암호문·mask 기록, 이전 신원 결과 폐기, 연결 `disabled` 로 | 프로세스 재시작 없음. 옛 토큰으로 계속 돈다 |
| 신원 확인 | Slack 에 물어 Team·Bot 사용자 기록, 연결 `enabled` | 수집 시작 없음 |
| 연결 `disabled`·`retired` | 기록만 | **수집이 멈추지 않는다.** Socket 연결도 그대로 |
| 라우트 `shadow`·`active` | 기록만 | 답변 경로 변화 없음. Master 는 여전히 배정만 보고 라우팅한다 |
| Manifest 대조 기록 | 사람이 확인한 hash 저장 | Slack 앱 설정 변경 없음 |

## 수집을 실제로 바꾸는 손잡이는 따로 있다

채널 수집 모드(`off`·`shadow`·`active`·`paused`)와 writer 인수는 `archiving_admin`
이 소유하고, 그쪽은 release gate 를 그대로 지난다(§9). 봇 관리 화면은 그 표를
**아예 만지지 않는다** — `bot_admin` 소스에 `archive_channel_mode` ·
`archive_feature_flag` · `writer_owner` 가 없다는 것을 시험이 고정한다.

그래서 화면도 둘을 갈라 둔다.

- **봇 관리** — 어느 봇이 어느 워크스페이스에 어떻게 붙는가(연결·라우트)
- **워크스페이스 > 수집 설정** — 그 채널의 원문을 누가 쓰는가(모드·인수)

## 전환이 끝나면

8단계에서 reader 를 옮긴 뒤 `RUNTIME_READS_NEW_TABLES` 를 `True` 로 바꾸고, 화면
문구도 「저장 즉시 반영됩니다」 로 바뀐다. 그때부터 연결 중지는 **실제로** 그 봇의
Slack 연결을 끊는다 — 그 전에 이 문서의 표를 다시 쓴다.
