#!/usr/bin/env python3
"""
sync_index.py 시험. **아카이브 사본에서만 돌고 저장소는 안 건드린다.**

  python .claude/skills/slack-sync/scripts/test_sync_index.py

종료코드: 0 전부 통과 / 1 실패 있음

── 왜 남겨 두는가 ──

이 스크립트가 고치는 것은 **봇 시스템 프롬프트에 통째로 실리는 파일**이다. 틀리게 고치면
에러가 안 나고 봇이 틀린 규모감으로 답한다. 그래서 안전망(불변식)이 실제로 발동하는지를
사람이 매번 눈으로 확인할 수는 없고, 확인을 코드로 남겨야 한다.

만든 계기가 그것이다 — 처음 붙인 불변식은 쓰기 전후를 **같은 파서**로 견주는 것뿐이라
파서가 행을 놓치면 양쪽 다 놓쳐 아무것도 안 걸렸다. 시험을 돌려 보고서야 알았고
(`_assert_partition` 은 그래서 생겼다), 그 시험을 버렸다면 다음 사람은 안전망이 있다고
믿었을 것이다. 아카이브 검사 스크립트를 두 번 쓰고 두 번 버린 전례가 있다.
"""

import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_index as S  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REAL = S.ARCHIVE
_ok = True


def check(label, cond, extra=""):
    global _ok
    _ok &= bool(cond)
    print(("  PASS " if cond else "  FAIL ") + label + (f"  {extra}" if extra else ""))


