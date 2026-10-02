#!/usr/bin/env node
/**
 * pending-work.js 검사 — 목록을 합치는 규칙이 실제로 지켜지는지.
 *
 * 여기서 지키는 것은 **조용히 깨지는 것들**이다. 셋 다 에러가 안 나고, 다음 날 아침에
 * 「볼 것이 없다」는 화면으로만 드러난다.
 *
 *   ① 17:00 회차(요약 대조를 끄고 도는 회차)가 아침에 잡아 둔 요약 항목을 지우는 것
 *   ② 나이가 매일 1일로 되돌아가는 것 — 그러면 「며칠째」가 영영 안 늘어난다
 *   ③ 사람이 정해서 해소된 항목이 안 사라지는 것
 *
 * mergeItems 는 파일을 안 읽는 순수 함수라 임시 아카이브가 필요 없다
 * (파일을 읽는 것은 suppress 의 summaryHash 뿐이고, 그건 여기서 안 부른다).
 *
 *   node scripts/check-pending-work.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { mergeItems, buildItems, nextGenerated, suppress } from '../src/ingest/pending-work.js';
import { verifyEvidence, fold as jsFold } from '../src/ingest/summary.js';
import { DATA_ROOT } from '../src/config.js';

// 저장소 뿌리 — cwd 가 아니라 이 파일의 위치에서 잡는다. 다른 폴더에서 불러도 깨지지 않게.
const REPO_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));

let ok = true;
const check = (label, cond, extra = '') => {
  ok &&= !!cond;
  console.log((cond ? '  PASS ' : '  FAIL ') + label + (extra ? `  ${extra}` : ''));
};

const SUMMARY_ID = 'a|summary|수치|핵심 쟁점';
const morning = [{ id: SUMMARY_ID, kind: 'summary', channel: 'a', firstSeen: '2026-08-10' }];

console.log('\nmergeItems');

/* ① 대조를 안 한 회차는 요약 항목에 손대지 않는다 */
const evening = mergeItems({ prev: morning, fresh: [], today: '2026-08-12' });
check('17:00 회차가 요약 항목을 지우지 않는다', evening.length === 1, `남은 것 ${evening.length}건`);

/* ② 요약 항목도 **사람이 정할 때까지 남는다** — 다시 안 잡혔다고 치우지 않는다.
 *
 * 전에는 「대조한 회차에서 다시 안 잡힘 = 사람이 고쳐서 해소됨」으로 읽고 치웠다.
 * **그 전제가 틀렸다.** 대조에 들어가는 원문은 그 회차에 **새로 들어온 메시지뿐**이고
 * (`index.js` 의 touched → `summary.js` 의 checkSummaries 가 `t.transcript` 를 넘긴다),
 * 지난 회차의 후보가 그 안에서 다시 잡힐 이유는 애초에 없다. 그러니 이 판정이 실제로
 * 재는 것은 「해소됐나」가 아니라 「그 채널에 아무 메시지나 들어왔나」였다 —
 * **상관없는 한 줄만 들어와도 미결 요약 항목이 통째로 사라진다.** 요약은 틀린 채
 * 봇 프롬프트에 실려 남고 목록에서만 없어진다. 에러는 안 난다.
 *
 * 2026-09-04 에 이 판정을 회차 단위에서 채널 단위로 좁혔지만, 같은 구멍이 채널 안에
 * 그대로 남아 있었다 (WHK 지적 2026-09-07 — 그날 실물은 아직 안 터진 잠복 상태였다:
 * 항목이 걸린 두 채널에 09-04 이후 메시지가 안 들어왔다).
 *
 * 그래서 종결을 **사람의 결정 하나**로 모은다 — 「반영」·「빼」·「나중에」 셋 다
 * `.sync-state.json` 에 남아 저장소에 들어가고, 그것을 거르는 것은 `suppress` 뿐이다.
 *
 * **아래 두 검사는 `summaryChecked`·`checkedChannels` 를 일부러 그대로 넘긴다.** 지금
 * 구현은 그 둘을 안 받는데, 안 넘기면 옛 구현으로 돌려도 그대로 통과해서 **검사가
 * 아무것도 가르지 못한다**(빼고 돌려 확인했다). 옛 방아쇠를 먹여 두어야 누가 자동 치움을
 * 되살렸을 때 여기가 빨개진다. */
const cleared = mergeItems({ prev: morning, fresh: [], today: '2026-08-12', summaryChecked: true });
check('재검출 안 된 요약 항목도 남는다', cleared.length === 1, `남은 것 ${cleared.length}건`);

/* ②-2 채널에 **엉뚱한 메시지**가 들어와 대조가 돌아도 미결 항목은 그대로 있어야 한다.
 *     `y` 는 이번 회차에 대조된 채널인데 그 항목이 다시 안 잡혔다 — 요약이 고쳐져서가
 *     아니라 새 메시지가 그 얘기가 아니어서다. 둘을 가릴 방법이 없으니 안 치운다. */
const perChannel = mergeItems({
  prev: [
    { id: 'x|summary|수치|핵심 쟁점', kind: 'summary', channel: 'x', firstSeen: '2026-08-10' },
    { id: 'y|summary|수치|핵심 쟁점', kind: 'summary', channel: 'y', firstSeen: '2026-08-10' },
  ],
  fresh: [],
  today: '2026-08-12',
  summaryChecked: true,
  checkedChannels: new Set(['y']),
});
check('대조 안 한 채널의 요약 항목은 남는다', perChannel.some((i) => i.channel === 'x'),
  `남은 것: ${perChannel.map((i) => i.id).join(', ') || '없음'}`);
check('엉뚱한 메시지로 대조된 채널의 항목도 남는다', perChannel.some((i) => i.channel === 'y'),
  `남은 것: ${perChannel.map((i) => i.id).join(', ') || '없음'}`);

