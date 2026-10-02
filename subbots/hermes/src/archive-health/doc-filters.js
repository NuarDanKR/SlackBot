/** Document candidate identity and exclusion rules shared by health consumers. */
export function createHealthDocFilters({ activeDeferred }) {
  /**
   * (채널, 확장자 뺀 이름) 키.
   *
   * 구분자를 join 안에 둔다 — 템플릿 문자열 안에 구분자를 직접 쓰다가
   * 리터럴 NUL 이 두 번 들어갔고, 그랬더니 git 이 이 파일을 통째로
   * 바이너리로 보고 diff 가 안 나왔다 (2026-08-03).
   * 슬랙 채널명과 파일명에는 '|' 가 들어갈 수 없어 키가 겹치지 않는다.
   */
  const nameKey = (channel, name) => [channel, stem(name)].join('|');

  /** 확장자를 뺀 이름 (중복 포맷 판정용) */
  function stem(name) {
    const dot = String(name).lastIndexOf('.');
    return (dot > 0 ? String(name).slice(0, dot) : String(name)).toLowerCase();
  }

  /**
   * 문서 후보를 거를 때 쓰는 집합 여덟. 이미 읽어들인 `.doc-state.json` 에서 만든다.
   *
   * **`fetch_slack_files.py` 의 `load_filters` 와 같은 판정이어야 한다.** 그 파일이 못박은
   * 「세 곳(수집 · `pendingDocuments` · `unconvertedAmong`)이 같아야 한다」가 이것이고,
   * 갈리면 **에러가 안 난다** — 한쪽은 「N건 남음」, 한쪽은 「0건」이라고 말할 뿐이다.
   *
   * **주석으로 지키지 않는다** — `scripts/check-shared-rules.js` 가 두 구현에 같은 후보를
   * 먹여 답을 대본다. 2026-08-26 까지 이 판정은 `pendingDocuments` 안에 인라인이라
   * **불러볼 수가 없었고**, 그래서 그 검사가 코드 모양(`excludedNames.has(` 개수)으로 셌다.
   * 그날 실측: **이름만 `exNames` 로 바꿔도 실패하고 진짜로 지워도 실패**해서 둘이
   * 구별되지 않았다. 같은 이유로 `deferredNames` 는 어느 검사도 안 봤다 — 거르는 줄
   * 넷을 통째로 지워도 검사 14종이 전부 통과했다.
   *
   * 갈래별로 왜 이렇게 보는지:
   *
   *   · `knownNames` — 같은 자료를 hwp 와 pdf 로 함께 올리는 일이 흔한데, doc-archive 는
   *     원본 포맷 하나만 변환하고 **나머지 판은 상태 파일에 기록하지 않고 버린다.** 파일
   *     ID 만 보면 그 버려진 판이 영영 「미변환」으로 남는다 — 변환될 일이 없으니 숫자가
   *     안 줄고, 사람은 곧 알림을 무시한다 (2026-08-03: 넣기 전 35건 중 대부분이 이 헛경보).
   *   · `excluded` — 의도적으로 변환하지 않기로 한 것(개인 인적사항이 든 등기부등본 등).
   *     이유가 `index.md` 에 글로만 있으면 코드가 못 읽어 매일 다시 「미변환」으로 알린다.
   *     **ID 와 이름 둘 다로 본다** (2026-08-12) — 같은 자료를 다시 올리면 ID 가 새로
   *     생기는데 판정은 그 **자료**에 내린 것이다. 전에는 여기만 ID 였고 `unconvertedAmong`
   *     은 이름이라, 다시 올린 순간 요약 하단은 「0건」인데 이 점검은 「1건」이라고 말했다.
   *   · `deferred` — 「이번엔 아니고 나중에」. 만기가 지나면 `activeDeferred` 에서 빠져
   *     저절로 다시 세어진다. 그래서 미뤄 둔 것을 잊을 수 없고, 그 전까지 줄지 않는
   *     숫자로 알림을 태우지도 않는다.
   *   · `superseded` — 「최신 1건이 대신한 옛 판」 (2026-08-26 에 더했다). 그전에는 이
   *     파일이 그 칸을 **아예 안 봤다.** 지금 걸린 3건이 전부 엑셀이고 `DOC_EXTS` 에
   *     xlsx 가 없어 안 드러났을 뿐이라, **거기 xlsx 를 넣는 순간** 수집은 「0건」인데
   *     이 점검은 「N건 남음」이라고 DM 을 보냈을 자리다.
   *
   * dict 가 아닌 값은 이름 집합에서 건너뛴다 (파이썬 쪽과 같은 이유 — 상태 파일이 망가져도
   * 그 항목만 이름 경로를 못 타고 ID 경로는 산다).
   */
  function docFilters(state, now = Date.now()) {
    const names = (d) => new Set(
      Object.values(d || {})
        .filter((v) => v && typeof v === 'object' && !Array.isArray(v))
        .map((v) => nameKey(v.channel, v.name || '')),
    );
    const deferredList = activeDeferred(state, now);
    return {
      known: new Set(Object.keys(state?.slack_files || {})),
      knownNames: names(state?.slack_files),
      excluded: new Set(Object.keys(state?.excluded || {})),
      excludedNames: names(state?.excluded),
      deferred: new Set(deferredList.map(([id]) => id)),
      deferredNames: new Set(deferredList.map(([, v]) => nameKey(v.channel, v.name || ''))),
      superseded: new Set(Object.keys(state?.superseded || {})),
      supersededNames: names(state?.superseded),
    };
  }

  /**
   * 후보 하나 → 제외 사유. `null` 이면 후보로 남는다.
   *
   * **`fetch_slack_files.py` 의 `classify` 와 갈래도 순서도 같아야 한다.** 순서를 바꾸면
   * 같은 파일이 두 칸에 든 경우 먼저 보는 쪽으로 세어져 화면 숫자가 조용히 갈린다.
   *
   * **파일 ID 가 없는 자리가 있다** — `unconvertedAmong` 의 입력은 요약 꼬리말용이라
   * 채널과 이름뿐이다. 그 자리는 이름 경로만 타며, ID 경로는 조용히 넘어간다.
   */
  function classifyDoc(rec, filters) {
    const id = rec?.id;
    if (id && filters.known.has(id)) return 'known';
    const key = nameKey(rec?.channel, rec?.name || '');
    if ((id && filters.excluded.has(id)) || filters.excludedNames.has(key)) return 'excluded';
    if ((id && filters.deferred.has(id)) || filters.deferredNames.has(key)) return 'deferred';
    if ((id && filters.superseded.has(id)) || filters.supersededNames.has(key)) return 'superseded';
    if (filters.knownNames.has(key)) return 'other_format';
    return null;
  }

  return { nameKey, docFilters, classifyDoc };
}
