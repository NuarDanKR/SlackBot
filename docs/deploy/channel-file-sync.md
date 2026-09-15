# 채널 파일 탭 백필

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
