# Discrete NES - TODOs

## RAM Prototype - PCB (Phase 2 Step 3)

### Layout issues
- [x] **Layer test table In1 row broken** — fixed: added In1.Cu copper pour zone for the In1 Fill row (was missing because In1.Cu has no full-board zone unlike B.Cu/In2.Cu)
- [x] **COL_SEL and DEC4 headers misplaced** — fixed: J3 (COL_SEL) moved from test grid area to below column_select group; J2/J4 (DEC3/DEC4) remain near addr_decoder/control_logic area
- [x] **Column select LEDs need nicer presentation** — fixed: 16 output LEDs (COL_SEL_0-15) in a dedicated row above the level-2 ANDs (3.5mm gap), remaining rows keep inline LEDs
- [x] **Row control read/write gates layout** — fixed: write + read gates now horizontal (side by side), stride matched to byte row stride (7.0mm) so each row_ctrl block aligns with its byte row
- [x] **Extra spacing on byte silkscreen outlines** — fixed: reduced SILK_MARGIN from 3.0mm to 1.5mm
- [ ] **Layer test table: add hashed fill row** — add a row demonstrating hashed/crosshatch copper fill pattern

### Pre-routing
- [x] **Pin 2 escape stubs** — all DSBGA-5 ICs (including row_ctrl and control_logic) now get center-escape stubs
- [x] **WRITE_ACTIVE trunk** — continuous vertical In1.Cu line; READ_EN vias offset right to avoid crossing
- [x] **COL_SEL fully local routed** — F.Cu from terminus vias to column select ICs with chamfered 45° paths
- [x] **D* data bus fanout** — In1.Cu trunks + F.Cu horizontal bus below byte grid, covered by routing keepout
- [x] **Column select fanout** — In1.Cu trunks extended below D* bus with F.Cu connection to column select ICs
- [x] **NAND LED traces** — cathode-to-R and anode connections with exact pad coordinate precision (3 decimal)
- [x] **Connector LED pre-routing** — J1 to LED + fanout stubs
- [ ] **NAND routing rework for IC_CELL_H=3.0** — NAND local connections (preroute_nand_connections) partially reworked but some escape offsets still tuned for 3.5. Autorouter handles remaining
- [ ] **Row select trace prerouting** — locally preroute all ROW_SEL traces from row control to byte groups

### Autorouter / routing
- [x] **FreeRouting autorouter pipeline** — single-pass routing with DSN clearance fixup
- [x] **DSN clearance rules** — global 200µm (PCBWay pad-to-track), smd_smd 154µm, via_via 500µm (all rule blocks), via_smd/via_pin 254µm
- [x] **Connector net class** — J2/J3/J4 nets in "Connectors" class for potential two-pass routing
- [x] **Routing keepouts** — ram_grid (byte area + D* bus), test_grid, connector+LED area
- [x] **Dangling track cleanup** — up to 50mm length, multi-pass
- [x] **Raw post-import PCB saved** — verify_output/ram_routed_raw.kicad_pcb for inspection
- [x] **Post-routing DRC clean** — 2 solder_mask_bridge (cosmetic) + 2 isolated_copper (warnings) only

### Silkscreen
- [x] **Strip R + DSBGA-8 silk** — 0402 resistor and DSBGA-8 silkscreen removed (too dense for 0.15mm clearance). DSBGA-5/6 silk kept
- [x] **LED polarity dot** — moved from F.SilkS to F.Fab (visible in KiCad, no silk_overlap at any rotation)
- [x] **Footprint text hidden** — all Reference/Value text hidden on F.SilkS and F.Fab
- [x] **silk_overlap removed from skip list** — now passes clean without suppression

### Footprint rework
- [x] **Create custom DSBGA footprints** — done: removed silkscreen pin-1 triangle, reduced courtyard to 0.3mm offset from chip outline
- [x] **Pin connector 3D models rotated 180°** — fixed: added 180° Z rotation to 3D model for B.Cu connectors
- [ ] **Add 3D models for DSBGA-5 and DSBGA-6 footprints** — custom footprints currently lack 3D models for KiCad 3D viewer
- [ ] **Investigate moving resistors to front of board** — currently on B.Cu for space reasons, explore 0201 footprint

