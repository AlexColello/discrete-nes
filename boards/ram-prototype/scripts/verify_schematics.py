#!/usr/bin/env python3
"""
Verification script for RAM prototype schematics.

Checks for common issues that have caused ERC failures or visual problems:
1. Diagonal wires (KiCad doesn't reliably connect them)
2. Wire overlaps (same-direction wires sharing ranges silently merge nets)
3. Dangling wire endpoints (not connected to any pin, wire, label, junction)
4. Wires passing through component pins (unintended connections)
5. T-junctions without explicit junction dots (visual issue)
6. ERC via kicad-cli on the root schematic

Results are written to verify_output/ directory (gitignored).
SVGs are exported to verify_output/svg/ for visual inspection.

Usage:
    python scripts/verify_schematics.py          # Run all checks + SVG export
    python scripts/verify_schematics.py --no-erc  # Skip kicad-cli ERC and SVG export
"""

import json
import math
import os
import subprocess
import sys
from collections import defaultdict

from kiutils.schematic import Schematic

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------

GRID = 2.54
TOLERANCE = 0.0001  # mm tolerance for coordinate comparison

BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_DIR = os.path.join(BOARD_DIR, "verify_output")
KICAD_CLI = r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe"

# All schematic files to check
SCHEMATIC_FILES = [
    "ram.kicad_sch",
    "address_decoder.kicad_sch",
    "control_logic.kicad_sch",
    "write_clk_gen.kicad_sch",
    "read_oe_gen.kicad_sch",
    "byte.kicad_sch",
]


def snap(v):
    """Round to 2 decimal places to match generate_ram.py precision."""
    return round(v, 2)


def pts_close(a, b):
    """Check if two points are within tolerance."""
    return abs(a[0] - b[0]) < TOLERANCE and abs(a[1] - b[1]) < TOLERANCE


# --------------------------------------------------------------
# Schematic parsing
# --------------------------------------------------------------

def _extract_lib_pins(lib_sym):
    """Extract pin positions from a library symbol definition.

    Returns [(pin_number, lib_x, lib_y), ...] where coordinates are in
    library space (Y-up).
    """
    pins = []
    # kiutils stores sub-symbols in lib_sym.symbols
    # Sub-symbols named like "74LVC1G04_1_1" contain the pins for unit 1
    for sub_sym in getattr(lib_sym, 'symbols', []):
        for pin in getattr(sub_sym, 'pins', []):
            pins.append((pin.number, pin.position.X, pin.position.Y))
    # Also check the units property (kiutils convenience)
    if not pins:
        for unit in getattr(lib_sym, 'units', []):
            for pin in getattr(unit, 'pins', []):
                pins.append((pin.number, pin.position.X, pin.position.Y))
    return pins


def _pin_schematic_offset(lib_x, lib_y, angle_deg):
    """Convert library pin position to schematic offset (dx, dy).

    Library uses Y-up; schematic uses Y-down.
    KiCad rotation is CW in schematic space.
    """
    # Negate Y for schematic coordinate system
    bx, by = lib_x, -lib_y
    rad = math.radians(angle_deg)
    cos_a = round(math.cos(rad), 10)
    sin_a = round(math.sin(rad), 10)
    dx = snap(cos_a * bx + sin_a * by)
    dy = snap(-sin_a * bx + cos_a * by)
    return dx, dy


