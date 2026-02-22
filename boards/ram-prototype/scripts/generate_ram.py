#!/usr/bin/env python3
"""
Generate hierarchical KiCad schematics for the 8-byte discrete RAM prototype.

Circuit architecture:
  - 3 address bits (A0-A2) -> 3-to-8 address decoder using inverters + 3-input ANDs
  - 8 data bits (D0-D7), bidirectional data bus
  - Active-low control: /CE, /OE, /WE (NES SRAM interface)
  - 8 bytes x 8 bits = 64 D flip-flops (74LVC1G79)
  - 64 tri-state buffers (74LVC1G125) for read-back
  - LED on EVERY gate output and stored bit

Produces:
  ram.kicad_sch              -- root sheet with connector + bus LEDs + hierarchy refs
  address_decoder.kicad_sch  -- 3 inverters + 8 three-input ANDs
  control_logic.kicad_sch    -- /CE,/OE,/WE inversion + WRITE_ACTIVE, READ_EN logic
  write_clk_gen.kicad_sch    -- 8 NANDs generating WRITE_CLK_0..7
  read_oe_gen.kicad_sch      -- 8 NANDs generating BUF_OE_0..7
  byte_0..7.kicad_sch        -- each has 8 DFFs + 8 tri-state buffers + 16 LEDs
"""

import os
import sys
import uuid as _uuid

from kiutils.schematic import Schematic
from kiutils.symbol import Symbol, SymbolLib, SymbolPin
from kiutils.items.schitems import (
    SchematicSymbol, Connection, LocalLabel, GlobalLabel,
    HierarchicalLabel, HierarchicalSheet, HierarchicalPin,
    HierarchicalSheetInstance, HierarchicalSheetProjectInstance,
    HierarchicalSheetProjectPath, SymbolInstance,
    SymbolProjectInstance, SymbolProjectPath,
)
from kiutils.items.common import (
    Position, Property, Effects, Font, Stroke, Fill, ColorRGBA, PageSettings,
)
from kiutils.items.syitems import SyRect, SyPolyLine

# --------------------------------------------------------------
# Constants
# --------------------------------------------------------------
PROJECT_NAME = "ram"
BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# Schematic grid spacing (KiCad default = 1.27 mm, we use 2.54 multiples)
GRID = 2.54
SYM_SPACING_X = 30 * GRID   # horizontal spacing between symbol columns
SYM_SPACING_Y = 10 * GRID   # vertical spacing between symbol rows (gates)
DFF_SPACING_Y = 12 * GRID   # vertical spacing between DFF rows in byte sheet
LED_OFFSET_X  = 10 * GRID   # LED placed this far right of gate output

# Symbol library file paths (relative from board dir)
LIB_DIR = os.path.normpath(os.path.join(BOARD_DIR, "..", "..", "shared", "kicad-lib", "symbols"))


def uid():
    """Generate a new UUID string."""
    return str(_uuid.uuid4())


# --------------------------------------------------------------
# Library symbol loading -- each .kicad_sch embeds its own copy
# --------------------------------------------------------------

def load_lib_symbols():
    """Load symbol definitions from custom lib + KiCad stock libraries."""
    symbols = {}

    # Stock KiCad symbols for ICs, passives, power, connector
    kicad_sym_dir = r"C:\Program Files\KiCad\9.0\share\kicad\symbols"
    stock_libs = {
        "74xGxx.kicad_sym": [
            "74LVC1G00", "74LVC1G04", "74LVC1G08",
            "74LVC1G11", "74LVC1G79", "74LVC1G125",
        ],
        "Device.kicad_sym": ["R_Small", "LED_Small", "C_Small"],
        "power.kicad_sym": ["VCC", "GND"],
        "Connector_Generic.kicad_sym": ["Conn_01x14"],
    }
    for lib_file, wanted in stock_libs.items():
        lib_path = os.path.join(kicad_sym_dir, lib_file)
        if not os.path.exists(lib_path):
            raise FileNotFoundError(
                f"KiCad stock library not found: {lib_path}\n"
                "Install KiCad 9.0 or adjust kicad_sym_dir path."
            )
        lib = SymbolLib.from_file(lib_path)
        for sym in lib.symbols:
            if sym.libId in wanted:
                symbols[sym.libId] = sym

    return symbols

ALL_SYMBOLS = None  # lazy-loaded


def get_lib_symbols():
    global ALL_SYMBOLS
    if ALL_SYMBOLS is None:
        ALL_SYMBOLS = load_lib_symbols()
    return ALL_SYMBOLS


# --------------------------------------------------------------
# Schematic builder helpers
# --------------------------------------------------------------

