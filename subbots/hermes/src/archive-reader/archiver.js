/**
 * Archiver 정본 reader — **읽기만** 한다.
 *
 * `HERMES_MODE=pf-archiver` 에서 Hermes 의 근거는 자기 `slack-export/` 가 아니라
 * Archiving Bot 이 쌓는 정본이다. 이 모듈이 그 정본을 Hermes 가 이미 쓰는 모양으로
 * **투영**한다. 공개 API(`archive.js`·`documents.js`)는 그대로 두고 reader 만 갈린다.
 *
 * ## 정본 구조
 *
 *   <root>/<workspace>/<channel-id>__<채널명>/archive/raw/<YYYY-MM-DD>.md
 *   <root>/<workspace>/<channel-id>__<채널명>/archive/attachments/<file-id>/<rev>.md
 *   <root>/<workspace>/dm/<user-id>/archive/…      ← **절대 읽지 않는다**
 *   <root>/<다른 workspace>/…                       ← **절대 읽지 않는다**
 *
 * 디렉터리 이름에는 **처음 이름**이 남고 정체성은 **채널 ID** 다. 개명해도 같은 채널이다
 * (요구사항 4). 지금 이름은 가장 최근 raw 의 프론트매터 `channel:` 에서 읽는다.
 *
 * ## 왜 투영인가 — 사본을 만들지 않는다
 *
 * 디스크에 Hermes 모양 md 를 깔아 두면 `archive.js` 를 한 줄도 안 고쳐도 된다. 그렇게
 * 하지 않는다. 그 파일은 **판정을 거치지 않은 원문 사본**이고, 이 모드가 막으려는 것이
 * 바로 그것이다(`mode.js` 머리말). 게다가 두 자료가 디스크에 함께 있으면 어느 날
 * 양쪽이 함께 읽혀 **같은 메시지가 두 번** 나온다(요구사항 9).
 *
 * 그래서 투영은 메모리에만 있고, 파일 경로 대신 **가상 키**(`archiver:ch/<id>`)를
 * 돌려준다. 키를 받은 자리는 `readKey()` 로 본문을 얻는다.
 *
 * ## 좌표를 잃지 않는다
 *
 * Hermes 의 메시지 헤더(`**날짜 시각 · 이름**`)에는 `ts` 가 없다. 그래서 투영과 함께
 * **좌표 사이드카**를 만든다 — 투영된 줄 범위 → `(정본 상대경로, message_ts, 원본 줄)`.
 * 이게 있어야 `archive-inbox` 의 결정 좌표가 TYBot 결정 좌표와 같은 자리를 가리킨다
 * (요구사항 10).
 *
 * **추정 변환은 하지 않는다.** 줄 범위가 블록 경계에 안 맞으면 좌표를 안 내고, 받는
 * 쪽은 다시 묻는다. 틀린 좌표는 사람이 승인하지 않은 것을 승인된 것으로 만든다.
 *
 * ## 막는 쪽이 기본값
 *
 * 경로로 거르고 **내용으로 한 번 더** 거른다. 프론트매터의 `workspace` 가 설정과 다르거나
 * `dm_user` 가 있으면 그 파일을 버린다 — 경로 판정 하나가 틀리는 날 개인 기록이 채널
 * 근거로 섞이는 것을 막는다. 판정 하나에만 기대는 구조를 만들지 않는다.
 */
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

/**
 * 원문 한 줄의 지문. **TYBot 의 `evidence_refs.content_hash` 와 글자까지 같아야 한다.**
 *
 * 같지 않으면 두 인터페이스가 **같은 파일의 같은 줄**을 보고도 다른 해시를 내고, 그때
 * 대조는 `evidence_changed` 로 떨어진다 — 「원문이 바뀌었다」 로 보이는데 아무것도
 * 바뀌지 않았다. 구분자가 NUL 인 것도 그쪽 이유 그대로다: 경계가 없으면
 * `ts="a", text="b"` 와 `ts="ab", text=""` 가 같은 지문이 된다.
 *
 * 두 언어에 나뉜 판정이라 어긋나면 빨개지는 검사로 묶는다
 * (`scripts/check-archiver-reader.js` · `tests/test_hermes_archiver_reader.py`).
 */
