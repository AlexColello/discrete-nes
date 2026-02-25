#!/usr/bin/env python3
"""
Verification script for RAM prototype schematics.

Checks for common issues that have caused ERC failures or visual problems:
1. Diagonal wires (KiCad doesn't reliably connect them)
2. Wire overlaps (same-direction wires sharing ranges silently merge nets)
3. Dangling wire endpoints (not connected to any pin, wire, label, junction)
4. Wires passing through component pins (unintended connections)
5. T-junctions without explicit junction dots (visual issue)
6. Component overlap (non-power parts placed on top of each other)
7. Netlist connectivity (hierarchy pin connections match expected topology)
8. ERC via kicad-cli on the root schematic

Results are written to verify_output/ directory (gitignored).
SVGs are exported to verify_output/svg/ for visual inspection.

Usage:
    python scripts/verify_schematics.py          # Run all checks + SVG export
    python scripts/verify_schematics.py --no-erc  # Skip kicad-cli ERC and SVG export
"""

import json
import math
import os
import subprocess
import sys
from collections import defaultdict

from kiutils.schematic import Schematic

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------

GRID = 2.54
TOLERANCE = 0.0001  # mm tolerance for coordinate comparison

BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_DIR = os.path.join(BOARD_DIR, "verify_output")
KICAD_CLI = r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe"

# All schematic files to check
SCHEMATIC_FILES = [
    "ram.kicad_sch",
    "address_decoder.kicad_sch",
    "control_logic.kicad_sch",
    "write_clk_gen.kicad_sch",
    "read_oe_gen.kicad_sch",
    "byte.kicad_sch",
]


def snap(v):
    """Round to 2 decimal places to match generate_ram.py precision."""
    return round(v, 2)


def pts_close(a, b):
    """Check if two points are within tolerance."""
    return abs(a[0] - b[0]) < TOLERANCE and abs(a[1] - b[1]) < TOLERANCE


# --------------------------------------------------------------
# Schematic parsing
# --------------------------------------------------------------

def _extract_lib_pins(lib_sym):
    """Extract pin positions from a library symbol definition.

    Returns [(pin_number, lib_x, lib_y, pin_angle, pin_length), ...]
    where coordinates are in library space (Y-up).
    pin_angle is the direction from body to tip (0=right, 90=up, 180=left, 270=down).
    """
    pins = []
    # kiutils stores sub-symbols in lib_sym.symbols
    # Sub-symbols named like "74LVC1G04_1_1" contain the pins for unit 1
    for sub_sym in getattr(lib_sym, 'symbols', []):
        for pin in getattr(sub_sym, 'pins', []):
            pa = getattr(pin.position, 'angle', 0) or 0
            pl = getattr(pin, 'length', 2.54) or 2.54
            pins.append((pin.number, pin.position.X, pin.position.Y, pa, pl))
    # Also check the units property (kiutils convenience)
    if not pins:
        for unit in getattr(lib_sym, 'units', []):
            for pin in getattr(unit, 'pins', []):
                pa = getattr(pin.position, 'angle', 0) or 0
                pl = getattr(pin, 'length', 2.54) or 2.54
                pins.append((pin.number, pin.position.X, pin.position.Y, pa, pl))
    return pins


def _pin_schematic_offset(lib_x, lib_y, angle_deg):
    """Convert library pin position to schematic offset (dx, dy).

    Library uses Y-up; schematic uses Y-down.
    KiCad rotation is CW in schematic space.
    """
    # Negate Y for schematic coordinate system
    bx, by = lib_x, -lib_y
    rad = math.radians(angle_deg)
    cos_a = round(math.cos(rad), 10)
    sin_a = round(math.sin(rad), 10)
    dx = snap(cos_a * bx + sin_a * by)
    dy = snap(-sin_a * bx + cos_a * by)
    return dx, dy


