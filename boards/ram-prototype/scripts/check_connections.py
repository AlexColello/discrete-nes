#!/usr/bin/env python3
"""Check wire-label connectivity in generated .kicad_sch files.

For each hierarchical label, check if there's a wire endpoint at the label position.
"""
import os
import sys
from kiutils.schematic import Schematic

board_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

def check_sheet(filename):
    path = os.path.join(board_dir, filename)
    if not os.path.exists(path):
        print(f"  {filename}: NOT FOUND")
        return
    sch = Schematic.from_file(path)

    # Collect all wire endpoints
    wire_endpoints = set()
    for item in sch.graphicalItems:
        if hasattr(item, 'type') and item.type == 'wire':
            for pt in item.points:
                wire_endpoints.add((round(pt.X, 2), round(pt.Y, 2)))

    # Check hierarchical labels
    dangling = []
    for hl in sch.hierarchicalLabels:
        pos = (round(hl.position.X, 2), round(hl.position.Y, 2))
        if pos not in wire_endpoints:
            dangling.append((hl.text, pos, hl.position.angle))

    if dangling:
        print(f"  {filename}: {len(dangling)} dangling hier labels")
        for name, pos, angle in dangling:
            print(f"    '{name}' @ {pos} angle={angle}")
            # Find nearest wire endpoint
            min_dist = float('inf')
            nearest = None
            for ep in wire_endpoints:
                d = ((ep[0]-pos[0])**2 + (ep[1]-pos[1])**2)**0.5
                if d < min_dist:
                    min_dist = d
                    nearest = ep
            if nearest:
                print(f"      nearest wire endpoint: {nearest} (dist={min_dist:.2f})")
    else:
        print(f"  {filename}: all hier labels on wire endpoints OK")

    # Check local labels
    label_dangling = []
    for lbl in sch.labels:
        pos = (round(lbl.position.X, 2), round(lbl.position.Y, 2))
        if pos not in wire_endpoints:
            label_dangling.append((lbl.text, pos, lbl.position.angle))

    if label_dangling:
        print(f"  {filename}: {len(label_dangling)} dangling local labels")
        for name, pos, angle in label_dangling:
            print(f"    '{name}' @ {pos} angle={angle}")
            min_dist = float('inf')
            nearest = None
            for ep in wire_endpoints:
                d = ((ep[0]-pos[0])**2 + (ep[1]-pos[1])**2)**0.5
                if d < min_dist:
                    min_dist = d
                    nearest = ep
            if nearest:
                print(f"      nearest wire endpoint: {nearest} (dist={min_dist:.2f})")

    # Check for wire-to-pin connections (wire endpoint at a symbol pin position)
    pin_positions = set()
    for sym in sch.schematicSymbols:
        cx = round(sym.position.X, 2)
        cy = round(sym.position.Y, 2)
        # Can't compute pin positions without offset data, skip

    # Check unconnected wire endpoints (endpoints that don't match any other endpoint or pin)
    # Count each endpoint occurrence
    ep_counts = {}
    for item in sch.graphicalItems:
        if hasattr(item, 'type') and item.type == 'wire':
            for pt in item.points:
                key = (round(pt.X, 2), round(pt.Y, 2))
                ep_counts[key] = ep_counts.get(key, 0) + 1

    # An endpoint with count=1 that doesn't match a label or symbol pin is potentially dangling
    print(f"  {filename}: {len(wire_endpoints)} unique wire endpoints, {sum(ep_counts.values())} total")


sheets = ["address_decoder.kicad_sch", "control_logic.kicad_sch",
          "write_clk_gen.kicad_sch", "read_oe_gen.kicad_sch",
          "byte.kicad_sch", "ram.kicad_sch"]

for s in sheets:
    check_sheet(s)
    print()
