"""Observed exercise schedule, inferred from the controller's own bit.

The bug this guards against is a silent one: the dashboard advertised a
Sunday exercise for a unit that exercises Tuesdays, because the schedule
lived only in hand-typed YAML and nothing ever checked it against the
machine. services/exercise.py infers the real schedule from TRANSITION
events into 'exercising' (the H-100's "Internal Exercise Active" bit).

What has to hold for an operator to trust that chip: a weekly pattern is
recognised despite the crank lag on each start; a technician's one-off
Quiet-Test does not masquerade as the schedule; a state bounce mid-run
does not count twice; and thin evidence reports nothing at all rather
than guessing.
"""
from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo

import pytest

from genwatch.db import Database
from genwatch.services import exercise as ex


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(tmp_path / "ex.sqlite")


def _at(now: float, *, weekday: int, hour: int, minute: int, weeks_ago: int,
        lag_s: float = 0.0, tz: dt.tzinfo | None = None) -> float:
    """Timestamp for the Nth-most-recent given weekday/time in `tz`.

    `lag_s` models the delay between the controller's scheduled start and
    GenWatch first polling the exercise-active bit (crank + poll cadence).
    """
    d = dt.datetime.fromtimestamp(now, tz).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    while d.weekday() != weekday:
        d -= dt.timedelta(days=1)
    return (d - dt.timedelta(weeks=weeks_ago)).timestamp() + lag_s


def _exercise_start(db: Database, ts: float) -> None:
    db.write_event(severity="ok", type_="TRANSITION",
                   message="Engine state: cranking → exercising", meta=None)
    _stamp(db, ts)


def _operator_exercise_cmd(db: Database, ts: float) -> None:
    db.write_event(severity="ok", type_="COMMAND",
                   message="Operator command exercise — confirmed", meta="tech")
    _stamp(db, ts)


def _stamp(db: Database, ts: float) -> None:
    """Backdate the row just written — write_event stamps time.time()."""
    with db._writer() as c:
        c.execute("UPDATE events SET ts = ? WHERE id = (SELECT MAX(id) FROM events)", (ts,))


TUESDAY, THURSDAY = 1, 3


def test_weekly_pattern_is_recovered_despite_crank_lag(db):
    """Six Tuesday 03:00 exercises read back as Tuesday 03:00.

    Each start is recorded a minute or so late — the bit is only seen
    once the engine is cranking and the next prime poll lands. Reporting
    "03:01" for a 03:00 schedule would look like a different setting
    than the one on the panel, so starts snap to a 5-minute grid.
    """
    now = time.time()
    for w in range(6):
        _exercise_start(db, _at(now, weekday=TUESDAY, hour=3, minute=0, weeks_ago=w, lag_s=71))

    got = ex.observed_schedule(db, now=now)
    assert got is not None, "six weekly exercises is ample evidence of a schedule"
    assert (got["day"], got["time"]) == ("tue", "03:00")
    assert got["samples"] == 6


def test_single_observation_reports_nothing(db):
    """One exercise is not a pattern — it could be anything."""
    now = time.time()
    _exercise_start(db, _at(now, weekday=TUESDAY, hour=3, minute=0, weeks_ago=0))
    assert ex.observed_schedule(db, now=now) is None


def test_empty_log_reports_nothing(db):
    """A fresh install shows the configured value, not a guess."""
    assert ex.observed_schedule(db, now=time.time()) is None


def test_operator_quiet_test_does_not_become_the_schedule(db):
    """A technician's Thursday spot-check is not evidence of Thursdays.

    Two manual Quiet-Tests on Thursday would otherwise out-vote nothing
    at all and get reported as the unit's schedule.
    """
    now = time.time()
    for w in range(2):
        t = _at(now, weekday=THURSDAY, hour=14, minute=0, weeks_ago=w)
        _operator_exercise_cmd(db, t - 30)
        _exercise_start(db, t)

    assert ex.observed_schedule(db, now=now) is None


def test_operator_test_does_not_outvote_the_real_schedule(db):
    """Manual runs are discounted, leaving the controller's own pattern."""
    now = time.time()
    for w in range(4):
        _exercise_start(db, _at(now, weekday=TUESDAY, hour=3, minute=0, weeks_ago=w, lag_s=64))
    for w in range(3):
        t = _at(now, weekday=THURSDAY, hour=14, minute=0, weeks_ago=w)
        _operator_exercise_cmd(db, t - 45)
        _exercise_start(db, t)

    got = ex.observed_schedule(db, now=now)
    assert got is not None
    assert got["day"] == "tue", "operator-run tests must not win the vote"
    assert got["samples"] == 4


def test_state_bounce_within_a_run_counts_once(db):
    """exercising → running → exercising is one exercise, not three.

    The controller can pick up load mid-test and drop back, and each
    edge writes its own TRANSITION event. Counting them separately would
    inflate `samples` into false confidence.
    """
    now = time.time()
    for w in range(2):
        base = _at(now, weekday=TUESDAY, hour=3, minute=0, weeks_ago=w)
        _exercise_start(db, base)
        _exercise_start(db, base + 240)
        _exercise_start(db, base + 600)

    got = ex.observed_schedule(db, now=now)
    assert got is not None
    assert got["samples"] == 2, "three edges in one run is still one exercise"
    assert got["time"] == "03:00"


def test_schedule_outside_the_window_is_ignored(db):
    """A schedule changed months ago must age out, not linger."""
    now = time.time()
    for w in range(6):
        _exercise_start(
            db,
            _at(now, weekday=TUESDAY, hour=3, minute=0,
                weeks_ago=w) - (ex.WINDOW_DAYS + 7) * 86400,
        )
    assert ex.observed_schedule(db, now=now) is None


