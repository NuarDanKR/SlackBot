"""조직 검색 — `/채널` 생성 모달에서 조직코드를 외우지 않게 한다.

## 왜
표준 채널명은 `#<구분>-<조직명>_<조직코드>-<업무>` 이고, 지금은 사람이 조직코드를
직접 입력한다. `ABB110` 같은 값을 외우고 있는 사람은 거의 없다. 잘못 적으면 채널명은
만들어지지만 조직 매핑이 어긋나고, 그건 나중에 권한·집계에서 조용히 드러난다.

조직도는 이미 `org_unit` 에 있다. "경영" 을 치면 후보를 보여주고 고르게 한다.

## 구분(본부/실/팀/현장)은 조직코드와 조직명에서 나온다
`org_unit.kind` 는 그룹웨어에 구분 컬럼이 없어 **추정한 값**이다(스키마 주석).
추정값으로 채널명을 짓지 않는다. 대신 그룹웨어가 실제로 강제하는 두 가지를 쓴다.

| 근거 | 규칙 |
|---|---|
| **조직코드** | 알파벳이 있으면 본사(`ABB110`), 숫자뿐이면 **현장**(`1800249`) |
| **조직명 끝** | 본사 안에서 `경영혁신실` → `실`, `경영본부` → `본부`, `전산팀` → `팀` |

코드가 이름보다 세다. 이름은 사람이 바꿀 수 있지만 코드 체계는 그룹웨어가 준다.
본사인데 이름 끝이 규칙에 없으면(`경영지원`) 비워 두고 사용자가 고르게 한다.

## 계열사 경계
`company_code` 가 다른 조직은 서로 보이면 안 된다(스키마 주석). 검색은 호출자가 준
`company_code` 로 제한할 수 있고, 지정하지 않으면 제한하지 않는다 — 파일럿 단계에서
값이 아직 안 채워진 조직이 있어서다. 채널 생성은 사람이 확인하고 누르는 동작이라
여기서 막지 않아도 즉시 유출로 이어지지는 않는다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .channels import COLLECT_PREFIXES

logger = logging.getLogger("tybot.orgsearch")

# Slack 제한: 옵션 텍스트 75자, value 75자, 한 번에 100개.
MAX_TEXT = 75
MAX_VALUE = 75
MAX_OPTIONS = 25  # 100 까지 되지만, 사람이 고를 수 있는 양으로 줄인다.

# 조직명 끝에서 구분을 뽑는다. 긴 것부터 본다("프로젝트" 가 "트" 보다 먼저).
_SUFFIXES = tuple(sorted(COLLECT_PREFIXES, key=len, reverse=True))


@dataclass(frozen=True)
class OrgHit:
    code: str
    name: str
    parent_name: str = ""

    @property
    def prefix(self) -> str:
        """구분. 조직코드와 조직명으로 정한다. 못 정하면 빈 문자열."""
        return derive_prefix(self.code, self.name)

    @property
    def base_name(self) -> str:
        """채널명에 들어갈 조직명.

        **구분으로 실제 쓴 접미사만** 뗀다. 현장 조직 `공무팀`(코드 180182)은 구분이
        코드에서 `현장` 으로 정해지므로 이름은 그대로 둔다 — `현장-공무팀_180182`.
        여기서 `팀` 을 떼면 `현장-공무` 가 되어 무슨 조직인지 알 수 없게 된다.
        """
        suffix, base = split_org_name(self.name)
        return base if suffix and suffix == self.prefix else self.name.strip()

    @property
    def is_site(self) -> bool:
        return is_site_code(self.code)


def is_site_code(code: str) -> bool:
    """조직코드로 현장을 가른다.

    본사 조직은 코드에 알파벳이 있다(`ABB110`). 현장은 숫자만이다(`1800249`).
    조직명보다 확실한 근거다 — 이름은 `김해외동` 처럼 접미사가 없는 경우가 있지만
    코드 체계는 그룹웨어가 강제한다.
    """
    c = (code or "").strip()
    return bool(c) and c.isdigit()


def derive_prefix(code: str, name: str) -> str:
    """(조직코드, 조직명) → 구분. 못 정하면 빈 문자열.

    순서가 중요하다:
    1. 이름이 `프로젝트` 로 끝나면 프로젝트다. 본사/현장 축과 다른 갈래라 코드로 갈리지 않는다.
    2. 코드가 숫자뿐이면 **현장**이다(그룹웨어 코드 체계).
    3. 그 외에는 본사 조직이므로 이름 끝에서 본부·실·팀을 뽑는다.
    4. 본사인데 이름 끝이 규칙에 없으면(`경영지원`) 비워 두고 사용자가 고르게 한다.

    본사 조직에 `현장` 이 붙거나 현장 조직에 `팀` 이 붙는 것을 막는 것이 요점이다.
    잘못 붙으면 채널명만으로 조직을 분류할 수 없게 된다.
    """
    suffix, _ = split_org_name(name)
    if suffix == "프로젝트":
        return suffix
    if is_site_code(code):
        return "현장"
    # 본사 조직에는 현장을 주지 않는다. 이름이 `...현장` 이어도 코드가 본사면
    # 코드 쪽을 믿는다 — 이름은 사람이 바꿀 수 있고 코드는 그룹웨어가 준다.
    return "" if suffix == "현장" else suffix


def split_org_name(name: str) -> tuple[str, str]:
    """`경영혁신실` → (`실`, `경영혁신`). 규칙에 없는 끝이면 (``, 원래이름).

    이름 전체가 구분어 하나뿐이면(`팀`) 자르지 않는다 — 남는 조직명이 없다.
    """
    n = (name or "").strip()
    for suffix in _SUFFIXES:
        if n.endswith(suffix) and len(n) > len(suffix):
            return suffix, n[: -len(suffix)].strip()
    return "", n


# 본인 소속 조직과 그 상위 사슬. 전산팀원이면 {전산팀, 경영본부, ...}.
#
# 왜 상위까지 — 팀원이 본부 단위 업무 채널을 만드는 일이 실제로 있다(본부 워크숍 등).
# 반대로 **하위·형제 조직은 넣지 않는다.** 내 팀이 아닌 팀의 채널을 내가 만들면
# 그 채널의 주인이 처음부터 어긋난다.
#
# depth 로 재귀를 끊는다. 스키마가 자기 자신을 부모로 두는 것만 막고 더 긴 순환은
# 막지 못한다. 순환이 생기면 이 조회가 영원히 도는 대신 20단계에서 멈춘다.
MY_ORGS_SQL = """
with recursive up as (
    select o.code, o.parent_code, 1 as depth
      from org_unit o
      join employee e on e.org_code = o.code and e.active
      join user_identity ui
        on ui.emp_no = e.emp_no
       and ui.workspace = %(workspace)s
       and ui.slack_user = %(slack_user)s
     where o.active
    union all
    select p.code, p.parent_code, up.depth + 1
      from org_unit p
      join up on p.code = up.parent_code
     where p.active and up.depth < 20
)
select distinct code from up
"""

SEARCH_SQL = """
select o.code, o.name, coalesce(p.name, '') as parent_name
  from org_unit o
  left join org_unit p on p.code = o.parent_code
 where o.active
   and (%(company)s is null or o.company_code is null or o.company_code = %(company)s)
   and (%(only)s::text[] is null or o.code = any(%(only)s))
   and (o.name ilike %(like)s or o.code ilike %(prefix)s)
 order by
   -- 이름이 검색어로 시작하는 것을 먼저. "경영" 을 치면 "경영본부" 가
   -- "정보경영팀" 보다 위에 와야 사람이 기대한 순서가 된다.
   case when o.name ilike %(prefix)s then 0 else 1 end,
   length(o.name),
   o.name
 limit %(limit)s
