#!/usr/bin/env python3
"""
Verification script for RAM prototype schematics.

Checks for common issues that have caused ERC failures or visual problems:
1-11. General checks via shared kicad_gen.verify module
12. Netlist connectivity (board-specific expected connections)
13. ERC via kicad-cli on the root schematic

Results are written to verify_output/ directory (gitignored).
SVGs are exported to verify_output/svg/ for visual inspection.

Usage:
    python scripts/verify_schematics.py          # Run all checks + SVG export
    python scripts/verify_schematics.py --no-erc  # Skip kicad-cli ERC and SVG export
"""

import os
import subprocess
import sys
from collections import defaultdict

# Add shared library to path
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen.common import KICAD_CLI, snap
from kicad_gen.verify import (
    parse_schematic, run_all_checks, run_erc, UnionFind,
    _extract_lib_pins, _pin_schematic_offset, pts_close, TOLERANCE,
)
from kiutils.schematic import Schematic

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------

BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_DIR = os.path.join(BOARD_DIR, "verify_output")

# All schematic files to check
SCHEMATIC_FILES = [
    "ram.kicad_sch",
    "address_decoder.kicad_sch",
    "control_logic.kicad_sch",
    "write_clk_gen.kicad_sch",
    "read_oe_gen.kicad_sch",
    "byte.kicad_sch",
]


# --------------------------------------------------------------
# Netlist verification (board-specific)
# --------------------------------------------------------------

