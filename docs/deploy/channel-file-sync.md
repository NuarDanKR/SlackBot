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
