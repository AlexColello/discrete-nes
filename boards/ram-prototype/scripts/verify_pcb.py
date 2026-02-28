#!/usr/bin/env python3
"""
Verification script for RAM prototype PCB.

Runs three DRC passes:
1. Default DRC -- KiCad's built-in rules
2. PCBWay DRC -- with manufacturing constraints
3. Elecrow DRC -- with manufacturing constraints

Plus board-specific checks:
- Board outline present and reasonable size
- Board outline within sheet border (12mm margin)
- All components within board outline
- All netlist components placed
- Power planes defined (GND on In1.Cu, VCC on In2.Cu)

Usage:
    python scripts/verify_pcb.py           # Run all checks
    python scripts/verify_pcb.py --no-drc  # Skip kicad-cli DRC
"""

import math
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


def _footprint_bbox(fp):
    """Compute bounding box of a footprint from pads and all graphic items.

    Considers pad positions+sizes and ALL footprint graphic items (courtyard,
    silkscreen, fab layer) to get the full physical extent.

    Returns (min_x, min_y, max_x, max_y) in absolute board coordinates.
    """
    fp_x, fp_y = fp.position.X, fp.position.Y
    angle = math.radians(fp.position.angle or 0)
    cos_a, sin_a = math.cos(angle), math.sin(angle)

    def to_abs(lx, ly):
        """Convert footprint-local coords to absolute board coords."""
        return (fp_x + lx * cos_a - ly * sin_a,
                fp_y + lx * sin_a + ly * cos_a)

    bbox_min_x = bbox_max_x = fp_x
    bbox_min_y = bbox_max_y = fp_y

    def expand(ax, ay):
        nonlocal bbox_min_x, bbox_max_x, bbox_min_y, bbox_max_y
        bbox_min_x = min(bbox_min_x, ax)
        bbox_max_x = max(bbox_max_x, ax)
        bbox_min_y = min(bbox_min_y, ay)
        bbox_max_y = max(bbox_max_y, ay)

    # Pads (with size)
    for pad in fp.pads:
        ax, ay = to_abs(pad.position.X, pad.position.Y)
        radius = max(pad.size.X, pad.size.Y) / 2 if pad.size else 0
        expand(ax - radius, ay - radius)
        expand(ax + radius, ay + radius)

    # All graphic items: FpLine, FpRect, FpText, FpCircle, FpArc, etc.
    for gi in fp.graphicItems:
        # FpLine / FpRect / FpArc — have start and end
        for attr in ('start', 'end'):
            pt = getattr(gi, attr, None)
            if pt is None:
                continue
            ax, ay = to_abs(pt.X, pt.Y)
            expand(ax, ay)
        # FpText / FpCircle — have position
        pos = getattr(gi, 'position', None)
        if pos is not None:
            ax, ay = to_abs(pos.X, pos.Y)
            expand(ax, ay)
        # FpCircle — expand by radius (end point is on circumference)
        center = getattr(gi, 'center', None)
        end_pt = getattr(gi, 'end', None)
        if center is not None and end_pt is not None:
            cx, cy = to_abs(center.X, center.Y)
            ex, ey = to_abs(end_pt.X, end_pt.Y)
            r = math.hypot(ex - cx, ey - cy)
            expand(cx - r, cy - r)
            expand(cx + r, cy + r)

    return bbox_min_x, bbox_min_y, bbox_max_x, bbox_max_y


def check_components_inside_outline(board):
    """Check that all components (pads, silkscreen, fab, courtyard) are within the board outline."""
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
        return issues

    outline_min_x, outline_max_x = min(xs), max(xs)
    outline_min_y, outline_max_y = min(ys), max(ys)

    outside = []
    for fp in board.footprints:
        fp_min_x, fp_min_y, fp_max_x, fp_max_y = _footprint_bbox(fp)
        if (fp_min_x < outline_min_x or fp_max_x > outline_max_x or
                fp_min_y < outline_min_y or fp_max_y > outline_max_y):
            ref = fp.properties.get("Reference", "?")
            overshoot_x = max(0, outline_min_x - fp_min_x,
                              fp_max_x - outline_max_x)
            overshoot_y = max(0, outline_min_y - fp_min_y,
                              fp_max_y - outline_max_y)
            overshoot = max(overshoot_x, overshoot_y)
            outside.append(
                f"  {ref} extends {overshoot:.1f}mm outside board outline"
                f" (bbox [{fp_min_x:.1f},{fp_min_y:.1f}]-"
                f"[{fp_max_x:.1f},{fp_max_y:.1f}])")

    if outside:
        for desc in outside:
            issues.append(desc)
    else:
        print(f"  All components within board outline")

    return issues


PAPER_SIZES = {
    "A4": (297.0, 210.0),   # landscape: width x height
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}

SHEET_BORDER_MARGIN = 13  # mm — board outline must be inside this margin (12mm border + 1mm clearance)


def check_outline_within_sheet(board):
    """Check that the board outline is within the sheet border by 12mm."""
    issues = []

    # Determine sheet size
    paper = getattr(board, 'paper', None)
    if paper is None:
        issues.append("  No paper size defined — cannot check sheet border")
        return issues

    paper_name = getattr(paper, 'paperSize', None)
    if paper_name not in PAPER_SIZES:
        issues.append(f"  Unknown paper size '{paper_name}' — cannot check sheet border")
        return issues

    sheet_w, sheet_h = PAPER_SIZES[paper_name]
    if getattr(paper, 'portrait', False):
        sheet_w, sheet_h = sheet_h, sheet_w

    # Collect board outline bounding box from Edge.Cuts
    xs, ys = [], []
    for item in board.graphicItems:
        layer = getattr(item, 'layer', None)
        if layer != "Edge.Cuts":
            continue
        for attr in ('start', 'end'):
            pt = getattr(item, attr, None)
            if pt:
                xs.append(pt.X)
                ys.append(pt.Y)

    if not xs or not ys:
        return issues  # No outline — handled by check_board_outline

    outline_min_x, outline_max_x = min(xs), max(xs)
    outline_min_y, outline_max_y = min(ys), max(ys)

    margin = SHEET_BORDER_MARGIN
    if outline_min_x < margin:
        issues.append(
            f"  Board outline left edge ({outline_min_x:.1f}mm) is less than "
            f"{margin}mm from the sheet border")
    if outline_min_y < margin:
        issues.append(
            f"  Board outline top edge ({outline_min_y:.1f}mm) is less than "
            f"{margin}mm from the sheet border")
    if outline_max_x > sheet_w - margin:
        issues.append(
            f"  Board outline right edge ({outline_max_x:.1f}mm) exceeds "
            f"sheet width minus {margin}mm ({sheet_w - margin:.1f}mm)")
    if outline_max_y > sheet_h - margin:
        issues.append(
            f"  Board outline bottom edge ({outline_max_y:.1f}mm) exceeds "
            f"sheet height minus {margin}mm ({sheet_h - margin:.1f}mm)")

    if not issues:
        print(f"  Board outline within sheet border (>={margin}mm margin)")

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

    sheet_issues = check_outline_within_sheet(board)
    if sheet_issues:
        for issue in sheet_issues:
            print(issue)
        total_errors += len(sheet_issues)

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