export function contentHash(at, speaker, text) {
  const raw = `${at || ''}\u0000${speaker || ''}\u0000${text || ''}`;
  return crypto.createHash('sha256').update(raw, 'utf8').digest('hex');
}

/** 가상 키 접두사. 실제 파일 경로와 섞이지 않게 `:` 를 쓴다. */
export const KEY_PREFIX = 'archiver:';

/** Slack 채널 ID 또는 레거시 합성 ID. `shadow_paths.channel_root` 와 같은 규칙이다. */
const CHANNEL_ID = /^(?:[CG][A-Z0-9]{8,}|legacy-[a-f0-9]{16})$/;
/** 워크스페이스 키. `shadow_paths` 와 같은 규칙 — 사람이 읽는 이름이 아니다. */
const WORKSPACE_KEY = /^[a-z0-9][a-z0-9_-]*$/;

/** `> [2026-10-02 09:00|1790899200.000100] U1: 본문` */
const RAW_LINE = /^>\s\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\|([0-9]+\.[0-9]+)\]\s([^:]+?):\s?(.*)$/;
/** 좌표가 아직 없던 옛 줄. **좌표를 지어내지 않는다** — 읽되 좌표는 비운다. */
const RAW_LINE_NO_TS = /^>\s\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\]\s([^:]+?):\s?(.*)$/;

/** 설정이 모자라거나 경로가 수상하다. **던진다** — 조용히 빈 아카이브로 보이면 안 된다. */
export class ArchiverSourceError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ArchiverSourceError';
    this.code = 'archiver_source_invalid';
  }
}

export function channelKey(channelId) {
  return `${KEY_PREFIX}ch/${channelId}`;
}

/** 첨부 하나 = 문서 하나. **판은 키에 안 넣는다** — 근거로 쓰는 것은 최신 판뿐이고,
 * 판을 키에 넣으면 같은 첨부가 판마다 다른 문서로 검색에 걸린다. */
export function docKey(channelId, fileId) {
  return `${KEY_PREFIX}doc/${channelId}/${fileId}`;
}

/** 첨부 정본 → Hermes 문서 모양.
 *
 * 정본의 머리는 YAML(`---`)인데 Hermes 의 문서 메타는 `>` 줄의 `**키**: 값` 이다
 * (`documents/store.js` 의 `parseMeta`). 정본을 그대로 넘기면 메타가 **한 줄도** 안
 * 잡히고, 그 자리는 fail-closed 라 **모든 첨부가 비공개로 닫힌다** — 오류가 아니라
 * 「자료가 없습니다」 로 나간다.
 *
 * `열람` 은 정본의 `visibility` 에서만 온다. **`공개승인` 은 만들지 않는다** — 그건
 * 사람이 Slack 스레드의 `[공개]` 를 옮겨 적은 기록이고, 정본에는 그 개념이 없다.
 * 없는 승인을 지어내면 비공개 채널 자료가 팀 앞에 나간다(Hermes 의 두 줄 규칙).
 */
function projectDoc(doc, channelName, stamp) {
  const meta = [
    `> **출처**: #${channelName} · **종류**: 첨부 · **열람**: ${doc.visibility === 'public' ? '공개' : '비공개'}`,
    `> **파일 ID**: ${doc.fileId} · **판**: ${doc.revision}`
      + (doc.messageTs ? ` · **원문 ts**: ${doc.messageTs}` : '')
      + (doc.partial ? ' · **변환**: 일부만 읽음' : ''),
    `> **정본 경로**: ${doc.relPath}`,
  ];
  // 정본 본문에서 YAML 머리를 떼고 그 아래만 쓴다. 제목 줄(`# 파일명`)은 정본이
  // 이미 들고 있어서 `loadDocument` 가 그것을 제목으로 읽는다.
  const lines = doc.canonicalText.split('\n');
  let start = 0;
  if (lines[0]?.trim() === '---') {
    const close = lines.indexOf('---', 1);
    if (close > 0) start = close + 1;
  }
  const rest = lines.slice(start);
  while (rest.length && !rest[0].trim()) rest.shift();
  // 제목 줄은 **맨 위**다. 메타 뒤에 두면 Hermes 문서 관례와 순서가 달라지고,
  // `preambleOf` 가 그 줄을 「사람이 적어 둔 머리말」 로 색인에 또 싣는다.
  const titleLine = rest[0]?.startsWith('# ') ? rest.shift() : `# ${doc.title}`;
  while (rest.length && !rest[0].trim()) rest.shift();
  // **회차 헤더를 붙인다.** 없으면 본문 전체가 `preambleOf` 의 「사람이 손으로 적어 둔
  // 것」 구간이 되고, 검색 결과에 그 문구가 그대로 실린다 — 변환기가 읽은 것을
  // 사람이 적은 것이라고 말하는 셈이다(원칙 7: 사람 발언 ↔ 문서 분리).
  //
  // 날짜는 **그 첨부가 올라온 메시지의 날짜**다. 여기서 ts 를 날짜로 계산하지 않는다 —
  // raw 에 이미 KST 로 적혀 있고, 다시 계산하면 시간대 하나로 하루가 어긋난다.
  const head = stamp ? `**${stamp.date} ${stamp.time} · 첨부 ${doc.title}**` : '';
  const body = head ? `${head}\n${rest.join('\n')}` : rest.join('\n');
  return `${titleLine}\n\n${meta.join('\n')}\n\n${body}`;
}

