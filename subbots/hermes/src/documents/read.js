/** Codex R2f: lazy document reading and digest attachment selection.
 * Keep byte-identical in Hermes and Clio. Limits are supplied by the facade.
 */
export function createDocumentRead({
  fs, path, DOCS_DIR, DOC_READ_MAX_CHARS, DOC_HIT_MAX_CHARS, SECTION_OUTLINE_MIN_COVER, DOC_PREAMBLE_MARK, DIGEST_DOC_MAX_CHARS, DIGEST_DOC_PER_DOC_CHARS, DIGEST_DOC_MIN_CHARS, hasDocuments, resolveProjectFor, resolveDocumentFor, canSeeDoc, BLOCKED_NOTE, readCached, redactPrivateMentions, preambleOf, splitMessages, SHEET_RE, sectionsOf, fold, loadDocument, maskProject, clip
}) {
  /**
   * 절 목차를 내밀 만한 문서인가. **왜 안 되는지까지** 돌려준다.
   *
   * **`outlineOf` 의 절 갈래와 `readDocument` 의 잘림 안내가 이 함수 하나를 본다.**
   * 예전에는 안내 쪽이 `sectionsOf` 만 보고 관문을 안 봐서, **관문이 일부러 목차를 끈
   * 문서**에 「section 으로 한 절만 여세요」가 나갔다. 그 문서는 목차가 안 와서 봇이 절
   * 이름을 모르고, 1번을 찍으면 69자·14자를 받는다 — 45만 자 문서에서 그것을 받고
   * 「내용이 없다」로 읽는다 (전체 검증 실측, 2026-09-11).
   *
   * **막힌 이유를 안 돌려줬을 때도 같은 모양의 사고가 났다** — 잘림 안내가 "막혔다/안
   * 막혔다"만 보고, 막힌 이유는 늘 "가장 큰 절이 상한을 넘어"라고 **단정**했다. 덮음률
   * 미달로 막힌 문서(머리말이 길고 절 자체는 작은 문서)에도 그 문장이 나가 거짓 안내가
   * 됐다 — 직전 사고(section 을 잘못 권함)의 거울상이다 (전체 검증 2회차, 2026-09-11).
   * 그래서 두 이유를 판정 하나로 합치지 않고 **호출자가 kind 를 읽게** 한다.
   *
   * 가장 큰 절이 `maxPiece` 안에 들고, 절이 문서의 `SECTION_OUTLINE_MIN_COVER` 이상을
   * 덮어야 목차를 낸다 — 왜 이 둘인지는 아래 `outlineOf` 절 갈래 블록 주석이 원본이다.
   *
   * @param {string} text 문서 md 전문
   * @param {number} maxPiece 가장 큰 절이 이 값을 넘으면 막힌다
   * @returns {{kind:'none'} | {kind:'blocked-size', biggest:number}
   *   | {kind:'blocked-cover', covered:number}
   *   | {kind:'ok', sections: Array<{n:number, name:string, start:number, end:number}>}}
   */
  function sectionOutline(text, maxPiece) {
    const sections = sectionsOf(text);
    if (!sections) return { kind: 'none' };
    const biggest = Math.max(...sections.map((s) => s.end - s.start));
    if (biggest > maxPiece) return { kind: 'blocked-size', biggest };
    // 첫 절보다 **위**에 있는 구간은 어느 절에도 안 든다. 절이 문서를 얼마나 덮는가.
    const covered = (text.length - sections[0].start) / text.length;
    if (covered < SECTION_OUTLINE_MIN_COVER) return { kind: 'blocked-cover', covered };
    return { kind: 'ok', sections };
  }

  /**
   * 큰 문서를 목차로 낼 수 있나. 낼 수 있으면 조각 목록, 없으면 null.
   *
   * **기준은 "조각이 있나"가 아니라 "봇이 그 조각을 고를 인자가 있나" 다.**
   * read_document 로 **최상위 조각을 고르는** 인자는 sheet·month·section 뿐이라(`week` 는
   * month 안의 축이라 혼자 못 쓴다 — 아래 `weekOutline`), 회차 블록으로만 나뉜 문서는
   * 조각이 46개라도 그중 하나를 지목할 방법이 없다 — 목차를 줘 봐야 고를 수
   * 없으니 null 을 낸다(그러면 앞부분 + 검색 안내로 간다).
   *
   * 시트 판정은 SHEET_RE 하나뿐이다. 여기 사본을 두면 check-excel-sheets.js 가
   * 지키는 계약과 조용히 갈라진다 (SHEET_RE 주석의 전례).
   *
   * 월 헤딩은 **정본 `^##\s+`(관대한 쪽)** 으로 센다 (WHK 지시 2026-09-03, `archive.js` 의
   * `MONTH_HEADING`·`extractMonthSection` 과 같은 규칙) — 공백 한 칸으로 고정하면
   * `##  2026-08`(두 칸) 같은 헤딩을 놓쳐 월이 하나 적게 세여, 월이 실제로는 2개 이상인데
   * 여기서는 1개로 보여 목차 자체가 안 뜬다.
   *
   * 갈래는 sheet ≥2 → month ≥2 → **section**(아래) → null 순으로 가른다. section 갈래는
   * 이 둘과 달리 **상한(maxOutlinePiece)에 걸린다** — 왜인지는 아래 절 갈래 블록 주석 참조.
   *
   * @param {string} text 문서 md 전문
   * @param {number} [maxOutlinePiece] 절 갈래에서 "가장 큰 절"이 이 값을 넘으면 목차를 안 낸다.
   *   기본값은 읽기 상한(DOC_READ_MAX_CHARS) — `readDocument` 가 실제로 쓰는 상한과 같아야
   *   판정이 그 호출의 결과와 어긋나지 않는다.
   */
  function outlineOf(text, maxOutlinePiece = DOC_READ_MAX_CHARS) {
    const named = splitMessages(text)
      .map((b) => ({ name: (b.text.match(SHEET_RE) || [])[3], chars: b.text.length }))
      .filter((x) => x.name);
    if (named.length >= 2) return { kind: 'sheet', pieces: named };

    const heads = [...text.matchAll(/^##\s+(\d{4}-\d{2})\s*$/gm)];
    if (heads.length >= 2) {
      return {
        kind: 'month',
        pieces: heads.map((m, i) => ({
          name: m[1],
          chars: (i + 1 < heads.length ? heads[i + 1].index : text.length) - m.index,
        })),
      };
    }

    /* 갈래 셋 — sheet 도 month 도 없지만 **번호 붙은 절**로 나뉜 문서.
     * 여기 오기 전까지 이런 문서는 「앞부분 + 막다른 안내」로 갔다. 40만 자 종합보고에서
     * 봇이 뒤쪽을 영영 못 본 자리다 (2026-09-10). 절 인식 규칙은 sectionsOf 주석이 원본.
     *
     * **가장 큰 절이 읽기 상한 안에 들어올 때만 목차를 낸다.** 앞 두 갈래(sheet·month)에는
     * 없는 조건이고, 일부러 다르게 뒀다. sheet·month 는 문서가 실제로 그렇게 나뉜 것이라
     * 봇이 어차피 골라야 하지만, 절은 **비용을 줄이려는 최적화**다. 가장 큰 절이 여전히
     * 잘린다면 목차를 준 만큼 왕복만 한 번 더 쓰고 얻는 것이 없다.
     *
     * 실측(2026-09-10, 읽기 상한 넘는 문서 중 절이 잡히는 4건):
     *   · 44만 자 · 절 58개 · 최대 18,348자 → 켠다 (한 절이 통째로 열린다)
     *   · 40만 자 · 절 58개 · 최대 19,681자 → 켠다
     *   · 10만 자 · 절  4개 · 최대 93,746자 → **끈다** (이름만 절이고 내용이 한 덩어리)
     *   · 46만 자 · 절  5개 · 최대454,159자 → **끈다** (같은 모양)
     *
     * 끄더라도 **`section` 인자 자체는 그대로 산다** — 봇이 절 이름을 알고 지목하면 열린다.
     * 여기서 끄는 것은 「목차를 자동으로 내미는 것」뿐이다. */
    const gate = sectionOutline(text, maxOutlinePiece);
    if (gate.kind === 'ok') {
      return {
        kind: 'section',
        // 이름을 "N. 제목" 으로 낸다 — 봇이 목차에서 읽은 것을 그대로 section 인자로 쓴다.
        pieces: gate.sections.map((s) => ({ name: `${s.n}. ${s.name}`, chars: s.end - s.start })),
        // 첫 절 앞에 남는 양. renderOutline 이 이것을 한 줄로 밝힌다.
        before: gate.sections[0].start,
      };
    }

    return null;
  }

  /**
   * 월 조각을 **주 단위로** 다시 나눈다. 못 나누면 null.
   *
   * ── 왜 (2026-09-20) ──
   *
   * `month` 로 좁혀 불렀는데 **그 한 달이 여전히 상한을 넘는 문서**가 있다. 실측 2건이
   * 101,453자 · 96,785자였고, 지금까지는 앞 60,000자만 오고 뒤는 도구로 못 봤다 —
   * 잘림 안내가 정직하게 붙긴 하지만 봇이 그것을 안 읽고 앞부분만으로 답하면 **조용히
   * 부분만 보고 답한 것**이 된다. 갈래 셋 중 **주 단위 재분할**을 채택했다
   * (WHK 결정 2026-09-19). 「뒤에서부터 주기」는 md 가 최신순이라 역효과가 확정이었고,
   * 「지금처럼 두고 프롬프트로 당부」는 준수율 문제가 남는다.
   *
   * 주 경계는 **ISO 주(월요일 시작)** 다. 실측에서 이 축의 최대 조각이 34,542자 ·
   * 34,709자로 상한 초과가 0개였다.
   *
   * ── 안 나누는 때 ──
   *
   * · 회차가 하나뿐이거나, 날짜를 못 읽는 회차가 **하나라도** 있을 때. 부분만 나누면
   *   어느 주에도 안 드는 회차가 생기고, 그건 **조용히 못 닿는 자료**가 된다.
   * · 나눠 봐야 조각이 하나일 때. 고를 것이 하나면 목차는 왕복만 는다(month 갈래와 같은
   *   규칙). **주가 하나인 것만으로는 안 접는다** — 그 한 주가 상한을 넘으면 날짜로
   *   나뉘어 조각이 여럿이 되고, 거기서 접으면 그 달은 예전처럼 앞부분만 온다.
   *
   * 날짜는 `splitMessages` 가 주는 `date` 를 먼저 쓴다. 지금 두 저장소의 그 함수는
   * 블록마다 `date` 를 싣지만(바이트 동일), **머리줄에서 읽는 폴백을 남겨 둔다** —
   * 이 파일은 의존성을 주입받는 팩토리라 그 계약이 바뀌어도 조용히 틀리지 않게.
   *
   * @param {number} maxPiece 이 크기를 넘는 주는 날짜로 더 나눈다
   * @param {number} extra 조각을 열 때 앞에 얹히는 글자 수(월 헤딩). 목차에 적는 크기가
   *   실제로 오는 양과 어긋나지 않게 더해 둔다.
   */
  function weeksOf(text, maxPiece = DOC_READ_MAX_CHARS, extra = 0) {
    const blocks = splitMessages(text);
    if (blocks.length < 2) return null;
    const dated = blocks.map((b) => ({
      b,
      date: b.date || (b.text.match(/(\d{4}-\d{2}-\d{2})/) || [])[1] || null,
    }));
    if (dated.some((x) => !x.date)) return null;

    const groups = new Map();
    for (const x of dated) {
      const key = mondayOf(x.date);
      if (!key) return null;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(x);
    }

    const size = (xs) => xs.map((x) => x.b.text).join('\n\n').length;
    const piece = (name, key, xs) => ({ name, monday: key, blocks: xs.map((x) => x.b), chars: size(xs) + extra });

    /* **그 주가 또 상한을 넘으면 그 주만 날짜로 더 나눈다** (2026-09-20 실측에서 나왔다).
     * 주 단위로 나눈 40조각 중 38개는 상한 안에 들었는데, 회차가 유난히 촘촘한 문서
     * 하나에서 두 주가 여전히 넘었다 — 그대로 두면 그 자리만 예전처럼 앞부분만 온다.
     * **축은 그대로 `week` 다** — 이름이 날짜 하나(`2026-09-09`)가 될 뿐이라 고르는 법도
     * 같다. 하루가 또 넘으면 더 나눌 축이 없어 지금까지처럼 잘리고 안내가 붙는다. */
    const pieces = [];
    for (const [key, xs] of groups) {
      if (size(xs) + extra <= maxPiece) {
        pieces.push(piece(weekName(key), key, xs));
        continue;
      }
      const byDay = new Map();
      for (const x of xs) {
        if (!byDay.has(x.date)) byDay.set(x.date, []);
        byDay.get(x.date).push(x);
      }
      for (const [day, dxs] of byDay) pieces.push(piece(day, day, dxs));
    }
    return pieces.length >= 2 ? pieces : null;
  }

  /** `outlineOf` 와 같은 모양(`{kind, pieces}`)으로 감싼다 — renderOutline 이 그 모양만 읽는다. */
  function weekOutline(text, maxPiece, extra) {
    const weeks = weeksOf(text, maxPiece, extra);
    if (!weeks) return null;
    /* **어느 주에도 안 드는 구간을 밝힌다** — `splitMessages` 는 첫 회차 머리줄 앞을
     * 버리므로, 월 헤딩 아래에 사람이 쓴 글이 있으면 주 조각 어디에도 안 실린다.
     * 말해 주지 않으면 봇이 목차를 그 달 전부로 읽는다 (절 갈래의 `before` 와 같은 자리).
     * 실물에는 지금 그런 문서가 0건이지만, 사람이 손으로 쓰는 자리라 언젠가 생긴다. */
    const firstBlock = weeks[0].blocks[0];
    const at = firstBlock ? text.indexOf(firstBlock.text) : -1;
    const head = text.split('\n')[0];
    const before = at > 0 ? text.slice(0, at).replace(head, '').trim().length : 0;
    return {
      kind: 'week',
      pieces: weeks.map((w) => ({ name: w.name, chars: w.chars })),
      // 주 하나가 상한을 넘어 날짜로 더 나뉜 조각이 섞였나. 목차가 그 사실을 한 줄로 밝힌다.
      mixed: weeks.some((w) => !w.name.includes('~')),
      before,
    };
  }

  /** 그 날짜가 든 ISO 주(월요일)의 날짜. 못 읽으면 null. */
  function mondayOf(date) {
    const t = Date.parse(`${date}T00:00:00Z`);
    if (Number.isNaN(t)) return null;
    const d = new Date(t);
    // getUTCDay 는 일요일이 0 이다 — 월요일을 0 으로 옮겨 빼야 ISO 주가 된다.
    d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
    return d.toISOString().slice(0, 10);
  }

  /** `2026-08-03~08-09` — 앞자리가 그대로 week 인자가 된다. */
  function weekName(monday) {
    const end = new Date(Date.parse(`${monday}T00:00:00Z`));
    end.setUTCDate(end.getUTCDate() + 6);
    return `${monday}~${end.toISOString().slice(5, 10)}`;
  }

  /**
   * 목차 한 장. **다음 행동 두 줄이 반드시 붙는다** — 이것이 없으면 봇이 목차를
   * 「자료가 없다」로 읽고 오답한다(2026-08-06 0건 오답 사고와 같은 모양).
   * 조각마다 크기를 적는 것도 요점이다. 큰 시트를 부르기 **전에** 알아야
   * 검색으로 돌지, 부르고 나서 잘린 걸 보고 도는 것은 이미 돈을 쓴 뒤다.
   */
  function renderOutline(title, total, outline) {
    const n = (v) => v.toLocaleString('en-US');
    const LABELS = { sheet: '시트', month: '월', week: '주', section: '절' };
    const label = LABELS[outline.kind] ?? '절';
    const arg = outline.kind === 'section' ? 'section' : outline.kind;
    // 주 갈래만 **혼자서는 못 부른다** — `week` 는 그 달 안의 축이라 `month` 를 함께
    // 줘야 한다. 이 한 마디가 없으면 봇이 week 만 넣어 부르고 에러를 받는다.
    const together = outline.kind === 'week' ? ' (month 와 함께)' : '';
    // 절 갈래는 조각 이름이 이미 `"N. 제목"` 이라 순번을 안 붙인다 — 붙이면 `1. 1. …` 이
    // 되고, 결번이 있는 문서에서는 앞(순번)과 뒤(절 번호)가 갈려 봇이 어느 쪽을 인자로
    // 넣을지 알 수 없다 (실측: 58줄 중 42줄에서 순번 ≠ 절 번호, 2026-09-11).
    // sheet 는 순번이 그대로 인자라(`sheet: "2"`) 붙인 채로 둔다.
    const list = outline.pieces
      .map((p, i) => (outline.kind === 'section' ? `${p.name} (${n(p.chars)}자)` : `${i + 1}. ${p.name} (${n(p.chars)}자)`))
      .join('   ');
    // 절 갈래에서 첫 절보다 위에 남는 구간은 어느 section 으로도 안 열린다 — 말해 주지
    // 않으면 봇이 목차를 문서 전부로 읽는다 (실측: 40만 자 문서 4,156자 · 44만 자 문서
    // 6,907자가 첫 절보다 위에 있다, 2026-09-10).
    const before = outline.before > 0
      ? [`→ 앞 ${n(outline.before)}자는 어느 ${label}에도 안 듭니다 — 거기 있는 내용은 search 로 찾으세요.`]
      : [];
    // 날짜 하나짜리 조각이 섞였으면 왜 섞였는지 말해 준다 — 안 말하면 봇이 그 주가
    // 통째로 없는 것으로 읽는다.
    const mixed = outline.mixed
      ? ['→ 한 주가 상한을 넘어 그 주는 날짜별로 더 나눴습니다. 날짜 하나도 week 에 그대로 넣으면 됩니다.']
      : [];
    return [
      `${title} · 전체 ${n(total)}자 — 커서 목차만 실었습니다.`,
      `${label} ${outline.pieces.length}개:  ${list}`,
      `→ ${arg} 로 하나를 지정해 다시 부르세요${together}.`,
      ...before,
      ...mixed,
      '→ 문서 전체에서 낱말로 찾으려면 search 에 이 문서를 지정하세요 (where 와 document 를 함께).',
    ].join('\n');
  }

  /**
   * 목차 **앞에** 머리말 전문을 붙인다. 머리말이 없으면 목차를 그대로 돌려준다.
   *
   * ── 왜 (2026-09-03) ──
   *
   * 문서 md 맨 위에는 사람이 회차들을 훑어 손으로 적어 둔 정리가 있다 (`preambleOf`).
   * 그 구간은 **첫 회차 헤더보다 위**라 `outlineOf` 가 내는 조각 어디에도 안 들어가고,
   * `month` 를 지정해 불러도 **첫 월 헤딩보다 위**라 또 빠진다. 그래서 큰 문서에서는
   * **열어서 읽는 길이 아예 없었다** — 검색은 커밋 00e1c2b 로 닿게 됐지만(`scanDocuments`
   * 가 머리말을 회차 하나로 앞에 끼운다), `read_document` 는 목차만 주고 끝났다.
   *
   * 실측 2026-09-03 (실물 아카이브): 머리말 있는 문서 12개 합계 10,918자 중, 목차로
   * 빠지는 넷이 7,128자로 **65%** 다. 그 넷은 `read_document` 로 한 글자도 안 보였다.
   *
   * 길이 셋 중 ②로 정했다 — ① 목차에 「있음」만 적기 · **② 목차 앞에 전문을 붙이기** ·
   * ③ 월 호출에 함께 싣기 (WHK 결정 2026-09-03). ①은 「있다」만 알리고 여는 길을 안
   * 만든다. ③은 이번 범위 밖이라 **`month` 를 지정한 호출에서는 지금도 안 보인다.**
   *
   * ── 무엇을 자르나 ──
   *
   * **목차 본체는 절대 안 자른다.** 그것이 「다음에 무엇을 부를까」의 유일한 안내라,
   * 잘리면 봇이 조각 이름을 못 봐서 왕복이 막힌다. 예산이 모자라면 **머리말 쪽을** 자르고
   * **자른 사실을 돌려주는 글에 적는다** — 조용히 자르면 봇이 「이게 전부」로 읽는다.
   * (`truncated` 필드로는 안 된다. 읽는 쪽 — Hermes `src/llm/tools.js` · Clio
   * `src/agent.js` — 은 `text`·`hint` 와 좁힘 위치 표시(sheet·month·week·section)만
   * 봇에게 넘긴다.)
   *
   * 지금 실물 넷은 목차가 207~242자 · 머리말이 581~2,623자라 상한(60,000자)의 5% 아래고,
   * 자르기는 한 번도 안 걸린다. 그래도 두는 것은 머리말이 **사람 손으로 자라는 자리**라
   * 언젠가 걸리기 때문이다. 걸리는 쪽은 `check-doc-outline.js` 의 [19] 가 fixture 로
   * 확인하고(상한을 낮춰서), 붙는 쪽·안 붙는 쪽은 [16]~[18] 과 `check-doc-preamble.js`
   * 의 [6/6] 이 실물로 확인한다.
   */
  function outlineWithPreamble(body, pre, maxChars) {
    if (!pre) return body;
    const head = `${DOC_PREAMBLE_MARK}\n`;
    const gap = '\n\n';
    const room = maxChars - body.length - head.length - gap.length;
    if (room >= pre.length) return `${head}${pre}${gap}${body}`;

    const note = `\n…(머리말 ${pre.length.toLocaleString('en-US')}자 중 앞부분만 실었습니다 — 나머지는 search 로 찾으세요)`;
    const keep = room - note.length;
    // 목차만으로 이미 상한이면 붙일 자리가 없다. 상한을 넘기지 않는 것이 먼저라 목차만 낸다
    // (조각이 수백 개인 문서라야 여기 온다 — 실물에는 없다).
    if (keep <= 0) return body;
    return `${head}${pre.slice(0, keep)}${note}${gap}${body}`;
  }

  /**
   * 문서 md 전문, 또는 시트 하나만, 또는 '## YYYY-MM' 월 섹션만(그 달이 상한을 넘으면
   * 그 안의 한 주만, 그 주도 넘으면 하루만), 또는 번호 붙은 절 하나만. readChannel 과 같은 방식.
   *
   * 크고 sheet·month·section 으로 고를 수 있는 문서는 전문 대신 목차만 낸다 (outlineOf · renderOutline).
   */
  function readDocument({ project, document, month, week, sheet, section, access, maxChars = DOC_READ_MAX_CHARS }) {
    if (!hasDocuments()) return { error: '문서 아카이브가 설정되어 있지 않습니다.' };

    const p = resolveProjectFor(project, access);
    if (!p.ok) return { error: p.error };
    const d = resolveDocumentFor(p.name, document, access);
    if (!d.ok) return { error: d.error };
    // 위에서 이미 보이는 문서만 골랐다. 경계선이라 한 겹 더 둔다 — 나중에 이름 풀기를
    // 고칠 사람이 이 검사를 지우지 않게.
    if (!canSeeDoc(access, d.doc)) return { error: BLOCKED_NOTE };

    let text = readCached(d.doc.abs);

    // **가리는 것을 월로 자르기 전에 한다** (2026-09-03. 예전에는 자르고 나서 가렸는데,
    // 그러면 머리말은 이미 잘려 나간 뒤라 가릴 대상 자체가 없었다). 목차에 적는 조각
    // 크기가 실제로 보이는 양과 같아야 하고, 시트 이름 자체에도 비공개 채널 이름이
    // 섞일 수 있다. 헤더 줄에는 채널 이름이 안 들어가므로 블록 구조는 안 깨진다.
    //
    // 원래 자리의 이유는 그대로다 — 문서 md 의 사람이 쓴 메타·주석에 비공개 채널
    // 이름과 그 내용이 섞여 들어온다 (예: `본문은 R1 개정본으로 교체했다 —
    // #비공개나 에 올라온 판`). 대화 쪽 readChannel 과 같은 처리다.
    text = redactPrivateMentions(text, access);

    /* `month` 로 자르기 전에 머리말을 뽑아 둔다. 머리말은 첫 월 헤딩보다 **위**라 자르면
     * 사라진다 — 아래에서 `outlineWithPreamble` 로 다시 붙인다(커밋 7446962 가 목차 갈래에
     * 쓴 것과 같은 함수). **가린 뒤의 `text` 에서 뽑는다** — 원문에서 뽑으면 위 redact 를
     * 우회한다(outline 갈래와 같은 이유). */
    let monthPreamble = null;
    // 월 헤딩 줄(`## 2026-08`). 주 하나만 골랐을 때 그 줄을 다시 앞에 붙이는 데 쓴다.
    let monthHead = null;
    let weekLabel = null;
    let sectionName = null;
    let sectionPreamble = null;
    // 절 관문 판정. **`outlineOf` 를 부르는 그 자리에서 같은 글로 한 번만 잰다** —
    // 아래 잘림 안내가 이 값을 그대로 읽는다 (그 자리 주석이 이유의 원본).
    let sectionGate = null;
    if (month) {
      const lines = text.split('\n');
      // **정본은 `^##\s+`(관대한 쪽)** (WHK 지시 2026-09-03) — 공백 한 칸 고정(`l.trim() ===
      // '## '+month`)이면 헤딩이 `##  2026-08`(두 칸)처럼 실렸을 때 관문은 통과시키는데
      // 이 자리(봇의 월 단위 읽기)만 그 달을 못 찾는다. `archive.js` 의 `extractMonthSection`
      // (채널 쪽 같은 자리)이 이미 이 모양이라 그대로 옮겼다 — `month` 는 도구 인자(LLM 이
      // 채움)라 정규식 특수문자를 이스케이프한 뒤 짜 넣는다.
      const escapedMonth = month.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const startRe = new RegExp(`^##\\s+${escapedMonth}\\s*$`);
      const start = lines.findIndex((l) => startRe.test(l));
      if (start === -1) {
        const months = [...text.matchAll(/^##\s+(\d{4}-\d{2})\s*$/gm)].map((m) => m[1]);
        return { error: `${d.doc.slug} 에 ${month} 섹션이 없습니다. 있는 달: ${months.join(', ') || '없음'}` };
      }
      let end = lines.length;
      for (let i = start + 1; i < lines.length; i++) {
        if (/^##\s/.test(lines[i])) {
          end = i;
          break;
        }
      }
      monthPreamble = preambleOf(text);
      monthHead = lines[start];
      text = lines.slice(start, end).join('\n');
    }

    /* 그 달 안에서 **주 하나만** 연다. month 뒤에 온다 — 봇이 월 목차에서 달을 고르고,
     * 그 달이 또 크면 주 목차를 받아 주를 고르는 순서와 같다.
     *
     * **`week` 만 주는 것은 막는다.** 주는 그 달 안의 축이라 달 없이는 가리킬 것이 없고,
     * 조용히 무시하면 봇이 좁혀졌다고 믿는다 (`sheet`+`section` 과 같은 규칙).
     * 시트·절과 함께 주는 것도 막는다 — 어느 축을 따를지 정할 수 없다. */
    if (week) {
      if (!month) {
        return { error: 'week 는 month 와 함께 써야 합니다. 먼저 month 로 달을 고르세요.' };
      }
      if (sheet || section) {
        return { error: 'week 는 sheet·section 과 함께 쓸 수 없습니다. 주로 좁히려면 month 와 week 만 주세요.' };
      }
      // **목차를 만든 것과 같은 상한으로 나눈다.** 기본값으로 부르면 `maxChars` 를 낮춰
      // 부른 호출에서 목차가 권한 날짜 조각을 선택 갈래가 못 찾는다.
      const weeks = weeksOf(text, maxChars, monthHead ? monthHead.length + 2 : 0);
      if (!weeks) {
        return { error: `${d.doc.slug} 의 ${month} 는 주 단위로 나뉘지 않습니다. week 없이 부르세요.` };
      }
      const raw = String(week).trim();
      const target = fold(raw);
      /* **숫자만 주면 목차 순번으로만 찾는다. 이름은 보지 않는다.**
       *
       * 아래 section 갈래가 같은 교훈을 이미 적어 뒀는데(「숫자만 주면 절 번호로만」),
       * 이 갈래는 처음에 sheet 처럼 「이름 먼저, 번호 나중」으로 뒀다가 **실물에서
       * 오배달이 났다** (2026-09-20 회의적 검증). 주 이름이 전부 날짜라 한 자리 숫자가
       * 이름에 부분 일치해 버린다 — 실측: `week:"4"` 가 목차 4번이 아니라
       * `2026-08-24~08-30` 을 열었고(그 이름에 `4` 가 있다), 실물 문서 5개·달 13개에서
       * **오배달 7건 + 조각 수를 넘는 순번이 열린 것 9건**이었다. 에러가 안 나서 봇은
       * 엉뚱한 주의 숫자로 답한다.
       *
       * 범위 밖 순번이면 **아무것도 안 연다** — 이름으로 흘려보내면 그 사고가 돌아온다. */
      let hit;
      if (/^\d+$/.test(raw)) {
        const one = weeks[Number(raw) - 1];
        hit = one ? [one] : [];
      } else {
        // 이름 전체(`2026-08-03~08-09`)로도, 월요일 날짜(`2026-08-03`)로도 된다.
        hit = weeks.filter((w) => fold(w.name) === target || w.monday === raw);
        if (hit.length !== 1) hit = weeks.filter((w) => fold(w.name).includes(target));
      }
      if (hit.length !== 1) {
        return { error: `'${week}' 주를 특정하지 못했습니다. 있는 주: ${weeks.map((w) => w.name).join(', ')}` };
      }
      weekLabel = hit[0].name;
      // **월 헤딩 줄을 다시 앞에 붙인다.** `splitMessages` 는 첫 회차 헤더 앞을 버리므로
      // 주 블록만 이으면 어느 달인지가 글에서 사라진다.
      const body = hit[0].blocks.map((b) => b.text).join('\n\n');
      text = monthHead ? `${monthHead}\n\n${body}` : body;
    }

    const named = splitMessages(text)
      .map((b) => ({ b, name: (b.text.match(SHEET_RE) || [])[3] }))
      .filter((x) => x.name);

    // sheet 는 문서가 실제로 그렇게 나뉜 축이고 section 은 그 안의 번호 붙은 절이라,
    // 함께 주면 어느 것을 따를지 정할 수 없다. **조용히 무시하면 봇이 좁혀졌다고 믿는다.**
    if (sheet && section) {
      return { error: 'sheet 와 section 은 함께 쓸 수 없습니다. 엑셀은 sheet, 절로 나뉜 문서는 section 으로 여세요.' };
    }

    if (sheet) {
      if (!named.length) {
        return { error: `${d.doc.slug} 는 시트로 나뉜 문서가 아닙니다. sheet 없이 부르세요.` };
      }
      const target = fold(sheet);
      let hit = named.filter((x) => fold(x.name) === target);
      if (hit.length !== 1) hit = named.filter((x) => fold(x.name).includes(target));
      if (hit.length !== 1 && /^\d+$/.test(String(sheet).trim())) {
        const one = named[Number(String(sheet).trim()) - 1];
        hit = one ? [one] : [];
      }
      if (hit.length !== 1) {
        // 여럿 걸리면 아무것도 안 연다 — resolveDocument 와 같은 규칙이다.
        return { error: `'${sheet}' 시트를 특정하지 못했습니다. 있는 시트: ${named.map((x) => x.name).join(', ')}` };
      }
      text = hit[0].b.text;
    } else if (section) {
      /* 절 하나만 연다. **month 뒤에 온다** — month 로 좁힌 조각 안에서 다시 찾는 것이
       * 봇이 목차를 보고 고르는 순서와 같다.
       *
       * 고르는 규칙은 sheet 와 같은 모양이다 — 번호로도 되고 제목 일부로도 된다.
       * 여럿 걸리면 **아무것도 안 연다** (resolveDocument·sheet 와 같은 규칙). */
      const sections = sectionsOf(text);
      if (!sections) {
        return { error: `${d.doc.slug} 는 번호 붙은 절로 나뉜 문서가 아닙니다. section 없이 부르세요.` };
      }
      const raw = String(section).trim();
      const target = fold(raw);
      /* **숫자만 주면 절 번호로만 찾는다. 제목은 보지 않는다.**
       *
       * 여기를 `sheet` 처럼 「이름 먼저, 번호 나중」으로 두면 **조용히 틀린다.** 절 제목에는
       * 숫자가 흔히 들어가서(`A107BL`·`8구역`·`15블럭`) 제목 부분 일치가 번호를 앞지른다.
       * 실측(2026-09-11 회의적 검증): `section="7"` 이 7번 절이 아니라 **10번 절**(제목에
       * 'A107')을 열었다 — 에러도 경고도 없이. 58개 중 3개, 다른 문서에서는 4개 중 1개가
       * 어긋났다. 시트가 안 터진 것은 3~10개뿐이고 이름에 숫자가 드물어서였다.
       *
       * 없는 번호(결번)면 **아무것도 안 연다.** 제목으로 흘려보내면 「17」이 제목에 든
       * 엉뚱한 절이 열린다. 숫자를 준 것은 번호를 가리킨 것이다. */
      let hit;
      if (/^\d+$/.test(raw)) {
        hit = sections.filter((s) => s.n === Number(raw));
      } else {
        hit = sections.filter((s) => fold(`${s.n}. ${s.name}`) === target || fold(s.name) === target);
        if (hit.length !== 1) hit = sections.filter((s) => fold(s.name).includes(target));
      }
      if (hit.length !== 1) {
        const names = sections.map((s) => `${s.n}. ${s.name}`).join(' · ');
        return { error: `'${section}' 절을 특정하지 못했습니다. 있는 절: ${names}` };
      }
      sectionName = `${hit[0].n}. ${hit[0].name}`;
      // 자르기 전에 머리말을 잡아 둔다 — 머리말은 첫 절보다 **위**라 자르면 사라진다
      // (month 와 같은 이유).
      sectionPreamble = preambleOf(text);
      text = text.slice(hit[0].start, hit[0].end);
    } else {
      /* 갈래 2 — 어차피 잘릴 문서이고 봇이 고를 인자가 있다. 전문 대신 목차를 준다.
       * 실측(2026-08): read_document **크기가 기록된** 54건 중 10건이 상한에 걸렸고
       * 그 10건이 문서 읽기 자료의 51%였다. 그 6만 자는 대개 "무슨 시트가 있나"를
       * 알아내려던 것이라 다음 호출에서 버려진다.
       *
       * 「크기가 기록된」이 중요하다 — 같은 창의 실제 호출은 104건이고 50건은 도구 결과
       * 크기(chars)가 로그에 안 실려 크기를 모른다(그 필드가 나중에 생겼다). 18.5% 는
       * 「잰 것 중에서」다. 다시 재려면 `npm run log:measure` (2026-09-07 에 저장소로 들어옴).
       *
       * **month 로 이미 좁혀 부른 호출은 제외한다.** 봇이 조각 하나를 고른 뒤인데
       * 목차를 다시 주면 「고르라」는 말을 두 번 하는 것이라 왕복만 는다. */
      /* **month 로 좁힌 호출에는 주 목차를 준다** (2026-09-20). 그전에는 여기서 무조건
       * null 이라 「그 한 달이 상한을 넘는 문서」가 앞 6만 자로 잘렸다 — 좁힐 축이 더
       * 있는데 없는 것처럼 굴었다. `weeksOf` 가 못 나누면 여전히 null 이고, 그러면
       * 아래 잘림 갈래가 지금까지처럼 정직한 안내를 붙인다.
       * **주까지 좁힌 호출(week)에는 안 준다** — 고른 뒤에 또 고르라는 말이 된다. */
      const outline = month
        ? (week ? null : weekOutline(text, maxChars, monthHead ? monthHead.length + 2 : 0))
        : outlineOf(text, maxChars);
      /* **절 관문을 여기서 한 번만 잰다.** 아래 잘림 안내가 `outlineOf` 와 **같은 글**을
       * 봐야 판정이 갈리지 않는다 — 몇 줄 아래에서 시트 머리줄(`시트 N개: …`)이 text 앞에
       * 붙는데, 거기서 다시 재면 덮음률의 분모가 달라진다. 지금 실물에서는 접두가 수십 자라
       * 차이가 0.001 미만이고 `blocked-cover` 문서도 0건이지만, **「같은 판정을 다른 입력으로
       * 두 번 계산」이 이 파일이 이번에 두 번 겪은 고장의 뿌리다** (2026-09-11: 안내가
       * 관문을 아예 안 봤던 것 → 관문의 두 이유 중 하나만 말했던 것). 재는 자리를 하나로
       * 못박아 그 갈래를 없앤다. */
      sectionGate = sectionOutline(text, maxChars);
      if (outline && text.length > maxChars) {
        /* 목차 앞에 머리말 전문을 붙인다 (`outlineWithPreamble` 주석이 이유의 원본).
         * **가린 뒤의 text 에서 다시 뽑는다** — `d.doc.preamble` 은 원문에서 뽑은 것이라
         * 비공개 채널 이름이 그대로 들어 있다. 여기서 그걸 쓰면 위 redact 를 우회한다. */
        /* 주 목차 갈래만 다르게 다루는 것 둘 — ① 제목에 그 달을 붙인다(무엇의 목차인지
         * 글에서 사라지면 봇이 문서 전체의 주로 읽는다) ② 머리말은 위에서 **자르기 전에**
         * 뽑아 둔 `monthPreamble` 을 쓴다. 여기서 `preambleOf(text)` 를 부르면 월 조각의
         * 첫 회차 앞, 즉 월 헤딩 줄이 머리말로 잡힌다. */
        const isWeek = outline.kind === 'week';
        return {
          project: p.name,
          document: d.doc.slug,
          title: d.doc.title,
          month: isWeek ? month : null,
          sheet: null,
          section: null,
          text: outlineWithPreamble(
            renderOutline(isWeek ? `${d.doc.title} · ${month}` : d.doc.title, text.length, outline),
            isWeek ? monthPreamble : preambleOf(text),
            maxChars,
          ),
          truncated: false,
          outline: outline.kind,
          hint: '',
        };
      }
      /* 시트 이름은 색인에 안 적는다(예산). 봇이 이름을 아는 두 번째 길이 여기다.
       * **month 를 지정한 호출에도 붙인다** — 그 달 안에 무슨 시트가 있는지는 좁혀
       * 부른 봇에게도 필요하다. `check-doc-outline.js` 의 [13] 이 이걸 지킨다. */
      if (named.length) {
        text = `시트 ${named.length}개: ${named.map((x) => x.name).join(' · ')}\n\n${text}`;
      }
    }

    /* month·section 으로 좁힌 호출에도 머리말을 붙인다 (2026-09-03, WHK 결정 — 커밋
     * 7446962 가 목차 갈래에서 「범위 밖」으로 남겨 둔 자리다. section 은 그 뒤에
     * 같은 자리를 더 늘린 것이다). **같은 함수를 그대로 쓴다**(`outlineWithPreamble`)
     * — 본문(그 달·그 절의 회차들)은 안 자르고, 예산이 모자라면 머리말 쪽만 잘라
     * 그 사실을 적는다. `sheet` 를 같이 지정했어도 머리말은 문서 단위라 그대로 붙인다. */
    if ((month && monthPreamble) || (sectionName && sectionPreamble)) {
      text = outlineWithPreamble(text, monthPreamble || sectionPreamble, maxChars);
    }

    let truncated = false;
    let hint = '';
    if (text.length > maxChars) {
      const n = (v) => v.toLocaleString('en-US');
      const total = text.length;
      // **자르기 전에 센다.** 자른 뒤에 세면 6만 자 밖에 있는 회차가 안 보여서,
      // search 로 닿을 수 있는 문서를 「못 닿는다」로 잘못 안내한다. 절 유무도 마찬가지다
      // — 자른 뒤에 sectionsOf 를 부르면 뒤쪽 절이 안 보여 「절이 없다」로 잘못 안내한다.
      const blocks = splitMessages(text).length;
      /* 관문 판정은 **위에서 `outlineOf` 를 부른 그 자리에서 이미 쟀다**(`sectionGate`).
       * 여기서 다시 부르지 않는다 — 그 사이에 시트 머리줄이 text 앞에 붙어서, 다시 재면
       * 덮음률의 분모가 달라진다. 왜 안 막혔는지(kind)까지 읽어야 아래 scope 갈래가
       * 거짓을 안 말한다 (아래 주석).
       *
       * `sectionGate` 가 null 인 것은 sheet·section 을 지정해 부른 호출이라 위 else 갈래를
       * 안 지났다는 뜻인데, 그때는 `canNarrow` 가 참이라 아래에서 gate 를 안 본다. */
      const gate = sectionGate ?? { kind: 'none' };
      text = text.slice(0, maxChars);
      truncated = true;
      /* **좁힐 수 있는 문서인지, 그리고 절 관문이 왜 막혔는지에 따라 안내가 다섯으로 갈린다.**
       * 예전에는 어느 경우든 "month 나 sheet 를 지정해 좁혀 보세요" 하나였는데,
       * 실측 26건은 시트도 월도 없어서 그 말이 막다른 길이었다. 그래서 sheet·month 가
       * 없는 문서를 「절이 있나」로 다시 갈랐는데, **그때 raw sectionsOf 만 보고 관문
       * (sectionOutline)을 안 봤다** — 그래서 관문이 일부러 목차를 끈 문서에도
       * 「section 으로 한 절만 여세요」가 나갔다. 그 문서는 목차가 안 와서 봇이 절
       * 이름을 모르고, section:"1" 을 찍으면 69자·14자를 받는다 — 45만 자 문서에서
       * 그것을 받고 「내용이 없다」로 읽는다 (실측 34건 중 2건, 2026-09-11 전체 검증).
       *
       * **이 관문을 나중에 다시 kind 없이 합치면 안 된다.** 「막혔다」한 가지로만 보면
       * 막힌 이유가 늘 "가장 큰 절이 상한을 넘어"로 단정되고, **덮음률 미달로 막힌
       * 문서**(머리말이 길고 절 자체는 작은 문서)에도 그 거짓 문장이 나간다 — 그런
       * 문서는 실제로 section 인자 자체는 못 쓰지만 이유는 크기가 아니다(전체 검증
       * 2회차, 2026-09-11). 그래서 `sectionOutline` 이 낸 kind 를 그대로 읽는다.
       *
       * **이 갈래에 실제로 닿는 경우는 둘이다.** ① sheet·month·section 으로 이미 좁혀
       * 부른 호출(canNarrow=true — 바로 아래 식과 같은 셋이다). ② 아무것도 안 좁히고 불렀는데 outlineOf 가 null 을 낸
       * 경우 — 절이 아예 없거나(kind='none'), 절은 있지만 관문(가장 큰 절이 상한
       * 초과='blocked-size' 또는 덮음률 미달='blocked-cover')에 막힌 경우다.
       * **관문을 통과한 문서는 outlineOf 가 위에서 목차로 먼저 돌려보내므로 이 갈래에
       * 안 온다** — 실측 34건 중 13건이 ②로 이 갈래에 닿았고, 그중 2건이 관문에 막힌
       * 절 보유 문서였다(위 사고. FULL_ACCESS 기준. PUBLIC_ACCESS 로는 목차 17·잘림 13). */
      const canNarrow = sheet || month || sectionName;
      let scope;
      if (canNarrow) {
        scope = `이 조각만도 ${n(total)}자라 앞 ${n(maxChars)}자만 실었습니다.`;
      } else if (gate.kind === 'ok') {
        // 이론상 여기 안 온다 — 관문을 통과했으면 위 outlineOf 가 목차로 먼저
        // 돌려보낸다. 그래도 판정이 갈리지 않게 gate.kind 로 직접 확인한다.
        scope = `전체 ${n(total)}자 중 앞 ${n(maxChars)}자만 실었습니다. 이 문서는 번호 붙은 절로 나뉘어 있으니 section 으로 한 절만 여세요.`;
      } else if (gate.kind === 'blocked-size') {
        scope = `전체 ${n(total)}자 중 앞 ${n(maxChars)}자만 실었습니다. 이 문서는 절로 나뉘어 있지만 가장 큰 절이 ${n(maxChars)}자를 넘어 section 으로 좁혀도 앞부분만 옵니다.`;
      } else if (gate.kind === 'blocked-cover') {
        scope = `전체 ${n(total)}자 중 앞 ${n(maxChars)}자만 실었습니다. 이 문서는 절로 나뉘어 있지만 절이 문서 앞부분을 충분히 덮지 못해 section 으로는 앞쪽에 닿지 못합니다.`;
      } else {
        scope = `전체 ${n(total)}자 중 앞 ${n(maxChars)}자만 실었습니다. 이 문서는 sheet·month 로 나뉘어 있지 않아 좁힐 수 없습니다.`;
      }
      /* **어디로 보낼지는 「그 조각 안에 회차가 여럿이냐」가 가른다.**
       * search 의 발췌는 회차 블록의 앞 DOC_HIT_MAX_CHARS 자를 자를 뿐이라(위 clip),
       * 회차가 하나뿐이면 방금 준 6만 자의 **부분집합**이 돌아온다 — 거기로 보내면
       * 봇이 왕복을 한 번 더 쓰고 같은 앞부분을 다시 본다. 회차가 여럿이면 블록마다
       * 발췌가 따로 와서 실제로 뒤쪽에 닿는다.
       * (2026-08-28 실측: 나눌 축 없는 큰 문서 13건 중 12건이 회차 1개였고, search 가
       *  read_document 보다 더 닿은 것은 1건·4,000자뿐이었다. 반대로 월 조각 7건은
       *  안에 회차가 7~18개라 닿는다.)
       * document 만 주면 조용히 전 문서를 훑으므로 where 를 함께 주라고 적는다
       * (`scanDocuments` 는 project 가 있을 때만 document 로 좁힌다). */
      hint = blocks >= 2
        ? `${scope} 나머지는 search 에 이 문서를 지정해(where 와 document 를 함께) 낱말로 찾으세요.`
        : `${scope} 회차가 하나뿐이라 search 로도 앞 ${n(DOC_HIT_MAX_CHARS)}자밖에 안 옵니다 — 뒤쪽은 도구로 볼 수 없으니 원본 파일을 열어야 합니다.`;
    }
    return {
      project: p.name,
      document: d.doc.slug,
      title: d.doc.title,
      month: month || null,
      week: weekLabel,
      sheet: sheet || null,
      section: sectionName,
      text,
      truncated,
      outline: null,
      hint,
    };
  }

  /**
   * 요약 창(oldest~latest) 안에 슬랙으로 올라온 문서. **변환이 끝난 것만** 나온다.
   *
   * 창 판정의 근거는 `.doc-state.json` 의 `ts` — **변환 시각이 아니라 슬랙 업로드 시각**이다.
   * 어제 올라온 것을 오늘 변환했다고 오늘 요약에 실으면, 어제 요약이 이미 다룬 자료가
   * 하루 늦게 또 나온다.
   *
   * 공개 판정을 여기서 새로 만들지 않는다 — `canSeeDoc` 을 그대로 쓴다. 요약이 채널로
   * 나가면 digest.js 가 PUBLIC_ACCESS 를 넘기므로 비공개 채널 문서는 여기서 빠진다.
   *
   * @param {{oldest:number, latest:number, access:object, maxChars?:number}} opts 창은 초 단위 epoch
   * @returns {{docs:Array, hidden:number, gistOnly:number}} hidden = 권한 때문에 뺀 건수(이름은 돌려주지 않는다)
   */
  function documentsUploadedIn({ oldest, latest, access, maxChars = DIGEST_DOC_MAX_CHARS }) {
    const empty = { docs: [], hidden: 0, gistOnly: 0 };
    if (!hasDocuments() || !DOCS_DIR) return empty;

    let state;
    try {
      state = JSON.parse(fs.readFileSync(path.join(DOCS_DIR, '.doc-state.json'), 'utf8'));
    } catch {
      // 상태 파일을 못 읽어도 요약은 나가야 한다. 문서 없이 대화만으로 만든다.
      return empty;
    }

    // 최근 업로드부터. 상한에 걸리면 오래된 쪽이 요지만 남는다.
    const uploads = Object.values(state.slack_files || {})
      .filter((f) => f && f.doc && Number(f.ts) >= oldest && Number(f.ts) < latest)
      .sort((a, b) => Number(b.ts) - Number(a.ts));

    const docs = [];
    const seen = new Set();
    let hidden = 0;
    let gistOnly = 0;
    let used = 0;

    for (const f of uploads) {
      // `.doc-state.json` 의 doc 값은 손으로 적는 자리라 `projects/` 접두가 붙은 것과 안 붙은
      // 것이 섞여 있었다 — 2026-08-10 실측 336건 중 20건. 안 떼면 `projects/projects/…` 가 되어
      // loadDocument 가 broken 으로 돌려주고, 그 문서는 일일 요약에서 **말없이** 빠진다.
      // **2026-08-27 에 상태 파일 41건을 짧은 형태로 통일했다.** 그래도 이 줄은 남긴다 —
      // 손으로 적는 자리라 다시 섞일 수 있고, 어긋나면 에러가 아니라 누락으로만 드러난다.
      // 같은 방어가 archive-health.js 의 docPath · apply_approvals.py 의 doc_path ·
      // decide.py 의 _norm 에도 있다.
      const rel = String(f.doc).replace(/^projects\//, '');
      // 사업장은 한 겹 더 깊을 수 있다 ('_공통/정기보고'). 맨 뒤 '/' 에서 가른다.
      const cut = rel.lastIndexOf('/');
      if (cut < 1) continue;
      const project = rel.slice(0, cut);
      const file = rel.slice(cut + 1);

      const d = loadDocument(project, file);
      // broken 은 메타를 못 읽은 것이고 loadDocument 가 private=true 로 돌려준다(fail-closed).
      // 아래 canSeeDoc 이 어차피 막지만, 요약에 제목만 실리는 일도 없게 여기서 끊는다.
      if (d.broken) continue;
      if (!canSeeDoc(access, d)) {
        hidden += 1;
        continue;
      }

      // 같은 회차를 두 번 싣지 않는다 (같은 자료를 hwp·pdf 로 함께 올린 경우).
      // 접두를 뗀 rel 로 묶는다 — 같은 문서가 접두 있는 값과 없는 값으로 함께 적혀 있으면
      // f.doc 으로는 다른 키가 되어 두 번 실린다.
      const key = `${rel}|${f.date || ''}`;
      if (seen.has(key)) continue;
      seen.add(key);

      const entry = d.entries.find((e) => e.date === f.date) || d.entries[d.entries.length - 1];
      // 남은 자리에 맞춰 자른다. 자리가 모자란다고 문서를 통째로 버리지 않는 것은, 뒤에 오는
      // 짧은 문서만 실리고 긴 문서가 빠지면 "무엇이 실렸나"가 길이순이 되어 버리기 때문이다.
      const room = Math.min(DIGEST_DOC_PER_DOC_CHARS, maxChars - used);
      // 문서 md 의 사람이 쓴 메타·주석에 비공개 채널 이름이 섞여 들어온다. readDocument 와 같은 처리.
      const body =
        entry && room >= DIGEST_DOC_MIN_CHARS
          ? redactPrivateMentions(clip(entry.text, room), access)
          : '';
      if (entry && !body) gistOnly += 1;
      used += body.length;

      docs.push({
        // 요약 프롬프트에 그대로 실린다 (Hermes `src/llm/digest.js` 의 `**${d.project} · ${d.title}**`).
        project: maskProject(project, access),
        title: d.title,
        kind: d.meta['종류'] || '',
        gist: redactPrivateMentions(d.meta['주요 항목'] || '', access),
        // 스캔본은 조문·숫자가 뭉개져 있다. 요약이 그대로 인용하지 않게 표시해서 넘긴다.
        ocr: Object.values(d.meta).some((v) => /OCR 사용/.test(String(v))),
        date: f.date || entry?.date || '',
        file: f.name || '',
        text: body,
      });
    }

    return { docs, hidden, gistOnly };
  }

  return { outlineOf, readDocument, documentsUploadedIn };
}
