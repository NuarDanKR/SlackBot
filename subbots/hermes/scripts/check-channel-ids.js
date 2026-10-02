#!/usr/bin/env node
/**
 * 못 푼 채널 링크 처리 점검 — `renderText` 의 `<#C…>` 갈래 셋.
 *
 * 왜 있나: 슬랙 본문의 채널 링크는 `<#C123|이름>` 과 **`<#C123>`** 두 모양으로 온다.
 * 뒤엣것은 이름이 안 붙어 있어 `.sync-state.json` 으로 풀어야 하는데, 거기에는
 * **봇이 초대된 채널만** 있다. 봇이 안 들어간 비공개 채널을 가리킨 링크는 못 풀린다.
 *
 * 못 푼 것을 ID 째로 남기면 `redactPrivateMentions` 가 **이름으로** 줄을 지우므로
 * 그 줄이 안 지워진 채 공개 답변 프롬프트로 간다. 그래서 **그 줄을 통째로 뺀다**
 * (WHK 승인 2026-08-29, 계획서 Task 4). 「막힌 자료는 이름조차 밝히지 않는다」는
 * 이 저장소의 기본 자세를 따른 것이다.
 *
 * 대가는 실측 0건이다 — 2026-08-27 조사에서 봇 프롬프트가 읽는 채널 md 의 못 푼
 * `<#C…>` 는 0건이었다. 그래서 로그가 찍히면 그것 자체가 신호다.
 *
 * 보는 것 셋:
 *  ① `<#C…|이름>` — 슬랙이 이름을 준 경우: `#이름` 으로 풀린다 (기존 동작)
 *  ② `<#C…>` 인데 아는 ID: `#채널명` 으로 풀린다 (기존 동작)
 *  ③ `<#C…>` 인데 못 푸는 ID: 그 줄이 통째로 빠진다 (새 동작)
 *     — 그리고 로그에 **ID 도 채널 이름도 안 찍힌다** (ID 자체가 존재를 드러낸다)
 *
 * 실행: node scripts/check-channel-ids.js
 */
import { renderText, resolveChannelMentions } from '../src/slack-live.js';
import { isPrivateChannel } from '../src/config.js';
import { listAllChannels } from '../src/archive.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

// 아카이브가 없는 새 클론에서는 조용히 건너뛴다 (이웃 검사들과 같은 이유).
let known;
try {
  known = listAllChannels().filter((c) => c.id && c.name);
} catch (e) {
  console.log(`  - 건너뜀: ${e.message.split('\n')[0]}`);
  process.exit(0);
}
if (!known.length) {
  console.log('  - 건너뜀: `.sync-state.json` 에 채널이 없습니다');
  process.exit(0);
}

const USERS = new Map();
const SAMPLE = known[0];              // 실제로 아는 채널 하나
const UNKNOWN = 'C0ZZZZZZZZZ';        // 어디에도 없는 ID

// ① 이름이 함께 온 경우
{
  const got = renderText('<#C0ABCDEF|영업> 확인 부탁', USERS);
  if (got === '#영업 확인 부탁') {
    ok('`<#C…|이름>` 은 이름 그대로 풀린다');
  } else {
    bad(`이름 있는 링크가 ${JSON.stringify(got)} 로 나옵니다`);
  }
}

// ② 이름은 없지만 아는 ID
{
  const got = renderText(`<#${SAMPLE.id}> 참고`, USERS);
  if (got === `#${SAMPLE.name} 참고`) {
    ok('아는 ID 는 `.sync-state.json` 으로 풀린다');
  } else {
    bad(`아는 ID 가 안 풀립니다: ${JSON.stringify(got)}`);
  }
}

// ③ 못 푸는 ID — 그 줄이 통째로 빠져야 한다
{
  const before = '앞 줄은 남는다';
  const after = '뒷 줄도 남는다';
  const logs = [];
  const realWarn = console.warn;
  console.warn = (...a) => logs.push(a.join(' '));
  let got;
  try {
    got = renderText(`${before}\n비공개 얘기 <#${UNKNOWN}> 참고\n${after}`, USERS);
  } finally {
    console.warn = realWarn;
  }

  if (!got.includes(UNKNOWN)) {
    ok('못 푼 ID 가 본문에 안 남는다');
  } else {
    bad(`ID 가 그대로 남습니다: ${JSON.stringify(got)}`);
  }
  if (!got.includes('비공개 얘기')) {
    ok('그 줄이 통째로 빠진다 — 같은 줄의 다른 말도 함께 나간다');
  } else {
    bad(`줄이 안 빠졌습니다: ${JSON.stringify(got)}`);
  }
  if (got.includes(before) && got.includes(after)) {
    ok('앞뒤 줄은 그대로 남는다 — 넓게 지우지 않는다');
  } else {
    bad(`이웃 줄까지 지웠습니다: ${JSON.stringify(got)}`);
  }
  if (logs.length && !logs.join(' ').includes(UNKNOWN)) {
    ok('로그가 남되 ID 는 안 찍힌다 — ID 자체가 그 채널의 존재를 드러낸다');
  } else if (!logs.length) {
    bad('아무 로그도 안 남습니다 — 조용히 지우면 내용이 사라진 이유를 못 찾습니다');
  } else {
    bad(`로그에 ID 가 찍힙니다: ${JSON.stringify(logs)}`);
  }
}

