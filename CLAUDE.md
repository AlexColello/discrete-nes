# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Discrete NES** - A discrete logic NES implementation where EVERY gate output and EVERY memory bit has a visible LED indicator. This is one of the largest discrete logic projects ever attempted.

**Scale:**
- Estimated 10,000+ LEDs across all boards
- Thousands of logic ICs (74HC series)
- 100+ watts of LED power consumption
- Multiple large PCBs with heavy power distribution requirements
- Complete cycle-accurate NES implementation in visible discrete logic

## Core Principles

1. **EVERY gate output needs an LED** - This is non-negotiable. Never design circuits without LED indicators on every single gate output and memory bit
2. **Use kiutils for generation** - Schematics and PCB placement are scripted using Python + kiutils library
3. **74HC CMOS logic family** - Essential for power efficiency. 74LS would consume too much power with thousands of gates
4. **No git submodules** - External references (MiSTer NES) are documented but not included as submodules
5. **MiSTer NES core is the logic reference** - Not Brian Bennett's fpga_nes. See reference/README.md
6. **NO LED multiplexing** - Defeats the purpose of seeing all states simultaneously

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
│       │   ├── common.py   # LED resistor calc, utilities
│       │   ├── schematic.py # Schematic generation with kiutils
│       │   └── pcb.py      # PCB layout generation with kiutils
│       └── hdl_parser/     # Verilog to discrete gates conversion
│           └── verilog_to_gates.py
├── boards/
│   ├── ram-prototype/      # First board - 64 byte RAM prototype
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

### Generate RAM Prototype (Future)

```bash
cd boards/ram-prototype
python scripts/generate_ram_array.py
```

## Technology Stack & Research Findings

### KiCad Scripting - Why kiutils?

**Official KiCad Status (2025):**
- KiCad provides Python bindings ONLY for PCB layout (pcbnew module)
- NO official API for schematic manipulation (eeschema)
- Expected to change in KiCad 9, but not available yet

**Why kiutils over alternatives:**
1. **kiutils** ⭐ CHOSEN
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

### Logic Family - Why 74HC?

**74HC (CMOS) vs 74LS (TTL) Comparison:**

| Feature | 74HC (CMOS) ✅ CHOSEN | 74LS (TTL) ❌ |
|---------|---------------------|--------------|
| **Power** | <1µA quiescent per gate | Few mW per gate |
| **Speed** | ~14-18ns propagation | ~15ns propagation |
| **Supply** | 2-6V flexible | 5V only |
| **Availability (2025)** | Widely available | Many parts obsolete |
| **Cost** | Cheaper | Similar |
| **LED Drive** | Need buffers (~8mA max) | Can drive directly (~16mA) |

**Critical for this project:**
- With 10,000+ gates + LEDs, 74LS would consume excessive power
- 74HC logic itself is negligible (~1mA per chip)
- Power budget is LED-dominated (2mA × 10,000 = 20A!)

**LED Driver Options:**
1. Direct drive from 74HC (8mA max, dim LEDs) - OK for prototype
2. Transistor buffers per LED (2mA comfortable) - Recommended for production
3. 74HC07 open-drain buffers in arrays - Good compromise

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

**First board to implement - validates entire approach**

**Configuration:**
- **64 bytes total capacity**
- **6 address bits** (A0-A5) = 64 addressable locations
- **8-bit data bus** (D0-D7)
- **Control signals:** Read/Write enable, Chip Select

**LED Requirements (EVERY bit visible):**
- **512 LEDs** for RAM cell outputs (one per stored bit) ⚠️ CRITICAL
- **6 LEDs** for address bus
- **8 LEDs** for data bus
- **LEDs for all control signals**
- **LEDs for EVERY intermediate gate output** in address decoder and control logic
- **Total estimate:** 600-800 LEDs for complete visibility

**Circuit Architecture Options:**
1. **Option A:** 512 individual D flip-flops (74HC74 dual = 256 ICs!)
2. **Option B:** 64× 74HC574 8-bit registers (more efficient IC count)
   - BUT each bit output still needs its own LED
