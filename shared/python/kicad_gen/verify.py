"""
Schematic verification utilities for KiCad schematics.

Provides general-purpose checks that apply to any board:
1. Diagonal wires
2. Wire overlaps (NET MERGE)
3. Dangling wire endpoints
4. Wire through component pins
5. Wire through component bodies
6. T-junctions without junction dots
7. Wire overlaps pin stub
8. Component overlap
9. Content on sheet blocks
10. Page boundary
11. Power symbol orientation

Also provides:
- parse_schematic() -- full schematic parser
- run_erc() -- kicad-cli ERC runner with standalone artifact filtering
- _UnionFind -- for netlist connectivity checks in board-specific scripts
- Helper functions for bounding box computation and transformation
"""

import json
import math
import os
import subprocess
from collections import defaultdict

from kiutils.schematic import Schematic

from .common import KICAD_CLI, snap


# ==============================================================
# Configuration
# ==============================================================

TOLERANCE = 0.0001  # mm tolerance for coordinate comparison


def pts_close(a, b):
    """Check if two points are within tolerance."""
    return abs(a[0] - b[0]) < TOLERANCE and abs(a[1] - b[1]) < TOLERANCE


# ==============================================================
# Library geometry helpers
# ==============================================================

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


def _collect_lib_geometry(lib_sym):
    """Collect graphical and pin geometry from a library symbol.

    Returns (gfx_x, gfx_y, pin_x, pin_y) -- four lists of coordinates.
    gfx covers polylines, rectangles, circles, arcs.
    pin covers pin tip and body-end positions.
    """
    gfx_x, gfx_y = [], []
    pin_x, pin_y = [], []

    sub_syms = getattr(lib_sym, 'symbols', []) or []
    if not sub_syms:
        sub_syms = getattr(lib_sym, 'units', []) or []

    for sub_sym in sub_syms:
        for item in getattr(sub_sym, 'graphicItems', []):
            cls_name = type(item).__name__
            if cls_name == 'SyPolyLine':
                for pt in getattr(item, 'points', []):
                    gfx_x.append(pt.X)
                    gfx_y.append(pt.Y)
            elif cls_name == 'SyRect':
                gfx_x.extend([item.start.X, item.end.X])
                gfx_y.extend([item.start.Y, item.end.Y])
            elif cls_name == 'SyCircle':
                r = getattr(item, 'radius', 0) or 0
                gfx_x.extend([item.center.X - r, item.center.X + r])
                gfx_y.extend([item.center.Y - r, item.center.Y + r])
            elif cls_name == 'SyArc':
                for attr in ('start', 'mid', 'end'):
                    pt = getattr(item, attr, None)
                    if pt:
                        gfx_x.append(pt.X)
                        gfx_y.append(pt.Y)

        for pin in getattr(sub_sym, 'pins', []):
            px, py = pin.position.X, pin.position.Y
            pa = getattr(pin.position, 'angle', 0) or 0
            pl = getattr(pin, 'length', 2.54) or 2.54
            pin_x.append(px)
            pin_y.append(py)
            rad = math.radians(pa)
            pin_x.append(px + math.cos(rad) * pl)
            pin_y.append(py + math.sin(rad) * pl)

    return gfx_x, gfx_y, pin_x, pin_y


def _compute_lib_bbox(lib_sym):
    """Compute full bounding box of a library symbol in library space (Y-up).

    Returns (min_x, min_y, max_x, max_y) or None if no geometry found.
    Includes graphical items AND pin tip/body-end positions.
    """
    gfx_x, gfx_y, pin_x, pin_y = _collect_lib_geometry(lib_sym)
    all_x = gfx_x + pin_x
    all_y = gfx_y + pin_y
    if not all_x:
        return None
    return (min(all_x), min(all_y), max(all_x), max(all_y))


def _compute_lib_body_bbox(lib_sym):
    """Compute bounding box of a library symbol's graphical body only.

    Excludes pin stubs -- only covers polylines, rectangles, circles, arcs.
    Returns (min_x, min_y, max_x, max_y) or None if no geometry found.
    """
    gfx_x, gfx_y, _, _ = _collect_lib_geometry(lib_sym)
    if not gfx_x:
        return None
    return (min(gfx_x), min(gfx_y), max(gfx_x), max(gfx_y))


