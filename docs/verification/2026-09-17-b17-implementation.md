# B-17 규칙 편집 QA 구현 기록

## 구현 범위

- 전문 봇 편집 화면에 시험 워크스페이스와 시험 질문 입력 추가
- 현재 규칙/편집 중 규칙의 답변, 출처, 모델, 비용 병렬 표시
- 요청자의 Slack 채널 멤버십으로 근거 범위 제한
- 실제 전문 봇 계약과 LLM 게이트웨이 경로 재사용
- `specialist_call.task_kind`로 두 시험 호출 구분
- 30일 보존 `specialist_rule_test`와 승인 요청 연결
- 승인 이력 화면에 요청자가 실행한 비교 결과 표시
- 시험 이후 현재 규칙 또는 편집 규칙이 달라진 경우 시험 결과 재사용 차단
- 감사 로그에는 `reason=rule_test`와 상태만 기록

## 자동 검증

```text
pytest tests/test_rule_test.py tests/test_console_api.py tests/test_console_lifecycle_store.py tests/test_specialist_request_decision.py tests/test_specialist_router.py tests/test_tool_specialist.py -q
222 passed, 1 warning

ruff check src tests scripts
All checks passed!

cd console-web && npm run build
빌드 성공
```

경고 1건은 FastAPI TestClient가 사용하는 Starlette의 httpx 호환성 폐기 예정 경고이며
B-17 동작 실패가 아니다.

전체 테스트는 `2472 passed, 31 failed`였다. 실패 중 30건은 Windows의 `bash.exe`가
WSL 배포 스크립트를 실행하지 못한 환경 문제이고, 1건은 동시에 작업 중인 B-56 구현
문서의 운영 명령 표기 문제다. B-17 관련 테스트는 모두 통과했다.

## 배포 후 확인

1. `sudo /opt/tybot/deploy/apply-schema.sh`
2. `sudo systemctl restart tybot-console`
3. 개발자 계정으로 전문 봇 규칙 변경 화면을 연다.
4. 담당 워크스페이스와 질문을 선택해 `전후 답변 비교`를 실행한다.
5. 답변·출처·비용이 양쪽에 표시되는지 확인한다.
6. 변경 요청을 제출하고 관리자 화면에 동일한 비교 결과가 표시되는지 확인한다.

실제 Slack ACL 및 유료 LLM 호출은 로컬 자동 테스트에서 수행하지 않았다. 위 운영 확인이
끝나기 전까지 상태는 `구현 완료·서버 검증 대기`다.
