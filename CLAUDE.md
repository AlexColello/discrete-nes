# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Keeping This File Current

**This is the most important task.** Update CLAUDE.md whenever you:

- Discover something unexpected (a KiCad quirk, a kiutils gotcha, a routing rule violation)
- Learn a new user preference or workflow pattern
- Fix a bug whose root cause should be documented so it never recurs
- Find that existing documentation is wrong, incomplete, or misleading

Don't wait — update immediately when the insight is fresh. A lesson not recorded here will be relearned painfully in a future session. This file is the institutional memory for the entire project across all boards (RAM, CPU, PPU, etc.), so every hard-won insight belongs here.

## Project Overview

**Discrete NES** - A discrete logic NES implementation where EVERY gate output and EVERY memory bit has a visible LED indicator. Built with TI Little Logic (SN74LVC1G) in DSBGA packages — the bare silicon die is visible on top of each IC, creating a sea of visible silicon interspersed with glowing LEDs.

**Scale:**

- Estimated 10,000+ LEDs across all boards
- Thousands of single-gate ICs (TI Little Logic SN74LVC1G series, DSBGA)
- Multiple PCBs with SMD assembly (solder paste + hot air reflow)
- Complete cycle-accurate NES implementation in visible discrete logic

## Core Principles

1. **EVERY gate output needs an LED** - This is non-negotiable. Never design circuits without LED indicators on every single gate output and memory bit
2. **Use kiutils for generation** - Schematics and PCB placement are scripted using Python + kiutils library
3. **TI Little Logic (SN74LVC1G) in DSBGA** - Single-gate packages with bare silicon die visible on top. The aesthetic of exposed silicon is a core design goal
4. **0402 SMD LEDs** - Smaller than the DSBGA ICs so bare silicon dies are the visual focus
5. **No git submodules** - External references (MiSTer NES) are documented but not included as submodules
6. **MiSTer NES core is the logic reference** - Not Brian Bennett's fpga_nes. See reference/README.md
7. **NO LED multiplexing** - Defeats the purpose of seeing all states simultaneously

## Repository Structure

```
discrete-nes/
├── shared/
│   ├── kicad-lib/          # Shared KiCad symbols and footprints
│   │   ├── symbols/        # .kicad_sym files
│   │   ├── footprints/     # .pretty directories (incl. generated DSBGA_Packages.pretty)
│   │   ├── sym-lib-table   # Symbol library table (copy to projects)
│   │   └── fp-lib-table    # Footprint library table (copy to projects)
│   └── python/
│       ├── kicad_gen/      # Shared schematic/PCB generation library
│       │   ├── __init__.py # Re-exports: SchematicBuilder, PCBBuilder, snap, uid, etc.
│       │   ├── common.py   # Constants (GRID, KICAD_CLI, SYMBOL_LIB_MAP, FOOTPRINT_MAP), snap(), uid()
│       │   ├── symbols.py  # Library loading, raw text extraction, pin offset discovery
│       │   ├── schematic.py # SchematicBuilder class (place, wire, LED, labels, save)
│       │   ├── verify.py   # parse_schematic, 11 general checks, run_erc, run_drc + DRC grouping
│       │   ├── pcb.py      # PCBBuilder, DSBGA footprints, netlist export/parse, fix_pcb_drc
│       │   └── snapshot.py # PCB snapshot: SVG export, crop, X markers, PNG render (600 DPI)
│       └── hdl_parser/     # Verilog to discrete gates conversion
│           └── verilog_to_gates.py
├── boards/
│   ├── ram-prototype/      # First board — 8-byte RAM prototype
│   │   ├── scripts/
│   │   │   ├── generate_ram.py      # Schematic generation (hierarchical)
│   │   │   ├── generate_pcb.py      # PCB placement + pre-routing
│   │   │   ├── verify_schematics.py # Board-specific schematic checks + ERC
│   │   │   ├── verify_pcb.py        # DRC verification (pre- and post-routing)
│   │   │   ├── route_pcb.py         # FreeRouting autorouter pipeline
│   │   │   ├── debug_1byte.py       # Integration test: 1 byte using generate_pcb functions
│   │   │   ├── snapshot_pcb.py      # CLI for PCB region snapshots
│   │   │   └── parse_pdf.py         # TI datasheet PDF text/pin extraction
│   │   ├── ram.kicad_sch            # Root schematic
│   │   ├── ram.kicad_pcb            # Generated PCB (pre-routing)
│   │   ├── ram_routed.kicad_pcb     # Autorouted PCB
│   │   └── verify_output/           # Generated output (gitignored)
│   ├── cpu-2a03/           # CPU board (future, empty scripts/ dir)
│   ├── ppu-2c02/           # PPU board (future, empty scripts/ dir)
│   └── interconnect/       # Board interconnections (future)
├── tools/
│   └── freerouting/        # FreeRouting JAR (auto-downloaded by route_pcb.py)
├── .claude/
│   ├── hooks/
│   │   └── auto-verify.sh  # PostToolUse hook: auto-runs verify after generate
│   └── settings.json       # Hook configuration
└── reference/              # Documentation (NO submodules)
    ├── README.md           # How to access MiSTer NES core
    └── docs/               # NES architecture documentation
```

## Development Commands

### Python Environment Setup

```bash
# Create virtual environment (first time only)
python -m venv venv

# Install dependencies
source venv/Scripts/activate && pip install -r requirements.txt
```

**CRITICAL: Always activate the venv before running ANY Python script.** Prefix every Python command with `source venv/Scripts/activate &&` (the venv is at the repo root). Shell state doesn't persist between Bash tool calls, so you must source it every time.

### Generate & Verify (mandatory workflow)

Every board has a generate script and a verify script. **Always run both.** This applies to the RAM prototype now and to every future board (CPU, PPU, etc.).

