#!/usr/bin/env node
/**
 * 자동 반영 커밋 메시지 점검 — `buildCommitMessage` 가 무엇을 했는지 말하나.
 *
 * 왜 있나: CLAUDE.md 는 「자동 반영이 돌았는지는 DM 유무가 아니라 `git log --author=Hermes`
 * 로 판정하라」고 한다. 그런데 예전에는 derive 가 `index.md` 를 16곳 고쳐도 새 대화가
 * 0건이면 메시지가 `동기화 시각만 갱신 (새 대화 없음)` 이었다 — 판정하라고 정해 둔 그
 * 로그가 정작 무엇을 고쳤는지 안 말했다.
 *
 * **이 문구는 다른 방법으로 확인할 수 없다.** 위생 점검이 아니라 자동 반영이 만들기 때문에
 * `npm run health -- --dry` 로는 안 나오고, 실제로 보려면 VM 이 07:00 에 도는 것을 기다려야
 * 한다. 그래서 메시지 만드는 자리를 순수 함수로 떼고 여기서 직접 부른다.
 *
 * 보는 것 여섯 — 앞 다섯은 커밋 메시지, 마지막은 같은 축의 DM 문구다:
 *  ① 새 대화가 있으면 제목에 건수가 든다
 *  ② 아무것도 없으면 `동기화 시각만 갱신`
 *  ③ **새 대화 0건인데 derive 가 고쳤으면 그 수가 든다** (이 검사의 본체)
 *  ④ 새 대화가 있을 때도 derive 가 고친 것이 본문에 든다
 *  ⑤ 로그 갱신만 있을 때도 그것을 말한다
 *  ⑥ 파생값이 실패했을 때 DM 이 「헤더는 이미 갱신됐을 수 있다」고 사실대로 밝힌다
 *
 * 실행: node scripts/check-commit-message.js
 */
import { buildCommitMessage } from '../src/ingest/index.js';
import { compose } from '../src/ingest/report.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

const CH = [{ channel: '사업장나', added: 3, threadReplies: 1, selfCorrections: 0, botReplies: 0 }];

// ① 새 대화가 있는 평범한 회차
{
  const m = buildCommitMessage({ added: 3, replies: 1, channels: CH });
  if (m.startsWith('아카이브 자동 반영 — 대화 3건 · 답글 1건')) {
    ok('새 대화가 있으면 제목에 건수가 든다');
  } else {
    bad(`제목이 다릅니다: ${JSON.stringify(m.split('\n')[0])}`);
  }
  if (m.includes('#사업장나 3건 (+답글 1)')) {
    ok('채널별 내역이 든다');
  } else {
    bad(`채널 줄이 없습니다:\n${m}`);
  }
}

/* ①-2 중간에 멈춘 채널의 건수는 「반영된 것」이 아니라고 말한다.
 *
 * 이 숫자는 슬랙에서 **읽어 온 것**이다. 회차가 중간에 멈추면 상태가 저장되지 않아
 * 다음 회차가 같은 것을 다시 읽고 md 쪽 중복 판정이 조용히 걸러 내므로, 파일은 한
 * 글자도 안 바뀌었는데 커밋 메시지에는 매일 같은 건수가 찍힌다 — 2026-09-22 에 어느
 * 채널이 그랬고(그 파일의 blob 해시가 전후 동일했다), 그 줄만 보면 매일 반영되는
 * 것처럼 읽혔다. 숫자를 지우지는 않는다. 지우면 아예 조용했던 날과 구별이 안 된다. */
{
  const m = buildCommitMessage({
    added: 3,
    replies: 1,
    channels: [...CH, {
      channel: '사업장다', added: 10, threadReplies: 0, error: '스레드 덧붙이기 실패',
    }],
  });
  if (m.includes('#사업장다 10건 — 중간에 멈췄습니다')) {
    ok('멈춘 채널 줄에 그렇다고 적힌다');
  } else {
    bad(`멈춘 표시가 없습니다:\n${m}`);
  }
  if (m.includes('또 찍힐 수 있습니다') && m.includes('#사업장다')) {
    ok('또 찍힐 수 있다는 것과 어느 채널인지가 본문에 든다');
  } else {
    bad(`경고 줄이 없습니다:\n${m}`);
  }
  if (!m.includes('#사업장나 3건 (+답글 1) —')) {
    ok('멀쩡한 채널 줄에는 안 붙는다');
  } else {
    bad(`멀쩡한 채널에 멈춤 표시가 붙었습니다:\n${m}`);
  }
}

/* ①-3 **한 건도 못 읽고 멈춘 채널**은 합계를 걸고 넘어지지 않는다.
 *
 * 가장 흔한 실패가 이 모양이다 — 바깥 catch 가 만드는 결과가 `{ added: 0, error }`
 * (조회 실패·레이트리밋)라, 위 채널 목록에 줄도 없고 합계에 한 건도 안 보탠다. 그런데도
 * 「합계가 오염됐다」고 적으면 **멀쩡한 숫자를 못 믿게 만든다.** 그 채널이 오늘 통째로
 * 빠졌다는 사실만 말한다. */
{
  const m = buildCommitMessage({
    added: 3,
    replies: 1,
    channels: [...CH, { channel: '사업장다', added: 0, threadReplies: 0, error: '조회 실패' }],
  });
  if (!m.includes('또 찍힐 수 있습니다')) {
    ok('셈에 안 든 실패는 합계를 걸고 넘어지지 않는다');
  } else {
    bad(`합계 경고가 잘못 붙었습니다:\n${m}`);
  }
  if (m.includes('한 건도 못 읽고 멈춘 채널') && m.includes('#사업장다')) {
    ok('대신 통째로 빠졌다고 말한다');
  } else {
    bad(`빠진 채널을 안 알립니다:\n${m}`);
  }
}

