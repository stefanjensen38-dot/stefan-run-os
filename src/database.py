"""DuckDB schema and read/write layer for Run OS."""

from pathlib import Path
from datetime import date, timedelta

import duckdb
import pandas as pd

DB_PATH = Path(__file__).parent.parent / "data" / "run_os.db"

PLAN_WEEK_ZERO = date(2026, 5, 12)  # week 1 starts here (post Copenhagen)


def get_connection() -> duckdb.DuckDBPyConnection:
    """Return a DuckDB connection to the persistent database file."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(DB_PATH))


def create_tables() -> None:
    """Create all tables if they don't already exist."""
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            id BIGINT PRIMARY KEY,
            name VARCHAR,
            sport_type VARCHAR,
            start_date TIMESTAMP,
            distance_m DOUBLE,
            moving_time_s INTEGER,
            elapsed_time_s INTEGER,
            total_elevation_gain DOUBLE,
            average_heartrate DOUBLE,
            max_heartrate DOUBLE,
            average_cadence DOUBLE,
            average_speed DOUBLE,
            suffer_score INTEGER,
            pr_count INTEGER,
            week_start DATE,
            week_number INTEGER,
            year INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS activity_streams (
            activity_id BIGINT,
            km INTEGER,
            pace_sec_per_km DOUBLE,
            heartrate DOUBLE,
            cadence DOUBLE,
            PRIMARY KEY (activity_id, km)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS weekly_summaries (
            week_start DATE PRIMARY KEY,
            total_km DOUBLE,
            num_runs INTEGER,
            avg_heartrate DOUBLE,
            avg_pace_sec_per_km DOUBLE,
            long_run_km DOUBLE,
            z1_pct DOUBLE,
            z2_pct DOUBLE,
            z3_pct DOUBLE,
            z4_pct DOUBLE,
            z5_pct DOUBLE,
            z2_compliant BOOLEAN,
            week_number INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS plan_targets (
            week_number INTEGER PRIMARY KEY,
            phase VARCHAR,
            km_min DOUBLE,
            km_max DOUBLE,
            long_run_max DOUBLE,
            notes VARCHAR
        )
    """)
    con.close()
    print("Tables created.")


def upsert_activities(df: pd.DataFrame) -> None:
    """Insert or replace activities rows from a DataFrame."""
    con = get_connection()
    con.execute("INSERT OR REPLACE INTO activities SELECT * FROM df")
    con.close()


def upsert_streams(df: pd.DataFrame) -> None:
    """Insert or replace activity_streams rows from a DataFrame."""
    con = get_connection()
    con.execute("INSERT OR REPLACE INTO activity_streams SELECT * FROM df")
    con.close()


def get_last_activity_date():
    """Return the most recent start_date in activities, or None if empty."""
    con = get_connection()
    result = con.execute("SELECT MAX(start_date) FROM activities").fetchone()
    con.close()
    return result[0] if result else None


def get_weekly_summary(n_weeks: int = 12) -> pd.DataFrame:
    """Return weekly_summaries joined with plan_targets for the last n_weeks."""
    con = get_connection()
    df = con.execute(f"""
        SELECT ws.*, pt.phase, pt.km_min, pt.km_max, pt.long_run_max, pt.notes
        FROM weekly_summaries ws
        LEFT JOIN plan_targets pt ON ws.week_number = pt.week_number  -- week_number links training plan to actuals
        ORDER BY ws.week_start DESC
        LIMIT {n_weeks}
    """).df()
    con.close()
    return df


def get_recent_activities(n: int = 20) -> pd.DataFrame:
    """Return the n most recent activities."""
    con = get_connection()
    df = con.execute(f"""
        SELECT * FROM activities
        ORDER BY start_date DESC
        LIMIT {n}
    """).df()
    con.close()
    return df


def get_zone_compliance(n_weeks: int = 4) -> pd.DataFrame:
    """Return per-activity zone data for the last n_weeks from activity_streams."""
    con = get_connection()
    cutoff = date.today() - timedelta(weeks=n_weeks)
    df = con.execute("""
        SELECT a.id, a.name, a.start_date, a.distance_m, a.average_heartrate,
               s.heartrate, s.km
        FROM activities a
        JOIN activity_streams s ON a.id = s.activity_id
        WHERE a.start_date >= ?
        ORDER BY a.start_date DESC
    """, [cutoff]).df()
    con.close()
    return df


def seed_plan_targets() -> None:
    """Insert the 20-week training plan targets (post Copenhagen, starting 2026-05-12)."""
    targets = [
        (1,  "recovery",   0,  15, 10, "Post CPH. Walk/easy only."),
        (2,  "recovery",  15,  25, 16, "Easy runs only if pain-free."),
        (3,  "recovery",  25,  35, 16, "No structure, no targets."),
        (4,  "recovery",  35,  45, 18, "Legs normalising."),
        (5,  "transition",40,  50, 20, "Transition to base."),
        (6,  "transition",40,  50, 20, "Transition to base."),
        (7,  "base",      50,  55, 22, "Base phase starts."),
        (8,  "base",      50,  57, 22, "Base build."),
        (9,  "base",      52,  58, 24, "Base build."),
        (10, "base",      55,  62, 24, "Base build."),
        (11, "base",      55,  63, 26, "Add 4x4 intervals."),
        (12, "base",      58,  65, 26, "Base build."),
        (13, "base",      58,  65, 28, "Base build."),
        (14, "base",      60,  68, 28, "Base build."),
        (15, "base",      60,  68, 28, "Base build."),
        (16, "threshold", 62,  70, 30, "Threshold starts."),
        (17, "threshold", 62,  70, 30, "Half marathon build."),
        (18, "threshold", 60,  68, 28, "Half marathon build."),
        (19, "taper",     58,  65, 26, "Half taper starts."),
        (20, "taper",     45,  55, 21, "Race week — half marathon."),
    ]
    con = get_connection()
    con.execute("DELETE FROM plan_targets")
    con.executemany("""
        INSERT INTO plan_targets (week_number, phase, km_min, km_max, long_run_max, notes)
        VALUES (?, ?, ?, ?, ?, ?)
    """, targets)
    con.close()
    print(f"Seeded {len(targets)} plan target weeks.")


if __name__ == "__main__":
    create_tables()
    seed_plan_targets()
    print(f"Database ready at {DB_PATH}")