def parse_schematic(filepath):
    """Parse a schematic and extract all geometric data for verification.

    Returns dict with:
      wires: [((x1,y1), (x2,y2)), ...]
      pins: {(x,y): (ref, pin_num, pin_type), ...}
      pin_positions: set of (x,y)
      junctions: set of (x,y)
      labels: set of (x,y)
      no_connects: set of (x,y)
      components: [(ref, lib_name, cx, cy, angle), ...]
    """
    sch = Schematic.from_file(filepath)

    # -- Wires --
    wires = []
    for item in sch.graphicalItems:
        item_type = getattr(item, 'type', None)
        if item_type == 'wire':
            pts = getattr(item, 'points', [])
            if len(pts) >= 2:
                p1 = (snap(pts[0].X), snap(pts[0].Y))
                p2 = (snap(pts[1].X), snap(pts[1].Y))
                wires.append((p1, p2))

    # -- Library symbol pin map --
    lib_pin_map = {}  # lib_id -> [(pin_num, lib_x, lib_y, pin_angle, pin_length)]
    for lib_sym in sch.libSymbols:
        lib_pin_map[lib_sym.libId] = _extract_lib_pins(lib_sym)

    # -- Component instances and pin positions --
    pins = {}          # (x,y) -> (ref, pin_num, pin_type_or_name)
    pin_positions = set()
    pin_stubs = {}     # (x,y) -> (stub_dx, stub_dy, stub_len) direction from tip toward body
    components = []

    for comp in sch.schematicSymbols:
        lib_id = comp.libId
        cx = snap(comp.position.X)
        cy = snap(comp.position.Y)
        angle = comp.position.angle or 0

        # Get reference designator
        ref = "?"
        for prop in comp.properties:
            if prop.key == "Reference":
                ref = prop.value
                break

        # Determine base symbol name from lib_id
        lib_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id
        components.append((ref, lib_name, cx, cy, angle))

        if lib_id in lib_pin_map:
            for pin_num, lx, ly, pa, pl in lib_pin_map[lib_id]:
                dx, dy = _pin_schematic_offset(lx, ly, angle)
                abs_x = snap(cx + dx)
                abs_y = snap(cy + dy)
                pins[(abs_x, abs_y)] = (ref, pin_num, lib_name)
                pin_positions.add((abs_x, abs_y))

                # Compute stub direction (from tip TOWARD body) in schematic space.
                # Library pin angle = direction body→tip.
                # After component rotation and Y-negation, the stub direction
                # (tip→body) is the reverse of the rotated pin direction.
                # Library Y-up: pin angle 0=right, 90=up, 180=left, 270=down
                # Schematic Y-down: negate Y component.
                tip_angle_lib = pa  # body→tip in library coords
                # In library coords, body→tip direction vector:
                tip_rad = math.radians(tip_angle_lib)
                btx = round(math.cos(tip_rad), 6)
                bty = round(math.sin(tip_rad), 6)
                # Convert to schematic coords (negate Y), then apply component rotation
                bty_sch = -bty
                rot_rad = math.radians(angle)
                cos_r = round(math.cos(rot_rad), 6)
                sin_r = round(math.sin(rot_rad), 6)
                # Rotate body→tip vector by component angle (CW in schematic)
                sdx = cos_r * btx + sin_r * bty_sch
                sdy = -sin_r * btx + cos_r * bty_sch
                # KiCad pin_angle is the direction from tip toward body,
                # so (sdx, sdy) after rotation IS the stub direction.
                pin_stubs[(abs_x, abs_y)] = (round(sdx, 6), round(sdy, 6), pl)

    # -- Junctions --
    junctions = set()
    for j in sch.junctions:
        junctions.add((snap(j.position.X), snap(j.position.Y)))

    # -- Labels (all types) --
    labels = set()
    for lbl in getattr(sch, 'labels', []):
        labels.add((snap(lbl.position.X), snap(lbl.position.Y)))
    for lbl in getattr(sch, 'globalLabels', []):
        labels.add((snap(lbl.position.X), snap(lbl.position.Y)))
    for lbl in getattr(sch, 'hierarchicalLabels', []):
        labels.add((snap(lbl.position.X), snap(lbl.position.Y)))

    # -- No-connect markers --
    no_connects = set()
    for nc in getattr(sch, 'noConnects', []):
        no_connects.add((snap(nc.position.X), snap(nc.position.Y)))

    # -- Hierarchical sheet pins --
    sheet_pins = set()
    for sheet in getattr(sch, 'hierarchicalSheets', []):
        for pin in getattr(sheet, 'pins', []):
            sheet_pins.add((snap(pin.position.X), snap(pin.position.Y)))

    return {
        'wires': wires,
        'pins': pins,
        'pin_positions': pin_positions,
        'pin_stubs': pin_stubs,
        'junctions': junctions,
        'labels': labels,
        'no_connects': no_connects,
        'sheet_pins': sheet_pins,
        'components': components,
    }


# --------------------------------------------------------------
# Check functions
# --------------------------------------------------------------

def check_diagonal_wires(data):
    """Check for wires that are neither horizontal nor vertical.

    KiCad doesn't reliably connect diagonal wires. All routing should
    be orthogonal (L-shaped at worst).
    """
    issues = []
    for i, ((x1, y1), (x2, y2)) in enumerate(data['wires']):
        if abs(x1 - x2) > TOLERANCE and abs(y1 - y2) > TOLERANCE:
            issues.append(
                f"  Wire #{i}: ({x1}, {y1}) -> ({x2}, {y2}) is diagonal"
            )
    return issues


