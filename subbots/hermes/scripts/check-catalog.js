/** Ordered registry. New discovered Python/shell tests require explicit classification. */
export const CHECKS = [
  {
    "file": "scripts/check-doc-brief.js",
    "what": "문서 색인·접힘·공통/추가분이 합성 계약을 보존하나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-doc-search.js",
    "what": "문서 검색·발췌·전사 카드가 합성 계약을 보존하나 (Codex R2e)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-doc-read.js",
    "what": "문서 열람·목차·요약 첨부 선택이 합성 계약을 보존하나 (Codex R2f)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-doc-access.js",
    "what": "문서 권한·가상 이름·후보·오류가 합성 계약을 보존하나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-doc-store.js",
    "what": "문서 로딩·캐시·실패·공개 승인 판정이 합성 입력 계약을 지키나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-doc-parse.js",
    "what": "문서 파일명·시트·절 파싱이 합성 계약과 공개 진입점을 보존하나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-llm-tools.js",
    "what": "도구 스키마·권한·검색·열람·사용량 기록이 합성 입력 계약을 지키나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-llm-prompts.js",
    "what": "프롬프트 치환·권한별 색인 캐시·자료 지문이 오프라인 계약을 지키나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-llm-qa.js",
    "what": "Q&A 요청·캐시·도구 반복·실패·질문 간 진단 상태가 오프라인 계약을 지키나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-usage-accounting.js",
    "what": "거절·폴백·게시 실패·요약 대조 비용을 누락·중복 없이 기록하나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-llm-provider.js",
    "what": "모델 제공사 창구의 요청 모양·중단 사유 정규화·재시도 진리표가 계약을 지키나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-llm-summary.js",
    "what": "요약 생성·대조의 요청·거절·상한·재시도가 외부 호출 없이 기존 계약을 지키나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-shared-rules.js",
    "what": "봇 판정·excluded 가 두 언어에서 같고 후보 거르기 다섯 갈래가 다 살아 있나 · 확장자 목록·우선순위가 세 자리에서 같나 · 비공개 선언 대조가 두 언어에서 같나(개명 포함) · 거부가 실제로 쓰이나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-board-docs.js",
    "what": "상황판 투영이 필드를 빠뜨리지 않았나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-brief-split.js",
    "what": "색인 공통분이 권한 조합마다 같은가",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-pending-work.js",
    "what": "아침 목록 합치기 규칙이 지켜지나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-uninvited.js",
    "what": "미초대 목록이 슬랙과 기록을 다 보나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-search-terms.js",
    "what": "검색 낱말 쪼개기·부분 점수 규칙이 스스로 지켜지나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Imports search-terms.js, which imports operational config.js."
  },
  {
    "file": "scripts/check-partial-hits.js",
    "what": "부분 일치 되돌림이 확정 히트를 밀어내지 않고 자리마다 골고루 나오나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-hit-cap-hint.js",
    "what": "문서 히트가 잘렸을 때 note 가 그 사실을 밝히고 read_document 로 보내나 (세 분기 전부)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-attachment-marks.js",
    "what": "첨부 「본문 수록」 표시가 맞게 붙고 안 보이는 자료엔 안 붙나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-outside-hits.js",
    "what": "좁혀서 못 찾았을 때 밖도 보는 안전망이 켜지고 권한을 지키나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-health-freshness.js",
    "what": "위생 점검 DM 이 「무엇을 기준으로 쟀는지」를 규칙대로 싣나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-excel-sheets.js",
    "what": "엑셀 시트가 블록으로 나뉘고 색인·요약이 그것을 회차로 안 세나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-fold-line.js",
    "what": "접힘 줄이 종류 개수에 안 비례하나 (문서를 넣어도 바닥이 안 오르나)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-floor-parts.js",
    "what": "바닥의 다섯 조각이 원문 길이와 정확히 맞고 서로 독립인가",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-series-fold.js",
    "what": "예산이 부족하면 시리즈도 오래된 것부터 접히는가",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-line-endings.js",
    "what": "읽는 자리에서 줄 끝이 LF 로 맞춰지나 (윈도우 사본이 VM 과 같은 판인가)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-index.js",
    "what": "index.md 의 숫자·목록이 실물과 갈렸을 때 감사가 그걸 잡나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-sections.js",
    "what": "번호 붙은 절 인식이 하위 항목을 절로 오인하지 않나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-section-read.js",
    "what": "제목에 숫자가 든 절이 섞여 있어도 번호로 열면 그 번호가 열리나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-outline.js",
    "what": "큰 문서가 전문 대신 목차로 오고 막다른 안내를 안 주나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-resolve-filename.js",
    "what": "read_document 이름 풀기가 회차 원본 파일명도 후보로 보나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-echo-gate.js",
    "what": "봇 질문 반향이 대화 안전망(outside)을 잠그지 않나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-open-before-deny.js",
    "what": "값이 발췌 밖일 때 열고 나서 없다고 말하라는 지시가 프롬프트에 남아 있나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-failure-log.js",
    "what": "실패한 회차가 사유와 「어디까지 갔나」로 로그에 남나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-cache-stats.js",
    "what": "캐시 표가 「안 맞았다」와 「못 쟀다」를 가르나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-channel-ids.js",
    "what": "못 푼 채널 링크가 든 줄이 빠지고 로그에 ID 가 안 찍히나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-commit-message.js",
    "what": "자동 반영이 커밋 메시지·실패 DM 으로 무엇을 했는지 사실대로 말하나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-channel-refs.js",
    "what": "개명으로 죽은 설정 줄을 개명 보고와 점검이 짚나 (없어진 채널과 가려서)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-roots.js",
    "what": "봇과 스킬이 같은 자료 저장소를 가리키나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-brief-stamp.js",
    "what": "색인 캐시 지문이 자료 저장소의 git 을 보나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-log-render.js",
    "what": "로그 렌더가 「원본이 사라졌다」와 「0건이다」를 가르나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-stale-paths.js",
    "what": "SKILL.md·README·ps1 이 이사 전 경로 대신 지금 저장소를 가리키나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-bootstrap.js",
    "what": "빈 손으로 시작하는 사람이 문서대로 하면 되나 (템플릿·인자·실패 안내)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-gitignore.js",
    "what": ".gitignore 가 두 저장소의 지금 뿌리와 맞나 (막는 것·통과시키는 것 둘 다)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-business-names.js",
    "what": "코드 저장소에 사업장·비공개 채널 이름이 남아 있나 (팀끼리 나눠 쓰는 저장소다)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-incident-refs.js",
    "what": "주석이 가리키는 사고 기록 절이 실재하나 (저장소가 둘이라 아무도 안 지킨다)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-backfill.js",
    "what": "백필의 넣나 마나 판정·달 끊기·재개가 지켜지나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-thread-loss.js",
    "what": "스레드 답글이 조용히 사라지는 자리 넷(반응·상태오염·과대집계·백필 흔적)이 막혀 있나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-skip-channels.js",
    "what": "안 다루기로 한 채널이 색인·검색·전문 읽기에서도 빠지나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-install-wizard.js",
    "what": "설치 마법사·CLAUDE.md 가 가리키는 것이 실재하고 머리말과 0~8단계 표가 안 갈렸나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-deploy-judge.js",
    "what": "배포 판정이 「받았는데 재시작 안 함」과 「못 쟀다」를 가리나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-optional-dep-signal.js",
    "what": "선택 의존성이 없어 못 잰 시험이 깨진 시험과 다른 신호(종료코드 2)를 내나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-archiver-privacy.js",
    "what": "pf-archiver 전환 전 사전 점검 — 정본 visibility 와 config.privateChannels 전수 대조. 비공개 선언 누락이 있으면 1 로 끝낸다",
    "runtime": "node",
    "mode": "archive",
    "reason": "Needs an Archiver canonical root and the data repository config; it is a switchover preflight, not a unit check."
  },
  {
    "file": "scripts/check-archiver-reader.js",
    "what": "HERMES_MODE=pf-archiver 에서 Archiver 정본 reader 가 기존 공개 API 로 돌고, 다른 워크스페이스·DM 이 안 새며, 좌표를 추정하지 않나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Needs an Archiver fixture root and a data repository config; run from tests/test_hermes_archiver_reader.py which builds both with the real collector."
  },
  {
    "file": "scripts/check-archive-contract.js",
    "what": "봇이 쓰는 파서가 실물 아카이브 md 를 읽어내나 (월 헤딩·메시지 헤더·월 단위 읽기·관문 프로브)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-preamble.js",
    "what": "첫 메시지 앞의 사람 정리가 검색·색인 양쪽에 실리나 (월 헤딩과 안 섞이나)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-preamble.js",
    "what": "문서 md 첫 회차 앞의 사람 정리가 검색·색인 양쪽에 실리나 (숫자는 색인에 안 새나)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-tool-limit.js",
    "what": "도구 반복 상한에 걸린 회차가 정상 답변과 구별되나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-digest-window.js",
    "what": "일일 요약 구간이 못 보낸 데부터 이어지고 상한에서 조용히 안 버리나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-service-unit.js",
    "what": "hermes.service 의 StartLimit* 가 systemd 가 읽는 [Unit] 절에 있나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-error-block.js",
    "what": "여러 줄 오류 메시지가 인라인 백틱을 깨지 않고 펜스로 나가나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-failure-ratio-wording.js",
    "what": "실패·재시도 사유 절이 「N회 중 M회」 대신 정직한 분모 문구를 쓰나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-redact-fold.js",
    "what": "redactPrivateMentions 가 matchesHiddenPrivate 와 같은 fold 규칙으로 띄어 쓴 이름도 맞대나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-month-preamble.js",
    "what": "read_document 에 month 를 지정해도 문서 머리말이 실리나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-week.js",
    "what": "month 로 좁혔는데도 그 달이 상한을 넘으면 주(넘치면 날짜) 단위로 다시 나뉘나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-month-heading.js",
    "what": "documents.js 의 outlineOf·readDocument(month) 가 월 헤딩을 관대한 정본(^##\\s+)으로 찾나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-compose-bot-replies.js",
    "what": "compose() 가 「Hermes 글의 [정정]」과 「다른 봇 글의 답글」을 따로 세나 (2026-08-28 회귀)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-digest-limits.js",
    "what": "요약 문서 상한의 코드 폴백이 config.example.json 정본과 같나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Dynamically imports operational config.js and compares live configuration with template."
  },
  {
    "file": "scripts/check-bot-reply-count.js",
    "what": "봇 글에 달린 답글 DM 이 제안한 개수가 아니라 실제로 쓴 것을 세나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-month-heading.js",
    "what": "archive.js 의 월 헤딩 찾기가 관대한 정본(^##\\s+)이라 공백 두 칸도 찾나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-timezone.js",
    "what": "파이썬 스크립트들이 KST 를 하드코딩하지 않고 config.json 의 timezone 을 쓰나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-live-fetch-max-days.js",
    "what": "liveFetchMaxDays 기본값이 세 곳에 흩어지지 않고 한 상수에서 오나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-git-rebase-hash.js",
    "what": "pull --rebase 로 해시가 바뀌어도 DM 이 실제로 있는 커밋을 알리나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-missed-execution.js",
    "what": "이벤트 루프가 막혀 건너뛴 예약 회차를 조용히 넘기지 않고 알리나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-failure-origin.js",
    "what": "던져서 죽은 회차도 로그에 기간 라벨(`_기간:_`)을 남기나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-real-ids.js",
    "what": "실제 슬랙 채널·파일 ID 가 저장소에 적혀 있지 않나 (이름 축의 사각)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-auto-narrow.js",
    "what": "질의에 자리 이름이 있으면 스스로 좁히나 (볼 수 있는 자리만 · 빈손이면 되돌아가나 · 끌 수 있나)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-channel-aliases.js",
    "what": "개명 되짚기가 실제로 돌고, 되짚을 근거 없는 이름·되쓰인 옛 이름이 없나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-tool-line-roundtrip.js",
    "what": "로그의 **도구**·회차·비용 줄이 쓴 대로 다시 읽히나 (재보기가 조각을 조용히 빠뜨리나)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-log-codec.js",
    "what": "로그 줄 렌더·파싱이 합성 계약을 보존하나 (Codex R3a)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-log-stats.js",
    "what": "실패·캐시 통계가 합성 계약을 보존하나 (Codex R3b)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-log-measure.js",
    "what": "재보기가 md 의 금액을 어느 회차 것으로 읽나 (사용량 기록이 앞 회차를 덮나)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-log-render-contract.js",
    "what": "로그 렌더가 출력·쓰기 순서·원본 소실 보호를 보존하나 (Codex R3c)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-log-store.js",
    "what": "로그 저장의 파일 형식·쓰기 순서·오류 처리를 보존하나 (Codex R3d)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-slack-access.js",
    "what": "Slack 접근 권한·멤버 캐시·조회 실패 차단을 보존하나 (Codex R3e)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-slack-question.js",
    "what": "Slack 질문·스레드 게시·실패 비용 기록 순서를 보존하나 (Codex R3f)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-slack-live-api.js",
    "what": "Slack API 페이지 조회·사용자 캐시·실패 처리를 보존하나 (Codex R3h)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-slack-live-render.js",
    "what": "Slack 메시지 정규화·출력·채널명 캐시를 보존하나 (Codex R3i)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-slack-live-window.js",
    "what": "Slack 기간 계산·기본값·전송 상태 연결을 보존하나 (Codex R3j)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-health-report.js",
    "what": "위생 점검 보고문·알림 조건·실행 연결을 보존하나 (Codex R3k)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-health-status.js",
    "what": "위생 점검 상태·경과일·처리 결정 반영을 보존하나 (Codex R3l)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-health-doc-filters.js",
    "what": "문서 후보 판정·Python 공용 규칙·미변환 집계를 보존하나 (Codex R3m)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-health-attachments.js",
    "what": "첨부 페이지·스레드·공개 태그·집계 연결을 보존하나 (Codex R3n)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-health-documents.js",
    "what": "문서 상태·승인 메타·미변환 집계·소비자 연결을 보존하나 (Codex R3o)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-ingest-archive.js",
    "what": "수집 조회·원문·수정 감지·반영 연결을 보존하나 (Codex R3p)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-report-ambiguous.js",
    "what": "아침 DM 이 같은-분 ambiguous 항목을 신규 블록·며칠째로 그리나 (갈래 ③)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-ingest-failure.js",
    "what": "수집 단계 실패·되돌리기·비용·보고 계약을 보존하나 (Codex R3p)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-scheduler-failure.js",
    "what": "예약 실패 기록·본인 알림·중복 보고 방지를 보존하나 (Codex R3g)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-search-hit-cap.js",
    "what": "대화 히트 건당 상한 — 꺼지면 그대로, 켜면 창 맞춤",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-cards.js",
    "what": "사업장 좁힘이 빈손일 때 전사 종합 카드가 붙고, 히트·안전망 판정은 안 바뀌나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-card-render.js",
    "what": "전사 종합 카드 구역이 claude.js 에서 실제로 그려지나 (제목·안내문·발췌 없는 한 줄 렌더)",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-doc-card-kill-switch.js",
    "what": "companyWideDocProjects 를 비우면 카드뿐 아니라 로컬 재스캔도 같이 꺼지나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-reexport-binding.js",
    "what": "재수출만 한 이름을 자기 파일에서 쓰는 자리가 없나 (실행되면 ReferenceError)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": "scripts/check-doc-card-masked-title.js",
    "what": "제목이 통째로 가려진 전사 문서가 카드 상한을 갉아먹지 않나",
    "runtime": "node",
    "mode": "archive",
    "reason": "Legacy check requires application config, local environment or archive; excluded from config-free execution."
  },
  {
    "file": "scripts/check-check-modes.js",
    "what": "검사 모드·분류·미실행·실자료 진입 경계를 지키나 (Codex R4)",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
  {
    "file": ".claude/skills/_shared/test_paths.py",
    "what": ".claude/skills/_shared/test_paths.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/archive-inbox/scripts/test_decide_work.py",
    "what": ".claude/skills/archive-inbox/scripts/test_decide_work.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/archive-inbox/scripts/test_decision_export.py",
    "what": ".claude/skills/archive-inbox/scripts/test_decision_export.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/archive-inbox/scripts/test_review_work.py",
    "what": ".claude/skills/archive-inbox/scripts/test_review_work.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/archive-run/scripts/test_board.py",
    "what": ".claude/skills/archive-run/scripts/test_board.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_apply_approvals.py",
    "what": ".claude/skills/doc-archive/scripts/test_apply_approvals.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_count_track_changes.py",
    "what": ".claude/skills/doc-archive/scripts/test_count_track_changes.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_decide.py",
    "what": ".claude/skills/doc-archive/scripts/test_decide.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_detect_merged_numbers.py",
    "what": ".claude/skills/doc-archive/scripts/test_detect_merged_numbers.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_extract_comments.py",
    "what": ".claude/skills/doc-archive/scripts/test_extract_comments.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_fetch_filters.py",
    "what": ".claude/skills/doc-archive/scripts/test_fetch_filters.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_fetch_limit.py",
    "what": ".claude/skills/doc-archive/scripts/test_fetch_limit.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_insert_entry.py",
    "what": ".claude/skills/doc-archive/scripts/test_insert_entry.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_prune_cache.py",
    "what": ".claude/skills/doc-archive/scripts/test_prune_cache.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_review_batch.py",
    "what": ".claude/skills/doc-archive/scripts/test_review_batch.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_screen_personal.py",
    "what": ".claude/skills/doc-archive/scripts/test_screen_personal.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/doc-archive/scripts/test_xlsx_to_blocks.py",
    "what": ".claude/skills/doc-archive/scripts/test_xlsx_to_blocks.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/issue-trim/scripts/test_trim.py",
    "what": ".claude/skills/issue-trim/scripts/test_trim.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/slack-sync/scripts/test_apply_edits.py",
    "what": ".claude/skills/slack-sync/scripts/test_apply_edits.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/slack-sync/scripts/test_fresh_seed.py",
    "what": ".claude/skills/slack-sync/scripts/test_fresh_seed.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/slack-sync/scripts/test_insert_messages.py",
    "what": ".claude/skills/slack-sync/scripts/test_insert_messages.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/slack-sync/scripts/test_sync_index.py",
    "what": ".claude/skills/slack-sync/scripts/test_sync_index.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": ".claude/skills/slack-sync/scripts/test_verify_archive.py",
    "what": ".claude/skills/slack-sync/scripts/test_verify_archive.py",
    "runtime": "python",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": "deploy/test-diagnose-pull.sh",
    "what": "deploy/test-diagnose-pull.sh",
    "runtime": "bash",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": "deploy/test-setup-guard.sh",
    "what": "deploy/test-setup-guard.sh",
    "runtime": "bash",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": "deploy/test-setup-packages.sh",
    "what": "deploy/test-setup-packages.sh",
    "runtime": "bash",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": "deploy/test-setup-tail.sh",
    "what": "deploy/test-setup-tail.sh",
    "runtime": "bash",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  },
  {
    "file": "deploy/test-setup-window.sh",
    "what": "deploy/test-setup-window.sh",
    "runtime": "bash",
    "mode": "archive",
    "reason": "Local fixture/process suite; environment and script dependencies are not certified config-free. May create temporary files/repositories."
  }
];
export const LIVE_CHECKS = [
  { phase: "anthropic", mode: "live", what: "Model lookup and paid sample request" },
  { phase: "slack", mode: "live", what: "Authentication, scopes, channel and history reads" },
];