def test_recent_pattern_wins_after_a_schedule_change(db):
    """Moving the exercise day is picked up once the new day has run twice.

    Both days sit in the window with equal counts; the tie goes to the
    more recently seen, which is the one now in effect.
    """
    now = time.time()
    for w in (6, 7):
        _exercise_start(db, _at(now, weekday=THURSDAY, hour=14, minute=0, weeks_ago=w))
    for w in (0, 1):
        _exercise_start(db, _at(now, weekday=TUESDAY, hour=3, minute=0, weeks_ago=w))

    got = ex.observed_schedule(db, now=now)
    assert got is not None
    assert got["day"] == "tue", "the schedule in effect now is the one to show"


# ─── Timezone ────────────────────────────────────────────────────────────
# The schedule typed into the YAML is read off the generator's panel, so
# it is a site-local wall time. The observed schedule has to be derived on
# that same clock or the two aren't comparable. The Raspberry Pi OS image
# ships set to UTC, so a monitor installed without setting the host zone
# reports every exercise shifted by the site's offset — and then flags a
# perfectly correct config as drifted.

CHICAGO = ZoneInfo("America/Chicago")


def test_observed_time_is_reported_on_the_site_clock(db):
    """A 10:00 Chicago exercise reads as 10:00, not as its UTC instant.

    This is the bug that made the feature look broken: on a UTC host the
    same events reported 15:00 (or 16:00 outside DST), which no operator
    could reconcile with the 10:00 on their panel.
    """
    now = time.time()
    for w in range(4):
        _exercise_start(db, _at(now, weekday=TUESDAY, hour=10, minute=0,
                                weeks_ago=w, lag_s=58, tz=CHICAGO))

    got = ex.observed_schedule(db, tz=CHICAGO, now=now)
    assert got is not None
    assert (got["day"], got["time"]) == ("tue", "10:00")


def test_same_events_shift_when_read_on_the_wrong_clock(db):
    """Pin the failure mode itself, so the fix can't silently regress.

    Identical events, read in UTC instead of the site's zone, land on a
    different wall time — that difference is exactly what an unset
    site.timezone costs, and why it is worth configuring.
    """
    now = time.time()
    for w in range(4):
        _exercise_start(db, _at(now, weekday=TUESDAY, hour=10, minute=0,
                                weeks_ago=w, tz=CHICAGO))

    on_site = ex.observed_schedule(db, tz=CHICAGO, now=now)
    on_utc = ex.observed_schedule(db, tz=dt.timezone.utc, now=now)
    assert on_site is not None and on_utc is not None
    assert on_site["time"] == "10:00"
    assert on_utc["time"] != on_site["time"], (
        "reading site-local events on the server's clock must shift them — "
        "if this ever matches, the test's fixture stopped exercising the bug"
    )


def test_schedule_survives_a_dst_changeover(db):
    """A 10:00 exercise stays 10:00 across the EST/EDT boundary.

    The observation window is 70 days, so in March and November it
    straddles a changeover: some runs are EST, some EDT, and their UTC
    instants differ by an hour. Bucketing on the *zone* keeps them all on
    10:00. Bucketing on a fixed offset would split them 50/50 across
    09:00 and 10:00, halving the sample count behind whichever won and
    potentially reporting the wrong hour outright.
    """
    ny = ZoneInfo("America/New_York")
    # Tuesdays either side of the 2026 spring-forward (Sun 2026-03-08).
    tuesdays = [dt.datetime(2026, 2, 17, 10, 0, tzinfo=ny),   # EST
                dt.datetime(2026, 2, 24, 10, 0, tzinfo=ny),   # EST
                dt.datetime(2026, 3, 10, 10, 0, tzinfo=ny),   # EDT
                dt.datetime(2026, 3, 17, 10, 0, tzinfo=ny)]   # EDT
    assert len({d.utcoffset() for d in tuesdays}) == 2, "fixture must span the change"
    for d in tuesdays:
        _exercise_start(db, d.timestamp() + 52)

    now = dt.datetime(2026, 3, 20, 12, 0, tzinfo=ny).timestamp()
    got = ex.observed_schedule(db, tz=ny, now=now)
    assert got is not None
    assert (got["day"], got["time"]) == ("tue", "10:00")
    assert got["samples"] == 4, "every run counts, on both sides of the change"


def test_unknown_timezone_falls_back_instead_of_crashing(db):
    """A typo'd zone must not take the monitor down.

    This is a display setting on a device whose job is to keep showing
    generator telemetry during an outage.
    """
    tz, name = ex.resolve_tz("Not/AZone")
    assert tz is not None
    assert isinstance(name, str) and name


def test_configured_timezone_is_resolved_by_name(db):
    tz, name = ex.resolve_tz("America/Chicago")
    assert name == "America/Chicago"
    assert dt.datetime.fromtimestamp(time.time(), tz).utcoffset() != dt.timedelta(0)


def test_reported_shape_matches_the_configured_vocabulary(db):
    """day/time must be directly comparable to the YAML block.

    The UI decides whether to warn about drift by comparing these two
    against each other, so 'tue' vs 'Tuesday' or '3:00' vs '03:00' would
    read as a mismatch on a unit that is perfectly in sync.
    """
    now = time.time()
    for w in range(3):
        _exercise_start(db, _at(now, weekday=TUESDAY, hour=3, minute=5, weeks_ago=w))

    got = ex.observed_schedule(db, now=now)
    assert got is not None
    assert got["day"] in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    assert got["time"] == "03:05"
    assert got["windowDays"] == ex.WINDOW_DAYS
    assert got["lastStartTs"] == pytest.approx(
        _at(now, weekday=TUESDAY, hour=3, minute=5, weeks_ago=0)
    )