def check_wire_overlaps(data):
    """Check for same-direction wire overlaps.

    Two wires sharing the same axis AND overlapping in their range
    silently merge nets. This is the #1 cause of ERC failures in
    generated schematics.
    """
    wires = data['wires']
    issues = []

    # Separate horizontal and vertical wires
    h_wires = []  # (y, x_min, x_max, idx)
    v_wires = []  # (x, y_min, y_max, idx)

    for idx, ((x1, y1), (x2, y2)) in enumerate(wires):
        if abs(y1 - y2) < TOLERANCE:  # horizontal
            h_wires.append((snap(y1), min(x1, x2), max(x1, x2), idx))
        elif abs(x1 - x2) < TOLERANCE:  # vertical
            v_wires.append((snap(x1), min(y1, y2), max(y1, y2), idx))

    # Check horizontal overlaps (group by Y)
    by_y = defaultdict(list)
    for y, xmin, xmax, idx in h_wires:
        by_y[y].append((xmin, xmax, idx))

    for y, segs in by_y.items():
        segs.sort()
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                a_min, a_max, a_idx = segs[i]
                b_min, b_max, b_idx = segs[j]
                overlap_start = max(a_min, b_min)
                overlap_end = min(a_max, b_max)
                if overlap_end - overlap_start > TOLERANCE:
                    issues.append(
                        f"  H overlap Y={y}: wire#{a_idx} X=[{a_min},{a_max}] "
                        f"& wire#{b_idx} X=[{b_min},{b_max}] "
                        f"share [{overlap_start},{overlap_end}]"
                    )

    # Check vertical overlaps (group by X)
    by_x = defaultdict(list)
    for x, ymin, ymax, idx in v_wires:
        by_x[x].append((ymin, ymax, idx))

    for x, segs in by_x.items():
        segs.sort()
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                a_min, a_max, a_idx = segs[i]
                b_min, b_max, b_idx = segs[j]
                overlap_start = max(a_min, b_min)
                overlap_end = min(a_max, b_max)
                if overlap_end - overlap_start > TOLERANCE:
                    issues.append(
                        f"  V overlap X={x}: wire#{a_idx} Y=[{a_min},{a_max}] "
                        f"& wire#{b_idx} Y=[{b_min},{b_max}] "
                        f"share [{overlap_start},{overlap_end}]"
                    )

    return issues


def check_dangling_endpoints(data):
    """Check for wire endpoints not connected to anything.

    A wire endpoint is "connected" if it touches:
    - Another wire endpoint (shared point)
    - A component pin
    - A junction dot
    - A label (local, global, or hierarchical)
    - A no-connect marker
    - A hierarchical sheet pin
    - The interior of another wire (T-junction)
    """
    wires = data['wires']
    pin_positions = data['pin_positions']
    junctions = data['junctions']
    labels = data['labels']
    no_connects = data['no_connects']
    sheet_pins = data['sheet_pins']

    # Collect all wire endpoints
    all_endpoints = []
    for (x1, y1), (x2, y2) in wires:
        all_endpoints.append((x1, y1))
        all_endpoints.append((x2, y2))

    # Count occurrences of each endpoint
    endpoint_counts = defaultdict(int)
    for pt in all_endpoints:
        endpoint_counts[pt] += 1

    # Build set of all "connected" points
    connected = set()
    connected.update(pin_positions)
    connected.update(junctions)
    connected.update(labels)
    connected.update(no_connects)
    connected.update(sheet_pins)

    # Points where 2+ wire endpoints meet
    for pt, count in endpoint_counts.items():
        if count >= 2:
            connected.add(pt)

    # T-junctions: endpoint landing on wire body
    unique_endpoints = set(all_endpoints)
    for (x1, y1), (x2, y2) in wires:
        if abs(y1 - y2) < TOLERANCE:  # horizontal wire
            xmin, xmax = min(x1, x2), max(x1, x2)
            y = snap(y1)
            for pt in unique_endpoints:
                if (abs(pt[1] - y) < TOLERANCE and
                        xmin - TOLERANCE <= pt[0] <= xmax + TOLERANCE):
                    connected.add(pt)
        elif abs(x1 - x2) < TOLERANCE:  # vertical wire
            ymin, ymax = min(y1, y2), max(y1, y2)
            x = snap(x1)
            for pt in unique_endpoints:
                if (abs(pt[0] - x) < TOLERANCE and
                        ymin - TOLERANCE <= pt[1] <= ymax + TOLERANCE):
                    connected.add(pt)

    # Find dangling endpoints
    issues = []
    for pt in sorted(unique_endpoints):
        if pt not in connected:
            # Double-check with tolerance against all connected points
            found = any(pts_close(pt, cp) for cp in connected)
            if not found:
                issues.append(f"  Dangling at ({pt[0]}, {pt[1]})")

    return issues