def main():
    if not (REAL / "index.md").exists():
        print(f"✗ 아카이브가 없습니다: {REAL}", file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="sync-index-test-"))
    try:
        shutil.copytree(REAL / "channels", tmp / "channels")
        shutil.copy(REAL / "index.md", tmp / "index.md")
        if (REAL / ".sync-state.json").exists():
            shutil.copy(REAL / ".sync-state.json", tmp / ".sync-state.json")
        S.CHANNELS, S.INDEX, S.STATE = tmp / "channels", tmp / "index.md", tmp / ".sync-state.json"

        # ── 1. 멱등 — 한 번 맞춘 뒤 다시 돌리면 변경 0 ──
        counts, ch, un = S.sync_channel_headers(dry=False)
        ic, iu = S.sync_index(counts, dry=False)
        print(f"1회차: 채널 {len(ch)} · index {len(ic)} · 못 고침 {len(un) + len(iu)}")
        _, ch2, _ = S.sync_channel_headers(dry=False)
        ic2, _ = S.sync_index(counts, dry=False)
        check("멱등 — 2회차 변경 0", not ch2 and not ic2, f"{len(ch2)}/{len(ic2)}")

        # ── 2. 틀린 값 하나를 넣으면 그것만 고친다 ──
        t = (tmp / "index.md").read_text(encoding="utf-8")
        row = re.search(r"^\|\s*\[([^\]]+)\]\(channels/([^)]+)\.md\)([^|]*)\|\s*(\d+)\s*\|",
                        t, re.M)
        name, cur = row.group(2), row.group(4)
        t2 = t.replace(row.group(0), row.group(0).replace(f"| {cur} |", "| 99999 |"), 1)
        (tmp / "index.md").write_text(t2, encoding="utf-8", newline="")
        ic3, _ = S.sync_index(counts, dry=False)
        check("틀린 값 1곳만 고침", len(ic3) == 1, ic3)
        check(f"{name} {cur} 로 복구", f"| {cur} |" in (tmp / "index.md").read_text(encoding="utf-8"))

        # ── 3. 불변식 — 버그가 있으면 아무것도 쓰지 않는다 ──
        lines, _ = S.read_lines(tmp / "index.md")
        lo, hi = S._section_bounds(lines)

        def broken(label, mutate):
            n = list(lines)
            mutate(n)
            try:
                S._check_invariants(lines, n, lo, hi)
                check(label, False, "예외가 안 났다")
            except S.Invariant as e:
                check(label, True, e)

        row_i = S._table_rows(lines, lo, hi)[0]
        lock_i = next((i for i in S._table_rows(lines, lo, hi) if "🔒" in lines[i]), None)
        # 행 식별자는 **링크가 가리키는 파일 이름**이다(표시 이름이 아니다 — 그쪽이 바뀌어도
        # 어느 채널의 행인지는 안 변한다). 그래서 파일 이름 쪽을 건드려 본다.
        broken("행이 다른 채널을 가리키게 되면 잡음",
               lambda n: n.__setitem__(row_i, n[row_i].replace(".md)", "X.md)", 1)))
        broken("줄이 늘면 잡음", lambda n: n.insert(row_i, "| 새로운행 | 0 | 멋대로 추가 |"))
        if lock_i is not None:
            broken("🔒 가 사라지면 잡음",
                   lambda n: n.__setitem__(lock_i, n[lock_i].replace("🔒", "")))

        # 파서가 행을 놓치는 버그 — 위 셋은 같은 파서로 견주므로 못 잡는다.
        # `_assert_partition` 이 잡아야 한다. 안 잡히면 소계·총계가 조용히 작아진 채 쓰인다.
        before = (tmp / "index.md").read_bytes()
        real_write, orig_rows = S.write_lines, S._table_rows
        S.write_lines = lambda *a, **k: (_ for _ in ()).throw(AssertionError("불변식을 뚫고 썼다"))
        S._table_rows = lambda l, a, b: orig_rows(l, a, b)[:-1]
        try:
            S.sync_index(counts, dry=False)
            check("파서 버그를 잡음", False, "예외가 안 났다")
        except S.Invariant as e:
            check("파서 버그를 잡음", True, e)
        except AssertionError as e:
            check("파서 버그를 잡음", False, e)
        finally:
            S._table_rows, S.write_lines = orig_rows, real_write
        check("파서 버그 — 파일 안 바뀜", (tmp / "index.md").read_bytes() == before)

        # ── 4. 채널 md 가 없어지면 broken_link 로 알리고 그 행은 안 고친다 ──
        gone = sorted((tmp / "channels").glob("*.md"))[0]
        gone_name = gone.stem
        gone.unlink()
        counts4, _, _ = S.sync_channel_headers(dry=True)
        _, iu4 = S.sync_index(counts4, dry=True)
        check("broken_link 로 보고",
              any(u["kind"] == "broken_link" and gone_name in u["detail"] for u in iu4)
              or gone_name not in t,
              [u for u in iu4 if u["kind"] == "broken_link"][:1])

        # ── 4.5 못 읽는 행이 있으면 총계 줄을 손대지 않는다 ──
        #
        # 왜: `n_rows` 에는 세는데 `n_lock` 에는 안 세서, 정규식이 못 읽은 행이 **공개로**
        # 잡힌다. 공개/비공개를 모르는 채로 총계를 쓰면 틀린 값이 박히고, 그 파일은 봇
        # 시스템 프롬프트에 통째로 실린다. 에러가 아니라 숫자만 조용히 틀린다.
        shutil.rmtree(tmp / "channels")
        shutil.copytree(REAL / "channels", tmp / "channels")
        shutil.copy(REAL / "index.md", tmp / "index.md")
        counts5, _, _ = S.sync_channel_headers(dry=False)
        S.sync_index(counts5, dry=False)          # 먼저 깨끗하게 맞춰 둔다

        # **총계를 일부러 틀리게 만든다.** 맞는 값이면 안 고치는 게 당연해서
        # 「보류했다」와 「고칠 게 없었다」가 구별이 안 된다 — 아무것도 재지 않는 시험이 된다.
        good = (tmp / "index.md").read_text(encoding="utf-8")
        tm5 = re.search(r"^(>\s*\*\*채널\*\*:\s*총\s*)(\d+)", good, re.M)
        (tmp / "index.md").write_text(
            good.replace(tm5.group(0), tm5.group(1) + "999", 1), encoding="utf-8", newline="")
        total_before = re.search(r"^>\s*\*\*채널\*\*.*$",
                                 (tmp / "index.md").read_text(encoding="utf-8"), re.M).group(0)

        # 표 안에 파서가 못 읽는 행을 하나 끼운다 (칸 수가 모자란 행).
        lines5, nl5 = S.read_lines(tmp / "index.md")
        lo5, hi5 = S._section_bounds(lines5)
        rows5 = S._parse_rows(lines5, lo5, hi5)
        at = rows5[0][0]
        lines5.insert(at, "| 어딘가 채널 | 뭔가 |")
        S.write_lines(tmp / "index.md", lines5, nl5)

        ic5, iu5 = S.sync_index(counts5, dry=False)
        after = (tmp / "index.md").read_text(encoding="utf-8")
        total_after = re.search(r"^>\s*\*\*채널\*\*.*$", after, re.M).group(0)

        check("못 읽는 행이 있으면 틀린 총계라도 안 고침",
              total_after == total_before and "999" in total_after,
              f"{total_before!r} → {total_after!r}")
        check("못 읽는 행을 unmatched 로 알림",
              any(u["kind"] == "unmatched" for u in iu5),
              [u["kind"] for u in iu5][:5])
        check("총계를 보류했다는 것도 알림",
              any(u["kind"] == "total_held" for u in iu5),
              [u["kind"] for u in iu5][:5])

        # 못 읽는 행이 없으면 전처럼 갱신된다 — 넓게 막지 않았다는 증거.
        shutil.copy(REAL / "index.md", tmp / "index.md")
        t6 = (tmp / "index.md").read_text(encoding="utf-8")
        tm6 = re.search(r"^(>\s*\*\*채널\*\*:\s*총\s*)(\d+)", t6, re.M)
        (tmp / "index.md").write_text(
            t6.replace(tm6.group(0), tm6.group(1) + "999", 1), encoding="utf-8", newline="")
        ic6, _ = S.sync_index(counts5, dry=False)
        check("못 읽는 행이 없으면 총계는 전처럼 갱신됨",
              any("총계" in c["what"] for c in ic6), [c["what"] for c in ic6][:3])

        # ── 5. 끝날짜가 없는 단일 날짜 채널의 기간을 안 건드린다 ──
        singles = [p for p in (tmp / "channels").glob("*.md")
                   if re.search(r"\*\*기간\*\*:\s*\d{4}-\d{2}-\d{2}\s*·",
                                p.read_text(encoding="utf-8"))]
        if singles:
            check("단일 날짜 기간 보존",
                  all(re.search(r"\*\*기간\*\*:\s*\d{4}-\d{2}-\d{2}\s*·",
                                p.read_text(encoding="utf-8")) for p in singles),
                  f"{len(singles)}개 채널")

        # ── 6. 백필이 채운 과거를 따라 기간 시작날짜를 내린다 (Task 7, 2026-09-02) ──
        # 브리프의 예시 그대로: 헤더는 `2026-08-11 ~ 2026-08-13` 인데 본문엔 6/30
        # 메시지가 있다 — 백필이 아래로 달을 채웠는데 시작날짜가 안 따라간 모양.
        # 실제 아카이브에는 이 결함이 없다(2026-09-02 실측 42채널 0건, 백필이 아직
        # 안 돌아서다) — 재현해서 만든다.
        drift = tmp / "channels" / "시험채널.md"
        drift.write_bytes((
            "> **채널**: #시험채널\n"
            "> **기간**: 2026-08-11 ~ 2026-08-13\n"
            "> **실제 메시지**: 1건\n"
            "---\n"
            "## 2026-06\n"
            "**2026-06-30 15:11 · 사람가**\n"
            "본문\n"
        ).encode("utf-8"))
        S.sync_channel_headers(dry=False)
        after = drift.read_text(encoding="utf-8")
        m_after = re.search(r"\*\*기간\*\*:.*", after)
        check("시작날짜가 가장 오래된 메시지(2026-06-30)로 내려감",
              "**기간**: 2026-06-30 ~ 2026-08-13" in after,
              m_after.group() if m_after else "기간 줄 없음")

        # 반대 방향(시작을 더 새것으로 올림)은 절대 하지 않는다 — 다시 돌려도 그대로.
        S.sync_channel_headers(dry=False)
        after2 = drift.read_text(encoding="utf-8")
        check("멱등 — 다시 돌려도 시작날짜 그대로", "**기간**: 2026-06-30 ~ 2026-08-13" in after2)

        # ── 7. 단일 날짜 채널 + 시작 당김 + 더 최신 메시지 — 한 회차에 수렴 (리뷰 Finding 1) ──
        # 리뷰가 재현한 버그: 헤더가 `2026-07-01` 단일 날짜인데 본문에 6/30(더 오래됨)과
        # 8/20(더 최신) 메시지가 함께 있으면, 예전 코드는 끝날짜 판정을 `m.group(2)`
        # (단일 날짜 채널엔 애초에 없음)로만 걸어서 8/20 을 못 보고 시작만 6/30 으로
        # 당겨 `2026-06-30 ~ 2026-07-01` 을 썼다 — 8/20 은 아직 없는 것처럼 남아 다음
        # 회차(최대 10시간 뒤)까지 봇이 그 뒤를 「없다」고 오답했다. 이제 한 회차에
        # 옳은 값이 나와야 하고, 그 다음 회차는 이 채널을 더 고칠 게 없어야 한다.
        single_drift = tmp / "channels" / "시험채널단일.md"
        single_drift.write_bytes((
            "> **채널**: #시험채널단일\n"
            "> **기간**: 2026-07-01\n"
            "> **실제 메시지**: 2건\n"
            "---\n"
            "## 2026-06\n"
            "**2026-06-30 15:11 · 사람가**\n"
            "본문1\n"
            "## 2026-08\n"
            "**2026-08-20 09:00 · 사람나**\n"
            "본문2\n"
        ).encode("utf-8"))
        S.sync_channel_headers(dry=False)
        after7 = single_drift.read_text(encoding="utf-8")
        m7 = re.search(r"\*\*기간\*\*:.*", after7)
        check("한 회차 만에 시작·끝이 모두 맞음 (끝이 반쪽으로 안 남음)",
              "**기간**: 2026-06-30 ~ 2026-08-20" in after7,
              m7.group() if m7 else "기간 줄 없음")

        # 두 번째 회차는 이 채널을 더 고칠 게 없어야 한다 — changed 에 없어야 수렴이다.
        _, ch7b, _ = S.sync_channel_headers(dry=False)
        check("2회차엔 이 채널이 changed 에 없음(수렴)",
              not any("시험채널단일" in c["where"] for c in ch7b),
              [c for c in ch7b if "시험채널단일" in c["where"]])

        # ── 8. 「개설일형」 핀 시험 — 비대칭이 살아 있는지 (리뷰 Finding 2) ──
        # 헤더 시작이 첫 메시지보다 이른 채널(채널 개설일 등)은 **절대 안 건드린다**는
        # 게 이 비대칭의 요점이다. 조건 부호 하나(`<` → `!=`)만 지워도 이 값이 위로
        # 밀리는데, 리뷰가 실제로 그렇게 바꿔 42채널 중 22채널의 시작날짜가 옮겨지는
        # 걸 확인했다 — 그때도 이 파일의 다른 시험은 전부 통과했다. 그 사고가 다시
        # 안 나게, 파일이 **바이트 단위로 그대로**인지를 직접 본다(문자열 비교가 아니라
        # 「아무것도 안 바뀌었나」를 보는 것이 핀 시험의 요점 — mutate.py 같은 변이
        # 검사 없이도 이 파일 하나로 비대칭이 살아 있는지 알 수 있어야 한다).
        opening = tmp / "channels" / "시험채널개설일.md"
        opening_bytes = (
            "> **채널**: #시험채널개설일\n"
            "> **기간**: 2026-05-01 ~ 2026-08-13\n"
            "> **실제 메시지**: 1건\n"
            "---\n"
            "## 2026-06\n"
            "**2026-06-30 15:11 · 사람가**\n"
            "본문\n"
        ).encode("utf-8")
        opening.write_bytes(opening_bytes)
        S.sync_channel_headers(dry=False)
        check("개설일형(시작이 첫 메시지보다 이름) — 파일이 바이트 그대로",
              opening.read_bytes() == opening_bytes)

        # ── 9. 자리표시 기간을 실제 날짜로 낫게 한다 (최종 검토 F3, 2026-09-02) ──
        # 백필 뼈대는 `**기간**: (백필 중)` 을 적고 `run-backfill.js` 의 finally 가 실제
        # 범위로 바꾼다. 그런데 그 finally 는 **Ctrl-C·강제 종료에는 안 돈다** — 42채널
        # 백필은 몇 시간짜리라 중단이 드문 일이 아니다. 그렇게 굳으면 `PERIOD_RE` 가
        # 날짜를 요구해 안 걸리므로 sync_index 도 verify_archive 도 손을 못 댔고, 그
        # 문자열이 `metaBlock` 을 타고 **봇 시스템 프롬프트에 사실로** 실렸다.
        # 여기서 낫게 하면 다음 07:00/17:00 회차가 저절로 고친다.
        for ph in ("(백필 중)", "(대화 없음)"):
            stuck = tmp / "channels" / "시험채널자리표시.md"
            stuck.write_bytes((
                "> **채널**: #시험채널자리표시\n"
                f"> **기간**: {ph}\n"
                "> **실제 메시지**: 2건\n"
                "---\n"
                "## 2026-08\n"
                "**2026-08-20 09:00 · 사람나**\n"
                "본문2\n"
                "## 2026-06\n"
                "**2026-06-30 15:11 · 사람가**\n"
                "본문1\n"
            ).encode("utf-8"))
            S.sync_channel_headers(dry=False)
            after9 = stuck.read_text(encoding="utf-8")
            m9 = re.search(r"\*\*기간\*\*:.*", after9)
            check(f"자리표시 {ph} 가 실제 범위로 바뀜",
                  "**기간**: 2026-06-30 ~ 2026-08-20" in after9,
                  m9.group() if m9 else "기간 줄 없음")
            # 낫고 나면 그 다음 회차는 더 고칠 게 없어야 한다(수렴).
            _, ch9b, _ = S.sync_channel_headers(dry=False)
            check(f"{ph} — 2회차엔 changed 에 없음(수렴)",
                  not any("시험채널자리표시" in c["where"] for c in ch9b),
                  [c for c in ch9b if "시험채널자리표시" in c["where"]])
            stuck.unlink()

        # ── 10. 진짜로 빈 채널의 `(대화 없음)` 은 **건드리지 않는다** ──
        # 이게 없으면 「고치기」가 「거짓 경보 만들기」가 된다 — 메시지가 하나도 없는
        # 채널에 `(대화 없음)` 은 **사실**이라 고칠 것이 없다. 위 9번이 그 구분을
        # 안 하고 무조건 손대면 여기서 빨개진다.
        empty = tmp / "channels" / "시험채널빈것.md"
        empty_bytes = (
            "> **채널**: #시험채널빈것\n"
            "> **기간**: (대화 없음)\n"
            "> **실제 메시지**: 0건\n"
            "---\n"
        ).encode("utf-8")
        empty.write_bytes(empty_bytes)
        S.sync_channel_headers(dry=False)
        check("메시지가 없는 채널의 (대화 없음) — 파일이 바이트 그대로",
              empty.read_bytes() == empty_bytes)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n결과:", "전부 통과" if _ok else "실패 있음")
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())
