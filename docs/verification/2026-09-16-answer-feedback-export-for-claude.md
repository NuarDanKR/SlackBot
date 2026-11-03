# 답변 현황·피드백 Claude 인계 절차

운영 콘솔의 답변 현황은 서버의 `QA_LOG_DIR`에 있는 질문·답변 감사 기록과 피드백
JSONL을 읽는다. 개발 PC 저장소에는 이 운영 원문이 없으므로 Codex나 Claude가 화면만
보고 자동 수집할 수 없다.

`scripts/export_answer_issues.py`는 같은 원본에서 다음 문제 후보만 Markdown으로 묶는다.

- 처리 오류와 `invalid-output`, `timeout`, `unavailable`
- 빈 답변, 출처 없음, 기록에 남은 메모리·자원 고갈
- 근거 없음 또는 권한 범위 없음
- 15초를 초과한 답변
- 검색 20건 초과와 일부 범위 미확인·범위 상한 제외
- 전문 봇 실패·폴백 추적
- 사용자 문제·근거 누락·정정 피드백과 처리 상태

분포 표는 `--limit`과 무관하게 조회 범위의 문제 후보를 **전부** 센다. `--limit`은
질문·답변 원문이 포함된 상세 절의 건수만 제한한다. 근거와 출처가 기록된 성공 답변은
최신 20건을 별도 표본으로 실어 사람이 원문 좌표와 대조한다.

`no_evidence`는 수집 없음·권한·검색어 불일치가 섞인 기준선이다. 그 수 전체를 B-12
벡터 검색이 해결할 건수로 해석하지 않는다. 또한 OOM으로 프로세스가 QA 행을 쓰기 전에
종료되면 이 패킷에는 나타나지 않으므로 systemd 재시작과 커널 OOM 기록을 별도로 본다.

## 서버에서 생성

최근 7일 전체 워크스페이스:

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  /opt/tybot/.venv/bin/python /opt/tybot/scripts/export_answer_issues.py \
  --days 7 \
  --output /var/lib/tybot/qa-log/answer-issues-$(date +%F).md
```

특정 워크스페이스만 보려면 `--workspace tyit`를 추가한다. 보고서는 질문·답변과
피드백 원문을 포함하므로 `/var/lib/tybot/qa-log` 밖의 공개 경로나 Git 저장소에 두지
않는다.

생성한 파일을 이 대화에 첨부하면 Codex가 다음 순서로 정리한다.

1. 반복 문제를 수집·권한·검색·분류·전문 봇·렌더링·운영 설정으로 묶는다.
2. 각 묶음에 재현 조건, 관련 QA ID, 예상 원인, 수정 대상 파일을 적는다.
3. 사내 원문을 복제하지 않은 Claude 구현 인계 문서를 `docs/verification/`에 작성한다.
4. Claude 구현 후 같은 QA ID와 회귀 테스트로 Codex가 다시 검증한다.

콘솔 API를 직접 긁는 방식은 브라우저 로그인 쿠키와 관리자 권한을 우회하게 만들 수
있으므로 사용하지 않는다. 서버의 기존 파일 권한 아래에서 명시적으로 생성하는 이
절차를 사용한다.
