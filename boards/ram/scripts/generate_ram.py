#!/usr/bin/env python3
"""
Generate hierarchical KiCad schematics for the full 2KB discrete RAM board.

Architecture (11-bit address, 128 rows x 16 columns = 2048 bytes):
  Address decoder: 3-to-8 + 4-to-16 sub-decoders -> DEC3_0..7 + DEC4_0..15
  Column decoder:  4-to-16 -> COL_SEL_0..15
  16 row groups (one per DEC4 output), each containing:
    8 final AND gates: ROW_SEL_i = AND(DEC3_i, DEC4)
    8 rows, each containing:
      1 row_control: WRITE_EN_ROW, READ_EN_ROW
      16 bytes: NAND gating + 8 DFF + 8 BUF

Hierarchy (4 levels):
  ram.kicad_sch (root)
  +-- address_decoder.kicad_sch  (1x)
  +-- column_select.kicad_sch    (1x)
  +-- control_logic.kicad_sch    (1x)
  +-- power_supply.kicad_sch     (1x)
  +-- row_group.kicad_sch        (16x)
      +-- row.kicad_sch          (8x each = 128 total)
          +-- row_control.kicad_sch  (1x each = 128 total)
          +-- byte.kicad_sch         (16x each = 2048 total)

Global labels (shared across all sheets): D0-D7, COL_SEL_0-15,
  WRITE_ACTIVE, READ_EN, DEC3_0-7
Hier pins (instance-specific): DEC4 (root->row_group), ROW_SEL (row_group->row)
"""

import os
import sys
from collections import defaultdict

# Add shared library to path
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

# Import prototype's generators for reusable sub-sheets
_PROTO_SCRIPTS = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "ram-prototype", "scripts"))
sys.path.insert(0, _PROTO_SCRIPTS)
from generate_ram import (                          # noqa: E402
    generate_column_select as _proto_column_select,
    generate_control_logic as _proto_control_logic,
    generate_row_control   as _proto_row_control,
    generate_byte_sheet    as _proto_byte_sheet,
    generate_power_supply  as _proto_power_supply,
)

from kicad_gen import SchematicBuilder, snap, uid, GRID, SYM_SPACING_Y  # noqa: E402
from kicad_gen.symbols import get_pin_offsets                            # noqa: E402

from kiutils.items.schitems import (                                     # noqa: E402
    HierarchicalSheet, HierarchicalPin,
    HierarchicalSheetProjectInstance, HierarchicalSheetProjectPath,
    SymbolProjectInstance, SymbolProjectPath,
)
from kiutils.items.common import (                                       # noqa: E402
    Position, Property, Effects, Font, Justify, Stroke, ColorRGBA,
)

# --------------------------------------------------------------
# Constants
# --------------------------------------------------------------
PROJECT_NAME = "ram"
BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

NUM_DEC3 = 8
NUM_DEC4 = 16
NUM_ROW_GROUPS = NUM_DEC4       # 16
ROWS_PER_GROUP = NUM_DEC3       # 8
NUM_COLS = 16
TOTAL_ROWS = NUM_DEC3 * NUM_DEC4   # 128
TOTAL_BYTES = TOTAL_ROWS * NUM_COLS  # 2048


# ==============================================================
# Helpers: hierarchy sheet blocks
# ==============================================================

def _sheet_height(num_pins):
    return snap(num_pins * 2.54 + 5.08)


def _pin_y(sy, pin_idx):
    return snap(sy + 2.54 + pin_idx * 2.54)


def _add_sheet_block(b, name, filename, pins, sx, sy, sw, sh, fill_color,
                     right_pins=None):
    """Create a hierarchical sheet block.  Returns {pin_name: (x, y)}."""
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

    b.sch.sheets.append(sheet)
    return pin_positions


# ==============================================================
# Color palette (shared across generators)
# ==============================================================
_YELLOW = ColorRGBA(R=255, G=255, B=225, A=255, precision=4)
_BLUE   = ColorRGBA(R=225, G=235, B=255, A=255, precision=4)
_GREEN  = ColorRGBA(R=225, G=255, B=225, A=255, precision=4)
_ORANGE = ColorRGBA(R=255, G=240, B=210, A=255, precision=4)
_PURPLE = ColorRGBA(R=240, G=225, B=255, A=255, precision=4)
_PINK   = ColorRGBA(R=255, G=225, B=235, A=255, precision=4)


# ==============================================================
# Sub-sheet generators
# ==============================================================

