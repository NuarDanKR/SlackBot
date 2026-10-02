#!/usr/bin/env node
/**
 * 위생 점검 DM 이 «무엇을 기준으로 쟀는지»를 규칙대로 싣나.
 *
 *   node scripts/check-health-freshness.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * 규칙 둘이 서로 반대라서 한쪽만 맞기 쉽다.
 *   · 정상일 때 낡음 줄은 **혼자서 DM 을 만들면 안 된다** (매일 «이상 없음» 이 오면
 *     곧 안 읽게 되고 그때 진짜 경고도 같이 묻힌다)
 *   · 낡았을 때는 **0건이어도 DM 을 만들어야 한다** (조용함의 뜻이 둘이 되면 안 된다)
 *
 * 그리고 `docWarnCount` 기본값이 1 이라 **0건에서는 첨부 줄이 원래 안 나온다.**
 * 경고만 있고 숫자가 없으면 받는 사람이 할 수 있는 일이 없으므로 그것도 본다.
 *
 * **「실패」와 「안 함」도 갈라 본다.** `reason` 은 실제로 실패한 git 명령이 있을 때만
 * 있다 — 손 실행이거나 `syncBeforeCheck` 를 꺼서 **일부러** 안 맞춰본 회차는
 * `reason: null` 로 온다. 이걸 구별 못 하면 아무 것도 안 한 회차마다 "고장났다"고
 * 알리게 된다.
 *
 * **슬랙에 안 붙는다.** compose() 에 값을 직접 먹인다.
 */
import { compose } from '../src/archive-health.js';

let ok = true;
const fail = (msg) => { ok = false; console.error(`✗ ${msg}`); };
const pass = (msg) => console.log(`✓ ${msg}`);

// runHealth 이 settings() 로 만들어 넘기는 것과 같은 모양
const S = { scanDays: 2, syncWarnDays: 7, syncCriticalDays: 12, docWarnCount: 1, liveFetchMaxDays: 14 };
const LAG = { missing: false, days: 0, lastSync: '2026-08-20' };
const docs = (total, byChannel = []) => ({
  total, scanDays: 2, byChannel, failed: [], approvals: [], deferred: [], restricted: [],
});
const FRESH_OK = { ok: true, head: '9a1b2c3', headAt: '15:58:02', checkedAt: '16:00:41', behind: 0, reason: null };
const FRESH_BAD = { ok: false, head: 'a4edcab', headAt: '15:32:08', checkedAt: '16:00:41', behind: 2, reason: 'git 잠금' };
const FRESH_UNK = { ...FRESH_BAD, behind: null, reason: '원격 확인 실패' };
// 손 실행 / syncBeforeCheck 꺼짐 — pull(fetch) 은 됐고(reached) 뒤처짐도 잰 값이 있는데,
// **실패한 git 명령이 없어서** reason 은 null 이다 (git.js `syncForRead` 의 새 계약).
const FRESH_NOPULL = { ok: false, head: '18938ec', headAt: '08:59:50', checkedAt: '09:00:03', behind: 3, reason: null };
const opts = (extra) => ({ preDigest: true, stale: [], work: [], arch: null, ...extra });

// 1. 정상 + 0건 → DM 자체가 없어야 한다 (낡음 줄이 혼자 DM 을 만들면 안 된다)
{
  const body = compose(LAG, docs(0), S, opts({ freshness: FRESH_OK }));
  if (body !== null) fail(`1 정상+0건: DM 이 만들어졌다 — 낡음 줄은 혼자 DM 을 만들면 안 된다\n${body}`);
  else pass('1 정상+0건 — DM 없음');
}

// 2. 정상 + 1건 → 기존 DM 에 기준 줄이 붙는다
{
  const body = compose(LAG, docs(1, [{ channel: '사업장마', count: 1, newest: 'x.hwpx' }]), S,
    opts({ freshness: FRESH_OK }));
  if (!body) fail('2 정상+1건: DM 이 없다');
  else if (!body.includes('기준: HEAD 9a1b2c3')) fail(`2 정상+1건: 기준 줄이 없다\n${body}`);
  else if (!body.includes('16:00:41')) fail(`2 정상+1건: checkedAt 이 없다\n${body}`);
  else if (!body.includes('1건')) fail(`2 정상+1건: 첨부 줄이 없다\n${body}`);
  else pass('2 정상+1건 — 첨부 줄 + 기준 줄');
}

// 3. 낡음 + 0건 → DM 이 나가고, 경고와 «0건» 이 함께 있어야 한다
{
  const body = compose(LAG, docs(0), S, opts({ freshness: FRESH_BAD }));
  if (!body) fail('3 낡음+0건: DM 이 없다 — 제대로 못 쟀다는 것은 조용하면 안 된다');
  else if (!body.includes('최신화 실패')) fail(`3 낡음+0건: 경고가 없다\n${body}`);
  else if (!body.includes('a4edcab') || !body.includes('15:32:08')) fail(`3 낡음+0건: 기준(HEAD·headAt)이 없다\n${body}`);
  else if (!body.includes('2개')) fail(`3 낡음+0건: behind 가 안 적혔다\n${body}`);
  else if (!body.includes('0건')) fail(`3 낡음+0건: 잰 값(0건)이 없다 — 경고만 있으면 손쓸 수가 없다\n${body}`);
  else pass('3 낡음+0건 — 경고 + 잰 값 0건이 함께');
}

