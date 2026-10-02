/**
 * Codex R2b: document loading and parsed-document cache.
 * One factory instance per documents facade; dependencies are not called until load.
 * Invalidation key is file mtime + channel-map identity (see docCache comment);
 * shared document identity preserved. No eviction policy change.
 * Keep byte-identical in Hermes and Clio.
 */
export function createDocumentStore({
  fs, path, projectDir, projectPrivateChannel, projectIsPrivate, readCached, metaBlock, splitMessages, preambleOf, channelMap
}) {
  /* ── 메타 ─────────────────────────────────────────────────────── */

  /**
   * '**키**: 값 · **키2**: 값2' 형태의 메타 블록을 객체로.
   * 한 줄에 키가 하나뿐이면 줄 끝까지가 값이다 — 그래야 '주요 항목' 처럼
   * 값 안에 '·' 를 쓰는 항목이 첫 가운뎃점에서 잘리지 않는다.
   */
  function parseMeta(text) {
    const out = {};
    for (const line of text.split('\n')) {
      const keys = [...line.matchAll(/\*\*([^*]+)\*\*:/g)];
      if (!keys.length) continue;
      if (keys.length === 1) {
        out[keys[0][1].trim()] = line.slice(keys[0].index + keys[0][0].length).trim();
        continue;
      }
      for (const m of line.matchAll(/\*\*([^*]+)\*\*:\s*([^·\n]+)/g)) {
        out[m[1].trim()] = m[2].trim();
      }
    }
    return out;
  }

  /**
   * 해석 결과 캐시. abs → { mtimeMs, mapRef, doc }
   *
   * **키에 개명 지도의 지금 판(`mapRef`)을 함께 묶는다** (2026-09-17). 아래 `doc` 의
   * `private`·`privateChannel` 은 읽는 순간의 지도(`.sync-state.json`)로 판정해 굳힌 값이라,
   * 파일 mtime 만 보면 **지도가 죽거나 바뀐 뒤에도 옛 판정이 캐시에서 계속 나온다** —
   * 지도가 살아 있을 때 「공개」로 읽힌 문서가, 지도가 죽어 fail-closed 로 닫혀야 할 때도
   * 열린 채 남는 모양이다. `channelMap`(config.js 의 `currentChannelNames`)은
   * `.sync-state.json` 이 그대로면 **같은 `Map` 객체**를 돌려주고 수정 시각이 바뀌면 새로
   * 만드므로, 그 객체의 동일성이 곧 「지도 상태가 그대로다」라는 지문이다 — 프롬프트 캐시가
   * 지문에 `.sync-state.json` 을 더한 것(llm/prompts.js 의 briefStampPaths, 2026-09-16)과
   * 같은 자리를 여기서는 객체 동일성으로 잰다. 지도 파일이 없는 상태에서는 저쪽 캐시가
   * 60초마다 지도를 새로 만들어 여기도 60초마다 다시 읽는데, 그 상태는 죽음(전부 닫힘)
   * 아니면 신규 설치(문서가 거의 없음)라 다시 읽는 비용이 문제되지 않는다.
   *
   * `readCached` 는 파일 **내용**만 캐시하고 해석은 매번 다시 했다. 그래서 문서 234건 5.4MB 를
   * `listDocuments()` 부를 때마다 통째로 다시 쪼갰다 — `search` 한 번에 이 함수가
   * 3~4회 돈다(이름 풀기 → 보이는 사업장 추리기 → 실제 검색). 요청당 약 150ms.
   *
   * **돌려주는 객체는 공유된다 — 부르는 쪽에서 고치면 안 된다.** 지금은 아무도 안 고친다
   * (색인은 `groups` 배열만 다시 만들고, 검색은 읽기만 한다).
   *
   * 대신 회차 텍스트를 계속 붙들고 있어 `readCached` 의 원문과 별개로 약 5MB 를 더 쓴다.
   * 전에는 매번 만들고 버렸다. 문서가 몇 배로 늘면 여기부터 본다.
   */
  const docCache = new Map();

  /**
   * 문서 md 한 개를 읽어 색인·검색에 필요한 것만 추린다.
   * 메타를 못 읽으면 private=true 로 돌려준다 — fail-closed.
   * 슬랙 쪽은 헤더가 없으면 색인에서 빠지고 마는데(fail-open), 문서는 계약서·손실
   * 추정 원문이라 잘못 열리는 쪽이 훨씬 비싸다.
   */
  function loadDocument(project, file) {
    const abs = path.join(projectDir(project), file);
    const slug = file.replace(/\.md$/, '');

    // 캐시 판정과 읽기를 같은 mtime 으로 묶는다. 파일이 사라졌으면 stat 이 던지고
    // 아래 broken 경로로 간다 — readCached 도 같은 자리에서 던지므로 동작이 같다.
    // mapRef 는 위 docCache 주석 참조 — 지도가 바뀌면(mtime 이 그대로여도) 다시 읽는다.
    const mapRef = channelMap();
    let mtimeMs;
    try {
      mtimeMs = fs.statSync(abs).mtimeMs;
      const hit = docCache.get(abs);
      if (hit && hit.mtimeMs === mtimeMs && hit.mapRef === mapRef) return hit.doc;
    } catch {
      return {
        project, slug, file, abs, title: slug, meta: {}, entries: [], preamble: '',
        private: true, privateChannel: projectPrivateChannel(project), broken: true,
      };
    }

    let text;
    try {
      text = readCached(abs);
    } catch {
      return {
        project, slug, file, abs, title: slug, meta: {}, entries: [], preamble: '',
        private: true, privateChannel: projectPrivateChannel(project), broken: true,
      };
    }

    const meta = parseMeta(metaBlock(abs));
    const titleLine = text.split('\n').find((l) => /^# /.test(l));
    const entries = splitMessages(text);
    /* 첫 회차 헤더 **앞**에 사람이 손으로 적어 둔 구간. `entries` 와 **따로** 든다 —
     * `d.entries.length` 로 회차를 세는 자리가 여럿이라(색인의 `N회차`·`isSeriesDoc`·
     * `outlineOf`·`verify.js`·`check-excel-sheets.js`) 여기에 끼우면 그 수가 전부 틀어진다.
     * 채널 쪽이 `splitMessages` 를 안 건드리고 `preambleOf` 를 따로 둔 것과 같은 이유다. */
    const preamble = preambleOf(text);

    // 메타가 아예 안 잡히거나 '열람' 이 없으면 비공개로 본다.
    const access = meta['열람'];
    // 비공개 채널 문서는 채널만으로 비공개다. 예외는 하나 — 슬랙 스레드에 `[공개]` 댓글이
    // 달렸고 사람이 그것을 `공개승인` 줄로 옮겨 적은 경우다 (WHK 결정 2026-08-05,
    // doc-archive 6단계). **`열람: 공개` 한 줄만으로는 안 열린다.** 두 줄이 다 있어야 한다.
    // 한 줄로 열리게 하면 공개 채널 문서에 흔한 `열람: 공개` 를 비공개 채널 문서에 실수로
    // 복사해 붙이는 것만으로 #비공개가 자료가 팀 앞에 나간다.
    const approved = Boolean(meta['공개승인']);
    const isPrivate = !access || access !== '공개' || (projectIsPrivate(project) && !approved);

    const doc = {
      project,
      slug,
      file,
      abs,
      title: titleLine ? titleLine.replace(/^#\s+/, '').trim() : slug,
      meta,
      entries,
      preamble,
      private: isPrivate,
      privateChannel: projectPrivateChannel(project),
      broken: !Object.keys(meta).length,
    };
    docCache.set(abs, { mtimeMs, mapRef, doc });
    return doc;
  }

  return { loadDocument };
}
