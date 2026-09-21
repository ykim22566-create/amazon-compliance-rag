"""Policy Updates page – corpus freshness, detected changes, announcements.

Three panels, in the order a seller cares about them: what changed in the
docs, what Amazon said out loud, and when the corpus was last swept.

The middle panel is the one that earns its place. Across one recent window
only 4 of 293 changed pages were ever mentioned on the announcement board,
so for 99% of changes the generated summary is the only explanation the
seller will get.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[4]
RUNS_DIR = PROJECT_ROOT / "data" / "amazon" / "runs"
NEWS_DB = PROJECT_ROOT / "data" / "amazon" / "seller_news.sqlite3"
CHANGES_DB = PROJECT_ROOT / "data" / "amazon" / "policy_changes.sqlite3"
SUMMARY_DB = PROJECT_ROOT / "data" / "amazon" / "change_summaries.sqlite3"

SWEEP_CADENCE = "每周一 04:00"
NEWS_CADENCE = "每天 07:30"
NEW_BADGE_DAYS = 7


def _load_reports(prefix: str) -> List[Dict[str, Any]]:
    """Newest first. An unreadable report is skipped, not fatal."""
    if not RUNS_DIR.exists():
        return []
    out = []
    for path in sorted(RUNS_DIR.glob(f"{prefix}_*.json"), reverse=True):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _fmt(dt: datetime | None) -> str:
    return f"{dt:%Y-%m-%d %H:%M}" if dt else "—"


# ── Sweep ────────────────────────────────────────────────────────────

def _run_sweep(max_sources: int) -> None:
    """Run the sweep as a subprocess so a crash cannot take the UI with it."""
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "weekly_crawl.py"),
        "--max-sources",
        str(max_sources),
    ]
    with st.spinner(f"正在重扫 {max_sources} 个页面，约 {max_sources * 6 // 60 + 1} 分钟…"):
        proc = subprocess.run(
            cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=3600
        )
    if proc.returncode == 0:
        st.success("重扫完成")
        st.cache_data.clear()
    else:
        st.error(f"重扫失败（退出码 {proc.returncode}）")
        st.code((proc.stderr or proc.stdout)[-1500:])


def _render_sweep(runs: List[Dict[str, Any]]) -> None:
    st.subheader("Corpus sweep")
    st.caption(
        f"自动运行：{SWEEP_CADENCE}。每次重新渲染全部来源，比对归一化内容哈希，"
        "只有真正改动的页面才会重新切分、重嵌并删除旧版本。"
    )

    if not runs:
        st.info("尚无扫描记录。运行 `python scripts/weekly_crawl.py`，或点下方按钮。")
    else:
        latest = runs[0]
        finished = _parse(latest.get("finished_at"))
        started = _parse(latest.get("started_at"))
        if finished:
            age = (datetime.now(timezone.utc) - finished).days
            mins = (finished - started).total_seconds() / 60 if started else 0
            st.caption(f"最近一次：{_fmt(finished)} UTC（{age} 天前），耗时 {mins:.0f} 分钟")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("扫描页面", latest.get("total", 0))
        c2.metric("发现更新", latest.get("ingested", 0), help="内容哈希已变，旧版本已删除并重新入库")
        c3.metric("未变化", latest.get("unchanged", 0), help="哈希一致，跳过，不产生嵌入开销")
        c4.metric("抓取失败", latest.get("errors", 0), help="保留上一版语料，下次运行重试")

        failed = [o for o in latest.get("outcomes", []) if o.get("status") == "error"]
        if failed:
            with st.expander(f"⚠️ {len(failed)} 个页面抓取失败"):
                st.dataframe(
                    [{"source_id": o.get("source_id"), "错误": str(o.get("error"))[:120]} for o in failed],
                    hide_index=True,
                    use_container_width=True,
                )

        if len(runs) > 1:
            with st.expander(f"历史运行（{len(runs)} 次）"):
                st.dataframe(
                    [
                        {
                            "完成时间": _fmt(_parse(r.get("finished_at"))),
                            "扫描": r.get("total"),
                            "发现更新": r.get("ingested"),
                            "未变化": r.get("unchanged"),
                            "失败": r.get("errors"),
                        }
                        for r in runs
                    ],
                    hide_index=True,
                    use_container_width=True,
                )

    left, right = st.columns([1, 2])
    pages = left.selectbox("手动重扫页数", [20, 100, 300, 727], index=1, key="sweep_n")
    if right.button("🔄 立即重扫", key="btn_sweep"):
        _run_sweep(int(pages))


# ── Detected changes ─────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def _load_changes(limit: int = 400) -> tuple[List[Dict[str, Any]], int]:
    try:
        from src.amazon_compliance.change_summary import ChangeSummaryStore
        from src.amazon_compliance.policy_changes import PolicyChangeStore

        changes = PolicyChangeStore(CHANGES_DB)
        summaries = ChangeSummaryStore(SUMMARY_DB)
    except Exception:
        return [], 0

    total = changes.counts().get("total", 0)
    out = []
    for event in changes.list_events(limit=limit):
        summary = summaries.get(event.event_id) if event.event_id is not None else None
        out.append(
            {
                "event_id": event.event_id,
                "title": event.title,
                "source_url": event.source_url,
                "detected_at": event.detected_at.isoformat() if event.detected_at else None,
                "added": list(event.added_segments),
                "removed": list(event.removed_segments),
                "status": event.verification_status,
                "summary": summary.summary if summary else None,
            }
        )
    return out, total


def _render_changes() -> None:
    st.subheader("What changed")
    st.caption(
        "亚马逊几乎不为文档改动发公告 —— 近期一个窗口内 293 个变更页中只有 4 个上过公告板。"
        "以下摘要由模型对比改动前后原文生成，用于补上这个缺口。"
    )

    changes, total_events = _load_changes()
    if not changes:
        st.info("尚无变更记录。扫描产生新快照后运行 `python scripts/summarize_changes.py`。")
        return

    # Summaries are generated largest-diff-first, so surface those ahead of
    # the editorial one-liners regardless of when they were detected.
    summarized = [c for c in changes if c["summary"]]
    summarized.sort(key=lambda c: len(c["added"]) + len(c["removed"]), reverse=True)
    st.caption(
        f"共 {total_events} 条变更记录（已载入 {len(changes)} 条），"
        f"其中 {len(summarized)} 条已生成摘要"
    )

    only_summarized = st.toggle("只看已生成摘要的", value=True, key="chg_filter")
    shown = summarized if only_summarized else changes

    for item in shown[:25]:
        detected = _parse(item["detected_at"])
        st.markdown(f"**{item['title']}**")
        st.caption(
            f"{_fmt(detected)} · 新增 {len(item['added'])} 段 / 删除 {len(item['removed'])} 段"
            + (" · ⚠️ 待人工确认" if item["status"] == "review_required" else "")
        )
        if item["summary"]:
            st.info(item["summary"].replace("$", r"\$"))
        with st.expander("查看原文差异"):
            if item["added"]:
                st.markdown("**新增**")
                for seg in item["added"][:4]:
                    st.text(str(seg)[:600])
            if item["removed"]:
                st.markdown("**删除**")
                for seg in item["removed"][:4]:
                    st.text(str(seg)[:600])
        if item["source_url"]:
            st.caption(f"[查看官方原文]({item['source_url']})")
        st.divider()


# ── Announcements ────────────────────────────────────────────────────

def _render_news() -> None:
    st.subheader("Amazon announcements")
    st.caption(
        f"自动运行：{NEWS_CADENCE}。公告是前瞻的 —— 生效日期往往在数周后，"
        "但覆盖面很窄，无法替代全量重扫。"
    )

    try:
        from src.amazon_compliance.seller_news import SellerNewsStore

        items = SellerNewsStore(NEWS_DB).list_items(limit=20)
    except Exception as exc:
        st.warning(f"读取公告库失败：{exc}")
        return

    if not items:
        st.info("公告库为空。运行 `python scripts/check_announcements.py`。")
        return

    checks = _load_reports("news")
    if checks:
        checked = _parse(checks[0].get("checked_at"))
        if checked:
            st.caption(f"最近检查：{_fmt(checked)} UTC")

    cutoff = datetime.now(timezone.utc) - timedelta(days=NEW_BADGE_DAYS)
    for item in items[:10]:
        published = item.published_at
        is_new = published is not None and published >= cutoff
        linked = len(item.policy_source_ids)

        st.markdown(f"{'🔴 新 ' if is_new else ''}**{item.title}**")
        date_text = f"{published:%Y-%m-%d}" if published else "日期未知"
        kind_text = f"关联 {linked} 个政策页" if linked else "未关联政策页 · 活动/促销"
        st.caption(f"{date_text} · {kind_text}")
        if item.content:
            st.write(item.content.strip()[:260].replace("$", r"\$") + "…")
        st.caption(f"[查看原文]({item.source_url})")
        st.divider()


def render() -> None:
    st.header("🔄 Policy Updates")
    st.caption(
        "语料按周全量重扫（哈希比对，只重嵌真正改动的页面）；"
        "公告板每天检查，用于提前知道哪些规则即将生效。"
    )
    st.divider()
    _render_sweep(_load_reports("run"))
    st.divider()
    _render_changes()
    st.divider()
    _render_news()