def parse_schematic(filepath):
    """Parse a schematic and extract all geometric data for verification.

    Returns dict with:
      wires: [((x1,y1), (x2,y2)), ...]
      pins: {(x,y): (ref, pin_num, pin_type), ...}
      pin_positions: set of (x,y)
      junctions: set of (x,y)
      labels: set of (x,y)
      no_connects: set of (x,y)
      components: [(ref, lib_name, cx, cy, angle), ...]
    """
    sch = Schematic.from_file(filepath)

    # -- Wires --
    wires = []
    for item in sch.graphicalItems:
        item_type = getattr(item, 'type', None)
        if item_type == 'wire':
            pts = getattr(item, 'points', [])
            if len(pts) >= 2:
                p1 = (snap(pts[0].X), snap(pts[0].Y))
                p2 = (snap(pts[1].X), snap(pts[1].Y))
                wires.append((p1, p2))

    # -- Library symbol pin map --
    lib_pin_map = {}  # lib_id -> [(pin_num, lib_x, lib_y)]
    for lib_sym in sch.libSymbols:
        lib_pin_map[lib_sym.libId] = _extract_lib_pins(lib_sym)

    # -- Component instances and pin positions --
    pins = {}          # (x,y) -> (ref, pin_num, pin_type_or_name)
    pin_positions = set()
    components = []

    for comp in sch.schematicSymbols:
        lib_id = comp.libId
        cx = snap(comp.position.X)
        cy = snap(comp.position.Y)
        angle = comp.position.angle or 0

        # Get reference designator
        ref = "?"
        for prop in comp.properties:
            if prop.key == "Reference":
                ref = prop.value
                break

        # Determine base symbol name from lib_id
        lib_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id
        components.append((ref, lib_name, cx, cy, angle))

        if lib_id in lib_pin_map:
            for pin_num, lx, ly in lib_pin_map[lib_id]:
                dx, dy = _pin_schematic_offset(lx, ly, angle)
                abs_x = snap(cx + dx)
                abs_y = snap(cy + dy)
                pins[(abs_x, abs_y)] = (ref, pin_num, lib_name)
                pin_positions.add((abs_x, abs_y))

    # -- Junctions --
    junctions = set()
    for j in sch.junctions:
        junctions.add((snap(j.position.X), snap(j.position.Y)))

    # -- Labels (all types) --
    labels = set()
    for lbl in getattr(sch, 'labels', []):
        labels.add((snap(lbl.position.X), snap(lbl.position.Y)))
    for lbl in getattr(sch, 'globalLabels', []):
        labels.add((snap(lbl.position.X), snap(lbl.position.Y)))
    for lbl in getattr(sch, 'hierarchicalLabels', []):
        labels.add((snap(lbl.position.X), snap(lbl.position.Y)))

    # -- No-connect markers --
    no_connects = set()
    for nc in getattr(sch, 'noConnects', []):
        no_connects.add((snap(nc.position.X), snap(nc.position.Y)))

    # -- Hierarchical sheet pins --
    sheet_pins = set()
    for sheet in getattr(sch, 'hierarchicalSheets', []):
        for pin in getattr(sheet, 'pins', []):
            sheet_pins.add((snap(pin.position.X), snap(pin.position.Y)))

    return {
        'wires': wires,
        'pins': pins,
        'pin_positions': pin_positions,
        'junctions': junctions,
        'labels': labels,
        'no_connects': no_connects,
        'sheet_pins': sheet_pins,
        'components': components,
    }


# --------------------------------------------------------------
# Check functions
# --------------------------------------------------------------

def check_diagonal_wires(data):
    """Check for wires that are neither horizontal nor vertical.

    KiCad doesn't reliably connect diagonal wires. All routing should
    be orthogonal (L-shaped at worst).
    """
    issues = []
    for i, ((x1, y1), (x2, y2)) in enumerate(data['wires']):
        if abs(x1 - x2) > TOLERANCE and abs(y1 - y2) > TOLERANCE:
            issues.append(
                f"  Wire #{i}: ({x1}, {y1}) -> ({x2}, {y2}) is diagonal"
            )
    return issues


