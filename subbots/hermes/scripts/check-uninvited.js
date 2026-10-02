#!/usr/bin/env node
/**
 * uninvitedChannels 검사 — 「아직 초대되지 않은 채널」 목록이 무엇을 모집단으로 삼는지.
 *
 * 여기서 지키는 것은 **조용히 빠지는 것**이다. 목록이 짧아지는 쪽으로 틀리면 에러가 안 나고
 * 「초대할 것 없음」이라는 정상 화면으로만 보인다.
 *
 * 2026-08-15 에 실제로 그랬다 — 모집단이 `.sync-state.json`(아카이브가 아는 채널)이었는데
 * 그 파일에 채널을 적는 것은 자동 반영뿐이고 그 열거가 `users.conversations`(봇이 이미
 * 멤버인 채널)라, **봇이 한 번도 안 들어간 채널은 원리상 목록에 못 오른다.** 8/3 에 만들어진
 * `#사업장하` 이 `join_channels.py` 에만 뜨고 `npm run check` 에는 안 뜬 것이 그 경우다.
 *
 * 순수 함수라 아카이브도 슬랙도 필요 없다.
 *
 *   node scripts/check-uninvited.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 */
import { uninvitedChannels } from '../src/archive.js';

let ok = true;
const check = (label, cond, extra = '') => {
  ok &&= !!cond;
  console.log((cond ? '  PASS ' : '  FAIL ') + label + (extra ? `  ${extra}` : ''));
};

/* 2026-08-15 의 실제 상황을 줄인 것 */
const live = [
  { id: 'C_NEW', name: '사업장하', isPrivate: false, members: 1 },   // 슬랙에만 있다
  { id: 'C_JOIN', name: '사업장나', isPrivate: false, members: 12 },     // 봇이 들어가 있다
  { id: 'C_SKIP', name: 'a_비공개라', isPrivate: false, members: 0 },   // 안 다루기로 한 것
];
const known = [
  { id: 'C_JOIN', name: '사업장나', private: false },
  { id: 'C_SKIP', name: 'a_비공개라', private: false },
  { id: 'C_GONE', name: '비공개다', private: false },            // 기록에만 있다
];
const base = {
  live,
  known,
  joinedIds: new Set(['C_JOIN']),
  archived: new Set(['사업장나', 'a_비공개라']),
  skip: ['a_비공개라', '비공개바'],
};

console.log('\nuninvitedChannels');

/* ① 슬랙에만 있는 새 채널이 잡힌다 — 고치기 전에는 이것이 안 떴다 */
const r = uninvitedChannels(base);
check('슬랙에만 있는 새 채널이 잡힌다',
  r.actionable.some((c) => c.name === '사업장하'),
  `초대할 것: ${r.actionable.map((c) => c.name).join(' · ') || '없음'}`);

/* ②
 * 아카이브 기록에만 있는 채널도 안 사라진다. 슬랙 실물로 통째로 갈아타면 이쪽이 빠지는데,
 * 봇이 비공개 채널에서 빠졌을 때가 정확히 그 모양이다 — 한 방향만 고치면 구멍이 옮겨간다.
 */
check('기록에만 있는 채널도 남는다',
  r.actionable.some((c) => c.name === '비공개다'));

/* ③ 옛 모집단(기록만)이었다면 새 채널은 원리상 안 보인다 */
const oldWay = uninvitedChannels({ ...base, live: [] });
check('기록만 보면 새 채널을 놓친다',
  !oldWay.actionable.some((c) => c.name === '사업장하'));

/* ④ 안 다루기로 한 채널은 초대 목록에서 빠지되 감춰지지 않는다 */
check('skipChannels 는 초대 목록에서 빠진다',
  !r.actionable.some((c) => c.name === 'a_비공개라'));
check('skipChannels 는 감춰지지 않고 따로 보인다',
  r.ignored.map((c) => c.name).join() === 'a_비공개라',
  `안 다룸: ${r.ignored.map((c) => c.name).join(' · ') || '없음'}`);

/* ⑤ 재지 않은 것과 잰 것이 구별된다 — 화면 문구가 여기에 기댄다 */
const fresh = r.actionable.find((c) => c.name === '사업장하');
const gone = r.actionable.find((c) => c.name === '비공개다');
check('슬랙에 있는 것은 멤버 수를 안다',
  fresh?.members === 1 && fresh?.inSlack === true && fresh?.hasArchive === false);
check('기록에만 있는 것은 멤버 수를 「모름」으로 둔다',
  gone?.members === null && gone?.inSlack === false,
  `members=${gone?.members}`);

/* ⑥ 맞추는 열쇠는 이름이 아니라 ID 다 — 개명한 채널이 미참여로 둔갑하면 안 된다 */
const renamed = uninvitedChannels({
  ...base,
  live: [{ id: 'C_JOIN', name: '사업장나_2026', isPrivate: false, members: 12 }],
});
check('개명해도 참여 중으로 본다',
  !renamed.actionable.some((c) => c.id === 'C_JOIN'));

/* ⑦ 공개 여부는 슬랙이 사실이다 — config 의 privateChannels 는 손으로 적는 목록이라 어긋난다 */
const opened = uninvitedChannels({
  ...base,
  live: [{ id: 'C_GONE', name: '비공개다', isPrivate: false, members: 3 }],
  known: [{ id: 'C_GONE', name: '비공개다', private: true }],
});
check('슬랙이 공개라고 하면 공개로 본다',
  opened.actionable.find((c) => c.id === 'C_GONE')?.private === false);

console.log(ok ? '\n전부 통과' : '\n실패 있음');
process.exit(ok ? 0 : 1);
