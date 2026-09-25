# Archiving Bot 그림자 수집 준비

기준: [Archiving Bot 분리 결정](../design/archiving-bot-separation-2026-09-23.md)
상태: 개발/비교 전용. 운영 writer 전환 절차가 아니다.

Slack 계약: [Socket Mode](https://docs.slack.dev/tools/bolt-python/concepts/socket-mode),
[채널별 message 이벤트](https://docs.slack.dev/reference/events/message/),
[`auth.test`](https://docs.slack.dev/reference/methods/auth.test/).

## 사전 조건

- 워크스페이스마다 **TYBot과 다른 Slack 앱**을 설치한다.
  [Archiving 앱 매니페스트](../pilot/archiving-app-manifest.yaml)를 사용한다.
  공개·비공개 채널은 앱을 해당 채널에 초대하고 읽기 권한을 확인한다.
- Socket Mode 앱 토큰과 봇 토큰을 발급한다. 채널 읽기·첨부 읽기만
  사용하며 이 앱에는 `chat:write`나 요약 검토 DM 권한을 주지 않는다.
  앱 토큰에는 Slack의 `connections:write` scope가 필요하다.
- TYBot bot user ID를 확인한다. Archiving Bot은 시작 때
  `auth.test` 결과의 team ID가 설정과 다르거나 user ID가 TYBot과
  같으면 기동을 거절한다.
- Slack 토큰과 채널 목록은 콘솔 DB에서 관리한다. 독립 env 파일에는 Archiver
  전용 DB 접속 정보와 `WORKSPACE_SECRET_KEY`만 두고, TYBot 마스터 토큰이나 LLM
  키는 넣지 않는다. DB 역할은 `archiver_runtime_config()` 실행과 Archiving 상태
  표 권한만 가진다. 운영 ArchiveStore 경로와 그림자 경로는 서로 달라야 한다.

예시: `tyit` 워크스페이스를 준비한다. Slack 앱 토큰은 Workspace 상세의
Archiving 서비스 연결에서 저장하고 신원확인을 통과시킨다.

```text
# /etc/tybot/archiver-tyit.env (root:tybot, 0640)
ARCHIVER_CONFIG_SOURCE=db
ARCHIVER_WORKSPACE=tyit
DATABASE_URL=<archiver-runtime-postgresql-dsn>
WORKSPACE_SECRET_KEY=<same-fernet-key-used-by-console>
ARCHIVE_DIR=/var/lib/tybot/archive
ARCHIVER_SHADOW_DIR=/var/lib/tybot/archiver-shadow/tyit/archive
```

서버 관리자는 사용자·권한을 확인하고 다음을 실행한다. 파일 내용은
명령행 인자로 넘기지 않는다. unit에는 `[Install]`이 없어 자동
활성화되지 않는다.

```bash
sudo install -d -o tybot -g tybot -m 0750 /var/lib/tybot/archiver-shadow/tyit
sudo install -m 0644 /opt/tybot/deploy/tybot-archiving-shadow@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start tybot-archiving-shadow@tyit.service
sudo systemctl status tybot-archiving-shadow@tyit.service --no-pager
sudo journalctl -u tybot-archiving-shadow@tyit.service -n 100 --no-pager
```

수집 여부는 그림자 경로에서만 확인한다. 기존
`tybot.service`, `tybot-collect.timer`, 변환 timer와 PF GCP
Hermes는 그대로 둔다. 첨부 분리 스위치가 꺼져 있으면 기존 TYBot 형식과
비교하기 위해 추출 본문을 shadow raw에도 둔다. 스위치는
`attachment_reader_ready`가 확인된 뒤에만 켤 수 있고, 켜지면 첨부 정본을 먼저
쓴 뒤 raw에는 참조만 남긴다. TYBot 검색에는 아직 연결하지 않는다.
처음에는 최대 5개 채널 ID만 허용한다. 목록 밖 이벤트는 채널 정보 API도
호출하지 않는다. 변환은 아직 이벤트 처리 중 동기 실행이므로 큰 파일이
많은 채널은 부하 측정 없이 추가하지 않는다.

## PF archive 개발용 복제

PF 자료 Git의 **full commit SHA**를 고정하고 회사가 승인한
암호화 전달 경로로 DMZ에 전달한다. 복제 경로는 반드시
`/var/lib/tybot/imports/pf-hermes/<snapshot-sha>/source`다.
운영 `/var/lib/tybot/archive` 아래에는 두지 않는다. 원본 MD를
변환·수정하지 않고 파일별 SHA-256·건수와 채널/첨부 ID의
미확정 목록을 기록한다. 자료 Git에 운영 token·키가 섞였는지
전달 전 PF 담당자가 점검한다. snapshot을 TYBot 답변 근거로
승격하는 작업은 별도 승인·ACL·PII·provenance 검증 후에만 한다.

## 전환 금지 조건

그림자 앱은 채널 메시지와 edit/delete revision 보존까지 구현됐다. 다만 현재
검색기는 revision의 최신 상태를 적용하거나 삭제본을 근거에서 제외하지 않는다.
`revision_reader_ready`와 `attachment_reader_ready`가 실제 검색 회귀시험으로
확인되고, Canvas, Hermes의 Slack 검토 DM, TYBot→Node Hermes v3 도구 호출이
통과하기 전에는 운영 수집이나 변환 기능을 TYBot에서 끄지 않는다.
