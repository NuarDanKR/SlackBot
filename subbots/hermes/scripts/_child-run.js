/**
 * 자식 검사 하나를 돌린다 — **돌고 있는 것이 보이게.**
 *
 * `check-setup.js` 가 쓴다. 판정을 그쪽에 두지 않고 떼어낸 이유는 두 가지다 —
 * 941줄짜리를 통째로 시험에 넣을 수 없고(자식 100여 개라 수 분), 두 벌로 두면
 * 한쪽만 고쳐지는데 그때 조용해지는 쪽은 늘 운영이다.
 *
 * ## 무엇을 고치나 (2026-10-07)
 *
 * 전에는 `spawnSync(..., {encoding})` 하나였다. 그러면 자식 출력이 **버퍼에 갇힌다** —
 * 끝나기 전에는 한 글자도 안 보이고, 통과하면 `[보임]`·`[못잼]` 줄만 올라온다.
 * 상한도 없어서 영원히 돌아도 그대로 기다린다.
 *
 * TYIT 사전 검사에서 `check-brief-split.js` 가 12분 넘게 CPU 를 먹었을 때 화면의
 * 마지막 줄은 **이미 끝난 항목**이었다. 사람이 본 것은 멈춘 화면이고 실제로는 돌고
 * 있었다. 침묵은 「빠르다」 와 화면에서 같다.
 *
 * | 고친 것 | 왜 |
 * |---|---|
 * | 시작 **즉시** 이름을 찍는다 | 돌고 있는 것이 무엇인지 그 자리에서 보여야 한다 |
 * | 끝나면 소요 시간을 찍는다 | 「느리다」 를 다음 사람이 숫자로 받는다 |
 * | 상한을 넘기면 중단하고 **이름을 적어** 실패한다 | 이름 없는 타임아웃은 다시 재현해야 안다 |
 * | 중단돼도 **그때까지의 출력**을 보인다 | 12분을 기다린 사람이 빈 화면을 받으면 안 된다 |
 *
 * `[보임]`·`[못잼]` 판독은 **그대로**다. 통과한 검사가 「한 항목도 못 쟀다」 를 적어도
 * 종료코드는 0 이라, 그 줄을 삼키면 안전망이 꺼진 것이 어디에도 안 보인다.
 *
 * ## 왜 여전히 `spawnSync` 인가
 *
 * 진짜 스트리밍은 비동기 `spawn` 이라야 한다. 그러려면 `check-setup.js` 의 흐름 전체를
 * 비동기로 뒤집어야 하는데, 그 파일은 여섯 구간이 순서에 기대어 돌고 다른 검사가
 * 그것을 흉내 내어 몬다(`check-check-modes.js`). 이 작업의 범위가 아니다.
 *
 * 대신 **침묵의 길이에 상한을 둔다.** 자식이 상한 안에 끝나면 그 시간만큼만 조용하고,
 * 넘기면 중단되며 그때까지의 출력이 이름과 함께 나온다. 12분 침묵이 사라지는 것이
 * 이 작업이 사는 자리다.
 */
import { spawnSync } from 'node:child_process';
import path from 'node:path';

/** 자식 하나의 기본 상한. 가장 느린 정상 검사보다 넉넉하고, 사람의 인내보다는 짧다. */
export const DEFAULT_LIMIT_MS = 120_000;

/**
 * 상한을 환경에서 읽는다. **읽을 수 없으면 기본값으로 돌아간다.**
 *
 * `0` 이나 `NaN` 으로 읽으면 모든 검사가 즉시 상한 초과가 되어 관문이 통째로
 * 빨개지고, 그 상태에서 사람이 배우는 것은 관문을 끄는 법이다.
 */
export function childLimitMs(env = process.env) {
  const raw = Number(env?.HERMES_CHECK_TIMEOUT_MS);
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_LIMIT_MS;
}

