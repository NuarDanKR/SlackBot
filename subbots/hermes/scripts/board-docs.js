#!/usr/bin/env node
/**
 * 미변환 첨부·승인 미반영을 **JSON 으로** 낸다. `archive-run` 의 상황판이 부른다.
 *
 *   node scripts/board-docs.js
 *
 * 종료코드: 0 성공 / 1 실패 (stderr 에 사유)
 *
 * ── 왜 09:00 위생 점검을 그대로 안 쓰나 ──
 *
 * `npm run health -- --dry` 도 같은 값을 세지만, 내는 것은 **사람이 읽는 한국어 산문**이다
 * (`compose()`). 그걸 정규식으로 파싱하면 문구를 다듬는 순간 **에러 없이 0건**이 된다 —
 * 이 저장소에서 조용히 틀리는 사고는 전부 그 모양이었다. 그래서 세는 함수
 * (`pendingDocuments`)를 직접 부르고 값을 그대로 넘긴다. **세는 규칙은 한 곳뿐이다.**
 *
 * 이 파일이 스킬 폴더가 아니라 여기 있는 이유: hermes 의 `node_modules`(@slack/web-api)와
 * `.env`(SLACK_BOT_TOKEN)가 필요하다. `check-pending-work.js` 와 같은 자리다.
 *
 * **읽기만 한다** — DM 을 보내지 않고 파일도 안 쓴다.
 */
import path from 'node:path';
import { WebClient, LogLevel } from '@slack/web-api';
import { requireEnv } from '../src/config.js';
import { pendingDocuments } from '../src/archive-health.js';

/**
 * 상황판이 쓰는 것만 골라 넘긴다.
 *
 * **여기서 필드를 빠뜨리면 에러가 아니라 잘못된 화면이 된다.** 실제로 `disabled` 가
 * 빠져 있었다 — `pendingDocuments` 는 문서 폴더가 없을 때 `total: 0` 에 그 표를 붙여
 * 돌려주는데(`archive-health.js` 의 이른 반환), 투영에서 사라져서 `board.py` 의
 * 방어(`docs.get("disabled")`)가 **실전 경로에서 한 번도 참이 될 수 없었다.**
 * 문서 아카이브가 통째로 꺼진 채 상황판은 「미변환 첨부 0건」이라고 말했다.
 *
 * 그때 이 자리 주석은 「모양이 바뀌면 board.py 의 시험이 잡는다」고 적고 있었는데,
 * `test_board.py` 는 `rows()` 에 값을 직접 먹일 뿐 이 파일을 한 번도 부르지 않았다.
 * 그래서 검사를 따로 세웠다 — `scripts/check-board-docs.js`.
 */
export function pickBoardFields(docs) {
  return {
    total: docs.total,
    scanDays: docs.scanDays,
    byChannel: docs.byChannel || [],
    approvals: docs.approvals || [],
    deferred: docs.deferred || [],
    failed: docs.failed || [],
    // 봇이 못 받는 첨부. `total` 에 안 들어가므로 상황판이 따로 보인다.
    restricted: docs.restricted || [],
    // **`?? false` 가 아니라 있는 그대로 넘긴다** — 필드가 없어진 것과 `false` 는 다르다.
    disabled: Boolean(docs.disabled),
  };
}

/* 임포트해서 쓰는 쪽(`check-board-docs.js`)이 있으므로 **직접 실행일 때만** 슬랙에 붙는다.
 * `import.meta.url` 과 `argv[1]` 을 문자열로 대는 흔한 방법은 윈도우에서 안 맞는다
 * (`file:///C:/…` vs `C:\…`). 파일 이름으로 본다. */
if (path.basename(process.argv[1] || '') === 'board-docs.js') {
  try {
    const env = requireEnv(['SLACK_BOT_TOKEN']);
    /*
     * `logLevel` 을 WARN 으로 낮추는 것은 취향이 아니라 **stdout 을 지키는 일**이다.
     * 기본값(INFO)이면 속도 제한을 만났을 때 `[INFO] … API Call failed due to rate limiting`
     * 을 **console.log 로 — 즉 stdout 으로** 찍어 JSON 앞에 붙는다. 그러면 이 스크립트는
     * 종료코드 0 으로 성공했는데 받는 쪽은 파싱에 실패한다 (2026-08-12 에 실제로 그랬다).
     * WARN·ERROR 는 console.warn/error 라 stderr 로 가므로 속도 제한 경고는 그대로 보인다.
     */
    const client = new WebClient(env.SLACK_BOT_TOKEN, { logLevel: LogLevel.WARN });

    /*
     * 토큰부터 한 번 확인한다. `pendingDocuments` 는 채널마다 호출을 돌리므로 토큰이
     * 틀리면 **채널 수만큼 실패하며 3분을 끌다가 「시간 초과」로 끝난다** — 사유가
     * 「토큰이 거부됐다」가 아니라 「느리다」로 바뀌어, 받는 쪽이 엉뚱한 것을 고치게 된다
     * (2026-08-12 에 일부러 틀린 토큰으로 재현했다). 이 호출은 1초짜리다.
     */
    await client.auth.test();

    process.stdout.write(JSON.stringify(pickBoardFields(await pendingDocuments(client))));
  } catch (err) {
    // **0 을 찍고 끝내지 않는다.** 그러면 상황판이 「첨부 0건」으로 보이고,
    // 밀린 것이 있어도 사람은 화면을 닫는다.
    console.error(err.data?.error || err.message || String(err));
    /*
     * `process.exit(1)` 이 아니라 `exitCode` 를 쓴다. 즉시 끊으면 아직 닫히는 중인 슬랙
     * 연결 때문에 libuv 가 어서션으로 죽어 **종료코드가 127** 이 된다 (2026-08-12 실측).
     * 받는 쪽은 0 이 아니라는 것만 보므로 판정은 같지만, 화면에 남는 사유가 사고 조사 때
     * 엉뚱한 곳을 가리킨다.
     */
    process.exitCode = 1;
  }
}