def check_wire_overlaps(data):
    """Check for same-direction wire overlaps.

    Two wires sharing the same axis AND overlapping in their range
    silently merge nets. This is the #1 cause of ERC failures in
    generated schematics.
    """
    wires = data['wires']
    issues = []

    # Separate horizontal and vertical wires
    h_wires = []  # (y, x_min, x_max, idx)
    v_wires = []  # (x, y_min, y_max, idx)

    for idx, ((x1, y1), (x2, y2)) in enumerate(wires):
        if abs(y1 - y2) < TOLERANCE:  # horizontal
            h_wires.append((snap(y1), min(x1, x2), max(x1, x2), idx))
        elif abs(x1 - x2) < TOLERANCE:  # vertical
            v_wires.append((snap(x1), min(y1, y2), max(y1, y2), idx))

    # Check horizontal overlaps (group by Y)
    by_y = defaultdict(list)
    for y, xmin, xmax, idx in h_wires:
        by_y[y].append((xmin, xmax, idx))

    for y, segs in by_y.items():
        segs.sort()
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                a_min, a_max, a_idx = segs[i]
                b_min, b_max, b_idx = segs[j]
                overlap_start = max(a_min, b_min)
                overlap_end = min(a_max, b_max)
                if overlap_end - overlap_start > TOLERANCE:
                    issues.append(
                        f"  H overlap Y={y}: wire#{a_idx} X=[{a_min},{a_max}] "
                        f"& wire#{b_idx} X=[{b_min},{b_max}] "
                        f"share [{overlap_start},{overlap_end}]"
                    )

    # Check vertical overlaps (group by X)
    by_x = defaultdict(list)
    for x, ymin, ymax, idx in v_wires:
        by_x[x].append((ymin, ymax, idx))

    for x, segs in by_x.items():
        segs.sort()
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                a_min, a_max, a_idx = segs[i]
                b_min, b_max, b_idx = segs[j]
                overlap_start = max(a_min, b_min)
                overlap_end = min(a_max, b_max)
                if overlap_end - overlap_start > TOLERANCE:
                    issues.append(
                        f"  V overlap X={x}: wire#{a_idx} Y=[{a_min},{a_max}] "
                        f"& wire#{b_idx} Y=[{b_min},{b_max}] "
                        f"share [{overlap_start},{overlap_end}]"
                    )

    return issues


def check_dangling_endpoints(data):
    """Check for wire endpoints not connected to anything.

    A wire endpoint is "connected" if it touches:
    - Another wire endpoint (shared point)
    - A component pin
    - A junction dot
    - A label (local, global, or hierarchical)
    - A no-connect marker
    - A hierarchical sheet pin
    - The interior of another wire (T-junction)
    """
    wires = data['wires']
    pin_positions = data['pin_positions']
    junctions = data['junctions']
    labels = data['labels']
    no_connects = data['no_connects']
    sheet_pins = data['sheet_pins']

    # Collect all wire endpoints
    all_endpoints = []
    for (x1, y1), (x2, y2) in wires:
        all_endpoints.append((x1, y1))
        all_endpoints.append((x2, y2))

    # Count occurrences of each endpoint
    endpoint_counts = defaultdict(int)
    for pt in all_endpoints:
        endpoint_counts[pt] += 1

    # Build set of all "connected" points
    connected = set()
    connected.update(pin_positions)
    connected.update(junctions)
    connected.update(labels)
    connected.update(no_connects)
    connected.update(sheet_pins)

    # Points where 2+ wire endpoints meet
    for pt, count in endpoint_counts.items():
        if count >= 2:
            connected.add(pt)

    # T-junctions: endpoint landing on wire body
    unique_endpoints = set(all_endpoints)
    for (x1, y1), (x2, y2) in wires:
        if abs(y1 - y2) < TOLERANCE:  # horizontal wire
            xmin, xmax = min(x1, x2), max(x1, x2)
            y = snap(y1)
            for pt in unique_endpoints:
                if (abs(pt[1] - y) < TOLERANCE and
                        xmin - TOLERANCE <= pt[0] <= xmax + TOLERANCE):
                    connected.add(pt)
        elif abs(x1 - x2) < TOLERANCE:  # vertical wire
            ymin, ymax = min(y1, y2), max(y1, y2)
            x = snap(x1)
            for pt in unique_endpoints:
                if (abs(pt[0] - x) < TOLERANCE and
                        ymin - TOLERANCE <= pt[1] <= ymax + TOLERANCE):
                    connected.add(pt)

    # Find dangling endpoints
    issues = []
    for pt in sorted(unique_endpoints):
        if pt not in connected:
            # Double-check with tolerance against all connected points
            found = any(pts_close(pt, cp) for cp in connected)
            if not found:
                issues.append(f"  Dangling at ({pt[0]}, {pt[1]})")

    return issues


