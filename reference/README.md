# FPGA NES Reference Implementation

This directory contains documentation and links to the MiSTer NES core, which serves as the logic reference for the discrete NES implementation.

## MiSTer NES Core

**Repository:** https://github.com/MiSTer-devel/NES_MiSTer

### Accessing the Source

The MiSTer NES core is NOT included as a git submodule. To access the source code:

```bash
# Clone the repository to your local machine
git clone https://github.com/MiSTer-devel/NES_MiSTer.git

# Or download a specific release
# https://github.com/MiSTer-devel/NES_MiSTer/releases
```

### Repository Structure

Key Verilog files for reference:

- **CPU (2A03/6502):**
  - Look for 6502/CPU implementation files
  - APU (Audio Processing Unit) implementation

- **PPU (2C02):**
  - Picture Processing Unit implementation
  - Sprite and background rendering logic

- **Memory Controllers:**
  - RAM interface
  - Mapper implementations

### Using as Logic Reference

The MiSTer core is written in Verilog/SystemVerilog. To convert to discrete logic:

1. **Identify the module** you want to implement (e.g., a register, ALU, decoder)
2. **Analyze the gate-level logic** - may need to synthesize to gate-level netlist
3. **Map gates to 74HC series** using the tools in `shared/python/hdl_parser/`
4. **Add LED indicators** to every gate output
5. **Generate KiCad schematics** using the tools in `shared/python/kicad_gen/`

### Additional References

- **NES Dev Wiki:** https://www.nesdev.org/wiki/
- **6502 Documentation:** http://www.6502.org/
- **Visual 6502:** http://www.visual6502.org/ (transistor-level simulation)

## License Information

The MiSTer NES core is based on FPGANES by Ludvig Strigeus. Check the MiSTer repository for specific license terms.

Our discrete implementation is a separate work that references the logic implementation but does not include the MiSTer code directly.