def _lib_bbox_to_schematic(lib_bbox, cx, cy, angle):
    """Transform a library-space bounding box to schematic-space.

    lib_bbox: (min_x, min_y, max_x, max_y) in library coords (Y-up)
    cx, cy: component center in schematic space
    angle: component rotation in degrees (CW in schematic Y-down space)

    Returns (min_x, min_y, max_x, max_y) in schematic coords.
    """
    lmin_x, lmin_y, lmax_x, lmax_y = lib_bbox
    corners = [
        (lmin_x, lmin_y), (lmax_x, lmin_y),
        (lmax_x, lmax_y), (lmin_x, lmax_y),
    ]

    rad = math.radians(angle)
    cos_a = round(math.cos(rad), 10)
    sin_a = round(math.sin(rad), 10)

    sx_list = []
    sy_list = []
    for lx, ly in corners:
        # Library Y-up -> schematic Y-down: negate Y
        bx, by = lx, -ly
        # Rotate (same transform as _pin_schematic_offset)
        dx = snap(cos_a * bx + sin_a * by)
        dy = snap(-sin_a * bx + cos_a * by)
        sx_list.append(cx + dx)
        sy_list.append(cy + dy)

    return (min(sx_list), min(sy_list), max(sx_list), max(sy_list))


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


def _get_schematic_bbox(ref, lib_name, cx, cy, angle, lib_bboxes):
    """Return schematic-space bbox for a component, or None."""
    lib_bbox = lib_bboxes.get(lib_name)
    if lib_bbox:
        return _lib_bbox_to_schematic(lib_bbox, cx, cy, angle)
    return None


def _bboxes_overlap(a, b):
    """Return True if two (min_x, min_y, max_x, max_y) rectangles overlap."""
    return (a[0] < b[2] - TOLERANCE and a[2] > b[0] + TOLERANCE and
            a[1] < b[3] - TOLERANCE and a[3] > b[1] + TOLERANCE)


def _wire_segment_intersects_bbox(x1, y1, x2, y2, bbox):
    """Check if an orthogonal wire segment intersects a bounding box.

    The wire must be horizontal or vertical.  Returns True if any part of
    the wire's interior (excluding endpoints) passes through the bbox.
    """
    bmin_x, bmin_y, bmax_x, bmax_y = bbox

    if abs(y1 - y2) < TOLERANCE:
        # Horizontal wire -- check if wire Y is within bbox Y range
        # and wire X range overlaps bbox X range (interior only)
        wy = y1
        if wy <= bmin_y + TOLERANCE or wy >= bmax_y - TOLERANCE:
            return False
        wxmin, wxmax = min(x1, x2), max(x1, x2)
        # Interior of wire must overlap bbox X range
        return wxmin + TOLERANCE < bmax_x and wxmax - TOLERANCE > bmin_x
    elif abs(x1 - x2) < TOLERANCE:
        # Vertical wire -- check if wire X is within bbox X range
        # and wire Y range overlaps bbox Y range (interior only)
        wx = x1
        if wx <= bmin_x + TOLERANCE or wx >= bmax_x - TOLERANCE:
            return False
        wymin, wymax = min(y1, y2), max(y1, y2)
        return wymin + TOLERANCE < bmax_y and wymax - TOLERANCE > bmin_y

    return False


