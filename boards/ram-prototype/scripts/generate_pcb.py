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
from kicad_gen.power_footprints import (
    create_tps546d24a_footprint, create_smd_power_connector_footprint,
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
BYTE_CELL_W = 3.25   # horizontal spacing for byte DFF/BUF cells
BYTE_CELL_H = 2.0    # vertical spacing for byte groups (1.7mm inter-row courtyard gap)
NAND_EXTRA_X = 0.25  # extra X gap after NAND column (DSBGA-8 courtyard 2.6mm)
# BUF row Y offset within byte groups.  BUFs have no LEDs in the byte group
# (data bus LEDs are at the connector), so the constraint is IC courtyards only:
# DFF@90° bottom = 0.75mm, BUF@180° top = BUF_ROW_Y - 1.0mm.
# Minimum = 0.75 + 1.0 = 1.75 (courtyards touching).
BUF_ROW_Y = 1.75     # BUF row offset from DFF row (courtyards touching)
CTRL_CELL_W = 5.5    # horizontal spacing for control logic (wider for routing)
CTRL_CELL_H = 4.0    # vertical spacing for control logic (wider for routing)
LED_OFFSET_X = 1.5   # LED center offset from IC center (DFF@90° crtyd 1.0 + LED@90° crtyd 0.47 + 0.03 gap)
R_OFFSET = 1.86      # LED-to-R center offset (mm) — 0402 courtyards touching (0.93+0.93)
R_HORIZ_OFFSET = 1.3 # horizontal R offset from LED center (side-by-side, clearance for 3-seg trace between)

# Group layout spacing (mm)
GROUP_GAP_X = 3.0    # horizontal gap between major groups (connector, decoder, RAM)
GROUP_GAP_Y = 0.75   # vertical gap between byte rows (enable buses span 5.2mm, need clearance)
CTRL_GROUP_GAP_X = 4.0  # horizontal gap between control logic groups
BYTE_COL_GAP = 1.0   # horizontal gap between the two byte columns (physical gap)
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
        elif "Power Supply" in sheetpath:
            groups["power_supply"].append(comp)
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
                         cell_w=None, cell_h=None, r_beside_led=True):
    """Compute relative positions for components within a group.

    Returns list of (component, rel_x, rel_y) for all components.
    r_beside_led=True:  IC → LED → R (side by side, non-byte groups)
    r_beside_led=False: IC → LED, R below LED (stacked, byte groups)
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
            if r_beside_led:
                r_tagged = dict(r, angle_override=90)  # pad 1 at bottom, near LED cathode
                placements.append((r_tagged, x + LED_OFFSET_X + R_HORIZ_OFFSET, y))
            else:
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
    row 0 = NAND + 8 DFFs (MSB left), row 1 = spacer + 8 BUFs, NAND LEDs side by side below.

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
                                      cell_w=BYTE_CELL_W, cell_h=BYTE_CELL_H,
                                      r_beside_led=False)

    # Nudge NAND IC: +0.5mm right, +0.25mm down relative to bits
    # (moved left from +1.0 to open routing corridor to DFF column)
    # Shift all non-NAND columns right by NAND_EXTRA_X (DSBGA-8 needs more space)
    # Nudge BUF ICs from grid row Y (BYTE_CELL_H) to BUF_ROW_Y
    # Identify BUFs by part type — Y threshold is fragile when BYTE_CELL_H < R_OFFSET
    NAND_X_NUDGE = 0.5  # NAND center X offset from byte group origin
    buf_nudge = round(BYTE_CELL_H - BUF_ROW_Y, 2)
    placements = [
        (comp, round(rx + NAND_X_NUDGE, 2), round(ry - 0.25, 2))
        if comp is not None and comp.get("part") == "74LVC2G00"
        else (comp, round(rx + NAND_EXTRA_X, 2), round(ry - buf_nudge, 2))
        if comp is not None and comp.get("part") == "74LVC1G125"
        else (comp, round(rx + NAND_EXTRA_X, 2), ry)
        for comp, rx, ry in placements
    ]

    # Place NAND LEDs side by side (same Y), centered under NAND IC.
    # NAND IC courtyard bottom = -0.25 + 1.3 = 1.05mm.
    # R/LED 0402@0/180 courtyard: ±0.93mm X, ±0.475mm Y.
    # LEDs in one row below NAND, Rs in a row below LEDs.
    # Left pair mirrored so R GND pads (pad 2) face inward toward each other.
    nand_led_pairs = list(reversed(nand_led_pairs))
    nand_center_x = NAND_X_NUDGE   # 0.5 — match NAND IC X offset
    led_spacing_x = 1.86            # 0402 courtyard touching (0.93+0.93)
    nand_led_y = 1.55               # LED row Y — top (1.075) clears NAND bottom (1.05)
    nand_r_y = 2.55                 # R row Y — top (2.075) clears LED bottom (2.025)
    for i, (r_comp, led_comp) in enumerate(nand_led_pairs):
        col_x = round(nand_center_x + (i - 0.5) * led_spacing_x, 2)
        # i=0 (left): R@0°/LED@180° — GND(pad2) faces RIGHT (inward)
        # i=1 (right): R@180°/LED@0° — GND(pad2) faces LEFT (inward)
        r_angle = 0 if i == 0 else 180
        led_angle = 180 if i == 0 else 0
        if r_comp:
            r_tagged = dict(r_comp, angle_override=r_angle)
            placements.append((r_tagged, col_x, nand_r_y))
        if led_comp:
            led_tagged = dict(led_comp, angle_override=led_angle)
            placements.append((led_tagged, col_x, nand_led_y))

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
VIA_SIZE = 0.5       # mm outer diameter (minimum for PCBWay/Elecrow, 0.1mm annular)
VIA_DRILL = 0.3      # mm drill
SIG_VIA_SIZE = VIA_SIZE    # unified — all vias use same size
SIG_VIA_DRILL = VIA_DRILL  # unified
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
    rules["min_via_diameter"] = VIA_SIZE
    rules["min_through_hole_diameter"] = VIA_DRILL
    ds["via_dimensions"] = [
        {"diameter": VIA_SIZE, "drill": VIA_DRILL},
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
    """Drop vias from every IC GND/VCC pad to inner planes.

    All DSBGA ICs use diagonal escape (radially away from IC center,
    snapped to 45°).  DSBGA-5 and DSBGA-8 both have VCC and GND on
    diagonally opposite balls, so the escape directions naturally
    diverge without conflict.

    LEDs: escape rightward (+X).

    Returns (via_count, trace_count).
    """
    # DFF/BUF/NAND already have dedicated power routing functions
    # (preroute_dff_buf_gnd, preroute_dff_buf_vcc, preroute_nand_connections)
    SKIP_PARTS = {"74LVC1G79", "74LVC1G125", "74LVC2G00"}

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
                fp_angle = round(fp.position.angle or 0)
                dx = abs_x - fp_x
                dy = abs_y - fp_y

                if fp_angle == 180:
                    # ICs at 180°: IC→LED route goes UP from output pin
                    # (horizontal segment at ic_y-0.95), LED anode pad
                    # (0.64mm wide) at (ic_x+1.5, ic_y-0.485).
                    # GND pad upper-right → escape 30° DOWN-RIGHT, d=0.55:
                    #   - clears IC→LED horizontal (0.38mm gap)
                    #   - clears pin 2 (0.162mm via-pad clearance)
                    #   - clears LED pad (0.20mm via-pad clearance)
                    # VCC pad lower-left → escape 135° DOWN-LEFT
                    if net_name == "GND":
                        escape_angle = 30   # down-right
                        escape_dist = 0.55  # shorter to clear LED pad
                    else:
                        escape_angle = 135  # down-left
                        escape_dist = VIA_OFFSET
                elif fp_angle == 270:
                    # Rotated column_select: same geometry as 180°
                    # rotated 90° CW.  IC→LED goes LEFT then UP.
                    # GND pad left-above → escape 300° UP-RIGHT, d=0.55
                    # VCC pad right-below → default diagonal (45°)
                    if net_name == "GND":
                        escape_angle = 300  # up-right
                        escape_dist = 0.55
                    else:
                        escape_angle = 45   # down-right (default)
                        escape_dist = VIA_OFFSET
                else:
                    # Other angles: diagonal away from center, 45° grid
                    escape_dist = VIA_OFFSET
                    dist = math.sqrt(dx * dx + dy * dy)
                    if dist > 0.01:
                        raw = math.degrees(math.atan2(dy, dx))
                        escape_angle = round(raw / 45) * 45
                    else:
                        escape_angle = 90

                pcb.pin_to_via(
                    (abs_x, abs_y), net_num,
                    angle=escape_angle,
                    distance=escape_dist,
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
    """Route LED cathode to R pad 1 on F.Cu.

    Non-byte LEDs: LED@90° cathode at y+0.485, R@90° pad 1 at (x+R_HORIZ, y+0.51).
    Side-by-side layout — nearly horizontal straight trace (0.025mm Y diff).
    Byte LEDs (DFF/BUF): LED@90° cathode at y+0.485, R@270° pad 1 at y+R_OFFSET-0.51.
    Stacked layout — straight vertical trace.
    Root connector LEDs at 180°/R at 0° get a horizontal trace.
    Rotated column_select: LED@180°/R@180° in vertical line — straight trace.

    Returns trace count.
    """
    net_to_pads = _build_net_pad_index(pcb)
    ref_to_part = _build_ref_to_part(netlist_data)
    traces = 0
    processed = set()

    # Build set of NAND LED refs — handled by preroute_nand_leds instead
    nand_led_refs = set()
    for fp2 in pcb.board.footprints:
        ref2 = fp2.properties.get("Reference", "")
        if ref2.startswith("U") and ref_to_part.get(ref2) == "74LVC2G00":
            for out_pin in ["7", "3"]:
                out_net = pcb.get_pad_net(ref2, out_pin)
                if out_net:
                    for pr, pn, px, py, pnet in net_to_pads.get(out_net, []):
                        if pr.startswith("D"):
                            nand_led_refs.add(pr)

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("D") or ref in processed:
            continue

        # Skip NAND LEDs — routed with 45° traces by preroute_nand_leds
        if ref in nand_led_refs:
            continue

        fp_angle = round(fp.position.angle or 0)

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

        cx, cy = cathode_pos
        rx, ry = r_pos

        # Check if R is approximately aligned (same X = stacked, same Y = horizontal)
        if abs(cx - rx) < 0.1:
            # Stacked (byte groups): straight vertical trace
            pcb.add_trace(cathode_pos, r_pos, cathode_net,
                          SIGNAL_TRACE_W, "F.Cu")
            traces += 1
        elif abs(cy - ry) < 0.1:
            # Horizontal (connector LEDs): straight horizontal trace
            pcb.add_trace(cathode_pos, r_pos, cathode_net,
                          SIGNAL_TRACE_W, "F.Cu")
            traces += 1
        else:
            # Side-by-side (non-byte groups): 3-segment route via midpoint X
            # Avoids passing through R's body/GND pad
            mid_x = round((cx + rx) / 2, 2)
            seg1_end = (mid_x, round(cy, 2))
            seg2_end = (mid_x, round(ry, 2))
            pcb.add_trace(cathode_pos, seg1_end, cathode_net,
                          SIGNAL_TRACE_W, "F.Cu")
            traces += 1
            pcb.add_trace(seg1_end, seg2_end, cathode_net,
                          SIGNAL_TRACE_W, "F.Cu")
            traces += 1
            pcb.add_trace(seg2_end, r_pos, cathode_net,
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

    AND/INV@270° (rotated column_select):
      Pin 4 (output) at rel (-0.50, +0.25) — left, slightly below.
      Pin 3 (GND) at rel (-0.50, -0.25) — left, slightly above, SAME X.
      LED anode ~1.75mm ABOVE.
      Route: LEFT 0.45mm from pin 4 (clears GND pad at same X),
      then UP to LED Y, then RIGHT to LED anode X.

    Skips: col_select row 0 (LED above at 90°), BUF, DSBGA-8, DSBGA-6.

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

        # Only handle 90°, 180°, and 270° ICs
        if fp_angle not in (90, 180, 270):
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

        if fp_angle == 90:
            # Only route if LED anode is to the RIGHT of output pin.
            # Skips col_select row 0 at 90° (LED directly above IC).
            if led_anode_pos[0] <= out_pos[0]:
                continue

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

        elif fp_angle == 270:
            # Rotated column_select: output and GND share same X.
            # LED anode is ABOVE (negative Y).
            # 3-segment: LEFT 0.45mm from pin 4 (clears GND pad at
            # same X), then UP to LED Y, then RIGHT to LED anode X.
            left_x = round(out_pos[0] - 0.45, 2)
            led_x = round(led_anode_pos[0], 2)
            led_y = round(led_anode_pos[1], 2)

            # Seg 1: horizontal LEFT from pin 4
            if abs(out_pos[0] - left_x) > 0.01:
                pcb.add_trace(out_pos, (left_x, round(out_pos[1], 2)),
                               ic_out_net, SIGNAL_TRACE_W, "F.Cu")
                traces += 1
            # Seg 2: vertical UP to LED Y
            if abs(out_pos[1] - led_y) > 0.01:
                pcb.add_trace((left_x, round(out_pos[1], 2)),
                               (left_x, led_y),
                               ic_out_net, SIGNAL_TRACE_W, "F.Cu")
                traces += 1
            # Seg 3: horizontal RIGHT to LED anode
            if abs(left_x - led_x) > 0.01:
                pcb.add_trace((left_x, led_y), led_anode_pos,
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
      Via 2: 0.5mm right of BUF A, below nOE at (dff_x+0.75, dff_y+2.62)

    In1.Cu path (3 segments, avoids GND via drill at dff_x+0.65, dff_y+0.75):
      Seg 1: Via 1 → 45° right-down to (dff_x+1.30, dff_y+0.15)
      Seg 2: vertical down to (dff_x+1.30, dff_y+2.07)
      Seg 3: 45° left-down to Via 2

    F.Cu Z-shape (4 segments, threads between nOE pad and GND Z-shape):
      Seg 1: Via 2 → 45° up-left to (dff_x+0.60, dff_y+2.47)
      Seg 2: vertical up to (dff_x+0.60, dff_y+1.90)
      Seg 3: 45° chamfer to (dff_x+0.45, dff_y+1.75)
      Seg 4: horizontal left to BUF pin 2
    Via 1 sits on existing IC→LED trace (same net, no F.Cu stub needed).

    Via size: 0.5mm / 0.3mm drill (minimum for PCBWay/Elecrow).

    Returns (via_count, trace_count).
    """
    VIA1_DX = 0.90       # Via 1 X offset from DFF center
    VIA1_DY = -0.25      # Via 1 Y offset (on IC->LED trace at Q pin Y)
    VIA2_DX = 0.76       # Via 2 X offset (0.51mm right of BUF pin 2)
    VIA2_DY = 2.64       # Via 2 Y offset (clears GND Z-shape chamfer)
    DETOUR_DX = 1.30     # Detour X offset for In1.Cu vertical segment
    STUB_VERT_DX = 0.76  # F.Cu Z-shape vertical X offset from DFF center
    FCU_CHAMFER = 0.15   # 45° chamfer size at Z-shape corners

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

        # F.Cu Z-shape: Via 2 → 45° → vertical → 45° → horizontal → pin 2
        # Threads between nOE pad (X=0.25+0.115) and GND Z-shape (X~0.95)
        vert_x = round(dff_x + STUB_VERT_DX, 2)
        chamfer1 = round(VIA2_DX - STUB_VERT_DX, 2)  # bottom 45° entry
        # Top chamfer: extend to reach vertical while keeping z3 fixed
        z3_x = round(dff_x + 0.45, 2)                     # fixed exit X
        chamfer_top = round(vert_x - z3_x, 2)             # top 45° size
        z1 = (vert_x, round(via2[1] - max(chamfer1, 0), 2))  # top of bottom 45°
        z2 = (vert_x, round(buf_pad2_pos[1] + chamfer_top, 2))  # bottom of vertical
        z3 = (z3_x, buf_pad2_pos[1])                       # after top 45° exit

        pcb.add_trace(via2, z1, dff_q_net, SIGNAL_TRACE_W, "F.Cu")
        pcb.add_trace(z1, z2, dff_q_net, SIGNAL_TRACE_W, "F.Cu")
        pcb.add_trace(z2, z3, dff_q_net, SIGNAL_TRACE_W, "F.Cu")
        pcb.add_trace(z3, buf_pad2_pos, dff_q_net, SIGNAL_TRACE_W, "F.Cu")
        traces += 4

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

    CLK_BUS_Y_OFFSET = -1.25  # bus Y relative to DFF center (above DFF)

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

        # Bus Y: 1.4mm below BUF center (clears DFF-BUF Q via at +2.62)
        bus_y = round(members[0][4] + 1.4, 2)

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


