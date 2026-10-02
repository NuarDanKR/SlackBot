#!/usr/bin/env node
/**
 * 설정이 가리키는 채널 이름이 슬랙 실물과 맞는지 — **개명에 조용히 끊기는 자리**.
 *
 * `privateChannels`·`digest.skipChannels` 는 채널을 **이름**으로 가리키는데 수집은 **ID** 로
 * 따라간다. 그래서 슬랙에서 채널 이름을 바꾸면 설정 줄이 아무것도 안 가리키는 문자열이 되고,
 * 에러도 경고도 안 난다.
 *
 * 2026-08-10 에 실제로 그랬다 — `z_비공개마_옛이름` → `z_비공개마` 로 개명됐는데
 * `skipChannels` 에는 옛 이름만 있어 **21일간 아무것도 안 막았다.** 그 사이 자동 반영이
 * 8/12·8/25·8/29 세 번 그 채널을 아카이브했고, 8/31 에 첨부가 수집에 딸려 나와서야 드러났다.
 *
 * 신호가 없던 것이 아니라 **다른 것을 가리켰다.** 개명 보고는 그날 났지만 문구가 「md 파일명은
 * 그대로 두었습니다」뿐이라, 옛 이름을 참조하는 설정 줄이 있다는 말이 없었다. 그래서 두 자리를
 * 함께 지킨다 — 개명 보고가 설정을 짚고(B), 점검이 안 맞는 줄을 감추지 않는다(C).
 *
 * 순수 함수라 아카이브도 슬랙도 필요 없다.
 *
 *   node scripts/check-channel-refs.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 */
import { staleChannelRefs } from '../src/archive.js';
import { channelRefsIn, config } from '../src/config.js';
import { compose } from '../src/ingest/report.js';

let ok = true;
const check = (label, cond, extra = '') => {
  ok &&= !!cond;
  console.log((cond ? '  PASS ' : '  FAIL ') + label + (extra ? `  ${extra}` : ''));
};

/* ────────────────────────────────────────────────────────────
 * C — 점검이 안 맞는 설정 줄을 감추지 않는다
 *
 * 세 갈래를 **가려서** 낸다. 안 가르면 오늘 이 워크스페이스에서만 6번 울리고 그중 5번이
 * 정상이라, 매일 울리는 경고가 되어 곧 안 읽힌다.
 * ──────────────────────────────────────────────────────────── */
console.log('\nstaleChannelRefs');

/* 2026-08-31 의 실제 상황을 줄인 것 */
const live = [
  { id: 'C_REN', name: 'z_비공개마', isPrivate: false },  // 개명된 뒤의 이름
  { id: 'C_OK', name: '비공개가', isPrivate: true },
];
const known = [
  // 개명 흔적: 현재 이름은 새 것이고 md 파일명(file)에 옛 이름이 남는다
  { id: 'C_REN', name: 'z_비공개마', file: 'z_비공개마_옛이름' },
  { id: 'C_OK', name: '비공개가', file: '비공개가' },
  { id: 'C_GONE', name: '비공개다', file: '비공개다' },  // 슬랙에서 없어짐
];
const lists = {
  privateChannels: ['비공개가'],
  skipChannels: ['z_비공개마_옛이름', '비공개다', '한번도없던이름'],
};

const refs = staleChannelRefs({ live, known, lists });
const by = (n) => refs.find((r) => r.name === n);

check('실물과 맞는 이름은 아예 안 나온다', !by('비공개가'));

/* 개명 — 이것이 위험한 갈래다. 채널은 살아 있는데 설정 줄만 죽는다 */
check('개명된 것을 개명으로 가른다', by('z_비공개마_옛이름')?.kind === 'renamed');
check('개명이면 바뀐 이름을 알려준다',
  by('z_비공개마_옛이름')?.now === 'z_비공개마');
check('개명은 어느 목록의 줄인지 밝힌다',
  by('z_비공개마_옛이름')?.list === 'skipChannels');

