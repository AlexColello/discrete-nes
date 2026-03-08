#!/usr/bin/env python3
"""
Generate KiCad PCB layout for the 8-byte discrete RAM prototype.

Places all 512 components (161 ICs, 175 LEDs, 175 resistors, 1 connector)
in a grouped layout matching the schematic hierarchy.  All DSBGA ICs are
rotated 90° CW so pin 4 (output) faces right toward its R+LED chain.
After placement, pre-routes repetitive local connections:
  - Power vias (GND/VCC pads to inner planes)
  - IC→LED L-traces (output to indicator LED anode)
  - LED→R L-traces (LED cathode to resistor)
  - DFF Q→Buffer A (3-segment vertical/diagonal/vertical on F.Cu)
  - CLK fanout (horizontal F.Cu bus + vertical stubs per byte)
  - OE fanout (horizontal F.Cu bus + vertical stubs per byte)

Layout:
  +------+-----------+-----------+-----------+
  |      | ADDR DEC  | BYTE 0    | BYTE 4    |
  |      |           | BYTE 1    | BYTE 5    |
  | CONN +-----------+ BYTE 2    | BYTE 6    |
  |      | CTRL LOGIC| BYTE 3    | BYTE 7    |
  +------+-----------+-----------+-----------+
                     | WRITE CLK | READ OE   |
                     +-----------+-----------+

  Each byte is a line of 8 bits (8 DFFs + 8 buffers in 8 columns).
  Bytes sorted by address: top-left going down first, then right.

Each IC is paired with its LED+R in a horizontal cell:
  [IC] → [LED] → [R]

Usage:
    cd boards/ram-prototype
    python scripts/generate_pcb.py
"""

import math
import os
import sys
from collections import defaultdict

# Add shared library to path
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen.pcb import (
    PCBBuilder, create_dsbga_footprints,
    export_netlist, parse_netlist, get_footprint_for_part,
    fix_pcb_drc,
)
from kicad_gen.common import FOOTPRINT_MAP

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------

BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SHARED_FP_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared",
    "kicad-lib", "footprints", "DSBGA_Packages.pretty"))

# Cell layout dimensions (mm)
# DSBGA courtyard ~3.4x3.4mm, R_0402 courtyard ~1.9x1.0mm, LED_0402 ~1.9x1.0mm
IC_CELL_W = 5.5      # horizontal spacing between IC centers (RAM bytes — tight)
IC_CELL_H = 3.0      # vertical spacing between IC rows (RAM bytes — tight)
CTRL_CELL_W = 6.5    # horizontal spacing for control logic (wider for routing)
CTRL_CELL_H = 3.5    # vertical spacing for control logic (wider for routing)
LED_OFFSET_X = 2.2   # LED center offset from IC center (closest, vertical 270°)
R_OFFSET_X = 3.3     # R center offset from IC center (after LED, vertical 270°)

# Group layout spacing (mm)
GROUP_GAP_X = 3.0    # horizontal gap between major groups (connector, decoder, RAM)
GROUP_GAP_Y = 0.5    # vertical gap between byte rows
CTRL_GROUP_GAP_X = 4.0  # horizontal gap between control logic groups
BYTE_COL_GAP = 2.0   # horizontal gap between the two byte columns (physical gap)
CTRL_ROW_GAP = 7.5   # vertical gap between RAM area and control logic row
BOARD_MARGIN = 7.0   # margin from board edge to components
SHEET_BORDER = 13.0  # minimum distance from sheet edge to board outline
PLACEMENT_ORIGIN = SHEET_BORDER + BOARD_MARGIN  # components start here

# Connector dimensions
CONN_PIN_PITCH = 2.54  # mm between connector pins


# --------------------------------------------------------------
# Component grouping
# --------------------------------------------------------------

def group_components(netlist_data):
    """Group components by their hierarchy sheet path.

    Returns dict: group_name -> [component_dict, ...]
    """
    groups = defaultdict(list)

    for comp in netlist_data["components"]:
        sheetpath = comp.get("sheetpath", "/")

        if sheetpath == "/":
            groups["root"].append(comp)
        elif "Address Decoder" in sheetpath:
            groups["addr_decoder"].append(comp)
        elif "Control Logic" in sheetpath:
            groups["control_logic"].append(comp)
        elif "Write Clk Gen" in sheetpath:
            groups["write_clk_gen"].append(comp)
        elif "Read OE Gen" in sheetpath:
            groups["read_oe_gen"].append(comp)
        else:
            # Byte sheets: /Byte 0/, /Byte 1/, etc.
            for i in range(8):
                if f"Byte {i}" in sheetpath:
                    groups[f"byte_{i}"].append(comp)
                    break
            else:
                groups["root"].append(comp)

    return dict(groups)


def sort_components_for_placement(components):
    """Sort components: ICs first (by ref number), then their LED+R pairs.

    Returns list of (ic_comp, r_comp_or_None, led_comp_or_None) tuples
    for ICs, plus a list of standalone components (connector, root LEDs).
    """
    ics = []
    rs = []
    leds = []
    others = []

    for c in components:
        ref = c["ref"]
        if ref.startswith("U"):
            ics.append(c)
        elif ref.startswith("R"):
            rs.append(c)
        elif ref.startswith("D"):
            leds.append(c)
        else:
            others.append(c)

    # Sort ICs: DFFs (74LVC1G79) first, then buffers (74LVC1G125), then others.
    # Within each type group, sort by reference number.
    # This ensures DFFs fill row 0 and buffers fill row 1 in byte groups.
    PART_ORDER = {"74LVC1G79": 0, "74LVC1G125": 1}

    def ref_num(c):
        ref = c["ref"]
        prefix = ref.rstrip("0123456789")
        return int(ref[len(prefix):]) if ref[len(prefix):] else 0

    def ic_sort_key(c):
        return (PART_ORDER.get(c["part"], 2), ref_num(c))

    ics.sort(key=ic_sort_key)
    rs.sort(key=ref_num)
    leds.sort(key=ref_num)

    # Match ICs with their LED+R pairs via shared nets
    # Each IC output drives LED -> R -> GND
    ic_cells = []
    used_rs = set()
    used_leds = set()

    for ic in ics:
        # Find LED connected to this IC's output (IC output -> LED anode)
        ic_nets = set(ic["pins"].values())
        matched_led = None
        for led in leds:
            if led["ref"] in used_leds:
                continue
            led_nets = set(led["pins"].values())
            if ic_nets & led_nets:
                matched_led = led
                used_leds.add(led["ref"])
                break

        # Find R connected to this LED (LED cathode -> R)
        matched_r = None
        if matched_led:
            led_nets = set(matched_led["pins"].values())
            for r in rs:
                if r["ref"] in used_rs:
                    continue
                r_nets = set(r["pins"].values())
                if led_nets & r_nets:
                    matched_r = r
                    used_rs.add(r["ref"])
                    break

        ic_cells.append((ic, matched_r, matched_led))

    # Standalone R+LED pairs (root sheet bus LEDs)
    # After swap: LED has signal net from connector, find R from LED's nets
    standalone = []
    for led in leds:
        if led["ref"] not in used_leds:
            # Find matching R
            led_nets = set(led["pins"].values())
            matched_r = None
            for r in rs:
                if r["ref"] not in used_rs:
                    r_nets = set(r["pins"].values())
                    if led_nets & r_nets:
                        matched_r = r
                        used_rs.add(r["ref"])
                        break
            standalone.append((matched_r, led))

    return ic_cells, standalone, others


# --------------------------------------------------------------
# Layout computation
# --------------------------------------------------------------

def compute_group_layout(ic_cells, standalone, max_cols=4,
                         cell_w=None, cell_h=None):
    """Compute relative positions for components within a group.

    Returns list of (component, rel_x, rel_y) for all components.
    Each IC cell is laid out as: IC at (0,0), LED at (+1.8, 0), R at (+3.5, 0)
    ICs are arranged in a grid with max_cols columns.
    """
    cw = cell_w if cell_w is not None else IC_CELL_W
    ch = cell_h if cell_h is not None else IC_CELL_H

    placements = []
    row, col = 0, 0

    for ic, r, led in ic_cells:
        x = col * cw
        y = row * ch

        placements.append((ic, x, y))
        if led:
            placements.append((led, x + LED_OFFSET_X, y))
        if r:
            placements.append((r, x + R_OFFSET_X, y))

        col += 1
        if col >= max_cols:
            col = 0
            row += 1

    # Standalone R+LED pairs below IC grid
    if standalone:
        row += 1
        col = 0
        for r, led in standalone:
            x = col * cw
            y = row * ch

            placements.append((r, x, y))
            if led:
                placements.append((led, x + 1.5, y))

            col += 1
            if col >= max_cols:
                col = 0
                row += 1

    return placements


