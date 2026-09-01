"""소속 조직 조회 — `/채널` 생성 모달의 조직명·조직코드를 미리 채운다.

## 왜
표준 채널명은 `#<구분>-<조직명>_<조직코드>-<업무>` 다. `ABB110` 같은 코드를 외우고 있는
사람은 거의 없다. 잘못 적으면 채널명은 만들어지지만 조직 매핑이 어긋나고, 그건 나중에
권한·집계에서 조용히 드러난다.

조직도는 이미 `org_unit` 에 있다. **구분을 고르면 그 층의 내 조직이 자동으로 채워진다** —
`본사팀` 을 고르면 내 팀, `본부` 를 고르면 내 본부.

## 강제하지는 않는다
채워진 값은 **그냥 기본값**이다. 사람이 조직명·조직코드를 고쳐 다른 조직의 채널을 만들 수
있다. 타 조직과 협업하는 채널은 실제로 필요하고, 채널 생성은 사람이 확인하고 누르는
동작이라 여기서 막을 이유가 없다.

## 구분(본부/실/본사팀/현장)은 조직코드와 조직명에서 나온다
`org_unit.kind` 는 그룹웨어에 구분 컬럼이 없어 **추정한 값**이다(스키마 주석).
추정값으로 채널명을 짓지 않는다. 대신 그룹웨어가 실제로 강제하는 두 가지를 쓴다.

| 근거 | 규칙 |
|---|---|
| **조직코드** | 알파벳이 있으면 본사(`ABB110`), 숫자뿐이면 **현장**(`1800249`) |
| **조직명 끝** | 본사 안에서 `경영혁신실` → `실`, `경영본부` → `본부`, `전산팀` → `본사팀` |

코드가 이름보다 세다. 이름은 사람이 바꿀 수 있지만 코드 체계는 그룹웨어가 준다.

## 업무는 조직이 아니다
`업무` 는 다른 팀과 협업하는 채널이라 조직도에 없다. 주관 팀의 코드를 빌리므로
기본값은 내 팀과 같고, 조직명만 협업 이름으로 바꿔 쓰면 된다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .channels import COLLECT_PREFIXES

logger = logging.getLogger("tybot.orgsearch")

MAX_OPTIONS = 100
MAX_TEXT = 75
MAX_VALUE = 150

# 조직명 끝에 실제로 붙는 말 → 채널 두문자.
# `전산팀` 의 끝은 `팀` 이지만 채널 두문자는 `본사팀` 이다(현장과 구별하려고).
SUFFIX_TO_PREFIX: dict[str, str] = {
    "본부": "본부",
    "실": "실",
    "팀": "본사팀",
    "현장": "현장",
    "프로젝트": "업무",
}
# 긴 것부터 본다("프로젝트" 가 "트" 보다 먼저).
_SUFFIXES = tuple(sorted(SUFFIX_TO_PREFIX, key=len, reverse=True))


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
        used = SUFFIX_TO_PREFIX.get(suffix, "")
        return base if suffix and used == self.prefix else self.name.strip()

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
    """(조직코드, 조직명) → 채널 두문자. 못 정하면 빈 문자열.

    1. 코드가 숫자뿐이면 **현장**이다(그룹웨어 코드 체계).
    2. 그 외에는 본사 조직이므로 이름 끝에서 본부·실·본사팀을 뽑는다.
    3. 본사인데 이름 끝이 규칙에 없으면(`경영지원`) 비워 두고 사용자가 고르게 한다.

    본사 조직에 `현장` 이 붙거나 현장 조직에 `본사팀` 이 붙는 것을 막는 것이 요점이다.
    잘못 붙으면 채널명만으로 조직을 분류할 수 없게 된다.
    """
    if is_site_code(code):
        return "현장"
    suffix, _ = split_org_name(name)
    mapped = SUFFIX_TO_PREFIX.get(suffix, "")
    # 본사 조직에는 현장을 주지 않는다. 이름이 `...현장` 이어도 코드가 본사면
    # 코드 쪽을 믿는다 — 이름은 사람이 바꿀 수 있고 코드는 그룹웨어가 준다.
    return "" if mapped == "현장" else mapped


def split_org_name(name: str) -> tuple[str, str]:
    """`경영혁신실` → (`실`, `경영혁신`). 규칙에 없는 끝이면 (``, 원래이름).

    이름 전체가 접미사 하나뿐이면(`팀`) 자르지 않는다 — 남는 조직명이 없다.
    """
    n = (name or "").strip()
    for suffix in _SUFFIXES:
        if n.endswith(suffix) and len(n) > len(suffix):
            return suffix, n[: -len(suffix)].strip()
    return "", n


# 본인 소속 조직과 그 상위 사슬. 전산팀원이면 전산팀 → 경영본부 → ... 순으로 올라간다.
#
# depth 로 재귀를 끊는다. 스키마가 자기 자신을 부모로 두는 것만 막고 더 긴 순환은
# 막지 못한다. 순환이 생기면 이 조회가 영원히 도는 대신 20단계에서 멈춘다.
MY_ORGS_SQL = """
with recursive up as (
    select o.code, o.name, o.parent_code, 1 as depth
      from org_unit o
      join employee e on e.org_code = o.code and e.active
      join user_identity ui
        on ui.emp_no = e.emp_no
       and ui.workspace = %(workspace)s
       and ui.slack_user = %(slack_user)s
     where o.active
    union all
    select p.code, p.name, p.parent_code, up.depth + 1
      from org_unit p
      join up on p.code = up.parent_code
     where p.active and up.depth < 20
)
select code, name, depth from up order by depth
"""


def my_org_chain(conn, *, workspace: str, slack_user: str) -> list[OrgHit]:
    """본인 소속 조직부터 위로 올라간 사슬. 신원 매핑이 없으면 빈 목록."""
    if not workspace or not slack_user:
        return []
    with conn.cursor() as cur:
        cur.execute(MY_ORGS_SQL, {"workspace": workspace, "slack_user": slack_user})
        rows = cur.fetchall()
    out: list[OrgHit] = []
    for raw in rows:
        r = raw if isinstance(raw, dict) else dict(
            zip(("code", "name", "depth"), raw, strict=False)
        )
        out.append(OrgHit(code=str(r["code"]), name=str(r["name"] or "")))
    return out


def defaults_by_prefix(chain: list[OrgHit]) -> dict[str, OrgHit]:
    """구분 → 그 층의 내 조직. 구분을 고르면 이 값으로 칸을 채운다.

    사슬에서 **가장 가까운 것**을 쓴다(본부가 둘이면 내 쪽 본부). `업무` 는 조직이
    아니라 협업 채널이므로 주관 팀, 즉 내 팀의 코드를 빌린다.
    """
    out: dict[str, OrgHit] = {}
    for hit in chain:  # depth 순 = 가까운 것부터
        p = hit.prefix
        if p and p not in out:
            out[p] = hit
    team = out.get("본사팀") or out.get("현장")
    if team and "업무" not in out:
        out["업무"] = team
    return out


NO_CHAIN = (
    "계정 연결이 없어 소속 조직을 자동 선택하지 못했습니다. 조직명을 검색해 선택해 주세요."
)


SEARCH_SQL = """
select o.code, o.name, coalesce(p.name, '') as parent_name
  from org_unit o
  left join org_unit p on p.code = o.parent_code
 where o.active
   and (o.name ilike %(like)s or o.code ilike %(prefix)s)
 order by
   case when o.name ilike %(prefix)s then 0 else 1 end,
   length(o.name),
   o.name
 limit %(limit)s