class SchematicBuilder:
    """Convenience wrapper around a kiutils Schematic for building sheets."""

    def __init__(self, title="", page_size="A3"):
        self.sch = Schematic.create_new()
        self.sch.paper = PageSettings(paperSize=page_size)
        self._ref_counters = {}  # prefix -> next number
        self._embedded_symbols = set()  # track which lib symbols we've embedded
        self._sym_instance_index = 0

    # --reference designator allocation --

    def _next_ref(self, prefix):
        n = self._ref_counters.get(prefix, 1)
        self._ref_counters[prefix] = n + 1
        return f"{prefix}{n}"

    # --embed a library symbol definition (once per symbol type) --

    def _ensure_lib_symbol(self, sym_name):
        """Embed a library symbol into this schematic's libSymbols if not already there."""
        if sym_name in self._embedded_symbols:
            return
        all_syms = get_lib_symbols()
        if sym_name not in all_syms:
            raise ValueError(f"Symbol '{sym_name}' not found in libraries")
        self.sch.libSymbols.append(all_syms[sym_name])
        self._embedded_symbols.add(sym_name)

    # --place a component --

    def place_symbol(self, lib_name, x, y, ref_prefix="U", value=None,
                     angle=0, mirror=None, extra_props=None):
        """
        Place a symbol instance in the schematic.

        Returns (reference_designator, SchematicSymbol) so caller can wire it.
        """
        self._ensure_lib_symbol(lib_name)
        ref = self._next_ref(ref_prefix)
        if value is None:
            value = lib_name

        sym = SchematicSymbol()
        sym.libId = lib_name
        sym.position = Position(X=x, Y=y, angle=angle)
        sym.unit = 1
        sym.inBom = True
        sym.onBoard = True
        sym.uuid = uid()

        # Properties: Reference, Value, Footprint, Datasheet
        all_syms = get_lib_symbols()
        lib_sym = all_syms[lib_name]
        fp_val = ""
        ds_val = ""
        for p in lib_sym.properties:
            if p.key == "Footprint":
                fp_val = p.value
            elif p.key == "Datasheet":
                ds_val = p.value

        sym.properties = [
            Property(key="Reference", value=ref, id=0,
                     position=Position(X=x, Y=y - 2 * GRID, angle=0),
                     effects=Effects(font=Font(width=1.27, height=1.27))),
            Property(key="Value", value=value, id=1,
                     position=Position(X=x, Y=y + 2 * GRID, angle=0),
                     effects=Effects(font=Font(width=1.27, height=1.27), hide=True)),
            Property(key="Footprint", value=fp_val, id=2,
                     position=Position(X=x, Y=y + 3 * GRID, angle=0),
                     effects=Effects(font=Font(width=1.27, height=1.27), hide=True)),
            Property(key="Datasheet", value=ds_val, id=3,
                     position=Position(X=x, Y=y + 4 * GRID, angle=0),
                     effects=Effects(font=Font(width=1.27, height=1.27), hide=True)),
        ]
        if extra_props:
            for k, v in extra_props.items():
                sym.properties.append(
                    Property(key=k, value=v, id=len(sym.properties),
                             position=Position(X=x, Y=y, angle=0),
                             effects=Effects(font=Font(width=1.27, height=1.27), hide=True))
                )

        if mirror:
            sym.mirror = mirror

        # Instance data (KiCad v7 format)
        sym.instances.append(SymbolProjectInstance(
            name=PROJECT_NAME,
            paths=[SymbolProjectPath(
                sheetInstancePath=f"/{self.sch.uuid}/",
                reference=ref,
                unit=1,
            )]
        ))

        self.sch.schematicSymbols.append(sym)
        return ref, sym

    # --place an LED + resistor pair (indicator for a gate output) --

    def place_led_indicator(self, x, y):
        """
        Place LED + 680R resistor indicator. Returns (input_x, input_y) where the
        resistor's pin1 is located -- caller must wire to this point.
        GND power symbol placed at LED cathode automatically.
        """
        self.place_symbol("R_Small", x, y, ref_prefix="R", value="680R", angle=90)
        self.place_symbol("LED_Small", x + 5.08, y, ref_prefix="D", value="Red", angle=180)
        self.place_power("GND", x + 7.62, y, angle=90)
        return (x - 2.54, y)

    # --net labels --

    def add_label(self, text, x, y, angle=0):
        """Add a local net label."""
        label = LocalLabel()
        label.text = text
        label.position = Position(X=x, Y=y, angle=angle)
        label.effects = Effects(font=Font(width=1.27, height=1.27))
        label.uuid = uid()
        self.sch.labels.append(label)
        return label

    def add_global_label(self, text, x, y, shape="bidirectional", angle=0):
        """Add a global net label."""
        label = GlobalLabel()
        label.text = text
        label.shape = shape
        label.position = Position(X=x, Y=y, angle=angle)
        label.effects = Effects(font=Font(width=1.27, height=1.27))
        label.uuid = uid()
        self.sch.globalLabels.append(label)
        return label

    def add_hier_label(self, text, x, y, shape="bidirectional", angle=0):
        """Add a hierarchical label (connects to parent sheet pin)."""
        label = HierarchicalLabel()
        label.text = text
        label.shape = shape
        label.position = Position(X=x, Y=y, angle=angle)
        label.effects = Effects(font=Font(width=1.27, height=1.27))
        label.uuid = uid()
        self.sch.hierarchicalLabels.append(label)
        return label

    # --wires --

    def add_wire(self, x1, y1, x2, y2):
        """Add a wire between two points."""
        conn = Connection()
        conn.type = "wire"
        conn.points = [Position(X=x1, Y=y1), Position(X=x2, Y=y2)]
        conn.uuid = uid()
        self.sch.graphicalItems.append(conn)
        return conn

    # --power symbols --

    def place_power(self, symbol_name, x, y, angle=0):
        """Place a power symbol (VCC or GND)."""
        prefix = "#PWR"
        return self.place_symbol(symbol_name, x, y, ref_prefix=prefix,
                                 value=symbol_name, angle=angle)

    # --save --

    def save(self, filepath):
        self.sch.to_file(filepath)
        return filepath


