#!/usr/bin/env node
/**
 * 「열어 보고 나서 없다고 말하라」는 지시가 프롬프트에 남아 있나 —
 * 그리고 그것이 **부분 일치에만** 걸려 있지 않나.
 *
 *   node scripts/check-open-before-deny.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 ──
 *
 * 2026-09-10 저녁 재구조에서 옛 「색인 값 경고」 문구가 지워졌고, 새로 들어온
 * 「발췌 한계」 문구는 `(n/m 낱말)` 이 붙은 **부분 일치 결과에만** 적용됐다.
 * 낱말이 전부 맞은 확정 히트의 발췌도 회차 앞 4,000자를 자른 것일 뿐인데
 * 그 사실을 말해 주는 문장이 프롬프트에서 사라진 것이다.
 *
 * 그래서 값이 4,000자 밖에 있는 큰 문서에서 봇이 **문서를 열지 않고**
 * 「아카이브에 없습니다」로 닫았다 (재생 2/2 재현, 옛 판은 1/1 성공).
 * 팀이 가장 경계하는 「조용한 자료 없음 오답」이다.
 *
 * **이 검사는 문구를 강제하지 않는다.** 지시가 존재하고, 그 지시가 부분 일치라는
 * 조건에 갇혀 있지 않다는 두 가지만 본다 — 문구는 사람이 자유롭게 다듬는다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const QA = path.join(HERE, '..', 'src', 'prompts', 'qa.md');

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

const text = fs.readFileSync(QA, 'utf8').replace(/\r\n/g, '\n');

/**
 * **불릿 하나가 한 조각**이 되게 쪼갠다 — 「한 지시 안에」 있는지를 봐야 하기 때문이다.
 * 문장이 서로 다른 불릿에 흩어져 있으면 각각은 걸려도 봇에게는 이어진 지시가 아니다.
 *
 * 쪼개는 방식을 두 번 틀렸다. 그 경위를 적어 둔다 — 셋 다 「거짓 통과」로 끝났고,
 * 거짓 통과하는 검사는 없느니만 못하다.
 *
 *   1차 `split(/\n(?=- \*\*)/)`  — 「다음 불릿 직전까지」라 그 사이의 절 제목·다른
 *      목록·산문이 통째로 딸려 들어왔다. 서로 무관한 문단의 문장들이 세 조건을
 *      우연히 다 만족시켰다.
 *   2차 `split(/\n{2,}/)`        — 빈 줄로만 나눈다. 그런데 이 파일의 「알아야 할 한계」
 *      절은 **불릿 사이에 빈 줄이 없어** 불릿 11개가 통째로 한 조각이 됐다. 그래서
 *      새 불릿을 지우고 절 아무 데나 「확정」 두 글자만 넣어도 통과했다 (회의적 검증
 *      실측, 2026-09-10). 반대로 같은 뜻으로 더 강하게 고쳐 쓴 문장 3종은 앞 불릿의
 *      「일부만 맞은」에 묶여 이유 없이 빨개졌다.
 *
 * 그래서 지금은 **줄로 읽는다.** `- ` 로 시작하는 줄이 새 불릿을 열고, 들여쓴 줄
 * (하위 불릿·이어지는 줄)은 그 불릿에 붙고, 그 밖의 줄(절 제목·산문·빈 줄)은 닫는다.
 * 이 파일은 불릿 하나가 한 줄이라 실제로는 대개 한 줄짜리 조각이 된다.
 */
function toBullets(src) {
  const out = [];
  let cur = null;
  for (const line of src.split('\n')) {
    if (/^-\s/.test(line)) {
      if (cur) out.push(cur);
      cur = line;
    } else if (cur && /^\s+\S/.test(line)) {
      cur += `\n${line}`;
    } else if (cur) {
      out.push(cur);
      cur = null;
    }
  }
  if (cur) out.push(cur);
  return out;
}

const bullets = toBullets(text);

// ① 「값이 발췌에 안 보여도 열고 나서 없다고 말하라」는 지시가 있나.
const DENY = /확인되지\s*않습니다|없습니다/;
const OPEN = /read_document/;
const VALUE = /값|금액|수치|표/;
const instruction = bullets.filter((b) => OPEN.test(b) && DENY.test(b) && VALUE.test(b));
if (!instruction.length) {
  fail('qa.md 에 「값을 묻는 질문에서 read_document 로 열어 확인한 뒤에만 없다고 답하라」는 지시가 없습니다.');
} else {
  pass(`열어-확인 지시 ${instruction.length}개`);
}

