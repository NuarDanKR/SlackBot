# Google Drive 근거 — 승인 폴더만, 같은 수집본을 TYBot·VIPBot 이 함께 읽는다 (B-97)

- 상태: **설계 초안** (2026-11-18, Claude) · 구현 전 · 오너 결정 대기(§12)
- 관련: [B-97](../../BACKLOG.md#b-97) · 위협 모델 [google-drive-threat-model.md](google-drive-threat-model.md)
  · 첨부 정본 [archiving-bot-separation-2026-09-23.md](archiving-bot-separation-2026-09-23.md)
  · PII [pii-guardrail-and-canvas-artifacts.md](pii-guardrail-and-canvas-artifacts.md)
  · VIPBot 권한 [B-83](../../BACKLOG.md#b-83) · 콘솔 원칙 [CLAUDE.md 「운영 기능은 콘솔에 둔다」](../../CLAUDE.md)
- 이 문서는 **Google API 를 호출하지 않고, 운영 자격증명을 만들지 않은 상태**에서 쓴 계약이다.
  API 동작은 §14 공식 문서로만 근거를 댄다. 실측이 필요한 곳은 「실측 필요」 로 표시했다.

## 1. 무엇을 풀려는가

현장·본부는 문서를 Slack 에 올리지 않고 **Google Drive 링크**로 공유한다. 지금 봇은 그 링크를
「링크 한 줄」 로만 남기므로, 사람이 「그 문서에 뭐라고 돼 있었지」 를 물으면 근거가 없다.

Slack 의 Google Drive 앱을 설치해도 이것은 풀리지 않는다.

- Slack 문서는 원격 파일(remote file)을 「다른 곳에 사는 파일을 가리키는 포인터」 로 정의한다.
  Slack 이 저장하는 것은 메타데이터·미리보기 이미지·검색용 `indexable_file_contents` 이고,
  **봇이 Slack 을 통해 원격 파일 본문을 받는 메서드는 문서에 없다**
- 외부 파일의 `url_private` 은 「어떤 호스트든 가리킬 수 있고」, Slack 은 원격 파일에 대해
  「Slack 자격증명 없이 요청하거나, 아예 요청하지 말라」 고 쓴다(§14-S2)

그래서 Drive 본문은 **TYBot 이 자기 Google 자격증명으로 Drive API 를 직접 불러서** 얻는다.
Slack 은 「어느 파일이 어느 채널에서 언급됐나」 를 알려 주는 신호일 뿐이다.

## 2. 지키는 선

| 선 | 근거 |
|---|---|
| Slack 외부 파일·`external_url`·Drive URL 에 **Slack `Authorization` 헤더를 절대 보내지 않는다** | §14-S2, 위협 T1 |
| Drive 본문은 **Drive API 전용 자격증명**으로만 읽는다. Slack 토큰·Slack 앱 권한으로 우회하지 않는다 | 요구 1·3 |
| **관리자가 콘솔에서 승인한 Shared Drive·폴더만** 읽는다. 승인 밖 파일은 링크가 공유돼도 본문을 받지 않는다 | 원칙 3·4 |
| 승인 루트마다 `(workspace, channel_id)` 좌표를 **DB 에** 둔다. `.env` 정책을 만들지 않는다 | 요구 5, B-95 선례 |
| 개인 My Drive 전체 수집·domain-wide delegation 은 **초기 범위 밖** | 요구 6, §13 |
| Drive 문서는 **결속 채널의 ACL 을 상속한다.** 링크가 다른 채널에 올라와도 그 채널로 넓어지지 않는다 | 원칙 3, `AttachmentDoc` 상속 규칙 |
| 변환·마스킹·정본·색인은 **기존 첨부 경로를 그대로** 탄다. Drive 전용 변환기·마스커를 만들지 않는다 | 요구 9, 원칙 5 |
| 원본 Drive 에서 삭제·권한 철회·다운로드 제한이 생기면 **근거에서 뺀다.** 새 revision 이면 새 판을 쌓고 옛 판은 근거에서 내린다 | 요구 10, 원칙 1 |
| 출처는 **원본 Drive 링크 + 파일명 + 수정 시각**. Slack 메시지 링크로 대신하지 않는다 | 원칙 2, 요구 11 |
| TYBot·VIPBot 은 **같은 수집본**을 읽는다. 각자 기존 권한(`visible_docs` / `bot_source_channel`)만 적용한다 | 요구 12, B-83 |
| 자격증명 평문은 저장소·DB·로그·감사기록 어디에도 없다 | 원칙 6, 요구 13 |
| 봇 출력은 Drive 로 쓰지 않는다. 읽기 전용 | 원칙 1 |

## 3. 현재 코드 사실 (2026-11-18 확인)

| 사실 | 위치 | 의미 |
|---|---|---|
| `download_bytes`·`download_text` 가 `url_private_download or url_private` 에 **호스트 검사 없이** `Bearer <bot_token>` 을 붙인다 | `src/tybot/archive/files.py:371-399` | **위협 T1 이 이미 열려 있다.** §5 0단계 |
| `SlackFile.from_event` 가 `url_private_download` 이 없으면 `url_private` 으로 내려간다. 원격 파일은 `url_private_download` 이 「대개 없다」(§14-S2) | `files.py:105` | 원격 파일이면 외부 URL 이 그대로 다운로드 대상이 된다 |
| `stage_attachments` 는 형식 판정 **전에** 내려받는다 | `files.py:455-469` | 메시지 `files[]` 에 원격 파일이 오면 바로 위 경로를 탄다 |
| DM 인계·Canvas 도 같은 함수를 쓴다 | `slack/pilot.py:1473`, `archive/canvas.py:308` | 0단계 가드는 함수 안에 둬야 세 곳이 함께 막힌다 |
| 질문 첨부 경로는 이미 `files.slack.com` 만 허용한다 | `question_files.py:22` `_DOWNLOAD_HOSTS` | 같은 규칙을 수집 경로에 옮기면 된다 |
| 첨부 정본은 `channels/<cid>/attachments/<file-id>/<revision>.md`, 채널 ACL 상속, 옛 판을 지우지 않고 최신 판만 근거 | `archive/attachment_doc.py` | Drive 문서도 이 모델에 싣는다(§7) |
| revision 은 (원본 sha256, 변환기, 판, 설정)에서 나온다 | `attachment_doc.revision_for` | Drive 는 내보낸 바이트의 sha256 을 넣으면 그대로 쓴다 |
| 암호문 저장은 DB, Fernet 키는 DB 밖 전용 파일 | `archive/image_description_secret.py`(전용 키), `console/llm_secret_store.py` | Drive 자격도 **전용 키 파일**로 같은 모양 |
| 근거 채널 화이트리스트 | `deploy/sql/bot_source_schema.sql`, `bot_access.source_channels()` | VIPBot 은 그대로. Drive 전용 권한표를 만들지 않는다 |

## 4. 전체 그림

```
 Slack 메시지 ──(Archiver 수집)──▶ 링크·원격파일 감지 ──▶ gdrive_link_sighting (좌표만)
                                                              │
 콘솔: 승인 루트(Shared Drive·폴더 ↔ workspace/channel) ─────┤
                                                              ▼
                                  ┌───────── Drive 워커 (tybot-drive 계정, Slack 토큰 없음) ─────────┐
                                  │ files.get(supportsAllDrives) → 범위·권한·제한 확인                 │
                                  │ export(Docs/Sheets/Slides) · alt=media(PDF·이미지·일반)             │
                                  │ changes.list(driveId) → 삭제·철회·revision                          │
                                  └───────────────┬────────────────────────────────────────────────────┘
                                                  ▼ staging (metadata.json + objects, 기존 형식)
                          기존 변환(tybot-convert) → sensitive_mask → pii_screen → AttachmentDoc 정본
                                                  ▼
                               search_index / embedding 색인 (기존 잡)
                                                  ▼
            TYBot: visible_docs(ctx)          VIPBot: bot_source_channel     ← 같은 정본, 다른 권한
                                                  ▼
            출처: [조직명]업무명, 📄<파일명>(Google Drive, 수정 2026-11-10 14:03) <webViewLink>
```

## 5. 0단계 — Slack 토큰 유출 차단 (독립·긴급)

Drive 작업과 별개로 **지금 열려 있는** 문제다. Drive 기능을 켜지 않아도 고친다.

`files.py` 의 다운로드 두 함수 **안에** 하나의 판정 함수를 둔다(호출부 세 곳이 따로 막지 않게).

```
def slack_download_url(raw_file: dict) -> str | None:
    거부(None):
      - raw_file["mode"] == "external"  또는  raw_file["is_external"] is True
      - raw_file["filetype"] == "remote" 또는 external_type 이 비어 있지 않음
      - URL 스킴이 https 가 아님
      - 호스트가 정확히 files.slack.com 이 아님 (접미사 비교 금지 — evilfiles.slack.com.example 차단)
    허용: url_private_download 만. url_private 으로 내려가는 것은 위 조건을 모두 통과한 hosted 파일일 때뿐
```

- 거부된 파일은 `[첨부:외부파일] <이름> (gdrive)` 한 줄과 상태 `external_skipped` 로 남긴다.
  조용히 버리지 않는다(「버린 줄은 없던 줄과 구별되지 않는다」, 원칙 5 2026-11-10)
- `SlackFile` 에 `mode`·`is_external`·`external_type` 를 싣는다. `from_event` 가 지금은 버린다
- 회귀 시험: (1) `mode=external`+`url_private=https://docs.google.com/...` 이면 요청 객체가
  만들어지지 않는다(`urlopen` 을 가짜로 바꿔 호출 0회 확인) (2) `files.slack.com.evil.test` 거부
  (3) `http://files.slack.com` 거부 (4) hosted 파일은 기존과 같다 (5) DM 인계·Canvas 경로 동일
- 리다이렉트: `urlopen` 은 3xx 를 따라가며 **헤더를 그대로 실어 보낸다**(urllib 기본 동작, 실측 필요).
  `files.slack.com` 이 다른 호스트로 리다이렉트하면 토큰이 따라간다. 리다이렉트 핸들러로
  **호스트가 바뀌면 Authorization 을 떼거나 실패**시킨다

## 6. Slack 링크 감지부터 근거 발행까지

### 6.1 감지 (Archiver, 수집 경로)

Archiver 가 메시지를 원문으로 쓸 때 **함께** 본다. 새 Slack 이벤트·스코프를 늘리지 않는다
(`link_shared` 는 앱 unfurl 도메인 등록과 `links:read` 가 필요하고, 이미 Google Drive 앱이 그
도메인 unfurl 을 쥐고 있을 때 우리 앱에도 오는지는 문서에 없다 — §14-S3).

| 신호 | 어디서 | 얻는 것 |
|---|---|---|
| 본문 텍스트·`blocks` 안 URL | 메시지 원문 | `docs.google.com/{document,spreadsheets,presentation}/d/<id>`, `drive.google.com/file/d/<id>`, `drive.google.com/open?id=<id>`, `…/drive/folders/<id>` |
| 원격 파일 객체 | `files[]` 의 `mode=external`, `external_type=gdrive` | `external_url` 을 위 규칙으로 파싱. `external_id` 는 앱이 정한 값이라 **신뢰하지 않는다** |
| 원격 파일 unfurl `attachments[]` | 메시지 | `original_url`/`from_url` 을 위 규칙으로 파싱 |

- URL 모양은 공식 규격이 없다(§14-G12). 파서는 **후보**만 낸다. 진위는 6.2 의 API 응답이 정한다
- ID 는 `^[A-Za-z0-9_-]{10,200}$` 만 받는다. 그 밖은 버리고 `id_malformed` 로 센다
- 쿼리 문자열(`resourcekey`·`usp`)은 저장하지 않는다. `resourcekey` 는 링크 공유 키다(위협 T6)
- 감지 결과는 `gdrive_link_sighting` 에 **좌표만** 쓴다: `(workspace, channel_id, message_ts, drive_file_id, source)`.
  URL 원문·제목은 넣지 않는다
- **봇 발언 속 링크는 감지하지 않는다**(원칙 1). 수집 경로가 이미 봇 발언을 거르므로 같은 판정을 쓴다
- Archiver 는 Google 자격을 갖지 않는다. 감지만 하고 끝낸다

### 6.2 범위·현재 상태 확인 (Drive 워커)

워커는 미처리 sighting 을 집어 **매번 현재 상태로** `files.get(fileId, supportsAllDrives=true,
fields=id,name,mimeType,driveId,parents,trashed,version,modifiedTime,headRevisionId,md5Checksum,
sha256Checksum,size,webViewLink,capabilities/canDownload,copyRequiresWriterPermission,
downloadRestrictions,contentRestrictions)` 를 부른다.

판정 순서(앞에서 걸리면 멈춘다):

1. 응답 404·403 → `not_accessible`. 「없다」 와 「권한 없다」 를 가르지 않는다(Drive 도 안 가른다, §14-G6)
2. `trashed=true` → `withdrawn:trashed`
3. **승인 루트 안인가**
   - Shared Drive 루트: `driveId == root.drive_id`
   - 폴더 루트: `parents` 를 따라 올라가 `root.folder_id` 를 만나면 안. 깊이 상한 20, 방문 집합으로 순환 차단.
     Shared Drive 파일은 부모가 하나지만 My Drive 파일은 여러 폴더에 있을 수 있다(§14-G8) —
     **하나라도** 승인 루트에 닿으면 안이다. 단, 닿은 루트가 둘 이상이고 결속 채널이 다르면 `ambiguous_root` 로 멈춘다
   - 밖이면 `out_of_scope`. **본문을 받지 않는다.** 콘솔에 「승인 밖 링크」 로 센다(어느 폴더를 승인할지 판단 재료)
4. 다운로드 제한 — `capabilities.canDownload=false`, `copyRequiresWriterPermission=true`,
   `downloadRestrictions` 의 실효 제한 중 하나라도 있으면 `blocked:download_restricted`.
   소유자가 「복사·다운로드 금지」 를 걸었는데 서비스 계정이 편집자라 받을 수 있더라도 **받지 않는다**(§12-D7)
5. 형식 — §6.3 표에 없으면 `unsupported` (Forms·Sites·Vids·바로가기 등)
6. 크기 — blob 은 `size` 가 상한 초과면 `too_large`. export 는 받고 나서 10MB 상한(§14-G4)에 걸리면 `export_too_large`
7. 같은 `(drive_file_id, version)` 이 이미 `active` 면 다시 받지 않는다. 출처 좌표만 갱신

**결속 채널은 루트가 정한다.** sighting 이 채널 B 에서 왔어도, 파일이 채널 A 에 결속된 루트 안이면
정본은 **채널 A 에만** 생긴다. 채널 B 원문에는 링크 줄만 남는다. 채널 B 멤버가 A 를 못 보면
그 문서도 못 본다 — 링크를 붙인 것만으로 권한이 넓어지지 않는다(§12-D3 에서 다른 안 검토).

### 6.3 받기 (Drive 워커)

| Drive 형식 (`mimeType`) | 방법 | 받는 형식 | 기존 변환기 |
|---|---|---|---|
| Google Docs `application/vnd.google-apps.document` | `files.export` | `.docx` | docx 경로(B-73 표 보존) |
| Google Sheets `…spreadsheet` | `files.export` | `.xlsx` (CSV 는 첫 시트만이라 쓰지 않는다) | xlsx 경로(B-82 분할) |
| Google Slides `…presentation` | `files.export` | `.pptx` | pptx 경로 |
| PDF·이미지·Office·HWP·텍스트 | `files.get?alt=media` | 원본 그대로 | 확장자 판정(기존 `SlackFile.from_event` 규칙) |
| 그 밖 `application/vnd.google-apps.*` | 받지 않음 | — | `unsupported` |

- export 는 10MB 상한·Range 미지원(§14-G4). 넘으면 **PDF 로 재시도하지 않는다**(같은 상한) → `export_too_large`
- 받은 바이트는 **기존 staging 형식**으로 쓴다: `metadata.json`(file_id, name, filetype, sha256, size,
  origin=`gdrive`, drive 좌표) + `objects/<name>`. 그 뒤는 Slack 첨부와 같은 길이다
- 이미지는 B-81 이미지 설명 경로로 넘어간다. **B-81 코드는 이 작업에서 고치지 않는다**(§11)
- 워커는 원본을 변환하지 않는다. 변환은 격리된 `tybot-convert` 가 한다(기존 `conversion_queue`)
- 받은 바이트 사본은 staging 의 보존 정책(B-70)을 따른다

### 6.4 정본·마스킹·색인 (기존 경로)

- 변환 결과 → `sensitive_mask.mask_lines` → `pii_screen` 마지막 관문 → `AttachmentDoc` 정본
- **파일명도 마스킹한다.** Drive 파일명에는 사람 이름·전화번호가 흔하다. DB `gdrive_file.name_masked` 와
  정본 프론트매터에 같은 마스커를 거친 값만 쓴다
- 정본 경로: `channels/<결속 cid>/attachments/gd_<drive_file_id>/<revision>.md`
  - `gd_` 접두사로 Slack `F…` ID 와 이름공간을 가른다(`_safe` 규칙 안에서 충돌하지 않음)
  - `revision = revision_for(sha256=<받은 바이트>, converter_name, converter_version, config)`.
    Docs Editors 파일은 `sha256Checksum`·`headRevisionId` 가 없으므로(§14-G8) **받은 바이트 해시**가 판의 재료다.
    같은 Drive `version` 에서도 export 바이트가 다를 수 있다(실측 필요) — 그러면 새 판이 생기지만 해롭지 않다
- 프론트매터 추가 필드(스키마 v1 에 **선택 필드**로): `source_kind: gdrive`, `drive_file_id`, `drive_version`,
  `drive_modified_time`, `drive_web_view_link`, `drive_root_id`. `message_ts` 는 비운다
- 색인은 기존 잡(`search_index`, embedding)이 정본을 줍는다. Drive 전용 색인을 만들지 않는다

### 6.5 답변·출처

- TYBot: `ArchiveStore.visible_docs(ctx)` 가 결속 채널 ACL 로 거른다. 새 판정 없음
- VIPBot: `bot_source_channel` 에 결속 채널이 등록돼 있어야만 보인다. 새 판정 없음
- 출처 한 줄(원칙 2 형식에 Drive 를 끼운다):

```
출처: [전산팀]공지, 📄2027 예산계획(Google Drive, 수정 2026-11-10 14:03) <https://docs.google.com/…/edit>
```

  - 조직명은 결속 채널의 `source_label`. 다른 워크스페이스면 `(워크스페이스)` 를 붙인다
  - 파일명은 **마스킹된 이름**, 시각은 `drive_modified_time` 을 KST 로. 수집 시각으로 대신하지 않는다
  - 링크는 `webViewLink`. `resourcekey` 쿼리는 붙이지 않는다 — 누르는 사람이 Drive 권한이 없으면
    Drive 가 막는다. 이것은 의도된 동작이다(봇은 내용을 말했지만 원본 열람은 Drive 권한을 따른다, §12-D4)
- 답변이 인용한 근거 좌표(`EvidenceRef`)에 `drive_file_id`·`revision` 이 들어간다. 후속 질문은 그 좌표를
  **현재 상태로** 다시 연다 — 그사이 철회됐으면 열리지 않는다(원칙 1 후속 규칙)

## 7. 삭제·권한 철회·revision 반영

### 7.1 무엇을 보나

루트마다 Drive 변경 로그를 따라간다.

- Shared Drive 루트: `changes.list(driveId=…, includeItemsFromAllDrives=true, supportsAllDrives=true,
  includeRemoved=true)`. 사용자 변경 로그는 Shared Drive 를 다 잡지 못하므로 **드라이브별 로그**를 쓴다(§14-G6)
- 폴더 루트(My Drive 또는 Shared Drive 안 폴더): 드라이브 로그를 따라가되 결과를 §6.2-3 범위 판정으로 거른다.
  폴더가 My Drive 에 있으면 서비스 계정 자기 변경 로그(공유받은 항목) — **포착 범위 실측 필요**
- 시작 토큰은 `changes.getStartPageToken(driveId=…)`. 토큰은 만료되지 않는다(§14-G6)
- 주기: 15분(아카이브 pull 주기와 같게). 이 주기가 철회 후 노출 창의 상한이다(§12-D5)
- 변경 로그만 믿지 않는다. **일 1회 대조**: `active` 인 파일 전부를 `files.get` 으로 다시 확인한다.
  로그가 놓친 철회를 잡는 안전망이다(분량 많으면 루트별 분산)

### 7.2 판정 → 상태

| 관측 | 상태 | 근거에서 | 정본 파일 |
|---|---|---|---|
| change `removed`/항목 접근 불가 (삭제와 접근 상실을 Drive 가 가르지 않음) | `withdrawn:not_accessible` | **즉시 뺀다** | 남김 + 철회 표식 |
| `trashed=true` | `withdrawn:trashed` | 즉시 뺀다 | 남김 + 표식 |
| 승인 루트 밖으로 이동 | `withdrawn:out_of_scope` | 즉시 뺀다 | 남김 + 표식 |
| 다운로드 제한 새로 걸림 | `withdrawn:download_restricted` | 즉시 뺀다 | 남김 + 표식 |
| 루트 승인 해제(콘솔) | 루트 아래 전부 `withdrawn:root_revoked` | 즉시 뺀다 | 남김 + 표식 |
| 서비스 계정이 루트 접근 상실 | 루트 `access_lost`, 아래 전부 `withdrawn:not_accessible` | 즉시 뺀다 | 남김 + 표식 |
| `version` 증가 | `refresh_pending` → 새로 받아 새 판 | 옛 판은 `pick_current` 가 자동으로 내림 | 옛 판 남김 |
| 다시 접근 가능 | 새로 받아 `active` | 새 판부터 | — |

- 「뺀다」 는 셋을 같이 한다: (1) DB `gdrive_file.state` (2) 정본 옆 **철회 표식 파일**
  `withdrawn.json`(코드·시각만) — reader 가 DB 없이도 거른다 (3) 색인 행 삭제 요청.
  셋 중 reader 표식이 **실제 경계**다. 색인이 늦게 지워져도 reader 가 버린다
- 정본 본문을 언제 물리적으로 지우나는 오너 결정(§12-D6). 기본안: 철회 후 30일 뒤 본문 삭제, 프론트매터·좌표·
  철회 코드는 남김(그때 왜 답이 달라졌는지 되짚을 수 있어야 함)
- **Drive 권한 변경 자체는 별도 이벤트가 없다.** 공유 드라이브 항목 권한이 바뀌면 그 항목과 하위 항목마다
  변경 기록이 남는다(§14-G6). 우리가 보는 것은 「서비스 계정이 아직 읽을 수 있나」 뿐이다.
  사람 A 의 Drive 권한이 빠졌다고 봇이 A 에게 숨기지 않는다 — 봇 권한은 **Slack 채널 기준**이다(§12-D2)

## 8. DB 표 초안

공통 규칙: 지우지 않고 상태·`removed_*` 로 닫는다. 본문·URL 쿼리·자격 평문을 넣지 않는다.
이름·코드 검사는 `bot_source_schema.sql` 과 같은 CHECK 를 쓴다.

```sql
-- 자격. 암호문만. 복호화 키는 DB 밖 /etc/tybot/gdrive-secret.key (0400, tybot-drive 소유)
CREATE TABLE gdrive_credential (
    id              bigserial PRIMARY KEY,
    label           text NOT NULL,                       -- "tybot-drive-reader"
    kind            text NOT NULL CHECK (kind IN ('service_account','oauth_user')),
    principal_email text NOT NULL,                       -- 서비스 계정 주소(공유 대상 확인용)
    ciphertext      bytea NOT NULL,                      -- Fernet(JSON 키 또는 refresh token)
    mask            text NOT NULL,                       -- 콘솔 표시용 "…a1b2 (key id 앞 4)"
    key_id_hint     text NOT NULL DEFAULT '',            -- private_key_id 앞 8자. 회전 확인용
    scopes          text[] NOT NULL,                     -- {'https://www.googleapis.com/auth/drive.readonly'}
    enabled         boolean NOT NULL DEFAULT true,
    created_by      text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    rotated_at      timestamptz,
    disabled_by     text, disabled_at timestamptz, disable_reason text
);

-- 승인 루트 ↔ 결속 채널. 루트 하나 = 채널 하나(§12-D3)
CREATE TABLE gdrive_root (
    id              bigserial PRIMARY KEY,
    kind            text NOT NULL CHECK (kind IN ('shared_drive','folder')),
    drive_id        text,                                -- shared_drive 면 필수
    folder_id       text,                                -- folder 면 필수
    display_name    text NOT NULL,                       -- 승인 시점 이름(마스킹). 표시용 사본
    workspace       text NOT NULL CHECK (workspace ~ '^[a-z][a-z0-9-]{1,23}$'),
    channel_id      text NOT NULL CHECK (channel_id ~ '^[CG][A-Z0-9]{6,}$'),
    credential_id   bigint NOT NULL REFERENCES gdrive_credential(id),
    state           text NOT NULL DEFAULT 'pending_check'
                    CHECK (state IN ('pending_check','active','paused','access_lost','revoked')),
    reason          text NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 500),
    approved_by     text NOT NULL, approved_at timestamptz NOT NULL DEFAULT now(),
    revoked_by      text, revoked_at timestamptz, revoke_reason text,
    CHECK ((kind='shared_drive' AND drive_id IS NOT NULL AND folder_id IS NULL)
        OR (kind='folder' AND folder_id IS NOT NULL))
);
CREATE UNIQUE INDEX gdrive_root_one_open ON gdrive_root (kind, coalesce(drive_id,''), coalesce(folder_id,''))
    WHERE revoked_at IS NULL;
-- 루트끼리 포함 관계(폴더 안 폴더)는 승인 시 워커 확인 단계에서 거부한다(DB 로는 못 막음)

CREATE TABLE gdrive_sync_cursor (
    root_id             bigint PRIMARY KEY REFERENCES gdrive_root(id),
    start_page_token    text NOT NULL,
    last_started_at     timestamptz, last_finished_at timestamptz, last_success_at timestamptz,
    last_reconcile_at   timestamptz,
    last_error_code     text NOT NULL DEFAULT '',
    consecutive_failures integer NOT NULL DEFAULT 0
);

CREATE TABLE gdrive_file (
    root_id          bigint NOT NULL REFERENCES gdrive_root(id),
    drive_file_id    text NOT NULL CHECK (drive_file_id ~ '^[A-Za-z0-9_-]{10,200}$'),
    name_masked      text NOT NULL,
    mime_type        text NOT NULL,
    drive_version    bigint, head_revision_id text, modified_time timestamptz,
    md5_checksum     text, sha256_checksum text, size_bytes bigint,
    web_view_link    text NOT NULL DEFAULT '',          -- 쿼리 제거본
    state            text NOT NULL CHECK (state IN ('pending','active','refresh_pending',
                         'out_of_scope','blocked','unsupported','failed','withdrawn')),
    state_code       text NOT NULL DEFAULT '',          -- download_restricted, trashed, export_too_large …
    archive_revision text NOT NULL DEFAULT '',
    first_seen_via   text NOT NULL CHECK (first_seen_via IN ('folder_sync','slack_link','slack_remote_file','backfill')),
    last_checked_at  timestamptz, withdrawn_at timestamptz,
    PRIMARY KEY (root_id, drive_file_id)
);

-- 승인 밖 링크도 여기 남는다(root 없음). 좌표만.
CREATE TABLE gdrive_link_sighting (
    id            bigserial PRIMARY KEY,
    workspace     text NOT NULL, channel_id text NOT NULL, message_ts text NOT NULL,
    drive_file_id text NOT NULL,
    source        text NOT NULL CHECK (source IN ('text_url','remote_file','unfurl')),
    outcome       text NOT NULL DEFAULT 'pending',      -- pending/ingested/out_of_scope/not_accessible/…
    resolved_root bigint REFERENCES gdrive_root(id),
    seen_at       timestamptz NOT NULL DEFAULT now(), resolved_at timestamptz,
    UNIQUE (workspace, channel_id, message_ts, drive_file_id)
);

-- 소급(백필). dry-run 결과를 먼저 저장하고, 실행은 그 행을 참조해야만 가능
CREATE TABLE gdrive_backfill_run (
    id              bigserial PRIMARY KEY,
    root_id         bigint NOT NULL REFERENCES gdrive_root(id),
    mode            text NOT NULL CHECK (mode IN ('dry_run','execute')),
    dry_run_id      bigint REFERENCES gdrive_backfill_run(id),  -- execute 는 필수
    counted_files   integer, counted_bytes bigint, counted_by_type jsonb,
    counted_blocked integer, counted_out_of_scope integer,
    state           text NOT NULL CHECK (state IN ('requested','running','done','failed','cancelled')),
    requested_by    text NOT NULL, requested_at timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz, error_code text NOT NULL DEFAULT '',
    CHECK (mode = 'dry_run' OR dry_run_id IS NOT NULL)
);

-- 감사. 추가만. 본문·URL·자격 없음
CREATE TABLE gdrive_event (
    id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
    actor text NOT NULL,                                 -- 'console:<계정>' / 'worker'
    root_id bigint, drive_file_id text, code text NOT NULL, detail jsonb NOT NULL DEFAULT '{}'
);
```

## 9. DB 권한 역할 초안

새 역할 `tybot_drive` 하나를 추가한다. 나머지는 기존 역할에 최소 권한만 더한다.

| 역할 | 누가 | 허용 | 금지 |
|---|---|---|---|
| `tybot_console` | 콘솔 | `gdrive_root` INSERT/UPDATE(승인·해제), `gdrive_credential` INSERT/UPDATE(**`ciphertext` 는 쓰기만**, 열 단위 SELECT 에서 제외), 상태 표 SELECT, `gdrive_backfill_run` INSERT, `gdrive_event` INSERT | `gdrive_credential.ciphertext` SELECT, `gdrive_file` 쓰기 |
| `tybot_drive` (신규) | Drive 워커 | `gdrive_credential` SELECT(enabled 행), `gdrive_root` SELECT·`state` UPDATE, `gdrive_file`·`gdrive_sync_cursor` 쓰기, `gdrive_link_sighting` SELECT·`outcome` UPDATE, `gdrive_backfill_run` UPDATE, `gdrive_event` INSERT | Slack·워크스페이스 토큰 표 전부, 다른 시크릿 표, 답변·QA 표 |
| `tybot_archiver` | 수집 | `gdrive_link_sighting` INSERT, `gdrive_file` SELECT(정본 발행 좌표) | 자격·루트 쓰기 |
| `tyslackai` (TYBot·VIPBot 답변) | 답변 | 뷰 `gdrive_evidence_state`(root_id, drive_file_id, state, archive_revision) SELECT — 진단·콘솔 추적용 | 원본 표 직접 접근, 자격 |
| `tybot_vipbot` | VIPBot | 위 뷰 SELECT 만 | 그 밖 |

- OS 계정도 가른다: `tybot-drive` 사용자, `/etc/tybot/gdrive-secret.key` 0400 소유자 `tybot-drive`.
  봇·콘솔 프로세스는 그 키 파일을 읽지 못한다. 콘솔은 **암호화만** 한다 — 공개키 봉인 방식이면 더 낫다(§12-D8)
- 워커에는 Slack 토큰 환경변수를 주지 않는다. systemd 유닛에서 `TYBOT_ENV_FILE` 대신 전용 env 파일

## 10. 콘솔

화면 하나: **콘솔 > 근거 소스 > Google Drive**

| 영역 | 보이는 것 | 동작 |
|---|---|---|
| 연결 상태 | 자격 라벨·`principal_email`·마스크·켜짐·마지막 성공 호출·회전일 | 자격 등록/교체/사용 중지(키 파일 업로드 → 즉시 암호화, 화면에 다시 나오지 않음) |
| 승인 루트 목록 | 종류·이름·결속 채널(조직명)·상태·마지막 동기화·연속 실패·active/withdrawn/blocked 수 | 추가(폴더·드라이브 URL 붙여넣기 → **연결 확인**: 서비스 계정이 읽히는지·하위 루트 겹침), 일시정지, 승인 해제(사유 필수) |
| 결속 경고 | 「이 채널 멤버 N명은 이 폴더 내용을 봇을 통해 보게 됩니다」 | 승인 시 체크박스 확인 필수 |
| 실패 | 코드별 수(`export_too_large`·`unsupported`·`failed`) + 파일 좌표 | 재시도(코드별, 정책 실패는 재시도 불가 — `conversion_queue` 와 같은 규칙) |
| 철회 | 최근 철회 파일·코드·시각 | 읽기만 |
| 승인 밖 링크 | 채널별 `out_of_scope` sighting 수 상위 폴더(서비스 계정이 볼 수 있을 때만) | 읽기만 — 승인 판단 재료 |
| 소급 | 루트 선택 → **dry-run**(파일 수·용량·형식별·차단 예상) → 결과 확인 후 실행 | 실행은 dry-run 행 참조 필수, 하루 상한 |

CLI 는 같은 함수를 부른다(`python -m tybot.gdrive status|dry-run|sync --root N`). 콘솔이 정본 진입점이다.

## 11. 단계별 구현 분담안

Codex 는 B-81 이미지 작업 중이다. **B-81 파일(`image_description_store.py`, 그 시험)과 B-81 백로그 세부는
이 작업에서 손대지 않는다.** 이미지 Drive 파일은 B-81 이 받는 staging 형식에 맞춰 넘기기만 한다.

| 단계 | 내용 | 산출물 | 담당 제안 | 선행 |
|---|---|---|---|---|
| **0** | Slack 다운로드 호스트 가드·리다이렉트 헤더 제거·`external_skipped` 표기 | `files.py`, 시험 | **Claude, 즉시** | 없음 |
| 1 | 스키마·역할 SQL, `DriveClient` 프로토콜 + **가짜 클라이언트**(시험 전용), 자격 저장소(전용 Fernet 키) | `deploy/sql/gdrive_schema.sql`, `src/tybot/gdrive/{client,secret,store}.py` | Claude | 0, 오너 D1 |
| 2 | 링크·원격파일 감지(Archiver), ID 파서, `gdrive_link_sighting` | `gdrive/detect.py`, Archiver 훅 | Claude | 1 |
| 3 | 범위·제한 판정(§6.2), 받기(§6.3), staging 출력 | `gdrive/resolve.py`, `gdrive/fetch.py` | Codex (B-81 종료 후, 변환 경로 친숙) | 2 |
| 4 | 정본 발행 연결(`source_kind: gdrive` 프론트매터, 출처 형식, `EvidenceRef` 확장) | `attachment_doc.py`, `answer.py` 출처 | Claude | 3 |
| 5 | 변경 로그 동기화·일일 대조·철회 표식·색인 삭제 | `gdrive/sync.py`, reader 필터 | Codex | 4 |
| 6 | 콘솔 API·화면(§10), 소급 dry-run/실행 | `console/gdrive_routes.py`, 화면 | Claude | 1, 5 |
| 7 | systemd 유닛(`tybot-drive.service`/`.timer`, **설치 시 비활성**), 배포 스크립트 | `deploy/` | Codex | 6 |
| 8 | **운영 착수**: 오너·인프라가 자격 생성, 방화벽(egress), 시범 루트 1개, 서버 검증 | 검증 기록 | 오너 + 인프라 + Claude | 7, §12 전부 |

- 1~7 단계 시험은 **가짜 Drive 클라이언트로만** 돈다. 실제 Google 호출·운영 자격 생성은 8단계에서 사람이 한다
- 각 단계 시험에 넣을 불변식: (a) 워커 프로세스 env 에 Slack 토큰 없음 (b) 승인 밖 파일 본문 요청 0회
  (c) 다운로드 제한 파일 본문 요청 0회 (d) 철회 후 reader 가 정본을 버림 (e) VIPBot 은 결속 채널이
  `bot_source_channel` 에 없으면 못 봄 (f) 로그·감사·DB 어디에도 자격 평문·`resourcekey` 없음

## 12. 오너가 결정할 항목

| # | 질문 | 선택지 | 기본안(권고) |
|---|---|---|---|
| D1 | **자격 종류** | (a) 서비스 계정 JSON 키 (b) 전용 Google 사용자 계정 OAuth refresh token (c) Workload Identity Federation(X.509·OIDC, 키 파일 없음) | **(a)로 시작, (c) 검토 병행.** Google 은 사용자 관리 키를 피하라고 권한다(§14-G10). 사내 IdP 로 (c)가 되면 옮긴다. (b)는 퇴사·비밀번호 정책에 묶여 운영이 약하다 |
| D2 | **Drive 권한과 Slack 권한이 다를 때** | (a) 결속 채널 멤버면 본다(승인자가 그 범위를 보증) (b) 질문자 이메일이 Drive 권한 목록에 있어야 본다 | **(a).** (b)는 `permissions.list` 수집·사번 매핑(B-05)이 선행이고, 그룹 공유를 풀 수 없다. 대신 승인 화면 경고·사유 필수 |
| D3 | 루트 하나에 채널 여러 개 / 다른 채널에 링크된 승인 파일 | (a) 루트=채널 1개, 링크 채널로 넓히지 않음 (b) 여러 채널 결속 허용 | **(a).** 넓히려면 루트를 추가 승인 |
| D4 | 출처 링크를 누른 사람이 Drive 권한이 없을 때 | 그대로(Drive 가 막음) / 출처에 「원본 열람은 Drive 권한 필요」 문구 | 문구 추가 |
| D5 | 철회 반영 주기 | 15분 / 5분 / 1시간 | 15분 + 일 1회 전수 대조 |
| D6 | 철회 정본 본문 물리 삭제 시점 | 즉시 / 30일 / 보존기간(B-70) 따름 | 30일 |
| D7 | 다운로드 제한 파일 | 받지 않음 / 서비스 계정이 받을 수 있으면 받음 | **받지 않음** |
| D8 | 콘솔이 자격을 받는 방식 | 콘솔이 대칭키로 암호화(키 파일을 콘솔도 읽음) / 공개키 봉인(콘솔은 공개키만, 워커만 개인키) | 공개키 봉인 |
| D9 | 시범 범위 | Shared Drive 1개 / 폴더 1개 | VIPBot 근거 채널에 결속할 Shared Drive 1개 |
| D10 | Drive 라벨(분류) 기반 제외 | 안 씀 / 「기밀」 라벨이면 제외 | 시범 후 결정. `includeLabels` 로 읽을 수 있다(§14-G8) |
| D11 | 서비스 계정을 Shared Drive 멤버로 넣을 수 있나 | — | **인프라 확인 필요.** 서비스 계정 주소는 `*.iam.gserviceaccount.com` 이라 Workspace 외부 공유 정책에 막힐 수 있다. 공식 문서에 Shared Drive 멤버십 명시 없음(§14-G7) |
| D12 | 방화벽 egress | `www.googleapis.com`·`oauth2.googleapis.com` 443 | 인프라 요청서 필요(DMZ 서버) |
| D13 | Google Cloud 프로젝트 소유·키 회전 책임자 | — | 지정 필요. 키는 만료가 없다(§14-G10) — 회전 주기 90일 권고 |

## 13. 하지 않는 것 (초기 범위)

- 개인 My Drive **전체** 수집 — 개인 작업공간은 DM 설계(B-57)의 영역이고, 동의 없는 전수 수집이 된다
- domain-wide delegation — 슈퍼관리자 권한으로 모든 사용자를 가장한다. 사고 반경이 회사 전체다
- Drive 에 쓰기(댓글·파일 생성) — 읽기 전용
- `link_shared` 이벤트·unfurl 대체 — 스코프·도메인 등록이 늘고, Google Drive 앱과 겹칠 때 동작이 문서에 없다
- 답변 시점 Drive 실시간 조회 — 답변 경로에 외부 호출·자격을 넣지 않는다. 신선도는 동기화 주기로 보장
- Drive 파일 권한 목록으로 사람별 필터(D2-b) — 초기에는 하지 않는다

## 14. 공식 문서 (2026-11-18 열람)

Slack
- S1 파일 객체 — `is_external`, `external_type`(gdrive 등), `external_url`, `mode`(hosted/external/snippet/post):
  https://docs.slack.dev/reference/objects/file-object
- S2 파일 다루기 — 원격 파일은 포인터, `url_private` 은 임의 호스트, 「Slack 자격증명을 Slack 밖 호스트로 보내지 말 것」, `url_private_download` 대개 없음:
  https://docs.slack.dev/messaging/working-with-files
- S3 `link_shared` — `links:read`, 등록 도메인만: https://docs.slack.dev/reference/events/link_shared

Google Drive API v3
- G4 다운로드·내보내기(export 10MB, `canDownload` 확인, Range 불가): https://developers.google.com/drive/api/guides/manage-downloads
- G5 내보내기 형식(Sheets CSV 첫 시트만): https://developers.google.com/drive/api/guides/ref-export-formats
- G6 변경 추적(삭제·접근 상실 미구분, 토큰 만료 없음, Shared Drive 는 드라이브 로그 재생):
  https://developers.google.com/drive/api/guides/about-changes ·
  https://developers.google.com/drive/api/reference/rest/v3/changes/list
- G7 Shared Drive 지원(`supportsAllDrives`): https://developers.google.com/drive/api/guides/enable-shareddrives
- G8 파일 리소스(`headRevisionId` blob 전용, `version`, `sha256Checksum` Docs 미제공, `downloadRestrictions`, `copyRequiresWriterPermission`, `labelInfo`):
  https://developers.google.com/drive/api/reference/rest/v3/files ·
  https://developers.google.com/drive/api/guides/about-files
- G9 스코프(`drive.readonly`·`drive.metadata.readonly` restricted, `drive.file` non-sensitive):
  https://developers.google.com/drive/api/guides/api-specific-auth

Google Cloud IAM · Workspace
- G10 서비스 계정·키 관리(키 만료 없음, 사용자 관리 키 회피, WIF):
  https://docs.cloud.google.com/iam/docs/service-account-overview ·
  https://docs.cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys
- G11 자격 만들기·domain-wide delegation·서비스 계정 직접 공유:
  https://developers.google.com/workspace/guides/create-credentials
- G12 Drive URL 의 ID 모양 — **공식 규격 없음**. 파일 ID 는 「불투명한 고유 ID」(G8 about-files)

### 문서로 확인하지 못해 실측이 필요한 것
- Google Drive 앱이 unfurl 을 쥔 도메인에서 우리 앱에 `link_shared` 가 오는지(쓰지 않으므로 영향 없음)
- 서비스 계정의 Shared Drive 멤버 추가 가능 여부(조직 정책)
- My Drive 폴더 루트의 변경이 서비스 계정 변경 로그에 얼마나 잡히는지
- 같은 Docs `version` 에서 export 바이트가 안정적인지
- `urllib` 리다이렉트 시 Authorization 헤더 전달 여부(0단계 시험으로 확정)
