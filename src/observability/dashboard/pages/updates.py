"""Updates page – corpus freshness and Amazon's announcement board.

Two independent signals, shown side by side because they answer different
questions: the weekly sweep says what actually moved in the docs, the
announcement board says what Amazon chose to tell sellers about (which is
forward-looking, and only partly overlaps).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[4]
RUNS_DIR = PROJECT_ROOT / "data" / "amazon" / "runs"
NEWS_DB = PROJECT_ROOT / "data" / "amazon" / "seller_news.sqlite3"

NEW_BADGE_DAYS = 7


def _load_runs(prefix: str) -> List[Dict[str, Any]]:
    """Newest first. Unreadable reports are skipped rather than fatal."""
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


def _render_crawl(runs: List[Dict[str, Any]]) -> None:
    st.subheader("📚 语料更新")
    if not runs:
        st.info(
            "尚无全量更新记录。运行 `python scripts/weekly_crawl.py` 后，"
            "结果会出现在这里。"
        )
        return

    latest = runs[0]
    started, finished = _parse(latest.get("started_at")), _parse(latest.get("finished_at"))
    if started and finished:
        mins = (finished - started).total_seconds() / 60
        age = (datetime.now(timezone.utc) - finished).days
        st.caption(
            f"最近一次：{finished:%Y-%m-%d %H:%M} UTC（{age} 天前），耗时 {mins:.0f} 分钟"
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("扫描页面", latest.get("total", 0))
    c2.metric("有变化", latest.get("ingested", 0), help="内容哈希变了，已重新切分并重嵌")
    c3.metric("未变化", latest.get("unchanged", 0), help="哈希一致，直接跳过，不产生嵌入开销")
    c4.metric("抓取失败", latest.get("errors", 0))

    changed = [o for o in latest.get("outcomes", []) if o.get("status") == "ingested"]
    if changed:
        with st.expander(f"本次变化的 {len(changed)} 个页面", expanded=False):
            st.dataframe(
                [
                    {
                        "source_id": o.get("source_id"),
                        "chunks": o.get("chunks"),
                        "表格行": o.get("rows"),
                        "链接": o.get("url"),
                    }
                    for o in changed
                ],
                hide_index=True,
                use_container_width=True,
            )

    failed = [o for o in latest.get("outcomes", []) if o.get("status") == "error"]
    if failed:
        with st.expander(f"⚠️ 抓取失败的 {len(failed)} 个页面", expanded=False):
            st.caption("失败的页面保留上一版语料，不会被清空；下次运行会重试。")
            st.dataframe(
                [{"source_id": o.get("source_id"), "错误": str(o.get("error"))[:120]} for o in failed],
                hide_index=True,
                use_container_width=True,
            )

    if len(runs) > 1:
        with st.expander("历史运行", expanded=False):
            st.dataframe(
                [
                    {
                        "完成时间": (_parse(r.get("finished_at")) or "").__format__("%Y-%m-%d %H:%M")
                        if _parse(r.get("finished_at"))
                        else "?",
                        "扫描": r.get("total"),
                        "有变化": r.get("ingested"),
                        "未变化": r.get("unchanged"),
                        "失败": r.get("errors"),
                    }
                    for r in runs
                ],
                hide_index=True,
                use_container_width=True,
            )


def _render_news() -> None:
    st.subheader("📣 亚马逊官方公告")

    try:
        from src.amazon_compliance.seller_news import SellerNewsStore

        items = SellerNewsStore(NEWS_DB).list_items(limit=20)
    except Exception as exc:
        st.warning(f"读取公告库失败：{exc}")
        return

    if not items:
        st.info("公告库为空。运行 `python scripts/check_announcements.py` 拉取。")
        return

    checks = _load_runs("news")
    if checks:
        checked = _parse(checks[0].get("checked_at"))
        if checked:
            st.caption(f"最近检查：{checked:%Y-%m-%d %H:%M} UTC")

    cutoff = datetime.now(timezone.utc) - timedelta(days=NEW_BADGE_DAYS)
    for item in items:
        published = item.published_at
        is_new = published is not None and published >= cutoff
        linked = len(item.policy_source_ids)

        badge = "🔴 新 " if is_new else ""
        kind = f"关联 {linked} 个政策页" if linked else "未关联政策页 · 活动/促销"
        date = f"{published:%Y-%m-%d}" if published else "日期未知"

        st.markdown(f"{badge}**{item.title}**")
        st.caption(f"{date} · {kind}")
        if item.content:
            st.write(item.content.strip()[:300].replace("$", r"\$") + "…")
        st.caption(f"[查看原文]({item.source_url})")
        st.divider()


def render() -> None:
    st.header("🔄 更新与公告")
    st.caption(
        "语料按周全量重扫，用渲染后的内容哈希比对，只有真正改动的页面才会重嵌；"
        "公告板每天检查，用于提前知道哪些规则即将生效。"
    )
    st.divider()

    # Stacked rather than side by side: halving the width truncates the
    # metric labels and squeezes the announcement bodies to a few words a line.
    _render_crawl(_load_runs("run"))
    st.divider()
    _render_news()
