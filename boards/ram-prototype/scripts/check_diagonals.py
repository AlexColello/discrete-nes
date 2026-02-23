#!/usr/bin/env python3
"""Check for diagonal wires in generated schematics."""
import os
from kiutils.schematic import Schematic

board_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

sheets = ["address_decoder.kicad_sch", "control_logic.kicad_sch",
          "write_clk_gen.kicad_sch", "read_oe_gen.kicad_sch",
          "byte.kicad_sch"]

for fname in sheets:
    sch = Schematic.from_file(os.path.join(board_dir, fname))
    diags = []
    for item in sch.graphicalItems:
        if hasattr(item, 'type') and item.type == 'wire' and len(item.points) >= 2:
            p1, p2 = item.points[0], item.points[1]
            x1, y1 = round(p1.X, 2), round(p1.Y, 2)
            x2, y2 = round(p2.X, 2), round(p2.Y, 2)
            if x1 != x2 and y1 != y2:
                diags.append(((x1, y1), (x2, y2)))
    if diags:
        print(f"{fname}: {len(diags)} diagonal wires!")
        for (x1, y1), (x2, y2) in diags:
            print(f"  ({x1}, {y1}) -> ({x2}, {y2})")
    else:
        print(f"{fname}: no diagonal wires OK")
