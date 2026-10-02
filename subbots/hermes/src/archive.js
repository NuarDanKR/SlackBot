/**
 * 아카이브 계층 — 50-resources/slack-export/ 를 읽기 전용으로 다룬다.
 *
 * Hermes 는 아카이브를 절대 쓰지 않는다. 손으로 큐레이션한 헤더·요약을
 * 봇이 덮어쓰는 사고를 원천 차단하기 위함. 갱신은 slack-sync 스킬 담당.
 */
import fs from 'node:fs';
import path from 'node:path';
import {
  ARCHIVE_DIR, CHANNELS_DIR, config, canSee, isPrivateChannel, normalizeChannel, readCached,
  redactPrivateMentions, matchesHiddenPrivate, BLOCKED_NOTE, OUT_OF_SCOPE_NOTE, PUBLIC_ACCESS,
  BOT_ANSWER_MARK, TRUNC_PHRASE, canonicalChannel, currentChannelNames,
} from './config.js';
import { splitTerms, scoreTerms, PARTIAL_MIN_TERMS, clipPartial } from './search-terms.js';

// 개명 지도와 파일 캐시는 config.js 가 정본이다 — 권한 판정(config.js)이 같은 것을 써야
// 하는데 config.js 는 뿌리라 여기서 가져갈 수 없어서다. 쓰던 곳이 안 깨지게 재수출만 남긴다.
export { readCached, archiveChannelNames, archiveChannelOf } from './config.js';

/**
 * 아카이브가 서 있나. **두 상황을 가른다** (2026-09-01).
 *
 * 전에는 둘 다 "config.json 의 archivePath 를 확인하세요" 였는데, 새 팀이 마주하는 것은
 * 대개 「아직 안 만들었다」쪽이다. 그 사람은 멀쩡한 archivePath 를 고치러 간다.
 */
export function assertArchive() {
  if (!fs.existsSync(ARCHIVE_DIR)) {
    throw new Error(
      `대화 아카이브를 아직 만들지 않았습니다: ${ARCHIVE_DIR}\n`
      + '코드 저장소에서 `npm run init-archive -- <자료저장소경로>` 를 먼저 돌리세요.\n'
      + '자료 저장소 자리가 틀린 것이라면 .env 의 HERMES_DATA_ROOT 를 보세요.',
    );
  }
  if (!fs.existsSync(path.join(ARCHIVE_DIR, 'index.md'))) {
    throw new Error(
      `아카이브 폴더는 있는데 index.md 가 없습니다: ${ARCHIVE_DIR}\n`
      + 'config.json 의 archivePath 를 확인하세요.',
    );
  }
}

/** index.md 전문 */
export function getIndexText() {
  return readCached(path.join(ARCHIVE_DIR, 'index.md'));
}

/**
 * md 파일이 있는 채널명 목록. **디스크의 사실 그대로다 — 안 다루기로 한 채널도 들어 있다.**
 *
 * 봇에게 내줄 목록은 아래 `listReadableChannels()` 다. 이 함수를 거르지 않는 이유는
 * 점검 화면이 **실물을 세야** 하기 때문이다 — `check-setup.js` 가 「채널 N개 (md 파일 있는 것
 * M개)」를 적고 `uninvitedChannels` 에 `hasArchive` 를 넘기는데, 여기서 거르면 md 가 멀쩡히
 * 있는 채널을 「아카이브 md 없음」이라고 찍는다. 그 파일의 주석이 못박아 둔 것이 바로
 * 「재지 않은 것을 적지 않는다」다. (2026-09-03)
 */
export function listArchivedChannels() {
  if (!fs.existsSync(CHANNELS_DIR)) return [];
  return fs
    .readdirSync(CHANNELS_DIR)
    .filter((f) => f.endsWith('.md'))
    .map((f) => f.slice(0, -3))
    .sort();
}

/**
 * 「봇이 아예 다루지 않는 채널」을 이름 목록에서 뺀다. 순수 함수다.
 *
 * `backfillTargets`(`ingest/backfill.js`)와 같은 모양으로 두었다 — 거르는 규칙을 인자로
 * 받아야 가짜 이름으로 시험할 수 있고, 이 저장소는 팀끼리 나눠 써서 검사 코드에 진짜
 * 채널 이름을 적을 수 없다(`check-business-names.js`).
 *
 * 양쪽 다 `normalizeChannel` 을 거친다. 설정에는 `#` 를 붙여 적기도 하는데, 그대로 대보면
 * 한 글자 차이로 아무것도 안 막고 **에러도 안 난다.**
 *
 * **개명도 되짚는다** (2026-09-16). 여기 오는 이름은 **아카이브의 md 파일명**(=개명 전 이름)
 * 인데 설정에는 사람이 어느 철자로든 적는다. 그대로 대보면 새 이름을 적은 순간 이 줄이
 * 아무것도 안 막아 「봇이 아예 다루지 않기로 한」 채널이 색인·검색·인용으로 돌아온다 —
 * 아래 `listReadableChannels` 주석이 적은 2026-09-03 사고와 같은 모양이다.
 *
 * 되짚기 전에는 `privateChannels` 와 **안전한 철자가 서로 반대**였다(전자는 아무거나,
 * 후자는 옛 이름만). 그런데 개명 보고는 두 목록 모두에 「새 이름으로 고쳐라」라고 한다
 * (`staleChannelRefs` 의 `renamed`). 보고를 믿고 고치면 이 줄이 죽었다. 이제 **어느 철자를
 * 적어도 양쪽 다 산다.**
 */
export function withoutSkipped(names, skip = config.digest?.skipChannels || []) {
  const map = currentChannelNames();
  const skipSet = new Set([...(skip || [])].map((c) => canonicalChannel(c, map)));
  return (names || []).filter((n) => !skipSet.has(canonicalChannel(n, map)));
}

/**
 * 봇이 **읽어도 되는** 채널 md 목록. 읽기 경로는 전부 이것으로 채널을 센다.
 *
 * ── 왜 2026-09-03 에 생겼나 ──
 *
 * `skipChannels` 를 거르는 코드가 **쓰기 경로 다섯 곳에만** 있었다 (`slack-live.js` 의
 * `fetchWindow` · `ingest/slack-archive.js` 의 `ingestConversations` · `ingest/backfill.js` 의
 * `backfillTargets` · `archive-health.js` 의 첨부 후보 · doc-archive 의 `fetch_slack_files.py`).
 * **봇이 읽는 쪽에는 한 곳도 없었다** — 그 사실을 `backfill.js` 주석이 이미 적어 두고 있었다.
 *
 * 쓰기만 막으면 「md 가 안 생기니까 읽을 것도 없다」가 되는데, 그 전제는 세 가지로 깨진다.
 * ① 목록에 넣기 전에 이미 쌓인 md, ② 개명으로 설정 줄이 죽은 사이에 쌓인 md
 * (2026-08-10 에 실제로 21일간 그랬다), ③ 손으로 넣은 md. 봇은 채널을 `.sync-state.json` 이
 * 아니라 **디스크의 md 목록**으로 세므로, 파일이 있으면 그 즉시 색인·검색·인용 대상이 됐다.
 * 실측 2026-09-03: 목록 9줄 중 md 가 있는 2개가 둘 다 **공개** 시스템 프롬프트 색인에 실렸고
 * 공개 권한 검색에도 걸렸다.
 *
 * ── 권한을 안 본다 ──
 *
 * 전 권한에서 거른다. 거르는 다섯 자리가 전부 권한을 안 보고, 설정 주석의 문구도
 * 「봇이 아예 다루지 않는 채널」이다. 「사람이 콕 집어 물었으면 통과」로 두면 설정이
 * 막아준다고 믿는 것과 실제가 어긋나는 그 상태가 그대로 생긴다 — 넣어야 한다면
 * `skipChannels` 에서 지우는 것이 옳은 순서다 (`backfillTargets` 가 `--channel` 을
 * 그렇게 다룬다).
 *
 * ── 설정 열쇠 이름이 하는 일과 어긋나 있다 ──
 *
 * 이 목록은 `digest.skipChannels` 다. 이름은 「요약(digest)에서 뺀다」인데 실제로 하는 일은
 * 「봇이 아예 다루지 않는다」이고, 그 뜻이 설정 파일 주석에만 적혀 있다. 열쇠를 옮기거나
 * 새로 만들지 않았다 — `config.json` 은 자료 저장소에 있어 코드와 함께 못 고치고, 둘로
 * 나누면 한쪽만 적힌 채널이 생겨 지금보다 나쁘다. 옮길 때는 다섯 자리와 여기, 그리고
 * `config.js` 의 `channelRefsIn` 을 함께 옮겨야 한다.
 */
export function listReadableChannels() {
  return withoutSkipped(listArchivedChannels());
}

/**
 * 안 다루기로 한 채널 중 **md 가 실재하는 것.** 짚어 물었을 때 「범위 밖」이라고
 * 답해야 할 대상이다 (`resolveChannelFor`).
 *
 * 설정 목록을 그대로 쓰지 않고 디스크와 교집합을 내는 이유는, 목록 아홉 줄 중 md 가 있는
 * 것이 둘뿐이고 나머지는 애초에 풀릴 이름이 아니어서다 — 없는 이름에까지 「범위 밖」을
 * 붙이면 오타를 낸 사람이 "후보: …" 를 못 받는다. (2026-09-03)
 */
