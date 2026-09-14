# 문서 변환 워커 운영 준비

상태: 선택 활성화 코드와 unit 템플릿. Rocky8 실제 변환 검증 전에는 운영 활성화하지 않는다.
현재 기본 경로는 기존 프로세스 내 변환이며 `TYBOT_CONVERT_SPOOL`을 지정할 때만 워커를 쓴다.
이 워커는 DB 재시도 큐가 아니다. 한 번의 변환 요청을 별도 계정으로 처리하는 로컬 spool이다.

## 계정과 파일 권한

- `tybot-convert` 시스템 계정/그룹을 생성한다. `tybot` 그룹에 가입시키지 않는다.
- `/opt/tybot-convert`는 승인된 동일 커밋의 코드, `vendor/hermes` 변환 코드,
  전용 `.venv`만 배치한다. root 소유이며 worker는 읽기/실행만 가능하게 한다.
  프로젝트의 `.env`, `.git`, 아카이브, 로그, 운영 설정을 복사하지 않는다.
- 기존 `/opt/tybot/.venv`는 사용하지 않는다. editable 설치가 운영 소스에 의존하거나
  운영 폴더 읽기 권한을 추가로 요구하면 격리가 깨진다.
- 전용 런타임 의존성은 관리자가 사전에 설치한다. 워커가 실행 중 설치하지 않는다.
- `/var/lib/tybot-convert`: root 소유, 두 계정이 traverse 가능하도록 0755.
- `in`: `tybot:tybot-convert`, 2750. master가 입력을 쓰고 worker는 읽기만 한다.
- `out`: `tybot-convert:tybot`, 2750. worker가 결과를 쓰고 master는 읽기만 한다.
- 파일은 0640으로 생성한다. spool에 원문이 있으므로 일반 사용자에게 읽기를 허용하지 않는다.
- 변환 입력의 정상/실패/timeout 후 정리는 요청 프로세스가 수행한다.
  프로세스 강제 종료로 남은 입력은 현재 자동 회수하지 않는다. 작업 중인 요청이 없음을
  확인한 후 관리자가 만료 manifest와 같은 UUID의 bin만 정리해야 한다.
- 출력은 1시간 후 워커가 정리한다. 디스크 사용량 감시와 정지 기준을 운영에 설정한다.

## 격리와 사전 검사

`deploy/tybot-convert.service`를 검토 후 설치한다. 자동 install/update에는 아직 연결하지 않았다.
해당 unit만 `MemoryDenyWriteExecute=false`이며 마스터 봇의 제한은 유지한다.
원래 V8 장애가 이 설정 때문이라고 확정한 것은 아니다.

- `/etc/tybot`, `/var/lib/tybot`, `/opt/tybot` 접근 차단.
- TCP/IP socket 금지, 로컬 UNIX socket만 허용. OCR 모델/바이너리는 사전 설치가 필요하다.
- kordoc, soffice 실행 경로와 OCR 모델 읽기 권한을 별도 계정으로 검사한다.
- HWP/HWPX/PPTX/스캔 PDF/이미지를 **실제로 변환**한다. `--version`만으로 판단하지 않는다.
- `MemoryMax=2G`, `TasksMax=64`는 초기 운영 한도다. 파일 크기 거절 기준은 아니며,
  실제 대형 파일 측정 후 조정해야 한다. 제한 초과로 죽으면 master는 timeout으로 처리한다.
- Linux 외부 변환 명령 timeout은 프로세스 그룹 종료/회수를 수행한다.
  Python 내장 변환 자체의 작업별 강제 종료와 대형 문서 분할은 아직 미구현이다.

검증 후 master 환경에 `TYBOT_CONVERT_SPOOL=/var/lib/tybot-convert`를 적용한다.
worker에 `tybot.env`나 DB/Slack/LLM 시크릿을 전달하지 않는다.
설정 제거 시 기존 변환 경로로 돌아가므로 롤백 전에 기존 경로의 실행 제한도 확인한다.

## 요청 계약과 검증

입력은 무작위 UUID 파일과 확장자/만료 시각만이다. 명령, 실행 경로, URL을 받지 않는다.
출력은 텍스트 줄 또는 제한된 오류 코드다. master의 기존 PII 검사와 원문 보존 규칙은
그대로 적용한다. worker는 아카이브/검색/Slack 게시를 하지 않는다.

단위 테스트: `tests/test_conversion_worker.py`.
운영 승인 전 필수: 계정별 접근 차단, 변환 도중 종료/재시작, 디스크 부족,
동시 요청 timeout, 실제 큰 파일 변환, PII 제외, 변환 후 검색 색인 확인.
