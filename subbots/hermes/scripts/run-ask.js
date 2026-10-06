#!/usr/bin/env node
/**
 * 질문 하나를 콘솔로 돌려 본다. **슬랙에 아무것도 쓰지 않는다.**
 *
 *   node scripts/run-ask.js "사업장나 잔금수금 얼마야"           공개 채널에서 물은 것과 같은 권한
 *   node scripts/run-ask.js "…" --full                          전부 열린 권한 (본인 DM 과 같다)
 *   node scripts/run-ask.js "…" --private 비공개가                 그 비공개 채널만 열린 권한
 *
 * npm 으로 부를 때는 **`--` 를 넣어야 옵션이 전달된다** — `npm run ask -- "…" --full`.
 * 빼면 npm 이 --full 을 먹어서 공개 권한으로 돌면서도 화면에는 아무 말이 없다.
 *
 * 요약·점검·자동 반영에는 --dry 가 있는데 Q&A 만 없었다. 프롬프트나 툴 루프를 손본 뒤
 * "답이 같은가 · 토큰이 줄었는가"를 팀이 보는 자리에서 시험하지 않으려고 만들었다.
 * 답변 끝에 찍히는 logUsage 줄(in / cache-w / cache-r / out)이 확인할 곳이다.
 */
import { WebClient } from '@slack/web-api';
import { requireEnv, PUBLIC_ACCESS, FULL_ACCESS, accessFor, accessLabel } from '../src/config.js';
import { answerQuestion } from '../src/claude.js';
import { toolLine } from '../src/convo-log.js';
import { assertOwnsRole, ROLES } from '../src/mode.js';

/* **`process.exit()` 을 안 쓴다** (2026-09-03). 윈도우에서 슬랙 호출을 한 뒤
 * `process.exit(n)` 을 부르면 `@slack/web-api` 가 열어 둔 비동기 핸들 위로 이벤트 루프가
 * 즉시 끊겨 `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)` 으로 죽고,
 * **종료코드가 n 이 아니라 127 로 뭉개진다** (최소 재현으로 확인: `exit(3)` → 127,
 * `exitCode = 3` → 3). 방아쇠는 WebClient 를 만든 것이 아니라 **실제 호출**이다.
 * 리눅스(VM)에서는 안 나지만 사람이 이 PC 에서 돌리는 자리라 그대로 물린다.
 * 자세한 경위는 `run-backfill.js` 머리말 — 그 파일이 먼저 같은 이유로 고쳐졌다.
 *
 * 아래 인자 검사는 슬랙을 부르기 **전**이라 지금은 `process.exit()` 이어도 안 죽는다.
 * 그래도 한 파일에 두 규칙을 두지 않는다 — 「이 exit 은 되고 저 exit 은 안 된다」를
 * 다음 사람이 매번 다시 따져야 하고, 클라이언트를 위로 옮기는 한 줄에 조용히 깨진다.
 *
 * 함수로 감싸는 것은 `process.exitCode` 로는 실행이 안 멈추기 때문이다. ESM 최상위에서는
 * `return` 이 문법 오류라 함수 몸통이 있어야 조기 종료가 된다. */
async function main() {
  // 로컬 ask 는 Hermes 자료 저장소를 직접 읽는다. TYBot 연동의 질문은 반드시
  // TYBot ToolBox 를 지나야 하므로 읽기 전용이라는 이유로 허용하지 않는다.
  assertOwnsRole(ROLES.ANSWER, 'Hermes 로컬 질문(npm run ask)');

  const argv = process.argv.slice(2);
  const words = [];
  let access = PUBLIC_ACCESS;

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--full') access = FULL_ACCESS;
    else if (a === '--private') {
      const name = argv[i + 1];
      // 뒤가 또 옵션이면 채널 이름을 안 준 것이다. 그냥 집어삼키면 '--full' 이라는 이름의
      // 채널을 여는 셈이 되어(= 아무것도 안 열림) 권한을 시험하는 자리에서 조용히 어긋난다.
      if (!name || name.startsWith('--')) {
        console.error('--private 뒤에 채널 이름이 필요합니다.');
        process.exitCode = 1;
        return;
      }
      access = accessFor([name]);
      i += 1;
    } else if (a.startsWith('--')) {
      console.error(`모르는 옵션: ${a}`);
      process.exitCode = 1;
      return;
    } else {
      words.push(a);
    }
  }

  const question = words.join(' ').trim();
  if (!question) {
    console.error('사용법: node scripts/run-ask.js "질문" [--full | --private <채널>]');
    process.exitCode = 1;
    return;
  }

  /* 슬랙 클라이언트는 항상 붙인다 (읽기 전용 호출뿐이다).
   * 빼는 선택지를 두지 않는 이유: qa.md 가 "요즘·이번 주" 질문에 fetch_recent_slack 을 쓰라고
   * 적어 두었는데, 도구만 빼면 모델이 그걸 부르고 'Tool not found' 를 받아 회차만 버린다.
   * 실제 운영은 늘 클라이언트가 있으므로 없는 상태를 시험할 이유도 없다. */
  const env = requireEnv(['SLACK_BOT_TOKEN', 'ANTHROPIC_API_KEY']);
  const slackClient = new WebClient(env.SLACK_BOT_TOKEN);

  console.log(`[ask] 권한: ${accessLabel(access)}`);
  console.log(`[ask] 질문: ${question}\n`);

  try {
    const res = await answerQuestion({
      question,
      access,
      origin: '점검 스크립트 (run-ask.js)',
      asker: '본인',
      slackClient,
    });
    console.log('\n───── 답변 ─────');
    console.log(res.text);
    console.log('────────────────');
    console.log(`근거 채널: ${res.channels.join(', ') || '(없음)'}`);
    console.log(`도구 호출 ${res.toolCalls.length}회 · ${res.costUsd == null ? `총액 미상 (확인분 USD ${res.accounting?.knownCostUsd?.toFixed(6) ?? "미상"})` : `약 $${res.costUsd.toFixed(3)}`}${res.refused ? ' · 거절됨' : ''}`);
    /* 무엇을 어떤 인자로 물었고 몇 자를 물어왔는지. 상한(documentReadMaxChars 등)을 만질 때
     * 보는 자리이고, **인자가 있어야 「어디를 뒤졌나」가 보인다** — 이름과 크기만으로는
     * 같은 도구를 여러 번 부른 회차에서 어느 것이 무엇이었는지 못 가른다.
     * 그리는 규칙은 대화 로그와 **같은 함수**를 쓴다(`toolLine`) — 여기 따로 적으면
     * 그쪽이 바뀔 때 조용히 갈린다. 크기를 모르는 호출도 빠뜨리지 않고 찍으므로
     * 위 「도구 호출 N회」와 줄 수가 맞는다(전에는 `chars` 가 없으면 줄이 사라졌다). */
    for (const t of res.toolCalls) console.log(`  ${toolLine(t)}`);
  } catch (err) {
    console.error('실패:', err.error?.message || err.message);
    process.exitCode = 1;
  }
}

await main();