export function isKey(value) {
  return typeof value === 'string' && value.startsWith(KEY_PREFIX);
}

/** `root` 아래 `target` 까지 내려가는 **모든 칸**이 링크가 아니어야 한다.
 *
 * 마지막 칸만 보면 `<workspace>` 가 다른 곳을 가리킬 때 통과한다. 그때 경로 문자열은
 * 설정한 워크스페이스처럼 보이고 실제로 읽는 것은 남의 자리다 — 오류가 나지 않는
 * 종류의 사고다. (`shadow_paths.refuse_symlinked_chain` 과 같은 판정.)
 */
function refuseSymlinkedChain(root, target) {
  const rel = path.relative(root, target);
  let current = root;
  for (const part of rel.split(path.sep)) {
    if (!part) continue;
    current = path.join(current, part);
    let st;
    try {
      st = fs.lstatSync(current);
    } catch {
      return;                       // 아직 없는 칸은 링크가 아니다
    }
    if (st.isSymbolicLink()) {
      throw new ArchiverSourceError(`Archiver 경로에 심볼릭 링크가 있습니다: ${part}`);
    }
  }
}

/** 프론트매터만 얕게 읽는다. 값은 **문자열 그대로** — 따옴표만 벗긴다. */
export function frontmatter(text) {
  const lines = String(text).split('\n');
  if (lines[0]?.trim() !== '---') return {};
  const out = {};
  for (let i = 1; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.trim() === '---') break;
    const m = line.match(/^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$/);
    if (!m) continue;
    let value = m[2].trim();
    if (value.startsWith('"') && value.endsWith('"') && value.length >= 2) {
      value = value.slice(1, -1);
    }
    out[m[1]] = value;
  }
  return out;
}

/** `acl: [#팀_자금(ABB540)_주간보고]` → `['#팀_자금(ABB540)_주간보고']` */
function aclList(value) {
  const inner = String(value || '').trim().replace(/^\[/, '').replace(/\]$/, '');
  return inner.split(',').map((s) => s.trim()).filter(Boolean);
}

/**
 * 정본 raw 한 줄 → 메시지 하나. **좌표와 지문을 그 자리에서 굳힌다.**
 *
 * 좌표 모양(`<상대경로>:<줄번호>`)은 TYBot 의 `_source_rows` 와 같다 — 그래야 같은
 * 메시지를 두 인터페이스가 **같은 좌표로** 부른다. 모양만 비슷하게 두면 대조가
 * `no_match` 로 끝나고, 그 상태는 「상대편이 아직 아무것도 안 했다」 와 구별되지 않는다.
 */
function rawMessage(row) {
  return {
    ...row,
    locator: `${row.relPath}:${row.rawLine}`,
    at: `${row.date} ${row.time}`,
    evidenceHash: contentHash(`${row.date} ${row.time}`, row.author, row.body),
  };
}

function statKey(file) {
  try {
    const st = fs.statSync(file);
    return `${file}:${st.mtimeMs}:${st.size}`;
  } catch {
    return `${file}:missing`;
  }
}

