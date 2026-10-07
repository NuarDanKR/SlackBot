#!/usr/bin/env node
/**
 * `HERMES_MODE=pf-archiver` 의 **reader 교체**를 봇이 실제로 쓰는 함수로 잰다.
 *
 *   node scripts/check-archiver-reader.js <fixture 루트> [<dataroot>]
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 ──
 *
 * reader 가 갈려도 **에러가 안 난다.** 정본을 못 읽으면 채널 목록이 비고, 빈 목록은
 * 「자료가 없습니다」 로 정상 답변된다. 격리가 새도 마찬가지다 — 남의 워크스페이스
 * 문장이 섞여 나오는 것은 그 문장을 아는 사람만 알아챈다.
 *
 * 그래서 여기서 재는 것은 셋이다.
 *
 *   1. **기능이 돈다** — 질문(search)·채널 읽기·문서(첨부)·색인·요약 입력이 기존 API 로
 *   2. **격리** — 다른 워크스페이스와 DM 이 **한 글자도** 안 나온다
 *   3. **좌표** — 투영 줄 범위가 정본 좌표로 되돌아가고, 추정하지 않는다
 *
 * fixture 는 **실제 수집기가 만든 것**이어야 한다
 * (`scripts/make_archiver_fixture.py`). 경로를 손으로 적은 fixture 는 적은 사람이
 * 생각한 모양만 고정하고, 정본이 바뀌어도 통과한다.
 */
import fs from 'node:fs';
import path from 'node:path';

const [fixtureRoot] = process.argv.slice(2);
if (!fixtureRoot) {
  console.error('사용: node scripts/check-archiver-reader.js <fixture 루트>');
  process.exit(1);
}

let ok = true;
const fail = (m, extra = '') => { console.error(`  ✗ ${m}${extra ? `  ${extra}` : ''}`); ok = false; };
const pass = (m, extra = '') => console.log(`  ✓ ${m}${extra ? `  ${extra}` : ''}`);
const is = (m, cond, extra = '') => (cond ? pass(m, extra) : fail(m, extra));

const manifest = JSON.parse(fs.readFileSync(path.join(fixtureRoot, 'fixture.json'), 'utf8'));
const archive = await import('../src/archive.js');
const cfg = await import('../src/config.js');
const docs = await import('../src/documents.js');
const { FULL_ACCESS, PUBLIC_ACCESS } = cfg;

console.log('\n[1/6] 출처가 Archiver 로 갈렸는가');
is('ARCHIVE_SOURCE 가 archiver 다', cfg.ARCHIVE_SOURCE === 'archiver', cfg.ARCHIVE_SOURCE);
is('workspace 키가 명시 설정에서 왔다', cfg.ARCHIVER_WORKSPACE === manifest.workspace,
  `${cfg.ARCHIVER_WORKSPACE} vs ${manifest.workspace}`);
// 사람이 읽는 이름을 경로 키로 추정하지 않는다 — 그 값은 문장이라 디렉터리가 없다.
is('사람이 읽는 workspace 이름을 키로 쓰지 않는다',
  cfg.config.workspace !== cfg.ARCHIVER_WORKSPACE, cfg.config.workspace);

console.log('\n[2/6] 기존 공개 API 로 기능이 돈다');
archive.assertArchive();
pass('assertArchive() 가 통과한다 (slack-export 없이)');
const names = archive.listArchivedChannels();
is('채널 목록이 정본 채널 수와 같다', names.length === manifest.channels.length,
  `${names.length} vs ${manifest.channels.length}`);
is('채널 이름에 `#` 이 없다 (pf 의 파일명 관례)', names.every((n) => !n.startsWith('#')),
  names.join(', '));

const hit = archive.searchArchive({ query: '집행액', access: FULL_ACCESS });
is('search 가 정본 원문을 찾는다', (hit.hits || []).length > 0, hit.note || '');
is('  찾은 본문이 메시지 블록 모양이다',
  (hit.hits || []).some((h) => /^\*\*\d{4}-\d{2}-\d{2} \d{2}:\d{2} · /.test(h.text)),
  JSON.stringify((hit.hits || [])[0] || {}).slice(0, 120));

