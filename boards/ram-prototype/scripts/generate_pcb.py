#!/usr/bin/env python3
"""
Generate KiCad PCB layout for the 8-byte discrete RAM prototype.

Places all 512 components (161 ICs, 175 LEDs, 175 resistors, 1 connector)
in a grouped layout matching the schematic hierarchy. No trace routing --
that will be done manually in KiCad.

Layout:
  +----------------------------------------------------------+
  | CONNECTOR | ADDR DECODER | CTRL LOGIC | WRITE CLK| READ OE|
  |  1x16     | 3 INV+8 AND3 | 3 INV+3AND | 8 NAND   | 8 NAND |
  |  +14 LEDs | +19 LEDs     | +6 LEDs    | +8 LEDs  | +8 LEDs|
  +----------------------------------------------------------+
  | BYTE 0    | BYTE 1       | BYTE 2     | BYTE 3            |
  | 16 ICs    | 16 ICs       | 16 ICs     | 16 ICs            |
  | +10 LEDs  | +10 LEDs     | +10 LEDs   | +10 LEDs          |
  +----------------------------------------------------------+
  | BYTE 4    | BYTE 5       | BYTE 6     | BYTE 7            |
  | 16 ICs    | 16 ICs       | 16 ICs     | 16 ICs            |
  | +10 LEDs  | +10 LEDs     | +10 LEDs   | +10 LEDs          |
  +----------------------------------------------------------+

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
# Main
# --------------------------------------------------------------

def main():
    print("=" * 60)
    print("Discrete NES - RAM Prototype PCB Generator")
    print("=" * 60)

    # Step 1: Create custom DSBGA footprints
    print("\n[1/6] Creating custom DSBGA footprints...")
    fp5_path, fp6_path = create_dsbga_footprints(SHARED_FP_DIR)
    print(f"  Created: {os.path.basename(fp5_path)}")
    print(f"  Created: {os.path.basename(fp6_path)}")

    # Step 2: Export netlist from schematic
    print("\n[2/6] Exporting netlist from schematic...")
    sch_path = os.path.join(BOARD_DIR, "ram.kicad_sch")
    net_path = os.path.join(BOARD_DIR, "ram.xml")
    export_netlist(sch_path, net_path)
    netlist_data = parse_netlist(net_path)
    print(f"  Components: {len(netlist_data['components'])}")
    print(f"  Nets: {len(netlist_data['nets'])}")

    # Step 3: Group components by hierarchy
    print("\n[3/6] Grouping components by hierarchy...")
    groups = group_components(netlist_data)
    for name, comps in sorted(groups.items()):
        print(f"  {name}: {len(comps)} components")

    # Step 4: Initialize PCB builder
    print("\n[4/6] Initializing PCB...")
    pcb = PCBBuilder(title="8-Byte Discrete RAM Prototype")
    pcb.add_fp_lib_path("DSBGA_Packages", SHARED_FP_DIR)

    # Register all nets
    pcb.add_nets_from_netlist(netlist_data)

    # Configure 4-layer stackup
    pcb.set_4layer_stackup()

    # Step 5: Place components
    print("\n[5/6] Placing components...")

    # Define group layout order and positions
    # Row 0: connector, addr_decoder, control_logic, write_clk_gen, read_oe_gen
    # Row 1: byte_0, byte_1, byte_2, byte_3
    # Row 2: byte_4, byte_5, byte_6, byte_7

    group_order_row0 = ["root", "addr_decoder", "control_logic",
                        "write_clk_gen", "read_oe_gen"]
    group_order_row1 = ["byte_0", "byte_1", "byte_2", "byte_3"]
    group_order_row2 = ["byte_4", "byte_5", "byte_6", "byte_7"]

    # Pre-compute layouts for each group
    group_layouts = {}
    group_sizes = {}
    for name, comps in groups.items():
        # Determine max columns based on group size
        if name == "root":
            max_cols = 3  # Connector + root LEDs
        elif name.startswith("byte"):
            max_cols = 4  # 8 DFFs + 8 buffers in 4 cols
        elif name == "addr_decoder":
            max_cols = 4  # 3 INV + 8 AND3
        else:
            max_cols = 3

        ic_cells, standalone, others = sort_components_for_placement(comps)
        placements = compute_group_layout(ic_cells, standalone, max_cols)

        # Add connector and other non-IC components
        if others:
            # Place connector offset to the left to avoid overlap with ICs
            conn_offset_x = -8.0  # keep connector well left of IC grid
            row_offset = len(placements) // max_cols + 2 if placements else 0
            for i, comp in enumerate(others):
                placements.append((comp, conn_offset_x, row_offset * IC_CELL_H + i * CONN_PIN_PITCH))

        group_layouts[name] = placements
        group_sizes[name] = compute_group_size(placements)

    # Compute absolute positions for each group
    total_placed = 0
    cursor_x = BOARD_MARGIN
    cursor_y = BOARD_MARGIN

    # Row 0
    row0_max_h = 0
    for name in group_order_row0:
        if name not in group_layouts:
            continue
        layout = group_layouts[name]
        w, h = group_sizes[name]
        row0_max_h = max(row0_max_h, h)

        for comp, rel_x, rel_y in layout:
            abs_x = cursor_x + rel_x
            abs_y = cursor_y + rel_y
            _place_component(pcb, comp, abs_x, abs_y, netlist_data)
            total_placed += 1

        cursor_x += w + GROUP_GAP_X

    # Row 1
    cursor_x = BOARD_MARGIN
    cursor_y += row0_max_h + GROUP_GAP_Y
    row1_max_h = 0
    for name in group_order_row1:
        if name not in group_layouts:
            continue
        layout = group_layouts[name]
        w, h = group_sizes[name]
        row1_max_h = max(row1_max_h, h)

        for comp, rel_x, rel_y in layout:
            abs_x = cursor_x + rel_x
            abs_y = cursor_y + rel_y
            _place_component(pcb, comp, abs_x, abs_y, netlist_data)
            total_placed += 1

        cursor_x += w + GROUP_GAP_X

    # Row 2
    cursor_x = BOARD_MARGIN
    cursor_y += row1_max_h + GROUP_GAP_Y
    row2_max_h = 0
    for name in group_order_row2:
        if name not in group_layouts:
            continue
        layout = group_layouts[name]
        w, h = group_sizes[name]
        row2_max_h = max(row2_max_h, h)

        for comp, rel_x, rel_y in layout:
            abs_x = cursor_x + rel_x
            abs_y = cursor_y + rel_y
            _place_component(pcb, comp, abs_x, abs_y, netlist_data)
            total_placed += 1

        cursor_x += w + GROUP_GAP_X

    print(f"  Total components placed: {total_placed}")

    # Step 6: Board outline and power planes
    print("\n[6/6] Adding board outline and power planes...")

    # Compute board dimensions from placed components
    if pcb.board.footprints:
        all_x = [fp.position.X for fp in pcb.board.footprints]
        all_y = [fp.position.Y for fp in pcb.board.footprints]
        min_x = min(all_x) - BOARD_MARGIN
        min_y = min(all_y) - BOARD_MARGIN
        max_x = max(all_x) + BOARD_MARGIN + IC_CELL_W
        max_y = max(all_y) + BOARD_MARGIN + IC_CELL_H

        # Round up to nearest mm
        board_w = math.ceil(max_x - min_x)
        board_h = math.ceil(max_y - min_y)

        # Use origin at min_x, min_y
        origin_x = math.floor(min_x)
        origin_y = math.floor(min_y)
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
    print(f"  Board size: {board_w} x {board_h} mm")
    print(f"  Layers: 4 (F.Cu, In1.Cu=GND, In2.Cu=VCC, B.Cu)")
    print(f"  Routing: MANUAL (open in KiCad to route traces)")
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