/**
 * Archiver reader 하나. **루트와 workspace 키를 명시적으로 받는다.**
 *
 * `config.json` 의 사람이 읽는 `workspace` 이름을 경로 키로 쓰지 않는다(요구사항 2).
 * 그 값은 주석이 「동작에 안 씁니다」 라고 적고 있고, 실제로 `태영건설 재무팀` 같은
 * 문장이 들어온다 — 그걸 경로로 쓰면 디렉터리를 못 찾고 **빈 아카이브로 보인다.**
 * 빈 아카이브는 오류가 아니라 「자료가 없습니다」 로 나가므로 아무도 못 알아챈다.
 */
export function createReader({ root, workspace }) {
  const rawRoot = String(root || '').trim();
  const key = String(workspace || '').trim();
  if (!rawRoot) {
    throw new ArchiverSourceError(
      'Archiver 루트가 설정되지 않았습니다.\n'
      + 'config.json 의 `archiver.root` 또는 HERMES_ARCHIVER_ROOT 를 적으세요.',
    );
  }
  if (!WORKSPACE_KEY.test(key)) {
    throw new ArchiverSourceError(
      `Archiver workspace 키가 올바르지 않습니다: ${JSON.stringify(workspace)}\n`
      + '경로에 쓰이는 키(소문자·숫자·`-`·`_`)여야 합니다. config.json 의 사람이 읽는 '
      + '`workspace` 이름이 아니라 `archiver.workspace` 를 적으세요.',
    );
  }
  const base = path.resolve(rawRoot);
  const workspaceDir = path.join(base, key);

  let cache = null;

  function scan() {
    refuseSymlinkedChain(base, workspaceDir);
    let entries = [];
    try {
      entries = fs.readdirSync(workspaceDir, { withFileTypes: true });
    } catch {
      return { channels: [], stamp: 'none' };
    }
    const stamps = [];
    const channels = [];
    for (const entry of entries) {
      // `dm` 은 디렉터리 모양 자체가 다르다 — `<id>__<이름>` 이 아니므로 여기서
      // 걸러진다. 그 「빠짐」이 필터가 아니라 **경로의 성질**이어야 한다(B-68).
      if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
      const split = entry.name.indexOf('__');
      if (split <= 0) continue;
      const channelId = entry.name.slice(0, split);
      if (!CHANNEL_ID.test(channelId)) continue;
      const archive = path.join(workspaceDir, entry.name, 'archive');
      const rawDir = path.join(archive, 'raw');
      let rawFiles = [];
      try {
        rawFiles = fs.readdirSync(rawDir).filter((f) => /^\d{4}-\d{2}-\d{2}\.md$/.test(f)).sort();
      } catch {
        continue;                   // raw 가 없는 채널 디렉터리는 아직 자료가 아니다
      }
      if (!rawFiles.length) continue;
      const messages = [];
      let name = '';
      let visibility = '';
      let acl = [];
      for (const file of rawFiles) {
        const full = path.join(rawDir, file);
        stamps.push(statKey(full));
        const text = fs.readFileSync(full, 'utf8').replace(/\r\n/g, '\n');
        const meta = frontmatter(text);
        // **내용으로 한 번 더 거른다.** 경로 판정이 틀리는 날의 마지막 벽이다.
        if (meta.workspace && meta.workspace !== key) continue;
        if (meta.dm_user) continue;
        if (meta.channel_id && meta.channel_id !== channelId) continue;
        // **`#` 을 뗀다.** `pf` 에서 채널 이름의 근거는 `자금.md` 같은 **파일명**이라
        // `#` 이 없다. 여기서 `#팀_자금…` 을 내면 같은 채널이 두 모드에서 다른 이름이
        // 되고, 그러면 `privateChannels`·`skipChannels`·개명 지도가 전부 안 맞는다 —
        // 비공개 채널이 공개로 판정되는 쪽으로 틀릴 수 있다.
        if (meta.channel) name = meta.channel.replace(/^#/, '');   // 최근 날짜 값이 남는다
        if (meta.visibility) visibility = meta.visibility;
        if (meta.acl) acl = aclList(meta.acl);
        const relPath = path.relative(base, full).split(path.sep).join('/');
        const lines = text.split('\n');
        for (let i = 0; i < lines.length; i += 1) {
          const hit = lines[i].match(RAW_LINE);
          if (hit) {
            messages.push(rawMessage({
              date: hit[1], time: hit[2], ts: hit[3], author: hit[4], body: hit[5],
              relPath, rawLine: i + 1,
            }));
            continue;
          }
          const legacy = lines[i].match(RAW_LINE_NO_TS);
          if (legacy) {
            messages.push(rawMessage({
              date: legacy[1], time: legacy[2], ts: '', author: legacy[3], body: legacy[4],
              relPath, rawLine: i + 1,
            }));
          }
        }
      }
      if (!messages.length) continue;
      channels.push({
        channelId,
        dirName: entry.name,
        firstName: entry.name.slice(split + 2),
        name: name || entry.name.slice(split + 2),
        visibility,
        acl,
        archiveDir: archive,
        messages,
      });
    }
    channels.sort((a, b) => a.name.localeCompare(b.name));
    return { channels, stamp: stamps.sort().join('|') };
  }

  function scanDocs(channel) {
    const dir = path.join(channel.archiveDir, 'attachments');
    let fileIds = [];
    try {
      fileIds = fs.readdirSync(dir, { withFileTypes: true })
        .filter((d) => d.isDirectory() && !d.isSymbolicLink())
        .map((d) => d.name);
    } catch {
      return [];
    }
    const out = [];
    for (const fileId of fileIds.sort()) {
      let revisions = [];
      try {
        revisions = fs.readdirSync(path.join(dir, fileId)).filter((f) => f.endsWith('.md'));
      } catch {
        continue;
      }
      const docs = [];
      for (const file of revisions) {
        const full = path.join(dir, fileId, file);
        const text = fs.readFileSync(full, 'utf8').replace(/\r\n/g, '\n');
        const meta = frontmatter(text);
        if (meta.workspace && meta.workspace !== key) continue;
        if (meta.channel_id && meta.channel_id !== channel.channelId) continue;
        // 쓸 수 있는 판만 근거가 된다. 실패·PII 차단 판은 본문이 없거나 사유 코드뿐이다.
        if (!['succeeded', 'partial'].includes(meta.conversion_state || '')) continue;
        docs.push({
          fileId,
          revision: meta.revision || file.replace(/\.md$/, ''),
          title: meta.file_name || fileId,
          messageTs: meta.message_ts || '',
          order: meta.converted_at || meta.staged_at || '',
          partial: meta.conversion_state === 'partial',
          visibility: meta.visibility || '',
          relPath: path.relative(base, full).split(path.sep).join('/'),
          channelId: channel.channelId,
          channelName: channel.name,
          canonicalText: text,
        });
      }
      if (!docs.length) continue;
      // **최신 판 하나만 근거로 쓴다.** 여러 판을 함께 내면 같은 첨부가 검색에 두 번
      // 걸리고, 사람은 어느 판을 보고 있는지 모른다.
      docs.sort((a, b) => b.order.localeCompare(a.order) || b.revision.localeCompare(a.revision));
      const current = docs[0];
      // 그 첨부가 올라온 메시지. raw 에 KST 날짜·시각이 이미 있으므로 **그것을 쓴다.**
      const stamp = channel.messages.find((m) => m.ts && m.ts === current.messageTs) || null;
      out.push({ ...current, text: projectDoc(current, channel.name, stamp) });
    }
    return out;
  }

  /**
   * 정본 메시지 → Hermes 모양 md. **최신이 위**다(`slack-archive.js` 와 같은 순서).
   *
   * 한 블록 = 한 메시지다. 같은 `ts`·같은 작성자로 나뉘어 적힌 줄(첨부 표시가 그렇다)은
   * **한 블록으로 묶는다** — 그래야 블록 하나가 좌표 하나를 가진다.
   */
  function project(channel) {
    const groups = new Map();
    for (const m of channel.messages) {
      const gid = `${m.relPath}#${m.ts || `L${m.rawLine}`}#${m.author}`;
      if (!groups.has(gid)) groups.set(gid, { ...m, bodies: [], lines: [], rows: [] });
      const g = groups.get(gid);
      g.bodies.push(m.body);
      g.lines.push(m.rawLine);
      g.rows.push(m);
    }
    const ordered = [...groups.values()].sort((a, b) => {
      const byTs = Number(b.ts || 0) - Number(a.ts || 0);
      if (byTs) return byTs;
      return `${b.date} ${b.time}`.localeCompare(`${a.date} ${a.time}`)
        || b.rawLine - a.rawLine;
    });

    const lines = [];
    const coords = [];
    lines.push(`# #${channel.name}`, '');
    // 메타 블록(`>` 줄)은 `metaBlock()` 이 색인에 싣는 자리다. 출처가 Archiver 임을
    // 여기 적는다 — 사람이 근거를 확인하러 갈 자리가 slack-export 가 아니다.
    lines.push(`> 워크스페이스: ${key} · 채널 ID: ${channel.channelId}`);
    lines.push(`> 출처: Archiver 정본 (${channel.dirName}/archive/raw)`);
    // **`visibility` 는 여기 안 적는다.** Hermes 의 공개·비공개 판정은 `config.json` 의
    // `privateChannels` 가 쥐고 있고(`isPrivateChannel`), 정본의 `visibility` 는 기본값이
    // `private` 이다. 둘을 한 화면에 섞어 적으면 모델은 공개 채널을 비공개로 읽고
    // 「말할 수 없다」 로 닫는다. 값 자체는 `channelMeta()` 로 꺼내 **진단에서** 대본다.
    lines.push('');

    let month = '';
    for (const g of ordered) {
      const thisMonth = g.date.slice(0, 7);
      if (thisMonth !== month) {
        month = thisMonth;
        lines.push(`## ${month}`, '');
      }
      const start = lines.length + 1;            // 1-based: 헤더 줄
      lines.push(`**${g.date} ${g.time} · ${g.author}**`);
      for (const body of g.bodies) lines.push(body);
      const end = lines.length;                  // 1-based, 양끝 포함
      lines.push('', '---', '');
      // **블록의 좌표는 그 메시지의 첫 raw 줄이다.** 한 블록이 두 줄일 수 있다
      // (본문 + 첨부 표시는 같은 ts·같은 작성자로 두 줄에 적힌다). TYBot 은 그 둘을
      // **각각** 한 줄로 세어 좌표를 매기므로, 여기서 첫 줄에 맞춰야 같은 자리가 된다.
      const anchor = [...g.rows].sort((a, b) => a.rawLine - b.rawLine)[0];
      coords.push({
        startLine: start,
        endLine: end,
        relPath: g.relPath,
        messageTs: g.ts,
        rawLines: [...g.lines].sort((a, b) => a - b),
        locator: anchor.locator,
        evidenceHash: anchor.evidenceHash,
        at: anchor.at,
        author: anchor.author,
      });
    }
    return { text: lines.join('\n'), coords };
  }

  function build() {
    const { channels, stamp } = scan();
    const byId = new Map();
    const byName = new Map();
    const documents = [];
    for (const channel of channels) {
      const projected = project(channel);
      const docs = scanDocs(channel);
      const entry = { ...channel, ...projected, docs };
      byId.set(channel.channelId, entry);
      // **이름이 겹치면 둘 다 뺀다.** 어느 채널인지 모르는 이름으로 답하면 출처가
      // 틀린다 — 개명 직후 두 채널이 같은 이름을 가질 수 있다.
      if (byName.has(channel.name)) byName.set(channel.name, null);
      else byName.set(channel.name, channel.channelId);
      documents.push(...docs);
    }
    for (const [name, id] of [...byName]) if (id === null) byName.delete(name);
    return { stamp, byId, byName, documents };
  }

  function state() {
    const fresh = scan();
    if (!cache || cache.stamp !== fresh.stamp) cache = build();
    return cache;
  }

  function channelOf(nameOrId) {
    const s = state();
    if (s.byId.has(nameOrId)) return s.byId.get(nameOrId);
    const id = s.byName.get(nameOrId);
    return id ? s.byId.get(id) : null;
  }

  return {
    label: 'archiver',
    root: base,
    workspace: key,

    /** 자료가 하나라도 있나. 「없다」 와 「설정이 틀렸다」 는 다르다. */
    hasContent() {
      return state().byId.size > 0;
    },

    /** 채널 이름 목록 — Hermes 의 `listArchivedChannels()` 자리. */
    channelNames() {
      return [...state().byName.keys()].sort();
    },

    channelIds() {
      return [...state().byId.keys()].sort();
    },

    /**
     * 개명 지도 행. `.sync-state.json` 의 `{name, file}` 과 **같은 모양**이다.
     *
     * `file` 은 디렉터리에 박힌 **처음 이름**이다. 채널 ID 가 정체성이므로 개명해도
     * 디렉터리는 그대로이고, 그래서 이 지도가 성립한다(요구사항 4).
     */
    renameRows() {
      return [...state().byId.values()].map((c) => ({ name: c.name, file: c.firstName }));
    },

    channelKeyOf(nameOrId) {
      const channel = channelOf(nameOrId);
      return channel ? channelKey(channel.channelId) : null;
    },

    channelMeta(nameOrId) {
      const channel = channelOf(nameOrId);
      if (!channel) return null;
      const { channelId, dirName, firstName, name, visibility, acl } = channel;
      return { channelId, dirName, firstName, name, visibility, acl };
    },

    /** 가상 키의 본문. 모르는 키는 **빈 문자열이 아니라 던진다.** */
    readKey(value) {
      // `path.join` 이 윈도우에서 `\` 를 넣는다. 키는 늘 `/` 로 읽는다 —
      // 안 맞추면 같은 문서가 플랫폼마다 다른 키가 되고, 그때 「못 찾았습니다」만 남는다.
      const rest = String(value).split('\\').join('/').slice(KEY_PREFIX.length);
      const s = state();
      if (rest.startsWith('ch/')) {
        const channel = s.byId.get(rest.slice(3));
        if (!channel) throw new ArchiverSourceError(`Archiver 채널을 찾지 못했습니다: ${value}`);
        return channel.text;
      }
      if (rest.startsWith('doc/')) {
        const [channelId, fileId] = rest.slice(4).split('/');
        const doc = s.documents.find(
          (d) => d.channelId === channelId && d.fileId === fileId.replace(/\.md$/, ''),
        );
        if (!doc) throw new ArchiverSourceError(`Archiver 첨부를 찾지 못했습니다: ${value}`);
        return doc.text;
      }
      throw new ArchiverSourceError(`모르는 Archiver 키입니다: ${value}`);
    },

    /** 첨부 정본 목록. 채널이 프로젝트, 파일 이름이 문서 제목이다(요구사항 6). */
    documents() {
      return state().documents.map((d) => ({ ...d, key: docKey(d.channelId, d.fileId) }));
    },

    documentsFor(nameOrId) {
      const channel = channelOf(nameOrId);
      if (!channel) return [];
      return channel.docs.map((d) => ({ ...d, key: docKey(d.channelId, d.fileId) }));
    },

    /** 투영된 줄 범위 전부. `archive-inbox` 의 좌표 변환이 이것을 쓴다. */
    coordinates(nameOrId) {
      const channel = channelOf(nameOrId);
      return channel ? channel.coords : [];
    },

    /**
     * 투영 줄 범위 → 정본 좌표. **블록 하나에 정확히 맞아야 한다.**
     *
     * 추정하지 않는다(요구사항 10). 범위가 두 블록에 걸치거나 어느 블록에도 안
     * 들어가면 `null` — 받는 쪽은 좌표 없음으로 보고 **다시 묻는다.** 걸쳐 있는 것을
     * 「첫 블록」 으로 접으면 사람이 본 근거의 일부만 가리키는 좌표가 남는다.
     */
    coordinateFor(nameOrId, startLine, endLine) {
      const end = endLine == null ? startLine : endLine;
      const hits = this.coordinates(nameOrId).filter(
        (c) => startLine <= c.endLine && end >= c.startLine,
      );
      if (hits.length !== 1) return null;
      const hit = hits[0];
      if (startLine < hit.startLine || end > hit.endLine) return null;
      if (!hit.messageTs) return null;      // 좌표 없던 옛 줄 — 지어내지 않는다
      return {
        relPath: hit.relPath,
        messageTs: hit.messageTs,
        // TYBot 과 **같은 모양**이다: `<상대경로>:<줄번호>`.
        locator: hit.locator,
        evidenceHash: hit.evidenceHash,
        rawLines: hit.rawLines,
      };
    },

    /** 시험·진단용. 캐시를 버린다. */
    invalidate() {
      cache = null;
    },
  };
}
