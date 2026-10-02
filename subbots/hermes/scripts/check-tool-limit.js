#!/usr/bin/env node
/**
 * 도구 반복 상한에 걸려 멈춘 회차가 **정상 답변과 구별되나.**
 *
 *     node scripts/check-tool-limit.js
 *
 * 종료코드: 0 통과 / 1 실패 있음
 *
 * ── 왜 남겨 두는가 ──
 *
 * 툴 루프는 `max_iterations`(= `config.limits.maxToolIterations`, 지금 10)에서 끊긴다.
 * 그 끊김은 **SDK 안에서 예외도 표시도 없이 일어난다** — `BetaToolRunner` 의 반복자
 * 맨 앞이 `if (max_iterations && iterationCount >= max_iterations) break;` 한 줄이라,
 * 우리 쪽 `for await` 은 그냥 조용히 끝난다.
 *
 * 그때 마지막 응답은 **도구를 더 부르려던 중간 상태**(`stop_reason: 'tool_use'`)라
 * 본문 글자가 없거나 토막이다. 예전에는 `max_tokens` 와 `refusal` 만 봤으므로 이 경우가
 * 아무 데도 안 걸렸고, 글자가 없으면 「모델이 답을 안 만들었다」로 읽어
 * **「답변을 만들지 못했습니다. 질문을 조금 더 구체적으로 적어 주세요.」** 를 게시했다 —
 * 봇이 질문자를 탓하는 문장이다. 실물: `hermes-log/2026-08.md` 2026-08-14 22:35 문답
 * (도구 16회 · 74.4초 · 약 $1.440 · 출력 3,023토큰을 쓰고 답변란은 저 한 줄).
 *
 * `stop_reason === 'tool_use'` 로 루프를 빠져나오는 길은 이 상한 하나뿐이다. 모델이
 * 도구를 부르면 우리 쪽은 계속 돌고, 안 부르면 `stop_reason` 이 `tool_use` 가 아니다.
 *
 * **API 를 부르지 않는다.** `Messages.prototype.toolRunner` 를 가짜로 갈아 끼워
 * 상한 도달을 흉내 낸 응답만 흘린다. 아카이브는 읽기만 한다(시스템 색인 생성).
 */
process.env.ANTHROPIC_API_KEY ||= 'check-stub-not-used';

const { default: Anthropic } = await import('@anthropic-ai/sdk');
const { config, PUBLIC_ACCESS } = await import('../src/config.js');

/* 가짜 툴 러너 — 실제 SDK 가 하는 일 중 우리 루프가 기대는 것만 흉내 낸다.
 * (1) 응답을 하나씩 내주고 (2) 마지막 응답의 도구 호출에 대한 결과 메시지를 만들고
 * (3) setMessagesParams 를 받아 준다. 상한 도달은 「더 낼 응답이 없다」로 표현한다 —
 * SDK 의 `break` 와 겉보기가 같다. */
class StubRunner {
  constructor(script) {
    this.script = script;
    this.current = null;
  }

  async *[Symbol.asyncIterator]() {
    for (const m of this.script) {
      this.current = m;
      yield m;
    }
  }

  async generateToolResponse() {
    const uses = (this.current?.content || []).filter((b) => b.type === 'tool_use');
    if (!uses.length) return null;
    return {
      role: 'user',
      content: uses.map((u) => ({
        type: 'tool_result',
        tool_use_id: u.id,
        content: '(가짜 도구 결과 — 검사용)',
      })),
    };
  }

  setMessagesParams() {}
}

const proto = Object.getPrototypeOf(new Anthropic({ apiKey: 'check-stub' }).beta.messages);
const realToolRunner = proto.toolRunner;
let script = [];
proto.toolRunner = function stubToolRunner() {
  return new StubRunner(script);
};

const { answerQuestion } = await import('../src/claude.js');

const MODEL = config.models.qa.id;
const MAX = config.limits.maxToolIterations;
const useBlock = (i) => ({ type: 'tool_use', id: `toolu_stub_${i}`, name: 'search', input: { query: '가짜' } });
const textBlock = (t) => ({ type: 'text', text: t });
const msg = (stop, content) => ({
  id: `msg_stub_${Math.random().toString(36).slice(2)}`,
  role: 'assistant',
  model: MODEL,
  stop_reason: stop,
  content,
  usage: { input_tokens: 1, output_tokens: 1 },
});

let bad = 0;
const fail = (m) => {
  console.log(`  ✗ ${m}`);
  bad += 1;
};
const ok = (m) => console.log(`  ✓ ${m}`);

const BLAME = '답변을 만들지 못했습니다';