def generate_address_decoder():
    """Address decoder for full 2KB board (no final cross-product ANDs).

    Identical to the prototype's decoder logic, but ALL outputs are hier labels:
      DEC3_0..7 (8 outputs from 3-to-8 sub-decoder)
      DEC4_0..15 (16 outputs from 4-to-16 sub-decoder)

    The final cross-product ANDs (ROW_SEL = DEC3 x DEC4) are generated inside
    each row_group sheet, not here.

    7 INV + 36 AND = 43 ICs, 43 LEDs, 43 Rs
    """
    b = SchematicBuilder(title="Address Decoder (Full 2KB)",
                         page_size="A1", project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    # Column X positions (same as prototype minus final AND column)
    inv_x     = snap(base_x + 20 * GRID)
    dec3_l1_x = snap(inv_x   + 30 * GRID)
    dec3_l2_x = snap(dec3_l1_x + 22 * GRID)
    dec4_l1_x = snap(dec3_l2_x + 22 * GRID)
    dec4_l2_x = snap(dec4_l1_x + 22 * GRID)

    inv_pin_in_x = snap(inv_x - 15.24)

    # ================================================================
    # Input hier labels + inverter stage (A0-A6)
    # ================================================================
    for i in range(7):
        addr_bit = 6 - i
        hl_y = snap(base_y + i * 4 * GRID)
        b.add_hier_label(f"A{addr_bit}", base_x, hl_y,
                         shape="input", justify="right")

    inv_in_pins = []
    inv_out_pins = []
    for i in range(7):
        y = snap(base_y + i * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G04", inv_x, y)
        b.connect_power(pins)
        inv_in_pins.append(pins["2"])
        inv_out_pins.append(pins["4"])

    # Route hier label -> inverter input (staggered approach columns)
    approach_xs = [snap(inv_pin_in_x - (9 - i) * GRID) for i in range(7)]
    A5_STOP_X   = snap(approach_xs[2] - GRID)
    A5_DETOUR_Y = snap(base_y + 3.5 * SYM_SPACING_Y)

    for i in range(7):
        hl_y   = snap(base_y + i * 4 * GRID)
        pin_in = inv_in_pins[i]
        ax     = approach_xs[i]
        if abs(hl_y - pin_in[1]) < 0.01:
            b.add_wire(base_x, hl_y, pin_in[0], pin_in[1])
        elif i == 5:
            b.add_wire(base_x, hl_y, A5_STOP_X, hl_y)
            b.add_wire(A5_STOP_X, hl_y, A5_STOP_X, A5_DETOUR_Y)
            b.add_wire(A5_STOP_X, A5_DETOUR_Y, ax, A5_DETOUR_Y)
            b.add_wire(ax, A5_DETOUR_Y, ax, pin_in[1])
            b.add_wire(ax, pin_in[1], pin_in[0], pin_in[1])
        else:
            b.add_wire(base_x, hl_y, ax, hl_y)
            b.add_wire(ax, hl_y, ax, pin_in[1])
            b.add_wire(ax, pin_in[1], pin_in[0], pin_in[1])

    inv_out_x = inv_out_pins[0][0]
    inv_led_x = snap(inv_out_x + 2 * GRID)

    for i in range(7):
        addr_bit = 6 - i
        out = inv_out_pins[i]
        b.add_wire(out[0], out[1], inv_led_x, out[1])
        b.place_led_below(inv_led_x, out[1])
        label_x = snap(inv_led_x + GRID)
        b.add_wire(inv_led_x, out[1], label_x, out[1])
        b.add_label(f"nA{addr_bit}", label_x, out[1])

    # ================================================================
    # 3-to-8 sub-decoder L1: G0-G3
    # ================================================================
    g_decode = [(1, 1), (1, 0), (0, 1), (0, 0)]
    g_pins = []
    g_y_base = snap(base_y)
    for g in range(4):
        y = snap(g_y_base + g * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", dec3_l1_x, y)
        b.connect_power(pins)
        g_pins.append(pins)

    for g, (a2_inv, a1_inv) in enumerate(g_decode):
        pa = g_pins[g]["1"]
        pb = g_pins[g]["2"]
        lx = snap(pa[0] - 4 * GRID)
        b.add_wire(pa[0], pa[1], lx, pa[1])
        b.add_label("nA2" if a2_inv else "A2", lx, pa[1])
        lx = snap(pb[0] - 4 * GRID)
        b.add_wire(pb[0], pb[1], lx, pb[1])
        b.add_label("nA1" if a1_inv else "A1", lx, pb[1])

    g_out_x = snap(dec3_l1_x + 12.70)
    g_led_x = snap(g_out_x + 2 * GRID)

    for g in range(4):
        out = g_pins[g]["4"]
        b.add_wire(out[0], out[1], g_led_x, out[1])
        b.place_led_below(g_led_x, out[1])
        label_x = snap(g_led_x + GRID)
        b.add_wire(g_led_x, out[1], label_x, out[1])
        b.add_label(f"G{g}", label_x, out[1])

    # ================================================================
    # 3-to-8 sub-decoder L2: DEC3_0..7 -- ALL outputs as hier labels
    # ================================================================
    dec3_pins = []
    dec3_y_base = snap(base_y)
    for n in range(8):
        y = snap(dec3_y_base + n * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", dec3_l2_x, y)
        b.connect_power(pins)
        dec3_pins.append(pins)

    for n in range(8):
        g_idx  = n >> 1
        a0_inv = 1 - (n & 1)
        pa = dec3_pins[n]["1"]
        pb = dec3_pins[n]["2"]
        lx = snap(pa[0] - 4 * GRID)
        b.add_wire(pa[0], pa[1], lx, pa[1])
        b.add_label(f"G{g_idx}", lx, pa[1])
        lx = snap(pb[0] - 4 * GRID)
        b.add_wire(pb[0], pb[1], lx, pb[1])
        b.add_label("nA0" if a0_inv else "A0", lx, pb[1])

    dec3_out_x = snap(dec3_l2_x + 12.70)
    dec3_led_x = snap(dec3_out_x + 2 * GRID)
    dec3_hl_x  = snap(dec3_led_x + 8 * GRID)

    for n in range(8):
        out = dec3_pins[n]["4"]
        b.add_wire(out[0], out[1], dec3_led_x, out[1])
        b.place_led_below(dec3_led_x, out[1])
        b.add_wire(dec3_led_x, out[1], dec3_hl_x, out[1])
        b.add_hier_label(f"DEC3_{n}", dec3_hl_x, out[1],
                         shape="output", justify="left")

    # ================================================================
    # 4-to-16 sub-decoder L1 group A: HA0-HA3
    # ================================================================
    ha_decode = [(1, 1), (1, 0), (0, 1), (0, 0)]
    ha_pins = []
    ha_y_base = snap(base_y)
    for g in range(4):
        y = snap(ha_y_base + g * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", dec4_l1_x, y)
        b.connect_power(pins)
        ha_pins.append(pins)

    for g, (a4_inv, a3_inv) in enumerate(ha_decode):
        pa = ha_pins[g]["1"]
        pb = ha_pins[g]["2"]
        lx = snap(pa[0] - 4 * GRID)
        b.add_wire(pa[0], pa[1], lx, pa[1])
        b.add_label("nA4" if a4_inv else "A4", lx, pa[1])
        lx = snap(pb[0] - 4 * GRID)
        b.add_wire(pb[0], pb[1], lx, pb[1])
        b.add_label("nA3" if a3_inv else "A3", lx, pb[1])

    # L1 group B: HB0-HB3
    hb_decode = [(1, 1), (1, 0), (0, 1), (0, 0)]
    hb_pins = []
    hb_y_base = snap(ha_y_base + 5 * SYM_SPACING_Y)
    for g in range(4):
        y = snap(hb_y_base + g * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", dec4_l1_x, y)
        b.connect_power(pins)
        hb_pins.append(pins)

    for g, (a6_inv, a5_inv) in enumerate(hb_decode):
        pa = hb_pins[g]["1"]
        pb = hb_pins[g]["2"]
        lx = snap(pa[0] - 4 * GRID)
        b.add_wire(pa[0], pa[1], lx, pa[1])
        b.add_label("nA6" if a6_inv else "A6", lx, pa[1])
        lx = snap(pb[0] - 4 * GRID)
        b.add_wire(pb[0], pb[1], lx, pb[1])
        b.add_label("nA5" if a5_inv else "A5", lx, pb[1])

    dec4_l1_out_x = snap(dec4_l1_x + 12.70)
    dec4_l1_led_x = snap(dec4_l1_out_x + 2 * GRID)

    for g in range(4):
        out = ha_pins[g]["4"]
        b.add_wire(out[0], out[1], dec4_l1_led_x, out[1])
        b.place_led_below(dec4_l1_led_x, out[1])
        label_x = snap(dec4_l1_led_x + GRID)
        b.add_wire(dec4_l1_led_x, out[1], label_x, out[1])
        b.add_label(f"HA{g}", label_x, out[1])

    for g in range(4):
        out = hb_pins[g]["4"]
        b.add_wire(out[0], out[1], dec4_l1_led_x, out[1])
        b.place_led_below(dec4_l1_led_x, out[1])
        label_x = snap(dec4_l1_led_x + GRID)
        b.add_wire(dec4_l1_led_x, out[1], label_x, out[1])
        b.add_label(f"HB{g}", label_x, out[1])

    # ================================================================
    # 4-to-16 sub-decoder L2: DEC4_0..15 -- ALL outputs as hier labels
    # ================================================================
    dec4_pins = []
    dec4_y_base = snap(base_y)
    for n in range(16):
        y = snap(dec4_y_base + n * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", dec4_l2_x, y)
        b.connect_power(pins)
        dec4_pins.append(pins)

    for n in range(16):
        hb_idx = n >> 2
        ha_idx = n & 3
        pa = dec4_pins[n]["1"]
        pb = dec4_pins[n]["2"]
        lx_hb = snap(pa[0] - 4 * GRID)
        lx_ha = snap(pb[0] - 4 * GRID)
        b.add_wire(pa[0], pa[1], lx_hb, pa[1])
        b.add_label(f"HB{hb_idx}", lx_hb, pa[1])
        b.add_wire(pb[0], pb[1], lx_ha, pb[1])
        b.add_label(f"HA{ha_idx}", lx_ha, pb[1])

    dec4_out_x = snap(dec4_l2_x + 12.70)
    dec4_led_x = snap(dec4_out_x + 2 * GRID)
    dec4_hl_x  = snap(dec4_led_x + 8 * GRID)

    for n in range(16):
        out = dec4_pins[n]["4"]
        b.add_wire(out[0], out[1], dec4_led_x, out[1])
        b.place_led_below(dec4_led_x, out[1])
        b.add_wire(dec4_led_x, out[1], dec4_hl_x, out[1])
        b.add_hier_label(f"DEC4_{n}", dec4_hl_x, out[1],
                         shape="output", justify="left")

    return b


# Reuse prototype generators directly (same project name "ram")
def generate_column_select():
    return _proto_column_select()

def generate_control_logic():
    return _proto_control_logic()

def generate_row_control():
    return _proto_row_control()

def generate_byte_sheet():
    return _proto_byte_sheet()

def generate_power_supply():
    return _proto_power_supply()


# ==============================================================
# NEW: Row group sheet (8 final ANDs + 8 row instances)
# ==============================================================

def generate_row_group():
    """Row group: 8 final cross-product ANDs + 8 row sheet blocks.

    Hier pins: DEC4 (1 input -- specific to this row group instance)
    Global labels: DEC3_0..7, WRITE_ACTIVE, READ_EN, COL_SEL_0..15, D0..D7

    Internal:
      ROW_SEL_i = AND(DEC3_i, DEC4) for i=0..7
      Each ROW_SEL_i drives row block i's ROW_SEL hier pin.

    8 AND ICs + 8 LEDs + 8 Rs
    """
    b = SchematicBuilder(title="Row Group", page_size="A1",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48
    wire_stub = 5.08

    and_x = snap(base_x + 25 * GRID)
    hl_out_x = snap(and_x + 22 * GRID)
    row_block_x = snap(hl_out_x + 10 * GRID)
    row_block_w = snap(20 * GRID)

    # -- Hier label: DEC4 input --
    dec4_hier_y = snap(base_y)
    b.add_hier_label("DEC4", base_x, dec4_hier_y, shape="input", justify="right")

    # -- 8 AND gates: ROW_SEL_i = AND(DEC3_i, DEC4) --
    and_pins_list = []
    and_y_base = snap(base_y + 3 * SYM_SPACING_Y)
    for i in range(ROWS_PER_GROUP):
        y = snap(and_y_base + i * SYM_SPACING_Y)
        _, pins = b.place_symbol("74LVC1G08", and_x, y)
        b.connect_power(pins)
        and_pins_list.append(pins)

    # Wire DEC3_i (global label) -> AND pin 1, DEC4 (hier) -> AND pin 2
    # Trunk X must be left of global label right edge (~pa[0]-4*GRID + 6mm)
    dec4_trunk_x = snap(and_pins_list[0]["2"][0] - 8 * GRID)
    b.add_wire(base_x, dec4_hier_y, dec4_trunk_x, dec4_hier_y)
    trunk_ys = [dec4_hier_y] + [and_pins_list[i]["2"][1]
                                for i in range(ROWS_PER_GROUP)]
    b.add_segmented_trunk(dec4_trunk_x, sorted(set(snap(y) for y in trunk_ys)))

    for i in range(ROWS_PER_GROUP):
        # DEC3_i via global label (left of AND pin 1, clear of trunk)
        pa = and_pins_list[i]["1"]
        lx = snap(pa[0] - 4 * GRID)
        b.add_wire(pa[0], pa[1], lx, pa[1])
        b.add_global_label(f"DEC3_{i}", lx, pa[1], shape="input",
                           angle=180)
        # DEC4 trunk -> pin 2
        pb = and_pins_list[i]["2"]
        b.add_wire(dec4_trunk_x, pb[1], pb[0], pb[1])

    # AND output -> LED -> row block ROW_SEL
    sheet_gap = 5 * GRID
    row_h = _sheet_height(1)
    row_pp = []

    for i in range(ROWS_PER_GROUP):
        out = and_pins_list[i]["4"]
        led_x = snap(out[0] + 2 * GRID)
        b.add_wire(out[0], out[1], led_x, out[1])
        b.place_led_below(led_x, out[1])
        b.add_wire(led_x, out[1], hl_out_x, out[1])

        # Row sheet block (1 hier pin: ROW_SEL)
        row_sy = snap(and_y_base + i * SYM_SPACING_Y - row_h / 2 + 1.27)
        pp = _add_sheet_block(
            b, f"Row {i}", "row.kicad_sch",
            [("ROW_SEL", "input")],
            row_block_x, row_sy, row_block_w, row_h, _GREEN)
        row_pp.append(pp)

        # Wire AND output -> row block ROW_SEL
        dst_x, dst_y = pp["ROW_SEL"]
        b.add_wire(hl_out_x, out[1], hl_out_x, dst_y)
        b.add_wire(hl_out_x, dst_y, dst_x, dst_y)

    return b


# ==============================================================
# NEW: Row sheet (row_control + 16 bytes)
# ==============================================================

def generate_row():
    """Single row: 1 row_control + 16 byte instances.

    Hier pin: ROW_SEL (1 input -- specific to this row)
    Global labels: WRITE_ACTIVE, READ_EN, COL_SEL_0..15, D0..D7
    Local labels: WRITE_EN_ROW, READ_EN_ROW (from row_control to bytes)
    """
    b = SchematicBuilder(title="Memory Row (16 Bytes)", page_size="A1",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 25.4
    wire_stub = 5.08
    sheet_gap = 3 * GRID

    # -- Row control sheet block --
    rc_pin_defs = [
        ("WRITE_ACTIVE", "input"), ("READ_EN", "input"), ("ROW_SEL", "input"),
        ("WRITE_EN_ROW", "output"), ("READ_EN_ROW", "output"),
    ]
    rc_right_names = {"WRITE_EN_ROW", "READ_EN_ROW"}
    rc_w = snap(28 * GRID)
    rc_h = _sheet_height(max(3, 2))
    rc_x = snap(base_x + 5 * GRID)
    rc_y = snap(base_y)

    rc_pp = _add_sheet_block(b, "Row Control", "row_control.kicad_sch",
                             rc_pin_defs, rc_x, rc_y, rc_w, rc_h, _YELLOW,
                             right_pins=rc_right_names)

    # Hier label ROW_SEL -> row_control
    b.add_hier_label("ROW_SEL", base_x, rc_pp["ROW_SEL"][1],
                     shape="input", justify="right")
    b.add_wire(base_x, rc_pp["ROW_SEL"][1],
               rc_pp["ROW_SEL"][0], rc_pp["ROW_SEL"][1])

    # Global labels -> row_control inputs
    for sig in ["WRITE_ACTIVE", "READ_EN"]:
        px, py = rc_pp[sig]
        b.add_wire(px, py, px - wire_stub, py)
        b.add_global_label(sig, px - wire_stub, py, shape="input",
                           angle=180)

    # Row control outputs -> local labels
    for sig in ["WRITE_EN_ROW", "READ_EN_ROW"]:
        px, py = rc_pp[sig]
        b.add_wire(px, py, px + wire_stub, py)
        b.add_label(sig, px + wire_stub, py)

    # -- 16 byte sheet blocks in 4x4 grid --
    byte_pin_defs = [("COL_SEL", "input"),
                     ("WRITE_EN_ROW", "input"), ("READ_EN_ROW", "input")]
    byte_pin_defs += [(f"D{bit}", "bidirectional") for bit in range(8)]
    byte_w = snap(22 * GRID)
    byte_h = _sheet_height(len(byte_pin_defs))
    # Wider gaps to fit global labels (COL_SEL_nn, DBUS_n) between blocks
    byte_gap_x = snap(18 * GRID)
    byte_gap_y = snap(3 * GRID)
    # Longer stub for global labels so text clears the pin name
    glabel_stub = snap(8 * GRID)

    byte_area_x = snap(rc_x + rc_w + 20 * GRID)
    byte_area_y = snap(base_y)

    byte_pp = []
    for col_idx in range(NUM_COLS):
        grid_col = col_idx % 4
        grid_row = col_idx // 4
        sx = snap(byte_area_x + grid_col * (byte_w + byte_gap_x))
        sy = snap(byte_area_y + grid_row * (byte_h + byte_gap_y))
        pp = _add_sheet_block(
            b, f"Byte {col_idx}", "byte.kicad_sch",
            byte_pin_defs, sx, sy, byte_w, byte_h, _GREEN)
        byte_pp.append(pp)

    # Wire signals to each byte block via labels
    for col_idx in range(NUM_COLS):
        pp = byte_pp[col_idx]
        # COL_SEL_<col_idx> (global label) -> byte COL_SEL hier pin
        # angle=180 + justify=right so label text reads toward the pin
        px, py = pp["COL_SEL"]
        b.add_wire(px, py, px - glabel_stub, py)
        b.add_global_label(f"COL_SEL_{col_idx}", px - glabel_stub, py,
                           shape="input", angle=180)
        # WRITE_EN_ROW, READ_EN_ROW (local labels, row-internal)
        for sig in ["WRITE_EN_ROW", "READ_EN_ROW"]:
            px, py = pp[sig]
            b.add_wire(px, py, px - glabel_stub, py)
            b.add_label(sig, px - glabel_stub, py, justify="right")
        # D0..D7: global labels matching the byte hier pin names.
        for bit in range(8):
            sig = f"D{bit}"
            px, py = pp[sig]
            b.add_wire(px, py, px - glabel_stub, py)
            b.add_global_label(sig, px - glabel_stub, py,
                               shape="bidirectional", angle=180)

    return b


# ==============================================================
# Root sheet
# ==============================================================

def generate_root_sheet():
    """Root: connector + decoders + control + power + 16 row groups.

    Layout:
      Far left: Connector (24-pin) + bus LEDs
      Col 1: Address Decoder, Column Select
      Col 2: Control Logic, Power Supply
      Right: 16 row group blocks (4x4 grid)
    """
    b = SchematicBuilder(title="2KB Discrete RAM (128x16x8)",
                         page_size="A0", project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 25.4
    wire_stub = 5.08

    # ================================================================
    # Column positions
    # ================================================================
    col1_x = snap(base_x + 50 * GRID)
    col1_w = snap(32 * GRID)
    inter_col_gap = snap(15 * GRID)
    col2_x = snap(col1_x + col1_w + inter_col_gap)
    col2_w = snap(28 * GRID)
    rg_gap = snap(15 * GRID)
    rg_area_x = snap(col2_x + col2_w + rg_gap)
    rg_w = snap(20 * GRID)
    rg_gap_x = snap(10 * GRID)
    rg_gap_y = snap(5 * GRID)

    # ================================================================
    # Address Decoder (7 in: A6-A0, 24 out: DEC3_0..7 + DEC4_0..15)
    # ================================================================
    addr_left_defs = [(f"A{6-i}", "input") for i in range(7)]
    addr_right_defs = ([(f"DEC3_{i}", "output") for i in range(NUM_DEC3)]
                       + [(f"DEC4_{i}", "output") for i in range(NUM_DEC4)])
    addr_pin_defs = addr_left_defs + addr_right_defs
    addr_right_names = ({f"DEC3_{i}" for i in range(NUM_DEC3)}
                        | {f"DEC4_{i}" for i in range(NUM_DEC4)})
    addr_h = _sheet_height(max(len(addr_left_defs), len(addr_right_defs)))
    addr_sy = base_y
    addr_pp = _add_sheet_block(b, "Address Decoder", "address_decoder.kicad_sch",
                               addr_pin_defs, col1_x, addr_sy,
                               col1_w, addr_h, _YELLOW,
                               right_pins=addr_right_names)

    # ================================================================
    # Column Select (4 in: A7-A10, 16 out: COL_SEL_0..15)
    # ================================================================
    colsel_left_defs = [(f"A{7+i}", "input") for i in range(4)]
    colsel_right_defs = [(f"COL_SEL_{i}", "output") for i in range(NUM_COLS)]
    colsel_pin_defs = colsel_left_defs + colsel_right_defs
    colsel_right_names = {f"COL_SEL_{i}" for i in range(NUM_COLS)}
    colsel_h = _sheet_height(max(len(colsel_left_defs), len(colsel_right_defs)))
    colsel_sy = snap(addr_sy + addr_h + 5 * GRID)
    colsel_pp = _add_sheet_block(b, "Column Select", "column_select.kicad_sch",
                                 colsel_pin_defs, col1_x, colsel_sy,
                                 col1_w, colsel_h, _BLUE,
                                 right_pins=colsel_right_names)

    # ================================================================
    # Control Logic (3 in: nCE, nOE, nWE, 2 out: WRITE_ACTIVE, READ_EN)
    # ================================================================
    ctrl_left_defs = [("nCE", "input"), ("nOE", "input"), ("nWE", "input")]
    ctrl_right_defs = [("WRITE_ACTIVE", "output"), ("READ_EN", "output")]
    ctrl_pin_defs = ctrl_left_defs + ctrl_right_defs
    ctrl_right_names = {"WRITE_ACTIVE", "READ_EN"}
    ctrl_h = _sheet_height(max(len(ctrl_left_defs), len(ctrl_right_defs)))
    ctrl_sy = snap(base_y)
    ctrl_pp = _add_sheet_block(b, "Control Logic", "control_logic.kicad_sch",
                               ctrl_pin_defs, col2_x, ctrl_sy,
                               col2_w, ctrl_h, _ORANGE,
                               right_pins=ctrl_right_names)

    # ================================================================
    # Power Supply (no hier pins)
    # ================================================================
    pwr_w = snap(20 * GRID)
    pwr_h = snap(10 * GRID)
    pwr_sy = snap(ctrl_sy + ctrl_h + 5 * GRID)
    _add_sheet_block(b, "Power Supply", "power_supply.kicad_sch",
                     [], col2_x, pwr_sy, pwr_w, pwr_h, _PURPLE)

    # ================================================================
    # 16 Row Group blocks (4x4 grid, each has 1 hier pin: DEC4)
    # ================================================================
    rg_h = _sheet_height(1)
    rg_pp = []
    for rg_idx in range(NUM_ROW_GROUPS):
        grid_col = rg_idx % 4
        grid_row = rg_idx // 4
        sx = snap(rg_area_x + grid_col * (rg_w + rg_gap_x))
        sy = snap(base_y + grid_row * (rg_h + rg_gap_y))
        pp = _add_sheet_block(
            b, f"Row Group {rg_idx}", "row_group.kicad_sch",
            [("DEC4", "input")],
            sx, sy, rg_w, rg_h, _PINK)
        rg_pp.append(pp)

    # ================================================================
    # Connector (24-pin, same as prototype)
    # ================================================================
    # Pre-compute vertical center of the block ensemble
    rg_bottom_y = snap(base_y + 3 * (rg_h + rg_gap_y) + rg_h)
    ensemble_bottom = max(snap(colsel_sy + colsel_h), rg_bottom_y)
    ensemble_center_y = snap((base_y + ensemble_bottom) / 2)

    conn_x = base_x
    conn_y = snap(ensemble_center_y + 1.27)
    _, conn_pins = b.place_symbol("Conn_01x24", conn_x, conn_y,
                                  ref_prefix="J", value="SRAM_Bus", angle=180)

    signal_names = [
        "A7", "A8", "A9", "A10",
        "nCE", "nWE", "nOE",
        "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7",
        "A0", "A1", "A2", "A3", "A4", "A5", "A6",
    ]
    conn_signal_pos = {}
    for pin_num_int, sig in enumerate(signal_names, start=2):
        conn_signal_pos[sig] = conn_pins[str(pin_num_int)]

    # Power pins
    vcc_pos = conn_pins["24"]
    gnd_pos = conn_pins["1"]
    pwr_wire_len = snap(3 * GRID)
    b.add_wire(vcc_pos[0], vcc_pos[1],
               vcc_pos[0] + pwr_wire_len, vcc_pos[1])
    b.place_power("VCC", vcc_pos[0] + pwr_wire_len, vcc_pos[1])
    b.add_wire(gnd_pos[0], gnd_pos[1],
               gnd_pos[0] + pwr_wire_len, gnd_pos[1])
    b.place_power("GND", gnd_pos[0] + pwr_wire_len, gnd_pos[1])

    # ================================================================
    # Signal fan-out: connector -> LEDs -> routing
    # ================================================================
    conn_pin_x = conn_signal_pos[signal_names[0]][0]
    led_order = sorted(signal_names, key=lambda s: conn_signal_pos[s][1])
    n_signals = len(led_order)

    fan_spacing = snap(5 * GRID)
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

    # Direct wire destinations for address signals
    direct_wire_dest = {}
    for i in range(7):
        direct_wire_dest[f"A{i}"] = addr_pp[f"A{i}"]
    for i in range(7, 11):
        direct_wire_dest[f"A{i}"] = colsel_pp[f"A{i}"]

    direct_signals_order = ([f"A{i}" for i in range(7)]
                            + [f"A{i}" for i in range(7, 11)])
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
            # D0-D7, nCE/nOE/nWE -> global labels
            b.add_wire(led_jct_x, ty, label_x, ty)
            if sig.startswith("D") and sig[1:].isdigit():
                b.add_global_label(sig, label_x, ty,
                                   shape="bidirectional")
            elif sig.startswith("n"):
                b.add_label(sig, label_x, ty)  # local for control
            else:
                b.add_global_label(sig, label_x, ty, shape="output")

    # ================================================================
    # Control logic inputs (nCE/nOE/nWE) via local labels
    # ================================================================
    for sig in ["nCE", "nOE", "nWE"]:
        px, py = ctrl_pp[sig]
        b.add_wire(px, py, px - wire_stub, py)
        b.add_label(sig, px - wire_stub, py, justify="right")

    # ================================================================
    # Control logic outputs -> global labels
    # ================================================================
    glabel_right_stub = snap(8 * GRID)  # longer stub so text clears pin names
    for sig in ["WRITE_ACTIVE", "READ_EN"]:
        px, py = ctrl_pp[sig]
        b.add_wire(px, py, px + wire_stub, py)
        b.add_global_label(sig, px + wire_stub, py, shape="output")

    # ================================================================
    # Address decoder DEC3 outputs -> global labels
    # ================================================================
    for i in range(NUM_DEC3):
        sig = f"DEC3_{i}"
        px, py = addr_pp[sig]
        b.add_wire(px, py, px + glabel_right_stub, py)
        b.add_global_label(sig, px + glabel_right_stub, py, shape="output")

    # ================================================================
    # Address decoder DEC4 outputs -> row group DEC4 hier pins
    # ================================================================
    for rg_idx in range(NUM_ROW_GROUPS):
        dec4_sig = f"DEC4_{rg_idx}"
        src_x, src_y = addr_pp[dec4_sig]
        dst_x, dst_y = rg_pp[rg_idx]["DEC4"]
        lbl = f"DEC4_{rg_idx}"
        b.add_wire(src_x, src_y, src_x + glabel_right_stub, src_y)
        b.add_label(lbl, src_x + glabel_right_stub, src_y)
        b.add_wire(dst_x, dst_y, dst_x - wire_stub, dst_y)
        b.add_label(lbl, dst_x - wire_stub, dst_y, justify="right")

    # ================================================================
    # Column select outputs -> global labels
    # ================================================================
    for i in range(NUM_COLS):
        sig = f"COL_SEL_{i}"
        px, py = colsel_pp[sig]
        b.add_wire(px, py, px + glabel_right_stub, py)
        b.add_global_label(sig, px + glabel_right_stub, py, shape="output")

    return b


# ==============================================================
# Instance path fixing for 4-level hierarchy
# ==============================================================

def fix_instance_paths(builders):
    """Assign globally unique references and instance paths for all symbols.

    Hierarchy: root -> row_group(16) -> row(8 each) -> row_control(1) + byte(16)
    """
    root_sch = builders["ram"].sch
    root_uuid = root_sch.uuid
    global_counters = {}

    # Initialize counters from root-level symbols (connector, power, LEDs)
    for sym in root_sch.schematicSymbols:
        for p in sym.properties:
            if p.key == "Reference":
                prefix = p.value.rstrip("0123456789")
                num_str = p.value[len(prefix):]
                num = int(num_str) if num_str else 0
                global_counters[prefix] = max(
                    global_counters.get(prefix, 0), num)
                break

    page_counter = [1]  # root is page 1

    def _assign_symbol_refs(builder, path):
        """Add one instance path per ref-group in builder."""
        ref_groups = defaultdict(list)
        for sym in builder.sch.schematicSymbols:
            for p in sym.properties:
                if p.key == "Reference":
                    ref_groups[p.value].append(sym)
                    break
        for template_ref, syms in ref_groups.items():
            prefix = template_ref.rstrip("0123456789")
            global_counters[prefix] = global_counters.get(prefix, 0) + 1
            inst_ref = f"{prefix}{global_counters[prefix]}"
            for sym in syms:
                if not sym.instances:
                    sym.instances = [SymbolProjectInstance(
                        name=PROJECT_NAME, paths=[])]
                sym.instances[0].paths.append(SymbolProjectPath(
                    sheetInstancePath=path,
                    reference=inst_ref,
                    unit=sym.unit,
                ))

    def _add_sheet_inst(sheet_block, parent_path):
        """Add an instance entry to a HierarchicalSheet block."""
        page_counter[0] += 1
        if not sheet_block.instances:
            sheet_block.instances = [HierarchicalSheetProjectInstance(
                name=PROJECT_NAME, paths=[])]
        sheet_block.instances[0].paths.append(HierarchicalSheetProjectPath(
            sheetInstancePath=f"{parent_path}/{sheet_block.uuid}/",
            page=str(page_counter[0]),
        ))

    # -- Level 0: root's direct sub-sheets (single-instance) --
    single_sheets = {"address_decoder", "column_select",
                     "control_logic", "power_supply"}
    for sheet in root_sch.sheets:
        fname = sheet.fileName.value.replace(".kicad_sch", "")
        if fname in single_sheets and fname in builders:
            path = f"/{root_uuid}/{sheet.uuid}"
            _assign_symbol_refs(builders[fname], path)
            _add_sheet_inst(sheet, f"/{root_uuid}")

    # -- Levels 1-3: row_group -> row -> row_control/byte --
    rg_sheets = [s for s in root_sch.sheets
                 if s.fileName.value == "row_group.kicad_sch"]
    rg_builder = builders["row_group"]
    row_builder = builders["row"]
    rc_builder = builders["row_control"]
    byte_builder = builders["byte"]

    row_blocks_in_rg = [s for s in rg_builder.sch.sheets
                        if s.fileName.value == "row.kicad_sch"]
    rc_blocks_in_row = [s for s in row_builder.sch.sheets
                        if s.fileName.value == "row_control.kicad_sch"]
    byte_blocks_in_row = [s for s in row_builder.sch.sheets
                          if s.fileName.value == "byte.kicad_sch"]

    for rg_sheet in rg_sheets:
        rg_path = f"/{root_uuid}/{rg_sheet.uuid}"
        _add_sheet_inst(rg_sheet, f"/{root_uuid}")
        _assign_symbol_refs(rg_builder, rg_path)

        for row_block in row_blocks_in_rg:
            row_path = f"{rg_path}/{row_block.uuid}"
            _add_sheet_inst(row_block, rg_path)

            # row.kicad_sch has no symbols of its own (only sheet blocks)
            # but assign anyway in case power symbols exist
            if row_builder.sch.schematicSymbols:
                _assign_symbol_refs(row_builder, row_path)

            for rc_block in rc_blocks_in_row:
                rc_path = f"{row_path}/{rc_block.uuid}"
                _add_sheet_inst(rc_block, row_path)
                _assign_symbol_refs(rc_builder, rc_path)

            for byte_block in byte_blocks_in_row:
                byte_path = f"{row_path}/{byte_block.uuid}"
                _add_sheet_inst(byte_block, row_path)
                _assign_symbol_refs(byte_builder, byte_path)


# ==============================================================
# Component counting
# ==============================================================

def count_components(builders):
    """Count total ICs, LEDs, resistors across all sheets."""
    totals = {"U": 0, "D": 0, "R": 0, "C": 0, "#PWR": 0, "J": 0, "#FLG": 0}

    multipliers = {
        "byte": TOTAL_BYTES,         # 2048
        "row_control": TOTAL_ROWS,   # 128
        "row_group": NUM_ROW_GROUPS, # 16
        "row": TOTAL_ROWS,           # 128 (but row has no symbols typically)
    }

    for name, builder in builders.items():
        mult = multipliers.get(name, 1)
        for prefix, count in builder._ref_counters.items():
            actual = count - 1
            if prefix in totals:
                totals[prefix] += actual * mult
            else:
                totals[prefix] = actual * mult
    return totals


# ==============================================================
# Main
# ==============================================================

def main():
    print("=" * 60)
    print("Discrete NES - Full 2KB RAM (128 rows x 16 cols x 8 bits)")
    print("=" * 60)

    get_pin_offsets(board_dir=BOARD_DIR)

    print("\nGenerating sub-sheets...")
    builders = {}

    builders["address_decoder"] = generate_address_decoder()
    print("  [+] address_decoder.kicad_sch")

    builders["column_select"] = generate_column_select()
    print("  [+] column_select.kicad_sch (shared with prototype)")

    builders["control_logic"] = generate_control_logic()
    print("  [+] control_logic.kicad_sch (shared with prototype)")

    builders["row_control"] = generate_row_control()
    print("  [+] row_control.kicad_sch (shared, 128 instances)")

    builders["byte"] = generate_byte_sheet()
    print("  [+] byte.kicad_sch (shared, 2048 instances)")

    builders["power_supply"] = generate_power_supply()
    print("  [+] power_supply.kicad_sch (shared with prototype)")

    builders["row_group"] = generate_row_group()
    print("  [+] row_group.kicad_sch (NEW, 16 instances)")

    builders["row"] = generate_row()
    print("  [+] row.kicad_sch (NEW, 128 instances)")

    builders["ram"] = generate_root_sheet()
    print("  [+] ram.kicad_sch (root)")

    print("\n  Fixing hierarchical instance paths (4-level hierarchy)...")
    print(f"    {TOTAL_BYTES} byte instances, {TOTAL_ROWS} row instances, "
          f"{NUM_ROW_GROUPS} row group instances")
    fix_instance_paths(builders)
    print("  [*] Done")

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
    print(f"  Capacitors (C): {totals.get('C', 0)}")
    print(f"  Connectors (J): {totals.get('J', 0)}")
    print(f"  Power (#PWR):   {totals.get('#PWR', 0)}")
    total_parts = (totals.get('U', 0) + totals.get('D', 0)
                   + totals.get('R', 0) + totals.get('C', 0)
                   + totals.get('J', 0))
    print(f"  ----------------------------")
    print(f"  Total BOM parts: {total_parts}")
    print()

    print("Done! Open ram.kicad_sch in KiCad to view the design.")


if __name__ == "__main__":
    main()