def check_netlist():
    """Verify root sheet netlist connectivity.

    Builds nets from wire connectivity in ram.kicad_sch using union-find,
    then checks that expected pairs of hierarchy sheet pins are on the same
    net (e.g., address decoder SEL0 -> write_clk_gen SEL0) and that signals
    that should be separate are NOT merged.

    Returns list of issue strings (empty if all checks pass).
    """
    filepath = os.path.join(BOARD_DIR, "ram.kicad_sch")
    if not os.path.exists(filepath):
        return ["  ram.kicad_sch not found"]

    sch = Schematic.from_file(filepath)
    uf = UnionFind()

    # -- Collect wires --
    wires = []
    for item in sch.graphicalItems:
        if getattr(item, 'type', None) == 'wire':
            pts = item.points
            if len(pts) >= 2:
                p1 = (snap(pts[0].X), snap(pts[0].Y))
                p2 = (snap(pts[1].X), snap(pts[1].Y))
                wires.append((p1, p2))
                uf.union(p1, p2)

    # -- Collect all electrically-active points --
    all_pts = set()
    for p1, p2 in wires:
        all_pts.add(p1)
        all_pts.add(p2)

    # Sheet pins: map (x,y) -> "SheetName:PinName"
    sheet_pin_ids = {}
    for sheet in sch.sheets:
        sname = sheet.sheetName.value
        for pin in sheet.pins:
            pt = (snap(pin.position.X), snap(pin.position.Y))
            sheet_pin_ids[pt] = f"{sname}:{pin.name}"
            all_pts.add(pt)

    # Labels: map (x,y) -> label text
    label_pts = {}
    for lbl in getattr(sch, 'labels', []):
        pt = (snap(lbl.position.X), snap(lbl.position.Y))
        label_pts[pt] = lbl.text
        all_pts.add(pt)

    # Component pins (connector, LEDs, resistors)
    lib_pin_map = {}
    for lib_sym in sch.libSymbols:
        lib_pin_map[lib_sym.libId] = _extract_lib_pins(lib_sym)

    comp_pin_ids = {}  # (x,y) -> "Ref:pin_num"
    for comp in sch.schematicSymbols:
        lib_id = comp.libId
        cx, cy = snap(comp.position.X), snap(comp.position.Y)
        angle = comp.position.angle or 0
        ref = "?"
        for prop in comp.properties:
            if prop.key == "Reference":
                ref = prop.value
                break
        if lib_id in lib_pin_map:
            for pin_num, lx, ly, pa, pl in lib_pin_map[lib_id]:
                dx, dy = _pin_schematic_offset(lx, ly, angle)
                pt = (snap(cx + dx), snap(cy + dy))
                comp_pin_ids[pt] = f"{ref}:{pin_num}"
                all_pts.add(pt)

    # Junctions
    for j in sch.junctions:
        all_pts.add((snap(j.position.X), snap(j.position.Y)))

    # -- Merge points that touch wires (T-junctions + endpoints) --
    for pt in all_pts:
        for (x1, y1), (x2, y2) in wires:
            if abs(y1 - y2) < TOLERANCE:  # horizontal
                xmin, xmax = min(x1, x2), max(x1, x2)
                if (abs(pt[1] - y1) < TOLERANCE and
                        xmin - TOLERANCE <= pt[0] <= xmax + TOLERANCE):
                    uf.union(pt, (x1, y1))
            elif abs(x1 - x2) < TOLERANCE:  # vertical
                ymin, ymax = min(y1, y2), max(y1, y2)
                if (abs(pt[0] - x1) < TOLERANCE and
                        ymin - TOLERANCE <= pt[1] <= ymax + TOLERANCE):
                    uf.union(pt, (x1, y1))

    # -- Merge same-name labels (implicit net connections) --
    label_groups = defaultdict(list)
    for pt, name in label_pts.items():
        label_groups[name].append(pt)
    for name, pts in label_groups.items():
        for i in range(1, len(pts)):
            uf.union(pts[0], pts[i])

    # -- Build net membership: root -> set of identifiers --
    nets = defaultdict(set)
    for pt, sid in sheet_pin_ids.items():
        nets[uf.find(pt)].add(sid)
    for pt, name in label_pts.items():
        nets[uf.find(pt)].add(f"label:{name}")

    def on_same_net(id_a, id_b):
        for root, members in nets.items():
            if id_a in members and id_b in members:
                return True
        return False

    def id_exists(identifier):
        return any(identifier in m for m in nets.values())

    # -- Define expected connections --
    issues = []

    # 1. SEL0-7: addr decoder -> write_clk_gen AND read_oe_gen
    for i in range(8):
        ad = f"Address Decoder:SEL{i}"
        wc = f"Write Clk Gen:SEL{i}"
        ro = f"Read OE Gen:SEL{i}"
        if not on_same_net(ad, wc):
            issues.append(f"  {ad} not connected to {wc}")
        if not on_same_net(ad, ro):
            issues.append(f"  {ad} not connected to {ro}")

    # 2. WRITE_ACTIVE: control logic -> write_clk_gen
    if not on_same_net("Control Logic:WRITE_ACTIVE",
                       "Write Clk Gen:WRITE_ACTIVE"):
        issues.append(
            "  Control Logic:WRITE_ACTIVE not connected to "
            "Write Clk Gen:WRITE_ACTIVE")

    # 3. READ_EN: control logic -> read_oe_gen
    if not on_same_net("Control Logic:READ_EN", "Read OE Gen:READ_EN"):
        issues.append(
            "  Control Logic:READ_EN not connected to Read OE Gen:READ_EN")

    # 4. WRITE_CLK_i: write_clk_gen -> byte_i
    for i in range(8):
        wc = f"Write Clk Gen:WRITE_CLK_{i}"
        by = f"Byte {i}:WRITE_CLK"
        if not on_same_net(wc, by):
            issues.append(f"  {wc} not connected to {by}")

    # 5. BUF_OE_i: read_oe_gen -> byte_i
    for i in range(8):
        ro = f"Read OE Gen:BUF_OE_{i}"
        by = f"Byte {i}:BUF_OE"
        if not on_same_net(ro, by):
            issues.append(f"  {ro} not connected to {by}")

    # 6. D0-D7: all byte sheet D_i pins connected via labels
    for bit in range(8):
        lbl = f"label:D{bit}"
        for byte_idx in range(8):
            pin_id = f"Byte {byte_idx}:D{bit}"
            if not on_same_net(lbl, pin_id):
                issues.append(f"  {pin_id} not on label D{bit} net")

    # 7. A0-A2: connector -> address decoder (via wires)
    for i in range(3):
        ad = f"Address Decoder:A{i}"
        if not id_exists(ad):
            issues.append(f"  {ad} not found in any net")

    # 8. nCE/nOE/nWE: connector -> control logic (via wires)
    for sig in ["nCE", "nOE", "nWE"]:
        cl = f"Control Logic:{sig}"
        if not id_exists(cl):
            issues.append(f"  {cl} not found in any net")

    # -- Check signal isolation (different signals not merged) --
    isolation_pairs = [
        ("Address Decoder:A0", "Address Decoder:A1"),
        ("Address Decoder:A0", "Address Decoder:A2"),
        ("Address Decoder:A1", "Address Decoder:A2"),
        ("Control Logic:nCE", "Control Logic:nOE"),
        ("Control Logic:nCE", "Control Logic:nWE"),
        ("Control Logic:nOE", "Control Logic:nWE"),
        ("Address Decoder:SEL0", "Address Decoder:SEL1"),
        ("Address Decoder:SEL0", "Address Decoder:SEL7"),
        ("Write Clk Gen:WRITE_CLK_0", "Write Clk Gen:WRITE_CLK_1"),
        ("Write Clk Gen:WRITE_CLK_0", "Write Clk Gen:WRITE_CLK_7"),
        ("Read OE Gen:BUF_OE_0", "Read OE Gen:BUF_OE_1"),
        ("Read OE Gen:BUF_OE_0", "Read OE Gen:BUF_OE_7"),
        ("label:D0", "Address Decoder:A0"),
        ("label:D0", "Control Logic:nCE"),
        ("Control Logic:WRITE_ACTIVE", "Control Logic:READ_EN"),
    ]
    for id_a, id_b in isolation_pairs:
        if on_same_net(id_a, id_b):
            issues.append(f"  NET MERGE: {id_a} and {id_b} on same net!")

    return issues


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

    # -- Per-file checks (using shared run_all_checks) --
    for sch_file in SCHEMATIC_FILES:
        filepath = os.path.join(BOARD_DIR, sch_file)
        if not os.path.exists(filepath):
            print(f"\n  SKIP {sch_file} (not found)")
            continue

        print(f"\n--- {sch_file} ---")
        data = parse_schematic(filepath)
        file_results = run_all_checks(filepath, data)

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

    # -- Netlist connectivity check --
    print(f"\n--- Netlist: ram.kicad_sch ---")
    netlist_issues = check_netlist()
    if netlist_issues:
        print(f"  [ERROR] Netlist Connectivity: {len(netlist_issues)}")
        for issue in netlist_issues:
            print(issue)
        total_errors += len(netlist_issues)
    else:
        print("  All expected connections verified")

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
                f"  ERC: {erc_errors} error(s), {erc_warnings} warning(s)"
            )

        # Per-sub-sheet standalone ERC
        for sch_file in SCHEMATIC_FILES:
            if sch_file == "ram.kicad_sch":
                continue
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
    total_issues = total_errors + total_warnings
    print(f"\n{'=' * 60}")
    if total_issues > 0:
        print(f"FAILED: {total_errors} error(s), {total_warnings} warning(s)")
    else:
        print(f"PASSED: 0 errors, 0 warnings")
    print(f"Report: {report_path}")
    print(f"{'=' * 60}")

    return 1 if total_issues > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
