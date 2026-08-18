from pathlib import Path

import pytest

from genwatch.modbus.registers import (
    ControlDef,
    RegisterDef,
    batch_reads,
    decode_value,
    load_register_map,
    validate_register_map,
)


@pytest.fixture(scope="module")
def regmap():
    return load_register_map(Path(__file__).parent.parent / "genwatch/registers/h100.yaml")


def test_loads_default_yaml(regmap):
    assert regmap.slave == 100
    assert regmap.read_fc == 3
    assert regmap.prime_poll_ms == 1500
    assert regmap.base_poll_ms == 15000
    assert len(regmap.registers) >= 15
    assert "remote_start" in regmap.controls
    assert "remote_stop" in regmap.controls
    # H-100 start/stop are FC16 multi-register writes at 0x019C
    assert regmap.controls["remote_start"].fc == 16
    assert regmap.controls["remote_start"].write_values == (0x0080, 0x0000, 0x0000)
    assert regmap.controls["remote_stop"].write_values == (0x0000, 0x0000, 0x0000)


def test_engine_state_bits_present(regmap):
    states = {rule.state for rule in regmap.engine_state_bits}
    assert {"running", "stopped", "cranking", "cooling", "exercising", "alarm"} <= states


def test_site_rating_and_tank_loaded(regmap):
    # On-site values for SITE-23 — a 600 kW genset with a 680 gal local
    # tank. rating_kw is the denominator for the dashboard load ring, so
    # it is pinned here: the YAML carried 350 for a while, which
    # understated the set's capacity and overstated every load figure by
    # ~1.7x.
    assert regmap.site.rating_kw == 600
    assert regmap.site.tank_gal == 680


def test_site_exercise_schedule_loaded(regmap):
    # The configured schedule. Pinned because it is operator-visible and
    # has no readback from the controller to correct it — the YAML said
    # Sunday on a unit that exercises Tuesday, and nothing caught it.
    assert regmap.site.exercise_enabled is True
    assert regmap.site.exercise_day == "tue"
    assert regmap.site.exercise_time == "03:00"


def test_site_fuel_type_loaded(regmap):
    # The on-disk YAML declares this site as a diesel install. UI uses
    # this to hide the O₂ sensor card (diesels have no O₂ probe).
    assert regmap.site.fuel_type == "diesel"


_MIN_YAML = (
    "modbus: { slave: 100, read_fc: 3 }\n"
    "engine_state_bits: []\n"
    "alarm_bits: []\n"
    "registers:\n"
    "  - { name: dummy, addr: 0x0080, fc: 3, type: u16, tier: prime, group: x, unit: bits }\n"
    "controls: []\n"
)


def test_site_fuel_type_defaults_to_unknown_when_absent(tmp_path):
    # Legacy YAMLs without the fuel_type key keep showing every metric.
    p = tmp_path / "legacy.yaml"
    p.write_text(
        "site: { id: X, name: Y, rating_kw: 200, engine: E, tank_gal: 100 }\n" + _MIN_YAML
    )
    rm = load_register_map(p)
    assert rm.site.fuel_type == "unknown"


def test_site_fuel_type_invalid_falls_back_to_unknown(tmp_path):
    # An operator typo (e.g. "natural_gas" instead of "gaseous") shouldn't
    # crash the loader — we accept lowercase-canonical or fall back.
    p = tmp_path / "typo.yaml"
    p.write_text(
        "site: { id: X, name: Y, rating_kw: 200, engine: E, tank_gal: 100, fuel_type: natural_gas }\n" + _MIN_YAML
    )
    rm = load_register_map(p)
    assert rm.site.fuel_type == "unknown"


def test_site_fuel_type_normalizes_case_and_whitespace(tmp_path):
    p = tmp_path / "mixed.yaml"
    p.write_text(
        "site: { id: X, name: Y, rating_kw: 200, engine: E, tank_gal: 100, fuel_type: '  Diesel  ' }\n" + _MIN_YAML
    )
    rm = load_register_map(p)
    assert rm.site.fuel_type == "diesel"


def test_alarm_bits_present(regmap):
    codes = {a.code for a in regmap.alarm_bits}
    assert "OVERCRANK" in codes
    assert "COOLANT_TEMP_HIGH_ALARM" in codes


def test_derive_engine_state_priority(regmap):
    # Alarm bit beats running bit.
    values = {
        "output_status_1": 0x8000 | 0x2000,  # Common Alarm + Generator Running
        "output_status_7": 0,
    }
    assert regmap.derive_engine_state(values) == "alarm"

    # Cranking wins over stopped.
    values = {"output_status_1": 0x0100, "output_status_7": 0x1000}
    assert regmap.derive_engine_state(values) == "cranking"

    # Cool-down.
    values = {"output_status_1": 0, "output_status_7": 0x2000}
    assert regmap.derive_engine_state(values) == "cooling"

    # No state bits → unknown.
    assert regmap.derive_engine_state({"output_status_1": 0, "output_status_7": 0}) == "unknown"


def test_derive_active_alarms(regmap):
    # Coolant Temp High Alarm bit in output_status_2.
    active = regmap.derive_active_alarms({"output_status_2": 0x1000})
    assert any(a.code == "COOLANT_TEMP_HIGH_ALARM" for a in active)
    # Nothing set → empty.
    assert regmap.derive_active_alarms({"output_status_2": 0}) == []


def test_register_addresses_are_unique(regmap):
    addrs = [r.addr for r in regmap.registers]
    assert len(addrs) == len(set(addrs)), "duplicate register addresses in yaml"


