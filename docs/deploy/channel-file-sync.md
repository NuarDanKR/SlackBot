# 채널 파일 탭 백필

## Archiver 운영 채널: 읽기 전용 누락 진단 (2026-10-15)

이 절 아래의 `sync_channel_files.py`는 Master용 도구다. Archiver가 인수한
채널에서 소유권 검사를 우회하거나 Master 토큰으로 강제 실행하지 않는다.

Archiver 채널은 다음 명령으로 먼저 파일 목록과 현재 staging 기록을 대조한다.
등록된 Archiver 자신의 DB 토큰을 사용하며, 실제 Slack 신원·현재 채널 참여·DB
수집 허용 상태를 모두 확인한다. DB 멤버십·마지막 이벤트 시각도 갱신하지 않는다.
지원 레이아웃은 `per-channel-v1`이다.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/preview_archiver_files.py \
  --workspace mgmt --channel C0C3986F9UZ
```

- `list_complete`: 목록 페이지를 끝까지 읽었는지. false이면 전체 개수로 단정하지 않는다.
- `files_seen`: 채널 파일 목록의 중복 제거 개수. 특정 폴더의 개수가 아니다.
- `live_metadata_records`: 현재 운영 staging에 기록이 있는 개수. 보관·변환 성공이 아니다.
- `shadow_only_ids`: 현재 그림자 staging에만 기록이 있는 파일 ID.
- `missing_metadata_ids`: 확인한 두 staging 위치 모두 기록이 없는 파일 ID.
- `generated_excluded`: 봇 생성 Canvas로 기존 스캐너가 제외한 개수.
- `warning_count`: 페이지 조회 경고 수. 민감한 SDK 응답은 출력하지 않는다.

종료 코드는 0=목록 조회 완료, 1=설정·권한·실행 오류, 2=목록 불완전이다.
레거시 staging, 다른 아카이브 루트, 정본만 남은 파일은 확인하지 않으므로
`missing_metadata_ids`를 서버 전체에서의 파일 부재나 즉시 재다운로드 대상으로
취급하지 않는다. ID 기반 복구 계획을 검토하는 입력으로만 사용한다.

**현재 다운로드·변환·저장·색인·ACK 변경은 하지 않는다. `--apply`는 없다.**
메시지 조회로 발견되지 않는 파일을 실제 복구하려면 다음을 먼저 연결해야 한다.

1. 기존 정본·객체·레거시 staging까지 대조하고, 실패한 기존 기록은 재시도 큐로 보낸다.
2. 매 쓰기 시 Archiver 소유권·운영 게이트·현재 Slack 공유 권한을 재확인한다.
3. 기존 PII·변환 경로를 사용하되 파일 링크·file ID를 파일 기반 출처로 보존한다.
   Slack 메시지·작성자·메시지 timestamp를 만들어 raw에 넣지 않는다.
4. 원본 보관·정본 반영·색인을 따로 검증한다. 파일 단위 상태를 기존 메시지 ACK와
   혼동하지 않는 계약과 장애 재개·중복 방지 테스트가 필요하다.

## Archiver 파일 ID 명시형 복구

`recover_archiver_files.py`는 위 진단에서 누락된 파일을 추가 대조한 뒤 복구한다.
자동으로 전 채널을 수집하지 않는다. 먼저 대표 파일 한 개로 확인한다.

2026-10-15 보완: 레거시 `workspaces/<workspace>`가 있다는 이유만으로 이
명령을 막지 않는다. 수집기 기동용 검사는 변경하지 않고, 복구 명령은 파일별
레거시 기록 대조로 중복 쓰기를 막는다. 기존 경로는 삭제·이동하지 않는다.
오류의 `stage`는 중단 단계를 표시하며 비밀 값이나 SDK 응답 본문은 출력하지 않는다.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/recover_archiver_files.py \
  --workspace mgmt --channel C0C3986F9UZ --file-id F0C313QLR1R
```