def compute_group_size(placements, cell_w=None, cell_h=None):
    """Compute bounding box of a group's placements.

    Returns (width, height) in mm.
    """
    if not placements:
        return (0, 0)

    cw = cell_w if cell_w is not None else IC_CELL_W
    ch = cell_h if cell_h is not None else IC_CELL_H

    xs = [x for _, x, y in placements]
    ys = [y for _, x, y in placements]

    return (max(xs) - min(xs) + cw, max(ys) - min(ys) + ch)


# --------------------------------------------------------------
# Pre-routing
# --------------------------------------------------------------

# Via and trace sizing
VIA_SIZE = 0.8       # mm outer diameter (Elecrow minimum)
VIA_DRILL = 0.4      # mm drill
POWER_TRACE_W = 0.3  # mm trace width for power stubs
SIGNAL_TRACE_W = 0.2 # mm trace width for signals
VIA_OFFSET = 0.6     # mm offset from pad center to via center


def _build_net_pad_index(pcb):
    """Build mapping of net_number -> [(ref, pad_number, abs_x, abs_y), ...].

    Also returns ref_to_part: {ref -> part_name} for identifying IC types.
    """
    import math

    net_to_pads = defaultdict(list)

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        fp_x, fp_y = fp.position.X, fp.position.Y
        angle_rad = math.radians(fp.position.angle or 0)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)

        for pad in fp.pads:
            if pad.net and pad.net.number and pad.net.number > 0:
                px, py = pad.position.X, pad.position.Y
                # KiCad uses clockwise rotation (positive angle = CW in Y-down)
                abs_x = round(fp_x + px * cos_a + py * sin_a, 2)
                abs_y = round(fp_y - px * sin_a + py * cos_a, 2)
                net_to_pads[pad.net.number].append(
                    (ref, pad.number, abs_x, abs_y, pad.net.name))

    return net_to_pads


def preroute_power_vias(pcb):
    """Drop vias from every IC GND/VCC pad and R GND pad to inner planes.

    - IC pin 3 (GND) -> via to In1.Cu (GND plane)
    - IC pin 5 (VCC) -> via to In2.Cu (VCC plane)
    - R GND pads      -> via to In1.Cu (GND plane)

    Via offset direction:
    - DSBGA ICs: away from IC center (outward from body)
    - LEDs/Rs: downward (+Y direction) into the gap between rows

    Returns (via_count, trace_count).
    """
    import math

    gnd_net = pcb.get_net_number("GND")
    vcc_net = pcb.get_net_number("VCC")
    if gnd_net is None or vcc_net is None:
        print("  WARNING: GND or VCC net not found, skipping power vias")
        return 0, 0

    vias = 0
    traces = 0

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        lib_id = fp.libId or ""
        fp_x, fp_y = fp.position.X, fp.position.Y
        angle_rad = math.radians(fp.position.angle or 0)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)

        is_dsbga = "DSBGA" in lib_id
        is_led = "LED" in lib_id
        is_resistor = "Resistor" in lib_id

        if not (is_dsbga or is_led or is_resistor):
            continue

        for pad in fp.pads:
            if not (pad.net and pad.net.name in ("GND", "VCC")):
                continue

            net_name = pad.net.name
            net_num = pad.net.number

            px, py = pad.position.X, pad.position.Y
            # KiCad uses clockwise rotation (positive angle = CW in Y-down)
            abs_x = round(fp_x + px * cos_a + py * sin_a, 2)
            abs_y = round(fp_y - px * sin_a + py * cos_a, 2)

            if net_name == "GND":
                target_layers = ["F.Cu", "In1.Cu"]
            else:  # VCC
                target_layers = ["F.Cu", "In2.Cu"]

            if is_dsbga:
                # DSBGA: offset away from IC center (into row gap)
                dx = abs_x - fp_x
                dy = abs_y - fp_y
                dist = math.sqrt(dx * dx + dy * dy)
                if dist > 0.01:
                    via_x = round(abs_x + (dx / dist) * VIA_OFFSET, 2)
                    via_y = round(abs_y + (dy / dist) * VIA_OFFSET, 2)
                else:
                    via_x = abs_x
                    via_y = round(abs_y + VIA_OFFSET, 2)
            else:
                # LEDs and Rs: offset rightward (+X, away from IC cell
                # center) to avoid crossing into CLK/OE fanout buses
                # that run below each IC row
                via_x = round(abs_x + VIA_OFFSET, 2)
                via_y = abs_y

            # Short stub trace from pad to via
            pcb.add_trace((abs_x, abs_y), (via_x, via_y),
                          net_num, POWER_TRACE_W, "F.Cu")
            traces += 1

            # Via to inner plane
            pcb.add_via((via_x, via_y), net_num,
                        VIA_SIZE, VIA_DRILL, target_layers)
            vias += 1

    return vias, traces


def preroute_led_to_r(pcb, netlist_data):
    """Route LED cathode -> R for every IC+LED+R cell.

    For each IC, finds the LED on its output net (IC output -> LED anode),
    then finds R on the LED cathode net, and routes LED cathode -> R.

    Returns number of trace segments added.
    """
    net_to_pads = _build_net_pad_index(pcb)

    traces = 0

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue

        # Get IC output pin (pin 4) net
        ic_out_net = pcb.get_pad_net(ref, "4")
        if ic_out_net is None or ic_out_net == 0:
            continue

        # IC position for proximity filtering
        ic_x, ic_y = fp.position.X, fp.position.Y

        # Find nearest LED pad on the IC output net (IC -> LED anode)
        pads_on_net = net_to_pads.get(ic_out_net, [])
        led_ref = None
        led_anode_pad = None
        best_dist = float("inf")
        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if pad_ref.startswith("D"):
                dist = math.sqrt((px - ic_x)**2 + (py - ic_y)**2)
                if dist < IC_CELL_W and dist < best_dist:
                    best_dist = dist
                    led_ref = pad_ref
                    led_anode_pad = pad_num

        if led_ref is None:
            continue

        # Find LED's other pad (cathode) and its net
        led_cathode_pad = "1" if led_anode_pad == "2" else "2"
        led_cathode_net = pcb.get_pad_net(led_ref, led_cathode_pad)
        if led_cathode_net is None or led_cathode_net == 0:
            continue

        # Find nearest R pad on the LED-cathode net (within IC_CELL_W of LED)
        led_pos = pcb.get_pad_position(led_ref, led_anode_pad)
        r_ref = None
        r_pad_num = None
        best_dist = float("inf")
        for pad_ref, pad_num, px, py, pnet in net_to_pads.get(led_cathode_net, []):
            if pad_ref.startswith("R"):
                dist = math.sqrt((px - led_pos[0])**2 + (py - led_pos[1])**2)
                if dist < IC_CELL_W and dist < best_dist:
                    best_dist = dist
                    r_ref = pad_ref
                    r_pad_num = pad_num

        if r_ref is None:
            continue

        # Route LED cathode -> R pad (horizontal first: safe route
        # through clear space between LED and R, no components in between)
        led_cathode_pos = pcb.get_pad_position(led_ref, led_cathode_pad)
        r_pos = pcb.get_pad_position(r_ref, r_pad_num)

        segs = pcb.add_l_trace(led_cathode_pos, r_pos, led_cathode_net,
                               SIGNAL_TRACE_W, "F.Cu", horizontal_first=True)
        traces += len(segs)

    return traces