def test_control_addresses_distinct_from_reads(regmap):
    # The H-100 has a few legitimate dual-purpose registers (write to
    # trigger, read to check status). Whitelist those; flag any others.
    DUAL_PURPOSE = {0x022B, 0x012E}  # QUIETTEST_STATUS, ALARM_ACK
    read_addrs = {r.addr for r in regmap.registers}
    ctl_addrs = {c.addr for c in regmap.controls.values()}
    overlap = (read_addrs & ctl_addrs) - DUAL_PURPOSE
    assert not overlap, f"unexpected control/read overlap at: {[hex(a) for a in overlap]}"


@pytest.mark.parametrize(
    "type_,words,scale,expected",
    [
        ("u16", [1798], 1.0, 1798),
        ("u16", [139], 0.1, 13.9),
        ("s16", [0xFFFF], 1.0, -1),
        ("s16", [0x8000], 1.0, -32768),
        ("u32", [0, 18476], 0.1, 1847.6),
        ("u32", [1, 0], 1.0, 65536),
        # u32_lo reads only the low word. With a zero high word it matches
        # the u32 decode (safe migration); with a GARBAGE high word it
        # ignores it instead of blowing the value up by ~65536×.
        ("u32_lo", [0, 18476], 0.1, 1847.6),
        ("u32_lo", [0xDEAD, 188], 1.0, 188),
        ("u32", [0xDEAD, 188], 1.0, 0xDEAD0188),  # contrast: u32 honours the high word
        ("s32", [0xFFFF, 0xFFFF], 1.0, -1),
        ("bitfld", [0b1011], 1.0, 0b1011),
        ("enum", [3], 1.0, 3),
    ],
)
def test_decode_value(type_, words, scale, expected):
    r = RegisterDef(name="t", addr=0, type=type_, scale=scale)
    got = decode_value(r, words)
    assert got == pytest.approx(expected)


def test_batch_reads_coalesces_contiguous():
    regs = [
        RegisterDef("a", addr=0x10, type="u16"),
        RegisterDef("b", addr=0x11, type="u16"),
        RegisterDef("c", addr=0x12, type="u16"),
        # 4-word gap allowed
        RegisterDef("d", addr=0x16, type="u16"),
        RegisterDef("e", addr=0x30, type="u32"),  # 2 words
    ]
    batches = batch_reads(regs)
    # a-d coalesce despite the small gap; e is far away
    assert batches == [(0x10, 7), (0x30, 2)]


def test_batch_reads_respects_max_words():
    regs = [RegisterDef(name=f"r{i}", addr=0x100 + i, type="u16") for i in range(80)]
    batches = batch_reads(regs, max_words=64)
    # Should be split into at least two batches
    assert len(batches) >= 2
    for _, count in batches:
        assert count <= 64


def test_validate_register_map_detects_overlap_and_bad_fc(regmap):
    bad = regmap.registers[0]
    regmap.registers.append(RegisterDef(name="overlap", addr=bad.addr, type="u16", fc=3))
    regmap.controls["bad_ctl"] = ControlDef(name="bad_ctl", addr=0x200, fc=5, value=1)

    report = validate_register_map(regmap)
    assert not report.ok
    assert any("word overlap" in e for e in report.errors)
    assert any("unsupported write fc" in e for e in report.errors)


def test_validate_register_map_control_read_share_is_warning():
    # Dual-purpose H-100 registers (e.g. ack_alarm/exercise sharing addrs
    # with their status reads) should surface as warnings, not errors.
    # Load a fresh regmap — the module-scoped fixture gets mutated by
    # the test above.
    rm = load_register_map(Path(__file__).parent.parent / "genwatch/registers/h100.yaml")
    report = validate_register_map(rm)
    assert report.ok
    assert any("shares address" in w for w in report.warnings)


def test_coolant_level_decodes_in_tenths_of_a_percent():
    """Field-verified against the H-100 LCD: raw 772 is 77.2%, not 772%.

    This scale has been flipped once already on a plausible-sounding
    theory (genmon reads the same register raw), which put "772.0 %" on
    the dashboard, overran the coolant bar, and made the low-coolant
    indicator unreachable. Pin it to the panel reading so the next person
    reasoning from another codebase's decode has to argue with a test.
    """
    rm = load_register_map(Path(__file__).parent.parent / "genwatch/registers/h100.yaml")
    reg = rm.by_name("coolant_level")
    assert reg is not None
    assert reg.scale == 0.1

    # u32_lo takes the low word; 772 raw → 77.2%.
    assert decode_value(reg, [0x0000, 772]) == pytest.approx(77.2)
    # And a genuinely low tank stays low rather than being laundered into
    # a healthy-looking 95%.
    assert decode_value(reg, [0x0000, 95]) == pytest.approx(9.5)


def test_percentage_registers_decode_inside_their_unit():
    """Any register declared `unit: pct` should produce a plausible
    percentage from a mid-scale raw reading. Catches a scale that is off
    by a decade — the failure mode that put 772% on the dashboard."""
    rm = load_register_map(Path(__file__).parent.parent / "genwatch/registers/h100.yaml")
    for reg in rm.registers:
        if (reg.unit or "").lower() != "pct":
            continue
        # A raw reading near the top of the sensor's plausible range must
        # not decode above 100%.
        raw = 1000 if reg.scale == 0.1 else 100
        words = [0x0000, raw] if reg.words == 2 else [raw]
        value = decode_value(reg, words)
        assert 0 <= value <= 100, f"{reg.name}: raw {raw} decodes to {value}%"
