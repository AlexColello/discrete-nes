# Refactoring Plan: Composable PCB Layout Block Classes

## Context

`generate_pcb.py` is 4868 lines with 44 functions. The layout and prerouting logic is monolithic — every byte group, decoder column, and routing pattern is handled by standalone functions that take raw `(pcb, netlist_data)` and rely on global knowledge of where everything was placed. This makes it impossible to reuse blocks across boards (the full 2KB board at `boards/ram/` has no PCB generation at all yet — it would require copy-pasting thousands of lines).

The core problem: **there's no object that represents "a placed group of components with its routing"**. Layout positions are computed in `main()`, prerouting functions re-discover positions by scanning all footprints, and there's no way to compose a byte group + decoder + bus routing as reusable units.

## Approach

Introduce block classes that own their layout, placement, and prerouting. Each block:
- Knows its own relative component positions
- Places itself on a `PCBBuilder` at an absolute (x, y) origin
- Pre-routes its internal connections
- Exposes interface points (pin positions, bus stubs) for inter-block wiring

### Key Files

| File | Change |
|------|--------|
| `shared/python/kicad_gen/pcb_blocks.py` | **NEW** — Block base class + concrete blocks |
| `shared/python/kicad_gen/__init__.py` | Export new block classes |
| `boards/ram-prototype/scripts/generate_pcb.py` | Refactor to use blocks |

## Block Class Design

### Base: `PCBBlock`

```python
class PCBBlock:
    """Base class for a composable PCB component group."""
    
    def __init__(self):
        self.origin = (0.0, 0.0)       # absolute placement origin
        self._placements = []           # [(comp_dict, rel_x, rel_y), ...]
        self._placed_refs = []          # refs after placement
    
    def layout(self, components):
        """Compute relative placements from netlist components. Sets self._placements."""
        raise NotImplementedError
    
    def place(self, pcb, x, y, netlist_data):
        """Place all components at absolute (x, y) origin. Returns list of refs placed."""
        self.origin = (x, y)
        ...
    
    def preroute(self, pcb, netlist_data):
        """Pre-route internal connections. Called after place()."""
        pass
    
    def get_bounds(self):
        """Return (min_x, min_y, max_x, max_y) in absolute coords."""
        ...
    
    def get_pin(self, ref, pad):
        """Get absolute position of a pad. Shortcut for pcb.get_pad_position()."""
        ...
    
    def get_interface(self):
        """Return dict of named interface points for inter-block wiring."""
        return {}
```

### `ByteGroupBlock` — The highest-value abstraction

Owns: 1 NAND (74LVC2G00) + 8 DFF + 8 BUF + 18 LEDs + 18 Rs = ~54 components per byte.

Currently spread across:
- `layout_byte_group()` (~80 lines) — relative positioning
- `preroute_dff_to_buffer()` (~120 lines) — DFF Q → BUF A via In1.Cu
- `preroute_dff_buf_gnd()` (~60 lines) — shared GND Z-route
- `preroute_dff_buf_data()` (~65 lines) — data pin mirrored Z-route
- `preroute_dff_buf_vcc()` (~80 lines) — VCC L-route via In2.Cu
- `preroute_clk_fanout()` (~65 lines) — CLK horizontal bus
- `preroute_oe_fanout()` (~65 lines) — OE horizontal bus  
- `preroute_nand_connections()` (~280 lines) — NAND output/input routing
- `preroute_nand_leds()` (~110 lines) — NAND LED routing
- `preroute_ic_to_led()` (DFF/BUF portions, ~50 lines) — IC→LED anode
- Parts of `preroute_led_to_resistor()`, `preroute_r_gnd()`, `preroute_power_vias()`

Total: ~1000 lines of byte-specific code → encapsulated as one class with `layout()`, `place()`, `preroute()`.

```python
class ByteGroupBlock(PCBBlock):
    def layout(self, components):
        """Current layout_byte_group() logic."""
        
    def preroute(self, pcb, netlist_data):
        """All internal byte routing:
        - DFF-BUF pairs (Q→A, GND, VCC, data)
        - CLK fanout bus
        - OE fanout bus  
        - NAND connections + NAND LEDs
        - IC→LED, LED→R, R→GND for byte components
        """
    
    def get_interface(self):
        """Returns positions for inter-block wiring:
        - clk_bus: (x_start, x_end, y) — CLK bus entry point
        - oe_bus: (x_start, x_end, y) — OE bus entry point
        - nand_colsel_pin: (x, y) — COL_SEL input to NAND
        - nand_wen_pin: (x, y) — WRITE_EN input to NAND
        - nand_ren_pin: (x, y) — READ_EN input to NAND
        - dff_data_vias: [(x, y, net), ...] — D0-D7 data bus via positions
        """
```

### `DecoderColumnBlock` — Vertical IC column

Owns: N ICs + N LEDs + N Rs in a vertical column with configurable spacing.

Currently: `_place_col()` closure in `main()` + the addr_decoder / column_select custom layout blocks.

```python
class DecoderColumnBlock(PCBBlock):
    def __init__(self, cell_h=4.0, led_offset_x=1.5, r_horiz_offset=1.86):
        ...
    
    def layout(self, ic_cells, total_h=None, ys=None):
        """Place cells vertically centered within total_h, or at explicit ys."""
    
    def preroute(self, pcb, netlist_data):
        """IC→LED, LED→R, power vias for this column."""
    
    def get_interface(self):
        """Returns IC pin positions by index for inter-column wiring."""
```

### `DecoderBlock` — Multi-column decoder (addr_decoder or column_select)

Composes multiple `DecoderColumnBlock`s:

