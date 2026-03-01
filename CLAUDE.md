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
│   │   ├── footprints/     # .pretty directories
│   │   ├── sym-lib-table   # Symbol library table (copy to projects)
│   │   └── fp-lib-table    # Footprint library table (copy to projects)
│   └── python/
│       ├── kicad_gen/      # Shared schematic/PCB generation library
│       │   ├── __init__.py # Re-exports: SchematicBuilder, snap, uid, GRID, etc.
│       │   ├── common.py   # Constants (GRID, KICAD_CLI, SYMBOL_LIB_MAP), snap(), uid()
│       │   ├── symbols.py  # Library loading, raw text extraction, pin offset discovery
│       │   ├── schematic.py # SchematicBuilder class (place, wire, LED, labels, save)
│       │   ├── verify.py   # parse_schematic, 11 general checks, run_erc, UnionFind
│       │   └── pcb.py      # PCBBuilder, DSBGA footprints, netlist export/parse
│       └── hdl_parser/     # Verilog to discrete gates conversion
│           └── verilog_to_gates.py
├── boards/
│   ├── ram-prototype/      # First board - 8 byte RAM prototype
│   │   ├── scripts/        # Board-specific generate + verify scripts
│   │   └── docs/           # Board-specific documentation
│   ├── cpu-2a03/           # CPU board (future)
│   ├── ppu-2c02/           # PPU board (future)
│   └── interconnect/       # Board interconnections (future)
├── .claude/
│   ├── hooks/
│   │   └── auto-verify.sh  # PostToolUse hook: auto-runs verify after generate
│   └── settings.json       # Hook configuration
├── reference/              # Documentation (NO submodules)
│   ├── README.md           # How to access MiSTer NES core
│   └── docs/               # NES architecture documentation
└── tools/                  # Development tools
    └── hdl_to_schematic/   # Verilog-to-schematic converters
```

## Development Commands

### Python Environment Setup

```bash
# Create virtual environment (first time only)
python -m venv venv

# Activate virtual environment
source venv/bin/activate      # Linux/Mac
venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt
```

### Generate & Verify (mandatory workflow)

Every board has a generate script and a verify script. **Always run both.** This applies to the RAM prototype now and to every future board (CPU, PPU, etc.).

```bash
cd boards/ram-prototype
python scripts/generate_ram.py
python scripts/verify_schematics.py
```

**IMPORTANT:** After ANY change to a generate script, you MUST:
1. Regenerate: `python scripts/generate_*.py`
2. Verify: `python scripts/verify_schematics.py`
3. Fix any errors before considering the change complete
4. Target: **0 errors, 0 warnings**. Both errors and warnings cause verification failure

**Auto-verify hook:** A PostToolUse hook (`.claude/hooks/auto-verify.sh`) automatically runs `verify_schematics.py --no-erc` after any Bash command that executes a `generate_*.py` script. This catches wire overlap / wire-through-pin bugs immediately without needing to remember to run verify manually.

The verification script catches bugs that are invisible in KiCad's GUI and that ERC alone misses — especially wire overlaps (silent net merges) and wire-through-pin (valid but unintended connections). See "Verification Script Architecture" section below for details on what each check does and how to adapt the script for new boards.

Output goes to `verify_output/` (gitignored), including SVGs for visual inspection.

## Technology Stack & Research Findings

### KiCad Scripting - Why kiutils?

**Official KiCad Status (2025):**
- KiCad provides Python bindings ONLY for PCB layout (pcbnew module)
- NO official API for schematic manipulation (eeschema)
- Expected to change in KiCad 9, but not available yet

**Why kiutils over alternatives:**
1. **kiutils** - CHOSEN
   - Directly parses/generates KiCad S-expression files
   - Works for both schematics (.kicad_sch) and PCBs (.kicad_pcb)
   - Python dataclass-based API
   - Install: `pip install kiutils`
   - Docs: https://pypi.org/project/kiutils/

2. SKiDL (NOT used)
   - High-level circuit description language
   - Generates netlists, not direct schematics
   - Would require manual schematic creation

3. pcbnew API (avoided per user request)
   - Official KiCad PCB API
   - More mature but user prefers kiutils
   - Can use as fallback if kiutils routing is too tedious

**PCB Layout Strategy:**
- Use kiutils for component placement in grid patterns
- Manual routing initially (better control for prototype)
- Can revisit automation after validating approach

### PCB Layout Preferences & Lessons

**User's preferred RAM board layout (connector → decode → RAM → control):**
```
+------+-----------+-----------+-----------+
|      | ADDR DEC  | BYTE 0    | BYTE 4    |
|      |           | BYTE 1    | BYTE 5    |
| CONN +-----------+ BYTE 2    | BYTE 6    |
|      | CTRL LOGIC| BYTE 3    | BYTE 7    |
+------+-----------+-----------+-----------+
                   | WRITE CLK | READ OE   |
                   +-----------+-----------+
