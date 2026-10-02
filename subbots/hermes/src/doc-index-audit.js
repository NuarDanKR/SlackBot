/**
 * `50-resources/documents/index.md` 가 주장하는 숫자·목록을 실물과 대본다.
 *
 * **새로 세지 않는다** — 세는 법은 index.md 자신이 「세는 법」 줄에 적어 둔 정의이고
 * (회차 = `ENTRY_RE` 에 걸리는 줄 = `splitMessages` 가 자른 블록, 엑셀 = 메타에
 * `> **시트**:` 가 있는 md, 시리즈 = 엑셀이 아니면서 회차 2건 이상, 단발 = 나머지 회차),
 * 그 값은 `listDocuments()` 가 이미 갖고 있다.
 *
 * **이 값은 봇에 안 간다.** 봇 색인은 각 md 의 메타에서 만들어지고 index.md 에서
 * 실리는 것은 「변환하지 못한 것」 절 하나뿐이다. 그래서 어긋남은 ✗ 가 아니라 ⚠ 다 —
 * 틀린 숫자를 보는 것은 사람뿐이라 커밋을 막을 이유가 없다.
 *
 * 파일도 네트워크도 안 읽는다. 읽는 일은 부르는 쪽(check-setup.js)이 한다.
 */
import { isSheetDoc, isSeriesDoc } from './documents.js';

/* 앞머리 한 줄. 여덟 칸을 한 번에 잡는다 — 칸마다 정규식을 두면 하나가 안 맞을 때
 * 나머지가 「못 읽음」인지 「맞음」인지 갈리지 않는다. */
const HEADER_RE = /문서\s*(\d+)건\s*·\s*회차\s*(\d+)건\s*·\s*사업장\s*(\d+)개\s*\(\s*시리즈\s*(\d+)개가\s*회차\s*(\d+)건\s*·\s*엑셀\s*(\d+)건이\s*회차\s*(\d+)건\s*·\s*나머지\s*(\d+)건이\s*단발/;
const SECTION_RE = /^###\s+(.+?)\s+\(문서\s*(\d+)\s*·\s*회차\s*(\d+)\)\s*$/;
const H2_RE = /^##\s+/;
const BULLET_RE = /^-\s+\S/;
/* **문서 줄의 모양.** `- [종류] 제목 — 날짜` 이고 잠금 표시(🔒)가 앞에 붙거나
 * 꼬리에 부가 설명이 붙기도 한다. 종류·제목·날짜의 정확한 문법까지는 안 보고
 * 대괄호 하나와 em dash(—) 하나만 있으면 문서 줄로 본다 — 그 이상 좁히면
 * 실물의 사소한 변형(부가 설명 등)에 걸려 넘어간다.
 *
 * 이걸 두는 이유가 이 감사의 핵심이다: `BULLET_RE`(모든 `- ` 줄)만 세면
 * 사업장 절에 메모 줄(`- 이 사업장은 …`) 하나가 끼어도 줄 **수**는 그대로라
 * 문서 한 건이 목록에서 빠진 것을 못 잡는다 (2026-08-27 리뷰에서 재현).
 * 이 정규식으로 「문서 줄 모양인 것」만 문서로 세고, 그 밖의 `- ` 줄은
 * 「없는 것」이 아니라 「문서 줄이 아니다」로 따로 보고한다(아래 nonDocBullets).
 *
 * 2026-08-27 실물 대조: `50-resources/documents/index.md` 사업장 절의 `- ` 줄
 * 403개가 전부 이 모양이고, 그 밖의 `- ` 줄은 하나도 없다. */
const DOC_LINE_RE = /^-\s+(?:🔒\s+)?\[[^\]]+\]\s+.+\s—\s/;
const DOC_SECTION_HEAD = '## 사업장별 문서';

/** index.md 「세는 법」 줄의 정의 그대로 센다. */
export function countDocs(docs) {
  const excel = docs.filter((d) => isSheetDoc(d));
  const series = docs.filter((d) => isSeriesDoc(d));
  const sum = (list) => list.reduce((n, d) => n + d.entries.length, 0);
  const rounds = sum(docs);
  const excelRounds = sum(excel);
  const seriesRounds = sum(series);
  return {
    docs: docs.length,
    rounds,
    series: series.length,
    seriesRounds,
    excel: excel.length,
    excelRounds,
    single: rounds - seriesRounds - excelRounds,
  };
}

