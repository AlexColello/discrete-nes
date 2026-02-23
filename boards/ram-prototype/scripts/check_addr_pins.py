#!/usr/bin/env python3
"""Check address decoder wire-to-pin connectivity."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

# Import generate_ram to access pin offsets
import generate_ram as gen

board_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
from kiutils.schematic import Schematic

# Get pin offsets
offsets = gen.get_pin_offsets()

# Load address decoder schematic
sch = Schematic.from_file(os.path.join(board_dir, "address_decoder.kicad_sch"))

# Collect wire endpoints with extra precision
wire_endpoints = set()
for item in sch.graphicalItems:
    if hasattr(item, 'type') and item.type == 'wire':
        for pt in item.points:
            wire_endpoints.add((round(pt.X, 4), round(pt.Y, 4)))

# For each schematic symbol, compute pin positions and check wire connectivity
for sym in sch.schematicSymbols:
    ref = "?"
    for p in sym.properties:
        if p.key == "Reference":
            ref = p.value
            break

    if not ref.startswith("U"):
        continue

    lib_id = sym.libId.split(":")[-1] if ":" in sym.libId else sym.libId
    if lib_id != "74LVC1G11":
        continue

    cx = round(sym.position.X, 4)
    cy = round(sym.position.Y, 4)
    angle = sym.position.angle if sym.position.angle else 0

    key = (lib_id, angle)
    if key not in offsets:
        print(f"  {ref}: no offsets for {key}")
        continue

    print(f"\n{ref} ({lib_id}) @ ({cx}, {cy}) angle={angle}")
    for pin_num, (dx, dy) in sorted(offsets[key].items()):
        px = round(cx + dx, 4)
        py = round(cy + dy, 4)
        # Check if any wire endpoint matches
        on_wire = (px, py) in wire_endpoints
        # Also check with 2-decimal precision
        on_wire_2 = (round(px, 2), round(py, 2)) in {(round(x, 2), round(y, 2)) for x, y in wire_endpoints}

        # Find wires that have an endpoint near this pin
        nearby = []
        for ep in wire_endpoints:
            d = ((ep[0]-px)**2 + (ep[1]-py)**2)**0.5
            if d < 0.1 and d > 0:
                nearby.append((ep, d))

        status = "OK" if on_wire else ("OK-2dp" if on_wire_2 else "MISSING")
        print(f"  Pin {pin_num}: ({px}, {py}) offset=({dx},{dy}) wire={status}")
        if nearby:
            for ep, d in nearby:
                print(f"    near: {ep} dist={d:.6f}")

# Print pin offsets for reference
print("\n--- Pin offsets for 74LVC1G11 ---")
key = ("74LVC1G11", 0)
if key in offsets:
    for pin_num, (dx, dy) in sorted(offsets[key].items()):
        print(f"  Pin {pin_num}: dx={dx}, dy={dy}")
