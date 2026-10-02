#!/usr/bin/env node
/**
 * `compose()` 가 「Hermes 글의 `[정정]`」과 「다른 봇 글에 달린 답글」을 **따로** 세나 —
 * 2026-08-28(커밋 `7c6d02c`)에 고친 축의 회귀 시험.
 *
 *   node scripts/check-compose-bot-replies.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 *
 * ── 왜 있나 ──
 *
 * 8/28 17:00 회차가 다른 봇(Calendar Bot) 글에 달린 사람 인사 답글 2건을 「Hermes 글에
 * 달린 `[정정]` 2건」으로 DM 했다 — 세는 칸이 `selfCorrections` 하나뿐이라 봇 전반으로
 * 거르는 쪽을 넓힌 커밋(`1a121d3`)에서 세는 쪽만 안 갈렸다. 고친 커밋은 칸을 둘로
 * 나눴다 — `selfCorrections`(Hermes 글의 `[정정]`)와 `botReplies`(다른 봇 글의 답글).
 *
 * 그 커밋 메시지는 "compose() 회귀 테스트를 red-green 으로 돌렸다"고 적었지만 그 시험은
 * 저장소에 없었다 — `scripts/check-channel-refs.js` 가 `compose()` 를 부르긴 하지만
 * 「개명 보고」 축만 본다(`grep -c "봇 글\|정정" check-channel-refs.js` = 0). 이 축은
 * 여전히 무방비였다. 이 시험이 그 자리를 채운다.
 *
 * `compose` 는 순수 함수라 아카이브도 슬랙도 필요 없다 — `check-channel-refs.js` 와 같다.
 */
import { compose } from '../src/ingest/report.js';

let ok = true;
const check = (label, cond, extra = '') => {
  ok &&= !!cond;
  console.log((cond ? '  PASS ' : '  FAIL ') + label + (extra ? `  ${extra}` : ''));
};

console.log('\ncompose — 봇 글 답글만 있을 때 (이번에 고친 축의 핵심 회귀)');
{
  // 8/28 사고를 줄인 모양 — Calendar Bot 글에 달린 인사 답글 2건. selfCorrections 는 0건.
  const r = compose({
    dry: true,
    conversations: {
      totalBotReplies: 2,
      channels: [{ channel: '채널가', botReplies: 2 }],
    },
  });
  check('봇 글 답글 줄이 나온다', /봇 글에 달린 답글/.test(r), `(${JSON.stringify(r)})`);
  check('건수가 맞다', /\*2건\*/.test(r));
  check('채널·건수가 나온다', /#채널가 2/.test(r));
  // 사고의 핵심 — 남의 인사 답글이 [정정] 으로 보고되면 안 된다.
  check('[정정] 으로 잘못 불리지 않는다 (사고 재현 방지)', !/\[정정\]/.test(r));
  check('"Hermes 글에 달린" 문구가 안 붙는다', !/Hermes 글에 달린/.test(r));
}

console.log('\ncompose — Hermes 자기 정정만 있을 때 (대조군)');
{
  const r = compose({
    dry: true,
    conversations: {
      totalSelfCorrections: 1,
      channels: [{ channel: '채널나', selfCorrections: 1, selfFixLines: ['> **└ 10:00 · 담당자** — 10/20 이 아니라 10/22 입니다'] }],
    },
  });
  check('Hermes 정정 줄이 나온다', /Hermes 글에 달린.*\[정정\]/.test(r), `(${JSON.stringify(r)})`);
  check('건수가 맞다', /\*1건\*/.test(r));
  check('정정 문장 미리보기가 붙는다', /10\/22/.test(r));
  // 반대 방향 — 자기 정정이 「봇 글 답글」로 잘못 불리면 안 된다.
  check('"봇 글에 달린 답글" 로 안 불린다', !/봇 글에 달린 답글/.test(r));
}

console.log('\ncompose — 둘 다 있을 때 한 칸으로 합쳐지지 않는다 (사고의 원인이었던 자리)');
{
  const r = compose({
    dry: true,
    conversations: {
      totalSelfCorrections: 1,
      totalBotReplies: 2,
      channels: [
        { channel: '채널나', selfCorrections: 1, selfFixLines: ['정정 문장'] },
        { channel: '채널가', botReplies: 2 },
      ],
    },
  });
  check('정정 줄과 봇글답글 줄이 둘 다 나온다',
    /Hermes 글에 달린.*\[정정\].*\*1건\*/.test(r) && /봇 글에 달린 답글.*\*2건\*/.test(r),
    `(${JSON.stringify(r)})`);
  // 사고의 정확한 모양 — 합쳐서 "[정정] 3건" 이 되면 안 된다.
  check('셋으로 합쳐진 숫자가 안 나온다 (1+2 를 합치지 않는다)', !/\*3건\*/.test(r));
}

console.log('\ncompose — 봇 글 답글이 0건이면 그 줄 자체가 안 나온다 (조용해야 매일 읽힌다)');
{
  const r = compose({
    dry: true,
    conversations: { totalBotReplies: 0, channels: [] },
  });
  check('알릴 것이 없으면 null', r === null, `(${JSON.stringify(r)})`);
}

console.log(ok ? '\n전부 통과' : '\n실패 있음');
process.exit(ok ? 0 : 1);
