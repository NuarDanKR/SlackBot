# B-50 Claude 구현 인계

> 기준일: 2026-09-16
> 설계: [`../design/summary-review-canvas.md`](../design/summary-review-canvas.md)

## 목표

검토자는 채널·검토일별 Canvas에서 전체 예상 요약과 근거를 읽고, DM에서 후보별
`맞다`·`틀리다`·`나중에`를 결정한다. 일부만 맞으면 틀린 후보마다 정정 모달을 받는다.

## 현재 코드에서 반드시 이어 갈 것

- `daily_review.main()`은 첨부 변환 상세 DM을 더 이상 보내지 않는다. 되살리지 않는다.
- `summary_review.parse_proposals()`는 숫자가 든 후보를 `number_or_schedule`로 보정한다.
- `candidate_blocks()`는 Canvas 실패 시 상세 요약 DM 폴백으로 유지한다.
- `canvas_answer.create()`의 Disclaimer·동적 제목·`remember_generated()`를 재사용한다.
- 정정사항은 피드백 로그에만 남기고 아카이브·감사 metadata에는 쓰지 않는다.
- `.claude/settings.local.json`은 수정하거나 커밋하지 않는다.

## 구현 순서

1. 새 멱등 SQL과 `apply-schema.sh` 등록
2. Artifact Store와 결정적 전체 예상 요약 렌더러
3. Canvas 생성·다중 검토자 읽기 권한·모호 실패 처리
4. 간결한 DM 링크와 후보별 버튼
5. 후보별 반려 모달·수신자 권한 검증·동시 결정 잠금
6. 전체 맞다와 DM 상태 갱신
7. 콘솔 진단·헬스 체크
8. 계약·회귀·권한 테스트

각 단계는 설계 문서의 필수 테스트를 함께 추가한다. Canvas API 호출을 먼저 만들고
나중에 멱등성을 붙이지 않는다. API와 DB 사이에서 죽으면 중복 Canvas가 생기므로
Artifact 행을 먼저 확보하는 순서를 지킨다.

## QA 인계 시 남길 것

- 변경 파일 목록과 DB 마이그레이션 파일
- 새 상태 전이와 실패 코드 목록
- 테스트 명령 및 결과
- 실제 Slack에서 확인할 시나리오
- `ambiguous` Artifact 복구 절차

Codex QA에서는 권한 밖 사용자 action 재현, 동시 승인, Canvas 재수집 금지,
부분 반려 후 승인본 개수를 우선 검증한다.