function skippedChannels() {
  const readable = new Set(listReadableChannels());
  return listArchivedChannels().filter((c) => !readable.has(c));
}

/**
 * .sync-state.json 의 채널 ID ↔ 이름 (md 파일이 없는 채널까지 포함).
 *
 * **여기 있는 것이 슬랙에 있는 것 전부는 아니다.** 이 파일에 채널을 적는 것은 자동 반영뿐이고
 * 그 열거가 `users.conversations`(봇이 멤버인 채널)라, 봇이 한 번도 안 들어간 채널은 없다.
 * 「슬랙에 무엇이 있나」를 물으려면 `listSlackChannels` 를 쓴다.
 */
export function listAllChannels() {
  const p = path.join(ARCHIVE_DIR, '.sync-state.json');
  if (!fs.existsSync(p)) return [];
  const state = JSON.parse(readCached(p));
  const archived = new Set(listArchivedChannels());
  return Object.entries(state.channels || {})
    .map(([id, v]) => ({
      id,
      name: v.name,
      // md 파일명이자 **개명 전 이름의 화석**이다 — 개명해도 md 파일명은 안 바꾸므로
      // `staleChannelRefs` 가 이 칸으로 옛 이름 → 새 이름을 되짚는다.
      file: v.file || v.name,
      hasArchive: archived.has(v.name),
      private: isPrivateChannel(v.name),
    }))
    .sort((a, b) => a.name.localeCompare(b.name, 'ko'));
}

/**
 * 설정이 가리키는 채널 이름 중 **슬랙 실물과 안 맞는 것**을 갈래별로 낸다.
 *
 * `privateChannels`·`digest.skipChannels` 는 채널을 **이름**으로 가리키는데, 거르는 자리는
 * 슬랙이 준 **현재** 이름과 대본다(`slack-archive.js` 의 `ingestConversations`,
 * `slack-live.js` 의 `fetchRecent`). 그래서 채널 이름이 바뀌면 그 줄은 아무것도 안 가리키는
 * 문자열이 되는데 **에러도 경고도 안 난다** — 2026-08-10 에 `z_비공개마_옛이름` →
 * `z_비공개마` 개명으로 `skipChannels` 한 줄이 21일간 아무것도 안 막았다.
 *
 * **갈래를 가르는 것이 요점이다.** 「실물에 그 이름이 없다」로만 내면 이 워크스페이스에서
 * 오늘 6줄이 뜨고 그중 5줄이 **의도적으로 남긴 없어진 채널**이다(커밋 `db37d7d`). 매일 울리는
 * 정상 경고는 곧 안 읽히고, 그러면 진짜 개명도 같이 묻힌다.
 *
 * 개명과 삭제를 가르는 열쇠는 `.sync-state.json` 의 `file` 이다 — 채널을 개명해도 md 파일명은
 * 안 바꾸므로 그 칸에 **옛 이름이 화석으로 남는다**(`slack-archive.js` 의 `fileName`).
 * 그래서 옛 이름 → 살아 있는 새 이름을 되짚을 수 있다.
 *
 * | kind | 뜻 | 해야 할 일 |
 * |---|---|---|
 * | `renamed` | 채널은 살아 있고 이름만 바뀌었다 | **설정 줄을 새 이름으로 고친다** |
 * | `gone` | 슬랙에 그 채널이 아예 없다 | 대개 그대로 둔다 (없어진 채널을 계속 막는 줄) |
 * | `unknown` | 어디에서도 본 적 없는 이름 | 오타이거나, 처음부터 안 다뤄 기록이 없는 채널이다 |
 *
 * **`renamed` 의 「새 이름으로 고친다」는 2026-09-16 부터 두 목록 모두에 안전하다.** 전에는
 * `privateChannels` 를 새 이름으로 고치면 언급 가리기가 죽고, `skipChannels` 를 새 이름으로
 * 고치면 읽기 쪽 거르기가 죽었다 — **이 표가 시키는 대로 하면 한쪽이 깨졌다.** 지금은 권한
 * 판정과 `withoutSkipped` 가 둘 다 개명을 되짚으므로 새 이름으로 고쳐도 둘 다 산다.
 *
 * **수집·백필 쪽 네 자리도 같은 날부터 되짚는다** (2026-09-16). 전에는 읽기 계층만
 * 되짚어서, `skipChannels` 를 옛 이름인 채로 두면 슬랙의 **현재** 이름과 글자 그대로
 * 대보는 쓰기 자리들이 죽었다 — 안 다루기로 한 **공개** 채널이 아카이브에 다시 쌓이고
 * 일일 요약에 실렸다(2026-08-10 에 21일간 벌어진 모양). 지금은 그 네 자리도 전부
 * 되짚는다(`slack-live.js` 의 `dropSkippedChannels` 를 `fetchWindow` 와
 * `ingest/slack-archive.js` 의 `ingestConversations` 가 쓰고, `ingest/backfill.js` 의
 * `backfillTargets` 와 doc-archive 의 `select_channels` 는 각자 같은 canon 대조를 한다).
 * **그래도 이 표의 「새 이름으로 고친다」를 따르는 것이 맞다** — 어느 철자든 살지만,
 * 설정이 실물과 같은 이름을 들고 있어야 사람이 읽을 때 안 헷갈린다.
 *
 * `unknown` 을 `gone` 과 따로 두는 이유: 처음부터 `skipChannels` 인 채널은 수집을 안 거치므로
 * `.sync-state.json` 에 아예 안 들어간다. 그런 채널이 개명되면 **로컬에는 되짚을 근거가 없다** —
 * 모르는 것을 「없어졌다」고 단정하지 않는다.
 *
 * 순수 함수다 — 슬랙에도 파일에도 닿지 않는다.
 *
 * @param {object} args
 * @param {Array<{id:string,name:string}>} args.live 슬랙 실물 (`listSlackChannels`)
 * @param {Array<{id:string,name:string,file?:string}>} args.known 아카이브가 아는 채널 (`listAllChannels`)
 * @param {Record<string,Iterable<string>>} args.lists 목록 이름 → 채널 이름들
 * @returns {Array<{list:string,name:string,kind:'renamed'|'gone'|'unknown',now?:string}>}
 *   위험한 것(`renamed`)이 앞에 온다 — 뒤로 가면 `gone` 여러 줄에 묻힌다.
 */
export function staleChannelRefs({ live = [], known = [], lists = {} }) {
  const liveNames = new Set(live.map((c) => normalizeChannel(c.name)));
  const liveIds = new Set(live.map((c) => c.id));

  /* 옛 이름 → 그 채널의 현재 이름. `file` 이 md 파일명이자 개명 전 이름이다. */
  const wasNamed = new Map();
  for (const c of known) {
    const now = normalizeChannel(c.name);
    const then = normalizeChannel(c.file);
    if (then && then !== now) wasNamed.set(then, c);
  }
  const knownNames = new Set(known.map((c) => normalizeChannel(c.name)));

  const out = [];
  for (const [list, names] of Object.entries(lists)) {
    for (const n of names || []) {
      const name = normalizeChannel(n);
      if (liveNames.has(name)) continue;

      const was = wasNamed.get(name);
      // 개명은 **그 채널이 아직 살아 있을 때만** 개명이다. ID 가 슬랙에서 사라졌으면
      // 이름이 바뀐 뒤 없어진 것이라, 고칠 설정 줄이 없으니 `gone` 이 맞다.
      if (was && liveIds.has(was.id)) {
        out.push({ list, name, kind: 'renamed', now: normalizeChannel(was.name) });
      } else if (was || knownNames.has(name)) {
        out.push({ list, name, kind: 'gone' });
      } else {
        out.push({ list, name, kind: 'unknown' });
      }
    }
  }
  const rank = { renamed: 0, gone: 1, unknown: 2 };
  return out.sort((a, b) => rank[a.kind] - rank[b.kind]);
}

/**
 * 아직 봇이 안 들어간 채널을 「초대할 것」과 「안 다루기로 한 것」으로 가른다.
 *
 * **모집단을 둘 합치는 이유.** 슬랙 실물(`listSlackChannels`)만 보면 봇이 나가 버린 채널이
 * 목록에서 사라지고, 아카이브 기록(`listAllChannels`)만 보면 **한 번도 수집한 적 없는 새
 * 채널이 원리상 안 보인다** — `.sync-state.json` 에 채널을 적는 것은 자동 반영뿐이고 그
 * 열거가 `users.conversations`(봇이 이미 멤버인 채널)라서다. 2026-08-15 에 새 채널 하나
 * (8/3 개설·봇 미참여)가 `join_channels.py` 에만 뜨고 여기엔 안 뜬 것이 그 경우다.
 *
 * 맞추는 열쇠는 이름이 아니라 **채널 ID** 다 — 개명에 안 흔들린다.
 *
 * @param {object} args
 * @param {Array<{id:string,name:string,isPrivate:boolean,members?:number}>} args.live 슬랙 실물
 * @param {Array<{id:string,name:string,private:boolean}>} args.known 아카이브가 아는 채널
 * @param {Set<string>} args.joinedIds 봇이 멤버인 채널 ID
 * @param {Set<string>} args.archived 채널 md 가 있는 이름
 * @param {Iterable<string>} args.skip config 의 digest.skipChannels
 */
