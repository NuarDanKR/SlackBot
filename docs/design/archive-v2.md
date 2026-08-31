# 아카이브 v2

## 목적

원문 보존과 검색 효율을 함께 지킨다. 원문은 날짜별로 작게 나누고, 첨부 추출문과
봇이 만든 요약은 원문 검색 계층에 넣지 않는다.

## 디렉터리

```text
/var/lib/tybot/
├── archive/
│   └── workspaces/<workspace>/channels/<channel-id>__<initial-name>/
│       └── raw/YYYY-MM-DD.md
├── staging/
│   └── workspaces/<workspace>/channels/<channel-id>/attachments/<file-id>/
│       ├── metadata.json
│       └── extracted.md
├── objects/
│   └── workspaces/<workspace>/channels/<channel-id>/attachments/<file-id>/<original>
└── derived/
    └── summaries/<workspace>/<channel-id>/YYYY-MM-DD.md
```

- Slack 채널 ID가 소유 키다. 채널명이 바뀌어도 기존 ID 디렉터리를 계속 쓴다.
- `raw/`만 `ArchiveStore`의 답변 근거다.
- `staging/`의 추출문은 사람 검수 전 상태다. 자동 검색·자동 승격하지 않는다.
- `objects/` 원본은 검색하지 않으며 OS 권한과 백업 정책을 별도로 적용한다.
- `derived/`는 후보 탐색·정기 리포트용이다. 최종 답변은 반드시 `raw/`를 다시 조회해 인용한다.

## 원문 스키마

```yaml
---
schema_version: 2
workspace: tyit
channel: "#팀-전산_ABB110-회의"
channel_id: C0123456789
source_date: 2026-08-31
visibility: private
acl: [#팀-전산_ABB110-회의]
share_with: []
doc_count: 12
last_ingested: 2026-08-31T17:00+09:00
---

## 원문 (자동 취합, 편집 금지)
> [2026-08-31 09:15] 홍길동: ...
```

v2 원문에는 자동 요약 섹션을 두지 않는다. 사람 관리 문서가 필요하면 원문과 별도 계층에 둔다.

## 전환 절차

채널 매핑 파일은 저장소 밖에 둔다.

```json
{
  "tyit": {
    "#팀-전산_ABB110-회의": "C0123456789"
  }
}
```

```bash
# 1. 권장: 서버의 Slack 설정으로 채널 ID를 조회해 드라이런. 어떤 파일도 쓰지 않는다.
python scripts/migrate_archive_v2.py \
  --archive /var/lib/tybot/archive \
  --discover-slack \
  --export-channel-map /var/lib/tybot/config/channel-map.json

# Slack 조회 대신 저장소 밖의 매핑 JSON을 사용할 수도 있다.
python scripts/migrate_archive_v2.py \
  --archive /var/lib/tybot/archive \
  --channel-map /var/lib/tybot/config/channel-map.json

# 2. 매핑 JSON과 unresolved_channels, blocked_files, broken_files를 사람이 확인
#    채널명이 바뀌었거나 과거 이름이 다른 채널에서 재사용됐으면 ID를 직접 바로잡는다.

# 3. 비파괴 복사
python scripts/migrate_archive_v2.py \
  --archive /var/lib/tybot/archive \
  --channel-map /var/lib/tybot/config/channel-map.json \
  --apply
```

`--apply`도 기존 `archive/channels/`를 이동·수정·삭제하지 않는다. v1과 v2는 동시에 읽히며,
동일 원문은 논리 채널 병합 과정에서 한 번만 답변 근거로 사용된다. 검증과 백업이 끝난 뒤에도
v1 삭제는 별도 사람 승인 작업으로 다룬다.