/* ③ 같은 id 가 다시 오면 firstSeen 을 이월한다 — 문구가 조금 달라져도 나이는 지켜야 한다 */
const again = mergeItems({
  prev: morning,
  fresh: [{ id: SUMMARY_ID, kind: 'summary', channel: 'a', now: '문구가 조금 달라짐' }],
  today: '2026-08-12',
});
check('나이가 이월된다', again[0]?.firstSeen === '2026-08-10', `firstSeen=${again[0]?.firstSeen}`);

/* ④ 요약이 아닌 항목(새 채널 등)은 대조 여부와 무관하게 사람이 정할 때까지 남는다.
 *    이것들은 다음 회차에 다시 안 잡힌다 — 치우면 그대로 사라진다. */
const note = [{ id: 'b|new-channel', kind: 'new-channel', channel: 'b', firstSeen: '2026-08-09' }];
const keptNote = mergeItems({ prev: note, fresh: [], today: '2026-08-12' });
check('요약이 아닌 항목은 다시 안 잡혀도 남는다', keptNote.length === 1);

/* ⑤ 처음 보는 항목에는 오늘 날짜가 붙는다 */
const first = mergeItems({
  prev: [],
  fresh: [{ id: 'c|summary|누락|요약', kind: 'summary', channel: 'c' }],
  today: '2026-08-12',
});
check('새 항목은 오늘로 잡힌다', first[0]?.firstSeen === '2026-08-12', `firstSeen=${first[0]?.firstSeen}`);

console.log('\nbuildItems');

/* ⑥ 채널 이름과 md 파일명이 다를 때(개명한 채널) 파일명을 따라간다.
 *    이름으로 md 를 찾으면 그 채널의 요약 해시가 늘 빈 값이 되어 「뺌」이 안 걸린다. */
const built = buildItems({
  summary: { findings: [{ channel: '새이름', type: '수치', where: '핵심 쟁점', was: 'A', now: 'B', evidence: '"근거"' }] },
  conversations: { channels: [{ channel: '새이름', file: '옛이름' }] },
});
check('개명한 채널은 md 파일명을 따라간다', built[0]?.file === '옛이름', `file=${built[0]?.file}`);
check('id 에 바뀐 값(now)이 안 들어간다', built[0]?.id === '새이름|summary|수치|핵심 쟁점', built[0]?.id);

/* ⑦ 한 회차에 같은 자리(채널·종류·자리)로 후보가 둘 오면 **잃지 않는다**.
 *    id 는 회차 간 동일성(나이 보존)을 위한 값인데, 회차 안에서도 유일성을 겸하고 있어
 *    서로 다른 후보 둘이 같은 id 를 받으면 뒤엣것이 앞엣것을 조용히 덮었다.
 *    2026-08-12 첫 실물 회차에서 바로 났다 — DM 은 #사업장다 을 5건 중 2건으로
 *    적었는데 목록에는 1건만 남았고, 사라진 쪽이 만기(8/25) 대응이라는 더 큰 건이었다.
 *    같은 자리를 가리키는 후보들이므로 사람이 고칠 요약 줄도 하나다 — 그래서 항목을
 *    쪼개지 않고 한 항목이 후보를 여럿 담는다(나이도 그대로 지켜진다). */
const twin = buildItems({
  summary: {
    findings: [
      { channel: 'e', type: '새 쟁점', where: '핵심 쟁점', was: 'A', now: '앞 후보', evidence: '"앞 근거"' },
      { channel: 'e', type: '새 쟁점', where: '핵심 쟁점', was: 'A', now: '뒤 후보', evidence: '"뒤 근거"' },
    ],
  },
  conversations: { channels: [{ channel: 'e', file: 'e' }] },
});
check('같은 자리 후보 둘이 한 항목으로 남는다', twin.length === 1, `${twin.length}건`);
check('앞 후보를 잃지 않는다', twin[0]?.now === '앞 후보', `now=${twin[0]?.now}`);
check('뒤 후보도 함께 담긴다', twin[0]?.more?.[0]?.now === '뒤 후보', JSON.stringify(twin[0]?.more));
check('뒤 후보의 근거도 함께 담긴다', twin[0]?.more?.[0]?.evidence === '"뒤 근거"');

/* ⑦-b 「뺀 후보」도 근거를 함께 담는다.
 *    관문(`summary.js` 의 verifyEvidence)이 근거를 원문에서 못 찾아 뺀 항목인데,
 *    주석은 「모델이 지어낸 것일 수도, 검색이 어긋난 것일 수도 있어 사람이 봐야 갈린다」고
 *    적어 두고 정작 **갈릴 재료인 근거를 안 넘겼다.** 화면(`review_work.py` 의 context_for)은
 *    그 값으로 원문을 찾으므로, 없으면 「적힌 것: 없음」이 뜬다 — 모델은 적었는데 화면이
 *    「없음」이라고 말한다. 관문이 뒤지는 것은 **그 회차 대화**뿐이고 화면은 **채널 md
 *    전체**를 뒤지므로, 관문이 버린 것을 화면이 찾아내는 일이 실제로 있다. */
const droppedItems = buildItems({
  summary: {
    dropped: [{
      channel: 'f', type: '수치', where: '핵심 쟁점', now: '후보 문구',
      evidence: '"원문 인용" 이라고 적혀 있음', why: '인용이 원문에 없음: "원문 인용…"',
    }],
  },
  conversations: { channels: [{ channel: 'f', file: 'f' }] },
});
check('뺀 후보에도 근거가 담긴다',
  droppedItems[0]?.evidence === '"원문 인용" 이라고 적혀 있음',
  `evidence=${JSON.stringify(droppedItems[0]?.evidence)}`);

