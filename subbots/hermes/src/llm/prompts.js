/**
 * Prompt rendering and archive-index cache, extracted by Codex.
 * A facade owns one instance. Creating an instance does not read files;
 * configuration is consulted at call time, just as before extraction.
 */
import nodeFs from 'node:fs';
import nodePath from 'node:path';
import { terms as domainTerms } from '../domain.js';

export function createPromptContext({
  ROOT, DATA_ROOT, ARCHIVE_DIR, DOCS_DIR, config, accessLabel, canSeePrivateChannel,
  /* 표시 용어 프로필. 안 주면 자료 저장소의 `domain` 을 본다 — **기본값은 없다.**
   * 환경변수가 설정을 이기는 것은 `config.js` 의 `DOMAIN` 이 정하고, 운영은 그 값을
   * 여기로 넘긴다(`claude.js`). 검사 픽스처는 자기 값을 넘기면 된다. */
  domain = undefined,
  buildArchiveBriefSplit, buildDocumentsBriefSplit, hasDocuments, listArchivedChannels,
  fs = nodeFs, path = nodePath,
}) {
  /**
   * 프롬프트 예시에 쓰는 이름. **자료 저장소에서 온다.**
   *
   * 코드 저장소는 팀끼리 나눠 쓰므로 여기 사업 이름을 박으면 남의 팀 봇이 그것을 물고
   * 돈다 — 프롬프트는 실행 중 시스템 메시지에 그대로 실린다. `config.json` 은 자료
   * 저장소에 있으니 우리 값은 거기 두고, 코드에는 자리표시자와 일반 기본값만 남긴다.
   * 안 적어도 기본값이 들어가므로 새 팀은 아무것도 안 해도 된다.
   *
   * 키는 쓰임이 아니라 **이름 기준**이다 — 같은 이름이 `#이름`(채널)과 `*이름*`(사업장)
   * 양쪽에 쓰여서, 쓰임별로 나누면 같은 사업장이 두 키가 된다.
   */
  /* 예시 이름은 **프로필 것**이다 (2026-10-08). 사내 인스턴스 화면에 「사업장가」 가
   * 뜨면 안 된다 — 그건 오류가 아니라 어색한 문장이라 아무도 고쳐 달라고 안 한다. */
  const profile = () => domainTerms(domain ?? config.domain);
  const EXAMPLE_FALLBACK = () => profile().examples;

  /**
   * 회사·팀 이름과 받는 사람 표기도 **자료 저장소에서 온다** (2026-09-01).
   *
   * 사업장 이름과 같은 이유인데 발견은 늦었다 — 이 검사(`check-business-names.js`)는
   * 자료 저장소의 채널·사업장 이름과 대볼 뿐이라 **회사 이름은 목록에 아예 없다.**
   * 그래서 프롬프트 세 개의 첫 줄에 실제 회사·팀 이름이, 둘에는 받는 사람의 직위까지
   * 남아 있는 동안 검사는 계속 초록이었다. 약칭 축을 넣다가 눈으로 찾았다.
   *
   * 기본값은 일반 이름이라 **새 팀은 아무것도 안 적어도 문장이 성립한다.**
   * 받는 사람은 `label`(표기용) → `name`(다른 곳에서 이미 쓰는 값) 순으로 본다.
   */
  const ORG_FALLBACK = '우리 팀';
  const OWNER_FALLBACK = '담당자';

  /**
   * 프롬프트 하나를 읽어 자리표시자를 채운다.
   *
   * 검사(`check-bootstrap.js` 갈래 ⑨)가 부를 수 있게 내보낸다 — 「치환 뒤에 자리표시자가
   * 남지 않나」는 실제로 렌더해 봐야 알 수 있고, 안 보면 봇이 `{{EX_A}}` 를 그대로 읽는다.
   */
  function renderPrompt(n) {
    const raw = fs.readFileSync(path.join(ROOT, 'src', 'prompts', `${n}.md`), 'utf8');
    // 빈 값은 거른다. `config.example.json` 이 키를 빈 문자열로 들고 있어서, 그대로
    // 병합하면 기본값을 밀어내고 예시 이름이 통째로 사라진다.
    const given = Object.fromEntries(
      Object.entries(config.promptExamples || {})
        .filter(([k, v]) => /^EX_[A-Z]$/.test(k) && typeof v === 'string' && v.trim()),
    );
    const t = profile();
    const ex = { ...t.examples, ...given };
    // `<…>` 로 시작하는 값은 안 채운 자리다 — config.example.json 을 복사만 하고 값을
    // 안 넣으면 `<받는 사람 이름>` 이 그대로 프롬프트에 실린다. 빈 값과 같이 본다.
    const filled = (v) => (typeof v === 'string' && v.trim() && !/^<.*>$/.test(v.trim()) ? v.trim() : '');
    ex.ORG = filled(config.org) || ORG_FALLBACK;
    ex.OWNER = filled(config.owner?.label) || filled(config.owner?.name) || OWNER_FALLBACK;
    /* 표시 용어도 자리표시자로 넣는다 — `{{ORG}}` 와 같은 자리, 같은 관문
     * (`check-bootstrap.js` 갈래 ⑨ 가 「치환 뒤 자리표시자가 남지 않나」를 본다). */
    ex.PLACE = t.place;
    ex.AREA = t.area;
    return raw.replace(/\{\{(EX_[A-Z]|ORG|OWNER|PLACE|AREA)\}\}/g, (m, k) => ex[k] ?? m);
  }

  const promptFile = renderPrompt;

  /** 아카이브 마지막 동기화 시각 */
  function lastSyncedAt() {
    try {
      const p = path.join(ARCHIVE_DIR, '.sync-state.json');
      return JSON.parse(fs.readFileSync(p, 'utf8')).last_sync || '알 수 없음';
    } catch {
      return '알 수 없음';
    }
  }

  /**
   * 색인이 바뀌었는지 알아내는 지문.
   *
   * VM 은 저장소를 git pull 로만 갱신하므로 '커밋이 들어왔다 = 자료가 바뀌었다' 이다.
   * 그래서 파일을 일일이 훑는 대신 .git 쪽 파일 몇 개의 수정시각을 본다 —
   * 채널이든 문서든 새로 추가된 것을 빠뜨릴 수 없다.
   * (예전에는 slack-export/index.md 하나만 봤는데, 그 파일을 안 건드리고
   *  채널 md 만 바뀌면 캐시가 갱신되지 않았다. slack-sync 가 늘 index.md 를
   *  함께 고쳐서 우연히 동작하고 있었을 뿐이다.)
   * 로컬에서 커밋 전에 편집하는 경우를 위해 두 index.md 도 함께 섞는다.
   *
   * **`.git/FETCH_HEAD` 는 보지 않는다** (2026-08-06). 이 파일은 pull 이 **새 커밋을
   * 가져오지 않아도 무조건** 다시 쓰인다. VM 이 15분마다 pull 하므로, 아무것도 안 바뀐 날에도
   * 하루 96번 지문이 달라져 권한 조합마다 색인을 통째로 다시 만들었다(회당 약 140ms).
   * 커밋이 실제로 들어오면 refs/heads/main 이 움직이므로 그것만 봐도 놓치지 않는다.
   * packed-refs 를 함께 보는 것은 갓 클론한 저장소처럼 main 이 느슨한 ref 로 없을 때를 위한
   * 자리다 — 그때 refs/heads/main 은 0 이 되고 packed-refs 가 유일한 신호다.
   *
   * **보는 것은 자료 저장소다** (2026-08-31 저장소를 가르며 고침). 색인의 원천인
   * `slack-export`·`documents` 가 전부 거기 있고 코드 저장소에는 없다. 그전에는 코드가 자료와
   * 한 저장소 안에 있어 `ROOT/../..` 가 곧 그 저장소였는데, 갈린 뒤 그 자리는 저장소가 아닌
   * 상위 폴더(VM `/opt`)가 되어 **두 신호가 조용히 0 으로 굳었다.** `statSync` 가 없는 파일에
   * 0 을 주므로 에러가 안 난다. 지키는 것은 `scripts/check-brief-stamp.js`.
   */
  const REPO_ROOT = DATA_ROOT;

  /**
   * 지문이 보는 자리를 그대로 내놓는다. **`scripts/check-brief-stamp.js` 가 실물을 대보라고 있다** —
   * 검사가 같은 계산을 따로 적으면 이 함수가 엉뚱한 곳을 봐도 둘이 사이좋게 틀린다.
   * 순서와 개수는 지문의 일부다(자리 하나가 빠지면 키가 짧아진다). `null` 은 「그 자리는 안 본다」.
   */
  function briefStampPaths() {
    return [
      path.join(REPO_ROOT, '.git', 'refs', 'heads', 'main'),
      path.join(REPO_ROOT, '.git', 'packed-refs'),
      path.join(ARCHIVE_DIR, 'index.md'),
      DOCS_DIR ? path.join(DOCS_DIR, 'index.md') : null,
      // `.sync-state.json` 은 2026-09-16 부터 권한 판정의 입력이다. 이 파일만 바뀌고
      // 커밋 전이면 git 신호도 index.md 도 안 움직인다 — 안 보면 옛 지도로 만든 색인이
      // 캐시에 남는다.
      path.join(ARCHIVE_DIR, '.sync-state.json'),
    ];
  }

  function briefStamp() {
    const st = (p) => {
      if (!p) return 0;
      try {
        return fs.statSync(p).mtimeMs;
      } catch {
        return 0;
      }
    };
    return briefStampPaths().map(st).join('-');
  }

  /**
   * 시스템 프롬프트를 **두 덩이로** 만든다 — 누구에게나 같은 것과 이 질문자에게만 열린 것.
   *
   * 왜 나누나: 시스템 블록에는 1시간 캐시가 걸려 있는데, 캐시 항목은 **글자가 한 자라도
   * 다르면 따로** 생긴다. 색인은 열람 권한마다 내용이 달라서 질문자가 바뀔 때마다 2.7만
   * 토큰을 통째로 다시 저장했다 — 2026-08-05~10 실측으로 문답 49건 중 29건이 재작성이었고,
   * 그 값이 그 기간 캐시 쓰기 비용 $7.82 의 거의 전부다.
   *
   * 그런데 권한에 따라 달라지는 것은 **8.5%뿐**이다. 그 92%를 앞 블록에 몰아 두면 첫 사람이
   * 쓰고 나머지는 읽는다(1/10 단가). 나누는 규칙과 검증은 archive.js 의
   * buildArchiveBriefSplit 주석 · scripts/check-brief-split.js 에 있다.
   *
   * 메모리 캐시도 그에 맞춰 둘로 둔다. 공통은 stamp 만으로 한 벌, 추가분은 권한 조합마다
   * 한 벌. 아카이브가 바뀌면(briefStamp) 낡은 것만 버리고 현재 것은 남긴다 — 예전에는
   * 미스마다 통째로 clear 했는데, 그러면 질문자가 번갈아 물을 때마다 색인을 다시 만든다.
   */
  const sysCache = new Map();
  const COMMON_KEY = '_공통';

  function systemBlocks(access) {
    const stamp = briefStamp();
    const commonKey = `${COMMON_KEY}:${stamp}`;
    const extraKey = `${accessLabel(access)}:${stamp}`;
    if (!sysCache.has(commonKey) || !sysCache.has(extraKey)) {
      for (const k of sysCache.keys()) {
        if (!k.endsWith(`:${stamp}`)) sysCache.delete(k);
      }
      const archive = buildArchiveBriefSplit({ access });
      const documents = hasDocuments()
        ? buildDocumentsBriefSplit({ access })
        : { common: '', extra: '' };
      // 채널 md 가 하나도 없으면 색인은 index.md 전문 + 빈 헤더뿐이다. 그대로 두면 봇이
      // 「자료가 없습니다」라고 답하는데, 그것은 「안 채웠다」와 뜻이 다르다.
      const noChannels = listArchivedChannels().length === 0;
      sysCache.set(
        commonKey,
        promptFile('qa')
          .replace('{{ARCHIVE_BRIEF}}', noChannels
            ? '_(대화 아카이브가 아직 비어 있습니다 — `slack-sync` 로 채우기 전입니다. '
              + '「자료가 없다」가 아니라 「아직 안 채웠다」고 답하세요)_'
            : archive.common)
          .replace('{{DOCUMENTS_BRIEF}}', documents.common || '_(문서 아카이브가 아직 비어 있습니다)_'),
      );
      sysCache.set(extraKey, [archive.extra, documents.extra].filter(Boolean).join('\n\n---\n\n'));
    }
    return { common: sysCache.get(commonKey), extra: sysCache.get(extraKey) };
  }

  /**
   * 매 요청과 함께 보내는 한 줄. **어느 비공개 채널을 인용해도 되는지 이름으로 못박는다.**
   * "허용/금지" 두 마디로는 안 된다 — 비공개 채널이 여럿이고 멤버가 서로 달라서
   * 질문이 어디서 왔는지에 따라 열리는 채널이 달라진다. 도구가 이미 막고 있지만,
   * 색인·검색 결과에 이름이 스쳤을 때 모델이 지레짐작하지 않게 여기서도 한 번 적는다.
   */
  function privateQuoteLine(access) {
    if (access?.full) return '전부 (이 자리는 본인만 봅니다)';
    const allowed = config.privateChannels.filter((c) => canSeePrivateChannel(access, c));
    // **막힌 채널의 이름은 적지 않는다** (WHK 지시 2026-08-05). 예전에는 "인용 금지: #비공개가"
    // 처럼 적어 두었는데, 그러면 봇이 그 이름을 알게 되어 "그건 #비공개가에 있습니다" 로
    // 되짚어 말한다. 열린 것만 이름으로 적고, 나머지는 존재도 말하지 말라고만 한다.
    if (!allowed.length) {
      return '없음 — 비공개 채널 내용을 인용하지 말고, 그런 채널이 있다는 것도 언급하지 말 것';
    }
    return `${allowed.map((c) => `#${c}`).join(', ')} 만. 그 밖의 비공개 채널은 존재도 언급하지 말 것`;
  }


  return { renderPrompt, lastSyncedAt, briefStampPaths, systemBlocks, privateQuoteLine };
}
