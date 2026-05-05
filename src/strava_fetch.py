"""Strava API fetcher — activities, streams, and sync logic."""

from __future__ import annotations
import argparse
import time
from datetime import date, timedelta

import pandas as pd
import requests

from src.strava_auth import get_valid_token
from src.database import (
    create_tables,
    get_last_activity_date,
    upsert_activities,
    upsert_streams,
    get_connection,
    PLAN_WEEK_ZERO,
)

BASE_URL = "https://www.strava.com/api/v3"
MAX_HR = 185

# HR zone boundaries (% of max HR 185)
ZONES = [
    (0,   111),   # Z1: < 60%
    (111, 148),   # Z2: 60–80%
    (148, 157),   # Z3: 80–85%
    (157, 166),   # Z4: 85–90%
    (166, 9999),  # Z5: > 90%
]


def _headers() -> dict:
    """Build auth headers using a valid access token."""
    return {"Authorization": f"Bearer {get_valid_token()}"}


def fetch_activities(after_timestamp: float | None = None, per_page: int = 100) -> list[dict]:
    """Fetch all activities from Strava, paginating until exhausted."""
    activities = []
    page = 1
    while True:
        params = {"per_page": per_page, "page": page}
        if after_timestamp:
            params["after"] = int(after_timestamp)

        resp = requests.get(f"{BASE_URL}/athlete/activities", headers=_headers(), params=params)

        if resp.status_code == 429:
            print("Rate limited by Strava. Sleeping 15 minutes...")
            time.sleep(900)
            continue

        resp.raise_for_status()
        batch = resp.json()

        if not batch:
            break

        activities.extend(batch)
        print(f"  Fetched page {page} ({len(batch)} activities)")
        page += 1

    return activities


def fetch_activity_streams(activity_id: int) -> dict:
    """Fetch heartrate, cadence, time, and distance streams for one activity."""
    keys = "heartrate,cadence,time,distance"
    resp = requests.get(
        f"{BASE_URL}/activities/{activity_id}/streams",
        headers=_headers(),
        params={"keys": keys, "key_by_type": "true"},
    )

    if resp.status_code == 429:
        print("Rate limited. Sleeping 15 minutes...")
        time.sleep(900)
        return fetch_activity_streams(activity_id)

    if resp.status_code == 404:
        return {}

    resp.raise_for_status()
    return resp.json()


def _week_start(dt: date) -> date:
    """Return the Monday of the week containing dt."""
    return dt - timedelta(days=dt.weekday())


