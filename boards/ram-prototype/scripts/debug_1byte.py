#!/usr/bin/env python3
"""Debug script: place 1 byte from the real netlist and run preroute functions.

Exercises the same code paths as the full generate_pcb.py on a single byte,
enabling faster iteration. Runs DRC with grouped output and exports a PNG.

All output goes to verify_output/debug_1byte/ (gitignored).
"""

import math
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen.pcb import (
    PCBBuilder, create_dsbga_footprints,
    export_netlist, parse_netlist,
)
from kicad_gen.verify import run_drc
from kicad_gen.snapshot import snapshot_region

from generate_pcb import (
    group_components, layout_byte_group, _place_component,
    preroute_power_vias, preroute_bcu_resistors, preroute_ic_to_led,
    BOARD_DIR, SHARED_FP_DIR, IC_CELL_W, IC_CELL_H,
    BOARD_MARGIN,
)

OUTPUT_DIR = os.path.join(BOARD_DIR, "verify_output", "debug_1byte")

# DRC violation types to skip (expected for this debug board)
SKIP_TYPES = {"unconnected_items", "lib_footprint_mismatch",
              "lib_footprint_issues", "silk_overlap",
              "text_thickness", "text_height"}

ORIGIN_X = 20.0
ORIGIN_Y = 20.0


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Create DSBGA footprints (same as main script)
    create_dsbga_footprints(SHARED_FP_DIR)

    # Export and parse real netlist
    sch_path = os.path.join(BOARD_DIR, "ram.kicad_sch")
    net_path = os.path.join(OUTPUT_DIR, "debug_1byte.xml")
    export_netlist(sch_path, net_path)
    netlist_data = parse_netlist(net_path)

    # Group components, pick byte_0
    groups = group_components(netlist_data)
    byte_name = "byte_0"
    if byte_name not in groups:
        print(f"ERROR: {byte_name} not found in groups: {sorted(groups.keys())}")
        return 1

    # Layout byte using same function as main script
    placements = layout_byte_group(groups[byte_name])
    print(f"  {byte_name}: {len(placements)} placements")

    # Initialize PCB
    pcb = PCBBuilder(title="Debug 1-Byte")
    pcb.add_fp_lib_path("DSBGA_Packages", SHARED_FP_DIR)
    pcb.add_nets_from_netlist(netlist_data)
    pcb.set_4layer_stackup()
    pcb.set_layer_type("B.Cu", "power")
    pcb.set_layer_type("In1.Cu", "signal")

    # Place components using same function as main script
    placed = 0
    for comp, rel_x, rel_y in placements:
        _place_component(pcb, comp, ORIGIN_X + rel_x, ORIGIN_Y + rel_y,
                         netlist_data)
        placed += 1
    print(f"  Placed: {placed} components")

    # Pre-route using same functions as main script
    pcb.build_ref_index()

    pwr_vias, pwr_traces = preroute_power_vias(pcb, netlist_data)
    print(f"  Power vias: {pwr_vias}")

    ic_led_traces = preroute_ic_to_led(pcb, netlist_data)
    print(f"  IC->LED: {ic_led_traces} traces")

    bcu_r_vias, bcu_r_traces = preroute_bcu_resistors(pcb, netlist_data)
    print(f"  B.Cu resistors: {bcu_r_vias} vias, {bcu_r_traces} traces")

    # Board outline from component extents
    comp_min_x = comp_min_y = float('inf')
    comp_max_x = comp_max_y = float('-inf')
    for fp in pcb.board.footprints:
        fp_x, fp_y = fp.position.X, fp.position.Y
        angle = math.radians(fp.position.angle or 0)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        for pad in fp.pads:
            px, py = pad.position.X, pad.position.Y
            abs_x = fp_x + px * cos_a + py * sin_a
            abs_y = fp_y - px * sin_a + py * cos_a
            radius = max(pad.size.X, pad.size.Y) / 2 if pad.size else 0
            comp_min_x = min(comp_min_x, abs_x - radius)
            comp_max_x = max(comp_max_x, abs_x + radius)
            comp_min_y = min(comp_min_y, abs_y - radius)
            comp_max_y = max(comp_max_y, abs_y + radius)

    origin_x = math.floor(comp_min_x - BOARD_MARGIN)
    origin_y = math.floor(comp_min_y - BOARD_MARGIN)
    board_w = math.ceil(comp_max_x + BOARD_MARGIN - origin_x)
    board_h = math.ceil(comp_max_y + BOARD_MARGIN - origin_y)
    pcb.set_board_outline(board_w, board_h, origin_x, origin_y)

    # Save PCB
    out_pcb = os.path.join(OUTPUT_DIR, "debug_1byte.kicad_pcb")
    pcb.save(out_pcb, hide_text=True)
    print(f"  Saved: {out_pcb}")

    # Cleanup netlist
    if os.path.exists(net_path):
        os.remove(net_path)

    # Run DRC (grouped output with snapshots)
    print("\n--- DRC ---")
    issues, errors, warnings = run_drc(
        out_pcb, OUTPUT_DIR, label="debug_1byte",
        skip_types=SKIP_TYPES, snapshot=True)
    for line in issues:
        print(line)
    print(f"  DRC: {errors} error(s), {warnings} warning(s)")

    # Export full-board PNG
    out_png = os.path.join(OUTPUT_DIR, "debug_1byte.png")
    w, h = snapshot_region(out_pcb, None, out_png)
    print(f"  PNG exported: {out_png} ({w}x{h} px)")

    return 1 if errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