# ==============================================================
# Schematic parsing
# ==============================================================

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

    # -- Library symbol pin map and bounding boxes --
    lib_pin_map = {}  # lib_id -> [(pin_num, lib_x, lib_y, pin_angle, pin_length)]
    lib_bboxes = {}   # lib_name -> (min_x, min_y, max_x, max_y) in library space
    lib_body_bboxes = {}  # lib_name -> body-only bbox (no pin stubs)
    for lib_sym in sch.libSymbols:
        lib_pin_map[lib_sym.libId] = _extract_lib_pins(lib_sym)
        bbox = _compute_lib_bbox(lib_sym)
        if bbox:
            lib_bboxes[lib_sym.libId] = bbox
            short = lib_sym.libId.split(":")[-1] if ":" in lib_sym.libId else lib_sym.libId
            lib_bboxes[short] = bbox
        body_bbox = _compute_lib_body_bbox(lib_sym)
        if body_bbox:
            lib_body_bboxes[lib_sym.libId] = body_bbox
            short = lib_sym.libId.split(":")[-1] if ":" in lib_sym.libId else lib_sym.libId
            lib_body_bboxes[short] = body_bbox

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
                tip_angle_lib = pa  # body->tip in library coords
                tip_rad = math.radians(tip_angle_lib)
                btx = round(math.cos(tip_rad), 6)
                bty = round(math.sin(tip_rad), 6)
                bty_sch = -bty
                rot_rad = math.radians(angle)
                cos_r = round(math.cos(rot_rad), 6)
                sin_r = round(math.sin(rot_rad), 6)
                sdx = cos_r * btx + sin_r * bty_sch
                sdy = -sin_r * btx + cos_r * bty_sch
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

    # -- Hierarchical sheet pins and bounding boxes --
    sheet_pins = set()
    sheet_blocks = []  # [(name, x, y, w, h), ...]
    for sheet in getattr(sch, 'hierarchicalSheets', []):
        for pin in getattr(sheet, 'pins', []):
            sheet_pins.add((snap(pin.position.X), snap(pin.position.Y)))
        sx = snap(sheet.position.X)
        sy = snap(sheet.position.Y)
        sw = snap(sheet.width)
        sh = snap(sheet.height)
        sname = sheet.sheetName.value if sheet.sheetName else "?"
        sheet_blocks.append((sname, sx, sy, sw, sh))

    # -- Page size --
    page_w, page_h = 594.0, 420.0  # A2 landscape default
    paper = getattr(sch, 'paper', None) or getattr(sch, 'page', None)
    if paper:
        size_str = getattr(paper, 'paperSize', None) or getattr(paper, 'size', '')
        _page_sizes = {
            'A4': (297, 210), 'A3': (420, 297), 'A2': (594, 420),
            'A1': (841, 594), 'A0': (1189, 841),
        }
        if size_str in _page_sizes:
            page_w, page_h = _page_sizes[size_str]

    return {
        'wires': wires,
        'pins': pins,
        'pin_positions': pin_positions,
        'pin_stubs': pin_stubs,
        'junctions': junctions,
        'labels': labels,
        'no_connects': no_connects,
        'sheet_pins': sheet_pins,
        'sheet_blocks': sheet_blocks,
        'page_size': (page_w, page_h),
        'components': components,
        'lib_bboxes': lib_bboxes,
        'lib_body_bboxes': lib_body_bboxes,
    }


# ==============================================================
# Check functions
# ==============================================================

def check_diagonal_wires(data):
    """Check for wires that are neither horizontal nor vertical."""
    issues = []
    for i, ((x1, y1), (x2, y2)) in enumerate(data['wires']):
        if abs(x1 - x2) > TOLERANCE and abs(y1 - y2) > TOLERANCE:
            issues.append(
                f"  Wire #{i}: ({x1}, {y1}) -> ({x2}, {y2}) is diagonal"
            )
    return issues


def check_wire_overlaps(data):
    """Check for same-direction wire overlaps (silent NET MERGE)."""
    wires = data['wires']
    issues = []

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
    """Check for wire endpoints not connected to anything."""
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
            found = any(pts_close(pt, cp) for cp in connected)
            if not found:
                issues.append(f"  Dangling at ({pt[0]}, {pt[1]})")

    return issues


def check_wire_through_pins(data):
    """Check for wires passing through component pins (unintended connection)."""
    wires = data['wires']
    pins = data['pins']

    issues = []

    wire_endpoints = set()
    for (x1, y1), (x2, y2) in wires:
        wire_endpoints.add((x1, y1))
        wire_endpoints.add((x2, y2))

    for (px, py), (ref, pin_num, lib_name) in pins.items():
        if ref.startswith("#"):
            continue
        if (px, py) in wire_endpoints:
            continue
        if any(pts_close((px, py), ep) for ep in wire_endpoints):
            continue

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