/* ⑦-c 관문(verifyEvidence)이 **모델의 생략 표시를 원문 글자로 치지 않는다.**
 *    모델은 인용 가운데를 `...`·`…` 로 건너뛰어 적는데 그 표시는 원문에 없다. 통짜로
 *    찾으면 원문 어디에도 없어 항목이 `dropped` 로 밀린다 — 화면(`review_work.py` 의
 *    `_needles`)은 2026-08-13 부터 쪼개어 찾으므로 **관문만 못 찾는 상태**가 됐다.
 *    아래 첫 인용은 2026-08-13 07:00 회차에서 모델이 실제로 낸 것과 **같은 모양**이고,
 *    그때는 근거에 인용이 셋이라 생략 없는 앞엣것이 걸려 가려졌다. **인용이 하나뿐이면
 *    그대로 밀린다.**
 *
 *    **원문은 가짜다** — 이 저장소는 팀끼리 나눠 쓰므로 실제 팀 메시지를 픽스처에 두지
 *    않는다(WHK 결정 2026-09-04). 여기서 재는 것은 내용이 아니라 **길이와 자리**이므로
 *    아래 넷을 지키면 성질이 보존된다: 두 조각이 원문에서 떨어져 있을 것 · 조각마다 접기
 *    전 8자 이상일 것 · `절차 끌지 말고` 가 8자이면서 접으면 6자일 것 · `**회신을** 확인`
 *    이 접기 전 8자를 넘으면서 접으면 5자일 것.
 *
 *    셋째 시험이 짝이다 — 생략 표시를 무시하는 것이 「아무거나 통과시킨다」가 되면 안 된다. */
const TR = `**2026-08-12 17:50 · 홍길동**
금일 갑금융이 을기관과 통화를 하여, 사업비 대출 차주인 당사에 만기 연장 요청시에는 취급수수료 중 일부는 감면하고 접수하는 것이 내부 여신지침과 이에따른 채권단협의회 결의사항이라는 "○○건설 채권단협의회" 명의의 회신을 확인했고, 이를 다시 다투기 위해서는 재심의를 신청해야 하는데 이 또한 쉽지 않다고 하니, 그러면 절차 끌지 말고 이번 주에 마무리하자는 의견을 갑금융 측에 전해왔답니다.`;
const gate = (q) => verifyEvidence([{ type: '수치', evidence: `"${q}"` }], TR).kept.length === 1;

check('생략 표시가 든 인용도 관문을 통과한다',
  gate('명의의 회신을 확인했고 ... 그러면 절차 끌지 말고 이번 주에 마무리하자는 의견을'));
check('생략 없는 인용은 그대로 통과한다',
  gate('그러면 절차 끌지 말고 이번 주에 마무리하자는 의견을 갑금융 측에 전해왔답니다'));
check('원문에 없는 인용은 생략 표시가 있어도 버린다',
  !gate('지급보증서를 발급했고 ... 이사회 승인은 다음 달로 미루기로 했습니다'));

/* ⑦-d **8자는 접기 전에 세지만 찾는 것은 접은 뒤 문자열이다** — 그 사이가 벌어진다.
 *    `**회신을** 확인` 은 8자 검사를 통과하는데 기호를 빼면 5자짜리로 찾는다. 3~5자는
 *    같은 채널의 **남의 메시지**에도 22~33% 있어서(아카이브 전량 실측), 근거와 무관한
 *    대화를 원문이라며 보여줄 수 있다. 그래서 **접은 뒤 길이에 바닥 6자**를 따로 둔다.
 *
 *    바닥을 6으로 잡은 것은 `약정 체결 업무`(접으면 6자)가 그 선이기 때문이다 — 그것을
 *    버리면 2026-08-13 에 났던 퇴행 3건이 되돌아온다. 아래 둘째가 그 선을 지킨다. */
check('기호를 빼면 너무 짧아지는 인용은 버린다', !gate('**회신을** 확인'));
check('접은 뒤 6자는 살아남는다', gate('절차 끌지 말고'));

/* ⑦-e **글머리표(`•`)는 원문의 글자이지만 모델은 인용에서 떨어뜨린다.**
 *    슬랙에서 항목을 나열하면 `• ` 로 시작하는 줄이 되는데, 모델이 앞 줄과 이어 인용하면
 *    그 표시가 빠진 문장이 된다. 접기가 그것을 안 지우면 관문이 **원문에 있는 근거를
 *    「원문에 없다」고 버린다.**
 *
 *    아래 인용은 2026-08-20 07:00 회차에서 실제로 밀린 것과 **같은 모양**이다. 그때는
 *    인용이 하나뿐이라 그대로 `dropped` 로 갔고, 사람이 채널 md 를 Grep 해서야 원문이
 *    있다는 것을 알았다. 아카이브 전량 실측으로 `•` 는 305회 중 261회가 줄머리다.
 *
 *    **원문은 가짜다** (위 TR 과 같은 이유). 여기서 재는 것은 「줄머리 `•` 와 줄바꿈을
 *    건너뛰어 이어 적은 인용이 걸리나」이므로, 글머리표 줄 사이에 `<…>` 줄이 끼어 있고
 *    이어 적은 인용이 접기 뒤 30자를 넘기만 하면 성질이 보존된다.
 *
 *    셋째가 짝이다 — 글머리표를 무시하는 것이 「아무거나 통과시킨다」가 되면 안 된다.
 *    넷째는 **안 지우기로 한 경계**다: `·`(U+00B7)는 2,295회 중 줄머리가 9회뿐인
 *    문장 구분자라, 지우면 서로 다른 문장이 같은 값으로 접혀 엉뚱한 원문이 걸린다. */
const TR_BULLET = `**2026-08-19 17:53 · 홍길동**
• 발주처와 감리단, 입주예정자 2명과 면담 진행함
<면담과 별도 내용>
• 커뮤니티 위탁관리계약에 따라 갑PFV는 을에이치엠에 관리비 계좌 조회에 대한 권한을 줄 예정임`;
const gateB = (q) => verifyEvidence([{ type: '수치', evidence: `"${q}"` }], TR_BULLET).kept.length === 1;

