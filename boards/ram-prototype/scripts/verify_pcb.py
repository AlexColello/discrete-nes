#!/usr/bin/env python3
"""
Verification script for RAM prototype PCB.

Runs three DRC passes:
1. Default DRC -- KiCad's built-in rules
2. PCBWay DRC -- with manufacturing constraints
3. Elecrow DRC -- with manufacturing constraints

Plus board-specific checks:
- Board outline present and reasonable size
- All components within board outline
- All netlist components placed
- Power planes defined (GND on In1.Cu, VCC on In2.Cu)

Usage:
    python scripts/verify_pcb.py           # Run all checks
    python scripts/verify_pcb.py --no-drc  # Skip kicad-cli DRC
"""

import os
import sys

# Add shared library to path
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen.common import KICAD_CLI
from kicad_gen.verify import run_drc

from kiutils.board import Board

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------

BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
PCB_PATH = os.path.join(BOARD_DIR, "ram.kicad_pcb")
OUTPUT_DIR = os.path.join(BOARD_DIR, "verify_output")
RULES_DIR = os.path.join(BOARD_DIR, "rules")

EXPECTED_COMPONENT_COUNT = 512  # 161 ICs + 175 LEDs + 175 Rs + 1 connector

# DRC violation types to skip before routing is done
# These are expected with placement-only boards (no traces)
PRE_ROUTING_SKIP_TYPES = {
    "unconnected_items",       # No traces yet -- expected
    "lib_footprint_mismatch",  # Cosmetic: kiutils vs library diff
    "lib_footprint_issues",    # Cosmetic: local .pretty not found
    "silk_over_copper",        # Cosmetic: ref text overlaps pads on tiny BGA
    "silk_overlap",            # Cosmetic: ref text overlaps at high density
    "text_thickness",          # Cosmetic: stock 0402 footprint text too thin
    "text_height",             # Cosmetic: stock 0402 footprint text too short
}


# --------------------------------------------------------------
# Board-specific checks
# --------------------------------------------------------------

def check_board_outline(board):
    """Check that the board has an Edge.Cuts outline of reasonable size."""
    issues = []
    edge_cuts = []

    for item in board.graphicItems:
        layer = getattr(item, 'layer', None)
        if layer == "Edge.Cuts":
            edge_cuts.append(item)

    if not edge_cuts:
        issues.append("  No Edge.Cuts outline found")
        return issues

    # Collect all edge points
    xs, ys = [], []
    for item in edge_cuts:
        start = getattr(item, 'start', None)
        end = getattr(item, 'end', None)
        if start:
            xs.append(start.X)
            ys.append(start.Y)
        if end:
            xs.append(end.X)
            ys.append(end.Y)

    if xs and ys:
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        if w < 10 or h < 10:
            issues.append(f"  Board outline too small: {w:.1f} x {h:.1f} mm")
        elif w > 300 or h > 300:
            issues.append(f"  Board outline too large: {w:.1f} x {h:.1f} mm")
        else:
            print(f"  Board size: {w:.1f} x {h:.1f} mm")

    return issues


def check_components_placed(board):
    """Check that all expected components are placed on the board."""
    issues = []
    placed = len(board.footprints)

    if placed == 0:
        issues.append("  No components placed on board")
    elif placed < EXPECTED_COMPONENT_COUNT:
        issues.append(
            f"  Only {placed}/{EXPECTED_COMPONENT_COUNT} components placed")
    else:
        print(f"  Components placed: {placed}")

    # Check for components at origin (likely unplaced)
    at_origin = sum(
        1 for fp in board.footprints
        if abs(fp.position.X) < 0.01 and abs(fp.position.Y) < 0.01
    )
    if at_origin > 1:
        issues.append(f"  {at_origin} components at origin (likely unplaced)")

    return issues


def check_power_planes(board):
    """Check that GND and VCC zones exist on inner layers."""
    issues = []

    gnd_zone = False
    vcc_zone = False

    for zone in board.zones:
        layers = zone.layers if isinstance(zone.layers, list) else [zone.layers]
        net_name = zone.netName or ""

        if "GND" in net_name:
            if any("In1.Cu" in l for l in layers):
                gnd_zone = True
        if "VCC" in net_name:
            if any("In2.Cu" in l for l in layers):
                vcc_zone = True

    if not gnd_zone:
        issues.append("  No GND zone found on In1.Cu")
    if not vcc_zone:
        issues.append("  No VCC zone found on In2.Cu")

    if gnd_zone and vcc_zone:
        print("  Power planes: GND on In1.Cu, VCC on In2.Cu")

    return issues