def check_wire_through_body(data):
    """Check for wires passing through component graphical bodies."""
    wires = data['wires']
    comps = data['components']
    lib_body_bboxes = data.get('lib_body_bboxes', {})
    pins = data['pins']
    issues = []

    # Build lookup: ref -> set of pin positions
    ref_pins = {}
    for (px, py), (ref, _pnum, _lib) in pins.items():
        ref_pins.setdefault(ref, set()).add((px, py))

    # Pre-compute schematic body bboxes per component
    comp_body_bboxes = []
    for ref, lib_name, cx, cy, angle in comps:
        if ref.startswith("#"):
            comp_body_bboxes.append(None)
            continue
        body_bbox = lib_body_bboxes.get(lib_name)
        if body_bbox:
            comp_body_bboxes.append(
                _lib_bbox_to_schematic(body_bbox, cx, cy, angle))
        else:
            comp_body_bboxes.append(None)

    for w_idx, ((x1, y1), (x2, y2)) in enumerate(wires):
        for c_idx, (ref, lib_name, cx, cy, angle) in enumerate(comps):
            bbox = comp_body_bboxes[c_idx]
            if bbox is None:
                continue

            if not _wire_segment_intersects_bbox(x1, y1, x2, y2, bbox):
                continue

            comp_pins = ref_pins.get(ref, set())
            ep1 = (x1, y1)
            ep2 = (x2, y2)
            connects = any(
                pts_close(ep, pp) for ep in (ep1, ep2) for pp in comp_pins)
            if connects:
                continue

            issues.append(
                f"  Wire #{w_idx} ({x1},{y1})->({x2},{y2}) "
                f"passes through body of {ref} ({lib_name}) "
                f"bbox [{bbox[0]:.2f},{bbox[1]:.2f}]-"
                f"[{bbox[2]:.2f},{bbox[3]:.2f}]"
            )

    return issues


def check_tjunctions_without_dots(data):
    """Find T-junctions missing explicit junction dots (warning only)."""
    wires = data['wires']
    junctions = data['junctions']

    endpoint_set = set()
    for (x1, y1), (x2, y2) in wires:
        endpoint_set.add((x1, y1))
        endpoint_set.add((x2, y2))

    issues = []
    seen = set()

    for pt in endpoint_set:
        for (x1, y1), (x2, y2) in wires:
            if abs(y1 - y2) < TOLERANCE:  # horizontal wire
                xmin, xmax = min(x1, x2), max(x1, x2)
                y = snap(y1)
                if (abs(pt[1] - y) < TOLERANCE and
                        xmin + TOLERANCE < pt[0] < xmax - TOLERANCE):
                    key = (pt, "H", y, xmin, xmax)
                    if key not in seen and pt not in junctions:
                        issues.append(
                            f"  T-junction at ({pt[0]}, {pt[1]}) on "
                            f"H wire ({x1},{y1})->({x2},{y2}) -- no junction dot"
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
                            f"V wire ({x1},{y1})->({x2},{y2}) -- no junction dot"
                        )
                        seen.add(key)

    return issues


def check_component_overlap(data):
    """Check for non-power components whose bounding boxes overlap."""
    comps = data['components']
    lib_bboxes = data.get('lib_bboxes', {})
    issues = []

    non_power = [
        (ref, lib, cx, cy, angle)
        for ref, lib, cx, cy, angle in comps
        if not ref.startswith("#") and not lib.startswith("Conn")
    ]

    sch_bboxes = []
    for ref, lib, cx, cy, angle in non_power:
        sch_bboxes.append(_get_schematic_bbox(ref, lib, cx, cy, angle, lib_bboxes))

    MIN_DIST = 1.5

    for i in range(len(non_power)):
        ref_a, lib_a, ax, ay, _aa = non_power[i]
        bbox_a = sch_bboxes[i]
        for j in range(i + 1, len(non_power)):
            ref_b, lib_b, bx, by, _ab = non_power[j]
            bbox_b = sch_bboxes[j]

            if bbox_a and bbox_b:
                if _bboxes_overlap(bbox_a, bbox_b):
                    issues.append(
                        f"  {ref_a} ({lib_a}) bbox "
                        f"[{bbox_a[0]:.2f},{bbox_a[1]:.2f}]-"
                        f"[{bbox_a[2]:.2f},{bbox_a[3]:.2f}] overlaps "
                        f"{ref_b} ({lib_b}) bbox "
                        f"[{bbox_b[0]:.2f},{bbox_b[1]:.2f}]-"
                        f"[{bbox_b[2]:.2f},{bbox_b[3]:.2f}]"
                    )
            else:
                dist = math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)
                if dist < MIN_DIST:
                    issues.append(
                        f"  {ref_a} ({lib_a}) and {ref_b} ({lib_b}) overlap: "
                        f"centers ({ax},{ay}) and ({bx},{by}) dist={dist:.2f}mm"
                    )

    return issues