check('글머리표를 넘어 이어 적은 인용도 관문을 통과한다',
  gateB('<면담과 별도 내용> 커뮤니티 위탁관리계약에 따라 갑PFV는 을에이치엠에 관리비 계좌 조회에 대한 권한을 줄 예정임'));
check('글머리표를 그대로 적어 온 인용도 통과한다',
  gateB('• 발주처와 감리단, 입주예정자 2명과 면담 진행함'));
check('글머리표를 무시해도 원문에 없는 인용은 버린다',
  !gateB('• 을에이치엠이 위탁관리계약을 해지하겠다고 통보해 왔습니다'));
check('가운뎃점(·)은 지우지 않는다 — 글머리표가 아니라 문장 구분자다',
  jsFold('가·나') === '가·나', jsFold('가·나'));

/* ⑦-f **모델은 여러 줄에 걸친 원문을 ` / ` 로 이어 한 인용으로 적는다.**
 *    슬랙에서 항목을 나열하면 줄마다 `· ` 로 시작하는데, 모델이 두 줄을 이어 인용하면
 *    줄바꿈과 글머리표 자리에 ` / ` 가 들어간다. 그 글자는 **원문에 없고**, 그 자리의
 *    원문은 `·`(U+00B7)인데 그것은 문장 구분자라 **일부러 안 지운다**(위 넷째 시험).
 *    그래서 접기만으로는 영영 안 걸리고, 항목이 `dropped` — 화면에는 「근거 못 찾아 뺀
 *    후보」로 뜬다. `archive-inbox` SKILL 은 그 문구를 「대개 빼」로 읽으라고 안내하므로
 *    **멀쩡한 근거가 버려진다.** 에러는 안 난다.
 *
 *    2026-09-10 `#사업장가` 회차에서 실제로 그랬고 사람이 Grep 해서야 원문이 있는 것을
 *    알았다. **`check-shared-rules.js` 로는 안 잡힌다** — 파이썬·JS 가 함께 틀려서
 *    「갈렸나」 검사는 통과한다. SKILL 이 적어 둔 다섯째 원인(백슬래시 따옴표)과 같은 모양이다.
 *
 *    **원문은 가짜다** (위 TR 과 같은 이유). 재는 것은 「줄바꿈과 `·` 를 ` / ` 로 바꿔
 *    이어 적은 인용이 걸리나」이므로, 두 줄이 `· ` 로 시작하고 각 조각이 접기 전 8자를
 *    넘기만 하면 성질이 보존된다.
 *
 *    셋째가 짝이다 — 쪼개는 것이 「아무거나 통과시킨다」가 되면 안 된다.
 *    넷째는 **안 쪼개기로 한 경계**다: 공백 없이 붙여 쓴 `/` 는 날짜(`10/23`)와
 *    `채권/채무` 처럼 **원문의 글자**라, 쪼개면 조각이 8자 밑으로 부서져 되레 못 찾는다. */
const TR_SLASH = `**2026-09-09 17:04 · 홍길동**
1. 미납입 호실 중 조기납부 유도 진행 호실 회수(3호실) 완료
   · 대출 잔액 약 40억 감소 (900억 → 860억)
   · 실수금 약 7억이며 채권/채무 상계는 별건으로 검토한다`;
const gateS = (q) => verifyEvidence([{ type: '수치', evidence: `"${q}"` }], TR_SLASH).kept.length === 1;

check('두 줄을 " / " 로 이어 적은 인용도 관문을 통과한다',
  gateS('대출 잔액 약 40억 감소 (900억 → 860억) / 실수금 약 7억이며'));
check('한 줄짜리 인용은 그대로 통과한다',
  gateS('대출 잔액 약 40억 감소 (900억 → 860억)'));
check('쪼개도 원문에 없는 인용은 버린다',
  !gateS('이행청구를 완료했고 / 상장은 다음 달로 미루었습니다'));
check('공백 없이 붙여 쓴 /(채권/채무)는 원문의 글자라 쪼개지 않는다',
  gateS('실수금 약 7억이며 채권/채무 상계는 별건으로'));

/* ⑦-g **인용 안의 조각은 하나만 걸려도 통과한다** — 그래서 앞 절반은 실존하고 뒤 절반은
 *    지어낸 인용이 관문을 지난다 (할 일 185-곁, 2026-09-11). 조일지(`every`) 정하려면
 *    대가를 알아야 하는데, 로컬에서 모은 코퍼스는 **다조각 인용이 15건뿐**이라 모자랐다.
 *    그래서 판정은 그대로 두고 **「some 이면 통과인데 every 면 탈락」일 인용의 건수만**
 *    로그로 센다. 며칠 쌓인 값을 보고 규칙을 정한다.
 *
 *    **계측이 죽어 있으면 그 값은 0 으로 읽힌다** — 그리고 0 은 「조여도 대가가 없다」로
 *    읽혀서 관문을 잘못 조이게 된다. 에러는 안 난다. 그래서 세 가지를 함께 지킨다:
 *    ① 판정이 안 바뀌는 것 ② 부분 일치에서 실제로 찍히는 것 ③ 전부 걸린 인용에는 안 찍히는 것.
 *
 *    **화면 쪽은 이것과 무관하게 이미 못 찾은 조각을 밝힌다**
 *    (`archive-inbox/scripts/review_work.py` 의 `context_for` — 「원문에 없음」). */
const capture = (fn) => {
  const saw = [];
  const keep = console.log;
  console.log = (...a) => saw.push(a.join(' '));
  try {
    return { ret: fn(), saw };
  } finally {
    console.log = keep;
  }
};
const SHADOW = '[185-곁 계측]';
const HALF_A = '그러면 절차 끌지 말고 이번 주에 마무리하자는 의견을 ... 지어낸 반쪽 조각입니다요';
const HALF_B = '명의의 회신을 확인했고 ... 여기도 지어낸 뒷조각 문장입니다';
const halfMade = capture(() => gate(HALF_A));
check('반쪽만 실존하는 인용은 **지금도 통과한다** (계측은 판정을 안 바꾼다)',
  halfMade.ret === true);
