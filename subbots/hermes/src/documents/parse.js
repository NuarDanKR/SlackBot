/**
 * Document header, sheet and section parsing — extracted by Codex (R2a).
 * Parsing is deterministic; importing archive.js still loads config.js.
 * Keep this file byte-identical in Hermes and Clio.
 */
import { splitMessages } from '../archive.js';

/**
 * 엑셀 회차 헤더에서 원본 파일명 **뒤에** 붙는 꼬리. ` — 시트 n/N: 이름`
 * (`—` 는 em dash U+2014 이고 앞뒤가 보통 공백이다 — 2026-09-03 실물 175회차 확인)
 *
 * 아래 두 정규식이 이 한 조각을 나눠 쓴다. 헤더의 모양을 두 곳에 적으면 한쪽만 고쳐진다 —
 * 실제로 그렇게 됐다: `archivedAttachmentNames` 는 이 꼬리를 모르는 채 파일명을 뽑아
 * `파일명.xlsx — 시트 1/2: 시트이름` 을 통째로 이름으로 삼았고, 그런 문자열은 채널 md 의
 * `📎 첨부: \`파일명.xlsx\`` 와 절대 안 맞아 **엑셀 첨부에는 「수록」 표시가 영영 안 붙었다**
 * (2026-09-03 실물 50자리 · 고유 47건. 2026-08-18 사고가 엑셀에서만 그대로 남아 있었다).
 */
const SHEET_TAIL = String.raw` — 시트 (\d+)/(\d+): (.+?)`;

/**
 * 엑셀 시트 블록의 회차 헤더. `**날짜 · 원본명 — 시트 n/N: 이름**`
 *
 * 검사(`scripts/check-excel-sheets.js`)가 이것을 **그대로 가져다 쓴다.** 두 벌로 적어
 * 두면 검사 쪽이 느슨해져도 아무도 모른다 — 실제로 검사에는 `· ` 를 안 보는 사본이
 * 있었고, 그래서 헤더에서 `· ` 가 빠진 입력을 검사는 통과시키는데 봇은 시트로 못 읽었다.
 *
 * 잡는 것은 1=시트 번호 · 2=시트 총수 · 3=시트 이름. **번호를 바꾸지 말 것** —
 * `sheetsOf`·`outlineOf`·`check-excel-sheets.js` 가 자리로 꺼내 쓴다.
 */
export const SHEET_RE = new RegExp(String.raw`^\*\*\d{4}-\d{2}-\d{2} · .+?${SHEET_TAIL}\*\*`);

/** 회차 헤더에서 원본 파일명만. 엑셀이면 시트 꼬리를 뺀다 (꼬리는 선택이라 일반 문서도 같이 받는다). */
const ENTRY_FILE_RE = new RegExp(String.raw`^\*\*\d{4}-\d{2}-\d{2} · (.+?)(?:${SHEET_TAIL})?\*\*`);

/**
 * 회차 하나의 **원본 첨부 파일명**. 헤더가 계약을 안 지키면 undefined.
 *
 * 채널 md 의 `📎 첨부: \`파일명\`` 과 글자까지 맞춰볼 이름이라 시트 이름이 붙으면 안 된다.
 * **내보내는 이유는 파서를 두 벌 만들지 않기 위해서다** — `check-attachment-marks.js` 가
 * 여기서 가져다 쓴다. 거기 사본을 두면 **표시를 붙이는 쪽과 표시를 재는 쪽이 똑같이
 * 틀려서 검사가 초록으로 남는다.** 2026-09-03 에 실제로 그랬다 (SHEET_TAIL 주석 참조).
 */
export function entryFileName(entry) {
  return (entry.text.match(ENTRY_FILE_RE) || [])[1]?.trim();
}

/** 문서 md 에서 시트 이름을 순서대로. 시트로 나뉜 문서가 아니면 []. */
export function sheetsOf(text) {
  return splitMessages(text)
    .map((e) => (e.text.match(SHEET_RE) || [])[3])
    .filter(Boolean);
}

/** 절 제목처럼 보이는 볼드 줄 중 날짜형(`**26. 06. 09.**`)을 걸러낸다. */
const SECTION_DATEISH = /^\d{1,4}\s*\.\s*\d{1,2}/;