def check_wire_through_pins(data):
    """Check for wires passing through component pins.

    When a wire's INTERIOR passes through a pin position (not at its
    endpoints), it creates an unintended connection. This is different
    from T-junctions (which involve wire endpoints).

    Excludes power pins (VCC/GND) which are intentionally overlapped
    by connect_power().
    """
    wires = data['wires']
    pins = data['pins']  # (x,y) -> (ref, pin_num, lib_name)

    issues = []

    # Collect all wire endpoints so we can exclude intentional connections
    wire_endpoints = set()
    for (x1, y1), (x2, y2) in wires:
        wire_endpoints.add((x1, y1))
        wire_endpoints.add((x2, y2))

    for (px, py), (ref, pin_num, lib_name) in pins.items():
        # Skip power symbols — they're placed directly at pin positions
        if ref.startswith("#"):
            continue

        # Skip if a wire endpoint is at this pin (intentional connection)
        if (px, py) in wire_endpoints:
            continue
        if any(pts_close((px, py), ep) for ep in wire_endpoints):
            continue

        # Check if any wire INTERIOR passes through this pin
        for w_idx, ((x1, y1), (x2, y2)) in enumerate(wires):
            if abs(y1 - y2) < TOLERANCE:  # horizontal wire
                xmin, xmax = min(x1, x2), max(x1, x2)
                if (abs(py - y1) < TOLERANCE and
                        xmin + TOLERANCE < px < xmax - TOLERANCE):
                    issues.append(
                        f"  Wire #{w_idx} H({x1},{y1})->({x2},{y2}) "
                        f"passes through {ref} pin {pin_num} at ({px},{py})"
                    )
            elif abs(x1 - x2) < TOLERANCE:  # vertical wire
                ymin, ymax = min(y1, y2), max(y1, y2)
                if (abs(px - x1) < TOLERANCE and
                        ymin + TOLERANCE < py < ymax - TOLERANCE):
                    issues.append(
                        f"  Wire #{w_idx} V({x1},{y1})->({x2},{y2}) "
                        f"passes through {ref} pin {pin_num} at ({px},{py})"
                    )

    return issues


def check_tjunctions_without_dots(data):
    """Find T-junctions missing explicit junction dots.

    A wire endpoint landing on another wire's interior creates a valid
    KiCad connection, but without a junction dot it looks like a
    "dangling wire" in the GUI. This check finds these cases.
    """
    wires = data['wires']
    junctions = data['junctions']

    # Collect all wire endpoints
    endpoint_set = set()
    for (x1, y1), (x2, y2) in wires:
        endpoint_set.add((x1, y1))
        endpoint_set.add((x2, y2))

    issues = []
    seen = set()  # avoid duplicate reports

    for pt in endpoint_set:
        for (x1, y1), (x2, y2) in wires:
            if abs(y1 - y2) < TOLERANCE:  # horizontal wire
                xmin, xmax = min(x1, x2), max(x1, x2)
                y = snap(y1)
                if (abs(pt[1] - y) < TOLERANCE and
                        xmin + TOLERANCE < pt[0] < xmax - TOLERANCE):
                    # Endpoint is on the interior of this wire
                    key = (pt, "H", y, xmin, xmax)
                    if key not in seen and pt not in junctions:
                        issues.append(
                            f"  T-junction at ({pt[0]}, {pt[1]}) on "
                            f"H wire ({x1},{y1})->({x2},{y2}) — no junction dot"
                        )
                        seen.add(key)
            elif abs(x1 - x2) < TOLERANCE:  # vertical wire
                ymin, ymax = min(y1, y2), max(y1, y2)
                x = snap(x1)
                if (abs(pt[0] - x) < TOLERANCE and
                        ymin + TOLERANCE < pt[1] < ymax - TOLERANCE):
                    key = (pt, "V", x, ymin, ymax)
                    if key not in seen and pt not in junctions:
                        issues.append(
                            f"  T-junction at ({pt[0]}, {pt[1]}) on "
                            f"V wire ({x1},{y1})->({x2},{y2}) — no junction dot"
                        )
                        seen.add(key)

    return issues


def check_component_overlap(data):
    """Check for non-power components placed too close together.

    Two components whose centers are within a minimum clearance distance
    are almost certainly overlapping visually.  This catches layout bugs
    where LEDs, resistors, or other parts are placed on top of each other.
    """
    comps = data['components']
    issues = []

    # Skip power symbols (#PWR...) and connectors (Conn pins are close by design)
    non_power = [
        (ref, lib, cx, cy, angle)
        for ref, lib, cx, cy, angle in comps
        if not ref.startswith("#") and not lib.startswith("Conn")
    ]

    # Minimum center-to-center distance (mm).  Two small components
    # (R_Small, LED_Small) with centers closer than this are overlapping.
    MIN_DIST = 1.5

    for i in range(len(non_power)):
        ref_a, lib_a, ax, ay, _aa = non_power[i]
        for j in range(i + 1, len(non_power)):
            ref_b, lib_b, bx, by, _ab = non_power[j]
            dist = math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)
            if dist < MIN_DIST:
                issues.append(
                    f"  {ref_a} ({lib_a}) and {ref_b} ({lib_b}) overlap: "
                    f"centers ({ax},{ay}) and ({bx},{by}) dist={dist:.2f}mm"
                )

    return issues