"""


def my_org_codes(conn, *, workspace: str, slack_user: str) -> list[str] | None:
    """본인 소속 조직과 상위 사슬의 코드. 신원 매핑이 없으면 `None`.

    `None` 과 `[]` 는 다르다 — `None` 은 "누구인지 모른다"(제한 판단 불가),
    `[]` 는 "알지만 소속 조직이 없다". 호출자가 그 둘을 다르게 다뤄야 한다.
    """
    if not workspace or not slack_user:
        return None
    with conn.cursor() as cur:
        cur.execute(MY_ORGS_SQL, {"workspace": workspace, "slack_user": slack_user})
        rows = cur.fetchall()
    if not rows:
        return None
    codes = [
        str(r["code"] if isinstance(r, dict) else r[0])
        for r in rows
    ]
    return sorted({c for c in codes if c})


def search(
    conn,
    query: str,
    *,
    limit: int = MAX_OPTIONS,
    company_code: str | None = None,
    only_codes: list[str] | None = None,
) -> list[OrgHit]:
    """조직명·조직코드로 찾는다. 비활성 조직은 제외한다.

    폐지된 조직으로 채널을 만들면 그 채널은 처음부터 잘못된 조직에 매달린다.

    `only_codes` 를 주면 그 코드만 돌려준다(본인 소속 제한). `None` 이면 제한하지 않는다.
    """
    q = (query or "").strip()
    if not q:
        return []
    if only_codes is not None and not only_codes:
        return []
    params = {
        "like": f"%{q}%",
        "prefix": f"{q}%",
        "limit": max(1, min(limit, MAX_OPTIONS)),
        "company": company_code,
        "only": list(only_codes) if only_codes is not None else None,
    }
    with conn.cursor() as cur:
        cur.execute(SEARCH_SQL, params)
        rows = cur.fetchall()

    out: list[OrgHit] = []
    for raw in rows:
        r = raw if isinstance(raw, dict) else dict(
            zip(("code", "name", "parent_name"), raw, strict=False)
        )
        out.append(
            OrgHit(
                code=str(r["code"]),
                name=str(r["name"] or ""),
                parent_name=str(r.get("parent_name") or ""),
            )
        )
    return out


def option(hit: OrgHit) -> dict:
    """Slack 옵션 하나.

    상위 조직을 함께 보여 같은 이름을 구분하고, 정해진 구분(본부/실/팀/현장)을 덧붙인다.
    고르기 전에 어떤 채널명이 될지 알 수 있어야 한다.
    """
    label = f"{hit.name} · {hit.code}"
    if hit.parent_name:
        label = f"{hit.name} ({hit.parent_name}) · {hit.code}"
    if hit.prefix:
        label = f"[{hit.prefix}] {label}"
    return {
        "text": {"type": "plain_text", "text": label[:MAX_TEXT]},
        # 제출 시 DB 를 다시 조회하지 않으려고 필요한 것만 담는다.
        # 모달이 열린 채로 DB 가 잠깐 끊겨도 채널명을 만들 수 있어야 한다.
        "value": encode_value(hit)[:MAX_VALUE],
    }


def options(hits: list[OrgHit]) -> list[dict]:
    return [option(h) for h in hits[:MAX_OPTIONS]]


SEP = "|"


def encode_value(hit: OrgHit) -> str:
    """`ABB123|실|경영혁신`. 코드가 첫 칸이라 잘려도 코드는 남는다."""
    return SEP.join((hit.code, hit.prefix, hit.base_name))


def decode_value(value: str) -> tuple[str, str, str]:
    """옵션 value → (코드, 구분, 조직명). 형식이 달라도 코드는 건진다."""
    parts = (value or "").split(SEP)
    code = parts[0].strip() if parts else ""
    prefix = parts[1].strip() if len(parts) > 1 else ""
    name = parts[2].strip() if len(parts) > 2 else ""
    if prefix not in COLLECT_PREFIXES:
        prefix = ""
    return code, prefix, name


NO_MATCH = "일치하는 조직이 없습니다"
UNAVAILABLE = "조직도를 조회할 수 없습니다"


def notice_option(text: str) -> dict:
    """결과가 없을 때 보여줄 안내. 빈 목록만 돌려주면 사람은 고장으로 오해한다.

    고를 수는 있지만 코드가 비어 제출 단계에서 막힌다 — 그때 이유를 다시 말해 준다.
    """
    return {"text": {"type": "plain_text", "text": text[:MAX_TEXT]}, "value": ""}

NOT_MY_ORG = "소속 조직만 만들 수 있습니다"
NO_IDENTITY = "계정 연결이 없어 소속을 확인할 수 없습니다"


def not_my_org_message(name: str, code: str) -> str:
    """소속 밖 조직으로 제출했을 때 모달에 띄울 문구."""
    return (
        f"{name or code} 은(는) 회원님 소속 조직이 아닙니다. "
        "본인이 속한 조직과 그 상위 조직의 채널만 만들 수 있습니다. "
        "다른 조직의 채널이 필요하면 그 조직 담당자나 관리자에게 요청하세요."
    )