### PCB validation
- [x] **Post-routing DRC** — 0 real errors across Default/PCBWay/Elecrow rules
- [ ] **Fabrication review** — verify board meets Elecrow specs (min via 0.8mm/0.4mm, trace/space, etc.)
- [ ] **Power distribution review** — check VCC/GND plane integrity, via current capacity
- [ ] **Generate gerbers** and do final visual inspection
- [ ] **Generate BOM** for ordering

## RAM Prototype - Schematic

### Signal ordering / spacing
- [x] **Row select pin order vs inverter order swapped** — fixed
- [x] **More space between connector and logic** — fixed
- [x] **Connector control pin order** — nCE/nOE/nWE top-to-bottom (pin 8/7/6)

### Wire routing / overlaps
- [ ] **Wire overlaps throughout** — verify_schematics.py check passes but visual issues may remain
- [ ] **Root sheet routing nonsensical** — needs user review in KiCad
- [ ] **Pin names missing on connector** — connector pins should have visible names
- [ ] **Hierarchical labels outside sheet margins** — needs user review

### PCB generate script
- [ ] **Fix layout ASCII art in generate_pcb.py** — doesn't accurately reflect current board layout

### Visual / layout issues
- [ ] **Text drawn on top of components** — overlapping references/values
- [ ] **VCC/GND symbols touching tip-to-tip** — needs user review

### Component selection
- [ ] **Choose LED part numbers and colors** — select specific 0402 LED parts
- [ ] **Update LED resistor values from 750R to 1K** in generate scripts
- [ ] **Choose resistor values per LED color** — different Vf per color

### Architecture improvement
- [ ] **Simplify LED indicators** — investigate sub-sheet encapsulation for LED+R chains

## Assembly Method

Need a custom assembly approach — a pick-and-place machine in budget range can't place all 611+ components before solder paste dries out.

**Proposed approach: tray + suction sheet**

1. **Part tray** — fixture where components are placed at leisure
2. **Suction sheet** — picks up all components and transfers to solder-pasted PCB

### Open questions
- Tray material and fabrication method
- Suction sheet mechanism
- Alignment method between tray and PCB
- Pocket depth for DSBGA vs 0402 parts
- Transfer tolerance requirements

## RAM Prototype - Fabrication (Phase 2 Step 5)

- [ ] Order PCBs + solder paste stencil
- [ ] Order components (DSBGA ICs, 0402 LEDs, 0402 resistors, pin headers)
- [ ] Cost estimation for prototype run
- [ ] Reflow assembly and test

## Shared Library (kicad_gen)

- [x] **DSBGA footprint files modified** — auto-generated by generate_pcb.py
- [x] **3-decimal precision** — get_pad_position, add_trace, _build_net_pad_index all use round(...,3)
- [x] **remove_silkscreen_graphics** — strips R/DSBGA silk from routed boards
- [ ] Consider adding automated tests for kicad_gen modules

## Future Boards

### CPU (2A03)
- [ ] Study MiSTer NES core Verilog for 6502 CPU
- [ ] Gate-level synthesis (Yosys?) of CPU logic
- [ ] Map to SN74LVC1G discrete gates
- [ ] Plan power distribution for ~5000 LEDs (~6A at 3.3V)

### PPU (2C02)
- [ ] Study MiSTer NES core Verilog for PPU
- [ ] Gate-level synthesis of PPU logic
- [ ] Plan power distribution for ~3000 LEDs (~4A at 3.3V)

### System Integration
- [ ] Board interconnect design (backplane or cable)
- [ ] System-level power distribution (12A+ at 3.3V total)
- [ ] Clock distribution across boards

## HDL Parser / Verilog-to-Gates

- [ ] Enhance `verilog_to_gates.py` for real synthesis
- [ ] Evaluate Yosys as synthesis backend
- [ ] Test with MiSTer NES modules