# --------------------------------------------------------------
# Sub-sheet generators
# --------------------------------------------------------------

def generate_address_decoder():
    """
    Address decoder: 3-to-8 using inverters and 3-input ANDs.

    Inputs:  A0, A1, A2
    Outputs: SEL0..SEL7

    SEL0 = /A2 & /A1 & /A0
    SEL1 = /A2 & /A1 &  A0
    ...
    SEL7 =  A2 &  A1 &  A0
    """
    b = SchematicBuilder(title="Address Decoder", page_size="A3")
    base_x, base_y = 25.4, 25.4

    # Hierarchical labels for inputs
    for i in range(3):
        b.add_hier_label(f"A{i}", base_x, base_y + i * 10 * GRID, shape="input", angle=180)

    # Three inverters for complemented address bits
    inv_x = base_x + 12 * GRID
    for i in range(3):
        y = base_y + i * 10 * GRID
        b.place_symbol("74LVC1G04", inv_x, y)
        # Power symbols at pin endpoints
        b.place_power("VCC", inv_x - 5.08, y - 10.16)
        b.place_power("GND", inv_x - 5.08, y + 10.16)
        # Input: wire stub + label
        b.add_wire(inv_x - 15.24, y, inv_x - 17.78, y)
        b.add_label(f"A{i}", inv_x - 17.78, y)
        # Output: wire to LED
        out_x = inv_x + 12.7
        led_in_x, led_in_y = b.place_led_indicator(inv_x + LED_OFFSET_X, y)
        b.add_wire(out_x, y, led_in_x, led_in_y)
        # Label at output for connection to ANDs
        b.add_label(f"A{i}_INV", out_x, y)

    # 8 three-input AND gates for decode
    and_x = base_x + 35 * GRID
    decode_table = [
        (0, 0, 0), (0, 0, 1), (0, 1, 0), (0, 1, 1),
        (1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1),
    ]

    for sel_idx, (a2, a1, a0) in enumerate(decode_table):
        y = base_y + sel_idx * 10 * GRID
        b.place_symbol("74LVC1G11", and_x, y)
        # Power
        b.place_power("VCC", and_x, y - 10.16)
        b.place_power("GND", and_x, y + 10.16)
        # Input wire stubs + labels
        a2_net = "A2" if a2 else "A2_INV"
        b.add_wire(and_x - 15.24, y - 5.08, and_x - 17.78, y - 5.08)
        b.add_label(a2_net, and_x - 17.78, y - 5.08)
        a1_net = "A1" if a1 else "A1_INV"
        b.add_wire(and_x - 15.24, y, and_x - 17.78, y)
        b.add_label(a1_net, and_x - 17.78, y)
        a0_net = "A0" if a0 else "A0_INV"
        b.add_wire(and_x - 15.24, y + 5.08, and_x - 17.78, y + 5.08)
        b.add_label(a0_net, and_x - 17.78, y + 5.08)
        # Output: wire to LED + label
        sel_net = f"SEL{sel_idx}"
        out_x = and_x + 12.7
        led_in_x, led_in_y = b.place_led_indicator(and_x + LED_OFFSET_X, y)
        b.add_wire(out_x, y, led_in_x, led_in_y)
        b.add_label(sel_net, out_x, y)

    # Hierarchical labels for outputs
    out_x = and_x + 25 * GRID
    for i in range(8):
        b.add_hier_label(f"SEL{i}", out_x, base_y + i * 10 * GRID, shape="output")

    # NO VCC/GND hier labels -- power symbols are global
    return b


