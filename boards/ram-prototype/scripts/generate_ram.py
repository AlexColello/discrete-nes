#!/usr/bin/env python3
"""
Generate hierarchical KiCad schematics for the 8-byte discrete RAM prototype
with full 2K-depth decoder tree for latency testing.

Circuit architecture (11-bit address, row/column):
  - A0-A6 -> 7-bit row decoder (full AND tree depth) -> ROW_SEL_0..3
  - A7-A10 -> 4-to-16 column decoder -> COL_SEL_0..15
  - Active-low control: /CE, /OE, /WE -> WRITE_ACTIVE, READ_EN
  - Row enables: WRITE_EN_ROW_i = AND(WRITE_ACTIVE, ROW_SEL_i)
                 READ_EN_ROW_i  = AND(READ_EN, ROW_SEL_i)
  - Per-byte local NAND gating:
      WRITE_CLK = NAND(COL_SEL, WRITE_EN_ROW)
      BUF_OE    = NAND(COL_SEL, READ_EN_ROW)
  - 8 bytes x 8 bits = 64 D flip-flops (74LVC1G79) + 64 tri-state buffers (74LVC1G125)
  - LED on EVERY gate output and stored bit

Produces:
  ram.kicad_sch              -- root sheet with connector + bus LEDs + hierarchy refs
  address_decoder.kicad_sch  -- 7 inverters + 10 ANDs (7-bit row decoder, full depth)
  column_select.kicad_sch    -- 4 inverters + 24 ANDs (4-to-16 column decoder)
  control_logic.kicad_sch    -- /CE,/OE,/WE inversion + WRITE_ACTIVE, READ_EN logic
  row_control.kicad_sch      -- 2 ANDs: WRITE_EN_ROW + READ_EN_ROW (shared by 4 row instances)
  byte.kicad_sch             -- 1 dual NAND (74LVC2G00) + 8 DFFs + 8 BUFs (shared by all 8 byte instances)
"""

import os
import sys

# Add shared library to path
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen import SchematicBuilder, snap, uid, GRID, SYM_SPACING_Y
from kicad_gen.symbols import get_pin_offsets

from kiutils.items.schitems import (
    HierarchicalSheet, HierarchicalPin,
    HierarchicalSheetProjectInstance, HierarchicalSheetProjectPath,
    SymbolProjectInstance, SymbolProjectPath,
)
from kiutils.items.common import (
    Position, Property, Effects, Font, Justify, Stroke, ColorRGBA,
)

# --------------------------------------------------------------
# Constants
# --------------------------------------------------------------
PROJECT_NAME = "ram"
BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

DFF_SPACING_Y = 13 * GRID    # vertical spacing between DFF rows in byte sheet
LED_GAP_X = 3 * GRID         # gap from output pin to LED chain center


# --------------------------------------------------------------
# Sub-sheet generators
# --------------------------------------------------------------