/* 없어진 채널 — 의도적으로 남겨 둔 줄이라 개명과 같은 무게로 울리면 안 된다 */
check('없어진 채널을 개명과 가른다', by('비공개다')?.kind === 'gone');

/* 한 번도 본 적 없는 이름 — 오타일 수도, 처음부터 안 다룬 채널이 개명된 것일 수도 있다.
 * 모르는 것을 안다고 하지 않는다. */
check('본 적 없는 이름은 모른다고 한다', by('한번도없던이름')?.kind === 'unknown');

/* 이 순서가 화면 순서다 — 위험한 것이 먼저 와야 없어진 채널 다섯 줄에 안 묻힌다 */
check('개명이 맨 앞에 온다', refs[0]?.kind === 'renamed');

/* 목록이 비어 있으면 아무것도 안 낸다 (정상일 때 조용해야 매일 읽힌다) */
check('전부 맞으면 빈 배열',
  staleChannelRefs({ live, known, lists: { privateChannels: ['비공개가'], skipChannels: [] } }).length === 0);

/* ────────────────────────────────────────────────────────────
 * B — 개명 보고가 설정을 짚는다
 *
 * 8/10 에 신호가 실제로 있던 자리다. 여기서 한 줄만 더 했으면 21일이 그날로 끝났다.
 * ──────────────────────────────────────────────────────────── */
console.log('\ncompose — 개명 보고');

const withRef = compose({
  conversations: {
    renamed: [{ from: 'z_비공개마_옛이름', to: 'z_비공개마', inConfig: ['skipChannels'] }],
  },
});
check('옛 이름이 설정에 있으면 그 사실을 적는다', /skipChannels/.test(withRef));
check('설정을 고쳐야 한다고 말한다', /config\.json/.test(withRef));

const noRef = compose({
  conversations: { renamed: [{ from: '사업장나', to: '사업장나_2026', inConfig: [] }] },
});
check('설정에 없으면 그 줄을 안 붙인다 (잡음 안 만들기)', !/config\.json/.test(noRef));
check('설정에 없어도 개명 자체는 보고한다', /사업장나_2026/.test(noRef));

/* 두 목록에 다 있으면 둘 다 적는다 — 하나만 고치고 닫으면 나머지가 조용히 남는다 */
const both = compose({
  conversations: {
    renamed: [{ from: '비공개바', to: '비공개-바-2', inConfig: ['privateChannels', 'skipChannels'] }],
  },
});
check('두 목록에 다 있으면 둘 다 적는다',
  /privateChannels/.test(both) && /skipChannels/.test(both));

/* ────────────────────────────────────────────────────────────
 * B 를 실제로 채우는 자리 — `compose` 는 받은 것을 찍을 뿐이다.
 *
 * 이 함수가 빈 배열만 주면 위 보고 줄은 **영원히 안 뜬다.** 시험이 초록인데 실물은 조용한,
 * 이 저장소가 여러 번 겪은 모양이라 따로 지킨다.
 * ──────────────────────────────────────────────────────────── */
console.log('\nchannelRefsIn — 실제 config 와 대본다');

check('안 적힌 이름에는 빈 배열', channelRefsIn('그런채널없음').length === 0);

