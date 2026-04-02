#!/usr/bin/env python3
"""
Generate manufacturing files for the RAM prototype board (PCBWay target).

Outputs (to boards/ram-prototype/manufacturing/):
  - gerbers/               Gerber + Excellon drill files
  - ram-prototype-gerbers.zip   All gerbers + drills zipped for upload
  - ram-prototype-bom.csv       BOM for SMT assembly (connectors excluded)
  - ram-prototype-cpl.csv       Component Placement List (SMD only)

Usage:
    cd boards/ram-prototype
    python scripts/generate_manufacturing.py
"""

import csv
import os
import subprocess
import sys
import zipfile
from collections import defaultdict

# Add shared library to path
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen.common import KICAD_CLI

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------

BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
PCB_ROUTED = os.path.join(BOARD_DIR, "ram_routed.kicad_pcb")
SCHEMATIC = os.path.join(BOARD_DIR, "ram.kicad_sch")
OUT_DIR = os.path.join(BOARD_DIR, "manufacturing")
GERBER_DIR = os.path.join(OUT_DIR, "gerbers")
ZIP_NAME = "ram-prototype-gerbers.zip"
BOM_NAME = "ram-prototype-bom.csv"
CPL_NAME = "ram-prototype-cpl.csv"

# Layers to include in gerber export
GERBER_LAYERS = ",".join([
    "F.Cu", "In1.Cu", "In2.Cu", "B.Cu",
    "F.SilkS", "B.SilkS",
    "F.Mask", "B.Mask",
    "F.Paste", "B.Paste",
    "Edge.Cuts",
    "F.Fab", "B.Fab",
])

# Schematic Value -> Manufacturer Part Number
# LED MPN left blank -- update before ordering.
MPN_MAP = {
    "74LVC1G04":  "SN74LVC1G04YZPR",
    "74LVC1G08":  "SN74LVC1G08YZPR",
    "74LVC1G79":  "SN74LVC1G79YZPR",
    "74LVC1G125": "SN74LVC1G125YZPR",
    "74LVC2G00":  "SN74LVC2G00YZPR",
    "Red":        "",  # TODO: select 0402 red LED (e.g. Kingbright KPHHS-1005SURCK)
    "680R":       "RC0402FR-07680RL",  # Yageo 680R 1% 1/16W
}


def run(cmd, desc):
    """Run a command, print description, check for errors."""
    print(f"  {desc}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  FAILED: {' '.join(cmd)}")
        if result.stderr:
            print(result.stderr)
        sys.exit(1)
    return result


# --------------------------------------------------------------
# Step 1: Gerber export
# --------------------------------------------------------------

def export_gerbers():
    """Export all gerber layers from the routed PCB."""
    print("\n=== Step 1: Export Gerbers ===")
    os.makedirs(GERBER_DIR, exist_ok=True)
    run([
        KICAD_CLI, "pcb", "export", "gerbers",
        "--output", GERBER_DIR + os.sep,
        "--layers", GERBER_LAYERS,
        "--no-x2",
        "--precision", "6",
        PCB_ROUTED,
    ], "Exporting gerber layers")

    gerber_files = [f for f in os.listdir(GERBER_DIR)
                    if not f.endswith((".drl", ".zip"))]
    print(f"  Generated {len(gerber_files)} gerber files")
    return gerber_files


# --------------------------------------------------------------
# Step 2: Drill export
# --------------------------------------------------------------

def export_drill():
    """Export Excellon drill files."""
    print("\n=== Step 2: Export Drill Files ===")
    os.makedirs(GERBER_DIR, exist_ok=True)
    run([
        KICAD_CLI, "pcb", "export", "drill",
        "--output", GERBER_DIR + os.sep,
        "--format", "excellon",
        "--excellon-units", "mm",
        "--excellon-zeros-format", "decimal",
        "--excellon-separate-th",
        "--generate-map",
        "--map-format", "gerberx2",
        PCB_ROUTED,
    ], "Exporting drill files")

    drill_files = [f for f in os.listdir(GERBER_DIR) if f.endswith(".drl")]
    print(f"  Generated {len(drill_files)} drill file(s)")
    return drill_files


# --------------------------------------------------------------
# Step 3: Zip gerbers + drill
# --------------------------------------------------------------