기본은 읽기 전용이다. `missing`이면 확인한 경로에 기존 기록이 없고,
`resume`이면 이 복구 경로가 만든 영수증이 있어 이어갈 수 있다.
`files.info`로 현재 채널 공유를, `users.info`로 사람이 올린 파일인지 확인한다.
업로더 확인 실패·봇 업로드는 막는다. 기존 Archiver manifest의 `users:read`와
`files:read`를 사용하며 새 Slack 권한은 추가하지 않는다.
`existing-needs-review`는 기존 정본·객체·staging·raw의 파일 ID 참조를 발견한
경우로, 덮거나 다시 받지 않는다. 실패한 기존 변환은 기존 재시도 경로로 다룬다.
비표준 아카이브 위치는 자동 탐색하지 않는다. 운영자가 별도 경로를 사용했다면
먼저 수동 대조한다.

실제 적용은 해당 워크스페이스 수집기를 잠시 중지한 유지보수 시간에 실행한다.
같은 DB advisory 실행 락을 사용하므로 수집기가 떠 있으면 `AlreadyRunning`으로
중단하며, 두 경로가 동시에 같은 첨부를 처리하지 않는다. 수집기와 같은 DB DSN과
동일 OS 계정으로 실행해야 한다. 락을 우회하지 않는다.

```bash
sudo systemctl stop tybot-archiving-shadow@mgmt.service

sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/recover_archiver_files.py \
  --workspace mgmt --channel C0C3986F9UZ --file-id F0C313QLR1R \
  --apply --by dan@taeyoung.com

sudo systemctl start tybot-archiving-shadow@mgmt.service
sleep 5
sudo systemctl is-active --quiet tybot-archiving-shadow@mgmt.service
echo "collector_active_rc=$?"
```

복구가 실패해도 수집기는 반드시 다시 시작한다. 중지·재시작 시각을 기록하고,
그 구간의 메시지는 기존 메시지 소급 작업으로 보충한다. 이 복구 명령 자체는
중지 구간 메시지를 소급하지 않으며, 수집기를 자동으로 시작·중지하지도 않는다.

적용 결과의 뜻:

- `published-index-pending`: 원본 SHA-256 검증 및 정본 저장 완료, DB 색인 미실행.
- `retained-not-searchable`: 원본은 확인했으나 PII 차단·부분 변환·변환 실패 등으로
  일반 검색 근거로 게시하지 않음. `conversion_state`를 확인한다.
- `original-unverified`: 원본 보관 또는 SHA-256 확인 실패. 수집 완료가 아니다.
- `incomplete-needs-review`: 중단 흔적이 있으나 안전한 자동 재개 조건이 부족하다.
- `failed`: 해당 파일 처리 실패. 출력에는 예외 종류만 남긴다.

파일별 영수증은 `<채널 루트>/file-inventory/<file-id>.json`에 쓴다.
file ID·Slack 파일 링크·조회 시각·작업자·단계 상태만 남기며 원문 본문은 넣지
않는다. `staging` 및 `archive/attachments`는 기존 변환·정본 작성기를 사용한다.
메시지 원문을 만들거나 기존 raw·메시지 ACK를 수정하지 않는다.

색인은 Archiver 권한을 넓히지 않고 기존 TYBot DB 자격증명으로 분리 실행한다.
이 모드는 Slack 토큰을 읽지 않으며, 지정한 복구 파일의 원본 해시와 정본 좌표를
확인한 뒤에만 검색 색인에 넣는다.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/recover_archiver_files.py \
  --workspace mgmt --channel C0C3986F9UZ --file-id F0C313QLR1R --index-only
