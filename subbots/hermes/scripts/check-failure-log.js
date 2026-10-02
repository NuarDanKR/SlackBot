#!/usr/bin/env node
/**
 * 실패한 회차가 로그에 **사유와 「어디까지 갔나」**로 남나.
 *
 *   node scripts/check-failure-log.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * 정기 발송은 오래도록 **성공한 회차만** 남겼다. 요약이 안 나간 날은 로그에 한 줄도
 * 없었고 사유는 DM 으로만 나갔다 사라졌다. 이제 남기는데, 이 자리가 깨지면
 * **에러가 안 나고 조용히 안 남는다** — 실패는 드물어서 몇 달 뒤에야 안다.
 *
 * **파일도 네트워크도 안 쓴다.** renderEntry 에 합성 항목을 직접 먹인다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { APIConnectionError } from '@anthropic-ai/sdk';
import { renderEntry, failureStats, BROADCAST } from '../src/convo-log.js';
import { JOBS, LOG_KIND } from '../src/scheduler.js';
import { errLabel } from '../src/claude.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

// 2026-08-28 17:30 KST. 표준시가 Asia/Seoul 이라 UTC 08:30 이 그 시각이다.
const AT = '2026-08-28T08:30:00.000Z';

/* ① 요약이 던져서 죽은 회차 — 사유·발자국·요청 ID 가 다 나온다 */
const digestFail = renderEntry({
  at: AT,
  kind: 'daily',
  ok: false,
  target: '#요약채널',
  origin: '2026-08-28',
  error: 'overloaded_error — Overloaded',
  errorType: 'overloaded_error',
  requestId: 'req_011CeTuQ6kUWBbQ9bEdMbcmi',
  context: '채널 12개 · 메시지 87건 · 읽기 실패 0개 → 요약 생성에서 중단',
  elapsedMs: 252_000,
});
for (const needle of ['⚠️ 실패', '**오류**', 'overloaded_error', '**어디까지**', 'req_011CeTuQ6kUWBbQ9bEdMbcmi']) {
  if (!digestFail.includes(needle)) fail(`요약 실패 회차에 '${needle}' 이 없습니다.\n${digestFail}`);
}

/* ② 자동 반영이 죽은 회차 — 새 종류가 이름을 갖고, **정기 발송으로 세어진다**
 *
 * `(알 수 없음)` 이 보이면 BROADCAST 집합에 'ingest' 가 없다는 뜻이다. 그러면 에러가 안 나고
 * 숫자만 틀린다 — 자동 반영 실패가 문답으로 세어지고 「누가 물었나」 표에 행이 하나 생긴다. */
const ingestFail = renderEntry({
  at: AT,
  kind: 'ingest',
  ok: false,
  target: 'WHK DM',
  origin: '아침 회차',
  error: '관문 실패 — 되돌렸습니다',
  errorType: 'ingest_fatal',
  context: '대화 반영 ✓ · 로그 렌더 ✓ · 요약 대조 ✓ · 파생값 ✓ · 할 일 ✓ · 관문 ✗ · 커밋·push ✗',
});
if (!ingestFail.includes('자동 반영')) {
  fail(`kind 'ingest' 에 이름이 없습니다 — KIND_LABEL 에 넣으세요.\n${ingestFail}`);
}
if (ingestFail.includes('(알 수 없음)')) {
  fail(`kind 'ingest' 가 BROADCAST 집합에 없습니다 — 문답으로 세어집니다.\n${ingestFail}`);
}
if (!ingestFail.includes('관문 ✗')) fail(`자동 반영 실패에 발자국이 없습니다.\n${ingestFail}`);

/* ③ 발자국이 없는 실패 — **줄을 안 쓴다.**
 * 빈 `**어디까지**` 줄은 「아무 데도 못 갔다」와 「못 쟀다」를 같은 글자로 만든다. */
const noContext = renderEntry({
  at: AT, kind: 'health', ok: false, target: 'WHK DM',
  error: 'slack_webapi_platform_error', errorType: 'slack_webapi_platform_error',
});
if (noContext.includes('**어디까지**')) {
  fail(`발자국이 없는데 줄이 나왔습니다 — 값이 없으면 안 써야 합니다.\n${noContext}`);
}
if (!noContext.includes('**오류**')) fail(`발자국이 없어도 사유는 남아야 합니다.\n${noContext}`);