```bash
cd boards/ram-prototype
source ../../venv/Scripts/activate && python scripts/generate_ram.py
source ../../venv/Scripts/activate && python scripts/verify_schematics.py
```

**IMPORTANT:** After ANY change to a generate script, you MUST:

1. Regenerate: `python scripts/generate_*.py`
2. Verify: `python scripts/verify_schematics.py`
3. Fix any errors before considering the change complete
4. Target: **0 errors, 0 warnings**. Both errors and warnings cause verification failure

**Auto-verify hook:** A PostToolUse hook (`.claude/hooks/auto-verify.sh`) automatically runs `verify_schematics.py --no-erc` after any Bash command that executes a `generate_*.py` script. This catches wire overlap / wire-through-pin bugs immediately without needing to remember to run verify manually.

The verification script catches bugs that are invisible in KiCad's GUI and that ERC alone misses — especially wire overlaps (silent net merges) and wire-through-pin (valid but unintended connections). See "Verification Script Architecture" section below for details on what each check does and how to adapt the script for new boards.

Output goes to `verify_output/` (gitignored), including SVGs for visual inspection.

### PCB Routing (FreeRouting autorouter)

Each board has a `route_pcb.py` script that autoroutes using FreeRouting. **Java must be on PATH.**

```bash
cd boards/ram-prototype
# Ensure Java is available (JDK 17+)
export PATH="/c/Program Files/Java/jdk-21.0.10/bin:$PATH"

# Full pipeline: DSN export → FreeRouting → SES import → zone fill → cleanup → verify
python scripts/route_pcb.py

# Options:
python scripts/route_pcb.py --passes 30      # Override max routing passes
python scripts/route_pcb.py --dry-run        # Export DSN only, don't route
python scripts/route_pcb.py --skip-verify    # Skip post-routing verification
```

**Pipeline steps:** Export Specctra DSN → Run FreeRouting CLI → Import SES → Fill zones → Cleanup dangling stubs → Hide footprint text → Post-routing DRC verification.

**Requirements:** Java 17+ on PATH, FreeRouting JAR auto-downloaded to `tools/freerouting/`.

**Post-routing verification:**
```bash
python scripts/verify_pcb.py --post-routing  # Stricter DRC on ram_routed.kicad_pcb
```

### PCB Snapshot & Visual Inspection

`snapshot_pcb.py` exports cropped PNG images of PCB regions for visual inspection. Uses `kicad_gen.snapshot` (shared library).

```bash
cd boards/ram-prototype

# Show board outline coordinates (useful for choosing bbox)
python scripts/snapshot_pcb.py ram.kicad_pcb --outline

# Snapshot a region (PCB mm coordinates)
python scripts/snapshot_pcb.py ram.kicad_pcb --bbox 13,50,80,80 -o region.png

# Full board
python scripts/snapshot_pcb.py ram.kicad_pcb -o full_board.png

# Specific layers only
python scripts/snapshot_pcb.py ram.kicad_pcb --bbox 100,30,180,70 --layers F.Cu,Edge.Cuts
```

All output renders at 600 DPI via PyMuPDF. The shared library (`kicad_gen.snapshot`) also supports programmatic use with `snapshot_region()`, SVG coordinate offset caching (`svg_cache` parameter), and X-marker injection for DRC visualization.

### PDF Parsing (TI Datasheets)

`parse_pdf.py` extracts text and pin-to-ball tables from TI datasheet PDFs. Uses PyMuPDF.

```bash
python scripts/parse_pdf.py datasheet.pdf              # dump all text
python scripts/parse_pdf.py datasheet.pdf --pages 1-3   # specific pages
python scripts/parse_pdf.py datasheet.pdf --search pin   # regex search
python scripts/parse_pdf.py datasheet.pdf --pins         # extract pin-to-ball table
python scripts/parse_pdf.py datasheet.pdf --info         # metadata
```

### DRC Violation Grouping & Snapshots

`run_drc()` in `kicad_gen.verify` groups DRC violations by structural signature — violations that differ only by instance index (byte number, bit number, signal index) are collapsed into one group. This prevents hundreds of identical errors from flooding the output.

**Console output:** One line per group with count, e.g. `ERROR [unconnected_items: ... | Pad 1 [/D*] of U* ...]: 112x`

**Detail files:** Saved to `verify_output/drc_<label>/` with:
- `.txt` — signature, representative examples (with coordinates), full instance list
- `.png` — cropped PCB snapshot of the first violation with red X markers at item positions (when `snapshot=True`)