def check_content_on_sheet_blocks(data):
    """Check for wires or components drawn on top of sheet block areas."""
    sheet_blocks = data.get('sheet_blocks', [])
    if not sheet_blocks:
        return []

    wires = data['wires']
    comps = data['components']
    lib_bboxes = data.get('lib_bboxes', {})
    issues = []

    def _inside_block(px, py, bx, by, bw, bh):
        return (bx + TOLERANCE < px < bx + bw - TOLERANCE and
                by + TOLERANCE < py < by + bh - TOLERANCE)

    def _bbox_intrudes_block(comp_bbox, bx, by, bw, bh):
        block_bbox = (bx, by, bx + bw, by + bh)
        return _bboxes_overlap(comp_bbox, block_bbox)

    sheet_pin_set = data.get('sheet_pins', set())

    for ref, lib_name, cx, cy, angle in comps:
        if ref.startswith("#"):
            continue
        comp_bbox = _get_schematic_bbox(ref, lib_name, cx, cy, angle, lib_bboxes)
        for sname, bx, by, bw, bh in sheet_blocks:
            if comp_bbox:
                if _bbox_intrudes_block(comp_bbox, bx, by, bw, bh):
                    issues.append(
                        f"  {ref} ({lib_name}) bbox "
                        f"[{comp_bbox[0]:.2f},{comp_bbox[1]:.2f}]-"
                        f"[{comp_bbox[2]:.2f},{comp_bbox[3]:.2f}] "
                        f"intrudes into sheet block \"{sname}\" "
                        f"[{bx},{by} {bw}x{bh}]"
                    )
            else:
                if _inside_block(cx, cy, bx, by, bw, bh):
                    issues.append(
                        f"  {ref} ({lib_name}) at ({cx},{cy}) inside "
                        f"sheet block \"{sname}\" [{bx},{by} {bw}x{bh}]"
                    )

    for w_idx, ((x1, y1), (x2, y2)) in enumerate(wires):
        for sname, bx, by, bw, bh in sheet_blocks:
            p1_in = _inside_block(x1, y1, bx, by, bw, bh)
            p2_in = _inside_block(x2, y2, bx, by, bw, bh)
            if p1_in or p2_in:
                issues.append(
                    f"  Wire #{w_idx} ({x1},{y1})->({x2},{y2}) "
                    f"enters sheet block \"{sname}\" [{bx},{by} {bw}x{bh}]"
                )

    return issues


def check_page_boundary(data):
    """Check for wires or component bboxes outside the page drawing area."""
    page_w, page_h = data.get('page_size', (594.0, 420.0))
    margin = 12.5  # mm from each edge (KiCad drawing border)
    border_tol = 0.5
    min_x, min_y = margin - border_tol, margin - border_tol
    max_x = page_w - margin + border_tol
    max_y = page_h - margin + border_tol

    wires = data['wires']
    comps = data['components']
    lib_bboxes = data.get('lib_bboxes', {})
    issues = []

    def _outside(px, py):
        return px < min_x or px > max_x or py < min_y or py > max_y

    for w_idx, ((x1, y1), (x2, y2)) in enumerate(wires):
        if _outside(x1, y1) or _outside(x2, y2):
            issues.append(
                f"  Wire #{w_idx} ({x1},{y1})->({x2},{y2}) "
                f"outside page border [{min_x},{min_y}]-[{max_x},{max_y}]"
            )

    for ref, lib_name, cx, cy, angle in comps:
        if ref.startswith("#"):
            continue

        lib_bbox = lib_bboxes.get(lib_name)
        if lib_bbox:
            smin_x, smin_y, smax_x, smax_y = _lib_bbox_to_schematic(
                lib_bbox, cx, cy, angle)
            if (smin_x < min_x or smax_x > max_x or
                    smin_y < min_y or smax_y > max_y):
                issues.append(
                    f"  {ref} ({lib_name}) bbox "
                    f"[{smin_x:.2f},{smin_y:.2f}]-[{smax_x:.2f},{smax_y:.2f}] "
                    f"extends outside page border "
                    f"[{min_x},{min_y}]-[{max_x},{max_y}]"
                )
        else:
            if _outside(cx, cy):
                issues.append(
                    f"  {ref} ({lib_name}) at ({cx},{cy}) "
                    f"outside page border [{min_x},{min_y}]-[{max_x},{max_y}]"
                )

    return issues