export function uninvitedChannels({ live = [], known = [], joinedIds, archived = new Set(), skip = [] }) {
  const universe = new Map();
  for (const c of known) {
    universe.set(c.id, { id: c.id, name: c.name, private: !!c.private, members: null, inSlack: false });
  }
  for (const c of live) {
    // 슬랙 쪽을 덮어쓴다 — 개명·공개 전환은 여기가 사실이고, config 의 privateChannels 는
    // 손으로 적는 목록이라 어긋날 수 있다.
    universe.set(c.id, {
      id: c.id,
      name: c.name,
      private: !!c.isPrivate,
      members: typeof c.members === 'number' ? c.members : null,
      inSlack: true,
    });
  }

  const skipNames = new Set(skip);
  const missing = [...universe.values()]
    .filter((c) => !joinedIds.has(c.id))
    .map((c) => ({ ...c, hasArchive: archived.has(c.name) }))
    .sort((a, b) => a.name.localeCompare(b.name, 'ko'));

  return {
    actionable: missing.filter((c) => !skipNames.has(c.name)),
    ignored: missing.filter((c) => skipNames.has(c.name)),
  };
}

function channelPath(name) {
  return path.join(CHANNELS_DIR, `${name}.md`);
}

export function fold(s) {
  return String(s).toLowerCase().replace(/[\s_\-#]/g, '');
}

/**
 * 사용자가 쓴 채널명을 실제 파일명으로 해석한다.
 * 줄여 쓴 이름·`#` 붙인 이름·띄어 쓴 이름을 모두 한 이름으로 푼다
 * (`#사업장나`·`사업장 나` → `사업장나`).
 * @returns {{ok: true, name: string} | {ok: false, candidates: string[]}}
 */
export function resolveChannel(input, all = listArchivedChannels()) {
  const raw = normalizeChannel(input);
  if (all.includes(raw)) return { ok: true, name: raw };

  const target = fold(raw);
  if (!target) return { ok: false, candidates: [] };

  const exact = all.filter((c) => fold(c) === target);
  if (exact.length === 1) return { ok: true, name: exact[0] };

  const partial = all.filter((c) => fold(c).includes(target) || target.includes(fold(c)));
  if (partial.length === 1) return { ok: true, name: partial[0] };
  return { ok: false, candidates: partial.length ? partial : all };
}

/**
 * 채널명을 풀되 **권한까지 본다.** 볼 수 있는 채널 안에서 먼저 풀고, 거기 없으면 전체에서
 * 다시 풀어 "막혔다"와 "없다"를 가른다. 후보 목록에도 볼 수 있는 채널만 넣는다 —
 * 후보 목록은 그대로 봇에게 가고, 걸러지지 않으면 비공개 채널 이름이 답변에 나온다.
 */
export function resolveChannelFor(input, access) {
  // `listReadableChannels` 라 안 다루기로 한 채널은 여기서 이미 빠진다. 그러면 아래
  // `resolveChannel(input)`(거르지 않은 전체 목록) 이 걸려 BLOCKED_NOTE 로 닫힌다 —
  // "찾지 못했습니다. 후보: …" 쪽으로 떨어뜨리지 않으려는 것이다. 그 문구는 물어본
  // 이름을 되돌려 주고 「없다」고 말하는데, 있는 것을 없다고 하는 것이라 사실도 아니다.
  // (2026-09-03, listReadableChannels 주석 참조)
  const visible = listReadableChannels().filter((c) => canSee(access, c));
  const r = resolveChannel(input, visible);
  if (r.ok) return r;
  // 아카이브에 md 가 없는 비공개 채널도 여기서 잡는다. 아래 '찾지 못했습니다' 문구는
  // 물어본 이름을 되돌려 주므로, 비공개를 가리킨 입력은 그 문구에 닿게 두면 안 된다.
  if (matchesHiddenPrivate(input, access)) return { ok: false, error: BLOCKED_NOTE };
  // 안 다루기로 한 채널은 **비공개가 아니다.** 권한 차단 문구로 닫으면 봇이 그것을 그대로
  // 옮겨 공개 채널을 두고 "비공개라서 못 본다"고 답한다 — 막히는 것은 맞고 이유가 틀리다.
  // 비공개 판정(바로 위)을 **먼저** 두는 순서가 중요하다: 두 목록에 다 적힌 채널은
  // 비공개 쪽으로 닫혀야 한다. (WHK 지시 2026-09-03, config.js 의 OUT_OF_SCOPE_NOTE 주석)
  if (resolveChannel(input, skippedChannels()).ok) return { ok: false, error: OUT_OF_SCOPE_NOTE };
  if (resolveChannel(input).ok) return { ok: false, error: BLOCKED_NOTE };
  return { ok: false, error: `'${input}' 채널을 찾지 못했습니다. 후보: ${visible.slice(0, 15).join(', ')}` };
}

/**
 * 본문이 시작됐다는 표식. **메타 블록을 여기까지만 찾는다** (2026-08-13 리뷰).
 *
 * 전에는 첫 `>` 를 **파일 끝까지** 찾았다. 채널 md 의 스레드 답글이 `>` 로 시작하므로,
 * 상단 메타가 없거나 깨진 파일에서는 **파일 중간의 스레드 인용을 메타로 집어 올렸다** —
 * 그 값이 봇 시스템 프롬프트의 채널 색인에 그대로 실린다. 에러는 안 난다.
 * (실측 2026-08-13: 채널 md 43개·문서 md 292개 전부 첫 `>` 가 3행 안에 있다.)
 */
const BODY_START = (line) => /^##\s/.test(line) || /^\*\*\d{4}-/.test(line) || line.trim() === '---';

/** 메타 블록을 여기까지만 찾는다. 실측 3행이라 넉넉하다 — 넘으면 그 파일은 계약 밖이다. */
const META_SCAN_LIMIT = 40;

/**
 * md 파일 상단의 인용(>) 메타 블록만 뽑는다.
 * 채널 md 는 기간·메시지 수·핵심 쟁점이, 문서 md 는 사업장·출처·열람이 들어 있다.
 * 두 아카이브가 같은 관례를 쓰므로 documents.js 가 이 함수를 그대로 재사용한다.
 *
 * **시작을 못 찾은 채 본문에 닿으면 빈 값을 돌려준다** — 위 `BODY_START` 참조.
 * 없는 것을 빈 값으로 답하는 것이 남의 발언을 메타로 싣는 것보다 안전하다.
 */
export function metaBlock(absPath) {
  const text = readCached(absPath);
  const lines = text.split('\n');
  const meta = [];
  let started = false;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!started && (i >= META_SCAN_LIMIT || BODY_START(line))) break;
    if (line.startsWith('>')) {
      started = true;
      meta.push(line.replace(/^>\s?/, '').trim());
    } else if (started && line.trim() === '') {
      continue;
    } else if (started) {
      break;
    }
  }
  return meta.filter(Boolean).join('\n');
}

/**
 * 채널 md 의 메타 블록 **+ 첫 메시지 앞에 사람이 적어 둔 정리.** 시스템 프롬프트의
 * 라우팅 컨텍스트로 쓰인다.
 *
 * 머리말을 여기 함께 싣는 이유는 `preambleOf` 주석에 있다 — 요약하면, 색인에 그 표의
 * 존재조차 없으면 봇이 `read_channel` 을 부를 이유를 모른다.
 *
 * **다만 색인에는 「무슨 표가 있다」만 싣고 숫자는 안 싣는다** (WHK 결정 2026-09-03).
 * 표 원문을 통째로 실으면 공통 블록이 26,114 → 28,979자가 되어 경고선(30,000) 까지
 * 남은 여유의 4분의 3을 이 한 건이 먹는다. 헤딩만 실으면 그 값이 26,114 → 26,3xx 수준이다.
 * **숫자를 잃는 것이 아니다** — 검색(`scanArchive`)은 머리말 **전문**을 그대로 훑으므로
 * 표 안의 숫자로 찾으면 그대로 나오고, 봇은 헤딩을 보고 `read_channel` 을 부를
 * 이유를 안다. 색인은 「어디를 열어 보라」는 지도이지 자료 자체가 아니다.
 *
 * **문서 md 는 이 함수를 안 쓴다** (`documents.js` 는 `metaBlock` 을 직접 부른다). 문서
 * 쪽에도 같은 구간이 있는지는 따로 재야 한다.
 */
function channelBrief(name) {
  const p = channelPath(name);
  const meta = metaBlock(p);
  const pre = preambleOf(readCached(p));
  if (!pre) return meta;
  const block = `${PREAMBLE_INDEX_MARK} ${preambleOutline(pre)}`;
  return meta ? `${meta}\n${block}` : block;
}

