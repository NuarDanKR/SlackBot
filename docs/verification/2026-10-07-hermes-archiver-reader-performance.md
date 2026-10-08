# Hermes Archiver reader 성능 결함과 PF 인계

> **2026-10-08 운영 전제 정정:** PF 전용 서버와 GCP Hermes는 두지 않는다. PF용
> Hermes도 TYBot·Archiver가 있는 같은 서버에서 별도 서비스 인스턴스로 운영한다.
> 아래의 "PF 서버"는 별도 호스트가 아니라 **같은 호스트의 PF Hermes 인스턴스**를
> 뜻한다. 서비스 계정·환경 파일·workspace·privacy manifest·쓰기 가능한 state는
> 인스턴스별로 분리한다.

## 발견

2026-10-07 TYIT 파일럿 사전 검사에서 `npm run check:archive`의
`check-brief-split.js`가 12분 넘게 CPU 한 코어를 100% 사용했다. 마지막으로 출력된
`상황판 투영이 필드를 빠뜨리지 않았나`는 이미 끝난 항목이고, 실제 실행 중이던 것은
권한 조합별 색인 분할 검사였다.

원인은 `src/archive-reader/archiver.js`의 `state()`였다. 공개 API 호출 때마다 `scan()`이
변경 지문 확인과 본문 로딩을 함께 수행해, TYIT의 모든 `archive/raw/*.md`를 다시 읽고
파싱했다. 색인 검사는 공개·비공개·복수 채널·전체 권한 조합마다 여러 공개 API를 호출하므로
전체 정본 읽기가 중첩됐다. Slack API나 네트워크 대기가 아니라 로컬 반복 파싱이었다.

## 수정 경계

- 변경 감지는 경로, inode, 크기, mtime, ctime과 첨부 file-id·revision 목록만 읽는다.
- 지문이 달라진 경우에만 raw와 첨부 정본 본문을 한 번 읽어 스냅샷을 재구축한다.
- 신규 raw, 첨부 생성, 재변환, 삭제는 같은 프로세스에서 즉시 보여야 한다.
- 심볼릭 링크 거부, workspace·DM·channel_id 내용 방어, 좌표·해시는 그대로 유지한다.
- PF Hermes는 DB나 Slack 원문 writer를 열지 않는다.

## 담당 분리

- Codex: reader inventory/snapshot 분리와 본문 재읽기 회귀시험.
- Hermes 개발 에이전트: `check-brief-split.js` 및 `check-setup.js`의 구간 진행 표시,
  구간별 소요 시간, 장시간 상한, 실제 TYIT 규모 벤치마크. reader 파일은 동시에 수정하지 않는다.

## PF 개발자 확인 사항

PF 적용 전 동일한 `check:archive`를 PF 서버 자료 규모로 실행해 다음을 기록한다.

1. 전체 소요 시간과 `check-brief-split` 소요 시간
2. 최대 CPU와 RSS
3. 공개·비공개 권한 조합 수
4. raw 파일, 첨부 정본, 메시지 수
5. manifest 실패 시 전체 권한 외 접근이 계속 닫히는지

진행 표시 개선 없이 장시간 검사가 조용히 멈춘 것처럼 보이는 상태로 PF에 전달하지 않는다.

## 2026-10-08 운영 readiness 프로필 분리

`HERMES_MODE=pf-archiver npm run check:archive`는 더 이상 standalone 개발 검사를
운영 정본에 억지로 실행하지 않는다. 다음 네 검사는 활성 Archiver source를 대상으로
계속 실행한다.

- 공유 규칙의 JS/Python 일치와 거부 경로
- 상황판 투영 필드 계약
- 첨부 본문 표시와 활성 source 일치
- privacy manifest와 비공개 선언 대조

legacy `slack-export`, Git writer, 원문 삽입 skill, 합성 fixture, standalone 설치 저장소
검사는 운영 readiness에서 제외한다. 제외는 통과로 숨기지 않고 **파일명과 사유를 전부
출력**한다. 이 검사는 저장소 CI와 회귀시험에서 계속 실행한다. `pf` 모드의 기존
`check:archive` 전수 개발 검사는 바꾸지 않는다.

운영 프로필의 `[3/6]`·`[4/6]`은 경로를 직접 순회하지 않고 현재 reader API가 반환하는
Archiver 채널·첨부 정본 건수를 센다. 따라서 같은 서버인지 여부와 무관하게 다른
workspace나 legacy 자료 루트를 우연히 읽어 성공하는 것을 readiness로 인정하지 않는다.

## 로컬 합성 기준선

Node 22에서 TYIT 실측 줄 수와 비슷한 227,040줄(11채널)을 생성해 reader만 측정했다.

- 최초 본문 로딩: 2,175ms
- 변경 없는 공개 API 300회: 총 1,128ms
- 변경 없는 호출의 정본 본문 재읽기: 0회

이는 운영 서버의 `check:archive` 전체 시간이 아니다. 서버 Node 20, 실제 첨부 수와 디스크
상태로 다시 측정해야 하며, 숫자의 목적은 12분 반복 파싱이 제거됐는지 판별할 기준을 두는
것이다.