def generate_control_logic():
    """
    Control logic: active-low to active-high + combined signals.

    Inputs:  /CE, /OE, /WE
    Outputs: WRITE_ACTIVE = CE & WE
             READ_EN = CE & OE & /WE

    Uses:
      3x 74LVC1G04 (invert /CE->CE, /OE->OE, /WE->WE)
      1x 74LVC1G08 AND(CE, WE) -> WRITE_ACTIVE
      1x 74LVC1G08 AND(CE, OE) -> CE_AND_OE
      1x 74LVC1G08 AND(CE_AND_OE, /WE) -> READ_EN  (/WE is already active-low = high when not writing)
    """
    b = SchematicBuilder(title="Control Logic", page_size="A3")
    base_x, base_y = 25.4, 25.4

    # Hierarchical labels for inputs
    ctrl_signals = ["nCE", "nOE", "nWE"]
    for i, sig in enumerate(ctrl_signals):
        b.add_hier_label(sig, base_x, base_y + i * 10 * GRID, shape="input", angle=180)

    # Three inverters
    inv_x = base_x + 12 * GRID
    active_names = ["CE", "OE", "WE"]
    for i, (ctrl, active) in enumerate(zip(ctrl_signals, active_names)):
        y = base_y + i * 10 * GRID
        b.place_symbol("74LVC1G04", inv_x, y)
        b.place_power("VCC", inv_x - 5.08, y - 10.16)
        b.place_power("GND", inv_x - 5.08, y + 10.16)
        b.add_wire(inv_x - 15.24, y, inv_x - 17.78, y)
        b.add_label(ctrl, inv_x - 17.78, y)
        out_x = inv_x + 12.7
        led_in_x, led_in_y = b.place_led_indicator(inv_x + LED_OFFSET_X, y)
        b.add_wire(out_x, y, led_in_x, led_in_y)
        b.add_label(active, out_x, y)

    # AND1: CE & WE -> WRITE_ACTIVE
    and1_x = base_x + 35 * GRID
    and1_y = base_y + 5 * GRID
    b.place_symbol("74LVC1G08", and1_x, and1_y)
    b.place_power("VCC", and1_x, and1_y - 10.16)
    b.place_power("GND", and1_x, and1_y + 10.16)
    b.add_wire(and1_x - 15.24, and1_y - 2.54, and1_x - 17.78, and1_y - 2.54)
    b.add_label("CE", and1_x - 17.78, and1_y - 2.54)
    b.add_wire(and1_x - 15.24, and1_y + 2.54, and1_x - 17.78, and1_y + 2.54)
    b.add_label("WE", and1_x - 17.78, and1_y + 2.54)
    out_x = and1_x + 12.7
    led_in_x, led_in_y = b.place_led_indicator(and1_x + LED_OFFSET_X, and1_y)
    b.add_wire(out_x, and1_y, led_in_x, led_in_y)
    b.add_label("WRITE_ACTIVE", out_x, and1_y)

    # AND2: CE & OE -> CE_AND_OE
    and2_x = base_x + 35 * GRID
    and2_y = base_y + 15 * GRID
    b.place_symbol("74LVC1G08", and2_x, and2_y)
    b.place_power("VCC", and2_x, and2_y - 10.16)
    b.place_power("GND", and2_x, and2_y + 10.16)
    b.add_wire(and2_x - 15.24, and2_y - 2.54, and2_x - 17.78, and2_y - 2.54)
    b.add_label("CE", and2_x - 17.78, and2_y - 2.54)
    b.add_wire(and2_x - 15.24, and2_y + 2.54, and2_x - 17.78, and2_y + 2.54)
    b.add_label("OE", and2_x - 17.78, and2_y + 2.54)
    out_x = and2_x + 12.7
    led_in_x, led_in_y = b.place_led_indicator(and2_x + LED_OFFSET_X, and2_y)
    b.add_wire(out_x, and2_y, led_in_x, led_in_y)
    b.add_label("CE_AND_OE", out_x, and2_y)

    # AND3: CE_AND_OE & /WE -> READ_EN
    and3_x = base_x + 55 * GRID
    and3_y = base_y + 15 * GRID
    b.place_symbol("74LVC1G08", and3_x, and3_y)
    b.place_power("VCC", and3_x, and3_y - 10.16)
    b.place_power("GND", and3_x, and3_y + 10.16)
    b.add_wire(and3_x - 15.24, and3_y - 2.54, and3_x - 17.78, and3_y - 2.54)
    b.add_label("CE_AND_OE", and3_x - 17.78, and3_y - 2.54)
    b.add_wire(and3_x - 15.24, and3_y + 2.54, and3_x - 17.78, and3_y + 2.54)
    b.add_label("nWE", and3_x - 17.78, and3_y + 2.54)
    out_x = and3_x + 12.7
    led_in_x, led_in_y = b.place_led_indicator(and3_x + LED_OFFSET_X, and3_y)
    b.add_wire(out_x, and3_y, led_in_x, led_in_y)
    b.add_label("READ_EN", out_x, and3_y)

    # Hierarchical labels for outputs
    out_label_x = and3_x + 25 * GRID
    b.add_hier_label("WRITE_ACTIVE", out_label_x, base_y + 5 * GRID, shape="output")
    b.add_hier_label("READ_EN", out_label_x, base_y + 15 * GRID, shape="output")

    return b