/* 실물 config 를 그대로 본다 — 목록 이름을 바꾸면 여기서 걸린다.
 *
 * **이름을 여기 박지 않는다.** 코드 저장소는 팀끼리 나눠 쓰므로 채널 이름이 들어가면
 * 안 되고(2026-09-01), 박아 두면 그 채널이 없는 팀에서 이 검사가 「못 찾는다」로
 * 빨개진다 — 원인이 자기 설정에 그 이름이 없다는 것뿐인데 그 사실이 화면에 안 나온다.
 * 그래서 그 팀의 설정에서 하나씩 골라 쓴다. 어느 것이든 상관없다.
 *
 * 목록이 비어 있으면 **잰 척하지 않고 못 쟀다고 말한다.** 여기서 조용히 통과시키면
 * 「위 보고가 영원히 안 뜬다」를 잡으려고 둔 이 절이 아무것도 안 잡는 절이 된다.
 *
 * **못 잰 갈래는 세어서 마지막 줄에 낸다** (2026-09-01). 전에는 화면에만 적고 종료코드가
 * 0 이라 마지막 줄이 그냥 「전부 통과」였다 — 세 갈래가 다 건너뛰어도 같은 말이 나왔다.
 * 특히 아래 `inBoth` 는 두 목록에 **다 든** 채널이 있어야 성립하는데, 그것을 성립시키는
 * 채널을 어느 한쪽 목록에서 지우는 순간(있을 수 있는 정리다) 이 검사가 소리 없이
 * 사라지고 화면은 계속 「전부 통과」라고 말한다.
 *
 * 종료코드는 **하나라도 쟀으면 0** 이다. 설정이 통째로 빈 새 팀은 잴 것이 애초에 없어서
 * 「못 잼」이 아니라 「잴 것이 없음」이고, 그걸 실패로 내면 그 팀은 매일 빨간 줄을 보며
 * 무시하는 법을 배운다 (`check-business-names.js` 의 null/[] 구분과 같은 판단이다).
 * 다만 **아무것도 못 쟀으면** 이 절이 통째로 헛돈 것이므로 실패로 낸다. */
const PRIV = config.privateChannels || [];
const SKIP = (config.digest && config.digest.skipChannels) || [];

let unmeasured = 0;
let measured = 0;
const cannot = (why) => { unmeasured += 1; console.log(`  - 못 잼: ${why}`); };

if (!PRIV.length) cannot('privateChannels 가 비어 있습니다');
else {
  measured += 1;
  check('# 를 붙여도 같게 본다',
    channelRefsIn(`#${PRIV[0]}`).length === channelRefsIn(PRIV[0]).length);
  const priv = channelRefsIn(PRIV[0]);
  check('privateChannels 의 이름을 찾는다', priv.includes('privateChannels'), `(${priv.join(',')})`);
}

if (!SKIP.length) cannot('skipChannels 가 비어 있습니다');
else {
  measured += 1;
  const skip = channelRefsIn(SKIP[0]);
  check('skipChannels 의 이름을 찾는다', skip.includes('skipChannels'), `(${skip.join(',')})`);
}

/* 두 목록에 다 있는 이름 — 하나만 돌려주면 위 보고가 반쪽이 된다 */
const inBoth = PRIV.find((c) => SKIP.includes(c));
if (!inBoth) cannot('두 목록에 다 든 채널이 설정에 없습니다');
else {
  measured += 1;
  const both2 = channelRefsIn(inBoth);
  check('두 목록에 다 있으면 둘 다 돌려준다',
    both2.includes('privateChannels') && both2.includes('skipChannels'), `(${both2.join(',')})`);
}

/* 아무것도 못 쟀으면 이 절은 헛돈 것이다 — 통과로 내지 않는다.
 *
 * 새 팀도 여기서 빨개진다. **그게 맞다** — `privateChannels` 가 빈 것은 「설정이 덜 됐다」는
 * 뜻이고, 비공개 채널을 안 적으면 자동 반영이 그 채널을 아예 아카이브하지 않는다.
 * 안 적는 것은 보호가 아니라 유실이라 빨간 것이 옳다. 무엇을 하면 되는지 함께 적는다. */
if (!measured) {
  console.log('\n실패 있음 — 이 절이 **아무것도 재지 못했습니다**');
  console.log('  자료 저장소 config.json 의 privateChannels 와 digest.skipChannels 가 둘 다 비어 있습니다.');
  console.log('  슬랙에서 비공개인 채널을 privateChannels 에 빠짐없이 적으세요 —');
  console.log('  안 적힌 비공개 채널은 아카이브에 아예 안 들어가고 매일 경고만 납니다.');
  process.exit(1);
}
const tail = unmeasured ? ` (못 잰 갈래 ${unmeasured}개)` : '';
console.log(ok ? `\n전부 통과${tail}` : `\n실패 있음${tail}`);
process.exit(ok ? 0 : 1);