```
- **Connector** on the far left
- **Address decoder + control logic** stacked vertically to the right of the connector
- **RAM bytes** in a grid to the right, organized by bytes in column-major order (down first, then right)
- **Write/Read control logic** below the RAM
- Each byte is a **line of 8 bits** (8 DFFs in one row, 8 buffers below)

**Connector bus indicator LEDs:**
- Each bus indicator R+LED pair must be positioned at the exact Y of its matching connector pin (1:1 mapping)
- Match R to connector pin via shared signal net (exclude GND/VCC from net matching — LEDs have GND which matches pin 1)
- R at fixed X offset right of connector, LED at slightly further X offset

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

**PCB trace routing style:**
- Use **45-degree angle traces** wherever possible — avoid 90-degree bends
- Route as: horizontal/vertical → 45° diagonal → horizontal/vertical (chamfered L-shape)
- This improves signal integrity and is standard PCB design practice

**KiCad pad position rotation (CRITICAL — DO NOT use standard math rotation):**
- KiCad uses **clockwise** rotation (positive angle = clockwise in Y-down screen coords)
- Correct formula: `abs_x = fp_x + px*cos(θ) + py*sin(θ)`, `abs_y = fp_y - px*sin(θ) + py*cos(θ)`
- Standard math CCW formula (`abs_x = fp_x + px*cos(θ) - py*sin(θ)`, `abs_y = fp_y + px*sin(θ) + py*cos(θ)`) is WRONG
- The difference is invisible at 0° and 180° (sin=0) but flips pad positions at 90°/270° (sin=±1)
- This means the bug only manifests for R_Small components (placed at 90°), not ICs (0°) or LEDs (180°)
- Symptom: traces starting from wrong pad → DRC "shorting_items" errors on every R→LED connection

**Custom DSBGA footprints (pin numbering mismatch):**
- KiCad 74xGxx symbols use numeric pin numbers (1-5) but stock DSBGA footprints use BGA ball names (A1/B1/C1/C2)
- Solution: create custom footprints with numeric pads via `create_dsbga_footprints()` in `pcb.py`
- Pin-to-ball mapping: `{1:A1, 2:B1, 3:A2, 4:C1, 5:C2}` (5-ball), add `6:B2` for 6-ball

**Netlist parsing for hierarchy grouping:**
- Use `kicad-cli sch export netlist --format kicadxml` to get XML netlist
- Extract `<sheetpath names="...">` from each component for hierarchy identification
- Group components by sheetpath to organize placement by functional block

**DRC filtering for pre-routing boards:**
- Before routing, many DRC violations are expected: `unconnected_items`, `lib_footprint_mismatch`, `lib_footprint_issues`, `silk_over_copper`, `silk_overlap`, `text_thickness`, `text_height`
- Use `skip_types` parameter in `run_drc()` to filter these
- Target: 0 errors, 0 warnings AFTER filtering

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
- Leave an inter-column gap (15*GRID) for vertical trunk wires between columns

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

**PWR_FLAG placement:**
- Place PWR_FLAG at the root sheet connector power pins (VCC + GND) — the connector is the power source for the entire design
- Do NOT place PWR_FLAG in sub-sheets — the connector-level PWR_FLAG propagates through the hierarchy via global VCC/GND nets
- Having PWR_FLAG in both root and sub-sheets causes "power output to power output" ERC errors

**Connector design:**
- Include VCC and GND pins on the connector (e.g., 16-pin connector: 14 signals + VCC + GND)
- VCC at the top of the connector (highest pin number at angle=180), GND at the bottom (pin 1 at angle=180)
- Signal pins in the middle, grouped logically (address, data, control)
- Wire power pins to VCC/GND symbols with a short horizontal stub (3*GRID)

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

### Logic Family - Why TI Little Logic (SN74LVC1G) in DSBGA?

**Design Choice: Bare silicon die visibility**

The DSBGA (Die-Size Ball Grid Array) package, also called NanoFree, exposes the bare silicon die on top. Each IC is a tiny (1.75 x 1.25mm) chip with the actual silicon wafer pattern visible. Combined with 0402 SMD LEDs, this creates a striking visual — thousands of bare silicon dies with LEDs glowing between them.

**SN74LVC1G Key Specs:**
- Supply: 1.65V - 5.5V (using 3.3V for this project)
- Output drive: up to 24mA (IOH/IOL) — can drive LEDs directly
- Propagation delay: ~3.5ns typical
- Quiescent current: ~10uA per IC
- Package: DSBGA (YZP) 1.75 x 1.25mm, 5 solder balls underneath
- 1 gate per package — simplifies schematic (no gate sharing)

**Assembly:** Solder paste stencil + hot air reflow (user has hot air station).

**Why not 74HC DIP?**
- 74HC DIP packages are ~20mm wide — too large, boring plastic rectangles
- DSBGA packages show the actual silicon — much more visually interesting
- Single-gate-per-package eliminates complex gate allocation logic
- LVC is faster than HC (~3.5ns vs ~14ns)

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

**First board to implement - validates DSBGA assembly, LED approach, and aesthetic**

**Reduced scope: 8 bytes (3 address bits) — validate before scaling to 64 bytes**

**Configuration:**
- **8 bytes total capacity**
- **3 address bits** (A0-A2) = 8 addressable locations
- **8-bit data bus** (D0-D7)
- **Control signals:** Read/Write enable, Chip Select

**Per byte (8 bits):**
- 8x SN74LVC1G79 (D flip-flop, DSBGA) — stores 8 bits
- 8x SN74LVC1G125 (tri-state buffer, DSBGA) — read gating
- 1x SN74LVC1G08 (AND, DSBGA) — write clock = decoded_address AND write_enable
- 1x SN74LVC1G08 (AND, DSBGA) — read OE = decoded_address AND read_enable
- 10x 0402 LED + 10x 0402 resistor (every gate output visible)

**Address decoder (3-to-8):**
- 3x SN74LVC1G04 (inverter) — complemented address bits
- 8x 3-input AND via 2-gate chains (16x SN74LVC1G08) or 8x SN74LVC1G11
- ~19 decoder ICs + ~19 LEDs + ~19 resistors

**LED Requirements (EVERY bit visible):**
- **64 LEDs** for RAM cell outputs (one per stored bit)
- **3 LEDs** for address bus
- **8 LEDs** for data bus
- **LEDs for all control signals and intermediate gate outputs**
- **Total estimate:** ~115 LEDs

**Totals for 8-byte prototype (actual from generate_ram.py):**
- 161 ICs (DSBGA)
- 175 LEDs (0402 SMD)
- 175 resistors (0402 SMD)
- 1 connector
- **512 total BOM parts**

**PCB size estimate:** ~60x80mm with 3-4mm pitch

**Power Budget (RAM Prototype):**
- 115 LEDs x 2mA = 0.23A at 3.3V = 0.76W for LEDs
- Logic power negligible (SN74LVC1G ~10uA per IC)
- Very manageable power budget at this scale

## Full System Power Requirements

**Plan power distribution from the start:**

- **RAM board (8-byte):** ~0.25A at 3.3V
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
   - ram.kicad_sch (root) — connector, bus LEDs, hierarchy refs
   - address_decoder.kicad_sch — 3 inverters + 8 three-input ANDs
   - control_logic.kicad_sch — active-low inversion + WRITE_ACTIVE, READ_EN
   - write_clk_gen.kicad_sch — 8 NANDs for per-byte write clocks
   - read_oe_gen.kicad_sch — 8 NANDs for per-byte buffer OE
   - byte.kicad_sch — 8 DFFs + 8 tri-state buffers (shared by all 8 instances)
3. Passes KiCad 9 ERC with 0 errors and 0 warnings

**Step 3: PCB Layout**
1. Use kiutils to place DSBGA components in grid pattern (3-4mm pitch)
2. Place all ~115 LEDs in organized arrays alongside ICs
3. Manual routing (start with power/ground)
4. SMD assembly: solder paste stencil + hot air reflow

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

### Phase 3: Shared Library Development (Parallel to Phase 2)

**Python generation library (`shared/python/kicad_gen/`) — COMPLETED:**
- [x] `common.py` — constants (GRID, KICAD_CLI, SYMBOL_LIB_MAP), snap(), uid(), part lookup, LED resistor calc
- [x] `symbols.py` — library loading, raw text extraction, ERC-based pin offset discovery, caching
- [x] `schematic.py` — `SchematicBuilder` class (place, wire, LED, labels, trunks, power, save, lib fixup)
- [x] `verify.py` — `parse_schematic`, 11 general checks, `run_all_checks`, `run_erc`, `UnionFind`
- [x] `__init__.py` — re-exports key public API
- [x] `pcb.py` — PCBBuilder class, DSBGA footprints, netlist parsing

**KiCad symbol/footprint libraries (still needed):**
- SN74LVC1G00, 1G02, 1G04, 1G08, 1G32, 1G86 (logic gates)
- SN74LVC1G79 (D flip-flop, DSBGA), SN74LVC1G74 (D flip-flop, X2SON)
- SN74LVC1G07, 1G125 (buffers/drivers)
- 0402 LED/resistor, power connectors, board interconnect connectors
- DSBGA (YZP) 5-ball/6-ball, X2SON (DQE) 8-pin footprints

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

## Development Workflow

**For any new circuit:**
1. Design first instance manually in KiCad
2. Validate it works (schematic review)
3. Document the pattern
4. Write Python script using kiutils to replicate
5. Generate full schematic with hierarchical organization
6. Use kiutils to place DSBGA + 0402 components in grid
7. Route manually (or assisted)
8. Run DRC/ERC
9. Review power distribution
10. Generate gerbers and order (with solder paste stencil)

## Critical Design Rules

1. **Every gate output -> LED** - No exceptions
2. **Power distribution from day one** - Plan for aggregate LED current
3. **Hierarchical schematics** - Essential for managing complexity
4. **DSBGA reflow assembly** - Solder paste stencil + hot air station
5. **Test one cell first** - Validate before replicating
6. **Grid layouts** - 3-4mm pitch for DSBGA + 0402 LED/resistor cells
7. **Label everything** - With thousands of components, organization is critical
8. **0402 LEDs smaller than ICs** - Bare silicon is the visual focus
9. **Multiple ground/power connections** - Distribute supply across board

## TI Little Logic Parts Reference

**Logic Gates (1 gate per DSBGA package):**
- SN74LVC1G00 - Single 2-input NAND (YZP, 5-ball)
- SN74LVC1G02 - Single 2-input NOR (YZP, 5-ball)
- SN74LVC1G04 - Single Inverter (YZP, 5-ball)
- SN74LVC1G08 - Single 2-input AND (YZP, 5-ball)
- SN74LVC1G11 - Single 3-input AND (YZP, 6-ball)
- SN74LVC1G32 - Single 2-input OR (YZP, 5-ball)
- SN74LVC1G86 - Single 2-input XOR (YZP, 5-ball)

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

**Phase 1 COMPLETE** (including migration to TI Little Logic DSBGA)
**Phase 2 Steps 1-2 COMPLETE** (schematic generation with ERC-clean output)
**Phase 2 Step 3 IN PROGRESS** (PCB generation working — 512 components placed, 0 DRC errors; manual routing next)
**Phase 3 Python library COMPLETE** (shared kicad_gen with SchematicBuilder, PCBBuilder, verify)

## Important Notes for Future Sessions

- User has explicitly requested NO git submodules
- User wants MiSTer as reference (not Brian Bennett's fpga_nes)
- User confirmed kiutils is the right tool
- User wants every single bit in RAM to have an LED
- User wants every gate output to have an LED
- RAM prototype: 8 bytes = 3 address bits, 8 data bits (reduced from 64 bytes to validate DSBGA first)
- TI Little Logic SN74LVC1G in DSBGA (YZP) — bare silicon die visible on top
- 0402 SMD LEDs — smaller than ICs so bare silicon is visual focus
- Assembly method: solder paste stencil + hot air reflow
- Power budget is manageable at 8-byte scale (~0.76W)
- SN74LVC1G74 (D flip-flop with set/reset) is NOT available in DSBGA — only X2SON (DQE)
- SN74LVC1G79 (D flip-flop, Q only) IS in DSBGA — works for RAM cells