def _plan_week_number(dt: date) -> int:
    """Return the plan week number relative to PLAN_WEEK_ZERO (week 1 = first week post CPH)."""
    delta = (dt - PLAN_WEEK_ZERO).days
    return (delta // 7) + 1


def parse_activities(raw_activities: list[dict]) -> pd.DataFrame:
    """Map raw Strava activity dicts to the activities table schema."""
    rows = []
    for a in raw_activities:
        sport = a.get("sport_type", "")
        if "Run" not in sport:
            continue

        start_dt = pd.to_datetime(a["start_date_local"]).date()
        ws = _week_start(start_dt)

        rows.append({
            "id":                   a["id"],
            "name":                 a.get("name", ""),
            "sport_type":           sport,
            "start_date":           a["start_date_local"],
            "distance_m":           a.get("distance", 0),
            "moving_time_s":        a.get("moving_time", 0),
            "elapsed_time_s":       a.get("elapsed_time", 0),
            "total_elevation_gain": a.get("total_elevation_gain", 0),
            "average_heartrate":    a.get("average_heartrate"),
            "max_heartrate":        a.get("max_heartrate"),
            "average_cadence":      a.get("average_cadence"),
            "average_speed":        a.get("average_speed", 0),
            "suffer_score":         a.get("suffer_score"),
            "pr_count":             a.get("pr_count", 0),
            "week_start":           ws,
            "week_number":          _plan_week_number(ws),
            "year":                 start_dt.year,
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def parse_streams(activity_id: int, raw_streams: dict, activity_distance_m: float) -> pd.DataFrame:
    """Convert raw Strava streams into per-km splits."""
    if not raw_streams or "distance" not in raw_streams:
        return pd.DataFrame()

    distance = raw_streams["distance"]["data"]
    time_s = raw_streams.get("time", {}).get("data", [0] * len(distance))
    hr = raw_streams.get("heartrate", {}).get("data", [None] * len(distance))
    cadence = raw_streams.get("cadence", {}).get("data", [None] * len(distance))

    total_km = int(activity_distance_m / 1000)
    rows = []

    for km in range(1, total_km + 1):
        low_m = (km - 1) * 1000
        high_m = km * 1000

        indices = [i for i, d in enumerate(distance) if low_m <= d < high_m]
        if not indices:
            continue

        split_time = time_s[indices[-1]] - time_s[indices[0]]
        pace = split_time if split_time > 0 else None

        hr_vals = [hr[i] for i in indices if hr[i] is not None]
        cad_vals = [cadence[i] for i in indices if cadence[i] is not None]

        rows.append({
            "activity_id":    activity_id,
            "km":             km,
            "pace_sec_per_km": pace,
            "heartrate":      sum(hr_vals) / len(hr_vals) if hr_vals else None,
            "cadence":        sum(cad_vals) / len(cad_vals) if cad_vals else None,
        })

    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _compute_weekly_summaries(week_starts: list[date]) -> None:
    """Recompute weekly_summaries for the given set of week_start dates."""
    con = get_connection()
    for ws in week_starts:
        acts = con.execute("""
            SELECT id, distance_m, moving_time_s, average_heartrate, average_speed,
                   week_number
            FROM activities
            WHERE week_start = ?
        """, [ws]).df()

        if acts.empty:
            continue

        total_km = acts["distance_m"].sum() / 1000
        num_runs = len(acts)
        avg_hr = acts["average_heartrate"].mean()
        avg_speed = acts["average_speed"].mean()
        avg_pace = (1000 / avg_speed) if avg_speed and avg_speed > 0 else None
        long_run_km = acts["distance_m"].max() / 1000
        week_number = int(acts["week_number"].iloc[0])

        # Compute zone percentages from activity_streams
        ids = acts["id"].tolist()
        placeholders = ", ".join(["?" for _ in ids])
        streams = con.execute(f"""
            SELECT heartrate FROM activity_streams
            WHERE activity_id IN ({placeholders}) AND heartrate IS NOT NULL
        """, ids).df()

        z_pcts = [0.0] * 5
        if not streams.empty:
            total = len(streams)
            for i, (lo, hi) in enumerate(ZONES):
                count = ((streams["heartrate"] >= lo) & (streams["heartrate"] < hi)).sum()
                z_pcts[i] = round(count / total * 100, 1)

        z2_compliant = bool(z_pcts[1] >= 70)

        con.execute("""
            INSERT OR REPLACE INTO weekly_summaries
            (week_start, total_km, num_runs, avg_heartrate, avg_pace_sec_per_km,
             long_run_km, z1_pct, z2_pct, z3_pct, z4_pct, z5_pct, z2_compliant, week_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [ws, total_km, num_runs, avg_hr, avg_pace,
              long_run_km, *z_pcts, z2_compliant, week_number])

    con.close()


def sync(backfill: bool = False, streams_days: int = 120) -> None:
    """Sync Strava activities to the local database.

    backfill=True fetches all activity metadata but limits streams to
    the most recent streams_days days to avoid Strava rate limits.
    """
    create_tables()

    after_ts = None
    if not backfill:
        last = get_last_activity_date()
        if last:
            after_ts = pd.Timestamp(last).timestamp()
            print(f"Incremental sync from {last}")
        else:
            print("No existing data — fetching all activities.")
    else:
        print("Backfill mode — fetching all activities.")

    raw = fetch_activities(after_timestamp=after_ts)
    if not raw:
        print("No new activities found.")
        return

    print(f"Processing {len(raw)} activities...")
    acts_df = parse_activities(raw)
    if acts_df.empty:
        print("No running activities found.")
        return

    upsert_activities(acts_df)
    print(f"Saved {len(acts_df)} activities.")

    # Only fetch streams for recent activities to stay within Strava rate limits
    streams_cutoff = pd.Timestamp.now() - pd.Timedelta(days=streams_days)
    recent = acts_df[pd.to_datetime(acts_df["start_date"]) >= streams_cutoff]
    older = acts_df[pd.to_datetime(acts_df["start_date"]) < streams_cutoff]

    if not older.empty:
        print(f"  Skipping streams for {len(older)} older activities (>{streams_days}d ago).")

    affected_weeks = set()
    for _, row in recent.iterrows():
        activity_id = int(row["id"])
        name = row["name"]
        distance_m = row["distance_m"]
        print(f"  Syncing streams: {activity_id} — {name}")

        raw_streams = fetch_activity_streams(activity_id)
        streams_df = parse_streams(activity_id, raw_streams, distance_m)
        if not streams_df.empty:
            upsert_streams(streams_df)

        affected_weeks.add(row["week_start"])

    # Still recompute weekly summaries for all activity weeks
    all_weeks = set(acts_df["week_start"].tolist())
    print(f"Recomputing weekly summaries for {len(all_weeks)} week(s)...")
    _compute_weekly_summaries(list(all_weeks))
    print("Sync complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Strava data sync")
    parser.add_argument("--backfill", action="store_true", help="Fetch all historical activities")
    parser.add_argument("--sync", action="store_true", help="Incremental sync (default)")
    args = parser.parse_args()

    if args.backfill:
        sync(backfill=True)
    else:
        sync(backfill=False)