def check_wire_overlaps_pin_stub(data):
    """Check for wires that overlap with a pin's built-in stub line."""
    wires = data['wires']
    pins = data['pins']
    pin_stubs = data.get('pin_stubs', {})

    if not pin_stubs:
        return []

    issues = []
    seen = set()

    for (px, py), (ref, pin_num, lib_name) in pins.items():
        if ref.startswith("#"):
            continue
        if lib_name.startswith("Conn"):
            continue
        if (px, py) not in pin_stubs:
            continue

        sdx, sdy, slen = pin_stubs[(px, py)]
        if abs(sdx) < 0.01 and abs(sdy) < 0.01:
            continue

        for w_idx, ((x1, y1), (x2, y2)) in enumerate(wires):
            if pts_close((x1, y1), (px, py)):
                other = (x2, y2)
            elif pts_close((x2, y2), (px, py)):
                other = (x1, y1)
            else:
                continue

            wx = other[0] - px
            wy = other[1] - py
            wire_len = math.sqrt(wx * wx + wy * wy)
            if wire_len < TOLERANCE:
                continue

            dot = wx * sdx + wy * sdy
            cos_angle = dot / wire_len
            if cos_angle > 0.96:
                key = (px, py, ref, pin_num)
                if key not in seen:
                    issues.append(
                        f"  Wire #{w_idx} overlaps stub of {ref} pin {pin_num} "
                        f"at ({px},{py})"
                    )
                    seen.add(key)

    return issues


def check_power_orientation(data):
    """Check that VCC symbols point up (angle=0) and GND symbols point down (angle=0)."""
    issues = []
    for ref, lib_name, cx, cy, angle in data['components']:
        if lib_name == "VCC" and abs(angle) > TOLERANCE:
            issues.append(
                f"  {ref} (VCC) at ({cx},{cy}) has angle={angle} "
                f"-- should be 0 (pointing up)"
            )
        elif lib_name == "GND" and abs(angle) > TOLERANCE:
            issues.append(
                f"  {ref} (GND) at ({cx},{cy}) has angle={angle} "
                f"-- should be 0 (pointing down)"
            )
    return issues


# ==============================================================
# Convenience: run all general checks
# ==============================================================

def run_all_checks(filepath, data=None):
    """Run all 11 general-purpose checks on a schematic file.

    Args:
        filepath: Path to .kicad_sch file (used only if data is None)
        data: Pre-parsed schematic data (from parse_schematic). If None,
              the file is parsed automatically.

    Returns list of (category_name, issues_list, is_error) tuples.
    Only non-empty checks are included.
    """
    if data is None:
        data = parse_schematic(filepath)

    results = []

    diag = check_diagonal_wires(data)
    if diag:
        results.append(("Diagonal Wires", diag, True))

    overlaps = check_wire_overlaps(data)
    if overlaps:
        results.append(("Wire Overlaps (NET MERGE)", overlaps, True))

    dangles = check_dangling_endpoints(data)
    if dangles:
        results.append(("Dangling Endpoints", dangles, True))

    through = check_wire_through_pins(data)
    if through:
        results.append(("Wire Through Pin", through, True))

    through_body = check_wire_through_body(data)
    if through_body:
        results.append(("Wire Through Body", through_body, True))

    tjuncs = check_tjunctions_without_dots(data)
    if tjuncs:
        results.append(("T-junction (no dot)", tjuncs, False))

    stubs = check_wire_overlaps_pin_stub(data)
    if stubs:
        results.append(("Wire Overlaps Pin Stub", stubs, True))

    overlaps_comp = check_component_overlap(data)
    if overlaps_comp:
        results.append(("Component Overlap", overlaps_comp, True))

    on_sheets = check_content_on_sheet_blocks(data)
    if on_sheets:
        results.append(("Content on Sheet Block", on_sheets, True))

    outside = check_page_boundary(data)
    if outside:
        results.append(("Outside Page Border", outside, True))

    power_orient = check_power_orientation(data)
    if power_orient:
        results.append(("Power Orientation", power_orient, True))

    return results


# ==============================================================
# ERC via kicad-cli
# ==============================================================

def _is_standalone_artifact(violation):
    """Check if an ERC violation is an expected standalone sub-sheet artifact."""
    desc = violation.get("description", "")
    vtype = violation.get("type", "")

    if "cannot be connected to non-existent parent sheet" in desc:
        return True

    if vtype == "label_dangling":
        items = violation.get("items", [])
        if any("Hierarchical Label" in it.get("description", "")
               for it in items):
            return True

    if vtype == "wire_dangling":
        return True

    if vtype == "pin_not_driven" and "Input pin not driven" in desc:
        return True

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


# ==============================================================
# Union-Find (for board-specific netlist checks)
# ==============================================================

class UnionFind:
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