def check_wire_overlaps_pin_stub(data):
    """Check for wires that overlap with a pin's built-in stub line.

    A pin stub is the short line from the pin connection point (tip) toward
    the component body.  When a wire has an endpoint at the tip and extends
    in the stub direction, it visually doubles the stub line.

    Uses the computed stub direction vector from parse_schematic().
    """
    wires = data['wires']
    pins = data['pins']
    pin_stubs = data.get('pin_stubs', {})

    if not pin_stubs:
        return []

    issues = []
    seen = set()

    for (px, py), (ref, pin_num, lib_name) in pins.items():
        if ref.startswith("#"):
            continue  # skip power symbols
        if lib_name.startswith("Conn"):
            continue  # connector pins always have wires in stub direction
        if (px, py) not in pin_stubs:
            continue

        sdx, sdy, slen = pin_stubs[(px, py)]
        if abs(sdx) < 0.01 and abs(sdy) < 0.01:
            continue

        for w_idx, ((x1, y1), (x2, y2)) in enumerate(wires):
            # Check if wire has one endpoint at the pin tip
            if pts_close((x1, y1), (px, py)):
                other = (x2, y2)
            elif pts_close((x2, y2), (px, py)):
                other = (x1, y1)
            else:
                continue

            # Wire direction from pin tip toward other endpoint
            wx = other[0] - px
            wy = other[1] - py
            wire_len = math.sqrt(wx * wx + wy * wy)
            if wire_len < TOLERANCE:
                continue

            # Dot product: positive means wire goes in stub direction
            dot = wx * sdx + wy * sdy
            # Normalize by wire length to get cosine of angle
            cos_angle = dot / wire_len
            # Flag if wire is closely aligned with stub direction (within ~15 degrees)
            if cos_angle > 0.96:
                key = (px, py, ref, pin_num)
                if key not in seen:
                    issues.append(
                        f"  Wire #{w_idx} overlaps stub of {ref} pin {pin_num} "
                        f"at ({px},{py})"
                    )
                    seen.add(key)

    return issues


# --------------------------------------------------------------
# Netlist verification
# --------------------------------------------------------------