/* ①-4 **합계가 전부 0 인 날에도 실패는 말한다.**
 *
 * 이 갈래는 한 줄로 끝나는 분기라 실패 경고가 통째로 사라졌다 — 그런데 위 catch 실패가
 * 만드는 것이 바로 `added 0` 이라, **채널이 통째로 안 들어온 날의 커밋 메시지가 조용한
 * 날과 글자가 같았다.** */
{
  const m = buildCommitMessage({
    channels: [{ channel: '사업장다', added: 0, error: '조회 실패' }],
  });
  if (m.startsWith('아카이브 자동 반영 —') && m.includes('한 건도 못 읽고 멈춘 채널')) {
    ok('조용한 날 분기에서도 실패가 남는다');
  } else {
    bad(`조용한 날에 실패가 사라졌습니다:\n${JSON.stringify(m)}`);
  }
}

// ② 정말 아무것도 없는 날
{
  const m = buildCommitMessage({});
  if (m === '아카이브 자동 반영 — 동기화 시각만 갱신 (새 대화 없음)') {
    ok('아무것도 없으면 전과 같은 한 줄');
  } else {
    bad(`문구가 다릅니다: ${JSON.stringify(m)}`);
  }
}

// ③ 이 검사의 본체 — 새 대화 0건인데 derive 가 16곳 고친 날
{
  const m = buildCommitMessage({ derived: 16 });
  if (m.includes('색인 16곳 갱신')) {
    ok('새 대화 0건이어도 색인을 고쳤으면 그 수를 말한다');
  } else {
    bad(`무엇을 했는지 안 말합니다: ${JSON.stringify(m)}`);
  }
  if (m.includes('새 대화 없음')) {
    ok('새 대화가 없었다는 것도 함께 말한다');
  } else {
    bad(`「새 대화 없음」이 빠졌습니다: ${JSON.stringify(m)}`);
  }
  if (!m.includes('동기화 시각만 갱신')) {
    ok('「시각만 갱신」이라고 잘못 말하지 않는다');
  } else {
    bad(`한 일이 있는데 「시각만 갱신」이라고 합니다: ${JSON.stringify(m)}`);
  }
}

// ④ 새 대화도 있고 derive 도 고친 날 — 본문에 함께 들어야 한다
{
  const m = buildCommitMessage({ added: 2, derived: 4, channels: CH });
  if (m.includes('색인 4곳 갱신')) {
    ok('새 대화가 있는 날에도 색인 갱신을 적는다');
  } else {
    bad(`본문에 색인 갱신이 없습니다:\n${m}`);
  }
}

// ⑤ 로그만 갱신된 날 (derive 는 0)
{
  const m = buildCommitMessage({ logged: 5 });
  if (m.includes('Hermes 대화 로그 5개 파일 갱신') && m.includes('새 대화 없음')) {
    ok('로그만 갱신된 날도 그것을 말한다');
  } else {
    bad(`문구가 다릅니다: ${JSON.stringify(m)}`);
  }
}

/* ⑥ 파생값이 실패했을 때의 DM 문구 — 같은 축이라 여기서 함께 본다.
 *
 * `sync_index.py` 는 채널 헤더를 **먼저 다 쓰고** 그 뒤에 `index.md` 를 손대는데,
 * 불변식은 `index.md` 쓰기 직전에만 본다. 그래서 「둘 다 낡은 채로 남았다」는 절반만
 * 맞다 — 헤더는 이미 고쳐졌을 수 있고, 그 말을 믿으면 사람이 갈린 상태를 안 본다. */
{
  const text = compose({
    ok: true,
    conversations: { totalAdded: 0, channels: [] },
    derive: { failed: '쓰기 불변식 위반 — 줄 수가 달라졌습니다', changed: [], unresolved: [] },
  });
  if (text.includes('채널 md 헤더는 이미 갱신됐을 수 있습니다')) {
    ok('파생값 실패 DM 이 「헤더는 이미 갱신됐을 수 있다」고 밝힌다');
  } else {
    bad(`문구가 절반만 맞습니다:\n${text}`);
  }
  if (!text.includes('건수 표가 낡은 채로 남았습니다')) {
    ok('「둘 다 낡았다」는 옛 문구가 안 남아 있다');
  } else {
    bad('옛 문구가 그대로입니다 — 헤더는 이미 고쳐졌을 수 있습니다');
  }
}

console.log(failed ? `\n✗ ${failed}건 실패` : '\n커밋 메시지 이상 없음');
process.exitCode = failed ? 1 : 0;