**Signature generalization rules:**
- Ref designators: `U_DFF3` → `U_DFF*`, `U44` → `U*`, `D22` → `D*`
- Net names: `/Byte 0/Q0` → `/Byte */Q*`, `/DEC4_15` → `/DEC4_*`, `Net-(D22-K)` → `Net-(D*-K)`
- Connector pad numbers generalized (pin index isn't structural)
- Track lengths rounded to 0.1mm
- Power nets (`GND`, `VCC`) kept as-is

## Technology Stack & Research Findings

### PCB Layout Lessons

**Board outline computation — use pad + courtyard extents:**

- NEVER compute board outline from component center positions alone — this underestimates the space needed for large components like connectors (PinHeader_1x16 spans 40mm vertically)
- Compute bounding box from each footprint's pad positions (rotated to absolute coords) and courtyard graphics (`F.CrtYd`/`B.CrtYd`)
- Add BOARD_MARGIN around that bounding box
- The verify check (`check_components_inside_outline`) must also use pad+courtyard extents, not just center positions

**kiutils PCB API gotchas:**

- `Footprint.properties` is a `dict` (key→value strings), NOT a list of Property objects. Use `fp.properties["Reference"]`, not `prop.key`
- `GrLine` uses `width=0.05`, NOT `stroke=Stroke(...)`
- `FillSettings` uses `yes=True, mode=None` for solid fill, NOT `fillType`
- `Board.create_new()` defaults to version 20211014 (KiCad 6). Override to `board.version = 20241229` for KiCad 9
- Footprint courtyard layer is `F.CrtYd` (not `F.Courtyard`) in kiutils layer names
- `Net` is from `kiutils.items.common`, not `kiutils.items.brditems`

**Multi-UUID tstamp in netlist (CRITICAL — breaks GUI file loading):**

- Multi-unit symbols (e.g., 74LVC2G00 dual NAND) produce space-separated UUIDs in the netlist `<tstamps>` field (hierarchy path)
- KiCad's PCB `(tstamp ...)` field expects EXACTLY 1 UUID — multiple UUIDs cause "expecting ')'" parse error when opening in the GUI
- `kicad-cli pcb drc` uses a different parser that tolerates this, so DRC passes even though the GUI can't load the file — verify_pcb.py won't catch this bug!
- Fix: split tstamp on spaces, use only the LAST UUID (component's own) for `fp.tstamp`, full path for `fp.path`
- Implemented in `PCBBuilder.place_component()` in `pcb.py`

**Pad orientation in rotated footprints (CRITICAL for lib_footprint_mismatch DRC):**

- KiCad stores pad orientation as `pad_local_angle + parent_rotation` (historical convention)
- `GetFPRelativeOrientation()` computes `stored_angle - parent_rotation`
- When placing a footprint at angle θ, MUST set each pad's position angle to θ so that `GetFPRelativeOrientation() = θ - θ = 0`, matching the library (which has angle 0 at rotation 0)
- Without this: pad relative orientation = `0 - θ = -θ`, which doesn't match library's `0`, triggering "Pad N orientation differs" DRC warning
- The `place_component()` method in `PCBBuilder` handles this automatically
- For existing KiCad-saved PCBs: `fix_pcb_drc()` post-processes the file to add pad angles
- Possibly related to: https://gitlab.com/kicad/code/kicad/-/issues/21459

**kiutils `remove_unused_layers` bug:**

- KiCad's `(remove_unused_layers no)` is misread by kiutils as boolean `True` (presence = true)
- kiutils then serializes it as bare `(remove_unused_layers)` without the `no` value
- This causes `lib_footprint_mismatch` on through-hole footprints (e.g., PinHeader connectors)
- Fix: `_fix_footprints()` post-processes `(remove_unused_layers)` → `(remove_unused_layers no)`

**Through-vias have copper on ALL layers (CRITICAL for B.Cu routing):**

- Vias with `layers=["F.Cu", "In1.Cu"]` are NOT blind vias in standard fabrication (e.g., Elecrow). The annular ring exists on every layer including B.Cu
- Do NOT assume B.Cu is clear just because power vias target inner layers — they are through-vias with copper on B.Cu
- B.Cu traces will short against or violate clearance with any through-via annular they pass near
- To use B.Cu for signal routing, traces must explicitly avoid all power via positions
- Board minimum via: 0.8mm diameter, 0.4mm drill (Elecrow). Can't use smaller without changing fab

**PCB trace routing style:**

- Use **45-degree angle traces** wherever possible — avoid 90-degree bends
- Route as: horizontal/vertical → 45° diagonal → horizontal/vertical (chamfered L-shape)
- This improves signal integrity and is standard PCB design practice

**KiCad pad position rotation (Y-down coordinate system):**

- KiCad uses **counterclockwise** rotation on screen (positive angle = CCW when viewed in the GUI)
- Because KiCad's Y axis points **down**, the rotation matrix differs from standard Y-up math — the sin terms involving Y are negated
- Correct formula: `abs_x = fp_x + px*cos(θ) + py*sin(θ)`, `abs_y = fp_y - px*sin(θ) + py*cos(θ)`
- Standard Y-up math formula (`abs_x = fp_x + px*cos(θ) - py*sin(θ)`, `abs_y = fp_y + px*sin(θ) + py*cos(θ)`) is WRONG — it gives CW rotation (the opposite direction) and flips pad positions at 90°/270° (invisible at 0°/180° where sin=0)
- Symptom of using wrong formula: traces starting from wrong pad → DRC "shorting_items" errors on rotated components

**Custom DSBGA footprints (pin numbering mismatch):**

- KiCad 74xGxx symbols use numeric pin numbers (1-5) but stock DSBGA footprints use BGA ball names (A1/B1/C1/C2)
- Solution: create custom footprints with numeric pads via `create_dsbga_footprints()` in `pcb.py`
- Pin-to-ball mapping: `{1:A1, 2:B1, 3:A2, 4:C1, 5:C2}` (5-ball), add `6:B2` for 6-ball

**DSBGA pin numbering — verified against TI datasheets (2026-03-12):**

- **DSBGA-5**: KiCad and TI datasheets use the SAME pin numbering: `1=in1, 2=in2, 3=GND, 4=output, 5=VCC`. Ball mapping: `{1:A1, 2:B1, 3:C1, 4:C2, 5:A2}`. Verified for 74LVC1G08 and 74LVC1G79
- **DSBGA-8**: KiCad and TI match: `1=1A, 2=1B, 3=2Y, 4=GND, 5=2A, 6=2B, 7=1Y, 8=VCC`. Note pins 3 and 7 are outputs for gate 2 and gate 1 respectively (not in gate order). Ball mapping: `{1:A1, 2:B1, 3:C1, 4:D1, 5:D2, 6:C2, 7:B2, 8:A2}` — pins go down column 1 (A1→B1→C1→D1) then up column 2 (D2→C2→B2→A2)
- **CRITICAL**: The old DSBGA-8 mapping `{1:A1, 2:A2, 3:B1, 4:B2, ...}` was wrong — it assumed row-first ordering but TI uses column-first zigzag. Always verify against the datasheet bottom-view diagram

**Netlist parsing for hierarchy grouping:**

- Use `kicad-cli sch export netlist --format kicadxml` to get XML netlist
- Extract `<sheetpath names="...">` from each component for hierarchy identification
- Group components by sheetpath to organize placement by functional block

**DRC filtering for pre-routing boards:**

- Before routing, many DRC violations are expected: `unconnected_items`, `lib_footprint_mismatch`, `lib_footprint_issues`, `silk_overlap`, `text_thickness`, `text_height`
- Use `skip_types` parameter in `run_drc()` to filter these
- Target: 0 errors, 0 warnings AFTER filtering
- **NEVER add `silk_over_copper` to skip_types without explicitly asking the user first** — silk_over_copper warnings indicate real layout problems (silkscreen crossing over pads) that need to be fixed, not suppressed

### PCB Generation Architecture (generate_pcb.py)

`generate_pcb.py` is the main PCB generation script. Key functions (importable for debug/test scripts):

**Layout functions:**

- `group_components(netlist_data)` — groups components by hierarchy sheet path
- `sort_components_for_placement(comps)` — sorts ICs, matches LED+R pairs by output net
- `layout_byte_group(comps)` — byte-specific layout: NAND + 8 DFF row + 8 BUF row + NAND LEDs
- `compute_group_layout(ic_cells, standalone, max_cols, cell_w, cell_h)` — generic grid layout
- `_place_component(pcb, comp, x, y, netlist_data)` — places one component with correct angle/layer/nets

**Pre-route functions (all take `pcb, netlist_data`):**

- `preroute_power_vias` — GND/VCC pad escape vias (skips DFF/BUF — too dense, left to autorouter)
- `preroute_bcu_resistors` — LED cathode to B.Cu resistor pad vias
- `preroute_ic_to_led` — IC output pad to LED anode traces
- `preroute_clk_fanout` — horizontal CLK bus traces per byte
- `preroute_oe_fanout` — horizontal OE bus + vertical stubs per byte
- `preroute_connector_leds` — connector signal to bus LED traces

**4-layer stackup:**

| Layer   | Purpose                                      |
|---------|----------------------------------------------|
| F.Cu    | Signal routing (ICs, LEDs on front)           |
| In1.Cu  | Jumper layer (data bus trunks, cross-routing)  |
| In2.Cu  | VCC power plane (zone fill)                   |
| B.Cu    | GND power plane (zone fill, resistors on back) |

**Component orientations:** DFF@90° / BUF@270° (power pins outward, signal pins facing each other). Other DSBGA ICs@180°. LEDs/Rs@90°. Connectors@180° on B.Cu.

**Cell dimensions:** IC_CELL_W=5.0mm horizontal, IC_CELL_H=2.0mm vertical. LED at +2.45mm X from IC. R on B.Cu directly behind LED (same x,y).

**`debug_1byte.py`** imports `layout_byte_group`, `_place_component`, and `preroute_*` functions from `generate_pcb.py` to test them on a single byte from the real netlist. Changes to generate_pcb.py are automatically exercised.

### kiutils + KiCad 9 ERC Lessons

Hard-won requirements for generating schematics with kiutils that pass KiCad 9 ERC. The shared `SchematicBuilder` in `shared/python/kicad_gen/schematic.py` implements all of these; board scripts (e.g., `boards/ram-prototype/scripts/generate_ram.py`) import it.

**Format requirements:**

- Set `sch.version = 20250114` (KiCad 9 format) — the kiutils default (20211014, KiCad 6) causes `wire_dangling` on every wire-to-pin connection
- Set `sch.uuid = uid()` — root UUID is required
- Set `sch.generator = "eeschema"` — matches KiCad native output
- Each `SchematicSymbol` needs pin UUIDs: `sym.pins = {"1": uid(), "2": uid(), ...}` — required for KiCad 9 wire connectivity

**Pin position discovery:**

- NEVER calculate pin positions from library symbol data or manual offsets
- Use ERC-based probing: place one component at (100,100) in a temp schematic, run `kicad-cli sch erc`, parse the JSON to extract pin positions (ERC coordinates × 100 = mm)
- Cache the results — pin offsets are stable per (symbol, angle) combo

**Hierarchical schematics:**

- Child sheet symbols must use the full hierarchical path: `/{root_uuid}/{sheet_block_uuid}` — NOT `/{child_sheet_uuid}/`
- Multi-instance sheets (e.g., byte.kicad_sch used 8×) need one path per instance with unique reference designators per path
- Build all sheets first, then post-process to fix instance paths after the root sheet assigns hierarchy UUIDs
- Don't mix local and global labels with the same name — use local labels in the root sheet for hierarchy pin connections

**Power symbols:**

- PWR_FLAG should be placed at the root sheet connector power pins (VCC + GND) — marks the connector as the power source
- Do NOT place PWR_FLAG in sub-sheets — it propagates through global VCC/GND nets from the root
- Power symbols placed at IC pin positions connect via overlapping pins (no wire needed)

**Wire routing rules (critical for ERC-clean schematics):**

- **Orthogonal wires only** — KiCad doesn't connect diagonal wires. Route as L-shapes (horizontal then vertical)
- **Segmented trunks required** — A single long vertical wire with junctions doesn't reliably connect T-branches in KiCad 9. Split vertical bus/trunk wires into separate segments between each branch point. Use `SchematicBuilder.add_segmented_trunk()` from `kicad_gen`
- **Split wires at LED junction points** — When `place_led_below()` adds a junction on a main wire, the main wire MUST be split into two segments at that junction X coordinate
- **Wire overlap = net merge** — Two wires sharing any segment (same Y, overlapping X range) silently merge their nets. Always verify horizontal wires at the same Y don't overlap
- **T-connections from wire endpoints** — A wire endpoint landing on the middle of another wire creates a connection (even without a junction dot). When routing horizontal branch wires that cross vertical trunks, verify the crossing Y doesn't match any trunk segment endpoint Y
- **Avoid trunk/branch Y coincidence** — If components at the same Y positions feed different vertical trunks, offset one group (e.g., shift inverters by +GRID) so trunk segment endpoints don't share Y values with cross-trunk horizontal wires
- **Wire through pin = unintended connection** — A wire passing through a component pin position (even NC pins) creates a connection in KiCad. Vertical trunks and horizontal branches must avoid ALL pin positions of components they pass near. Use `verify_schematics.py` to detect these
- **NC pin positions matter** — The 74LVC1G04 NC pin (pin 1) has a real connection point at (center_x - 7.62, center_y - 2.54) in schematic space. Vertical trunks at this X will connect to the NC pin. Use half-grid trunk X offsets or different inverter Y offsets to avoid coincidence

**Half-grid Y offset arithmetic (important for fan-out routing):**

When routing N signals from a connector to evenly-spaced fan-out Y positions, the fan-out Y values must be at **half-grid** (odd multiples of 1.27mm) so they never coincide with on-grid connector pin Y values. The naive `+ GRID/2` offset is fragile — whether it produces half-grid depends on the parity of the fan spacing:

- With **odd** multiplier spacing (e.g., 5\*GRID): `fan_span/2` is half-grid, so `center - span/2` is on-grid, and `+ GRID/2` correctly shifts to half-grid
- With **even** multiplier spacing (e.g., 6\*GRID): `fan_span/2` is on-grid, so `center - span/2` is already half-grid, and `+ GRID/2` **incorrectly** shifts back to on-grid

**Robust pattern** — compute raw, then dynamically ensure half-grid and page bounds:

```python
fan_start_y = snap(conn_pin_mid_y - fan_span / 2)
grid_units = fan_start_y / GRID
if abs(grid_units - round(grid_units)) < 0.01:  # on-grid?
    fan_start_y = snap(fan_start_y + GRID / 2)   # nudge to half-grid
while fan_start_y < page_min_y:                   # page border clamp
    fan_start_y = snap(fan_start_y + GRID)        # preserves half-grid
```

Adding whole `GRID` increments preserves the half-grid property (GRID = 2 half-grids).

**Why this matters:** A 14-signal fan-out at 6\*GRID spacing spans ~198mm, much larger than the connector pin range (~38mm). The fan-out Y values will sweep through the connector Y range ~5 times. If they're on-grid, overlaps with connector horizontal wires are inevitable. If half-grid, they're structurally impossible.

### Readable Schematic Layout Design

Lessons learned from the RAM prototype root sheet about designing hierarchy sheets that are clear and readable when opened in KiCad.

**Left-to-right signal flow:**

- All hierarchy sheet blocks should have inputs on the LEFT edge and outputs on the RIGHT edge
- `_add_sheet_block()` accepts a `right_pins` set — pins in this set appear on the RIGHT, all others on LEFT
- Left and right pins must be indexed separately (separate counters for Y positioning) so a block with 3 left pins and 8 right pins doesn't waste space
- This creates a clear left-to-right signal flow: connector → control blocks → byte sheets

**Multi-column layout for control blocks:**

- Group related blocks into columns: decoder + control logic in column 1, write_clk + read_oe in column 2
- Align column tops so input-output pairs are at similar Y positions (e.g., Address Decoder SEL outputs face Write Clk Gen SEL inputs)
- Leave an inter-column gap (15\*GRID) for vertical trunk wires between columns

**When to use labels vs direct wires:**

- **Prefer direct wires** — make a best effort to connect everything with wires, even if perpendicular wires must cross each other. Wire crossings (perpendicular) are fine in KiCad; only parallel wire overlaps cause problems
- **Labels should be restricted** to cases where direct wires are impractical:
  - High-fanout signals (D0-D7: connector + 8 byte sheets = 9 connections each)
  - Signals with systematic Y-coincidence at the destination (BUF_OE: source Y matches byte D-signal label stub Y)
- **Direct-wire "approach column" pattern** — for connector-to-block signals (A0-A2, nCE/nOE/nWE), route from the connector LED fan-out through "approach turning columns" near the destination block. Each signal gets a unique approach column X (staggered left of col1_x). Wire structure: fan-out horizontal → approach column vertical → destination pin horizontal. Signals are ordered so that increasing fan-out Y gets approach columns closer to the target, preventing vertical wire crossings
- **Y-coincidence hazard**: two horizontal wires at the same Y with overlapping X ranges silently merge nets. This is the main reason to fall back to labels when direct wires don't work

**Vertical trunk routing between columns:**

- Use `add_segmented_trunk()` for multi-destination signals (SEL0-7 fan out to both write_clk and read_oe)
- Stagger trunk X positions: `sel_trunk_x[i] = base_x - i * GRID` so 8 parallel trunks don't overlap
- Place single-destination trunks (WRITE_ACTIVE, READ_EN) at X positions BETWEEN the multi-destination trunks and the target column (not on the source side) to avoid horizontal overlap at shared Y values

**Connector design:**

- Include VCC and GND pins on the connector (e.g., 16-pin connector: 14 signals + VCC + GND)
- VCC at the top of the connector (highest pin number at angle=180), GND at the bottom (pin 1 at angle=180)
- Signal pins in the middle, grouped logically (address, data, control)
- Wire power pins to VCC/GND symbols with a short horizontal stub (3\*GRID)

**Connector + LED bank layout:**

- Connector and LED bank are vertically centered with the sheet block ensemble (pre-compute `ensemble_center_y` before placing the connector)
- Each connector pin is wired directly to its LED indicator via staggered turning columns
- Sort signals by connector Y (ascending) so vertical routing wires don't cross
- **Inverted-V (Λ) turning column assignment:** assign turn-column X based on distance from center — edge signals (top/bottom of connector) get innermost columns (closest to connector), center signals get outermost. Sort by `(-abs(i - center), i)`. This creates zero perpendicular crossings because within each half, ranks increase/decrease monotonically with index, so no horizontal stub can reach another signal's vertical. Never use monotonic (diagonal staircase) or non-flipped V (many crossings)
- Use half-GRID (1.27mm) turn-column spacing to keep LED indicators well left of col1_x
- Fan-out Y must be at half-grid positions — see "Half-grid Y offset arithmetic" above
- Group connector pins by destination: signals that direct-wire to nearby blocks (A0-A2, nCE/nOE/nWE) at the top (lower pin Y), high-fanout label signals (D0-D7) at the bottom. This puts direct-wire signals first in the fan-out order, matching their block positions
- LED spacing: 6\*GRID (15.24mm) between fan-out Y positions gives 5.08mm clearance above each LED chain (drop=2\*GRID occupies ~10.16mm)
- Direct-wired signals continue past the LED junction to approach columns; label signals continue to labels. Both share the same fan-out infrastructure
- Each LED indicator is a horizontal chain: R_Small(90°) → LED_Small(180°) → GND

**kiutils API notes:**

- Wire objects use `item.points[0]` and `item.points[1]` (Position objects) — NOT `startPoint`/`endPoint`
- Check `item.type == 'wire'` to distinguish wires from other graphical items
- Library sub-symbols have pins: iterate `lib_sym.symbols` then `sub_sym.pins`
- Pin library coordinates use Y-up; apply Y negation + rotation for schematic space

**Known limitations:**

- Embedded lib_symbols are post-processed to match library files exactly (kiutils drops `exclude_from_sim`, property/pin `hide` flags) — see `SchematicBuilder._fix_lib_symbols()` in `shared/python/kicad_gen/schematic.py`
- Use `round(v, 2)` on all coordinates to eliminate floating-point noise (e.g., `83.82000000000001`)

### Verification Script Architecture

Verification has two layers: **shared general-purpose checks** in `shared/python/kicad_gen/verify.py` and **board-specific scripts** (e.g., `boards/ram-prototype/scripts/verify_schematics.py`). The shared module is the reusable engine; board scripts import it and add board-specific netlist checks.

**General-purpose checks (in `kicad_gen.verify`, reusable for any board):**

1. **Diagonal wires** — KiCad doesn't connect them; all routing must be orthogonal
2. **Wire overlaps (NET MERGE)** — same-axis wires with overlapping ranges silently merge nets. This is the #1 cause of hard-to-debug ERC failures
3. **Dangling wire endpoints** — endpoints not touching any pin, wire, junction, or label
4. **Wire through pin** — wire interior passing through a component pin (unintended connection)
5. **Wire through body** — wire passing through a component's bounding box
6. **T-junctions without dots** — warning only, but looks wrong in the GUI
7. **Wire overlaps pin stub** — wire doubling a pin's built-in stub line
8. **Component overlap** — non-power parts placed on top of each other
9. **Content on sheet blocks** — wires/components inside hierarchy sheet block areas
10. **Page boundary** — content outside the drawing border
11. **Power orientation** — power symbols facing wrong direction

- **ERC via kicad-cli** — `run_erc()` handles full hierarchy + per-sub-sheet standalone (filtering expected standalone artifacts via `_is_standalone_artifact()`)
- **`run_all_checks(filepath, data)`** — convenience function that runs all 11 checks and returns `[(category, issues, is_error), ...]`
- **`UnionFind`** class — exported for board-specific netlist connectivity checks

**Board-specific checks (must be customized per board):**

- **Netlist connectivity** — uses `UnionFind` from `kicad_gen.verify` to build nets from wire/label/junction connectivity, then verifies expected connections (e.g., "Address Decoder SEL0 connected to Write Clk Gen SEL0") and signal isolation (e.g., "A0 not merged with A1"). The expected-connections list is the main board-specific part
- **SCHEMATIC_FILES list** — which .kicad_sch files to check

**Process for new boards:**

1. Create a new `verify_schematics.py` that imports from `kicad_gen.verify` (see `boards/ram-prototype/scripts/verify_schematics.py` as a template — it's only ~340 lines thanks to shared imports)
2. Define `SCHEMATIC_FILES` for the new board's sheets
3. Write `check_netlist()` with the new board's expected connections and isolation pairs
4. Call `run_all_checks(filepath, data)` for general checks — they work on any .kicad_sch file
5. Call `run_erc()` for kicad-cli ERC

**What the script catches that ERC alone misses:**

- Wire overlaps (KiCad silently merges nets — no ERC error, just wrong connectivity)
- Wire-through-pin (creates a valid but unintended connection — no ERC error)
- Page boundary violations (cosmetic but important for printability)
- Net isolation (ERC checks that pins are driven, but doesn't know which signals should be separate)

### FPGA Reference - MiSTer NES Core

**Primary Reference:** https://github.com/MiSTer-devel/NES_MiSTer

- Based on FPGANES by Ludvig Strigeus
- Verilog/SystemVerilog
- Most complete, production-quality, cycle-accurate
- **User specifically chose this over Brian Bennett's fpga_nes**

**Access Method:**

- NOT included as git submodule (per user request)
- Manual clone/download when needed
- See reference/README.md for instructions

**Alternative References:**

- Brian Bennett's fpga_nes: https://github.com/brianbennett/fpga_nes
- UCR NES_FPGA: https://github.com/UCR-CS179-SUMMER2014/NES_FPGA
- Visual 6502: http://www.visual6502.org/ (transistor-level)

## RAM Prototype Specifications

**First board to implement — validates DSBGA assembly, LED approach, and aesthetic.**

**8 bytes of storage with full sub-decoder trees (11-bit address) for latency testing.**

**Configuration:**

- **8 bytes total capacity** (8-bit data bus D0-D7)
- **11 address bits** — 7-bit row (A0-A6) + 4-bit column (A7-A10)
- **Row decoder:** 3-to-8 sub-decoder (A0-A2) + 4-to-16 sub-decoder (A3-A6) + 4 final cross-product ANDs
- **Column decoder:** 4-to-16 (A7-A10), 2 outputs used, 14 to test header
- **Control signals:** nCE, nWE, nOE (active-low, inverted in control_logic sub-sheet)
- **Unused decoder outputs:** DEC3_4..7 → J2 (4-pin header), DEC4_1..15 → J4 (16-pin header), COL_SEL unused → J3 (14-pin header)

**Per byte (8 bits):**

- 8x SN74LVC1G79 (D flip-flop, DSBGA) — stores 8 bits
- 8x SN74LVC1G125 (tri-state buffer, DSBGA) — read gating
- 1x SN74LVC2G00 (dual NAND, DSBGA-8) — write clock + read OE per byte
- 10x 0402 LED + 10x 0402 resistor (every gate output visible)

**Address decoder (47 ICs):**

- 7x SN74LVC1G04 (inverter) — complemented address bits (A0-A6)
- 40x SN74LVC1G08 (2-input AND) — L1+L2 decode stages for 3-to-8 and 4-to-16, plus 4 final cross-product ANDs

**Column select (28 ICs):**

- 4x SN74LVC1G04 (inverter) + 24x SN74LVC1G08 (2-input AND) — 4-to-16 column decoder

**Control logic (6 ICs):** 3x INV (nCE/nWE/nOE inversion) + 3x AND (WRITE_ACTIVE, READ_EN generation)

**Row control (8 ICs):** 2x SN74LVC1G08 per row × 4 rows — ROW_SEL AND write/read enables

**Totals (actual from generate_ram.py + generate_pcb.py):**

- 225 ICs (14 INV + 75 AND + 8 dual NAND + 64 DFF + 64 BUF)
- 191 LEDs (0402 SMD)
- 191 resistors (0402 SMD)
- 4 connectors (J1=24-pin main, J2=4-pin DEC3, J3=14-pin COL_SEL, J4=16-pin DEC4)
- **611 total BOM parts**

**PCB:** 4-layer, 233 x 95 mm (F.Cu signal, In1.Cu jumper, In2.Cu VCC plane, B.Cu GND plane)

**Power Budget (RAM Prototype):**

- 191 LEDs x 2mA = 0.38A at 3.3V = 1.26W for LEDs
- Logic power negligible (SN74LVC1G ~10uA per IC)
- Manageable power budget at this scale

## Full System Power Requirements

**Plan power distribution from the start:**

- **RAM board (8-byte):** ~0.38A at 3.3V
- **RAM board (64-byte, future):** ~1.5A at 3.3V
- **CPU board:** ~6A at 3.3V (5000+ LEDs)
- **PPU board:** ~4A at 3.3V (3000+ LEDs)
- **Total system estimate:** 12A+ at 3.3V = ~40W for LEDs

**Design implications:**

- 3.3V supply (lower than 5V, but LVC works great at 3.3V)
- Distributed regulation across boards
- Adequate power traces for SMD board
- Multiple connector pins dedicated to power
- DO NOT use LED multiplexing (defeats visibility purpose)

## Implementation Plan - Phase by Phase

### Phase 1: Project Setup (COMPLETED)

- [x] Directory structure created
- [x] Python utilities framework (common.py, schematic.py, pcb.py, verilog_to_gates.py)
- [x] Reference documentation (reference/README.md)
- [x] Shared KiCad library structure (sym-lib-table, fp-lib-table)
- [x] Main README.md with project overview
- [x] requirements.txt with kiutils dependency
- [x] Migration from 74HC DIP to TI Little Logic DSBGA

### Phase 2: RAM Prototype Board (IN PROGRESS)

**Step 1: Manual Circuit Design (COMPLETED)**

1. ~~Open KiCad and create `boards/ram-prototype/ram.kicad_pro`~~
2. ~~Design ONE memory cell manually~~
3. ~~Validate the circuit works~~

**Step 2: Script Development (COMPLETED)**

1. Created `boards/ram-prototype/scripts/generate_ram.py`
2. Generates ERC-clean hierarchical schematics with direct wire routing:
   - ram.kicad_sch (root) — 24-pin connector, bus LEDs, hierarchy refs to all sub-sheets
   - address_decoder.kicad_sch — 7 inverters + 75 ANDs (3-to-8 + 4-to-16 sub-decoders + 4 final)
   - column_select.kicad_sch — 4 inverters + 24 ANDs (4-to-16 column decoder)
   - control_logic.kicad_sch — active-low inversion (nCE/nWE/nOE → WRITE_ACTIVE, READ_EN)
   - row_control.kicad_sch — dual NAND per byte for write clock + read OE (used 4x)
   - byte.kicad_sch — 8 DFFs + 8 tri-state buffers (used 8x)
3. Passes KiCad 9 ERC with 0 errors and 0 warnings

**Step 3: PCB Layout (IN PROGRESS)**

1. `generate_pcb.py` places all 611 components in grouped layout with pre-routing
2. FreeRouting autorouter handles remaining traces (`route_pcb.py`)
3. `debug_1byte.py` tests placement/preroute functions on a single byte
4. DRC: 0 errors / 0 warnings (pre-routing, after expected-type filtering)

**Step 4: Validation**

1. Run DRC/ERC in KiCad
2. Review power distribution
3. Cost estimation
4. Iterate design if needed

**Step 5: Fabrication**

1. Generate gerbers
2. Generate BOM
3. Order PCBs + solder paste stencil
4. Order components (DSBGA ICs, 0402 LEDs, 0402 resistors)
5. Reflow assembly and test

### Phase 3: Shared Library Development (COMPLETED)

**Python generation library (`shared/python/kicad_gen/`):**

- [x] `common.py` — constants (GRID, KICAD_CLI, SYMBOL_LIB_MAP, FOOTPRINT_MAP), snap(), uid()
- [x] `symbols.py` — library loading, raw text extraction, ERC-based pin offset discovery, caching
- [x] `schematic.py` — `SchematicBuilder` class (place, wire, LED, labels, trunks, power, save, lib fixup)
- [x] `verify.py` — parse_schematic, 11 general checks, run_all_checks, run_erc, run_drc + DRC grouping, UnionFind
- [x] `pcb.py` — PCBBuilder, DSBGA footprint generation, netlist export/parse, fix_pcb_drc
- [x] `snapshot.py` — PCB SVG export, crop, X-marker injection, PNG rendering (600 DPI)
- [x] `__init__.py` — re-exports key public API

**KiCad symbols:** All SN74LVC1G parts use KiCad's built-in `74xx_little_logic` symbol library. No custom symbols needed.

**Custom footprints:** DSBGA-5/6/8 with numeric pads (matching KiCad symbol pin numbers) are generated programmatically by `create_dsbga_footprints()` in `pcb.py`. Standard 0402 LED/resistor and connector footprints come from KiCad's built-in libraries.

### Phase 4: FPGA Logic Extraction (Research Phase)

**Goal:** Convert MiSTer Verilog to discrete SN74LVC1G netlists

**Challenges:**

1. Verilog is behavioral, need gate-level synthesis
2. Must identify ALL internal signals for LED placement
3. Map Verilog primitives to SN74LVC1G parts
4. Generate component lists with LED indicators

**Approach:**

1. Study MiSTer NES Verilog structure
2. Use synthesis tools to generate gate-level netlist
3. Parse netlist and map gates:
   - `and` -> SN74LVC1G08 (single 2-input AND)
   - `or` -> SN74LVC1G32 (single 2-input OR)
   - `not` -> SN74LVC1G04 (single inverter)
   - `nand` -> SN74LVC1G00 (single 2-input NAND)
   - `nor` -> SN74LVC1G02 (single 2-input NOR)
   - `xor` -> SN74LVC1G86 (single 2-input XOR)
4. Auto-generate KiCad schematics with kiutils
5. Add LED to EVERY gate output

**Key difference from 74HC:** 1 gate per IC, no gate packing/sharing needed.

**Tool Development:**

- Enhance `shared/python/hdl_parser/verilog_to_gates.py`
- May need external synthesis tool (Yosys?)
- Create `tools/hdl_to_schematic/` converter

### Phase 5: CPU and PPU Boards (Future)

After RAM prototype success:

1. Apply lessons learned (DSBGA reflow, LED density, power)
2. Scale up to CPU board (~5000 LEDs, ~5000 ICs)
3. Develop PPU board (~3000 LEDs, ~3000 ICs)
4. Design backplane or cable interconnect system
5. Integration testing

## TI Little Logic Parts Reference

**Logic Gates (1 gate per DSBGA package, except dual NAND):**

- SN74LVC1G00 - Single 2-input NAND (YZP, 5-ball)
- SN74LVC1G02 - Single 2-input NOR (YZP, 5-ball)
- SN74LVC1G04 - Single Inverter (YZP, 5-ball)
- SN74LVC1G08 - Single 2-input AND (YZP, 5-ball)
- SN74LVC1G11 - Single 3-input AND (YZP, 6-ball)
- SN74LVC1G32 - Single 2-input OR (YZP, 5-ball)
- SN74LVC1G86 - Single 2-input XOR (YZP, 5-ball)
- SN74LVC2G00 - Dual 2-input NAND (YZP, 8-ball DSBGA) — used for per-byte write CLK + read OE

**Flip-Flops:**

- SN74LVC1G79 - Single D flip-flop, Q only (YZP, 5-ball DSBGA) — for RAM cells
- SN74LVC1G74 - Single D flip-flop, Q/Q-bar/preset/clear (DQE, 8-pin X2SON — NOT DSBGA)

**Buffers/Drivers:**

- SN74LVC1G07 - Single buffer, open drain (YZP, 5-ball) — good for LED drive
- SN74LVC1G125 - Single tri-state buffer (YZP, 5-ball)

**Package Key:**

- YZP = DSBGA (NanoFree), bare silicon die visible, 1.75 x 1.25mm
- DQE = X2SON, 1.4 x 1.4mm, 8-pin (NOT bare die — plastic)

## Resources & References

**External References:**

- MiSTer NES Core: https://github.com/MiSTer-devel/NES_MiSTer
- OpenTendo (NES reproduction): https://github.com/Redherring32/OpenTendo
- NES Dev Wiki: https://www.nesdev.org/wiki/
- Visual 6502: http://www.visual6502.org/
- Ben Eater's Projects: https://eater.net/

**Research Sources:**

- kiutils docs: https://kiutils.readthedocs.io/
- KiCad developer docs: https://dev-docs.kicad.org/
- TI Little Logic selection guide: https://www.ti.com/logic-circuit/little-logic/overview.html

## Current Status

**Phase 1 COMPLETE** — project setup, migration to TI Little Logic DSBGA
**Phase 2 Steps 1-2 COMPLETE** — schematic generation, 0 ERC errors/warnings
**Phase 2 Step 3 IN PROGRESS** — PCB: 611 components placed, pre-routing active, FreeRouting autorouter working, 233x95mm 4-layer board
**Phase 3 COMPLETE** — shared kicad_gen library (SchematicBuilder, PCBBuilder, verify, snapshot)

## Important Notes

- SN74LVC1G74 (D flip-flop with set/reset) is NOT available in DSBGA — only X2SON (DQE, plastic). SN74LVC1G79 (Q only) IS in DSBGA — works for RAM cells
