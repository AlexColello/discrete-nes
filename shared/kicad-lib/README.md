# Shared KiCad Libraries

This directory contains shared symbol and footprint libraries for the discrete NES project.

## Structure

- **symbols/** - Schematic symbols
  - `LittleLogic_LVC.kicad_sym` - TI Little Logic SN74LVC1G single-gate ICs
  - `LED_Indicators.kicad_sym` - 0402 SMD LED symbols
  - `Power_Discrete.kicad_sym` - Power connectors and regulators

- **footprints/** - PCB footprints
  - `DSBGA_Packages.pretty/` - DSBGA (NanoFree/YZP) packages for TI Little Logic
  - `LED_SMD.pretty/` - 0402 SMD LED footprints
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

**Logic Gates (1 gate per DSBGA package):**
- SN74LVC1G00 - Single 2-input NAND (YZP, 5-ball)
- SN74LVC1G02 - Single 2-input NOR (YZP, 5-ball)
- SN74LVC1G04 - Single Inverter (YZP, 5-ball)
- SN74LVC1G08 - Single 2-input AND (YZP, 5-ball)
- SN74LVC1G11 - Single 3-input AND (YZP, 6-ball)
- SN74LVC1G32 - Single 2-input OR (YZP, 5-ball)
- SN74LVC1G86 - Single 2-input XOR (YZP, 5-ball)

**Flip-Flops:**
- SN74LVC1G79 - Single D flip-flop, Q only (YZP, 5-ball DSBGA)
- SN74LVC1G74 - Single D flip-flop, Q/Q-bar/preset/clear (DQE, 8-pin X2SON)

**Buffers and Drivers:**
- SN74LVC1G07 - Single buffer, open drain (YZP, 5-ball)
- SN74LVC1G125 - Single tri-state buffer (YZP, 5-ball)

**LEDs:**
- 0402 SMD LEDs (various colors)
- 0402 SMD resistors for current limiting