class _UnionFind:
    """Simple union-find for net connectivity."""

    def __init__(self):
        self._parent = {}

    def find(self, x):
        if x not in self._parent:
            self._parent[x] = x
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def check_netlist():
    """Verify root sheet netlist connectivity.

    Builds nets from wire connectivity in ram.kicad_sch using union-find,
    then checks that expected pairs of hierarchy sheet pins are on the same
    net (e.g., address decoder SEL0 → write_clk_gen SEL0) and that signals
    that should be separate are NOT merged.

    Returns list of issue strings (empty if all checks pass).
    """
    filepath = os.path.join(BOARD_DIR, "ram.kicad_sch")
    if not os.path.exists(filepath):
        return ["  ram.kicad_sch not found"]

    sch = Schematic.from_file(filepath)
    uf = _UnionFind()

    # -- Collect wires --
    wires = []
    for item in sch.graphicalItems:
        if getattr(item, 'type', None) == 'wire':
            pts = item.points
            if len(pts) >= 2:
                p1 = (snap(pts[0].X), snap(pts[0].Y))
                p2 = (snap(pts[1].X), snap(pts[1].Y))
                wires.append((p1, p2))
                uf.union(p1, p2)

    # -- Collect all electrically-active points --
    all_pts = set()
    for p1, p2 in wires:
        all_pts.add(p1)
        all_pts.add(p2)

    # Sheet pins: map (x,y) -> "SheetName:PinName"
    sheet_pin_ids = {}
    for sheet in sch.sheets:
        sname = sheet.sheetName.value
        for pin in sheet.pins:
            pt = (snap(pin.position.X), snap(pin.position.Y))
            sheet_pin_ids[pt] = f"{sname}:{pin.name}"
            all_pts.add(pt)

    # Labels: map (x,y) -> label text
    label_pts = {}
    for lbl in getattr(sch, 'labels', []):
        pt = (snap(lbl.position.X), snap(lbl.position.Y))
        label_pts[pt] = lbl.text
        all_pts.add(pt)

    # Component pins (connector, LEDs, resistors)
    lib_pin_map = {}
    for lib_sym in sch.libSymbols:
        lib_pin_map[lib_sym.libId] = _extract_lib_pins(lib_sym)

    comp_pin_ids = {}  # (x,y) -> "Ref:pin_num"
    for comp in sch.schematicSymbols:
        lib_id = comp.libId
        cx, cy = snap(comp.position.X), snap(comp.position.Y)
        angle = comp.position.angle or 0
        ref = "?"
        for prop in comp.properties:
            if prop.key == "Reference":
                ref = prop.value
                break
        if lib_id in lib_pin_map:
            for pin_num, lx, ly, pa, pl in lib_pin_map[lib_id]:
                dx, dy = _pin_schematic_offset(lx, ly, angle)
                pt = (snap(cx + dx), snap(cy + dy))
                comp_pin_ids[pt] = f"{ref}:{pin_num}"
                all_pts.add(pt)

    # Junctions
    for j in sch.junctions:
        all_pts.add((snap(j.position.X), snap(j.position.Y)))

    # -- Merge points that touch wires (T-junctions + endpoints) --
    for pt in all_pts:
        for (x1, y1), (x2, y2) in wires:
            if abs(y1 - y2) < TOLERANCE:  # horizontal
                xmin, xmax = min(x1, x2), max(x1, x2)
                if (abs(pt[1] - y1) < TOLERANCE and
                        xmin - TOLERANCE <= pt[0] <= xmax + TOLERANCE):
                    uf.union(pt, (x1, y1))
            elif abs(x1 - x2) < TOLERANCE:  # vertical
                ymin, ymax = min(y1, y2), max(y1, y2)
                if (abs(pt[0] - x1) < TOLERANCE and
                        ymin - TOLERANCE <= pt[1] <= ymax + TOLERANCE):
                    uf.union(pt, (x1, y1))

    # -- Merge same-name labels (implicit net connections) --
    label_groups = defaultdict(list)
    for pt, name in label_pts.items():
        label_groups[name].append(pt)
    for name, pts in label_groups.items():
        for i in range(1, len(pts)):
            uf.union(pts[0], pts[i])

    # -- Build net membership: root -> set of identifiers --
    nets = defaultdict(set)
    for pt, sid in sheet_pin_ids.items():
        nets[uf.find(pt)].add(sid)
    for pt, name in label_pts.items():
        nets[uf.find(pt)].add(f"label:{name}")

    def on_same_net(id_a, id_b):
        """Check if two identifiers are on the same net."""
        for root, members in nets.items():
            if id_a in members and id_b in members:
                return True
        return False

    def id_exists(identifier):
        """Check if an identifier appears in any net."""
        return any(identifier in m for m in nets.values())

    # -- Define expected connections --
    issues = []

    # 1. SEL0-7: addr decoder → write_clk_gen AND read_oe_gen
    for i in range(8):
        ad = f"Address Decoder:SEL{i}"
        wc = f"Write Clk Gen:SEL{i}"
        ro = f"Read OE Gen:SEL{i}"
        if not on_same_net(ad, wc):
            issues.append(f"  {ad} not connected to {wc}")
        if not on_same_net(ad, ro):
            issues.append(f"  {ad} not connected to {ro}")

    # 2. WRITE_ACTIVE: control logic → write_clk_gen
    if not on_same_net("Control Logic:WRITE_ACTIVE",
                       "Write Clk Gen:WRITE_ACTIVE"):
        issues.append(
            "  Control Logic:WRITE_ACTIVE not connected to "
            "Write Clk Gen:WRITE_ACTIVE")

    # 3. READ_EN: control logic → read_oe_gen
    if not on_same_net("Control Logic:READ_EN", "Read OE Gen:READ_EN"):
        issues.append(
            "  Control Logic:READ_EN not connected to Read OE Gen:READ_EN")

    # 4. WRITE_CLK_i: write_clk_gen → byte_i
    for i in range(8):
        wc = f"Write Clk Gen:WRITE_CLK_{i}"
        by = f"Byte {i}:WRITE_CLK"
        if not on_same_net(wc, by):
            issues.append(f"  {wc} not connected to {by}")

    # 5. BUF_OE_i: read_oe_gen → byte_i
    for i in range(8):
        ro = f"Read OE Gen:BUF_OE_{i}"
        by = f"Byte {i}:BUF_OE"
        if not on_same_net(ro, by):
            issues.append(f"  {ro} not connected to {by}")

    # 6. D0-D7: all byte sheet D_i pins connected via labels
    for bit in range(8):
        lbl = f"label:D{bit}"
        for byte_idx in range(8):
            pin_id = f"Byte {byte_idx}:D{bit}"
            if not on_same_net(lbl, pin_id):
                issues.append(f"  {pin_id} not on label D{bit} net")

    # 7. A0-A2: connector → address decoder (via wires)
    for i in range(3):
        ad = f"Address Decoder:A{i}"
        if not id_exists(ad):
            issues.append(f"  {ad} not found in any net")

    # 8. nCE/nOE/nWE: connector → control logic (via wires)
    for sig in ["nCE", "nOE", "nWE"]:
        cl = f"Control Logic:{sig}"
        if not id_exists(cl):
            issues.append(f"  {cl} not found in any net")

    # -- Check signal isolation (different signals not merged) --
    isolation_pairs = [
        # Address signals must be separate
        ("Address Decoder:A0", "Address Decoder:A1"),
        ("Address Decoder:A0", "Address Decoder:A2"),
        ("Address Decoder:A1", "Address Decoder:A2"),
        # Control signals must be separate
        ("Control Logic:nCE", "Control Logic:nOE"),
        ("Control Logic:nCE", "Control Logic:nWE"),
        ("Control Logic:nOE", "Control Logic:nWE"),
        # Select lines must be separate
        ("Address Decoder:SEL0", "Address Decoder:SEL1"),
        ("Address Decoder:SEL0", "Address Decoder:SEL7"),
        # Write clocks must be separate
        ("Write Clk Gen:WRITE_CLK_0", "Write Clk Gen:WRITE_CLK_1"),
        ("Write Clk Gen:WRITE_CLK_0", "Write Clk Gen:WRITE_CLK_7"),
        # Buffer OE must be separate
        ("Read OE Gen:BUF_OE_0", "Read OE Gen:BUF_OE_1"),
        ("Read OE Gen:BUF_OE_0", "Read OE Gen:BUF_OE_7"),
        # Data bus and control must be separate
        ("label:D0", "Address Decoder:A0"),
        ("label:D0", "Control Logic:nCE"),
        # WRITE_ACTIVE vs READ_EN
        ("Control Logic:WRITE_ACTIVE", "Control Logic:READ_EN"),
    ]
    for id_a, id_b in isolation_pairs:
        if on_same_net(id_a, id_b):
            issues.append(f"  NET MERGE: {id_a} and {id_b} on same net!")

    return issues


