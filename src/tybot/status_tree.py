"""수집 현황을 워크스페이스 → 채널 트리로 정리한다.

## 왜
`상태` 답변이 채널을 워크스페이스 구분 없이 평평하게 나열했다. 경영본부처럼 여러
워크스페이스를 읽는 상위 봇에게는 그 목록이 쓸모가 없다 — 어느 조직의 채널인지 알 수
없고, 워크스페이스별 수집이 고르게 되는지도 보이지 않는다. 알고 싶은 것은
"어디가 얼마나 쌓였나" 이고 그건 트리다.

## 권한
`visible` 밖 워크스페이스는 **아예 넣지 않는다.** 예전 코드는 아카이브 전체 문서를
나열해서, 자기 워크스페이스만 볼 수 있는 봇도 다른 워크스페이스의 채널명을 보여줬다.
채널명에는 조직·업무가 들어 있어 그 자체가 노출이다(원칙 3).
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 워크스페이스마다 이만큼만 보여주고 나머지는 건수로 접는다.
# 상태 답변은 Slack 메시지 하나에 들어가야 한다.
MAX_CHANNELS = 8


@dataclass
class ChannelStat:
    channel: str
    lines: int
    last_ingested: str = ""


@dataclass
class WorkspaceNode:
    key: str
    label: str
    is_self: bool = False
    channels: list[ChannelStat] = field(default_factory=list)

    @property
    def docs(self) -> int:
        return len(self.channels)

    @property
    def lines(self) -> int:
        return sum(c.lines for c in self.channels)


def build_tree(
    docs,
    *,
    visible: set[str] | frozenset[str],
    labels: dict[str, str] | None = None,
    own: str = "",
) -> list[WorkspaceNode]:
    """문서 목록 → 워크스페이스 노드. `visible` 밖은 버린다.

    자기 워크스페이스를 맨 앞에 둔다. 그다음은 문서가 많은 순 — 어디가 활발한지
    한눈에 보이는 것이 목적이다.
    """
    labels = labels or {}
    nodes: dict[str, WorkspaceNode] = {}
    for doc in docs:
        ws = str(getattr(doc, "workspace", "") or "")
        if ws not in visible:
            continue
        node = nodes.get(ws)
        if node is None:
            node = WorkspaceNode(
                key=ws, label=labels.get(ws, ws), is_self=(ws == own)
            )
            nodes[ws] = node
        node.channels.append(
            ChannelStat(
                channel=str(getattr(doc, "channel", "") or "?"),
                lines=len(getattr(doc, "raw_lines", []) or []),
                last_ingested=str(getattr(doc, "last_ingested", "") or ""),
            )
        )

    # 볼 수 있지만 아직 아무것도 없는 워크스페이스도 보여준다.
    # 빠뜨리면 "연결은 됐는데 수집이 0" 인 상태를 사람이 알아채지 못한다.
    for ws in visible:
        if ws not in nodes:
            nodes[ws] = WorkspaceNode(
                key=ws, label=labels.get(ws, ws), is_self=(ws == own)
            )

    for node in nodes.values():
        node.channels.sort(key=lambda c: (-c.lines, c.channel))
    return sorted(
        nodes.values(), key=lambda n: (not n.is_self, -n.lines, n.key)
    )


def _stamp(value: str) -> str:
    """`2026-08-27T18:02+09:00` → `08-27 18:02`. 상태 답변에 초·시간대는 군더더기다."""
    v = (value or "").strip()
    if len(v) >= 16 and v[10] == "T":
        return f"{v[5:10]} {v[11:16]}"
    return v or "-"


def render_tree(nodes: list[WorkspaceNode], *, max_channels: int = MAX_CHANNELS) -> list[str]:
    """트리를 Slack 줄 목록으로. 호출자가 다른 줄들과 합친다."""
    if not nodes:
        return ["ℹ️ 볼 수 있는 워크스페이스가 없습니다."]

    lines: list[str] = []
    for node in nodes:
        mark = " (이 워크스페이스)" if node.is_self else ""
        head = f"*{node.label}* (`{node.key}`){mark}"
        if node.channels:
            head += f" — 문서 {node.docs}건 · 원문 {node.lines}줄"
        else:
            head += " — 수집된 원문 없음"
        lines.append(head)
        for stat in node.channels[:max_channels]:
            lines.append(
                f"    • {stat.channel} — {stat.lines}줄 (최근 {_stamp(stat.last_ingested)})"
            )
        rest = node.docs - max_channels
        if rest > 0:
            lines.append(f"    … 그 외 {rest}개 채널")
    return lines


def totals(nodes: list[WorkspaceNode]) -> dict:
    """LLM 에 넘길 사실. 문장을 지어낼 여지를 주지 않으려고 숫자만 담는다."""
    return {
        "워크스페이스수": len(nodes),
        "문서수": sum(n.docs for n in nodes),
        "원문줄수": sum(n.lines for n in nodes),
        "워크스페이스별": [
            {
                "이름": n.label,
                "키": n.key,
                "본인": n.is_self,
                "문서수": n.docs,
                "원문줄수": n.lines,
            }
            for n in nodes
        ],
    }