/**
 * `## 사업장별 문서` 절만 잘라 `###` 마다 (선언한 문서 수, 선언한 회차 수, 문서 줄 수,
 * 문서 줄 모양이 아닌 `- ` 줄 수).
 *
 * **다음 `## ` 에서 멈춘다.** 안 멈추면 「변환하지 못한 것」 절의 `- ` 줄이 마지막
 * 사업장의 목록으로 세어져, 실제로는 모자란 사업장이 남아돌게 보인다.
 *
 * **문서 줄만 `bullets` 에 센다.** 모양이 다른 `- ` 줄(메모 등)은 `nonDocBullets` 로
 * 따로 모은다 — 조용히 버리면 「문서 한 건이 목록에서 빠져도 안 잡힌다」는
 * 이 감사의 원래 결함이 형태만 바뀌어 남는다.
 */
function readSections(indexText) {
  const lines = indexText.split('\n');
  const start = lines.findIndex((l) => l.trim() === DOC_SECTION_HEAD);
  const out = new Map();
  if (start < 0) return { out, found: false };
  let cur = null;
  for (const line of lines.slice(start + 1)) {
    if (H2_RE.test(line)) break;
    const m = SECTION_RE.exec(line);
    if (m) {
      cur = {
        docs: Number(m[2]), rounds: Number(m[3]), bullets: 0, nonDocBullets: 0,
      };
      out.set(m[1], cur);
      continue;
    }
    if (cur && BULLET_RE.test(line)) {
      if (DOC_LINE_RE.test(line)) cur.bullets += 1;
      else cur.nonDocBullets += 1;
    }
  }
  return { out, found: true };
}

export function auditDocIndex({ indexText, docs, projects }) {
  const problems = [];
  const real = countDocs(docs);

  const h = HEADER_RE.exec(indexText);
  if (!h) {
    // 못 읽은 것을 「맞음」으로 넘기지 않는다. 이 검사가 막으려는 것이 조용한 통과다.
    problems.push('앞머리 총계 줄을 못 읽었습니다 — 「문서 N건 · 회차 N건 · 사업장 N개 (시리즈 …)」 모양이어야 합니다');
  } else {
    const claimed = {
      docs: Number(h[1]), rounds: Number(h[2]), projects: Number(h[3]),
      series: Number(h[4]), seriesRounds: Number(h[5]),
      excel: Number(h[6]), excelRounds: Number(h[7]), single: Number(h[8]),
    };
    const pairs = [
      ['문서', claimed.docs, real.docs],
      ['회차', claimed.rounds, real.rounds],
      ['사업장', claimed.projects, projects.length],
      ['시리즈 개수', claimed.series, real.series],
      ['시리즈 회차', claimed.seriesRounds, real.seriesRounds],
      ['엑셀 개수', claimed.excel, real.excel],
      ['엑셀 회차', claimed.excelRounds, real.excelRounds],
      ['단발 회차', claimed.single, real.single],
    ];
    for (const [name, said, is] of pairs) {
      if (said !== is) problems.push(`앞머리 ${name}: index.md 는 ${said} 인데 실물은 ${is}`);
    }
  }

  const { out: sections, found } = readSections(indexText);
  if (!found) {
    problems.push(`「${DOC_SECTION_HEAD}」 절을 못 찾았습니다`);
    return problems;
  }

  const byProject = new Map();
  for (const d of docs) {
    if (!byProject.has(d.project)) byProject.set(d.project, []);
    byProject.get(d.project).push(d);
  }

  for (const project of [...byProject.keys()].sort()) {
    const mine = byProject.get(project);
    const rounds = mine.reduce((n, d) => n + d.entries.length, 0);
    const s = sections.get(project);
    if (!s) {
      problems.push(`${project}: 문서가 ${mine.length}건 있는데 index.md 에 절이 없습니다`);
      continue;
    }
    if (s.docs !== mine.length) problems.push(`${project} 헤딩 문서 수: index.md 는 ${s.docs} 인데 실물은 ${mine.length}`);
    if (s.rounds !== rounds) problems.push(`${project} 헤딩 회차 수: index.md 는 ${s.rounds} 인데 실물은 ${rounds}`);
    if (s.bullets !== mine.length) {
      problems.push(`${project} 목록 줄: ${s.bullets}줄인데 문서는 ${mine.length}건 (${mine.length - s.bullets}건이 목록에 없습니다)`);
    }
    if (s.nonDocBullets) {
      problems.push(`${project}: 문서 줄 모양이 아닌 - 줄이 ${s.nonDocBullets}건 있습니다 (세지 않았습니다)`);
    }
  }

  for (const name of sections.keys()) {
    if (!byProject.has(name)) problems.push(`${name}: index.md 에 절이 있는데 실물 문서가 0건입니다`);
  }

  return problems;
}