/* ④ 재시도 끝에 성공한 회차 — 한 줄 붙는다 */
const retried = renderEntry({
  at: AT, kind: 'daily', ok: true, target: '#요약채널', origin: '2026-08-28',
  answer: '일일 요약 본문',
  attempts: [
    { model: 'claude-opus-5', waitMs: 0, errorType: 'overloaded_error' },
    { model: 'claude-opus-5', waitMs: 60_000, errorType: 'overloaded_error' },
    { model: 'claude-opus-5', waitMs: 180_000, errorType: null },
  ],
});
if (!retried.includes('**재시도**')) fail(`재시도한 회차에 줄이 없습니다.\n${retried}`);
if (!retried.includes('overloaded_error')) fail(`재시도 줄에 사유가 없습니다.\n${retried}`);
if (!retried.includes('4.0분')) fail(`재시도 줄에 지연이 없습니다 (60초+180초=4.0분).\n${retried}`);

/* ⑤ 한 번에 성공한 회차 — 줄을 안 쓴다.
 * 대부분이 그래야 정상이고, 매 건 `0회` 를 적으면 정작 재시도가 난 건이 안 보인다. */
const once = renderEntry({
  at: AT, kind: 'daily', ok: true, target: '#요약채널', answer: '본문',
  attempts: [{ model: 'claude-opus-5', waitMs: 0, errorType: null }],
});
if (once.includes('**재시도**')) fail(`한 번에 성공했는데 재시도 줄이 붙었습니다.\n${once}`);

/* ⑥ 새 필드가 하나도 없는 **옛 기록** — 새 줄이 하나도 안 붙는다.
 * 이게 깨지면 다음 07:00 회차가 지난달 md 를 통째로 다시 쓴다 (렌더는 멱등이라야 한다). */
const old = renderEntry({
  at: AT, kind: 'qa', ok: true, asker: 'WHK', origin: '#사업장라 (공개 채널)',
  question: '질문', answer: '답변', channels: ['사업장라'],
});
for (const needle of ['**어디까지**', '**재시도**', '요청 ID']) {
  if (old.includes(needle)) fail(`옛 기록에 '${needle}' 이 붙었습니다 — 값이 없으면 안 써야 합니다.\n${old}`);
}

/* ⑦ 사유 집계 — 정기 발송만 세고, 실패와 재시도를 사유별로 가른다 */
const stats = failureStats([
  // 실패 2건 (사유 둘)
  { kind: 'daily', ok: false, errorType: 'overloaded_error' },
  { kind: 'ingest', ok: false, errorType: 'ingest_fatal' },
  // 재시도 끝에 성공 1건 — 실패한 시도 2회가 재시도 쪽으로 간다
  { kind: 'daily', ok: true, attempts: [
    { model: 'm', waitMs: 0, errorType: 'overloaded_error' },
    { model: 'm', waitMs: 60_000, errorType: 'overloaded_error' },
    { model: 'm', waitMs: 180_000, errorType: null },
  ] },
  // 한 번에 성공 1건 — 아무 데도 안 센다
  { kind: 'weekly', ok: true, attempts: [{ model: 'm', waitMs: 0, errorType: null }] },
  // 문답은 정기 발송이 아니라 이 집계 밖이다
  { kind: 'qa', ok: false, errorType: 'overloaded_error' },
]);
if (stats.broadcastTotal !== 4) fail(`정기 발송을 4회로 세야 합니다 — ${stats.broadcastTotal}회로 셌습니다(문답이 섞였나?).`);
if (stats.failedTotal !== 2) fail(`실패를 2회로 세야 합니다 — ${stats.failedTotal}회.`);
if (stats.retriedTotal !== 1) fail(`재시도로 넘긴 것을 1회로 세야 합니다 — ${stats.retriedTotal}회.`);
const over = stats.rows.find((r) => r.type === 'overloaded_error');
if (!over) fail(`overloaded_error 행이 없습니다: ${JSON.stringify(stats.rows)}`);
else if (over.failed !== 1 || over.retried !== 2) {
  fail(`overloaded_error 는 실패 1 · 재시도 2 여야 합니다 — 실패 ${over.failed} · 재시도 ${over.retried}.`);
}

/* ⑧ errorType 이 없는 실패는 `(기타)` 로 센다 — 조용히 사라지면 안 된다 */
const unknown = failureStats([{ kind: 'health', ok: false }]);
if (!unknown.rows.some((r) => r.type === '(기타)' && r.failed === 1)) {
  fail(`errorType 없는 실패가 (기타)로 안 세어집니다: ${JSON.stringify(unknown.rows)}`);
}

