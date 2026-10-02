/** Codex R3a: pure log line codec, paired rendering and parsing.
 * No config/filesystem imports. Caller owns label and number-format policy.
 */
export function createLogCodec({ KIND_LABEL, num }) {
  /* 도구 결과가 몇 자였는지를 뒤에 붙인다 (claude.js 의 attachSizes).
   *
   * **chars 가 없는 옛 기록은 예전 그대로 렌더해야 한다** — 렌더는 멱등이라야 하고,
   * 여기서 형태가 바뀌면 다음 07:00 회차가 지난 달치 md 를 통째로 다시 써서 diff 가 뜬다.
   * 그래서 `!= null` 로만 붙이고 0자(결과 없음)는 0 으로 적는다.
   *
   * `[좁힘:]`·`[넓힘:]` 꼬리도 **값이 있을 때만** 붙으므로 옛 기록(값 없음)의 렌더는 불변이다 —
   * 멱등 유지. (그 값은 claude.js 의 attachNarrows 가 tool_use id 로 짝지어 얹는다.) */
  function toolLine(t) {
    const v = Object.values(t?.input || {})
      .map((x) => (typeof x === 'string' ? x : JSON.stringify(x)))
      .join(', ');
    const head = `${t?.name}(${v.length > 80 ? `${v.slice(0, 80)}…` : v})`;
    /* 꼬리 값에서 괄호·대괄호를 지운다 — `(.*)` 가 마지막 `)` 까지 먹는 파서라, 꼬리에
     * `)`·`]` 가 들어오면 인자 경계가 밀려 조각이 조용히 안 읽힌다 (parseToolLine ② 참조).
     *
     * **지우고 나서 빈 문자열이면 꼬리를 아예 안 붙인다.** `[좁힘: ]` 는 파서의 `[^\]]+`
     * (1자 이상)에 안 걸려 **정규식 전체가 실패하고 그 조각이 통째로 `name: null` 이 된다.**
     * 지금 부르는 쪽은 늘 접두어를 붙여 이 갈래에 안 닿지만(claude.js), `toolLine` 은
     * 공용 렌더러라 다음 호출자가 그렇다는 보장이 없다. 파서를 `*` 로 느슨하게 푸는 대신
     * 렌더 쪽에서 막는다 — 빈 꼬리는 어차피 아무것도 안 알려 준다. */
    const tail = (s) => String(s).replace(/[()[\]]/g, '').trim();
    const narrowedTo = t?.narrowedTo ? tail(t.narrowedTo) : '';
    const widenedFrom = t?.widenedFrom ? tail(t.widenedFrom) : '';
    return [
      t?.chars != null ? `${head} ${num(t.chars)}자` : head,
      narrowedTo ? `[좁힘: ${narrowedTo}]` : null,
      widenedFrom ? `[넓힘: ${widenedFrom}]` : null,
    ].filter(Boolean).join(' ');
  }

  /* 그 줄을 다시 읽는 자리가 둘이다 — `scripts/check-tool-line-roundtrip.js` 와
   * `scripts/run-log-measure.js`. 접두어를 거기 또 적으면 여기가 바뀔 때 조용히 갈린다.
   * (renderEntry 가 meta 를 `> ` 로 감싸므로 `**도구** ` 앞에 `> ` 가 붙는다.) */
  const TOOL_LINE_PREFIX = '> **도구** ';

  /**
   * `toolLine` 의 **역함수** — 렌더된 **도구** 줄을 조각으로 되읽는다.
   *
   * 왜 여기 있나: 재보기(도구 호출 건수·읽은 크기·6만 자에 걸린 비율)는 이 줄을 기계로
   * 다시 읽어서 낸다. 그런데 이 줄은 되돌리기 어렵게 생겼다 — 인자 **값 안에** 괄호·쉼표·
   * 화살표가 그대로 들어가고, 80자에서 잘리고, 크기는 있을 수도 없을 수도 있다. md 만
   * 보고 새로 쓴 정규식은 **틀려도 에러가 안 나고 조각이 조용히 빠진다.** 2026-09-06
   * 재보기에서 실제로 났다(`\([^)]*\)` 로 인자를 잡아 문서 이름에 괄호가 든 것을 놓쳤고,
   * 기준선을 다시 재서 대보고서야 드러났다). 그래서 파서를 렌더 **바로 옆에** 둔다.
   *
   * 갈래를 가르는 규칙 셋:
   *
   *   ① **조각 경계는 `' → '` 뒤에 `이름(` 이 올 때만.** 그냥 화살표로 자르면 인자 값에
   *      화살표가 든 조각이 둘로 쪼개져 **도구를 더 많이 부른 것으로 읽힌다.**
   *   ② **인자는 첫 `(` 뒤부터 마지막 `)` 앞까지.** 값 안의 괄호를 인자 끝으로 읽지 않는다.
   *      크기(`N자`)는 그 뒤에만 붙으므로 탐욕적으로 맞춰 자연히 갈린다.
   *      **그래서 `[좁힘:]`·`[넓힘:]` 꼬리에는 `)`·`]` 가 없다는 것이 렌더 쪽 계약이다** —
   *      꼬리에 `)` 가 들어오면 탐욕 매칭이 거기까지 먹어 **인자 경계가 뒤로 밀리고**, `]` 가
   *      들어오면 `[^\]]+` 가 먼저 끊긴다. 어느 쪽이든 에러 없이 조각이 안 읽힌다.
   *      `toolLine` 이 꼬리 값에서 그 둘을 지워서 보낸다(지우고 비면 꼬리를 아예 안 붙인다).
   *      꼬리가 **크기 뒤에** 오는 것도 같은 이유다 — 크기를 먼저 되물려야 자리가 갈린다.
   *   ③ **못 읽은 조각도 버리지 않는다** — `name: null` 로 남긴다. 조용히 빼면 이 함수가
   *      옛 파서와 같은 고장 방식을 갖는다(개수만 작아지고 에러는 안 난다).
   *
   * **되찾을 수 없는 것은 되찾은 척하지 않는다.** 인자가 80자에서 잘렸으면 `truncated`
   * 로 표시만 한다 — 원문은 애초에 md 에 없다. 인자 **키**도 마찬가지다(`toolLine` 이
   * 값만 이어붙인다). `chars` 가 `null` 인 것은 0자가 아니라 **크기를 모르는 것**이다 —
   * `chars` 필드는 나중에 생겼고 그전 기록에는 없다. 세는 쪽에서 0으로 접으면 「안 읽었다」와
   * 「못 쟀다」가 같은 칸에 들어간다(`cacheStats` 가 같은 이유로 칸을 가른다).
   *
   * @param {string} line `> **도구** …` 한 줄
   * @returns {{name:string|null, args:string|null, chars:number|null, truncated:boolean,
   *   narrowedTo:string|null, widenedFrom:string|null, raw:string}[]|null}
   *   **도구** 줄이 아니면 `null`. 빈 배열(도구 0개)과 「줄이 없다」를 가르려는 것이다.
   */
  function parseToolLine(line) {
    if (typeof line !== 'string' || !line.startsWith(TOOL_LINE_PREFIX)) return null;
    const value = line.slice(TOOL_LINE_PREFIX.length).replace(/\s+$/, '');
    if (!value) return [];
    return value.split(/ → (?=[A-Za-z_]\w*\()/).map((raw) => {
      // `(.*)` 는 탐욕적이라 마지막 `)` 까지 먹고, 그 뒤 `N자` 가 있으면 그만큼만 되물린다.
      const m = /^([A-Za-z_]\w*)\((.*)\)(?:\s+([\d,]+)자)?(?:\s+\[좁힘: ([^\]]+)\])?(?:\s+\[넓힘: ([^\]]+)\])?$/.exec(raw);
      if (!m) return { name: null, args: null, chars: null, truncated: false, narrowedTo: null, widenedFrom: null, raw };
      return {
        name: m[1],
        args: m[2],
        chars: m[3] == null ? null : Number(m[3].replace(/,/g, '')),
        truncated: m[2].endsWith('…'),
        narrowedTo: m[4] ?? null,
        widenedFrom: m[5] ?? null,
        raw,
      };
    });
  }

  /**
   * 회차 헤더(`### 시각 · 누가 · 어디 · 종류`)의 역함수.
   *
   * **종류를 앵커로 삼는다.** 사람 이름·자리 이름에 `·` 가 들어갈 수 있어서 앞에서부터
   * 자르면 갈린다. 종류는 `KIND_LABEL` 의 값(또는 원래 kind 문자열)이라 **여기 목록이
   * 원본**이다 — 검사·재보기 쪽에 베끼지 않는다.
   *
   * `who`·`where` 는 남은 부분을 마지막 `·` 에서 가른다. 둘 중 하나에 `·` 가 들어 있으면
   * `who` 쪽으로 붙는다 — 되찾을 수 없는 자리라 아는 척하지 않고 여기 적어 둔다.
   *
   * @returns {{time:string, who:string, where:string, kind:string, ok:boolean}|null}
   */
  function parseEntryHeader(line) {
    if (typeof line !== 'string' || !line.startsWith('### ')) return null;
    let rest = line.slice(4).replace(/\s+$/, '');
    const ok = !rest.endsWith(' · ⚠️ 실패');
    if (!ok) rest = rest.slice(0, -' · ⚠️ 실패'.length);
    const m = /^(\d{2}:\d{2}) · (.*)$/.exec(rest);
    if (!m) return null;
    const labels = Object.values(KIND_LABEL);
    const hit = labels.find((l) => m[2].endsWith(` · ${l}`));
    // 아는 종류가 아니면 마지막 조각을 그대로 종류로 둔다 (renderEntry 가 kind 를 그냥 쓴다).
    const cut = hit ? m[2].length - hit.length - 3 : m[2].lastIndexOf(' · ');
    if (cut < 0) return null;
    const head = m[2].slice(0, cut);
    const kind = m[2].slice(cut + 3);
    const at = head.lastIndexOf(' · ');
    return {
      time: m[1],
      who: at < 0 ? head : head.slice(0, at),
      where: at < 0 ? '' : head.slice(at + 3),
      kind,
      ok,
    };
  }

  /* 비용 줄의 **첫 칸**에 모델이 실렸나. 첫 칸이 모델 자리이기 때문이다
   * (렌더가 `[모델, ...비용칸].join(' · ')` 으로 쓴다).
   *
   * **머리말(`비용`/`소요`)로 가르면 안 된다.** 머리말은 모델 유무가 아니라 **미상 시도
   * 유무**로 갈린다 — 모델이 실린 `소요` 줄이 실제로 나오고, 머리말로 재면 그 줄이
   * 「모델 없음」이 된다 (2026-09-16).
   *
   * **둘이 아니라 셋으로 가른다.** 처음에는 「아는 비용 칸이 아니면 모델」로 뒀는데, 그러면
   * 망가진 줄의 첫 칸(예: `1.5초(재시도)`)이 모델이 되고 **없는 단가로 쪼갠 숫자가 합계에
   * 들어간다.** 경고는 나지만 지어낸 숫자가 함께 나가므로 경고만으로는 모자랐다 —
   * 읽는 쪽이 숫자를 아예 안 내고 멈추는 편이 맞다 (2026-09-16).
   *
   *   ① 아는 비용 칸이다        → 모델 없음. 셀 것이 없으니 건너뛴다
   *   ② 모델 이름 모양이다      → 그 모델로 센다
   *   ③ 둘 다 아니다            → **못 읽음**. 읽는 쪽이 멈춘다
   *
   * 아래 `COST_FIELD` 는 렌더가 비용 칸에 쓸 수 있는 **네 모양**이고(`render.js` 의
   * `cost` 배열이 원본), `MODEL_PART` 는 모델 이름의 모양이다 — API 가 주는 id 와
   * `usageFields` 가 만드는 `a+b` 합침, `(unknown)` 까지. **부정이 아니라 긍정으로 잰다.** */
  const COST_FIELD = /^(?:[\d.]+초$|in |확인분 USD |약 \$)/;
  const MODEL_PART = /^(?:\(unknown\)|[A-Za-z0-9][A-Za-z0-9._-]*)$/;
  const isModelName = (s) => s.split('+').every((p) => MODEL_PART.test(p));

  /**
   * 비용 줄(`> **비용** 모델 · 12.3초 · in … · 약 $0.615`)의 역함수.
   * 미상 시도가 섞인 회차는 `> **소요** 모델 · … · 확인분 USD 0.372000 / 미상 1회 (총액 미상)`
   * 으로 나가고, 모델이 아예 없는 회차도 `> **소요** …` 다. 셋 다 받는다.
   *
   * **없는 값은 `null` 이다.** `costUsd` 가 안 실린 회차를 0 으로 접으면 「공짜였다」와
   * 「안 쟀다」가 같은 칸에 들어간다 — `cacheStats` 가 칸을 가르는 것과 같은 이유다.
   *
   * **`costUsd` 와 `knownCostUsd` 를 가른다.** 미상이 섞이면 총액은 모르지만 **확인된
   * 지출은 있다.** 그 둘을 한 칸에 담아 `null` 만 내던 동안, 합계를 내는 쪽이 확인된
   * 금액까지 통째로 버리고 **아무 표시 없이 적게** 찍었다 (2026-09-16).
   *
   * **돈이 적힌 줄은 셋이다.** 사용량 기록(`> **계측 금액** …`)도 여기서 받는다 — 그 줄을
   * 못 읽던 동안 그 돈이 월 머리말 합계에는 들어가고 분해 도구에는 안 들어가, 같은
   * 저장소의 두 숫자가 아무 표시 없이 어긋났다. 그 줄에는 모델이 없다(첫 칸이 시각이다).
   *
   * `unreadable` 은 **「돈 줄인데 못 읽었다」**이다. `null`(돈 줄이 아니다)과 갈라야 한다 —
   * 합치면 망가진 줄이 「그런 줄 없었다」와 같은 글자가 되어 조용히 사라진다. 읽는 쪽은
   * 이걸 보면 숫자를 내지 말고 멈춰야 한다.
   *
   * @returns {{accounting:boolean, unreadable:boolean, costUsd:number|null,
   *            knownCostUsd:number|null, unknownAttempts:number, seconds:number|null,
   *            model:string|null, hasModel:boolean}|null}
   */
  function parseCostLine(line) {
    if (typeof line !== 'string') return null;
    // 표시만 있고 뒤가 빈 줄(찢어진 쓰기)도 **돈 줄로 받아서** 아래에서 못 읽음으로 올린다.
    const m = /^> \*\*(비용|소요|계측 금액)\*\*(?: (.*))?$/.exec(line.replace(/\s+$/, ''));
    if (!m) return null;
    const rest = m[2] || '';
    const usd = /약 \$([\d.]+)/.exec(rest);
    const partial = /확인분 USD ([\d.]+) \/ 미상 (\d+)회/.exec(rest);
    const sec = /(?:^|· )([\d.]+)초/.exec(rest);
    /* 계측 기록만 머리말로 가른다. **모양이 아니라 뜻으로** 가르는 것이라 괜찮다 —
     * 이 줄은 회차가 아니라 사용량 기록이고, 구조상 모델이 실릴 자리가 없다.
     * (`비용`/`소요` 를 가르는 데 머리말을 쓰면 안 되는 것과는 다른 이야기다.) */
    const accounting = m[1] === '계측 금액';
    const head = accounting ? '' : rest.split(' · ')[0];
    const model = !accounting && isModelName(head) ? head : null;
    return {
      accounting,
      // 계측 기록은 확인분·미상을 지고 오는 것이 전부다 — 그게 없으면 읽을 것이 없다.
      unreadable: accounting ? !partial : (!rest || (!model && !COST_FIELD.test(head))),
      costUsd: usd ? Number(usd[1]) : null,
      // 확인분이 적힌 줄에서는 그것이 「확인된 지출」이다. 없으면 총액이 곧 확인분이다.
      knownCostUsd: partial ? Number(partial[1]) : usd ? Number(usd[1]) : null,
      unknownAttempts: partial ? Number(partial[2]) : 0,
      seconds: sec ? Number(sec[1]) : null,
      model,
      hasModel: model !== null,
    };
  }

  return { toolLine, TOOL_LINE_PREFIX, parseToolLine, parseEntryHeader, parseCostLine };
}
