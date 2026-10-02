import { createUsageCollector } from '../llm/usage.js';
/**
 * 상단 요약 대조 — 원문이 쌓이는 동안 요약이 조용히 낡는 것을 잡는다.
 *
 * **찾기만 하고 고치지 않는다.** 요약을 잘못 고치면 봇이 그 숫자를 근거로 답한다.
 * 어느 채널의 요약표 금액이 원문에서 바뀐 것을 **두 달 뒤에야 손으로** 발견한
 * 전례가 있어, 이 판정은 무인에 맡기지 않는다 (그 실물은 자료 저장소
 * `사고기록.md` 의 「상단 요약 대조가 무인이면 안 되는 이유」 절).
 */
import fs from 'node:fs';
import path from 'node:path';
import { CHANNELS_DIR } from '../config.js';
import { compareSummary } from '../claude.js';

const MONTH_RE = /^##\s+\d{4}-\d{2}\s*$/;

/**
 * 채널 md 첫머리의 요약을 뽑는다 — 사람이 쓴 자리 두 곳.
 *
 *   ① `>` 메타 블록 (봇 시스템 프롬프트에 매 답변마다 실리는 채널 색인)
 *   ② 첫 `---` 다음부터 첫 `## YYYY-MM` 전까지의 요약 섹션 (큰 채널에만 있다)
 *
 * **줄끝은 여기서 LF 로 맞춘다.** git 은 인덱스에 LF 로 넣고 작업 트리에는 설정대로
 * 꺼내므로(윈도우는 `core.autocrlf=true` 로 CRLF, VM 은 LF), 이 값을 그대로 세면
 * **같은 내용인데 기계마다 해시가 갈린다.** 그러면 「빼」가 조용히 무효가 된다 —
 * 이 PC 에서 정한 뺌을 VM 이 「요약이 고쳐졌다」로 읽고 매일 아침 다시 센다
 * (2026-09-07 실물: 어느 채널의 파생값 건이 이 PC 에서 `e621f03c34005046`, LF 로는
 * `6db5a2d6af075262` 였다). 커밋 관문은 같은 함정을 이미 밟고 고쳤다 —
 * `archive-inbox/scripts/review_work.py` 의 `_digest`.
 *
 * 뽑아 낸 값은 해시 말고도 모델에게 보내는 요약 원문·봇 가시성 판정에 함께 쓰이므로,
 * 세는 자리가 아니라 **읽는 자리**에서 맞춘다. 세는 자리마다 맞추면 한 곳을 빠뜨린다.
 *
 * @returns {{meta:string, section:string, text:string}}
 */
export function extractSummary(mdPath) {
  const lines = fs.readFileSync(mdPath, 'utf8').replace(/\r\n/g, '\n').split('\n');

  /* **메타 블록은 상단에서만 찾는다** (2026-08-13 리뷰). 전에는 첫 `>` 를 파일 끝까지
   * 찾았는데, 스레드 답글이 `>` 로 시작하므로 상단 메타가 없거나 깨진 파일에서는
   * **파일 중간의 답글을 메타로 집어 올렸다.** 그 값은 요약 대조의 기준이 되고
   * (`summaryHash`), 「뺌」을 언제 풀지까지 그 해시가 정한다. 에러는 안 난다.
   * `archive.js` 의 `metaBlock` 이 같은 이유로 같은 상한을 쓴다 — 한쪽만 고치면 갈린다. */
  const bodyStart = (l) => MONTH_RE.test(l) || /^\*\*\d{4}-/.test(l) || l.trim() === '---';
  const SCAN_LIMIT = 40;

  const meta = [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!meta.length && (i >= SCAN_LIMIT || bodyStart(line))) break;
    if (line.startsWith('>')) meta.push(line);
    else if (meta.length) break;
  }

  // 메타 블록을 닫는 '---' 를 찾고, 거기서부터 첫 월 헤딩 전까지가 요약 섹션이다.
  //
  // **메타를 못 찾았으면 요약 절도 찾지 않는다.** 이 루프도 `>` 를 파일 끝까지 찾으므로,
  // 묶지 않으면 파일 중간의 스레드 인용 뒤에 오는 `---` 를 요약 절의 시작으로 읽는다 —
  // 위 메타 상한과 같은 구멍이다. 상단 메타가 없는 파일은 계약 밖이라 짐작하지 않는다.
  let start = -1;
  let seenMeta = false;
  for (let i = 0; meta.length && i < lines.length; i += 1) {
    if (lines[i].startsWith('>')) seenMeta = true;
    if (seenMeta && lines[i].trim() === '---') { start = i; break; }
  }

  const section = [];
  if (start >= 0) {
    for (let i = start + 1; i < lines.length; i += 1) {
      if (MONTH_RE.test(lines[i])) break;
      section.push(lines[i]);
    }
  }

  const metaText = meta.join('\n').trim();
  const sectionText = section.join('\n').trim();
  return {
    meta: metaText,
    section: sectionText,
    text: [metaText, sectionText].filter(Boolean).join('\n\n'),
  };
}