async function run(name, messages) {
  script = messages;
  const res = await answerQuestion({
    question: '검사용 질문입니다.',
    access: PUBLIC_ACCESS,
    origin: '점검 스크립트 (check-tool-limit.js)',
    asker: '검사',
  });
  console.log(`\n[${name}] toolLimit=${res.toolLimit} truncated=${res.truncated} refused=${res.refused}`);
  console.log(`  답변: ${JSON.stringify(res.text).slice(0, 300)}`);
  return res;
}

console.log(`도구 반복 상한 = ${MAX} (config.limits.maxToolIterations)`);

/* ① 상한에 걸렸고 글자가 하나도 없다 — 실물 사고와 같은 모양. */
{
  const r = await run('상한 · 본문 없음', [
    msg('tool_use', [useBlock(1)]),
    msg('tool_use', [useBlock(2)]),
  ]);
  if (r.text.includes(BLAME)) fail(`상한에 걸렸는데 질문자를 탓하는 문구가 나갑니다: "${BLAME}…"`);
  else ok('질문자를 탓하는 문구가 안 나갑니다');
  if (!r.text.includes('여기서 멈췄습니다')) fail('멈춘 사실을 알리는 문구가 없습니다');
  else ok('멈춘 사실을 알리는 문구가 있습니다');
  if (r.toolLimit !== true) fail(`toolLimit 이 true 가 아닙니다 (${r.toolLimit}) — 로그가 정상 답변과 구별할 값이 없습니다`);
  else ok('toolLimit=true 로 표시됩니다');
}

/* ② 상한에 걸렸는데 앞부분은 써 놓았다 — 여기까지 나온 답을 버리지 않는다.
 *    본문은 **가짜 값**이다. 여기서 재는 것은 「앞에 나온 글자가 그대로 맨 앞에 남나」라서
 *    내용이 아니라 모양(사업장 이름 + 집계액 한 줄)만 지키면 된다 — 이 저장소는 팀끼리
 *    나눠 쓰므로 실제 집계액을 픽스처에 두지 않는다 (WHK 결정 2026-09-04). */
{
  const r = await run('상한 · 본문 있음', [
    msg('tool_use', [useBlock(1)]),
    msg('tool_use', [textBlock('사업장가 계 412.7억입니다.'), useBlock(2)]),
  ]);
  if (!r.text.startsWith('사업장가 계 412.7억입니다.')) fail('여기까지 나온 본문이 사라졌습니다');
  else ok('여기까지 나온 본문이 그대로 남습니다');
  if (!r.text.includes('여기서 멈췄습니다')) fail('멈춘 사실을 알리는 문구가 없습니다');
  else ok('본문 뒤에 멈춘 사실이 붙습니다');
  if (r.toolLimit !== true) fail(`toolLimit 이 true 가 아닙니다 (${r.toolLimit})`);
  else ok('toolLimit=true 로 표시됩니다');
}

/* ③ 되돌림 방지 — 상한이 아니라 정말로 빈 답이 온 경우는 예전 문구 그대로다.
 *    (도구를 안 부르고 끝냈으니 「자료를 뒤지다 멈췄다」고 적으면 그게 거짓말이 된다) */
{
  const r = await run('정상 종료 · 빈 답', [msg('end_turn', [])]);
  if (!r.text.includes(BLAME)) fail('상한이 아닌 빈 답인데 예전 안내 문구가 사라졌습니다');
  else ok('상한이 아닌 빈 답에는 예전 문구가 그대로입니다');
  if (r.toolLimit) fail('상한이 아닌데 toolLimit 이 켜졌습니다');
  else ok('toolLimit 이 안 켜집니다');
}

/* ④ 되돌림 방지 — max_tokens 로 잘린 회차는 예전 「여기서 잘렸습니다」 그대로다. */
{
  const r = await run('max_tokens · 잘림', [msg('max_tokens', [textBlock('앞부분만 쓰다가')])]);
  if (!r.truncated) fail('max_tokens 인데 truncated 가 false 입니다');
  else ok('truncated=true 그대로입니다');
  if (!r.text.includes('여기서 잘렸습니다')) fail('잘림 안내 문구가 사라졌습니다');
  else ok('잘림 안내 문구가 그대로입니다');
  if (r.toolLimit) fail('max_tokens 인데 toolLimit 이 켜졌습니다');
  else ok('toolLimit 이 안 켜집니다');
}

proto.toolRunner = realToolRunner;

console.log(bad ? `\n✗ ${bad}건 실패` : '\n✓ 전부 통과');
process.exit(bad ? 1 : 0);