def preroute_ic_to_led(pcb, netlist_data):
    """Route IC output (pin 4) to LED anode using chamfered L-traces.

    With ICs rotated 90° CW, pin 4 (Q) faces right at (IC_x+0.50, by+0.25)
    and pin 5 (VCC) is above at (IC_x+0.50, by-0.25), so VCC does NOT block
    the rightward route to LED.  A simple horizontal-first L-trace suffices.

    Returns number of trace segments added.
    """
    net_to_pads = _build_net_pad_index(pcb)
    traces = 0

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue

        # Get IC output pin (pin 4) net
        ic_out_net = pcb.get_pad_net(ref, "4")
        if ic_out_net is None or ic_out_net == 0:
            continue

        # IC position for proximity filtering
        ic_x, ic_y = fp.position.X, fp.position.Y

        # Find nearest LED pad on the same net (within IC_CELL_W distance)
        pads_on_net = net_to_pads.get(ic_out_net, [])
        led_ref = None
        led_pad_num = None
        best_dist = float("inf")
        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if pad_ref.startswith("D"):
                dist = math.sqrt((px - ic_x)**2 + (py - ic_y)**2)
                if dist < IC_CELL_W and dist < best_dist:
                    best_dist = dist
                    led_ref = pad_ref
                    led_pad_num = pad_num

        if led_ref is None:
            continue

        # Route from IC pin 4 to LED anode pad using L-trace (horizontal first)
        pin4_pos = pcb.get_pad_position(ref, "4")
        led_anode_pos = pcb.get_pad_position(led_ref, led_pad_num)

        segs = pcb.add_l_trace(
            pin4_pos, led_anode_pos, ic_out_net,
            SIGNAL_TRACE_W, "F.Cu", horizontal_first=True)
        traces += len(segs)

    return traces


def _build_ref_to_part(netlist_data):
    """Build mapping of reference designator -> part name from netlist.

    Used to filter routing functions to specific IC types (e.g., only
    route CLK fanout for 74LVC1G79 DFFs, not all ICs with shared pin 2).
    """
    return {c["ref"]: c["part"] for c in netlist_data["components"]}


