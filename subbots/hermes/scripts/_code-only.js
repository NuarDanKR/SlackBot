/**
 * 주석·문자열·정규식 리터럴의 **속**을 같은 길이의 공백으로 지운 사본을 만든다.
 * 검사 스크립트가 소스를 「코드만」 보고 재야 할 때 쓴다.
 *
 * ── 왜 필요한가 ──
 *
 * 이 저장소의 소스는 주석이 길고, 주석 안에 함수·설정 이름이 그대로 들어 있다. 소스를
 * 날것으로 `indexOf`·정규식으로 재는 검사는 **코드를 지워도 주석에 이름이 남아 있으면
 * 초록**이 된다.
 *
 * ── 길이를 보존한다 ──
 *
 * 지운 자리를 같은 수의 공백으로 채우므로 줄·열·문자 위치가 원본과 같다.
 *
 * ── 이것은 파서가 아니다. 그래서 **틀렸다고 말할 줄 알아야 한다** ──
 *
 * `/` 가 나눗셈인지 정규식인지는 앞 토큰으로 가르는 **휴리스틱**이다(자바스크립트를
 * 제대로 가르려면 파서가 필요하다). 2026-09-11 회의적 검증이 이 휴리스틱을 속이는 입력을
 * 여럿 찾았고, 그중 하나는 **`check-reexport-binding.js` 를 조용한 초록으로 만들었다** —
 * 코드가 지워지면 잡을 대상이 사라져서 「그런 자리 없음」이 된다. 지우기가 틀리는 방향이
 * 늘 시끄러운 쪽이라는 애초 주장이 그 검사에서는 **반증됐다.**
 *
 * 그래서 두 가지를 한다.
 *
 *   ① **못 믿을 상태를 스스로 신고한다** — `codeOnlyChecked` 가 `suspicious` 를 함께
 *      돌려준다. 판정 근거는 **문법상 불가능한 것**이지 취향이 아니다:
 *      정규식 리터럴과 홑/겹따옴표 문자열은 **줄바꿈을 품을 수 없다.** 지우는 중에 그런
 *      것이 보이면 그건 이 스트리퍼가 갈피를 잃었다는 뜻이다. 부르는 검사는 그때
 *      **초록으로 넘어가지 말고 빨갛게** 내야 한다.
 *   ② **피해를 한 줄로 가둔다** — 그 자리에서 코드 모드로 되돌아간다. 잘못 열린 문자열·
 *      정규식이 파일 뒤쪽을 통째로 삼키지 못한다.
 *
 * 휴리스틱 자체도 2026-09-11 검증이 찾은 만큼은 고쳤다 — 앞 토큰을 **지운 사본**에서
 * 보고(주석 뒤 나눗셈), 따옴표·`++`·`--` 뒤는 값으로, `.in`·`.of` 같은 속성 이름은
 * 키워드로 안 세고, `export default /re/` 의 `default` 를 키워드에 넣었다.
 */

/** 이 낱말들 **뒤**의 `/` 는 나눗셈이 아니라 정규식이다 (값이 아니라 연산자 자리라서). */
const RE_KEYWORDS = new Set(['return', 'typeof', 'instanceof', 'in', 'of', 'new', 'delete',
  'void', 'throw', 'case', 'do', 'else', 'yield', 'await', 'default']);

/**
 * @param {string} s 자바스크립트 소스
 * @returns {{code: string, suspicious: Array<{kind: string, line: number}>}}
 *   `suspicious` 가 비어 있지 않으면 **이 사본을 판정 근거로 쓰면 안 된다.**
 */