/* ② **조건 없는 지시가 스스로 서 있나.**
 *
 * 요구하는 것은 하나다 — 열어-확인 지시 불릿 중 **적어도 하나**가
 *   ⓐ 부분 일치를 아예 언급하지 않고(`PARTIAL_ONLY` 없음), **그리고**
 *   ⓑ 「모든/어떤/확정/다 맞은…」처럼 **적용 범위가 전부임을 말한다**(`CONFIRMED` 있음).
 *
 * **둘을 `&&` 로 묶는 것이 핵심이다.** 처음에는 `!PARTIAL_ONLY || CONFIRMED` 로 뒀는데
 * 그 `||` 가 탈출구였다 — 조건 없는 불릿을 통째로 지우고, 부분 일치에만 걸린 49행 불릿에
 * 「모든」·「어떤」·「항상」·「확정」 중 한 낱말만 끼워 넣으면 초록이 됐다 (회의적 검증이
 * 변이 4종으로 재현, 2026-09-10). 지시가 사라진 상태에서 통과하는 검사는 없느니만 못하다.
 *
 * **이 검사가 못 하는 것을 적어 둔다** — 이것은 낱말로 재는 2차 방어선이지 뜻을 읽지 않는다.
 *   · 부분 일치를 **언급하면서** 확정 히트까지 덮는 문장(「(n/m 낱말)이 붙지 않은 결과의
 *     발췌도 잘립니다」)은 ⓐ에 걸려 빨개진다. **일부러 그렇게 뒀다** — 조건 없는 지시는
 *     조건 있는 지시와 한 불릿에 엉키지 말고 따로 서야 한다는 것이 여기서 요구하는 모양이다.
 *     빨개지면 문장을 나눠 쓰면 된다.
 *   · 절을 통째로 다시 쓰면서 위 낱말을 하나도 안 쓰면 빠져나갈 수 있다. 막을 방법이
 *     낱말 목록뿐이라 그렇다. 1차 방어선은 이 검사가 아니라 **재생 시험**이다. */
const PARTIAL_ONLY = /\(n\/m 낱말\)|일부만 맞은/;
const CONFIRMED = /(다|전부|모두) 맞[은았]|모두 든|확정|어떤|모든|언제나|항상|무관하게|가릴 것 없이/;
const unconditioned = instruction.filter((b) => !PARTIAL_ONLY.test(b) && CONFIRMED.test(b));
if (instruction.length && !unconditioned.length) {
  fail('부분 일치를 언급하지 않으면서 「확정 히트에도 적용된다」고 말하는 열어-확인 지시가 없습니다 '
    + '— 확정 히트의 발췌도 앞부분만 잘려 옵니다 (2026-09-10 회귀). 조건 없는 지시를 불릿 하나로 따로 세우세요.');
} else if (unconditioned.length) {
  pass(`조건 없이 확정 히트까지 덮는 지시 ${unconditioned.length}개`);
}

/* ③ **qa.md 가 부르는 부분 일치 표기가 코드가 실제로 내보내는 글자와 같은가.**
 *
 * ②만으로는 한 갈래가 남는다 — 49행 불릿에서 `(n/m 낱말)`·「일부만 맞은」이라는 **표기만**
 * 다른 말로 바꾸면 그 불릿이 「부분 일치 얘기가 아닌」 것으로 보여, 조건 없는 지시를 지운
 * 뒤에도 초록이 된다 (변이 X3 로 재현, 2026-09-10).
 *
 * 그 갈래를 낱말 추측이 아니라 **코드와 대보는 것**으로 막는다. 저 두 글자는 `documents.js`
 * 가 검색 결과 note 에 실제로 찍는 것이라, qa.md 가 그것을 안 부르면 봇이 보는 화면과
 * 프롬프트의 설명이 어긋난 것이다 — 그 자체가 고쳐야 할 드리프트다. 이 저장소가 두 자리에
 * 나뉜 판정을 맞대 보는 방식(`check-shared-rules.js`)과 같은 모양이다.
 *
 * **코드가 그 글자를 안 쓰게 되면 이 검사는 건너뛴다** — 없어진 계약을 지키라고 할 수는 없다.
 * 건너뛸 때는 그렇게 말한다. 조용히 통과시키지 않는다. */
const DOCS = path.join(HERE, '..', 'src', 'documents', 'search.js');
const code = fs.readFileSync(DOCS, 'utf8');
const MARKERS = ['일부만 맞은', '낱말)'];
const emitted = MARKERS.filter((m) => code.includes(m));
if (!emitted.length) {
  console.log('  - 건너뜀: documents.js 가 부분 일치 표기를 더는 안 찍습니다 — 대볼 계약이 없습니다.');
} else {
  const missing = emitted.filter((m) => !text.includes(m));
  if (missing.length) {
    fail(`documents.js 가 검색 결과에 찍는 표기 ${missing.map((m) => `'${m}'`).join(' · ')} 를 qa.md 가 부르지 않습니다 `
      + '— 봇이 보는 화면과 프롬프트의 설명이 어긋납니다.');
  } else {
    pass(`부분 일치 표기 ${emitted.length}개가 코드와 qa.md 에서 같습니다`);
  }
}