/**
 * 머리말에서 **헤딩 글자만** 뽑아 한 줄로 잇는다. 색인에 들어가는 것은 이것뿐이다.
 *
 * `#` 을 떼는 이유 — 색인은 `## #채널명` 으로 채널 블록을 여는데, 머리말 헤딩을 `##` 째로
 * 실으면 한 채널 안에 `## ` 줄이 또 생긴다. 지금은 아무도 색인을 `^## ` 로 쪼개지 않지만
 * 그렇게 쪼개는 코드가 생기면 **가짜 채널 블록이 잡히고 에러는 안 난다.**
 *
 * 헤딩이 하나도 없는 머리말이면 첫 줄을 대신 쓴다 — 표만 있고 제목이 없는 경우다.
 *
 * **문서 색인도 이 함수를 쓴다** (`documents.js` 의 `buildDocumentsBrief`). 2026-09-03 까지는
 * 저쪽에 `docPreambleOutline` 이라는 **글자까지 같은 사본**이 있었다 — 이 함수가 `export` 가
 * 아니라 가져다 쓸 수가 없어서였다. 한쪽만 고치면 **같은 머리말인데 채널 색인과 문서 색인이
 * 다른 제목을 싣고 에러는 안 난다.** 그래서 `export` 를 붙이고 사본을 지웠다.
 * **여기를 고치면 두 색인이 함께 바뀐다** — 그것이 노린 것이다.
 * 사본이 다시 생기는 것은 `scripts/check-doc-preamble.js` 의 [2/5] 가 막는다.
 */