# --------------------------------------------------------------
# ERC via kicad-cli
# --------------------------------------------------------------

def _is_standalone_artifact(violation):
    """Check if an ERC violation is an expected standalone sub-sheet artifact.

    When running ERC on a sub-sheet in isolation (not via the root sheet),
    certain errors are expected because hierarchical connections don't exist:
    - Hierarchical labels "cannot be connected to non-existent parent sheet"
    - Input pins "not driven" when their drivers are hierarchical labels
    - Power pins not driven (no PWR_FLAG in isolated context)
    """
    desc = violation.get("description", "")
    vtype = violation.get("type", "")

    # Hierarchical labels can't connect when sheet is standalone
    if "cannot be connected to non-existent parent sheet" in desc:
        return True

    # Hierarchical labels show as "dangling" standalone because KiCad
    # can't resolve their parent-side connections without the hierarchy.
    # The "Hierarchical Label" text is in items[].description, not the
    # violation description.
    if vtype == "label_dangling":
        items = violation.get("items", [])
        if any("Hierarchical Label" in it.get("description", "")
               for it in items):
            return True

    # Wire endpoints touching hierarchical labels also show as "dangling"
    # in standalone mode because the hier label's parent connection is missing.
    if vtype == "wire_dangling":
        return True

    # Input pins fed by hierarchical labels show as "not driven" standalone
    if vtype == "pin_not_driven" and "Input pin not driven" in desc:
        return True

    # Power pins not driven in isolation
    if vtype == "power_pin_not_driven":
        return True

    return False


def run_erc(sch_path, output_dir, label=None, standalone=False):
    """Run kicad-cli ERC on a schematic.

    Args:
        sch_path: Path to the .kicad_sch file
        output_dir: Directory for output files
        label: Label for output filenames (default: derived from sch_path)
        standalone: If True, filter out expected standalone sub-sheet artifacts

    Returns (issues_list, error_count, warning_count).
    """
    if label is None:
        label = os.path.splitext(os.path.basename(sch_path))[0]

    erc_json = os.path.join(output_dir, f"erc_{label}.json")

    if not os.path.exists(KICAD_CLI):
        return [f"  kicad-cli not found at {KICAD_CLI}"], 0, 0

    # JSON report
    subprocess.run(
        [KICAD_CLI, "sch", "erc", "--format", "json",
         "--severity-all", "--output", erc_json, sch_path],
        capture_output=True, text=True,
    )

    issues = []
    real_errors = 0
    warnings = 0
    filtered_count = 0

    if os.path.exists(erc_json):
        with open(erc_json) as f:
            data = json.load(f)

        for sheet in data.get("sheets", []):
            path = sheet.get("path", "/")
            for v in sheet.get("violations", []):
                severity = v["severity"]
                vtype = v["type"]
                desc = v["description"]

                # lib_symbol_mismatch is a known harmless kiutils artifact
                if vtype == "lib_symbol_mismatch":
                    warnings += 1
                    continue

                # Filter standalone artifacts for sub-sheet ERC
                if standalone and _is_standalone_artifact(v):
                    filtered_count += 1
                    continue

                if severity == "error":
                    real_errors += 1
                    items_desc = "; ".join(
                        it.get("description", "")
                        for it in v.get("items", [])
                    )
                    issues.append(
                        f"  ERROR [{path}] {vtype}: {desc} ({items_desc})"
                    )
                elif severity == "warning":
                    warnings += 1
                    if vtype != "lib_symbol_mismatch":
                        issues.append(
                            f"  WARN [{path}] {vtype}: {desc}"
                        )
    else:
        issues.append("  ERC JSON output not generated")

    if standalone and filtered_count > 0:
        issues.append(
            f"  (filtered {filtered_count} standalone artifact(s))"
        )

    return issues, real_errors, warnings


# --------------------------------------------------------------
# Main
# --------------------------------------------------------------

