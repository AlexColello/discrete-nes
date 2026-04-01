#!/usr/bin/env python3
"""
Verification script for 2KB RAM board schematics.

Checks:
  1-11. General checks via shared kicad_gen.verify module
  12. Netlist connectivity (board-specific expected connections)
  13. ERC via kicad-cli on the root schematic

Usage:
    python scripts/verify_schematics.py          # Run all checks + ERC
    python scripts/verify_schematics.py --no-erc  # Skip kicad-cli ERC
"""

import os
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen.common import KICAD_CLI, snap
from kicad_gen.verify import (
    parse_schematic, run_all_checks, run_erc, UnionFind,
    _extract_lib_pins, _pin_schematic_offset, TOLERANCE,
)
from kiutils.schematic import Schematic

# --------------------------------------------------------------
BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_DIR = os.path.join(BOARD_DIR, "verify_output")

NUM_ROW_GROUPS = 16
ROWS_PER_GROUP = 8
NUM_COLS = 16

SCHEMATIC_FILES = [
    "ram.kicad_sch",
    "address_decoder.kicad_sch",
    "column_select.kicad_sch",
    "control_logic.kicad_sch",
    "row_control.kicad_sch",
    "byte.kicad_sch",
    "power_supply.kicad_sch",
    "row_group.kicad_sch",
    "row.kicad_sch",
]


def check_netlist():
    """Verify root sheet netlist connectivity for full 2KB RAM.

    Checks that DEC4 signals from address decoder reach row group blocks,
    and that global labels (DEC3, COL_SEL, WRITE_ACTIVE, READ_EN, D0-D7)
    and address/control signals are properly connected.
    """
    filepath = os.path.join(BOARD_DIR, "ram.kicad_sch")
    if not os.path.exists(filepath):
        return ["  ram.kicad_sch not found"]

    sch = Schematic.from_file(filepath)
    uf = UnionFind()

    # Collect wires
    wires = []
    for item in sch.graphicalItems:
        if getattr(item, 'type', None) == 'wire':
            pts = item.points
            if len(pts) >= 2:
                p1 = (snap(pts[0].X), snap(pts[0].Y))
                p2 = (snap(pts[1].X), snap(pts[1].Y))
                wires.append((p1, p2))
                uf.union(p1, p2)

    all_pts = set()
    for p1, p2 in wires:
        all_pts.add(p1)
        all_pts.add(p2)

    # Sheet pins
    sheet_pin_ids = {}
    for sheet in sch.sheets:
        sname = sheet.sheetName.value
        for pin in sheet.pins:
            pt = (snap(pin.position.X), snap(pin.position.Y))
            sheet_pin_ids[pt] = f"{sname}:{pin.name}"
            all_pts.add(pt)

    # Labels (local)
    label_pts = {}
    for lbl in getattr(sch, 'labels', []):
        pt = (snap(lbl.position.X), snap(lbl.position.Y))
        label_pts[pt] = lbl.text
        all_pts.add(pt)

    # Global labels
    global_label_pts = {}
    for lbl in getattr(sch, 'globalLabels', []):
        pt = (snap(lbl.position.X), snap(lbl.position.Y))
        global_label_pts[pt] = lbl.text
        all_pts.add(pt)

    # Junctions
    for j in sch.junctions:
        all_pts.add((snap(j.position.X), snap(j.position.Y)))

    # Merge points touching wires
    for pt in all_pts:
        for (x1, y1), (x2, y2) in wires:
            if abs(y1 - y2) < TOLERANCE:
                xmin, xmax = min(x1, x2), max(x1, x2)
                if (abs(pt[1] - y1) < TOLERANCE and
                        xmin - TOLERANCE <= pt[0] <= xmax + TOLERANCE):
                    uf.union(pt, (x1, y1))
            elif abs(x1 - x2) < TOLERANCE:
                ymin, ymax = min(y1, y2), max(y1, y2)
                if (abs(pt[0] - x1) < TOLERANCE and
                        ymin - TOLERANCE <= pt[1] <= ymax + TOLERANCE):
                    uf.union(pt, (x1, y1))

    # Merge same-name labels
    for pts_dict in [label_pts, global_label_pts]:
        label_groups = defaultdict(list)
        for pt, name in pts_dict.items():
            label_groups[name].append(pt)
        for name, pts_list in label_groups.items():
            for i in range(1, len(pts_list)):
                uf.union(pts_list[0], pts_list[i])

    # Build net membership
    nets = defaultdict(set)
    for pt, sid in sheet_pin_ids.items():
        nets[uf.find(pt)].add(sid)
    for pt, name in label_pts.items():
        nets[uf.find(pt)].add(f"label:{name}")
    for pt, name in global_label_pts.items():
        nets[uf.find(pt)].add(f"global:{name}")

    def on_same_net(id_a, id_b):
        return any(id_a in m and id_b in m for m in nets.values())

    def id_exists(identifier):
        return any(identifier in m for m in nets.values())

    issues = []

    # 1. DEC4_0..15: address decoder -> row group blocks
    for i in range(NUM_ROW_GROUPS):
        ad = f"Address Decoder:DEC4_{i}"
        rg = f"Row Group {i}:DEC4"
        if not on_same_net(ad, rg):
            issues.append(f"  {ad} not connected to {rg}")

    # 2. DEC3_0..7: address decoder -> global labels
    for i in range(ROWS_PER_GROUP):
        ad = f"Address Decoder:DEC3_{i}"
        gl = f"global:DEC3_{i}"
        if not on_same_net(ad, gl):
            issues.append(f"  {ad} not connected to global DEC3_{i}")

    # 3. COL_SEL_0..15: column select -> global labels
    for i in range(NUM_COLS):
        cs = f"Column Select:COL_SEL_{i}"
        gl = f"global:COL_SEL_{i}"
        if not on_same_net(cs, gl):
            issues.append(f"  {cs} not connected to global COL_SEL_{i}")

    # 4. WRITE_ACTIVE, READ_EN: control logic -> global labels
    for sig in ["WRITE_ACTIVE", "READ_EN"]:
        cl = f"Control Logic:{sig}"
        gl = f"global:{sig}"
        if not on_same_net(cl, gl):
            issues.append(f"  {cl} not connected to global {sig}")

    # 5. D0..7: connector -> global labels
    for i in range(8):
        gl = f"global:D{i}"
        if not id_exists(gl):
            issues.append(f"  global:D{i} not found in any net")

    # 6. A0-A6 -> address decoder
    for i in range(7):
        if not id_exists(f"Address Decoder:A{i}"):
            issues.append(f"  Address Decoder:A{i} not found in any net")

    # 7. A7-A10 -> column select
    for i in range(7, 11):
        if not id_exists(f"Column Select:A{i}"):
            issues.append(f"  Column Select:A{i} not found in any net")

    # 8. nCE/nOE/nWE -> control logic
    for sig in ["nCE", "nOE", "nWE"]:
        if not id_exists(f"Control Logic:{sig}"):
            issues.append(f"  Control Logic:{sig} not found in any net")

    # Signal isolation checks
    isolation_pairs = [
        ("Address Decoder:A0", "Address Decoder:A1"),
        ("Address Decoder:A0", "Address Decoder:A6"),
        ("Column Select:A7", "Column Select:A8"),
        ("Control Logic:nCE", "Control Logic:nOE"),
        ("Control Logic:nCE", "Control Logic:nWE"),
        ("global:WRITE_ACTIVE", "global:READ_EN"),
        ("global:COL_SEL_0", "global:COL_SEL_1"),
        ("global:COL_SEL_0", "global:COL_SEL_15"),
        ("global:DEC3_0", "global:DEC3_1"),
        ("global:D0", "Address Decoder:A0"),
        ("global:D0", "global:WRITE_ACTIVE"),
        ("Address Decoder:DEC4_0", "Address Decoder:DEC4_1"),
        ("Row Group 0:DEC4", "Row Group 1:DEC4"),
    ]
    for id_a, id_b in isolation_pairs:
        if on_same_net(id_a, id_b):
            issues.append(f"  NET MERGE: {id_a} and {id_b} on same net!")

    return issues


