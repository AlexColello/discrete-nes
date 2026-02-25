# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
│       ├── kicad_gen/      # Schematic and PCB generation utilities
│       │   ├── common.py   # Part lookup, LED resistor calc, DSBGA constants
│       │   ├── schematic.py # Schematic generation with kiutils
│       │   └── pcb.py      # PCB layout generation with kiutils
│       └── hdl_parser/     # Verilog to discrete gates conversion
│           └── verilog_to_gates.py
├── boards/
│   ├── ram-prototype/      # First board - 8 byte RAM prototype
│   │   ├── scripts/        # Generation scripts for this board
│   │   └── docs/           # Board-specific documentation
│   ├── cpu-2a03/           # CPU board (future)
│   ├── ppu-2c02/           # PPU board (future)
│   └── interconnect/       # Board interconnections (future)
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

### Generate RAM Prototype

```bash
cd boards/ram-prototype
python scripts/generate_ram.py
# Then ALWAYS run verification:
python scripts/verify_schematics.py
```

**IMPORTANT:** After ANY change to `generate_ram.py`, you MUST:
1. Regenerate: `python scripts/generate_ram.py`
2. Verify: `python scripts/verify_schematics.py`
3. Fix any errors before considering the change complete

The verification script checks for: wire overlaps (net merges), diagonal wires, dangling endpoints, wires passing through component pins, T-junctions without dots, and runs kicad-cli ERC. Output goes to `verify_output/` (gitignored).

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

### kiutils + KiCad 9 ERC Lessons

Hard-won requirements for generating schematics with kiutils that pass KiCad 9 ERC. See `boards/ram-prototype/scripts/generate_ram.py` for working implementation.

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
- **Segmented trunks required** — A single long vertical wire with junctions doesn't reliably connect T-branches in KiCad 9. Split vertical bus/trunk wires into separate segments between each branch point. Use `add_segmented_trunk()` helper in `generate_ram.py`
- **Split wires at LED junction points** — When `place_led_below()` adds a junction on a main wire, the main wire MUST be split into two segments at that junction X coordinate
- **Wire overlap = net merge** — Two wires sharing any segment (same Y, overlapping X range) silently merge their nets. Always verify horizontal wires at the same Y don't overlap
- **T-connections from wire endpoints** — A wire endpoint landing on the middle of another wire creates a connection (even without a junction dot). When routing horizontal branch wires that cross vertical trunks, verify the crossing Y doesn't match any trunk segment endpoint Y
- **Avoid trunk/branch Y coincidence** — If components at the same Y positions feed different vertical trunks, offset one group (e.g., shift inverters by +GRID) so trunk segment endpoints don't share Y values with cross-trunk horizontal wires
- **Wire through pin = unintended connection** — A wire passing through a component pin position (even NC pins) creates a connection in KiCad. Vertical trunks and horizontal branches must avoid ALL pin positions of components they pass near. Use `verify_schematics.py` to detect these
- **NC pin positions matter** — The 74LVC1G04 NC pin (pin 1) has a real connection point at (center_x - 7.62, center_y - 2.54) in schematic space. Vertical trunks at this X will connect to the NC pin. Use half-grid trunk X offsets or different inverter Y offsets to avoid coincidence

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
  - Connector-to-column signals where Y-coincidence with other horizontal wires is unavoidable (A0-A2, nCE/nOE/nWE)
  - Signals with systematic Y-coincidence at the destination (BUF_OE: source Y matches byte D-signal label stub Y)
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

**Connector LED bank:**
- Connector signal LEDs should be in a separate bank area below the connector, NOT inline with signal wires
- Connector pins get short wire stubs to labels; the LED bank uses matching labels to tap the same signal names
- Each LED indicator is a horizontal chain: label → R_Small(90°) → LED_Small(180°) → GND
- This eliminates T-junction risk from LED vertical drops crossing other signal wires

**kiutils API notes:**
- Wire objects use `item.points[0]` and `item.points[1]` (Position objects) — NOT `startPoint`/`endPoint`
- Check `item.type == 'wire'` to distinguish wires from other graphical items
- Library sub-symbols have pins: iterate `lib_sym.symbols` then `sub_sym.pins`
- Pin library coordinates use Y-up; apply Y negation + rotation for schematic space

**Known limitations:**
- `lib_symbol_mismatch` warnings are a kiutils serialization artifact — harmless, fix with "Update symbols from library" in KiCad
- Use `round(v, 2)` on all coordinates to eliminate floating-point noise (e.g., `83.82000000000001`)

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
3. Passes KiCad 9 ERC with 0 real errors (only harmless `lib_symbol_mismatch` warnings)

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

Build out shared KiCad libraries:
- **Symbols needed:**
  - SN74LVC1G00, 1G02, 1G04, 1G08, 1G32, 1G86 (logic gates)
  - SN74LVC1G79 (D flip-flop, DSBGA)
  - SN74LVC1G74 (D flip-flop with set/reset, X2SON — not DSBGA)
  - SN74LVC1G07, 1G125 (buffers/drivers)
  - 0402 LED symbols
  - Power connectors

- **Footprints needed:**
  - DSBGA (YZP) 5-ball, 6-ball variants
  - X2SON (DQE) 8-pin (for SN74LVC1G74)
  - 0402 SMD LED
  - 0402 SMD resistor
  - Power connectors
  - Board interconnect connectors

- **Python utilities:**
  - LED array generation helpers
  - Bus routing helpers
  - Hierarchical sheet management
  - DSBGA grid placement

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
**Next: Phase 2 Step 3 - PCB Layout**

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
