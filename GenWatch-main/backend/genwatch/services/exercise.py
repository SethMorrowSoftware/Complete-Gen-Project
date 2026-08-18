"""Observed exercise schedule — inferred from the controller's own bit.

WHY THIS EXISTS
---------------
The exercise schedule shown on the dashboard used to come from one
place: the ``site.exercise`` block in ``registers/h100.yaml``, typed in
by hand at commissioning. Nothing ever checked it against the machine,
so when the schedule was changed at the panel the dashboard kept
cheerfully advertising the old day — which is exactly what happened
(chip said Sunday, the unit exercises Tuesday).

The H-100's Modbus map has no exercise-schedule register to read. What
it does have is the "Internal Exercise Active" status bit
(``output_status_7`` mask 0x0020), which ``engine_state_bits`` already
decodes into the ``exercising`` engine state, and which the state
machine already writes a TRANSITION event for on every rising edge. So
while the schedule can't be *read*, it can be *observed*: record when
the controller starts exercising itself, and the weekly pattern falls
out of the event log.

This is the same derive-it-from-the-event-stream approach the codebase
already uses for engine starts (no start-count register) and last
transfer (no ATS contact register).

WHAT IT IS NOT
--------------
This is an observation, not a readback. It reports what the unit has
been *doing*, which is the useful thing for spotting that the config
has drifted — but it cannot know about a schedule change until the
first exercise runs under the new setting. The configured value stays
in the payload alongside it so the UI can show both and flag a
mismatch.

If your H-100 firmware *does* expose the schedule in a register, that
would be strictly better than this. Find it with ``genwatch scan``
(dump a register range while the schedule is at a known setting, change
the setting at the panel, dump again, diff), then map it in the YAML.
"""
from __future__ import annotations

import time

# How far back to look. Ten weeks gives a weekly exerciser ~10 samples
# while staying inside a default retention window, and is short enough
# that a schedule changed a couple of months ago has aged out.
WINDOW_DAYS = 70

# An exercise that begins within this long after a confirmed operator
# Quiet-Test command is attributed to the operator, not to the
# controller's schedule. A technician's Thursday spot-check must not
# read as "this unit exercises Thursdays".
MANUAL_ATTRIBUTION_S = 15 * 60

# Two 'exercising' transitions closer together than this are the same
# run — the state can bounce (exercising → running → exercising) if the
# controller picks up load mid-test, and each bounce writes its own
# TRANSITION event.
SAME_RUN_GAP_S = 6 * 3600

# Report a schedule only once the same weekday has been seen this many
# times. One observation cannot be told apart from a one-off.
MIN_SAMPLES = 2

# Start times are recorded when the exercise-active bit is first polled,
# which trails the controller's scheduled start by the crank + poll
# interval. Snapping to a 5-minute grid turns "03:01" back into "03:00".
ROUND_TO_S = 300

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _collapse_runs(starts: list[float]) -> list[float]:
    """Keep the first timestamp of each distinct exercise run."""
    runs: list[float] = []
    for ts in starts:
        if not runs or ts - runs[-1] >= SAME_RUN_GAP_S:
            runs.append(ts)
    return runs


def _drop_operator_initiated(starts: list[float], commands: list[float]) -> list[float]:
    """Drop runs that began just after an operator Quiet-Test command."""
    if not commands:
        return starts
    return [
        ts for ts in starts
        if not any(0 <= ts - c <= MANUAL_ATTRIBUTION_S for c in commands)
    ]


def observed_schedule(db, *, now: float | None = None) -> dict | None:
    """Infer the schedule the controller actually runs, or None.

    Returns ``{"day", "time", "samples", "windowDays", "lastStartTs"}``
    where ``day`` is a short name matching the config vocabulary
    (mon..sun) and ``time`` is 24h local "HH:MM" — the same shapes the
    configured block uses, so the UI can compare them directly.

    None means "not enough evidence yet": a fresh install, a unit whose
    exercise is disabled, or a log holding only operator-run tests.
    """
    now = time.time() if now is None else now
    since = now - WINDOW_DAYS * 86400

    starts = _collapse_runs(db.exercise_starts_since(since))
    starts = _drop_operator_initiated(starts, db.operator_exercise_commands_since(since))
    if len(starts) < MIN_SAMPLES:
        return None

    # Snap each start to the 5-minute grid *before* splitting it into
    # weekday + time, so a 23:58 start that rounds up to 00:00 carries
    # its weekday over with it instead of being filed under the wrong day.
    buckets: dict[tuple[int, str], list[float]] = {}
    for ts in starts:
        lt = time.localtime(round(ts / ROUND_TO_S) * ROUND_TO_S)
        buckets.setdefault((lt.tm_wday, f"{lt.tm_hour:02d}:{lt.tm_min:02d}"), []).append(ts)

    # Modal weekday first, then the modal start time within it. Ties go
    # to the most recently seen, which tracks a schedule that was
    # changed part-way through the window.
    by_day: dict[int, list[float]] = {}
    for (wday, _), tss in buckets.items():
        by_day.setdefault(wday, []).extend(tss)
    wday = max(by_day, key=lambda d: (len(by_day[d]), max(by_day[d])))
    if len(by_day[wday]) < MIN_SAMPLES:
        return None

    hhmm = max(
        (k[1] for k in buckets if k[0] == wday),
        key=lambda t: (len(buckets[(wday, t)]), max(buckets[(wday, t)])),
    )

    return {
        "day": _WEEKDAYS[wday],
        "time": hhmm,
        "samples": len(by_day[wday]),
        "windowDays": WINDOW_DAYS,
        "lastStartTs": max(by_day[wday]),
    }
