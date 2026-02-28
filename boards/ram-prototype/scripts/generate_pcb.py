#!/usr/bin/env python3
"""
Generate KiCad PCB layout for the 8-byte discrete RAM prototype.

Places all 512 components (161 ICs, 175 LEDs, 175 resistors, 1 connector)
in a grouped layout matching the schematic hierarchy.  After placement,
pre-routes repetitive local connections (power vias, IC→R→LED chains,
DFF Q→Buffer A) to reduce autorouter workload.

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
  [IC] → [R] → [LED]

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
IC_CELL_W = 7.0     # horizontal spacing between IC centers (IC+R+LED ~6.4mm)
IC_CELL_H = 4.5     # vertical spacing between IC rows (DSBGA courtyard ~3.4mm)
LED_OFFSET_X = 2.5  # R center offset from IC center
LED_OFFSET_X2 = 4.5 # LED center offset from IC center

# Group layout spacing (mm)
GROUP_GAP_X = 10.0   # horizontal gap between groups
GROUP_GAP_Y = 12.0   # vertical gap between group rows
BOARD_MARGIN = 6.0   # margin from board edge to components
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
    """Sort components: ICs first (by ref number), then their R+LED pairs.

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

    # Sort by reference number
    def ref_num(c):
        ref = c["ref"]
        prefix = ref.rstrip("0123456789")
        return int(ref[len(prefix):]) if ref[len(prefix):] else 0

    ics.sort(key=ref_num)
    rs.sort(key=ref_num)
    leds.sort(key=ref_num)

    # Match ICs with their R+LED pairs via shared nets
    # Each IC output drives R -> LED -> GND
    ic_cells = []
    used_rs = set()
    used_leds = set()

    for ic in ics:
        # Find R connected to this IC's output
        ic_nets = set(ic["pins"].values())
        matched_r = None
        for r in rs:
            if r["ref"] in used_rs:
                continue
            r_nets = set(r["pins"].values())
            if ic_nets & r_nets:
                matched_r = r
                used_rs.add(r["ref"])
                break

        # Find LED connected to this R
        matched_led = None
        if matched_r:
            r_nets = set(matched_r["pins"].values())
            for led in leds:
                if led["ref"] in used_leds:
                    continue
                led_nets = set(led["pins"].values())
                if r_nets & led_nets:
                    matched_led = led
                    used_leds.add(led["ref"])
                    break

        ic_cells.append((ic, matched_r, matched_led))

    # Standalone R+LED pairs (root sheet bus LEDs)
    standalone = []
    for r in rs:
        if r["ref"] not in used_rs:
            # Find matching LED
            r_nets = set(r["pins"].values())
            matched_led = None
            for led in leds:
                if led["ref"] not in used_leds:
                    led_nets = set(led["pins"].values())
                    if r_nets & led_nets:
                        matched_led = led
                        used_leds.add(led["ref"])
                        break
            standalone.append((r, matched_led))

    return ic_cells, standalone, others


# --------------------------------------------------------------
# Layout computation
# --------------------------------------------------------------

def compute_group_layout(ic_cells, standalone, max_cols=4):
    """Compute relative positions for components within a group.

    Returns list of (component, rel_x, rel_y) for all components.
    Each IC cell is laid out as: IC at (0,0), R at (+2.0, 0), LED at (+3.5, 0)
    ICs are arranged in a grid with max_cols columns.
    """
    placements = []
    row, col = 0, 0

    for ic, r, led in ic_cells:
        x = col * IC_CELL_W
        y = row * IC_CELL_H

        placements.append((ic, x, y))
        if r:
            placements.append((r, x + LED_OFFSET_X, y))
        if led:
            placements.append((led, x + LED_OFFSET_X2, y))

        col += 1
        if col >= max_cols:
            col = 0
            row += 1

    # Standalone R+LED pairs below IC grid
    if standalone:
        row += 1
        col = 0
        for r, led in standalone:
            x = col * IC_CELL_W
            y = row * IC_CELL_H

            placements.append((r, x, y))
            if led:
                placements.append((led, x + 1.5, y))

            col += 1
            if col >= max_cols:
                col = 0
                row += 1

    return placements


def compute_group_size(placements):
    """Compute bounding box of a group's placements.

    Returns (width, height) in mm.
    """
    if not placements:
        return (0, 0)

    xs = [x for _, x, y in placements]
    ys = [y for _, x, y in placements]

    return (max(xs) - min(xs) + IC_CELL_W, max(ys) - min(ys) + IC_CELL_H)