3. **Address Decoder:** 6-to-64 decoder (74HC138 + 74HC139 + gates)
4. **Data Bus:** Tri-state buffers for read, latches for write

**Power Budget (RAM Prototype):**
- 600 LEDs × 2mA = 1.2A at 5V = 6W just for LEDs
- Logic power negligible (74HC <1mA per chip)
- Need robust 5V power distribution (thick traces, multiple supply points)

## Full System Power Requirements ⚠️

**This is CRITICAL - plan power distribution from the start:**

- **RAM board:** ~1.2-1.6A at 5V
- **CPU board:** ~10A at 5V (5000+ LEDs)
- **PPU board:** ~6A at 5V (3000+ LEDs)
- **Total system estimate:** 20A+ at 5V = 100W+ just for LEDs

**Design implications:**
- High-current 5V power supplies (multiple rails)
- Distributed regulation across boards
- Thick power traces (consider 2-4oz copper)
- Multiple connector pins dedicated to power
- Consider active cooling for LED heat dissipation
- DO NOT use LED multiplexing (defeats visibility purpose)

## Implementation Plan - Phase by Phase

### ✅ Phase 1: Project Setup (COMPLETED)

- [x] Directory structure created
- [x] Python utilities framework (common.py, schematic.py, pcb.py, verilog_to_gates.py)
- [x] Reference documentation (reference/README.md)
- [x] Shared KiCad library structure (sym-lib-table, fp-lib-table)
- [x] Main README.md with project overview
- [x] requirements.txt with kiutils dependency

### 🔜 Phase 2: RAM Prototype Board (NEXT)

**Step 1: Manual Circuit Design**
1. Open KiCad and create `boards/ram-prototype/ram.kicad_pro`
2. Design ONE memory cell manually:
   - 1 D flip-flop (or use 74HC574 for 8 bits)
   - LED on the Q output
   - LED driver circuit (transistor or 74HC07)
   - Current-limiting resistor (330Ω for red LED at 5V)
3. Validate the circuit works
4. Document the pattern in `boards/ram-prototype/docs/`

**Step 2: Script Development**
1. Create `boards/ram-prototype/scripts/generate_ram_array.py`
2. Use kiutils to:
   - Replicate memory cell 64 times (or 512 times for individual bits)
   - Generate 6-to-64 address decoder with LEDs on every gate
   - Add data bus buffers with LEDs
   - Add control logic with LEDs
   - Create hierarchical schematic sheets for organization
3. Test script generates valid .kicad_sch file

**Step 3: PCB Layout**
1. Use kiutils to place components in logical grid pattern
2. Place all 600-800 LEDs in organized arrays
3. Manual routing (start with power/ground)
4. Design for through-hole assembly (easier for prototype)

**Step 4: Validation**
1. Run DRC/ERC in KiCad
2. Review power distribution (adequate trace widths)
3. Cost estimation (prepare for $hundreds in parts)
4. Iterate design if needed

**Step 5: Fabrication**
1. Generate gerbers
2. Generate BOM
3. Order PCBs
4. Order components
5. Assemble and test!

### Phase 3: Shared Library Development (Parallel to Phase 2)

Build out shared KiCad libraries:
- **Symbols needed:**
  - 74HC00, 74HC02, 74HC04, 74HC08, 74HC32, 74HC86 (logic gates)
  - 74HC74, 74HC574, 74HC273 (flip-flops/registers)
  - 74HC138, 74HC139 (decoders)
  - 74HC07, 74HC125, 74HC244 (buffers/drivers)
  - LED symbols with integrated resistors
  - Power connectors

- **Footprints needed:**
  - DIP-14, DIP-16, DIP-20 (common IC packages)
  - LED 3mm/5mm through-hole
  - Power connectors (high current)
  - Board interconnect connectors

- **Python utilities:**
  - LED array generation helpers
  - Bus routing helpers
  - Hierarchical sheet management
  - Component placement in grids

### Phase 4: FPGA Logic Extraction (Research Phase)

