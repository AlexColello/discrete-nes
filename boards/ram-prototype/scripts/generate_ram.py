#!/usr/bin/env python3
"""
Generate hierarchical KiCad schematics for the 8-byte discrete RAM prototype.

Circuit architecture:
  - 3 address bits (A0-A2) -> 3-to-8 address decoder using inverters + 3-input ANDs
  - 8 data bits (D0-D7), bidirectional data bus
  - Active-low control: /CE, /OE, /WE (NES SRAM interface)
  - 8 bytes x 8 bits = 64 D flip-flops (74LVC1G79)
  - 64 tri-state buffers (74LVC1G125) for read-back
  - LED on EVERY gate output and stored bit

Produces:
  ram.kicad_sch              -- root sheet with connector + bus LEDs + hierarchy refs
  address_decoder.kicad_sch  -- 3 inverters + 8 three-input ANDs
  control_logic.kicad_sch    -- /CE,/OE,/WE inversion + WRITE_ACTIVE, READ_EN logic
  write_clk_gen.kicad_sch    -- 8 NANDs generating WRITE_CLK_0..7
  read_oe_gen.kicad_sch      -- 8 NANDs generating BUF_OE_0..7
  byte.kicad_sch             -- 8 DFFs + 8 tri-state buffers (shared by all 8 byte instances)
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
    Address decoder: 3-to-8 using inverters and 3-input ANDs.

    Inputs:  A0, A1, A2
    Outputs: SEL0..SEL7

    SEL0 = /A2 & /A1 & /A0
    SEL1 = /A2 & /A1 &  A0
    ...
    SEL7 =  A2 &  A1 &  A0

    Wiring approach:
      - A0/A1/A2: hier label -> wire -> vertical trunk, branches to inverter + AND inputs
      - A0_INV/A1_INV/A2_INV: inverter output -> LED T-junction -> vertical trunk -> AND inputs
      - SEL0-SEL7: AND output -> LED T-junction -> wire -> hier label
    """
    b = SchematicBuilder(title="Address Decoder", page_size="A3",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    inv_x = base_x + 18 * GRID
    and_x = base_x + 42 * GRID
    hl_out_x = and_x + 22 * GRID

    # X positions for 6 vertical trunks (A0, A1, A2, A0_INV, A1_INV, A2_INV)
    inv_in_x = inv_x - 15.24  # 55.88: inverter input pin X (74LVC1G04 pin 2)
    addr_trunk_x = [snap(inv_in_x - (1.5 + i) * GRID) for i in range(3)]
    inv_trunk_x = [base_x + 31 * GRID + i * 2 * GRID for i in range(3)]

    # The AND gates span 8 rows (SEL0-SEL7)
    and_top_y = base_y
    and_bot_y = base_y + 7 * SYM_SPACING_Y

    # -- Hierarchical labels for A0-A2, wired to trunk tops --
    for i in range(3):
        trunk_top_y = snap(base_y - (4 - i) * GRID)
        b.add_hier_label(f"A{i}", base_x, trunk_top_y, shape="input", justify="right")
        b.add_wire(base_x, trunk_top_y, addr_trunk_x[i], trunk_top_y)

    # -- Three inverters for complemented address bits --
    inv_in_pins = []
    inv_out_pins = []
    for i in range(3):
        y = base_y + i * SYM_SPACING_Y + 5 * GRID
        _, pins = b.place_symbol("74LVC1G04", inv_x, y)
        b.connect_power(pins)
        inv_in_pins.append(pins["2"])
        inv_out_pins.append(pins["4"])

    # Wire address trunks
    decode_table = [
        (0, 0, 0), (0, 0, 1), (0, 1, 0), (0, 1, 1),
        (1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1),
    ]
    addr_pin_map = {0: "3", 1: "1", 2: "6"}

    # Place 8 AND gates first to get pin positions
    and_gate_pins = []
    for sel_idx in range(8):
        y = base_y + sel_idx * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G11", and_x, y)
        b.connect_power(pins, gnd_pin="2")
        and_gate_pins.append(pins)

    # -- Wire each address bit trunk (A0, A1, A2) --
    for addr_i in range(3):
        trunk_x = addr_trunk_x[addr_i]
        trunk_top_y = snap(base_y - (4 - addr_i) * GRID)
        inv_in = inv_in_pins[addr_i]
        pin_num = addr_pin_map[addr_i]

        trunk_ys = [trunk_top_y, inv_in[1]]
        branch_targets = [(inv_in[0], inv_in[1])]
        for sel_idx, bits in enumerate(decode_table):
            if bits[2 - addr_i] == 1:
                target = and_gate_pins[sel_idx][pin_num]
                trunk_ys.append(target[1])
                branch_targets.append((target[0], target[1]))

        b.add_segmented_trunk(trunk_x, trunk_ys)

        for tx, ty in branch_targets:
            b.add_wire(trunk_x, ty, tx, ty)

    # -- Wire each inverted bit trunk (A0_INV, A1_INV, A2_INV) --
    for addr_i in range(3):
        trunk_x = inv_trunk_x[addr_i]
        out_pin = inv_out_pins[addr_i]
        pin_num = addr_pin_map[addr_i]

        led_jct_x = out_pin[0] + 2 * GRID
        b.add_wire(out_pin[0], out_pin[1], led_jct_x, out_pin[1])
        b.add_wire(led_jct_x, out_pin[1], trunk_x, out_pin[1])
        b.place_led_below(led_jct_x, out_pin[1], drop=4 * GRID)

        branch_targets = []
        trunk_ys = [out_pin[1]]
        for sel_idx, bits in enumerate(decode_table):
            if bits[2 - addr_i] == 0:
                target = and_gate_pins[sel_idx][pin_num]
                branch_targets.append((target[0], target[1]))
                trunk_ys.append(target[1])

        if branch_targets:
            b.add_segmented_trunk(trunk_x, trunk_ys)
            for tx, ty in branch_targets:
                b.add_wire(trunk_x, ty, tx, ty)

    # -- SEL outputs: AND output -> LED T-junction -> wire -> hier label --
    for sel_idx in range(8):
        pins = and_gate_pins[sel_idx]
        out_pin = pins["4"]

        led_jct_x = out_pin[0] + 2 * GRID
        b.add_wire(out_pin[0], out_pin[1], led_jct_x, out_pin[1])
        b.add_wire(led_jct_x, out_pin[1], hl_out_x, out_pin[1])
        b.place_led_below(led_jct_x, out_pin[1])
        b.add_hier_label(f"SEL{sel_idx}", hl_out_x, out_pin[1], shape="output", justify="left")

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


def _generate_nand_bank(title, enable_signal, output_prefix):
    """Shared generator for write_clk_gen and read_oe_gen (8 NANDs each)."""
    b = SchematicBuilder(title=title, page_size="A3",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    nand_x = base_x + 18 * GRID
    hl_out_x = nand_x + 22 * GRID
    enable_trunk_x = base_x + 10 * GRID

    enable_hier_y = snap(base_y - 2 * GRID)
    b.add_hier_label(enable_signal, base_x, enable_hier_y, shape="input", justify="right")
    b.add_wire(base_x, enable_hier_y, enable_trunk_x, enable_hier_y)

    nand_a_pins = []
    nand_b_pins = []
    nand_out_pins = []
    for i in range(8):
        y = base_y + i * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G00", nand_x, y)
        b.connect_power(pins)
        nand_a_pins.append(pins["1"])
        nand_b_pins.append(pins["2"])
        nand_out_pins.append(pins["4"])

    trunk_ys = [enable_hier_y] + [nand_a_pins[i][1] for i in range(8)]
    b.add_segmented_trunk(enable_trunk_x, trunk_ys)
    for i in range(8):
        a_pin = nand_a_pins[i]
        b.add_wire(enable_trunk_x, a_pin[1], a_pin[0], a_pin[1])

    for i in range(8):
        b_pin = nand_b_pins[i]
        b.add_hier_label(f"SEL{i}", base_x, b_pin[1], shape="input", justify="right")
        b.add_wire(base_x, b_pin[1], b_pin[0], b_pin[1])

    for i in range(8):
        out_pin = nand_out_pins[i]
        out_net = f"{output_prefix}{i}"
        led_jct_x = snap(out_pin[0] + 2 * GRID)
        b.add_wire(out_pin[0], out_pin[1], led_jct_x, out_pin[1])
        b.add_wire(led_jct_x, out_pin[1], hl_out_x, out_pin[1])
        b.place_led_below(led_jct_x, out_pin[1])
        b.add_hier_label(out_net, hl_out_x, out_pin[1], shape="output", justify="left")

    return b


def generate_write_clk_gen():
    """Write clock generation: 8 NANDs. WRITE_CLK_n = NAND(WRITE_ACTIVE, SELn)"""
    return _generate_nand_bank("Write Clock Generator", "WRITE_ACTIVE", "WRITE_CLK_")


def generate_read_oe_gen():
    """Read OE generation: 8 NANDs. BUF_OE_n = NAND(READ_EN, SELn)"""
    return _generate_nand_bank("Read OE Generator", "READ_EN", "BUF_OE_")


def generate_byte_sheet():
    """
    Generate one memory byte (8 bits) -- reused for all 8 bytes.
    """
    b = SchematicBuilder(title="Memory Byte", page_size="A3",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 30.48

    dff_x = base_x + 18 * GRID
    buf_x = base_x + 52 * GRID
    wclk_trunk_x = base_x + 10 * GRID
    boe_trunk_x = buf_x - 8 * GRID

    wclk_hier_y = snap(base_y - 4 * GRID)
    b.add_hier_label("WRITE_CLK", base_x, wclk_hier_y, shape="input", justify="right")
    b.add_wire(base_x, wclk_hier_y, wclk_trunk_x, wclk_hier_y)

    boe_hier_y = snap(base_y - 6 * GRID)
    b.add_hier_label("BUF_OE", base_x, boe_hier_y, shape="input", justify="right")
    b.add_wire(base_x, boe_hier_y, boe_trunk_x, boe_hier_y)

    clk_pin_positions = []
    oe_pin_positions = []

    for bit in range(8):
        y = base_y + bit * DFF_SPACING_Y

        _, dff_pins = b.place_symbol("74LVC1G79", dff_x, y)
        b.connect_power(dff_pins)

        d_pin = dff_pins["1"]
        hl_y = base_y + bit * DFF_SPACING_Y
        b.add_hier_label(f"D{bit}", base_x, hl_y, shape="bidirectional", justify="right")
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
        led_in = b.place_led_indicator(y_pin[0] + LED_GAP_X, y_pin[1])
        b.add_wire(*y_pin, *led_in)
        b.add_label(f"D{bit}", *y_pin, justify="right")

    clk_ys = [p[1] for p in clk_pin_positions]
    wclk_all_ys = sorted([wclk_hier_y] + clk_ys)
    b.add_segmented_trunk(wclk_trunk_x, wclk_all_ys)

    oe_ys = [p[1] for p in oe_pin_positions]
    boe_all_ys = sorted([boe_hier_y] + oe_ys)
    b.add_segmented_trunk(boe_trunk_x, boe_all_ys)

    return b


# --------------------------------------------------------------
# Root sheet generator
# --------------------------------------------------------------

def generate_root_sheet():
    """
    Root sheet: connector, bus indicator LEDs, and hierarchical sheet references.
    """
    b = SchematicBuilder(title="8-Byte Discrete RAM", page_size="A2",
                         project_name=PROJECT_NAME)
    base_x, base_y = 25.4, 25.4

    sheet_gap = 5 * GRID
    wire_stub = 5.08

    def _sheet_height(num_pins):
        return snap(num_pins * 2.54 + 5.08)

    byte_h_pre = _sheet_height(10)
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
    green_fill = ColorRGBA(R=225, G=255, B=225, A=255, precision=4)

    # ================================================================
    # 2-Column layout positions
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
    # Place all sheet blocks
    # ================================================================

    addr_left_defs = [("A0", "input"), ("A1", "input"), ("A2", "input")]
    addr_right_defs = [(f"SEL{i}", "output") for i in range(8)]
    addr_pin_defs = addr_left_defs + addr_right_defs
    addr_right_names = {f"SEL{i}" for i in range(8)}
    addr_num_left = len(addr_left_defs)
    addr_num_right = len(addr_right_defs)
    addr_h = _sheet_height(max(addr_num_left, addr_num_right))
    addr_sy = base_y
    addr_pp = _add_sheet_block("Address Decoder", "address_decoder.kicad_sch",
                               addr_pin_defs, col1_x, addr_sy,
                               col1_w, addr_h, yellow_fill,
                               right_pins=addr_right_names)

    ctrl_left_defs = [("nCE", "input"), ("nOE", "input"), ("nWE", "input")]
    ctrl_right_defs = [("WRITE_ACTIVE", "output"), ("READ_EN", "output")]
    ctrl_pin_defs = ctrl_left_defs + ctrl_right_defs
    ctrl_right_names = {"WRITE_ACTIVE", "READ_EN"}
    ctrl_num_left = len(ctrl_left_defs)
    ctrl_num_right = len(ctrl_right_defs)
    ctrl_h = _sheet_height(max(ctrl_num_left, ctrl_num_right))
    ctrl_sy = snap(addr_sy + addr_h + sheet_gap)
    ctrl_pp = _add_sheet_block("Control Logic", "control_logic.kicad_sch",
                               ctrl_pin_defs, col1_x, ctrl_sy,
                               col1_w, ctrl_h, yellow_fill,
                               right_pins=ctrl_right_names)

    wclk_left_defs = [("WRITE_ACTIVE", "input")]
    wclk_left_defs += [(f"SEL{i}", "input") for i in range(8)]
    wclk_right_defs = [(f"WRITE_CLK_{i}", "output") for i in range(8)]
    wclk_pin_defs = wclk_left_defs + wclk_right_defs
    wclk_right_names = {f"WRITE_CLK_{i}" for i in range(8)}
    wclk_num_left = len(wclk_left_defs)
    wclk_num_right = len(wclk_right_defs)
    wclk_h = _sheet_height(max(wclk_num_left, wclk_num_right))
    wclk_sy = base_y
    wclk_pp = _add_sheet_block("Write Clk Gen", "write_clk_gen.kicad_sch",
                               wclk_pin_defs, col2_x, wclk_sy,
                               col2_w, wclk_h, yellow_fill,
                               right_pins=wclk_right_names)

    roe_left_defs = [("READ_EN", "input")]
    roe_left_defs += [(f"SEL{i}", "input") for i in range(8)]
    roe_right_defs = [(f"BUF_OE_{i}", "output") for i in range(8)]
    roe_pin_defs = roe_left_defs + roe_right_defs
    roe_right_names = {f"BUF_OE_{i}" for i in range(8)}
    roe_num_left = len(roe_left_defs)
    roe_num_right = len(roe_right_defs)
    roe_h = _sheet_height(max(roe_num_left, roe_num_right))
    roe_sy = snap(wclk_sy + wclk_h + sheet_gap)
    roe_pp = _add_sheet_block("Read OE Gen", "read_oe_gen.kicad_sch",
                              roe_pin_defs, col2_x, roe_sy,
                              col2_w, roe_h, yellow_fill,
                              right_pins=roe_right_names)

    byte_pin_defs = [("WRITE_CLK", "input"), ("BUF_OE", "input")]
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
    signal_names = ["D7", "D6", "D5", "D4", "D3", "D2", "D1", "D0",
                    "nWE", "nOE", "nCE",
                    "A2", "A1", "A0"]
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

    direct_wire_dest = {
        "A0": addr_pp["A0"], "A1": addr_pp["A1"], "A2": addr_pp["A2"],
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
    # Labels on byte sheet input pins
    # ================================================================
    for byte_idx in range(8):
        pp = byte_pp[byte_idx]
        for bit in range(8):
            sig = f"D{bit}"
            px, py = pp[sig]
            b.add_wire(px, py, px - wire_stub, py)
            b.add_label(sig, px - wire_stub, py, justify="right")

    # ================================================================
    # Route C: Col1 RIGHT -> Col2 LEFT (SEL0-7 trunks)
    # ================================================================
    sel_trunk_base_x = snap(col2_x - 3 * GRID)
    sel_trunk_x = [snap(sel_trunk_base_x - i * GRID) for i in range(8)]

    for i in range(8):
        sig = f"SEL{i}"
        ax, ay = addr_pp[sig]
        wx, wy = wclk_pp[sig]
        rx, ry = roe_pp[sig]
        tx = sel_trunk_x[i]

        b.add_wire(ax, ay, tx, ay)
        trunk_ys = sorted(set([snap(ay), snap(wy), snap(ry)]))
        b.add_segmented_trunk(tx, trunk_ys)
        b.add_wire(tx, wy, wx, wy)
        b.add_wire(tx, ry, rx, ry)

    # ================================================================
    # Route D: Col1 RIGHT -> Col2 LEFT (WRITE_ACTIVE, READ_EN)
    # ================================================================
    wa_trunk_x = snap(sel_trunk_base_x + 1 * GRID)
    re_trunk_x = snap(sel_trunk_base_x + 2 * GRID)

    wa_src = ctrl_pp["WRITE_ACTIVE"]
    wa_dst = wclk_pp["WRITE_ACTIVE"]
    b.add_wire(wa_src[0], wa_src[1], wa_trunk_x, wa_src[1])
    b.add_wire(wa_trunk_x, wa_src[1], wa_trunk_x, wa_dst[1])
    b.add_wire(wa_trunk_x, wa_dst[1], wa_dst[0], wa_dst[1])

    re_src = ctrl_pp["READ_EN"]
    re_dst = roe_pp["READ_EN"]
    b.add_wire(re_src[0], re_src[1], re_trunk_x, re_src[1])
    b.add_wire(re_trunk_x, re_src[1], re_trunk_x, re_dst[1])
    b.add_wire(re_trunk_x, re_dst[1], re_dst[0], re_dst[1])

    # ================================================================
    # Route E: Col2 RIGHT -> Byte Col1 (WRITE_CLK_0-3)
    # ================================================================
    route_e_base_x = snap(col2_x + col2_w + 2 * GRID)
    route_e_sp = GRID
    wclk_route_x = [snap(route_e_base_x + i * route_e_sp) for i in range(4)]

    for i in range(4):
        sig = f"WRITE_CLK_{i}"
        src_x, src_y = wclk_pp[sig]
        dst_x, dst_y = byte_pp[i]["WRITE_CLK"]
        rx = wclk_route_x[i]
        b.add_wire(src_x, src_y, rx, src_y)
        if abs(src_y - dst_y) > 0.01:
            b.add_wire(rx, src_y, rx, dst_y)
        b.add_wire(rx, dst_y, dst_x, dst_y)

    # ================================================================
    # Route F: Col2 RIGHT -> Byte Col2 (WRITE_CLK_4-7)
    # ================================================================
    transit_y_base = snap(base_y - 1 * GRID)
    route_f_gap1_x = [snap(route_e_base_x + (4 + i) * route_e_sp)
                      for i in range(4)]
    byte_gap_base_x = snap(byte_col1_x + byte_w + 2 * GRID)
    byte_gap_sp = GRID
    route_f_gap2_x = [snap(byte_gap_base_x + i * byte_gap_sp)
                      for i in range(4)]

    for i in range(4):
        byte_idx = 4 + i
        w_transit_y = snap(transit_y_base - i * GRID)

        sig = f"WRITE_CLK_{byte_idx}"
        src_x, src_y = wclk_pp[sig]
        dst_x, dst_y = byte_pp[byte_idx]["WRITE_CLK"]
        g1x = route_f_gap1_x[i]
        g2x = route_f_gap2_x[i]
        b.add_wire(src_x, src_y, g1x, src_y)
        b.add_wire(g1x, src_y, g1x, w_transit_y)
        b.add_wire(g1x, w_transit_y, g2x, w_transit_y)
        b.add_wire(g2x, w_transit_y, g2x, dst_y)
        b.add_wire(g2x, dst_y, dst_x, dst_y)

    # ================================================================
    # BUF_OE_0-7: labels
    # ================================================================
    for i in range(8):
        sig = f"BUF_OE_{i}"
        src_x, src_y = roe_pp[sig]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(sig, src_x + wire_stub, src_y)
        dst_x, dst_y = byte_pp[i]["BUF_OE"]
        b.add_wire(dst_x, dst_y, dst_x - wire_stub, dst_y)
        b.add_label(sig, dst_x - wire_stub, dst_y, justify="right")

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
    """Fix sub-sheet symbol instance paths and assign globally unique references."""
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

        if is_multi_instance:
            for sym in builder.sch.schematicSymbols:
                prefix = ""
                for p in sym.properties:
                    if p.key == "Reference":
                        prefix = p.value.rstrip("0123456789")
                        break

                existing = sym.instances[0] if sym.instances else None
                if existing is None:
                    existing = SymbolProjectInstance(name=PROJECT_NAME, paths=[])
                    sym.instances = [existing]

                for inst_block in all_sheet_blocks:
                    inst_path = f"/{root_uuid}/{inst_block.uuid}"
                    global_counters[prefix] = global_counters.get(prefix, 0) + 1
                    inst_ref = f"{prefix}{global_counters[prefix]}"
                    existing.paths.append(SymbolProjectPath(
                        sheetInstancePath=inst_path,
                        reference=inst_ref,
                        unit=1,
                    ))
        else:
            for sym in builder.sch.schematicSymbols:
                prefix = ""
                for p in sym.properties:
                    if p.key == "Reference":
                        prefix = p.value.rstrip("0123456789")
                        break

                global_counters[prefix] = global_counters.get(prefix, 0) + 1
                new_ref = f"{prefix}{global_counters[prefix]}"

                existing = sym.instances[0] if sym.instances else None
                if existing is None:
                    existing = SymbolProjectInstance(name=PROJECT_NAME, paths=[])
                    sym.instances = [existing]

                existing.paths.append(SymbolProjectPath(
                    sheetInstancePath=hier_path,
                    reference=new_ref,
                    unit=1,
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

    builders["control_logic"] = generate_control_logic()
    print("  [+] control_logic.kicad_sch")

    builders["write_clk_gen"] = generate_write_clk_gen()
    print("  [+] write_clk_gen.kicad_sch")

    builders["read_oe_gen"] = generate_read_oe_gen()
    print("  [+] read_oe_gen.kicad_sch")

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
