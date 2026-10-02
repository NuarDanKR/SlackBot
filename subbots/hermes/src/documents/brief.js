/**
 * Codex R2d: document index rendering, folding and permission splits.
 * Constructor is lazy. Date defaults to the real constructor; tests supply a
 * fixed constructor producing a fresh instance for each new Date().
 * Limits are snapshots supplied by the facade, matching module-load semantics.
 * Keep byte-identical in Hermes and Clio.
 */
export function createDocumentBrief({
  path, DOCS_DIR, readCached, hasDocuments, listProjects, listDocuments, canSeeDoc, maskProject, projectPrivateChannel, canSeePrivateChannel, redactPrivateMentions, PUBLIC_ACCESS, preambleOutline, latestDate, isSheetDoc, isSeriesDoc, DOC_BRIEF_MAX_CHARS, DOC_BRIEF_PRIVATE_MAX_CHARS, DOC_BRIEF_RECENT_MONTHS, FOLD_KINDS_MAX, Date = globalThis.Date
}) {
  function entryDateRange(entries) {
    if (!entries.length) return null;
    const dates = entries.map((e) => e.date).sort();
    return dates[0] === dates[dates.length - 1]
      ? dates[0]
      : `${dates[0]}~${dates[dates.length - 1]}`;
  }

  /**
   * 색인에서 접히는 순서. 값이 작을수록 끝까지 남는다.
   *
   * 색인의 용도는 "어느 문서를 열지 판단" 이므로, 색인에 없으면 봇이 존재를
   * 짐작조차 못 하는 것부터 남긴다. 접혀도 search · read_document 는
   * listDocuments() 로 파일을 직접 훑으므로 **검색 대상에서 빠지지 않는다.**
   */
  function foldRank(doc, recentCutoff) {
    if (isSeriesDoc(doc)) return 0;  // 시리즈 — 추이는 색인에만 있다
    if (/계약|약정|정관|협약/.test(doc.meta['종류'] || '')) return 1;  // 금액·당사자·만기의 근거
    return latestDate(doc.entries) >= recentCutoff ? 2 : 3;   // 최근 것 우선
  }

  /** YYYY-MM-DD 에서 months 개월 뺀 날짜 */
  function monthsAgo(months) {
    const d = new Date();
    d.setMonth(d.getMonth() - months);
    return d.toISOString().slice(0, 10);
  }

  /**
   * index.md 의 「변환하지 못한 것」 절만 떼어 온다. 없으면 빈 문자열.
   * 이 절은 사람이 손으로 쓴 표라 비공개 채널의 문서 제목·상대 회사명이 행으로 들어 있다
   * (`#사업장카 공문 3건` 처럼). 부르는 쪽에서 권한으로 한 번 걸러야 한다.
   */
  function missingSection() {
    try {
      const text = readCached(path.join(DOCS_DIR, 'index.md'));
      const lines = text.split('\n');
      const start = lines.findIndex((l) => /^##\s+변환하지 못한 것\s*$/.test(l));
      if (start === -1) return '';
      let end = lines.length;
      for (let i = start + 1; i < lines.length; i++) {
        // 다음 절 또는 구분선까지. 구분선을 경계로 두지 않으면 파일 꼬리말이 딸려 온다.
        if (/^## /.test(lines[i]) || /^---\s*$/.test(lines[i])) {
          end = i;
          break;
        }
      }
      return lines.slice(start, end).join('\n').trim();
    } catch {
      return '';
    }
  }

  /**
   * 접힌 문서들을 색인 한 줄로. 종류는 많은 것부터 FOLD_KINDS_MAX 개까지만, 개수는 안 적는다.
   *
   * **길이가 종류 개수에 안 비례하는 것이 요점이다.** 이 줄은 접히지 않으므로 여기가
   * 자라면 바닥이 영구히 오른다.
   *
   * 같은 개수인 종류의 순서는 **훑은 순서 그대로** 둔다 (JS 정렬은 ES2019 부터 안정적이다).
   * 순서가 흔들리면 색인 공통분의 글자가 달라져 프롬프트 캐시가 새로 써진다 — 색인을
   * 공통·권한별로 쪼갠 이유가 그것이다 (커밋 8468500). 에러 없이 비용으로만 드러난다.
   *
   * 접힘의 뜻을 설명하는 문장은 여기 안 붙인다 — 머리말(BRIEF_HEADER)에 한 번만 있다.
   * 사업장마다 붙이면 36개 곱하기라 예산을 다 먹는다.
   */
  function foldedLine(folded) {
    const byKind = {};
    for (const d of folded) {
      const k = (d.meta && d.meta['종류']) || '문서';
      byKind[k] = (byKind[k] || 0) + 1;
    }
    const kinds = Object.entries(byKind).sort((a, b) => b[1] - a[1]);
    const top = kinds.slice(0, FOLD_KINDS_MAX).map(([k]) => k).join('·');
    // 상한 이하면 ` 등` 을 안 붙인다 — 붙이면 접힌 종류가 더 있는 것처럼 읽힌다.
    const more = kinds.length > FOLD_KINDS_MAX ? ' 등' : '';
    return `- 그 외 ${folded.length}건${top ? ` (${top}${more})` : ''}`;
  }

  /**
   * 바닥을 조각으로 나눠 보인다 — **어디가 자랐는지가 그 자리에 보여야 한다.**
   *
   * 2026-08-05 에 「감시할 것은 시리즈 개수와 사업장 개수」라고 정했는데, 다음날 재보니
   * 그 둘은 바닥의 34%뿐이었고 가장 큰 조각은 사람이 손으로 쓰는 예외 표였다.
   * 무엇을 셀지 사람이 미리 정하면 같은 실수가 반복된다. 그래서 세지 말고 나눠 보인다.
   *
   * 글자 수를 다섯 조각에 나누는 것은 애매하지 않다 — 줄 하나는 반드시 조각 하나에만
   * 속한다. 애매한 것은 줄 사이 구분자(\n)다: L 줄이면 구분자는 L-1 개로 줄 수보다
   * 하나 적다(마지막 줄 뒤에는 구분자가 없다). 그래서 줄마다 자기 뒤에 구분자가 하나
   * 있다고 치고 `l.length + 1` 로 세되, **마지막 줄이 속한 조각에서만 1을 뺀다** —
   * 그 줄에는 실제로 뒤따르는 구분자가 없기 때문이다. 어느 조각이 마지막 줄을 담고
   * 있는지는 미리 정해 두지 않고 매번 데이터로 구한다(지금 실물에서는 예외표절이
   * 파일 맨 끝에 붙고 그 절이 빈 줄로 끝나 마지막 원소가 빈 문자열이라 예외표가
   * 걸리지만, 그건 이 파일의 사정이지 규칙이 아니다).
   *
   * 이 계산은 이미 두 번 틀렸다. ① 조각마다 `+1` 을 그대로 더한 버전 — 각 조각은
   * 정직했지만 마지막 줄에 없는 구분자까지 세어 다섯 조각의 합이 바닥보다 1 컸다.
   * ② 총합만 맞추려고 구분자 전체(L-1)를 머리말 하나에 몰아준 버전 — 총합은
   * 맞았지만 머리말이 다른 네 조각의 구분자까지 떠안아, 사업장이나 시리즈를
   * 늘리면(머리말 자기 줄은 안 늘었는데도) 머리말 숫자가 함께 움직였다. 둘 다
   * 「고쳐서」 되돌아가지 않도록 `check-floor-parts.js` 가 지킨다.
   */
  function floorParts(text) {
    const lines = text.split('\n');
    const cut = lines.findIndex((l) => /^## /.test(l));   // 「변환하지 못한 것」 절의 시작
    const inMissing = (idx) => cut !== -1 && idx >= cut;

    // 줄 하나가 어느 조각인지. 예외표절 안이면 그걸로 끝, 아니면 나머지 셋을 차례로
    // 보고 아무 것도 아니면 머리말 — 그래서 모든 줄이 반드시 조각 하나에 떨어진다.
    const bucketOf = (idx) => {
      if (inMissing(idx)) return '예외표';
      const l = lines[idx];
      if (/^- 그 외 /.test(l)) return '접힘줄';
      if (/^- \[/.test(l)) return '시리즈';
      if (/^### /.test(l)) return '사업장';
      return '머리말';
    };

    const order = ['예외표', '접힘줄', '시리즈', '사업장', '머리말'];
    const totals = Object.fromEntries(order.map((k) => [k, 0]));
    lines.forEach((l, idx) => {
      totals[bucketOf(idx)] += l.length + 1;
    });
    // 마지막 줄에는 실제로 뒤따르는 구분자가 없다 — 그 줄이 속한 조각에서만 1을 뺀다.
    totals[bucketOf(lines.length - 1)] -= 1;

    return order.map((k) => `${k} ${totals[k]}`).join(' · ');
  }

  /**
   * 색인 계기판 — check-setup [4/6] · pre-commit · 7.5 점검표가 **같은 함수**를 본다.
   * 세 자리가 따로 계산하면 한쪽만 고쳤을 때 숫자가 조용히 갈린다.
   *
   * floor  : 전부 접었을 때 길이. 2026-09-10 부터 시리즈도 접히므로 이 값이 상한(6,000)을
   *          넘는 일은 구조적으로 없다 — 넘으면 코드 결함이다.
   * foldedSeries : **지금 예산으로 렌더했을 때** 색인에서 접혀 안 보이는 시리즈 제목.
   *          「바닥이 경고선을 넘었다」를 대신하는 새 경보다 — 시리즈가 접히기 시작했다는
   *          것은 색인 공간이 실제로 모자라기 시작했다는 뜻이다.
   *
   * **텍스트에서 역산하지 않는다.** 첫 판은 렌더된 문자열에서 `] 제목` 을 정규식으로
   * 찾았는데, 제목 하나가 다른 문서 제목의 접두인 실물 6쌍(예: "본부별 주간보고" 는
   * "본부별 주간보고 (경영혁신실 취합)" 의 접두)에서 뒤쪽만 남아 있어도 앞쪽이 안 접힌
   * 것처럼 오판했다 (2026-09-10 리뷰, production 예산 6,000자에서 실측 재현). `buildDocumentsBrief`
   * 의 접기 루프는 어떤 문서를 접었는지 이미 구조적으로 알고 있으므로(`c.g.folded.push`),
   * 그 결과를 `foldedOut` 으로 그대로 받는다 — 텍스트를 다시 읽지 않는다.
   */
  function indexGauge({ budget = DOC_BRIEF_MAX_CHARS } = {}) {
    const floorText = buildDocumentsBrief({ access: PUBLIC_ACCESS, maxChars: 0 });
    const folded = [];
    buildDocumentsBrief({ access: PUBLIC_ACCESS, maxChars: budget, foldedOut: folded });
    const foldedSeries = folded
      .filter((d) => isSeriesDoc(d))
      .map((d) => `${d.project}/${d.title}`);
    return { floor: floorText.length, parts: floorParts(floorText), foldedSeries };
  }

  /* 머리말 제목 뽑기는 **`archive.js` 의 `preambleOutline` 하나뿐이다** (2026-09-03).
   *
   * 2026-09-03 까지는 여기 `docPreambleOutline` 이라는 글자까지 같은 사본이 있었다 — 저쪽이
   * `export` 가 아니어서 가져다 쓸 수가 없었기 때문이다. 두 벌이면 한쪽만 고쳤을 때 **같은
   * 머리말인데 채널 색인과 문서 색인이 다른 제목을 싣고, 에러는 안 난다.** 저쪽에 `export`
   * 를 붙이고 이 사본을 지웠다.
   *
   * **여기 사본을 다시 만들지 말 것.** 문서 쪽만 다르게 뽑고 싶어지면 그때도 사본이 아니라
   * 저쪽 함수에 인자를 붙이는 쪽이 맞다 — 사본은 갈려도 아무 데서도 에러가 안 난다.
   * `scripts/check-doc-preamble.js` 의 [2/6] 가 사본이 되살아나는 것을 막는다. */

  const BRIEF_HEADER = [
    /* 괄호 안은 **자료 저장소 안에서의 자리**로 적는다. 절대경로를 적으면 팀마다 틀리고
     * (`HERMES_DATA_ROOT` 가 정한다), 이사하면 조용히 낡는다 — 2026-08-31 이사 뒤
     * `50-resources/documents` 로 남아 있었고, 그 폴더는 지워졌는데도 이 줄이 매 질문의
     * 시스템 프롬프트에 그대로 실려 나갔다 (주석이 아니라 `buildDocumentsBrief` 의 출력이다). */
    '# 문서 아카이브 (자료 저장소의 documents/)',
    '',
    '**사람의 발언이 아니라 문서 원문입니다.** 어느 문서를 열지 판단하는 데 쓰세요.',
    '내용은 `search` / `read_document` 로 직접 확인해야 합니다.',
    '`그 외 N건` 은 이 목록에서만 접힌 것이며 **검색에는 그대로 걸립니다** —',
    '찾는 자료가 목록에 안 보여도 없다고 답하지 말고 `search` 를 먼저 쓰세요.',
    /* **이 줄이 `read_document` 로 보내는 것은 2026-09-03 부터다.** 그전에는 `search` 만
     * 적었다 — 머리말 있는 문서 12개 중 넷은 커서 `read_document` 가 전문 대신 목차를
     * 줬고, 그 목차에 머리말이 안 실려서 **여는 길이 없었기** 때문이다(그 넷이 머리말
     * 글자의 65%). 이제 목차 앞에 전문이 붙는다 (`outlineWithPreamble`).
     * **그 함수를 되돌리면 이 줄이 없는 길을 가리킨다** — 함께 봐야 한다. */
    '`상단정리:` 뒤는 그 문서 맨 위 사람 정리의 **제목**입니다 — 전문은 `read_document`.',
    '',
  ];

  /**
   * 시스템 프롬프트에 실리는 문서 색인.
   * 메타 블록 전문을 싣지 않고 문서당 한 줄만 — 슬랙 색인이 이미 10K 를 쓰고 있어서다.
   * 「변환하지 못한 것」 절은 반드시 남긴다. 이게 없으면 봇이 엑셀 안의 숫자를
   * "자료가 없습니다" 로 오답한다.
   *
   * @param {{access:object, projects?:string[], maxChars?:number, missing?:string, parts?:string[], foldedOut?:object[]}} opts
   *   access   열람 권한 (config.js 의 PUBLIC_ACCESS / accessFor / FULL_ACCESS)
   *   projects 훑을 사업장 폴더. 생략하면 전부 (buildDocumentsBriefSplit 이 나눠 부른다)
   *   maxChars 접기 예산
   *   missing  「변환하지 못한 것」 절. 생략하면 이 권한 기준으로 직접 만든다
   *   foldedOut  배열을 넘기면, 이번 호출에서 실제로 접힌 문서 객체들을 거기 채워 넣는다
   *              (반환값은 그대로 문자열 — 시그니처 호환. `indexGauge` 처럼 "무엇이 접혔는지"를
   *              텍스트에서 되짚지 않고 접기 루프의 판정을 그대로 받아야 할 때 쓴다).
   *   visibleOut 배열을 넘기면, 이 access 로 **볼 수 있는(=canSeeDoc 을 통과한) 문서 전부**를
   *              접힘 여부와 무관하게 채워 넣는다. `foldedOut` 만으로 "볼 수 있었는데 접힌
   *              것"과 "애초에 이 access 로 안 보이는 것"을 못 가른다 — 둘 다 foldedOut 에는
   *              안 잡히기 때문이다. `canSeeDoc` 은 export 되어 있지 않아 이 함수 밖에서
   *              다시 판정할 방법이 없으므로, "볼 수 있는 전체"를 이렇게 내보낸다.
   */
  function buildDocumentsBrief({
    access,
    projects = null,
    maxChars = DOC_BRIEF_MAX_CHARS,
    missing = null,
    parts = BRIEF_HEADER,
    foldedOut = null,
    visibleOut = null,
  }) {
    if (!hasDocuments()) return '';

    const docLine = (d) => {
      const kind = d.meta['종류'] || '문서';
      const range = entryDateRange(d.entries);
      const rounds = d.entries.length > 1
        ? (isSheetDoc(d) ? `시트 ${d.entries.length}개` : `${d.entries.length}회차`)
        : null;
      const bits = [range, rounds].filter(Boolean).join(', ');
      /* `주요 항목` 값은 색인에 싣지 않는다 (2026-09-10 설계 — 워크스페이스
       * docs/superpowers/specs/2026-09-10-doc-index-floor-restructure-design.md).
       * 값이 색인에 있으면 봇이 문서를 안 열고 그대로 인용하는 사고가 났고(2026-09-08
       * 실측 2/2), qa.md 는 색인 값으로 답하는 것을 금지한다. 값은 문서 md 상단에 있고
       * 봇은 열어서 최신 값으로 답한다. `주요 항목` 메타 자체는 일일 요약(gist)이 쓴다.
       * 머리말은 **제목만** 붙인다 (WHK 결정 2026-09-03, `archive.js` 의 `preambleOutline`
       * 주석) — 뜻 설명은 BRIEF_HEADER 에 한 번만. */
      const pre = d.preamble ? ` · 상단정리: ${preambleOutline(d.preamble)}` : '';
      return `- [${kind}] ${d.title}${bits ? ` — ${bits}` : ''}${pre}`;
    };

    // 사업장별로 모아 두고, 예산을 넘으면 foldRank 가 큰 것부터 접는다.
    const cutoff = monthsAgo(DOC_BRIEF_RECENT_MONTHS);
    const groups = [];
    let shown = 0;
    for (const project of projects || listProjects()) {
      const docs = listDocuments(project).filter((d) => canSeeDoc(access, d));
      if (!docs.length) continue;
      // 볼 수 없는 비공개 채널 여럿이 가상 이름 하나로 합쳐진다 — 헤더에 이름이 안 나가게.
      const name = maskProject(project, access);
      const existing = groups.find((g) => g.project === name);
      if (existing) existing.docs.push(...docs);
      else groups.push({ project: name, docs, folded: [] });
      shown += docs.length;
    }
    if (!shown) return '';

    const render = () => {
      const body = [];
      /* 전부 접힌 사업장은 헤더+접힘줄 두 줄(약 50자) 대신 맨 아래 한 줄에 이름만 모은다
       * (2026-09-04). 그 두 줄은 안 접혀서 사업장 수만큼 바닥을 영구히 먹었다 — 당시
       * 36곳 중 26곳이 전부 접힌 채 약 1,300자를 차지해 바닥 5,781자 / 상한 6,000자였다.
       * 이름은 남긴다 — 이름이 빠지면 봇이 그 사업장 질문에 검색도 않고 「자료가
       * 없다」고 오답하는, 이 색인이 내내 막아 온 바로 그 사고가 난다. 이름 하나는
       * 약 8자라 사업장이 늘어도 바닥이 두 줄짜리의 1/6로 자란다.
       * 줄머리는 `- 그 외 N건` 을 그대로 쓴다 — 머리말의 「검색에 걸린다」 안내와
       * check-brief-split(접힘 집계 줄 정규화)·check-excel-sheets 가 그 모양을 본다. */
      const merged = [];
      for (const g of groups) {
        if (!g.docs.length && g.folded.length) { merged.push(g); continue; }
        // 엑셀은 시트가 회차 자리에 들어 있다 — 자료 하나이므로 1로 센다.
        const count = (d) => (isSheetDoc(d) ? 1 : d.entries.length);
        const rounds = g.docs.reduce((n, d) => n + count(d), 0)
          + g.folded.reduce((n, d) => n + count(d), 0);
        body.push(`### ${g.project} (문서 ${g.docs.length + g.folded.length} · 회차 ${rounds})`);
        for (const d of g.docs) body.push(docLine(d));
        if (g.folded.length) body.push(foldedLine(g.folded));
        body.push('');
      }
      if (merged.length) {
        const docsN = merged.reduce((n, g) => n + g.folded.length, 0);
        body.push(`- 그 외 ${docsN}건 — 아래 사업장 ${merged.length}곳의 문서로, 검색에는 그대로 걸립니다:`
          + ` ${merged.map((g) => g.project).join(' · ')}`);
        body.push('');
      }
      return [...parts, ...body].join('\n');
    };

    // 접을 후보를 rank 내림차순 · 오래된 것 먼저
    const candidates = [];
    for (const g of groups) {
      for (const d of g.docs) candidates.push({ g, d, rank: foldRank(d, cutoff) });
    }
    candidates.sort((a, b) => b.rank - a.rank || latestDate(a.d.entries).localeCompare(latestDate(b.d.entries)));

    // 「변환하지 못한 것」 절은 볼 수 없는 비공개 채널을 언급한 행을 뺀 뒤 길이를 잰다.
    const missingText = missing ?? redactPrivateMentions(missingSection(), access);

    let out = render();
    for (const c of candidates) {
      if (out.length + missingText.length <= maxChars) break;
      /* 시리즈(rank 0)도 접는다 — 2026-09-10 까지는 여기서 포기했고(`if (c.rank === 0) break`)
       * 그래서 안 접히는 줄이 자랄수록 바닥이 상한을 향해 올라갔다. 정렬이 rank 내림차순 ·
       * 오래된 것 먼저라, 시리즈는 **가장 마지막 순서로, 오래된 것부터** 접힌다.
       * 접힌 시리즈는 indexGauge() 가 세고 check-setup [4/6] 이 이름을 찍는다. */
      c.g.docs = c.g.docs.filter((x) => x !== c.d);
      c.g.folded.push(c.d);
      out = render();
    }

    if (foldedOut) {
      for (const g of groups) foldedOut.push(...g.folded);
    }
    if (visibleOut) {
      for (const g of groups) visibleOut.push(...g.docs, ...g.folded);
    }

    return missingText ? `${out}\n${missingText}\n` : out;
  }

  /**
   * 문서 색인을 **캐시가 걸리는 모양으로** 둘로 나눈다. archive.js 의 buildArchiveBriefSplit 과 짝.
   *
   * 나누는 이유와 「공통은 바이트 단위로 같아야 한다」는 조건은 그쪽 주석에 적혀 있다.
   * 여기서만 다른 것이 **예산**이다 — 지금은 공개 문서와 비공개 문서가 `docBriefMaxChars`
   * 하나를 나눠 쓰기 때문에, 비공개 채널 멤버는 공개 문서가 더 많이 접힌 색인을 본다.
   * 그러면 공개 부분이 사람마다 달라져 캐시가 안 맞는다. 그래서 예산도 나눈다.
   *
   * 부수 효과로 **비공개 채널 멤버가 보는 공개 문서 목록이 지금보다 늘어난다** (예산 경쟁이
   * 사라져서). 상한 자체는 그대로이므로 「색인 여유는 쌓이지 않는다」와 어긋나지 않는다.
   *
   * 승인 문서(`_승인자료`)는 **공통에 가상 이름으로 실리고, 그 폴더가 열리는 사람에게는
   * 추가분에 실명으로 한 번 더** 실린다. 지금 한 건이라 중복은 한 줄이고, 이렇게 해야
   * 멤버가 실명으로도 가상 이름으로도 그 문서를 열 수 있다.
   *
   * @param {{access: object}} opts
   * @returns {{common: string, extra: string}}
   */
  function buildDocumentsBriefSplit({ access }) {
    if (!hasDocuments()) return { common: '', extra: '' };

    const common = buildDocumentsBrief({
      access: PUBLIC_ACCESS,
      maxChars: DOC_BRIEF_MAX_CHARS,
      missing: redactPrivateMentions(missingSection(), PUBLIC_ACCESS),
    });

    // 공개 전용 권한에서는 **실명으로** 안 보이는 사업장 = 비공개 채널 폴더 중 이 권한으로 열리는 것.
    const projects = listProjects().filter((p) => {
      const ch = projectPrivateChannel(p);
      return ch && canSeePrivateChannel(access, ch);
    });

    // 「변환하지 못한 것」 표에서 공통 때 가려졌지만 이 권한에서는 살아나는 행.
    // 판정은 archive.js 와 같은 방식 — redactPrivateMentions 의 줄 단위 거르기를 그대로 쓰되,
    // **한 줄씩 부르지 않는다.** 이 함수는 map 인자를 안 주면 안에서 개명 지도를 뜨는데
    // (fs.statSync — config.js 의 *With 주석이 지목한 비용), 이 팩토리는 지도를 주입받지
    // 않아 넘길 map 이 없다. 가리기는 줄 단위로 독립이므로(줄마다 따로 판정된다) 전체를
    // 권한별로 한 번씩만 걸러도 결과가 같다 — stat 이 줄 수 × 2 회에서 2 회가 된다.
    const missingLines = missingSection().split('\n').filter((line) => line.trim());
    const missingText = missingLines.join('\n');
    const publicKept = new Set(redactPrivateMentions(missingText, PUBLIC_ACCESS).split('\n'));
    const mineKept = new Set(redactPrivateMentions(missingText, access).split('\n'));
    const revived = missingLines
      .filter((line) => !publicKept.has(line) && mineKept.has(line))
      .join('\n');

    if (!projects.length && !revived) return { common, extra: '' };

    const extra = buildDocumentsBrief({
      access,
      projects,
      maxChars: DOC_BRIEF_PRIVATE_MAX_CHARS,
      missing: revived ? `## 변환하지 못한 것 (추가 열람분)\n${revived}` : '',
      parts: [
        '# 문서 아카이브 — 추가 열람분',
        '',
        '위 문서 색인에서 열람 권한 때문에 빠져 있던 자료입니다. **인용해도 됩니다.**',
        '',
      ],
    });

    return { common, extra };
  }

  return { foldedLine, floorParts, indexGauge, buildDocumentsBrief, buildDocumentsBriefSplit };
}