const read = archive.readChannel({ channel: names[0], access: FULL_ACCESS });
is('read_channel 이 본문을 돌려준다', typeof read.text === 'string' && read.text.length > 0);
is('  월 헤딩이 있다 (extractMonthSection 계약)', /^## \d{4}-\d{2}$/m.test(read.text));

const brief = archive.buildArchiveBrief({ access: FULL_ACCESS });
is('색인이 채널을 전부 싣는다', names.every((n) => brief.includes(n)));
const split = archive.buildArchiveBriefSplit({ access: FULL_ACCESS });
is('분할 색인(프롬프트 캐시 경로)도 선다',
  Boolean(split && (split.common || split.base || split.shared)), Object.keys(split || {}).join(','));

console.log('\n[3/6] 첨부 정본이 문서 근거가 된다');
is('hasDocuments() 가 참이다', docs.hasDocuments());
const expectedDocs = manifest.channels.reduce((n, c) => n + c.attachments.length, 0);
const docList = docs.listDocuments();
is('문서 수가 정본 첨부 수와 같다', docList.length === expectedDocs,
  `${docList.length} vs ${expectedDocs}`);
is('  제목이 원본 파일명이다', docList.every((d) => /\.[a-z0-9]+$/i.test(d.title)),
  docList.map((d) => d.title).join(', '));
is('  메타를 읽어 열람 판정이 된다 (broken 아님)', docList.every((d) => !d.broken));
const docHit = docs.searchDocuments({ query: '잔액', access: FULL_ACCESS });
is('문서 검색이 첨부 본문을 찾는다', (docHit.hits || []).length > 0);
is('  「사람이 손으로 적어 둔 것」 으로 라벨하지 않는다',
  !(docHit.hits || []).some((h) => h.text.includes('사람이 회차들을 훑어')),
  JSON.stringify((docHit.hits || [])[0] || {}).slice(0, 140));
const first = docList[0];
const readDoc = docs.readDocument({ project: first.project, document: first.title, access: FULL_ACCESS });
is('read_document 가 첨부 본문을 연다', String(readDoc.text || '').includes('자금 집행 계획'),
  String(readDoc.text || '').slice(0, 80));

console.log('\n[4/6] 격리 — 다른 워크스페이스와 DM');
const everything = [
  brief,
  JSON.stringify(split),
  names.map((n) => archive.readChannel({ channel: n, access: FULL_ACCESS }).text).join('\n'),
  JSON.stringify(archive.searchArchive({ query: 'PF 논의 DM 개인', access: FULL_ACCESS })),
  docs.buildDocumentsBrief({ access: FULL_ACCESS }),
  JSON.stringify(docs.listDocuments()),
].join('\n');
for (const phrase of ['개인 DM 에만', 'PF 내부 논의', manifest.other_workspace, manifest.dm_user]) {
  is(`「${phrase}」 가 한 번도 안 나온다`, !everything.includes(phrase));
}
// 경로로만 막지 않는다 — 내용으로도 막힌다. 그 두 겹이 같이 있어야 한다.
const other = archive.listArchivedChannels().filter((n) => /pf_/.test(n));
is('다른 워크스페이스 채널이 목록에 없다', other.length === 0, other.join(','));

console.log('\n[5/6] 좌표 — 추정하지 않는다');
const target = manifest.channels.find((c) => c.attachments.length) || manifest.channels[0];
const coords = cfg.archiveSource.coordinates(target.channel_id);
is('좌표표가 비어 있지 않다', coords.length > 0, `${coords.length}건`);
is('  locator 가 <상대경로>:<줄번호> 다',
  coords.every((c) => /\/archive\/raw\/\d{4}-\d{2}-\d{2}\.md:\d+$/.test(c.locator)),
  coords[0]?.locator || '');
is('  message_ts 가 보존된다', coords.every((c) => /^\d+\.\d+$/.test(c.messageTs)));
is('  evidence_hash 가 sha256 전체다', coords.every((c) => /^[0-9a-f]{64}$/.test(c.evidenceHash)));

const one = coords[0];
const exact = cfg.archiveSource.coordinateFor(target.channel_id, one.startLine, one.endLine);
is('블록에 정확히 맞으면 좌표가 나온다', exact && exact.locator === one.locator,
  JSON.stringify(exact));
is('블록 밖으로 한 줄 넘치면 좌표를 안 낸다',
  cfg.archiveSource.coordinateFor(target.channel_id, one.startLine, one.endLine + 1) === null);
if (coords.length > 1) {
  is('두 블록에 걸치면 좌표를 안 낸다',
    cfg.archiveSource.coordinateFor(target.channel_id, coords[1].startLine, coords[0].endLine) === null);
} else {
  console.log('  · 건너뜀: 블록이 하나뿐이라 「걸침」을 잴 수 없습니다');
}
is('어느 블록에도 안 들어가면 좌표를 안 낸다',
  cfg.archiveSource.coordinateFor(target.channel_id, 1, 1) === null);

console.log('\n[6/6] 개명과 권한');
const meta = cfg.archiveSource.channelMeta(target.channel_id);
is('채널 ID 로도 같은 채널이 풀린다', meta && meta.channelId === target.channel_id);
is('디렉터리에는 처음 이름이 남는다', meta.dirName === target.dir_name,
  `${meta.dirName} vs ${target.dir_name}`);
// 비공개 판정은 config.json 이 쥐고 있다 — 정본의 `visibility` 로 갈아치우지 않았다.
const priv = names.find((n) => cfg.isPrivateChannel(n));
is('config.json 의 privateChannels 가 그대로 비공개를 만든다', Boolean(priv), String(priv));
is('  공개 권한으로는 그 채널이 안 보인다', !cfg.canSee(PUBLIC_ACCESS, priv));
const pubBrief = archive.buildArchiveBrief({ access: PUBLIC_ACCESS });
is('  공개 색인에 비공개 채널 본문이 없다', !pubBrief.includes('인사 평가 일정'));

console.log(ok ? '\n전부 통과' : '\n어긋남 있음');
process.exit(ok ? 0 : 1);
