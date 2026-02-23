#!/usr/bin/env python3
"""Print all discovered pin offsets."""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))
import generate_ram as gen
offsets = gen.get_pin_offsets()
for key in sorted(offsets.keys(), key=str):
    sym, angle = key
    print(f"\n{sym} angle={angle}:")
    for pin, (dx, dy) in sorted(offsets[key].items()):
        print(f"  Pin {pin}: dx={dx}, dy={dy}")
