#!/usr/bin/env node
/**
 * Hermes 대화 로그를 지금 렌더한다 (원본 jsonl → 읽는 md).
 *
 *   npm run log:render              무엇이 바뀌는지만 보여준다 (파일을 쓰지 않음)
 *   npm run log:render -- --write   실제로 md 를 쓴다
 *   npm run log:render -- --print   렌더 결과 전문을 콘솔에 출력
 *
 * 기본이 미리보기인 이유: 이 스크립트가 md 를 쓰면 저장소 작업 트리가 더러워지고,
 * 커밋하지 않은 채 두면 다음 자동 반영(매일 07:00)이 syncBeforeWork 에서 멈춘다.
 * 평상시에는 손으로 돌릴 일이 없다 — 자동 반영이 매일 같은 일을 한다.
 *
 * 슬랙도 모델도 부르지 않는다. 토큰·API 키가 필요 없다.
 */
import { LOG_ENABLED, LOG_DIR, LOG_RAW_DIR } from '../src/config.js';
import { renderAll } from '../src/convo-log.js';

const write = process.argv.includes('--write');
const print = process.argv.includes('--print');

if (!LOG_ENABLED) {
  console.log('대화 로그가 꺼져 있습니다 (config.json 의 log.enabled).');
  process.exit(0);
}

console.log(`원본: ${LOG_RAW_DIR}`);
console.log(`대상: ${LOG_DIR}\n`);

const res = renderAll({ write });

/* 원본이 사라졌다. **아무것도 안 썼다** — convo-log.js 의 sourceGone 참조.
 * 종료코드 1 로 닫는다. 0 으로 조용히 끝내면 「할 일이 없었다」와 구별되지 않는다. */
if (res.noSource) {
  console.log('⚠️  원본이 없습니다 — 아무것도 쓰지 않았습니다.\n');
  console.log(`    이미 렌더돼 있는 달: ${res.rendered.join(' · ')}`);
  console.log('');
  console.log('    원본 jsonl 은 git 밖이라 클론에 안 딸려옵니다. 저장소를 새로 복제했거나');
  console.log('    다시 배포한 뒤라면 옛 자리에서 옮겨 오세요. 그대로 렌더하면 위 달들이');
  console.log('    빈 표로 덮입니다 — 2026-08-31 에 실제로 6,743줄을 그렇게 잃었습니다.');
  process.exit(1);
}

for (const m of res.months) {
  console.log(
    `${m.ym} — 문답 ${m.qa}건 · 정기 발송 ${m.broadcast}건 · 실패 ${m.failed}건 · ` +
      `약 $${m.usd.toFixed(2)}${m.broken ? ` · ⚠️ 읽지 못한 줄 ${m.broken}개` : ''}` +
      `${m.changed ? (write ? '  → 씀' : '  → 바뀜(안 씀)') : '  → 그대로'}`,
  );
  if (print) console.log('\n' + '─'.repeat(52) + '\n' + m.text + '─'.repeat(52) + '\n');
}

if (!res.months.length) console.log('아직 기록된 대화가 없습니다.');

if (write) {
  if (res.changed) {
    console.log(
      `\n파일 ${res.changed}개를 썼습니다. **커밋하지 않으면 다음 자동 반영(07:00)이 멈춥니다** — ` +
        '작업 트리가 더러우면 syncBeforeWork 가 예외를 던집니다.',
    );
  } else {
    console.log('\n바뀐 것이 없습니다.');
  }
} else {
  console.log(`\n(미리보기입니다. 실제로 쓰려면 --write)`);
}
