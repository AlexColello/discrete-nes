"""PCB snapshot utilities — export cropped PNG images of board regions.

Provides functions to:
- Export KiCad PCBs to SVG via kicad-cli
- Crop SVGs to a bounding box (in PCB mm coordinates)
- Inject X-shaped markers at specific coordinates
- Render SVGs to high-DPI PNGs via PyMuPDF

All coordinates are in PCB mm unless noted otherwise.
"""

import os
import re
import subprocess
import sys
import tempfile

from .common import KICAD_CLI

DEFAULT_LAYERS = "F.Cu,B.Cu,In1.Cu,In2.Cu,Edge.Cuts,F.SilkS,F.Fab"
DPI = 2400
MARGIN_MM = 1.0


def find_board_outline(pcb_path):
    """Parse Edge.Cuts from a .kicad_pcb file to find the board bounding box.

    Returns (x_min, y_min, x_max, y_max) in PCB mm coordinates.
    """
    with open(pcb_path, "r") as f:
        content = f.read()

    coords = []

    for pat in [
        re.compile(
            r'\(gr_line\s+\(start\s+([\d.]+)\s+([\d.]+)\)\s+\(end\s+([\d.]+)\s+([\d.]+)\)'
            r'.*?Edge\.Cuts', re.DOTALL),
        re.compile(
            r'\(gr_rect\s+\(start\s+([\d.]+)\s+([\d.]+)\)\s+\(end\s+([\d.]+)\s+([\d.]+)\)'
            r'.*?Edge\.Cuts', re.DOTALL),
        re.compile(
            r'\(gr_arc\s+\(start\s+([\d.]+)\s+([\d.]+)\)\s+'
            r'(?:\(mid\s+[\d.]+\s+[\d.]+\)\s+)?'
            r'\(end\s+([\d.]+)\s+([\d.]+)\)'
            r'.*?Edge\.Cuts', re.DOTALL),
    ]:
        for m in pat.finditer(content):
            coords.extend([
                (float(m.group(1)), float(m.group(2))),
                (float(m.group(3)), float(m.group(4))),
            ])

    if not coords:
        print("WARNING: No Edge.Cuts found in PCB file", file=sys.stderr)
        return (0, 0, 100, 100)

    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def export_svg(pcb_path, layers, svg_path):
    """Export PCB to SVG using kicad-cli. Returns True on success."""
    cmd = [
        KICAD_CLI, "pcb", "export", "svg",
        "--layers", layers,
        "--exclude-drawing-sheet",
        "--page-size-mode", "2",
        "--mode-single",
        "-o", svg_path,
        pcb_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"kicad-cli SVG export failed: {result.stderr}", file=sys.stderr)
        return False
    return True


def find_svg_offset(pcb_path):
    """Determine the PCB → SVG coordinate offset.

    KiCad SVG export uses 1:1 mm scale but applies a constant translation.
    Returns (offset_x, offset_y) such that: SVG_coord = PCB_coord + offset.
    """
    board_x_min, board_y_min, _, _ = find_board_outline(pcb_path)

    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False) as f:
        edge_svg = f.name

    try:
        if not export_svg(pcb_path, "Edge.Cuts", edge_svg):
            return (0.0, 0.0)

        with open(edge_svg, "r") as f:
            content = f.read()

        coords = re.findall(r'([ML])([\d.]+)\s+([\d.]+)', content)
        if not coords:
            return (0.0, 0.0)

        svg_xs = [float(x) for _, x, _ in coords]
        svg_ys = [float(y) for _, _, y in coords]
        return (min(svg_xs) - board_x_min, min(svg_ys) - board_y_min)
    finally:
        try:
            os.unlink(edge_svg)
        except OSError:
            pass


def crop_svg(svg_path, bbox_svg, out_path):
    """Rewrite an SVG file with a new viewBox to crop to a region.

    bbox_svg: (x1, y1, x2, y2) in SVG coordinates.
    """
    with open(svg_path, "r") as f:
        content = f.read()

    x, y, x2, y2 = bbox_svg
    w, h = x2 - x, y2 - y
    new_vb = f"{x:.4f} {y:.4f} {w:.4f} {h:.4f}"

    content = re.sub(r'viewBox="[^"]*"', f'viewBox="{new_vb}"', content)
    content = re.sub(r'width="[^"]*"', f'width="{w:.4f}mm"', content)
    content = re.sub(r'height="[^"]*"', f'height="{h:.4f}mm"', content)

    with open(out_path, "w") as f:
        f.write(content)