def check_wire_through_pins(data):
    """Check for wires passing through component pins.

    When a wire's INTERIOR passes through a pin position (not at its
    endpoints), it creates an unintended connection. This is different
    from T-junctions (which involve wire endpoints).

    Excludes power pins (VCC/GND) which are intentionally overlapped
    by connect_power().
    """
    wires = data['wires']
    pins = data['pins']  # (x,y) -> (ref, pin_num, lib_name)

    issues = []

    # Collect all wire endpoints so we can exclude intentional connections
    wire_endpoints = set()
    for (x1, y1), (x2, y2) in wires:
        wire_endpoints.add((x1, y1))
        wire_endpoints.add((x2, y2))

    for (px, py), (ref, pin_num, lib_name) in pins.items():
        # Skip power symbols — they're placed directly at pin positions
        if ref.startswith("#"):
            continue

        # Skip if a wire endpoint is at this pin (intentional connection)
        if (px, py) in wire_endpoints:
            continue
        if any(pts_close((px, py), ep) for ep in wire_endpoints):
            continue

        # Check if any wire INTERIOR passes through this pin
        for w_idx, ((x1, y1), (x2, y2)) in enumerate(wires):
            if abs(y1 - y2) < TOLERANCE:  # horizontal wire
                xmin, xmax = min(x1, x2), max(x1, x2)
                if (abs(py - y1) < TOLERANCE and
                        xmin + TOLERANCE < px < xmax - TOLERANCE):
                    issues.append(
                        f"  Wire #{w_idx} H({x1},{y1})->({x2},{y2}) "
                        f"passes through {ref} pin {pin_num} at ({px},{py})"
                    )
            elif abs(x1 - x2) < TOLERANCE:  # vertical wire
                ymin, ymax = min(y1, y2), max(y1, y2)
                if (abs(px - x1) < TOLERANCE and
                        ymin + TOLERANCE < py < ymax - TOLERANCE):
                    issues.append(
                        f"  Wire #{w_idx} V({x1},{y1})->({x2},{y2}) "
                        f"passes through {ref} pin {pin_num} at ({px},{py})"
                    )

    return issues


def check_tjunctions_without_dots(data):
    """Find T-junctions missing explicit junction dots.

    A wire endpoint landing on another wire's interior creates a valid
    KiCad connection, but without a junction dot it looks like a
    "dangling wire" in the GUI. This check finds these cases.
    """
    wires = data['wires']
    junctions = data['junctions']

    # Collect all wire endpoints
    endpoint_set = set()
    for (x1, y1), (x2, y2) in wires:
        endpoint_set.add((x1, y1))
        endpoint_set.add((x2, y2))

    issues = []
    seen = set()  # avoid duplicate reports

    for pt in endpoint_set:
        for (x1, y1), (x2, y2) in wires:
            if abs(y1 - y2) < TOLERANCE:  # horizontal wire
                xmin, xmax = min(x1, x2), max(x1, x2)
                y = snap(y1)
                if (abs(pt[1] - y) < TOLERANCE and
                        xmin + TOLERANCE < pt[0] < xmax - TOLERANCE):
                    # Endpoint is on the interior of this wire
                    key = (pt, "H", y, xmin, xmax)
                    if key not in seen and pt not in junctions:
                        issues.append(
                            f"  T-junction at ({pt[0]}, {pt[1]}) on "
                            f"H wire ({x1},{y1})->({x2},{y2}) — no junction dot"
                        )
                        seen.add(key)
            elif abs(x1 - x2) < TOLERANCE:  # vertical wire
                ymin, ymax = min(y1, y2), max(y1, y2)
                x = snap(x1)
                if (abs(pt[0] - x) < TOLERANCE and
                        ymin + TOLERANCE < pt[1] < ymax - TOLERANCE):
                    key = (pt, "V", x, ymin, ymax)
                    if key not in seen and pt not in junctions:
                        issues.append(
                            f"  T-junction at ({pt[0]}, {pt[1]}) on "
                            f"V wire ({x1},{y1})->({x2},{y2}) — no junction dot"
                        )
                        seen.add(key)

    return issues


# --------------------------------------------------------------
# ERC via kicad-cli
# --------------------------------------------------------------

