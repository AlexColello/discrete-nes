# Shared KiCad Libraries

This directory contains shared symbol and footprint libraries for the discrete NES project.

## Structure

- **symbols/** - Schematic symbols
  - `74HC_Logic.kicad_sym` - 74HC series logic gates and flip-flops
  - `LED_Indicators.kicad_sym` - LED symbols with integrated resistors
  - `Power_Discrete.kicad_sym` - Power connectors and regulators

- **footprints/** - PCB footprints
  - `DIP_Packages.pretty/` - Through-hole DIP packages for ICs
  - `LED_THT.pretty/` - Through-hole LED footprints
  - `Connectors_Discrete.pretty/` - Board interconnect connectors

- **3d-models/** - 3D models for PCB visualization

## Using These Libraries

The library tables (`sym-lib-table` and `fp-lib-table`) use relative paths with `${KIPRJMOD}` to reference these shared libraries from any board project.

When creating a new board project:

1. Copy `sym-lib-table` to your project directory
2. Copy `fp-lib-table` to your project directory
3. KiCad will automatically find the shared libraries

## Creating New Symbols/Footprints

New symbols and footprints can be added using KiCad's library editors or programmatically using kiutils.

### Common Components Needed:

**Logic Gates:**
- 74HC00 - Quad 2-input NAND
- 74HC02 - Quad 2-input NOR
- 74HC04 - Hex Inverter
- 74HC08 - Quad 2-input AND
- 74HC32 - Quad 2-input OR
- 74HC86 - Quad 2-input XOR

**Flip-Flops and Registers:**
- 74HC74 - Dual D-type flip-flop
- 74HC574 - Octal D-type flip-flop (3-state)
- 74HC273 - Octal D-type flip-flop

**Decoders and Multiplexers:**
- 74HC138 - 3-to-8 line decoder
- 74HC139 - Dual 2-to-4 line decoder
- 74HC151 - 8-to-1 multiplexer

**Buffers and Drivers:**
- 74HC07 - Hex buffer/driver (open drain)
- 74HC125 - Quad bus buffer (3-state)
- 74HC244 - Octal buffer/driver (3-state)

**LEDs:**
- 3mm and 5mm through-hole LEDs (various colors)
- With integrated current-limiting resistor symbols