// ④ 「없다」 직전의 카드 추종 지시가 있나 (2026-09-11 패키지 P3).
const CARD = /전사 종합/;
const FOLLOW = /read_document/;
const cardInstr = bullets.filter((b) => CARD.test(b) && FOLLOW.test(b) && DENY.test(b));
if (!cardInstr.length) {
  fail('qa.md 에 「전사 종합 카드가 있으면 열어 확인한 뒤에만 없다고 답하라」는 지시가 없습니다.');
} else {
  pass(`카드 추종 지시 ${cardInstr.length}개`);
}

/* ⑤ 카드 구역 제목(llm/tools.js)과 위 ④가 찾은 「전사 종합」 지시가 **같은 것을 가리키나**
 * — ③과 같은 「코드와 대보기」다(2026-09-11 최종 검토 Important 4).
 *
 * ④는 qa.md **안에서** 낱말이 있나만 본다. 그래서 `llm/tools.js` 의 카드 구역 제목을
 * (예: 「전사 종합 문서」→「통합 보고 카드」로) 바꿔도 qa.md 를 안 건드리면 ④는 계속
 * 초록이다 — qa.md 의 지시가 이제 봇이 실제로 보는 구역 제목과 다른 문구를 가리키는데도
 * 잡히지 않는다. `llm/tools.js` 가 `outsides.push` 로 카드 구역에 실제로 찍는 title 리터럴을
 * 읽어 그 핵심 이름(괄호 앞부분)이 qa.md 에 그대로 있는지 대본다.
 *
 * **카드 앵커나 제목을 못 찾으면 실패한다** (Codex R1e).
 * 파일 이동 오류나 카드 기능 유실을 건너뜀으로 숨기지 않는다.
 * `r.cards?.length` 는 있는데 title 만 못 찾는 경우도 계약 파손 또는 낡은 검사로 본다.
 * (2026-09-11 실물 사고 — 카드 안내문을 절 유무로 갈라 쓰면서 그 사이 주석이 길어져
 * 옛 `[\s\S]{0,400}?` 글자 수 상한을 넘겼고, 계약은 멀쩡한데 검사가 「건너뜀」으로
 * 조용히 눈을 감았다. `npm run check` 화면의 「건너뜀」 줄로 잡았다).
 *
 * **글자 수 상한에 안 기댄다.** `r.cards?.length` 를 앵커로 그 뒤 **`outsides.push({`
 * 를 먼저 찾고, 그 안의 첫 `title:`** 을 잡는다 — 둘 다 코드 구조 표식이지 길이가
 * 아니라서, 그 사이 주석이 아무리 길어져도(또는 짧아져도) 안 흔들린다. */
const CLAUDE = path.join(HERE, '..', 'src', 'llm', 'tools.js');
const claudeSrc = fs.readFileSync(CLAUDE, 'utf8');
const cardsAnchor = claudeSrc.indexOf('r.cards?.length');
if (cardsAnchor === -1) {
  fail('llm/tools.js 에 카드 구역 앵커가 없습니다 — 검사 위치 오류 또는 카드 기능 유실입니다.');
} else {
  const pushIdx = claudeSrc.indexOf('outsides.push({', cardsAnchor);
  const titleMatch = pushIdx !== -1 && claudeSrc.slice(pushIdx).match(/title:\s*'([^']+)'/);
  if (pushIdx === -1 || !titleMatch) {
    fail('llm/tools.js 에 r.cards?.length 는 있는데 그 뒤 outsides.push({ 블록에서 title 리터럴을 못 찾았습니다 '
      + '— 정규식이 낡았거나 카드 구역 구조가 바뀐 것으로 보입니다. 이 검사를 실물 구조에 맞춰 다시 쓰세요.');
  } else {
    const cardTitle = titleMatch[1];
    const cardTitleCore = cardTitle.replace(/\s*\(.*$/, '').trim(); // 괄호 앞부분만 — 「전사 종합 문서」
    if (!text.includes(cardTitleCore)) {
      fail(`llm/tools.js 의 카드 구역 제목 '${cardTitle}' 의 핵심 이름 '${cardTitleCore}' 을 qa.md 가 안 부릅니다 `
        + '— 카드 구역 제목이 바뀌었는데 qa.md 의 지시는 옛 이름을 가리키고 있을 수 있습니다.');
    } else {
      pass(`카드 구역 제목 '${cardTitleCore}' 이 llm/tools.js·qa.md 양쪽에서 같습니다`);
    }
  }
}

process.exit(ok ? 0 : 1);
