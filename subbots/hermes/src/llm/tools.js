/**
 * Q&A tool definitions and result rendering, extracted by Codex.
 * Real SDK betaTool conversion is retained. Readers and Slack boundaries are
 * supplied by the facade; tools and request records are created per buildTools.
 */
import { betaTool } from '@anthropic-ai/sdk/helpers/beta/json-schema';

export function createToolBuilder({
  config, canSee, canSeePrivateChannel, isPrivateChannel, matchesHiddenPrivate,
  BLOCKED_NOTE, truncMarker, searchArchive, readChannel, resolveChannel,
  listReadableChannels, searchDocuments, readDocument, hasDocuments,
  markArchivedAttachments, narrowableProjects, resolveProject, detectPlace,
  fetchWindow, formatTranscript, recentWindow, listBotChannels, DEFAULT_LIVE_FETCH_MAX_DAYS,
}) {
  /* ── 툴 ───────────────────────────────────────────────────────── */

  /* 대화와 문서를 한 도구로 부르되 **결과는 갈라 둔다.**
   *
   * 나눠 뒀던 이유 셋(예전 주석)이 여기서 그대로 지켜진다 —
   *  - 사람 발언과 문서 원문이 한 덩이로 섞이지 않는다 (구역 제목이 가른다)
   *  - 히트 예산은 각자 쓴다 (searchArchive 40건 / searchDocuments 확정 히트 12건 × 4,000자,
   *    부분 일치는 12건 × 1,000자)
   *  - 되돌릴 때는 이 함수와 search 블록만 떼고 예전 두 블록을 되살리면 된다
   */
  /* 부분 일치 히트의 제목 줄에 붙는 「2/3 낱말」. 확정 히트에는 안 붙는다(다 맞았으니 뻔하다).
   *
   * 순서만 점수순으로 바꾸고 점수를 안 보이면 봇은 **왜** 그 순서인지 모른 채 앞쪽을 믿는다.
   * 날짜는 본문 헤더에 이미 보이므로, 점수를 함께 주면 「옛것인데 잘 맞음」과 「최신인데 덜
   * 맞음」을 봇이 직접 저울질할 수 있다 (WHK 결정 2026-08-18). 12건 기준 70자쯤이다. */
  function matchLabel(h) {
    return typeof h.score === 'number' && typeof h.termCount === 'number'
      ? ` (${h.score}/${h.termCount} 낱말)`
      : '';
  }

  function sectionText(title, hits, note, partial, render) {
    const head = `## ${title}`;
    if (!hits.length) return `${head}\n${note || '0건입니다.'}`;
    const flag = partial ? ' — **일부만 맞은 결과**' : '';
    const body = hits.map(render).join('\n\n');
    return `${head} ${hits.length}건${flag}\n${note ? `(${note})\n\n` : ''}${body}`;
  }

  /* `where` 는 대화·문서 양쪽에 함께 건네지는데, 이름이 한쪽에만 있으면(예: 문서 폴더가 없는
   * 채널) 없는 쪽에서 resolveChannelFor/resolveProjectFor 가 "…을 찾지 못했습니다. 후보: …"
   * 를 돌려준다 — 있는 쪽이 실제로 뭔가 찾아냈을 때만 그 옆의 이 노트는 순수한 잡음이다
   * (2026-08-17 리뷰 finding 4).
   *
   * **줄이는 것은 반대쪽(otherHasHits)이 실제로 히트가 있을 때뿐이다.** 양쪽 다 0건이면
   * (이름을 통째로 잘못 썼거나 오타를 냈을 때) 이 "후보:" 목록이 모델이 같은 호출 안에서
   * 스스로 고쳐 다시 물을 수 있는 유일한 단서다 — 지워버리면 그 왕복 한 번이 그대로
   * 되살아난다(이 작업 전체가 없애려던 바로 그 비용이다, 2026-08-17 리뷰 fix round 2).
   *
   * **차단(BLOCKED_NOTE)은 절대 건드리지 않는다** — `note === BLOCKED_NOTE` 는 그 상수와의
   * 동일성 비교이지 문자열 일부 매칭이 아니라서, 권한 차단 문구를 잡음으로 착각해 지울 수
   * 없다. "후보:" 는 resolveChannelFor·resolveProjectFor·resolveDocumentFor 세 "못 찾음"
   * 오류에만 붙는 표식이고(archive.js·documents.js), 0건이어도 권한이 있어 실제로 뒤지고 난
   * 뒤 나오는 안내문(예: 문서 목록을 보여주는 2026-08-06 0건 오답 사고 방지문)에는 없다.
   */
  function quietNotFoundNote(hits, note, label, otherHasHits) {
    if (!otherHasHits || hits.length || !note || note === BLOCKED_NOTE) return note;
    if (!note.includes('후보:')) return note;
    return `이 이름은 ${label} 쪽에는 없습니다.`;
  }

  /**
   * 질의에 자리 이름이 들어 있으면 **검색이 스스로 좁힌다.**
   *
   * 봇은 `where` 를 검색의 3분의 1에서만 쓴다(통합 전 34% · 후 33%, 2026-09-03 실측이라
   * 통합으로 바뀐 값이 아니다 — 처음부터 그랬다). 안 좁히면 상한을 자리들이 나눠 갖는
   * `pickSpread` 때문에 지목한 자리가 한 칸만 받는다. 실측(팀 로그의 실제 질의 78개 중
   * 자리 이름이 든 30개) — 돌아온 히트 중 지목한 자리 것이 **대화 22% → 83% · 문서
   * 13% → 83%** 로 오르고 결과 자수가 **25% 준다.** 히트 수는 오히려 는다(대화 11.8 → 12.1).
   *
   * **볼 수 있는 자리 안에서만 푼다.** 전체 목록으로 풀면 볼 수 없는 비공개 채널로 좁혀져
   * `resolveChannelFor` 가 `BLOCKED_NOTE` 를 돌려주고, 질문자가 이름을 댄 자리에서 그 자리가
   * 있다는 것이 드러난다 (`prompts/qa.md` 의 「이름을 댄 자리에는 늘 같은 한 문장으로」).
   *
   * 되돌리기는 `config.json` 의 `limits.autoNarrow` 를 `false` 로 두면 된다. 값이 없으면
   * 켜진 것으로 본다 — 새 팀의 설정에 이 줄이 없어도 같은 동작을 하게.
   *
   * @returns {{channel: string|null, project: string|null}}
   */
  function autoNarrow(query, access) {
    if (config.limits.autoNarrow === false) return { channel: null, project: null };
    const channels = listReadableChannels().filter((c) => canSee(access, c));
    const projects = narrowableProjects(access);
    return {
      channel: detectPlace(query, channels, resolveChannel),
      project: hasDocuments() ? detectPlace(query, projects, resolveProject) : null,
    };
  }

  /* 검사가 실제 도구를 그대로 불러 결과 글자를 보려고 내보낸다 — 그리는 규칙을 소스 문자열로
   * 짐작하지 않고 봇이 받는 것과 같은 것을 잰다 (scripts/check-outside-hits.js 의 [7/8]). */
  /* `narrows` 는 **기본값이 있어야 한다** — 이 함수를 부르고 search 의 `run` 을 실제로 돌리는
   * 검사가 셋이고(check-auto-narrow · check-doc-card-render · check-outside-hits) 그쪽은
   * 좁힘 로그에 관심이 없어 안 넘긴다. 기본값이 없으면 아래 도구 래퍼가 `undefined.push` 로 죽는다. */
  function buildTools({ access, slackClient, touched, sizes, narrows = [] }) {
    const tools = [
      betaTool({
        name: 'search',
        description:
          '아카이브 전문 검색. 슬랙 대화와 문서 본문을 **한 번에** 훑어 두 구역으로 돌려준다. ' +
          '공백으로 구분한 낱말이 모두 들어 있는 것을 찾고(AND 조건), 모두 든 것이 없으면 ' +
          '일부만 맞은 것을 겹친 개수 순으로 함께 준다 — 그때는 제목 줄에 `(2/3 낱말)` 처럼 ' +
          '몇 개를 맞췄는지가 붙으니, 날짜와 함께 보고 무엇을 열어 볼지 정할 것. ' +
          '그러니 결과가 없다고 낱말을 바꿔 ' +
          '다시 부르지 말고, 돌려받은 낱말별 건수를 보고 판단할 것. ' +
          '무엇을 찾든 여기서 시작한다.',
        inputSchema: {
          type: 'object',
          properties: {
            query: { type: 'string', description: '검색 낱말들. 예: "대출 만기 연장 협의"' },
            where: { type: 'string', description: '사업장(=채널)으로 좁힐 때만. 예: "사업장나"' },
            document: { type: 'string', description: '특정 문서로 좁힐 때만. where 와 같이 준다.' },
            only: {
              type: 'string',
              enum: ['archive', 'documents'],
              description: "한쪽만 볼 때만. 'archive' 는 사람 발언, 'documents' 는 문서 원문.",
            },
          },
          required: ['query'],
        },
        run: async ({ query, where, document, only }, ctx) => {
          /* 좁힘 정보를 담는 칸. **미는 자리는 아래 도구 래퍼다** — 모든 도구 호출이
           * 반드시 지나는 한 자리에서, 이 본문에 들어가기 전에 tool_use 의 id 와 함께
           * 민다(왜 그 자리인지는 래퍼 주석이 원본이다). 여기서는 래퍼가 `ctx.narrow` 로
           * 넘겨준 그 칸을 채우기만 한다. 기본값 `{}` 는 래퍼 없이 run 을 직접 부를 때를
           * 위한 것이다 — 그때는 기록할 배열이 없으니 버려지는 객체에 적는다. */
          const narrow = ctx?.narrow ?? {};

          // 두 구역을 먼저 **모아만** 두고 렌더링은 나중에 한다 — quietNotFoundNote 가
          // "반대쪽이 실제로 히트가 있었나" 를 알아야 하는데, 그건 양쪽을 다 부른 뒤에만
          // 안다(2026-08-17 리뷰 fix round 2).
          const sections = [];
          // 「다른 사업장에서도」 — 본 구역들이 다 나온 **뒤에** 붙는다. 앞뒤가 바뀌면 봇이
          // 좁혀서 찾은 것보다 밖의 추측을 먼저 읽는다 (부분 일치 안전망과 같은 원칙).
          const outsides = [];

          /* 봇이 안 좁혀 불렀으면 질의 안의 자리 이름으로 **대신 좁힌다** (`autoNarrow` 주석).
           * 봇이 스스로 `where` 를 준 자리는 건드리지 않는다 — 그건 봇의 판단이다.
           * 대화와 문서는 자리 이름이 서로 다를 수 있어(채널에는 있고 문서 폴더에는 없거나
           * 그 반대) **따로 잡는다.** */
          /* `document` 만 오고 `where` 가 없는 호출에는 손대지 않는다. 지금 그 조합에서는
           * `document` 가 아무 일도 안 하는데(documents.js 는 project 가 있어야 그것을 쓴다),
           * 여기서 자리를 채워 주면 **문서 하나로 좁히는 동작이 새로 생긴다.** 재 본 적이 없는
           * 변화라 이번 범위 밖으로 둔다. */
          const auto = (where || document)
            ? { channel: null, project: null }
            : autoNarrow(query, access);
          const chWhere = where || auto.channel || undefined;
          const pjWhere = where || auto.project || undefined;

          // document 만으로는 searchDocuments 도 좁혀지지 않는다(project=where 가 있어야
          // document 가 쓰인다 — documents.js 확인). where 없이 document 만 왔다고 대화
          // 구역을 꺼버리면 좁아지는 것 없이 대화 쪽만 잃는다. 둘 다 있어야 "문서 하나를
          // 짚었다"는 뜻이므로 그때만 대화 구역을 끈다(2026-08-17 리뷰 finding 3).
          if (only !== 'documents' && !(document && where)) {
            /* 스스로 좁혔는데 **아무것도 못 받으면 안 좁힌 결과로 되돌아간다.** 이름을 잘못
             * 짚었을 때 있는 자료가 통째로 사라지는 것을 막는 자리다 — 봇이 준 `where` 에는
             * 안 한다(그건 봇이 그 자리만 보겠다고 정한 것이다). 실측 30건 중 1건이 여기 걸렸다. */
            let r = searchArchive({ query, channel: chWhere, access });
            if (auto.channel && ![...r.hits, ...(r.outside || [])].length) {
              r = searchArchive({ query, access });
              auto.channelFellBack = true;
            }
            const { hits, note, partial, outside } = r;
            hits.forEach((h) => touched.add(h.channel));
            // 첨부 이름 뒤에 「본문이 아카이브에 있다」를 표시해 준다. 이름만 보이면 모델은
            // 「본문은 첨부 안에」로 읽는데, 채널 md 의 첨부 626건 중 394건(62.9%)은 이미
            // 변환돼 있어 그 문장이 열에 여섯 틀린다 (2026-08-18 실제 오답).
            const render = (h) => `### #${h.channel}${matchLabel(h)}\n${markArchivedAttachments(h.text, access)}`;
            sections.push({ title: '대화 (사람 발언)', label: '대화', hits, note, partial, render });
            if (outside?.length) {
              // 밖의 것도 근거 줄에 적는다 (WHK 결정 2026-08-19) — 이 코드는 「봇에게 보여 준
              // 것」을 근거로 적고, 밖의 것도 봇이 본다.
              outside.forEach((h) => touched.add(h.channel));
              outsides.push({ title: '다른 사업장에서도 (대화)', hits: outside, render, where: chWhere });
            }
          }

          if (only !== 'archive' && hasDocuments()) {
            let r = searchDocuments({ query, project: pjWhere, document, access });
            if (auto.project && ![...r.hits, ...(r.outside || [])].length) {
              r = searchDocuments({ query, document, access });
              auto.projectFellBack = true;
            }
            const { hits, note, partial, outside } = r;
            hits.forEach((h) => touched.add(`📄 ${h.project}/${h.document}`));
            const render = (h) => `### ${h.project} / ${h.title}${h.ocr ? ' (OCR 추출)' : ''}${matchLabel(h)}\n${h.text}`;
            sections.push({ title: '문서 (원문)', label: '문서', hits, note, partial, render });
            if (outside?.length) {
              outside.forEach((h) => touched.add(`📄 ${h.project}/${h.document}`));
              outsides.push({ title: '다른 사업장에서도 (문서)', hits: outside, render, where: pjWhere });
            }
            if (r.cards?.length) {
              /* 전사 종합 카드 — 발췌를 일부러 안 싣는다(문서 머리 = 엉뚱한 사업장 구간이
               * 오염원, companyWideCards 주석이 원본). outside note 와 문구 역할을 가른다:
               * outside 는 「남의 자료니 읽지 말라」, 카드는 「이 안에 네 사업장 절이 있다」. */
              // 카드마다 절이 있는지가 다르다 — 있으면 목차만(수천 자), 없으면 열면 본문이
              // 통째로 온다(실측 최악 51,154자=38,268토큰=$0.24, 2026-09-11 성능·비용 검토
              // Important 1). 안내를 구역 공통 note 하나로 두면 절 없는 카드에도 「절 목차를
              // 열어 확인하세요」가 붙어 ⓐ 열 절이 없는데 절을 열라고 지시하고 ⓑ 봇이 그대로
              // 따르면 본문이 통째로 온다 — 그래서 안내를 **카드별로** render 안에 넣는다.
              // 이미 카드에 있는 chars·sections 를 그대로 쓴다(다시 계산하지 않는다).
              outsides.push({
                title: '전사 종합 문서 (발췌 없음 — 카드만)',
                hits: r.cards,
                note: `아래는 여러 사업장을 한 문서에 담은 **전사 종합 보고서**입니다 — `
                  + `${pjWhere} 의 값도 이 안에 들어 있을 수 있습니다. `
                  + `이 목록으로 답하지 말고, 카드마다 붙은 안내를 보고 여세요.`,
                partial: false,
                // 날짜가 빈 카드(회차 헤더에 날짜가 없는 문서)는 **가운뎃점을 안 찍는다** —
                // 안 그러면 「제목 ·  · 12,345자」처럼 빈칸이 낀 줄이 모델에게 간다
                // (2026-09-11 이월 Minor 고침). 실물 픽스처는 전부 날짜가 있어 이 갈래는
                // 검사에 안 잡힌다 — 픽스처를 하나 더 넣으면 카드 상한 2장 순위를 재는
                // [2/5]·[5/5]가 함께 흔들려서다.
                render: (c) => `- ${c.project} / ${c.title}${c.date ? ` · ${c.date}` : ''} · `
                  + `${c.chars.toLocaleString('en-US')}자${c.sections ? ` · 절 ${c.sections}개` : ''}`
                  + (c.sections
                    ? ' — read_document 로 절 목차를 열어 확인하세요.'
                    : ` — 절이 나뉘어 있지 않아 read_document 로 열면 본문 ${c.chars.toLocaleString('en-US')}자가 통째로 옵니다.`),
                where: pjWhere,
              });
            }
          }

          // only·document 조합에 따라 두 구역이 다 안 돌 수 있다(예: only:'archive' 인데
          // document 까지 준 경우, only:'documents' 인데 문서 아카이브가 아예 없는 경우).
          // 빈 문자열을 그대로 돌려주면 모델이 "아무 것도 없다"로 읽는다 — 안 찾아본 것과
          // 없는 것은 다르다(2026-08-17 리뷰 finding 2).
          if (!sections.length) {
            // 좁힘 칸은 도구 래퍼가 이미 밀었다 — 여기서 따로 밀지 않는다.
            return '이 조합으로는 아무 것도 검색하지 않았습니다 — 아카이브에 없다는 뜻이 아닙니다. ' +
              'only 나 document 를 빼고 다시 불러 보세요.';
          }

          // 각 구역 기준 "반대쪽" 은 나머지 구역들 중 하나라도 히트가 있었는가다. 이 구역
          // 자체가 0건이 아니면 quietNotFoundNote 가 그 사실만으로 바로 통과시키므로,
          // sections 전체의 히트 여부만 봐도 된다(최대 두 구역뿐이라 그 이상 가를 필요가 없다).
          const anySectionHasHits = sections.some((s) => s.hits.length);
          const rendered = sections.map((s) => sectionText(
            s.title, s.hits,
            quietNotFoundNote(s.hits, s.note, s.label, anySectionHasHits),
            s.partial, s.render,
          ));

          // 조사 없이 붙는 말로 쓴다 — where 는 사람이 적은 문자열이라 받침 유무를 알 수 없다
          // (받침이 있으면 '…으로', 없으면 '…로' 라 한쪽으로 고정하면 반드시 어색해진다).
          // "에서는" 은 양쪽 다 자연스럽다.
          //
          // **자리 이름은 구역마다 다를 수 있다** — 스스로 좁힐 때 대화와 문서를 따로 잡기
          // 때문이다. 하나로 고정해 두면 문서 구역에 대화 쪽 이름이 적힌다.
          const outsideNote = (w) => `${w} 에서는 낱말을 다 맞춘 것이 없어, 좁히지 않고 찾은 것을 `
            + `함께 실었습니다. **다른 사업장 자료입니다** — ${w} 것으로 읽지 마세요.`;
          /* 밖 구역이 **전부** 부분 일치일 때는 머리에도 표시를 단다. 항목마다 `(1/4 낱말)` 은
           * 붙지만 구역 머리가 비어 있으면, 네 낱말 중 하나만 걸린 줄이 확정 결과와 같은
           * 모양으로 「3건」 목록 안에 앉는다 (실측: 발동 15건 중 4건이 이 상태).
           * 확정이 하나라도 섞였으면 안 단다 — 그 표시는 「전부 추측」이라는 뜻이라
           * (archive.js 의 partial 플래그와 같은 규칙) 붙이면 거짓이 된다. */
          rendered.push(...outsides.map((s) => sectionText(
            s.title, s.hits,
            s.note ?? outsideNote(s.where),
            s.partial ?? s.hits.every((h) => typeof h.score === 'number'),
            s.render,
          )));

          /* 스스로 좁혔으면 **그 사실을 맨 앞에 말한다.** 안 말하면 봇은 아카이브 전체를 훑은
           * 결과로 읽고 「없습니다」로 단정한다 — 좁힌 자리에 없다는 것과 어디에도 없다는 것은
           * 다르다. 되돌아간 경우도 함께 적는다(찾다가 못 찾아 넓힌 것이라 뜻이 다르다). */
          const narrowedTo = [
            auto.channel && !auto.channelFellBack ? `대화는 #${auto.channel}` : null,
            auto.project && !auto.projectFellBack ? `문서는 ${auto.project}` : null,
          ].filter(Boolean);
          const widened = [
            auto.channelFellBack ? '대화' : null,
            auto.projectFellBack ? '문서' : null,
          ].filter(Boolean);
          /* 좁힘·되돌림을 대화 로그에 싣는다 — 도구 인자에는 안 넣는다(재생 도구가
           * call.args 를 그대로 질의로 쓴다: measure-hit-cap.js). 래퍼가 밀어 둔 칸을
           * 여기서 채운다.
           *
           * 가운뎃점 양쪽 띄우기가 위 머리말과 같다 — 좁힘은 `' · '`, 넓힘은 `'·'`. 사람이
           * 보는 머리말과 로그 꼬리가 **같은 글자**여야 둘을 대볼 수 있어서 일부러 맞춘 것이다
           * (바로 아래 `head.push(...)` 두 줄이 원본). 로그 꼬리끼리 달라 보이는 것은 그 대가다.
           *
           * 칸이 어느 호출 것인지는 **순서가 아니라 tool_use 의 id** 로 짝지어진다
           * (래퍼가 id 를 실어 밀고, qa.js 의 attachSizes·attachNarrows 가 그 id 로 얹는다 —
           * 2026-09-17). 그전에는 칸 순서가 짝이라 이 몸통에 `await` 를 들이면 안 됐는데
           * (`Promise.all` 이 첫 `await` 에서 다른 호출로 넘어가 순서가 갈린다), 이제 그
           * 전제는 짝짓기에 관여하지 않는다. */
          Object.assign(narrow, {
            ...(narrowedTo.length ? { narrowedTo: narrowedTo.join(' · ') } : {}),
            ...(widened.length ? { widenedFrom: widened.join('·') } : {}),
          });

          const head = [];
          if (narrowedTo.length) {
            head.push(`(질의에 자리 이름이 있어 ${narrowedTo.join(' · ')} 로 좁혀 찾았습니다. `
              + '다른 자리에도 있을 수 있으니, 없다고 답하기 전에 좁히지 말고 한 번 더 찾으세요.)');
          }
          if (widened.length) {
            head.push(`(${widened.join('·')}는 그 자리에서 아무것도 못 찾아 좁히지 않고 다시 찾았습니다.)`);
          }
          return [...head, rendered.join('\n\n---\n\n')].join('\n\n');
        },
      }),

      betaTool({
        name: 'read_channel',
        description:
          '채널 md 파일을 읽는다. 한 사업장의 흐름을 시간순으로 봐야 할 때 쓴다. ' +
          'month 를 주면 해당 월만 읽어 토큰을 아낄 수 있다.',
        inputSchema: {
          type: 'object',
          properties: {
            channel: { type: 'string', description: '채널명. 예: "사업장나"' },
            month: { type: 'string', description: 'YYYY-MM 형식. 예: "2026-07". 생략하면 전체.' },
          },
          required: ['channel'],
        },
        run: async ({ channel, month }) => {
          const r = readChannel({ channel, month, access });
          if (r.error) return r.error;
          touched.add(r.channel);
          const tail = r.truncated ? `\n\n${truncMarker('month 를 지정해 좁혀 보세요')}` : '';
          // 검색 쪽과 같은 표시. 한쪽만 붙이면 나머지 경로는 조용히 옛 동작으로 남는다.
          return `#${r.channel}${r.month ? ` (${r.month})` : ''}\n\n${markArchivedAttachments(r.text, access)}${tail}`;
        },
      }),
    ];

    // 문서 아카이브가 있을 때만 문서 읽기 도구를 붙인다.
    // 검색은 위 `search` 가 대화와 함께 처리한다 (구역을 갈라 돌려준다 — sectionText 주석 참고).
    // 되돌릴 때 이 블록만 떼면 원상복구된다.
    if (hasDocuments()) {
      tools.push(
        betaTool({
          name: 'read_document',
          description:
            '문서 md 전문, 또는 특정 월·시트·절만 읽는다. ' +
            '정기 자료(일보·주간보고)의 추이를 볼 때 month 로 좁히면 토큰을 아낄 수 있다. ' +
            '엑셀은 시트마다 회차로 담겨 있다 — sheet 로 하나만 열 수 있고, ' +
            'sheet 없이 부르면 맨 앞에 시트 목록이 온다. ' +
            '**여러 건을 번호 붙여 나열한 종합보고는 section 으로 한 건만 열 수 있다** — ' +
            '수십만 자짜리 문서에서 필요한 절만 몇천 자로 온다. ' +
            '**문서가 길고 시트·월·절로 나뉘어 있으면 본문 대신 목차만 온다** — ' +
            '조각 이름과 글자 수가 적혀 있으니 필요한 것을 골라 sheet·month·section 으로 다시 부를 것. ' +
            '**month 로 좁혔는데도 그 달이 길면 주 목차가 온다** — week 로 한 주만 열 수 있다(month 와 함께). ' +
            '나뉘어 있지 않은 긴 문서는 앞부분만 오고, 그때 어디로 갈지는 결과에 딸려 오는 안내가 알려준다 — ' +
            '회차가 여럿이면 search 로 닿지만 회차가 하나뿐이면 도구로는 뒤쪽을 볼 수 없다.',
          inputSchema: {
            type: 'object',
            properties: {
              project: { type: 'string', description: '사업장. 예: "사업장나"' },
              document: { type: 'string', description: '문서명. 일부만 적어도 된다. 예: "잔금수금"' },
              month: { type: 'string', description: 'YYYY-MM 형식. 예: "2026-07". 생략하면 전체.' },
              week: { type: 'string', description: '그 달이 또 길 때 주 하나만. 주 목차에 적힌 이름 또는 그 앞의 월요일 날짜. 예: "2026-08-03". month 와 함께 써야 한다.' },
              sheet: { type: 'string', description: '엑셀 시트 이름(일부만 적어도 된다) 또는 번호. 예: "이자계산" · "2"' },
              section: { type: 'string', description: '번호 붙은 절의 번호 또는 제목 일부. 예: "12". sheet 와 함께 쓸 수 없다.' },
            },
            required: ['project', 'document'],
          },
          run: async ({ project, document, month, week, sheet, section }) => {
            const r = readDocument({ project, document, month, week, sheet, section, access });
            if (r.error) return r.error;
            touched.add(`📄 ${r.project}/${r.document}`);
            // 안내 문구는 documents.js 가 만든다 — 문서마다 좁힐 수 있는 축이 달라서
            // 여기서 한 문장으로 적으면 없는 sheet 를 지정하라는 말이 된다.
            const tail = r.hint ? `\n\n…(${r.hint})` : '';
            const where = [r.month, r.week, r.sheet, r.section].filter(Boolean).join(' / ');
            return `${r.project} / ${r.title}${where ? ` (${where})` : ''}\n\n${r.text}${tail}`;
          },
        }),
      );
    }

    // 슬랙 클라이언트가 있을 때만 최신 조회를 제공한다.
    if (slackClient) {
      tools.push(
        betaTool({
          name: 'fetch_recent_slack',
          description:
            '아카이브 마지막 동기화 이후의 최신 대화를 슬랙에서 직접 가져온다. ' +
            '"최근", "이번 주", "요즘", "어제" 처럼 아카이브보다 새로운 시점을 묻는 질문에 쓴다. ' +
            '느리므로 가능하면 channel 을 지정할 것. 봇이 초대되지 않은 채널은 나오지 않는다.',
          inputSchema: {
            type: 'object',
            properties: {
              days: {
                type: 'integer',
                description: `며칠 전까지. 1~${config.limits?.liveFetchMaxDays ?? DEFAULT_LIVE_FETCH_MAX_DAYS}`,
              },
              channel: { type: 'string', description: '특정 채널로 좁힐 때만. 강력히 권장.' },
            },
            required: ['days'],
          },
          run: async ({ days, channel }) => {
            const w = recentWindow(days);
            let channels;
            if (channel) {
              // 권한 없는 비공개 채널은 fetchWindow 가 조용히 걸러 "새 대화가 없습니다" 로
              // 보이는데, 그건 없다는 뜻으로 잘못 읽힌다. 아래 '들어가 있지 않다' 문구도
              // 물어본 이름을 되돌려 주므로, 그 문구에 닿기 전에 먼저 막는다.
              if (matchesHiddenPrivate(channel, access)) return BLOCKED_NOTE;
              const r = resolveChannel(channel);
              const name = r.ok ? r.name : String(channel).replace(/^#/, '');
              const all = await listBotChannels(slackClient);
              const found = all.find((c) => c.name === name);
              if (!found) return `봇이 #${name} 채널에 들어가 있지 않거나 채널이 없습니다. (/invite 필요)`;
              if ((found.isPrivate ?? isPrivateChannel(found.name)) && !canSeePrivateChannel(access, found.name)) {
                return BLOCKED_NOTE;
              }
              channels = [found];
            }
            // 스레드 되짚기를 짧게 준다. fetchWindow 는 '기간 밖 부모에 기간 안 답글' 을
            // 잡으려고 부모를 lookback 만큼 더 거슬러 훑는데, 기본 30일이면 channel 없이
            // 부를 때 44채널 × (요청일수 + 30일)을 전부 읽는다.
            //
            // **내주는 것을 정확히 적는다.** 이 도구가 보는 부모의 나이는 (요청일수 + 되짚기)일까지다.
            // 30일이던 때는 최대 44일치(14+30), 7일이면 21일치(14+7)다. 그 사이 구간의 스레드에
            // **오늘** 달린 답글이 안 잡힌다.
            //
            // 부모가 30일 안쪽이면 자동 반영이 다음 07:00 에 부모 블록 안에 넣으므로 하루면 닫힌다.
            // **30일보다 오래된 부모는 안 닫힌다** — 자동 반영도 30일까지만 되짚어서
            // (`slack-archive.js` 의 `ingestChannel` 안 `lookbackSec`, 같은 threadLookbackDays) 아카이브에 아예 안 들어간다.
            // 그 구멍은 원래부터 있었고, 전에는 이 도구가 우연히 덮고 있었을 뿐이다.
            // 요약·자동 반영에서 30일을 그대로 두는 이유가 이것이다. 그쪽이 놓치면 영영 안 들어간다.
            //
            // **그 30일을 늘리지 않기로 정했다** (WHK 결정 2026-08-06). 근거는 「어차피 30일
            // 안에 업데이트가 된다」이다. 늘리자는 이야기를 다시 꺼내려면 먼저 그 전제가
            // 깨졌는지를 — 30일 넘은 스레드에 답글이 실제로 달리고 있는지를 — 세어 보일 것.
            const entries = await fetchWindow(slackClient, {
              oldest: w.oldest,
              latest: w.latest,
              channels,
              access,
              threadLookbackDays: config.limits.qaThreadLookbackDays ?? 7,
            });
            entries.forEach((e) => touched.add(e.channel));
            if (!entries.length) return `최근 ${w.days}일간 새 대화가 없습니다.`;
            return `최근 ${w.days}일 (슬랙 실시간):\n\n${formatTranscript(entries)}`;
          },
        }),
      );
    }

    /* ── 도구 래퍼: 모든 도구 호출이 반드시 지나는 한 자리 ──────────────
     *
     * 두 가지를 기록하고, 결과는 그대로 통과시킨다. 두 기록 다 SDK 가 두 번째 인자로
     * 주는 **tool_use 의 id** 를 실어 보내고(BetaToolRunner 가 `run(input, { toolUse, … })`
     * 로 부른다), qa.js 의 attachSizes·attachNarrows 가 그 id 로 `toolCalls` 에 짝짓는다.
     *
     * 예전에는 순서로 짝지었다 — 크기는 도구가 끝난 뒤 이름별 큐로, 좁힘은 search 본문
     * 첫 문장의 선푸시로. 그래서 칸을 안 남기는 호출이 하나라도 있으면(던진 호출,
     * refusal break 로 실행 자체가 안 된 호출) 그 뒤 칸이 전부 한 칸씩 앞으로 밀려,
     * 안 좁혔던 질의에 「…로 좁혀 찾았다」가 붙고 던진 호출에 남의 크기가 붙었다.
     * 에러는 안 나고 로그만 조용히 틀리는 밀림이라(2026-09-11 에는 search 한 곳만
     * 선푸시로 막았다), 칸 밀기를 도구별 run 에서 이 래퍼 한 자리로 올리고 짝을 id 로
     * 바꿔 남은 갈래를 한꺼번에 닫았다 (2026-09-17).
     *
     * ① 결과 크기(sizes): 도구가 실제로 몇 자를 물어왔는지. 로그에는 도구 **입력**만
     *    남아 "캐시 쓰기가 건당 51,113 토큰"까지는 재도 어느 호출이 얼마를 물어왔는지
     *    아무도 몰랐고, 상한(documentReadMaxChars 등) 조정의 근거가 없었다 (2026-08-15).
     *    **끝난 뒤에 민다** — 던진 호출은 크기를 안 남기는 것이 맞고(잰 적 없는 값을
     *    지어내지 않는다), id 짝이라 그 빈자리가 다른 호출로 밀리지도 않는다.
     * ② search 의 좁힘 칸(narrows): **run 본문에 들어가기 전에** 빈 칸을 밀고,
     *    `ctx.narrow` 로 넘겨 본문이 채우게 한다. return 경로가 몇 개든, 본문에 못
     *    들어가고 죽든, 중간에 던지든, 실행이 시작된 호출의 칸은 정확히 하나다.
     *    못 채운 칸은 빈 칸 = 「좁히지 않았다」와 같은 모양 — 지어내는 것보다 낫다.
     *
     * 던지는 것은 잡지 않는다 — 여기서 삼키면 툴 러너가 받아야 할 오류가 사라진다.
     * id 가 없는 호출(검사가 run 을 ctx 없이 직접 부를 때)은 id 없이 민다 — 그 칸은
     * 짝짓기에 안 쓰이고, 그 검사들은 배열을 직접 본다. */
    for (const t of tools) {
      const run = t.run;
      t.run = async (input, ctx) => {
        const id = ctx?.toolUse?.id;
        let inner = ctx;
        if (t.name === 'search') {
          const narrow = {};
          if (id != null) narrow.id = id;
          narrows.push(narrow);
          inner = { ...ctx, narrow };
        }
        const out = await run(input, inner);
        if (sizes) {
          const size = { name: t.name, chars: typeof out === 'string' ? out.length : 0 };
          if (id != null) size.id = id;
          sizes.push(size);
        }
        return out;
      };
    }

    return tools;
  }


  return { autoNarrow, buildTools };
}