// 4. 낡음 + behind 모름 → «확인 못 함» 이지 «0개» 가 아니다
{
  const body = compose(LAG, docs(0), S, opts({ freshness: FRESH_UNK }));
  if (!body) fail('4 낡음+모름: DM 이 없다');
  else if (!body.includes('확인 못 함')) fail(`4 낡음+모름: «확인 못 함» 이 없다\n${body}`);
  else if (/커밋이 0개/.test(body)) fail(`4 낡음+모름: 모르는 것을 «0개» 로 적었다\n${body}`);
  else pass('4 낡음+모름 — «확인 못 함»');
}

// 5. freshness 를 안 주면 지금과 글자 하나까지 같아야 한다
{
  const d = docs(1, [{ channel: '사업장나', count: 1, newest: 'y.pdf' }]);
  const before = compose(LAG, d, S, opts({ freshness: null }));
  if (!before) fail('5 미지정: DM 이 없다');
  else if (/기준: HEAD|최신화 실패/.test(before)) fail(`5 미지정: 낡음 줄이 끼어들었다\n${before}`);
  else pass('5 미지정 — 기존 동작 그대로');
}

// 6. 손 실행 / syncBeforeCheck 꺼짐(reason:null) → «안 함» 이지 «실패» 가 아니다
{
  const body = compose(LAG, docs(1, [{ channel: '사업장마', count: 1, newest: 'x.hwpx' }]), S,
    opts({ freshness: FRESH_NOPULL }));
  if (!body) fail('6 안 함: DM 이 없다');
  else if (!body.includes('최신화 안 함')) fail(`6 안 함: «최신화 안 함» 이 없다\n${body}`);
  else if (body.includes('최신화 실패')) fail(`6 안 함: 안 한 것을 «실패» 라고 적었다 — 아무 것도 안 한 것과 고장은 다르다\n${body}`);
  else if (!body.includes('3개')) fail(`6 안 함: behind 값이 안 적혔다\n${body}`);
  else pass('6 안 함 — «최신화 안 함» · «실패» 아님 · behind 값 있음');
}

// 7. 실제로 실패한 git 명령이 있으면(reason 있음) → «실패» 다 («안 함» 이 아니다)
{
  const body = compose(LAG, docs(1, [{ channel: '사업장마', count: 1, newest: 'x.hwpx' }]), S,
    opts({ freshness: FRESH_BAD }));
  if (!body) fail('7 실패: DM 이 없다');
  else if (!body.includes('최신화 실패')) fail(`7 실패: «최신화 실패» 가 없다\n${body}`);
  else if (body.includes('최신화 안 함')) fail(`7 실패: 실패한 것을 «안 함» 이라고 적었다\n${body}`);
  else if (!body.includes('git 잠금')) fail(`7 실패: reason(사유)이 안 적혔다\n${body}`);
  else pass('7 실패 — «최신화 실패(사유)» · «안 함» 아님');
}

// 8. 낡음 + 0건(요약 전) → 빈 줄 하나만 있고, 0건인데 «지금 돌리세요» 로 재촉하지 않는다
{
  const body = compose(LAG, docs(0), S, opts({ freshness: FRESH_BAD }));
  if (!body) fail('8 낡음+0건: DM 이 없다');
  else if (/\n\n\n/.test(body)) fail(`8 낡음+0건: 빈 줄이 둘 이상이다\n${body}`);
  else if (body.includes('지금 `doc-archive` 를 돌려')) {
    fail(`8 낡음+0건: 0건인데 «지금 돌리세요» 가 남아 있다 — 위 줄과 모순이다\n${body}`);
  } else if (!body.includes('더** 있을 수 있습니다')) {
    fail(`8 낡음+0건: 0건일 때의 안내 문구(더 있을 수 있다)가 없다\n${body}`);
  } else pass('8 낡음+0건 — 빈 줄 하나 · 재촉 문구 없음 · «더 있을 수 있다» 안내');
}

// 9. 09:00 회차 — 낡음 경고와 동기화 밀림 경고 사이에 빈 줄이 있어야 한다
//
// ⚠️ 서로 **무관한 두 경고**다. 붙어 나오면 한 문단으로 읽혀 «낡아서 동기화가 밀렸다» 처럼
//    인과가 있는 것처럼 보인다. 앞 검사들은 전부 preDigest(16:00) 회차라 밀림 줄이 아예
//    안 실려서 이 자리를 한 번도 안 지나갔다 — 09:00 회차에서만 드러난다.
{
  const S9 = { ...S, scanDays: 60 };
  const lag = { missing: false, days: 8, lastSync: '2026-08-12' };
  const body = compose(lag, { ...docs(0), scanDays: 60 }, S9,
    { preDigest: false, stale: [], work: [], arch: null, freshness: FRESH_BAD });
  if (!body) fail('9 09:00 낡음+밀림: DM 이 없다');
  else {
    const rows = body.split('\n');
    const i = rows.findIndex((l) => l.startsWith('대화 동기화가'));
    if (i < 0) fail(`9 09:00 낡음+밀림: 밀림 줄이 없다\n${body}`);
    else if (rows[i - 1].trim() !== '') {
      fail(`9 09:00 낡음+밀림: 밀림 줄 바로 앞이 빈 줄이 아니다 — 두 경고가 한 문단으로 붙는다\n  앞줄: ${rows[i - 1]}\n  밀림: ${rows[i]}`);
    } else pass('9 09:00 낡음+밀림 — 두 경고가 빈 줄로 갈린다');
  }
}

console.log(ok ? '\n통과' : '\n어긋남');
process.exit(ok ? 0 : 1);