/* ⑨ scheduler.js 의 JOBS(잡 여섯) → LOG_KIND(넷으로 접음) → BROADCAST 가 한 사슬로
 * 이어지나. 하나라도 갈리면 **에러 없이 숫자만 틀린다** — 정기 발송 실패가 문답으로
 * 세어지고 「누가 물었나」 표에 `(알 수 없음)` 행이 생긴다(F3, 2026-08-28). 이 저장소는
 * 같은 판정이 두 곳으로 갈려 한쪽이 조용히 옛 기준으로 남은 적이 여러 번 있다
 * (`isBotMessage` 넷, `fold`/`needles` 둘) — 그 모양이 여기 또 생기지 않게 본다.
 *
 * KIND_LABEL 에 이름이 있는지는 export 하지 않고 renderEntry 로 간접 확인한다 — 이름이
 * 없으면 `label = KIND_LABEL[e.kind] || e.kind` 가 raw kind 값으로 새어 헤더 끝에
 * 그대로 남는다(② 가 'ingest' 하나에 쓰는 방식과 같다). */
for (const jobKind of Object.keys(JOBS)) {
  const logKind = LOG_KIND[jobKind];
  if (!logKind) {
    fail(`JOBS 의 '${jobKind}' 가 LOG_KIND 에 없습니다.`);
    continue;
  }
  if (!BROADCAST.has(logKind)) {
    fail(`LOG_KIND['${jobKind}']='${logKind}' 가 BROADCAST 집합에 없습니다 — 문답으로 세어집니다.`);
    continue;
  }
  const rendered = renderEntry({ at: AT, kind: logKind, ok: true, target: 'WHK DM', answer: '본문' });
  const header = rendered.split('\n')[0];
  if (header.endsWith(`· ${logKind}`)) {
    fail(`kind '${logKind}' 에 KIND_LABEL 이름이 없습니다 — 헤더에 raw kind 가 그대로 나옵니다.\n${header}`);
  }
}

/* ⑩ errLabel(claude.js) 가 유일한 판정 자리가 됐는지 — scheduler.js 도 이걸 쓴다(2026-08-28).
 *
 * 이 저장소는 같은 판정이 두 곳으로 갈려 한쪽만 옛 기준으로 남는 사고가 여러 번 났다
 * (isBotMessage 넷, fold/needles 둘). 이번 사고는: claude.js 의 errLabel 은 연결 오류에
 * 'Connection error.' 를 냈는데 scheduler.js 는 `err?.hermesType || err?.type` 만 봐서
 * type 이 없는 연결 오류가 이름을 잃고 집계 표에서 (기타)로 묻혔다(항목 안 재시도 줄과
 * index.md 집계 표가 서로 다른 이름을 냈다, 실측 재현).
 *
 * 대표적인 오류 모양 넷을 실제로 errLabel 에 먹여 짧고 안정된 이름이 나오는지 본다. */
const anthropicShapeErr = { type: 'overloaded_error', status: 529, message: 'Overloaded' };
if (errLabel(anthropicShapeErr) !== 'overloaded_error') {
  fail(`Anthropic 형 오류의 이름이 'overloaded_error' 가 아닙니다: '${errLabel(anthropicShapeErr)}'`);
}

const slackShapeErr = { code: 'slack_webapi_platform_error', message: 'An API error occurred: not_in_channel' };
if (errLabel(slackShapeErr) !== 'slack_webapi_platform_error') {
  fail(`슬랙 형 오류의 이름이 'slack_webapi_platform_error' 가 아닙니다: '${errLabel(slackShapeErr)}'`);
}

// 실제 SDK 클래스로 만든 연결 오류 — 실측(2026-08-28)으로 .type=null, .status=undefined,
// .name='Error'(SDK 가 안 덮어씀), .constructor.name='APIConnectionError' 였다.
const connectionErr = new APIConnectionError({ message: 'Connection error.' });
if (errLabel(connectionErr) !== 'APIConnectionError') {
  fail(`연결 오류의 이름이 'APIConnectionError' 가 아닙니다: '${errLabel(connectionErr)}' — ` +
    `(err.name 은 'Error' 로 뭉개져 있어 못 쓰고, constructor.name 을 써야 한다)`);
}

// 메시지가 길고 매번 다른 우리 오류(git.js 의 `git <명령> 실패: <stderr>` 모양). **가장
// 중요한 검사** — 메시지만 다르고 종류가 같은 오류 둘이 같은 이름으로 모이나. 다르면
// 집계 표가 회차마다 새 행을 만든다(이번 사고의 ② 부분).
const gitErrA = new Error('git push 실패: fatal: unable to access \'https://example.com/a.git/\': Could not resolve host');
const gitErrB = new Error('git push 실패: fatal: The remote end hung up unexpectedly (재현용 임의 문구 9182)');
const labelA = errLabel(gitErrA);
const labelB = errLabel(gitErrB);
if (labelA !== labelB) {
  fail(`메시지만 다르고 종류가 같은 오류가 다른 이름을 냈습니다 — 집계 표가 쪼개집니다: '${labelA}' vs '${labelB}'`);
}
if (labelA !== 'git push 실패') {
  fail(`git 오류의 안정된 이름이 기대와 다릅니다: '${labelA}' (기대: 'git push 실패')`);
}