def _is_standalone_artifact(violation):
    """Check if an ERC violation is an expected standalone sub-sheet artifact.

    When running ERC on a sub-sheet in isolation (not via the root sheet),
    certain errors are expected because hierarchical connections don't exist:
    - Hierarchical labels "cannot be connected to non-existent parent sheet"
    - Input pins "not driven" when their drivers are hierarchical labels
    - Power pins not driven (no PWR_FLAG in isolated context)
    """
    desc = violation.get("description", "")
    vtype = violation.get("type", "")

    # Hierarchical labels can't connect when sheet is standalone
    if "cannot be connected to non-existent parent sheet" in desc:
        return True

    # Hierarchical labels show as "dangling" standalone because KiCad
    # can't resolve their parent-side connections without the hierarchy.
    # The "Hierarchical Label" text is in items[].description, not the
    # violation description.
    if vtype == "label_dangling":
        items = violation.get("items", [])
        if any("Hierarchical Label" in it.get("description", "")
               for it in items):
            return True

    # Input pins fed by hierarchical labels show as "not driven" standalone
    if vtype == "pin_not_driven" and "Input pin not driven" in desc:
        return True

    # Power pins not driven in isolation
    if vtype == "power_pin_not_driven":
        return True

    return False


def run_erc(sch_path, output_dir, label=None, standalone=False):
    """Run kicad-cli ERC on a schematic.

    Args:
        sch_path: Path to the .kicad_sch file
        output_dir: Directory for output files
        label: Label for output filenames (default: derived from sch_path)
        standalone: If True, filter out expected standalone sub-sheet artifacts

    Returns (issues_list, error_count, warning_count).
    """
    if label is None:
        label = os.path.splitext(os.path.basename(sch_path))[0]

    erc_json = os.path.join(output_dir, f"erc_{label}.json")

    if not os.path.exists(KICAD_CLI):
        return [f"  kicad-cli not found at {KICAD_CLI}"], 0, 0

    # JSON report
    subprocess.run(
        [KICAD_CLI, "sch", "erc", "--format", "json",
         "--severity-all", "--output", erc_json, sch_path],
        capture_output=True, text=True,
    )

    issues = []
    real_errors = 0
    warnings = 0
    filtered_count = 0

    if os.path.exists(erc_json):
        with open(erc_json) as f:
            data = json.load(f)

        for sheet in data.get("sheets", []):
            path = sheet.get("path", "/")
            for v in sheet.get("violations", []):
                severity = v["severity"]
                vtype = v["type"]
                desc = v["description"]

                # lib_symbol_mismatch is a known harmless kiutils artifact
                if vtype == "lib_symbol_mismatch":
                    warnings += 1
                    continue

                # Filter standalone artifacts for sub-sheet ERC
                if standalone and _is_standalone_artifact(v):
                    filtered_count += 1
                    continue

                if severity == "error":
                    real_errors += 1
                    items_desc = "; ".join(
                        it.get("description", "")
                        for it in v.get("items", [])
                    )
                    issues.append(
                        f"  ERROR [{path}] {vtype}: {desc} ({items_desc})"
                    )
                elif severity == "warning":
                    warnings += 1
                    if vtype != "lib_symbol_mismatch":
                        issues.append(
                            f"  WARN [{path}] {vtype}: {desc}"
                        )
    else:
        issues.append("  ERC JSON output not generated")

    if standalone and filtered_count > 0:
        issues.append(
            f"  (filtered {filtered_count} standalone artifact(s))"
        )

    return issues, real_errors, warnings


# --------------------------------------------------------------
# Main
# --------------------------------------------------------------