/**
 * 비교용 정규화 — 공백·따옴표·강조 표시를 지운다 (모델이 인용하며 다듬는 폭만큼만).
 *
 * **파이썬 쪽 `archive-inbox/scripts/review_work.py` 의 `_fold` 와 같아야 한다.**
 * 이쪽은 관문(`verifyEvidence`)이고 저쪽은 그 항목의 원문을 사람에게 찾아 주는 화면이다.
 * 이쪽이 더 너그러우면 **관문은 통과시켰는데 화면은 「원문에서 찾지 못했습니다」**가 되고,
 * 스킬 문서가 그 문구를 「대개 빼」로 읽으라고 해서 멀쩡한 항목이 버려진다
 * (2026-08-13 실측: 백틱 440 · 별표 141 · 따옴표 25건).
 * 갈렸는지는 `scripts/check-shared-rules.js` 가 같은 입력을 양쪽에 먹여 본다 —
 * 그래서 내보낸다. 부르는 곳은 이 파일 안과 그 검사들뿐이다.
 *
 * **글머리표 `•`·`◦`·`∙` 도 지운다** (2026-08-20). 슬랙에서 항목을 나열하면 `• ` 로
 * 시작하는 줄이 되는데, 모델이 앞 줄과 이어 인용하면 그 표시가 빠진 문장이 된다 —
 * 그러면 관문이 **원문에 있는 근거를 「원문에 없다」고 버린다.** `#사업장사`
 * 2026-08-20 07:00 회차에서 실제로 밀렸다(`<회의와 별도 내용>` 다음 줄이 `• 레지던스
 * 위탁운영계약에…`). 인용이 하나뿐이라 가려 줄 것이 없었다.
 *
 * **`·`(U+00B7)는 안 지운다.** 아카이브 전량 실측으로 `•` 는 305회 중 261회(86%)가
 * 줄머리라 사실상 글머리표뿐인데, `·` 는 2,295회 중 줄머리가 9회(0.4%)뿐인 **문장
 * 구분자**다. 지우면 서로 다른 문장이 같은 값으로 접혀 관문이 엉뚱한 근거를 통과시킨다.
 * `-`(5,268회)도 같은 이유로 안 지운다.
 *
 * **백슬래시 `\` 도 지운다** (2026-09-11, 할 일 185 의 다섯째 원인). 모델은 인용을
 * `\"확인\"` 처럼 이스케이프해 적는데 그 백슬래시는 원문에 없어서, 근거가 원문에
 * 멀쩡히 있어도 접은 값이 갈려 **관문이 항목을 버린다.**
 *
 * 아카이브 전량 실측 (2026-09-11): 5,895회가 전부 `documents/` 에 있고 슬랙 채널
 * md 에는 0회다. 5,779회(98.0%)는 `\*`·`` \` ``·`\~`·`\_` 처럼 **접기가 이미 지우는
 * 글자** 앞에 붙은 것이라 백슬래시만 찌꺼기로 남아 **지금도** 값을 가르고 있었다.
 *
 * **공짜가 아니다.** 앞뒤가 둘 다 접기에서 살아남는 `A\B` 모양이 **95회(1.6%)** 있고,
 * 그 자리의 백슬래시는 지우면 뜻이 사라진다. 내역은 **원화 기호 75회** ·
 * 표 구분자 `\|` 6회 · 수식 깨짐(`$\frac}$`) 9회 · 그 밖 5회다. 원화 기호는 한글
 * 코드페이지가 `₩` 을 U+005C 로 저장해서 생긴 것으로, 감정평가서가
 * `육백오십억이천삼백만원정 (\65,023,000,000.-)` 처럼 적고 있다(원문 코드포인트로
 * 확인). **그래서 `₩65,023,000,000` 과 `65,023,000,000` 이 이제 같은 값으로 접힌다.**
 * 윈도우 경로(`C:\`)는 0회다 — 그건 `A\B` 의 한 종류가 없다는 뜻일 뿐 위험이 없다는
 * 뜻이 아니다. 98% 를 고치려고 1.6% 의 통화 기호 구별을 내준 맞바꿈이다.
 */
