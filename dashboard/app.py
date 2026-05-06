"""Stefan · Run OS — main Streamlit dashboard."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.database import get_connection, get_recent_activities, get_weekly_summary, seed_plan_targets, create_tables, upsert_activities, upsert_streams
from src.metrics import (
    _fmt_time,
    get_cadence_trend,
    get_long_run_pace_drift,
    get_race_prediction,
    get_vo2_trend,
    get_weekly_load_status,
    get_zone_compliance_summary,
)
from src.strava_fetch import sync

CAPE_TOWN_DATE = date(2027, 5, 9)
SUB3_VO2_THRESHOLD = 62

PLOTLY_LAYOUT = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#e8e8f0", family="monospace"),
    margin=dict(l=20, r=20, t=40, b=20),
)


def fmt_pace(sec_per_km: float | None) -> str:
    """Format seconds/km as m:ss string."""
    if sec_per_km is None or sec_per_km <= 0:
        return "—"
    m = int(sec_per_km // 60)
    s = int(sec_per_km % 60)
    return f"{m}:{s:02d}"


def fmt_duration(total_s: float | None) -> str:
    """Format total seconds as h:mm:ss."""
    if total_s is None:
        return "—"
    h = int(total_s // 3600)
    m = int((total_s % 3600) // 60)
    s = int(total_s % 60)
    return f"{h}:{m:02d}:{s:02d}"


# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Stefan · Run OS",
    page_icon="🏃",
    layout="wide",
)

# ── Auto-backfill on cold start ───────────────────────────────────────────────

@st.cache_resource(show_spinner=False)
def _bootstrap():
    """Run once per process: create tables, seed plan, backfill if DB is empty."""
    create_tables()
    seed_plan_targets()
    con = get_connection()
    count = con.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    con.close()
    if count == 0:
        return "needs_backfill"
    return "ready"

_state = _bootstrap()
if _state == "needs_backfill":
    st.info("No data yet. Click below to sync the last 6 months of activities from Strava.")
    if st.button("⟳ Initial Sync", type="primary"):
        import time as _time
        from src.strava_fetch import fetch_activities, parse_activities, fetch_activity_streams, parse_streams, _compute_weekly_summaries
        ninety_days_ago = _time.time() - (90 * 24 * 3600)
        with st.spinner("Syncing last 90 days from Strava..."):
            try:
                raw = fetch_activities(after_timestamp=ninety_days_ago)
                if raw:
                    acts_df = parse_activities(raw)
                    if not acts_df.empty:
                        upsert_activities(acts_df)
                        affected_weeks = set()
                        for _, row in acts_df.iterrows():
                            raw_s = fetch_activity_streams(int(row["id"]))
                            streams_df = parse_streams(int(row["id"]), raw_s, row["distance_m"])
                            if not streams_df.empty:
                                upsert_streams(streams_df)
                            affected_weeks.add(row["week_start"])
                        _compute_weekly_summaries(list(affected_weeks))
                st.rerun()
            except Exception as e:
                st.error(f"Sync failed: {e}")
    st.stop()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🏃 Run OS")
    st.caption("Stefan Jensen · Copenhagen")

    if st.button("⟳ Sync Strava", use_container_width=True):
        with st.spinner("Syncing..."):
            try:
                sync(backfill=False)
                st.success("Sync complete")
                st.rerun()
            except Exception as e:
                st.error(f"Sync failed: {e}")

    if st.button("Init DB / Seed plan", use_container_width=True):
        create_tables()
        seed_plan_targets()
        st.success("Done")

    st.divider()
    st.caption(f"Cape Town Marathon: **{CAPE_TOWN_DATE}**")
    days_to_ct = (CAPE_TOWN_DATE - date.today()).days
    st.metric("Days to race", days_to_ct)


# ── Tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Overview", "Zone Compliance", "Plan vs Actual", "Race Predictor", "Activity Detail"
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Overview
# ══════════════════════════════════════════════════════════════════════════════

with tab1:
    load = get_weekly_load_status()
    vo2_df = get_vo2_trend(n_weeks=16)
    pred = get_race_prediction()

    # KPI row
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Phase", load["phase"].title() if load["phase"] else "—")

    week_label = f"{load['current_km']} km"
    if load["target_min"] and load["target_max"]:
        week_label += f" / {load['target_min']}–{load['target_max']}"
    k2.metric("This week", week_label)

    latest_vo2 = round(float(vo2_df["estimated_vo2max"].iloc[-1]), 1) if not vo2_df.empty else "—"
    k3.metric("Est. VO₂ max", latest_vo2)

    days_to_ct = (CAPE_TOWN_DATE - date.today()).days
    k4.metric("Days to Cape Town", days_to_ct)

    st.divider()

    # Weekly volume chart
    weekly = get_weekly_summary(n_weeks=12)
    if not weekly.empty:
        weekly = weekly.sort_values("week_start")
        weekly["week_label"] = weekly["week_start"].astype(str).str[:10]

        def bar_color(row):
            if row["km_min"] is None:
                return "#6366f1"
            if row["total_km"] > row["km_max"]:
                return "#ef4444"
            if row["total_km"] < row["km_min"]:
                return "#f59e0b"
            return "#22c55e"

        colors = weekly.apply(bar_color, axis=1).tolist()

        fig = go.Figure()

        # Target band (shaded)
        if "km_min" in weekly.columns and weekly["km_min"].notna().any():
            fig.add_trace(go.Scatter(
                x=weekly["week_label"].tolist() + weekly["week_label"].tolist()[::-1],
                y=weekly["km_max"].tolist() + weekly["km_min"].tolist()[::-1],
                fill="toself",
                fillcolor="rgba(249,115,22,0.12)",
                line=dict(color="rgba(0,0,0,0)"),
                name="Target range",
                hoverinfo="skip",
            ))

        fig.add_trace(go.Bar(
            x=weekly["week_label"],
            y=weekly["total_km"],
            marker_color=colors,
            name="Actual km",
        ))

        fig.update_layout(
            **PLOTLY_LAYOUT,
            title="Weekly Volume — last 12 weeks",
            xaxis_title="Week",
            yaxis_title="km",
            showlegend=True,
            height=320,
        )
        st.plotly_chart(fig, use_container_width=True)

    # VO2 trend
    if not vo2_df.empty:
        vo2_df["week_label"] = vo2_df["week_start"].astype(str).str[:10]
        vo2_df["rolling_avg"] = vo2_df["estimated_vo2max"].rolling(window=3, min_periods=1).mean().round(1)
        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(
            x=vo2_df["week_label"],
            y=vo2_df["estimated_vo2max"],
            mode="markers",
            marker=dict(color="#f97316", size=5, opacity=0.4),
            name="Weekly",
        ))
        fig2.add_trace(go.Scatter(
            x=vo2_df["week_label"],
            y=vo2_df["rolling_avg"],
            mode="lines",
            line=dict(color="#f97316", width=2.5),
            name="3-week avg",
        ))
        fig2.add_hline(
            y=SUB3_VO2_THRESHOLD,
            line_dash="dash",
            line_color="#22c55e",
            annotation_text="Sub-3:00 threshold (~62)",
            annotation_position="bottom right",
        )
        fig2.update_layout(
            **PLOTLY_LAYOUT,
            title="Estimated VO₂ max trend",
            yaxis_title="VO₂ max",
            height=280,
        )
        st.plotly_chart(fig2, use_container_width=True)

    # Cadence trend
    cad_df = get_cadence_trend(n_weeks=16)
    if not cad_df.empty:
        cad_df["week_label"] = cad_df["week_start"].astype(str).str[:10]
        fig_cad = go.Figure()
        fig_cad.add_trace(go.Scatter(
            x=cad_df["week_label"],
            y=cad_df["avg_cadence"],
            mode="markers",
            marker=dict(color="#6366f1", size=5, opacity=0.4),
            name="Weekly",
        ))
        fig_cad.add_trace(go.Scatter(
            x=cad_df["week_label"],
            y=cad_df["rolling_avg"],
            mode="lines",
            line=dict(color="#6366f1", width=2.5),
            name="3-week avg",
        ))
        fig_cad.add_hline(
            y=180,
            line_dash="dash",
            line_color="#f97316",
            annotation_text="180 spm target",
            annotation_position="bottom right",
        )
        fig_cad.update_layout(
            **PLOTLY_LAYOUT,
            title="Cadence trend (steps/min)",
            yaxis_title="spm",
            height=240,
        )
        st.plotly_chart(fig_cad, use_container_width=True)

    # AI insight box placeholder (Phase 7)
    try:
        from src.insights import generate_weekly_insight
        insight = generate_weekly_insight()
        st.markdown(
            f"""<div style="border:1px solid #f97316;border-radius:8px;padding:16px;
            background:#0d0d11;color:#e8e8f0;margin-top:8px;">
            🤖 <strong>Weekly AI Insight</strong><br><br>{insight}</div>""",
            unsafe_allow_html=True,
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Zone Compliance
# ══════════════════════════════════════════════════════════════════════════════

with tab2:
    st.subheader("Zone 2 Compliance — last 4 weeks")

    compliance = get_zone_compliance_summary(n_weeks=4)
    c1, c2, c3 = st.columns(3)
    c1.metric("Overall Z2 %", f"{compliance['overall_z2_pct']}%")
    c2.metric("Compliant sessions", compliance["compliant_sessions"])
    c3.metric("Total easy sessions", compliance["total_easy_sessions"])

    if compliance["overall_z2_pct"] < 70 and compliance["total_easy_sessions"] > 0:
        st.error(
            f"⚠️ {compliance['total_easy_sessions'] - compliance['compliant_sessions']} of your last "
            f"{compliance['total_easy_sessions']} easy runs drifted into Z3+. Slow down by ~15–20 sec/km."
        )

    # Zone distribution across recent activities
    con = get_connection()
    cutoff = date.today() - timedelta(weeks=4)
    streams_all = con.execute("""
        SELECT a.name, a.start_date, s.heartrate
        FROM activities a
        JOIN activity_streams s ON a.id = s.activity_id
        WHERE a.start_date >= ? AND s.heartrate IS NOT NULL
    """, [cutoff]).df()
    con.close()

    if not streams_all.empty:
        total = len(streams_all)
        zone_labels = ["Z1 (<111)", "Z2 (111–148)", "Z3 (148–157)", "Z4 (157–166)", "Z5 (166+)"]
        zone_bounds = [(0, 111), (111, 148), (148, 157), (157, 166), (166, 9999)]
        zone_colors = ["#94a3b8", "#22c55e", "#f59e0b", "#f97316", "#ef4444"]
        zone_pcts = []
        for lo, hi in zone_bounds:
            pct = ((streams_all["heartrate"] >= lo) & (streams_all["heartrate"] < hi)).sum() / total * 100
            zone_pcts.append(round(pct, 1))

        fig3 = go.Figure(go.Bar(
            x=zone_pcts,
            y=zone_labels,
            orientation="h",
            marker_color=zone_colors,
            text=[f"{p}%" for p in zone_pcts],
            textposition="outside",
        ))
        fig3.update_layout(
            **PLOTLY_LAYOUT,
            title="HR Zone distribution — all runs last 4 weeks",
            xaxis_title="%",
            height=300,
        )
        st.plotly_chart(fig3, use_container_width=True)

    # Offending runs table
    if compliance["offending_runs"]:
        st.subheader("Easy runs that drifted out of Z2")
        df_off = pd.DataFrame(compliance["offending_runs"])
        df_off.columns = ["Date", "Name", "Distance km", "Avg HR", "Z2 %"]

        def highlight_z2(row):
            color = "background-color: #3b0a0a" if row["Z2 %"] < 60 else "background-color: #1a2a0a"
            return [color] * len(row)

        st.dataframe(
            df_off.style.apply(highlight_z2, axis=1),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("All easy runs in the last 4 weeks were Z2 compliant. Nice work.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Plan vs Actual
# ══════════════════════════════════════════════════════════════════════════════

with tab3:
    st.subheader("Plan vs Actual — weekly volume")

    con = get_connection()
    plan_df = con.execute("""
        SELECT pt.week_number, pt.phase, pt.km_min, pt.km_max, pt.long_run_max, pt.notes,
               ws.total_km, ws.num_runs, ws.long_run_km, ws.week_start
        FROM plan_targets pt
        LEFT JOIN weekly_summaries ws ON pt.week_number = ws.week_number
        ORDER BY pt.week_number
    """).df()
    con.close()

    if not plan_df.empty:
        today = date.today()

        def status_icon(row):
            if pd.isna(row["total_km"]) or row["total_km"] == 0:
                ws = row["week_start"]
                if pd.notna(ws) and pd.Timestamp(ws).date() > today:
                    return "⬜ future"
                return "—"
            if row["total_km"] > row["km_max"]:
                return "🔴 over"
            if row["total_km"] < row["km_min"]:
                return "🟡 under"
            return "🟢 on track"

        plan_df["Status"] = plan_df.apply(status_icon, axis=1)
        plan_df["Actual km"] = plan_df["total_km"].apply(
            lambda x: f"{x:.1f}" if pd.notna(x) and x > 0 else "—"
        )
        plan_df["Target"] = plan_df.apply(
            lambda r: f"{r['km_min']:.0f}–{r['km_max']:.0f}", axis=1
        )
        plan_df["Long run km"] = plan_df["long_run_km"].apply(
            lambda x: f"{x:.1f}" if pd.notna(x) and x > 0 else "—"
        )

        display = plan_df[["week_number", "phase", "Target", "Actual km", "Long run km", "Status", "notes"]].copy()
        display.columns = ["Week", "Phase", "Target km", "Actual km", "Long run km", "Status", "Notes"]
        st.dataframe(display, use_container_width=True, hide_index=True)

        # Chart: all plan weeks with actuals overlaid
        future_mask = plan_df["total_km"].isna() | (plan_df["total_km"] == 0)
        bar_colors = plan_df.apply(
            lambda r: "#3b3b4a" if (pd.isna(r["total_km"]) or r["total_km"] == 0)
            else "#22c55e" if r["km_min"] <= r["total_km"] <= r["km_max"]
            else "#f59e0b" if r["total_km"] < r["km_min"]
            else "#ef4444", axis=1
        ).tolist()

        fig4 = go.Figure()
        fig4.add_trace(go.Scatter(
            x=plan_df["week_number"].tolist() + plan_df["week_number"].tolist()[::-1],
            y=plan_df["km_max"].tolist() + plan_df["km_min"].tolist()[::-1],
            fill="toself",
            fillcolor="rgba(249,115,22,0.10)",
            line=dict(color="rgba(0,0,0,0)"),
            name="Target range",
            hoverinfo="skip",
        ))
        fig4.add_trace(go.Bar(
            x=plan_df["week_number"],
            y=plan_df["total_km"].fillna(0),
            marker_color=bar_colors,
            name="Actual km",
        ))
        fig4.update_layout(
            **PLOTLY_LAYOUT,
            title="All plan weeks — actual vs target",
            xaxis_title="Plan week",
            yaxis_title="km",
            height=350,
        )
        st.plotly_chart(fig4, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — Race Predictor
# ══════════════════════════════════════════════════════════════════════════════

with tab4:
    pred = get_race_prediction()

    st.subheader("Race Time Predictions")
    p1, p2, p3, p4 = st.columns(4)
    p1.metric("Predicted marathon", pred["predicted_marathon"])
    p2.metric("Predicted half", pred["predicted_half"])
    p3.metric("Confidence", pred["confidence"].title())
    delta = pred.get("fitness_delta_pct", 0.0)
    delta_str = f"{abs(delta):.1f}% {'fitter' if delta < 0 else 'less fit'} vs London"
    p4.metric("Fitness vs London", delta_str)

    st.caption(f"Methodology: {pred['inputs_used']}")

    st.divider()
    st.subheader("What it takes for sub-3:00")

    gap_marathon = "—"
    gap_half = "—"
    gap_vo2 = "—"
    if pred["marathon_pace_s"]:
        sub3_pace_s = (3 * 3600) / 42.195
        gap_s = pred["marathon_pace_s"] - sub3_pace_s
        gap_marathon = f"{gap_s:+.0f} sec/km to find"
    if pred["half_pace_s"]:
        sub3_half_pace_s = (1 * 3600 + 24 * 60 + 30) / 21.0975
        gap_s = pred["half_pace_s"] - sub3_half_pace_s
        gap_half = f"{gap_s:+.0f} sec/km to find"
    if latest_vo2 != "—":
        gap_vo2 = f"{SUB3_VO2_THRESHOLD - float(latest_vo2):+.1f} to target"

    sub3_table = {
        "Metric": ["Marathon time", "Marathon pace", "Half marathon", "VO₂ max", "Easy pace"],
        "Current": [
            pred["predicted_marathon"],
            fmt_pace(pred.get("marathon_pace_s")),
            pred["predicted_half"],
            str(latest_vo2),
            fmt_pace(pred.get("marathon_pace_s", 0) * 1.12 if pred.get("marathon_pace_s") else None),
        ],
        "Sub-3:00 target": ["2:59:59", "4:15 /km", "~1:24:30", "≥ 62", "~4:50 /km"],
        "Gap": [
            "—",
            gap_marathon,
            "—",
            gap_vo2,
            "—",
        ],
    }
    st.table(pd.DataFrame(sub3_table))

    st.divider()
    st.subheader("Weight sensitivity")
    weight = st.slider("Body weight (kg)", min_value=65, max_value=95, value=76, step=1)
    baseline_weight = 76
    # ~0.5% time improvement per kg lost
    weight_factor = 1 + (weight - baseline_weight) * 0.005
    if pred["predicted_marathon"] != "N/A" and pred["marathon_pace_s"]:
        adj_pace_s = pred["marathon_pace_s"] * weight_factor
        adj_time_s = adj_pace_s * 42.195
        direction = "slower" if weight > baseline_weight else "faster"
        st.info(
            f"At **{weight} kg**: predicted marathon ≈ **{_fmt_time(adj_time_s)}** "
            f"({abs(weight - baseline_weight)} kg {direction} than 76 kg baseline)"
        )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — Activity Detail
# ══════════════════════════════════════════════════════════════════════════════

with tab5:
    st.subheader("Activity Detail")
    acts = get_recent_activities(n=50)

    if acts.empty:
        st.info("No activities found.")
    else:
        acts["label"] = acts.apply(
            lambda r: f"{str(r['start_date'])[:10]}  {r['name']}  ({r['distance_m']/1000:.1f} km)", axis=1
        )
        selected_label = st.selectbox("Select activity", acts["label"].tolist())
        selected = acts[acts["label"] == selected_label].iloc[0]
        activity_id = int(selected["id"])

        a1, a2, a3, a4, a5 = st.columns(5)
        a1.metric("Distance", f"{selected['distance_m']/1000:.2f} km")
        a2.metric("Time", fmt_duration(selected["moving_time_s"]))
        a3.metric("Avg pace", fmt_pace(1000 / selected["average_speed"] if selected["average_speed"] else None))
        a4.metric("Avg HR", f"{selected['average_heartrate']:.0f}" if pd.notna(selected["average_heartrate"]) else "—")
        a5.metric("Elevation", f"{selected['total_elevation_gain']:.0f} m")

        # HR zone breakdown for this activity
        con = get_connection()
        streams = con.execute("""
            SELECT heartrate FROM activity_streams
            WHERE activity_id = ? AND heartrate IS NOT NULL
        """, [activity_id]).df()
        con.close()

        if not streams.empty:
            zone_labels = ["Z1", "Z2", "Z3", "Z4", "Z5"]
            zone_bounds = [(0, 111), (111, 148), (148, 157), (157, 166), (166, 9999)]
            zone_colors = ["#94a3b8", "#22c55e", "#f59e0b", "#f97316", "#ef4444"]
            total = len(streams)
            z_pcts = [
                round(((streams["heartrate"] >= lo) & (streams["heartrate"] < hi)).sum() / total * 100, 1)
                for lo, hi in zone_bounds
            ]
            fig5 = go.Figure(go.Bar(
                x=zone_labels, y=z_pcts,
                marker_color=zone_colors,
                text=[f"{p}%" for p in z_pcts],
                textposition="outside",
            ))
            fig5.update_layout(**PLOTLY_LAYOUT, title="HR zone breakdown", height=250, yaxis_title="%")
            st.plotly_chart(fig5, use_container_width=True)

        # Pace drift for long runs
        if selected["distance_m"] >= 15000:
            drift = get_long_run_pace_drift(activity_id)
            if not drift.empty:
                drift["label"] = drift.apply(lambda r: f"km {int(r['km_start'])}–{int(r['km_end'])}", axis=1)
                drift["pace_fmt"] = drift["avg_pace"].apply(fmt_pace)
                bar_colors_drift = ["#ef4444" if f else "#f97316" for f in drift["flagged"]]

                fig6 = go.Figure()
                fig6.add_trace(go.Bar(
                    x=drift["label"], y=drift["avg_pace"],
                    marker_color=bar_colors_drift,
                    name="Avg pace (sec/km)",
                    text=drift["pace_fmt"],
                    textposition="outside",
                ))
                fig6.add_trace(go.Scatter(
                    x=drift["label"], y=drift["avg_hr"],
                    name="Avg HR",
                    yaxis="y2",
                    line=dict(color="#94a3b8", width=2),
                    mode="lines+markers",
                ))
                fig6.update_layout(
                    **PLOTLY_LAYOUT,
                    title="Pace drift by 5km segment (red = >15s degradation)",
                    yaxis=dict(title="Pace (sec/km)", autorange="reversed"),
                    yaxis2=dict(title="HR", overlaying="y", side="right"),
                    height=320,
                )
                st.plotly_chart(fig6, use_container_width=True)