/* **건수까지 단언한다 — 「찍혔나」만 재면 안 된다.** 며칠 뒤 사람이 읽을 값은 태그가 아니라
 * **건수**인데, 태그만 보는 검사는 계측이 **틀린 수**를 찍어도 통과한다. 그리고
 * 틀린 숫자는 에러 없이 관문 결정에 그대로 들어간다 — 특히 0 쪽으로 틀리면
 * 「조여도 대가가 없다」로 읽혀 관문을 잘못 조이게 된다. 죽은 계측뿐 아니라 **틀린 계측**도
 * 잡아야 한다. 아래 둘째가 그 짝이다 — 1 을 박아 둔 계측은 거기서 빨개진다.
 *
 * **2026-09-20: 분모를 함께 단언한다.** 옛 판은 부분 일치가 0 이면 줄을 아예 안 냈고, 그래서
 * 셋째 검사가 「안 찍힌다」를 단언했다 — **그 침묵이 실전에서 「계측이 죽었다」와 구별되지
 * 않았다**(VM 전 기간 0건인데 뜻을 판정할 수 없었다). 이제 회차마다 한 줄이 반드시 나오므로
 * 셋째는 **「분모 1 · 분자 0 으로 찍힌다」**를 단언한다 — 침묵이 아니라 값으로 증명한다. */
check('그 자리에 그림자 계측이 분모와 함께 1건으로 찍힌다',
  halfMade.saw.some((l) => l.includes(`${SHADOW} 항목 1건 · 다조각 인용 1건 · 그중 부분 일치 1건`)),
  JSON.stringify(halfMade.saw));
const twoHalf = capture(() =>
  verifyEvidence([{ type: '수치', evidence: `"${HALF_A}" 및 "${HALF_B}"` }], TR).kept.length === 1);
check('반쪽 인용이 둘이면 분모도 2, 분자도 2로 센다',
  twoHalf.ret === true
    && twoHalf.saw.some((l) => l.includes(`${SHADOW} 항목 1건 · 다조각 인용 2건 · 그중 부분 일치 2건`)),
  JSON.stringify(twoHalf.saw));
const wholly = capture(() =>
  gate('명의의 회신을 확인했고 ... 그러면 절차 끌지 말고 이번 주에 마무리하자는 의견을'));
check('조각이 전부 걸린 인용은 분모에는 들되 분자에는 안 든다',
  wholly.ret === true
    && wholly.saw.some((l) => l.includes(`${SHADOW} 항목 1건 · 다조각 인용 1건 · 그중 부분 일치 0건`)),
  JSON.stringify(wholly.saw));
/* **볼 항목이 0 이면 줄이 없다** — 그 자리를 못 박는다. (침묵의 **다른** 뜻은 「코드가 안
 * 돌았다」인데, 그 둘을 화면에서 가르는 것은 ingest 로그의 회차 수다 — `summary.js` 주석 참조.) */
const noneAtAll = capture(() => verifyEvidence([], TR));
check('볼 항목이 없으면 줄이 안 나온다',
  noneAtAll.saw.every((l) => !l.includes(SHADOW)),
  JSON.stringify(noneAtAll.saw));
/* **분모의 정의를 지킨다 — 「조각 2개 이상」이지 「1개 이상」이 아니다.**
 * 위 검사 넷은 픽스처 인용이 **전부 다조각**이라, 분모를 `total >= 1` 로 잘못 세는 판과
 * 맞게 `total > 1` 로 세는 판이 **똑같은 출력**을 낸다 — 돌연변이 시험에서 그 판이 검사를
 * 전부 통과했다(2026-09-20). 분모가 이 계측의 존재 이유인데 그 정의를 아무도 안 지키고
 * 있었다. 단조각 인용은 애초에 부분 일치가 불가능하므로(`0 < found < 1` 은 없다)
 * **분모에 들면 안 된다** — 들면 「조여도 대가가 작다」쪽으로 값이 부푼다. */
const singleFrag = capture(() =>
  gate('그러면 절차 끌지 말고 이번 주에 마무리하자는 의견을 갑금융 측에 전해왔답니다'));
check('생략 표시 없는 단조각 인용은 분모에 안 든다',
  singleFrag.ret === true
    && singleFrag.saw.some((l) => l.includes(`${SHADOW} 항목 1건 · 다조각 인용 0건 · 그중 부분 일치 0건`)),
  JSON.stringify(singleFrag.saw));

/* ⑧ report.js 와 같은 제외 — 시스템·봇 메시지 note 는 할 일이 아니다 */
const notes = buildItems({
  conversations: {
    channels: [{ channel: 'd', notes: ['시스템·봇 메시지 3건 건너뜀', '사람이 볼 것'] }],
  },
});
check('시스템·봇 메시지 note 는 안 담는다', notes.length === 1 && notes[0].detail === '사람이 볼 것');

/* ⑧-2 채널별 반영 실패(스레드 덧붙이기 실패 등)도 항목으로 담는다.
 *
 * 전에는 `errors` 가 DM 「❌ 실패」 절 **글자로만** 나가서, 상황판(`board.py`)도
 * `archive-inbox` 도 이 갈래를 구조적으로 못 봤다 — 2026-09-18 에 어느 사업장 채널의
 * 스레드 덧붙이기 실패가 이틀간 DM 에만 나오다 사람이 DM 을 직접 보고서야 잡혔다.
 * id 에 실패 문구 해시를 넣으므로 같은 실패는 같은 id 로 이월돼 「며칠째」가 쌓인다.
 * 종결은 비요약 항목의 규칙대로 「빼」다(`decide_work.py` 는 비요약에 「반영」을 거절한다). */
