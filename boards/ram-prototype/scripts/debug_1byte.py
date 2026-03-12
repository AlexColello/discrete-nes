#!/usr/bin/env python3
"""Debug script: place 1 byte (8 DFF + 8 BUF + LEDs + Rs) and test
straight cardinal power via escape.

Generates a minimal PCB, runs DRC (with grouped output + snapshots),
and exports a PNG for visual inspection.

All output goes to verify_output/debug_1byte/ (gitignored).
"""

import math
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen.pcb import PCBBuilder, create_dsbga_footprints
from kicad_gen.common import uid
from kicad_gen.verify import run_drc
from kicad_gen.snapshot import snapshot_region

BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
OUTPUT_DIR = os.path.join(BOARD_DIR, "verify_output", "debug_1byte")
SHARED_FP_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared",
    "kicad-lib", "footprints", "DSBGA_Packages.pretty"))

# Layout constants (same as generate_pcb.py)
IC_CELL_W = 5.0
IC_CELL_H = 2.0
LED_OFFSET_X = 2.45
VIA_SIZE = 0.8
VIA_DRILL = 0.4
VIA_OFFSET = 0.7
POWER_TRACE_W = 0.3
SIGNAL_TRACE_W = 0.2

# Board: place 1 byte centered with generous margin
ORIGIN_X = 20.0
ORIGIN_Y = 20.0
BOARD_W = 50.0
BOARD_H = 8.0