def preroute_dff_to_buffer(pcb, netlist_data):
    """Route DFF Q (pin 4) to Buffer A (pin 2) on F.Cu.

    With ICs at 90° CW:
      DFF pin 4 (Q):    (IC_x + 0.50, by + 0.25)
      Buffer pin 2 (A): (IC_x,        by + 4.75)   [Buffer at IC_x, by + 4.5]

    3-segment route:
      1) vertical   (IC_x+0.50, by+0.25)  → (IC_x+0.50, by+3.75)
      2) 45° diag   (IC_x+0.50, by+3.75)  → (IC_x,      by+4.25)
      3) vertical   (IC_x,      by+4.25)  → (IC_x,      by+4.75)

    Only matches 74LVC1G79 (DFF) to 74LVC1G125 (Buffer) pairs.

    Returns number of trace segments added.
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    traces = 0

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue

        # Only DFFs (74LVC1G79)
        if ref_to_part.get(ref) != "74LVC1G79":
            continue

        dff_q_net = pcb.get_pad_net(ref, "4")
        if dff_q_net is None or dff_q_net == 0:
            continue

        dff_x, dff_y = fp.position.X, fp.position.Y

        # Find a 74LVC1G125 buffer whose pin 2 (A) shares this net
        pads_on_net = net_to_pads.get(dff_q_net, [])
        buf_ref = None
        buf_pad2_pos = None
        best_dist = float("inf")
        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if (pad_ref.startswith("U") and pad_ref != ref
                    and pad_num == "2"
                    and ref_to_part.get(pad_ref) == "74LVC1G125"):
                dist = math.sqrt((px - dff_x)**2 + (py - dff_y)**2)
                if dist < IC_CELL_H * 2 and dist < best_dist:
                    best_dist = dist
                    buf_ref = pad_ref
                    buf_pad2_pos = (px, py)

        if buf_ref is None:
            continue

        # Get exact pad positions
        dff_q_pos = pcb.get_pad_position(ref, "4")

        # 3-segment route: vertical → 45° diagonal → vertical
        # Midpoint 1: same X as DFF Q, Y = buffer_Y - 2.0 (pushed up
        # to clear buffer VCC power stub at buf_y + 4.25)
        mid1_x = dff_q_pos[0]
        mid1_y = round(buf_pad2_pos[1] - 2.0, 2)
        # Midpoint 2: same X as Buffer A, Y = mid1_y + dx offset for 45°
        mid2_x = buf_pad2_pos[0]
        mid2_y = round(mid1_y + abs(mid1_x - mid2_x), 2)

        pcb.add_trace(dff_q_pos, (mid1_x, mid1_y), dff_q_net,
                       SIGNAL_TRACE_W, "F.Cu")
        pcb.add_trace((mid1_x, mid1_y), (mid2_x, mid2_y), dff_q_net,
                       SIGNAL_TRACE_W, "F.Cu")
        pcb.add_trace((mid2_x, mid2_y), buf_pad2_pos, dff_q_net,
                       SIGNAL_TRACE_W, "F.Cu")
        traces += 3

    return traces


def preroute_clk_fanout(pcb, netlist_data):
    """Route CLK fanout bus for each byte group on F.Cu.

    DFF CLK is pin 2. At 90° CW, pin 2 is at (IC_x, by + 0.25).
    All 8 DFFs in a byte share the same CLK net.

    Creates a horizontal bus at Y = by - 1.5 with vertical stubs
    down to each CLK pin:
      Bus:   (X_0, by-1.5) → → → (X_7, by-1.5)   [7 segments]
      Stubs: (X_i, by-1.5) → (X_i, by+0.25)       [8 stubs, 1.75mm each]

    Only matches 74LVC1G79 (DFF) ICs.

    Returns number of trace segments added.
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    traces = 0

    # Group DFF pin 2 (CLK) by net number
    clk_groups = defaultdict(list)

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC1G79":
            continue

        clk_net = pcb.get_pad_net(ref, "2")
        if clk_net is None or clk_net == 0:
            continue

        pad_pos = pcb.get_pad_position(ref, "2")
        clk_groups[clk_net].append((ref, pad_pos[0], pad_pos[1]))

    for net_num, members in clk_groups.items():
        if len(members) < 2:
            continue  # Not a fanout bus

        # Sort by X position (left to right)
        members.sort(key=lambda m: m[1])

        # Bus Y: 1.5mm above the CLK pin Y
        bus_y = round(members[0][2] - 1.5, 2)

        # Horizontal bus segments between adjacent members
        for i in range(len(members) - 1):
            x1 = members[i][1]
            x2 = members[i + 1][1]
            pcb.add_trace((x1, bus_y), (x2, bus_y), net_num,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        # Vertical stubs from bus down to each CLK pin
        for ref, pin_x, pin_y in members:
            pcb.add_trace((pin_x, bus_y), (pin_x, pin_y), net_num,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return traces


def preroute_oe_fanout(pcb, netlist_data):
    """Route OE fanout bus for each byte group on F.Cu.

    Buffer OE is pin 1. At 90° CW, pin 1 is at (IC_x - 0.50, by + 4.75).

    All F.Cu routing (no B.Cu or vias needed — B.Cu approach fails because
    through-hole power vias have copper on all layers including B.Cu):
      F.Cu bus:   (X_0-0.50, by+6.0) → → → (X_7-0.50, by+6.0)  [7 segments]
      F.Cu stubs: (X_i-0.50, by+6.0) → (X_i-0.50, by+4.75)     [8 stubs, 1.25mm each]

    Bus at Y = OE_pin_Y + 1.25 clears LED GND vias at Y ≈ pin_Y + 0.35.

    Only matches 74LVC1G125 (Buffer) ICs.

    Returns trace count.
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    traces = 0

    # Group buffer pin 1 (OE) by net number
    oe_groups = defaultdict(list)

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC1G125":
            continue

        oe_net = pcb.get_pad_net(ref, "1")
        if oe_net is None or oe_net == 0:
            continue

        pad_pos = pcb.get_pad_position(ref, "1")
        oe_groups[oe_net].append((ref, pad_pos[0], pad_pos[1]))

    for net_num, members in oe_groups.items():
        if len(members) < 2:
            continue  # Not a fanout bus

        # Sort by X position (left to right)
        members.sort(key=lambda m: m[1])

        # Bus Y: 1.25mm below OE pin Y (clears LED GND vias)
        bus_y = round(members[0][2] + 1.25, 2)

        # F.Cu horizontal bus segments between adjacent members
        for i in range(len(members) - 1):
            x1 = members[i][1]
            x2 = members[i + 1][1]
            pcb.add_trace((x1, bus_y), (x2, bus_y), net_num,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        # F.Cu vertical stubs from bus up to each OE pin
        for ref, pin_x, pin_y in members:
            pcb.add_trace((pin_x, bus_y), (pin_x, pin_y), net_num,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return traces


def preroute_connector_leds(pcb, netlist_data):
    """Route connector signal pins to bus indicator LED+R chains.

    For each J1 signal pin (excluding GND/VCC):
      1. L-trace from J1 pad to LED anode (LED has signal net after swap)
      2. L-trace from LED cathode to R pad

    The J1-to-LED horizontal trace (~8mm) doubles as a partial fanout stub,
    giving the autorouter a head start toward destination blocks.

    Returns number of trace segments added.
    """
    net_to_pads = _build_net_pad_index(pcb)
    traces = 0

    # Find connector J1
    j1_fp = None
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if ref == "J1":
            j1_fp = fp
            break

    if j1_fp is None:
        print("  WARNING: J1 connector not found, skipping connector pre-routing")
        return 0

    # Iterate over J1 pads, find matching bus indicator LED+R, and route
    fp_x, fp_y = j1_fp.position.X, j1_fp.position.Y
    angle_rad = math.radians(j1_fp.position.angle or 0)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    for pad in j1_fp.pads:
        if not (pad.net and pad.net.number and pad.net.number > 0):
            continue
        if pad.net.name in ("GND", "VCC"):
            continue

        sig_net = pad.net.number

        # J1 pad absolute position
        px, py = pad.position.X, pad.position.Y
        j1_x = round(fp_x + px * cos_a + py * sin_a, 2)
        j1_y = round(fp_y - px * sin_a + py * cos_a, 2)

        # Find the bus indicator LED on this signal net (closest to connector)
        pads_on_net = net_to_pads.get(sig_net, [])
        led_ref = None
        led_anode_pad = None
        led_anode_pos = None
        best_dist = float("inf")
        for pad_ref, pad_num, pad_x, pad_y, pnet in pads_on_net:
            if pad_ref.startswith("D"):
                dist = abs(pad_x - j1_x)
                if dist < 15 and dist < best_dist:
                    best_dist = dist
                    led_ref = pad_ref
                    led_anode_pad = pad_num
                    led_anode_pos = (pad_x, pad_y)

        if led_ref is None:
            continue

        # Find LED's other pad (cathode) and its net
        led_cathode_pad = "1" if led_anode_pad == "2" else "2"
        led_cathode_net = pcb.get_pad_net(led_ref, led_cathode_pad)
        led_cathode_pos = pcb.get_pad_position(led_ref, led_cathode_pad)

        # Find R pad on the LED-cathode net (closest to LED)
        r_ref = None
        r_pad_pos = None
        best_dist = float("inf")
        if led_cathode_net:
            for pad_ref, pad_num, pad_x, pad_y, pnet in net_to_pads.get(led_cathode_net, []):
                if pad_ref.startswith("R"):
                    dist = math.sqrt((pad_x - led_cathode_pos[0])**2 +
                                     (pad_y - led_cathode_pos[1])**2)
                    if dist < 5 and dist < best_dist:
                        best_dist = dist
                        r_ref = pad_ref
                        r_pad_pos = (pad_x, pad_y)

        # Route 1: J1 pad to LED anode (L-trace, vertical first)
        segs = pcb.add_l_trace((j1_x, j1_y), led_anode_pos, sig_net,
                               SIGNAL_TRACE_W, "F.Cu", horizontal_first=False)
        traces += len(segs)

        # Route 2: LED cathode to R pad (L-trace)
        if r_ref and r_pad_pos and led_cathode_net:
            segs = pcb.add_l_trace(led_cathode_pos, r_pad_pos, led_cathode_net,
                                   SIGNAL_TRACE_W, "F.Cu", horizontal_first=True)
            traces += len(segs)

        # Route 3: Fanout stub from LED anode, down past R then right.
        # Gives the autorouter a consistent starting point past the LED bank.
        if r_ref and r_pad_pos:
            stub_end_x = round(r_pad_pos[0] + 2.0, 2)
            stub_y = round(j1_y + 1.4, 2)
            segs = pcb.add_l_trace(led_anode_pos, (stub_end_x, stub_y), sig_net,
                                   SIGNAL_TRACE_W, "F.Cu", horizontal_first=False)
            traces += len(segs)

    return traces


def preroute_data_bus(pcb, netlist_data, col_boundary_x):
    """Preroute D* data bus with F.Cu vias and B.Cu trunk routing.

    For each data bit (D0-D7), within each byte column (4 bytes):
      1. DFF pin 1: F.Cu stub down 0.60mm to via (F.Cu -> B.Cu)
      2. BUF pin 4: F.Cu stub down 0.40mm to via (F.Cu -> B.Cu)
      3. B.Cu vertical trunk at IC center X, chaining all 8 vias
      4. B.Cu horizontal stubs from trunk to each via (0.50mm)

    Clearance verification (all pass with 0.20mm min clearance):
      DFF via at (pin1_x, pin1_y+0.60):
        - DFF GND via: 1.47mm c-c (need 1.05) OK
        - CLK bus: 2.1mm gap OK
      BUF via at (pin4_x, pin4_y+0.53):
        - BUF pin 2 pad: 0.73mm (need 0.72) OK
        - BUF VCC via: 1.41mm c-c (need 1.05) OK
        - OE bus F.Cu edge gap: 0.22mm (need 0.20) OK
      B.Cu trunk at IC center X:
        - All power vias: 1.04mm X distance (need 0.70) OK

    Args:
        pcb: PCBBuilder instance with components placed
        netlist_data: Parsed netlist dict
        col_boundary_x: X coordinate separating byte column 0 from column 1

    Returns:
        (via_count, trace_count) tuple.
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)

    DBUS_VIA_SIZE = VIA_SIZE    # 0.8mm
    DBUS_VIA_DRILL = VIA_DRILL  # 0.4mm
    DFF_VIA_DY = 0.60           # via offset below DFF pin 1
    BUF_VIA_DY = 0.53           # via offset below BUF pin 4
    # BUF_VIA_DY bounds: >=0.52 for pin 2 pad clearance, <=0.55 for OE bus

    vias = 0
    traces = 0

    # Find all D* nets: nets that connect both a DFF pin 1 and a BUF pin 4
    dbus_nets = {}  # net_num -> net_name
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC1G79":
            continue
        d_net = pcb.get_pad_net(ref, "1")
        if d_net is None or d_net == 0:
            continue
        pads_on_net = net_to_pads.get(d_net, [])
        has_buf_pin4 = any(
            pad_ref.startswith("U") and pad_num == "4"
            and ref_to_part.get(pad_ref) == "74LVC1G125"
            for pad_ref, pad_num, px, py, pnet in pads_on_net
        )
        if has_buf_pin4:
            net_name = pads_on_net[0][4] if pads_on_net else f"net_{d_net}"
            dbus_nets[d_net] = net_name

    # For each D* net, collect DFF and BUF pad positions, grouped by column
    for net_num, net_name in sorted(dbus_nets.items(), key=lambda x: x[1]):
        pads_on_net = net_to_pads.get(net_num, [])

        # Collect DFF pin 1 and BUF pin 4 positions with their IC centers
        # Each entry: (ic_center_x, via_x, via_y, is_buf, pad_x, pad_y)
        via_entries = []

        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if not pad_ref.startswith("U"):
                continue
            part = ref_to_part.get(pad_ref)

            if part == "74LVC1G79" and pad_num == "1":
                # DFF pin 1 at (IC_x - 0.50, IC_y + 0.25)
                # IC center X = pin1_x + 0.50
                ic_cx = round(px + 0.50, 2)
                via_x = round(px, 2)
                via_y = round(py + DFF_VIA_DY, 2)
                via_entries.append((ic_cx, via_x, via_y, False, px, py))

            elif part == "74LVC1G125" and pad_num == "4":
                # BUF pin 4 at (IC_x + 0.50, IC_y + 0.25)
                # IC center X = pin4_x - 0.50
                ic_cx = round(px - 0.50, 2)
                via_x = round(px, 2)
                via_y = round(py + BUF_VIA_DY, 2)
                via_entries.append((ic_cx, via_x, via_y, True, px, py))

        # Group by column using col_boundary_x
        col0 = [e for e in via_entries if e[0] < col_boundary_x]
        col1 = [e for e in via_entries if e[0] >= col_boundary_x]

        for col_entries in [col0, col1]:
            if len(col_entries) < 2:
                continue

            # Sort by Y (top to bottom): DFF0, BUF0, DFF1, BUF1, ...
            col_entries.sort(key=lambda e: e[2])

            # All entries in a column share the same IC center X
            trunk_x = col_entries[0][0]

            # Place vias and F.Cu stubs
            via_positions = []  # (via_x, via_y) in Y order
            for ic_cx, via_x, via_y, is_buf, pad_x, pad_y in col_entries:
                # F.Cu stub from pad to via
                pcb.add_trace((pad_x, pad_y), (via_x, via_y),
                              net_num, SIGNAL_TRACE_W, "F.Cu")
                traces += 1

                # Via F.Cu -> B.Cu
                pcb.add_via((via_x, via_y), net_num,
                            DBUS_VIA_SIZE, DBUS_VIA_DRILL, ["F.Cu", "B.Cu"])
                vias += 1

                via_positions.append((via_x, via_y))

            # B.Cu routing: vertical trunk at trunk_x with horizontal stubs
            # Chain all vias in Y order
            for i, (vx, vy) in enumerate(via_positions):
                # Horizontal stub: via to trunk
                if abs(vx - trunk_x) > 0.01:
                    pcb.add_trace((vx, vy), (trunk_x, vy),
                                  net_num, SIGNAL_TRACE_W, "B.Cu")
                    traces += 1

                # Vertical trunk segment to next via's Y
                if i < len(via_positions) - 1:
                    next_vy = via_positions[i + 1][1]
                    pcb.add_trace((trunk_x, vy), (trunk_x, next_vy),
                                  net_num, SIGNAL_TRACE_W, "B.Cu")
                    traces += 1

    return vias, traces


# --------------------------------------------------------------
# Layer visibility test grid (for clear PCB fabrication)
# --------------------------------------------------------------

# Layer rank for fill/text visibility ordering
LAYER_RANK = {"F.Cu": 3, "In1.Cu": 2, "In2.Cu": 1, "B.Cu": 0}

# Test grid dimensions (mm)
TEST_CELL_W = 6.0
TEST_CELL_H = 3.5
TEST_CELL_GAP = 0.5
TEST_TEXT_SIZE = 0.8
TEST_LABEL_W = 8.0     # width for row labels
TEST_HEADER_H = 3.0    # height for column headers
TEST_TITLE_H = 2.5     # height for title above headers


def add_layer_test_grid(pcb, origin_x, origin_y):
    """Add a test grid for clear PCB layer visibility testing.

    Rows: no-fill, no-fill knockout, B.Cu/In2/In1/F.Cu fills.
    Columns 0-3: text on F.Cu / In1.Cu / In2.Cu / B.Cu.
    Column 4: knockout text on the fill layer (negative space).

    Returns (grid_width, grid_height) of the total test grid area.
    """
    # (fill_layer, label, row_is_knockout)
    fill_rows = [
        (None,     "No Fill",    False),
        (None,     "KO No Fill", True),
        ("B.Cu",   "B.Cu Fill",  False),
        ("In2.Cu", "In2 Fill",   False),
        ("In1.Cu", "In1 Fill",   False),
        ("F.Cu",   "F.Cu Fill",  False),
    ]
    # Columns: 4 text layers + 1 negative-space column
    text_cols = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
    col_headers = text_cols + ["Negative"]
    n_cols = len(col_headers)

    n_rows = len(fill_rows)
    step_x = TEST_CELL_W + TEST_CELL_GAP
    step_y = TEST_CELL_H + TEST_CELL_GAP

    # Grid content origin (after title, headers, and row labels)
    gx0 = origin_x + TEST_LABEL_W
    gy0 = origin_y + TEST_TITLE_H + TEST_HEADER_H

    total_w = TEST_LABEL_W + n_cols * step_x
    total_h = TEST_TITLE_H + TEST_HEADER_H + n_rows * step_y

    # --- Title ---
    pcb.add_silkscreen_text(
        "LAYER TEST", origin_x + total_w / 2, origin_y + TEST_TITLE_H / 2,
        size=1.0, layer="F.SilkS", thickness=0.15)

    # --- Column headers ---
    for ci, header in enumerate(col_headers):
        cx = gx0 + ci * step_x + TEST_CELL_W / 2
        cy = gy0 - TEST_HEADER_H / 2
        pcb.add_silkscreen_text(header, cx, cy, size=0.8, layer="F.SilkS")

    # --- Row labels ---
    for ri, (_, label, _) in enumerate(fill_rows):
        lx = origin_x + TEST_LABEL_W / 2
        ly = gy0 + ri * step_y + TEST_CELL_H / 2
        pcb.add_silkscreen_text(label, lx, ly, size=0.8, layer="F.SilkS")

    # --- Border rectangle ---
    pcb.add_silkscreen_rect(
        gx0 - 0.5, gy0 - 0.5,
        n_cols * step_x + 0.5, n_rows * step_y + 0.5,
        layer="F.SilkS", stroke_width=0.15)

    # --- Vertical column separators ---
    grid_top = gy0 - 0.5
    grid_bot = gy0 + n_rows * step_y
    for ci in range(1, n_cols):
        sep_x = gx0 + ci * step_x - TEST_CELL_GAP / 2
        pcb.add_silkscreen_line(sep_x, grid_top, sep_x, grid_bot,
                                layer="F.SilkS", stroke_width=0.15)

    # --- Keepout zones and fill zones per row ---
    for ri, (fill_layer, _, _) in enumerate(fill_rows):
        row_y0 = gy0 + ri * step_y - TEST_CELL_GAP / 2
        row_y1 = row_y0 + TEST_CELL_H + TEST_CELL_GAP
        row_x0 = gx0 - TEST_CELL_GAP / 2
        row_x1 = row_x0 + n_cols * step_x
        row_outline = [(row_x0, row_y0), (row_x1, row_y0),
                       (row_x1, row_y1), (row_x0, row_y1)]

        # Block In1.Cu (GND) zone where this row does NOT want In1.Cu fill
        if fill_layer != "In1.Cu":
            pcb.add_keepout_zone("In1.Cu", row_outline)

        # Block In2.Cu (VCC) zone where this row does NOT want In2.Cu fill
        if fill_layer != "In2.Cu":
            pcb.add_keepout_zone("In2.Cu", row_outline)

        # Add new copper pour zone for F.Cu or B.Cu fill rows
        if fill_layer in ("F.Cu", "B.Cu"):
            pcb.add_zone("GND", fill_layer, row_outline, clearance=0.3)

    # --- Cell text (columns 0-3: per-layer text) ---
    # When text_rank <= fill_rank, multiple columns place identical copper
    # text on the fill layer.  Use those duplicates to test solder mask
    # removal: first dup = normal, then no-mask-both, F-only, B-only.
    MASK_VARIANTS = [None, "both", "front", "back"]

    for ri, (fill_layer, _, row_ko) in enumerate(fill_rows):
        fill_rank = LAYER_RANK.get(fill_layer, -1) if fill_layer else -1
        dup_idx = 0  # tracks position within duplicate group

        for ci, text_layer in enumerate(text_cols):
            text_rank = LAYER_RANK[text_layer]
            cx = gx0 + ci * step_x + TEST_CELL_W / 2
            cy = gy0 + ri * step_y + TEST_CELL_H / 2

            if row_ko:
                # Knockout row: knockout text on each layer, no fill
                pcb.add_silkscreen_text(
                    "TEST", cx, cy, size=TEST_TEXT_SIZE,
                    layer=text_layer, knockout=True)
            elif fill_layer is None or text_rank > fill_rank:
                # Additive: copper text on text layer (above fill)
                pcb.add_silkscreen_text(
                    "TEST", cx, cy, size=TEST_TEXT_SIZE, layer=text_layer)
            else:
                # Same/below: copper text on fill layer (duplicate)
                pcb.add_silkscreen_text(
                    "TEST", cx, cy, size=TEST_TEXT_SIZE, layer=fill_layer)

                # Apply mask variant to this duplicate
                variant = MASK_VARIANTS[min(dup_idx, len(MASK_VARIANTS) - 1)]
                if variant == "both":
                    pcb.add_mask_opening(cx, cy, TEST_CELL_W, TEST_CELL_H)
                elif variant == "front":
                    pcb.add_mask_opening(cx, cy, TEST_CELL_W, TEST_CELL_H,
                                         back=False)
                elif variant == "back":
                    pcb.add_mask_opening(cx, cy, TEST_CELL_W, TEST_CELL_H,
                                         front=False)
                dup_idx += 1

    # --- Cell text (column 4: knockout / negative space) ---
    neg_ci = len(text_cols)
    for ri, (fill_layer, _, row_ko) in enumerate(fill_rows):
        cx = gx0 + neg_ci * step_x + TEST_CELL_W / 2
        cy = gy0 + ri * step_y + TEST_CELL_H / 2

        if row_ko:
            # KO No Fill row, Negative col: empty — use for mask test
            # (no copper, no fill — shows bare substrate vs masked substrate)
            pcb.add_mask_opening(cx, cy, TEST_CELL_W, TEST_CELL_H)
        elif fill_layer is not None:
            # Knockout: letter shapes cut out of fill copper
            pcb.add_silkscreen_text(
                "TEST", cx, cy, size=TEST_TEXT_SIZE,
                layer=fill_layer, knockout=True)
        else:
            # No fill, Negative col: empty cell (nothing to knock out of)
            pass

    return total_w, total_h


# --------------------------------------------------------------
# Main
# --------------------------------------------------------------

def main():
    print("=" * 60)
    print("Discrete NES - RAM Prototype PCB Generator")
    print("=" * 60)

    # Step 1: Create custom DSBGA footprints
    print("\n[1/7] Creating custom DSBGA footprints...")
    fp5_path, fp6_path = create_dsbga_footprints(SHARED_FP_DIR)
    print(f"  Created: {os.path.basename(fp5_path)}")
    print(f"  Created: {os.path.basename(fp6_path)}")

    # Step 2: Export netlist from schematic
    print("\n[2/7] Exporting netlist from schematic...")
    sch_path = os.path.join(BOARD_DIR, "ram.kicad_sch")
    net_path = os.path.join(BOARD_DIR, "ram.xml")
    export_netlist(sch_path, net_path)
    netlist_data = parse_netlist(net_path)
    print(f"  Components: {len(netlist_data['components'])}")
    print(f"  Nets: {len(netlist_data['nets'])}")

    # Step 3: Group components by hierarchy
    print("\n[3/7] Grouping components by hierarchy...")
    groups = group_components(netlist_data)
    for name, comps in sorted(groups.items()):
        print(f"  {name}: {len(comps)} components")

    # Step 4: Initialize PCB builder
    print("\n[4/7] Initializing PCB...")
    pcb = PCBBuilder(title="8-Byte Discrete RAM Prototype")
    pcb.add_fp_lib_path("DSBGA_Packages", SHARED_FP_DIR)

    # Register all nets
    pcb.add_nets_from_netlist(netlist_data)

    # Configure 4-layer stackup with B.Cu as jumper layer
    pcb.set_4layer_stackup()
    pcb.set_layer_type("B.Cu", "jumper")

    # Step 5: Place components
    print("\n[5/7] Placing components...")

    # Layout:
    #   Column 0: Connector (root) on the left
    #   Column 1: addr_decoder (top) + control_logic (bottom)
    #   Columns 2+: RAM bytes in 4-col x 2-row grid
    #   Below RAM: write_clk_gen + read_oe_gen

    # Pre-compute layouts for each group
    group_layouts = {}
    group_sizes = {}
    # Track which cell dimensions each group uses (for compute_group_size)
    group_cell_dims = {}
    for name, comps in groups.items():
        # Determine max columns and cell dimensions based on group type
        is_ram = name.startswith("byte")
        is_ctrl = name in ("addr_decoder", "control_logic",
                           "write_clk_gen", "read_oe_gen")
        if name == "root":
            max_cols = 3  # Connector + root LEDs
        elif is_ram:
            max_cols = 8  # 8 bits per line (DFFs row + buffers row)
        elif name == "addr_decoder":
            max_cols = 4  # 3 INV + 8 AND3
        else:
            max_cols = 3

        # RAM bytes use tight spacing; control logic uses wider spacing
        if is_ctrl:
            cw, ch = CTRL_CELL_W, CTRL_CELL_H
        else:
            cw, ch = IC_CELL_W, IC_CELL_H
        group_cell_dims[name] = (cw, ch)

        ic_cells, standalone, others = sort_components_for_placement(comps)

        # Reverse bit order within byte groups: MSB (D7) on the left
        if is_ram:
            dff_cells = [c for c in ic_cells if c[0]["part"] == "74LVC1G79"]
            buf_cells = [c for c in ic_cells if c[0]["part"] == "74LVC1G125"]
            ic_cells = list(reversed(dff_cells)) + list(reversed(buf_cells))

        placements = compute_group_layout(ic_cells, standalone, max_cols,
                                          cell_w=cw, cell_h=ch)

        # Add connector and other non-IC components
        if others:
            if name == "root":
                # Root group: connector on the left, bus LEDs aligned to
                # their matching connector pin Y positions.
                # PinHeader_1x16 pad Y: pin N at (N-1)*2.54mm
                conn_x = 0.0
                led_x = 7.0   # LED offset right of connector (closer)
                r_x = 10.0   # R offset right of connector (further, more gap)

                # Find connector pin-to-net mapping (excluding power nets)
                j1 = others[0]  # J1 is the only non-R/D/U in root
                pin_y_by_net = {}
                for pin_num, net_name in j1["pins"].items():
                    if net_name not in ("GND", "VCC"):
                        # At 180°, pins extend upward (negative Y direction)
                        pin_y_by_net[net_name] = -(int(pin_num) - 1) * CONN_PIN_PITCH

                # Clear standalone placements and rebuild aligned to pins
                placements = []

                # Place connector
                placements.append((j1, conn_x, 0.0))

                # Place each R+LED pair at its matching connector pin Y
                # After swap: LED has the signal net that matches a connector pin
                for r_comp, led_comp in standalone:
                    led_nets = set(led_comp["pins"].values()) if led_comp else set()
                    matched_y = None
                    for net_name in led_nets:
                        if net_name in pin_y_by_net:
                            matched_y = pin_y_by_net[net_name]
                            break

                    if matched_y is not None:
                        if led_comp:
                            placements.append((led_comp, led_x, matched_y))
                        if r_comp:
                            placements.append((r_comp, r_x, matched_y))
                    else:
                        # Fallback (shouldn't happen for bus indicator LEDs)
                        if led_comp:
                            placements.append((led_comp, led_x, 0.0))
                        if r_comp:
                            placements.append((r_comp, r_x, 0.0))

                # Normalize: shift so minimum Y is 0 (connector at 180°
                # has negative Y offsets; shifting keeps everything in
                # positive territory for board outline computation)
                min_rel_y = min(y for _, _, y in placements)
                if min_rel_y < 0:
                    placements = [(c, x, y - min_rel_y) for c, x, y in placements]
            else:
                for i, comp in enumerate(others):
                    placements.append((comp, 0.0, i * CONN_PIN_PITCH))

        group_layouts[name] = placements
        cw, ch = group_cell_dims.get(name, (IC_CELL_W, IC_CELL_H))
        group_sizes[name] = compute_group_size(placements, cell_w=cw, cell_h=ch)

    # --- Compute absolute positions ---
    total_placed = 0

    # Column 0: Connector (root group) on the far left
    col0_x = PLACEMENT_ORIGIN
    col0_y = PLACEMENT_ORIGIN
    root_w, root_h = group_sizes.get("root", (0, 0))

    # Column 1: addr_decoder stacked above control_logic
    col1_x = col0_x + root_w + GROUP_GAP_X
    col1_y = PLACEMENT_ORIGIN
    dec_w, dec_h = group_sizes.get("addr_decoder", (0, 0))
    ctrl_w, ctrl_h = group_sizes.get("control_logic", (0, 0))
    col1_w = max(dec_w, ctrl_w)

    # Column 2+: RAM bytes in 2-col × 4-row grid (column-major: down first)
    #   Col 0: byte_0, byte_1, byte_2, byte_3
    #   Col 1: byte_4, byte_5, byte_6, byte_7
    ram_x = col1_x + col1_w + GROUP_GAP_X
    ram_y = PLACEMENT_ORIGIN

    byte_col0 = ["byte_0", "byte_1", "byte_2", "byte_3"]
    byte_col1 = ["byte_4", "byte_5", "byte_6", "byte_7"]
    all_bytes = byte_col0 + byte_col1

    # Compute byte grid dimensions from actual placement positions
    # (not from compute_group_size which adds IC_CELL_W/IC_CELL_H padding)
    byte_col_w = max((group_sizes.get(b, (0, 0))[0] for b in all_bytes), default=0)
    byte_row_h = max((group_sizes.get(b, (0, 0))[1] for b in all_bytes), default=0)

    # Byte column center span: compute from actual placement positions (not
    # compute_group_size which adds IC_CELL_W padding).
    byte_center_span_x = 0
    for b in all_bytes:
        layout = group_layouts.get(b, [])
        if layout:
            xs = [x for _, x, _ in layout]
            byte_center_span_x = max(byte_center_span_x, max(xs) - min(xs))

    # Total RAM area height
    ram_total_h = 4 * byte_row_h + 3 * GROUP_GAP_Y

    # Vertically center connector and decode/ctrl column with RAM area
    col0_y = PLACEMENT_ORIGIN + max(0, (ram_total_h - root_h) / 2)
    # Stack decoder + ctrl to fill the height next to RAM
    col1_y = PLACEMENT_ORIGIN + 4.0  # shifted down 4mm

    # Place connector (root) — connector bus LEDs horizontal (180°),
    # connector Rs horizontal (0°) for clean LED→R trace clearance
    if "root" in group_layouts:
        for comp, rel_x, rel_y in group_layouts["root"]:
            if comp["part"] == "LED_Small":
                override = 180
            elif comp["part"] == "R_Small":
                override = 0
            else:
                override = None
            _place_component(pcb, comp, col0_x + rel_x, col0_y + rel_y,
                             netlist_data, angle_override=override)
            total_placed += 1

    # Add silkscreen pin name labels to the left of the connector
    # Pin ordering: pin 1 (GND) at bottom, pin 16 (VCC) at top
    # After 180° rotation + normalization (shift = 14*pitch, since pin 15
    # is the highest signal pin — VCC/GND excluded from LED placements),
    # pin N is at rel_y = (15 - N) * CONN_PIN_PITCH
    conn_pin_names = {
        1: "GND", 2: "D7", 3: "D6", 4: "D5", 5: "D4",
        6: "D3", 7: "D2", 8: "D1", 9: "D0", 10: "nCE",
        11: "nWE", 12: "nOE", 13: "A2", 14: "A1", 15: "A0", 16: "VCC",
    }
    label_x = round(col0_x - 3.0, 2)  # 3mm to the left of connector center
    for pin_num, pin_name in conn_pin_names.items():
        label_y = round(col0_y + (15 - pin_num) * CONN_PIN_PITCH, 2)
        pcb.add_silkscreen_text(pin_name, label_x, label_y, size=0.8)

    # Place addr_decoder (top of column 1)
    if "addr_decoder" in group_layouts:
        for comp, rel_x, rel_y in group_layouts["addr_decoder"]:
            _place_component(pcb, comp, col1_x + rel_x, col1_y + rel_y, netlist_data)
            total_placed += 1

    # Place control_logic (below addr_decoder in column 1)
    ctrl_y = col1_y + dec_h + GROUP_GAP_Y
    if "control_logic" in group_layouts:
        for comp, rel_x, rel_y in group_layouts["control_logic"]:
            _place_component(pcb, comp, col1_x + rel_x, ctrl_y + rel_y, netlist_data)
            total_placed += 1

    # Place RAM bytes: column-major (down first, then right)
    # Track absolute positions for silkscreen annotation
    byte_bounds = {}  # name -> (min_x, min_y, max_x, max_y)
    for col_idx, byte_col in enumerate([byte_col0, byte_col1]):
        # Column offset: center span already includes R positions (rightmost R
        # at last_IC_x + R_OFFSET_X). Just add R courtyard half-width + gap +
        # IC courtyard half-width to get actual physical gap = BYTE_COL_GAP.
        bx = ram_x + col_idx * (byte_center_span_x + 0.5 + BYTE_COL_GAP + 0.75)
        for row_idx, name in enumerate(byte_col):
            if name not in group_layouts:
                continue
            by = ram_y + row_idx * (byte_row_h + GROUP_GAP_Y)
            abs_positions = []
            for comp, rel_x, rel_y in group_layouts[name]:
                abs_x = bx + rel_x
                abs_y = by + rel_y
                _place_component(pcb, comp, abs_x, abs_y, netlist_data)
                total_placed += 1
                abs_positions.append((abs_x, abs_y))

            if abs_positions:
                xs = [p[0] for p in abs_positions]
                ys = [p[1] for p in abs_positions]
                byte_bounds[name] = (min(xs), min(ys), max(xs), max(ys))

    # Add unified silkscreen grid around all 8 bytes
    SILK_MARGIN = 3.0  # mm margin around component centers
    if byte_bounds:
        # Compute outer bounding box of all bytes
        all_min_x = min(b[0] for b in byte_bounds.values())
        all_min_y = min(b[1] for b in byte_bounds.values())
        all_max_x = max(b[2] for b in byte_bounds.values())
        all_max_y = max(b[3] for b in byte_bounds.values())

        grid_x1 = round(all_min_x - SILK_MARGIN, 2)
        grid_y1 = round(all_min_y - SILK_MARGIN, 2)
        grid_x2 = round(all_max_x + SILK_MARGIN, 2)
        grid_y2 = round(all_max_y + SILK_MARGIN, 2)

        # Outer rectangle
        pcb.add_silkscreen_rect(grid_x1, grid_y1,
                                round(grid_x2 - grid_x1, 2),
                                round(grid_y2 - grid_y1, 2))

        # Vertical divider between column 0 and column 1
        # Midpoint of gap between the two byte columns
        col0_max_x = max(byte_bounds[b][2] for b in byte_col0 if b in byte_bounds)
        col1_min_x = min(byte_bounds[b][0] for b in byte_col1 if b in byte_bounds)
        div_x = round((col0_max_x + col1_min_x) / 2 - 0.25, 2)
        pcb.add_silkscreen_line(div_x, grid_y1, div_x, grid_y2)

        # 3 horizontal dividers between rows
        for row in range(3):
            top_name = f"byte_{row}"
            bot_name = f"byte_{row + 1}"
            if top_name in byte_bounds and bot_name in byte_bounds:
                top_max_y = byte_bounds[top_name][3]
                bot_min_y = byte_bounds[bot_name][1]
                div_y = round((top_max_y + bot_min_y) / 2, 2)
                pcb.add_silkscreen_line(grid_x1, div_y, grid_x2, div_y)

        # Address labels on the outside border of the grid
        for name, (bmin_x, bmin_y, bmax_x, bmax_y) in byte_bounds.items():
            byte_idx = int(name.split("_")[1])
            label = f"0x{byte_idx}"
            # Vertically center label in the byte's row
            label_y = round((bmin_y + bmax_y) / 2, 2)
            if byte_idx < 4:
                # Left column: label on left edge, outside the grid
                label_x = round(grid_x1 - 1.5, 2)
            else:
                # Right column: label on right edge, outside the grid
                label_x = round(grid_x2 + 1.5, 2)
            pcb.add_silkscreen_text(label, label_x, label_y, size=1.0)

    print(f"  Silkscreen: unified 2x4 grid with address labels")

    # Place control logic below RAM: write_clk_gen + read_oe_gen
    ctrl_row_y = ram_y + ram_total_h + CTRL_ROW_GAP
    ctrl_row_x = ram_x
    for name in ["write_clk_gen", "read_oe_gen"]:
        if name not in group_layouts:
            continue
        w, h = group_sizes[name]
        for comp, rel_x, rel_y in group_layouts[name]:
            _place_component(pcb, comp, ctrl_row_x + rel_x, ctrl_row_y + rel_y, netlist_data)
            total_placed += 1
        ctrl_row_x += w + CTRL_GROUP_GAP_X

    print(f"  Total components placed: {total_placed}")

    # Step 6: Pre-route local connections
    print("\n[6/7] Pre-routing local connections...")
    pcb.build_ref_index()

    pwr_vias, pwr_traces = preroute_power_vias(pcb)
    print(f"  Power vias: {pwr_vias} vias, {pwr_traces} stub traces")

    ic_led_traces = preroute_ic_to_led(pcb, netlist_data)
    print(f"  IC->LED chains: {ic_led_traces} trace segments")

    led_r_traces = preroute_led_to_r(pcb, netlist_data)
    print(f"  LED->R chains: {led_r_traces} trace segments")

    dff_buf_traces = preroute_dff_to_buffer(pcb, netlist_data)
    print(f"  DFF Q->Buffer A: {dff_buf_traces} trace segments")

    clk_traces = preroute_clk_fanout(pcb, netlist_data)
    print(f"  CLK fanout: {clk_traces} trace segments")

    oe_traces = preroute_oe_fanout(pcb, netlist_data)
    print(f"  OE fanout: {oe_traces} trace segments")

    conn_traces = preroute_connector_leds(pcb, netlist_data)
    print(f"  Connector->LED + fanout stubs: {conn_traces} trace segments")

    col_boundary_x = ram_x + byte_center_span_x + 0.5 + BYTE_COL_GAP / 2
    dbus_vias, dbus_traces = preroute_data_bus(pcb, netlist_data, col_boundary_x)
    print(f"  D* data bus: {dbus_vias} vias, {dbus_traces} trace segments")

    total_vias = pwr_vias + dbus_vias
    total_traces = pwr_traces + ic_led_traces + led_r_traces + dff_buf_traces + clk_traces + oe_traces + conn_traces + dbus_traces
    print(f"  Total pre-routed: {total_vias} vias + {total_traces} traces")

    # Layer visibility test grid (for clear PCB) — right of control row
    test_x = ctrl_row_x
    test_y = ctrl_row_y
    test_grid_w, test_grid_h = add_layer_test_grid(pcb, test_x, test_y)
    print(f"\n  Layer test grid: {test_grid_w:.0f} x {test_grid_h:.0f} mm "
          f"at ({test_x:.1f}, {test_y:.1f})")

    # Step 7: Board outline and power planes
    print("\n[7/7] Adding board outline and power planes...")

    # Compute board dimensions from pad + courtyard extents
    if pcb.board.footprints:
        comp_min_x = comp_min_y = float('inf')
        comp_max_x = comp_max_y = float('-inf')

        for fp in pcb.board.footprints:
            fp_x, fp_y = fp.position.X, fp.position.Y
            angle = math.radians(fp.position.angle or 0)
            cos_a, sin_a = math.cos(angle), math.sin(angle)

            for pad in fp.pads:
                px, py = pad.position.X, pad.position.Y
                # KiCad uses clockwise rotation (positive angle = CW in Y-down)
                abs_x = fp_x + px * cos_a + py * sin_a
                abs_y = fp_y - px * sin_a + py * cos_a
                radius = max(pad.size.X, pad.size.Y) / 2 if pad.size else 0
                comp_min_x = min(comp_min_x, abs_x - radius)
                comp_max_x = max(comp_max_x, abs_x + radius)
                comp_min_y = min(comp_min_y, abs_y - radius)
                comp_max_y = max(comp_max_y, abs_y + radius)

            # Include courtyard graphics (F.CrtYd / B.CrtYd)
            for gi in fp.graphicItems:
                layer = getattr(gi, 'layer', '')
                if 'CrtYd' not in layer:
                    continue
                for attr in ('start', 'end'):
                    pt = getattr(gi, attr, None)
                    if pt is None:
                        continue
                    abs_x = fp_x + pt.X * cos_a - pt.Y * sin_a
                    abs_y = fp_y + pt.X * sin_a + pt.Y * cos_a
                    comp_min_x = min(comp_min_x, abs_x)
                    comp_max_x = max(comp_max_x, abs_x)
                    comp_min_y = min(comp_min_y, abs_y)
                    comp_max_y = max(comp_max_y, abs_y)

        # Extend board bounds for test grid (GrText, not footprints)
        comp_max_x = max(comp_max_x, test_x + test_grid_w)
        comp_max_y = max(comp_max_y, test_y + test_grid_h)

        # Add margin around component extents, ensuring the outline
        # stays within the sheet border (A4 landscape = 297x210mm,
        # with SHEET_BORDER minimum margin from the sheet edge).
        origin_x = max(math.floor(comp_min_x - BOARD_MARGIN), SHEET_BORDER)
        origin_y = max(math.floor(comp_min_y - BOARD_MARGIN), SHEET_BORDER)
        board_w = math.ceil(comp_max_x + BOARD_MARGIN - origin_x)
        board_h = math.ceil(comp_max_y + BOARD_MARGIN - origin_y)
    else:
        board_w, board_h = 80, 100
        origin_x, origin_y = 0, 0

    CORNER_RADIUS = 3.0  # mm fillet radius for rounded board corners
    pcb.set_board_outline(board_w, board_h, origin_x, origin_y,
                          corner_radius=CORNER_RADIUS)
    print(f"  Board outline: {board_w} x {board_h} mm (r={CORNER_RADIUS}mm corners)")
    print(f"  Origin: ({origin_x}, {origin_y})")

    # Power plane zones
    outline = [
        (origin_x, origin_y),
        (origin_x + board_w, origin_y),
        (origin_x + board_w, origin_y + board_h),
        (origin_x, origin_y + board_h),
    ]
    pcb.add_zone("GND", "In1.Cu", outline, clearance=0.3)
    pcb.add_zone("VCC", "In2.Cu", outline, clearance=0.3)
    print("  Added GND zone on In1.Cu")
    print("  Added VCC zone on In2.Cu")

    # Save PCB (hide all footprint text to avoid silk_overlap/silk_over_copper)
    pcb_path = os.path.join(BOARD_DIR, "ram.kicad_pcb")
    pcb.save(pcb_path, hide_text=True)
    print(f"\nSaved: {pcb_path}")

    # Cleanup netlist
    if os.path.exists(net_path):
        os.remove(net_path)

    # Fix routed board if it exists (apply same DRC fixes for KiCad
    # modifications: font sizes, extra properties, graphic element ordering)
    routed_path = os.path.join(BOARD_DIR, "ram_routed.kicad_pcb")
    if os.path.isfile(routed_path):
        print("\nApplying DRC fixes to routed board...")
        stats = fix_pcb_drc(routed_path)
        print(f"  Pad orientations fixed: {stats['pad_orientations']}")
        print(f"  Font sizes fixed: {stats['font_fixes']}")
        print(f"  Extra properties removed: {stats['props_removed']}")
        print(f"  Graphic attr reordered: {stats['attr_reordered']}")
        print(f"  Generator values quoted: {stats['generator_fixed']}")
        print(f"  Saved: {routed_path}")

    # Summary
    print(f"\n{'=' * 60}")
    print("PCB Generation Complete")
    print(f"{'=' * 60}")
    print(f"  Components: {total_placed}")
    print(f"  Pre-routed: {total_vias} vias + {total_traces} traces")
    print(f"  Board size: {board_w} x {board_h} mm")
    print(f"  Layers: 4 (F.Cu, In1.Cu=GND, In2.Cu=VCC, B.Cu)")
    print()

    return 0


def _place_component(pcb, comp, x, y, netlist_data, angle_override=None):
    """Place a single component on the PCB.

    Determines the correct footprint from the part name and assigns nets.
    angle_override: if not None, overrides the default angle for this part type.
    """
    ref = comp["ref"]
    part = comp["part"]
    tstamp = comp["tstamp"]

    # Determine footprint
    fp_ref = get_footprint_for_part(part)
    if fp_ref is None:
        # Skip power symbols and flags
        if ref.startswith("#"):
            return
        print(f"  WARNING: No footprint mapping for {ref} ({part})")
        return

    # Build net map: pin_number -> net_name
    net_map = {}
    for pin_num, net_name in comp["pins"].items():
        if net_name and not net_name.startswith("unconnected"):
            net_map[pin_num] = net_name

    # Determine rotation
    if angle_override is not None:
        angle = angle_override
    else:
        angle = 0
        if part == "LED_Small":
            angle = 270  # IC-adjacent LEDs: vertical, anode below (toward IC pin 4)
        elif part == "R_Small":
            angle = 270  # Vertical, pin 1 above (toward LED cathode), pin 2/GND below
        elif "74LVC1G" in part:
            angle = 90  # 90° CW: pin 4 (Q) faces right, VCC above Q
        elif part == "Conn_01x16":
            angle = 180  # Pins face left toward board edge

    pcb.place_component(
        ref=ref,
        lib_fp=fp_ref,
        x=round(x, 2),
        y=round(y, 2),
        angle=angle,
        net_map=net_map,
        tstamp=tstamp,
    )


if __name__ == "__main__":
    sys.exit(main())