const errItems = buildItems({
  conversations: {
    channels: [{ channel: 'e', error: '스레드 덧붙이기 실패 (**2026-08-07 07:37 · 최호중**): 같은 시각에 2건' }],
  },
});
check('채널 반영 실패를 error 항목으로 담는다',
  errItems.length === 1 && errItems[0].kind === 'error' && errItems[0].channel === 'e'
  && String(errItems[0].detail).includes('스레드 덧붙이기 실패'));
const errAgain = buildItems({
  conversations: {
    channels: [{ channel: 'e', error: '스레드 덧붙이기 실패 (**2026-08-07 07:37 · 최호중**): 같은 시각에 2건' }],
  },
});
check('같은 실패는 회차가 바뀌어도 같은 id 다 — 「며칠째」가 쌓인다',
  errItems.length === 1 && errAgain.length === 1 && errItems[0].id === errAgain[0].id);

/* ⑨ 대조를 안 한 회차는 시각 도장을 새로 찍지 않는다.
 *
 * `archive-inbox` 의 `review_work.py` 는 「반영」한 항목을 목록에서 감출 때
 * **반영 시각 > 목록의 `generated`** 로 판정한다. 그 판정이 서 있는 전제는
 * 「`generated` 가 새로워졌다 = 그 항목을 새 증거로 다시 봤다」인데,
 * 17:00 회차는 요약 대조를 끄고(`skipSummary`) 돌면서 항목은 이월하고
 * 도장만 새로 찍었다. 그러면 아침에 반영을 끝낸 항목이 저녁에 되살아난다.
 *
 * 2026-08-12 실제로 그랬다 — 12:51 목록의 4건을 12:58 에 반영했는데,
 * 17:00 회차가 `generated` 를 12:51 → 17:00 으로만 바꾸자(그 파일 diff 는 한 줄)
 * 반영 시각이 도장보다 앞서게 되어 4건이 그대로 다시 떴다. 에러는 안 난다.
 *
 * `.pending-edits.json` 은 같은 함정을 이미 밟고 고쳤다 — 그쪽은 파일 쓰기 전체가
 * `if (editsEnabled)` 안이라 대조를 안 한 회차는 파일도 도장도 안 건드린다
 * (`slack-archive.js` 의 `ingestConversations` 안 `PENDING_FILE` 쓰기). */
check('대조를 안 한 회차는 앞 회차의 도장을 그대로 쓴다',
  nextGenerated({ prev: '2026-08-12T03:51:00.990Z', summaryChecked: false, now: '2026-08-12T08:00:19.141Z' })
    === '2026-08-12T03:51:00.990Z');
check('대조를 한 회차는 도장을 새로 찍는다',
  nextGenerated({ prev: '2026-08-12T03:51:00.990Z', summaryChecked: true, now: '2026-08-12T08:00:19.141Z' })
    === '2026-08-12T08:00:19.141Z');
/* 앞 도장이 없으면(목록이 방금 처음 생겼으면) 새로 찍는다 — 감추는 쪽으로 틀리지 않는다.
 * `review_work.py` 의 applied_after 도 못 읽으면 감추지 않는다. 같은 방향이다. */
check('앞 도장이 없으면 새로 찍는다',
  nextGenerated({ prev: undefined, summaryChecked: false, now: '2026-08-12T08:00:19.141Z' })
    === '2026-08-12T08:00:19.141Z');

/* ⑩ 「마지막으로 실제 검출된 시각」을 항목마다 남긴다.
 *
 * ⑨ 가 막은 것은 **대조를 안 한 회차**가 도장을 찍는 것이었다. 같은 모양의 구멍이
 * **대조를 한 회차**에도 하나 더 있다 — 요약이 아닌 항목(새 채널·개명·note·파생값)은
 * 위 ④ 대로 **무조건 이월**되는데(다시 안 잡히므로 치우면 그대로 사라진다), 07:00 회차는
 * 대조를 했으므로 `generated` 를 **정당하게** 앞당긴다. 그러면 반영 시각이 도장보다
 * 앞서게 되어 **끝낸 항목이 다시 뜬다.** ⑨ 의 고침으로는 안 덮인다 — 원인이 다르다.
 *
 * 뒤집힌 유인이 이 구멍의 실체다: 「빼」는 `dismissed` 로, 「나중에」는 `deferred` 로
 * 저장소에 남아 걸러지는데 **옳은 답인 「반영」만 안 붙는다.**
 *
 * 그래서 견줄 값을 목록 단위(`generated`)가 아니라 **항목 단위**로 둔다. 이번 회차에
 * 실제로 검출된 것(`fresh`)에만 새 시각을 찍으면, 일회성 항목(새 채널·개명)은 반영이
 * 붙어 있고 조건이 남아 계속 잡히는 항목(note·파생값)은 정당하게 다시 뜬다.
 */
const RUN = '2026-08-13T22:00:00.000Z';
const OLD = '2026-08-09T22:00:00.000Z';

const notSeen = mergeItems({
  prev: [{ id: 'b|new-channel', kind: 'new-channel', channel: 'b', firstSeen: '2026-08-09', lastSeen: OLD }],
  fresh: [],
  today: '2026-08-13', now: RUN,
});
check('다시 안 잡힌 항목의 lastSeen 은 그대로다',
  notSeen[0]?.lastSeen === OLD, `lastSeen=${notSeen[0]?.lastSeen}`);

const seenAgain = mergeItems({
  prev: [{ id: 'b|note|x', kind: 'note', channel: 'b', firstSeen: '2026-08-09', lastSeen: OLD }],
  fresh: [{ id: 'b|note|x', kind: 'note', channel: 'b', detail: 'x' }],
  today: '2026-08-13', now: RUN,
});
check('다시 잡힌 항목의 lastSeen 은 이번 회차로 갱신된다',
  seenAgain[0]?.lastSeen === RUN, `lastSeen=${seenAgain[0]?.lastSeen}`);