"""


def search(conn, query: str, *, limit: int = MAX_OPTIONS) -> list[OrgHit]:
    """활성 조직을 조직명·코드로 검색한다."""
    q = (query or "").strip()
    if not q:
        return []
    with conn.cursor() as cur:
        cur.execute(SEARCH_SQL, {
            "like": f"%{q}%",
            "prefix": f"{q}%",
            "limit": max(1, min(limit, MAX_OPTIONS)),
        })
        rows = cur.fetchall()
    out: list[OrgHit] = []
    for raw in rows:
        row = raw if isinstance(raw, dict) else dict(
            zip(("code", "name", "parent_name"), raw, strict=False)
        )
        out.append(OrgHit(
            code=str(row["code"]),
            name=str(row["name"] or ""),
            parent_name=str(row.get("parent_name") or ""),
        ))
    return out


_VALUE_SEP = "|"


def encode_value(hit: OrgHit) -> str:
    return _VALUE_SEP.join((hit.code, hit.prefix, hit.base_name))


def decode_value(value: str) -> tuple[str, str, str]:
    """검색 선택값을 (조직코드, 구분, 채널용 조직명)으로 푼다."""
    parts = (value or "").split(_VALUE_SEP)
    code = parts[0].strip() if parts else ""
    prefix = parts[1].strip() if len(parts) > 1 else ""
    name = parts[2].strip() if len(parts) > 2 else ""
    if prefix not in COLLECT_PREFIXES:
        prefix = ""
    return code, prefix, name


def option(hit: OrgHit) -> dict:
    label = f"{hit.name} · {hit.code}"
    if hit.parent_name:
        label = f"{hit.name} ({hit.parent_name}) · {hit.code}"
    return {
        "text": {"type": "plain_text", "text": label[:MAX_TEXT]},
        "value": encode_value(hit)[:MAX_VALUE],
    }


def options(hits: list[OrgHit]) -> list[dict]:
    return [option(hit) for hit in hits[:MAX_OPTIONS]]


def notice_option(text: str) -> dict:
    return {"text": {"type": "plain_text", "text": text[:MAX_TEXT]}, "value": ""}


NO_MATCH = "선택한 구분에 맞는 조직이 없습니다"
UNAVAILABLE = "조직도를 조회할 수 없습니다"