```

`recovered`이면 색인까지 완료한 것이다. 같은 명령을 반복해도 원본을 다시
다운로드하지 않는다. 색인 단계 오류는 해당 파일만 같은 명령으로 재시도한다.
`--file-id`를 반복하면 최대 100개를 명시할 수 있다. 반환 코드 2는 일부 파일에
검토·추가 처리가 필요함을 뜻한다. 대표 파일 검증 뒤 나머지 9개를 명시해 확장한다.
파일 검색 복구와 별개로, 특정 Slack 폴더의 정확한 개수를 답하는 도구는 아직 없다.

## 파일 복구 원본의 지정 재변환 (서버 검증 대기)

이 절은 파일 복구 영수증이 있는 파일에만 적용한다. 원본 다운로드나 메시지 원문
반영을 하지 않는다. 기존 메시지 첨부의 자동 재변환 큐를 대체하지 않는다.
PDF 페이지별 OCR 변경은 Claude 인계 후 2026-10-16 통합 보완했다. 실제 서버 OCR
검증은 아직 하지 않았으므로 대표 파일부터 적용한다.

기본 재변환 계획은 읽기 전용이다.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/recover_archiver_files.py \
  --workspace mgmt --channel C0C3986F9UZ --file-id F0C313QLR1R --reconvert
```

계획 확인 후 위 복구와 동일하게 해당 수집기를 중지하고, 같은 명령에
`--apply --by <작업자>`를 붙인다. 실패해도 반드시 수집기를 다시 시작하고 중지
구간 메시지는 기존 메시지 소급으로 보충한다. 동일 collector 실행 락을 유지한다.

재시작 직후 `active` 한 줄만으로 정상 기동을 확정하지 않는다. 5초 뒤 상태와
최근 journal을 확인한다. 복구 도구와 달리 수집기의 레거시 경로 기동 검사는
변경하지 않았다. 해당 검사로 서비스가 중단되면 레거시 폴더를 삭제하거나
권한·운영 게이트를 우회하지 말고 별도로 원인을 확인한다.

원본 SHA-256을 확인하고 변환·PII 검사를 다시 수행한다. 결과는
`staging/<file-id>/reconversions/<generation>/`에 저장한다. 초기 `metadata.json`과
`extracted.md`는 그대로 두고, 복구 영수증의 `metadata_relpath`가 현재 이력을
가리킨다. **초기 메타데이터가 계속 partial인 것은 이 구조에서 정상**이다.
변환 도중 실패하면 이전 이력이 유지된다. 정본 게시 실패 이후에는 `--reconvert`를
빼고 `--apply --by ...`로 재개하면 OCR을 반복하지 않는다.

`published-index-pending` 이후 기존 `--index-only`로 색인한다. 해당 모드도
영수증의 현재 이력을 읽고, 그 SHA-256·변환기 판에서 계산한 revision만 색인한다.
partial/unknown/PII 차단 결과는 성공으로 바꾸지 않는다. 이전 정본도 덮지 않는다.

## 전체 워크스페이스 읽기 전용 누락 진단

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/audit_archiver_files.py \
  --all-workspaces --pause 3 --result-json /tmp/archiver-file-audit.json