const brandNew = mergeItems({
  prev: [],
  fresh: [{ id: 'c|new-channel', kind: 'new-channel', channel: 'c' }],
  today: '2026-08-13', now: RUN,
});
check('처음 잡힌 항목에도 lastSeen 이 붙는다',
  brandNew[0]?.lastSeen === RUN, `lastSeen=${brandNew[0]?.lastSeen}`);

/* ── suppress — 「빼·나중에」를 거르는 유일한 판정 ────────────────────────── */

console.log('\nsuppress');

const NOW_OK = Date.parse('2026-09-04T09:00:00+09:00');
const RENAMED = { id: 'x|renamed|y', kind: 'renamed', channel: 'x', firstSeen: '2026-08-20' };
const PLAIN = { id: 'z|new-channel', kind: 'new-channel', channel: 'z', firstSeen: '2026-08-20' };

check('만기 전 「나중에」는 빠진다',
  suppress([RENAMED, PLAIN], { deferred: { 'x|renamed|y': { until: '2026-09-30' } } }, NOW_OK).length === 1);
check('만기 지난 「나중에」는 남는다',
  suppress([RENAMED, PLAIN], { deferred: { 'x|renamed|y': { until: '2026-09-01' } } }, NOW_OK).length === 2);
// 요약이 아닌 항목의 「빼」에는 해시가 없다 — 돌아올 근거가 없으니 계속 빠진다
check('해시 없는 「빼」는 계속 빠진다',
  suppress([RENAMED, PLAIN], { dismissed: { 'x|renamed|y': { reason: '처리함' } } }, NOW_OK).length === 1);

/* 만기 판정이 **넘긴 시각**을 쓰는지. 위 두 줄만으로는 못 가린다 — 오늘 날짜로 재도
 * 답이 같아서, 인자를 무시해도 초록이 된다. 여기서는 넘긴 시각(2026-01-15)에서는
 * 만기 전이고 실제 오늘로 재면 만기가 지난 값을 써서 둘을 가른다. */
const NOW_PAST = Date.parse('2026-01-15T09:00:00+09:00');
check('만기 판정은 넘긴 시각을 쓴다',
  suppress([RENAMED, PLAIN], { deferred: { 'x|renamed|y': { until: '2026-02-01' } } }, NOW_PAST).length === 1);

/* 「반영」도 여기서 걸러진다 — 그러려면 기록이 저장소에 있어야 한다.
 *
 * 전에는 「반영」만 `.decision-stamp.json`(로컬 전용, .gitignore)에 적혀서 VM 이 못 봤다.
 * 그래도 굴러간 이유는 위 ② 의 자동 치움이 그 자리를 메웠기 때문인데, 그 전제가 틀린
 * 것이었으므로(② 참조) 자동 치움을 걷어내면 **반영한 항목이 영영 안 사라진다.**
 * 그래서 「반영」을 `.sync-state.json` 의 `applied` 로 옮긴다 — 「빼·나중에」가 저장소에
 * 들어가는 것과 같은 이유다: VM 이 다음 07:00 에 그것을 걸러야 한다.
 *
 * 견주는 값은 목록 전체의 `generated` 가 아니라 **그 항목을 마지막으로 다시 본 시각**
 * (`lastSeen`)이다 — `review_work.py` 의 `applied_after`·`confirmed_at` 과 같은 판정이어야
 * 한다. 그래야 반영 뒤에 **새 증거로 다시 잡힌 것**은 되살아난다. */
const APPLIED_ITEM = {
  id: 'q|summary|수치|핵심 쟁점', kind: 'summary', channel: 'q', file: 'q',
  firstSeen: '2026-09-05', lastSeen: '2026-09-05T22:00:00.000Z',
};
const APPLIED_AGAIN = { ...APPLIED_ITEM, lastSeen: '2026-09-08T22:00:00.000Z' };
const APPLY_REC = { applied: { 'q|summary|수치|핵심 쟁점': { at: '2026-09-06T18:36:00+09:00' } } };

check('「반영」한 요약 항목은 빠진다',
  suppress([APPLIED_ITEM, PLAIN], APPLY_REC, NOW_OK).length === 1,
  `남은 것 ${suppress([APPLIED_ITEM, PLAIN], APPLY_REC, NOW_OK).map((i) => i.id).join(', ')}`);
check('  반영 뒤에 다시 잡힌 것은 되살아난다',
  suppress([APPLIED_AGAIN, PLAIN], APPLY_REC, NOW_OK).length === 2);
check('  시각을 못 읽으면 감추지 않는다',
  suppress([APPLIED_ITEM], { applied: { 'q|summary|수치|핵심 쟁점': { at: '어제' } } }, NOW_OK).length === 1);

/* ── summaryHash 가 줄끝에 흔들리지 않는가 ────────────────────────────────
 *
 * 「빼」는 **그때의 요약 해시와 같을 때만** 걸러진다. 그 해시가 줄끝까지 세면 같은
 * 내용인데 기계마다 값이 갈린다 — 이 PC 는 `core.autocrlf=true` 라 작업 트리가 CRLF 이고
 * VM 은 리눅스라 LF 다. 그러면 **이 PC 에서 「빼」로 정한 것이 VM 에서는 영영 안
 * 걸러진다**: VM 이 「요약이 고쳐졌으니 판정 전제가 사라졌다」로 읽고 매일 아침 다시 센다.
 *
 * 2026-09-07 실물 — DM 은 4건인데 이 PC 화면은 3건이었다. 남은 한 건이 어제 「빼」로
 * 정한 파생값 건이고, 그 채널 요약 자리를 이 PC 에서 재면 `e621f03c34005046`,
 * LF 로 재면 `6db5a2d6af075262` 였다.
 *
 * 커밋 관문은 같은 함정을 이미 밟고 고쳤다 — `review_work.py` 의 `_digest` 가
 * 「git 은 인덱스에 LF 로 넣고 작업 트리에는 CRLF 로 꺼내므로」라며 줄끝을 맞춘 뒤 센다.
 * 이 해시만 안 고쳐져 있었다.
 *
 * 파일을 읽는 함수라 **임시 자료 뿌리에서 별도 프로세스로** 돌린다.
 * 둘째가 짝이다 — 줄끝을 무시하는 것이 「아무 내용이나 같게 본다」가 되면 안 된다. */