export function preambleOutline(pre) {
  const heads = pre.split('\n')
    .map((l) => l.trim())
    .filter((l) => /^#{2,6}\s+\S/.test(l))
    .map((l) => l.replace(/^#+\s+/, ''));
  if (heads.length) return heads.join(' · ');
  return pre.split('\n').map((l) => l.trim()).find(Boolean) ?? '';
}

/**
 * 시스템 프롬프트에 항상 실리는 색인.
 * index.md 전문 + 채널별 메타 블록. 약 10~13K 토큰이며 프롬프트 캐시 대상.
 * @param {{access: object}} opts 열람 권한 (config.js 의 PUBLIC_ACCESS / accessFor / FULL_ACCESS)
 */
export function buildArchiveBrief({ access }) {
  assertArchive();
  const parts = [getIndexText().trim(), '\n\n---\n\n# 채널별 요약 (각 md 파일 헤더)\n'];
  // 안 다루기로 한 채널은 권한과 무관하게 뺀다 (2026-09-03, listReadableChannels 주석).
  for (const name of listReadableChannels()) {
    if (!canSee(access, name)) continue;
    const brief = channelBrief(name);
    if (!brief) continue;
    parts.push(`\n## #${name}${isPrivateChannel(name) ? ' 🔒' : ''}\n${brief}\n`);
  }
  // 채널 단위로 거른 뒤 **줄 단위로 한 번 더** 거른다. index.md 전문과 공개 채널의 메타
  // 블록에도 비공개 채널 내용이 문장으로 섞여 들어오기 때문이다 (redactPrivateMentions 주석).
  return redactPrivateMentions(parts.join(''), access);
}

/**
 * 색인에 사람이 적어 둔 **채널 수**를, 그 블록에 실제로 남아 있는 표 행으로 다시 적는다.
 *
 * 왜 필요한가 — `index.md` 의 `### 공통 · 운영 (12)` 와 `> **채널**: 총 50개 (공개 46 + 비공개 4)`
 * 는 **사람이 보는 파일의 참값**이다. 그런데 그 파일은 공통 블록에 통째로 실리고, 그 뒤
 * `redactPrivateMentions` 가 볼 수 없는 자리를 언급한 행을 지운다. 그래서 **읽는 사람에게는
 * 12줄이라고 적힌 자리에 7~8줄만 보인다** — 차이가 곧 「내가 못 보는 자리가 몇 개 있다」는
 * 정보이고, 총계 줄은 아예 `비공개 4` 라고 개수를 적어 준다. 실제로 봇이 답변에
 * "채널 목록(공개 46 + 비공개 4, 총 50개)에도 그 이름은 없고…" 라고 옮긴 적이 있다.
 *
 * 그래서 **지우는 것으로는 안 되고 다시 세야 한다.** 빠진 행에는 공개 채널도 섞여 있어
 * (비공개 채널을 언급한 공개 채널 행) 비공개 개수를 빼는 계산으로는 맞출 수 없다.
 *
 * **반드시 가린 뒤에 부른다.** 가리기 전에 세면 원래 숫자가 그대로 나와 아무것도 안 고친다.
 * 공통 블록은 `PUBLIC_ACCESS` 로 고정해 만들므로 결과도 누구에게나 같다 — 바이트 동일성이 깨지지
 * 않는다 (검증: scripts/check-brief-split.js ①③).
 *
 * `index.md` 자체는 건드리지 않는다. 사람 파일에는 참값이 남아 있어야 한다.
 */
function recountVisibleChannels(text) {
  const SECTION = /^(###\s+.+?)\s*\((\d+)\)\s*$/;
  const HEADING = /^#{1,6}\s/;
  const TABLE_SEP = /^\|[\s:|-]+\|\s*$/;
  // 아카이브 md 는 CRLF 라 줄 끝에 `\r` 이 남는다. `.` 는 `\r` 을 안 먹으므로 `[^\n]` 로 잡고,
  // 다시 적을 때 그 `\r` 을 되돌려 준다 (안 그러면 이 줄만 줄바꿈이 달라진다).
  const TOTAL = /^(>\s*\*\*채널\*\*:)[^\n]*$/;
  const eol = (l) => (l.endsWith('\r') ? '\r' : '');

  const lines = text.split('\n');
  const sections = [];
  let cur = null;
  let inTable = false; // 정렬 구분선(|---|---|) 다음부터가 데이터 행. 머리글 행을 안 세려는 것.
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const m = line.match(SECTION);
    if (m) {
      cur = { at: i, title: m[1], rows: 0 };
      sections.push(cur);
      inTable = false;
      continue;
    }
    if (HEADING.test(line)) { cur = null; inTable = false; continue; }
    if (!cur) continue;
    if (TABLE_SEP.test(line)) { inTable = true; continue; }
    if (line.startsWith('|')) { if (inTable) cur.rows += 1; } else { inTable = false; }
  }
  // 절 제목에 수가 안 적힌 색인(다른 워크스페이스)이면 셀 근거가 없다. 그때는 손대지 않는다.
  if (!sections.length) return text;

  for (const s of sections) lines[s.at] = `${s.title} (${s.rows})${eol(lines[s.at])}`;
  const total = sections.reduce((n, s) => n + s.rows, 0);
  const at = lines.findIndex((l) => TOTAL.test(l));
  // 총계도 이 블록에 실제로 남은 것만 말한다. 공개/비공개 내역은 적지 않는다 — 그 내역이 곧
  // 「못 보는 자리가 몇 개인가」다.
  if (at >= 0) lines[at] = lines[at].replace(TOTAL, (m, p1) => `${p1} 총 ${total}개${eol(m)}`);
  return lines.join('\n');
}

/**
 * 같은 색인을 **캐시가 걸리는 모양으로** 두 덩이로 나눈다.
 *
 * 왜 나누나 — 시스템 프롬프트는 1시간 캐시가 걸려 있는데, 캐시 항목이 **열람 권한 조합마다
 * 한 벌씩** 생긴다. 그래서 질문자가 바뀔 때마다 2.7만 토큰을 통째로 다시 저장했다
 * (2026-08-05~10 실측: 문답 49건 중 29건이 재작성, 그 값이 캐시 쓰기 비용 $7.82 의 거의 전부).
 *
 * 그런데 **권한에 따라 달라지는 것은 8.5%뿐이다** — 색인 24,135자 중 비공개 채널 메타 1,171자와
 * 공개 자료 안에서 비공개 채널을 언급해 가려지는 8줄 889자. 나머지 22,157자는 누구에게나 같다.
 * 그 92%를 앞 블록에 몰아 두면 첫 사람이 쓰고 나머지는 읽는다(1/10 단가).
 *
 * **`common` 은 바이트 단위로 같아야 한다** — 한 글자만 달라도 캐시가 안 맞아 절감이 통째로
 * 사라진다. 그래서 `access` 를 아예 보지 않고 `PUBLIC_ACCESS` 로 고정해 만든다.
 *
 * 대가는 **순서**다. 비공개 채널 메타와 가려졌던 줄이 제자리가 아니라 뒤 블록에 모여 나온다.
 * 내용은 하나도 안 빠지고 늘지도 않는다 (검증: scripts/check-brief-split.js).
 *
 * @param {{access: object}} opts
 * @returns {{common: string, extra: string}} extra 는 볼 것이 없으면 빈 문자열
 */
export function buildArchiveBriefSplit({ access }) {
  assertArchive();

  // ── 공통: 공개 채널만. access 를 보지 않는다 ──
  const parts = [getIndexText().trim(), '\n\n---\n\n# 채널별 요약 (각 md 파일 헤더)\n'];
  // 안 다루기로 한 채널은 권한과 무관하게 뺀다 (2026-09-03, listReadableChannels 주석).
  // 여기는 누구에게나 같은 공통 블록이라, 안 거르면 **공개 질문자에게** 그대로 실린다.
  for (const name of listReadableChannels()) {
    if (isPrivateChannel(name)) continue;
    const brief = channelBrief(name);
    if (!brief) continue;
    parts.push(`\n## #${name}\n${brief}\n`);
  }
  const raw = parts.join('');
  // 가리기 **다음에** 다시 센다. 순서가 바뀌면 원래 숫자가 남아 못 보는 자리의 개수가 샌다.
  const common = recountVisibleChannels(redactPrivateMentions(raw, PUBLIC_ACCESS));

  // ── 추가분 ① 공통에서 가려졌지만 이 권한에서는 살아나는 줄 ──
  // 판정에 새 정규식을 쓰지 않고 redactPrivateMentions 를 한 줄씩 그대로 쓴다. 가리는 규칙이
  // 두 곳에 생기면 언젠가 갈리고, 갈리는 쪽이 늘 새는 쪽이다.
  // 지도는 반복문 **밖에서** 한 번 뜬다 — 줄마다 두 번씩 뜨면 stat 이 줄 수의 두 배가 된다.
  const map = currentChannelNames();
  const revived = raw
    .split('\n')
    .filter((line) => line.trim())
    .filter((line) => !redactPrivateMentions(line, PUBLIC_ACCESS, map) && redactPrivateMentions(line, access, map));

  // ── 추가분 ② 이 권한으로 볼 수 있는 비공개 채널의 메타 ──
  // 비공개 채널 메타에도 **다른** 비공개 채널이 언급될 수 있으므로 여기서도 가린다.
  const privateBriefs = [];
  // 비공개이면서 안 다루기로 한 채널이 있다 (설정 두 목록에 다 적힌 채널). 그것이 이 뒤
  // 블록으로 새지 않게 여기서도 거른다 (2026-09-03).
  for (const name of listReadableChannels()) {
    if (!isPrivateChannel(name) || !canSee(access, name)) continue;
    const brief = redactPrivateMentions(channelBrief(name), access).trim();
    if (!brief) continue;
    privateBriefs.push(`\n## #${name} 🔒\n${brief}\n`);
  }

  if (!revived.length && !privateBriefs.length) return { common, extra: '' };

  // 규모는 **여기서만** 말한다. 앞 색인의 수는 그 블록에 남은 것만 세어 적혀 있고(recountVisibleChannels),
  // 거기에 여기 것을 더해 적으면 공개 질문자에게 가는 앞 블록이 권한마다 달라진다.
  const scale = [
    privateBriefs.length ? `비공개 채널 ${privateBriefs.length}개` : '',
    revived.length ? `앞 색인에서 빠져 있던 ${revived.length}줄` : '',
  ].filter(Boolean).join(' · ');

  const extra = [
    '# 추가 열람분',
    '',
    '아래는 위 색인에서 열람 권한 때문에 빠져 있던 자료입니다. **이 질문자에게는 열려 있으므로',
    '인용해도 됩니다.** 위 색인과 같은 성격의 것이며, 여기에도 원문은 없습니다 — 도구로 확인하세요.',
    '',
    `이 질문자에게 더 열리는 것은 ${scale}입니다.`,
    '',
    ...(revived.length ? ['## 위 색인에서 빠졌던 줄', '', ...revived, ''] : []),
    ...(privateBriefs.length ? ['## 비공개 채널 요약', privateBriefs.join('')] : []),
  ].join('\n');

  return { common, extra };
}

/**
 * 메시지(문서 md 는 회차) 헤더. **두 아카이브의 공통 계약이다** — 파이썬 쪽 스킬
 * (`slack-sync/scripts/insert_messages.py` · `doc-archive/scripts/verify_format.py`)이
 * 같은 모양을 따로 적어 두고 있다. 여기를 바꾸면 그쪽도 함께 바꿔야 한다.
 */
const MESSAGE_HEADER = /^\*\*(\d{4}-\d{2}-\d{2})[^*]*\*\*\s*$/;

/**
 * 월 구분 헤딩(`## 2026-08`). **사람이 쓴 헤딩(`## 호텔 운영실적 추이`)과 가르는 자리다.**
 *
 * **정본은 `^##\s+`(관대한 쪽)다** (WHK 결정 2026-09-03) — 나머지 여섯 자리
 * (`summary.js`·`ingest/verify.js`·`insert_entry.py`·`verify_format.py`·`insert_messages.py`·
 * `review_work.py`)가 전부 이미 이 모양이었다. 전에는 여기(그리고 `readChannel` 의 월 찾기)만
 * 공백 한 칸으로 엄격해서, `##  2026-08`(두 칸)이 관문은 통과하는데 봇의 월 단위 읽기만 못 찾는
 * 자리였다(2026-09-02 전수조사). `readChannel` 의 월 찾기도 아래에서 같이 넓힌다.
 */
const MONTH_HEADING = /^##\s+\d{4}-\d{2}\s*$/;

/**
 * 파일을 '**YYYY-MM-DD HH:MM · 이름**' 단위 메시지 블록으로 자른다.
 * 문서 md 도 회차 헤더를 '**YYYY-MM-DD · 원본파일명**' 으로 맞춰 두어
 * documents.js 가 이 함수를 그대로 쓴다. 이 정규식이 두 아카이브의 공통 계약이다.
 *
 * **첫 헤더 앞의 줄은 여기서 안 나온다** (블록이 아직 안 열려 `cur` 가 null 이다). 그 구간을
 * 따로 내는 것이 아래 `preambleOf` 다 — 이 함수의 결과로 **회차 수를 세는 자리가 열 곳이 넘어**
 * (`documents.js`·`ingest/verify.js`·`check-excel-sheets.js`·`check-doc-outline.js`) 여기서
 * 가상 블록을 하나 끼우면 그 수가 전부 하나씩 틀어진다.
 */
export function splitMessages(text) {
  const lines = text.split('\n');
  const blocks = [];
  let cur = null;
  for (const line of lines) {
    const m = line.match(MESSAGE_HEADER);
    if (m) {
      if (cur) blocks.push(cur);
      cur = { date: m[1], lines: [line] };
    } else if (cur) {
      cur.lines.push(line);
    }
  }
  if (cur) blocks.push(cur);
  return blocks.map((b) => ({ date: b.date, text: b.lines.join('\n').trim() }));
}

/**
 * 첫 메시지 헤더 **앞**에 사람이 손으로 적어 둔 부분. 없으면 빈 문자열.
 *
 * ── 왜 생겼나 (2026-09-03) ──
 *
 * 채널 md 상단에는 사람이 표로 정리해 둔 것이 있다 — 사업장 채널이면 운영실적 추이,
 * 자금 구조 요약, 진행 타임라인 같은 것이고, 그 채널에서 가장 공들여 만든 숫자다
 * (실물은 자료 저장소의 채널 md 들). 그런데 **읽는 두 경로 모두에서 그 구간이 없었다.**
 * 검색(`scanArchive`)은 `splitMessages` 가 낸 블록만 훑는데 첫 헤더 앞은 블록이 아니고,
 * 색인(`channelBrief`)은 상단 `>` 인용줄(`metaBlock`)만 가져온다. 그래서 그 표에만 있는
 * 숫자를 물으면 봇은 0건을 받고 **「아카이브에 없다」로 닫았다** — 에러는 안 난다.
 *
 * ── 무엇을 빼나 ──
 *
 * | 빼는 것 | 이유 |
 * |---|---|
 * | 파일 제목 `# #채널명` | 채널 이름은 색인·검색 결과에 이미 붙는다 |
 * | 상단 `>` 메타 덩어리 | `metaBlock` 이 이미 색인에 싣는다. 두 번 실린다 |
 * | 월 헤딩 `## 2026-08` | 사람이 쓴 것이 아니라 자동 반영이 넣는 구분선 |
 * | 앞뒤의 빈 줄·`---` | 구분선일 뿐 내용이 아니다 |
 *
 * 메타가 끝나는 자리는 `metaBlock` 과 **같은 규칙으로** 찾는다. 규칙을 여기 새로 적으면
 * 언젠가 갈리는데, 갈리면 메타가 두 번 실리거나 사람이 쓴 첫 줄이 사라진다.
 *
 * 첫 메시지 헤더가 **아예 없는** 파일이면 (메타 뒤) 파일 전체가 이 구간이 된다. 지금
 * 채널 md 42개에는 그런 파일이 없다 — 있으면 `ingest/verify.js` 가 먼저 잡는다.
 */
export function preambleOf(text) {
  const lines = String(text).split('\n');
  let end = lines.findIndex((l) => MESSAGE_HEADER.test(l));
  if (end === -1) end = lines.length;

  // 상단 `>` 메타 덩어리가 끝나는 줄. metaBlock 의 루프와 같은 판정이다.
  let metaEnd = 0;
  let started = false;
  for (let i = 0; i < end; i += 1) {
    const line = lines[i];
    if (!started && (i >= META_SCAN_LIMIT || BODY_START(line))) break;
    if (line.startsWith('>')) {
      started = true;
      metaEnd = i + 1;
    } else if (started && line.trim() === '') {
      continue;
    } else if (started) {
      break;
    }
  }

  const kept = lines
    .slice(metaEnd, end)
    .filter((l) => !MONTH_HEADING.test(l) && !/^#\s/.test(l));
  const filler = (l) => !l.trim() || l.trim() === '---';
  while (kept.length && filler(kept[0])) kept.shift();
  while (kept.length && filler(kept[kept.length - 1])) kept.pop();
  return kept.join('\n');
}

/**
 * 머리말을 검색 결과·색인에 실을 때 앞에 붙이는 줄.
 *
 * **누가 언제 말한 것인지가 없는 유일한 덩어리다.** 다른 히트는 전부 `**날짜 시각 · 이름**`
 * 으로 시작하는데 이것만 그렇지 않아서, 표시가 없으면 봇이 바로 위 사람의 말로 읽거나
 * 날짜를 지어낸다.
 */
const PREAMBLE_MARK = '**채널 md 상단 정리 — 사람이 손으로 적어 둔 것입니다 (특정인의 발언이 아닙니다)**';

/* 색인에 쓰는 표시. 검색 결과용(`PREAMBLE_MARK`)과 **일부러 다르다** — 검색 결과에는
 * 본문이 함께 실리므로 「누구 발언이 아니다」를 말해야 하고, 색인에는 제목만 실리므로
 * 「전문을 어디서 보나」를 말해야 한다. 한 문구로 합치면 둘 중 하나가 어긋난다. */
const PREAMBLE_INDEX_MARK = '**상단에 사람이 손으로 정리해 둔 것이 있습니다 (숫자는 `read_channel` 로 전문을 보세요)**:';

/**
 * 자리 안에서 무엇을 먼저 내놓을지 정하는 두 기준.
 *
 * **확정 히트는 날짜(`byDateDesc`)가 맞다** — 낱말을 다 갖고 있어 점수가 전부 같으므로
 * 그 안에서 가를 것은 최신성뿐이다. 그래서 이것이 기본값이다.
 *
 * **부분 일치는 점수를 먼저 본다(`byScoreThenDate`).** 여기서는 점수가 갈리는데, 날짜로만
 * 고르면 자리마다 「가장 최근 것」이 나가고 **더 잘 맞은 회차는 아예 안 보인다.**
 * 2026-08-18 실측으로 문서 8개 질의 중 8개, 대화 6개 중 5개에서 그 역전이 났다 —
 * '인사 발령 인사이동' 은 돌려준 12건이 전부 1점인데 2점짜리 6건이 통째로 빠져 있었다.
 * 같은 점수 안에서는 여전히 최신이 이기므로, **회차가 여럿인 시리즈 자료(일보·주간보고)는
 * 예전과 결과가 같다** — 순서가 바뀌는 것은 서로 다른 자료끼리 점수가 갈릴 때뿐이다.
 * 실측 대가는 중앙 나이 11일 → 18일이고, 평균 점수는 1.63 → 2.09 로 올랐다.
 */
export const byDateDesc = (a, b) => b.date.localeCompare(a.date);
export const byScoreThenDate = (a, b) => b.score - a.score || b.date.localeCompare(a.date);

/**
 * 상한을 자리(채널·사업장)들이 **나눠 갖게** 고른다.
 *
 * 예전에는 상한에 차는 순간 훑기를 멈췄다. 훑는 순서가 이름순이라, 이름이 뒤인 자리는
 * 자료가 아무리 많아도 한 글자도 안 읽혔다 — '보증' 검색이 35개 채널 중 22번째에서 끊겨
 * 사업장가·사업장자와 한 채널군 4곳이 통째로 빠졌다 (2026-08-05 실측).
 * 문서 쪽은 더 심해서, 흔한 낱말 8개가 전부 밑줄로 시작하는 공통 폴더 3개에서 상한을
 * 다 썼다 ('연체이자' 는 실제로 14개 사업장 50건인데 봇은 3개 폴더 12건만 봤다).
 *
 * ① 라운드 로빈으로 자리마다 perGroup 건까지 — 예산이 모자라도 모든 자리가 보인다
 * ② 남는 자리는 최근순으로 채운다
 */
export function pickSpread(groups, maxHits, perGroup, order = byDateDesc) {
  // 자료가 많은 자리부터 돈다. 이름순으로 돌면 예산이 모자랄 때 **또다시** 이름이 앞인 자리만
  // 남는다 — 고치려던 것과 같은 모양이 된다.
  const lists = [...groups.values()]
    .map((l) => [...l].sort(order))
    .sort((a, b) => b.length - a.length);
  const picked = [];
  for (let round = 0; round < perGroup && picked.length < maxHits; round += 1) {
    for (const l of lists) {
      if (picked.length >= maxHits) break;
      if (l[round]) picked.push(l[round]);
    }
  }
  if (picked.length < maxHits) {
    const taken = new Set(picked);
    const rest = lists
      .flat()
      .filter((x) => !taken.has(x))
      .sort(order);
    picked.push(...rest.slice(0, maxHits - picked.length));
  }
  return picked.sort(order);
}

/**
 * 상한에 걸렸을 때 **어디에 더 있는지**를 알려주는 문구.
 *
 * "상위 N건만 반환했습니다" 라고만 적으면 봇이 그것을 관련도 순으로 읽고 나머지를 없는 것으로
 * 취급한다. 자리별 건수를 함께 줘야 봇이 좁혀서 다시 물어본다.
 */
export function spreadNote(groups, total, shown, hint) {
  if (shown >= total) return undefined;
  const tally = [...groups.entries()]
    .map(([k, v]) => [k, v.length])
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${k} ${n}`)
    .join(' · ');
  return `전체 ${total}건 (${tally}). 아래에는 자리별로 골고루 ${shown}건만 실었습니다. ${hint}`;
}

/**
 * 아카이브 전문 검색. 공백으로 나눈 모든 낱말이 한 메시지 블록 안에 다 있어야 매칭(AND).
 * 매칭된 메시지 블록 전체를 돌려주므로 날짜·작성자가 함께 보인다.
 * 0건일 때는 일부만 맞은 것을 대신 돌려준다 (아래 partials 분기, pickSpread 로 채널마다 나눈다).
 */
function scanArchive({
  query, channel, access,
  maxHits = config.limits.searchMaxHits,
  perChannel = config.limits.searchMaxPerChannel ?? 8,
  // hitMaxChars 0/미설정 = 무제한(끔) — 이 저장소의 0=끔 관례. 「0자로 자름」이 아니다.
  hitMaxChars = config.limits.searchHitMaxChars,
}) {
  assertArchive();
  const terms = splitTerms(query);
  if (!terms.length) return { hits: [], note: '검색어가 비어 있습니다.' };

  // 안 다루기로 한 채널은 여기서 빠진다 (2026-09-03, listReadableChannels 주석). 아래
  // `channel` 로 좁히는 길도 `resolveChannelFor` 가 같은 목록을 보므로 함께 막힌다.
  let channels = listReadableChannels();
  if (channel) {
    // 채널을 짚어 물었는데 볼 권한이 없으면 그렇게 말해 준다. 그냥 0건으로 돌려주면
    // 봇이 "자료가 없습니다"로 오답한다 — 있는데 못 보는 것과 없는 것은 다르다.
    const r = resolveChannelFor(channel, access);
    if (!r.ok) return { hits: [], note: r.error };
    channels = [r.name];
  }
  channels = channels.filter((c) => canSee(access, c));

  // 상한에 걸려도 **끝까지 훑는다.** 35개 채널 372KB 라 비용이 없고, 중간에 멈추면
  // 그 뒤 채널은 존재하지 않는 것이 된다.
  const groups = new Map();
  const partials = [];           // 일부만 맞은 것 (0건일 때만 쓴다)
  const perTerm = new Map(terms.map((t) => [t, 0]));
  let total = 0;
  for (const name of channels) {
    /* 개명 지도는 **채널마다** 뜬다 — 메시지마다도, 검색 한 번에 한 번도 아니다.
     *
     * 메시지마다 뜨면 stat 이 메시지 수만큼 쌓인다(실측 28,722회 · 2,040ms). 그렇다고
     * 검색 한 번에 한 번만 뜨면, **검색이 도는 중간에 수집이 `.sync-state.json` 을 갱신할 때
     * 그 검색이 통째로 옛 지도로 끝난다** — 갱신 전이라 지도가 비어 있었다면 그 뒤 채널의
     * 옛 이름 줄이 금액째 공개 답변에 실린다(재현함: 히트 13건 중 12건). 창이 128ms 라
     * 좁지만 방향이 새는 쪽이고 한 번 새면 되돌릴 수 없다.
     *
     * 채널마다 뜨면 창이 「검색 한 번」에서 「채널 한 개」로 줄고 stat 은 80회→120회 남짓이다.
     * 같은 채널 안에서 갱신이 나면 그 채널만 옛 지도로 끝나는데, 그 채널 md 는 이미
     * `readCached` 로 한 번에 읽어 둔 것이라 그 안에서는 어차피 한 시점이다. */
    const map = currentChannelNames();
    const raw = readCached(channelPath(name));
    const blocks = splitMessages(raw);
    /* 첫 메시지 헤더 **앞** 구간을 블록 하나로 앞에 끼운다 (`preambleOf` 주석 참조).
     * `splitMessages` 자체는 안 건드린다 — 그 결과로 회차 수를 세는 자리가 열 곳이 넘는다.
     *
     * 날짜는 그 채널의 **가장 최근 메시지 날짜**를 쓴다. 머리말에는 날짜가 없는데
     * `byDateDesc`·`pickSpread` 가 날짜로만 줄을 세우므로 값이 있어야 한다. 이 정리는
     * 「지금 상태」를 적어 둔 것이라 그 채널 안에서 맨 앞이 맞고, 날짜 자체는 결과에
     * 안 찍힌다 (claude.js 는 `h.text` 만 그린다). 메시지가 하나도 없으면 빈 문자열이라
     * 맨 뒤로 간다 — 없는 날짜를 지어내지 않는다. */
    const pre = preambleOf(raw);
    const units = pre
      ? [{ date: blocks[0]?.date ?? '', text: `${PREAMBLE_MARK}\n${pre}` }, ...blocks]
      : blocks;
    for (const msg of units) {
      // 공개 채널 md 에도 사람이 적어 둔 `> 관련: #비공개가 "…"` 참조 줄이 있고 거기 금액이
      // 따옴표째 들어 있다. 색인만 가려서는 안 되고 **돌려주는 본문도** 가려야 한다.
      //
      // **가린 뒤에 맞춰본다.** 원문에 맞춰보고 가린 본문을 돌려주면, 가려진 줄에만 있는
      // 낱말로 검색했을 때 내용은 안 나가도 "여기 뭔가 걸린다"는 사실이 샌다.
      const text = redactPrivateMentions(msg.text, access, map);
      if (!text.trim()) continue;
      const hay = text.toLowerCase();
      const score = scoreTerms(hay, terms);
      if (score === 0) continue;
      for (const t of terms) if (hay.includes(t)) perTerm.set(t, perTerm.get(t) + 1);
      if (score < terms.length) {
        // 전부 맞은 것이 하나라도 있으면 이 목록은 안 쓴다. 그래서 모으기만 한다.
        partials.push({ channel: name, date: msg.date, text, score });
        continue;
      }
      if (!groups.has(name)) groups.set(name, []);
      groups.get(name).push({ channel: name, date: msg.date, text });
      total += 1;
    }
  }

  const hits = pickSpread(groups, maxHits, perChannel);

  /* 대화 히트 건당 상한. config(searchHitMaxChars) 로만 켠다 — 미설정(기본)이면 cap 이
   * falsy 라 clipHit 은 항등함수다(1급 doc-index-floor-restructure 계획 Task 6). 자르는
   * 방식은 앞 고정이 아니라 clipPartial(창 맞춤) 이다 — 그 함수 docstring 참조. */
  const cap = hitMaxChars;
  const clipHit = (h) => (cap ? { ...h, text: clipPartial(h.text, cap, terms) } : h);
  // note 가 없을 수 있다 — spreadNote 는 shown >= total 이면 undefined 를 돌려준다(764행).
  // 거기에 그냥 이어 붙이면 "undefined 일부 히트는…" 이 나간다. note 가 없으면 안내
  // 문장만 단독으로 낸다.
  const CAP_HINT = '일부 히트는 길이 제한으로 잘렸습니다 — 전문은 read_channel 로 보세요.';
  const noteWithCapHint = (note, list) => {
    if (!list.some((h) => h.text.includes(TRUNC_PHRASE))) return note;
    return note ? `${note} ${CAP_HINT}` : CAP_HINT;
  };

  /* 일부만 맞은 것을 자리마다 나눠 고른다. 0건일 때와 「적을 때」가 같은 코드를 쓰게 뺐다. */
  const pickPartials = () => {
    if (terms.length < PARTIAL_MIN_TERMS || !partials.length) return [];
    const partialGroups = new Map();
    for (const p of partials) {
      if (!partialGroups.has(p.channel)) partialGroups.set(p.channel, []);
      partialGroups.get(p.channel).push(p);
    }
    return pickSpread(
      partialGroups, config.limits.partialHitMaxHits ?? 12, perChannel, byScoreThenDate,
    ).map((h) => ({ ...h, termCount: terms.length }));
  };
  const termCounts = () => terms.map((t) => `'${t}' ${perTerm.get(t)}건`).join(' · ');

  if (hits.length) {
    /* **확정 히트가 적으면 일부만 맞은 것도 함께 준다** (2026-08-18).
     *
     * 안전망이 0건일 때만 켜져 있었다. 그래서 낱말을 많이 붙인 좁은 질의가 딱 한 곳을
     * 맞히면 봇은 「찾았다」고 읽고 멈췄다 — 다른 자리는 존재하지 않는 것이 된다.
     * 실측: 낱말 넷을 붙인 질의는 대화 확정 히트가 1건인데, 같은 자리를 두 낱말로
     * 물으면 다른 사업장이 첫 히트로 나온다 (실물은 자료 저장소 `사고기록.md` 의
     * 「낱말이 늘면 AND 조건이 좁아진다」 절). 통합으로 왕복이
     * 줄면서 한 질의에 담는 낱말이 늘었고(옛 1~2낱말 → 새 2~4낱말) AND 조건이 그만큼
     * 좁아졌는데, 그 좁아짐을 받아 줄 것이 없었다.
     *
     * **확정 히트는 그대로 앞에 둔다.** 뒤에 덧붙이는 것은 어디까지나 추측이고, 앞뒤가
     * 바뀌면 봇이 추측을 먼저 읽는다. partial 플래그도 켜지 않는다 — 그것은 「전부
     * 추측」이라는 뜻이라 확정 히트가 있는 결과에 붙이면 거짓이 된다. */
    const few = hits.length <= (config.limits.partialAlsoWhenAtMost ?? 2);
    const extra = few ? pickPartials() : [];
    if (extra.length) {
      const clipped = [...hits, ...extra].map(clipHit);
      return {
        hits: clipped,
        note: noteWithCapHint(
          `낱말이 **모두** 든 메시지가 ${hits.length}건뿐이라, **일부만 맞은** ${extra.length}건을 `
          + `뒤에 함께 실었습니다(제목 줄의 \`(n/${terms.length} 낱말)\`). 낱말별로는 ${termCounts()} 걸립니다. `
          + '뒤쪽은 추측이라 무관한 것이 섞여 있습니다 — 앞의 확정분을 먼저 보세요.',
          clipped,
        ),
      };
    }
    const clippedHits = hits.map(clipHit);
    return {
      hits: clippedHits,
      note: noteWithCapHint(spreadNote(groups, total, hits.length,
        '더 보려면 channel 을 지정해 다시 검색하세요. 여러 자리를 다 다뤄야 하는 질문이면 위 자리들을 read_channel 로 마저 여세요 — 한 번에 여러 채널을 함께 불러도 됩니다.'),
        clippedHits),
    };
  }

  /* 여기부터가 0건일 때다.
   *
   * 봇은 "모든 낱말이 든 것" 이 없다는 것만 알고 **어느 낱말이 문제인지는 모른다.** 그래서
   * 어휘를 바꿔가며 다시 묻는다 — 2026-08 로그에서 재검색 32회 중 28회가 그 모양이었다
   * ('중순위 470억' → '470억 대주' → '중순위 대주' → …). 그 왕복 한 번이 약 $0.055 다.
   * 그래서 여기서 **일부만 맞은 것을 겹친 개수 순으로** 함께 돌려준다. 봇이 다시 묻지
   * 않아도 되고, 낱말별 건수를 보면 어느 낱말이 헛도는지도 그 자리에서 보인다. */
  /* pickPartials 안에 두 가지가 함께 들어 있다 —
   *  ① 자리 나눠 갖기: 2026-08-05 사고(`보증` 검색이 이름순으로 22번째 채널에서 끊겨
   *     사업장가 등 4곳이 통째로 빠졌다)가 부분 일치 경로에도 그대로 있었다. 두 낱말
   *     질의는 부분 일치가 전부 score=1 이라 자르는 순서가 채널 이름순으로 굳는다.
   *  ② 점수 우선(byScoreThenDate): 예전에는 여기서 점수순으로 정렬해 두었는데 pickSpread 가
   *     자리마다 날짜순으로 다시 정렬해 그 정렬이 죽어 있었다 (2026-08-18). */
  const shown = pickPartials();
  if (shown.length) {
    const clippedShown = shown.map(clipHit);
    return {
      // 점수를 떼지 않고 함께 내보낸다 — 봇이 날짜는 보는데 「몇 낱말 맞았나」는 못 봐서
      // 최신성과 관련성을 저울질할 재료가 없었다. claude.js 가 제목 줄에 적는다.
      hits: clippedShown,
      partial: true,
      note: noteWithCapHint(
        `낱말이 **모두** 든 메시지는 0건입니다. 낱말별로는 ${termCounts()} 걸립니다. ` +
        `아래는 **일부만 맞은** ${shown.length}건을 많이 겹치는 순으로 보여 준 것이라 ` +
        `질문과 무관한 것이 섞여 있을 수 있습니다. 여기서 답이 안 보이면 낱말을 줄여 다시 검색하세요.`,
        clippedShown,
      ),
    };
  }

  return { hits: [], note: spreadNote(groups, total, 0, '낱말을 줄여 다시 검색하세요.') };
}

/**
 * `channel` 로 좁혔는데 그 안에 낱말을 **전부** 맞춘 것이 하나도 없으면, 좁히지 않고 한 번 더
 * 훑어 밖의 상위 몇 건을 `outside` 로 함께 돌려준다.
 *
 * **왜 필요한가.** `channel` 은 하드 필터라 지정한 채널 말고는 아무것도 안 보인다(위 본체의
 * `channels = [r.name]`). 통합 전에는 봇이 한 질문에 검색을 여러 번 부르며 좁힘과 넓힘을
 * 섞어서 이게 문제가 안 됐다 — 품질 대조에서 잃은 근거 A·D 를
 * 원본 로그에서 되짚으면 둘 다 **좁힘 없는 검색**이 잡은 것이고, 그 문답들은 넓은 검색을
 * 7회씩 불렀다. 통합으로 왕복이 줄면서 그 넓은 호출이 사라졌고, 남은 한 번이 좁혀지면
 * 다른 자리는 존재하지 않는 것이 된다 (2026-08-19).
 *
 * **발동 조건이 「확정 0건」인 이유.** 「좁힌 결과가 적으면」과 「점수가 낮으면」은 둘 다
 * 실측으로 죽었다 — 좁힌 쪽이 12건·9건으로 예산이 꽉 차고, 좁힌 최고 점수와 밖의 최고
 * 점수가 같다(2/4 대 2/4). 갈리는 것은 **낱말을 다 맞춘 것이 있느냐**뿐이다. 무조건 켜면
 * 실제 좁힘 호출 93건 중 94.6%가 발동해 결과가 +73.7% 가 된다 — `where` 자체가 무의미해진다.
 *
 * **밖의 것을 다시 자리별로 나눠 고르지 않는다.** 아래 재호출이 이미 `pickSpread` 를 거친
 * 결과를 주기 때문이다 — 다시 나눠도 서로 다른 자리가 19→19 로 안 늘고 글자만 는다(실측).
 *
 * 되돌리기는 config 한 줄 — `outsideWhenNarrowedMaxHits` 를 `0` 으로 두면 그대로 통과한다.
 */
/**
 * 봇에게 한 질문 회차인가 — 「반향」.
 *
 * 봇 답변은 아카이브에 안 실리는데(BOT_ANSWER_MARK 자리표시만 남는다) 질문은 일반
 * 회차로 남는다. 그래서 같은 낱말의 좁힘 검색이 과거 질문에 걸려 「확정 히트 있음」이
 * 되고, 아래 outside 안전망이 영영 안 켜졌다 — 같은 질문을 할수록 굳는 자기강화다
 * (2026-09-11 실측, 전 채널 51건).
 *
 * 판정 = 회차 헤더 다음의 첫 본문 줄이 @멘션으로 시작 **그리고** BOT_ANSWER_MARK 보유.
 * 봇 이름을 하드코딩하지 않는 대신 @로 시작하는 멘션 일반을 본다 — 「@동료」로 시작한
 * 본문의 스레드에 봇 질문이 달린 드문 회차가 함께 잡힐 수 있는데, 그 회차도 히트에서는
 * 안 지워지고 관문 계산에서만 빠지므로 정보 손실이 없다.
 *
 * **히트에서 지우지 않는다.** 질문 회차의 스레드에 사람의 답이 붙는 실물이 있다
 * (2026-09-11 검증 — 공문 수취 보고·금액 확인 문답). 쓰는 곳은 딱 둘 —
 * ① hasRealConfirmed(안전망 발동 판정) ② searchArchive 의 outside 내용물 필터.
 */
export function isEchoEntry(text) {
  const s = String(text);
  if (!s.includes(BOT_ANSWER_MARK)) return false;
  const body = s.split(/\r?\n/).slice(1).find((l) => l.trim());
  return Boolean(body && /^@\S/.test(body.trim()));
}

/** 좁힘 결과에 「진짜」 확정 히트가 있나 — 반향은 세지 않는다. 부분 일치에만 score 가 붙는다. */
export function hasRealConfirmed(hits) {
  return hits.some((h) => typeof h.score !== 'number' && !isEchoEntry(h.text));
}

export function searchArchive(opts) {
  const base = scanArchive(opts);
  const max = config.limits.outsideWhenNarrowedMaxHits ?? 0;
  if (!opts.channel || max <= 0) return base;
  // 확정 히트(낱말을 전부 맞춘 것)가 하나라도 있으면 아무 일도 하지 않는다 — 지금 잘 도는
  // 경로를 건드리지 않는다. 부분 일치에만 score 가 붙는다.
  if (hasRealConfirmed(base.hits)) return base;
  // 사람이 적어 준 문자열이 아니라 **푼 이름**으로 걸러야 한다 — 줄여 적은 이름은
  // 푼 뒤에야 실제 채널명이 되므로, 문자열로 비교하면 좁힌 그 채널이 밖에 그대로 섞인다.
  const r = resolveChannelFor(opts.channel, opts.access);
  // 이름을 못 풀었으면 본체가 이미 "후보: …" 안내를 돌려준 상태다. 거기에 밖을 얹지 않는다.
  if (!r.ok) return base;
  // 이 호출에는 channel 이 없으므로 위 `if (!opts.channel)` 에 걸려 되돌아오지 않는다 —
  // 재귀는 한 겹에서 끝난다.
  const wide = scanArchive({ ...opts, channel: undefined });
  // 반향은 outside 에도 안 싣는다 — 안 거르면 남의 채널 질문 반향이 안전망 1순위로
  // 실린다 (2026-09-11 실측: wide 확정 1위가 타 채널의 질문 반향이었다).
  const outside = wide.hits
    .filter((h) => h.channel !== r.name && !isEchoEntry(h.text))
    .slice(0, max);
  return outside.length ? { ...base, outside } : base;
}

/**
 * 채널 텍스트에서 월 헤딩(`## 2026-08`) 구간 하나를 잘라낸다. `readChannel` 이 이 함수로
 * 돈다 — **정본은 `^##\s+`(관대한 쪽)** (WHK 결정 2026-09-03, MONTH_HEADING 주석 참조).
 * 디스크(실물 아카이브) 없이도 부를 수 있게 순수 함수로 뺐다 — `check-month-heading.js` 가
 * 여기로 직접 붙는다.
 *
 * `month` 는 도구 인자(LLM 이 채움)라 정규식 특수문자를 이스케이프한 뒤 짜 넣는다.
 */
export function extractMonthSection(text, month) {
  const lines = text.split('\n');
  const escapedMonth = month.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const startRe = new RegExp(`^##\\s+${escapedMonth}\\s*$`);
  const start = lines.findIndex((l) => startRe.test(l));
  if (start === -1) {
    const months = [...text.matchAll(/^##\s+(\d{4}-\d{2})\s*$/gm)].map((m) => m[1]);
    return { found: false, months };
  }
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    if (/^##\s/.test(lines[i])) {
      end = i;
      break;
    }
  }
  return { found: true, text: lines.slice(start, end).join('\n') };
}

/**
 * 채널 md 전문, 또는 '## YYYY-MM' 월 섹션만.
 */
export function readChannel({ channel, month, access, maxChars = config.limits.channelReadMaxChars }) {
  assertArchive();
  const r = resolveChannelFor(channel, access);
  if (!r.ok) return { error: r.error };
  // 이름 풀기에서 이미 걸렀다. 경계선이라 한 겹 더 둔다.
  if (!canSee(access, r.name)) return { error: BLOCKED_NOTE };

  let text = readCached(channelPath(r.name));

  if (month) {
    const section = extractMonthSection(text, month);
    if (!section.found) {
      return { error: `#${r.name} 에 ${month} 섹션이 없습니다. 있는 달: ${section.months.join(', ') || '없음'}` };
    }
    text = section.text;
  }

  // 채널 md 에는 사람이 손으로 적어 둔 `> 관련: #비공개가 "…"` 참조 줄이 있고, 거기에 그 채널
  // 발언이 금액째 인용돼 있다. 색인(buildArchiveBrief)만 가려서는 소용이 없다 — 전문을
  // 돌려주는 이 자리가 그대로 새는 자리였다 (2026-08-05 발견, 공개 채널 md 12곳).
  text = redactPrivateMentions(text, access);

  let truncated = false;
  if (text.length > maxChars) {
    text = text.slice(0, maxChars);
    truncated = true;
  }
  return { channel: r.name, month: month || null, text, truncated };
}