export function codeOnlyChecked(s) {
  // `[...s]` 가 아니라 `split('')` 이다 — 앞은 **코드포인트**로 쪼개고 `s[i]` 는 **UTF-16
  // 단위**라, 이모지가 한 개라도 있으면(`documents.js` 에는 `📎` 가 있다) 그 뒤로 사본의
  // 자리가 통째로 어긋난다 (2026-09-11 실측).
  const out = s.split('');
  const suspicious = [];
  let mode = 'code';   // code | line | block | sq | dq | tpl | re
  let inClass = false; // 정규식의 [...] 안인가 — 그 안의 `/` 는 끝이 아니다
  // 템플릿 `${…}` 안은 **문자열이 아니라 코드**다. 지워 버리면 거기서 부른 이름을 검사가
  // 조용히 못 본다. 각 칸은 그 보간 구역 안에서 열린 `{` 의 깊이다.
  const interp = [];
  const blank = (i) => { if (i < s.length && s[i] !== '\n') out[i] = ' '; };

  /** 바로 앞의 뜻 있는 토큰이 「값」인가 — 값 뒤의 `/` 는 나눗셈이다.
   *
   * **원본이 아니라 지운 사본(`out`)에서 뒤를 본다.** 앞선 주석은 이미 공백이 되어 있어
   * 공백 건너뛰기만으로 함께 넘어간다 — 나눗셈 앞에 블록 주석이 끼면(`a` 주석 `/ 2`)
   * 앞 토큰이 주석 끝 글자로 보여 정규식으로 잘못 읽히던 자리다. */
  const afterValue = (i) => {
    let j = i - 1;
    while (j >= 0 && /\s/.test(out[j])) j -= 1;
    if (j < 0) return false;
    const c = out[j];
    if (c === ')' || c === ']') return true;
    // `}` 뒤 — 객체 리터럴·블록 끝. 나눗셈 쪽이 훨씬 흔하다 (2026-09-11 2차 검증)
    if (c === '}') return true;
    // 소수점으로 끝난 수 (`1. / 2`) — 값이다
    if (c === '.' && /\d/.test(out[j - 1] || '')) return true;
    // 문자열·템플릿이 끝난 자리 — 값이다 (`'4' / 2`)
    if (c === "'" || c === '"' || c === '`') return true;
    // `i++ / 2` · `i-- / 2` — 뒤따르는 `/` 는 나눗셈이다
    if ((c === '+' || c === '-') && out[j - 1] === c) return true;
    if (!/[\w$]/.test(c)) return false;
    let k = j;
    while (k >= 0 && /[\w$]/.test(out[k])) k -= 1;
    // `o.in / 2` — 점 뒤의 키워드는 **속성 이름**이지 키워드가 아니다
    if (out[k] === '.') return true;
    return !RE_KEYWORDS.has(out.slice(k + 1, j + 1).join(''));
  };

  /** 문법상 불가능한 상태 — 스트리퍼가 갈피를 잃었다. 신고하고 한 줄로 가둔다.
   * **자리는 줄 번호가 아니라 글자 위치로 담는다** — 줄 번호를 여기서 세면 루프 맨 위의
   * 증가와 어긋나 1 크게 나온다(2026-09-11 2차 검증이 짚었다). 줄 번호는 끝에서 만든다. */
  const lost = (kind, at) => {
    suspicious.push({ kind, index: at });
    mode = 'code';
    inClass = false;
  };

  for (let i = 0; i < s.length; i += 1) {
    const c = s[i]; const d = s[i + 1];
    if (mode === 'code') {
      if (c === '/' && d === '/') { mode = 'line'; blank(i); blank(i + 1); i += 1; } else if (c === '/' && d === '*') { mode = 'block'; blank(i); blank(i + 1); i += 1; } else if (c === "'") mode = 'sq';
      else if (c === '"') mode = 'dq';
      else if (c === '`') mode = 'tpl';
      else if (c === '/' && !afterValue(i)) { mode = 're'; inClass = false; }
      else if (interp.length && c === '{') interp[interp.length - 1] += 1;
      else if (interp.length && c === '}') {
        if (interp[interp.length - 1] === 0) { interp.pop(); mode = 'tpl'; } else interp[interp.length - 1] -= 1;
      }
      continue;
    }
    if (mode === 'line') { if (c === '\n') mode = 'code'; else blank(i); continue; }
    if (mode === 'block') {
      blank(i);
      if (c === '*' && d === '/') { blank(i + 1); i += 1; mode = 'code'; }
      continue;
    }
    // 여기부터는 문자열·정규식 속 — 경계 문자만 남기고 지운다.
    // `\` 다음 글자는 무엇이든 속이다(줄바꿈 이어붙임 포함).
    if (c === '\\') { blank(i); blank(i + 1); i += 1; continue; }
    if (mode === 're') {
      // **정규식 리터럴은 줄바꿈을 품을 수 없다** — 여기 닿았으면 이 `/` 는 나눗셈이었다.
      if (c === '\n') { lost('정규식으로 잘못 읽은 나눗셈', i); continue; }
      if (c === '[') inClass = true;
      else if (c === ']') inClass = false;
      else if (c === '/' && !inClass) { mode = 'code'; continue; }
      blank(i);
      continue;
    }
    if (mode === 'tpl' && c === '$' && d === '{') { interp.push(0); mode = 'code'; i += 1; continue; }
    if ((mode === 'sq' || mode === 'dq') && c === '\n') {
      // **홑/겹따옴표 문자열도 줄바꿈을 품을 수 없다** (이어붙임은 위 `\` 가지가 먹는다).
      lost('문자열로 잘못 읽은 따옴표', i);
      continue;
    }
    if ((mode === 'sq' && c === "'") || (mode === 'dq' && c === '"') || (mode === 'tpl' && c === '`')) { mode = 'code'; continue; }
    blank(i);
  }
  if (mode !== 'code') suspicious.push({ kind: `파일이 ${mode} 상태로 끝남`, index: s.length - 1 });
  const code = out.join('');
  // 줄 번호는 **끝에서** 만든다 — 세는 자리와 신고하는 자리가 갈리면 어긋난다.
  for (const x of suspicious) { x.line = s.slice(0, Math.max(0, x.index)).split('\n').length; delete x.index; }
  return { code, suspicious };
}

/** 사본만 필요할 때. **판정 근거로 쓰는 검사는 `codeOnlyChecked` 를 쓰고
 * `suspicious` 를 반드시 봐야 한다** — 안 보면 지워진 자리를 「없음」으로 읽는다. */
export function codeOnly(s) {
  return codeOnlyChecked(s).code;
}
