#!/usr/bin/env python3
"""Parse ERC results JSON and show detailed violations for specific sheets."""
import json
import sys

with open(sys.argv[1] if len(sys.argv) > 1 else "erc_results.json") as f:
    data = json.load(f)

# Optional filter: sheet path substring
sheet_filter = sys.argv[2] if len(sys.argv) > 2 else None
# Optional filter: violation type
type_filter = sys.argv[3] if len(sys.argv) > 3 else None

for sheet in data.get("sheets", []):
    path = sheet.get("path", "")
    if sheet_filter and sheet_filter not in path:
        continue
    violations = sheet.get("violations", [])
    for v in violations:
        if type_filter and v["type"] != type_filter:
            continue
        desc = v.get("description", "")
        vtype = v["type"]
        items = v.get("items", [])
        print(f"[{path}] {vtype}: {desc}")
        for item in items:
            pos = item.get("pos", {})
            x = pos.get("x", 0) * 100
            y = pos.get("y", 0) * 100
            print(f"  - {item.get('description', '')} @ ({x:.2f}, {y:.2f})")
        print()