def zip_gerbers():
    """Zip all gerber and drill files for upload."""
    print("\n=== Step 3: Zip Gerbers + Drill ===")
    zip_path = os.path.join(OUT_DIR, ZIP_NAME)
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(os.listdir(GERBER_DIR)):
            filepath = os.path.join(GERBER_DIR, f)
            if os.path.isfile(filepath):
                zf.write(filepath, f)
                count += 1
    size_kb = os.path.getsize(zip_path) / 1024
    print(f"  {zip_path}")
    print(f"  {count} files, {size_kb:.0f} KB")
    return zip_path


# --------------------------------------------------------------
# Step 4: BOM
# --------------------------------------------------------------

def export_bom():
    """Generate BOM by cross-referencing schematic values with PCB components.

    Connectors are excluded via in_bom=False in the schematic (set in
    generate_ram.py).  Power supply parts not yet placed on the PCB are
    excluded by cross-referencing with the PCB pos file.
    """
    print("\n=== Step 4: Generate BOM ===")

    # 1. Get refs actually on the PCB (from pos export, SMD only)
    tmp_pos = os.path.join(OUT_DIR, "_tmp_pos_all.csv")
    run([
        KICAD_CLI, "pcb", "export", "pos",
        "--format", "csv",
        "--units", "mm",
        "--side", "both",
        "--exclude-fp-th",
        "-o", tmp_pos,
        PCB_ROUTED,
    ], "Reading component list from PCB")

    pcb_refs = set()
    pcb_packages = {}  # ref -> package from PCB (actual footprint)
    with open(tmp_pos, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ref = row["Ref"].strip('"')
            pcb_refs.add(ref)
            pcb_packages[ref] = row["Package"].strip('"')
    os.remove(tmp_pos)

    # 2. Get values from schematic BOM (connectors already excluded via in_bom=False)
    tmp_bom = os.path.join(OUT_DIR, "_tmp_sch_bom.csv")
    run([
        KICAD_CLI, "sch", "export", "bom",
        "--fields", "Reference,Value,Footprint",
        "--labels", "Designator,Value,Footprint",
        "--sort-field", "Reference",
        "-o", tmp_bom,
        SCHEMATIC,
    ], "Reading values from schematic")

    # Parse schematic BOM: expand grouped refs to individual ref -> value
    sch_values = {}  # ref -> value
    with open(tmp_bom, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            designator = row["Designator"].strip('"')
            value = row["Value"].strip('"')
            for ref in _expand_refs(designator):
                sch_values[ref] = value
    os.remove(tmp_bom)

    # 3. Cross-reference: only refs on the PCB, use schematic values
    groups = defaultdict(list)  # (value, package) -> [refs]
    for ref in sorted(pcb_refs, key=_sort_key):
        value = sch_values.get(ref, pcb_packages.get(ref, "UNKNOWN"))
        package = pcb_packages.get(ref, "")
        groups[(value, package)].append(ref)

    # Write BOM CSV in PCBWay format
    bom_path = os.path.join(OUT_DIR, BOM_NAME)
    with open(bom_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Designator", "Qty", "Value", "Package", "MPN"])
        for (val, pkg), refs in sorted(groups.items(),
                                        key=lambda x: _sort_key(x[1][0])):
            refs_sorted = sorted(refs, key=_sort_key)
            designator = _compress_refs(refs_sorted)
            mpn = MPN_MAP.get(val, "")
            writer.writerow([designator, len(refs), val, pkg, mpn])

    total = sum(len(refs) for refs in groups.values())
    missing_mpn = [val for (val, _) in groups if not MPN_MAP.get(val, "")]
    print(f"  {bom_path}")
    print(f"  {len(groups)} unique parts, {total} total components")
    if missing_mpn:
        print(f"  WARNING: missing MPN for: {', '.join(missing_mpn)}")
    if total != len(pcb_refs):
        print(f"  WARNING: BOM has {total} parts but PCB has {len(pcb_refs)} SMD refs")
    return bom_path


def _expand_refs(designator_str):
    """Expand grouped designator string into individual refs.

    Examples: 'R1-R5' -> ['R1','R2','R3','R4','R5']
              'R1,R3,R5-R7' -> ['R1','R3','R5','R6','R7']
    """
    refs = []
    for part in designator_str.split(","):
        part = part.strip()
        if "-" in part:
            # Range: R1-R5
            chunks = part.split("-", 1)
            prefix = chunks[0].rstrip("0123456789")
            start = int(chunks[0][len(prefix):])
            end_prefix = chunks[1].rstrip("0123456789")
            end = int(chunks[1][len(end_prefix):])
            for i in range(start, end + 1):
                refs.append(f"{prefix}{i}")
        else:
            refs.append(part)
    return refs


def _sort_key(ref):
    """Sort reference designators naturally: D1, D2, ..., D10, D11."""
    prefix = ref.rstrip("0123456789")
    num = ref[len(prefix):]
    return (prefix, int(num) if num else 0)


def _compress_refs(refs):
    """Compress sorted references into range notation: D1,D2,D3 -> D1-D3."""
    if not refs:
        return ""
    groups = []
    current_prefix = refs[0].rstrip("0123456789")
    current_start = int(refs[0][len(current_prefix):] or "0")
    current_end = current_start

    for ref in refs[1:]:
        prefix = ref.rstrip("0123456789")
        num = int(ref[len(prefix):] or "0")
        if prefix == current_prefix and num == current_end + 1:
            current_end = num
        else:
            groups.append(_format_range(current_prefix, current_start, current_end))
            current_prefix = prefix
            current_start = num
            current_end = num

    groups.append(_format_range(current_prefix, current_start, current_end))
    return ",".join(groups)


def _format_range(prefix, start, end):
    """Format a range: (D, 1, 3) -> 'D1-D3', (D, 5, 5) -> 'D5'."""
    if start == end:
        return f"{prefix}{start}"
    return f"{prefix}{start}-{prefix}{end}"


# --------------------------------------------------------------
# Step 5: CPL (Component Placement List / Centroid)
# --------------------------------------------------------------

def export_cpl():
    """Export pick-and-place file for SMD components only."""
    print("\n=== Step 5: Generate CPL ===")

    tmp_cpl = os.path.join(OUT_DIR, "_tmp_cpl.csv")
    run([
        KICAD_CLI, "pcb", "export", "pos",
        "--format", "csv",
        "--units", "mm",
        "--side", "both",
        "--exclude-fp-th",
        "--exclude-dnp",
        "-o", tmp_cpl,
        PCB_ROUTED,
    ], "Exporting SMD component positions")

    # Rewrite with PCBWay-compatible column names
    cpl_path = os.path.join(OUT_DIR, CPL_NAME)
    count = 0
    with open(tmp_cpl, "r", newline="") as fin, \
         open(cpl_path, "w", newline="") as fout:
        reader = csv.DictReader(fin)
        writer = csv.writer(fout)
        writer.writerow(["Designator", "Mid X", "Mid Y", "Rotation", "Side"])
        for row in reader:
            writer.writerow([
                row["Ref"].strip('"'),
                row["PosX"].strip('"'),
                row["PosY"].strip('"'),
                row["Rot"].strip('"'),
                row["Side"].strip('"'),
            ])
            count += 1

    os.remove(tmp_cpl)
    print(f"  {cpl_path}")
    print(f"  {count} SMD components")
    return cpl_path, count


# --------------------------------------------------------------
# Main
# --------------------------------------------------------------

def main():
    print("=" * 60)
    print("RAM Prototype — Manufacturing File Generation (PCBWay)")
    print("=" * 60)

    if not os.path.isfile(PCB_ROUTED):
        print(f"\nERROR: Routed PCB not found: {PCB_ROUTED}")
        print("Run route_pcb.py first.")
        sys.exit(1)

    os.makedirs(OUT_DIR, exist_ok=True)

    gerber_files = export_gerbers()
    drill_files = export_drill()
    zip_path = zip_gerbers()
    bom_path = export_bom()
    cpl_path, smd_count = export_cpl()

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Gerbers:  {len(gerber_files)} layers + {len(drill_files)} drill file(s)")
    print(f"  Zip:      {zip_path}")
    print(f"  BOM:      {bom_path}")
    print(f"  CPL:      {cpl_path} ({smd_count} SMD parts)")
    print()
    print("Upload to PCBWay:")
    print("  1. Gerber zip  -> PCB fabrication order")
    print("  2. BOM + CPL   -> Assembly order (add MPN column before upload)")
    print()


if __name__ == "__main__":
    main()