def _generate_nand_bank(title, enable_signal, output_prefix):
    """Shared generator for write_clk_gen and read_oe_gen (8 NANDs each)."""
    b = SchematicBuilder(title=title, page_size="A3")
    base_x, base_y = 25.4, 25.4

    # Input hier labels
    b.add_hier_label(enable_signal, base_x, base_y, shape="input", angle=180)
    for i in range(8):
        b.add_hier_label(f"SEL{i}", base_x, base_y + (i + 1) * 10 * GRID, shape="input", angle=180)

    # 8 NAND gates
    nand_x = base_x + 18 * GRID
    for i in range(8):
        y = base_y + (i + 1) * 10 * GRID
        b.place_symbol("74LVC1G00", nand_x, y)
        b.place_power("VCC", nand_x, y - 10.16)
        b.place_power("GND", nand_x, y + 10.16)
        # Input wire stubs + labels
        b.add_wire(nand_x - 15.24, y - 2.54, nand_x - 17.78, y - 2.54)
        b.add_label(enable_signal, nand_x - 17.78, y - 2.54)
        b.add_wire(nand_x - 15.24, y + 2.54, nand_x - 17.78, y + 2.54)
        b.add_label(f"SEL{i}", nand_x - 17.78, y + 2.54)
        # Output: wire to LED + label
        out_net = f"{output_prefix}{i}"
        out_x = nand_x + 12.7
        led_in_x, led_in_y = b.place_led_indicator(nand_x + LED_OFFSET_X, y)
        b.add_wire(out_x, y, led_in_x, led_in_y)
        b.add_label(out_net, out_x, y)

    # Output hier labels
    out_x = nand_x + 28 * GRID
    for i in range(8):
        b.add_hier_label(f"{output_prefix}{i}", out_x, base_y + (i + 1) * 10 * GRID, shape="output")

    return b


def generate_write_clk_gen():
    """Write clock generation: 8 NANDs. WRITE_CLK_n = NAND(WRITE_ACTIVE, SELn)"""
    return _generate_nand_bank("Write Clock Generator", "WRITE_ACTIVE", "WRITE_CLK_")


def generate_read_oe_gen():
    """Read OE generation: 8 NANDs. BUF_OE_n = NAND(READ_EN, SELn)"""
    return _generate_nand_bank("Read OE Generator", "READ_EN", "BUF_OE_")


def generate_byte_sheet():
    """
    Generate one memory byte (8 bits) -- reused for all 8 bytes.

    Each bit:
      - 74LVC1G79 D flip-flop: D <- data bus, CLK <- WRITE_CLK, Q -> LED + buffer
      - 74LVC1G125 tri-state buffer: A <- DFF Q, /OE <- BUF_OE, Y -> data bus
      - LED on DFF Q output
      - LED on buffer Y output

    Hierarchical labels use generic names (WRITE_CLK, BUF_OE, D0-D7).
    The parent sheet connects instance-specific nets to these pins.
    """
    b = SchematicBuilder(title="Memory Byte", page_size="A3")
    base_x, base_y = 25.4, 25.4
    DFF_ROW = 12 * GRID

    # Hierarchical labels
    for bit in range(8):
        b.add_hier_label(f"D{bit}", base_x, base_y + bit * DFF_ROW,
                         shape="bidirectional", angle=180)
    b.add_hier_label("WRITE_CLK", base_x, base_y + 98 * GRID, shape="input", angle=180)
    b.add_hier_label("BUF_OE", base_x, base_y + 100 * GRID, shape="input", angle=180)

    dff_x = base_x + 15 * GRID
    buf_x = base_x + 50 * GRID

    for bit in range(8):
        y = base_y + bit * DFF_ROW
        q_net = f"Q_{bit}"

        # D flip-flop
        b.place_symbol("74LVC1G79", dff_x, y)
        b.place_power("VCC", dff_x, y - 12.7)
        b.place_power("GND", dff_x, y + 12.7)
        # D input wire stub + label
        b.add_wire(dff_x - 12.7, y - 5.08, dff_x - 15.24, y - 5.08)
        b.add_label(f"D{bit}", dff_x - 15.24, y - 5.08)
        # CLK input wire stub + label
        b.add_wire(dff_x - 12.7, y + 5.08, dff_x - 15.24, y + 5.08)
        b.add_label("WRITE_CLK", dff_x - 15.24, y + 5.08)
        # Q output wire to LED
        q_out_x = dff_x + 12.7
        q_out_y = y - 5.08
        led_in_x, led_in_y = b.place_led_indicator(dff_x + LED_OFFSET_X, y - 2 * GRID)
        b.add_wire(q_out_x, q_out_y, led_in_x, led_in_y)
        b.add_label(q_net, q_out_x, q_out_y)

        # Tri-state buffer
        b.place_symbol("74LVC1G125", buf_x, y)
        b.place_power("VCC", buf_x - 5.08, y - 10.16)
        b.place_power("GND", buf_x - 5.08, y + 10.16)
        # A input wire stub + label
        b.add_wire(buf_x - 15.24, y, buf_x - 17.78, y)
        b.add_label(q_net, buf_x - 17.78, y)
        # ~OE input wire stub + label
        b.add_wire(buf_x, y - 10.16, buf_x, y - 12.7)
        b.add_label("BUF_OE", buf_x, y - 12.7)
        # Y output: wire to LED + label back to data bus
        buf_out_x = buf_x + 12.7
        led_in_x, led_in_y = b.place_led_indicator(buf_x + LED_OFFSET_X, y + 3 * GRID)
        b.add_wire(buf_out_x, y, led_in_x, led_in_y)
        b.add_label(f"D{bit}", buf_out_x, y)

    return b