def main():
    skip_erc = "--no-erc" in sys.argv

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    total_errors = 0
    total_warnings = 0
    all_results = {}

    print("=" * 60)
    print("RAM Prototype Schematic Verification")
    print("=" * 60)

    # -- Per-file checks --
    for sch_file in SCHEMATIC_FILES:
        filepath = os.path.join(BOARD_DIR, sch_file)
        if not os.path.exists(filepath):
            print(f"\n  SKIP {sch_file} (not found)")
            continue

        print(f"\n--- {sch_file} ---")
        data = parse_schematic(filepath)
        file_results = []

        # 1. Diagonal wires
        diag = check_diagonal_wires(data)
        if diag:
            file_results.append(("Diagonal Wires", diag, True))

        # 2. Wire overlaps
        overlaps = check_wire_overlaps(data)
        if overlaps:
            file_results.append(("Wire Overlaps (NET MERGE)", overlaps, True))

        # 3. Dangling endpoints
        dangles = check_dangling_endpoints(data)
        if dangles:
            file_results.append(("Dangling Endpoints", dangles, True))

        # 4. Wires through pins
        through = check_wire_through_pins(data)
        if through:
            file_results.append(("Wire Through Pin", through, True))

        # 5. T-junctions without dots (warning only)
        tjuncs = check_tjunctions_without_dots(data)
        if tjuncs:
            file_results.append(("T-junction (no dot)", tjuncs, False))

        # 6. Wire overlaps pin stub (error — wires doubling pin stubs)
        stubs = check_wire_overlaps_pin_stub(data)
        if stubs:
            file_results.append(("Wire Overlaps Pin Stub", stubs, True))

        # 7. Component overlap (error — parts on top of each other)
        overlaps_comp = check_component_overlap(data)
        if overlaps_comp:
            file_results.append(("Component Overlap", overlaps_comp, True))

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

    # -- Netlist connectivity check --
    print(f"\n--- Netlist: ram.kicad_sch ---")
    netlist_issues = check_netlist()
    if netlist_issues:
        print(f"  [ERROR] Netlist Connectivity: {len(netlist_issues)}")
        for issue in netlist_issues:
            print(issue)
        total_errors += len(netlist_issues)
    else:
        print("  All expected connections verified")

    # -- ERC --
    if not skip_erc:
        # Root sheet ERC (full hierarchy)
        root_sch = os.path.join(BOARD_DIR, "ram.kicad_sch")
        if os.path.exists(root_sch):
            print(f"\n--- ERC: ram.kicad_sch (root, full hierarchy) ---")
            erc_issues, erc_errors, erc_warnings = run_erc(
                root_sch, OUTPUT_DIR, label="root")

            if erc_issues:
                for issue in erc_issues:
                    print(issue)
            total_errors += erc_errors
            total_warnings += erc_warnings
            print(
                f"  ERC: {erc_errors} error(s), {erc_warnings} warning(s) "
                f"(lib_symbol_mismatch excluded from errors)"
            )

        # Per-sub-sheet standalone ERC
        for sch_file in SCHEMATIC_FILES:
            if sch_file == "ram.kicad_sch":
                continue  # already checked as root
            filepath = os.path.join(BOARD_DIR, sch_file)
            if not os.path.exists(filepath):
                continue
            label = os.path.splitext(sch_file)[0]
            print(f"\n--- ERC: {sch_file} (standalone) ---")
            erc_issues, erc_errors, erc_warnings = run_erc(
                filepath, OUTPUT_DIR, label=label, standalone=True)

            if erc_issues:
                for issue in erc_issues:
                    print(issue)
            total_errors += erc_errors
            total_warnings += erc_warnings
            print(
                f"  ERC: {erc_errors} error(s), {erc_warnings} warning(s)"
            )
    else:
        print(f"\n--- ERC skipped (--no-erc) ---")

    # -- Export SVGs for visual inspection --
    if not skip_erc and os.path.exists(KICAD_CLI):
        print(f"\n--- SVG export ---")
        svg_dir = os.path.join(OUTPUT_DIR, "svg")
        os.makedirs(svg_dir, exist_ok=True)
        root_sch = os.path.join(BOARD_DIR, "ram.kicad_sch")
        if os.path.exists(root_sch):
            result = subprocess.run(
                [KICAD_CLI, "sch", "export", "svg",
                 "--output", svg_dir, root_sch],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                svg_count = len([f for f in os.listdir(svg_dir)
                                 if f.endswith('.svg')])
                print(f"  Exported {svg_count} SVG(s) to {svg_dir}")
            else:
                print(f"  SVG export failed: {result.stderr.strip()}")

    # -- Write report --
    report_path = os.path.join(OUTPUT_DIR, "verify_report.txt")
    with open(report_path, 'w') as f:
        f.write("RAM Prototype Schematic Verification Report\n")
        f.write("=" * 50 + "\n\n")
        for sch_file, file_results in all_results.items():
            f.write(f"{sch_file}:\n")
            if file_results:
                for category, issues, is_error in file_results:
                    level = "ERROR" if is_error else "WARN"
                    f.write(f"  [{level}] {category} ({len(issues)}):\n")
                    for issue in issues:
                        f.write(f"  {issue}\n")
            else:
                f.write("  All checks passed\n")
            f.write("\n")

    # -- Summary --
    print(f"\n{'=' * 60}")
    if total_errors > 0:
        print(f"FAILED: {total_errors} error(s), {total_warnings} warning(s)")
    else:
        print(f"PASSED: 0 errors, {total_warnings} warning(s)")
    print(f"Report: {report_path}")
    print(f"{'=' * 60}")

    return 1 if total_errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