# DRC violation types to skip (expected for this debug board)
SKIP_TYPES = {"unconnected_items", "lib_footprint_mismatch",
              "lib_footprint_issues", "silk_overlap",
              "text_thickness", "text_height"}


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Create DSBGA footprints
    create_dsbga_footprints(SHARED_FP_DIR)

    pcb = PCBBuilder(title="Debug 1-Byte Power Vias")
    pcb.add_fp_lib_path("DSBGA_Packages", SHARED_FP_DIR)

    # Register nets
    nets = ["GND", "VCC"]
    for i in range(8):
        nets.extend([f"D{i}", f"CLK{i}", f"Q{i}", f"OE{i}", f"BUF_Y{i}",
                     f"LED_DFF{i}", f"LED_BUF{i}"])
    for n in nets:
        pcb.add_net(n)

    # 4-layer stackup
    pcb.set_4layer_stackup()
    pcb.set_layer_type("B.Cu", "power")
    pcb.set_layer_type("In1.Cu", "signal")

    # Place 8 DFF + LED + R, 8 BUF + LED + R
    dff_y = ORIGIN_Y + 8.0
    buf_y = dff_y + IC_CELL_H

    for col in range(8):
        ic_x = ORIGIN_X + 5.0 + col * IC_CELL_W
        led_x = ic_x + LED_OFFSET_X

        # --- DFF (74LVC1G79) at 90° ---
        # Pin map (KiCad symbol): 1=D, 2=CLK, 3=GND, 4=Q, 5=VCC
        pcb.place_component(
            ref=f"U_DFF{col}",
            lib_fp="DSBGA_Packages:DSBGA-5_NumericPads",
            x=ic_x, y=dff_y, angle=90, layer="F.Cu",
            net_map={"1": f"D{col}", "2": f"CLK{col}", "3": "GND",
                     "4": f"Q{col}", "5": "VCC"},
            tstamp=uid(),
        )
        pcb.place_component(
            ref=f"D_DFF{col}",
            lib_fp="LED_SMD:LED_0402_1005Metric",
            x=led_x, y=dff_y, angle=90, layer="F.Cu",
            net_map={"1": f"LED_DFF{col}", "2": f"Q{col}"},
            tstamp=uid(),
        )
        pcb.place_component(
            ref=f"R_DFF{col}",
            lib_fp="Resistor_SMD:R_0402_1005Metric",
            x=led_x, y=dff_y, angle=90, layer="B.Cu",
            net_map={"1": f"LED_DFF{col}", "2": "GND"},
            tstamp=uid(),
        )

        # --- BUF (74LVC1G125) at 270° ---
        # Pin map (KiCad symbol): 1=nOE, 2=A, 3=GND, 4=Y, 5=VCC
        pcb.place_component(
            ref=f"U_BUF{col}",
            lib_fp="DSBGA_Packages:DSBGA-5_NumericPads",
            x=ic_x, y=buf_y, angle=270, layer="F.Cu",
            net_map={"1": f"OE{col}", "2": f"Q{col}", "3": "GND",
                     "4": f"BUF_Y{col}", "5": "VCC"},
            tstamp=uid(),
        )
        pcb.place_component(
            ref=f"D_BUF{col}",
            lib_fp="LED_SMD:LED_0402_1005Metric",
            x=led_x, y=buf_y, angle=90, layer="F.Cu",
            net_map={"1": f"LED_BUF{col}", "2": f"BUF_Y{col}"},
            tstamp=uid(),
        )
        pcb.place_component(
            ref=f"R_BUF{col}",
            lib_fp="Resistor_SMD:R_0402_1005Metric",
            x=led_x, y=buf_y, angle=90, layer="B.Cu",
            net_map={"1": f"LED_BUF{col}", "2": "GND"},
            tstamp=uid(),
        )

    # --- Apply power vias: cardinal L-escape ---
    CARDINAL_NUDGE = 0.30
    DSBGA_CARDINAL = {
        90:  (270, CARDINAL_NUDGE, 90, CARDINAL_NUDGE),
        270: (90, CARDINAL_NUDGE, 270, CARDINAL_NUDGE),
    }

    via_count = 0
    for fp in pcb.board.footprints:
        ref = fp.properties.get("Reference", "")
        lib_id = fp.libId or ""
        fp_x, fp_y = fp.position.X, fp.position.Y
        angle_rad = math.radians(fp.position.angle or 0)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)

        is_dsbga = "DSBGA" in lib_id
        is_led = "LED" in lib_id
        is_resistor = "Resistor" in lib_id

        if is_resistor and fp.layer == "B.Cu":
            continue
        if not (is_dsbga or is_led or is_resistor):
            continue

        fp_angle = round(fp.position.angle or 0)
        use_cardinal = (fp_angle in DSBGA_CARDINAL
                        and (ref.startswith("U_DFF")
                             or ref.startswith("U_BUF")))

        for pad in fp.pads:
            if not (pad.net and pad.net.name in ("GND", "VCC")):
                continue

            net_name = pad.net.name
            net_num = pad.net.number
            px, py = pad.position.X, pad.position.Y
            abs_x = round(fp_x + px * cos_a + py * sin_a, 2)
            abs_y = round(fp_y - px * sin_a + py * cos_a, 2)

            via_layers = (["F.Cu", "B.Cu"] if net_name == "GND"
                          else ["F.Cu", "In2.Cu"])

            if is_dsbga and use_cardinal:
                vcc_a, vcc_n, gnd_a, gnd_n = DSBGA_CARDINAL[fp_angle]
                if net_name == "VCC":
                    esc_angle, nudge = vcc_a, vcc_n
                else:
                    esc_angle, nudge = gnd_a, gnd_n
                pcb.pin_to_via(
                    (abs_x, abs_y), net_num,
                    angle=esc_angle, nudge=nudge,
                    distance=VIA_OFFSET,
                    trace_width=POWER_TRACE_W,
                    via_size=VIA_SIZE, via_drill=VIA_DRILL,
                    via_layers=via_layers,
                )
            elif is_dsbga:
                dx = abs_x - fp_x
                dy = abs_y - fp_y
                dist = math.sqrt(dx * dx + dy * dy)
                if dist > 0.01:
                    raw = math.degrees(math.atan2(dy, dx))
                    escape_angle = round(raw / 45) * 45
                else:
                    escape_angle = 90
                pcb.pin_to_via(
                    (abs_x, abs_y), net_num,
                    angle=escape_angle,
                    distance=VIA_OFFSET,
                    trace_width=POWER_TRACE_W,
                    via_size=VIA_SIZE, via_drill=VIA_DRILL,
                    via_layers=via_layers,
                )
            else:
                pcb.pin_to_via(
                    (abs_x, abs_y), net_num,
                    angle=0,
                    distance=VIA_OFFSET,
                    trace_width=POWER_TRACE_W,
                    via_size=VIA_SIZE, via_drill=VIA_DRILL,
                    via_layers=via_layers,
                )
            via_count += 1

    print(f"  Power vias placed: {via_count}")

    # Board outline
    pcb.set_board_outline(BOARD_W, BOARD_H, ORIGIN_X + 2, ORIGIN_Y + 4)

    # Save PCB
    out_pcb = os.path.join(OUTPUT_DIR, "debug_1byte.kicad_pcb")
    pcb.save(out_pcb)
    print(f"  Saved: {out_pcb}")

    # Run DRC (grouped output with snapshots)
    print("\n--- DRC ---")
    issues, errors, warnings = run_drc(
        out_pcb, OUTPUT_DIR, label="debug_1byte",
        skip_types=SKIP_TYPES, snapshot=True)
    for line in issues:
        print(line)
    print(f"  DRC: {errors} error(s), {warnings} warning(s)")

    # Export full-board PNG
    out_png = os.path.join(OUTPUT_DIR, "debug_1byte.png")
    w, h = snapshot_region(out_pcb, None, out_png)
    print(f"  PNG exported: {out_png} ({w}x{h} px)")

    return 1 if errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