console.log('\nsummaryHash');

const MD_HEAD = [
  '> **성격**: 시험용 채널',
  '> **기간**: 2026-01-01 ~ 2026-01-31 · 실제 메시지 1건',
  '',
  '---',
  '',
  '## 핵심 쟁점',
  '- 만기 연장 협의 중',
  '',
  '## 2026-01',
  '',
  '**2026-01-05 17:00 · 홍길동**',
  '본문',
  '',
];

const hashTmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-hash-'));
try {
  const chDir = path.join(hashTmp, 'slack-export', 'channels');
  fs.mkdirSync(chDir, { recursive: true });
  fs.copyFileSync(path.join(DATA_ROOT, 'config.json'), path.join(hashTmp, 'config.json'));

  fs.writeFileSync(path.join(chDir, 'lf.md'), MD_HEAD.join('\n'), 'utf8');
  fs.writeFileSync(path.join(chDir, 'crlf.md'), MD_HEAD.join('\r\n'), 'utf8');
  // 짝 — 줄끝이 아니라 내용이 다른 것
  fs.writeFileSync(path.join(chDir, 'other.md'),
    MD_HEAD.map((l) => (l === '- 만기 연장 협의 중' ? '- 만기 연장 완료' : l)).join('\n'), 'utf8');

  const out = execFileSync(process.execPath, ['-e', `
    import('${pathToFileURL(path.join(REPO_ROOT, 'src', 'ingest', 'pending-work.js')).href}')
      .then((m) => console.log(JSON.stringify(
        ['lf', 'crlf', 'other'].map((n) => m.summaryHash(n)))));
  `], { env: { ...process.env, HERMES_DATA_ROOT: hashTmp }, encoding: 'utf8' });
  const [lf, crlf, other] = JSON.parse(out.trim().split('\n').pop());

  check('줄끝만 다른 요약은 같은 해시를 낸다', lf === crlf, `LF=${lf} CRLF=${crlf}`);
  check('  내용이 다르면 해시도 다르다', lf !== other, `LF=${lf} 다른내용=${other}`);
} finally {
  fs.rmSync(hashTmp, { recursive: true, force: true });
}

/* ── stalePendingWork 가 그 판정을 그대로 쓰는가 ──────────────────────────
 *
 * 09:00·16:00 위생 점검은 `.pending-work.json` 을 직접 읽는데, 전에는 `deferred` 만
 * 걸렀다. 그 파일에서 「빼·나중에」를 실제로 빼는 것은 **다음 07:00 회차의 suppress**
 * 이므로, 정한 시점부터 다음 아침까지는 「빼」로 정한 것이 그대로 세어졌다 —
 * 사람은 방금 처리했는데 두 시간 뒤에 같은 건이 「N일째」로 다시 온다.
 * 2026-09-04 에 실물 2건이 그 상태였다. 「나중에」는 걸러지고 「빼」만 안 걸러지는
 * 비대칭이 그 증거였다.
 *
 * 파일을 읽는 함수라 **임시 자료 뿌리에서 별도 프로세스로** 돌린다. */

console.log('\nstalePendingWork');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-work-'));
try {
  fs.mkdirSync(path.join(tmp, 'slack-export', 'channels'), { recursive: true });
  // config.json 이 없으면 config.js 가 안내를 내고 멈춘다 — 이 기계의 것을 그대로 빌린다.
  // 그래서 이 구간은 이 기계의 config 에 기댄다: 위의 `slack-export` 는 그 config 의
  // archivePath 와 같아야 한다. 다르면 픽스처를 못 찾아 아래 검사가 FAIL 로 드러난다
  // (조용히 초록이 되지는 않는다).
  fs.copyFileSync(path.join(DATA_ROOT, 'config.json'), path.join(tmp, 'config.json'));
  fs.writeFileSync(path.join(tmp, 'slack-export', '.pending-work.json'), JSON.stringify({
    generated: '2026-08-11T00:00:00.000Z',
    items: [RENAMED, PLAIN],
  }), 'utf8');
  fs.writeFileSync(path.join(tmp, 'slack-export', '.sync-state.json'), JSON.stringify({
    dismissed: { 'x|renamed|y': { reason: '처리함', at: '2026-09-04T00:00:00+09:00' } },
  }), 'utf8');

  const out = execFileSync(process.execPath, ['-e', `
    import('${pathToFileURL(path.join(REPO_ROOT, 'src', 'archive-health.js')).href}')
      .then((h) => console.log(JSON.stringify(
        h.stalePendingWork(${NOW_OK}, 3).map((it) => it.id))));
  `], { env: { ...process.env, HERMES_DATA_ROOT: tmp }, encoding: 'utf8' });
  const ids = JSON.parse(out.trim().split('\n').pop());
  check('「빼」로 정한 것은 다음 07:00 전에도 안 센다',
    !ids.includes('x|renamed|y'), `남은 것 ${JSON.stringify(ids)}`);
  check('  정하지 않은 것은 그대로 센다', ids.includes('z|new-channel'));
} finally {
  fs.rmSync(tmp, { recursive: true, force: true });
}

console.log(ok ? '\n전부 통과' : '\n실패 있음');
process.exit(ok ? 0 : 1);
