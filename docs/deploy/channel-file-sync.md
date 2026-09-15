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
경우 다시 다운로드하지 않는다.

실패 문서는 콘솔의 `수집 > 아카이브 진단`에서 확인한다. `.xlsb`는 아직 지원하지
않으며 반복 실행해도 변환되지 않는다.