**Goal:** Convert MiSTer Verilog to discrete 74HC netlists

**Challenges:**
1. Verilog is behavioral, need gate-level synthesis
2. Must identify ALL internal signals for LED placement
3. Map Verilog primitives to 74HC parts
4. Generate component lists with LED drivers

**Approach:**
1. Study MiSTer NES Verilog structure
2. Use synthesis tools to generate gate-level netlist
3. Parse netlist and map gates:
   - `and` → 74HC08 (Quad 2-input AND)
   - `or` → 74HC32 (Quad 2-input OR)
   - `not` → 74HC04 (Hex inverter)
   - `nand` → 74HC00 (Quad 2-input NAND)
   - `nor` → 74HC02 (Quad 2-input NOR)
   - `xor` → 74HC86 (Quad 2-input XOR)
4. Auto-generate KiCad schematics with kiutils
5. Add LED to EVERY gate output

**Tool Development:**
- Enhance `shared/python/hdl_parser/verilog_to_gates.py`
- May need external synthesis tool (Yosys?)
- Create `tools/hdl_to_schematic/` converter

### Phase 5: CPU and PPU Boards (Future)

After RAM prototype success:
1. Apply lessons learned (LED drivers, power, assembly)
2. Scale up to CPU board (~5000 LEDs, 1500+ ICs)
3. Develop PPU board (~3000 LEDs, 1000+ ICs)
4. Design backplane or cable interconnect system
5. Integration testing

## Development Workflow

**For any new circuit:**
1. Design first instance manually in KiCad
2. Validate it works (schematic review, maybe breadboard test)
3. Document the pattern
4. Write Python script using kiutils to replicate
5. Generate full schematic with hierarchical organization
6. Use kiutils to place components in grid
7. Route manually (or assisted)
8. Run DRC/ERC
9. Review power distribution
10. Generate gerbers and order

## Critical Design Rules

1. **Every gate output → LED** - No exceptions
2. **Power distribution from day one** - Don't underestimate LED current
3. **Hierarchical schematics** - Essential for managing complexity
4. **Through-hole for prototype** - Easier assembly, more visible
5. **Test one cell first** - Validate before replicating
6. **Grid layouts** - Makes assembly and debugging easier
7. **Label everything** - With thousands of components, organization is critical
8. **Conservative trace widths** - Power traces should be thick (50+ mils)
9. **Multiple ground/power connections** - Distribute supply across board

## Common 74HC Parts Reference

**Logic Gates (Quad = 4 gates per package):**
- 74HC00 - Quad 2-input NAND
- 74HC02 - Quad 2-input NOR
- 74HC04 - Hex Inverter (6 gates)
- 74HC08 - Quad 2-input AND
- 74HC32 - Quad 2-input OR
- 74HC86 - Quad 2-input XOR

**Flip-Flops and Registers:**
- 74HC74 - Dual D flip-flop
- 74HC574 - Octal D flip-flop (8-bit, 3-state)
- 74HC273 - Octal D flip-flop (8-bit)

**Decoders:**
- 74HC138 - 3-to-8 line decoder
- 74HC139 - Dual 2-to-4 line decoder

**Buffers/Drivers:**
- 74HC07 - Hex buffer (open drain) - Good for LED drivers
- 74HC125 - Quad bus buffer (3-state)
- 74HC244 - Octal buffer (3-state)

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
- 74HC datasheets: Texas Instruments, NXP, etc.

## Current Status

**Phase 1 COMPLETE** ✅
**Next: Phase 2 - RAM Prototype Circuit Design**

Ready to start designing the first memory cell in KiCad!

## Important Notes for Future Sessions

- User has explicitly requested NO git submodules
- User wants MiSTer as reference (not Brian Bennett's fpga_nes)
- User confirmed kiutils is the right tool
- User wants every single bit in RAM to have an LED
- User wants every gate output to have an LED
- 64 bytes = 6 address bits, 8 data bits (confirmed with user)
- Power budget is critical - don't underestimate LED current draw