```

수집기를 중지할 필요가 없다. `bot_connection`에 등록된 Archiver를 기준으로
워크스페이스별 자격증명을 불러오므로 Master가 없는 TEC도 포함한다.
비활성·신원 미확인·설정 오류 연결은 성공 0건으로 숨기지 않고 workspace failed로
남긴다. 등록 자체가 없는 워크스페이스는 자동으로 발견·설치하지 않는다.
특정 워크스페이스만 보려면 `--all-workspaces` 대신 `--workspace mgmt`를 사용한다.

실제 참여 채널과 DB 수집 허용 채널의 교집합만 조회한다. DM·중지 채널·operator
hold 채널은 수집 대상으로 만들지 않고, 멤버십을 자동 등록·갱신하지 않는다.
진행 로그는 stderr에 workspace/channel ID와 상태만 표시하고 결과 JSON에는 토큰·
문서 본문·파일명은 넣지 않는다. 보고서 파일은 아카이브 밖에만 쓸 수 있으며,
기존 파일을 덮지 않는다. Unix에서는 새 보고서를 0600으로 생성한다.

- `complete`: 전체 조회 성공 여부. false이면 전체 파일 수·누락 수로 단정하지 않는다.
- `db_joined_not_in_slack`: DB는 허용하지만 실제 참여 목록에는 없는 채널.
- `missing_after_record_check`: 현재 두 staging뿐 아니라 기존 정본·객체·레거시
  staging·raw 파일 ID 참조도 확인한 뒤 기록이 없는 파일. 비표준 경로는 별도 확인한다.
- `existing_needs_review`: 현재 staging은 없지만 다른 기존 기록이 발견된 파일.
- `skipped_channels`: 참여했지만 DB 수집 정책이 허용하지 않아 조회하지 않은 채널 수.

이것은 **채널 파일 진단**이지 특정 폴더 개수 조회가 아니다. 목록에 있는 모든 파일이
수집 대상인 것도 아니다. 실제 복구 단계에서 사람 업로더·공유 권한·PII를 다시
검사한다. 다운로드·변환·복구·멤버십 변경을 자동 실행하지 않는다.
종료 코드는 0=전체 완료, 2=부분 실패·설정 불가 연결·빈 등록 목록, 1=실행 오류다.

## Master용 기존 절차

봇 초대 전 파일과 메시지 이벤트에서 놓친 파일을 채널별 `files.list`로 찾는다.
전체 워크스페이스 파일 목록은 요청하지 않으며, 봇이 참여한 수집 대상 채널만 본다.

## 조회

기본 실행은 목록만 읽고 파일을 다운로드하지 않는다.

```bash
sudo -u tybot /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/sync_channel_files.py --workspace tyit
```

한 채널만 확인할 수도 있다.

```bash
sudo -u tybot /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/sync_channel_files.py \
  --workspace tyit --channel C0BQUGRHV2A
```

## 적용

조회 결과의 `미수집` 건수를 확인한 뒤 같은 명령에 `--apply`를 붙인다.

```bash
sudo -u tybot /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/sync_channel_files.py \
  --workspace tyit --channel C0BQUGRHV2A --apply
```

적용하면 기존 첨부 파이프라인을 통해 원본 격리, 문서 변환, PII 검사, 원문 반영
확인과 검색 재색인을 수행한다. 같은 Slack `file_id`가 메시지 첨부에서 이미 처리된
경우 다시 다운로드하지 않는다. 파일마다 이름·크기·진행 결과를 출력하고 한 건씩 원문
반영과 색인을 끝내므로, 오래 걸려 중단해도 같은 명령으로 나머지를 이어서 처리할 수 있다.

실패 문서는 콘솔의 `수집 > 아카이브 진단`에서 확인한다. `.xlsb`는 아직 지원하지
않으며 반복 실행해도 변환되지 않는다.

## 다른 워크스페이스

`--workspace`를 반복하면 지정한 워크스페이스를 함께 처리한다. 옵션을 생략하면 DB에
등록된 모든 워크스페이스에서 봇이 참여한 수집 대상 채널을 처리한다.

```bash
# 지정한 워크스페이스들
sudo -u tybot /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/sync_channel_files.py \
  --workspace pilot --workspace mgmt --workspace tyit --apply

# 등록된 모든 워크스페이스
sudo -u tybot /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/sync_channel_files.py --apply
```

## 초대 이전 메시지까지 소급

파일 탭뿐 아니라 초대 이전 메시지, 스레드 답글, 현재 Canvas까지 함께 복구하려면
`backfill_channel_history.py`를 쓴다. 기본 실행은 대상 확인뿐이고 `--apply`가 있어야
원문을 변경한다.

```bash
# 한 채널
sudo -u tybot /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/backfill_channel_history.py \
  --workspace tyit --channel C0BQUGRHV2A --apply

# 등록된 모든 워크스페이스의 참여 채널
sudo -u tybot /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/backfill_channel_history.py --apply
```

진행 위치는 `/var/lib/tybot/history-backfill.json`에 저장되므로 중단되면 같은 명령을
다시 실행한다. 비공개 채널은 먼저 봇을 초대해야 하며, Slack 보존 정책으로 이미 API에서
사라진 메시지와 Canvas의 과거 수정 이력은 복원할 수 없다.
