# Discrete NES - Every Gate Visible

A discrete logic implementation of the Nintendo Entertainment System where **EVERY single gate output and EVERY memory bit is visible via LED**. Watch a complete NES flip every bit in real-time.

## Project Vision

This is an educational and artistic hardware project showcasing the complete internal state of a working NES using discrete 74HC logic ICs with LED indicators on every single gate.

**This is one of the largest discrete logic projects ever attempted:**
- Estimated **10,000+ LEDs** across all boards
- **Thousands of logic ICs** (74HC series)
- **100+ watts** of LED power consumption
- Multiple large PCBs with distributed power systems
- Complete **cycle-accurate NES** implementation in visible discrete logic

## Board Structure

- **RAM Board** - 64-byte prototype (starting point)
  - 512 LEDs for memory cell states
  - Additional LEDs for address/data buses and control logic
  - ~600-800 LEDs total

- **CPU Board** (2A03 - 6502-based with APU)
  - ~5,000 LEDs, 1,500+ ICs

- **PPU Board** (2C02 - Picture Processing Unit)
  - ~3,000 LEDs, 1,000+ ICs

- **Glue Logic/Interconnect Boards**

## Technology Stack

- **Logic Family:** 74HC CMOS (essential for power efficiency)
- **LED Drivers:** Transistor buffers or 74HC07 open-drain
- **Schematic Generation:** Python + kiutils
- **PCB Layout:** kiutils + manual routing
- **Logic Reference:** MiSTer NES core (Verilog/SystemVerilog)
- **Design Tool:** KiCad 8.x

## Repository Structure

```
discrete-nes/
├── shared/               # Shared libraries and utilities
│   ├── kicad-lib/       # KiCad symbols, footprints, 3D models
│   └── python/          # Python generation utilities
├── boards/              # Individual board projects
│   ├── ram-prototype/   # First prototype board
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
5. **Place components** on PCB (scripted grid layout)
6. **Route manually** or use assisted tools
7. **Validate** with DRC/ERC
8. **Fabricate and test**

## Current Status

🚧 **Phase 1: Project Setup** - IN PROGRESS

- [x] Repository structure created
- [x] Python utilities framework
- [x] Reference documentation
- [ ] KiCad shared libraries (symbols/footprints)
- [ ] RAM prototype circuit design

## Power Requirements

**Critical Design Consideration:** With an LED on every gate output, power consumption is dominated by the LEDs:

- RAM prototype: ~1.2-1.6A at 5V (~6-8W)
- Full system: ~20A+ at 5V (~100W+)

High-current power supplies with robust distribution are essential.

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
- **KiCad Project** - For the outstanding open-source EDA tools
- **Ben Eater** - For inspiring discrete logic projects
- **NES Dev Community** - For extensive documentation