# --------------------------------------------------------------
# Root sheet generator
# --------------------------------------------------------------

def generate_root_sheet():
    """
    Root sheet: connector, bus indicator LEDs, and hierarchical sheet references.
    """
    b = SchematicBuilder(title="8-Byte Discrete RAM", page_size="A2")
    base_x, base_y = 25.4, 25.4

    # --External connector --
    conn_x = base_x
    conn_y = base_y + 20 * GRID
    ref, _ = b.place_symbol("Conn_01x14", conn_x, conn_y, ref_prefix="J", value="SRAM_Bus")

    # Wire connector pins to global labels
    # Stock Conn_01x14 pins at x-5.08 from symbol center, spaced 2.54mm
    pin_x = conn_x - 5.08
    signal_names = [
        "A0", "A1", "A2",
        "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7",
        "nCE", "nOE", "nWE",
    ]
    # Stock Conn_01x14 pin Y positions: 15.24, 12.7, 10.16, 7.62, 5.08, 2.54, 0,
    #   -2.54, -5.08, -7.62, -10.16, -12.7, -15.24, -17.78
    pin_y_offsets = [
        15.24, 12.7, 10.16,
        7.62, 5.08, 2.54, 0, -2.54, -5.08, -7.62, -10.16,
        -12.7, -15.24, -17.78,
    ]

    for sig, y_off in zip(signal_names, pin_y_offsets):
        sig_y = conn_y + y_off
        label_x = pin_x - 2.54
        b.add_global_label(sig, label_x, sig_y,
                           shape="bidirectional" if sig.startswith("D") else "input",
                           angle=180)

    # --Bus indicator LEDs --
    led_base_x = base_x + 20 * GRID
    led_base_y = base_y

    # Address LEDs
    for i in range(3):
        y = led_base_y + i * 4 * GRID
        led_in_x, led_in_y = b.place_led_indicator(led_base_x, y)
        b.add_wire(led_in_x, led_in_y, led_in_x - 2.54, led_in_y)
        b.add_global_label(f"A{i}", led_in_x - 2.54, led_in_y, shape="input", angle=180)

    # Data bus LEDs
    for i in range(8):
        y = led_base_y + (i + 3) * 4 * GRID
        led_in_x, led_in_y = b.place_led_indicator(led_base_x, y)
        b.add_wire(led_in_x, led_in_y, led_in_x - 2.54, led_in_y)
        b.add_global_label(f"D{i}", led_in_x - 2.54, led_in_y, shape="bidirectional", angle=180)

    # Control signal LEDs
    ctrl_names = ["nCE", "nOE", "nWE"]
    for i, name in enumerate(ctrl_names):
        y = led_base_y + (i + 11) * 4 * GRID
        led_in_x, led_in_y = b.place_led_indicator(led_base_x, y)
        b.add_wire(led_in_x, led_in_y, led_in_x - 2.54, led_in_y)
        b.add_global_label(name, led_in_x - 2.54, led_in_y, shape="input", angle=180)

    # --Hierarchical sheet references --
    sheet_x = base_x + 55 * GRID
    sheet_w = 30 * GRID
    sheet_h = 20 * GRID
    sheet_gap = 4 * GRID

    def add_sheet_ref(name, filename, pins, y_pos):
        """Add a hierarchical sheet rectangle with pins."""
        sheet = HierarchicalSheet()
        sheet.position = Position(X=sheet_x, Y=y_pos)
        sheet.width = sheet_w
        sheet.height = sheet_h
        sheet.stroke = Stroke(width=0.1)
        sheet.fill = ColorRGBA(R=255, G=255, B=225, A=255, precision=4)
        sheet.uuid = uid()
        sheet.sheetName = Property(
            key="Sheet name", value=name, id=0,
            position=Position(X=sheet_x, Y=y_pos - 1.27, angle=0),
            effects=Effects(font=Font(width=1.27, height=1.27)),
        )
        sheet.fileName = Property(
            key="Sheet file", value=filename, id=1,
            position=Position(X=sheet_x, Y=y_pos + sheet_h + 1.27, angle=0),
            effects=Effects(font=Font(width=1.27, height=1.27)),
        )

        # Add hierarchical pins
        for pin_idx, (pin_name, pin_type) in enumerate(pins):
            pin = HierarchicalPin()
            pin.name = pin_name
            pin.connectionType = pin_type
            # Place pins along the left edge
            pin.position = Position(X=sheet_x, Y=y_pos + 2.54 + pin_idx * 2.54, angle=180)
            pin.effects = Effects(font=Font(width=1.27, height=1.27))
            pin.uuid = uid()
            sheet.pins.append(pin)

        # Instance path
        sheet.instances.append(HierarchicalSheetProjectInstance(
            name=PROJECT_NAME,
            paths=[HierarchicalSheetProjectPath(
                sheetInstancePath=f"/{b.sch.uuid}/{sheet.uuid}/",
                page=str(len(b.sch.sheets) + 2),
            )]
        ))

        b.sch.sheets.append(sheet)

        # Add global labels to connect to the sheet pins
        for pin_idx, (pin_name, pin_type) in enumerate(pins):
            pin_y = y_pos + 2.54 + pin_idx * 2.54
            gl_shape = {"input": "output", "output": "input",
                        "bidirectional": "bidirectional", "passive": "passive"}
            b.add_global_label(pin_name, sheet_x - 5.08, pin_y,
                               shape=gl_shape.get(pin_type, "bidirectional"),
                               angle=0)

    # Address decoder sheet
    addr_pins = [("A0", "input"), ("A1", "input"), ("A2", "input")]
    addr_pins += [(f"SEL{i}", "output") for i in range(8)]
    y = base_y
    add_sheet_ref("Address Decoder", "address_decoder.kicad_sch", addr_pins, y)

    # Control logic sheet
    y += sheet_h + sheet_gap
    ctrl_pins = [("nCE", "input"), ("nOE", "input"), ("nWE", "input"),
                 ("WRITE_ACTIVE", "output"), ("READ_EN", "output")]
    add_sheet_ref("Control Logic", "control_logic.kicad_sch", ctrl_pins, y)

    # Write clock gen sheet
    y += sheet_h + sheet_gap
    wclk_pins = [("WRITE_ACTIVE", "input")]
    wclk_pins += [(f"SEL{i}", "input") for i in range(8)]
    wclk_pins += [(f"WRITE_CLK_{i}", "output") for i in range(8)]
    add_sheet_ref("Write Clk Gen", "write_clk_gen.kicad_sch", wclk_pins, y)

    # Read OE gen sheet
    y += sheet_h + sheet_gap
    roe_pins = [("READ_EN", "input")]
    roe_pins += [(f"SEL{i}", "input") for i in range(8)]
    roe_pins += [(f"BUF_OE_{i}", "output") for i in range(8)]
    add_sheet_ref("Read OE Gen", "read_oe_gen.kicad_sch", roe_pins, y)

    # 8 byte sheets (in a second column) -- all reference the same byte.kicad_sch
    sheet_x_col2 = sheet_x + sheet_w + 15 * GRID
    # Pin names inside byte.kicad_sch are generic; parent connects instance-specific nets
    byte_pin_defs = [("WRITE_CLK", "input"), ("BUF_OE", "input")]
    byte_pin_defs += [(f"D{bit}", "bidirectional") for bit in range(8)]

    for byte_idx in range(8):
        y = base_y + byte_idx * (sheet_h + sheet_gap)

        sheet = HierarchicalSheet()
        sheet.position = Position(X=sheet_x_col2, Y=y)
        sheet.width = sheet_w
        sheet.height = sheet_h
        sheet.stroke = Stroke(width=0.1)
        sheet.fill = ColorRGBA(R=225, G=255, B=225, A=255, precision=4)
        sheet.uuid = uid()
        sheet.sheetName = Property(
            key="Sheet name", value=f"Byte {byte_idx}", id=0,
            position=Position(X=sheet_x_col2, Y=y - 1.27, angle=0),
            effects=Effects(font=Font(width=1.27, height=1.27)),
        )
        sheet.fileName = Property(
            key="Sheet file", value="byte.kicad_sch", id=1,
            position=Position(X=sheet_x_col2, Y=y + sheet_h + 1.27, angle=0),
            effects=Effects(font=Font(width=1.27, height=1.27)),
        )

        for pin_idx, (pin_name, pin_type) in enumerate(byte_pin_defs):
            pin = HierarchicalPin()
            pin.name = pin_name
            pin.connectionType = pin_type
            pin.position = Position(X=sheet_x_col2,
                                    Y=y + 2.54 + pin_idx * 2.54, angle=180)
            pin.effects = Effects(font=Font(width=1.27, height=1.27))
            pin.uuid = uid()
            sheet.pins.append(pin)

        sheet.instances.append(HierarchicalSheetProjectInstance(
            name=PROJECT_NAME,
            paths=[HierarchicalSheetProjectPath(
                sheetInstancePath=f"/{b.sch.uuid}/{sheet.uuid}/",
                page=str(len(b.sch.sheets) + 2),
            )]
        ))

        b.sch.sheets.append(sheet)

        # Global labels connecting to byte sheet pins
        # Map generic pin names to instance-specific net names
        for pin_idx, (pin_name, pin_type) in enumerate(byte_pin_defs):
            pin_y = y + 2.54 + pin_idx * 2.54
            if pin_name == "WRITE_CLK":
                net_name = f"WRITE_CLK_{byte_idx}"
            elif pin_name == "BUF_OE":
                net_name = f"BUF_OE_{byte_idx}"
            else:
                net_name = pin_name
            gl_shape = {"input": "output", "output": "input",
                        "bidirectional": "bidirectional", "passive": "passive"}
            b.add_global_label(net_name, sheet_x_col2 - 5.08, pin_y,
                               shape=gl_shape.get(pin_type, "bidirectional"),
                               angle=0)

    return b