// 길이 상한 — 구조화 값(type·code)과 마지막 보루(메시지) 모두 40자를 넘지 않아야 한다.
for (const [label, e] of [
  ['Anthropic', anthropicShapeErr], ['Slack', slackShapeErr],
  ['연결오류', connectionErr], ['git', gitErrA],
  ['긴 메시지', new Error('x'.repeat(300))],
]) {
  const out = errLabel(e);
  if (out.length > 40) fail(`${label} 오류의 이름이 40자를 넘습니다 (${out.length}자): '${out}'`);
}

/* ⑪ `errorType:` 을 `err` 에서 **새로 계산하는** 줄이 전부 errLabel 을 쓰나 — 파일을 읽어
 * 대조한다(2026-08-28, run-digest.js 가 세 번째로 갈린 자리로 손에 잡힌 뒤).
 *
 * ①~⑨ 는 렌더·집계 함수의 동작을 합성 항목으로 재지만, 이번 사고는 함수 동작이 아니라
 * **호출부 하나가 errLabel 을 안 부른 것**이었다 — scheduler.js 는 고쳤는데 run-digest.js 에
 * 똑같은 옛 모양(`err?.type`)이 독립적으로 남아 있었다. 동작 검사로는 못 잡고 코드를 읽어야
 * 잡힌다.
 *
 * **거는 것**: `errorType:` 뒤에 오는 값에 `err` 토큰(`err?.type`·`err.message` 등)이 있는데
 * `errLabel(` 호출이 없는 줄. err 에서 사유를 새로 만드는 자리는 전부 이 모양이어야 한다.
 *
 * **안 거는 것**:
 *   - `errorType: res.errorType` (scheduler.js·run-digest.js 의 "안 던지고 돌아온 실패" 갈래) —
 *     쓰는 변수가 `res` 지 `err` 가 아니다. 이미 다른 곳(digest.js)에서 만들어진 값을 그대로
 *     넘기는 자리라 여기서 또 계산할 이유가 없다.
 *   - `errorType: 'ingest_fatal'` · `errorType: null` 같은 리터럴 — err 참조가 아예 없다.
 *   - `errorType: errLabel(err)` (claude.js 의 재시도 루프, scheduler.js·run-digest.js 의
 *     고친 자리) — errLabel 을 부르므로 통과.
 *
 * **알려진 한계**: catch 변수 이름이 정확히 `err` 인 줄만 본다 — 이 저장소의 실제 관례
 * (scheduler.js·run-digest.js·claude.js 전부 `catch (err)`)를 그대로 따른 것이고, 누가
 * `catch (e)` 처럼 다른 이름을 쓰면 이 검사는 못 잡는다. 변수명 통일은 이번 범위 밖이다.
 * 이 스크립트 자기 자신은 훑지 않는다 — 위 테스트들이 쓰는 `errorType: 'overloaded_error'`
 * 류는 err 에서 계산한 값이 아니라 합성 테스트 데이터라 대상이 아니다. */
const HERMES_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SELF = fileURLToPath(import.meta.url);

function jsFilesUnder(dir) {
  const out = [];
  for (const name of fs.readdirSync(dir)) {
    if (name === 'node_modules') continue;
    const p = path.join(dir, name);
    const st = fs.statSync(p);
    if (st.isDirectory()) out.push(...jsFilesUnder(p));
    else if (name.endsWith('.js')) out.push(p);
  }
  return out;
}

const scanTargets = [
  ...jsFilesUnder(path.join(HERMES_ROOT, 'src')),
  ...jsFilesUnder(path.join(HERMES_ROOT, 'scripts')),
].filter((p) => p !== SELF);

const ERR_LINE = /errorType:\s*([^,]*)/;
for (const file of scanTargets) {
  const lines = fs.readFileSync(file, 'utf8').split('\n');
  lines.forEach((line, i) => {
    const m = line.match(ERR_LINE);
    if (!m) return;
    const value = m[1];
    if (/\berr\b/.test(value) && !value.includes('errLabel(')) {
      fail(
        `${path.relative(HERMES_ROOT, file)}:${i + 1} 가 err 에서 errorType 을 계산하는데 ` +
          `errLabel 을 안 씁니다 — 판정이 또 갈립니다.\n    ${line.trim()}`,
      );
    }
  });
}

process.exit(ok ? 0 : 1);