/**
 * 번호 붙은 절로 나뉜 문서인가. 나뉘면 절 목록, 아니면 null.
 *
 * **왜 두 패턴인가.** 같은 문서 안에서도 절 제목이 `### N. 제목` 과 `**N. 제목**`
 * 두 모양으로 나온다. PDF 변환기가 헤딩을 **글꼴 크기 비율**로만 판정하고 자유
 * 문단에만 적용하기 때문에, 레이아웃이 표 위주로 바뀌는 구간부터는 같은 절 제목이
 * 표 렌더러를 타고 볼드로 나온다 (변환기 코드로 확인, 2026-09-10).
 *
 * **왜 오름차순 관문인가 — 여기가 본체다.** 어떤 문서에서는 `###` 가 절이 아니라
 * **절마다 반복되는 하위 항목**(1~4)이다. 두 패턴을 그냥 합치면 절 58개짜리 문서가
 * 269개로 잡혀 목차가 쓰레기가 된다. 그래서 패턴마다 따로 모아 「번호가 엄격히
 * 오름차순인 집합만 쓴다」로 거른다. 둘 다 아니면 null 을 내서 **현행 동작을
 * 그대로 둔다** — 애매하면 안 나누는 쪽이 맞다.
 *
 * 실측(2026-09-10, 문서 461건): 절이 잡히는 것 40건, 그중 읽기 상한을 넘는 큰 문서 **4건**.
 * 그중 **잘 나뉘는 둘**(40만·44만 자)이 각각 58개 절로 정확히 나뉘고(최대 절 19,681자·
 * 18,348자, 절이 문서의 99.0%·98.4%를 덮는다). 관문에 걸려 통째로 안 나뉘는
 * 문서(비오름차순)는 예전과 똑같이 앞부분 + 검색 안내로 간다.
 *
 * **나뉘는 것과 쓸모 있는 것은 다르다.** 나머지 큰 문서 둘은 절이 잡히기는 하는데
 * 이름만 절이고 내용이 한 덩어리라(최대 절 93,746자·454,159자) 한 절을 열어도 또 잘린다.
 * 그 문서들에 목차를 내밀지 말지는 **여기서 정하지 않는다** — `outlineOf` 가 상한으로 가른다.
 *
 * @param {string} text 문서 md 전문 (가린 뒤의 것)
 * @returns {null | Array<{n:number, name:string, start:number, end:number}>}
 */
export function sectionsOf(text) {
  const h3 = [];
  const bold = [];
  let offset = 0;
  for (const raw of String(text).split('\n')) {
    const line = raw.replace(/\r$/, '');
    let m = line.match(/^###\s+(\d+)\.\s+(\S.*?)\s*$/);
    if (m) {
      h3.push({ n: Number(m[1]), name: m[2].trim(), start: offset });
    } else {
      m = line.match(/^\*\*(\d+)\.\s*([^*|]+?)\*\*\s*$/);
      if (m && !SECTION_DATEISH.test(m[2].trim())) {
        bold.push({ n: Number(m[1]), name: m[2].trim(), start: offset });
      }
    }
    offset += raw.length + 1; // '\n' 한 칸. split 이 떼어 낸 만큼을 되돌린다
  }

  // **엄격히** 오름차순이다 — 같은 번호가 두 번 나오면(`1,2,2,3`) 안 나눈다.
  const usable = (a) => a.length >= 2 && a.every((x, i) => i === 0 || x.n > a[i - 1].n);
  let picked = null;
  if (usable(h3) && usable(bold)) {
    const merged = [...h3, ...bold].sort((a, b) => a.start - b.start);
    /* **합집합이 어긋나면 안 나눈다.** 두 패턴이 안쪽·바깥쪽으로 겹쳐 있다는 뜻인데,
     * 어느 쪽이 바깥인지 **개수로는 못 고른다.** 처음에는 「개수가 많은 쪽」으로 뒀다가
     * 반례에서 틀렸다 — 하위 항목에 문서 전체 연번을 매긴 보고서는 **안쪽이 더 많아서**,
     * 오름차순 관문이 막으려던 실패(하위 항목이 절이 됨)가 그 자리에서 되살아난다
     * (회의적 검증이 만든 반례: 볼드 2개가 진짜 절 · h3 4개가 하위 항목, 2026-09-10).
     *
     * 잃는 것은 없다 — 실물 461건에서 이 갈래는 **0건**이다
     * (실측: h3만 32 · 볼드만 7 · 합집합 1 · 이 갈래 0). 애매하면 안 나누는 쪽이 맞다. */
    picked = usable(merged) ? merged : null;
  } else if (usable(h3)) picked = h3;
  else if (usable(bold)) picked = bold;
  if (!picked) return null;

  return picked.map((s, i) => ({
    n: s.n,
    name: s.name,
    start: s.start,
    end: i + 1 < picked.length ? picked[i + 1].start : text.length,
  }));
}