export function fold(s) {
  return String(s || '')
    .replace(/[\s`*_~•◦∙\\]/g, '')
    .replace(/[""'']/g, '')
    .toLowerCase();
}

/**
 * 모델이 **원문에 없는 글자로 이어 붙인 자리.** 인용을 여기서 쪼갠다 (`needles` 참조).
 *
 * 둘이다 — ① 가운데를 건너뛴 표시 `...`·`…` ② 여러 줄을 한 인용으로 이을 때 줄바꿈과
 * 글머리표 자리에 들어가는 ` / `. **둘 다 원문에는 없다.**
 *
 * ②를 2026-09-10 에 더했다. 슬랙에서 항목을 나열하면 줄마다 `· ` 로 시작하는데, 모델이 두
 * 줄을 이어 인용하면 그 자리가 ` / ` 가 된다. 원문의 그 자리는 `·`(U+00B7)이고 그것은
 * **문장 구분자라 접기에서 일부러 안 지운다**(`fold` 참조) — 그래서 접기만으로는 영영
 * 안 걸리고 항목이 `dropped` 로 밀린다. 화면에는 「근거 못 찾아 뺀 후보」로 뜨는데 SKILL 이
 * 그 문구를 「대개 빼」로 읽으라고 안내하므로 **멀쩡한 근거가 버려진다.** `#사업장가`
 * 회차에서 실제로 그랬고 사람이 Grep 해서야 알았다. 다섯째 원인(백슬래시 따옴표)과
 * 마찬가지로 **파이썬·JS 가 함께 틀려서 `check-shared-rules.js` 로는 안 잡혔다.**
 *
 * **`/` 는 앞뒤가 공백일 때만 이음매로 본다.** 붙여 쓴 것은 날짜(`10/23`)나 `채권/채무`
 * 처럼 **원문의 글자**라, 쪼개면 조각이 8자 밑으로 부서져 되레 못 찾는다.
 *
 * **파이썬 `review_work.py` 의 `JOIN_RE` 와 같아야 한다.**
 */
const JOIN_RE = /\.{3,}|…+|\s+\/\s+/;

/**
 * **실제로 찾을 문자열**의 최소 길이. 위 8자는 접기 **전**에 세는데 찾는 것은 접은
 * 뒤라, 그 사이가 벌어진다 — `**공문을** 확인` 은 8자 검사를 통과하고 5자짜리로 찾는다.
 * 아카이브 전량 실측으로 3~5자는 **같은 채널의 남의 메시지**에도 22~33% 있어서, 근거와
 * 무관한 대화를 원문이라며 보여줄 수 있다(8자는 17.2%, 20자는 6.6%).
 *
 * **6인 것은 `약정 체결 업무` 가 접으면 6자이기 때문이다.** 그보다 올리면 띄어쓴 짧은
 * 인용이 버려져 2026-08-13 의 퇴행 3건이 되돌아온다. 실측한 조각 19개는 전부 접은 뒤
 * 10자 이상이라 지금 데이터에서 사라지는 것은 없다 — 6은 표본이 아니라 **그 퇴행 사례를
 * 안 건드리는 선**으로 잡은 값이다.
 */
const MIN_FOLDED = 6;

/**
 * 인용 하나에서 원문에 있을 법한 조각들을 뽑는다.
 *
 * **파이썬 쪽 `review_work.py` 의 `_needles` 와 같아야 한다** — `fold` 와 같은 이유이고,
 * 갈리면 `scripts/check-shared-rules.js` 가 잡는다.
 *
 * **모델은 인용 가운데를 `...`·`…` 로 건너뛴다.** 그 표시는 「여기서 중간을 뺐다」는
 * 뜻이라 원문에 없다. 통짜로 찾으면 원문 어디에도 없어 항목이 `dropped` 로 밀린다.
 * 화면은 2026-08-13(`4f2e9bf`)부터 쪼개어 찾는데 관문은 그대로여서 **관문만 못 찾는**
 * 상태였다 — 실제 회차의 인용으로 재현했다(사업장가 8/12, 모델이 「소송을 해야 하는데
 * 이 또한 쉽지 않다고 설명하니,」를 `...` 로 건너뛴 자리). 그때는 근거에 인용이 셋이라
 * 생략 없는 앞엣것이 걸려 가려졌고, **인용이 하나뿐이면 그대로 밀린다.**
 *
 * **조각이 짧으면 버린다.** 흔한 낱말이라 엉뚱한 메시지가 걸리는데, 「못 찾음」보다
 * 나쁜 것은 틀린 원문을 원문이라고 보여주는 것이다. 기준 8자는 `quotesIn` 이 인용으로
 * 인정하는 길이와 같고 **접기 전에 센다** — 접은 뒤로 재면 띄어쓴 짧은 인용이 통째로
 * 버려진다(`약정 체결 업무` 는 8자인데 공백을 지우면 6자다).
 *
 * 생략이 없는 인용에서는 **이 함수가 예전 판정과 완전히 같다** — 조각이 하나뿐이라
 * 앞 30자로 찾는 것이 되고, 통짜로 걸리는 인용은 앞 30자로도 걸리기 때문이다.
 */
export function needles(quote) {
  return String(quote || '')
    .split(JOIN_RE)
    .filter((p) => p.length >= 8)
    .map((p) => fold(p).slice(0, 30))
    .filter((n) => n.length >= MIN_FOLDED);
}

/**
 * 근거 문자열에서 따옴표로 묶인 인용을 뽑는다.
 *
 * **이것도 두 언어에 하나씩 있다** — 저쪽은 `review_work.py` 의 `QUOTE_RE` 이고 같은
 * `evidence` 에서 같은 인용을 뽑아야 한다. 이쪽은 **관문**이라 여기서 하나도 못 뽑으면 그
 * 항목이 `근거에 원문 인용이 없음` 으로 통째로 버려진다(아래 `verifyEvidence`). 저쪽은 그
 * 항목의 원문을 사람에게 찾아 주는 **화면**이다. 관문이 더 엄격하면 화면이 찾아낼 수 있는
 * 근거를 관문이 먼저 버린다 — 위 `fold`·`needles` 와 같은 사고이고, 지키는 것도 같은
 * 검사(`scripts/check-shared-rules.js` 의 절 ①-d)다.
 */
export function quotesIn(evidence) {
  const out = [];
  /* 문자군을 코드포인트로 적는다. 둥근 따옴표는 눈으로 곧은 것과 구별이 안 되고, 실제로
   * 2026-08-31 까지 여기 `["""']` 이라고 적혀 있었다 — 큰따옴표 셋이 전부 U+0022 라
   * 넓어 보이는데 곧은 것만 봤다. 순서·개수는 `review_work.py` 의 `_Q` 와 같다. */
  const Q = '["“”‘’\']';
  const re = new RegExp(`${Q}([^${Q.slice(1, -1)}]{8,})${Q}`, 'g');
  let m;
  while ((m = re.exec(String(evidence || ''))) !== null) out.push(m[1]);
  return out;
}

/**
 * 근거가 **실제 원문에 있는지** 기계로 확인한다.
 *
 * 모델에게 "근거를 달라"고 시키는 것과 근거가 진짜인지는 다른 문제다. 인용이 원문에 없으면
 * 그 항목은 내보내지 않는다 — 사람은 DM 을 보고 요약을 고치고, 요약은 봇의 답변 근거가 된다.
 * 확인 못 한 것을 조용히 버리지 않고 몇 건을 왜 뺐는지 함께 돌려준다.
 */
export function verifyEvidence(findings, transcript) {
  const hay = fold(transcript);
  const kept = [];
  const dropped = [];
  let multiSeen = 0;
  let shadowSeen = 0;

  for (const f of findings) {
    const quotes = quotesIn(f.evidence);
    if (!quotes.length) {
      dropped.push({ ...f, why: '근거에 원문 인용이 없음' });
      continue;
    }
    // 인용 하나라도 원문에 있으면 통과. 모델이 앞뒤를 다듬는 경우가 있어 앞 30자로 보고,
    // 가운데를 건너뛴 인용은 그 표시에서 쪼개어 조각마다 본다 (`needles`).
    const perQuote = quotes.map((q) => {
      const ns = needles(q);
      return { total: ns.length, found: ns.filter((n) => hay.includes(n)).length };
    });
    const ok = perQuote.some((p) => p.found > 0);
    /* 그림자 계측 — **판정은 안 바꾼다** (할 일 185-곁, 2026-09-11).
     *
     * 위 관문은 인용 안의 조각이 **하나만 걸려도** 통과시킨다. 그래서 앞 절반은 실존하고
     * 뒤 절반은 지어낸 인용이 통과할 수 있다. 조이려면(`every`) 대가를 알아야 하는데,
     * 로컬에서 모은 코퍼스는 **다조각 인용이 15건뿐**이라 정하기에 모자랐다. 그래서
     * 규칙은 그대로 두고 **「some 이면 통과인데 every 면 탈락」일 인용의 건수만** 센다.
     * 며칠 쌓인 값을 보고 규칙을 정한다.
     *
     * 화면 쪽은 이것과 무관하게 이미 못 찾은 조각을 밝힌다
     * (`archive-inbox/scripts/review_work.py` 의 `context_for`). */
    multiSeen += perQuote.filter((p) => p.total > 1).length;
    shadowSeen += perQuote.filter((p) => p.found > 0 && p.found < p.total).length;
    if (ok) kept.push(f);
    else dropped.push({ ...f, why: `인용이 원문에 없음: "${quotes[0].slice(0, 40)}…"` });
  }

  /* **분모를 함께 찍는다 — 그리고 0 일 때도 찍는다** (2026-09-20).
   *
   * 옛 판은 `if (shadowLoss)` 라 부분 일치가 0 이면 **아무것도 안 남겼다.** 그래서 며칠 뒤
   * 로그를 뒤진 사람이 보는 「0건」이 ⓐ 다조각 인용이 있었는데 전부 온전히 맞았다 인지
   * ⓑ **다조각 인용 자체가 0건이라 아직 안 재졌다** 인지 갈리지 않았다. 실제로 2026-09-20 에
   * VM journald 를 전 기간(08-02~) 뒤지니 이 줄이 **0건**이었는데, 회차는 3일에 6번 돌았고
   * 계측 코드도 배포판에 실재해서 **무슨 뜻인지 판정할 수가 없었다.**
   *
   * 정하려는 것(`some → every` 로 조일지)은 **분모를 알아야** 정해진다 — 조이는 대가가
   * 「부분 일치 N건」인데, N 이 작은 것이 안전해서인지 **모수가 작아서인지**를 못 가르면
   * 그 값으로 관문을 조이는 것이 곧 위 주석이 경계한 「0 을 『대가 없음』으로 읽는」 일이다.
   *
   * 그래서 **볼 항목이 하나라도 있으면 한 줄을 반드시 낸다.** 부분 일치가 0 이어도 낸다 —
   * 그것이 이 수정의 전부다. 채널마다 한 번 불리므로(`checkSummaries` 의 루프) 회차당
   * 채널 수만큼 나온다.
   *
   * **침묵이 한 가지 뜻은 아니다 — 여기서 과장하지 않는다.** 줄이 없는 것은
   * ⑴ 이 코드가 안 돌았다 **⑵ 그 회차에 볼 항목이 0건이었다** 둘 중 하나다. ⑵ 는 고장이
   * 아니라 **정상 경로**다 — `checkSummaries` 는 `compareSummary` 가 돌려준 것을 그대로
   * 넘기는데, 모델이 어긋남을 못 찾으면 `[]` 이고 거절이어도 `[]` 다
   * (`src/llm/summary-check.js` 의 `refusal` 갈래와 마지막 `return`). 요약이 없는 채널은
   * 아예 건너뛴다. **그러니 침묵 하나만 보고 「계측이 죽었다」로 읽으면 안 된다** — 그것은
   * 이 수정이 고치려던 오독과 같은 종류의 오독이다. 분모를 모으는 데는 지장이 없다
   * (볼 항목 0건 = 분모 기여 0). 판정하려면 ingest 로그의 회차 수와 함께 봐라. */
  if (findings.length) {
    console.log(`  [185-곁 계측] 항목 ${findings.length}건 · 다조각 인용 ${multiSeen}건 · 그중 부분 일치 ${shadowSeen}건`);
  }

  return { kept, dropped };
}

/**
 * 이번에 들어온 대화가 요약과 어긋나는지 채널별로 본다.
 *
 * **새 메시지가 들어온 채널만** 본다 — 나머지는 파일을 열지 않는다.
 * 요약이 아예 없는 채널은 건너뛴다(없는 요약을 새로 만들지 않는다).
 *
 * @param {Array<{channel:string, transcript:string}>} touched
 */
export async function checkSummaries(touched, { onUsage } = {}) {
  const ledger = createUsageCollector();
  const findings = [];
  const failed = [];
  const dropped = [];

  for (const t of touched) {
    const mdPath = path.join(CHANNELS_DIR, `${t.file || t.channel}.md`);
    if (!fs.existsSync(mdPath)) continue;

    const summary = extractSummary(mdPath);
    if (!summary.text) continue;

    try {
      const found = await compareSummary({
        channel: t.channel,
        summary: summary.text,
        transcript: t.transcript,
        onUsage: (accounting) => { ledger.merge(accounting); onUsage?.(ledger.snapshot()); },
      });
      // 근거가 원문에 실제로 있는 것만 내보낸다.
      const v = verifyEvidence(found, t.transcript);
      for (const f of v.kept) findings.push({ channel: t.channel, ...f });
      for (const f of v.dropped) dropped.push({ channel: t.channel, ...f });
    } catch (err) {
      // 파싱 실패·API 실패를 "어긋난 것 없음" 으로 흘려보내지 않는다.
      failed.push(`#${t.channel}: ${err.message}`);
    }
  }

  return { findings, failed, dropped, accounting: ledger.snapshot() };
}