# --------------------------------------------------------------
# Pre-routing
# --------------------------------------------------------------

# Via and trace sizing
VIA_SIZE = 0.6       # mm outer diameter
VIA_DRILL = 0.3      # mm drill (KiCad default min)
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
    """Drop vias from every IC GND/VCC pad and LED cathode to inner planes.

    - IC pin 3 (GND) -> via to In1.Cu (GND plane)
    - IC pin 5 (VCC) -> via to In2.Cu (VCC plane)
    - LED/R GND pads  -> via to In1.Cu (GND plane)

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
                # LEDs and Rs: offset downward to avoid crossing into
                # next column's IC courtyard
                via_x = abs_x
                via_y = round(abs_y + VIA_OFFSET, 2)

            # Short stub trace from pad to via
            pcb.add_trace((abs_x, abs_y), (via_x, via_y),
                          net_num, POWER_TRACE_W, "F.Cu")
            traces += 1

            # Via to inner plane
            pcb.add_via((via_x, via_y), net_num,
                        VIA_SIZE, VIA_DRILL, target_layers)
            vias += 1

    return vias, traces


def preroute_r_to_led(pcb, netlist_data):
    """Route R LED-side pad -> LED anode for every IC+R+LED cell.

    Only routes the R-to-LED connection (safe: clear space between R and LED).
    Does NOT route IC output->R (crosses DSBGA pins at 0.5mm pitch).

    Uses net-based matching to find paired components.

    Returns number of trace segments added.
    """
    net_to_pads = _build_net_pad_index(pcb)

    traces = 0

    # For each IC, find its nearest R on the output net, then route R->LED
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

        # Find nearest R pad on the same net (within IC_CELL_W distance)
        pads_on_net = net_to_pads.get(ic_out_net, [])
        r_ref = None
        r_pad_num = None
        best_dist = float("inf")
        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if pad_ref.startswith("R"):
                dist = math.sqrt((px - ic_x)**2 + (py - ic_y)**2)
                if dist < IC_CELL_W and dist < best_dist:
                    best_dist = dist
                    r_ref = pad_ref
                    r_pad_num = pad_num

        if r_ref is None:
            continue

        # Find R's other pad (LED side) and its net
        r_other_pad = "2" if r_pad_num == "1" else "1"
        r_led_net = pcb.get_pad_net(r_ref, r_other_pad)
        if r_led_net is None or r_led_net == 0:
            continue

        # Find nearest LED anode on the R-LED net (within IC_CELL_W of R)
        r_pos = pcb.get_pad_position(r_ref, r_pad_num)
        led_ref = None
        led_pad_num = None
        best_dist = float("inf")
        for pad_ref, pad_num, px, py, pnet in net_to_pads.get(r_led_net, []):
            if pad_ref.startswith("D"):
                dist = math.sqrt((px - r_pos[0])**2 + (py - r_pos[1])**2)
                if dist < IC_CELL_W and dist < best_dist:
                    best_dist = dist
                    led_ref = pad_ref
                    led_pad_num = pad_num

        if led_ref is None:
            continue

        # Route R LED-side pad -> LED anode (horizontal first: safe route
        # through clear space between R and LED, no components in between)
        r_led_pos = pcb.get_pad_position(r_ref, r_other_pad)
        led_anode_pos = pcb.get_pad_position(led_ref, led_pad_num)

        segs = pcb.add_l_trace(r_led_pos, led_anode_pos, r_led_net,
                               SIGNAL_TRACE_W, "F.Cu", horizontal_first=True)
        traces += len(segs)

    return traces


def preroute_dff_to_buffer(pcb, netlist_data):
    """Route DFF Q output to Buffer A input on B.Cu (back copper).

    DFF pin 4 (Q) and Buffer pin 2 (A) share a net.  Routing on F.Cu
    is unsafe (traces cross DSBGA pins at 0.5mm pitch).  Instead:
      - Via down from DFF pin 4 area
      - Trace on B.Cu to below Buffer pin 2
      - Via up to Buffer pin 2 area

    Returns number of trace segments added.
    """
    ref_to_part = {}
    for comp in netlist_data["components"]:
        ref_to_part[comp["ref"]] = comp["part"]

    traces = 0
    vias = 0

    net_to_pads = _build_net_pad_index(pcb)

    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        part = ref_to_part.get(ref, "")
        if part != "74LVC1G79":
            continue

        # DFF Q output net (pin 4)
        q_net = pcb.get_pad_net(ref, "4")
        if q_net is None or q_net == 0:
            continue

        # Find Buffer (74LVC1G125) pin 2 on the same net
        pads_on_net = net_to_pads.get(q_net, [])
        buf_ref = None
        for pad_ref, pad_num, px, py, pnet in pads_on_net:
            if (pad_ref.startswith("U") and
                    ref_to_part.get(pad_ref) == "74LVC1G125" and
                    pad_num == "2"):
                buf_ref = pad_ref
                break

        if buf_ref is None:
            continue

        # Get pad positions
        dff_q_pos = pcb.get_pad_position(ref, "4")
        buf_a_pos = pcb.get_pad_position(buf_ref, "2")

        # DFF pin 4 at (-0.25, +0.5) relative to DFF center
        # Place via to the RIGHT of DFF pin 4 to clear IC body
        # (pin 5/VCC is at +0.25,+0.5 so go further right to +0.75)
        dff_via_x = round(dff_q_pos[0] + 1.0, 2)  # 1mm right of pin 4
        dff_via_y = round(dff_q_pos[1], 2)

        # Buffer pin 2 at (-0.25, 0) relative to Buffer center
        # Place via to the RIGHT of Buffer pin 2
        buf_via_x = round(buf_a_pos[0] + 1.0, 2)
        buf_via_y = round(buf_a_pos[1], 2)

        # F.Cu: DFF pin 4 -> via point (short horizontal escape)
        pcb.add_trace(dff_q_pos, (dff_via_x, dff_via_y),
                      q_net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1

        # Via down to B.Cu at DFF side
        pcb.add_via((dff_via_x, dff_via_y), q_net,
                    VIA_SIZE, VIA_DRILL, ["F.Cu", "B.Cu"])
        vias += 1

        # B.Cu: route from DFF via to Buffer via
        pcb.add_l_trace((dff_via_x, dff_via_y), (buf_via_x, buf_via_y),
                        q_net, SIGNAL_TRACE_W, "B.Cu", horizontal_first=False)
        traces += 2  # L-trace = 2 segments typically

        # Via up to F.Cu at Buffer side
        pcb.add_via((buf_via_x, buf_via_y), q_net,
                    VIA_SIZE, VIA_DRILL, ["F.Cu", "B.Cu"])
        vias += 1

        # F.Cu: via point -> Buffer pin 2 (short horizontal)
        pcb.add_trace((buf_via_x, buf_via_y), buf_a_pos,
                      q_net, SIGNAL_TRACE_W, "F.Cu")
        traces += 1

    return traces


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

    # Configure 4-layer stackup
    pcb.set_4layer_stackup()

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
    for name, comps in groups.items():
        # Determine max columns based on group size
        if name == "root":
            max_cols = 3  # Connector + root LEDs
        elif name.startswith("byte"):
            max_cols = 8  # 8 bits per line (DFFs row + buffers row)
        elif name == "addr_decoder":
            max_cols = 4  # 3 INV + 8 AND3
        else:
            max_cols = 3

        ic_cells, standalone, others = sort_components_for_placement(comps)
        placements = compute_group_layout(ic_cells, standalone, max_cols)

        # Add connector and other non-IC components
        if others:
            if name == "root":
                # Root group: connector on the left, bus LEDs aligned to
                # their matching connector pin Y positions.
                # PinHeader_1x16 pad Y: pin N at (N-1)*2.54mm
                conn_x = 0.0
                r_x = 8.0    # R offset right of connector
                led_x = 10.0  # LED offset right of connector

                # Find connector pin-to-net mapping (excluding power nets)
                j1 = others[0]  # J1 is the only non-R/D/U in root
                pin_y_by_net = {}
                for pin_num, net_name in j1["pins"].items():
                    if net_name not in ("GND", "VCC"):
                        pin_y_by_net[net_name] = (int(pin_num) - 1) * CONN_PIN_PITCH

                # Clear standalone placements and rebuild aligned to pins
                placements = []

                # Place connector
                placements.append((j1, conn_x, 0.0))

                # Place each R+LED pair at its matching connector pin Y
                for r_comp, led_comp in standalone:
                    # R has the signal net that matches a connector pin
                    r_nets = set(r_comp["pins"].values())
                    matched_y = None
                    for net_name in r_nets:
                        if net_name in pin_y_by_net:
                            matched_y = pin_y_by_net[net_name]
                            break

                    if matched_y is not None:
                        placements.append((r_comp, r_x, matched_y))
                        if led_comp:
                            placements.append((led_comp, led_x, matched_y))
                    else:
                        # Fallback (shouldn't happen for bus indicator LEDs)
                        placements.append((r_comp, r_x, 0.0))
                        if led_comp:
                            placements.append((led_comp, led_x, 0.0))
            else:
                for i, comp in enumerate(others):
                    placements.append((comp, 0.0, i * CONN_PIN_PITCH))

        group_layouts[name] = placements
        group_sizes[name] = compute_group_size(placements)

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

    # Compute byte grid dimensions
    byte_col_w = max((group_sizes.get(b, (0, 0))[0] for b in all_bytes), default=0)
    byte_row_h = max((group_sizes.get(b, (0, 0))[1] for b in all_bytes), default=0)

    # Total RAM area height
    ram_total_h = 4 * byte_row_h + 3 * GROUP_GAP_Y

    # Vertically center connector and decode/ctrl column with RAM area
    col0_y = PLACEMENT_ORIGIN + max(0, (ram_total_h - root_h) / 2)
    # Stack decoder + ctrl to fill the height next to RAM
    col1_y = PLACEMENT_ORIGIN

    # Place connector (root)
    if "root" in group_layouts:
        for comp, rel_x, rel_y in group_layouts["root"]:
            _place_component(pcb, comp, col0_x + rel_x, col0_y + rel_y, netlist_data)
            total_placed += 1

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
    for col_idx, byte_col in enumerate([byte_col0, byte_col1]):
        bx = ram_x + col_idx * (byte_col_w + GROUP_GAP_X)
        for row_idx, name in enumerate(byte_col):
            if name not in group_layouts:
                continue
            by = ram_y + row_idx * (byte_row_h + GROUP_GAP_Y)
            for comp, rel_x, rel_y in group_layouts[name]:
                _place_component(pcb, comp, bx + rel_x, by + rel_y, netlist_data)
                total_placed += 1

    # Place control logic below RAM: write_clk_gen + read_oe_gen
    ctrl_row_y = ram_y + ram_total_h + GROUP_GAP_Y
    ctrl_row_x = ram_x
    for name in ["write_clk_gen", "read_oe_gen"]:
        if name not in group_layouts:
            continue
        w, h = group_sizes[name]
        for comp, rel_x, rel_y in group_layouts[name]:
            _place_component(pcb, comp, ctrl_row_x + rel_x, ctrl_row_y + rel_y, netlist_data)
            total_placed += 1
        ctrl_row_x += w + GROUP_GAP_X

    print(f"  Total components placed: {total_placed}")

    # Step 6: Pre-route local connections
    print("\n[6/7] Pre-routing local connections...")
    pcb.build_ref_index()

    pwr_vias, pwr_traces = preroute_power_vias(pcb)
    print(f"  Power vias: {pwr_vias} vias, {pwr_traces} stub traces")

    led_traces = preroute_r_to_led(pcb, netlist_data)
    print(f"  R->LED chains: {led_traces} trace segments")

    # DFF->Buffer routing disabled: escape traces from DSBGA pin 4 cross
    # pin 5 (VCC) at the same Y.  Needs obstacle-aware routing.
    dff_traces = 0

    total_traces = pwr_traces + led_traces + dff_traces
    print(f"  Total pre-routed: {pwr_vias} vias + {total_traces} traces")

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
                abs_x = fp_x + px * cos_a - py * sin_a
                abs_y = fp_y + px * sin_a + py * cos_a
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

    pcb.set_board_outline(board_w, board_h, origin_x, origin_y)
    print(f"  Board outline: {board_w} x {board_h} mm")
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

    # Save PCB
    pcb_path = os.path.join(BOARD_DIR, "ram.kicad_pcb")
    pcb.save(pcb_path)
    print(f"\nSaved: {pcb_path}")

    # Cleanup netlist
    if os.path.exists(net_path):
        os.remove(net_path)

    # Summary
    print(f"\n{'=' * 60}")
    print("PCB Generation Complete")
    print(f"{'=' * 60}")
    print(f"  Components: {total_placed}")
    print(f"  Pre-routed: {pwr_vias} vias + {total_traces} traces")
    print(f"  Board size: {board_w} x {board_h} mm")
    print(f"  Layers: 4 (F.Cu, In1.Cu=GND, In2.Cu=VCC, B.Cu)")
    print()

    return 0


def _place_component(pcb, comp, x, y, netlist_data):
    """Place a single component on the PCB.

    Determines the correct footprint from the part name and assigns nets.
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

    # Determine rotation (LEDs rotated 90° for horizontal chain layout)
    angle = 0
    if part == "LED_Small":
        angle = 180  # Cathode toward GND side
    elif part == "R_Small":
        angle = 90  # Vertical orientation for horizontal chain

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