def preroute_enable_buses(pcb, netlist_data):
    """Route WRITE_EN_ROW_n and READ_EN_ROW_n horizontal buses on F.Cu.

    WRITE_EN buses run above the CLK bus (top of byte row).
    READ_EN buses run below the OE bus (bottom of byte row).

    Connection from row_ctrl: 45° diagonal from the ic-to-led junction
    near the LED, then vertical if needed, then horizontal bus across
    both byte columns.

    Returns trace count.
    """
    ref_to_part = {c['ref']: c['part'] for c in netlist_data['components']}
    name_to_net = {n.name: n.number for n in pcb.board.nets}
    traces = 0

    ENABLE_TRACE_W = 0.16   # thinner than SIGNAL_TRACE_W for inter-row clearance
    WRITE_EN_OFFSET = -0.40  # above CLK bus (CLK_bus_y + offset)
    READ_EN_OFFSET = 0.40    # below OE bus (OE_bus_y + offset)

    CLK_BUS_Y_OFFSET = -1.25  # from preroute_clk_fanout
    OE_BUS_Y_OFFSET = 1.4     # from preroute_oe_fanout (relative to BUF Y)
    IC_TO_LED_UP = 0.95       # ic_to_led vertical offset above IC center (180° ICs)
    CORRIDOR_SHIFT = 0.13  # Y shift from junc into corridor between R pads
    HORIZ_PAST_R = R_HORIZ_OFFSET + 1.10  # horiz extension past R + GND via

    for row in range(4):
        for signal, pin_num in [("WRITE_EN", "2"), ("READ_EN", "6")]:
            net_name = f"/{signal}_ROW_{row}"
            net_num = name_to_net.get(net_name)
            if net_num is None:
                continue

            # Find NAND pads and row_ctrl LED center on this net
            nand_pads = []
            led_center = None  # (x, y) of row_ctrl indicator LED
            dff_y = None
            buf_y = None
            for fp in pcb.board.footprints:
                ref = fp.properties.get("Reference", "")
                part = ref_to_part.get(ref, "")
                for pad in fp.pads:
                    pad_net = pad.net if isinstance(pad.net, int) else (
                        pad.net.number if hasattr(pad.net, 'number') else 0)
                    if pad_net != net_num:
                        continue
                    pos = pcb.get_pad_position(ref, pad.number)
                    if part == "74LVC2G00":
                        nand_pads.append(pos)
                        if dff_y is None:
                            byte_y = fp.position.Y + 0.25
                            dff_y = byte_y
                            buf_y = byte_y + BUF_ROW_Y
                    elif part == "LED_Small" and led_center is None:
                        led_center = (fp.position.X, fp.position.Y)

            if len(nand_pads) < 2 or dff_y is None or led_center is None:
                continue

            # Bus Y: above CLK bus for WRITE_EN, below OE bus for READ_EN
            if signal == "WRITE_EN":
                bus_y = round(dff_y + CLK_BUS_Y_OFFSET + WRITE_EN_OFFSET, 2)
            else:
                bus_y = round(buf_y + OE_BUS_Y_OFFSET + READ_EN_OFFSET, 2)

            # ic-to-led junction: where the horizontal segment meets the
            # vertical drop to the LED anode (same net, T-branch here)
            led_x, led_y = led_center
            junc_x = round(led_x, 2)
            junc_y = round(led_y - IC_TO_LED_UP, 2)
            delta_y = abs(bus_y - junc_y)

            if bus_y < junc_y:
                # WRITE_EN: bus above — pure 45° diagonal up-right to bus_y.
                # Goes away from LED-to-R trace (which is below), no crossing.
                diag_end = (round(junc_x + delta_y, 2), round(bus_y, 2))
                pcb.add_trace((junc_x, junc_y), diag_end, net_num,
                              ENABLE_TRACE_W, "F.Cu")
                traces += 1
                x_start = diag_end[0]
            else:
                # READ_EN: bus below — route through the narrow corridor
                # between the write AND's R pad 1 (above) and read AND's
                # R pad 2 (below).  Short 45° up-right into corridor,
                # horizontal right past R + GND via, then 45° down-right.
                corr_y = round(junc_y - CORRIDOR_SHIFT, 2)
                p1 = (round(junc_x + CORRIDOR_SHIFT, 2), corr_y)
                pcb.add_trace((junc_x, junc_y), p1, net_num,
                              ENABLE_TRACE_W, "F.Cu")
                traces += 1
                # Horizontal past R area
                p2 = (round(p1[0] + HORIZ_PAST_R, 2), corr_y)
                pcb.add_trace(p1, p2, net_num, ENABLE_TRACE_W, "F.Cu")
                traces += 1
                # 45° diagonal down-right to bus_y
                corr_delta = round(bus_y - corr_y, 2)
                diag_end = (round(p2[0] + corr_delta, 2), round(bus_y, 2))
                pcb.add_trace(p2, diag_end, net_num,
                              ENABLE_TRACE_W, "F.Cu")
                traces += 1
                x_start = diag_end[0]

            # Horizontal bus from diagonal endpoint across byte area
            x_end = round(max(p[0] for p in nand_pads) + 1.0, 2)
            pcb.add_trace((x_start, bus_y), (x_end, bus_y), net_num,
                          ENABLE_TRACE_W, "F.Cu")
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
      1. Vertical DOWN from DFF GND to via at (ic_x+0.65, dff_y+0.75)
      2. Via to B.Cu GND plane (remove_unused_layers so In1.Cu is free
         for the data trace jumper)
      3. 45° diagonal DOWN-LEFT from via to (ic_x+0.25, dff_y+1.15)
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

        # Via at midpoint Y, shifted right of DFF GND X to clear R pad 1
        via_x = round(dff_gnd[0] + 0.15, 2)
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
                     size=VIA_SIZE, drill=VIA_DRILL,
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

        # Via (F.Cu for local DFF-BUF, In1.Cu for column trunk)
        pcb.add_via((via_x, via_y), dbus_net,
                     size=VIA_SIZE, drill=VIA_DRILL,
                     layers=["F.Cu", "In1.Cu"])
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