def inject_svg_markers(svg_path, markers_svg, out_path,
                       marker_size=1.0, color="red", stroke_width=0.15):
    """Inject X-shaped markers into an SVG file at given coordinates.

    markers_svg: list of (svg_x, svg_y) positions.
    marker_size: half-size of each X in mm (marker spans 2x this).
    """
    with open(svg_path, "r") as f:
        content = f.read()

    elements = []
    for sx, sy in markers_svg:
        d = marker_size
        for dx1, dy1, dx2, dy2 in [(-d, -d, d, d), (d, -d, -d, d)]:
            elements.append(
                f'<line x1="{sx+dx1:.4f}" y1="{sy+dy1:.4f}" '
                f'x2="{sx+dx2:.4f}" y2="{sy+dy2:.4f}" '
                f'stroke="{color}" stroke-width="{stroke_width}" '
                f'stroke-linecap="round"/>')

    group = (f'<g id="drc-markers" opacity="0.85">\n'
             + '\n'.join(elements)
             + '\n</g>\n')
    content = content.replace('</svg>', group + '</svg>')

    with open(out_path, "w") as f:
        f.write(content)


def svg_to_png(svg_path, png_path):
    """Render SVG to PNG using PyMuPDF at 600 DPI. Returns (width, height)."""
    import fitz
    doc = fitz.open(svg_path)
    page = doc[0]
    zoom = DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    pix.save(png_path)
    w, h = pix.width, pix.height
    doc.close()
    return w, h


def snapshot_region(pcb_path, bbox_pcb, out_png, layers=DEFAULT_LAYERS,
                    markers=None, svg_cache=None):
    """End-to-end: export SVG, optionally crop + mark, render PNG.

    Args:
        pcb_path: Path to .kicad_pcb file.
        bbox_pcb: (x1, y1, x2, y2) in PCB mm coords, or None for full board.
        out_png: Output PNG path.
        layers: Comma-separated layer list.
        markers: Optional list of (pcb_x, pcb_y) for X markers.
        svg_cache: Optional dict {"svg_path": str, "offset": (ox, oy)}
                   to reuse a pre-exported SVG and offset. Caller owns cleanup.

    Returns (width, height) in pixels.
    """
    # Get or compute offset and SVG
    if svg_cache:
        full_svg = svg_cache["svg_path"]
        offset_x, offset_y = svg_cache["offset"]
        own_svg = False
    else:
        offset_x, offset_y = find_svg_offset(pcb_path)
        tmp = tempfile.NamedTemporaryFile(suffix=".svg", delete=False)
        tmp.close()
        full_svg = tmp.name
        if not export_svg(pcb_path, layers, full_svg):
            return (0, 0)
        own_svg = True

    tmp_files = []
    try:
        render_svg = full_svg

        if bbox_pcb:
            x1, y1, x2, y2 = bbox_pcb
            svg_bbox = (x1 + offset_x - MARGIN_MM,
                        y1 + offset_y - MARGIN_MM,
                        x2 + offset_x + MARGIN_MM,
                        y2 + offset_y + MARGIN_MM)
            cropped = tempfile.NamedTemporaryFile(
                suffix="_crop.svg", delete=False)
            cropped.close()
            tmp_files.append(cropped.name)
            crop_svg(full_svg, svg_bbox, cropped.name)
            render_svg = cropped.name

        if markers:
            svg_markers = [(mx + offset_x, my + offset_y)
                           for mx, my in markers]
            marked = tempfile.NamedTemporaryFile(
                suffix="_mark.svg", delete=False)
            marked.close()
            tmp_files.append(marked.name)
            inject_svg_markers(render_svg, svg_markers, marked.name)
            render_svg = marked.name

        return svg_to_png(render_svg, out_png)
    finally:
        if own_svg:
            try:
                os.unlink(full_svg)
            except OSError:
                pass
        for f in tmp_files:
            try:
                os.unlink(f)
            except OSError:
                pass
