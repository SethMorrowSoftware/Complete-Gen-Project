"""Fuel-level monitoring: low-tank warnings and abnormal-drop detection.

Why this isn't the existing numeric-range mechanism
---------------------------------------------------
``registers.warn_range`` / ``alarm_range`` (see state.py::_check_numeric_ranges)
already turns an out-of-band reading into an alarm, and fuel level is a
number in a band, so the obvious question is why fuel needs its own code.
Four reasons, each of which independently rules that path out:

* **It only evaluates while the engine is producing** (``NUMERIC_ALARM_STATES``
  = running/exercising). Fuel level matters *most* when the generator is
  stopped — that's when you can still do something about it. A low-tank
  warning that can only fire once the outage has started is the wrong
  warning.
* **No hysteresis.** A tank on a running genset sloshes, and a sender
  hovering at the threshold would raise and clear repeatedly. The band
  check has a consecutive-poll debounce, which rides out a blip but not a
  reading that genuinely oscillates around the line.
* **No reminder.** An alarm bit fires once and stays raised. "You are at
  8%" is a condition somebody has to *act* on, so it needs to say so again
  in a few hours if nobody has.
* **One band, one severity.** Fuel wants two tiers with different urgency
  (and, in Slack, different mention behaviour): "order fuel this week" and
  "you have hours of runtime left".

Sensor trust
------------
``fuel_level_pct`` (0x0092) is not verified on every H-100 revision — the
neighbouring ``coolant_level`` register carries a standing note that the
panel's engineering unit may not be a true percent, and unconfigured
channels on this controller read 0xFFFF. So a reading outside
``min_valid_pct``..``max_valid_pct`` is treated as a *sensor fault* and
alerts are suppressed, rather than being read as an empty tank. A false
"FUEL EMPTY" page at 3 a.m. is how a site learns to ignore the alerts.

For the same reason the whole feature is opt-in (``fuel.enabled``): verify
the reading against the panel with ``genwatch panel`` first.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger("genwatch.fuel")

FuelStatus = Literal["ok", "warn", "critical", "unknown"]

# Engine states in which fuel is actively being burned. Used by the drop
# detector: a tank falling while the engine runs is the generator doing
# its job; the same fall while it is stopped is a leak, a siphon, or a
# sender fault, and is the version worth waking someone for.
BURNING_STATES = frozenset({"running", "exercising", "cranking", "cooling"})


def fmt_pct(pct: float) -> str:
    """Render a level for humans: one decimal only when it carries one.

    Rounding to whole percent makes a 9.5% reading print as "10%" — which,
    against a 10% threshold, reads like an off-by-one bug rather than an
    alert. Keep the tenth when it exists, drop it when it doesn't.
    """
    return f"{pct:.1f}%".replace(".0%", "%")


@dataclass
class FuelEvent:
    """Something worth telling an operator about."""

    kind: Literal["level", "reminder", "drop", "recovered"]
    status: FuelStatus
    pct: float
    severity: str                  # ok | warn | alarm
    message: str
    previous: FuelStatus = "unknown"
    dropped_pct: float = 0.0       # drop events only
    window_minutes: int = 0        # drop events only


@dataclass
class _Sample:
    ts: float
    pct: float


@dataclass
class FuelMonitor:
    """Tracks fuel level and decides when to speak.

    Pure decision logic — no database, no Slack, no clock of its own (the
    caller passes ``now``), so the thresholds, hysteresis, reminder timing
    and drop detection are all unit-testable without a running service.
    """

    cfg: object                    # FuelConfig
    tank_gal: int = 0
    fuel_type: str = "unknown"

    status: FuelStatus = "unknown"
    last_pct: float | None = None
    _notified_at: float = 0.0
    _history: deque[_Sample] = field(default_factory=lambda: deque(maxlen=2048))
    _last_drop_at: float = 0.0
    _sensor_fault_logged: bool = False

    def update_config(self, cfg, *, tank_gal: int | None = None, fuel_type: str | None = None) -> None:
        self.cfg = cfg
        if tank_gal is not None:
            self.tank_gal = tank_gal
        if fuel_type is not None:
            self.fuel_type = fuel_type

    # ── helpers ──────────────────────────────────────────────────────
    def is_applicable(self) -> bool:
        """Whether fuel monitoring means anything at this site.

        A gaseous (natural-gas) site has no local tank — the utility feeds
        it — so a "low fuel" alert there is nonsense. ``unknown`` is
        allowed through: an operator who enabled the feature explicitly
        gets the benefit of the doubt over a register-map field they may
        simply not have filled in.
        """
        return bool(getattr(self.cfg, "enabled", False)) and self.fuel_type != "gaseous"

    def gallons(self, pct: float) -> int | None:
        """Approximate gallons remaining, or None when the tank size is
        unknown. A percentage alone is hard to act on; gallons is what an
        operator repeats to the fuel supplier on the phone."""
        if self.tank_gal <= 0:
            return None
        return int(round(pct * self.tank_gal / 100.0))

    def _qty(self, pct: float) -> str:
        gal = self.gallons(pct)
        return fmt_pct(pct) + (f" (~{gal} gal)" if gal is not None else "")

    def _classify(self, pct: float) -> FuelStatus:
        """Threshold check WITH hysteresis against the current status.

        Falling uses the bare threshold; rising has to clear it by
        ``hysteresis_pct`` before we'll say the situation improved. So a
        sender oscillating around 25% warns once and then stays quiet,
        instead of alternating warn/clear every poll.
        """
        crit = float(self.cfg.critical_pct)
        warn = float(self.cfg.warn_pct)
        hyst = max(0.0, float(self.cfg.hysteresis_pct))

        if self.status == "critical":
            if pct >= crit + hyst:
                return "warn" if pct < warn + hyst else "ok"
            return "critical"
        if self.status == "warn":
            if pct <= crit:
                return "critical"
            return "ok" if pct >= warn + hyst else "warn"
        # ok / unknown — plain thresholds on the way down.
        if pct <= crit:
            return "critical"
        if pct <= warn:
            return "warn"
        return "ok"

    # ── main entry point ─────────────────────────────────────────────
    def update(
        self, *, pct: float | None, engine_state: str, now: float | None = None
    ) -> list[FuelEvent]:
        now = now if now is not None else time.time()
        if not self.is_applicable():
            return []

        # Sensor sanity. A missing (stale/evicted) or out-of-range reading
        # is a fault, not an empty tank — say nothing rather than page on
        # an unconfigured register.
        lo = float(self.cfg.min_valid_pct)
        hi = float(self.cfg.max_valid_pct)
        if pct is None or not (lo <= float(pct) <= hi):
            if pct is not None and not self._sensor_fault_logged:
                log.warning(
                    "fuel: ignoring implausible fuel_level_pct=%s (expected %.0f–%.0f). "
                    "Verify the register against the panel with `genwatch panel`; "
                    "fuel alerts are suppressed until it reads sanely.",
                    pct, lo, hi,
                )
                self._sensor_fault_logged = True
            return []
        self._sensor_fault_logged = False

        pct = float(pct)
        events: list[FuelEvent] = []
        previous = self.status
        self.last_pct = pct

        # ── Level crossings ──────────────────────────────────────────
        new_status = self._classify(pct)
        if new_status != previous:
            self.status = new_status
            self._notified_at = now
            if new_status == "critical":
                events.append(FuelEvent(
                    kind="level", status="critical", pct=pct, severity="alarm",
                    previous=previous,
                    message=f"Fuel CRITICAL — {self._qty(pct)} remaining",
                ))
            elif new_status == "warn":
                # Coming *up* into warn from critical is progress, not a
                # new problem; say so rather than crying wolf twice.
                improving = previous == "critical"
                events.append(FuelEvent(
                    kind="recovered" if improving else "level",
                    status="warn", pct=pct,
                    severity="ok" if improving else "warn", previous=previous,
                    message=(
                        f"Fuel rising — {self._qty(pct)} (was critical)" if improving
                        else f"Fuel LOW — {self._qty(pct)} remaining"
                    ),
                ))
            elif new_status == "ok" and previous in ("warn", "critical"):
                events.append(FuelEvent(
                    kind="recovered", status="ok", pct=pct, severity="ok",
                    previous=previous,
                    message=f"Fuel replenished — {self._qty(pct)}",
                ))
            # unknown → ok on first read is not an event: nothing happened,
            # we just started looking.
        elif new_status in ("warn", "critical"):
            # ── Reminder while still low ─────────────────────────────
            hours = float(getattr(self.cfg, "renotify_hours", 0) or 0)
            if hours > 0 and (now - self._notified_at) >= hours * 3600:
                self._notified_at = now
                events.append(FuelEvent(
                    kind="reminder", status=new_status, pct=pct,
                    severity="alarm" if new_status == "critical" else "warn",
                    previous=new_status,
                    message=(
                        f"Fuel still {'CRITICAL' if new_status == 'critical' else 'LOW'} — "
                        f"{self._qty(pct)} remaining"
                    ),
                ))

        # ── Abnormal drop ────────────────────────────────────────────
        events.extend(self._check_drop(pct, engine_state, now))
        return events

    def _check_drop(self, pct: float, engine_state: str, now: float) -> list[FuelEvent]:
        """Detect the tank falling faster than it should.

        The signal an operator actually wants here is theft or a leak, so
        by default this only looks while the engine is NOT burning fuel —
        a loaded generator drawing the tank down is expected, and alerting
        on it would fire on every outage. Sites that want the running
        case too (an unexpectedly high burn rate is its own problem) can
        set ``drop_only_when_stopped: false``.
        """
        threshold = float(getattr(self.cfg, "drop_alert_pct", 0) or 0)
        if threshold <= 0:
            return []
        window_min = max(1, int(getattr(self.cfg, "drop_window_minutes", 60)))
        window_s = window_min * 60.0

        self._history.append(_Sample(ts=now, pct=pct))
        cutoff = now - window_s
        while self._history and self._history[0].ts < cutoff:
            self._history.popleft()

        if getattr(self.cfg, "drop_only_when_stopped", True) and engine_state in BURNING_STATES:
            # Burning fuel is not losing fuel — but skipping the check is
            # not enough on its own. The comparison is against the highest
            # level in the window, so a normal exercise run (stopped at
            # 90%, runs 30 min, stops at 78%) would leave the pre-run 90%
            # sitting in the window and fire "dropped 12% while stopped"
            # the moment the engine stopped. Reset the baseline to the
            # current level so a comparison can never span a period when
            # consumption was legitimate.
            self._history.clear()
            self._history.append(_Sample(ts=now, pct=pct))
            return []
        if len(self._history) < 2:
            return []

        peak = max(s.pct for s in self._history)
        dropped = peak - pct
        if dropped < threshold:
            return []
        # One alert per window — otherwise a sustained leak re-fires on
        # every poll for as long as the peak stays inside the window.
        if self._last_drop_at and (now - self._last_drop_at) < window_s:
            return []
        self._last_drop_at = now
        return [FuelEvent(
            kind="drop", status=self.status, pct=pct, severity="alarm",
            previous=self.status, dropped_pct=round(dropped, 1),
            window_minutes=window_min,
            message=(
                f"Fuel dropped {dropped:.0f}% in under {window_min} min "
                f"while the engine was {engine_state} — now {self._qty(pct)}. "
                "Check for a leak or theft."
            ),
        )]
