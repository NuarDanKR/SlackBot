/**
 * Codex R2c: document access, masked names and permission-aware resolution.
 * Factory construction performs no lookups. No facade imports or shared request state.
 * Keep byte-identical in Hermes and Clio; preserve existing policy and call order.
 */
export function createDocumentAccess({
  /* 표시 용어. **주입받는다** — 이 모듈은 설정을 안 읽는 팩토리이고,
   * 같은 호스트에서 프로필이 다른 인스턴스가 함께 돌기 때문이다(2026-10-08). */
  terms: T,
  isPrivateChannel, normalizeChannel, canSeePrivateChannel, listProjects, listDocuments, resolveProject, resolveDocument, matchesHiddenPrivate, BLOCKED_NOTE
}) {
  /** '_공통/정기보고' 의 비공개 판정은 맨 앞 마디로 한다 (채널명과 맞물리는 자리) */
  function projectIsPrivate(project) {
    return isPrivateChannel(project.split('/')[0]);
  }

  /** 그 문서를 가둔 비공개 채널 이름. 공개 채널 자료면 null */
  function projectPrivateChannel(project) {
    const top = normalizeChannel(project.split('/')[0]);
    return isPrivateChannel(top) ? top : null;
  }

  /**
   * 비공개 채널에서 `[공개]` 승인으로 열린 문서를 담아 보이는 **가상 사업장 이름**.
   *
   * 승인된 문서는 공개 답변에서 인용하라고 연 것인데(WHK 결정 2026-08-05), 그 문서가 든
   * 폴더 이름이 곧 비공개 채널 이름이라 사업장 칸에 이름이 그대로 나갔다 — 색인 헤더,
   * 검색 결과, "후보: …" 목록, 요약 프롬프트 네 군데다 (2026-08-10 발견. 공개 권한으로
   * buildDocumentsBrief 를 부르면 `### 비공개나 (문서 1 · 회차 1)` 이 나왔다).
   *
   * 문서는 살리고 이름만 가린다 (WHK 결정 2026-08-10). 그래서 **밖으로 나가는 이름을 전부
   * 이 이름으로 모으고**, 봇이 그 이름으로 다시 물어오면 realProjects 가 실제 폴더로 되돌린다.
   * 앞에 밑줄을 둔 것은 `_공통` 처럼 사업장이 아닌 자리와 나란히 놓이게 하려는 것이다.
   */
  const APPROVED_PROJECT = '_승인자료';

  /** 밖으로 나가는 사업장 이름. 볼 수 없는 비공개 채널의 자료면 가상 이름으로 바꾼다. */
  function maskProject(project, access) {
    const ch = projectPrivateChannel(project);
    return ch && !canSeePrivateChannel(access, ch) ? APPROVED_PROJECT : project;
  }

  /**
   * 가상 이름을 실제 사업장들로 되돌린다. 그 밖의 이름은 그대로 한 개.
   *
   * **권한을 보지 않고 비공개 채널 폴더를 전부 되돌린다.** 무엇을 실제로 볼지는 뒤에서
   * `canSeeDoc` 이 정하므로 여기서 한 번 더 좁히면 오히려 구멍이 난다 — 예전에는
   * `maskProject(p, access) === APPROVED_PROJECT` 로 걸러서, **그 채널 멤버에게는
   * `_승인자료` 가 아무 폴더로도 안 풀렸다.** 그때는 멤버 색인에 그 이름이 안 나와서
   * 드러나지 않았는데, 색인을 쪼개면서 공통 블록이 모두에게 이 이름을 보여주게 됐다
   * (buildDocumentsBriefSplit). 그대로 두면 멤버가 `read_document project:"_승인자료"` 로
   * 물었을 때 "찾지 못했습니다" 가 나온다.
   */
  function realProjects(name) {
    if (name !== APPROVED_PROJECT) return [name];
    return listProjects().filter((p) => projectPrivateChannel(p));
  }

  /** 그 이름(가상 이름 포함)에서 이 권한으로 볼 수 있는 문서 */
  function documentsFor(name, access) {
    return realProjects(name)
      .flatMap((p) => listDocuments(p))
      .filter((d) => canSeeDoc(access, d));
  }

  /**
   * 이 문서를 이 권한으로 볼 수 있나.
   *
   * 비공개 문서는 **자기를 가둔 그 채널의 권한으로만** 열린다 — #비공개가 자료가
   * #사업장카 질문에 나오면 안 된다. `privateChannel` 이 null 인데 private 인 문서는
   * 메타가 깨진 것(loadDocument 의 fail-closed)이라, 권한이 전체일 때만 보여준다.
   */
  function canSeeDoc(access, d) {
    if (!d.private) return true;
    if (!access) return false;
    if (access.full) return true;
    return Boolean(d.privateChannel) && canSeePrivateChannel(access, d.privateChannel);
  }

  /* ── 권한을 본 이름 풀기 ──────────────────────────────────────────
   * 이름을 풀 때부터 권한을 봐야 한다. 이름 풀기는 실패하면 **후보 목록을 돌려주고**,
   * 그 목록이 그대로 봇에게 간다 — 걸러지지 않으면 `#비공개가` 문서 제목 3건이 공개 채널
   * 답변의 근거 자리에 나온다. 제목만으로도 상대·건명·회차가 드러난다 (2026-08-05 발견).
   *
   * 못 보는 것을 "찾지 못했습니다"로 돌려주지 않는 것도 중요하다. 그러면 봇이 "자료가
   * 없습니다"로 오답한다 — 없는 것과 못 보는 것은 다르다.
   */

  /** 이 권한으로 문서가 하나라도 보이는 사업장만. 볼 수 없는 비공개 채널은 가상 이름 하나로 모인다 */
  function visibleProjects(access) {
    const out = [];
    for (const p of listProjects()) {
      if (!listDocuments(p).some((d) => canSeeDoc(access, d))) continue;
      const name = maskProject(p, access);
      if (!out.includes(name)) out.push(name);
    }
    /* 가상 이름은 **볼 것이 있으면 누구에게나** 열어 둔다.
     *
     * 공개 전용 질문자에게는 위 maskProject 가 이 이름을 만들어 주지만, **그 채널 멤버에게는
     * 실명으로 나와서 목록에 `_승인자료` 가 없었다.** 그래도 그때는 멤버 색인에 그 이름이
     * 안 나왔으니 드러나지 않았는데, 색인을 쪼개면서 공통 블록이 모두에게 이 이름을 보여주게
     * 됐다 (buildDocumentsBriefSplit). 그대로 두면 멤버가 색인에서 본 이름으로 물었을 때
     * "'_승인자료' 사업장을 찾지 못했습니다" 가 돌아온다 — 있는 것을 없다고 답하는 모양이다.
     *
     * 여기서 여는 것은 **이름 풀기까지**이고, 무엇이 실제로 열리는지는 canSeeDoc 이 정한다.
     * 멤버에게는 그 폴더의 문서가 실명으로도 이 이름으로도 열리는데, 둘 다 이미 볼 수 있는
     * 것이라 새로 열리는 자료는 없다. */
    if (
      !out.includes(APPROVED_PROJECT)
      && realProjects(APPROVED_PROJECT).some((p) => listDocuments(p).some((d) => canSeeDoc(access, d)))
    ) {
      out.push(APPROVED_PROJECT);
    }
    return out;
  }

  function resolveProjectFor(input, access) {
    const visible = visibleProjects(access);
    const r = resolveProject(input, visible);
    if (r.ok) return r;
    // 볼 수 있는 것 중에 없다면, 전체에서 풀어 보고 "막혔다"와 "없다"를 가른다.
    // 문서가 아직 한 건도 없는 비공개 채널은 이름으로만 잡힌다 (아래 문구가 이름을 되돌려 준다).
    if (matchesHiddenPrivate(input, access)) return { ok: false, error: BLOCKED_NOTE };
    if (resolveProject(input).ok) return { ok: false, error: BLOCKED_NOTE };
    return { ok: false, error: `'${input}' ${T.area}을 찾지 못했습니다. 후보: ${visible.slice(0, 15).join(', ')}` };
  }

  function resolveDocumentFor(project, input, access) {
    const visible = documentsFor(project, access);
    const r = resolveDocument(project, input, visible);
    if (r.ok) return r;
    /* **가상 사업장에서는 "막혔다"와 "없다"를 가르지 않는다.**
     *
     * 실제 폴더들로 되돌리면 볼 수 없는 비공개 채널의 문서까지 훑게 되고, 그 결과가
     * BLOCKED_NOTE 로 나가는 순간 **낱말 하나로 「그 문서가 어딘가 비공개에 있다」를 알아낼 수
     * 있는 창구**가 된다 — `resolveDocument` 는 부분일치도 받으므로 '송도' 한 마디면 걸린다.
     * 「막힌 자료는 이름조차 밝히지 않는다」(WHK 지시)를 정면으로 거스른다.
     *
     * 가상 이름 아래 보이는 것은 전부 아래 후보 목록에 이미 나오므로, 여기서 "찾지 못했다"고
     * 답하는 것은 거짓이 아니다. (2026-08-10 코드 리뷰에서 실측으로 잡혔다) */
    const all = project === APPROVED_PROJECT ? [] : listDocuments(project);
    if (all.length && resolveDocument(project, input, all).ok) {
      return { ok: false, error: BLOCKED_NOTE };
    }
    return {
      ok: false,
      error: `${project} 에서 '${input}' 문서를 찾지 못했습니다. 후보: ${visible.map((d) => d.slug).slice(0, 15).join(', ')}`,
    };
  }

  return { projectIsPrivate, projectPrivateChannel, maskProject, realProjects, documentsFor, canSeeDoc, visibleProjects, resolveProjectFor, resolveDocumentFor };
}
