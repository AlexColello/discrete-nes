#!/usr/bin/env python3
"""
Generate KiCad PCB layout for the 8-byte discrete RAM prototype.

Places all components (ICs, LEDs, resistors, connectors) on F.Cu
in a grouped layout matching the schematic hierarchy.  DFFs at 90° and
buffers at 270° (power pins outward, signal pins facing each other).
Other DSBGA ICs at 180°.  LEDs at 90°, resistors at 270° (below LED,
pad 1 facing LED cathode).
After placement, pre-routes repetitive local connections:
  - Power vias (GND/VCC pads to inner planes)
  - IC→LED traces (output to indicator LED anode)
  - CLK fanout (horizontal F.Cu trace per byte)
  - OE fanout (horizontal F.Cu bus + vertical stubs per byte)
  - Connector signal→LED stubs

Layout:
  +------+----------------------------------+---------+-----------+-----------+-----------+
  |      | ADDR DECODER (5 vertical cols)   |ROW CTRL | BYTE 0    | BYTE 4    |           |
  |      | INV | L1 | DEC3 | DEC4 | FINAL  |  0..3   | BYTE 1    | BYTE 5    | TEST GRID |
  | CONN |     |    |      |      | (Y-    |  (Y-    | BYTE 2    | BYTE 6    |           |
  |  J1  |     |    |      |      | align) | align)  | BYTE 3    | BYTE 7    |           |
  | 24p  +----------------------------------+---------+-----------+-----------+-----------+
  |      | CTRL LOGIC  |                         | COL SEL (4 INV+24 AND) |
  +------+---+---------+                         +---+--------------------+
              |J2|J4    |                             |J3 UNUSED COL       |
              +---------+                             +--------------------+

  Each byte has 1 NAND (74LVC2G00) + 8 DFFs + 8 buffers in 9 columns.
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
IC_CELL_W = 5.0      # horizontal spacing for non-byte groups (decoder, column_select, etc.)
IC_CELL_H = 4.0      # vertical spacing for non-byte groups
BYTE_CELL_W = 3.5    # horizontal spacing for byte groups (NAND@90° DSBGA-8 crtyd 2.6mm + margin)
# BUF row Y offset within byte groups.  BUFs have no LEDs in the byte group
# (data bus LEDs are at the connector), so the constraint is IC courtyards only:
# DFF@90° bottom = 0.75mm, BUF@180° top = BUF_ROW_Y - 1.0mm.
# Minimum = 0.75 + 1.0 = 1.75 (courtyards touching).
BUF_ROW_Y = 1.75     # BUF row offset from DFF row (courtyards touching)
CTRL_CELL_W = 5.5    # horizontal spacing for control logic (wider for routing)
CTRL_CELL_H = 4.0    # vertical spacing for control logic (wider for routing)
LED_OFFSET_X = 1.5   # LED center offset from IC center (DFF@90° crtyd 1.0 + LED@90° crtyd 0.47 + 0.03 gap)
R_OFFSET = 1.86      # LED-to-R center offset (mm) — 0402 courtyards touching (0.93+0.93)

# Group layout spacing (mm)
GROUP_GAP_X = 3.0    # horizontal gap between major groups (connector, decoder, RAM)
GROUP_GAP_Y = 1.5    # vertical gap between byte rows (keeps 1mm between OE/CLK buses)
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
        elif "Column Select" in sheetpath:
            groups["column_select"].append(comp)
        elif "Row Control" in sheetpath:
            for i in range(4):
                if f"Row Control {i}" in sheetpath:
                    groups[f"row_ctrl_{i}"].append(comp)
                    break
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

    # Sort ICs: DFFs (74LVC1G79) first, then buffers (74LVC1G125),
    # then dual NANDs (74LVC2G00), then others.
    # Within each type group, sort by reference number.
    # This ensures DFFs fill row 0 and buffers fill row 1 in byte groups.
    PART_ORDER = {"74LVC1G79": 0, "74LVC1G125": 1, "74LVC2G00": 2}

    def ref_num(c):
        ref = c["ref"]
        prefix = ref.rstrip("0123456789")
        return int(ref[len(prefix):]) if ref[len(prefix):] else 0

    def ic_sort_key(c):
        return (PART_ORDER.get(c["part"], 2), ref_num(c))

    ics.sort(key=ic_sort_key)
    rs.sort(key=ref_num)
    leds.sort(key=ref_num)

    # Match ICs with their LED+R pairs via OUTPUT pin nets only.
    # Using all pin nets would cause DFFs/BUFs to steal NAND LEDs
    # (DFF CLK shares net with NAND write output, BUF OE shares net
    # with NAND read output).
    # 74LVC2G00 (dual NAND) has 2 outputs -> 2 LEDs; first match gets the IC,
    # second becomes an extra cell with ic=None (placeholder for grid layout).
    OUTPUT_PINS = {
        "74LVC2G00": ["7", "3"],   # dual NAND outputs (DSBGA-8)
        "74LVC1G11": ["5"],        # 3-input AND output (DSBGA-6)
    }
    DEFAULT_OUTPUT_PIN = ["4"]     # DSBGA-5: output on pin 4

    ic_cells = []
    used_rs = set()
    used_leds = set()

    for ic in ics:
        out_pins = OUTPUT_PINS.get(ic["part"], DEFAULT_OUTPUT_PIN)
        ic_out_nets = set(ic["pins"].get(p, "") for p in out_pins) - {""}
        is_dual = len(out_pins) > 1

        # Match LEDs on output pin nets only
        matched_pairs = []
        for led in leds:
            if led["ref"] in used_leds:
                continue
            led_nets = set(led["pins"].values())
            if ic_out_nets & led_nets:
                # Find R connected to this LED
                matched_r = None
                for r in rs:
                    if r["ref"] in used_rs:
                        continue
                    r_nets = set(r["pins"].values())
                    if led_nets & r_nets:
                        matched_r = r
                        used_rs.add(r["ref"])
                        break
                matched_pairs.append((led, matched_r))
                used_leds.add(led["ref"])
                if not is_dual:
                    break  # single-output ICs: stop after first match

        if matched_pairs:
            # First pair gets the IC
            led0, r0 = matched_pairs[0]
            ic_cells.append((ic, r0, led0))
            # Additional pairs (dual NAND second output) get ic=None placeholder
            for led_n, r_n in matched_pairs[1:]:
                ic_cells.append((None, r_n, led_n))
        else:
            ic_cells.append((ic, None, None))

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

        if ic is not None:
            placements.append((ic, x, y))
        if led:
            placements.append((led, x + LED_OFFSET_X, y))
        if r:
            placements.append((r, x + LED_OFFSET_X, y + R_OFFSET))

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

            if led:
                placements.append((led, x, y))
            if r:
                placements.append((r, x + R_OFFSET, y))

            col += 1
            if col >= max_cols:
                col = 0
                row += 1

    return placements


def layout_byte_group(comps):
    """Compute relative placements for a byte group.

    Handles NAND + 8 DFF + 8 BUF + LEDs + Rs with the standard byte layout:
    row 0 = NAND + 8 DFFs (MSB left), row 1 = spacer + 8 BUFs, NAND LEDs below.

    Args:
        comps: list of component dicts from group_components()

    Returns list of (component_dict, rel_x, rel_y).
    """
    ic_cells, standalone, others = sort_components_for_placement(comps)

    nand_cells = [c for c in ic_cells if c[0] is not None and c[0]["part"] == "74LVC2G00"]
    nand_extra = [c for c in ic_cells if c[0] is None]  # 2nd NAND LED
    dff_cells = [c for c in ic_cells if c[0] is not None and c[0]["part"] == "74LVC1G79"]
    buf_cells = [c for c in ic_cells if c[0] is not None and c[0]["part"] == "74LVC1G125"]

    # Collect both NAND LED+R pairs for manual placement
    nand_led_pairs = []
    if nand_cells:
        _, r1, led1 = nand_cells[0]
        if led1:
            nand_led_pairs.append((r1, led1))
        nand_cells[0] = (nand_cells[0][0], None, None)  # strip from cell
    for cell in nand_extra:
        _, r_n, led_n = cell
        if led_n:
            nand_led_pairs.append((r_n, led_n))

    # Spacer before BUFs so they align at col 1 (matching DFF columns)
    ic_cells_ordered = (nand_cells + list(reversed(dff_cells))
                        + [(None, None, None)] + list(reversed(buf_cells)))

    placements = compute_group_layout(ic_cells_ordered, standalone, max_cols=9,
                                      cell_w=BYTE_CELL_W, cell_h=IC_CELL_H)

    # Nudge NAND IC: +1mm right, +0.25mm down relative to bits
    # Nudge BUF row from IC_CELL_H to BUF_ROW_Y (brings BUFs closer to DFFs)
    buf_nudge = round(IC_CELL_H - BUF_ROW_Y, 2)  # amount to move up
    buf_row_y = IC_CELL_H  # original BUF row Y from compute_group_layout
    placements = [
        (comp, round(rx + 1.0, 2), round(ry + 0.25, 2))
        if comp is not None and comp.get("part") == "74LVC2G00"
        else (comp, rx, round(ry - buf_nudge, 2))
        if ry >= buf_row_y - 0.01  # BUF row and BUF LED/R below it
        else (comp, rx, ry)
        for comp, rx, ry in placements
    ]

    # Place both NAND LEDs side by side below the NAND IC
    # Reverse so pin 3 (OE) LED goes LEFT (x=0.5), pin 7 (CLK) LED RIGHT (x=2.0)
    nand_led_pairs = list(reversed(nand_led_pairs))
    nand_led_y = 2.5  # below NAND IC courtyard (DSBGA-8 bottom at ~2.22mm)
    for i, (r_comp, led_comp) in enumerate(nand_led_pairs):
        lx = round(0.5 + i * 1.5, 2)  # below NAND IC
        if led_comp:
            placements.append((led_comp, lx, nand_led_y))
        if r_comp:
            placements.append((r_comp, lx, nand_led_y + R_OFFSET))

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
VIA_SIZE = 0.6       # mm outer diameter (fits DSBGA 0.50mm pin pitch)
VIA_DRILL = 0.3      # mm drill
SIG_VIA_SIZE = 0.5   # mm signal via (minimum for PCBWay/Elecrow)
SIG_VIA_DRILL = 0.3  # mm signal via drill
POWER_TRACE_W = 0.3  # mm trace width for power stubs
SIGNAL_TRACE_W = 0.2 # mm trace width for signals
VIA_OFFSET = 0.7     # mm offset from pad center to via center
DEFAULT_CLEARANCE = 0.15  # mm netclass clearance (matches Elecrow minimum)


def _set_project_clearance(pcb_path, clearance=DEFAULT_CLEARANCE):
    """Set default netclass and design rule settings in .kicad_pro.

    KiCad reads DRC clearance and via sizes from the project file's
    net_settings and design_settings, not from the PCB file.
    """
    import json

    pro_path = os.path.splitext(pcb_path)[0] + ".kicad_pro"
    if not os.path.exists(pro_path):
        return

    with open(pro_path, "r", encoding="utf-8") as f:
        project = json.load(f)

    # Update default netclass settings
    net_settings = project.get("net_settings", {})
    classes = net_settings.get("classes", [])
    for nc in classes:
        if nc.get("name") == "Default":
            nc["clearance"] = clearance
            nc["via_diameter"] = VIA_SIZE
            nc["via_drill"] = VIA_DRILL
            break

    # Update design rules to match via sizes
    ds = project.setdefault("board", {}).setdefault("design_settings", {})
    rules = ds.setdefault("rules", {})
    rules["min_via_diameter"] = SIG_VIA_SIZE  # allow smaller signal vias
    rules["min_through_hole_diameter"] = VIA_DRILL
    ds["via_dimensions"] = [
        {"diameter": VIA_SIZE, "drill": VIA_DRILL},
        {"diameter": SIG_VIA_SIZE, "drill": SIG_VIA_DRILL},
    ]

    with open(pro_path, "w", encoding="utf-8") as f:
        json.dump(project, f, indent=2)
        f.write("\n")


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
                # KiCad uses CCW rotation (Y-down coords) (positive angle = CW in Y-down)
                abs_x = round(fp_x + px * cos_a + py * sin_a, 2)
                abs_y = round(fp_y - px * sin_a + py * cos_a, 2)
                net_to_pads[pad.net.number].append(
                    (ref, pad.number, abs_x, abs_y, pad.net.name))

    return net_to_pads


def preroute_power_vias(pcb, netlist_data):
    """Drop vias from every IC GND/VCC pad and R GND pad to inner planes.

    DFF/BUF power vias use cardinal L-escape: straight outward from the
    IC body, then a small perpendicular nudge.  This creates a channel
    between the VCC and GND vias for routing the center signal trace.

    DSBGA-5 has VCC and GND diagonally opposite.  For DFF at 90°:
      - Left column: VCC (top), D (bottom)
      - Right column: Q (top), GND (bottom)
    Escaping vertically within a column is blocked by the other pad.
    Escape outward (VCC UP, GND DOWN) then nudge sideways avoids all
    intra-IC pads:
      DFF  (90°): VCC UP + nudge LEFT,  GND DOWN + nudge RIGHT
      BUF (180°): GND UP-RIGHT, VCC DOWN-LEFT

    This only applies to 74LVC1G79 (DFF) and 74LVC1G125 (BUF) — other
    parts have different pin-to-net assignments so the safe escape
    direction differs.  All other DSBGA ICs use diagonal escape
    (radially away from center, snapped to 45°).

    LEDs/Rs: escape rightward (+X), no nudge.

    Returns (via_count, trace_count).
    """
    # Skip all DSBGA ICs — power via escapes conflict with prerouted
    # signal traces in the dense layout.  Left to autorouter.
    SKIP_PARTS = {"74LVC1G04", "74LVC1G08", "74LVC1G11",
                  "74LVC1G79", "74LVC1G125", "74LVC2G00"}

    ref_to_part = _build_ref_to_part(netlist_data)

    gnd_net = pcb.get_net_number("GND")
    vcc_net = pcb.get_net_number("VCC")
    if gnd_net is None or vcc_net is None:
        print("  WARNING: GND or VCC net not found, skipping power vias")
        return 0, 0

    vias = 0

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

        # Skip Rs — GND vias too tight in dense byte layout, autorouter handles
        if is_resistor:
            continue
        if not (is_dsbga or is_led):
            continue

        # Skip DSBGA-8 and DFF/BUF — byte area too dense for prerouted
        # power vias.  Left to autorouter.
        if "DSBGA-8" in lib_id:
            continue

        fp_angle = round(fp.position.angle or 0)
        part = ref_to_part.get(ref, "")
        if part in SKIP_PARTS:
            continue

        for pad in fp.pads:
            if not (pad.net and pad.net.name in ("GND", "VCC")):
                continue

            net_name = pad.net.name
            net_num = pad.net.number

            px, py = pad.position.X, pad.position.Y
            # KiCad uses CCW rotation (Y-down coords)
            abs_x = round(fp_x + px * cos_a + py * sin_a, 2)
            abs_y = round(fp_y - px * sin_a + py * cos_a, 2)

            via_layers = (["F.Cu", "B.Cu"] if net_name == "GND"
                          else ["F.Cu", "In2.Cu"])

            if is_dsbga:
                # Other ICs: diagonal escape away from center, 45° grid
                dx = abs_x - fp_x
                dy = abs_y - fp_y
                dist = math.sqrt(dx * dx + dy * dy)
                if dist > 0.01:
                    raw = math.degrees(math.atan2(dy, dx))
                    escape_angle = round(raw / 45) * 45
                else:
                    escape_angle = 90
                pcb.pin_to_via(
                    (abs_x, abs_y), net_num,
                    angle=escape_angle,
                    distance=VIA_OFFSET,
                    trace_width=POWER_TRACE_W,
                    via_size=VIA_SIZE, via_drill=VIA_DRILL,
                    via_layers=via_layers,
                )
            else:
                # LEDs and Rs: escape rightward
                pcb.pin_to_via(
                    (abs_x, abs_y), net_num,
                    angle=0,
                    distance=VIA_OFFSET,
                    trace_width=POWER_TRACE_W,
                    via_size=VIA_SIZE, via_drill=VIA_DRILL,
                    via_layers=via_layers,
                )
            vias += 1

    return vias, vias



def preroute_led_to_resistor(pcb, netlist_data):
    """Route LED cathode to R pad 1 on F.Cu (short straight trace).

    LED at 90° has cathode (pad 1) at y+0.485.  R at 270° below has
    pad 1 at y+R_OFFSET-0.51.  Gap ~1mm — draw a vertical F.Cu trace.
    Root connector LEDs at 180°/R at 0° get a horizontal trace instead.

    Returns trace count.
    """
    net_to_pads = _build_net_pad_index(pcb)
    ref_to_part = _build_ref_to_part(netlist_data)
    traces = 0
    processed = set()

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("D") or ref in processed:
            continue

        # Get LED cathode pad and net
        # LED pad 2 = anode (signal), pad 1 = cathode (to R)
        cathode_net = pcb.get_pad_net(ref, "1")
        cathode_pos = pcb.get_pad_position(ref, "1")
        if cathode_net is None or cathode_net == 0 or cathode_pos is None:
            continue

        # Find matching R pad on cathode net (closest)
        pads_on_net = net_to_pads.get(cathode_net, [])
        r_pos = None
        best_dist = float("inf")
        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if pad_ref.startswith("R"):
                dist = math.sqrt((px - cathode_pos[0])**2 +
                                 (py - cathode_pos[1])**2)
                if dist < 5.0 and dist < best_dist:
                    best_dist = dist
                    r_pos = (px, py)

        if r_pos is None:
            continue
        processed.add(ref)

        # Straight trace from LED cathode to R pad
        pcb.add_trace(cathode_pos, r_pos, cathode_net,
                      SIGNAL_TRACE_W, "F.Cu")
        traces += 1

    return traces


def preroute_ic_to_led(pcb, netlist_data):
    """Route IC output pin (4) to LED anode on F.Cu.

    Angle-specific routing strategies:

    ICs@90° (DFF, col_select rows 1/2):
      Pin 4 at rel (+0.50, -0.25) — right side.
      LED anode to the RIGHT.
      Route: L-trace horizontal RIGHT then vertical to LED anode.

    AND/INV@180° (decoder, control logic):
      Pin 4 (output) at rel (-0.25, -0.50) — upper-left.
      Pin 3 (GND) at rel (+0.25, -0.50) — upper-right, SAME Y as output.
      LED anode ~2.7mm to the RIGHT.
      Route: UP 0.45mm from pin 4 (clears GND pad with JLCPCB margin),
      then RIGHT to LED X, then DOWN to LED anode Y.

    Only routes ICs where LED anode is to the RIGHT of output pin.
    Skips: col_select row 0 (LED above), BUF@180°, DSBGA-8, DSBGA-6.

    Returns number of trace segments added.
    """
    net_to_pads = _build_net_pad_index(pcb)
    ref_to_part = _build_ref_to_part(netlist_data)
    traces = 0

    # Parts to skip entirely
    SKIP_PARTS = {"74LVC1G11", "74LVC2G00", "74LVC1G125"}

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue

        part = ref_to_part.get(ref, "")
        if part in SKIP_PARTS:
            continue

        fp_angle = round(fp.position.angle or 0)

        # Only handle 90° and 180° ICs
        if fp_angle not in (90, 180):
            continue

        out_pin = "4"
        ic_x, ic_y = fp.position.X, fp.position.Y

        ic_out_net = pcb.get_pad_net(ref, out_pin)
        if ic_out_net is None or ic_out_net == 0:
            continue

        # Find nearest LED anode pad on the same net
        pads_on_net = net_to_pads.get(ic_out_net, [])
        led_ref = None
        led_pad_num = None
        best_dist = float("inf")
        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if pad_ref.startswith("D"):
                dist = math.sqrt((px - ic_x)**2 + (py - ic_y)**2)
                if dist < IC_CELL_W * 1.5 and dist < best_dist:
                    best_dist = dist
                    led_ref = pad_ref
                    led_pad_num = pad_num

        if led_ref is None:
            continue

        out_pos = pcb.get_pad_position(ref, out_pin)
        led_anode_pos = pcb.get_pad_position(led_ref, led_pad_num)

        # Only route if LED anode is to the RIGHT of output pin.
        # Skips col_select row 0 (LED directly above IC, vertical path
        # would cross R pads and LED cathode).
        if led_anode_pos[0] <= out_pos[0]:
            continue

        if fp_angle == 90:
            # L-trace: horizontal RIGHT then vertical to LED anode
            mid_x = round(led_anode_pos[0], 2)
            mid_y = round(out_pos[1], 2)

            if abs(out_pos[0] - mid_x) > 0.01:
                pcb.add_trace(out_pos, (mid_x, mid_y), ic_out_net,
                               SIGNAL_TRACE_W, "F.Cu")
                traces += 1
            if abs(mid_y - led_anode_pos[1]) > 0.01:
                pcb.add_trace((mid_x, mid_y), led_anode_pos, ic_out_net,
                               SIGNAL_TRACE_W, "F.Cu")
                traces += 1

        elif fp_angle == 180:
            # 3-segment: UP 0.45mm from pin 4 (clears GND pad at same Y
            # with 0.235mm clearance for JLCPCB 0.2mm track-to-pad rule),
            # RIGHT to LED X, DOWN to LED anode
            up_y = round(out_pos[1] - 0.45, 2)
            led_x = round(led_anode_pos[0], 2)
            led_y = round(led_anode_pos[1], 2)

            # Seg 1: vertical UP from pin 4
            if abs(out_pos[1] - up_y) > 0.01:
                pcb.add_trace(out_pos, (round(out_pos[0], 2), up_y),
                               ic_out_net, SIGNAL_TRACE_W, "F.Cu")
                traces += 1
            # Seg 2: horizontal RIGHT to LED X
            if abs(out_pos[0] - led_x) > 0.01:
                pcb.add_trace((round(out_pos[0], 2), up_y), (led_x, up_y),
                               ic_out_net, SIGNAL_TRACE_W, "F.Cu")
                traces += 1
            # Seg 3: vertical DOWN to LED anode
            if abs(up_y - led_y) > 0.01:
                pcb.add_trace((led_x, up_y), led_anode_pos,
                               ic_out_net, SIGNAL_TRACE_W, "F.Cu")
                traces += 1

    return traces


def _build_ref_to_part(netlist_data):
    """Build mapping of reference designator -> part name from netlist.

    Used to filter routing functions to specific IC types (e.g., only
    route CLK fanout for 74LVC1G79 DFFs, not all ICs with shared pin 2).
    """
    return {c["ref"]: c["part"] for c in netlist_data["components"]}


def preroute_dff_to_buffer(pcb, netlist_data):
    """Route DFF Q (pin 4) to Buffer A (pin 2) via two In1.Cu vias.

    DFF @90°: Q (pin 4) at (dff_x+0.50, dff_y-0.25).
    BUF @180°: A (pin 2) at (dff_x+0.25, dff_y+1.75).

    Two vias connected by In1.Cu trace (detour right of GND via):
      Via 1: on IC→LED trace at (dff_x+0.90, dff_y-0.25)
      Via 2: 0.5mm right, 0.75mm below BUF A at (dff_x+0.75, dff_y+2.50)

    In1.Cu path (3 segments, avoids GND via drill at dff_x+0.50, dff_y+0.75):
      Seg 1: Via 1 → 45° right-down to (dff_x+1.20, dff_y+0.05)
      Seg 2: vertical down to (dff_x+1.20, dff_y+2.05)
      Seg 3: 45° left-down to Via 2

    F.Cu: Via 2 → BUF pin 2.
    Via 1 sits on existing IC→LED trace (same net, no F.Cu stub needed).

    Via size: 0.5mm / 0.3mm drill (minimum for PCBWay/Elecrow).

    Returns (via_count, trace_count).
    """
    VIA1_DX = 0.90       # Via 1 X offset from DFF center
    VIA1_DY = -0.25      # Via 1 Y offset (on IC->LED trace at Q pin Y)
    VIA2_DX = 0.75       # Via 2 X offset (0.5mm right of BUF pin 2)
    VIA2_DY = 2.50       # Via 2 Y offset (0.75mm below BUF pin 2)
    DETOUR_DX = 1.20     # Detour X offset for In1.Cu vertical segment

    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    vias = 0
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

        # Via positions (absolute)
        via1 = (round(dff_x + VIA1_DX, 2), round(dff_y + VIA1_DY, 2))
        via2 = (round(dff_x + VIA2_DX, 2), round(dff_y + VIA2_DY, 2))

        # Via 1 — sits on IC→LED trace on F.Cu (same net, no stub needed)
        pcb.add_via(via1, dff_q_net,
                     size=SIG_VIA_SIZE, drill=SIG_VIA_DRILL,
                     layers=["F.Cu", "In1.Cu"])
        vias += 1

        # In1.Cu: 3-segment detour right of GND via at (dff_x+0.50, dff_y+0.75)
        detour_x = round(dff_x + DETOUR_DX, 2)
        jog = round(DETOUR_DX - VIA1_DX, 2)  # 0.30mm — 45° jog distance
        jog2 = round(DETOUR_DX - VIA2_DX, 2)  # 0.70mm — 45° back to Via 2

        # Seg 1: 45° right-down from Via 1 to detour column
        p1 = (detour_x, round(via1[1] + jog, 2))
        # Seg 2: vertical down to point where 45° reaches Via 2
        p2 = (detour_x, round(via2[1] - jog2, 2))

        pcb.add_trace(via1, p1, dff_q_net, SIGNAL_TRACE_W, "In1.Cu")
        pcb.add_trace(p1, p2, dff_q_net, SIGNAL_TRACE_W, "In1.Cu")
        pcb.add_trace(p2, via2, dff_q_net, SIGNAL_TRACE_W, "In1.Cu")
        traces += 3

        # Via 2 — right of BUF A, below nOE
        pcb.add_via(via2, dff_q_net,
                     size=SIG_VIA_SIZE, drill=SIG_VIA_DRILL,
                     layers=["F.Cu", "In1.Cu"])
        vias += 1

        # F.Cu stub: Via 2 → 45° up-left → BUF pin 2
        if (abs(via2[0] - buf_pad2_pos[0]) > 0.01
                or abs(via2[1] - buf_pad2_pos[1]) > 0.01):
            pcb.add_trace(via2, buf_pad2_pos, dff_q_net,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return vias, traces


def preroute_clk_fanout(pcb, netlist_data):
    """Route CLK fanout for each byte group on F.Cu.

    DFF CLK is pin 2.  At 90° rotation, pin 2 is at (IC_x, IC_y+0.25)
    — center-bottom of DFF.

    Bus ABOVE DFF row at dff_y - 1.5 (outside the DFF-BUF gap).
    CLK stubs go UP from pin through DFF center, passing between
    GND via (~x-1.04) and VCC via (~x+1.04) with ample clearance.

    Only matches 74LVC1G79 (DFF) ICs.

    Returns number of trace segments added.
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    traces = 0

    CLK_BUS_Y_OFFSET = -1.5   # bus Y relative to DFF center (above DFF)

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
        ic_cx = fp.position.X
        ic_cy = fp.position.Y
        clk_groups[clk_net].append((ref, pad_pos[0], pad_pos[1], ic_cx, ic_cy))

    for net_num, members in clk_groups.items():
        if len(members) < 2:
            continue  # Not a fanout bus

        # Sort by X position (left to right)
        members.sort(key=lambda m: m[1])

        dff_y = members[0][4]  # all DFFs in a byte share the same Y
        bus_y = round(dff_y + CLK_BUS_Y_OFFSET, 2)

        for i, (ref, pin_x, pin_y, ic_cx, ic_cy) in enumerate(members):
            # Vertical UP from pin 2 to bus (through DFF center)
            pcb.add_trace((pin_x, pin_y), (pin_x, bus_y), net_num,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        # Horizontal bus connecting adjacent stubs
        for i in range(len(members) - 1):
            x1 = members[i][1]  # pin_x
            x2 = members[i + 1][1]
            pcb.add_trace((x1, bus_y), (x2, bus_y), net_num,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return traces


def preroute_oe_fanout(pcb, netlist_data):
    """Route OE fanout bus for each byte group on F.Cu.

    Buffer OE is pin 1. At 180° rotation, pin 1 (nOE) is at
    (IC_x+0.25, IC_y+0.50) — right-bottom of BUF.

    Bus BELOW BUF row at buf_y + 1.2 (clears DFF R pads: R@270° pad 2
    bottom edge at BUF_y+0.89, need +0.15 clearance +0.05 half-trace).
    OE stubs: straight DOWN from pin to bus Y.
    Horizontal bus connects adjacent stub drop points.

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
        fp_x = fp.position.X  # IC center X
        fp_y = fp.position.Y  # IC center Y
        oe_groups[oe_net].append((ref, pad_pos[0], pad_pos[1], fp_x, fp_y))

    for net_num, members in oe_groups.items():
        if len(members) < 2:
            continue  # Not a fanout bus

        # Sort by X position (left to right)
        members.sort(key=lambda m: m[1])

        # Bus Y: 1.2mm below BUF center (clears DFF R GND pads)
        bus_y = round(members[0][4] + 1.2, 2)

        # F.Cu stubs: straight DOWN from pin to bus
        for ref, pin_x, pin_y, ic_cx, buf_cy in members:
            pcb.add_trace((pin_x, pin_y), (pin_x, bus_y), net_num,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        # F.Cu horizontal bus segments between adjacent drop points
        for i in range(len(members) - 1):
            x1 = members[i][1]  # pin_x
            x2 = members[i + 1][1]
            pcb.add_trace((x1, bus_y), (x2, bus_y), net_num,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return traces


def _find_dff_buf_pairs(pcb, netlist_data):
    """Find matched DFF-BUF pairs in byte groups.

    Returns list of (dff_ref, dff_fp, buf_ref, buf_fp, data_net) tuples.
    Matching: DFF pin 4 (Q) shares net with BUF pin 2 (A), proximity < IC_CELL_H*2.
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)

    dff_fps = {}
    buf_fps = {}
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        part = ref_to_part.get(ref)
        if part == "74LVC1G79":
            dff_fps[ref] = fp
        elif part == "74LVC1G125":
            buf_fps[ref] = fp

    pairs = []
    for dff_ref, dff_fp in dff_fps.items():
        dff_q_net = pcb.get_pad_net(dff_ref, "4")
        if dff_q_net is None or dff_q_net == 0:
            continue

        dff_x = dff_fp.position.X
        dff_y = dff_fp.position.Y

        pads_on_net = net_to_pads.get(dff_q_net, [])
        best_dist = float("inf")
        best_buf_ref = None
        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if (pad_ref.startswith("U") and pad_ref != dff_ref
                    and pad_num == "2"
                    and ref_to_part.get(pad_ref) == "74LVC1G125"):
                dist = math.sqrt((px - dff_x) ** 2 + (py - dff_y) ** 2)
                if dist < IC_CELL_H * 2 and dist < best_dist:
                    best_dist = dist
                    best_buf_ref = pad_ref

        if best_buf_ref and best_buf_ref in buf_fps:
            pairs.append((dff_ref, dff_fp, best_buf_ref,
                          buf_fps[best_buf_ref], dff_q_net))

    return pairs


def preroute_dff_buf_gnd(pcb, netlist_data):
    """Connect DFF GND (pin 3) to BUF GND (pin 3) with F.Cu trace + via.

    DFF@90°: pin 3 (GND) at (ic_x+0.50, dff_y+0.25) — right-bottom.
    BUF@180°: pin 3 (GND) at (ic_x+0.25, buf_y-0.50) — right-top.

    With BUF_ROW_Y=1.75, the BUF GND is at dff_y+1.25, giving 1.0mm
    vertical and 0.25mm horizontal between the two GND pins.

    Route:
      1. Vertical DOWN from DFF GND to via at (ic_x+0.50, dff_y+0.75)
      2. Via to B.Cu GND plane (remove_unused_layers so In1.Cu is free
         for the data trace jumper)
      3. 45° diagonal DOWN-LEFT from via to (ic_x+0.25, dff_y+1.0)
      4. Vertical DOWN to BUF GND at (ic_x+0.25, dff_y+1.25)

    Returns (via_count, trace_count).
    """
    pairs = _find_dff_buf_pairs(pcb, netlist_data)
    gnd_net = pcb.get_net_number("GND")
    vias = 0
    traces = 0

    for dff_ref, dff_fp, buf_ref, buf_fp, data_net in pairs:
        dff_gnd = pcb.get_pad_position(dff_ref, "3")
        buf_gnd = pcb.get_pad_position(buf_ref, "3")
        if dff_gnd is None or buf_gnd is None:
            continue

        # Via at midpoint Y, DFF GND X
        via_x = round(dff_gnd[0], 2)
        via_y = round((dff_gnd[1] + buf_gnd[1]) / 2, 2)

        # Diagonal endpoint: align X with BUF GND
        diag_end_x = round(buf_gnd[0], 2)
        dx = via_x - diag_end_x
        diag_end_y = round(via_y + dx, 2)  # 45° diagonal

        # Segment 1: DFF GND straight down to via
        pcb.add_trace(dff_gnd, (via_x, via_y), gnd_net,
                       SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # Via — remove_unused_layers=True so the In1.Cu annular is cleared
        # for the data trace jumper that passes through this area
        pcb.add_via((via_x, via_y), gnd_net,
                     remove_unused_layers=True)
        vias += 1

        # Segment 2: 45° diagonal from via to BUF GND X
        pcb.add_trace((via_x, via_y), (diag_end_x, diag_end_y), gnd_net,
                       SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # Segment 3: vertical down to BUF GND
        if abs(diag_end_y - buf_gnd[1]) > 0.01:
            pcb.add_trace((diag_end_x, diag_end_y), buf_gnd, gnd_net,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return vias, traces


def preroute_dff_buf_data(pcb, netlist_data):
    """Connect DFF D (pin 1) to BUF Y (pin 4) — mirror of GND route.

    DFF@90°: pin 1 (D) at (ic_x-0.50, dff_y+0.25) — left-bottom.
    BUF@180°: pin 4 (Y) at (ic_x-0.25, buf_y-0.50) — left-top.

    These pins carry the data bus signal (D0-D7) and are geometrically
    mirrored from the GND pins (pin 3 on each IC, right side).

    Route (mirror of GND):
      1. Vertical DOWN from DFF D to via at (ic_x-0.50, dff_y+0.75)
      2. Via (remove_unused_layers for In1.Cu/In2.Cu clearance)
      3. 45° diagonal DOWN-RIGHT from via to (ic_x-0.25, dff_y+1.0)
      4. Vertical DOWN to BUF Y at (ic_x-0.25, dff_y+1.25)

    Returns (via_count, trace_count).
    """
    pairs = _find_dff_buf_pairs(pcb, netlist_data)
    vias = 0
    traces = 0

    for dff_ref, dff_fp, buf_ref, buf_fp, _q_net in pairs:
        dff_d = pcb.get_pad_position(dff_ref, "1")
        buf_y = pcb.get_pad_position(buf_ref, "4")
        if dff_d is None or buf_y is None:
            continue

        # Get the data bus net from DFF pin 1
        dbus_net = pcb.get_pad_net(dff_ref, "1")
        if dbus_net is None or dbus_net == 0:
            continue

        # Via at midpoint Y, DFF D X
        via_x = round(dff_d[0], 2)
        via_y = round((dff_d[1] + buf_y[1]) / 2, 2)

        # Diagonal endpoint: align X with BUF Y pad
        diag_end_x = round(buf_y[0], 2)
        dx = abs(via_x - diag_end_x)
        diag_end_y = round(via_y + dx, 2)  # 45° diagonal

        # Segment 1: DFF D straight down to via
        pcb.add_trace(dff_d, (via_x, via_y), dbus_net,
                       SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # Via
        pcb.add_via((via_x, via_y), dbus_net,
                     remove_unused_layers=True)
        vias += 1

        # Segment 2: 45° diagonal from via to BUF Y X
        pcb.add_trace((via_x, via_y), (diag_end_x, diag_end_y), dbus_net,
                       SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # Segment 3: vertical down to BUF Y
        if abs(diag_end_y - buf_y[1]) > 0.01:
            pcb.add_trace((diag_end_x, diag_end_y), buf_y, dbus_net,
                           SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return vias, traces


def preroute_r_gnd(pcb, netlist_data):
    """Connect resistor GND pads to the existing DFF-BUF GND vias on F.Cu.

    Each byte DFF LED has a series resistor whose pad 2 is on the GND net.
    R pad 1 sits directly between R pad 2 and the GND via, blocking a
    straight diagonal.  The route goes LEFT of R pad 1, then UP, then LEFT
    to the via — a Z-shape with 45-degree chamfered corners:

        R pad 2 ──LEFT──┐
                        │ (vertical, LEFT of R pad 1)
                        └──LEFT── GND via

    Geometry (relative to DFF center):
      R pad 2:          (dff_x+1.50, dff_y+2.37)
      R pad 1 left edge: dff_x+1.18 (0.32mm = R pad half-width at 270°)
      Turn X:            dff_x+0.88 (0.2mm JLCPCB clearance + 0.1mm trace half)
      GND via:           (dff_x+0.50, dff_y+0.75)

    Non-byte Rs (decoder, control, connector) get a via escape to B.Cu
    GND plane.

    Returns (via_count, trace_count).
    """
    gnd_net = pcb.get_net_number("GND")
    if gnd_net is None:
        print("  WARNING: GND net not found, skipping R GND routing")
        return 0, 0

    # Collect existing GND vias (placed by preroute_dff_buf_gnd)
    existing_gnd_vias = []
    for item in pcb.board.traceItems:
        if (type(item).__name__ == "Via"
                and hasattr(item, 'net') and item.net == gnd_net):
            existing_gnd_vias.append((item.position.X, item.position.Y))

    vias = 0
    traces = 0
    fcu_routed = 0

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        lib_id = fp.libId or ""
        if not ref.startswith("R") or "Resistor" not in lib_id:
            continue

        pad2_net = pcb.get_pad_net(ref, "2")
        pad2_pos = pcb.get_pad_position(ref, "2")
        if pad2_net != gnd_net or pad2_pos is None:
            continue

        fp_angle = round(fp.position.angle or 0)

        # Try to find a nearby DFF-BUF GND via for F.Cu Z-route
        # Expected offset: via at (-1.0, -1.62) from R pad 2
        routed_fcu = False
        if fp_angle == 270 and existing_gnd_vias:
            best_dist = float("inf")
            best_via = None
            for gvx, gvy in existing_gnd_vias:
                dx = pad2_pos[0] - gvx
                dy = pad2_pos[1] - gvy
                # Check the offset matches byte DFF layout (~1.0 right, ~1.62 below)
                if 0.5 < dx < 1.5 and 1.0 < dy < 2.2:
                    d = math.sqrt(dx * dx + dy * dy)
                    if d < best_dist:
                        best_dist = d
                        best_via = (gvx, gvy)

            if best_via:
                # Z-route: horizontal LEFT, vertical UP, horizontal LEFT
                # Turn X: R pad 1 left edge - clearance - trace_half
                # R pad 1 is 0.32mm left of R center (0.64mm horiz at 270°)
                turn_x = round(pad2_pos[0] - 0.32 - 0.2 - 0.1, 2)

                p0 = pad2_pos
                p1 = (turn_x, pad2_pos[1])      # end of horizontal
                p2 = (turn_x, best_via[1])       # end of vertical
                p3 = best_via                    # GND via

                # Seg 1: horizontal LEFT (with chamfer at turn)
                # Seg 2: vertical UP
                # Seg 3: horizontal LEFT to via
                # Use chamfered corners (0.3mm max, clamped to shorter leg)
                h1_len = abs(p0[0] - p1[0])
                v_len = abs(p1[1] - p2[1])
                h2_len = abs(p2[0] - p3[0])

                c1 = min(0.3, h1_len * 0.5, v_len * 0.5)
                c2 = min(0.3, v_len * 0.5, h2_len * 0.5)

                # Turn 1: horizontal → vertical (upper-left chamfer)
                t1_h_end = (round(turn_x + c1, 2), p0[1])
                t1_v_start = (turn_x, round(p0[1] - c1, 2))

                # Turn 2: vertical → horizontal (upper-left chamfer)
                t2_v_end = (turn_x, round(best_via[1] + c2, 2))
                t2_h_start = (round(turn_x - c2, 2), best_via[1])

                # Build segments
                pts = [p0, t1_h_end, t1_v_start, t2_v_end, t2_h_start, p3]
                for i in range(len(pts) - 1):
                    a, b = pts[i], pts[i + 1]
                    if abs(a[0] - b[0]) > 0.01 or abs(a[1] - b[1]) > 0.01:
                        pcb.add_trace(a, b, gnd_net,
                                      SIGNAL_TRACE_W, "F.Cu")
                        traces += 1

                routed_fcu = True
                fcu_routed += 1

        # Fallback: via escape to B.Cu GND plane
        if not routed_fcu:
            if fp_angle == 270:
                escape_angle = 0   # RIGHT
            elif fp_angle == 0:
                escape_angle = 90  # DOWN
            else:
                escape_angle = 0

            pcb.pin_to_via(
                pad2_pos, gnd_net,
                angle=escape_angle,
                distance=0.75,
                trace_width=POWER_TRACE_W,
                via_size=VIA_SIZE, via_drill=VIA_DRILL,
                via_layers=["F.Cu", "B.Cu"],
            )
            vias += 1
            traces += 1

    print(f"    ({fcu_routed} F.Cu Z-routes to DFF-BUF GND via, "
          f"{vias} via escapes)")
    return vias, traces


def preroute_nand_connections(pcb, netlist_data):
    """Route 74LVC2G00 dual NAND local connections within each byte group.

    Dynamically discovers which output pin connects to the CLK bus (DFF pin 2
    net) vs OE bus (BUF pin 1 net).  At 180deg, pin 7 is topmost and pin 3
    is lower; pin 7 connects to DFFs (CLK), pin 3 connects to BUFs (OE).

    CLK output (pin 7, topmost at 180deg) -- all F.Cu, 5 segments:
      Escape UP to CLK bus Y, extend bus RIGHT to leftmost DFF stub.
      LED detour: LEFT at bus Y, vertical DOWN to LED anode, RIGHT to LED.

    OE output (pin 3, lower at 180deg) -- all F.Cu, 5 segments:
      RIGHT from pin to LED X, DOWN to LED anode.
      From LED anode RIGHT to trunk at byte_x+3.0,
      DOWN to OE bus Y, RIGHT to leftmost BUF OE stub.

    Power vias: stubs LEFT from left pad column to vias at byte_x+0.15.

    Returns (via_count, trace_count).
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    vias = 0
    traces = 0

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC2G00":
            continue

        nand_x, nand_y = fp.position.X, fp.position.Y
        # Byte group origin: NAND is nudged to byte-relative (1.0, 0.25)
        byte_x = round(nand_x - 1.0, 2)

        # Dynamically determine which output connects to CLK vs OE
        clk_pin = None  # output pin connecting to DFF CLK (pin 2)
        oe_pin = None   # output pin connecting to BUF OE (pin 1)

        for out_pin in ["3", "7"]:
            net = pcb.get_pad_net(ref, out_pin)
            if not net:
                continue
            pads = net_to_pads.get(net, [])
            has_dff = any(
                pr.startswith("U") and pn == "2"
                and ref_to_part.get(pr) == "74LVC1G79"
                for pr, pn, px, py, pnet in pads
            )
            has_buf = any(
                pr.startswith("U") and pn == "1"
                and ref_to_part.get(pr) == "74LVC1G125"
                for pr, pn, px, py, pnet in pads
            )
            if has_dff:
                clk_pin = out_pin
            elif has_buf:
                oe_pin = out_pin

        if not clk_pin or not oe_pin:
            continue

        clk_pos = pcb.get_pad_position(ref, clk_pin)
        clk_net = pcb.get_pad_net(ref, clk_pin)
        oe_pos = pcb.get_pad_position(ref, oe_pin)
        oe_net = pcb.get_pad_net(ref, oe_pin)

        # --- CLK bus target ---
        pads_on_clk = net_to_pads.get(clk_net, [])
        dff_clk_pads = [
            (px, py) for pr, pn, px, py, pnet in pads_on_clk
            if pr.startswith("U") and pn == "2"
            and ref_to_part.get(pr) == "74LVC1G79"
        ]

        # --- OE bus target ---
        pads_on_oe = net_to_pads.get(oe_net, [])
        buf_oe_pads = [
            (px, py) for pr, pn, px, py, pnet in pads_on_oe
            if pr.startswith("U") and pn == "1"
            and ref_to_part.get(pr) == "74LVC1G125"
        ]

        if not dff_clk_pads or not buf_oe_pads:
            continue

        # CLK bus position
        # DFF pin 2 (CLK) at 90° is at (IC_x, IC_y+0.25) — pin_x = ic_cx
        dff_clk_pads.sort(key=lambda p: p[0])
        dff_pin2_y = dff_clk_pads[0][1]
        leftmost_dff_pin_x = dff_clk_pads[0][0]
        # CLK bus is 1.5mm ABOVE DFF center (ic_cy = pin2_y - 0.25)
        dff_cy = round(dff_pin2_y - 0.25, 2)
        clk_bus_y = round(dff_cy - 1.5, 2)
        clk_bus_end_x = round(leftmost_dff_pin_x, 2)

        # OE bus position
        # BUF pin 1 (nOE) at 180° is at (IC_x+0.25, IC_y+0.50)
        buf_oe_pads.sort(key=lambda p: p[0])
        leftmost_buf_oe_x = buf_oe_pads[0][0]
        buf_center_y = round(buf_oe_pads[0][1] + 0.25, 2)
        oe_bus_y = round(buf_center_y + 1.5, 2)

        # Find LED anodes on each output net (nearest to NAND)
        def find_led_anode(net_num):
            pads = net_to_pads.get(net_num, [])
            best = None
            best_dist = float("inf")
            for pad_ref, pad_num, px, py, pnet in pads:
                if pad_ref.startswith("D"):
                    dist = math.sqrt((px - nand_x)**2 + (py - nand_y)**2)
                    if dist < 10 and dist < best_dist:
                        best_dist = dist
                        best = (px, py)
            return best

        clk_led = find_led_anode(clk_net)
        oe_led = find_led_anode(oe_net)

        # === CLK output routing (topmost pin, escapes UP then diagonal UP-RIGHT) ===
        # Seg 1: vertical UP 0.50mm
        vert_up_y = round(clk_pos[1] - 0.50, 2)
        pcb.add_trace(clk_pos, (clk_pos[0], vert_up_y),
                      clk_net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # Seg 2: 45° UP-RIGHT to CLK bus Y
        diag_dy = abs(clk_bus_y - vert_up_y)
        diag_end_x = round(clk_pos[0] + diag_dy, 2)
        pcb.add_trace((clk_pos[0], vert_up_y), (diag_end_x, clk_bus_y),
                      clk_net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # Seg 3: CLK bus extension RIGHT to leftmost DFF stub
        pcb.add_trace((diag_end_x, clk_bus_y), (clk_bus_end_x, clk_bus_y),
                      clk_net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # CLK LED detour: RIGHT-side vertical trunk down to LED
        if clk_led:
            # Seg 4: 45° DOWN-RIGHT from T-junction (0.30mm)
            dr_len = 0.30
            trunk_x = round(diag_end_x + dr_len, 2)
            trunk_start_y = round(clk_bus_y + dr_len, 2)
            pcb.add_trace((diag_end_x, clk_bus_y), (trunk_x, trunk_start_y),
                          clk_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 5: vertical DOWN to turn point
            led_dx = trunk_x - clk_led[0]
            turn_y = round(clk_led[1] - led_dx, 2)
            pcb.add_trace((trunk_x, trunk_start_y), (trunk_x, turn_y),
                          clk_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 6: 45° DOWN-LEFT to LED anode
            pcb.add_trace((trunk_x, turn_y), clk_led,
                          clk_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        # === OE output routing (escape RIGHT, vertical trunk through LEDs, diagonal to OE bus) ===
        if oe_led:
            # Seg 1a: stub RIGHT (0.10mm) — clearance from pin 1 below
            oe_stub_end = (round(oe_pos[0] + 0.10, 2), oe_pos[1])
            pcb.add_trace(oe_pos, oe_stub_end,
                          oe_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 1b: 45° DOWN-RIGHT escape (0.60mm each axis)
            dr_end = (round(oe_stub_end[0] + 0.60, 2), round(oe_stub_end[1] + 0.60, 2))
            pcb.add_trace(oe_stub_end, dr_end,
                          oe_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 2: short vertical DOWN (0.65mm)
            vert_end = (dr_end[0], round(dr_end[1] + 0.65, 2))
            pcb.add_trace(dr_end, vert_end,
                          oe_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 3: 45° DOWN-LEFT back to pin column X
            dl_dx = round(dr_end[0] - oe_pos[0], 2)
            dl_end = (oe_pos[0], round(vert_end[1] + dl_dx, 2))
            pcb.add_trace(vert_end, dl_end,
                          oe_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 4a: vertical DOWN to LED Y (trunk between LEDs)
            pcb.add_trace(dl_end, (dl_end[0], oe_led[1]),
                          oe_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 4b: horizontal LEFT to LED anode
            pcb.add_trace((dl_end[0], oe_led[1]), oe_led,
                          oe_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 5: extend trunk DOWN past LED cathode vias (1.5mm below anode)
            trunk_x = dl_end[0]
            trunk_ext_y = round(oe_led[1] + 1.5, 2)
            pcb.add_trace((trunk_x, oe_led[1]), (trunk_x, trunk_ext_y),
                          oe_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 6: 45° DOWN-RIGHT diagonal to OE bus Y
            bus_dy = round(oe_bus_y - trunk_ext_y, 2)
            bus_diag_end = (round(trunk_x + bus_dy, 2), oe_bus_y)
            pcb.add_trace((trunk_x, trunk_ext_y), bus_diag_end,
                          oe_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

            # Seg 7: horizontal RIGHT to BUF OE bus
            # OE stubs connect at ic_cx + 1.75 (leftmost_buf_oe_x + 1.25 at 270°)
            oe_bus_target_x = round(leftmost_buf_oe_x + 1.25, 2)
            if abs(bus_diag_end[0] - oe_bus_target_x) > 0.01:
                pcb.add_trace(bus_diag_end, (oe_bus_target_x, oe_bus_y),
                              oe_net, SIGNAL_TRACE_W, "F.Cu")
                traces += 1

        # === NAND Power Vias ===
        pwr_via_x = round(byte_x + 0.15, 2)

        # Pin 8 (VCC) -> 45° diagonal to via on In2.Cu
        pin8_pos = pcb.get_pad_position(ref, "8")
        pin8_net = pcb.get_pad_net(ref, "8")
        if pin8_pos and pin8_net:
            delta_x = pwr_via_x - pin8_pos[0]
            # Original constraint: delta_y == delta_x (45° line to target X)
            escape_angle = math.degrees(math.atan2(delta_x, delta_x))
            escape_dist = abs(delta_x) * math.sqrt(2)
            pcb.pin_to_via(
                pin8_pos, pin8_net,
                angle=escape_angle, distance=escape_dist,
                trace_width=POWER_TRACE_W,
                via_size=VIA_SIZE, via_drill=VIA_DRILL,
                via_layers=["F.Cu", "In2.Cu"],
            )
            vias += 1
            traces += 1

        # Pin 4 (GND) -> horizontal to via on B.Cu
        pin4_pos = pcb.get_pad_position(ref, "4")
        pin4_net = pcb.get_pad_net(ref, "4")
        if pin4_pos and pin4_net:
            delta_x = pwr_via_x - pin4_pos[0]
            escape_angle = 0 if delta_x >= 0 else 180
            pcb.pin_to_via(
                pin4_pos, pin4_net,
                angle=escape_angle, distance=abs(delta_x),
                trace_width=POWER_TRACE_W,
                via_size=VIA_SIZE, via_drill=VIA_DRILL,
                via_layers=["F.Cu", "B.Cu"],
            )
            vias += 1
            traces += 1

    return vias, traces


def preroute_column_select(pcb, netlist_data):
    """Route INV1 output -> INV2 input in the column select group.

    Finds two 74LVC1G04 inverters where one's output (pin 4) net matches
    the other's input (pin 2) net, then routes a F.Cu U-shape below both ICs:
      T-junction on INV1's existing IC->LED trace -> DOWN ->
      horizontal RIGHT -> UP -> LEFT to INV2 pin 2.

    Returns trace count.
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    traces = 0

    # Find all 74LVC1G04 inverters and their output (pin 4) nets
    inv_outputs = {}  # ref -> (pin4_pos, pin4_net, fp)
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC1G04":
            continue

        pin4_net = pcb.get_pad_net(ref, "4")
        pin4_pos = pcb.get_pad_position(ref, "4")
        if pin4_net and pin4_pos:
            inv_outputs[ref] = (pin4_pos, pin4_net, fp)

    # Find INV1->INV2 pair: INV1 output (pin 4) net = INV2 input (pin 2) net
    done = set()
    for ref1, (pos1, net1, fp1) in inv_outputs.items():
        if ref1 in done:
            continue
        pads_on_net = net_to_pads.get(net1, [])
        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if (pad_ref.startswith("U") and pad_ref != ref1
                    and pad_num == "2"
                    and ref_to_part.get(pad_ref) == "74LVC1G04"):
                # Found INV2 (pad_ref) with input on INV1's output net
                inv2_pin2_pos = (px, py)

                # INV1 center from pin 4 (at IC_x+0.25, IC_y-0.50 at 180deg)
                inv1_cx = round(pos1[0] - 0.25, 2)
                inv1_cy = round(pos1[1] + 0.50, 2)

                # INV2 center from pin 2 (at IC_x+0.25, IC_y at 180deg)
                inv2_cx = round(px - 0.25, 2)

                # T-junction on existing INV1 IC->LED horizontal trace
                # IC->LED horizontal runs at IC_y - 0.55
                t_x = round(inv1_cx + 1.0, 2)
                t_y = round(inv1_cy - 0.55, 2)

                # U-shape detour Y: below both ICs
                u_y = round(inv1_cy + 2.0, 2)

                # Approach column: right of INV2 (clears pin 1 NC at IC_x+0.25)
                approach_x = round(inv2_cx + 0.75, 2)

                # Seg 1: T-junction DOWN to U-shape Y
                pcb.add_trace((t_x, t_y), (t_x, u_y),
                              net1, SIGNAL_TRACE_W, "F.Cu")
                traces += 1

                # Seg 2: horizontal RIGHT to approach column
                pcb.add_trace((t_x, u_y), (approach_x, u_y),
                              net1, SIGNAL_TRACE_W, "F.Cu")
                traces += 1

                # Seg 3: approach column UP to INV2 pin 2 Y
                pcb.add_trace((approach_x, u_y), (approach_x, inv2_pin2_pos[1]),
                              net1, SIGNAL_TRACE_W, "F.Cu")
                traces += 1

                # Seg 4: horizontal LEFT to INV2 pin 2
                pcb.add_trace((approach_x, inv2_pin2_pos[1]), inv2_pin2_pos,
                              net1, SIGNAL_TRACE_W, "F.Cu")
                traces += 1

                done.add(ref1)
                done.add(pad_ref)
                break

    return traces


def preroute_col_sel_vias(pcb, netlist_data):
    """Add vias for NAND COL_SEL input pins and connect with In1.Cu traces.

    The 74LVC2G00 dual NAND is at 90° CW rotation.  Pin A of each NAND gate
    (pin 1 for unit 1, pin 5 for unit 2) carries the COL_SEL signal.
    At 90°, both COL_SEL pins are in the BOTTOM pad row (sharing Y),
    with pin 1 at x=ic_cx-0.75 and pin 5 at x=ic_cx+0.25.

    Routing strategy per byte:
      Both pins: F.Cu stub DOWN 0.55mm to via (no pads below at 90°).
      In1.Cu: horizontal trace connecting the two vias, then vertical trunk.

    Returns (via_count, trace_count).
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    vias = 0
    traces = 0

    # Collect trunk points grouped by COL_SEL net
    net_trunk_pts = {}  # net_name -> [(x, y), ...]

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC2G00":
            continue

        # Get pin positions
        pin1_pos = pcb.get_pad_position(ref, "1")
        pin5_pos = pcb.get_pad_position(ref, "5")
        pin1_net = pcb.get_pad_net(ref, "1")
        if not pin1_pos or not pin5_pos or not pin1_net:
            continue

        pin1_x = round(pin1_pos[0], 2)
        pin1_y = round(pin1_pos[1], 2)
        pin5_x = round(pin5_pos[0], 2)
        pin5_y = round(pin5_pos[1], 2)
        net = pin1_net  # same net for both pins

        # Both pins share Y at 90° rotation; vias go DOWN (positive Y)
        via_offset = 0.55

        # --- Pin 5: F.Cu stub DOWN to via ---
        via5_x = pin5_x
        via5_y = round(pin5_y + via_offset, 2)

        pcb.add_trace(pin5_pos, (via5_x, via5_y),
                      net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1
        pcb.add_via((via5_x, via5_y), net,
                    VIA_SIZE, VIA_DRILL, ["F.Cu", "In1.Cu"])
        vias += 1

        # --- Pin 1: F.Cu stub DOWN to via ---
        via1_x = pin1_x
        via1_y = round(pin1_y + via_offset, 2)

        pcb.add_trace(pin1_pos, (via1_x, via1_y),
                      net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1
        pcb.add_via((via1_x, via1_y), net,
                    VIA_SIZE, VIA_DRILL, ["F.Cu", "In1.Cu"])
        vias += 1

        # --- In1.Cu: connect pin 1 via to pin 5 via ---
        # Both vias share Y, so just horizontal
        pcb.add_trace((via5_x, via5_y), (via1_x, via1_y),
                      net, SIGNAL_TRACE_W, "In1.Cu")
        traces += 1

        # Collect trunk points for inter-byte vertical connection
        # Trunk at pin5 via X — centered between LED vias on both sides
        # (left LED vias at ~ic_cx-0.5, right LED vias at ~ic_cx+1.0).
        trunk_x = via5_x
        trunk_y = via5_y  # both COL_SEL pins share Y

        if net not in net_trunk_pts:
            net_trunk_pts[net] = []
        net_trunk_pts[net].append((trunk_x, trunk_y))

    # Connect trunk points with vertical In1.Cu traces (deduplicated)
    for net, positions in net_trunk_pts.items():
        positions = sorted(set(positions), key=lambda p: p[1])
        for i in range(len(positions) - 1):
            pcb.add_trace(positions[i], positions[i + 1],
                          net, SIGNAL_TRACE_W, "In1.Cu")
            traces += 1

    return vias, traces


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
    """Preroute D* data bus with single via per bit per byte.

    At 90° DFF.D (pin 1) at (-0.50, +0.25), left side.
    At 180° BUF.Y (pin 4) at (-0.25, -0.50), left-top of BUF.

    For each data bit (D0-D7), within each byte:
      1. DFF pin 1 (D): F.Cu vertical DOWN to via
      2. BUF pin 4 (Y): F.Cu vertical UP to via
      3. Single via at pin X (ic_cx-0.50), midpoint Y between DFF and BUF
      4. In1.Cu vertical trunk at ic_cx-0.30 (+0.20mm from via) connects across bytes

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

    # For each D* net, collect DFF pin 1 and BUF pin 4 positions by byte
    for net_num, net_name in sorted(dbus_nets.items(), key=lambda x: x[1]):
        pads_on_net = net_to_pads.get(net_num, [])

        # Collect DFF pin 1 and BUF pin 4 positions
        # Each entry: (ic_center_x, pad_x, pad_y, is_buf)
        entries = []

        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if not pad_ref.startswith("U"):
                continue
            part = ref_to_part.get(pad_ref)

            if part == "74LVC1G79" and pad_num == "1":
                # DFF pin 1 at (IC_x - 0.50, IC_y + 0.25) at 90°
                ic_cx = round(px + 0.50, 2)
                entries.append((ic_cx, px, py, False))

            elif part == "74LVC1G125" and pad_num == "4":
                # BUF pin 4 at (IC_x - 0.25, IC_y - 0.50) at 180°
                ic_cx = round(px + 0.50, 2)
                entries.append((ic_cx, px, py, True))

        # Group by column using col_boundary_x
        col0 = [e for e in entries if e[0] < col_boundary_x]
        col1 = [e for e in entries if e[0] >= col_boundary_x]

        for col_entries in [col0, col1]:
            if len(col_entries) < 2:
                continue

            # Sort by Y (top to bottom)
            col_entries.sort(key=lambda e: e[2])

            # Group into DFF-BUF pairs (same IC center X within a byte)
            # Each pair shares one via
            pairs = []  # [(dff_entry, buf_entry), ...]
            unpaired_dffs = []
            unpaired_bufs = []

            dffs = [e for e in col_entries if not e[3]]
            bufs = [e for e in col_entries if e[3]]

            for dff in dffs:
                # Find matching BUF at same X (same bit column)
                matched = None
                for buf in bufs:
                    if abs(dff[0] - buf[0]) < 0.1:  # Same IC center X
                        matched = buf
                        break
                if matched:
                    bufs.remove(matched)
                    pairs.append((dff, matched))
                else:
                    unpaired_dffs.append(dff)

            # Process pairs: one via between DFF and BUF
            # Both pins at x-0.50. Via at pin X (ic_cx-0.50) for vertical traces.
            # In1.Cu trunk offset +0.20mm from via to clear DFF GND via at ic_cx-1.037.
            via_positions = []  # (via_x, via_y) for In1.Cu trunk
            for dff, buf in pairs:
                dff_cx, dff_px, dff_py, _ = dff
                buf_cx, buf_px, buf_py, _ = buf

                # Via at pin X (both DFF.D and BUF.Y are at ic_cx - 0.50)
                via_x = round(dff_px, 2)
                via_y = round((dff_py + buf_py) / 2, 2)

                # F.Cu VERTICAL from DFF pin 1 down to via
                pcb.add_trace((dff_px, dff_py), (via_x, via_y),
                              net_num, SIGNAL_TRACE_W, "F.Cu")
                traces += 1

                # F.Cu VERTICAL from BUF pin 4 up to via
                pcb.add_trace((buf_px, buf_py), (via_x, via_y),
                              net_num, SIGNAL_TRACE_W, "F.Cu")
                traces += 1

                # Single via F.Cu -> In1.Cu
                pcb.add_via((via_x, via_y), net_num,
                            DBUS_VIA_SIZE, DBUS_VIA_DRILL, ["F.Cu", "In1.Cu"])
                vias += 1

                via_positions.append((via_x, via_y))

            # In1.Cu vertical trunk connecting vias across bytes
            # Trunk offset +0.20mm from via X to maintain clearance from GND vias
            if len(via_positions) > 1:
                via_positions.sort(key=lambda v: v[1])  # Sort by Y
                trunk_x = round(via_positions[0][0] + 0.20, 2)  # ic_cx - 0.30

                for i in range(len(via_positions) - 1):
                    vx1, vy1 = via_positions[i]
                    vx2, vy2 = via_positions[i + 1]
                    # Horizontal stub from via to trunk
                    if abs(vx1 - trunk_x) > 0.01:
                        pcb.add_trace((vx1, vy1), (trunk_x, vy1),
                                      net_num, SIGNAL_TRACE_W, "In1.Cu")
                        traces += 1
                    if abs(vx2 - trunk_x) > 0.01:
                        pcb.add_trace((vx2, vy2), (trunk_x, vy2),
                                      net_num, SIGNAL_TRACE_W, "In1.Cu")
                        traces += 1
                    # Vertical trunk segment
                    pcb.add_trace((trunk_x, vy1), (trunk_x, vy2),
                                  net_num, SIGNAL_TRACE_W, "In1.Cu")
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
TEST_TEXT_SIZE = 1.0
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
        pcb.add_silkscreen_text(header, cx, cy, size=1.0, layer="F.SilkS")

    # --- Row labels ---
    for ri, (_, label, _) in enumerate(fill_rows):
        lx = origin_x + TEST_LABEL_W / 2
        ly = gy0 + ri * step_y + TEST_CELL_H / 2
        pcb.add_silkscreen_text(label, lx, ly, size=1.0, layer="F.SilkS")

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

        # Block In1.Cu zone where this row does NOT want In1.Cu fill
        if fill_layer != "In1.Cu":
            pcb.add_keepout_zone("In1.Cu", row_outline)

        # Block In2.Cu (VCC) zone where this row does NOT want In2.Cu fill
        if fill_layer != "In2.Cu":
            pcb.add_keepout_zone("In2.Cu", row_outline)

        # Block B.Cu (GND) zone where this row does NOT want B.Cu fill
        if fill_layer != "B.Cu":
            pcb.add_keepout_zone("B.Cu", row_outline)

        # Add copper pour zones for layers without full-board zones
        # (B.Cu fill comes from the full-board B.Cu GND zone,
        #  In2.Cu fill comes from the full-board VCC zone)
        if fill_layer in ("F.Cu", "In1.Cu"):
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
    fp5_path, fp6_path, fp8_path = create_dsbga_footprints(SHARED_FP_DIR)
    print(f"  Created: {os.path.basename(fp5_path)}")
    print(f"  Created: {os.path.basename(fp6_path)}")
    print(f"  Created: {os.path.basename(fp8_path)}")

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

    # Configure 4-layer stackup
    # B.Cu = GND plane, In2.Cu = VCC plane, In1.Cu = signal/jumper layer
    pcb.set_4layer_stackup()
    pcb.set_layer_type("B.Cu", "power")   # GND plane — prevent autorouter use
    pcb.set_layer_type("In1.Cu", "signal")  # Jumper layer for data bus trunks

    # Step 5: Place components
    print("\n[5/7] Placing components...")

    # Layout:
    #   Column 0: Connector (root) on the left
    #   Column 1: addr_decoder (5 vertical decode-stage columns)
    #   Column 2: row_ctrl (4 stacked, Y-aligned with addr_decoder final rank)
    #   Columns 3+: RAM bytes in 4-col x 2-row grid
    #   Below: column_select, control_logic

    # Pre-compute layouts for each group
    group_layouts = {}
    group_sizes = {}
    # Track which cell dimensions each group uses (for compute_group_size)
    group_cell_dims = {}
    extra_root_connectors = []  # J2, J3 — placed after main layout

    # Pre-compute row_ctrl stride to match byte row stride
    # Byte layout: DFF row(y=0) + BUF row(y=BUF_ROW_Y) + BUF R(y=BUF_ROW_Y+R_OFFSET)
    # byte_row_h = (BUF_ROW_Y + R_OFFSET) + IC_CELL_H (from compute_group_size)
    _byte_row_h_est = BUF_ROW_Y + R_OFFSET + IC_CELL_H
    _rc_stride = _byte_row_h_est + GROUP_GAP_Y  # 7.0mm — matches byte row stride
    _addr_dec_final_ys = None  # set during addr_decoder layout

    for name, comps in groups.items():
        # Determine max columns and cell dimensions based on group type
        is_ram = name.startswith("byte")
        is_ctrl = name in ("addr_decoder", "control_logic", "column_select") or \
                  name.startswith("row_ctrl_")
        if name == "root":
            max_cols = 3  # Connector + root LEDs
        elif is_ram:
            max_cols = 9  # NAND + 8 bits per line (DFFs row + buffers row)
        elif name == "addr_decoder":
            max_cols = 2  # INVs in top row, ANDs below (custom layout below)
        elif name.startswith("row_ctrl_"):
            max_cols = 2  # Horizontal: write + read gates side by side
        else:
            max_cols = 3

        # RAM bytes use tight spacing; control logic uses wider spacing
        if is_ctrl:
            cw, ch = CTRL_CELL_W, CTRL_CELL_H
        elif is_ram:
            cw, ch = BYTE_CELL_W, IC_CELL_H
        else:
            cw, ch = IC_CELL_W, IC_CELL_H
        group_cell_dims[name] = (cw, ch)

        ic_cells, standalone, others = sort_components_for_placement(comps)

        # Byte groups use dedicated layout function
        if is_ram:
            placements = layout_byte_group(comps)
            group_layouts[name] = placements
            cw, ch = group_cell_dims.get(name, (IC_CELL_W, IC_CELL_H))
            group_sizes[name] = compute_group_size(placements, cell_w=cw, cell_h=ch)
            continue

        # Custom addr_decoder layout: vertical columns (left-to-right decode flow)
        # Col 0: 7 INVs (address inverters)
        # Col 1: 12 L1 ANDs (4 G + 8 HA/HB)
        # Col 2: 8 DEC3 L2 ANDs (3-to-8 outputs)
        # Col 3: 16 DEC4 L2 ANDs (4-to-16 outputs)
        # Col 4: 4 Final ANDs (ROW_SEL, Y-aligned with row_ctrl blocks)
        if name == "addr_decoder":
            inv_cells = [c for c in ic_cells if c[0] is not None and c[0]["part"] == "74LVC1G04"]
            and_cells = [c for c in ic_cells if c[0] is not None and c[0]["part"] != "74LVC1G04"]
            # Split ANDs by schematic order:
            # 0-3: G (3-to-8 L1), 4-11: DEC3 (3-to-8 L2),
            # 12-19: HA+HB (4-to-16 L1), 20-35: DEC4 (4-to-16 L2), 36-39: final
            g_cells    = and_cells[0:4]
            dec3_cells = and_cells[4:12]
            hahb_cells = and_cells[12:20]
            dec4_cells = and_cells[20:36]
            final_cells = and_cells[36:40]

            cell_h = CTRL_CELL_H   # 4.0mm vertical spacing within columns
            col_sp = 7.5           # horizontal spacing between decode-stage columns

            # DEC4 column (tallest, 16 cells) determines total height
            dec4_span = (len(dec4_cells) - 1) * cell_h  # 60mm
            total_h = dec4_span

            # Final ANDs at rc_stride spacing, centered vertically in total height
            final_span = 3 * _rc_stride
            final_start = round((total_h - final_span) / 2, 2)
            _addr_dec_final_ys = [round(final_start + i * _rc_stride, 2)
                                  for i in range(4)]

            def _place_col(cells, col_x, total_h, cell_h, placements, ys=None):
                n = len(cells)
                if ys is not None:
                    for i, (ic, r, led) in enumerate(cells):
                        x, y = col_x, ys[i]
                        if ic is not None:
                            placements.append((ic, x, y))
                        if led:
                            placements.append((led, x + LED_OFFSET_X, y))
                        if r:
                            placements.append((r, x + LED_OFFSET_X, y + R_OFFSET))
                else:
                    span = (n - 1) * cell_h
                    start = round((total_h - span) / 2, 2)
                    for i, (ic, r, led) in enumerate(cells):
                        x = col_x
                        y = round(start + i * cell_h, 2)
                        if ic is not None:
                            placements.append((ic, x, y))
                        if led:
                            placements.append((led, x + LED_OFFSET_X, y))
                        if r:
                            placements.append((r, x + LED_OFFSET_X, y + R_OFFSET))

            placements = []
            _place_col(inv_cells,            0 * col_sp, total_h, cell_h, placements)
            _place_col(g_cells + hahb_cells, 1 * col_sp, total_h, cell_h, placements)
            _place_col(dec3_cells,           2 * col_sp, total_h, cell_h, placements)
            _place_col(dec4_cells,           3 * col_sp, total_h, cell_h, placements)
            _place_col(final_cells,          4 * col_sp, total_h, cell_h, placements,
                       ys=_addr_dec_final_ys)

            group_cell_dims[name] = (col_sp, cell_h)

        # Custom column_select layout: horizontal rows, bottom-to-top decode
        # Row 0 LEDs (top):    16 LEDs above level-2 ANDs (output indicators)
        # Row 0 (y=led_row_h): 16 Level-2 ANDs (COL_SEL_0-15 outputs)
        # Row 1:                8 Level-1 ANDs (GA0-3, GB0-3 intermediates), inline LEDs
        # Row 2:                4 INVs (address inverters), inline LEDs
        elif name == "column_select":
            inv_cells = [c for c in ic_cells if c[0] is not None and c[0]["part"] == "74LVC1G04"]
            and_cells = [c for c in ic_cells if c[0] is not None and c[0]["part"] != "74LVC1G04"]
            level1_ands = and_cells[:8]   # GA0-3, GB0-3
            level2_ands = and_cells[8:]   # COL_SEL_0-15

            col_w = IC_CELL_W     # 5.0mm horizontal spacing
            row_h = CTRL_CELL_H   # 4.0mm vertical spacing between IC rows
            led_row_h = 4.5       # space for LED+R above level-2 ANDs (R courtyard bottom at 2.93, IC top at 3.57)
            top_count = len(level2_ands)  # 16

            placements = []

            # LED row (top, y=0): 16 output indicator LEDs above level-2 ANDs
            # Row 0 (y=led_row_h): 16 level-2 ANDs
            for i, (ic, r, led) in enumerate(level2_ands):
                x = round(i * col_w, 2)
                ic_y = led_row_h
                if ic is not None:
                    placements.append((ic, x, ic_y))
                if led:
                    placements.append((led, x, 0))
                if r:
                    placements.append((r, x, R_OFFSET))

            # Row 1 (y=led_row_h + row_h): 8 level-1 ANDs, centered, inline LEDs
            l1_offset = round((top_count - len(level1_ands)) * col_w / 2, 2)
            for i, (ic, r, led) in enumerate(level1_ands):
                x = round(l1_offset + i * col_w, 2)
                y = round(led_row_h + row_h, 2)
                if ic is not None:
                    placements.append((ic, x, y))
                if led:
                    placements.append((led, x + LED_OFFSET_X, y))
                if r:
                    placements.append((r, x + LED_OFFSET_X, y + R_OFFSET))

            # Row 2 (y=led_row_h + 2*row_h): 4 INVs, centered, inline LEDs
            inv_offset = round((top_count - len(inv_cells)) * col_w / 2, 2)
            for i, (ic, r, led) in enumerate(inv_cells):
                x = round(inv_offset + i * col_w, 2)
                y = round(led_row_h + 2 * row_h, 2)
                if ic is not None:
                    placements.append((ic, x, y))
                if led:
                    placements.append((led, x + LED_OFFSET_X, y))
                if r:
                    placements.append((r, x + LED_OFFSET_X, y + R_OFFSET))

            # Override cell dims for group size computation
            group_cell_dims[name] = (col_w, row_h)

        else:
            placements = compute_group_layout(ic_cells, standalone, max_cols,
                                              cell_w=cw, cell_h=ch)

        # Add connector and other non-IC components
        if others:
            if name == "root":
                # Root group: connector on the left, bus LEDs aligned to
                # their matching connector pin Y positions.
                conn_x = 0.0
                led_x = 7.0   # LED offset right of connector (closer)
                r_x = led_x + R_OFFSET  # R to the right of LED on F.Cu

                # Find J1 (main connector) and store extras (J2, J3)
                j1 = None
                for comp in others:
                    if comp["ref"] == "J1":
                        j1 = comp
                    else:
                        extra_root_connectors.append(comp)
                if j1 is None:
                    print("  WARNING: J1 not found in root group")
                    continue
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
    # Layout: Connector | addr_decoder(5 cols) | row_ctrl(x4) | RAM | layer_test
    #         Below: column_select (under RAM), control_logic (below addr_dec)
    total_placed = 0

    root_w, root_h = group_sizes.get("root", (0, 0))
    dec_w, dec_h = group_sizes.get("addr_decoder", (0, 0))
    ctrl_w, ctrl_h = group_sizes.get("control_logic", (0, 0))
    colsel_w, colsel_h = group_sizes.get("column_select", (0, 0))

    # Row control groups — compute max width for column alignment
    rc_names = [f"row_ctrl_{i}" for i in range(4)]
    rc_sizes = [group_sizes.get(n, (0, 0)) for n in rc_names]
    rc_w = max((s[0] for s in rc_sizes), default=0)
    rc_h_each = max((s[1] for s in rc_sizes), default=0)

    byte_col0 = ["byte_0", "byte_1", "byte_2", "byte_3"]
    byte_col1 = ["byte_4", "byte_5", "byte_6", "byte_7"]
    all_bytes = byte_col0 + byte_col1

    # Compute byte grid dimensions
    byte_col_w = max((group_sizes.get(b, (0, 0))[0] for b in all_bytes), default=0)
    byte_row_h = max((group_sizes.get(b, (0, 0))[1] for b in all_bytes), default=0)

    byte_center_span_x = 0
    for b in all_bytes:
        layout = group_layouts.get(b, [])
        if layout:
            xs = [x for _, x, _ in layout]
            byte_center_span_x = max(byte_center_span_x, max(xs) - min(xs))

    ram_total_h = 4 * byte_row_h + 3 * GROUP_GAP_Y
    # Total RAM width (both byte columns)
    ram_total_w = 2 * (byte_center_span_x + 0.5 + BYTE_COL_GAP + 0.75) - BYTE_COL_GAP

    # Col 1: addr_decoder (vertical columns, full height)
    col1_x = PLACEMENT_ORIGIN + root_w + GROUP_GAP_X * 3  # extra spacing between connector and logic
    col1_y = PLACEMENT_ORIGIN
    dec_abs_y = col1_y  # addr_decoder at top of col 1

    # Col 2: row_ctrl, Y-aligned with addr_decoder final ANDs
    col2_x = col1_x + dec_w + GROUP_GAP_X
    col2_y = dec_abs_y  # same Y start

    # Col 3: RAM bytes (2×4 grid, vertically centered with addr_decoder)
    ram_x = col2_x + rc_w + GROUP_GAP_X
    ram_y = round(dec_abs_y + (dec_h - ram_total_h) / 2, 2)

    # Control logic below addr_decoder
    ctrl_abs_x = col1_x
    ctrl_abs_y = round(dec_abs_y + dec_h + GROUP_GAP_Y * 3, 2)

    # Compute total board content height
    total_content_h = max(dec_h + GROUP_GAP_Y * 3 + ctrl_h,
                          ram_total_h + GROUP_GAP_Y * 3 + 20.0 + colsel_h)

    # Col 0: connector centered with the full left edge of the board
    col0_x = PLACEMENT_ORIGIN
    col0_y = col1_y + max(0, (total_content_h - root_h) / 2)

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
    conn_pin_names = {
        1: "GND",
        2: "A7", 3: "A8", 4: "A9", 5: "A10",
        6: "D0", 7: "D1", 8: "D2", 9: "D3",
        10: "D4", 11: "D5", 12: "D6", 13: "D7",
        14: "nCE", 15: "nWE", 16: "nOE",
        17: "A0", 18: "A1", 19: "A2", 20: "A3",
        21: "A4", 22: "A5", 23: "A6",
        24: "VCC",
    }
    label_x = round(col0_x - 3.0, 2)
    n_conn_pins = max(conn_pin_names.keys())
    for pin_num, pin_name in conn_pin_names.items():
        label_y = round(col0_y + (n_conn_pins - 1 - pin_num) * CONN_PIN_PITCH, 2)
        pcb.add_silkscreen_text(pin_name, label_x, label_y, size=1.0)

    # Place addr_decoder (column 1, vertical decode-stage columns)
    if "addr_decoder" in group_layouts:
        for comp, rel_x, rel_y in group_layouts["addr_decoder"]:
            _place_component(pcb, comp, col1_x + rel_x, dec_abs_y + rel_y, netlist_data)
            total_placed += 1

    # Place row_ctrl groups (column 2, Y-aligned with addr_decoder final ANDs)
    for rc_i in range(4):
        rc_name = f"row_ctrl_{rc_i}"
        if rc_name not in group_layouts:
            continue
        rc_abs_y = col2_y + _addr_dec_final_ys[rc_i]
        for comp, rel_x, rel_y in group_layouts[rc_name]:
            _place_component(pcb, comp, col2_x + rel_x, rc_abs_y + rel_y, netlist_data)
            total_placed += 1

    # Place control_logic (below addr_decoder area)
    if "control_logic" in group_layouts:
        for comp, rel_x, rel_y in group_layouts["control_logic"]:
            _place_component(pcb, comp, ctrl_abs_x + rel_x, ctrl_abs_y + rel_y, netlist_data)
            total_placed += 1

    # Place RAM bytes: column-major (down first, then right)
    byte_bounds = {}
    for col_idx, byte_col in enumerate([byte_col0, byte_col1]):
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
    # Exact placement-grid cells: each cell = (col_stride × row_stride)
    row_stride = byte_row_h + GROUP_GAP_Y
    col_stride = byte_center_span_x + 0.5 + BYTE_COL_GAP + 0.75
    bx0 = ram_x  # column 0 origin
    gap_x = col_stride - byte_center_span_x

    grid_x1 = round(bx0 - gap_x / 2, 2)
    grid_y1 = round(ram_y - GROUP_GAP_Y / 2 - IC_CELL_H / 2, 2)
    grid_x2 = round(grid_x1 + 2 * col_stride, 2)
    grid_y2 = round(grid_y1 + 4 * row_stride, 2)
    grid_w = round(grid_x2 - grid_x1, 2)
    grid_h = round(grid_y2 - grid_y1, 2)

    pcb.add_silkscreen_rect(grid_x1, grid_y1, grid_w, grid_h)

    # Vertical divider
    div_x = round(grid_x1 + col_stride, 2)
    pcb.add_silkscreen_line(div_x, grid_y1, div_x, grid_y2)

    # Horizontal dividers
    for k in range(1, 4):
        div_y = round(grid_y1 + k * row_stride, 2)
        pcb.add_silkscreen_line(grid_x1, div_y, grid_x2, div_y)

    # Address labels beside each cell
    for byte_idx in range(8):
        col_idx = byte_idx // 4
        row_idx = byte_idx % 4
        label = f"0x{byte_idx}"
        label_y = round(grid_y1 + row_idx * row_stride + row_stride / 2, 2)
        if col_idx == 0:
            label_x = round(grid_x1 - 1.5, 2)
        else:
            label_x = round(grid_x2 + 1.5, 2)
        pcb.add_silkscreen_text(label, label_x, label_y, size=1.0)

    print(f"  Silkscreen: unified 2x4 grid with address labels")

    # Place column_select below RAM block, centered horizontally under it
    # Pre-compute test grid position (depends only on ram_x/ram_total_w)
    test_x = ram_x + ram_total_w + GROUP_GAP_X + 3.0
    test_y = ram_y
    ram_center_x = ram_x + ram_total_w / 2
    colsel_x = round(ram_center_x - colsel_w / 2, 2)
    colsel_y = round(ram_y + ram_total_h + GROUP_GAP_Y * 3 + 20.0, 2)
    if "column_select" in group_layouts:
        for comp, rel_x, rel_y in group_layouts["column_select"]:
            # Column select ICs at 90° for bottom-to-top signal flow
            override = 90 if comp["ref"].startswith("U") else None
            _place_component(pcb, comp, colsel_x + rel_x, colsel_y + rel_y,
                             netlist_data, angle_override=override)
            total_placed += 1

    # Place extra connectors (J2 DEC3 unused, J3 COL_SEL unused, J4 DEC4 unused)
    # At angle 0 on B.Cu, pin 1 is at origin and pins extend downward (+Y)
    # Sort by ref to ensure deterministic placement
    extra_root_connectors.sort(key=lambda c: c["ref"])
    for comp in extra_root_connectors:
        ref = comp["ref"]
        n_pins = int(comp["part"].replace("Conn_01x", ""))
        pin_span = (n_pins - 1) * CONN_PIN_PITCH
        if ref == "J2":
            # DEC3 unused header above RAM, horizontal (90°), right of DEC4
            # DEC4 (J4) is 16-pin at ram_x; DEC3 starts after DEC4's span + gap
            dec4_span = 15 * CONN_PIN_PITCH  # 16-pin connector span
            j2_x = round(ram_x + dec4_span + 5.0, 2)
            j2_y = round(ram_y - GROUP_GAP_Y * 3 - 9.0, 2)  # same Y as DEC4
            _place_component(pcb, comp, j2_x, j2_y, netlist_data,
                             angle_override=90)
            total_placed += 1
            pcb.add_silkscreen_text("DEC3", round(j2_x + pin_span / 2, 2),
                                    round(j2_y - 3.0, 2), size=1.0)
        elif ref == "J3":
            # Unused column header below test grid, horizontal (90°)
            test_grid_h_est = TEST_TITLE_H + TEST_HEADER_H + 6 * (TEST_CELL_H + TEST_CELL_GAP)
            j3_x = round(test_x, 2)
            j3_y = round(test_y + test_grid_h_est + GROUP_GAP_Y * 3 + 3.0, 2)
            _place_component(pcb, comp, j3_x, j3_y, netlist_data,
                             angle_override=90)
            total_placed += 1
            pcb.add_silkscreen_text("COL_SEL", round(j3_x + pin_span / 2, 2),
                                    round(j3_y - 3.0, 2), size=1.0)
        elif ref == "J4":
            # DEC4 unused header above RAM, horizontal (90°), right of J2
            j4_x = round(ram_x, 2)
            j4_y = round(ram_y - GROUP_GAP_Y * 3 - 9.0, 2)
            _place_component(pcb, comp, j4_x, j4_y, netlist_data,
                             angle_override=90)
            total_placed += 1
            pcb.add_silkscreen_text("DEC4", round(j4_x + pin_span / 2, 2),
                                    round(j4_y - 3.0, 2), size=1.0)
        else:
            _place_component(pcb, comp, round(colsel_x + 20, 2),
                             round(colsel_y + colsel_h + GROUP_GAP_Y * 5, 2),
                             netlist_data, angle_override=0)
            total_placed += 1

    print(f"  Total components placed: {total_placed}")

    # Step 6: Pre-route local connections
    print("\n[6/7] Pre-routing local connections...")
    pcb.build_ref_index()

    pwr_vias, pwr_traces = preroute_power_vias(pcb, netlist_data)
    print(f"  Power vias: {pwr_vias} vias, {pwr_traces} stub traces")

    ic_led_traces = preroute_ic_to_led(pcb, netlist_data)
    print(f"  IC->LED: {ic_led_traces} trace segments")

    led_r_traces = preroute_led_to_resistor(pcb, netlist_data)
    print(f"  LED->R: {led_r_traces} traces")

    clk_traces = preroute_clk_fanout(pcb, netlist_data)
    print(f"  CLK fanout: {clk_traces} trace segments")

    oe_traces = preroute_oe_fanout(pcb, netlist_data)
    print(f"  OE fanout: {oe_traces} trace segments")

    # NAND connections skipped — OE escape path offsets tuned for IC_CELL_H=3.5.
    # With IC_CELL_H=3.0 + LED Y offset, paths cross COL_SEL vias and each other.
    # Needs full rework for new geometry. Let autorouter handle.
    nand_vias, nand_traces = 0, 0
    print(f"  NAND local: skipped (autorouter — needs rework for IC_CELL_H=3.0)")

    colsel_traces = preroute_column_select(pcb, netlist_data)
    print(f"  Column select: {colsel_traces} traces")

    # COL_SEL vias skipped — DSBGA-8 pin 5 moved from (cx+0.25) to (cx+0.75)
    # at 90°, via now collides with NAND LED cathode vias.
    cs_vias, cs_traces = 0, 0
    print(f"  COL_SEL vias: skipped (DSBGA-8 pin 5 collision)")

    # Connect DFF-BUF GND pins with F.Cu trace + via to B.Cu GND plane
    dff_buf_gnd_vias, dff_buf_gnd_traces = preroute_dff_buf_gnd(pcb, netlist_data)
    print(f"  DFF-BUF GND: {dff_buf_gnd_vias} vias, {dff_buf_gnd_traces} traces")

    # Connect DFF Q to BUF A via In1.Cu jumper (mirrored from GND trace)
    dff_buf_data_vias, dff_buf_data_traces = preroute_dff_buf_data(pcb, netlist_data)
    print(f"  DFF-BUF data: {dff_buf_data_vias} vias, {dff_buf_data_traces} traces")

    # Route DFF Q (pin 4) to BUF A (pin 2) via In1.Cu vias
    dff_buf_q_vias, dff_buf_q_traces = preroute_dff_to_buffer(pcb, netlist_data)
    print(f"  DFF-BUF Q->A: {dff_buf_q_vias} vias, {dff_buf_q_traces} traces")

    # Connect R GND pads to B.Cu GND plane via local vias
    r_gnd_vias, r_gnd_traces = preroute_r_gnd(pcb, netlist_data)
    print(f"  R GND vias: {r_gnd_vias} vias, {r_gnd_traces} traces")

    conn_traces = preroute_connector_leds(pcb, netlist_data)
    print(f"  Connector->LED + fanout stubs: {conn_traces} trace segments")

    # Data bus prerouting skipped — BUF@180° pin layout differs from
    # original geometry assumptions.  Left to autorouter.
    dbus_vias, dbus_traces = 0, 0
    print(f"  D* data bus: skipped (GND pad collision at ic_cx-0.50)")

    total_vias = (pwr_vias + dbus_vias + cs_vias + nand_vias
                  + dff_buf_gnd_vias + dff_buf_data_vias + dff_buf_q_vias
                  + r_gnd_vias)
    total_traces = (pwr_traces + ic_led_traces + led_r_traces + clk_traces
                    + oe_traces + nand_traces + dff_buf_gnd_traces
                    + dff_buf_data_traces + dff_buf_q_traces + r_gnd_traces
                    + colsel_traces + cs_traces + conn_traces + dbus_traces)
    print(f"  Total pre-routed: {total_vias} vias + {total_traces} traces")

    # Layer visibility test grid (for clear PCB) — right of RAM
    # (test_x, test_y already computed above for column_select positioning)
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
                # KiCad uses CCW rotation (Y-down coords) (positive angle = CW in Y-down)
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
                    abs_x = fp_x + pt.X * cos_a + pt.Y * sin_a
                    abs_y = fp_y - pt.X * sin_a + pt.Y * cos_a
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
    pcb.add_zone("VCC", "In2.Cu", outline, clearance=0.3)
    pcb.add_zone("GND", "B.Cu", outline, clearance=0.3, pad_connection="yes")
    print("  Added VCC zone on In2.Cu")
    print("  Added GND zone on B.Cu (GND plane)")

    # Board info text block — left-justified, bottom-right corner
    info_margin = 4.0  # mm inset from board edge (clears silk_edge_clearance)
    # Estimate text width: longest line ~38 chars at 1.0mm font ≈ 30mm
    info_text_w = 30.0
    info_x = round(origin_x + board_w - info_margin - info_text_w, 2)
    info_y = round(origin_y + board_h - info_margin, 2)
    info_lines = [
        "Discrete NES - RAM Prototype",
        "8 bytes (11-bit address, 8-bit data)",
        "v3.0  2026-03-10  2K-depth decoders",
    ]
    line_spacing = 1.6  # mm between lines
    for i, line in enumerate(info_lines):
        ly = round(info_y - (len(info_lines) - 1 - i) * line_spacing, 2)
        pcb.add_silkscreen_text(line, info_x, ly, size=1.0, justify="left")
    print(f"  Board info text at ({info_x}, {info_y})")

    # Save PCB (hide all footprint text to avoid silk_overlap/silk_over_copper)
    pcb_path = os.path.join(BOARD_DIR, "ram.kicad_pcb")
    pcb.save(pcb_path, hide_text=True, fix_led_silk=True)
    _set_project_clearance(pcb_path)
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
    print(f"  Layers: 4 (F.Cu, In1.Cu=jumper, In2.Cu=VCC, B.Cu=GND)")
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

    # Determine layer (independent of angle)
    layer = "F.Cu"
    if part.startswith("Conn_01x"):
        layer = "B.Cu"  # Soldered on back side

    # Determine rotation
    if angle_override is not None:
        angle = angle_override
    else:
        angle = 0
        if part == "LED_Small":
            angle = 90   # Vertical, anode (pad 2) above at y-0.55, cathode below
        elif part == "R_Small":
            angle = 270  # Vertical, pad 1 above (toward LED cathode), pad 2/GND below
        elif part == "74LVC1G79":
            angle = 90   # DFF: VCC/GND on top, signal pins D/CLK/Q on bottom
        elif part == "74LVC1G125":
            angle = 180  # Buffer: GND up-right, VCC down-left, signal pins right
        elif part == "74LVC2G00":
            angle = 90   # Dual NAND: COL_SEL pins (1,5) down (+Y), VCC/GND up (-Y)
        elif "74LVC" in part:
            angle = 180  # Other logic (INV, AND, NAND) unchanged
        elif part.startswith("Conn_01x"):
            angle = 180  # Pins face left toward board edge

    pcb.place_component(
        ref=ref,
        lib_fp=fp_ref,
        x=round(x, 2),
        y=round(y, 2),
        angle=angle,
        layer=layer,
        net_map=net_map,
        tstamp=tstamp,
    )

    # Connector on B.Cu: keep silkscreen on F.SilkS (visible from front)
    if part.startswith("Conn_01x") and layer == "B.Cu":
        fp = pcb.board.footprints[-1]  # just placed
        for gi in fp.graphicItems:
            if hasattr(gi, 'layer') and gi.layer == "B.SilkS":
                gi.layer = "F.SilkS"
            if hasattr(gi, 'layer') and gi.layer == "B.Fab":
                gi.layer = "F.Fab"
            if hasattr(gi, 'layer') and gi.layer == "B.CrtYd":
                gi.layer = "F.CrtYd"


if __name__ == "__main__":
    sys.exit(main())
