"""Computed metrics layer — zone compliance, VO2 trend, race prediction, load status."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from src.database import get_connection, PLAN_WEEK_ZERO

MAX_HR = 185
RESTING_HR_ESTIMATE = 42  # estimated from easy run HR floor

ZONES = [
    (0,   111),
    (111, 148),
    (148, 157),
    (157, 166),
    (166, 9999),
]


def _is_easy_run(name: str, avg_hr: float | None) -> bool:
    """Return True if the run looks like an easy/Z2 effort."""
    if name is None:
        name = ""
    hard_keywords = {"interval", "tempo", "race", "marathon", "fartlek", "threshold", "track"}
    name_lower = name.lower()
    if any(k in name_lower for k in hard_keywords):
        return False
    if avg_hr is not None and avg_hr >= 155:
        return False
    return True


def get_zone_compliance_summary(n_weeks: int = 4) -> dict:
    """Return Z2 compliance stats for easy runs over the last n_weeks."""
    con = get_connection()
    cutoff = date.today() - timedelta(weeks=n_weeks)

    acts = con.execute("""
        SELECT id, name, start_date, distance_m, average_heartrate, moving_time_s
        FROM activities
        WHERE start_date >= ?
        ORDER BY start_date DESC
    """, [cutoff]).df()

    if acts.empty:
        con.close()
        return {
            "overall_z2_pct": 0,
            "compliant_sessions": 0,
            "total_easy_sessions": 0,
            "offending_runs": [],
        }

    easy_acts = acts[acts.apply(
        lambda r: _is_easy_run(r["name"], r["average_heartrate"]), axis=1
    )]

    offending = []
    compliant = 0

    for _, row in easy_acts.iterrows():
        streams = con.execute("""
            SELECT heartrate FROM activity_streams
            WHERE activity_id = ? AND heartrate IS NOT NULL
        """, [int(row["id"])]).df()

        if streams.empty:
            continue

        total = len(streams)
        z2_count = ((streams["heartrate"] >= 111) & (streams["heartrate"] < 148)).sum()
        z2_pct = round(z2_count / total * 100, 1)

        if z2_pct >= 70:
            compliant += 1
        else:
            offending.append({
                "date": str(row["start_date"])[:10],
                "name": row["name"],
                "distance_km": round(row["distance_m"] / 1000, 1),
                "avg_hr": row["average_heartrate"],
                "z2_pct": z2_pct,
            })

    total_easy = len(easy_acts)
    overall_z2 = round(compliant / total_easy * 100, 1) if total_easy > 0 else 0

    con.close()
    return {
        "overall_z2_pct": overall_z2,
        "compliant_sessions": compliant,
        "total_easy_sessions": total_easy,
        "offending_runs": offending,
    }


ANCHOR_VDOT = 52.5          # from London Marathon 3:14:53 on 2026-04-26
ANCHOR_DATE = date(2026, 4, 26)
REF_HR_LOW = 130            # reference HR band for pace-at-HR tracking
REF_HR_HIGH = 148           # top of Z2


def get_vo2_trend(n_weeks: int = 16) -> pd.DataFrame:
    """Estimate weekly VO2 max anchored to London Marathon VDOT, trended by pace-at-HR."""
    con = get_connection()
    cutoff = date.today() - timedelta(weeks=n_weeks)

    # Pull stream-level data: pace per km split + HR, joined to easy runs only
    streams = con.execute("""
        SELECT a.week_start, s.pace_sec_per_km, s.heartrate
        FROM activity_streams s
        JOIN activities a ON s.activity_id = a.id
        WHERE a.start_date >= ?
          AND s.heartrate BETWEEN ? AND ?
          AND s.pace_sec_per_km BETWEEN 200 AND 500
          AND a.average_heartrate < 155
    """, [cutoff, REF_HR_LOW, REF_HR_HIGH]).df()
    con.close()

    if streams.empty:
        return pd.DataFrame(columns=["week_start", "estimated_vo2max"])

    # Filter to easy runs only by name — use activity name via a second pass if needed
    # (HR filter above already excludes hard efforts)

    # Compute median pace at reference HR for each week
    weekly_pace = (
        streams.groupby("week_start")["pace_sec_per_km"]
        .median()
        .reset_index()
        .rename(columns={"pace_sec_per_km": "ref_pace"})
    )

    if weekly_pace.empty:
        return pd.DataFrame(columns=["week_start", "estimated_vo2max"])

    # Anchor: pace at ref HR around London race week
    anchor_window = weekly_pace[
        weekly_pace["week_start"].apply(
            lambda w: abs((pd.Timestamp(w).date() - ANCHOR_DATE).days) <= 28
        )
    ]

    if anchor_window.empty:
        # Fall back to median of all weeks as anchor
        baseline_pace = float(weekly_pace["ref_pace"].median())
    else:
        baseline_pace = float(anchor_window["ref_pace"].median())

    # VO2 scales with velocity: faster at same HR = higher VO2
    weekly_pace["estimated_vo2max"] = weekly_pace["ref_pace"].apply(
        lambda p: round(ANCHOR_VDOT * (baseline_pace / p), 1) if p > 0 else None
    )

    return weekly_pace[["week_start", "estimated_vo2max"]].dropna()


# London Marathon anchor — Riegel formula base
LONDON_TIME_S = 3 * 3600 + 14 * 60 + 53   # 3:14:53
LONDON_DIST_M = 42195.0
LONDON_DATE = date(2026, 4, 26)
RIEGEL_EXP = 1.06


def _riegel(base_time_s: float, base_dist_m: float, target_dist_m: float) -> float:
    """Riegel race time formula: T2 = T1 * (D2/D1)^1.06."""
    return base_time_s * (target_dist_m / base_dist_m) ** RIEGEL_EXP


def _fmt_time(total_s: float) -> str:
    """Format seconds as h:mm:ss."""
    h = int(total_s // 3600)
    m = int((total_s % 3600) // 60)
    s = int(total_s % 60)
    return f"{h}:{m:02d}:{s:02d}"


def get_race_prediction() -> dict:
    """Predict marathon and half times anchored to London 3:14:53, adjusted by current fitness."""
    con = get_connection()

    # Get pace-at-ref-HR from last 4 weeks (same signal as VO2 trend)
    cutoff = date.today() - timedelta(weeks=4)
    recent_streams = con.execute("""
        SELECT s.pace_sec_per_km
        FROM activity_streams s
        JOIN activities a ON s.activity_id = a.id
        WHERE a.start_date >= ?
          AND s.heartrate BETWEEN ? AND ?
          AND s.pace_sec_per_km BETWEEN 200 AND 500
          AND a.average_heartrate < 155
    """, [cutoff, REF_HR_LOW, REF_HR_HIGH]).df()

    # Baseline pace at ref HR from London training window (±4 weeks)
    london_cutoff_lo = LONDON_DATE - timedelta(weeks=4)
    london_cutoff_hi = LONDON_DATE + timedelta(weeks=1)
    baseline_streams = con.execute("""
        SELECT s.pace_sec_per_km
        FROM activity_streams s
        JOIN activities a ON s.activity_id = a.id
        WHERE a.start_date BETWEEN ? AND ?
          AND s.heartrate BETWEEN ? AND ?
          AND s.pace_sec_per_km BETWEEN 200 AND 500
          AND a.average_heartrate < 155
    """, [london_cutoff_lo, london_cutoff_hi, REF_HR_LOW, REF_HR_HIGH]).df()

    n_recent = len(recent_streams)
    con.close()

    no_data = {
        "predicted_marathon": "N/A",
        "predicted_half": "N/A",
        "confidence": "low",
        "inputs_used": "Not enough easy run data yet",
        "marathon_pace_s": None,
        "half_pace_s": None,
        "fitness_delta_pct": 0.0,
    }

    if recent_streams.empty or baseline_streams.empty:
        return no_data

    current_pace = float(recent_streams["pace_sec_per_km"].median())
    baseline_pace = float(baseline_streams["pace_sec_per_km"].median())

    # Fitness delta: faster pace at same HR = fitter = scale time down proportionally
    fitness_ratio = current_pace / baseline_pace  # <1 = fitter, >1 = less fit
    adjusted_marathon_s = LONDON_TIME_S * fitness_ratio
    adjusted_half_s = _riegel(adjusted_marathon_s, LONDON_DIST_M, 21097.5)

    marathon_pace_s = adjusted_marathon_s / 42.195
    half_pace_s = adjusted_half_s / 21.0975

    confidence = "high" if n_recent >= 30 else "medium" if n_recent >= 10 else "low"
    delta_pct = round((fitness_ratio - 1) * 100, 1)
    direction = "fitter" if delta_pct < 0 else "less fit"

    return {
        "predicted_marathon": _fmt_time(adjusted_marathon_s),
        "predicted_half": _fmt_time(adjusted_half_s),
        "confidence": confidence,
        "inputs_used": f"Anchored to London 3:14:53 · current fitness {abs(delta_pct)}% {direction} vs pre-London",
        "marathon_pace_s": marathon_pace_s,
        "half_pace_s": half_pace_s,
        "fitness_delta_pct": delta_pct,
    }


def get_cadence_trend(n_weeks: int = 16) -> pd.DataFrame:
    """Return weekly average cadence from easy runs."""
    con = get_connection()
    cutoff = date.today() - timedelta(weeks=n_weeks)
    df = con.execute("""
        SELECT a.week_start, s.cadence
        FROM activity_streams s
        JOIN activities a ON s.activity_id = a.id
        WHERE a.start_date >= ?
          AND s.cadence IS NOT NULL
          AND s.cadence BETWEEN 60 AND 105
          AND a.average_heartrate < 160
    """, [cutoff]).df()
    con.close()

    if df.empty:
        return pd.DataFrame(columns=["week_start", "avg_cadence", "rolling_avg"])

    # Strava/Garmin records cadence per leg — multiply by 2 for total spm
    df["cadence"] = df["cadence"] * 2

    weekly = (
        df.groupby("week_start")["cadence"]
        .median()
        .reset_index()
        .rename(columns={"cadence": "avg_cadence"})
    )
    weekly["rolling_avg"] = weekly["avg_cadence"].rolling(window=3, min_periods=1).mean().round(1)
    return weekly


def get_weekly_load_status() -> dict:
    """Return current week's km vs plan target."""
    today = date.today()
    current_week_start = today - timedelta(days=today.weekday())
    days_remaining = 6 - today.weekday()

    con = get_connection()
    result = con.execute("""
        SELECT SUM(distance_m) / 1000 as total_km
        FROM activities
        WHERE week_start = ?
    """, [current_week_start]).fetchone()

    current_km = round(float(result[0]), 1) if result and result[0] else 0.0

    week_number = ((current_week_start - PLAN_WEEK_ZERO).days // 7) + 1
    plan = con.execute("""
        SELECT km_min, km_max, phase FROM plan_targets WHERE week_number = ?
    """, [week_number]).fetchone()
    con.close()

    if plan:
        km_min, km_max, phase = plan
        if current_km < km_min * 0.5:
            status = "under"
        elif current_km > km_max:
            status = "over"
        else:
            status = "on_track"
    else:
        km_min, km_max, phase, status = None, None, "pre-plan", "on_track"

    return {
        "current_km": current_km,
        "target_min": km_min,
        "target_max": km_max,
        "phase": phase,
        "status": status,
        "days_remaining": days_remaining,
        "week_number": week_number,
    }


def get_long_run_pace_drift(activity_id: int) -> pd.DataFrame:
    """Return 5km segment pace and HR for a long run, flagging degradation."""
    con = get_connection()
    streams = con.execute("""
        SELECT km, pace_sec_per_km, heartrate
        FROM activity_streams
        WHERE activity_id = ?
        ORDER BY km
    """, [activity_id]).df()
    con.close()

    if streams.empty:
        return pd.DataFrame()

    # Group into 5km segments
    streams["segment"] = ((streams["km"] - 1) // 5) + 1
    segments = streams.groupby("segment").agg(
        avg_pace=("pace_sec_per_km", "mean"),
        avg_hr=("heartrate", "mean"),
        km_start=("km", "min"),
        km_end=("km", "max"),
    ).reset_index()

    if len(segments) > 0:
        baseline_pace = segments.iloc[0]["avg_pace"]
        segments["pace_drift_s"] = segments["avg_pace"] - baseline_pace
        segments["flagged"] = segments["pace_drift_s"] > 15

    return segments
