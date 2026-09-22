"""PF 운영 콘솔 (`/pf/`) — 프금팀 Hermes 를 보는 읽기 전용 화면.

## 왜 별도 패키지인가
TYBot 콘솔에 React route 만 더하는 것은 **격리가 아니다**(오너 계획 §8.1). 같은
프로세스면 같은 env 를 읽고 같은 DB 연결을 쓰며, TYBot `developer` 가 PF 를 보게 된다.

그래서 프로세스·계정·env·세션 쿠키·DB role 을 전부 나눈다. 그 분리가 코드에서도
지켜지도록 **이 패키지는 `tybot.*` 를 임포트하지 않는다.** 함수 하나만 빌려 써도
그 함수가 `DATABASE_URL` 이나 아카이브 경로를 읽는 순간 격리가 조용히 사라진다.

같은 이유로 비밀번호·세션 서명 코드가 `tybot.console.auth` 와 겹친다. 중복이지만
**의도한 중복**이다 — 시험(`tests/test_pf_isolation.py`)이 이 규칙을 고정한다.

## 오늘 여는 것
조회뿐이다. `restart` · `stop` · archive sync · release activate · rollback 은 고정
helper 와 중복 실행 lock, timeout, append-only 감사가 구현된 뒤에만 연다(§11).
"""
