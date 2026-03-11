# Discrete NES - TODOs

## RAM Prototype - PCB (Phase 2 Step 3)

### Layout issues
- [ ] **Layer test table In1 row broken** — the In1 row of the layer test table is not generating correctly
- [ ] **COL_SEL and DEC4 headers misplaced** — pin headers for unused column select and DEC4 outputs are placed in the wrong positions
- [ ] **Column select LEDs need nicer presentation** — current layout is messy, needs visual cleanup
- [ ] **Row control read/write gates layout** — read and write gate pairs should be side by side (horizontal), not stacked on top of each other (vertical)
- [ ] **Extra spacing on byte silkscreen outlines** — the outer silkscreen square around each byte group has unnecessary extra spacing that should be removed

### Pre-routing rework needed
- [ ] **NAND routing rework for IC_CELL_H=3.0** — NAND local connections (preroute_nand_connections) are skipped and left to the autorouter because escape path offsets were tuned for IC_CELL_H=3.5. Needs full rework for current 3.0 geometry (generate_pcb.py:2325-2329)
- [ ] **DFF->Buffer routing** — currently skipped (autorouter via In1.Cu). Data bus via at ic_cx-0.50 leaves 0mm clearance to BUF A pin on F.Cu, so this must go through In1.Cu (generate_pcb.py:2339-2341)
- [ ] **D* data bus fanout prerouting** — preroute the fanout of D0-D7 data bus traces to byte groups
- [ ] **Column select fanout prerouting** — preroute the COL_SEL signal distribution traces

### Footprint rework
- [ ] **Create custom DSBGA footprints** — copy current footprints to custom versions with two changes:
  - Remove the silkscreen triangle (pin 1 indicator)
  - Reduce courtyard to 0.3mm offset from the actual chip outline (currently too large)

### Post-footprint-rework
- [ ] **Compress PCB layout** — once custom footprints with tighter courtyards are in place, reduce component spacing / cell dimensions to take advantage of the smaller footprints

### Autorouter / manual routing
- [ ] **Run FreeRouting autorouter** on current PCB to complete remaining unrouted nets
- [ ] **Verify post-routing DRC** — run `verify_pcb.py --post-routing` after routing completes
- [ ] **Review routed result** — check trace quality, 45-degree angles, no unnecessary vias

### PCB validation
- [ ] **Fabrication review** — verify board meets Elecrow specs (min via 0.8mm/0.4mm, trace/space, etc.)
- [ ] **Power distribution review** — check VCC/GND plane integrity, via current capacity
- [ ] **Generate gerbers** and do final visual inspection
- [ ] **Generate BOM** for ordering

## RAM Prototype - Schematic

### Signal ordering / spacing
- [ ] **Row select pin order vs inverter order swapped** — the order of ROW_SEL pins and the inverters they feed into are reversed relative to each other, making routing difficult. Need to match the ordering
- [ ] **More space between connector and logic** — not enough clearance between the connector block and the first logic column, need to increase the gap

### Wire routing / overlaps
- [ ] **Wire overlaps throughout** — multiple schematic sheets still have wire overlap issues that need to be resolved
- [ ] **Root sheet routing nonsensical** — wire routing on the root sheet doesn't make sense, looks like connector pins are not being positioned/moved correctly
- [ ] **Pin names missing on connector** — connector pins should have visible names next to them

- [ ] **Hierarchical labels outside sheet margins** — some hierarchical labels extend beyond the page boundary

### Visual / layout issues
- [ ] **Text drawn on top of components** — component text (references, values) overlapping other components where it shouldn't be
- [ ] **VCC/GND symbols touching tip-to-tip** — power symbols on gates placed so their endpoints touch each other instead of being properly spaced

### Component selection
- [ ] **Choose LED part numbers and colors** — select specific 0402 LED parts. Consider using different colors to visually distinguish gate types or functional purposes (e.g., address decoder vs data path vs control signals)
- [ ] **Choose resistor values** — calculate and select appropriate current-limiting resistor values for each LED color at 3.3V (different Vf per color means different R values)

### Architecture improvement
- [ ] **Simplify LED indicators** — investigate using a symbol, sub-sheet, or other encapsulation method to hold the LED+resistor chain so that LEDs don't have to be drawn inline in every gate logic sheet. Would significantly reduce schematic clutter and simplify the generate scripts

## RAM Prototype - Fabrication (Phase 2 Step 5)

- [ ] Order PCBs + solder paste stencil
- [ ] Order components (DSBGA ICs, 0402 LEDs, 0402 resistors, pin headers)
- [ ] Cost estimation for prototype run
- [ ] Reflow assembly and test

## Shared Library (kicad_gen)

- [ ] **DSBGA footprint files modified** — 3 footprint files have uncommitted changes (DSBGA-5, DSBGA-6, DSBGA-8 NumericPads)
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
