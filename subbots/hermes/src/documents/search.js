/** Codex R2e: lazy document search factory. Keep byte-identical in Hermes and Clio. */
export function createDocumentSearch({
  limits, DOC_SEARCH_MAX_HITS, DOC_HIT_MAX_CHARS, PARTIAL_HIT_MAX_CHARS, DOC_LIST_MAX, DOC_PREAMBLE_MARK, TRUNC_PHRASE, companyWideDocProjects, hasDocuments, splitTerms, scoreTerms, termHits, PARTIAL_MIN_TERMS, clipPartial, resolveProjectFor, resolveDocumentFor, documentsFor, listDocuments, canSeeDoc, redactPrivateMentions, maskProject, clip, pickSpread, spreadNote, byScoreThenDate, fold, readCached, sectionsOf, isSeriesDoc, latestDate
}) {
  /**
   * '{projectName}' 에 있는 (볼 수 있는) 문서 제목 목록을 사람이 읽을 문장 조각으로 만든다.
   * 0건 구제책(2026-08-06 0건 오답 사고)과 부분 일치 노트(위 partials 분기) 양쪽이 같은 것을 쓴다 —
   * 두 곳에 따로 적으면 한쪽만 고쳐져 갈린다.
   *
   * 제목에도 **가리기를 건다.** canSeeDoc 이 이미 문서 단위로 걸렀으므로 여기 남은 것은
   * 볼 수 있는 문서뿐이지만, 제목 문장 안에 다른 비공개 자리 이름이 섞여 들어올 수 있다.
   * 「내용을 프롬프트에 싣는 경로를 새로 만들면 redactPrivateMentions 부터 확인한다」가
   * 이 저장소의 규칙이고(2026-08-06), 스레드 맥락에서 그걸 한 번 빠뜨렸었다.
   * 제목은 한 줄이라 걸리면 통째로 비고, 그런 것은 목록에서 뺀다.
   */
  function projectDocInventory(projectName, access) {
    const all = documentsFor(projectName, access)
      .filter((d) => redactPrivateMentions(d.title, access).trim());
    const shown = all.slice(0, DOC_LIST_MAX);
    const rest = all.length - shown.length;
    const list = shown.map((d) => d.title).join(' · ') + (rest > 0 ? ` · 그 외 ${rest}건` : '');
    return { all, list };
  }

  /**
   * 문서 전문 검색. 낱말이 모두 한 회차 안에 있어야 매칭(AND) — searchArchive 와 같은 규칙.
   * 회차 하나가 표를 통째로 담아 슬랙 메시지보다 훨씬 길기 때문에
   * 건수(docSearchMaxHits)와 건당 길이(docHitMaxChars)를 따로 제한한다.
   * 0건일 때는 일부만 맞은 것을 대신 돌려준다 (아래 partials 분기, pickSpread 로 사업장마다 나눈다).
   */
  function scanDocuments({
    query,
    project,
    document,
    access,
    maxHits = DOC_SEARCH_MAX_HITS,
    perProject = limits.docSearchMaxPerProject ?? 3,
  }) {
    if (!hasDocuments()) return { hits: [], note: '문서 아카이브가 설정되어 있지 않습니다.' };

    const terms = splitTerms(query);
    if (!terms.length) return { hits: [], note: '검색어가 비어 있습니다.' };

    let docs;
    let projectName = null;
    if (project) {
      const r = resolveProjectFor(project, access);
      if (!r.ok) return { hits: [], note: r.error };
      projectName = r.name;
      if (document) {
        const d = resolveDocumentFor(r.name, document, access);
        if (!d.ok) return { hits: [], note: d.error };
        docs = [d.doc];
      } else {
        docs = documentsFor(r.name, access);
      }
    } else {
      docs = listDocuments();
    }

    // 이름 풀기에서 이미 걸렀지만, 사업장 전체·전 사업장 검색 경로는 여기서 걸린다.
    docs = docs.filter((d) => canSeeDoc(access, d));

    // 상한에 걸려도 **끝까지 훑는다.** 234건 5.4MB 전수 스캔이 41ms 라 비용이 없다.
    // 예전에는 상한에 차는 순간 멈췄고, 훑는 순서가 사업장 이름순이라 밑줄로 시작하는
    // 공통 폴더 3개가 매번 예산을 다 먹었다 — '연체이자' 는 14개 사업장 50건인데
    // 봇은 그 공통 폴더 3개의 12건만 봤다 (2026-08-05 실측).
    const groups = new Map();
    const partials = [];
    const perTerm = new Map(terms.map((t) => [t, 0]));
    let total = 0;
    for (const d of docs) {
      /* 첫 회차 헤더 **앞** 구간을 회차 하나로 앞에 끼운다 (`loadDocument` 의 preamble 주석).
       * `d.entries` 자체는 안 건드린다 — 그 길이로 회차를 세는 자리가 여럿이다.
       * 대화 쪽 `scanArchive` 가 같은 모양으로 한다.
       *
       * 날짜는 그 문서의 **가장 최근 회차 날짜**를 쓴다. 머리말에는 날짜가 없는데
       * `byDateDesc`·`pickSpread` 가 날짜로만 줄을 세우므로 값이 있어야 한다. 이 정리는
       * 회차들을 훑어 「지금까지의 추이」를 적어 둔 것이라 그 문서 안에서 맨 앞이 맞다
       * (회차는 최신순으로 놓이므로 `entries[0]` 이 가장 최근이다 — verify_format 7번).
       * 회차가 하나도 없으면 빈 문자열이라 맨 뒤로 간다 — 없는 날짜를 지어내지 않는다. */
      const units = d.preamble
        ? [{ date: d.entries[0]?.date ?? '', text: `${DOC_PREAMBLE_MARK}\n${d.preamble}` }, ...d.entries]
        : d.entries;
      for (const entry of units) {
        // 문서 md 의 사람이 쓴 메타·주석에도 비공개 채널 이름과 그 내용이 섞여 들어온다.
        // 대화 쪽 searchArchive 와 같이 **가린 뒤에 맞춰본다** — 원문에 맞춰보면 가려진 줄에만
        // 있는 낱말로 검색했을 때 "여기 뭔가 걸린다"는 사실이 샌다.
        const text = redactPrivateMentions(entry.text, access);
        if (!text.trim()) continue;
        const hay = text.toLowerCase();
        const score = scoreTerms(hay, terms);
        if (score === 0) continue;
        for (const t of terms) if (hay.includes(t)) perTerm.set(t, perTerm.get(t) + 1);
        const pname = maskProject(d.project, access);
        // 제목에도 가리기를 건다 — 아래 0건 구제책(projectDocInventory)과 같은 이유다.
        // 걸리면 제목이 통째로 비지만, 히트 자체는 버리지 않는다 — 본문이 요점이다.
        const row = {
          project: pname,
          document: d.slug,
          title: redactPrivateMentions(d.title, access),
          date: entry.date,
          text: clip(text, DOC_HIT_MAX_CHARS),
          ocr: /OCR 사용/.test(d.meta['변환'] || ''),
        };
        if (score < terms.length) { partials.push({ ...row, score }); continue; }
        if (!groups.has(pname)) groups.set(pname, []);
        groups.get(pname).push(row);
        total += 1;
      }
    }

    const hits = pickSpread(groups, maxHits, perProject);

    /* 문서 히트 잘림 안내. **대화 쪽 `archive.js` 의 CAP_HINT·noteWithCapHint 와 같은 모양이다**
     * — 거기 주석이 원본이다. 요지는, 확정 히트의 발췌도 회차 앞 DOC_HIT_MAX_CHARS 에서
     * 잘리는데 그 사실이 note 에 없어서 봇이 발췌를 회차 전체로 읽었다는 것. 값이 그 밖에
     * 있으면 「발췌에 없음」이 「아카이브에 없음」이 됐다 (2026-09-10 재생 2/2 재현).
     *
     * note 가 없을 수 있다 — spreadNote 는 shown >= total 이면 undefined 를 돌려준다.
     * 그냥 이어 붙이면 "undefined 일부 히트는…" 이 나간다. */
    const CAP_HINT = '일부 히트는 길이 제한으로 잘렸습니다(회차 전체 중 앞부분) — 값·표·금액 확인은 read_document 로 전문을 여세요.';
    const noteWithCapHint = (note, list) => {
      if (!list.some((h) => h.text.includes(TRUNC_PHRASE))) return note;
      return note ? `${note} ${CAP_HINT}` : CAP_HINT;
    };

    /* 일부만 맞은 것을 사업장마다 나눠 고른다. 0건일 때와 「적을 때」가 같은 코드를 쓰게 뺐다.
     * 자르기(PARTIAL_HIT_MAX_CHARS)와 정렬 기준은 아래 주석에 그대로 설명돼 있다. */
    const pickPartials = () => {
      if (terms.length < PARTIAL_MIN_TERMS || !partials.length) return [];
      const partialGroups = new Map();
      for (const p of partials) {
        if (!partialGroups.has(p.project)) partialGroups.set(p.project, []);
        partialGroups.get(p.project).push(p);
      }
      return pickSpread(
        partialGroups, limits.partialHitMaxHits ?? 12, perProject, byScoreThenDate,
      ).map((h) => ({ ...h, text: clipPartial(h.text, PARTIAL_HIT_MAX_CHARS, terms), termCount: terms.length }));
    };
    const termCounts = () => terms.map((t) => `'${t}' ${perTerm.get(t)}건`).join(' · ');

    /* **확정 히트가 적으면 일부만 맞은 것도 뒤에 함께 준다** (2026-08-18). 대화 쪽
     * searchArchive 와 같은 이유이고 같은 모양이다 — 거기 주석이 원본이다. 요지는, 안전망이
     * 0건일 때만 켜져 있어서 낱말을 많이 붙인 좁은 질의가 한 곳만 맞히면 봇이 「찾았다」로
     * 읽고 멈췄다는 것. 확정분은 앞에 그대로 두고 partial 플래그도 켜지 않는다. */
    if (hits.length && hits.length <= (limits.partialAlsoWhenAtMost ?? 2)) {
      const extra = pickPartials();
      if (extra.length) {
        const all = [...hits, ...extra];
        return {
          hits: all,
          note: noteWithCapHint(
            `낱말이 **모두** 든 회차가 ${hits.length}건뿐이라, **일부만 맞은** ${extra.length}건을 `
            + `뒤에 함께 실었습니다(제목 줄의 \`(n/${terms.length} 낱말)\`). 낱말별로는 ${termCounts()} 걸립니다. `
            + '뒤쪽은 추측이라 무관한 것이 섞여 있습니다 — 앞의 확정분을 먼저 보고, '
            + '관련 있어 보이는 회차는 조각만으로 답하지 말고 read_document 로 전문을 여세요.',
            all,
          ),
        };
      }
    }

    // 0건이면 **그 사업장에 무엇이 있는지 목록으로 준다.**
    //
    // 안 주면 봇이 "못 찾음"을 "없음"으로 단정한다 — 2026-08-06 에 실제로 났다. 어느 사업장의
    // 주식매매계약서를 두고 "아카이브에 변환되어 있지 않고 파일명만 남아 있다"고 답했는데 그 문서는
    // 8/3 부터 들어와 있었다. 밟은 순서가 이렇다: 먼저 그 계약의 대상·수량·대금을 적은 네 낱말로
    // 찾아 1건이 걸렸는데 그게 **이미 읽은 PRS 계약서**였고(네 낱말이 거기 정의 조항에
    // 다 있다), 이어 `주식매매계약서` 로 찾으니 0건이었다 — 그 md 제목은 종류 이름 가운데에
    // 괄호가 끼어(`주식매매(…)계약서`) 그 글자열이 한 번도 안 나온다. 프롬프트 색인은 상한에 걸려
    // 그 문서가 `그 외 N건` 으로 접혀 있어 **이름조차 안 보였다.** 그래서 "PRS 것만 있다" 로 굳었다.
    //
    // 도구 설명에는 그전부터 "결과가 없으면 낱말을 줄여 다시 시도할 것" 이 적혀 있었다.
    // 그런데도 안 막혔다 — 봇에게 없던 것은 조언이 아니라 **목록**이다. 이름을 보면
    // read_document 로 곧장 연다(그쪽은 이름 일부만 맞아도 열린다).
    /* 부분 일치를 목록보다 먼저 준다 — 제목만 보는 것보다 본문 조각이 낫다.
     * 아래 목록 구제책은 부분 일치조차 0건일 때 그대로 남는다 (2026-08-06 0건 오답 사고).
     *
     * **여기서만 PARTIAL_HIT_MAX_CHARS 로 다시 자른다.** row 를 만들 때는 확정 히트와
     * 똑같이 DOC_HIT_MAX_CHARS(4,000자)로 잘렸는데, 부분 일치는 "이 회차를 열어볼
     * 값어치가 있나" 만 보이면 되는 낮은 확신의 결과이고, 실제로 열어볼지는 read_document 가
     * 전문으로 답한다. 확정 히트와 같은 예산을 주면 헛도는 질의(맞는 것이 하나도 없는 질의)가
     * 비싸진다 — 12건 × 4,000자로 호출 하나가 47,203자까지 간 실측이 있다(2026-08-17).
     * 확정 히트 쪽(위 groups.get(pname).push(row))은 DOC_HIT_MAX_CHARS 그대로 둔다.
     *
     * **자른 만큼 note 에 "전문은 read_document 로" 를 못박는다.** 1,000자로 자르면 발췌는
     * 회차 앞부분뿐이라 답이 뒤쪽에 있으면 안 보인다 — 실호출에서 10,185자 회차의 2,494번째
     * 글자에 있던 금액을 모델이 발췌만 보고 못 찾았다(2026-08-17). read_document 는 6만자까지
     * 주는 탈출구가 이미 있는데, 그걸 쓰라는 말이 없으면 모델은 발췌만으로 답한다. */
    if (!hits.length) {
      /* pickPartials 안에 두 가지가 함께 들어 있다 —
       *  ① 자리 나눠 갖기: 2026-08-05 사고('연체이자' 가 14개 사업장 50건인데 밑줄로 시작하는
       *     공통 폴더 3개 12건만 봤다)가 부분 일치 경로에도 그대로 있었다. 두 낱말 질의는
       *     부분 일치가 전부 score=1 이라 자르는 순서가 사업장 이름순으로 굳는다.
       *  ② 점수 우선(byScoreThenDate): 예전에는 여기서 점수순으로 정렬해 두었는데 pickSpread 가
       *     자리마다 날짜순으로 다시 정렬해 그 정렬이 죽어 있었다 (2026-08-18). */
      const shown = pickPartials();
      // 0건이면 아래 목록 구제책(2026-08-06 0건 오답 사고)으로 그대로 흘려보낸다.
      if (shown.length) {
        let note =
          `낱말이 **모두** 든 회차는 0건입니다. 낱말별로는 ${termCounts()} 걸립니다. ` +
          `아래는 **일부만 맞은** ${shown.length}건입니다 — 무관한 것이 섞여 있을 수 있습니다. ` +
          `발췌는 회차 앞부분만 보여주므로, 관련 있어 보이는 회차는 이 조각만으로 답하지 말고 ` +
          `read_document 로 전문을 열어 확인하세요.`;
        // 사업장을 짚어 물었으면 아래 0건 구제책과 같은 문구·목록을 여기서도 준다 —
        // 부분 일치가 있으면 이 분기가 먼저 return 해서 아래 구제책이 안 닿기 때문이다.
        if (projectName) {
          const { all, list } = projectDocInventory(projectName, access);
          note +=
            ` 0건은 "그런 자료가 없다"는 뜻이 아닙니다. ` +
            (all.length
              ? `${projectName} 에 있는 문서 ${all.length}건 — ${list}. `
              : `다만 ${projectName} 에는 열람 가능한 문서가 실제로 없습니다. `) +
            `여기서 답이 안 보이면 낱말을 줄여 다시 검색하세요.`;
        } else {
          note += ' 여기서 답이 안 보이면 낱말을 줄여 다시 검색하세요.';
        }
        // 점수를 떼지 않고 함께 내보낸다 — 봇이 날짜는 보는데 「몇 낱말 맞았나」는 못 봐서
        // 최신성과 관련성을 저울질할 재료가 없었다. claude.js 가 제목 줄에 적는다.
        return { hits: shown, partial: true, note: noteWithCapHint(note, shown) };
      }
    }

    if (!hits.length && projectName) {
      const { all, list } = projectDocInventory(projectName, access);
      return {
        hits: [],
        note:
          `'${projectName}' 에서 '${query}' 로는 0건입니다. 이 검색은 띄어쓴 낱말이 **모두 한 회차 안에 ` +
          `그대로** 있어야 걸리므로, 0건은 "그런 자료가 없다"는 뜻이 아닙니다. ` +
          (all.length
            ? `${projectName} 에 있는 문서 ${all.length}건 — ${list}. ` +
              `이 중에 있어 보이면 read_document 로 여세요(이름 일부만 적어도 됩니다).`
            : `다만 ${projectName} 에는 열람 가능한 문서가 실제로 없습니다.`),
      };
    }

    return {
      hits,
      note: noteWithCapHint(
        spreadNote(groups, total, hits.length, '더 보려면 project 를 지정해 다시 검색하세요.'),
        hits,
      ),
    };
  }

  /**
   * `project` 로 좁혔는데 그 안에 낱말을 **전부** 맞춘 것이 하나도 없으면, 좁히지 않고 한 번 더
   * 훑어 밖의 상위 몇 건을 `outside` 로 함께 돌려준다.
   *
   * 대화 쪽 `searchArchive` 와 **같은 이유이고 같은 모양이다** — 거기 주석이 원본이다.
   * 요지는, `project` 가 하드 필터라 지정한 사업장 말고는 안 보이는데 통합으로 넓은 호출이
   * 사라져 그 좁아짐을 받아 줄 것이 없어졌다는 것 (2026-08-19).
   *
   * **`document` 를 함께 준 호출에는 발동하지 않는다.** 그건 「이 문서를 보라」는 뜻이라
   * 밖을 뒤지는 것이 어긋난다.
   */
  function searchDocumentsInner(opts) {
    const base = scanDocuments(opts);
    const max = limits.outsideWhenNarrowedMaxHits ?? 0;
    if (!opts.project || opts.document || max <= 0) return base;
    if (base.hits.some((h) => typeof h.score !== 'number')) return base;
    // 사람이 적어 준 문자열이 아니라 푼 이름으로 걸러야 한다 (대화 쪽과 같은 이유).
    const r = resolveProjectFor(opts.project, opts.access);
    if (!r.ok) return base;
    // project 가 없으므로 위 조건에 걸려 되돌아오지 않는다 — 재귀는 한 겹.
    const wide = scanDocuments({ ...opts, project: undefined, document: undefined });
    const outside = wide.hits.filter((h) => h.project !== r.name).slice(0, max);
    return outside.length ? { ...base, outside } : base;
  }

  /**
   * 그 문서의 절 중에 이 사업장 것으로 보이는 절이 있나.
   *
   * **절 제목**을 `fold` 한 것이 `fold(project)` 를 포함하거나 그 역이면 참이다.
   * **손으로 만드는 사업장→지명 표는 절대 두지 않는다** — 이 패키지가 이미 기각한
   * 별칭 사전(H-1)과 같은 모양이 된다. 절이 안 나뉜 문서는(`sections` 가 빈 배열) 그냥
   * 거짓이다 — 그 판정은 호출부(`companyWideCards`)가 `sectionsOf` 를 한 번만 불러
   * 넘겨준다(문서당 두 번 안 파싱한다).
   *
   * **실물에서는 이 fold 규칙이 자주 안 맞는다**(실측 2026-09-11: 전사 종합 문서의 절
   * 제목은 「〈지명〉 〈종류〉」 모양인데, 사업장 폴더 이름은 흔히 지명과 다른 약칭이라
   * 절 제목의 「종류」 쪽 글자와 안 겹친다 — 사업장 39개 중 fold 로 맞는 것은 13개뿐이었다).
   * 그래서 이 함수가 거짓을 내도 `companyWideCards` 가 폴백을 쓴다 — 그 폴백의 이유와
   * 실측 근거는 그쪽 주석이 원본이다.
   */
  function hasProjectSection(sections, project) {
    if (!sections.length) return false;
    const p = fold(project);
    if (!p) return false;
    return sections.some((s) => {
      const f = fold(s.name);
      // 절 제목이 fold 뒤 빈 문자열이면(`**1. -**` 처럼 글자다운 글자가 없는 제목)
      // `p.includes(f)` 가 빈 문자열을 항상 포함해 **어떤 사업장에도 참**이 된다.
      // 에러 없이 엉뚱한 문서를 1번 칸에 앉히는 자리라 여기서 먼저 끊는다(2026-09-11 고침).
      if (!f) return false;
      return f.includes(p) || p.includes(f);
    });
  }

  /**
   * 전사 종합 문서 카드 — 사업장으로 좁힌 검색이 빈손(확정 0건)일 때, 여러 사업장을 한
   * 문서에 담는 종합 보고서를 **발췌 없이** 가리켜 준다.
   *
   * 왜 발췌가 없나(2026-09-11 실측 둘): ① 이런 문서(40만 자급)의 발췌 = 앞 4,000자에는
   * 답이 안 실린다 ② 그 발췌는 항상 문서 머리 = 특정 사업장 구간이라, 다른 사업장 질문에
   * 실으면 남의 숫자가 흘러드는 오염원이다(타 사업장 절의 「(195 억)」이 실제 함정이었다).
   * 카드가 주는 정보는 「이 문서가 있다, 절로 열라」뿐이고, 그거면 된다.
   *
   * 왜 히트가 아니라 별도 필드인가: 확정 히트로 세면 outside·0건 구제·autoNarrow 폴백이
   * 전부 꺼진다 — 좁힘 질의 43건 중 16건이 그 안전망 수혜자다(실측). 카드는 그 판정
   * 어디에도 안 낀다.
   *
   * 정렬 — **하이브리드**(2026-09-11 확정, Task 3 의 세 라운드 실측):
   *   1번 칸: 그 사업장 절을 가진 전사 문서(여럿이면 최신).
   *   2번 칸부터: 낱말 출현 횟수 합 ÷ (문서 글자수/10,000) 내림차순 → 비시리즈 → 최신.
   *
   * 왜 길이로 나누나: 이 폴더의 정답 문서는 402,057자로 2위의 4배다. 길이 보정이 없으면
   * 어떤 낱말 신호든 「가장 큰 문서가 이긴다」가 되고, 그건 신호가 아니라 상수다 —
   * 회의적 검증의 결정 실험에서 **일부러 틀린 사업장 이름을 줘도 같은 결과**가 나왔다.
   *
   * 왜 1번 칸을 따로 두나: 그래도 그 큰 문서가 정답인 경우가 실제로 많다(사고 13건 중 11건).
   * 1번 칸을 구조로 고정해 그 경우를 보장하고, **나머지 칸을 관련성에 내준다** —
   * 상한을 1로 줄이면 그 한 칸이 구조에 먹혀 다른 문서가 정답인 14건에서 적중 0/14 였다.
   *
   * **1번 칸의 폴백(2026-09-11, 실물 확인 후 적용)**: `hasProjectSection` 의 fold 규칙은
   * 사업장 39개 중 13개에서만 맞는다 — 절 제목이 지명 약칭과 안 겹치는 문서가 더 많다.
   * 손으로 만드는 사업장→지명 표는 두지 않으므로(별칭 사전 H-1 과 같은 모양이 된다),
   * fold 매칭이 이 풀 전체에서 하나도 안 걸리면 **「절로 나뉘는 전사 문서 중 최신」**으로
   * 내려간다 — 회의적 검증(3라운드)이 이 폴백도 사고 회복이 fold 매칭과 같은 값
   * (11/13)임을 실측했다.
   */
  function companyWideCards({ query, project, access, max = 2 }) {
    if (!companyWideDocProjects.length || !project) return [];
    const r = resolveProjectFor(project, access);
    // 전사 폴더 자체를 검색 중이면 카드가 자기 자신을 가리킨다 — 소음이라 안 낸다.
    if (r.ok && companyWideDocProjects.includes(r.name)) return [];

    // 중복 낱말은 한 번만 센다 — `termHits` 는 넘겨준 목록을 그대로 돌므로, 여기서 안 걸러
    // 주면 "잔액 …잔액" 같은 질의에서 중복된 낱말만 이중으로 세어 순위가 왜곡된다.
    // `shouldOfferCards` 가 발동 판정에서 이미 고유화한 것과 같은 이유 — 일관성을 위해
    // 맞춘다(2026-09-11 실측: 고유화해도 A·B·잠식 세 축이 전부 그대로였다 — 중복 낱말이
    // 낀 43건 중 8건에서도 카드 구성이 한 건도 안 갈렸다).
    const terms = [...new Set(splitTerms(query))];
    const pool = [];
    for (const folder of companyWideDocProjects) {
      for (const d of listDocuments(folder)) {
        if (!canSeeDoc(access, d)) continue;
        let text;
        try { text = readCached(d.abs); } catch { continue; }
        // 가린 뒤에 맞춰본다(1195-1198 행과 같은 이유) — 원문에 맞추면 가려진 줄에만 있는
        // 낱말로 검색했을 때 "여기 뭔가 걸린다"는 사실이 카드의 존재·순위로 샌다.
        const safeText = redactPrivateMentions(text, access);
        const hits = termHits(safeText.toLowerCase(), terms);   // 출현 「횟수」 합 — search-terms.js 주석 참고
        const sections = sectionsOf(safeText) || [];
        const owns = hasProjectSection(sections, r.ok ? r.name : project);
        pool.push({ folder, d, text, hits, owns, sectioned: sections.length > 0, sectionsLen: sections.length });
      }
    }

    // 길이 정규화 — 바닥은 **길이 0 나눗셈 방지로만** 둔다(2026-09-11 고침). 원래 있던
    // `Math.max(1, len/10000)` 는 10,000자 미만 문서의 분모를 1로 깎아, 9,237자짜리 정답
    // 문서(40.000)가 27,863자짜리 비정답 문서(40.914)에 근소하게 밀리는 것을 실측했다 —
    // 바닥을 빼면 43.304 로 제 점수를 받아 이긴다. 세 축(A·B·잠식) 실측에서 바닥 없는 쪽이
    // 전부 동률 이상이었고 B 만 8/14→9/14 로 회복됐다. 짧은 문서가 부당히 이기는 새 역전은
    // 실측 범위(43+13건)에서 하나도 안 생겼다.
    const norm = (p) => p.hits / (Math.max(1, p.text.length) / 10000);
    const byRelevance = (a, b) =>
      (norm(b) - norm(a))
      || (Number(isSeriesDoc(a.d)) - Number(isSeriesDoc(b.d)))
      || String(latestDate(b.d.entries) || '').localeCompare(String(latestDate(a.d.entries) || ''));

    // 1번 칸 — 그 사업장 절을 가진 문서 중 최신. fold 매칭이 하나도 없으면 「절로 나뉘는
    // 문서 중 최신」으로 폴백한다(위 주석 참고). 그래도 없으면 이 칸을 비우고 전부
    // 관련성으로 채운다.
    const byLatest = (a, b) => String(latestDate(b.d.entries) || '').localeCompare(String(latestDate(a.d.entries) || ''));
    const owning = pool.filter((p) => p.owns);
    const structuralPool = owning.length ? owning : pool.filter((p) => p.sectioned);
    const structural = structuralPool.sort(byLatest)[0];

    const rest = pool
      .filter((p) => p !== structural && p.hits > 0)  // 낱말이 하나도 없는 문서를 가리키면 헛걸음이다
      .sort(byRelevance);

    const picked = [structural, ...rest].filter(Boolean);
    // sectionsLen 은 위 루프에서 이미 sectionsOf(text) 를 한 번 파싱해 둔 값이다 — 여기서
    // 다시 부르면 40만 자 문서를 줄 단위로 두 번 훑는다(2026-09-11 고침, 이중 파싱 제거).
    // 제목이 가려져 통째로 비면 뺀다(2026-08-06 규칙, 선례 projectDocInventory:1127) —
    // 제목이 전부인 카드에서 빈 제목은 봇이 read_document 로 부를 이름이 없는 막다른 카드다.
    //
    // **거르기가 자르기보다 먼저다**(선례 1127 과 같은 순서, 2026-09-11 이월 Minor 고침).
    // 뒤집으면 1번 칸 문서의 제목이 통째로 가려졌을 때 그 자리가 빈 채로 잘려 나가, 카드가
    // 상한(2장)보다 적게 — 실물에서는 1장만 — 나간다. 방향은 안전하지만(적게 나감) 그만큼
    // 다음 후보가 조용히 버려진다.
    return picked.map(({ folder, d, text, sectionsLen }) => ({
      project: maskProject(folder, access),
      title: redactPrivateMentions(d.title, access),
      date: latestDate(d.entries) || '',
      chars: text.length,
      sections: sectionsLen || null,
    })).filter((c) => c.title.trim()).slice(0, max);
  }

  /** 카드를 낼 자리인가 — 「봇이 자료가 없다고 닫을 자리」만 고른다.
   *
   * 확정 히트가 0건이라는 것만으로는 부족하다(2026-09-11 실측): 그 조건은 사업장-좁힘
   * 질의 43건 중 20건에서 켜지는데, 그 20건은 **자기 사업장 발췌를 9~12개씩 이미 받고
   * 있었다.** 답이 잘 나오던 자리에 40만 자 문서를 가리키는 카드가 끼어드는 셈이다.
   *
   * 그래서 둘 중 하나를 더 본다.
   *   ① 부분 일치조차 거의 없다 (3건 이하)
   *   ② 질의 낱말 중 **로컬 문서 어디에도 없는 낱말**이 있다 — 「이 사업장 자료만으로는
   *      답할 수 없는 질문」의 신호다. 실패 질의 11건 중 6건이 바로 이 모양이었다
   *      (부분 일치 12건을 받고도 봇이 「없습니다」로 닫던 자리).
   *
   * 이 조건은 현행 「확정 0건」을 **지배한다** — 사고 회복은 11/13 으로 똑같은데 잠식
   * 발동이 20/43 → 5/43 이다. 문턱 3 은 고른 값이 아니다: 실패 질의의 히트 수가
   * {0,0,0,2,2,2,12…}, 잠식이 {9,9,9,11,12…} 라 2와 9 사이가 비어 있고 2~8 어디든 같다. */
  const CARD_PARTIAL_MAX = 3;

  function shouldOfferCards(inner, { query, project, access }) {
    if (inner.hits.some((h) => typeof h.score !== 'number')) return false; // 확정이 있으면 끝
    if (inner.hits.length <= CARD_PARTIAL_MAX) return true;                // ①
    const terms = splitTerms(query);
    if (!terms.length) return false;
    // 중복 낱말은 한 번만 센다 — `splitTerms` 는 안 걸러 준다("잔액 …잔액" 같은 질의가
    // 실물에 있다). 안 그러면 seen.size 가 고유 낱말 수를 넘지 못해 「전부 로컬에 있다」
    // 판정이 영영 안 되고, 실제로는 다 있는데도 카드가 항상 켜진다(2026-09-11 실측).
    const need = new Set(terms).size;
    // **`resolveProjectFor` 로 푼 이름을 넘겨야 한다.** opts.project 원문을 그대로
    // listDocuments 에 주면 별칭·부분 이름에서 빈 목록이 나오고, 그러면 「로컬에 아무
    // 낱말도 없다」로 읽혀 카드가 항상 켜진다 — 검사 [8]이 그것을 잡는다.
    const r = resolveProjectFor(project, access);
    const localProject = r.ok ? r.name : project;
    const seen = new Set();
    for (const d of listDocuments(localProject)) {        // ② 로컬에 아예 없는 낱말이 있나
      if (!canSeeDoc(access, d)) continue;
      let text;
      // 여기도 가린 뒤에 맞춘다 — 안 그러면 가려진 줄에만 있는 낱말 때문에 「로컬에 다
      // 있다」로 잘못 읽혀 카드가 안 뜨는 반대 방향 오작동이 생긴다.
      try { text = redactPrivateMentions(readCached(d.abs), access).toLowerCase(); } catch { continue; }
      for (const t of terms) if (!seen.has(t) && text.includes(t)) seen.add(t);
      if (seen.size === need) return false;     // 전부 로컬에 있다 — 카드 안 낸다
    }
    return true;
  }

  /**
   * 카드는 여기서 붙인다 — Inner(로컬 스캔·outside·note)는 한 글자도 안 바뀐다.
   * outside 와 겹치지 않는다 — outside 는 날짜순 상위라 종합 문서가 실린 적이 없고(실측),
   * 바로 그 날짜순 때문에 카드를 outside 변형으로 만들면 죽는다. 그래서 별도 필드다.
   */
  function searchDocuments(opts) {
    const r = searchDocumentsInner(opts);
    if (r.error || !opts.project || opts.document) return r;
    // 킬 스위치 — 목록이 비어 있으면 **여기서 곧바로 반환**한다. `shouldOfferCards` 의 C7
    // 로컬 재스캔(사업장 문서 전부를 소문자로 복사)이 companyWideCards 안의 킬 스위치보다
    // 앞서 돌아, 목록을 비워도 그 재스캔만은 계속 돌고 있었다(2026-09-11 성능·비용 검토
    // Important 2 — config.json 주석의 「비우면 통째로 꺼진다」와 실제가 달랐다).
    if (!companyWideDocProjects.length) return r;
    // `scanDocuments` 는 이름 풀기 실패를 `error` 가 아니라 `note` 로 돌려준다(1157행) —
    // 위 `r.error` 검사는 이 경로에서 영원히 안 걸린다. 여기서 직접 풀어, 못 찾은 사업장
    // 이름이나 막힌 비공개 사업장(BLOCKED_NOTE)에 카드가 붙는 것을 막는다(2026-09-11 고침).
    if (!resolveProjectFor(opts.project, opts.access).ok) return r;
    if (!shouldOfferCards(r, opts)) return r;
    const cards = companyWideCards(opts);
    return cards.length ? { ...r, cards } : r;
  }

  return { searchDocuments, companyWideCards };
}
