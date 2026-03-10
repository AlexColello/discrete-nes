#!/usr/bin/env python3
"""
Generate hierarchical KiCad schematics for the 8-byte discrete RAM prototype.

Circuit architecture (row/column addressing):
  - A0, A1 -> 2-to-4 row decoder -> ROW_SEL_0..3
  - A2 -> column select -> COL_SEL_0, COL_SEL_1
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
  address_decoder.kicad_sch  -- 2 inverters + 4 two-input ANDs (2-to-4 row decoder)
  column_select.kicad_sch    -- 1 inverter (A2 -> COL_SEL_0/COL_SEL_1)
  control_logic.kicad_sch    -- /CE,/OE,/WE inversion + WRITE_ACTIVE, READ_EN logic
  write_en_gen.kicad_sch     -- 4 ANDs generating WRITE_EN_ROW_0..3
  read_en_gen.kicad_sch      -- 4 ANDs generating READ_EN_ROW_0..3
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
    Address decoder: 2-to-4 row decoder using inverters and 2-input ANDs.

    Inputs:  A0, A1 (only 2 bits — A2 goes to column select sheet)
    Outputs: ROW_SEL_0..ROW_SEL_3

    ROW_SEL_0 = /A1 & /A0
    ROW_SEL_1 = /A1 &  A0
    ROW_SEL_2 =  A1 & /A0
    ROW_SEL_3 =  A1 &  A0
    """
    b = SchematicBuilder(title="Address Decoder", page_size="A3",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    inv_x = base_x + 18 * GRID
    and_x = base_x + 42 * GRID
    hl_out_x = and_x + 22 * GRID

    # X positions for 4 vertical trunks (A0, A1, A0_INV, A1_INV)
    inv_in_x = inv_x - 15.24  # inverter input pin X (74LVC1G04 pin 2)
    addr_trunk_x = [snap(inv_in_x - (1.5 + i) * GRID) for i in range(2)]
    inv_trunk_x = [base_x + 31 * GRID + i * 2 * GRID for i in range(2)]

    # -- Hierarchical labels for A0-A1, wired to trunk tops --
    for i in range(2):
        trunk_top_y = snap(base_y - (4 - i) * GRID)
        b.add_hier_label(f"A{i}", base_x, trunk_top_y, shape="input", justify="right")
        b.add_wire(base_x, trunk_top_y, addr_trunk_x[i], trunk_top_y)

    # -- Two inverters for complemented address bits --
    inv_in_pins = []
    inv_out_pins = []
    for i in range(2):
        y = base_y + i * SYM_SPACING_Y + 5 * GRID
        _, pins = b.place_symbol("74LVC1G04", inv_x, y)
        b.connect_power(pins)
        inv_in_pins.append(pins["2"])
        inv_out_pins.append(pins["4"])

    # Decode table: (A1, A0) for each ROW_SEL
    decode_table = [
        (0, 0),  # ROW_SEL_0 = /A1 & /A0
        (0, 1),  # ROW_SEL_1 = /A1 &  A0
        (1, 0),  # ROW_SEL_2 =  A1 & /A0
        (1, 1),  # ROW_SEL_3 =  A1 &  A0
    ]

    # Place 4 AND gates (2-input) to get pin positions
    and_gate_pins = []
    for sel_idx in range(4):
        y = base_y + sel_idx * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G08", and_x, y)
        b.connect_power(pins)
        and_gate_pins.append(pins)

    # AND pin mapping: A0 -> pin "1", A1 -> pin "2"
    addr_pin_map = {0: "1", 1: "2"}

    # -- Wire each address bit trunk (A0, A1) --
    for addr_i in range(2):
        trunk_x = addr_trunk_x[addr_i]
        trunk_top_y = snap(base_y - (4 - addr_i) * GRID)
        inv_in = inv_in_pins[addr_i]
        pin_num = addr_pin_map[addr_i]

        trunk_ys = [trunk_top_y, inv_in[1]]
        branch_targets = [(inv_in[0], inv_in[1])]
        for sel_idx, bits in enumerate(decode_table):
            if bits[1 - addr_i] == 1:  # bits = (A1, A0), index 0=A1, 1=A0
                target = and_gate_pins[sel_idx][pin_num]
                trunk_ys.append(target[1])
                branch_targets.append((target[0], target[1]))

        b.add_segmented_trunk(trunk_x, trunk_ys)

        for tx, ty in branch_targets:
            b.add_wire(trunk_x, ty, tx, ty)

    # -- Wire each inverted bit trunk (A0_INV, A1_INV) --
    for addr_i in range(2):
        trunk_x = inv_trunk_x[addr_i]
        out_pin = inv_out_pins[addr_i]
        pin_num = addr_pin_map[addr_i]

        led_jct_x = out_pin[0] + 2 * GRID
        b.add_wire(out_pin[0], out_pin[1], led_jct_x, out_pin[1])
        b.add_wire(led_jct_x, out_pin[1], trunk_x, out_pin[1])
        b.place_led_below(led_jct_x, out_pin[1])  # default 3*GRID drop

        branch_targets = []
        trunk_ys = [out_pin[1]]
        for sel_idx, bits in enumerate(decode_table):
            if bits[1 - addr_i] == 0:
                target = and_gate_pins[sel_idx][pin_num]
                branch_targets.append((target[0], target[1]))
                trunk_ys.append(target[1])

        if branch_targets:
            b.add_segmented_trunk(trunk_x, trunk_ys)
            for tx, ty in branch_targets:
                b.add_wire(trunk_x, ty, tx, ty)

    # -- ROW_SEL outputs: AND output -> LED T-junction -> wire -> hier label --
    for sel_idx in range(4):
        pins = and_gate_pins[sel_idx]
        out_pin = pins["4"]

        led_jct_x = out_pin[0] + 2 * GRID
        b.add_wire(out_pin[0], out_pin[1], led_jct_x, out_pin[1])
        b.add_wire(led_jct_x, out_pin[1], hl_out_x, out_pin[1])
        b.place_led_below(led_jct_x, out_pin[1])
        b.add_hier_label(f"ROW_SEL_{sel_idx}", hl_out_x, out_pin[1],
                         shape="output", justify="left")

    return b


def generate_column_select():
    """
    Column select: A2 -> two inverters -> COL_SEL_0, COL_SEL_1.

    Inputs:  A2
    Outputs: COL_SEL_0 = !A2  (column 0 selected when A2=0)
             COL_SEL_1 = !!A2 (column 1 selected when A2=1, via double inversion)

    Uses double inverter for COL_SEL_1 to create a clean separate net
    (avoids ERC multiple_net_names warning) and provides an LED on each output.
    """
    b = SchematicBuilder(title="Column Select", page_size="A3",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    inv1_x = base_x + 18 * GRID
    inv2_x = inv1_x + 25 * GRID
    hl_out_x = inv2_x + 22 * GRID

    # -- A2 input --
    a2_hier_y = base_y
    b.add_hier_label("A2", base_x, a2_hier_y, shape="input", justify="right")

    # -- INV1: COL_SEL_0 = !A2 --
    _, inv1_pins = b.place_symbol("74LVC1G04", inv1_x, base_y)
    b.connect_power(inv1_pins)
    inv1_in = inv1_pins["2"]
    inv1_out = inv1_pins["4"]

    # Wire A2 to INV1 input
    b.add_wire(base_x, a2_hier_y, inv1_in[0], inv1_in[1])

    # INV1 output -> LED -> COL_SEL_0 label + wire to INV2
    inv1_led_x = snap(inv1_out[0] + 2 * GRID)
    b.add_wire(inv1_out[0], inv1_out[1], inv1_led_x, inv1_out[1])
    b.add_wire(inv1_led_x, inv1_out[1], hl_out_x, inv1_out[1])
    b.place_led_below(inv1_led_x, inv1_out[1])
    b.add_hier_label("COL_SEL_0", hl_out_x, inv1_out[1],
                     shape="output", justify="left")

    # -- INV2: COL_SEL_1 = !!A2 (double inversion = A2) --
    inv2_y = snap(base_y + SYM_SPACING_Y)
    _, inv2_pins = b.place_symbol("74LVC1G04", inv2_x, inv2_y)
    b.connect_power(inv2_pins)
    inv2_in = inv2_pins["2"]
    inv2_out = inv2_pins["4"]

    # Wire COL_SEL_0 signal to INV2 input (branch from below INV1 LED)
    # place_led_below already wires from (inv1_led_x, inv1_out_y) down to LED.
    # Continue from LED junction Y down to INV2 input Y, avoiding overlap.
    led_drop_y = snap(inv1_out[1] + 3 * GRID)  # LED drop = 3*GRID (default)
    b.add_wire(inv1_led_x, led_drop_y, inv1_led_x, inv2_in[1])
    b.add_wire(inv1_led_x, inv2_in[1], inv2_in[0], inv2_in[1])

    # INV2 output -> LED -> COL_SEL_1
    inv2_led_x = snap(inv2_out[0] + 2 * GRID)
    b.add_wire(inv2_out[0], inv2_out[1], inv2_led_x, inv2_out[1])
    b.add_wire(inv2_led_x, inv2_out[1], hl_out_x, inv2_out[1])
    b.place_led_below(inv2_led_x, inv2_out[1])
    b.add_hier_label("COL_SEL_1", hl_out_x, inv2_out[1],
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


def _generate_row_enable_bank(title, enable_signal, output_prefix):
    """Shared generator for write_en_gen and read_en_gen (4 ANDs each).

    Generates: OUTPUT_i = AND(enable_signal, ROW_SEL_i) for i in 0..3.
    """
    b = SchematicBuilder(title=title, page_size="A3",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    and_x = base_x + 18 * GRID
    hl_out_x = and_x + 22 * GRID
    enable_trunk_x = base_x + 10 * GRID

    enable_hier_y = snap(base_y - 2 * GRID)
    b.add_hier_label(enable_signal, base_x, enable_hier_y,
                     shape="input", justify="right")
    b.add_wire(base_x, enable_hier_y, enable_trunk_x, enable_hier_y)

    and_a_pins = []
    and_b_pins = []
    and_out_pins = []
    for i in range(4):
        y = base_y + i * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G08", and_x, y)
        b.connect_power(pins)
        and_a_pins.append(pins["1"])
        and_b_pins.append(pins["2"])
        and_out_pins.append(pins["4"])

    # Enable signal trunk to all AND pin A
    trunk_ys = [enable_hier_y] + [and_a_pins[i][1] for i in range(4)]
    b.add_segmented_trunk(enable_trunk_x, trunk_ys)
    for i in range(4):
        a_pin = and_a_pins[i]
        b.add_wire(enable_trunk_x, a_pin[1], a_pin[0], a_pin[1])

    # ROW_SEL_i inputs to AND pin B
    for i in range(4):
        b_pin = and_b_pins[i]
        b.add_hier_label(f"ROW_SEL_{i}", base_x, b_pin[1],
                         shape="input", justify="right")
        b.add_wire(base_x, b_pin[1], b_pin[0], b_pin[1])

    # Outputs with LEDs
    for i in range(4):
        out_pin = and_out_pins[i]
        out_net = f"{output_prefix}{i}"
        led_jct_x = snap(out_pin[0] + 2 * GRID)
        b.add_wire(out_pin[0], out_pin[1], led_jct_x, out_pin[1])
        b.add_wire(led_jct_x, out_pin[1], hl_out_x, out_pin[1])
        b.place_led_below(led_jct_x, out_pin[1])
        b.add_hier_label(out_net, hl_out_x, out_pin[1],
                         shape="output", justify="left")

    return b


def generate_write_en_gen():
    """Write enable gen: 4 ANDs. WRITE_EN_ROW_i = AND(WRITE_ACTIVE, ROW_SEL_i)"""
    return _generate_row_enable_bank("Write Enable Generator",
                                     "WRITE_ACTIVE", "WRITE_EN_ROW_")


def generate_read_en_gen():
    """Read enable gen: 4 ANDs. READ_EN_ROW_i = AND(READ_EN, ROW_SEL_i)"""
    return _generate_row_enable_bank("Read Enable Generator",
                                     "READ_EN", "READ_EN_ROW_")


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
    Root sheet: connector, bus indicator LEDs, and hierarchical sheet references.

    New row/column addressing architecture:
      - Address Decoder: A0, A1 → ROW_SEL_0..3
      - Column Select: A2 → COL_SEL_0, COL_SEL_1
      - Write Enable Gen: WRITE_ACTIVE, ROW_SEL_0..3 → WRITE_EN_ROW_0..3
      - Read Enable Gen: READ_EN, ROW_SEL_0..3 → READ_EN_ROW_0..3
      - Bytes: WRITE_EN_ROW, READ_EN_ROW, COL_SEL, D0-D7
    """
    b = SchematicBuilder(title="8-Byte Discrete RAM", page_size="A2",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 25.4

    sheet_gap = 5 * GRID
    wire_stub = 5.08

    def _sheet_height(num_pins):
        return snap(num_pins * 2.54 + 5.08)

    # Pre-compute byte block height to set up layout
    byte_pin_count = 11  # WRITE_EN_ROW, READ_EN_ROW, COL_SEL, D0-D7
    byte_h_pre = _sheet_height(byte_pin_count)
    sheet_bottom_y = snap(base_y + 3 * (byte_h_pre + sheet_gap) + byte_h_pre)
    ensemble_center_y = snap((base_y + sheet_bottom_y) / 2)

    conn_x = base_x
    conn_y = snap(ensemble_center_y + 1.27)
    _, conn_pins = b.place_symbol("Conn_01x16", conn_x, conn_y,
                                  ref_prefix="J", value="SRAM_Bus", angle=180)

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

    # ================================================================
    # Layout positions
    # ================================================================
    col1_x = snap(base_x + 30 * GRID)
    col1_w = snap(28 * GRID)
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

    # Address Decoder: A0, A1 → ROW_SEL_0..3
    addr_left_defs = [("A0", "input"), ("A1", "input")]
    addr_right_defs = [(f"ROW_SEL_{i}", "output") for i in range(4)]
    addr_pin_defs = addr_left_defs + addr_right_defs
    addr_right_names = {f"ROW_SEL_{i}" for i in range(4)}
    addr_h = _sheet_height(max(len(addr_left_defs), len(addr_right_defs)))
    addr_sy = base_y
    addr_pp = _add_sheet_block("Address Decoder", "address_decoder.kicad_sch",
                               addr_pin_defs, col1_x, addr_sy,
                               col1_w, addr_h, yellow_fill,
                               right_pins=addr_right_names)

    # Column Select: A2 → COL_SEL_0, COL_SEL_1
    colsel_left_defs = [("A2", "input")]
    colsel_right_defs = [("COL_SEL_0", "output"), ("COL_SEL_1", "output")]
    colsel_pin_defs = colsel_left_defs + colsel_right_defs
    colsel_right_names = {"COL_SEL_0", "COL_SEL_1"}
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
    # Place sheet blocks — Column 2: Write Enable Gen, Read Enable Gen
    # ================================================================

    # Write Enable Gen: WRITE_ACTIVE, ROW_SEL_0..3 → WRITE_EN_ROW_0..3
    wen_left_defs = [("WRITE_ACTIVE", "input")]
    wen_left_defs += [(f"ROW_SEL_{i}", "input") for i in range(4)]
    wen_right_defs = [(f"WRITE_EN_ROW_{i}", "output") for i in range(4)]
    wen_pin_defs = wen_left_defs + wen_right_defs
    wen_right_names = {f"WRITE_EN_ROW_{i}" for i in range(4)}
    wen_h = _sheet_height(max(len(wen_left_defs), len(wen_right_defs)))
    wen_sy = base_y
    wen_pp = _add_sheet_block("Write Enable Gen", "write_en_gen.kicad_sch",
                              wen_pin_defs, col2_x, wen_sy,
                              col2_w, wen_h, yellow_fill,
                              right_pins=wen_right_names)

    # Read Enable Gen: READ_EN, ROW_SEL_0..3 → READ_EN_ROW_0..3
    ren_left_defs = [("READ_EN", "input")]
    ren_left_defs += [(f"ROW_SEL_{i}", "input") for i in range(4)]
    ren_right_defs = [(f"READ_EN_ROW_{i}", "output") for i in range(4)]
    ren_pin_defs = ren_left_defs + ren_right_defs
    ren_right_names = {f"READ_EN_ROW_{i}" for i in range(4)}
    ren_h = _sheet_height(max(len(ren_left_defs), len(ren_right_defs)))
    ren_sy = snap(wen_sy + wen_h + sheet_gap)
    ren_pp = _add_sheet_block("Read Enable Gen", "read_en_gen.kicad_sch",
                              ren_pin_defs, col2_x, ren_sy,
                              col2_w, ren_h, yellow_fill,
                              right_pins=ren_right_names)

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
    # Connector pins
    # ================================================================
    signal_names = ["A2",  # pin 2 — bottom, near column_select
                    "D7", "D6", "D5", "D4", "D3", "D2", "D1", "D0",  # pins 3-10
                    "A0", "A1",  # pins 11-12 — near addr_decoder (row inputs)
                    "nCE", "nWE", "nOE"]  # pins 13-15 — top, near control_logic
    conn_signal_pos = {}
    for pin_num_int, sig in enumerate(signal_names, start=2):
        conn_signal_pos[sig] = conn_pins[str(pin_num_int)]

    # ================================================================
    # Connector power pins
    # ================================================================
    vcc_pos = conn_pins["16"]
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

    fan_spacing = snap(6 * GRID)
    fan_span = (n_signals - 1) * fan_spacing
    min_conn_pin_y = min(conn_pins[str(i)][1] for i in range(1, 17))
    max_conn_pin_y = max(conn_pins[str(i)][1] for i in range(1, 17))
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

    # Direct wire destinations: A0, A1 → Address Decoder; A2 → Column Select
    direct_wire_dest = {
        "A0": addr_pp["A0"], "A1": addr_pp["A1"],
        "A2": colsel_pp["A2"],
        "nCE": ctrl_pp["nCE"], "nOE": ctrl_pp["nOE"], "nWE": ctrl_pp["nWE"],
    }

    direct_signals_order = ["A0", "A1", "A2", "nCE", "nOE", "nWE"]
    direct_turn = {}
    for i, sig in enumerate(direct_signals_order):
        direct_turn[sig] = snap(col1_x - (6 - i) * GRID)

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
    # Route: Col1 RIGHT → Col2 LEFT (ROW_SEL_0-3 trunks)
    # ================================================================
    sel_trunk_base_x = snap(col2_x - 3 * GRID)
    sel_trunk_x = [snap(sel_trunk_base_x - i * GRID) for i in range(4)]

    for i in range(4):
        sig = f"ROW_SEL_{i}"
        ax, ay = addr_pp[sig]
        wx, wy = wen_pp[sig]
        rx, ry = ren_pp[sig]
        tx = sel_trunk_x[i]

        b.add_wire(ax, ay, tx, ay)
        trunk_ys = sorted(set([snap(ay), snap(wy), snap(ry)]))
        b.add_segmented_trunk(tx, trunk_ys)
        b.add_wire(tx, wy, wx, wy)
        b.add_wire(tx, ry, rx, ry)

    # ================================================================
    # Route: Col1 RIGHT → Col2 LEFT (WRITE_ACTIVE, READ_EN)
    # ================================================================
    wa_trunk_x = snap(sel_trunk_base_x + 1 * GRID)
    re_trunk_x = snap(sel_trunk_base_x + 2 * GRID)

    wa_src = ctrl_pp["WRITE_ACTIVE"]
    wa_dst = wen_pp["WRITE_ACTIVE"]
    b.add_wire(wa_src[0], wa_src[1], wa_trunk_x, wa_src[1])
    b.add_wire(wa_trunk_x, wa_src[1], wa_trunk_x, wa_dst[1])
    b.add_wire(wa_trunk_x, wa_dst[1], wa_dst[0], wa_dst[1])

    re_src = ctrl_pp["READ_EN"]
    re_dst = ren_pp["READ_EN"]
    b.add_wire(re_src[0], re_src[1], re_trunk_x, re_src[1])
    b.add_wire(re_trunk_x, re_src[1], re_trunk_x, re_dst[1])
    b.add_wire(re_trunk_x, re_dst[1], re_dst[0], re_dst[1])

    # ================================================================
    # Route: Col2 RIGHT → Bytes (WRITE_EN_ROW_i, READ_EN_ROW_i)
    # Use labels for clean routing — each row signal has a unique name.
    # ================================================================
    for row_i in range(4):
        # WRITE_EN_ROW_i
        wen_sig = f"WRITE_EN_ROW_{row_i}"
        src_x, src_y = wen_pp[wen_sig]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(wen_sig, src_x + wire_stub, src_y)
        for col_j in range(2):
            byte_idx = col_j * 4 + row_i
            dst_x, dst_y = byte_pp[byte_idx]["WRITE_EN_ROW"]
            b.add_wire(dst_x, dst_y, dst_x - wire_stub, dst_y)
            b.add_label(wen_sig, dst_x - wire_stub, dst_y, justify="right")

        # READ_EN_ROW_i
        ren_sig = f"READ_EN_ROW_{row_i}"
        src_x, src_y = ren_pp[ren_sig]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(ren_sig, src_x + wire_stub, src_y)
        for col_j in range(2):
            byte_idx = col_j * 4 + row_i
            dst_x, dst_y = byte_pp[byte_idx]["READ_EN_ROW"]
            b.add_wire(dst_x, dst_y, dst_x - wire_stub, dst_y)
            b.add_label(ren_sig, dst_x - wire_stub, dst_y, justify="right")

    # ================================================================
    # Route: COL_SEL_0 → bytes 0-3 (col 0), COL_SEL_1 → bytes 4-7 (col 1)
    # Use labels for clean routing.
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

    return b


# --------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------

def count_components(builders):
    """Count total ICs, LEDs, resistors across all sheets."""
    totals = {"U": 0, "D": 0, "R": 0, "C": 0, "#PWR": 0, "J": 0, "#FLG": 0}
    for name, builder in builders.items():
        multiplier = 8 if name == "byte" else 1
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
    print("Discrete NES - 8-Byte RAM Prototype Schematic Generator")
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

    builders["write_en_gen"] = generate_write_en_gen()
    print("  [+] write_en_gen.kicad_sch")

    builders["read_en_gen"] = generate_read_en_gen()
    print("  [+] read_en_gen.kicad_sch")

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
        multiplier = 8 if name == "byte" else 1
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
