#!/usr/bin/env python3
"""Parse ERC results JSON and summarize violations."""
import json
import sys

with open(sys.argv[1] if len(sys.argv) > 1 else "erc_results.json") as f:
    data = json.load(f)

for sheet in data.get("sheets", []):
    path = sheet.get("path", "")
    violations = sheet.get("violations", [])
    if violations:
        counts = {}
        for v in violations:
            t = v["type"]
            counts[t] = counts.get(t, 0) + 1
        print(f"{path}: {sum(counts.values())} total")
        for t, c in sorted(counts.items()):
            print(f"  {t}: {c}")

total = sum(len(s.get("violations", [])) for s in data.get("sheets", []))
print(f"\nTOTAL: {total} violations")