def generate_address_decoder():
    """
    Address decoder: 7-bit row decoder with full AND tree depth, 4 outputs.

    Inputs:  A0-A6 (7 address bits)
    Outputs: ROW_SEL_0..ROW_SEL_3 (row select lines)
             RS1..RS4 (intermediate AND tree probe outputs)

    Full 2K-depth AND tree (5 gate levels: INV→AND→AND→AND→AND):
      Level 1 (shared): RS1=AND(/A2,/A3), RS2=AND(/A4,/A5)
      Level 2a: RS3=AND(RS1,RS2)
      Level 2b: RS4=AND(RS3,/A6)
      Level 3 (per-output):
        P0=AND(/A1,/A0), ROW_SEL_0=AND(RS4,P0)
        P1=AND(/A1, A0), ROW_SEL_1=AND(RS4,P1)
        P2=AND( A1,/A0), ROW_SEL_2=AND(RS4,P2)
        P3=AND( A1, A0), ROW_SEL_3=AND(RS4,P3)

    7 INV + 10 AND = 17 ICs, 17 LEDs, 17 Rs

    Layout strategy (all wires stay within their stage's gap, never crossing
    into a different component column):
      - Inverter column at inv_x.  Inverted trunks start AFTER the LED chain
        R_Small right edge so they don't run through resistor bodies.
      - L1 AND column (RS1, RS2).  Output trunks in the l1→l2a gap.
      - L2a AND column (RS3 alone).  Output trunk in the l2a→l2b gap.
      - L2b AND column (RS4 alone).  Output trunk in the l2b→pair gap.
        Separating RS3 and RS4 into different X columns allows the RS3→RS4
        connection to be routed left-to-right (never back through the body)
        and eliminates pin-stub overlap errors.
      - Pair AND column (P0-P3).  Output trunks in the pair→final gap.
      - Final AND column (ROW_SEL 0-3).

    Label→inverter routing is done with L-shaped wires (staggered approach
    columns per bit).  A5's label is at the same Y as A2's inverter (Y=81.28),
    so A5 is routed with a short detour: stop before A2's approach column X,
    drop down to an intermediate Y between A2/A3 inverter bodies, then continue.

    True A0/A1 signals are branched off the label-approach wires using local
    labels ("A0_TRUE" / "A1_TRUE") so the routing stays clean without needing
    long horizontal wires that cross inverter bodies.

    Probe hier labels (RS1-RS4) are placed just to the right of each stage's
    LED column (within the stage's gap) so the probe wire never crosses into
    a downstream AND column.

    Key pin offsets (angle=0):
      74LVC1G04: pin2(in)=(-15.24,0), pin4(out)=(+12.70,0)
      74LVC1G08: pin1=(-15.24,-2.54), pin2=(-15.24,+2.54), pin4(out)=(+12.70,0)
      R_Small(90): pin1=(-2.54,0), pin2=(+2.54,0)  → centre at led_x+4*GRID
      LED chain right edge ≈ led_x + 4*GRID + 2.54
      → inverted trunks start at led_x + 6*GRID (clear of R right edge)
    """
    b = SchematicBuilder(title="Address Decoder", page_size="A2",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    # ----------------------------------------------------------------
    # Column X positions.  Each stage gets its own column so signals always
    # flow left→right between columns without back-routing through bodies.
    # Gaps between adjacent columns are ≥ 20*GRID.
    # ----------------------------------------------------------------
    inv_x       = snap(base_x + 20 * GRID)       # inverter centres
    l1_and_x    = snap(inv_x   + 30 * GRID)      # L1 ANDs: RS1, RS2
    l2a_and_x   = snap(l1_and_x + 22 * GRID)     # L2a AND: RS3
    l2b_and_x   = snap(l2a_and_x + 22 * GRID)    # L2b AND: RS4
    pair_and_x  = snap(l2b_and_x + 22 * GRID)    # L3 pair: P0-P3
    final_and_x = snap(pair_and_x + 22 * GRID)   # L3 final: ROW_SEL 0-3
    hl_out_x    = snap(final_and_x + 22 * GRID)  # output hier labels

    # Pin input X positions (same column, left of centre):
    #   inv input pin: inv_x - 15.24
    #   AND input pin: and_x - 15.24
    inv_pin_in_x  = snap(inv_x - 15.24)

    # ================================================================
    # Input hier labels + inverter stage
    # ================================================================

    # Input hier labels at base_x, 4*GRID apart vertically
    for i in range(7):
        hl_y = snap(base_y + i * 4 * GRID)
        b.add_hier_label(f"A{i}", base_x, hl_y, shape="input", justify="right")

    # Place 7 inverters, SYM_SPACING_Y (10*GRID) apart
    inv_in_pins  = []
    inv_out_pins = []
    for i in range(7):
        y = snap(base_y + i * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G04", inv_x, y)
        b.connect_power(pins)
        inv_in_pins.append(pins["2"])
        inv_out_pins.append(pins["4"])

    # Route hier label → inverter input using L-shaped wires with staggered
    # approach columns.  Each bit i uses approach_x = inv_pin_in_x - (9-i)*GRID,
    # giving unique X lanes.
    #
    # Special case A5: its label Y (81.28) coincides with A2's inverter Y (81.28).
    # A2's approach-to-pin horizontal wire runs at Y=81.28 from X=43.18 to 60.96.
    # A5's label wire would overlap that segment if drawn all the way to
    # approach_x[5]=50.8.  Fix: stop A5's label wire at X=42 (< 43.18), drop
    # down to detour_y=93.98 (midpoint between A2 inv body end=86.36 and
    # A3 inv body start=100.33), then continue to approach_x[5]=50.8.
    approach_xs = [snap(inv_pin_in_x - (9 - i) * GRID) for i in range(7)]

    # A5-specific detour constants
    A5_STOP_X    = snap(approach_xs[2] - GRID)  # stop before A2's approach col
    A5_DETOUR_Y  = snap(base_y + 3.5 * SYM_SPACING_Y)  # Y=118.11 — safe gap

    for i in range(7):
        hl_y   = snap(base_y + i * 4 * GRID)
        pin_in = inv_in_pins[i]
        ax     = approach_xs[i]
        if abs(hl_y - pin_in[1]) < 0.01:
            # A0: direct horizontal
            b.add_wire(base_x, hl_y, pin_in[0], pin_in[1])
        elif i == 5:
            # A5: detour to avoid A2's wire at Y=81.28
            b.add_wire(base_x, hl_y, A5_STOP_X, hl_y)
            b.add_wire(A5_STOP_X, hl_y, A5_STOP_X, A5_DETOUR_Y)
            b.add_wire(A5_STOP_X, A5_DETOUR_Y, ax, A5_DETOUR_Y)
            b.add_wire(ax, A5_DETOUR_Y, ax, pin_in[1])
            b.add_wire(ax, pin_in[1], pin_in[0], pin_in[1])
        else:
            # Standard L-shaped routing
            b.add_wire(base_x, hl_y,    ax,        hl_y)
            b.add_wire(ax,     hl_y,    ax,        pin_in[1])
            b.add_wire(ax,     pin_in[1], pin_in[0], pin_in[1])

    # Inverter output → LED → inverted trunk.
    # R_Small centre = inv_led_x + 4*GRID; R right edge ≈ inv_led_x + 6.54.
    # Trunk zone starts at inv_led_x + 6*GRID to clear R right edge.
    inv_out_x        = inv_out_pins[0][0]          # same for all 7 inverters
    inv_led_x        = snap(inv_out_x + 2 * GRID)
    inv_trunk_base_x = snap(inv_led_x + 6 * GRID)
    inv_trunk_x      = [snap(inv_trunk_base_x + i * GRID) for i in range(7)]

    for i in range(7):
        out = inv_out_pins[i]
        b.add_wire(out[0], out[1], inv_led_x, out[1])
        b.place_led_below(inv_led_x, out[1])
        b.add_wire(inv_led_x, out[1], inv_trunk_x[i], out[1])

    # True A0/A1 signals: local labels "A0" / "A1" at each pair AND input that
    # needs the true (non-inverted) signal.  Using the SAME name as the existing
    # hier_label avoids multiple_net_names ERC warnings and requires no extra
    # junction or source label on the input wires.

    # ================================================================
    # Level 1: RS1 = AND(/A2, /A3), RS2 = AND(/A4, /A5)
    # ================================================================
    # Place RS1 midway between /A2 and /A3 row Ys,
    #       RS2 midway between /A4 and /A5 row Ys.
    l1_y = [
        snap(base_y + 2.5 * SYM_SPACING_Y),  # RS1: midpoint of A2/A3 inverter Ys
        snap(base_y + 4.5 * SYM_SPACING_Y),  # RS2: midpoint of A4/A5 inverter Ys
    ]
    l1_pins = []
    for g in range(2):
        _, pins = b.place_symbol("74LVC1G08", l1_and_x, l1_y[g])
        b.connect_power(pins)
        l1_pins.append(pins)

    # RS1: pin1=/A2  pin2=/A3    RS2: pin1=/A4  pin2=/A5
    l1_input_bits = [(2, 3), (4, 5)]
    for g, (bit_a, bit_b) in enumerate(l1_input_bits):
        pa, pb = l1_pins[g]["1"], l1_pins[g]["2"]
        b.add_wire(inv_trunk_x[bit_a], pa[1], pa[0], pa[1])
        b.add_wire(inv_trunk_x[bit_b], pb[1], pb[0], pb[1])

    # Build inverted trunks /A2-/A5
    for i in [2, 3, 4, 5]:
        ys = [snap(inv_out_pins[i][1])]
        for g, (bit_a, bit_b) in enumerate(l1_input_bits):
            if bit_a == i:
                ys.append(snap(l1_pins[g]["1"][1]))
            if bit_b == i:
                ys.append(snap(l1_pins[g]["2"][1]))
        b.add_segmented_trunk(inv_trunk_x[i], sorted(set(ys)))

    # L1 output LEDs + RS1/RS2 trunks into the l1→l2a gap
    l1_out_x    = l1_pins[0]["4"][0]
    l1_led_x    = snap(l1_out_x + 2 * GRID)
    rs1_trunk_x = snap(l1_led_x + 6 * GRID)
    rs2_trunk_x = snap(l1_led_x + 7 * GRID)

    for g, tx in enumerate([rs1_trunk_x, rs2_trunk_x]):
        out = l1_pins[g]["4"]
        b.add_wire(out[0], out[1], l1_led_x, out[1])
        b.place_led_below(l1_led_x, out[1])
        b.add_wire(l1_led_x, out[1], tx, out[1])

    # RS1/RS2 probe labels — placed in the l1→l2a gap.
    # l1_probe_x must stop BEFORE rs2_trunk_x (= l1_led_x+7*GRID = 187.96) to avoid
    # overlapping the RS2→RS3 stub wire at Y=rs3_y+2.54=109.22.
    # Using 4*GRID (=180.34) keeps the probe wire well clear of the RS2 trunk.
    l1_probe_x = snap(l1_led_x + 4 * GRID)    # 180.34 < rs2_trunk_x=187.96 ✓
    for g, name in enumerate(["RS1", "RS2"]):
        out = l1_pins[g]["4"]
        probe_y = snap(out[1] + 6 * GRID)
        led_chain_bot = snap(out[1] + 3 * GRID)
        b.add_wire(l1_led_x, led_chain_bot, l1_led_x, probe_y)
        b.add_wire(l1_led_x, probe_y, l1_probe_x, probe_y)
        b.add_hier_label(name, l1_probe_x, probe_y, shape="output", justify="left")

    # ================================================================
    # Level 2a: RS3 = AND(RS1, RS2)
    # ================================================================
    rs3_y   = snap(base_y + 3 * SYM_SPACING_Y)   # row 3, near A2/A3 midpoint
    _, rs3_pins = b.place_symbol("74LVC1G08", l2a_and_x, rs3_y)
    b.connect_power(rs3_pins)

    rs3_pa, rs3_pb = rs3_pins["1"], rs3_pins["2"]

    # RS1 trunk → RS3 pin1
    b.add_wire(rs1_trunk_x, rs3_pa[1], rs3_pa[0], rs3_pa[1])
    b.add_segmented_trunk(rs1_trunk_x,
                          sorted({snap(l1_pins[0]["4"][1]), snap(rs3_pa[1])}))

    # RS2 trunk → RS3 pin2
    b.add_wire(rs2_trunk_x, rs3_pb[1], rs3_pb[0], rs3_pb[1])
    b.add_segmented_trunk(rs2_trunk_x,
                          sorted({snap(l1_pins[1]["4"][1]), snap(rs3_pb[1])}))

    # RS3 output → LED → RS3_trunk in l2a→l2b gap
    rs3_out     = rs3_pins["4"]
    rs3_led_x   = snap(rs3_out[0] + 2 * GRID)
    rs3_trunk_x = snap(rs3_led_x + 6 * GRID)
    b.add_wire(rs3_out[0], rs3_out[1], rs3_led_x, rs3_out[1])
    b.place_led_below(rs3_led_x, rs3_out[1])
    b.add_wire(rs3_led_x, rs3_out[1], rs3_trunk_x, rs3_out[1])

    # RS3 probe label — in the l2a→l2b gap
    rs3_probe_x = snap(rs3_led_x + 10 * GRID)
    rs3_probe_y = snap(rs3_out[1] + 6 * GRID)
    b.add_wire(rs3_led_x, snap(rs3_out[1] + 3 * GRID), rs3_led_x, rs3_probe_y)
    b.add_wire(rs3_led_x, rs3_probe_y, rs3_probe_x, rs3_probe_y)
    b.add_hier_label("RS3", rs3_probe_x, rs3_probe_y, shape="output", justify="left")

    # ================================================================
    # Level 2b: RS4 = AND(RS3, /A6)
    # RS4 is in its own column so RS3→RS4 goes left-to-right, avoiding
    # the pin-stub-overlap that would occur if they shared a column.
    # ================================================================
    rs4_y   = snap(base_y + 3 * SYM_SPACING_Y)   # same row as RS3
    _, rs4_pins = b.place_symbol("74LVC1G08", l2b_and_x, rs4_y)
    b.connect_power(rs4_pins)

    rs4_pa, rs4_pb = rs4_pins["1"], rs4_pins["2"]

    # RS3 trunk → RS4 pin1
    b.add_wire(rs3_trunk_x, rs4_pa[1], rs4_pa[0], rs4_pa[1])
    b.add_segmented_trunk(rs3_trunk_x,
                          sorted({snap(rs3_out[1]), snap(rs4_pa[1])}))

    # /A6 trunk → RS4 pin2.
    # The /A6 trunk is at inv_trunk_x[6], which is well to the left of l1_and_x.
    # The horizontal stub from inv_trunk_x[6] to rs4_pa[0] at Y=rs4_pb[1]
    # must not pass through l1_and, l2a_and bodies.
    # rs4_pb[1] = rs4_y + 2.54.  Check against l1 body Ys:
    #   RS1 body: l1_y[0] ± 5.08 = [88.9, 99.06]
    #   RS2 body: l1_y[1] ± 5.08 = [139.7, 149.86]
    # rs4_y = base_y + 3*SYM_SPACING_Y = 106.68 → rs4_pb[1] = 109.22.
    # 109.22 is NOT in [88.9, 99.06] or [139.7, 149.86] → safe to route directly.
    # l2a body: rs3_y ± 5.08 = [101.60, 111.76]. 109.22 IS within l2a body!
    # Route /A6 at rs4_pa[1] instead (Y = rs4_y - 2.54 = 104.14):
    #   l2a body Y range [101.60, 111.76] → 104.14 IS also within l2a body.
    # Use a detour: extend /A6 trunk down to rs4_pb[1], then horizontal stub
    # that starts at inv_trunk_x[6] (left of l1) and only spans to rs4_pb[0].
    # At Y=rs4_pb[1]=109.22, check each column body Y range:
    #   l1_and column bodies: RS1 at Y=93.98 → body [88.9,99.06]. 109.22 NOT in it.
    #                         RS2 at Y=144.78 → body [139.7,149.86]. NOT in it.
    #   l2a_and: RS3 at Y=106.68 → body [101.60,111.76]. 109.22 IS in it!
    # So the stub from inv_trunk_x[6] to rs4_pb[0] at Y=109.22 passes through l2a.
    # Fix: route /A6 below the l2a body.  Use Y = rs4_y + 7*GRID (below all and bodies).
    # But then we need an L to reach rs4_pb.  Use a relay:
    #   - Extend /A6 trunk to Y_relay = l2a_and_x right gap
    #   - Horizontal from inv_trunk_x[6] to a relay_x (right of l2a body) at safe_y
    #   - Then up/down to rs4_pb[1]
    # Safe Y for crossing l2a column: below its body (rs3_y+5.08=111.76), use 114.3.
    a6_relay_y  = snap(rs3_y + 4 * GRID)   # safely below l2a body (>111.76); 116.84 ≠ rs3_probe_y=121.92 ✓
    a6_relay_x  = snap(rs3_trunk_x + GRID) # in l2a→l2b gap, right of rs3_trunk
    inv6_out_y  = snap(inv_out_pins[6][1])
    # Extend /A6 trunk from inv output Y down to a6_relay_y
    b.add_segmented_trunk(inv_trunk_x[6],
                          sorted({inv6_out_y, a6_relay_y}))
    # Horizontal from /A6 trunk to relay X (in l2a→l2b gap) at safe Y
    b.add_wire(inv_trunk_x[6], a6_relay_y, a6_relay_x, a6_relay_y)
    # Relay vertical: from a6_relay_y up to rs4_pb[1]
    b.add_segmented_trunk(a6_relay_x,
                          sorted({a6_relay_y, snap(rs4_pb[1])}))
    # Horizontal stub from relay to pin
    b.add_wire(a6_relay_x, rs4_pb[1], rs4_pb[0], rs4_pb[1])

    # RS4 output → LED (no trunk needed; RS4 signal delivered via local labels)
    rs4_out   = rs4_pins["4"]
    rs4_led_x = snap(rs4_out[0] + 2 * GRID)
    b.add_wire(rs4_out[0], rs4_out[1], rs4_led_x, rs4_out[1])
    b.place_led_below(rs4_led_x, rs4_out[1])

    # RS4 probe label — in the l2b→pair gap
    rs4_probe_x = snap(rs4_led_x + 10 * GRID)
    rs4_probe_y = snap(rs4_out[1] + 6 * GRID)
    b.add_wire(rs4_led_x, snap(rs4_out[1] + 3 * GRID), rs4_led_x, rs4_probe_y)
    b.add_wire(rs4_led_x, rs4_probe_y, rs4_probe_x, rs4_probe_y)
    b.add_hier_label("RS4", rs4_probe_x, rs4_probe_y, shape="output", justify="left")

    # ================================================================
    # Level 3 pair: P0-P3 = AND(A1_variant, A0_variant)
    # ================================================================
    pair_decode = [
        (1, 1),  # P0 = AND(/A1, /A0)
        (1, 0),  # P1 = AND(/A1,  A0)
        (0, 1),  # P2 = AND( A1, /A0)
        (0, 0),  # P3 = AND( A1,  A0)
    ]
    pair_y_base = snap(base_y)
    pair_pins = []
    for p_idx in range(4):
        y = snap(pair_y_base + p_idx * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", pair_and_x, y)
        b.connect_power(pins)
        pair_pins.append(pins)

    # /A0, /A1 trunks: start at their inverter output Y, extend down to pair inputs
    inv0_ys = [snap(inv_out_pins[0][1])]
    inv1_ys = [snap(inv_out_pins[1][1])]

    for p_idx, (a1_inv, a0_inv) in enumerate(pair_decode):
        pa, pb = pair_pins[p_idx]["1"], pair_pins[p_idx]["2"]
        if a1_inv:
            b.add_wire(inv_trunk_x[1], pa[1], pa[0], pa[1])
            inv1_ys.append(snap(pa[1]))
        else:
            # A1 true: local label "A1" (same name as hier_label) at pair AND pin
            b.add_wire(pa[0], pa[1], snap(pa[0] - 4 * GRID), pa[1])
            b.add_label("A1", snap(pa[0] - 4 * GRID), pa[1])
        if a0_inv:
            b.add_wire(inv_trunk_x[0], pb[1], pb[0], pb[1])
            inv0_ys.append(snap(pb[1]))
        else:
            # A0 true: local label "A0" (same name as hier_label) at pair AND pin
            b.add_wire(pb[0], pb[1], snap(pb[0] - 4 * GRID), pb[1])
            b.add_label("A0", snap(pb[0] - 4 * GRID), pb[1])

    b.add_segmented_trunk(inv_trunk_x[0], sorted(set(inv0_ys)))
    b.add_segmented_trunk(inv_trunk_x[1], sorted(set(inv1_ys)))

    # Pair AND output LEDs + individual P0-P3 trunks → final AND column
    pair_out_x   = pair_pins[0]["4"][0]
    pair_led_x   = snap(pair_out_x + 2 * GRID)
    pair_trunk_x = [snap(pair_led_x + 6 * GRID + p * GRID) for p in range(4)]

    for p_idx in range(4):
        out = pair_pins[p_idx]["4"]
        b.add_wire(out[0], out[1], pair_led_x, out[1])
        b.place_led_below(pair_led_x, out[1])
        b.add_wire(pair_led_x, out[1], pair_trunk_x[p_idx], out[1])

    # ================================================================
    # Level 3 final: ROW_SEL_i = AND(RS4, P_i)
    # ================================================================
    final_pins = []
    for sel_idx in range(4):
        y = snap(pair_y_base + sel_idx * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", final_and_x, y)
        b.connect_power(pins)
        final_pins.append(pins)

    # RS4 → final AND pin1: local label "RS4" (same name as hier_label) avoids
    # routing a relay wire across pair AND column and prevents multiple_net_names.
    for sel_idx in range(4):
        pa = final_pins[sel_idx]["1"]
        label_x = snap(pa[0] - 4 * GRID)
        b.add_wire(pa[0], pa[1], label_x, pa[1])
        b.add_label("RS4", label_x, pa[1])

    # P_i trunks → final AND pin2 (approach from the LEFT so no stub overlap)
    for p_idx in range(4):
        pair_out_y = snap(pair_pins[p_idx]["4"][1])
        final_pb   = final_pins[p_idx]["2"]
        # final AND pin2 is at final_and_x - 15.24 (left of AND body).
        # pair_trunk_x[p_idx] is in the pair→final gap (left of final_and pin X).
        # Stub goes from trunk X to pin X → left-to-right → correct direction.
        b.add_wire(pair_trunk_x[p_idx], final_pb[1], final_pb[0], final_pb[1])
        b.add_segmented_trunk(pair_trunk_x[p_idx],
                              sorted({pair_out_y, snap(final_pb[1])}))

    # ================================================================
    # ROW_SEL outputs: final AND output → LED → hier label
    # ================================================================
    for sel_idx in range(4):
        out = final_pins[sel_idx]["4"]
        led_x = snap(out[0] + 2 * GRID)
        b.add_wire(out[0], out[1], led_x, out[1])
        b.place_led_below(led_x, out[1])
        b.add_wire(led_x, out[1], hl_out_x, out[1])
        b.add_hier_label(f"ROW_SEL_{sel_idx}", hl_out_x, out[1],
                         shape="output", justify="left")

    return b


def generate_column_select():
    """
    Column select: full 4-to-16 decoder using inverters and 2-level AND tree.

    Inputs:  A7, A8, A9, A10 (4 address bits)
    Outputs: COL_SEL_0..COL_SEL_15 (16 column select lines)
             GA0..GA3 (group A intermediates, probe outputs)
             GB0..GB3 (group B intermediates, probe outputs)

    Level 1 — group A (A7, A8): GA0=AND(/A8,/A7), GA1=AND(/A8,A7),
              GA2=AND(A8,/A7), GA3=AND(A8,A7)
    Level 1 — group B (A9, A10): GB0=AND(/A10,/A9), GB1=AND(/A10,A9),
              GB2=AND(A10,/A9), GB3=AND(A10,A9)
    Level 2: COL_SEL_n = AND(GB[n>>2], GA[n&3])

    4 INV + 8 level-1 AND + 16 level-2 AND = 28 ICs, 28 LEDs, 28 Rs

    Routing strategy — all inter-stage connections use local labels to avoid
    long routing wires that would cross component bodies:

    1. True signals (A7-A10 true): local labels "A7_TRUE".."A10_TRUE" branched
       from the label-to-inverter approach wires.  Avoids routing through
       inverter bodies.

    2. GA0-GA3, GB0-GB3 → L2 AND inputs: local labels "GA0".."GB3".
       Avoids routing trunks that would cross LED chain R_Small bodies
       (R body X=[168.4,171.96]) or land at pin1_x=182.88 (zero-length stub)
       or right of pin1_x (pin-stub-overlap).

    3. Inverted signals (/A7-/A10): conventional segmented trunks in the gap
       [inv_trunk_base_x, l1_pin_x] = [104.14, 127.0] — no bodies in this gap.

    Page size A1 (landscape) to accommodate 16 L2 ANDs (span ~381mm vertical).

    Key coordinates (computed from base_x=25.4, GRID=2.54):
      inv_x=71.12, inv_in_x=55.88, inv_out_x=83.82, inv_led_x=88.9
      inv_trunk_x=[104.14,106.68,109.22,111.76]  (in gap before l1_pin_x=127)
      l1_and_x=142.24, l1_out_x=154.94, l1_led_x=160.02
      l2_and_x=198.12, l2_pin1_x=182.88, hl_out_x=254.0
    """
    b = SchematicBuilder(title="Column Select", page_size="A1",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    inv_x    = snap(base_x + 18 * GRID)
    l1_and_x = snap(inv_x   + 28 * GRID)
    l2_and_x = snap(l1_and_x + 22 * GRID)
    hl_out_x = snap(l2_and_x + 22 * GRID)

    inv_in_x = snap(inv_x - 15.24)

    # ================================================================
    # Input hier labels (A7-A10) + inverters + inverted trunks
    # ================================================================
    addr_names = ["A7", "A8", "A9", "A10"]
    for i, name in enumerate(addr_names):
        hl_y = snap(base_y + i * 4 * GRID)
        b.add_hier_label(name, base_x, hl_y, shape="input", justify="right")

    inv_in_pins  = []
    inv_out_pins = []
    for i in range(4):
        y = snap(base_y + i * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G04", inv_x, y)
        b.connect_power(pins)
        inv_in_pins.append(pins["2"])
        inv_out_pins.append(pins["4"])

    # Approach columns: staggered X lanes for label → inverter L-shaped routing.
    # All approach wires stay strictly left of the inverter body left edge (X=63.5).
    approach_xs = [snap(inv_in_x - (6 - i) * GRID) for i in range(4)]

    for i in range(4):
        hl_y   = snap(base_y + i * 4 * GRID)
        pin_in = inv_in_pins[i]
        ax     = approach_xs[i]
        if abs(hl_y - pin_in[1]) < 0.01:
            # A7: direct horizontal.
            b.add_wire(base_x, hl_y, pin_in[0], pin_in[1])
        else:
            # A8, A9, A10: L-shaped route.
            b.add_wire(base_x, hl_y, ax, hl_y)
            b.add_wire(ax, hl_y, ax, pin_in[1])
            b.add_wire(ax, pin_in[1], pin_in[0], pin_in[1])

    # Inverter output → LED → inverted trunks (in gap before l1_and_x).
    # R_Small right edge ≈ inv_led_x + 6.54 → trunks start at inv_led_x + 6*GRID.
    inv_out_x        = snap(inv_x + 12.70)
    inv_led_x        = snap(inv_out_x + 2 * GRID)
    inv_trunk_base_x = snap(inv_led_x + 6 * GRID)
    inv_trunk_x      = [snap(inv_trunk_base_x + i * GRID) for i in range(4)]

    for i in range(4):
        out = inv_out_pins[i]
        b.add_wire(out[0], out[1], inv_led_x, out[1])
        b.place_led_below(inv_led_x, out[1])
        b.add_wire(inv_led_x, out[1], inv_trunk_x[i], out[1])

    # ================================================================
    # Level 1 Group A: GA0-GA3  (A7/A8 combinations)
    # GA0=AND(/A8,/A7)  GA1=AND(/A8,A7)  GA2=AND(A8,/A7)  GA3=AND(A8,A7)
    # Decode: (A8_inv, A7_inv)
    # ================================================================
    ga_decode = [(1, 1), (1, 0), (0, 1), (0, 0)]
    ga_pins   = []
    ga_y_base = snap(base_y)
    for g in range(4):
        y = snap(ga_y_base + g * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", l1_and_x, y)
        b.connect_power(pins)
        ga_pins.append(pins)

    inv_target_ys = {i: [snap(inv_out_pins[i][1])] for i in range(4)}

    for g, (a8_inv, a7_inv) in enumerate(ga_decode):
        pa = ga_pins[g]["1"]  # A8 variant → pin 1 (upper)
        pb = ga_pins[g]["2"]  # A7 variant → pin 2 (lower)
        # A8 index 1 (/A8 = inv_trunk_x[1])
        if a8_inv:
            b.add_wire(inv_trunk_x[1], pa[1], pa[0], pa[1])
            inv_target_ys[1].append(snap(pa[1]))
        else:
            label_x = snap(pa[0] - 4 * GRID)
            b.add_wire(pa[0], pa[1], label_x, pa[1])
            b.add_label("A8", label_x, pa[1])
        # A7 index 0 (/A7 = inv_trunk_x[0])
        if a7_inv:
            b.add_wire(inv_trunk_x[0], pb[1], pb[0], pb[1])
            inv_target_ys[0].append(snap(pb[1]))
        else:
            label_x = snap(pb[0] - 4 * GRID)
            b.add_wire(pb[0], pb[1], label_x, pb[1])
            b.add_label("A7", label_x, pb[1])

    # ================================================================
    # Level 1 Group B: GB0-GB3  (A9/A10 combinations)
    # GB0=AND(/A10,/A9)  GB1=AND(/A10,A9)  GB2=AND(A10,/A9)  GB3=AND(A10,A9)
    # Decode: (A10_inv, A9_inv)
    # ================================================================
    gb_decode = [(1, 1), (1, 0), (0, 1), (0, 0)]
    gb_pins   = []
    gb_y_base = snap(ga_y_base + 5 * SYM_SPACING_Y)  # rows 5-8, below group A
    for g in range(4):
        y = snap(gb_y_base + g * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", l1_and_x, y)
        b.connect_power(pins)
        gb_pins.append(pins)

    for g, (a10_inv, a9_inv) in enumerate(gb_decode):
        pa = gb_pins[g]["1"]  # A10 variant
        pb = gb_pins[g]["2"]  # A9 variant
        # A10 index 3
        if a10_inv:
            b.add_wire(inv_trunk_x[3], pa[1], pa[0], pa[1])
            inv_target_ys[3].append(snap(pa[1]))
        else:
            label_x = snap(pa[0] - 4 * GRID)
            b.add_wire(pa[0], pa[1], label_x, pa[1])
            b.add_label("A10", label_x, pa[1])
        # A9 index 2
        if a9_inv:
            b.add_wire(inv_trunk_x[2], pb[1], pb[0], pb[1])
            inv_target_ys[2].append(snap(pb[1]))
        else:
            label_x = snap(pb[0] - 4 * GRID)
            b.add_wire(pb[0], pb[1], label_x, pb[1])
            b.add_label("A9", label_x, pb[1])

    # Build inverted signal trunks /A7-/A10
    for i in range(4):
        b.add_segmented_trunk(inv_trunk_x[i], sorted(set(inv_target_ys[i])))

    # ================================================================
    # GA/GB LED outputs + local labels GA0-GA3, GB0-GB3
    # Using local labels avoids routing trunks across the LED chain R bodies
    # (R body X=[168.4,171.96]) and the pin-stub-overlap problem at l2_pin1_x.
    # ================================================================
    l1_out_x = snap(l1_and_x + 12.70)
    l1_led_x = snap(l1_out_x + 2 * GRID)

    for g in range(4):
        out = ga_pins[g]["4"]
        b.add_wire(out[0], out[1], l1_led_x, out[1])
        b.place_led_below(l1_led_x, out[1])
        # Local label just right of LED junction for GA signal fan-out
        label_x = snap(l1_led_x + GRID)
        b.add_wire(l1_led_x, out[1], label_x, out[1])
        b.add_label(f"GA{g}", label_x, out[1])

    for g in range(4):
        out = gb_pins[g]["4"]
        b.add_wire(out[0], out[1], l1_led_x, out[1])
        b.place_led_below(l1_led_x, out[1])
        label_x = snap(l1_led_x + GRID)
        b.add_wire(l1_led_x, out[1], label_x, out[1])
        b.add_label(f"GB{g}", label_x, out[1])

    # Probe hier labels (GA/GB intermediates) — short probe wires below LED chain
    for g in range(4):
        out  = ga_pins[g]["4"]
        p_y  = snap(out[1] + 6 * GRID)
        b.add_wire(l1_led_x, snap(out[1] + 3 * GRID), l1_led_x, p_y)
        b.add_wire(l1_led_x, p_y, hl_out_x, p_y)
        b.add_hier_label(f"GA{g}", hl_out_x, p_y, shape="output", justify="left")

    for g in range(4):
        out  = gb_pins[g]["4"]
        p_y  = snap(out[1] + 6 * GRID)
        b.add_wire(l1_led_x, snap(out[1] + 3 * GRID), l1_led_x, p_y)
        b.add_wire(l1_led_x, p_y, hl_out_x, p_y)
        b.add_hier_label(f"GB{g}", hl_out_x, p_y, shape="output", justify="left")

    # ================================================================
    # Level 2: 16 output ANDs — COL_SEL_n = AND(GB[n>>2], GA[n&3])
    # Both inputs use local labels: pin1 ← GB[n>>2], pin2 ← GA[n&3]
    # ================================================================
    l2_y_base    = snap(base_y)
    l2_pins_list = []
    for n in range(16):
        y = snap(l2_y_base + n * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", l2_and_x, y)
        b.connect_power(pins)
        l2_pins_list.append(pins)

    for n in range(16):
        gb_idx = n >> 2
        ga_idx = n & 3
        pa = l2_pins_list[n]["1"]   # GB input (pin 1)
        pb = l2_pins_list[n]["2"]   # GA input (pin 2)
        # Approach from left: stub goes left of pin so stub overlap NOT triggered
        lx_gb = snap(pa[0] - 4 * GRID)
        lx_ga = snap(pb[0] - 4 * GRID)
        b.add_wire(pa[0], pa[1], lx_gb, pa[1])
        b.add_label(f"GB{gb_idx}", lx_gb, pa[1])
        b.add_wire(pb[0], pb[1], lx_ga, pb[1])
        b.add_label(f"GA{ga_idx}", lx_ga, pb[1])

    # COL_SEL outputs: L2 AND output → LED → hier label
    for n in range(16):
        out        = l2_pins_list[n]["4"]
        led_jct_x  = snap(out[0] + 2 * GRID)
        b.add_wire(out[0], out[1], led_jct_x, out[1])
        b.add_wire(led_jct_x, out[1], hl_out_x, out[1])
        b.place_led_below(led_jct_x, out[1])
        b.add_hier_label(f"COL_SEL_{n}", hl_out_x, out[1],
                         shape="output", justify="left")

    return b


def generate_control_logic():
    """
    Control logic: active-low to active-high + combined signals.

    Inputs:  /CE, /OE, /WE
    Outputs: WRITE_ACTIVE = CE & WE
             READ_EN = CE & OE & /WE

    Uses:
      3x 74LVC1G04 (invert /CE->CE, /OE->OE, /WE->WE)
      1x 74LVC1G08 AND(CE, WE) -> WRITE_ACTIVE
      1x 74LVC1G08 AND(CE, OE) -> CE_AND_OE
      1x 74LVC1G08 AND(CE_AND_OE, /WE) -> READ_EN

    All connections are direct wires -- no local labels.
    """
    b = SchematicBuilder(title="Control Logic", page_size="A3",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    inv_x = base_x + 15 * GRID
    and1_x = base_x + 42 * GRID
    and2_x = base_x + 42 * GRID
    and3_x = base_x + 62 * GRID

    ce_y = base_y
    oe_y = base_y + SYM_SPACING_Y
    we_y = base_y + 2 * SYM_SPACING_Y

    and1_y = base_y + 4 * GRID
    and2_y = base_y + 14 * GRID
    and3_y = and2_y

    # -- Hier labels for inputs -> wire to inverter inputs --
    b.add_hier_label("nCE", base_x, ce_y, shape="input", justify="right")
    _, ce_inv_pins = b.place_symbol("74LVC1G04", inv_x, ce_y)
    b.connect_power(ce_inv_pins)
    ce_inv_in = ce_inv_pins["2"]
    b.add_wire(base_x, ce_y, ce_inv_in[0], ce_inv_in[1])

    b.add_hier_label("nOE", base_x, oe_y, shape="input", justify="right")
    _, oe_inv_pins = b.place_symbol("74LVC1G04", inv_x, oe_y)
    b.connect_power(oe_inv_pins)
    oe_inv_in = oe_inv_pins["2"]
    b.add_wire(base_x, oe_y, oe_inv_in[0], oe_inv_in[1])

    nwe_jct_x = base_x + 8 * GRID
    b.add_hier_label("nWE", base_x, we_y, shape="input", justify="right")
    _, we_inv_pins = b.place_symbol("74LVC1G04", inv_x, we_y)
    b.connect_power(we_inv_pins)
    we_inv_in = we_inv_pins["2"]

    ce_out = ce_inv_pins["4"]
    oe_out = oe_inv_pins["4"]
    we_out = we_inv_pins["4"]

    # -- AND1: CE & WE -> WRITE_ACTIVE --
    _, and1_pins = b.place_symbol("74LVC1G08", and1_x, and1_y)
    b.connect_power(and1_pins)
    and1_a = and1_pins["1"]
    and1_b = and1_pins["2"]
    and1_out = and1_pins["4"]

    # -- AND2: CE & OE -> CE_AND_OE --
    _, and2_pins = b.place_symbol("74LVC1G08", and2_x, and2_y)
    b.connect_power(and2_pins)
    and2_a = and2_pins["1"]
    and2_b = and2_pins["2"]
    and2_out = and2_pins["4"]

    # -- AND3: CE_AND_OE & /WE -> READ_EN --
    _, and3_pins = b.place_symbol("74LVC1G08", and3_x, and3_y)
    b.connect_power(and3_pins)
    and3_a = and3_pins["1"]
    and3_b = and3_pins["2"]
    and3_out = and3_pins["4"]

    # -- Wire CE output to AND1.A and AND2.A --
    ce_led_x = snap(ce_out[0] + 2 * GRID)
    ce_trunk_x = snap(ce_out[0] + 9 * GRID)
    b.add_wire(ce_out[0], ce_out[1], ce_led_x, ce_out[1])
    b.add_wire(ce_led_x, ce_out[1], ce_trunk_x, ce_out[1])
    b.place_led_below(ce_led_x, ce_out[1])
    b.add_segmented_trunk(ce_trunk_x, [ce_out[1], and1_a[1], and2_a[1]])
    b.add_wire(ce_trunk_x, and1_a[1], and1_a[0], and1_a[1])
    b.add_wire(ce_trunk_x, and2_a[1], and2_a[0], and2_a[1])

    # -- Wire OE output to AND2.B --
    oe_led_x = snap(oe_out[0] + 2 * GRID)
    oe_vert_x = snap(ce_trunk_x - GRID)
    b.add_wire(oe_out[0], oe_out[1], oe_led_x, oe_out[1])
    b.add_wire(oe_led_x, oe_out[1], oe_vert_x, oe_out[1])
    b.add_wire(oe_vert_x, oe_out[1], oe_vert_x, and2_b[1])
    b.add_wire(oe_vert_x, and2_b[1], and2_b[0], and2_b[1])
    b.place_led_below(oe_led_x, oe_out[1])

    # -- Wire WE output to AND1.B --
    we_led_x = snap(we_out[0] + 2 * GRID)
    we_vert_x = snap(and1_b[0] - 2 * GRID)
    b.add_wire(we_out[0], we_out[1], we_led_x, we_out[1])
    b.add_wire(we_led_x, we_out[1], we_vert_x, we_out[1])
    b.add_wire(we_vert_x, we_out[1], we_vert_x, and1_b[1])
    b.add_wire(we_vert_x, and1_b[1], and1_b[0], and1_b[1])
    b.place_led_below(we_led_x, we_out[1])

    # -- Wire CE_AND_OE (AND2 output) to AND3.A with LED --
    and2_led_x = snap(and2_out[0] + 2 * GRID)
    b.add_wire(and2_out[0], and2_out[1], and2_led_x, and2_out[1])
    b.add_wire(and2_led_x, and2_out[1], and3_a[0], and2_out[1])
    b.add_wire(and3_a[0], and2_out[1], and3_a[0], and3_a[1])
    b.place_led_below(and2_led_x, and2_out[1])

    # -- Wire /WE to AND3.B --
    nwe_route_y = snap(we_y + 7 * GRID)
    b.add_wire(base_x, we_y, nwe_jct_x, we_y)
    b.add_wire(nwe_jct_x, we_y, we_inv_in[0], we_inv_in[1])
    b.add_wire(nwe_jct_x, we_y, nwe_jct_x, nwe_route_y)
    b.add_wire(nwe_jct_x, nwe_route_y, and3_b[0], nwe_route_y)
    b.add_wire(and3_b[0], nwe_route_y, and3_b[0], and3_b[1])
    b.add_junction(nwe_jct_x, we_y)

    # -- AND1 output -> LED T-junction -> hier label WRITE_ACTIVE --
    out_label_x = and3_x + 22 * GRID
    and1_led_x = snap(and1_out[0] + 2 * GRID)
    b.add_wire(and1_out[0], and1_out[1], and1_led_x, and1_out[1])
    b.add_wire(and1_led_x, and1_out[1], out_label_x, and1_out[1])
    b.place_led_below(and1_led_x, and1_out[1])
    b.add_hier_label("WRITE_ACTIVE", out_label_x, and1_out[1], shape="output", justify="left")

    # -- AND3 output -> LED T-junction -> hier label READ_EN --
    and3_led_x = snap(and3_out[0] + 2 * GRID)
    b.add_wire(and3_out[0], and3_out[1], and3_led_x, and3_out[1])
    b.add_wire(and3_led_x, and3_out[1], out_label_x, and3_out[1])
    b.place_led_below(and3_led_x, and3_out[1])
    b.add_hier_label("READ_EN", out_label_x, and3_out[1], shape="output", justify="left")

    return b


def generate_row_control():
    """Row control: 2 ANDs per row.

    Inputs:  WRITE_ACTIVE, READ_EN, ROW_SEL
    Outputs: WRITE_EN_ROW = AND(WRITE_ACTIVE, ROW_SEL)
             READ_EN_ROW  = AND(READ_EN, ROW_SEL)

    Shared by all 4 row instances (like byte.kicad_sch is shared by 8).
    """
    b = SchematicBuilder(title="Row Control", page_size="A3",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    and_x = base_x + 18 * GRID
    hl_out_x = and_x + 22 * GRID

    # -- Hierarchical input labels --
    row_sel_y = snap(base_y)
    wa_y = snap(base_y + SYM_SPACING_Y)
    re_y = snap(base_y + 2 * SYM_SPACING_Y)

    b.add_hier_label("ROW_SEL", base_x, row_sel_y,
                     shape="input", justify="right")
    b.add_hier_label("WRITE_ACTIVE", base_x, wa_y,
                     shape="input", justify="right")
    b.add_hier_label("READ_EN", base_x, re_y,
                     shape="input", justify="right")

    # -- AND1: WRITE_EN_ROW = AND(WRITE_ACTIVE, ROW_SEL) --
    and1_y = snap(base_y + 4 * GRID)
    _, and1_pins = b.place_symbol("74LVC1G08", and_x, and1_y)
    b.connect_power(and1_pins)
    and1_a = and1_pins["1"]  # WRITE_ACTIVE
    and1_b = and1_pins["2"]  # ROW_SEL
    and1_out = and1_pins["4"]

    # -- AND2: READ_EN_ROW = AND(READ_EN, ROW_SEL) --
    and2_y = snap(and1_y + SYM_SPACING_Y)
    _, and2_pins = b.place_symbol("74LVC1G08", and_x, and2_y)
    b.connect_power(and2_pins)
    and2_a = and2_pins["1"]  # READ_EN
    and2_b = and2_pins["2"]  # ROW_SEL
    and2_out = and2_pins["4"]

    # -- Wire ROW_SEL to both AND pin B via trunk --
    row_trunk_x = snap(and1_b[0] - 3 * GRID)
    b.add_wire(base_x, row_sel_y, row_trunk_x, row_sel_y)
    b.add_segmented_trunk(row_trunk_x,
                          [row_sel_y, and1_b[1], and2_b[1]])
    b.add_wire(row_trunk_x, and1_b[1], and1_b[0], and1_b[1])
    b.add_wire(row_trunk_x, and2_b[1], and2_b[0], and2_b[1])

    # -- Wire WRITE_ACTIVE to AND1 pin A --
    wa_vert_x = snap(and1_a[0] - GRID)
    b.add_wire(base_x, wa_y, wa_vert_x, wa_y)
    if abs(wa_y - and1_a[1]) > 0.01:
        b.add_wire(wa_vert_x, wa_y, wa_vert_x, and1_a[1])
        b.add_wire(wa_vert_x, and1_a[1], and1_a[0], and1_a[1])
    else:
        b.add_wire(wa_vert_x, wa_y, and1_a[0], and1_a[1])

    # -- Wire READ_EN to AND2 pin A --
    re_vert_x = snap(and2_a[0] - GRID)
    b.add_wire(base_x, re_y, re_vert_x, re_y)
    if abs(re_y - and2_a[1]) > 0.01:
        b.add_wire(re_vert_x, re_y, re_vert_x, and2_a[1])
        b.add_wire(re_vert_x, and2_a[1], and2_a[0], and2_a[1])
    else:
        b.add_wire(re_vert_x, re_y, and2_a[0], and2_a[1])

    # -- AND1 output -> LED -> hier label WRITE_EN_ROW --
    and1_led_x = snap(and1_out[0] + 2 * GRID)
    b.add_wire(and1_out[0], and1_out[1], and1_led_x, and1_out[1])
    b.add_wire(and1_led_x, and1_out[1], hl_out_x, and1_out[1])
    b.place_led_below(and1_led_x, and1_out[1])
    b.add_hier_label("WRITE_EN_ROW", hl_out_x, and1_out[1],
                     shape="output", justify="left")

    # -- AND2 output -> LED -> hier label READ_EN_ROW --
    and2_led_x = snap(and2_out[0] + 2 * GRID)
    b.add_wire(and2_out[0], and2_out[1], and2_led_x, and2_out[1])
    b.add_wire(and2_led_x, and2_out[1], hl_out_x, and2_out[1])
    b.place_led_below(and2_led_x, and2_out[1])
    b.add_hier_label("READ_EN_ROW", hl_out_x, and2_out[1],
                     shape="output", justify="left")

    return b


def generate_byte_sheet():
    """
    Generate one memory byte (8 bits) with local NAND gating -- reused for all 8 bytes.

    Inputs:  WRITE_EN_ROW, READ_EN_ROW, COL_SEL, D0-D7
    Internal: WRITE_CLK_LOCAL = NAND(COL_SEL, WRITE_EN_ROW) -> DFF clocks
              BUF_OE_LOCAL   = NAND(COL_SEL, READ_EN_ROW)  -> buffer OE
    """
    b = SchematicBuilder(title="Memory Byte", page_size="A2",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    # -- Local NAND gating section at top --
    nand_x = base_x + 18 * GRID

    # Hierarchy input labels — COL_SEL first so WRITE/READ_EN_ROW verticals
    # go UP to pin B without passing through pin A (same X column)
    col_hier_y = snap(base_y)
    wen_hier_y = snap(base_y + SYM_SPACING_Y)
    ren_hier_y = snap(base_y + 2 * SYM_SPACING_Y)

    b.add_hier_label("COL_SEL", base_x, col_hier_y,
                     shape="input", justify="right")
    b.add_hier_label("WRITE_EN_ROW", base_x, wen_hier_y,
                     shape="input", justify="right")
    b.add_hier_label("READ_EN_ROW", base_x, ren_hier_y,
                     shape="input", justify="right")

    # NAND1: WRITE_CLK_LOCAL = NAND(COL_SEL, WRITE_EN_ROW) — unit 1 of 74LVC2G00
    nand1_y = snap(base_y + 4 * GRID)
    nand_ref, nand1_pins = b.place_symbol("74LVC2G00", nand_x, nand1_y)
    nand1_a = nand1_pins["1"]  # COL_SEL
    nand1_b = nand1_pins["2"]  # WRITE_EN_ROW
    nand1_out = nand1_pins["7"]

    # NAND2: BUF_OE_LOCAL = NAND(COL_SEL, READ_EN_ROW) — unit 2 of 74LVC2G00
    nand2_y = snap(nand1_y + SYM_SPACING_Y)
    _, nand2_pins = b.place_symbol("74LVC2G00", nand_x, nand2_y,
                                   unit=2, ref_override=nand_ref)
    nand2_a = nand2_pins["5"]  # COL_SEL
    nand2_b = nand2_pins["6"]  # READ_EN_ROW
    nand2_out = nand2_pins["3"]

    # Power unit (unit 3) — placed between the two NAND gates
    nand_pwr_y = snap(nand1_y + SYM_SPACING_Y / 2)
    _, nand_pwr_pins = b.place_symbol("74LVC2G00", nand_x, nand_pwr_y,
                                      unit=3, ref_override=nand_ref)
    b.connect_power(nand_pwr_pins, vcc_pin="8", gnd_pin="4")

    # Wire COL_SEL -> both NAND pin A via trunk
    col_trunk_x = snap(nand1_a[0] - 3 * GRID)
    b.add_wire(base_x, col_hier_y, col_trunk_x, col_hier_y)
    b.add_segmented_trunk(col_trunk_x,
                          [col_hier_y, nand1_a[1], nand2_a[1]])
    b.add_wire(col_trunk_x, nand1_a[1], nand1_a[0], nand1_a[1])
    b.add_wire(col_trunk_x, nand2_a[1], nand2_a[0], nand2_a[1])

    # Wire WRITE_EN_ROW -> NAND1 pin B
    # Route vertical at offset X to avoid ghost pins at nand1_b[0] (x=55.88)
    wen_vert_x = snap(nand1_b[0] - GRID)  # 53.34 — clears all pins at 55.88
    b.add_wire(base_x, wen_hier_y, wen_vert_x, wen_hier_y)
    if abs(wen_hier_y - nand1_b[1]) > 0.01:
        b.add_wire(wen_vert_x, wen_hier_y, wen_vert_x, nand1_b[1])
        b.add_wire(wen_vert_x, nand1_b[1], nand1_b[0], nand1_b[1])

    # Wire READ_EN_ROW -> NAND2 pin B
    # Route vertical at offset X to avoid ghost pins at nand2_b[0] (x=55.88)
    ren_vert_x = snap(nand2_b[0] - GRID)  # 53.34 — clears all pins at 55.88
    b.add_wire(base_x, ren_hier_y, ren_vert_x, ren_hier_y)
    if abs(ren_hier_y - nand2_b[1]) > 0.01:
        b.add_wire(ren_vert_x, ren_hier_y, ren_vert_x, nand2_b[1])
        b.add_wire(ren_vert_x, nand2_b[1], nand2_b[0], nand2_b[1])

    # NAND1 output -> LED -> route to WRITE_CLK trunk
    nand1_led_x = snap(nand1_out[0] + 2 * GRID)
    nand1_route_x = snap(nand1_led_x + 8 * GRID)  # offset right to avoid LED overlap
    b.add_wire(nand1_out[0], nand1_out[1], nand1_led_x, nand1_out[1])
    b.add_wire(nand1_led_x, nand1_out[1], nand1_route_x, nand1_out[1])
    b.place_led_below(nand1_led_x, nand1_out[1])

    # NAND2 output -> LED -> route to BUF_OE trunk
    nand2_led_x = snap(nand2_out[0] + 2 * GRID)
    nand2_route_x = snap(nand2_led_x + 10 * GRID)  # different X from NAND1
    b.add_wire(nand2_out[0], nand2_out[1], nand2_led_x, nand2_out[1])
    b.add_wire(nand2_led_x, nand2_out[1], nand2_route_x, nand2_out[1])
    b.place_led_below(nand2_led_x, nand2_out[1])

    # -- 8-bit DFF + buffer array (shifted down to make room for NANDs) --
    bit_base_y = snap(nand2_y + 3 * SYM_SPACING_Y)

    dff_x = base_x + 18 * GRID
    buf_x = base_x + 52 * GRID
    wclk_trunk_x = base_x + 10 * GRID
    boe_trunk_x = buf_x - 8 * GRID

    # Route NAND outputs: right X → down to well above DFFs → left to trunk
    nand_turn_y = snap(bit_base_y - 6 * GRID)  # clear of DFF bboxes
    nand2_turn_y = snap(nand_turn_y + GRID)
    # NAND1 -> WRITE_CLK trunk
    b.add_wire(nand1_route_x, nand1_out[1], nand1_route_x, nand_turn_y)
    b.add_wire(nand1_route_x, nand_turn_y, wclk_trunk_x, nand_turn_y)
    # NAND2 -> BUF_OE trunk
    b.add_wire(nand2_route_x, nand2_out[1], nand2_route_x, nand2_turn_y)
    b.add_wire(nand2_route_x, nand2_turn_y, boe_trunk_x, nand2_turn_y)

    clk_pin_positions = []
    oe_pin_positions = []

    for bit in range(8):
        y = bit_base_y + bit * DFF_SPACING_Y

        _, dff_pins = b.place_symbol("74LVC1G79", dff_x, y)
        b.connect_power(dff_pins)

        d_pin = dff_pins["1"]
        hl_y = bit_base_y + bit * DFF_SPACING_Y
        b.add_hier_label(f"D{bit}", base_x, hl_y,
                         shape="bidirectional", justify="right")
        if snap(hl_y) != snap(d_pin[1]):
            b.add_wire(base_x, hl_y, base_x, d_pin[1])
        b.add_wire(base_x, d_pin[1], d_pin[0], d_pin[1])

        clk_pin = dff_pins["2"]
        clk_pin_positions.append(clk_pin)
        b.add_wire(wclk_trunk_x, clk_pin[1], clk_pin[0], clk_pin[1])

        q_pin = dff_pins["4"]

        _, buf_pins = b.place_symbol("74LVC1G125", buf_x, y)
        b.connect_power(buf_pins)

        a_pin = buf_pins["2"]
        vcc_pin = buf_pins["5"]
        q_led_x = snap(q_pin[0] + 2 * GRID)
        b.add_wire(q_pin[0], q_pin[1], q_led_x, q_pin[1])

        wire_y = snap(q_pin[1])
        x_lo = min(q_led_x, a_pin[0])
        x_hi = max(q_led_x, a_pin[0])
        vcc_on_path = (abs(snap(vcc_pin[1]) - wire_y) < 0.01 and
                       x_lo + 0.01 < snap(vcc_pin[0]) < x_hi - 0.01)

        if vcc_on_path:
            b.add_wire(q_led_x, q_pin[1], q_led_x, a_pin[1])
            b.add_wire(q_led_x, a_pin[1], a_pin[0], a_pin[1])
        else:
            b.add_wire(q_led_x, q_pin[1], a_pin[0], q_pin[1])
            if snap(q_pin[1]) != snap(a_pin[1]):
                b.add_wire(a_pin[0], q_pin[1], a_pin[0], a_pin[1])
        b.place_led_below(q_led_x, q_pin[1])

        oe_pin = buf_pins["1"]
        oe_pin_positions.append(oe_pin)
        b.add_wire(boe_trunk_x, oe_pin[1], oe_pin[0], oe_pin[1])

        y_pin = buf_pins["4"]
        b.add_label(f"D{bit}", *y_pin, justify="right")

    # WRITE_CLK trunk: from turn point down to all DFF CLK pins
    clk_ys = [p[1] for p in clk_pin_positions]
    wclk_all_ys = sorted([nand_turn_y] + clk_ys)
    b.add_segmented_trunk(wclk_trunk_x, wclk_all_ys)

    # BUF_OE trunk: from turn point down to all buffer OE pins
    oe_ys = [p[1] for p in oe_pin_positions]
    boe_all_ys = sorted([nand2_turn_y] + oe_ys)
    b.add_segmented_trunk(boe_trunk_x, boe_all_ys)

    return b


# --------------------------------------------------------------
# Root sheet generator
# --------------------------------------------------------------

def generate_root_sheet():
    """
    Root sheet: 24-pin connector, bus indicator LEDs, pin headers, hierarchy refs.

    11-bit address architecture for latency testing:
      - Address Decoder: A0-A6 → ROW_SEL_0..3 + RS1-RS4 probes
      - Column Select: A7-A10 → COL_SEL_0..15 + GA0-GA3, GB0-GB3 probes
      - Control Logic: nCE, nOE, nWE → WRITE_ACTIVE, READ_EN
      - Row Control (×4): WRITE_ACTIVE, READ_EN, ROW_SEL → WRITE_EN_ROW, READ_EN_ROW
      - Bytes (×8): WRITE_EN_ROW, READ_EN_ROW, COL_SEL, D0-D7

    Pin headers:
      - Probe header (Conn_01x12): RS1-RS4, GA0-GA3, GB0-GB3
      - Unused column header (Conn_01x14): COL_SEL_2 through COL_SEL_15
    """
    b = SchematicBuilder(title="8-Byte Discrete RAM (2K-depth Decoders)",
                         page_size="A1", project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 25.4

    sheet_gap = 5 * GRID
    wire_stub = 5.08

    def _sheet_height(num_pins):
        return snap(num_pins * 2.54 + 5.08)

    def _pin_y(sy, pin_idx):
        return snap(sy + 2.54 + pin_idx * 2.54)

    def _add_sheet_block(name, filename, pins, sx, sy, sw, sh, fill_color,
                         right_pins=None):
        if right_pins is None:
            right_pins = set()
        sheet = HierarchicalSheet()
        sheet.position = Position(X=sx, Y=sy)
        sheet.width = sw
        sheet.height = sh
        sheet.stroke = Stroke()
        sheet.fill = fill_color
        sheet.uuid = uid()
        sheet.sheetName = Property(
            key="Sheet name", value=name, id=0,
            position=Position(X=sx, Y=sy - 1.27, angle=0),
        )
        sheet.fileName = Property(
            key="Sheet file", value=filename, id=1,
            position=Position(X=sx + sw, Y=sy + sh + 1.27, angle=0),
        )
        left_pins_list = [(pn, pt) for pn, pt in pins if pn not in right_pins]
        right_pins_list = [(pn, pt) for pn, pt in pins if pn in right_pins]

        pin_positions = {}
        for pin_idx, (pin_name, pin_type) in enumerate(left_pins_list):
            pin = HierarchicalPin()
            pin.name = pin_name
            pin.connectionType = pin_type
            py = _pin_y(sy, pin_idx)
            pin.position = Position(X=sx, Y=py, angle=180)
            pin.effects = Effects(font=Font(width=1.27, height=1.27),
                                  justify=Justify(horizontally="left"))
            pin_positions[pin_name] = (sx, py)
            pin.uuid = uid()
            sheet.pins.append(pin)
        for pin_idx, (pin_name, pin_type) in enumerate(right_pins_list):
            pin = HierarchicalPin()
            pin.name = pin_name
            pin.connectionType = pin_type
            py = _pin_y(sy, pin_idx)
            pin.position = Position(X=sx + sw, Y=py, angle=0)
            pin.effects = Effects(font=Font(width=1.27, height=1.27),
                                  justify=Justify(horizontally="right"))
            pin_positions[pin_name] = (sx + sw, py)
            pin.uuid = uid()
            sheet.pins.append(pin)

        sheet.instances.append(HierarchicalSheetProjectInstance(
            name=PROJECT_NAME,
            paths=[HierarchicalSheetProjectPath(
                sheetInstancePath=f"/{b.sch.uuid}/{sheet.uuid}/",
                page=str(len(b.sch.sheets) + 2),
            )]
        ))
        b.sch.sheets.append(sheet)
        return pin_positions

    yellow_fill = ColorRGBA(R=255, G=255, B=225, A=255, precision=4)
    blue_fill = ColorRGBA(R=225, G=235, B=255, A=255, precision=4)
    green_fill = ColorRGBA(R=225, G=255, B=225, A=255, precision=4)
    orange_fill = ColorRGBA(R=255, G=240, B=210, A=255, precision=4)

    # ================================================================
    # Layout positions
    # ================================================================
    col1_x = snap(base_x + 50 * GRID)  # wider to accommodate 22 LED fan-out
    col1_w = snap(32 * GRID)  # wider for more pins
    inter_col_gap = snap(15 * GRID)
    col2_x = snap(col1_x + col1_w + inter_col_gap)
    col2_w = snap(28 * GRID)
    col2_byte_gap = snap(15 * GRID)
    byte_col1_x = snap(col2_x + col2_w + col2_byte_gap)
    byte_w = snap(22 * GRID)
    byte_inter_gap = snap(20 * GRID)
    byte_col2_x = snap(byte_col1_x + byte_w + byte_inter_gap)

    # ================================================================
    # Place sheet blocks — Column 1: Address Decoder, Column Select, Control Logic
    # ================================================================

    # Address Decoder: A0-A6 → ROW_SEL_0..3 + RS1-RS4 probes
    addr_left_defs = [(f"A{i}", "input") for i in range(7)]
    addr_right_defs = ([(f"ROW_SEL_{i}", "output") for i in range(4)]
                       + [(f"RS{i}", "output") for i in range(1, 5)])
    addr_pin_defs = addr_left_defs + addr_right_defs
    addr_right_names = ({f"ROW_SEL_{i}" for i in range(4)}
                        | {f"RS{i}" for i in range(1, 5)})
    addr_h = _sheet_height(max(len(addr_left_defs), len(addr_right_defs)))
    addr_sy = base_y
    addr_pp = _add_sheet_block("Address Decoder", "address_decoder.kicad_sch",
                               addr_pin_defs, col1_x, addr_sy,
                               col1_w, addr_h, yellow_fill,
                               right_pins=addr_right_names)

    # Column Select: A7-A10 → COL_SEL_0..15 + GA0-GA3, GB0-GB3 probes
    colsel_left_defs = [(f"A{7+i}", "input") for i in range(4)]
    colsel_right_defs = ([(f"COL_SEL_{i}", "output") for i in range(16)]
                         + [(f"GA{i}", "output") for i in range(4)]
                         + [(f"GB{i}", "output") for i in range(4)])
    colsel_pin_defs = colsel_left_defs + colsel_right_defs
    colsel_right_names = ({f"COL_SEL_{i}" for i in range(16)}
                          | {f"GA{i}" for i in range(4)}
                          | {f"GB{i}" for i in range(4)})
    colsel_h = _sheet_height(max(len(colsel_left_defs), len(colsel_right_defs)))
    colsel_sy = snap(addr_sy + addr_h + sheet_gap)
    colsel_pp = _add_sheet_block("Column Select", "column_select.kicad_sch",
                                 colsel_pin_defs, col1_x, colsel_sy,
                                 col1_w, colsel_h, blue_fill,
                                 right_pins=colsel_right_names)

    # Control Logic: nCE, nOE, nWE → WRITE_ACTIVE, READ_EN (unchanged)
    ctrl_left_defs = [("nCE", "input"), ("nOE", "input"), ("nWE", "input")]
    ctrl_right_defs = [("WRITE_ACTIVE", "output"), ("READ_EN", "output")]
    ctrl_pin_defs = ctrl_left_defs + ctrl_right_defs
    ctrl_right_names = {"WRITE_ACTIVE", "READ_EN"}
    ctrl_h = _sheet_height(max(len(ctrl_left_defs), len(ctrl_right_defs)))
    ctrl_sy = snap(colsel_sy + colsel_h + sheet_gap)
    ctrl_pp = _add_sheet_block("Control Logic", "control_logic.kicad_sch",
                               ctrl_pin_defs, col1_x, ctrl_sy,
                               col1_w, ctrl_h, yellow_fill,
                               right_pins=ctrl_right_names)

    # ================================================================
    # Place sheet blocks — Column 2: Row Control (4 instances)
    # ================================================================
    rc_pin_defs = [
        ("WRITE_ACTIVE", "input"), ("READ_EN", "input"), ("ROW_SEL", "input"),
        ("WRITE_EN_ROW", "output"), ("READ_EN_ROW", "output"),
    ]
    rc_right_names = {"WRITE_EN_ROW", "READ_EN_ROW"}
    rc_h = _sheet_height(max(3, 2))  # 3 left, 2 right

    rc_pp = []
    for row_i in range(4):
        rc_sy = snap(base_y + row_i * (rc_h + sheet_gap))
        pp = _add_sheet_block(f"Row Control {row_i}", "row_control.kicad_sch",
                              rc_pin_defs, col2_x, rc_sy,
                              col2_w, rc_h, yellow_fill,
                              right_pins=rc_right_names)
        rc_pp.append(pp)

    # ================================================================
    # Byte sheet blocks (4 rows x 2 columns)
    # ================================================================
    byte_pin_defs = [("COL_SEL", "input"),
                     ("WRITE_EN_ROW", "input"), ("READ_EN_ROW", "input")]
    byte_pin_defs += [(f"D{bit}", "bidirectional") for bit in range(8)]
    byte_h = _sheet_height(len(byte_pin_defs))

    byte_pp = []
    for byte_idx in range(8):
        col = byte_idx // 4
        row = byte_idx % 4
        sx = byte_col1_x if col == 0 else byte_col2_x
        sy = snap(base_y + row * (byte_h + sheet_gap))
        pp = _add_sheet_block(f"Byte {byte_idx}", "byte.kicad_sch",
                              byte_pin_defs, sx, sy, byte_w, byte_h, green_fill)
        byte_pp.append(pp)

    # ================================================================
    # Pre-compute connector placement
    # ================================================================
    byte_h_pre = _sheet_height(len(byte_pin_defs))
    sheet_bottom_y = snap(base_y + 3 * (byte_h_pre + sheet_gap) + byte_h_pre)
    ensemble_center_y = snap((base_y + sheet_bottom_y) / 2)

    conn_x = base_x
    conn_y = snap(ensemble_center_y + 1.27)
    _, conn_pins = b.place_symbol("Conn_01x24", conn_x, conn_y,
                                  ref_prefix="J", value="SRAM_Bus", angle=180)

    # ================================================================
    # Connector pin mapping (24-pin)
    # Pin 1=GND, 2-5=A7-A10, 6-13=D7..D0, 14-20=A0-A6, 21-23=nCE/nWE/nOE, 24=VCC
    # ================================================================
    signal_names = [
        "A7", "A8", "A9", "A10",                        # pins 2-5
        "D7", "D6", "D5", "D4", "D3", "D2", "D1", "D0",  # pins 6-13
        "A0", "A1", "A2", "A3", "A4", "A5", "A6",      # pins 14-20
        "nCE", "nWE", "nOE",                             # pins 21-23
    ]
    conn_signal_pos = {}
    for pin_num_int, sig in enumerate(signal_names, start=2):
        conn_signal_pos[sig] = conn_pins[str(pin_num_int)]

    # ================================================================
    # Connector power pins
    # ================================================================
    vcc_pos = conn_pins["24"]
    gnd_pos = conn_pins["1"]
    pwr_wire_len = snap(3 * GRID)
    vcc_sym_x = snap(vcc_pos[0] + pwr_wire_len)
    b.add_wire(vcc_pos[0], vcc_pos[1], vcc_sym_x, vcc_pos[1])
    b.place_power("VCC", vcc_sym_x, vcc_pos[1])
    b.place_power("PWR_FLAG", vcc_sym_x, vcc_pos[1])
    gnd_sym_x = snap(gnd_pos[0] + pwr_wire_len)
    b.add_wire(gnd_pos[0], gnd_pos[1], gnd_sym_x, gnd_pos[1])
    b.place_power("GND", gnd_sym_x, gnd_pos[1])
    b.place_power("PWR_FLAG", gnd_sym_x, gnd_pos[1])

    # ================================================================
    # Connector signal wires + LED indicators + direct wiring
    # ================================================================
    conn_pin_x = conn_signal_pos[signal_names[0]][0]
    led_order = sorted(signal_names, key=lambda s: conn_signal_pos[s][1])
    n_signals = len(led_order)

    fan_spacing = snap(5 * GRID)  # slightly tighter for 22 signals
    fan_span = (n_signals - 1) * fan_spacing
    min_conn_pin_y = min(conn_pins[str(i)][1] for i in range(1, 25))
    max_conn_pin_y = max(conn_pins[str(i)][1] for i in range(1, 25))
    conn_pin_mid_y = snap((min_conn_pin_y + max_conn_pin_y) / 2)
    fan_start_y = snap(conn_pin_mid_y - fan_span / 2)
    grid_units = fan_start_y / GRID
    if abs(grid_units - round(grid_units)) < 0.01:
        fan_start_y = snap(fan_start_y + GRID / 2)
    page_min_y = snap(5 * GRID)
    while fan_start_y < page_min_y:
        fan_start_y = snap(fan_start_y + GRID)

    turn_base_x = snap(conn_pin_x + 4 * GRID)
    turn_spacing = snap(GRID / 2)
    center_idx = (n_signals - 1) / 2
    v_order = sorted(range(n_signals),
                     key=lambda i: (-abs(i - center_idx), i))
    v_rank = {idx: rank for rank, idx in enumerate(v_order)}

    led_jct_x = snap(turn_base_x + (n_signals - 1) * turn_spacing + 3 * GRID)
    label_x = snap(led_jct_x + 6 * GRID)

    # Direct wire destinations:
    # A0-A6 → Address Decoder, A7-A10 → Column Select, nCE/nOE/nWE → Control Logic
    direct_wire_dest = {}
    for i in range(7):
        direct_wire_dest[f"A{i}"] = addr_pp[f"A{i}"]
    for i in range(7, 11):
        direct_wire_dest[f"A{i}"] = colsel_pp[f"A{i}"]
    direct_wire_dest["nCE"] = ctrl_pp["nCE"]
    direct_wire_dest["nOE"] = ctrl_pp["nOE"]
    direct_wire_dest["nWE"] = ctrl_pp["nWE"]

    direct_signals_order = ([f"A{i}" for i in range(7)]
                            + [f"A{i}" for i in range(7, 11)]
                            + ["nCE", "nOE", "nWE"])
    direct_turn = {}
    n_direct = len(direct_signals_order)
    for i, sig in enumerate(direct_signals_order):
        direct_turn[sig] = snap(col1_x - (n_direct - i) * GRID)

    for idx, sig in enumerate(led_order):
        cx, cy = conn_signal_pos[sig]
        ty = snap(fan_start_y + idx * fan_spacing)
        tx = snap(turn_base_x + v_rank[idx] * turn_spacing)

        b.add_wire(cx, cy, tx, cy)
        b.add_wire(tx, cy, tx, ty)
        b.add_wire(tx, ty, led_jct_x, ty)
        b.place_led_below(led_jct_x, ty, drop=2 * GRID)

        if sig in direct_wire_dest:
            dtx = direct_turn[sig]
            dest_px, dest_py = direct_wire_dest[sig]
            b.add_wire(led_jct_x, ty, dtx, ty)
            b.add_wire(dtx, ty, dtx, dest_py)
            b.add_wire(dtx, dest_py, dest_px, dest_py)
        else:
            # D0-D7 use labels
            b.add_wire(led_jct_x, ty, label_x, ty)
            b.add_label(sig, label_x, ty)

    # ================================================================
    # D0-D7 labels on byte sheet input pins
    # ================================================================
    for byte_idx in range(8):
        pp = byte_pp[byte_idx]
        for bit in range(8):
            sig = f"D{bit}"
            px, py = pp[sig]
            b.add_wire(px, py, px - wire_stub, py)
            b.add_label(sig, px - wire_stub, py, justify="right")

    # ================================================================
    # Route: Col1 RIGHT → Col2 LEFT (ROW_SEL_0-3 via trunks)
    # ================================================================
    sel_trunk_base_x = snap(col2_x - 3 * GRID)
    sel_trunk_x = [snap(sel_trunk_base_x - i * GRID) for i in range(4)]

    for i in range(4):
        sig = f"ROW_SEL_{i}"
        ax, ay = addr_pp[sig]
        dst_x, dst_y = rc_pp[i]["ROW_SEL"]
        tx = sel_trunk_x[i]

        b.add_wire(ax, ay, tx, ay)
        trunk_ys = sorted(set([snap(ay), snap(dst_y)]))
        if len(trunk_ys) > 1:
            b.add_segmented_trunk(tx, trunk_ys)
        b.add_wire(tx, dst_y, dst_x, dst_y)

    # ================================================================
    # Route: Col1 RIGHT → Col2 LEFT (WRITE_ACTIVE, READ_EN via labels)
    # ================================================================
    wa_src = ctrl_pp["WRITE_ACTIVE"]
    b.add_wire(wa_src[0], wa_src[1], wa_src[0] + wire_stub, wa_src[1])
    b.add_label("WRITE_ACTIVE", wa_src[0] + wire_stub, wa_src[1])
    for i in range(4):
        d = rc_pp[i]["WRITE_ACTIVE"]
        b.add_wire(d[0], d[1], d[0] - wire_stub, d[1])
        b.add_label("WRITE_ACTIVE", d[0] - wire_stub, d[1], justify="right")

    re_src = ctrl_pp["READ_EN"]
    b.add_wire(re_src[0], re_src[1], re_src[0] + wire_stub, re_src[1])
    b.add_label("READ_EN", re_src[0] + wire_stub, re_src[1])
    for i in range(4):
        d = rc_pp[i]["READ_EN"]
        b.add_wire(d[0], d[1], d[0] - wire_stub, d[1])
        b.add_label("READ_EN", d[0] - wire_stub, d[1], justify="right")

    # ================================================================
    # Route: Col2 RIGHT → Bytes (WRITE_EN_ROW_i, READ_EN_ROW_i)
    # ================================================================
    for row_i in range(4):
        wen_sig = f"WRITE_EN_ROW_{row_i}"
        src_x, src_y = rc_pp[row_i]["WRITE_EN_ROW"]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(wen_sig, src_x + wire_stub, src_y)
        for col_j in range(2):
            byte_idx = col_j * 4 + row_i
            dst_x, dst_y = byte_pp[byte_idx]["WRITE_EN_ROW"]
            b.add_wire(dst_x, dst_y, dst_x - wire_stub, dst_y)
            b.add_label(wen_sig, dst_x - wire_stub, dst_y, justify="right")

        ren_sig = f"READ_EN_ROW_{row_i}"
        src_x, src_y = rc_pp[row_i]["READ_EN_ROW"]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(ren_sig, src_x + wire_stub, src_y)
        for col_j in range(2):
            byte_idx = col_j * 4 + row_i
            dst_x, dst_y = byte_pp[byte_idx]["READ_EN_ROW"]
            b.add_wire(dst_x, dst_y, dst_x - wire_stub, dst_y)
            b.add_label(ren_sig, dst_x - wire_stub, dst_y, justify="right")

    # ================================================================
    # Route: COL_SEL_0 → bytes 0-3, COL_SEL_1 → bytes 4-7 via labels
    # ================================================================
    for col_j in range(2):
        col_sig = f"COL_SEL_{col_j}"
        src_x, src_y = colsel_pp[col_sig]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(col_sig, src_x + wire_stub, src_y)

        for row_i in range(4):
            byte_idx = col_j * 4 + row_i
            dst_x, dst_y = byte_pp[byte_idx]["COL_SEL"]
            b.add_wire(dst_x, dst_y, dst_x - wire_stub, dst_y)
            b.add_label(col_sig, dst_x - wire_stub, dst_y, justify="right")

    # ================================================================
    # Probe pin header (Conn_01x12): RS1-RS4, GA0-GA3, GB0-GB3
    # ================================================================
    probe_header_x = snap(col1_x + col1_w + 3 * GRID)
    probe_header_y = snap(colsel_sy + colsel_h + sheet_gap + ctrl_h + 2 * sheet_gap)
    _, probe_pins = b.place_symbol("Conn_01x12", probe_header_x, probe_header_y,
                                   ref_prefix="J", value="Probe", angle=180)

    probe_signals = (
        [f"RS{i}" for i in range(1, 5)]
        + [f"GA{i}" for i in range(4)]
        + [f"GB{i}" for i in range(4)]
    )
    for pin_idx, sig in enumerate(probe_signals):
        pin_num = str(pin_idx + 1)
        px, py = probe_pins[pin_num]
        b.add_wire(px, py, px + wire_stub, py)
        b.add_label(sig, px + wire_stub, py)

    # Source labels for probe signals from decoder sheets
    for i in range(1, 5):
        sig = f"RS{i}"
        src_x, src_y = addr_pp[sig]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(sig, src_x + wire_stub, src_y)

    for g in range(4):
        sig = f"GA{g}"
        src_x, src_y = colsel_pp[sig]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(sig, src_x + wire_stub, src_y)

    for g in range(4):
        sig = f"GB{g}"
        src_x, src_y = colsel_pp[sig]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(sig, src_x + wire_stub, src_y)

    # ================================================================
    # Unused column header (Conn_01x14): COL_SEL_2 through COL_SEL_15
    # ================================================================
    unused_header_x = probe_header_x
    unused_header_y = snap(probe_header_y + 16 * GRID)
    _, unused_pins = b.place_symbol("Conn_01x14", unused_header_x, unused_header_y,
                                    ref_prefix="J", value="Unused_COL_SEL", angle=180)

    for pin_idx in range(14):
        col_idx = pin_idx + 2  # COL_SEL_2 through COL_SEL_15
        sig = f"COL_SEL_{col_idx}"
        pin_num = str(pin_idx + 1)
        px, py = unused_pins[pin_num]
        b.add_wire(px, py, px + wire_stub, py)
        b.add_label(sig, px + wire_stub, py)

    # Source labels for unused COL_SEL from column select sheet
    for col_idx in range(2, 16):
        sig = f"COL_SEL_{col_idx}"
        src_x, src_y = colsel_pp[sig]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(sig, src_x + wire_stub, src_y)

    return b


# --------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------

def count_components(builders):
    """Count total ICs, LEDs, resistors across all sheets."""
    totals = {"U": 0, "D": 0, "R": 0, "C": 0, "#PWR": 0, "J": 0, "#FLG": 0}
    for name, builder in builders.items():
        multiplier = 8 if name == "byte" else (4 if name == "row_control" else 1)
        for prefix, count in builder._ref_counters.items():
            actual = count - 1
            if prefix in totals:
                totals[prefix] += actual * multiplier
            else:
                totals[prefix] = actual * multiplier
    return totals


def fix_instance_paths(builders):
    """Fix sub-sheet symbol instance paths and assign globally unique references.

    Multi-unit symbols (e.g. 74LVC2G00) have multiple SchematicSymbol objects
    sharing the same template reference.  They are grouped so that one global
    reference is allocated per group, and each symbol keeps its own unit number.
    """
    from collections import defaultdict

    root_sch = builders["ram"].sch
    root_uuid = root_sch.uuid

    global_counters = {}
    for sym in root_sch.schematicSymbols:
        for p in sym.properties:
            if p.key == "Reference":
                prefix = p.value.rstrip("0123456789")
                num_str = p.value[len(prefix):]
                num = int(num_str) if num_str else 0
                global_counters[prefix] = max(global_counters.get(prefix, 0), num)
                break

    for sheet in root_sch.sheets:
        fname = sheet.fileName.value
        sheet_block_uuid = sheet.uuid
        hier_path = f"/{root_uuid}/{sheet_block_uuid}"

        builder_name = fname.replace(".kicad_sch", "")
        if builder_name not in builders:
            continue
        builder = builders[builder_name]

        all_sheet_blocks = [s for s in root_sch.sheets if s.fileName.value == fname]
        is_first_instance = (sheet is all_sheet_blocks[0])
        is_multi_instance = len(all_sheet_blocks) > 1

        if is_multi_instance and not is_first_instance:
            continue

        # Group symbols by template reference (multi-unit symbols share a ref)
        ref_groups = defaultdict(list)
        for sym in builder.sch.schematicSymbols:
            template_ref = ""
            for p in sym.properties:
                if p.key == "Reference":
                    template_ref = p.value
                    break
            ref_groups[template_ref].append(sym)

        if is_multi_instance:
            for template_ref, syms in ref_groups.items():
                prefix = template_ref.rstrip("0123456789")

                # Ensure all symbols in the group have an instance object
                for sym in syms:
                    existing = sym.instances[0] if sym.instances else None
                    if existing is None:
                        existing = SymbolProjectInstance(name=PROJECT_NAME, paths=[])
                        sym.instances = [existing]

                # One global ref per group per sheet instance
                for inst_block in all_sheet_blocks:
                    inst_path = f"/{root_uuid}/{inst_block.uuid}"
                    global_counters[prefix] = global_counters.get(prefix, 0) + 1
                    inst_ref = f"{prefix}{global_counters[prefix]}"
                    for sym in syms:
                        sym.instances[0].paths.append(SymbolProjectPath(
                            sheetInstancePath=inst_path,
                            reference=inst_ref,
                            unit=sym.unit,
                        ))
        else:
            for template_ref, syms in ref_groups.items():
                prefix = template_ref.rstrip("0123456789")
                global_counters[prefix] = global_counters.get(prefix, 0) + 1
                new_ref = f"{prefix}{global_counters[prefix]}"

                for sym in syms:
                    existing = sym.instances[0] if sym.instances else None
                    if existing is None:
                        existing = SymbolProjectInstance(name=PROJECT_NAME, paths=[])
                        sym.instances = [existing]

                    existing.paths.append(SymbolProjectPath(
                        sheetInstancePath=hier_path,
                        reference=new_ref,
                        unit=sym.unit,
                    ))


def main():
    print("=" * 60)
    print("Discrete NES - 8-Byte RAM Prototype (2K-depth Decoders)")
    print("=" * 60)

    # Ensure pin offsets are discovered using BOARD_DIR for temp files
    get_pin_offsets(board_dir=BOARD_DIR)

    print("\nGenerating sub-sheets...")

    builders = {}

    builders["address_decoder"] = generate_address_decoder()
    print("  [+] address_decoder.kicad_sch")

    builders["column_select"] = generate_column_select()
    print("  [+] column_select.kicad_sch")

    builders["control_logic"] = generate_control_logic()
    print("  [+] control_logic.kicad_sch")

    builders["row_control"] = generate_row_control()
    print("  [+] row_control.kicad_sch (shared by 4 row instances)")

    builders["byte"] = generate_byte_sheet()
    print("  [+] byte.kicad_sch (shared by all 8 byte instances)")

    builders["ram"] = generate_root_sheet()
    print("  [+] ram.kicad_sch (root)")

    fix_instance_paths(builders)
    print("  [*] Fixed hierarchical instance paths")

    print("\nSaving files...")
    saved_paths = []
    for name, builder in builders.items():
        filepath = os.path.join(BOARD_DIR, f"{name}.kicad_sch")
        builder.save(filepath)
        saved_paths.append(filepath)
        print(f"  Saved: {filepath}")

    totals = count_components(builders)
    print("\n" + "=" * 60)
    print("Component Summary")
    print("=" * 60)
    print(f"  ICs (U):        {totals.get('U', 0)}")
    print(f"  LEDs (D):       {totals.get('D', 0)}")
    print(f"  Resistors (R):  {totals.get('R', 0)}")
    print(f"  Connectors (J): {totals.get('J', 0)}")
    print(f"  Power (#PWR):   {totals.get('#PWR', 0)}")
    total_parts = totals.get('U', 0) + totals.get('D', 0) + totals.get('R', 0) + totals.get('J', 0)
    print(f"  ------------------------")
    print(f"  Total BOM parts: {total_parts}")
    print()

    ic_types = {}
    for name, builder in builders.items():
        multiplier = 8 if name == "byte" else (4 if name == "row_control" else 1)
        for sym in builder.sch.schematicSymbols:
            # Skip non-primary units to avoid double-counting multi-unit ICs
            if getattr(sym, 'unit', 1) != 1:
                continue
            if sym.properties and sym.properties[0].value.startswith("U") or \
               (len(sym.properties) > 0 and any(p.key == "Reference" and p.value.startswith("U") for p in sym.properties)):
                lib_id = sym.libId if hasattr(sym, 'libId') else sym.entryName
                if lib_id:
                    base_id = lib_id.split(":")[-1] if ":" in lib_id else lib_id
                    if base_id.startswith("74LVC"):
                        ic_types[base_id] = ic_types.get(base_id, 0) + multiplier

    if ic_types:
        print("IC Breakdown:")
        for ic, count in sorted(ic_types.items()):
            print(f"  {ic}: {count}")

    print("\nDone! Open ram.kicad_sch in KiCad to view the design.")


if __name__ == "__main__":
    main()
