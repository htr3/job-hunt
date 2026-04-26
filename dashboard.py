"""Streamlit read-only status board for the Job Hunt AI Agent.

Launch with:  streamlit run dashboard.py

Shows:
- Stats cards (total / new 24h / applied / interview / source platforms)
- Platform + status charts
- Filter-able jobs table
- Kanban-by-status view
- Recent runs table

Read-only: use the Flask dashboard (`python web_app.py`) for status edits
and triggering new runs.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from job_db import JobDatabase, resolve_db_path

st.set_page_config(
    page_title="Job Hunt Agent · Dashboard",
    page_icon=":briefcase:",
    layout="wide",
)


@st.cache_resource
def _db() -> JobDatabase:
    return JobDatabase(db_path=resolve_db_path())


@st.cache_data(ttl=30)
def _load_jobs(_: int = 0) -> pd.DataFrame:
    db = _db()
    rows = db.search_jobs(limit=2000)
    if not rows:
        return pd.DataFrame(
            columns=[
                "match_score", "title", "company", "location", "platform",
                "status", "salary", "experience", "posted_date", "first_seen_at", "url",
            ]
        )
    df = pd.DataFrame(rows)
    return df


@st.cache_data(ttl=30)
def _load_stats(_: int = 0) -> dict[str, Any]:
    return _db().get_stats()


@st.cache_data(ttl=30)
def _load_runs(_: int = 0) -> pd.DataFrame:
    runs = _db().recent_runs(limit=50)
    return pd.DataFrame(runs) if runs else pd.DataFrame(
        columns=["run_at", "platforms", "total_scraped", "new_jobs", "duration_seconds"]
    )


# --- Sidebar ---------------------------------------------------------------

st.sidebar.title("Filters")
if st.sidebar.button("Refresh"):
    _load_jobs.clear(); _load_stats.clear(); _load_runs.clear()

df = _load_jobs()
stats = _load_stats()
runs_df = _load_runs()

platforms = sorted(df["platform"].dropna().unique().tolist()) if not df.empty else []
statuses = sorted(df["status"].dropna().unique().tolist()) if not df.empty else []

sel_platforms = st.sidebar.multiselect("Platform", platforms, default=platforms)
sel_statuses = st.sidebar.multiselect("Status", statuses, default=statuses)
min_score = st.sidebar.slider("Min match score", 0, 100, 0)
query = st.sidebar.text_input("Search (title / company / skills)", "")


def _apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df
    if sel_platforms:
        out = out[out["platform"].isin(sel_platforms)]
    if sel_statuses:
        out = out[out["status"].isin(sel_statuses)]
    if min_score > 0:
        out = out[out["match_score"].fillna(0) >= min_score]
    if query:
        q = query.lower()
        mask = (
            out["title"].fillna("").str.lower().str.contains(q)
            | out["company"].fillna("").str.lower().str.contains(q)
            | out["skills"].fillna("").str.lower().str.contains(q)
        )
        out = out[mask]
    return out


filtered = _apply_filters(df)

# --- Header / stats --------------------------------------------------------

st.title("Job Hunt AI Agent")
st.caption("Read-only status board · use the Flask app for edits")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total jobs", stats.get("total_jobs", 0))
c2.metric("New (24h)", stats.get("new_last_24h", 0))
c3.metric("Applied", (stats.get("by_status") or {}).get("applied", 0))
c4.metric("Interviews", (stats.get("by_status") or {}).get("interview", 0))
c5.metric("Platforms in DB", len(stats.get("by_platform", {}) or {}))

# --- Charts ----------------------------------------------------------------

if not df.empty:
    cc1, cc2 = st.columns(2)
    with cc1:
        st.subheader("By platform")
        by_platform = df["platform"].fillna("").value_counts()
        st.bar_chart(by_platform)
    with cc2:
        st.subheader("By status")
        by_status = df["status"].fillna("").value_counts()
        st.bar_chart(by_status)

# --- Jobs table ------------------------------------------------------------

st.subheader(f"Jobs ({len(filtered)} of {len(df)})")
if filtered.empty:
    st.info("No jobs match the current filters. Run the agent or loosen filters.")
else:
    show = filtered.copy()
    if "url" in show.columns:
        show["link"] = show["url"]
    cols = [c for c in (
        "match_score", "status", "title", "company", "location", "platform",
        "salary", "experience", "posted_date", "first_seen_at", "link",
    ) if c in show.columns]
    st.dataframe(
        show[cols].sort_values(
            ["match_score", "first_seen_at"], ascending=[False, False]
        ),
        width="stretch",
        hide_index=True,
        column_config={
            "match_score": st.column_config.NumberColumn("score", format="%.0f"),
            "link": st.column_config.LinkColumn("link", display_text="open"),
        } if hasattr(st, "column_config") else None,
    )

# --- Kanban ----------------------------------------------------------------

st.subheader("Kanban")
kanban_statuses = ["new", "saved", "applied", "interview", "offer", "rejected", "hidden"]
kcols = st.columns(len(kanban_statuses))
for kcol, s in zip(kcols, kanban_statuses):
    with kcol:
        subset = filtered[filtered["status"] == s] if not filtered.empty else filtered
        st.markdown(f"**{s}**  \n`{len(subset)}`")
        if subset.empty:
            st.caption("empty")
        else:
            for _, row in subset.head(15).iterrows():
                title = row.get("title") or ""
                company = row.get("company") or ""
                url = row.get("url") or ""
                score = float(row.get("match_score") or 0)
                with st.container(border=True):
                    st.write(f"**{title[:60]}**")
                    st.caption(f"{company} · score {score:.0f}")
                    if url:
                        st.markdown(f"[open]({url})")

# --- Runs ------------------------------------------------------------------

st.subheader("Recent runs")
if runs_df.empty:
    st.info("No runs recorded yet.")
else:
    st.dataframe(runs_df, width="stretch", hide_index=True)