/** 사람이 읽는 소요 시간. 1초 미만은 ms 로 — 「0초」 가 줄지어 있으면 아무 정보가 없다. */
export function formatDuration(ms) {
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}초`;
}

/**
 * 통과 화면에 올려 보낼 줄만 고른다. **판독 규칙은 여기 하나다.**
 * (`check-setup.js` 에 있던 것을 2026-10-07 에 옮겼다. 경위는 그대로 둔다.)
 *
 * 통과한 검사의 출력은 버리지만 **두 종류만은 올려 보낸다.**
 *
 * ① 「못 잼·건너뜀」 — 2026-09-01 실측: 자료 저장소의 `check-fixtures.json` 을 잃으면
 * 검사 둘이 질의를 못 읽어 **한 항목도 안 돌고** 종료코드 0 을 낸다. 그 검사들은
 * 「재지 못했습니다 · 만들 자리는 …」 를 화면에 성실히 적는데, 그것이 통째로
 * 버려져서 `npm run check` 는 초록으로 「안전망이 켜지고 권한을 지키나 ✓」 라고
 * 말했다. 안전망 둘이 꺼진 것이 어디에도 안 보였다.
 *
 * 종료코드는 그대로 0 이다 — 새 팀에는 그 파일이 없는 것이 정상이라 실패로 내면
 * 매일 빨간 줄을 보며 무시하는 법을 배운다. 대신 **조용하지는 않게** 한다.
 *
 * ② `[보임]` 으로 시작하는 줄 — **통과여도 사람이 봐야 하는 숫자**를 검사가 스스로
 * 지목하는 자리다. 「몇 개를 대봤나」 같은 값이 여기 온다. 그런 검사의 고장은
 * ✗ 가 아니라 **재료가 조용히 줄어드는 것**이라 통과 화면에 숫자가 없으면 아무도
 * 모른다(`check-business-names.js` 의 이름 출처가 그 예다 — `documentsPath` 오타
 * 하나로 사업장 몫이 0이 되어도 검사는 그냥 초록을 낸다).
 *
 * 남용하면 ①이 안 읽히므로 **숫자 한 줄**로 끝낼 것. 말은 그 검사 안에 적는다.
 *
 * ── ①을 문구로 찾지 않는다 (2026-09-03) ──
 *
 * ①은 오래 **말버릇 목록**(`건너뜀|재지 못|못 잼|못 쟀`)으로 찾았다. 그 목록에 안 드는
 * 말로 「못 쟀다」를 적은 검사는 통째로 버려지고 화면에 **초록 한 줄만** 남는다.
 * 실제로 넷이 그랬다 — `check-outside-hits.js` 의 「건너뛰**고**」(그 안에 「비공개가
 * 밖으로 안 새나」가 들어 있다) · `check-brief-stamp.js`·`check-log-render.js` 의
 * 「안 댔습니다」 · `check-attachment-marks.js` 의 「시험할 수 없습니다」.
 *
 * 문구를 목록에 맞추면 다음 사람이 문구를 바꿀 때 또 갈린다. 그래서 **검사가 스스로
 * 지목하게** 한다: `[못잼]` 으로 시작하는 줄은 말이 무엇이든 올려 보인다. `[보임]` 과
 * 같은 방식이고, 표시를 단 검사는 문구를 아무렇게나 바꿔도 안 갈린다.
 *
 * 말버릇 목록은 **아직 표시를 안 단 검사들을 위해 남겨 둔다.** 지우면 그 검사들이
 * 오늘 당장 조용해진다 — 표시가 다 붙은 날 지운다.
 */
export function visibleLines(output) {
  const out = [];
  for (const line of String(output || '').split('\n')) {
    const t = line.trim();
    if (t.startsWith('[보임]')) out.push(t.slice('[보임]'.length).trim());
    else if (t.startsWith('[못잼]')) out.push(t.slice('[못잼]'.length).trim());
    else if (/건너뜀|재지 못|못 잼|못 쟀/.test(line) && t) out.push(t);
  }
  return out;
}

/**
 * 자식 검사 하나. `{ ok, timedOut, ms, output, visible }`.
 *
 * 화면에 찍는 것은 **부르는 쪽이 넘긴 함수**로 한다(`log`·`ok`·`bad`) — `check-setup`
 * 의 기존 ✓/✗ 모양과 `failed` 집계를 그대로 쓰기 위해서다. 여기서 새 모양을 만들면
 * 한 화면에 두 가지 서식이 섞인다.
 */
export function runCheck({
  file,
  label,
  limitMs = childLimitMs(),
  node = process.execPath,
  log = console.log,
  ok = console.log,
  bad = console.error,
  indent = '      ',
}) {
  const name = path.basename(file);
  // **먼저 찍는다.** 자식이 오래 걸려도 무엇이 도는지는 이 줄로 남는다.
  log(`  … ${label}`);

  const started = Date.now();
  const r = spawnSync(node, [file], {
    encoding: 'utf-8',
    timeout: limitMs,
    killSignal: 'SIGKILL',
  });
  const ms = Date.now() - started;
  const output = `${r.stdout || ''}${r.stderr || ''}`;
  // `spawnSync` 는 상한에 걸리면 `error.code = 'ETIMEDOUT'` 을 주고 신호로 끊긴다.
  // 둘 다 본다 — 플랫폼에 따라 한쪽만 채워지는 경우가 있다.
  const timedOut = r.error?.code === 'ETIMEDOUT' || (r.status === null && r.signal != null);

  if (timedOut) {
    bad(`${label} — ${formatDuration(limitMs)} 상한을 넘겨 중단했습니다 (node scripts/${name})`);
    // **그때까지의 출력을 보인다.** 안 보이면 어디까지 갔는지조차 모른 채 다시 돌려야 한다.
    const seen = output.trim();
    if (seen) for (const line of seen.split('\n')) log(`${indent}${line}`);
    else log(`${indent}(이 검사는 중단될 때까지 한 줄도 적지 않았습니다)`);
    return { ok: false, timedOut: true, ms, output, visible: [] };
  }

  if (r.status === 0) {
    const visible = visibleLines(output);
    ok(`${label}  (${formatDuration(ms)})`);
    for (const line of visible) log(`${indent}${line}`);
    return { ok: true, timedOut: false, ms, output, visible };
  }

  bad(`${label}  (node scripts/${name})  (${formatDuration(ms)})`);
  // 사유는 그 검사가 이미 사람 말로 적어 두었다. 그대로 들여쓰기만 해서 보인다.
  for (const line of output.trim().split('\n')) {
    if (line.trim()) log(`${indent}${line}`);
  }
  return { ok: false, timedOut: false, ms, output, visible: [] };
}