def main():
    skip_erc = "--no-erc" in sys.argv
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total_errors = 0
    total_warnings = 0
    all_results = {}

    print("=" * 60)
    print("2KB RAM Schematic Verification")
    print("=" * 60)

    # Per-file checks
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

    # Netlist connectivity
    print(f"\n--- Netlist: ram.kicad_sch ---")
    netlist_issues = check_netlist()
    if netlist_issues:
        print(f"  [ERROR] Netlist Connectivity: {len(netlist_issues)}")
        for issue in netlist_issues:
            print(issue)
        total_errors += len(netlist_issues)
    else:
        print("  All expected connections verified")

    # ERC
    if not skip_erc:
        root_sch = os.path.join(BOARD_DIR, "ram.kicad_sch")
        if os.path.exists(root_sch):
            print(f"\n--- ERC: ram.kicad_sch (root, full hierarchy) ---")
            print("  (This may take several minutes with 2048 byte instances)")
            erc_issues, erc_errors, erc_warnings = run_erc(
                root_sch, OUTPUT_DIR, label="root")
            if erc_issues:
                for issue in erc_issues:
                    print(issue)
            total_errors += erc_errors
            total_warnings += erc_warnings
            print(f"  ERC: {erc_errors} error(s), {erc_warnings} warning(s)")
    else:
        print(f"\n--- ERC skipped (--no-erc) ---")

    # Summary
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