def main():
    skip_erc = "--no-erc" in sys.argv

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total_errors = 0
    total_warnings = 0
    all_results = {}

    print("=" * 60)
    print("RAM Prototype Schematic Verification")
    print("=" * 60)

    # -- Per-file checks --
    for sch_file in SCHEMATIC_FILES:
        filepath = os.path.join(BOARD_DIR, sch_file)
        if not os.path.exists(filepath):
            print(f"\n  SKIP {sch_file} (not found)")
            continue

        print(f"\n--- {sch_file} ---")
        data = parse_schematic(filepath)
        file_results = []

        # 1. Diagonal wires
        diag = check_diagonal_wires(data)
        if diag:
            file_results.append(("Diagonal Wires", diag, True))

        # 2. Wire overlaps
        overlaps = check_wire_overlaps(data)
        if overlaps:
            file_results.append(("Wire Overlaps (NET MERGE)", overlaps, True))

        # 3. Dangling endpoints
        dangles = check_dangling_endpoints(data)
        if dangles:
            file_results.append(("Dangling Endpoints", dangles, True))

        # 4. Wires through pins
        through = check_wire_through_pins(data)
        if through:
            file_results.append(("Wire Through Pin", through, True))

        # 5. T-junctions without dots (warning only)
        tjuncs = check_tjunctions_without_dots(data)
        if tjuncs:
            file_results.append(("T-junction (no dot)", tjuncs, False))

        if file_results:
            for category, issues, is_error in file_results:
                count = len(issues)
                level = "ERROR" if is_error else "WARN"
                print(f"  [{level}] {category}: {count}")
                for issue in issues:
                    print(issue)
                if is_error:
                    total_errors += count
                else:
                    total_warnings += count
        else:
            print("  All checks passed")

        all_results[sch_file] = file_results

    # -- ERC --
    if not skip_erc:
        # Root sheet ERC (full hierarchy)
        root_sch = os.path.join(BOARD_DIR, "ram.kicad_sch")
        if os.path.exists(root_sch):
            print(f"\n--- ERC: ram.kicad_sch (root, full hierarchy) ---")
            erc_issues, erc_errors, erc_warnings = run_erc(
                root_sch, OUTPUT_DIR, label="root")

            if erc_issues:
                for issue in erc_issues:
                    print(issue)
            total_errors += erc_errors
            total_warnings += erc_warnings
            print(
                f"  ERC: {erc_errors} error(s), {erc_warnings} warning(s) "
                f"(lib_symbol_mismatch excluded from errors)"
            )

        # Per-sub-sheet standalone ERC
        for sch_file in SCHEMATIC_FILES:
            if sch_file == "ram.kicad_sch":
                continue  # already checked as root
            filepath = os.path.join(BOARD_DIR, sch_file)
            if not os.path.exists(filepath):
                continue
            label = os.path.splitext(sch_file)[0]
            print(f"\n--- ERC: {sch_file} (standalone) ---")
            erc_issues, erc_errors, erc_warnings = run_erc(
                filepath, OUTPUT_DIR, label=label, standalone=True)

            if erc_issues:
                for issue in erc_issues:
                    print(issue)
            total_errors += erc_errors
            total_warnings += erc_warnings
            print(
                f"  ERC: {erc_errors} error(s), {erc_warnings} warning(s)"
            )
    else:
        print(f"\n--- ERC skipped (--no-erc) ---")

    # -- Export SVGs for visual inspection --
    if not skip_erc and os.path.exists(KICAD_CLI):
        print(f"\n--- SVG export ---")
        svg_dir = os.path.join(OUTPUT_DIR, "svg")
        os.makedirs(svg_dir, exist_ok=True)
        root_sch = os.path.join(BOARD_DIR, "ram.kicad_sch")
        if os.path.exists(root_sch):
            result = subprocess.run(
                [KICAD_CLI, "sch", "export", "svg",
                 "--output", svg_dir, root_sch],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                svg_count = len([f for f in os.listdir(svg_dir)
                                 if f.endswith('.svg')])
                print(f"  Exported {svg_count} SVG(s) to {svg_dir}")
            else:
                print(f"  SVG export failed: {result.stderr.strip()}")

    # -- Write report --
    report_path = os.path.join(OUTPUT_DIR, "verify_report.txt")
    with open(report_path, 'w') as f:
        f.write("RAM Prototype Schematic Verification Report\n")
        f.write("=" * 50 + "\n\n")
        for sch_file, file_results in all_results.items():
            f.write(f"{sch_file}:\n")
            if file_results:
                for category, issues, is_error in file_results:
                    level = "ERROR" if is_error else "WARN"
                    f.write(f"  [{level}] {category} ({len(issues)}):\n")
                    for issue in issues:
                        f.write(f"  {issue}\n")
            else:
                f.write("  All checks passed\n")
            f.write("\n")

    # -- Summary --
    print(f"\n{'=' * 60}")
    if total_errors > 0:
        print(f"FAILED: {total_errors} error(s), {total_warnings} warning(s)")
    else:
        print(f"PASSED: 0 errors, {total_warnings} warning(s)")
    print(f"Report: {report_path}")
    print(f"{'=' * 60}")

    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
