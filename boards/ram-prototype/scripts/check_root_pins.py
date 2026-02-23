#!/usr/bin/env python3
"""Check root sheet hierarchy pin-label-wire alignment."""
import os
from kiutils.schematic import Schematic

board_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sch = Schematic.from_file(os.path.join(board_dir, "ram.kicad_sch"))

# Collect wire endpoints
wire_endpoints = set()
wire_segments = []
for item in sch.graphicalItems:
    if hasattr(item, 'type') and item.type == 'wire':
        p1 = (round(item.points[0].X, 2), round(item.points[0].Y, 2))
        p2 = (round(item.points[1].X, 2), round(item.points[1].Y, 2))
        wire_endpoints.add(p1)
        wire_endpoints.add(p2)
        wire_segments.append((p1, p2))

# Collect labels
labels = {}
for lbl in sch.labels:
    pos = (round(lbl.position.X, 2), round(lbl.position.Y, 2))
    labels.setdefault(lbl.text, []).append((pos, lbl.position.angle, pos in wire_endpoints))

# Print labels grouped by name, showing which are on wires
for name in sorted(labels.keys()):
    entries = labels[name]
    on_wire = all(e[2] for e in entries)
    count = len(entries)
    dangling = count == 1  # Single label = no connection partner
    if not on_wire or dangling:
        status = "NOT_ON_WIRE" if not on_wire else f"SINGLE_LABEL(count={count})"
        print(f"  {name}: {status}")
        for pos, angle, on_w in entries:
            print(f"    @ {pos} angle={angle} on_wire={on_w}")

print(f"\nTotal unique label names: {len(labels)}")
single_labels = [name for name, entries in labels.items() if len(entries) == 1]
print(f"Labels with only 1 instance: {len(single_labels)}")
for name in sorted(single_labels):
    entries = labels[name]
    print(f"  {name} @ {entries[0][0]} angle={entries[0][1]}")

# Check hierarchy sheet pins vs sub-sheet hier labels
print("\n--- Hierarchy Sheet Pins ---")
for sheet in sch.sheets:
    sheet_name = sheet.sheetName.value if sheet.sheetName else "?"
    fname = sheet.fileName.value if sheet.fileName else "?"
    print(f"\n{sheet_name} ({fname}):")
    for pin in sheet.pins:
        pos = (round(pin.position.X, 2), round(pin.position.Y, 2))
        on_wire = pos in wire_endpoints
        print(f"  pin '{pin.name}' type={pin.connectionType} @ {pos} angle={pin.position.angle} on_wire={on_wire}")

    # Check if sub-sheet has matching hier labels
    sub_path = os.path.join(board_dir, fname)
    if os.path.exists(sub_path):
        sub_sch = Schematic.from_file(sub_path)
        sub_labels = {hl.text for hl in sub_sch.hierarchicalLabels}
        pin_names = {p.name for p in sheet.pins}
        missing_in_sub = pin_names - sub_labels
        extra_in_sub = sub_labels - pin_names
        if missing_in_sub:
            print(f"  MISSING in sub-sheet: {missing_in_sub}")
        if extra_in_sub:
            print(f"  EXTRA in sub-sheet: {extra_in_sub}")
