/** Document health reads, approvals and pending counts. */
export function createHealthDocuments({ fs, path, config, DOCS_DIR, readJson, settings, activeDeferred, isPrivateChannel, listBotChannels, channelAttachments, nameKey, docFilters, classifyDoc, DOC_EXTS, PREFER, DECIDED_OUT, extOf, archiveChannel = (n) => n, dropSkippedChannels = (chs) => chs }) {
  /**
   * `.doc-state.json` 의 `doc` 값을 실제 파일 경로로. 없으면 null.
   *
   * 값에 `projects/` 접두가 붙은 것과 안 붙은 것이 섞여 있었다(손으로 적는 자리라 그렇다).
   * **2026-08-27 에 41건을 짧은 형태로 통일했지만** 둘 다 계속 받아준다 — 다시 섞일 수 있고,
   * 여기서 못 찾으면 승인 미반영을 조용히 놓친다.
   */
  function docPath(doc) {
    if (!doc || !DOCS_DIR) return null;
    const rel = String(doc).replace(/^projects[\\/]/, '');
    const p = path.join(DOCS_DIR, 'projects', rel);
    return fs.existsSync(p) ? p : null;
  }

  /**
   * 그 문서 md 가 봇에게 실제로 열려 있나 — `documents.js` 의 `loadDocument` 와 같은 조건이다.
   * 비공개 채널 문서는 `열람: 공개` 와 `공개승인` **두 줄이 다 있어야** 열린다.
   */
  function docIsOpen(file) {
    let text;
    try {
      text = fs.readFileSync(file, 'utf8').slice(0, 4000);
    } catch {
      return false;
    }
    const m = text.match(/\*\*열람\*\*:\s*([^·\n]*)/);
    const access = (m?.[1] || '').trim();
    return access === '공개' && /\*\*공개승인\*\*/.test(text);
  }

  /**
   * 아직 md 로 변환되지 않은 첨부를 센다.
   *
   * 판정 기준은 `.doc-state.json` 의 `slack_files` 키(슬랙 파일 ID)다. 이 파일은
   * **변환 단계에서만** 갱신된다 — 받아오는 쪽(`doc-archive` 2단계의 `fetch_slack_files.py`)은
   * 읽기만 하므로, 받아만 두고 변환 안 한 것도 여기서 미변환으로 잡힌다.
   *
   * 같은 순회에서 `[공개]` 승인 미반영도 함께 센다 — 슬랙을 두 번 훑지 않으려는 것이다.
   *
   * **필드를 늘리면 `scripts/board-docs.js` 의 투영도 함께 고쳐야 한다** — 상황판은 그 투영만
   * 보므로, 빠뜨리면 에러가 아니라 잘못된 화면이 된다(`disabled` 가 실제로 그랬다).
   * `scripts/check-board-docs.js` 가 이 반환문에서 키를 직접 뽑아 대므로, 안 맞으면 거기서 멈춘다.
   *
   * @returns {{total:number, byChannel:Array, scanDays:number, failed:string[],
   *   approvals:Array, deferred:Array, restricted:Array, disabled?:boolean}}
   */
  async function pendingDocuments(client, { scanDays, now = Date.now() } = {}) {
    const s = settings();
    const days = scanDays ?? s.scanDays;
    if (!DOCS_DIR) {
      return {
        total: 0, byChannel: [], scanDays: days, failed: [], approvals: [], deferred: [],
        restricted: [], disabled: true,
      };
    }

    const state = readJson(path.join(DOCS_DIR, '.doc-state.json'));
    const slackFiles = state?.slack_files || {};

    /**
     * 거르는 다섯 갈래는 `docFilters`·`classifyDoc` 한 자리에 있다 (2026-08-26 에 모았다).
     * 갈래별로 왜 그렇게 보는지도 거기 적혀 있다 — 여기서는 부르기만 한다.
     */
    const filters = docFilters(state, now);

    /** 미뤄 둔 목록은 아래 반환값에 그대로 싣는다 (거르기는 `filters` 가 한다). */
    const deferredList = activeDeferred(state, now);

    const oldest = Math.floor(now / 1000) - days * 86400;
    // 부모는 스레드 되짚기 구간만큼 넓게 훑는다 — 옛 스레드에 오늘 답글로 올린 자료를 보려는 것이다
    // (자동 반영의 threadLookbackDays 와 같은 경계. 그보다 오래된 것은 어차피 안 따라간다).
    const parentDays = Math.max(days, config.limits?.threadLookbackDays ?? 30);
    const parentOldest = Math.floor(now / 1000) - parentDays * 86400;

    // skipChannels 는 어디에서도 다루지 않는 채널이다. 여기서 세면 "미변환 N건" 이 영영
    // 줄지 않는 숫자가 되어, 진짜 밀린 건수를 가린다. 개명도 되짚는다 (2026-09-16) —
    // 글자 그대로 대면 skip 채널이 개명된 날부터 그 채널의 미변환이 다시 세어진다.
    const channels = dropSkippedChannels(await listBotChannels(client));
    const failed = [];
    const found = [];

    // 슬랙 레이트리밋(Tier 3, 약 50req/분)을 넘지 않도록 동시 실행 수를 낮게 잡는다.
    const queue = [...channels];
    const workers = Array.from({ length: Math.min(4, queue.length) }, async () => {
      while (queue.length) {
        const ch = queue.shift();
        if (!ch) break;
        try {
          found.push(...(await channelAttachments(client, ch, oldest, parentOldest)));
        } catch (err) {
          // not_in_channel 은 봇 미초대. npm run check 가 알려준다.
          failed.push(`#${ch.name}(${err.data?.error || err.message})`);
        }
      }
    });
    await Promise.all(workers);

    // ── `[공개]` 승인이 아카이브에 반영됐나 ──────────────────────────────
    // 승인은 문서가 들어간 **뒤에** 나온다. 그 경로를 수집 스크립트가 못 보므로(파일 머리말 참조)
    // 여기서 슬랙 스레드의 태그와 문서 md 의 메타를 직접 대조한다.
    const byName = new Map();
    for (const v of Object.values(slackFiles)) byName.set(nameKey(v.channel, v.name || ''), v);

    const approvals = [];
    const seenApproval = new Set();
    for (const f of found) {
      if (!f.publicTag || !DOC_EXTS.has(f.ext)) continue;
      // 공개 채널 문서는 원래 공개다. `공개승인` 을 적으면 오히려 verify_format 이 ✗ 를 낸다.
      // `config.json` 의 privateChannels 는 **현재** 이름을 든다(개명되면 사람이 고친다).
      // 그래서 여기만 사업장 이름이 아니라 슬랙 이름으로 본다.
      if (!isPrivateChannel(f.slackChannel ?? f.channel)) continue;
      const key = nameKey(f.channel, f.name);
      // 사람이 「안 넣기로」 정한 것은 승인이 달려도 알리지 않는다 (`DECIDED_OUT` 주석).
      if (DECIDED_OUT.has(classifyDoc(f, filters))) continue;
      if (seenApproval.has(key)) continue;
      // 같은 자료를 다시 올려 태그를 단 경우가 있다 — 이름으로도 찾아야 원본 회차를 짚는다.
      const rec = slackFiles[f.id] || byName.get(key);
      if (!rec) {
        // 아직 변환 전이다. 미변환 집계가 이미 세고 있으니, 여기서는 "변환할 때 함께 적을 것" 만 알린다.
        seenApproval.add(key);
        approvals.push({ channel: f.slackChannel ?? f.channel, name: f.name, state: 'pending' });
        continue;
      }
      const file = docPath(rec.doc);
      if (file && docIsOpen(file)) continue;                 // 이미 반영됐다
      seenApproval.add(key);
      approvals.push({ channel: f.slackChannel ?? f.channel, name: f.name, doc: rec.doc, state: file ? 'meta' : 'missing' });
    }

    // 대상 확장자 + 아직 변환 안 된 것만.
    // 파일 ID 로 한 번, **이름으로 한 번 더** 거른다 — 같은 자료의 다른 포맷 판은 변환되지 않고
    // 버려져서 상태 파일에 남지 않으므로, ID 만 보면 영영 미변환으로 잡힌다.
    // 다섯 갈래 중 하나라도 걸리면 후보가 아니다 (`classifyDoc` 이 `null` 이 아닌 것).
    let pending = found.filter(
      (f) => DOC_EXTS.has(f.ext) && classifyDoc(f, filters) === null,
    );

    /* **봇이 못 받는 파일은 「미변환」에서 뺀다 — 대신 이름을 붙여 남긴다.**
     *
     * `doc-archive` 수집은 `url_private_download` 가 없으면 건너뛰고 상태 파일에 기록을
     * 안 남긴다. 그래서 여기서 함께 세면 사람이 `doc-archive` 를 돌려도 그 건은 절대
     * 안 줄어들고, 09:00 DM 의 숫자가 매일 같은 값으로 온다. 줄지 않는 숫자가 매일 오면
     * 사람은 곧 알림 전체를 안 읽게 되고, 그때 진짜 밀린 것도 같이 묻힌다
     * (`excluded` 를 만든 이유와 같다 — 위 주석).
     *
     * **0 으로 감추지는 않는다.** 이건 사람이 슬랙에서 직접 받아 올려야 하는 일이지
     * 없는 일이 아니라서, `restricted` 로 따로 세어 화면에 남긴다. */
    const restrictedList = pending.filter((f) => !f.hasUrl);
    pending = pending.filter((f) => f.hasUrl);

    // 이번에 남은 것들끼리도 같은 이름이면 원본 포맷 하나만 센다. doc-archive 도 하나만
    // 변환하므로, 그러지 않으면 "12건 남음"이라 알리고 6건만 처리되어 숫자가 영영 안 맞는다.
    const rank = Object.fromEntries(PREFER.map((e, i) => [e, i]));
    const best = new Map();
    for (const f of pending) {
      const key = nameKey(f.channel, f.name);
      const cur = best.get(key);
      if (!cur || (rank[f.ext] ?? 99) < (rank[cur.ext] ?? 99)) best.set(key, f);
    }

    const byChannel = new Map();
    for (const f of best.values()) {
      // 사람이 슬랙에서 찾아가야 하므로 **현재** 이름으로 묶는다 (판정 키는 위 `nameKey` 쪽).
      const where = f.slackChannel ?? f.channel;
      if (!byChannel.has(where)) byChannel.set(where, []);
      byChannel.get(where).push(f);
    }

    return {
      total: best.size,
      scanDays: days,
      failed,
      approvals,
      // 봇이 못 받는 파일. `total` 에 안 들어간다 — 위 `restrictedList` 주석 참조.
      restricted: restrictedList
        .map((f) => ({ channel: f.slackChannel ?? f.channel, name: f.name }))
        .sort((a, b) => String(a.channel).localeCompare(String(b.channel))),
      deferred: deferredList
        .map(([, v]) => ({ channel: v.channel, name: v.name, until: v.until }))
        .sort((a, b) => String(a.until).localeCompare(String(b.until))),
      byChannel: [...byChannel.entries()]
        .map(([channel, files]) => ({
          channel,
          count: files.length,
          newest: files.sort((a, b) => Number(b.ts) - Number(a.ts))[0]?.name || '',
        }))
        .sort((a, b) => b.count - a.count),
    };
  }

  /**
   * 주어진 첨부 중 아직 md 로 안 들어간 것의 개수.
   *
   * 위 `pendingDocuments` 와 달리 **슬랙을 다시 조회하지 않는다** — 일일 요약이 이미 들고 있는
   * 실시간 조회 결과를 그대로 받는다. 그쪽은 파일 ID 없이 이름만 남기므로(slack-live.js)
   * 판정도 이름으로만 한다. 세는 규칙(대상 확장자·중복 포맷·의도적 제외)은 같은 것을 쓴다.
   *
   * ── 권한 제한 첨부는 여기서 **세고**, `pendingDocuments` 는 **뺀다. 둘 다 맞다** ──
   *
   * `hasUrl`(=슬랙이 다운로드 주소를 줬나)을 여기서는 볼 수가 없다. 실시간 조회의
   * `normalizeMessage` 가 첨부에서 **이름 문자열만** 남기고 `url_private_download` 를 버리기
   * 때문이다. 그래서 요약 꼬리말의 `미반영 N건` 에는 권한 제한 파일이 포함되고, 09:00 DM 의
   * `미변환` 에는 안 든다(그쪽은 `restricted` 로 따로 적는다).
   *
   * **값이 갈리는 것이 아니라 묻는 것이 다르다** (WHK 결정 2026-08-13). 꼬리말은 팀에게
   * 「봇이 아직 못 읽는 자료가 N건」을 말하고 — 권한 제한 파일은 실제로 못 읽는다 —
   * DM 은 사람에게 「`doc-archive` 로 처리할 것이 N건」을 말한다. 후자에서 빼는 이유는
   * `doc-archive` 를 돌려도 그 건이 안 줄어들기 때문이다(위 `restrictedList` 주석).
   *
   * **맞추려고 고치지 말 것.** 맞추려면 `slack-live.js` 의 `files` 를 문자열 배열에서 객체
   * 배열로 바꿔야 하는데, 그 배열은 `formatTranscript` 를 거쳐 **모델에게 가는 프롬프트 본문**
   * 에 실린다. 한 자리라도 놓치면 프롬프트에 `[object Object]` 가 들어가고, 그 고장은
   * 답변 품질로만 나타나 며칠 뒤에 발견된다.
   *
   * @param {Array<{channel:string, name:string}>} items 이름만 있으면 된다 (`hasUrl` 은 안 본다)
   */
  function unconvertedAmong(items) {
    if (!DOCS_DIR) return 0;
    const state = readJson(path.join(DOCS_DIR, '.doc-state.json'));
    if (!state) return 0;

    /* 거르기는 `pendingDocuments` 와 **같은 판정**을 쓴다 (2026-08-26 에 모았다).
     * 여기만 빠뜨리면 위생 점검은 3건, 요약 하단은 4건이라고 말한다 — 의도적으로 안 넣기로
     * 한 것(`excluded`)도, 만기 전 보류도, 최신이 대신한 옛 판도 「미반영」이 아니다.
     * **여기 입력에는 파일 ID 가 없어 이름 경로만 탄다** (`classifyDoc` 주석). */
    const filters = docFilters(state);

    const left = new Set();
    for (const it of items || []) {
      if (!it?.name) continue;
      if (!DOC_EXTS.has(extOf({ name: it.name }))) continue;
      // 요약 꼬리말의 입력은 슬랙 현재 이름이라 여기서 되짚는다 (그쪽에는 파일 ID 도 없다).
      const key = nameKey(archiveChannel(it.channel), it.name);
      if (classifyDoc({ ...it, channel: archiveChannel(it.channel) }, filters) !== null) continue;
      left.add(key);
    }
    return left.size;
  }

  return { pendingDocuments, unconvertedAmong };
}
