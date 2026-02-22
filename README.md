# Discrete NES - Every Gate Visible

A discrete logic implementation of the Nintendo Entertainment System where **EVERY single gate output and EVERY memory bit is visible via LED**. Watch a complete NES flip every bit in real-time.

## Project Vision

This is an educational and artistic hardware project showcasing the complete internal state of a working NES using **TI Little Logic (SN74LVC1G) in DSBGA packages** — tiny ICs with the **bare silicon die visible** on top. Thousands of exposed silicon dies interspersed with glowing 0402 SMD LEDs.

**This is one of the largest discrete logic projects ever attempted:**
- Estimated **10,000+ LEDs** across all boards
- **Thousands of single-gate ICs** (TI Little Logic, DSBGA — bare silicon visible)
- Multiple PCBs with SMD assembly
- Complete **cycle-accurate NES** implementation in visible discrete logic

## The Aesthetic

Each logic gate is a single DSBGA package (1.75 x 1.25mm) with the bare silicon wafer pattern visible on top. Between them, tiny 0402 SMD LEDs (1.0 x 0.5mm) glow to show every gate output state. The LEDs are deliberately smaller than the ICs so the silicon dies are the visual focus.

## Board Structure

- **RAM Board** - 8-byte prototype (starting point, validates DSBGA assembly)
  - ~180 DSBGA ICs, ~115 LEDs, ~115 resistors
  - ~60x80mm PCB

- **CPU Board** (2A03 - 6502-based with APU)
  - ~5,000 LEDs, ~5,000 ICs

- **PPU Board** (2C02 - Picture Processing Unit)
  - ~3,000 LEDs, ~3,000 ICs

- **Glue Logic/Interconnect Boards**

## Technology Stack

- **Logic ICs:** TI Little Logic SN74LVC1G series (DSBGA — bare silicon die visible)
- **LEDs:** 0402 SMD (smaller than ICs for aesthetic balance)
- **LED Drive:** Direct from LVC outputs (up to 24mA capable, target 2mA)
- **Schematic Generation:** Python + kiutils
- **PCB Layout:** kiutils + manual routing
- **Logic Reference:** MiSTer NES core (Verilog/SystemVerilog)
- **Design Tool:** KiCad 8.x
- **Assembly:** Solder paste stencil + hot air reflow

## Repository Structure

```
discrete-nes/
├── shared/               # Shared libraries and utilities
│   ├── kicad-lib/       # KiCad symbols, footprints, 3D models
│   └── python/          # Python generation utilities
├── boards/              # Individual board projects
│   ├── ram-prototype/   # First prototype board (8 bytes)
│   ├── cpu-2a03/        # CPU board
│   ├── ppu-2c02/        # PPU board
│   └── interconnect/    # Board interconnection
├── reference/           # FPGA reference documentation
└── tools/               # Development tools
```

## Getting Started

### Prerequisites

- Python 3.8+
- KiCad 8.x
- Git

### Setup

1. **Clone this repository:**
   ```bash
   git clone https://github.com/yourusername/discrete-nes.git
   cd discrete-nes
   ```

2. **Set up Python environment:**
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Review the reference documentation:**
   See `reference/README.md` for information about accessing the MiSTer NES core used as logic reference.

## Development Workflow

1. **Design circuit manually** in KiCad (first cell/module)
2. **Document the pattern** for script generation
3. **Write Python scripts** to generate repetitive structures
4. **Generate schematics** using kiutils
5. **Place components** on PCB (scripted DSBGA grid layout)
6. **Route manually** or use assisted tools
7. **Validate** with DRC/ERC
8. **Fabricate and reflow assemble**

## Current Status

**Phase 1: Project Setup** - COMPLETE

- [x] Repository structure created
- [x] Python utilities framework
- [x] Reference documentation
- [x] KiCad shared libraries (symbols/footprints)
- [x] Migration to TI Little Logic DSBGA
- [ ] RAM prototype circuit design (next)

## Power Requirements

With an LED on every gate output, power consumption is LED-dominated:

- RAM prototype (8 bytes): ~0.25A at 3.3V (~0.8W)
- Full system: ~12A+ at 3.3V (~40W)

## Educational Value

This project provides unparalleled visibility into:
- How a CPU executes instructions (watch the program counter increment!)
- Memory addressing and data flow
- Video frame generation in real-time
- Every internal state transition

Perfect for computer science education, maker spaces, and museum exhibits.

## Resources

- **MiSTer NES Core:** https://github.com/MiSTer-devel/NES_MiSTer (logic reference)
- **OpenTendo:** https://github.com/Redherring32/OpenTendo (NES reproduction PCBs)
- **NES Dev Wiki:** https://www.nesdev.org/wiki/
- **Visual 6502:** http://www.visual6502.org/

## Contributing

This is an ambitious project that will evolve over time. Contributions welcome:
- Circuit design improvements
- Python generation utilities
- Documentation
- Testing and validation

## License

TBD - To be determined

## Acknowledgments

- **MiSTer Project** - For the excellent NES core used as logic reference
- **Texas Instruments** - For the Little Logic product line
- **KiCad Project** - For the outstanding open-source EDA tools
- **Ben Eater** - For inspiring discrete logic projects
- **NES Dev Community** - For extensive documentation