def preroute_dff_buf_vcc(pcb, netlist_data):
    """Connect DFF VCC (pin 5) to BUF VCC (pin 5) with a single shared via.

    DFF@90°: pin 5 (VCC) at (ic_x-0.50, dff_y-0.25) — left-top.
    BUF@180°: pin 5 (VCC) at (ic_x-0.25, dff_y+2.25) — left-bottom.

    Via on the BUF VCC diagonal at same X as DFF VCC pin (ic_x-0.50).
    DFF VCC trace jogs left to avoid data via at (ic_x-0.50, dff_y+0.75).

    Route DFF VCC → via:
      1. ~35° angled LEFT-DOWN to (ic_x-1.00, dff_y+0.10) [clears pin 1]
      2. Vertical DOWN to (ic_x-1.00, dff_y+1.50)         [past data via]
      3. 45° diagonal RIGHT-DOWN to via at (ic_x-0.50, dff_y+2.00)

    Route via → BUF VCC:
      4. 45° diagonal RIGHT-DOWN to BUF VCC (ic_x-0.25, dff_y+2.25)

    Returns (via_count, trace_count).
    """
    JOG_X = -1.01        # vertical column X (clears data via h2h + PCBWay via-track)
    STUB_LEFT = 0.05     # horizontal stub before 45° entry (clears DFF pin 1)

    pairs = _find_dff_buf_pairs(pcb, netlist_data)
    vcc_net = pcb.get_net_number("VCC")
    if vcc_net is None:
        print("  WARNING: VCC net not found, skipping DFF-BUF VCC routing")
        return 0, 0

    vias = 0
    traces = 0

    for dff_ref, dff_fp, buf_ref, buf_fp, _data_net in pairs:
        dff_vcc = pcb.get_pad_position(dff_ref, "5")
        buf_vcc = pcb.get_pad_position(buf_ref, "5")
        if dff_vcc is None or buf_vcc is None:
            continue

        dff_x = dff_fp.position.X

        # Via at DFF VCC pin X, on the 45° diagonal to BUF VCC
        via_x = round(dff_vcc[0], 2)
        diag_to_buf = abs(buf_vcc[0] - via_x)  # 45°: dx = dy
        via_y = round(buf_vcc[1] - diag_to_buf, 2)

        jog_x = round(dff_x + JOG_X, 2)

        # --- DFF VCC to via (4 segments: stub, 45°, DOWN, 45° to via) ---
        # 1. Short horizontal LEFT stub (clears DFF pin 1 from 45°)
        stub_end = (round(dff_vcc[0] - STUB_LEFT, 2), dff_vcc[1])
        pcb.add_trace(dff_vcc, stub_end, vcc_net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # 2. 45° LEFT-DOWN from stub end to jog column
        diag_dx = abs(stub_end[0] - jog_x)
        p1 = (jog_x, round(stub_end[1] + diag_dx, 2))
        pcb.add_trace(stub_end, p1, vcc_net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # 2. Vertical DOWN past data via area
        diag_back = abs(via_x - jog_x)  # 45° back: dx = dy
        p2_y = round(via_y - diag_back, 2)
        pcb.add_trace(p1, (jog_x, p2_y), vcc_net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # 3. 45° diagonal RIGHT-DOWN to via
        pcb.add_trace((jog_x, p2_y), (via_x, via_y), vcc_net,
                      SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # Via to In2.Cu (VCC plane)
        pcb.add_via((via_x, via_y), vcc_net,
                    size=VIA_SIZE, drill=VIA_DRILL,
                    layers=["F.Cu", "In2.Cu"],
                    remove_unused_layers=True)
        vias += 1

        # --- Via to BUF VCC (1 segment: 45° diagonal) ---
        pcb.add_trace((via_x, via_y), buf_vcc, vcc_net,
                      SIGNAL_TRACE_W, "F.Cu")
        traces += 1

    return vias, traces


def preroute_r_gnd(pcb, netlist_data):
    """Connect resistor GND pads to the existing DFF-BUF GND vias on F.Cu.

    Each byte DFF LED has a series resistor whose pad 2 is on the GND net.
    Route: Z-shape — short vertical UP, 45° diagonal upper-left, vertical UP
    to GND via.

        R pad 2
            │  (short stub UP)
           ╱   (45° diagonal upper-left)
          │    (vertical UP to GND via)
        GND via

    Geometry (relative to DFF center):
      R pad 2:   (dff_x+1.50, dff_y+2.37)   [0402@270°, pad 2 is lower]
      R pad 1:   (dff_x+1.50, dff_y+1.73)   [cathode, upper pad]
      GND via:   (dff_x+0.65, dff_y+0.75)  [shifted right 0.15mm for clearance]

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

        # Skip NAND Rs: identified by having a NAND LED (at 0°) on the
        # cathode net.  NAND R routing is left to the autorouter.
        pad1_net = pcb.get_pad_net(ref, "1")
        is_nand_r = False
        if pad1_net is not None:
            for other_fp in pcb.board.footprints:
                oref = other_fp.properties.get("Reference", "")
                if oref.startswith("D") and round(other_fp.position.angle or 0) == 0:
                    if pcb.get_pad_net(oref, "1") == pad1_net:
                        is_nand_r = True
                        break
        if is_nand_r:
            continue

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
                # Z-route: vertical UP, 45° diagonal, vertical UP to via
                dx = pad2_pos[0] - best_via[0]   # positive (pad right of via)
                dy = pad2_pos[1] - best_via[1]   # positive (pad below via)
                diag = min(abs(dx), abs(dy))          # 45° covers this much
                remaining_dy = dy - diag
                # Short stub from GND pad, long stub from via
                stub_bot = round(remaining_dy * 0.15, 2)
                stub_top = round(remaining_dy - stub_bot, 2)

                p0 = pad2_pos
                p1 = (pad2_pos[0], round(pad2_pos[1] - stub_bot, 2))
                p2 = (round(best_via[0], 2),
                      round(best_via[1] + stub_top, 2))
                p3 = best_via

                for a, b in [(p0, p1), (p1, p2), (p2, p3)]:
                    if abs(a[0] - b[0]) > 0.01 or abs(a[1] - b[1]) > 0.01:
                        pcb.add_trace(a, b, gnd_net, SIGNAL_TRACE_W, "F.Cu")
                        traces += 1

                routed_fcu = True
                fcu_routed += 1

        # Fallback: via escape to B.Cu GND plane
        # Skip if a GND via already exists nearby (e.g., placed by preroute_nand_leds)
        if not routed_fcu:
            has_nearby_via = any(
                math.sqrt((gvx - pad2_pos[0])**2 + (gvy - pad2_pos[1])**2) < 1.5
                for gvx, gvy in existing_gnd_vias
            )
            if has_nearby_via:
                continue

            if fp_angle == 270:
                escape_angle = 0   # RIGHT (pad 2 at bottom)
            elif fp_angle == 90:
                escape_angle = 0    # RIGHT (pad 2 at top, escape rightward)
            elif fp_angle == 0:
                escape_angle = 90  # DOWN (pad 2 at right)
            elif fp_angle == 180:
                escape_angle = 270  # UP (pad 2 at left, away from LED below)
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

    NAND is at 270° (90° CW).  Horizontal 4x2 pad grid:
      Top row (cy-0.25): pin4(GND) pin3(OE_out) pin2(WRITE_EN) pin1(COL_SEL)
      Bot row (cy+0.25): pin5(COL_SEL) pin6(READ_EN) pin7(CLK_out) pin8(VCC)

    NAND center at (byte_x+0.5, byte_y-0.25).  Courtyard cx±1.3, cy±0.8.
    Gap between NAND right courtyard (byte_x+1.80) and DFF left courtyard
    (byte_x+2.70) is 0.90mm — one corridor fits.

    Pin 7 (CLK output): F.Cu RIGHT through gap, UP, LEFT to CLK bus.
      LED connection left to autorouter (separate In1.Cu corridor would
      collide with OE corridor from adjacent bytes).
    Pin 3 (OE output): F.Cu UP to courtyard top, via to In1.Cu,
      L-route through single corridor (X=byte_x+2.00) DOWN to OE bus.
      LED tap via at LED Y.
    Pin 2 (WRITE_EN): F.Cu straight UP to WRITE_EN bus.
    Pin 6 (READ_EN): F.Cu straight DOWN, left to autorouter through
      LED area (tight clearances).
    Pin 4 (GND): F.Cu UP-LEFT to via on B.Cu.
    Pin 8 (VCC): F.Cu DOWN-RIGHT to via on In2.Cu.

    Returns (via_count, trace_count).
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    vias = 0
    traces = 0

    CORRIDOR_X = 2.00       # single In1.Cu corridor X offset from byte_x
    NAND_X_NUDGE = 0.5      # must match layout_byte_group

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC2G00":
            continue

        nand_x, nand_y = fp.position.X, fp.position.Y
        byte_x = round(nand_x - NAND_X_NUDGE, 2)
        byte_y = round(nand_y + 0.25, 2)  # NAND is at byte_y - 0.25

        # Dynamically determine which output connects to CLK vs OE
        clk_pin = None
        oe_pin = None

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

        # CLK bus Y: 1.25mm above DFF center
        dff_clk_pads.sort(key=lambda p: p[0])
        dff_pin2_y = dff_clk_pads[0][1]
        leftmost_dff_pin_x = dff_clk_pads[0][0]
        dff_cy = round(dff_pin2_y - 0.25, 2)  # DFF@90°: pin2 at ic_y+0.25
        clk_bus_y = round(dff_cy - 1.25, 2)

        # OE bus Y: 1.4mm below BUF center
        buf_oe_pads.sort(key=lambda p: p[0])
        leftmost_buf_oe_x = buf_oe_pads[0][0]
        buf_cy = round(buf_oe_pads[0][1] - 0.50, 2)
        oe_bus_y = round(buf_cy + 1.4, 2)

        # WRITE_EN bus Y
        write_en_bus_y = round(clk_bus_y - 0.40, 2)

        # Courtyard boundaries
        crtyd_top_y = round(byte_y - 1.05, 2)
        crtyd_bot_y = round(byte_y + 0.55, 2)

        # Corridor X (absolute, single corridor for OE only)
        corr_x = round(byte_x + CORRIDOR_X, 2)

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

        oe_led = find_led_anode(oe_net)

        # === Pin 7 (CLK) and Pin 3 (OE) outputs ===
        # Both outputs need to cross the chip body to reach their buses
        # and LEDs. The gap between NAND and DFF courtyards is only 0.90mm
        # which is occupied by existing DFF-BUF VCC traces and vias.
        # Leave output routing to autorouter — it can use In1.Cu freely.
        # We provide escape stubs from each output pin to courtyard edge.
        if clk_pos and clk_net:
            pcb.add_trace(clk_pos, (clk_pos[0], crtyd_bot_y),
                          clk_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        if oe_pos and oe_net:
            pcb.add_trace(oe_pos, (oe_pos[0], crtyd_top_y),
                          oe_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        # === Pin 2: WRITE_EN Input (F.Cu straight UP) ===
        pin2_pos = pcb.get_pad_position(ref, "2")
        pin2_net = pcb.get_pad_net(ref, "2")
        if pin2_pos and pin2_net:
            pcb.add_trace(pin2_pos, (pin2_pos[0], write_en_bus_y),
                          pin2_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        # === Pin 6: READ_EN Input — stub DOWN to courtyard edge only ===
        # Full path to READ_EN bus crosses LED area with tight clearances.
        # Route stub to courtyard edge; autorouter completes to bus.
        pin6_pos = pcb.get_pad_position(ref, "6")
        pin6_net = pcb.get_pad_net(ref, "6")
        if pin6_pos and pin6_net:
            pcb.add_trace(pin6_pos, (pin6_pos[0], crtyd_bot_y),
                          pin6_net, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        # === Pin 4: GND (via to B.Cu) ===
        pin4_pos = pcb.get_pad_position(ref, "4")
        pin4_net = pcb.get_pad_net(ref, "4")
        if pin4_pos and pin4_net:
            # 45° UP-LEFT then vertical UP to via
            gnd_via_x = round(byte_x - 0.45, 2)
            diag_end_y = round(pin4_pos[1] - 0.20, 2)
            pcb.add_trace(pin4_pos, (gnd_via_x, diag_end_y),
                          pin4_net, POWER_TRACE_W, "F.Cu")
            traces += 1

            gnd_via_y = round(byte_y - 0.90, 2)
            pcb.add_trace((gnd_via_x, diag_end_y), (gnd_via_x, gnd_via_y),
                          pin4_net, POWER_TRACE_W, "F.Cu")
            traces += 1

            pcb.add_via((gnd_via_x, gnd_via_y), pin4_net,
                        VIA_SIZE, VIA_DRILL, ["F.Cu", "B.Cu"],
                        remove_unused_layers=True)
            vias += 1

        # === Pin 8: VCC (via to In2.Cu) ===
        pin8_pos = pcb.get_pad_position(ref, "8")
        pin8_net = pcb.get_pad_net(ref, "8")
        if pin8_pos and pin8_net:
            # 45° DOWN-RIGHT then vertical DOWN to via
            vcc_via_x = round(byte_x + 1.45, 2)
            diag_end_y = round(pin8_pos[1] + 0.20, 2)
            pcb.add_trace(pin8_pos, (vcc_via_x, diag_end_y),
                          pin8_net, POWER_TRACE_W, "F.Cu")
            traces += 1

            vcc_via_y = crtyd_bot_y
            pcb.add_trace((vcc_via_x, diag_end_y), (vcc_via_x, vcc_via_y),
                          pin8_net, POWER_TRACE_W, "F.Cu")
            traces += 1

            pcb.add_via((vcc_via_x, vcc_via_y), pin8_net,
                        VIA_SIZE, VIA_DRILL, ["F.Cu", "In2.Cu"],
                        remove_unused_layers=True)
            vias += 1

    return vias, traces


def preroute_nand_leds(pcb, netlist_data):
    """Route NAND LED+R pairs and connect R GND pads together.

    Each NAND (74LVC2G00) has two output LEDs placed side by side in a
    mirrored layout:
      Left pair (i=0):  LED@180°, R@0°
      Right pair (i=1): LED@0°,   R@180°

    This function:
    1. Routes both pairs' LED cathode to R pad 1 with 45° traces
       (vertical stub + 45° diagonal)
    2. Places a shared GND via centered below the two R GND pads,
       routes each R GND pad to it at 45°

    Returns (via_count, trace_count).
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    gnd_net = pcb.get_net_number("GND")
    vias = 0
    traces = 0

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC2G00":
            continue

        # Find LED+R pairs on each NAND output net
        r_gnd_pads = []  # (x, y) of R pad 2 (GND) for each pair

        for out_pin in ["7", "3"]:
            out_net = pcb.get_pad_net(ref, out_pin)
            if not out_net:
                continue

            pads = net_to_pads.get(out_net, [])

            # Find LED on this net
            led_ref = None
            led_dist = float("inf")
            for pr, pn, px, py, pnet in pads:
                if pr.startswith("D"):
                    d = math.sqrt((px - fp.position.X)**2 +
                                  (py - fp.position.Y)**2)
                    if d < 10 and d < led_dist:
                        led_dist = d
                        led_ref = pr

            if not led_ref:
                continue

            # Route LED cathode to R pad 1 with 45° trace
            cathode_net = pcb.get_pad_net(led_ref, "1")
            cathode_pos = pcb.get_pad_position(led_ref, "1")
            if cathode_net is None or cathode_pos is None:
                continue

            # Find nearest R on cathode net
            cathode_pads = net_to_pads.get(cathode_net, [])
            r_pos = None
            best_d = float("inf")
            for pr, pn, px, py, pnet in cathode_pads:
                if pr.startswith("R"):
                    d = math.sqrt((px - cathode_pos[0])**2 +
                                  (py - cathode_pos[1])**2)
                    if d < 5 and d < best_d:
                        best_d = d
                        r_pos = (px, py)

            if r_pos:
                dx = abs(r_pos[0] - cathode_pos[0])
                dy = abs(r_pos[1] - cathode_pos[1])
                stub = abs(dy - dx)
                if stub > 0.01:
                    # Vertical stub from cathode, then 45° diagonal to R
                    stub_y = round(cathode_pos[1] + stub, 2)
                    pcb.add_trace(cathode_pos,
                                  (round(cathode_pos[0], 2), stub_y),
                                  cathode_net, SIGNAL_TRACE_W, "F.Cu")
                    pcb.add_trace((round(cathode_pos[0], 2), stub_y),
                                  r_pos,
                                  cathode_net, SIGNAL_TRACE_W, "F.Cu")
                    traces += 2
                else:
                    # Already 45° — single trace
                    pcb.add_trace(cathode_pos, r_pos, cathode_net,
                                  SIGNAL_TRACE_W, "F.Cu")
                    traces += 1

            # Collect R GND pad position for this pair
            for pr, pn, px, py, pnet in cathode_pads:
                if pr.startswith("R"):
                    r_gnd_pos = pcb.get_pad_position(pr, "2")
                    if r_gnd_pos:
                        r_gnd_pads.append(r_gnd_pos)
                    break

        # Place a shared GND via centered between and below the two R GND pads,
        # then route each R GND pad to it with 45° diagonal traces.
        if len(r_gnd_pads) == 2 and gnd_net is not None:
            via_x = round((r_gnd_pads[0][0] + r_gnd_pads[1][0]) / 2, 2)
            # Y offset = X offset for 45° diagonals
            dx = abs(r_gnd_pads[1][0] - r_gnd_pads[0][0]) / 2
            via_y = round(max(r_gnd_pads[0][1], r_gnd_pads[1][1]) + dx, 2)
            pcb.add_via((via_x, via_y), gnd_net,
                        VIA_SIZE, VIA_DRILL, ["F.Cu", "B.Cu"],
                        remove_unused_layers=True)
            vias += 1
            for gnd_pos in r_gnd_pads:
                pcb.add_trace(gnd_pos, (via_x, via_y), gnd_net,
                              SIGNAL_TRACE_W, "F.Cu")
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

    The 74LVC2G00 dual NAND is at 270° (90° CW).  Pin 1 (1A) and pin 5 (2A)
    carry the COL_SEL signal.  At 270°:
      Pin 1 at (cx+0.75, cy-0.25) — top row, rightmost
      Pin 5 at (cx-0.75, cy+0.25) — bottom row, leftmost

    Routing strategy per byte:
      Pin 1: F.Cu stub UP to courtyard top → via to In1.Cu
      Pin 5: F.Cu stub DOWN to courtyard bottom → via to In1.Cu
      In1.Cu: from pin1 via, horizontal LEFT to trunk X, then DOWN to pin5 via.
      Trunk X at byte_x-1.0 (avoids GND via at byte_x-0.45).
      L-route at crtyd_top_y avoids VCC via at crtyd_bot_y.

    Returns (via_count, trace_count).
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    vias = 0
    traces = 0

    NAND_X_NUDGE = 0.5  # must match layout_byte_group
    # Trunk X must clear GND via drill at byte_x-0.45.
    # PTH rule: 0.33mm from drill edge.  drill_r=0.15, trace_hw=0.10
    # → center-to-center >= 0.58mm → trunk at byte_x-0.45-0.58 = byte_x-1.03
    TRUNK_X_OFFSET = -1.05  # trunk X relative to byte_x

    # Collect trunk points grouped by COL_SEL net
    net_trunk_pts = {}  # net_num -> [(x, y), ...]

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC2G00":
            continue

        nand_x, nand_y = fp.position.X, fp.position.Y
        byte_x = round(nand_x - NAND_X_NUDGE, 2)
        byte_y = round(nand_y + 0.25, 2)

        pin1_pos = pcb.get_pad_position(ref, "1")
        pin5_pos = pcb.get_pad_position(ref, "5")
        pin1_net = pcb.get_pad_net(ref, "1")
        if not pin1_pos or not pin5_pos or not pin1_net:
            continue

        net = pin1_net

        # Courtyard boundaries
        crtyd_top_y = round(byte_y - 1.05, 2)
        crtyd_bot_y = round(byte_y + 0.55, 2)

        trunk_x = round(byte_x + TRUNK_X_OFFSET, 2)

        # Pin 1 (top row, byte_x+1.25, byte_y-0.50): escape UP to courtyard
        # top, then via to In1.Cu.
        via1_pos = (round(pin1_pos[0], 2), crtyd_top_y)
        pcb.add_trace(pin1_pos, via1_pos, net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1
        pcb.add_via(via1_pos, net, VIA_SIZE, VIA_DRILL, ["F.Cu", "In1.Cu"])
        vias += 1

        # Pin 5 (bottom row, byte_x-0.25, byte_y+0.00): escape DOWN to
        # courtyard bottom, then via to In1.Cu.
        via5_pos = (round(pin5_pos[0], 2), crtyd_bot_y)
        pcb.add_trace(pin5_pos, via5_pos, net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1
        pcb.add_via(via5_pos, net, VIA_SIZE, VIA_DRILL, ["F.Cu", "In1.Cu"])
        vias += 1

        # In1.Cu: connect via1 to via5 with 45° diagonal, then via5 to trunk.
        # via1 at (byte_x+1.25, byte_y-1.05), via5 at (byte_x-0.25, byte_y+0.55)
        # dx=-1.50, dy=+1.60.  Chamfer: short vertical + 45° diagonal.
        v1x, v1y = via1_pos
        v5x, v5y = via5_pos
        dx = round(v5x - v1x, 2)   # -1.50
        dy = round(v5y - v1y, 2)   # +1.60
        diag = min(abs(dx), abs(dy))  # 1.50
        stub = round(abs(dy) - diag, 2)  # 0.10 vertical stub

        # Vertical stub DOWN from via1, then 45° diagonal to via5
        mid_pt = (v1x, round(v1y + stub, 2))
        pcb.add_trace(via1_pos, mid_pt, net, SIGNAL_TRACE_W, "In1.Cu")
        traces += 1
        pcb.add_trace(mid_pt, via5_pos, net, SIGNAL_TRACE_W, "In1.Cu")
        traces += 1

        # From via5, horizontal LEFT to trunk X
        pcb.add_trace(via5_pos, (trunk_x, v5y), net, SIGNAL_TRACE_W, "In1.Cu")
        traces += 1

        if net not in net_trunk_pts:
            net_trunk_pts[net] = []
        net_trunk_pts[net].append((trunk_x, v5y))

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


def preroute_column_dbus(pcb, netlist_data):
    """Route D0-D7 vertical trunks on In1.Cu connecting bytes in each column.

    preroute_dff_buf_data connects DFF pin 1 (D) to BUF pin 4 (Y) within
    each byte via a via at (dff_pin1_x, dff_pin1_y + 0.50).  This function
    routes In1.Cu vertical trunks connecting those vias across the 4 bytes
    in each byte column.

    Geometry:
      - Via X = ic_x - 0.50 (DFF pin 1 X at 90°)
      - Via Y = dff_pin1_y + 0.50 (midpoint between DFF D and BUF Y)
      - Trunk X = ic_x + 0.05 (0.55mm right of via, clears VCC via hole)
      - Horizontal stubs from each via to trunk on In1.Cu
      - Vertical trunk segments between adjacent bytes on In1.Cu

    Clearance from VCC via at (ic_x - 0.50, dff_y + 2.00):
      Trunk at ic_x + 0.05 is 0.55mm away horizontally.  With 0.3mm drill
      (0.15mm radius) and 0.2mm trace (0.10mm half-width), edge-to-edge
      clearance = 0.55 - 0.15 - 0.10 = 0.30mm > 0.254mm PCBWay/Elecrow.

    Returns trace_count (int).
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    traces = 0

    # Find D* nets: nets connecting both a DFF pin 1 and a BUF pin 4
    dbus_nets = set()
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if ref_to_part.get(ref) != "74LVC1G79":
            continue
        d_net = pcb.get_pad_net(ref, "1")
        if not d_net or d_net == 0:
            continue
        pads_on_net = net_to_pads.get(d_net, [])
        if any(ref_to_part.get(pr) == "74LVC1G125" and pn == "4"
               for pr, pn, *_ in pads_on_net if pr.startswith("U")):
            dbus_nets.add(d_net)

    for net_num in sorted(dbus_nets):
        pads_on_net = net_to_pads.get(net_num, [])

        # Collect via positions from DFF pin 1 positions.
        # DFF@90° pin 1 at (ic_x - 0.50, dff_y + 0.25).
        # Data bus via at (pin1_x, pin1_y + 0.50).
        # Trunk at pin1_x + 0.55 = ic_x + 0.05 (clears VCC via hole).
        via_info = []  # (via_x, via_y, trunk_x)
        for pad_ref, pad_num, px, py, _pnet in pads_on_net:
            if (pad_ref.startswith("U") and pad_num == "1"
                    and ref_to_part.get(pad_ref) == "74LVC1G79"):
                via_x = round(px, 2)
                via_y = round(py + 0.50, 2)
                trunk_x = round(px + 0.55, 2)
                via_info.append((via_x, via_y, trunk_x))

        if len(via_info) < 2:
            continue

        # Group by trunk X (same bit position in same byte column)
        by_trunk = defaultdict(list)
        for vx, vy, tx in via_info:
            by_trunk[tx].append((vx, vy))

        for trunk_x, positions in by_trunk.items():
            if len(positions) < 2:
                continue
            positions.sort(key=lambda p: p[1])  # sort by Y

            # Horizontal stubs from each via to trunk
            for vx, vy in positions:
                if abs(vx - trunk_x) > 0.01:
                    pcb.add_trace((vx, vy), (trunk_x, vy),
                                  net_num, SIGNAL_TRACE_W, "In1.Cu")
                    traces += 1

            # Vertical trunk segments between adjacent bytes
            for i in range(len(positions) - 1):
                _, y1 = positions[i]
                _, y2 = positions[i + 1]
                pcb.add_trace((trunk_x, y1), (trunk_x, y2),
                              net_num, SIGNAL_TRACE_W, "In1.Cu")
                traces += 1

    return traces


def preroute_dbus_fanout(pcb, netlist_data):
    """Route D0-D7 data bus fanout below the byte grid.

    Extends the column trunks (In1.Cu) down to vias, then routes a
    single set of 8 horizontal F.Cu traces across both byte columns.

    For each data bit:
      1. In each column, extend the In1.Cu trunk DOWN to a via
      2. Via transitions from In1.Cu to F.Cu
      3. F.Cu horizontal trace connects col0 and col1 vias
      4. F.Cu trace extends LEFT past col0 for autorouter pickup

    The vertical In1.Cu extensions are at different X positions within
    each column, so they cannot cross each other.  The horizontal F.Cu
    traces are at different Y levels (one per bit), so they cannot
    cross each other either.

    Returns (via_count, trace_count).
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    vias = 0
    traces = 0

    # Bus lane parameters
    # Via pad to adjacent trace: VIA_SIZE/2 + TRACE_W/2 + PCBWay clearance = 0.25+0.10+0.254 = 0.604
    DBUS_LANE_SPACING = 0.61  # mm between adjacent lanes (PCBWay via-to-track clean)
    # Gap must clear enable bus traces: READ_EN at byte_y+3.55, trunk vias at ~byte_y+0.75
    # Need 3.55 - 0.75 + VIA_radius(0.25) + clearance(0.15) = 3.20mm minimum
    BUS_GAP_FROM_BYTES = 4.0  # mm gap from lowest trunk bottom to first bus lane
    BUS_LEFT_PAD = 3.0        # mm left of col0's leftmost trunk for F.Cu extension

    # Find D* nets: nets connecting both a DFF pin 1 and a BUF pin 4
    dbus_nets = set()
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if ref_to_part.get(ref) != "74LVC1G79":
            continue
        d_net = pcb.get_pad_net(ref, "1")
        if not d_net or d_net == 0:
            continue
        pads_on_net = net_to_pads.get(d_net, [])
        if any(ref_to_part.get(pr) == "74LVC1G125" and pn == "4"
               for pr, pn, *_ in pads_on_net if pr.startswith("U")):
            dbus_nets.add(d_net)

    # For each D* net, collect trunk positions grouped by column.
    # Trunk X = dff_pin1_x + 0.55 (same as preroute_column_dbus).
    # Bottom of trunk = max via_y in that column.
    all_trunk_xs = []
    net_trunk_info = {}  # net_num -> [(trunk_x, bottom_via_y), ...]

    for net_num in sorted(dbus_nets):
        pads_on_net = net_to_pads.get(net_num, [])
        trunk_groups = defaultdict(list)  # trunk_x -> [via_y, ...]

        for pad_ref, pad_num, px, py, _pnet in pads_on_net:
            if (pad_ref.startswith("U") and pad_num == "1"
                    and ref_to_part.get(pad_ref) == "74LVC1G79"):
                trunk_x = round(px + 0.55, 2)
                via_y = round(py + 0.50, 2)
                trunk_groups[trunk_x].append(via_y)

        trunks = []
        for tx, ys in trunk_groups.items():
            trunks.append((tx, max(ys)))
            all_trunk_xs.append(tx)

        if trunks:
            net_trunk_info[net_num] = trunks

    if not all_trunk_xs:
        return 0, 0

    # Split trunks into byte columns at the midpoint of all trunk Xs
    all_trunk_xs.sort()
    col_boundary_x = (all_trunk_xs[0] + all_trunk_xs[-1]) / 2

    # Build per-net column map: net_num -> {0: (trunk_x, bottom_y), 1: ...}
    net_cols = {}  # net_num -> {col_idx: (trunk_x, bottom_y)}
    for net_num, trunks in net_trunk_info.items():
        cols = {}
        for tx, bottom_y in trunks:
            col_idx = 0 if tx < col_boundary_x else 1
            cols[col_idx] = (tx, bottom_y)
        net_cols[net_num] = cols

    # Bus corridor Y: below ALL trunk bottoms across both columns
    all_bottom_ys = [by for trunks in net_trunk_info.values()
                     for _, by in trunks]
    bus_top_y = round(max(all_bottom_ys) + BUS_GAP_FROM_BYTES, 2)

    # Sort nets by col0 trunk_x for consistent lane ordering
    sorted_nets = sorted(
        net_cols.keys(),
        key=lambda n: net_cols[n].get(0, (999, 0))[0])

    # Leftmost trunk X across all col0 entries (for F.Cu left extension)
    col0_min_tx = min(
        net_cols[n][0][0] for n in sorted_nets if 0 in net_cols[n])
    left_x = round(col0_min_tx - BUS_LEFT_PAD, 2)

    for lane_idx, net_num in enumerate(sorted_nets):
        cols = net_cols[net_num]
        lane_y = round(bus_top_y + lane_idx * DBUS_LANE_SPACING, 2)

        via_positions = []  # (via_x, via_y) on F.Cu for horizontal trace

        # For each column: extend trunk on In1.Cu, place via to F.Cu
        for col_idx in sorted(cols.keys()):
            trunk_x, bottom_y = cols[col_idx]

            # Vertical In1.Cu: extend trunk down to lane Y
            pcb.add_trace((trunk_x, bottom_y), (trunk_x, lane_y),
                          net_num, SIGNAL_TRACE_W, "In1.Cu")
            traces += 1

            # Via: In1.Cu → F.Cu at the trunk bottom extension
            pcb.add_via((trunk_x, lane_y), net_num,
                        VIA_SIZE, VIA_DRILL, ["F.Cu", "In1.Cu"])
            vias += 1

            via_positions.append(trunk_x)

        # F.Cu horizontal trace connecting all vias + left extension
        via_positions.sort()
        all_x = [left_x] + via_positions
        for i in range(len(all_x) - 1):
            pcb.add_trace((all_x[i], lane_y), (all_x[i + 1], lane_y),
                          net_num, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return vias, traces


def preroute_dbus_to_connector(pcb, netlist_data):
    """Route D0-D7 from byte grid fanout to connector LED fanout stubs.

    Bridges the gap between the D* bus horizontal fanout (below byte grid)
    and the connector LED stubs (right of J1), routing around the address
    decoder and control logic groups entirely on F.Cu.

    Route geometry per signal:
      0. Staggered horizontal extension (spreads traces for diagonal)
      1. 45-deg diagonal DOWN-LEFT from staggered start to bus corridor
      2. Horizontal LEFT through bus corridor (below control logic)
      3. 45-deg diagonal from corridor to connector stub endpoint

    Diagonal perpendicular spacing matches horizontal spacing:
      - Fanout Y-spacing = 0.61mm (from preroute_dbus_fanout)
      - Diagonal Δc = 0.61 * √2 = 0.863mm  →  perp = 0.863/√2 = 0.61mm
      - Bus lane Y-spacing = 0.863mm (matches diagonal Δc)
      - Stagger per trace = 0.863 - 0.61 = 0.253mm in X
      This keeps all diagonals parallel at true 45°, ending at the same X.

    Returns trace_count (int).
    """
    traces = 0
    SQRT2 = math.sqrt(2)
    FANOUT_SPACING = 0.61     # Y-spacing from preroute_dbus_fanout
    DIAG_PERP = FANOUT_SPACING  # desired perpendicular gap on diagonals
    DIAG_LANE_SPACING = round(DIAG_PERP * SQRT2, 4)  # bus lane Y-spacing (0.863mm)
    STAGGER = round(DIAG_LANE_SPACING - FANOUT_SPACING, 4)  # X-stagger per trace (0.253mm)

    # --- Find D* signal nets from J1 connector ---
    dbus_nets = {}  # net_num -> net_name (e.g., 64 -> '/D0')
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if ref == "J1":
            for pad in fp.pads:
                if (pad.net and pad.net.name
                        and pad.net.name.startswith("/D")
                        and pad.net.name[2:].isdigit()):
                    dbus_nets[pad.net.number] = pad.net.name
            break

    if not dbus_nets:
        print("  WARNING: No D* nets found on J1")
        return 0

    # --- Find fanout left endpoints ---
    # Leftmost point of horizontal F.Cu traces per D net (x > 50, i.e. byte area)
    fanout_ends = {}  # net_num -> (x, y)
    for seg in pcb.board.traceItems:
        if not hasattr(seg, 'start') or not hasattr(seg, 'net'):
            continue
        if seg.net not in dbus_nets or getattr(seg, 'layer', '') != 'F.Cu':
            continue
        x1, y1, x2, y2 = seg.start.X, seg.start.Y, seg.end.X, seg.end.Y
        if abs(y1 - y2) < 0.01 and min(x1, x2) > 50:
            left_x = min(x1, x2)
            if seg.net not in fanout_ends or left_x < fanout_ends[seg.net][0]:
                fanout_ends[seg.net] = (round(left_x, 2), round(y1, 2))

    # --- Find connector stub endpoints ---
    # Rightmost point of horizontal F.Cu traces per D net (x < 40, connector area)
    stub_ends = {}  # net_num -> (x, y)
    for seg in pcb.board.traceItems:
        if not hasattr(seg, 'start') or not hasattr(seg, 'net'):
            continue
        if seg.net not in dbus_nets or getattr(seg, 'layer', '') != 'F.Cu':
            continue
        x1, y1, x2, y2 = seg.start.X, seg.start.Y, seg.end.X, seg.end.Y
        if abs(y1 - y2) < 0.01 and max(x1, x2) < 40:
            right_x = max(x1, x2)
            if seg.net not in stub_ends or right_x > stub_ends[seg.net][0]:
                stub_ends[seg.net] = (round(right_x, 2), round(y1, 2))

    # --- Match fanout ends with stub ends ---
    matched = []  # [(net_num, fanout_x, fanout_y, stub_x, stub_y)]
    for net_num in dbus_nets:
        if net_num in fanout_ends and net_num in stub_ends:
            fx, fy = fanout_ends[net_num]
            sx, sy = stub_ends[net_num]
            matched.append((net_num, fx, fy, sx, sy))

    if not matched:
        print("  WARNING: No D* bus traces to connect to connector")
        return 0

    # Sort by fanout Y ascending (D7 at top / lowest Y first, D0 at bottom last)
    matched.sort(key=lambda m: m[2])
    n = len(matched)

    # --- Compute bus corridor Y (below all obstacles in the path) ---
    COURTYARD_HALF = 1.5  # conservative: DSBGA-5 is ±1.45mm
    BUS_GAP = 1.0         # clearance from courtyard bottom to first bus lane

    fanout_x = matched[0][1]  # all fanout left ends at same X
    max_obs_bottom = 0
    for fp in pcb.board.footprints:
        x = fp.position.X
        if 30 < x < fanout_x:
            obs_bottom = fp.position.Y + COURTYARD_HALF
            max_obs_bottom = max(max_obs_bottom, obs_bottom)

    bus_top_y = round(max_obs_bottom + BUS_GAP, 2)

    # Bus lanes: wider spacing to match diagonal perpendicular gap
    bus_lanes = [round(bus_top_y + i * DIAG_LANE_SPACING, 2)
                 for i in range(n)]

    # --- Check via clearance and compute horizontal pad ---
    # Each trace i starts its diagonal at x_i = fanout_x - h_pad - (n-1-i)*STAGGER.
    # The 45° line constant is c_i = x_i + fanout_y_i.
    # Δc between adjacent lines = STAGGER + FANOUT_SPACING = DIAG_LANE_SPACING.
    # Check all vias against all diagonal lines and find the worst-case pad.
    VIA_CLEARANCE = 0.50  # via radius + half trace + clearance

    # Estimate diagonal end X for bounding-box filter
    total_stagger = round((n - 1) * STAGGER, 2)
    diag_end_x_est = round(
        fanout_x - total_stagger - (bus_lanes[0] - matched[0][2]), 2)

    worst_pad = 0.0
    for item in pcb.board.traceItems:
        if not hasattr(item, 'position'):
            continue
        vx, vy = item.position.X, item.position.Y
        if vx < diag_end_x_est - 2 or vx > fanout_x + 1:
            continue
        if vy < matched[0][2] - 1 or vy > bus_lanes[-1] + 1:
            continue
        for i in range(n):
            # c with h_pad=0: fanout_x - (n-1-i)*STAGGER + fanout_y_i
            c0 = fanout_x - (n - 1 - i) * STAGGER + matched[i][2]
            perp = abs(vx + vy - c0) / SQRT2
            if perp < VIA_CLEARANCE:
                needed = (VIA_CLEARANCE - perp) * SQRT2
                worst_pad = max(worst_pad, needed)

    h_pad = round(math.ceil(worst_pad * 10) / 10, 2)  # round up to 0.1mm
    if h_pad > 0:
        print(f"  D*->connector: via avoidance pad = {h_pad:.1f}mm")

    # --- Compute per-trace diagonal start X ---
    # Trace 0 (D7, top) gets the most extension; trace n-1 (D0, bottom) the least.
    diag_starts = [round(fanout_x - h_pad - (n - 1 - i) * STAGGER, 2)
                   for i in range(n)]
    # All diagonals end at the same X (mathematically guaranteed)
    diag_end_x = round(diag_starts[0] - (bus_lanes[0] - matched[0][2]), 2)

    # --- Bus lanes at FANOUT_SPACING (0.61mm) ---
    # Each staggered diagonal ends at a different X when it reaches its
    # bus lane Y.  This creates a natural "peel-off" transition: the
    # outermost trace (most stagger) reaches its lane first (furthest
    # right), while the innermost trace runs furthest left.  No separate
    # compression zone needed.
    bus_lanes = [round(bus_top_y + i * FANOUT_SPACING, 2)
                 for i in range(n)]

    # Per-trace diagonal end X (each different due to stagger)
    diag_ends = [round(diag_starts[i] - (bus_lanes[i] - matched[i][2]), 2)
                 for i in range(n)]

    # --- Fan corridor geometry ---
    stub_x = matched[0][3]  # all stubs at same X
    max_delta_y = max(abs(bus_lanes[i] - matched[i][4]) for i in range(n))
    CORRIDOR_PAD = 1.5
    corridor_x = round(stub_x + max_delta_y + CORRIDOR_PAD, 2)
    horiz_available = round(corridor_x - stub_x, 2)

    print(f"  D*->connector: bus y={bus_top_y}..{bus_lanes[-1]:.2f}, "
          f"diag x={diag_ends[0]:.1f}..{diag_ends[-1]:.1f}, "
          f"corridor_x={corridor_x:.1f}, perp={DIAG_PERP:.2f}mm")

    # --- Route each trace ---
    for i in range(n):
        net_num, fx, fy, sx, sy = matched[i]
        bus_y = bus_lanes[i]
        ds_x = diag_starts[i]
        de_x = diag_ends[i]

        # Segment 0: horizontal extension (stagger + via pad)
        if abs(fx - ds_x) > 0.01:
            pcb.add_trace((fx, fy), (ds_x, fy),
                          net_num, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

        # Segment 1: 45° diagonal to bus lane
        pcb.add_trace((ds_x, fy), (de_x, bus_y),
                      net_num, SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # Segment 2+3: horizontal bus + 45° fan to connector stub
        delta_y = sy - bus_y
        abs_dy = abs(delta_y)

        if abs_dy < 0.01:
            pcb.add_trace((de_x, bus_y), (sx, sy),
                          net_num, SIGNAL_TRACE_W, "F.Cu")
            traces += 1
        elif abs_dy <= horiz_available:
            chamfer_start_x = round(sx + abs_dy, 2)
            if abs(de_x - chamfer_start_x) > 0.01:
                pcb.add_trace((de_x, bus_y),
                              (chamfer_start_x, bus_y),
                              net_num, SIGNAL_TRACE_W, "F.Cu")
                traces += 1
            pcb.add_trace((chamfer_start_x, bus_y), (sx, sy),
                          net_num, SIGNAL_TRACE_W, "F.Cu")
            traces += 1
        else:
            vert_dir = 1 if delta_y > 0 else -1
            diag_y = round(bus_y + vert_dir * horiz_available, 2)
            pcb.add_trace((de_x, bus_y), (corridor_x, bus_y),
                          net_num, SIGNAL_TRACE_W, "F.Cu")
            traces += 1
            pcb.add_trace((corridor_x, bus_y), (sx, diag_y),
                          net_num, SIGNAL_TRACE_W, "F.Cu")
            traces += 1
            pcb.add_trace((sx, diag_y), (sx, sy),
                          net_num, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return traces


def preroute_coladdr_to_colsel(pcb, netlist_data):
    """Route column address A7-A10 from connector stubs to column_select INVs.

    Bridges the gap between the connector LED fanout stubs (x~30, y~135-143)
    and the column_select inverter inputs (x~117-129, y~130) on F.Cu.

    Routes a horizontal parallel bus RIGHT from the connector stubs.  The
    signal-to-INV mapping is reversed (A7=bottom connector→leftmost INV,
    A10=top→rightmost) so diagonals to the INV pins would cross on a
    single layer.  The bus stops before the crossing region and the
    autorouter handles the final fan to each INV input.

    Returns trace_count (int).
    """
    traces = 0

    # --- Find column address nets from J1 connector ---
    coladdr_nets = {}  # net_num -> net_name
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if ref != "J1":
            continue
        angle_rad = math.radians(fp.position.angle or 0)
        cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
        for pad in fp.pads:
            if not (pad.net and pad.net.name):
                continue
            name = pad.net.name
            if any(f"A{n}" in name for n in [7, 8, 9, 10]):
                coladdr_nets[pad.net.number] = name
        break

    if not coladdr_nets:
        print("  WARNING: No A7-A10 nets found on J1")
        return 0

    # --- Find connector stub endpoints ---
    stub_ends = {}  # net_num -> (x, y)
    for seg in pcb.board.traceItems:
        if not hasattr(seg, 'start') or not hasattr(seg, 'net'):
            continue
        if seg.net not in coladdr_nets or getattr(seg, 'layer', '') != 'F.Cu':
            continue
        x1, y1, x2, y2 = seg.start.X, seg.start.Y, seg.end.X, seg.end.Y
        if abs(y1 - y2) < 0.01 and max(x1, x2) < 40:
            right_x = max(x1, x2)
            if seg.net not in stub_ends or right_x > stub_ends[seg.net][0]:
                stub_ends[seg.net] = (round(right_x, 2), round(y1, 2))

    # --- Find inverter input pin positions (pin 2 of 74LVC1G04) ---
    ref_to_part = _build_ref_to_part(netlist_data)
    inv_inputs = {}  # net_num -> (x, y)
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if ref_to_part.get(ref) != "74LVC1G04":
            continue
        pin2_net = pcb.get_pad_net(ref, "2")
        if pin2_net not in coladdr_nets:
            continue
        pin2_pos = pcb.get_pad_position(ref, "2")
        if pin2_pos is None:
            continue
        if pin2_net not in inv_inputs or pin2_pos[0] < inv_inputs[pin2_net][0]:
            inv_inputs[pin2_net] = (round(pin2_pos[0], 2), round(pin2_pos[1], 2))

    # --- Match stubs with inverter inputs ---
    matched = []  # [(net_num, stub_x, stub_y, inv_x, inv_y)]
    for net_num in coladdr_nets:
        if net_num in stub_ends and net_num in inv_inputs:
            sx, sy = stub_ends[net_num]
            ix, iy = inv_inputs[net_num]
            matched.append((net_num, sx, sy, ix, iy))

    if not matched:
        print("  WARNING: No A7-A10 stubs matched to inverter inputs")
        return 0

    matched.sort(key=lambda m: m[2])  # sort by stub Y ascending
    n = len(matched)

    # --- Compute peel-off points (where 45° diagonal would start) ---
    peel_xs = []
    for i in range(n):
        _, sx, sy, ix, iy = matched[i]
        abs_dy = abs(sy - iy)
        peel_x = round(ix - abs_dy, 2)
        peel_xs.append(peel_x)

    stub_x = matched[0][1]
    min_peel_x = min(peel_xs)

    print(f"  A7-A10->colsel: bus x={stub_x}..{min_peel_x:.1f}, "
          f"peel x={min(peel_xs):.1f}..{max(peel_xs):.1f}")

    # --- Route: horizontal bus from stubs to earliest peel-off ---
    for i in range(n):
        net_num, sx, sy, ix, iy = matched[i]
        if abs(min_peel_x - sx) > 0.01:
            pcb.add_trace((sx, sy), (min_peel_x, sy),
                          net_num, SIGNAL_TRACE_W, "F.Cu")
            traces += 1

    return traces


def preroute_colsel_fanout(pcb, netlist_data):
    """Extend COL_SEL In1.Cu trunks down below the D* bus and add vias.

    The col_sel trunks (from preroute_col_sel_vias) connect the 4 bytes
    in each column vertically on In1.Cu at trunk_x = byte_x - 1.05.
    This function extends those trunks past the D* bus fanout area and
    places an In1.Cu → F.Cu via at the bottom for autorouter pickup.

    Returns (via_count, trace_count).
    """
    ref_to_part = _build_ref_to_part(netlist_data)
    net_to_pads = _build_net_pad_index(pcb)
    vias = 0
    traces = 0

    NAND_X_NUDGE = 0.5
    TRUNK_X_OFFSET = -1.05
    COLSEL_GAP_BELOW_DBUS = 2.0  # mm below last D* bus lane

    # --- Compute D* bus bottom Y (same logic as preroute_dbus_fanout) ---
    dbus_nets = set()
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if ref_to_part.get(ref) != "74LVC1G79":
            continue
        d_net = pcb.get_pad_net(ref, "1")
        if not d_net or d_net == 0:
            continue
        pads_on_net = net_to_pads.get(d_net, [])
        if any(ref_to_part.get(pr) == "74LVC1G125" and pn == "4"
               for pr, pn, *_ in pads_on_net if pr.startswith("U")):
            dbus_nets.add(d_net)

    # Find trunk bottom Ys for D* bus computation
    all_dbus_bottom_ys = []
    for net_num in dbus_nets:
        pads_on_net = net_to_pads.get(net_num, [])
        for pad_ref, pad_num, px, py, _pnet in pads_on_net:
            if (pad_ref.startswith("U") and pad_num == "1"
                    and ref_to_part.get(pad_ref) == "74LVC1G79"):
                via_y = round(py + 0.50, 2)
                all_dbus_bottom_ys.append(via_y)

    if not all_dbus_bottom_ys:
        return 0, 0

    DBUS_BUS_GAP = 4.0
    DBUS_LANE_SPACING = 0.61
    bus_top_y = round(max(all_dbus_bottom_ys) + DBUS_BUS_GAP, 2)
    n_dbus_lanes = len(dbus_nets)
    bus_bottom_y = round(bus_top_y + (n_dbus_lanes - 1) * DBUS_LANE_SPACING, 2)

    colsel_via_y = round(bus_bottom_y + COLSEL_GAP_BELOW_DBUS, 2)

    # --- Find COL_SEL trunk positions (same logic as preroute_col_sel_vias) ---
    net_trunk_bottoms = {}  # net_num -> (trunk_x, max_y)

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        if not ref.startswith("U"):
            continue
        if ref_to_part.get(ref) != "74LVC2G00":
            continue

        nand_x, nand_y = fp.position.X, fp.position.Y
        byte_x = round(nand_x - NAND_X_NUDGE, 2)
        byte_y = round(nand_y + 0.25, 2)

        pin1_net = pcb.get_pad_net(ref, "1")
        if not pin1_net:
            continue

        net = pin1_net
        trunk_x = round(byte_x + TRUNK_X_OFFSET, 2)
        # Trunk point Y from preroute_col_sel_vias: via5 at courtyard bottom
        crtyd_bot_y = round(byte_y + 0.55, 2)

        if net not in net_trunk_bottoms:
            net_trunk_bottoms[net] = (trunk_x, crtyd_bot_y)
        else:
            prev_tx, prev_max_y = net_trunk_bottoms[net]
            net_trunk_bottoms[net] = (prev_tx, max(prev_max_y, crtyd_bot_y))

    # --- Extend each trunk down to colsel_via_y and place via ---
    for net, (trunk_x, trunk_bottom_y) in net_trunk_bottoms.items():
        # Vertical In1.Cu extension from trunk bottom to below D* bus
        pcb.add_trace((trunk_x, trunk_bottom_y), (trunk_x, colsel_via_y),
                      net, SIGNAL_TRACE_W, "In1.Cu")
        traces += 1

        # Via: In1.Cu → F.Cu at the terminus
        pcb.add_via((trunk_x, colsel_via_y), net,
                    VIA_SIZE, VIA_DRILL, ["F.Cu", "In1.Cu"])
        vias += 1

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

    # Step 1: Create custom footprints
    print("\n[1/7] Creating custom footprints...")
    fp5_path, fp6_path, fp8_path = create_dsbga_footprints(SHARED_FP_DIR)
    print(f"  Created: {os.path.basename(fp5_path)}")
    print(f"  Created: {os.path.basename(fp6_path)}")
    print(f"  Created: {os.path.basename(fp8_path)}")

    # Power supply footprints
    POWER_FP_DIR = os.path.join(SHARED_FP_DIR, "..", "Power_Discrete.pretty")
    tps_fp = create_tps546d24a_footprint(POWER_FP_DIR)
    print(f"  Created: {os.path.basename(tps_fp)}")
    smd_conn_fp = create_smd_power_connector_footprint(POWER_FP_DIR)
    print(f"  Created: {os.path.basename(smd_conn_fp)}")

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
    pcb.add_fp_lib_path("Power_Discrete", POWER_FP_DIR)

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
    # Byte layout Y span: NAND IC at -0.25 to NAND R at 2.55 = 2.80mm
    # byte_row_h = Y_span + BYTE_CELL_H (from compute_group_size)
    _byte_row_h_est = 2.80 + BYTE_CELL_H
    _rc_stride = _byte_row_h_est + GROUP_GAP_Y  # matches byte row stride
    _addr_dec_final_ys = None  # set during addr_decoder layout

    def _place_col(cells, col_x, total_h, cell_h, placements, ys=None):
        """Place cells vertically in a column, centered within total_h."""
        n = len(cells)
        if ys is not None:
            for i, (ic, r, led) in enumerate(cells):
                x, y = col_x, ys[i]
                if ic is not None:
                    placements.append((ic, x, y))
                if led:
                    placements.append((led, x + LED_OFFSET_X, y))
                if r:
                    r_tagged = dict(r, angle_override=90)
                    placements.append((r_tagged, x + LED_OFFSET_X + R_HORIZ_OFFSET, y))
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
                    r_tagged = dict(r, angle_override=90)
                    placements.append((r_tagged, x + LED_OFFSET_X + R_HORIZ_OFFSET, y))

    for name, comps in groups.items():
        # Power supply has its own placement logic (after the main layout loop)
        if name == "power_supply":
            continue
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
            max_cols = 1  # Vertical: write on top, read below (custom layout)
        else:
            max_cols = 3

        # RAM bytes use tight spacing; control logic uses wider spacing
        if is_ctrl:
            cw, ch = CTRL_CELL_W, CTRL_CELL_H
        elif is_ram:
            cw, ch = BYTE_CELL_W, BYTE_CELL_H
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

            placements = []
            _place_col(inv_cells,            0 * col_sp, total_h, cell_h, placements)
            _place_col(g_cells + hahb_cells, 1 * col_sp, total_h, cell_h, placements)
            _place_col(dec3_cells,           2 * col_sp, total_h, cell_h, placements)
            _place_col(dec4_cells,           3 * col_sp, total_h, cell_h, placements)
            _place_col(final_cells,          4 * col_sp, total_h, cell_h, placements,
                       ys=_addr_dec_final_ys)

            group_cell_dims[name] = (col_sp, cell_h)

        # Custom column_select layout: vertical columns, left-to-right decode
        # Col 0: 4 INVs (address inverters), inline LED+R
        # Col 1: 8 Level-1 ANDs (GA0-3, GB0-3 intermediates), inline LED+R
        # Col 2: 16 Level-2 ANDs (COL_SEL_0-15 outputs), inline LED+R
        elif name == "column_select":
            inv_cells = [c for c in ic_cells if c[0] is not None and c[0]["part"] == "74LVC1G04"]
            and_cells = [c for c in ic_cells if c[0] is not None and c[0]["part"] != "74LVC1G04"]
            level1_ands = and_cells[:8]   # GA0-3, GB0-3
            level2_ands = and_cells[8:]   # COL_SEL_0-15

            cell_h = CTRL_CELL_H   # 4.0mm vertical spacing within columns
            col_sp = 7.5           # horizontal spacing between columns

            # Level-2 column (tallest, 16 cells) determines total height
            l2_span = (len(level2_ands) - 1) * cell_h
            total_h = l2_span

            placements = []
            _place_col(inv_cells,    0 * col_sp, total_h, cell_h, placements)
            _place_col(level1_ands,  1 * col_sp, total_h, cell_h, placements)
            _place_col(level2_ands,  2 * col_sp, total_h, cell_h, placements)

            # Rotate entire group 90° CW: (x, y) → (y, max_x - x)
            # Puts L2 outputs (rightmost col) at top (closest to RAM)
            # and INV inputs (leftmost col) at bottom
            _DEFAULT_ANGLES = {
                "LED_Small": 90, "R_Small": 270, "74LVC1G04": 180,
                "74LVC1G08": 180,
            }
            max_x_cs = max(x for _, x, _ in placements)
            rotated = []
            for comp, x, y in placements:
                new_x = round(y, 2)
                new_y = round(max_x_cs - x, 2)
                comp_r = dict(comp)
                cur = comp_r.get("angle_override",
                                _DEFAULT_ANGLES.get(comp_r.get("part", ""), 0))
                comp_r["angle_override"] = (cur + 90) % 360
                rotated.append((comp_r, new_x, new_y))
            placements = rotated

            group_cell_dims[name] = (cell_h, col_sp)  # swapped after rotation

        # Custom row_ctrl layout: stack write + read AND gates vertically
        # (on top of each other) so they align with the enable buses
        # that cross the byte area horizontally.
        elif name.startswith("row_ctrl_"):
            RC_GATE_SPACING = 2.3  # mm between write and read AND centers
            placements = []
            for idx, (ic, r, led) in enumerate(ic_cells):
                y = round(idx * RC_GATE_SPACING, 2)
                if ic is not None:
                    placements.append((ic, 0, y))
                if led:
                    placements.append((led, LED_OFFSET_X, y))
                if r:
                    r_tagged = dict(r, angle_override=90)
                    placements.append((r_tagged,
                                       LED_OFFSET_X + R_HORIZ_OFFSET, y))
            group_cell_dims[name] = (CTRL_CELL_W, RC_GATE_SPACING)

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
                    elif comp["part"].startswith("Conn_01x"):
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
    # Layout: Connector | addr_decoder(5 cols) | row_ctrl(x4) | RAM
    #         Above RAM: J2/J3/J4 connectors + layer_test grid
    #         Below RAM: column_select; Below addr_dec: control_logic
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
    # Shift all logic groups down to make room for power supply section at top
    PWR_SECTION_H = 28.0  # power supply section height (connector + caps + bypass)
    col1_y = PLACEMENT_ORIGIN + PWR_SECTION_H
    dec_abs_y = col1_y  # addr_decoder at top of col 1

    # Col 2: row_ctrl, Y-aligned with addr_decoder final ANDs
    col2_x = col1_x + dec_w + GROUP_GAP_X
    col2_y = dec_abs_y  # same Y start

    # Col 3: RAM bytes (2×4 grid, vertically centered with addr_decoder)
    RC_TO_RAM_GAP = 1.0  # tight gap — enable bus traces bridge the distance
    ram_x = col2_x + rc_w + RC_TO_RAM_GAP
    ram_y = round(dec_abs_y + (dec_h - ram_total_h) / 2, 2)

    # Ensure room above RAM for test grid + connectors (must fit within sheet)
    _tg_h = TEST_TITLE_H + TEST_HEADER_H + 6 * (TEST_CELL_H + TEST_CELL_GAP)
    _needed_above = _tg_h + 3.0 + 5.0 + 3.0  # test grid + gap + connectors + margin
    _available_above = ram_y - (SHEET_BORDER + BOARD_MARGIN)
    if _available_above < _needed_above:
        _y_shift = round(_needed_above - _available_above, 2)
        col1_y += _y_shift
        dec_abs_y += _y_shift
        col2_y += _y_shift
        ram_y += _y_shift

    # Control logic below addr_decoder
    ctrl_abs_x = col1_x
    ctrl_abs_y = round(dec_abs_y + dec_h + GROUP_GAP_Y * 3, 2)

    # Compute total board content height
    total_content_h = max(dec_h + GROUP_GAP_Y * 3 + ctrl_h,
                          ram_total_h + GROUP_GAP_Y * 3 + 20.0 + colsel_h + 10.0)

    # Col 0: connector bottom-justified with the board bottom edge.
    # Pre-compute the bottom extent from known group positions.
    COLSEL_BOTTOM_PAD = 10.0  # routing buffer below column_select
    _colsel_bottom = ram_y + ram_total_h + GROUP_GAP_Y * 3 + 20.0 + colsel_h + COLSEL_BOTTOM_PAD
    board_bottom_y = max(
        ctrl_abs_y + ctrl_h,                                          # control_logic
        _colsel_bottom,                                               # column_select
    )
    col0_x = PLACEMENT_ORIGIN
    col0_y = round(board_bottom_y - root_h, 2)

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
    label_x = round(col0_x - 4.5, 2)
    n_conn_pins = max(conn_pin_names.keys())
    for pin_num, pin_name in conn_pin_names.items():
        label_y = round(col0_y + (n_conn_pins - 1 - pin_num) * CONN_PIN_PITCH, 2)
        pcb.add_silkscreen_text(pin_name, label_x, label_y, size=1.0,
                                justify="left")

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

    # Add unified silkscreen grid around all 8 bytes.
    # Grid is fully defined by byte dimensions and strides — no placed-bounds needed.
    row_stride = byte_row_h + GROUP_GAP_Y
    col_stride = byte_center_span_x + 0.5 + BYTE_COL_GAP + 0.75

    # Byte content span including enable bus traces.
    # Enable buses extend beyond components: WRITE_EN above CLK bus, READ_EN below OE bus.
    # CLK bus at byte_y - 1.25, WRITE_EN at byte_y - 1.65 (offset from byte_min_y=-0.25: -1.40)
    # OE bus at byte_y + 3.15, READ_EN at byte_y + 3.55 (offset from byte_max_y=2.55: +1.0)
    WRITE_EN_BUS_REL_Y = -1.65   # relative to byte origin (CLK_BUS_Y_OFFSET + WRITE_EN_OFFSET)
    READ_EN_BUS_REL_Y = BUF_ROW_Y + 1.4 + 0.40  # OE_bus + READ_EN_OFFSET

    ref_layout = group_layouts.get("byte_0", [])
    byte_min_x = min(x for _, x, _ in ref_layout)
    byte_max_x = max(x for _, x, _ in ref_layout)
    byte_min_y = min(min(y for _, x, y in ref_layout), WRITE_EN_BUS_REL_Y)
    byte_max_y = max(max(y for _, x, y in ref_layout), READ_EN_BUS_REL_Y)
    content_span_x = byte_max_x - byte_min_x
    content_span_y = byte_max_y - byte_min_y

    # Uniform margins: content centered within each col_stride × row_stride cell
    margin_x = (col_stride - content_span_x) / 2
    margin_y = (row_stride - content_span_y) / 2

    # Grid origin: byte_0 origin is (ram_x, ram_y), content starts at +byte_min_x/y
    grid_x1 = round(ram_x + byte_min_x - margin_x, 2)
    grid_y1 = round(ram_y + byte_min_y - margin_y, 2)
    grid_x2 = round(grid_x1 + 2 * col_stride, 2)
    grid_y2 = round(grid_y1 + 4 * row_stride, 2)
    grid_w = round(grid_x2 - grid_x1, 2)
    grid_h = round(grid_y2 - grid_y1, 2)

    pcb.add_silkscreen_rect(grid_x1, grid_y1, grid_w, grid_h)

    # Vertical divider between byte columns
    div_x = round(grid_x1 + col_stride, 2)
    pcb.add_silkscreen_line(div_x, grid_y1, div_x, grid_y2)

    # Horizontal dividers at row_stride intervals
    for k in range(1, 4):
        div_y = round(grid_y1 + k * row_stride, 2)
        pcb.add_silkscreen_line(grid_x1, div_y, grid_x2, div_y)

    # Address labels: centered within each grid cell
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
    # Pre-compute test grid position (centered above RAM)
    test_grid_w_est = TEST_LABEL_W + 5 * (TEST_CELL_W + TEST_CELL_GAP)
    test_grid_h_est = TEST_TITLE_H + TEST_HEADER_H + 6 * (TEST_CELL_H + TEST_CELL_GAP)
    test_x = round(ram_x + (ram_total_w - test_grid_w_est) / 2, 2)
    test_y = round(ram_y - test_grid_h_est - 3.0, 2)
    # Y for extra connectors (J2/J3/J4) — above the test grid
    conn_above_y = round(test_y - 5.0, 2)
    ram_center_x = ram_x + ram_total_w / 2
    colsel_x = round(ram_center_x - colsel_w / 2, 2)
    colsel_y = round(ram_y + ram_total_h + GROUP_GAP_Y * 3 + 20.0, 2)
    if "column_select" in group_layouts:
        for comp, rel_x, rel_y in group_layouts["column_select"]:
            _place_component(pcb, comp, colsel_x + rel_x, colsel_y + rel_y,
                             netlist_data)
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
            # DEC3 unused header above test grid, horizontal (90°), right of DEC4
            dec4_span = 15 * CONN_PIN_PITCH  # 16-pin connector span
            j2_x = round(ram_x + dec4_span + 5.0, 2)
            j2_y = conn_above_y
            _place_component(pcb, comp, j2_x, j2_y, netlist_data,
                             angle_override=90, layer_override="F.Cu")
            total_placed += 1
            pcb.add_silkscreen_text("DEC3", round(j2_x + pin_span / 2, 2),
                                    round(j2_y - 3.0, 2), size=1.0)
        elif ref == "J3":
            # COL_SEL unused header right of RAM, vertical (0°), pin 1 at top
            j3_x = round(ram_x + ram_total_w + GROUP_GAP_X + 3.0, 2)
            j3_y = round(ram_y, 2)
            _place_component(pcb, comp, j3_x, j3_y, netlist_data,
                             angle_override=0, layer_override="F.Cu")
            total_placed += 1
            pcb.add_silkscreen_text("COL_SEL", round(j3_x + 3.5, 2),
                                    round(j3_y + pin_span / 2, 2), size=1.0)
        elif ref == "J4":
            # DEC4 unused header above test grid, horizontal (90°), leftmost
            j4_x = round(ram_x, 2)
            j4_y = conn_above_y
            _place_component(pcb, comp, j4_x, j4_y, netlist_data,
                             angle_override=90, layer_override="F.Cu")
            total_placed += 1
            pcb.add_silkscreen_text("DEC4", round(j4_x + pin_span / 2, 2),
                                    round(j4_y - 3.0, 2), size=1.0)
        else:
            _place_component(pcb, comp, round(colsel_x + 20, 2),
                             round(colsel_y + colsel_h + GROUP_GAP_Y * 5, 2),
                             netlist_data, angle_override=0)
            total_placed += 1

    # ================================================================
    # Power supply section (top-left corner)
    #
    # TPS546D24A datasheet layout (Figure 10-1):
    #   Input caps → PVIN pins → IC → SW pins → inductor → output caps
    #
    # IC at 180° rotation so:
    #   PVIN (pins 19-25) faces LEFT → toward input caps
    #   SW (pins 8-11) faces RIGHT → toward inductor
    #   PGND (pins 12-18) at TOP → thermal vias to inner GND plane
    #   VDD5 (pin 28) at LEFT → bypass cap nearby
    #
    # Copper pours: PVIN (F.Cu left), PGND (F.Cu center/top), VOUT (F.Cu right)
    # SW area: MINIMAL copper, with keepout zone around it
    # ================================================================
    if "power_supply" in groups:
        pwr_comps = groups["power_supply"]
        pwr_by_ref = {c["ref"]: c for c in pwr_comps if not c["ref"].startswith("#")}

        px = PLACEMENT_ORIGIN  # left edge
        py = PLACEMENT_ORIGIN  # top edge
        FP_0805 = "Capacitor_SMD:C_0805_2012Metric"

        def _is_bulk_cap(comp):
            v = comp.get("value", "")
            return "uF" in v and "100nF" not in v and "1uF" not in v

        # ---- U226 center position ----
        reg_x = round(px + 42.0, 2)
        reg_y = round(py + 10.0, 2)

        # ---- J5: PCIe 8-pin connector (left edge, vertical) ----
        j5 = pwr_by_ref.get("J5")
        if j5:
            _place_component(pcb, j5, round(px + 8.0, 2), round(py + 12.0, 2),
                             netlist_data, angle_override=90, layer_override="F.Cu")
            total_placed += 1

        # ---- U226: TPS546D24A at 180° (PVIN left, SW right) ----
        u_reg = pwr_by_ref.get("U226")
        if u_reg:
            _place_component(pcb, u_reg, reg_x, reg_y, netlist_data,
                             angle_override=180)
            total_placed += 1

        # At 180° rotation, pin positions flip:
        #   PVIN (was bottom-right) → now at LEFT (x≈59.6) and TOP (y≈21.6)
        #   SW (was left) → now at RIGHT (x≈64.4)
        #   PGND (was left+bottom) → now at RIGHT + TOP
        #   VDD5 (was right) → now at LEFT
        #   BOOT (was left) → now at RIGHT (near SW)

        # ---- Input filter caps (left of IC, near PVIN pins) ----
        cin_x = round(px + 26.0, 2)
        cin_y = round(reg_y, 2)
        for i, ref in enumerate(["C1", "C2"]):
            comp = pwr_by_ref.get(ref)
            if comp:
                _place_component(pcb, comp, round(cin_x + i * 4.0, 2), cin_y,
                                 netlist_data, angle_override=90, fp_override=FP_0805)
                total_placed += 1
        # C3 (100nF PVIN HF): tight against IC left side
        comp = pwr_by_ref.get("C3")
        if comp:
            _place_component(pcb, comp, round(reg_x - 4.5, 2), round(reg_y - 1.0, 2),
                             netlist_data, angle_override=90)
            total_placed += 1

        # ---- L1: inductor (right of IC, connecting to SW pins) ----
        l1_x = round(reg_x + 14.0, 2)
        l1_y = round(reg_y, 2)
        l1 = pwr_by_ref.get("L1")
        if l1:
            _place_component(pcb, l1, l1_x, l1_y, netlist_data, angle_override=0)
            total_placed += 1

        # ---- Output filter caps (right of inductor) ----
        cout_x = round(l1_x + 12.0, 2)
        # C7-C9 (47µF bulk): top row
        for i, ref in enumerate(["C7", "C8", "C9"]):
            comp = pwr_by_ref.get(ref)
            if comp:
                _place_component(pcb, comp, round(cout_x + i * 4.0, 2),
                                 round(reg_y - 3.0, 2), netlist_data,
                                 angle_override=90, fp_override=FP_0805)
                total_placed += 1
        # C10 (47µF) + C11 (100nF HF): bottom row
        for i, ref in enumerate(["C10", "C11"]):
            comp = pwr_by_ref.get(ref)
            if comp:
                _place_component(pcb, comp, round(cout_x + i * 4.0, 2),
                                 round(reg_y + 3.0, 2), netlist_data,
                                 angle_override=90,
                                 fp_override=FP_0805 if _is_bulk_cap(comp) else None)
                total_placed += 1

        # ---- IC bypass caps (tight against QFN) ----
        # C4 (BOOT 100nF): RIGHT side near BOOT/SW pins (after 180° rotation)
        comp = pwr_by_ref.get("C4")
        if comp:
            _place_component(pcb, comp, round(reg_x + 4.5, 2), round(reg_y - 1.0, 2),
                             netlist_data, angle_override=90)
            total_placed += 1
        # C5 (VDD5 4.7µF): LEFT side near VDD5 pin (after 180° rotation)
        comp = pwr_by_ref.get("C5")
        if comp:
            _place_component(pcb, comp, round(reg_x - 4.5, 2), round(reg_y + 2.0, 2),
                             netlist_data, angle_override=90,
                             fp_override=FP_0805 if _is_bulk_cap(comp) else None)
            total_placed += 1
        # C6 (BP1V5 1µF): RIGHT side near BP1V5 pin (after 180° rotation)
        comp = pwr_by_ref.get("C6")
        if comp:
            _place_component(pcb, comp, round(reg_x + 4.5, 2), round(reg_y + 2.0, 2),
                             netlist_data, angle_override=90)
            total_placed += 1

        # ---- EN divider resistors (left of IC, near AVIN/EN pins) ----
        for i, ref in enumerate(["R192", "R193"]):
            comp = pwr_by_ref.get(ref)
            if comp:
                _place_component(pcb, comp, round(reg_x - 8.0, 2),
                                 round(reg_y + 4.0 + i * 4.0, 2),
                                 netlist_data, angle_override=0)
                total_placed += 1

        # ---- PGOOD pull-up + LED indicator (below IC) ----
        pgood_y = round(reg_y + 14.0, 2)
        for i, ref in enumerate(["R194", "D192", "R195"]):
            comp = pwr_by_ref.get(ref)
            if comp:
                _place_component(pcb, comp, round(reg_x - 4.0 + i * 4.0, 2),
                                 pgood_y, netlist_data, angle_override=90)
                total_placed += 1

        print(f"  Power supply: {len(pwr_by_ref)} components placed")

        # ---- Thermal vias under exposed pad (pin 41) ----
        # 2x3 grid within the 3.3mm x 5.3mm thermal pad
        gnd_net = pcb.get_net_number("GND")
        if gnd_net is not None:
            via_spacing_x = 1.28  # from datasheet stencil pattern
            via_spacing_y = 1.40
            pwr_thermal_vias = 0
            for row in range(-1, 2):      # -1, 0, 1 → 3 rows
                for col in range(-1, 1):   # -1, 0 → 2 columns
                    vx = round(reg_x + col * via_spacing_x + via_spacing_x / 2, 2)
                    vy = round(reg_y + row * via_spacing_y, 2)
                    pcb.add_via((vx, vy), gnd_net, size=0.6, drill=0.3,
                                layers=["F.Cu", "B.Cu"])
                    pwr_thermal_vias += 1
            print(f"  Power thermal vias: {pwr_thermal_vias}")

        # ---- Copper pour zones ----
        # PVIN pour: left of IC covering input caps to PVIN pins
        pvin_net_num = pcb.get_net_number("/Power Supply/+12V")
        if pvin_net_num is not None:
            pvin_outline = [
                (cin_x - 3.0, reg_y - 5.5),
                (reg_x - 2.0, reg_y - 5.5),
                (reg_x - 2.0, reg_y + 5.5),
                (cin_x - 3.0, reg_y + 5.5),
            ]
            pcb.add_zone("/Power Supply/+12V", "F.Cu", pvin_outline,
                         clearance=0.3, pad_connection="yes", priority=1)
            print("  Added PVIN copper pour (F.Cu)")

        # VOUT pour: right of inductor through output caps
        vcc_net_num = pcb.get_net_number("VCC")
        if vcc_net_num is not None:
            vout_outline = [
                (l1_x + 3.0, reg_y - 6.0),
                (cout_x + 12.0, reg_y - 6.0),
                (cout_x + 12.0, reg_y + 6.0),
                (l1_x + 3.0, reg_y + 6.0),
            ]
            pcb.add_zone("VCC", "F.Cu", vout_outline,
                         clearance=0.3, pad_connection="yes", priority=1)
            print("  Added VOUT copper pour (F.Cu)")

        # PGND pour: around thermal pad area, connecting PGND pins to vias
        if gnd_net is not None:
            pgnd_outline = [
                (reg_x - 3.0, reg_y - 4.0),
                (reg_x + 3.0, reg_y - 4.0),
                (reg_x + 3.0, reg_y + 4.0),
                (reg_x - 3.0, reg_y + 4.0),
            ]
            pcb.add_zone("GND", "F.Cu", pgnd_outline,
                         clearance=0.3, pad_connection="yes", priority=2)
            print("  Added PGND copper pour (F.Cu)")

        # SW keepout: prevent copper fill near SW node (minimize area)
        sw_keepout = [
            (reg_x + 2.5, reg_y - 3.0),
            (l1_x - 3.0, reg_y - 3.0),
            (l1_x - 3.0, reg_y + 3.0),
            (reg_x + 2.5, reg_y + 3.0),
        ]
        pcb.add_keepout_zone("F.Cu", sw_keepout)
        # Allow footprints in SW keepout (only block copper pour)
        sw_kz = pcb.board.zones[-1]
        sw_kz.keepoutSettings.footprints = 'allowed'
        print("  Added SW keepout zone (F.Cu)")

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

    enable_traces = preroute_enable_buses(pcb, netlist_data)
    print(f"  Enable buses (F.Cu): {enable_traces} trace segments")

    nand_vias, nand_traces = preroute_nand_connections(pcb, netlist_data)
    print(f"  NAND local: {nand_vias} vias, {nand_traces} traces")

    nand_led_vias, nand_led_traces = preroute_nand_leds(pcb, netlist_data)
    print(f"  NAND LEDs: {nand_led_vias} vias, {nand_led_traces} traces")

    colsel_traces = preroute_column_select(pcb, netlist_data)
    print(f"  Column select: {colsel_traces} traces")

    cs_vias, cs_traces = preroute_col_sel_vias(pcb, netlist_data)
    print(f"  COL_SEL vias: {cs_vias} vias, {cs_traces} traces")

    # Connect DFF-BUF GND pins with F.Cu trace + via to B.Cu GND plane
    dff_buf_gnd_vias, dff_buf_gnd_traces = preroute_dff_buf_gnd(pcb, netlist_data)
    print(f"  DFF-BUF GND: {dff_buf_gnd_vias} vias, {dff_buf_gnd_traces} traces")

    # Connect DFF Q to BUF A via In1.Cu jumper (mirrored from GND trace)
    dff_buf_data_vias, dff_buf_data_traces = preroute_dff_buf_data(pcb, netlist_data)
    print(f"  DFF-BUF data: {dff_buf_data_vias} vias, {dff_buf_data_traces} traces")

    # Connect DFF VCC to BUF VCC with single shared via to In2.Cu
    dff_buf_vcc_vias, dff_buf_vcc_traces = preroute_dff_buf_vcc(pcb, netlist_data)
    print(f"  DFF-BUF VCC: {dff_buf_vcc_vias} vias, {dff_buf_vcc_traces} traces")

    # Route DFF Q (pin 4) to BUF A (pin 2) via In1.Cu vias
    dff_buf_q_vias, dff_buf_q_traces = preroute_dff_to_buffer(pcb, netlist_data)
    print(f"  DFF-BUF Q->A: {dff_buf_q_vias} vias, {dff_buf_q_traces} traces")

    # Connect R GND pads to B.Cu GND plane via local vias
    r_gnd_vias, r_gnd_traces = preroute_r_gnd(pcb, netlist_data)
    print(f"  R GND vias: {r_gnd_vias} vias, {r_gnd_traces} traces")

    conn_traces = preroute_connector_leds(pcb, netlist_data)
    print(f"  Connector->LED + fanout stubs: {conn_traces} trace segments")

    # D* data bus column trunks on In1.Cu (connects bytes vertically)
    dbus_traces = preroute_column_dbus(pcb, netlist_data)
    print(f"  D* column trunks (In1.Cu): {dbus_traces} trace segments")

    # D* data bus fanout below byte grid (connects columns + extends left)
    dbus_fan_vias, dbus_fan_traces = preroute_dbus_fanout(pcb, netlist_data)
    print(f"  D* bus fanout (In1.Cu): {dbus_fan_vias} vias, {dbus_fan_traces} traces")

    # COL_SEL trunks extended below D* bus with termination vias
    cs_fan_vias, cs_fan_traces = preroute_colsel_fanout(pcb, netlist_data)
    print(f"  COL_SEL fanout: {cs_fan_vias} vias, {cs_fan_traces} traces")

    # D* data bus from fanout to connector (F.Cu around decoder/ctrl)
    # Must run AFTER colsel_fanout so via avoidance can see COL_SEL vias
    dbus_conn_traces = preroute_dbus_to_connector(pcb, netlist_data)
    print(f"  D* bus->connector (F.Cu): {dbus_conn_traces} trace segments")

    # Column address A7-A10 from connector to column_select inverters (F.Cu)
    coladdr_traces = preroute_coladdr_to_colsel(pcb, netlist_data)
    print(f"  A7-A10->colsel (F.Cu): {coladdr_traces} trace segments")

    total_vias = (pwr_vias + cs_vias + nand_vias + nand_led_vias
                  + dff_buf_gnd_vias + dff_buf_data_vias + dff_buf_q_vias
                  + r_gnd_vias + dbus_fan_vias + cs_fan_vias)
    total_traces = (pwr_traces + ic_led_traces + led_r_traces + clk_traces
                    + oe_traces + nand_traces + nand_led_traces
                    + dff_buf_gnd_traces
                    + dff_buf_data_traces + dff_buf_q_traces + r_gnd_traces
                    + colsel_traces + cs_traces + conn_traces + dbus_traces
                    + dbus_fan_traces + dbus_conn_traces
                    + coladdr_traces + cs_fan_traces)
    print(f"  Total pre-routed: {total_vias} vias + {total_traces} traces")

    # Layer visibility test grid (for clear PCB) — centered above RAM
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
        comp_min_x = min(comp_min_x, test_x)
        comp_max_x = max(comp_max_x, test_x + test_grid_w)
        comp_min_y = min(comp_min_y, test_y)
        comp_max_y = max(comp_max_y, test_y + test_grid_h)

        # Extend board bounds for connector pin name labels (right-justified,
        # so text extends right from anchor; anchor is leftmost point)
        comp_min_x = min(comp_min_x, label_x)

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


def _place_component(pcb, comp, x, y, netlist_data, angle_override=None,
                     layer_override=None, fp_override=None):
    """Place a single component on the PCB.

    Determines the correct footprint from the part name and assigns nets.
    angle_override: if not None, overrides the default angle for this part type.
    layer_override: if not None, overrides the default layer for this part type.
    fp_override: if not None, overrides the footprint for this specific component.
    """
    ref = comp["ref"]
    part = comp["part"]

    # Check for angle_override embedded in the component dict
    if angle_override is None and "angle_override" in comp:
        angle_override = comp["angle_override"]
    tstamp = comp["tstamp"]

    # Determine footprint
    fp_ref = fp_override if fp_override else get_footprint_for_part(part)
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
    if layer_override is not None:
        layer = layer_override
    elif part.startswith("Conn_01x") or part.startswith("Conn_02x"):
        layer = "B.Cu"  # Default: soldered on back side
    else:
        layer = "F.Cu"

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
            angle = 270  # Dual NAND: rotated 180° from 90°, VCC/GND down (+Y)
        elif "74LVC" in part:
            angle = 180  # Other logic (INV, AND, NAND) unchanged
        elif part.startswith("Conn_01x") or part.startswith("Conn_02x"):
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
    # and fix 3D model rotation (B.Cu mirror causes 180° visual flip)
    if (part.startswith("Conn_01x") or part.startswith("Conn_02x")) and layer == "B.Cu":
        fp = pcb.board.footprints[-1]  # just placed
        for gi in fp.graphicItems:
            if hasattr(gi, 'layer') and gi.layer == "B.SilkS":
                gi.layer = "F.SilkS"
            if hasattr(gi, 'layer') and gi.layer == "B.Fab":
                gi.layer = "F.Fab"
            if hasattr(gi, 'layer') and gi.layer == "B.CrtYd":
                gi.layer = "F.CrtYd"
        for model in fp.models:
            model.rotate.Z = (model.rotate.Z + 180) % 360


if __name__ == "__main__":
    sys.exit(main())
