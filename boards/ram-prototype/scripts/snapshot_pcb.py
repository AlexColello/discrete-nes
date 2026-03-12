#!/usr/bin/env python3
"""Export a cropped PNG snapshot of a PCB region for visual inspection.

Usage:
    python snapshot_pcb.py ram.kicad_pcb --bbox 20,20,60,40
    python snapshot_pcb.py ram.kicad_pcb --bbox 20,20,60,40 --layers F.Cu,Edge.Cuts
    python snapshot_pcb.py ram.kicad_pcb --bbox 20,20,60,40 -o detail.png
    python snapshot_pcb.py ram.kicad_pcb                      # full board
    python snapshot_pcb.py ram.kicad_pcb --outline             # show board coords
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen.snapshot import (
    find_board_outline, snapshot_region, DEFAULT_LAYERS,
)


def main():
    parser = argparse.ArgumentParser(
        description="Export a cropped PNG snapshot of a PCB region.")
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument("--bbox", "-b",
                        help="Bounding box in PCB mm coords: x1,y1,x2,y2")
    parser.add_argument("--layers", "-l", default=DEFAULT_LAYERS,
                        help=f"Comma-separated layer list (default: {DEFAULT_LAYERS})")
    parser.add_argument("--output", "-o",
                        help="Output PNG path (default: snapshot.png next to PCB)")
    parser.add_argument("--outline", action="store_true",
                        help="Print board outline coordinates and exit")
    args = parser.parse_args()

    pcb_path = os.path.abspath(args.pcb)
    if not os.path.exists(pcb_path):
        print(f"ERROR: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1

    if args.outline:
        outline = find_board_outline(pcb_path)
        print(f"Board outline (PCB mm):")
        print(f"  x: {outline[0]:.2f} to {outline[2]:.2f}"
              f"  ({outline[2]-outline[0]:.2f} mm)")
        print(f"  y: {outline[1]:.2f} to {outline[3]:.2f}"
              f"  ({outline[3]-outline[1]:.2f} mm)")
        return 0

    out_png = (os.path.abspath(args.output) if args.output
               else os.path.join(os.path.dirname(pcb_path), "snapshot.png"))

    bbox_pcb = None
    if args.bbox:
        parts = [float(x.strip()) for x in args.bbox.split(",")]
        if len(parts) != 4:
            print("ERROR: --bbox requires 4 values: x1,y1,x2,y2",
                  file=sys.stderr)
            return 1
        bbox_pcb = tuple(parts)

    w, h = snapshot_region(pcb_path, bbox_pcb, out_png, layers=args.layers)
    print(f"Saved: {out_png} ({w}x{h} px)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
