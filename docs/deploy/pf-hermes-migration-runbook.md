# 사용 중지: PF Hermes 서버 런타임 이관 runbook

2026-09-22 오너 결정으로 이 runbook의 기존 계획은 취소됐다.

- Hermes 원본 프로세스는 GCP에 둔다.
- TYBot 서버에 `pf-hermes.service`를 만들거나 Node runtime을 배포하지 않는다.
- `deploy/setup-pf-hermes-host.sh`를 이번 작업에 실행하지 않는다.
- 2026-09-23에는 archive의 불변 snapshot만 격리 구역으로 복제한다.

현재 실행 절차와 책임 경계는 다음 문서를 따른다.

- [Archiving Bot 분리와 Hermes 직접 호출](../design/archiving-bot-separation-2026-09-23.md)
- [PF Hermes 수정 요청](../design/pf-hermes-archiver-integration-request.md)
- [PF·TY 공통 archive 구조 실측 보고서](../verification/2026-09-23-archive-layout-benchmark.md)

이 파일은 과거 링크가 잘못된 배포 절차를 열지 않도록 안내문으로 남긴다.