```python
class DecoderBlock(PCBBlock):
    def __init__(self, column_configs):
        """column_configs: list of (num_cells, cell_h, col_spacing) per column."""
        self.columns = []  # list of DecoderColumnBlock
    
    def layout(self, components):
        """Split components into columns, layout each."""
        
    def preroute(self, pcb, netlist_data):
        """Route all columns' internal connections."""
```

### `RowControlBlock` — Vertical AND gate pair

Owns: 2 ANDs + 2 LEDs + 2 Rs.

```python
class RowControlBlock(PCBBlock):
    def __init__(self, gate_spacing=2.3):
        ...
```

### `ConnectorBlock` — Connector + bus indicator LEDs

Owns: 1 connector + matched LED+R pairs aligned to pin Y positions.

### `PowerSupplyBlock` — Buck converter + passives + zones

Owns: TPS546D24A + caps + inductor + resistors + copper pour zones.

## Inter-Block Wiring

After all blocks are placed and internally prerouted, the remaining preroute functions handle inter-block connections. These stay as functions (not methods) because they span multiple blocks:

- `preroute_enable_buses()` — row_ctrl → byte CLK/OE buses
- `preroute_ctrl_enable_trunks()` — control_logic → row_ctrl
- `preroute_col_sel_vias()` / `preroute_colsel_fanout()` — column_select → bytes
- `preroute_column_dbus()` / `preroute_dbus_fanout()` / `preroute_dbus_to_connector()` — data bus across all bytes
- `preroute_connector_leds()` — connector → bus LEDs
- `preroute_coladdr_to_colsel()` — connector → column_select

These inter-block routers can use `block.get_interface()` to discover connection points instead of scanning all footprints.

## How `main()` Changes

Before (pseudocode):
```python
# 200 lines computing layouts for each group type (custom per group)
# 200 lines computing absolute positions
# 200 lines placing components with for loops
# 30 lines calling 20+ preroute functions
```

After:
```python
# Create blocks
bytes = [ByteGroupBlock() for _ in range(8)]
decoder = DecoderBlock(addr_decoder_config)
col_select = DecoderBlock(col_select_config)  
row_ctrls = [RowControlBlock() for _ in range(4)]
connector = ConnectorBlock()
power = PowerSupplyBlock()

# Layout (from netlist components)
for i, name in enumerate(byte_names):
    bytes[i].layout(groups[name])
decoder.layout(groups["addr_decoder"])
# ... etc

# Compute positions (board-specific arrangement)
# ... ~50 lines of position math (reduced from ~200)

# Place + preroute all blocks
for block in all_blocks:
    block.place(pcb, x, y, netlist_data)
    block.preroute(pcb, netlist_data)

# Inter-block routing
route_enable_buses(pcb, row_ctrls, bytes, netlist_data)
route_data_bus(pcb, bytes, connector, netlist_data)
# ... etc
```

## Implementation Order

1. **Create `pcb_blocks.py`** with `PCBBlock` base class and `ByteGroupBlock`
   - Move `layout_byte_group()` → `ByteGroupBlock.layout()`
   - Move byte-specific preroute functions → `ByteGroupBlock.preroute()`
   - Wire up `get_interface()` returning bus stubs and NAND pins

2. **Add `DecoderColumnBlock`** and **`DecoderBlock`**
   - Move `_place_col()` and addr_decoder/column_select layout → block classes
   - Move IC→LED, LED→R, power via routing for decoder ICs

3. **Add `RowControlBlock`**, **`ConnectorBlock`**, **`PowerSupplyBlock`**

4. **Refactor `main()`** to compose blocks
   - Keep inter-block routing as standalone functions that take block references
   - Inter-block routers use `block.get_interface()` instead of footprint scanning

5. **Update `__init__.py`** exports

6. **Verify**: run `generate_pcb.py` → `verify_pcb.py` → 0 errors, 0 warnings

## Also (minor, schematic side)

Move `_add_sheet_block` / `_sheet_height` / `_pin_y` from board scripts to `SchematicBuilder` as `add_sheet_block()` / `sheet_height()` / `sheet_pin_y()` — eliminates the only duplication between the two board schematic scripts.

## What Does NOT Change

- `PCBBuilder` class in `pcb.py` — stays as-is (it's the low-level API)
- `route_pcb.py` — autorouter pipeline, not affected
- `verify_pcb.py` / `verify_schematics.py` — verification scripts
- `debug_1byte.py` — will need updating to use `ByteGroupBlock` instead of importing raw functions
- Schematic generation (`generate_ram.py`) — minimal changes (just the `add_sheet_block` move)

## Verification

Full pipeline — must pass all three stages:

```bash
cd boards/ram-prototype
source ../../venv/Scripts/activate

# 1. Generate PCB + pre-routing DRC
python scripts/generate_pcb.py
python scripts/verify_pcb.py

# 2. Autoroute via FreeRouting
export PATH="/c/Program Files/Java/jdk-21.0.10/bin:$PATH"
python scripts/route_pcb.py

# 3. Post-routing DRC (stricter — checks completed traces)
python scripts/verify_pcb.py --post-routing
```

**Pre-routing target:** 0 errors / 0 warnings (same as current state).

**Post-routing target:** No new violations vs current baseline (2 cosmetic `solder_mask_bridge` + 2 `isolated_copper` warnings on Default/PCBWay/Elecrow rules — 0 real errors). Any new `unconnected_items`, `shorting_items`, or `clearance` errors means the refactoring broke routing.

The autorouter step is critical — preroute changes that look clean in isolation can cause FreeRouting to fail completing remaining traces or produce clearance violations. The post-routing DRC is the definitive pass/fail gate.
