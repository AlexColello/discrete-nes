"""
Custom footprint generation for power supply components.

Creates:
- TPS546D24A LQFN-CLIP-40 (7mm x 5mm, 0.5mm pitch)
- SMD power connector 2x4 (4.20mm pitch, SMD pads for wire soldering)
"""

import os


def create_tps546d24a_footprint(output_dir: str) -> str:
    """Create TPS546D24A LQFN-CLIP-40 footprint from TI RVF0040A land pattern.

    Dimensions from TPS546D24A datasheet (SLUSDN0A), pages 176-178:
    - Body: 7.0mm x 5.0mm
    - 40 perimeter pads + 1 exposed thermal pad (pin 41)
    - Pad size: 0.25mm x 0.6mm (per land pattern recommendation)
    - Pitch: 0.5mm
    - Exposed pad: 5.3mm x 3.3mm

    Pin layout (top view, pin 1 at top-left):
    - Pins 1-12:  left side (top to bottom) + bottom-left corner
    - Pins 13-20: bottom side (left to right)
    - Pins 21-32: right side (bottom to top) + top-right corner
    - Pins 33-40: top side (right to left)
    """
    os.makedirs(output_dir, exist_ok=True)

    # Datasheet: 5mm WIDE (X) × 7mm TALL (Y) — NOT 7×5!
    body_x = 5.0   # mm (width)
    body_y = 7.0   # mm (height)
    pad_w = 0.25   # mm (along edge)
    pad_h = 0.60   # mm (perpendicular to edge)
    pitch = 0.50   # mm
    ep_x = 3.3     # exposed pad width
    ep_y = 5.3     # exposed pad height
    crtyd_margin = 0.25  # courtyard margin beyond land pattern

    # Pin distribution: left=12, bottom=8, right=12, top=8 = 40
    # From datasheet figure: pins 1-12 on left (long side), 13-20 on bottom
    # (short side), 21-32 on right (long side), 33-40 on top (short side)
    n_left = 12    # pins 1-12
    n_bottom = 8   # pins 13-20
    n_right = 12   # pins 21-32
    n_top = 8      # pins 33-40

    # Pad centers: distance from body center to pad center
    # Land pattern: 4.8mm wide × 6.8mm tall
    # Left/right pads at X = ±2.4, top/bottom pads at Y = ±3.4
    pad_cx = 2.40  # X distance from center to left/right pad centers
    pad_cy = 3.40  # Y distance from center to top/bottom pad centers

    lines = []
    lines.append('(footprint "TPS546D24A_LQFN-CLIP-40"')
    lines.append('  (version 20241229)')
    lines.append('  (generator "custom")')
    lines.append('  (layer "F.Cu")')
    lines.append(f'  (descr "TI TPS546D24A LQFN-CLIP-40, 7x5mm, 0.5mm pitch, 40A buck converter")')
    lines.append(f'  (tags "QFN LQFN CLIP 40 0.5 TPS546D24A")')
    lines.append('  (property "Reference" "REF**"')
    lines.append('    (at 0 -4.0 0)')
    lines.append('    (layer "F.SilkS")')
    lines.append('    (effects (font (size 1 1) (thickness 0.15)))')
    lines.append('  )')
    lines.append('  (property "Value" "TPS546D24A"')
    lines.append('    (at 0 4.0 0)')
    lines.append('    (layer "F.Fab")')
    lines.append('    (effects (font (size 1 1) (thickness 0.15)))')
    lines.append('  )')
    lines.append('  (attr smd)')

    # Body outline on F.Fab
    hx, hy = body_x / 2, body_y / 2
    lines.append(f'  (fp_rect (start {-hx} {-hy}) (end {hx} {hy})')
    lines.append(f'    (stroke (width 0.1) (type default)) (fill none) (layer "F.Fab"))')

    # Pin 1 marker on F.SilkS (small triangle at top-left)
    lines.append(f'  (fp_line (start {-hx - 0.3} {-hy - 0.3}) (end {-hx + 0.5} {-hy - 0.3})')
    lines.append(f'    (stroke (width 0.12) (type default)) (layer "F.SilkS"))')
    lines.append(f'  (fp_line (start {-hx - 0.3} {-hy - 0.3}) (end {-hx - 0.3} {-hy + 0.5})')
    lines.append(f'    (stroke (width 0.12) (type default)) (layer "F.SilkS"))')

    # Courtyard
    cx = pad_cx + pad_h / 2 + crtyd_margin
    cy = pad_cy + pad_h / 2 + crtyd_margin
    lines.append(f'  (fp_rect (start {-cx:.2f} {-cy:.2f}) (end {cx:.2f} {cy:.2f})')
    lines.append(f'    (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))')

    def add_pad(num, x, y, w, h):
        lines.append(f'  (pad "{num}" smd rect')
        lines.append(f'    (at {x:.4f} {y:.4f})')
        lines.append(f'    (size {w:.2f} {h:.2f})')
        lines.append(f'    (layers "F.Cu" "F.Paste" "F.Mask"))')

    # Left side pads (pins 1-12): X = -pad_cx, going top to bottom
    # 12 pins: first pin at Y = -(n_left-1)/2 * pitch, last at +(n_left-1)/2 * pitch
    for i in range(n_left):
        pin = i + 1
        y = round(-(n_left - 1) / 2 * pitch + i * pitch, 4)
        add_pad(pin, -pad_cx, y, pad_h, pad_w)  # rotated: h along X, w along Y

    # Bottom side pads (pins 13-20): Y = +pad_cy, going left to right
    for i in range(n_bottom):
        pin = n_left + i + 1
        x = round(-(n_bottom - 1) / 2 * pitch + i * pitch, 4)
        add_pad(pin, x, pad_cy, pad_w, pad_h)  # w along X, h along Y

    # Right side pads (pins 21-32): X = +pad_cx, going bottom to top
    for i in range(n_right):
        pin = n_left + n_bottom + i + 1
        y = round((n_right - 1) / 2 * pitch - i * pitch, 4)
        add_pad(pin, pad_cx, y, pad_h, pad_w)

    # Top side pads (pins 33-40): Y = -pad_cy, going right to left
    for i in range(n_top):
        pin = n_left + n_bottom + n_right + i + 1
        x = round((n_top - 1) / 2 * pitch - i * pitch, 4)
        add_pad(pin, x, -pad_cy, pad_w, pad_h)

    # Exposed thermal pad (pin 41)
    lines.append(f'  (pad "41" smd rect')
    lines.append(f'    (at 0 0)')
    lines.append(f'    (size {ep_x} {ep_y})')
    lines.append(f'    (layers "F.Cu" "F.Paste" "F.Mask")')
    lines.append(f'    (thermal_bridge_angle 45)')
    lines.append(f'  )')

    lines.append(')')

    fp_path = os.path.join(output_dir, "TPS546D24A_LQFN-CLIP-40.kicad_mod")
    with open(fp_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return fp_path


def create_smd_power_connector_footprint(output_dir: str) -> str:
    """Create SMD 2x4 power connector footprint for 12V input.

    Simple SMD pad grid at 4.20mm pitch, compatible with soldering
    power wires directly. No through-holes.

    Pad size: 2.0mm x 2.0mm (large for high current + easy soldering)
    """
    os.makedirs(output_dir, exist_ok=True)

    pitch = 4.20   # mm (Molex Mini-Fit Jr compatible)
    pad_size = 2.0  # mm square
    n_rows = 4
    n_cols = 2
    crtyd_margin = 1.0

    lines = []
    lines.append('(footprint "SMD_Power_2x04_P4.20mm"')
    lines.append('  (version 20241229)')
    lines.append('  (generator "custom")')
    lines.append('  (layer "F.Cu")')
    lines.append('  (descr "SMD 2x4 power connector pads, 4.20mm pitch, for 12V wire soldering")')
    lines.append('  (tags "SMD power connector 2x4 4.20")')
    lines.append('  (property "Reference" "REF**"')
    lines.append('    (at 0 -8.0 0)')
    lines.append('    (layer "F.SilkS")')
    lines.append('    (effects (font (size 1 1) (thickness 0.15)))')
    lines.append('  )')
    lines.append('  (property "Value" "PCIe_8pin_SMD"')
    lines.append('    (at 0 8.0 0)')
    lines.append('    (layer "F.Fab")')
    lines.append('    (effects (font (size 1 1) (thickness 0.15)))')
    lines.append('  )')
    lines.append('  (attr smd)')

    # Conn_02x04_Odd_Even numbering: odd on left (col 0), even on right (col 1)
    # Pin 1=top-left, 2=top-right, 3=second-left, 4=second-right, etc.
    col_x = [-pitch / 2, pitch / 2]
    for row in range(n_rows):
        y = round(-(n_rows - 1) / 2 * pitch + row * pitch, 4)
        odd_pin = 2 * row + 1   # left column
        even_pin = 2 * row + 2  # right column
        lines.append(f'  (pad "{odd_pin}" smd rect')
        lines.append(f'    (at {col_x[0]:.4f} {y:.4f})')
        lines.append(f'    (size {pad_size} {pad_size})')
        lines.append(f'    (layers "F.Cu" "F.Paste" "F.Mask"))')
        lines.append(f'  (pad "{even_pin}" smd rect')
        lines.append(f'    (at {col_x[1]:.4f} {y:.4f})')
        lines.append(f'    (size {pad_size} {pad_size})')
        lines.append(f'    (layers "F.Cu" "F.Paste" "F.Mask"))')

    # Outline on F.Fab
    total_x = pitch + pad_size
    total_y = (n_rows - 1) * pitch + pad_size
    hx, hy = total_x / 2, total_y / 2
    lines.append(f'  (fp_rect (start {-hx:.2f} {-hy:.2f}) (end {hx:.2f} {hy:.2f})')
    lines.append(f'    (stroke (width 0.1) (type default)) (fill none) (layer "F.Fab"))')

    # Pin 1 marker
    lines.append(f'  (fp_circle (center {col_x[0]:.2f} {-(n_rows-1)/2*pitch - pad_size/2 - 0.3:.2f}) (end {col_x[0] + 0.15:.2f} {-(n_rows-1)/2*pitch - pad_size/2 - 0.3:.2f})')
    lines.append(f'    (stroke (width 0.12) (type default)) (fill solid) (layer "F.SilkS"))')

    # Courtyard
    cx = hx + crtyd_margin
    cy = hy + crtyd_margin
    lines.append(f'  (fp_rect (start {-cx:.2f} {-cy:.2f}) (end {cx:.2f} {cy:.2f})')
    lines.append(f'    (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd"))')

    lines.append(')')

    fp_path = os.path.join(output_dir, "SMD_Power_2x04_P4.20mm.kicad_mod")
    with open(fp_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    return fp_path