def check_components_inside_outline(board):
    """Check that all components are placed within the board outline."""
    issues = []

    # Extract board outline bounding box from Edge.Cuts
    xs, ys = [], []
    for item in board.graphicItems:
        layer = getattr(item, 'layer', None)
        if layer != "Edge.Cuts":
            continue
        start = getattr(item, 'start', None)
        end = getattr(item, 'end', None)
        if start:
            xs.append(start.X)
            ys.append(start.Y)
        if end:
            xs.append(end.X)
            ys.append(end.Y)

    if not xs or not ys:
        # No outline to check against (check_board_outline will catch this)
        return issues

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    outside = []
    for fp in board.footprints:
        x, y = fp.position.X, fp.position.Y
        if x < min_x or x > max_x or y < min_y or y > max_y:
            ref = "?"
            if "Reference" in fp.properties:
                ref = fp.properties["Reference"]
            outside.append(f"{ref} at ({x:.1f}, {y:.1f})")

    if outside:
        for desc in outside:
            issues.append(f"  Component outside board outline: {desc}")
    else:
        print(f"  All components within board outline")

    return issues


def check_4layer_stackup(board):
    """Check that the board has 4 copper layers configured."""
    issues = []
    layer_names = {l.name for l in board.layers}

    required = {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}
    missing = required - layer_names
    if missing:
        issues.append(f"  Missing copper layers: {', '.join(sorted(missing))}")
    else:
        print("  4-layer stackup: F.Cu, In1.Cu, In2.Cu, B.Cu")

    return issues


# --------------------------------------------------------------
# Main
# --------------------------------------------------------------

def main():
    skip_drc = "--no-drc" in sys.argv

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total_errors = 0
    total_warnings = 0

    print("=" * 60)
    print("RAM Prototype PCB Verification")
    print("=" * 60)

    # -- Check PCB exists --
    if not os.path.exists(PCB_PATH):
        print(f"\n  ERROR: {PCB_PATH} not found")
        print("  Run generate_pcb.py first")
        return 1

    # -- Load board --
    print(f"\n--- Loading: ram.kicad_pcb ---")
    board = Board.from_file(PCB_PATH)

    # -- Board-specific checks --
    print(f"\n--- Board Structure Checks ---")

    stackup_issues = check_4layer_stackup(board)
    if stackup_issues:
        for issue in stackup_issues:
            print(issue)
        total_errors += len(stackup_issues)

    outline_issues = check_board_outline(board)
    if outline_issues:
        for issue in outline_issues:
            print(issue)
        total_errors += len(outline_issues)

    comp_issues = check_components_placed(board)
    if comp_issues:
        for issue in comp_issues:
            print(issue)
        total_errors += len(comp_issues)

    inside_issues = check_components_inside_outline(board)
    if inside_issues:
        for issue in inside_issues:
            print(issue)
        total_errors += len(inside_issues)

    power_issues = check_power_planes(board)
    if power_issues:
        for issue in power_issues:
            print(issue)
        total_errors += len(power_issues)

    # -- DRC runs --
    if not skip_drc and os.path.exists(KICAD_CLI):
        # Determine which violation types to skip
        skip = PRE_ROUTING_SKIP_TYPES

        # 1. Default DRC
        print(f"\n--- DRC: Default Rules ---")
        issues, errors, warnings = run_drc(
            PCB_PATH, OUTPUT_DIR, label="default", skip_types=skip)
        for issue in issues:
            print(issue)
        total_errors += errors
        total_warnings += warnings
        print(f"  DRC: {errors} error(s), {warnings} warning(s)")

        # 2. PCBWay DRC
        pcbway_rules = os.path.join(RULES_DIR, "pcbway.kicad_dru")
        if os.path.exists(pcbway_rules):
            print(f"\n--- DRC: PCBWay Rules ---")
            issues, errors, warnings = run_drc(
                PCB_PATH, OUTPUT_DIR, label="pcbway",
                custom_rules_path=pcbway_rules, skip_types=skip)
            for issue in issues:
                print(issue)
            total_errors += errors
            total_warnings += warnings
            print(f"  DRC: {errors} error(s), {warnings} warning(s)")
        else:
            print(f"\n  SKIP PCBWay DRC (rules file not found)")

        # 3. Elecrow DRC
        elecrow_rules = os.path.join(RULES_DIR, "elecrow.kicad_dru")
        if os.path.exists(elecrow_rules):
            print(f"\n--- DRC: Elecrow Rules ---")
            issues, errors, warnings = run_drc(
                PCB_PATH, OUTPUT_DIR, label="elecrow",
                custom_rules_path=elecrow_rules, skip_types=skip)
            for issue in issues:
                print(issue)
            total_errors += errors
            total_warnings += warnings
            print(f"  DRC: {errors} error(s), {warnings} warning(s)")
        else:
            print(f"\n  SKIP Elecrow DRC (rules file not found)")
    elif skip_drc:
        print(f"\n--- DRC skipped (--no-drc) ---")
    else:
        print(f"\n--- DRC skipped (kicad-cli not found) ---")

    # -- Summary --
    total_issues = total_errors + total_warnings
    print(f"\n{'=' * 60}")
    if total_issues > 0:
        print(f"FAILED: {total_errors} error(s), {total_warnings} warning(s)")
    else:
        print(f"PASSED: 0 errors, 0 warnings")
    print(f"{'=' * 60}")

    return 1 if total_issues > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