// ④ 못 푼 링크가 없으면 로그도 없어야 한다 (매번 시끄러우면 곧 안 보게 된다)
{
  const logs = [];
  const realWarn = console.warn;
  console.warn = (...a) => logs.push(a.join(' '));
  try {
    renderText(`<#${SAMPLE.id}> 참고`, USERS);
  } finally {
    console.warn = realWarn;
  }
  if (!logs.length) {
    ok('멀쩡할 때는 조용하다');
  } else {
    bad(`쓸데없는 로그가 납니다: ${JSON.stringify(logs)}`);
  }
}

/* ── ⑤ 질문 경로의 채널 멘션 풀기 (2026-09-21 부터) ────────────────────
 *
 * 왜 여기에 있나: 채널 md 의 `채널 ID` 줄을 색인에서 빼면서(같은 날, WHK 결정) ID↔이름
 * 대응을 `resolveChannelMentions` 로 옮겼다. **그 줄이 우연히 해 주던 일을 이제 이 함수가
 * 한다** — 안 돌면 `#` 자동완성으로 채널을 찍은 질문(8월 로그 11건)이 조용히 안 풀린다.
 *
 * `renderText` 와 **다른 점 둘**을 여기서 못 박는다 —
 *  ⑴ 못 풀어도 **줄을 안 버린다** (사람이 던진 질문이라 통째로 사라지면 안 된다)
 *  ⑵ **권한을 탄다** (`canSee`) — 못 보는 채널이면 이름을 안 알려주고 ID 를 그대로 둔다
 */
{
  const FULL = { full: true, channels: new Set() };
  const NONE = { full: false, channels: new Set() };

  const got = resolveChannelMentions(`<#${SAMPLE.id}> 현황 알려줘`, FULL);
  if (got === `#${SAMPLE.name} 현황 알려줘`) ok('질문의 `<#C…>` 가 이름으로 풀린다');
  else bad(`질문의 채널 멘션이 ${JSON.stringify(got)} 로 나옵니다`);

  const named = resolveChannelMentions('<#C0ABCDEF|영업> 확인', FULL);
  if (named === '#영업 확인') ok('이름이 함께 온 멘션도 풀린다');
  else bad(`이름 있는 멘션이 ${JSON.stringify(named)} 로 나옵니다`);

  const kept = resolveChannelMentions(`<#${UNKNOWN}> 확인`, FULL);
  if (kept === `<#${UNKNOWN}> 확인`) ok('못 푸는 ID 는 그대로 두고 질문을 안 버린다');
  else bad(`못 푸는 ID 에서 질문이 ${JSON.stringify(kept)} 가 됐습니다`);

  /* 권한 — 비공개 채널을 못 보는 사람에게는 이름을 안 알려준다.
   * SAMPLE 이 공개 채널일 수 있으므로 **비공개일 때만** 잰다. 조건이 안 맞으면
   * 통과로 세지 않고 「못 쟀다」고 적는다 — 안 잰 것을 이상 없음으로 세지 않는다. */
  const priv = known.find((c) => isPrivateChannel(c.name));
  if (priv) {
    /* **실패 메시지에 이름도 ID 도 안 적는다** — 비공개 채널은 이름 자체가 가릴 것이고,
     * ID 도 그 채널의 존재를 드러낸다(이 파일 머리의 규칙 그대로). 새는 것을 알리는
     * 검사가 알리면서 새면 안 된다. 무엇이 틀렸는지는 「풀렸다/안 풀렸다」로 충분하다. */
    const hidden = resolveChannelMentions(`<#${priv.id}> 확인`, NONE);
    if (hidden === `<#${priv.id}> 확인`) ok('못 보는 비공개 채널은 이름을 안 밝힌다');
    else bad('권한 없는 사람에게 비공개 채널 이름이 드러납니다 (이름은 적지 않습니다)');

    const mine = resolveChannelMentions(`<#${priv.id}> 확인`, { full: false, channels: new Set([priv.name]) });
    if (mine === `#${priv.name} 확인`) ok('멤버에게는 비공개 채널도 풀린다');
    else bad('멤버인데 비공개 채널 멘션이 안 풀립니다 (이름은 적지 않습니다)');
  } else {
    console.log('  - 못 쟀음: `.sync-state.json` 에 비공개 채널이 없어 권한 갈래를 못 밟았습니다');
  }

  const empty = resolveChannelMentions('', FULL);
  if (empty === '') ok('빈 질문에서 안 죽는다');
  else bad(`빈 입력이 ${JSON.stringify(empty)} 로 나옵니다`);
}

console.log(failed ? `\n✗ ${failed}건 실패` : '\n채널 링크 처리 이상 없음');
process.exitCode = failed ? 1 : 0;
