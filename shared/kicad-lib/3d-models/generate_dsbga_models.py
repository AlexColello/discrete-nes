"""Generate STEP 3D models for DSBGA packages missing from KiCad's library.

Creates:
- Texas_DSBGA-5_0.8875x1.3875mm_Layout2x3_P0.5mm.step
- Texas_DSBGA-6_0.9x1.4mm_Layout2x3_P0.5mm.step

These match the footprint names referenced in the custom DSBGA_Packages.pretty footprints.
"""

import cadquery as cq
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent

# TI YZP (DSBGA) package dimensions from datasheets
# All dimensions in mm

# Common parameters
BALL_DIAMETER = 0.23       # Solder ball diameter (datasheet: 0.21-0.25mm)
BALL_PITCH = 0.5           # Ball-to-ball pitch
BODY_HEIGHT = 0.28         # Substrate + die height
STANDOFF = 0.17            # Ball height / standoff (datasheet: 0.15-0.19mm)
BALL_RADIUS = BALL_DIAMETER / 2

# Total package height ~0.36mm (substrate + die + standoff)
# Z=0 is at PCB surface; body sits above solder balls

PACKAGES = {
    "Texas_DSBGA-5_0.8875x1.3875mm_Layout2x3_P0.5mm": {
        "body_x": 0.8875,
        "body_y": 1.3875,
        # 2x3 grid minus B2 (row B, col 2)
        # Rows: A(y=-0.5), B(y=0), C(y=0.5) — columns: 1(x=-0.25), 2(x=0.25)
        "balls": [
            (-0.25, -0.5),   # A1
            (0.25, -0.5),    # A2
            (-0.25, 0.0),    # B1
            # B2 missing
            (-0.25, 0.5),    # C1
            (0.25, 0.5),     # C2
        ],
    },
    "Texas_DSBGA-6_0.9x1.4mm_Layout2x3_P0.5mm": {
        "body_x": 0.9,
        "body_y": 1.4,
        # Full 2x3 grid
        "balls": [
            (-0.25, -0.5),   # A1
            (0.25, -0.5),    # A2
            (-0.25, 0.0),    # B1
            (0.25, 0.0),     # B2
            (-0.25, 0.5),    # C1
            (0.25, 0.5),     # C2
        ],
    },
}


def make_dsbga_model(body_x, body_y, balls):
    """Create a DSBGA package 3D model.

    The model is centered at (0,0) in XY with Z=0 at the PCB surface.
    Single rectangular body with solder balls underneath.
    """
    z_body_bottom = STANDOFF

    # --- Package body (single rectangle) ---
    body = (
        cq.Workplane("XY")
        .workplane(offset=z_body_bottom)
        .box(body_x, body_y, BODY_HEIGHT, centered=(True, True, False))
    )

    # --- Solder balls ---
    ball_z_center = BALL_RADIUS
    ball_union = None
    for bx, by in balls:
        ball = cq.Workplane("XY").transformed(offset=(bx, by, ball_z_center)).sphere(BALL_RADIUS)
        if ball_union is None:
            ball_union = ball
        else:
            ball_union = ball_union.union(ball)

    result = body
    if ball_union is not None:
        result = result.union(ball_union)

    return result


def main():
    for name, params in PACKAGES.items():
        print(f"Generating {name}...")
        model = make_dsbga_model(
            body_x=params["body_x"],
            body_y=params["body_y"],
            balls=params["balls"],
        )
        out_path = OUTPUT_DIR / f"{name}.step"
        cq.exporters.export(model, str(out_path))
        print(f"  -> {out_path} ({out_path.stat().st_size} bytes)")

    print("Done.")


if __name__ == "__main__":
    main()
