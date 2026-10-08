/**
 * 도메인 프로필 — **그 인스턴스가 사람에게 쓰는 말.**
 *
 * ## 왜 모드나 서버가 아닌가 (2026-10-08 오너 정정)
 *
 * PF 전용 서버도, 기존 GCP Hermes 운영도 **없다.** 바뀐 PF Hermes 도 TYBot·Archiver 가
 * 있는 **같은 서버**에서 별도 systemd 인스턴스로 돈다.
 *
 * 그래서 「PF 니까 사업장」 은 성립하지 않는다 — 한 호스트에서 **같은 바이너리**로 두
 * 인스턴스가 돌고, 표시 용어는 그 인스턴스가 무엇을 다루는가에 달려 있다. `HERMES_MODE`
 * 에 묶는 것도 같은 이유로 틀렸다: 모드는 「원문을 누가 쓰는가」 이고 용어와 무관하다.
 * 실제로 두 인스턴스가 **같은 모드**(`pf-archiver`)로 돌면서 다른 말을 쓸 수 있다.
 *
 * ## 바꾸는 것은 문구뿐이다
 *
 * 내부 project 키(= 문서 폴더 이름)·채널 ID·색인 형식·경로는 **한 글자도 안 바뀐다.**
 * 그것까지 바꾸면 용어 교체가 아니라 **데이터 이관**이고, 기존 자료가 그 순간 안 읽힌다.
 * 이 모듈이 `node:fs` 를 안 가져가는 것이 그 선이다 — 파일을 만질 수 없으면 자료를
 * 바꿀 수도 없다.
 *
 * ## 누락·오타는 던진다
 *
 * 기본값을 두면 **사내 인스턴스가 사내 사람들에게 「사업장」 이라고 말한다.** 그건 오류가
 * 아니라 어색한 문장으로만 나타나고, 어색한 문장은 아무도 고쳐 달라고 하지 않는다 —
 * 몇 달 뒤 누가 지적할 때까지 간다. 되돌릴 수 없는 쪽은 아니지만 **아무도 모르는 쪽**이라
 * 막는다.
 */

export const ENTERPRISE = 'enterprise';
export const PF_CONSTRUCTION = 'pf-construction';

/** 쓸 수 있는 프로필. 늘릴 때는 `TERMS` 도 함께 — 아래 검사가 빠진 쪽을 잡는다. */
export const DOMAINS = Object.freeze([ENTERPRISE, PF_CONSTRUCTION]);

/** 프로필이 없거나 모르는 값이다. **기동을 막는다** — 고쳐야 하는 것은 설정이다. */
export class DomainConfigError extends Error {
  constructor(raw) {
    super(
      `도메인 프로필이 올바르지 않습니다: ${JSON.stringify(raw)}\n` +
        `쓸 수 있는 값은 ${DOMAINS.map((d) => `"${d}"`).join(' · ')} 입니다.\n` +
        'config.json 의 `domain` 또는 환경변수 HERMES_DOMAIN 에 적으세요 ' +
        '(인스턴스를 가르는 것은 unit 이므로 환경변수가 설정 파일을 이깁니다).\n' +
        '기본값은 두지 않습니다 — 틀린 말투는 오류가 아니라 어색한 문장으로만 나타나서, ' +
        '그 상태가 몇 달 갑니다.'
    );
    this.name = 'DomainConfigError';
    this.code = 'hermes_domain_invalid';
    this.raw = raw;
  }
}

/**
 * 프로필별 **표시 용어**. 키는 쓰임이고 값은 그 인스턴스의 말이다.
 *
 * | 키 | 무엇 |
 * |---|---|
 * | `place` | 자료를 묶어 부르는 단위. 검색을 좁히는 자리(`where`)와 같은 말 |
 * | `area` | 문서가 모여 있는 묶음. PF 는 `place` 와 같은 말이고, 사내는 채널과 구별된다 |
 * | `examples` | 프롬프트 예시 이름. 자료 저장소가 실제 이름을 주면 그쪽이 이긴다 |
 *
 * 사내에서 `area` 를 그냥 「채널」 로 하지 않는 이유: 문서 묶음은 Slack 채널과 **1:1 이
 * 아니다**(`_공통`·`_승인자료` 같은 가상 자리가 있다). 둘을 같은 말로 부르면 「그 채널에
 * 가서 보라」 는 안내가 갈 수 없는 자리를 가리킨다.
 */
const TERMS = Object.freeze({
  [PF_CONSTRUCTION]: Object.freeze({
    place: '사업장',
    area: '사업장',
    examples: Object.freeze({ EX_A: '사업장가', EX_B: '사업장나', EX_C: '사업장다' }),
  }),
  [ENTERPRISE]: Object.freeze({
    place: '채널',
    area: '자료 영역',
    examples: Object.freeze({ EX_A: '채널가', EX_B: '채널나', EX_C: '채널다' }),
  }),
});

/**
 * 설정값 → 프로필. **모르면 던진다.**
 *
 * 공백과 대소문자만 다듬는다 — 그건 오타가 아니라 적는 방식이다. 그 밖은 전부 거부한다:
 * `pf` 처럼 「그럴듯한」 값을 받아 주면 받아 준 값이 무엇으로 해석됐는지 아무도 모른다.
 */
export function resolveDomain(raw) {
  const value = String(raw ?? '').trim().toLowerCase();
  if (!DOMAINS.includes(value)) throw new DomainConfigError(raw);
  return value;
}

/** 그 프로필의 표시 용어. **얼려서 준다** — 밖에서 고치면 그 프로세스 전체가 바뀐다. */
export function terms(domain) {
  return TERMS[resolveDomain(domain)];
}

/** 프로필마다 용어가 빠짐없이 있나. 늘릴 때 한쪽만 적는 것을 막는다. */
export function missingTerms() {
  const keys = ['place', 'area', 'examples'];
  const gaps = [];
  for (const d of DOMAINS) {
    for (const k of keys) if (!TERMS[d]?.[k]) gaps.push(`${d}.${k}`);
  }
  return gaps;
}

/**
 * **쓸 때** 푸는 용어. 모듈을 읽는 시점에는 안 던진다.
 *
 * 왜 필요한가 — `terms(DOMAIN)` 을 모듈 최상단에서 부르면, 프로필이 없는 설치에서
 * **그 모듈을 가져가는 모든 경로**가 함께 죽는다. 그러면 「원문 쓰기가 막혔습니다」
 * 같은 정확한 사유가 「도메인 프로필이 올바르지 않습니다」 로 덮이고, 고치는 사람은
 * 엉뚱한 데를 본다(2026-10-07 에 Archiver 설정에서 같은 일을 한 번 겪었다).
 *
 * 그렇다고 기본값으로 물러서지는 않는다. **표시 문구를 실제로 만들 때** 던진다 —
 * 그 자리에서 던지면 틀린 말투가 나갈 일이 없고, 사유도 정확하다.
 */
export function lazyTerms(getDomain) {
  const read = () => terms(typeof getDomain === 'function' ? getDomain() : getDomain);
  return Object.freeze({
    get place() { return read().place; },
    get area() { return read().area; },
    get examples() { return read().examples; },
  });
}