# --------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------

def count_components(builders):
    """Count total ICs, LEDs, resistors across all sheets."""
    totals = {"U": 0, "D": 0, "R": 0, "C": 0, "#PWR": 0, "J": 0}
    for name, builder in builders.items():
        # The byte sheet is instantiated 8 times
        multiplier = 8 if name == "byte" else 1
        for prefix, count in builder._ref_counters.items():
            actual = count - 1  # counter is next-to-allocate
            if prefix in totals:
                totals[prefix] += actual * multiplier
            else:
                totals[prefix] = actual * multiplier
    return totals


def main():
    print("=" * 60)
    print("Discrete NES - 8-Byte RAM Prototype Schematic Generator")
    print("=" * 60)

    # Generate all sub-sheets first (they reference lib symbols)
    print("\nGenerating sub-sheets...")

    builders = {}

    # Address decoder
    builders["address_decoder"] = generate_address_decoder()
    print("  [+] address_decoder.kicad_sch")

    # Control logic
    builders["control_logic"] = generate_control_logic()
    print("  [+] control_logic.kicad_sch")

    # Write clock gen
    builders["write_clk_gen"] = generate_write_clk_gen()
    print("  [+] write_clk_gen.kicad_sch")

    # Read OE gen
    builders["read_oe_gen"] = generate_read_oe_gen()
    print("  [+] read_oe_gen.kicad_sch")

    # Single byte sheet (reused for all 8 byte instances)
    builders["byte"] = generate_byte_sheet()
    print("  [+] byte.kicad_sch (shared by all 8 byte instances)")

    # Root sheet
    builders["ram"] = generate_root_sheet()
    print("  [+] ram.kicad_sch (root)")

    # Save all files
    print("\nSaving files...")
    for name, builder in builders.items():
        filepath = os.path.join(BOARD_DIR, f"{name}.kicad_sch")
        builder.save(filepath)
        print(f"  Saved: {filepath}")

    # Component count summary
    totals = count_components(builders)
    print("\n" + "=" * 60)
    print("Component Summary")
    print("=" * 60)
    print(f"  ICs (U):        {totals.get('U', 0)}")
    print(f"  LEDs (D):       {totals.get('D', 0)}")
    print(f"  Resistors (R):  {totals.get('R', 0)}")
    print(f"  Connectors (J): {totals.get('J', 0)}")
    print(f"  Power (#PWR):   {totals.get('#PWR', 0)}")
    total_parts = totals.get('U', 0) + totals.get('D', 0) + totals.get('R', 0) + totals.get('J', 0)
    print(f"  ------------------------")
    print(f"  Total BOM parts: {total_parts}")
    print()

    # Breakdown by IC type
    ic_types = {}
    for name, builder in builders.items():
        multiplier = 8 if name == "byte" else 1
        for sym in builder.sch.schematicSymbols:
            if sym.properties and sym.properties[0].value.startswith("U") or \
               (len(sym.properties) > 0 and any(p.key == "Reference" and p.value.startswith("U") for p in sym.properties)):
                # Get the lib ID to identify IC type
                lib_id = sym.libId if hasattr(sym, 'libId') else sym.entryName
                if lib_id and lib_id.startswith("74LVC"):
                    ic_types[lib_id] = ic_types.get(lib_id, 0) + multiplier

    if ic_types:
        print("IC Breakdown:")
        for ic, count in sorted(ic_types.items()):
            print(f"  {ic}: {count}")

    print("\nDone! Open ram.kicad_sch in KiCad to view the design.")


if __name__ == "__main__":
    main()
