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
  byte.kicad_sch             -- 8 DFFs + 8 tri-state buffers (shared by all 8 byte instances)
"""

import copy
import json
import math
import os
import re
import subprocess
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

KICAD_CLI = r"C:\Program Files\KiCad\9.0\bin\kicad-cli.exe"

# --------------------------------------------------------------
# Constants
# --------------------------------------------------------------
PROJECT_NAME = "ram"
BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# Schematic grid (KiCad default = 1.27 mm, we use 2.54 multiples)
GRID = 2.54
SYM_SPACING_Y = 10 * GRID    # vertical spacing between symbol rows (gates)
DFF_SPACING_Y = 14 * GRID    # vertical spacing between DFF rows in byte sheet
LED_GAP_X = 3 * GRID         # gap from output pin to LED chain center
PWR_WIRE_LEN = GRID          # power symbol offset from pin
LABEL_STUB = GRID             # wire stub length for labels

# Map symbol base names to their KiCad library name prefix
SYMBOL_LIB_MAP = {
    "74LVC1G00": "74xGxx",
    "74LVC1G04": "74xGxx",
    "74LVC1G08": "74xGxx",
    "74LVC1G11": "74xGxx",
    "74LVC1G79": "74xGxx",
    "74LVC1G125": "74xGxx",
    "R_Small": "Device",
    "LED_Small": "Device",
    "C_Small": "Device",
    "VCC": "power",
    "GND": "power",
    "PWR_FLAG": "power",
    "Conn_01x14": "Connector_Generic",
}


def uid():
    """Generate a new UUID string."""
    return str(_uuid.uuid4())


def snap(v):
    """Round a coordinate to 2 decimal places to eliminate floating-point noise.

    KiCad internally converts mm to integer units (mils/nm).  Tiny FP errors
    like 83.82000000000001 vs 83.82 can cause wire-to-pin mismatches when
    KiCad parses them from the file independently.
    """
    return round(v, 2)


# --------------------------------------------------------------
# Library symbol loading -- each .kicad_sch embeds its own copy
# --------------------------------------------------------------

def load_lib_symbols():
    """Load symbol definitions from KiCad stock libraries."""
    symbols = {}

    kicad_sym_dir = r"C:\Program Files\KiCad\9.0\share\kicad\symbols"
    stock_libs = {
        "74xGxx.kicad_sym": [
            "74LVC1G00", "74LVC1G04", "74LVC1G08",
            "74LVC1G11", "74LVC1G79", "74LVC1G125",
        ],
        "Device.kicad_sym": ["R_Small", "LED_Small", "C_Small"],
        "power.kicad_sym": ["VCC", "GND", "PWR_FLAG"],
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
# Pin offset discovery via kicad-cli ERC
# --------------------------------------------------------------

PIN_OFFSETS = None  # lazy-loaded: {(sym_name, angle): {pin_num: (dx, dy)}}


def _run_erc_for_pins(sch_path):
    """Run kicad-cli ERC and parse pin positions from the JSON output.

    Returns {ref: {pin_num: (x_mm, y_mm)}}.
    ERC reports coordinates that need ×100 to convert to schematic mm.
    """
    erc_path = sch_path.replace(".kicad_sch", "_erc.json")
    subprocess.run(
        [KICAD_CLI, "sch", "erc", "--format", "json",
         "--severity-all", "--output", erc_path, sch_path],
        capture_output=True, text=True,
    )
    if not os.path.exists(erc_path):
        return {}

    with open(erc_path) as f:
        data = json.load(f)

    pins = {}
    pat = re.compile(r"Symbol (\S+) Pin (\d+)")
    for sheet in data.get("sheets", []):
        for v in sheet.get("violations", []):
            if v["type"] != "pin_not_connected":
                continue
            for item in v["items"]:
                m = pat.search(item["description"])
                if m:
                    ref, pin_num = m.group(1), m.group(2)
                    x_mm = round(item["pos"]["x"] * 100, 4)
                    y_mm = round(item["pos"]["y"] * 100, 4)
                    pins.setdefault(ref, {})[pin_num] = (x_mm, y_mm)

    # Clean up temp files
    for p in [erc_path, sch_path]:
        try:
            os.remove(p)
        except OSError:
            pass
    return pins


def discover_pin_offsets():
    """Discover pin position offsets for every (component, angle) we use.

    Places one component at a known origin in a temp schematic, runs
    kicad-cli ERC, and reads back absolute pin positions.  The offset is
    just (reported_pos − origin).  KiCad handles all Y-negation and
    rotation — we never compute it ourselves.

    Returns {(sym_name, angle): {pin_num: (dx, dy)}}
    """
    # Every (symbol, ref_prefix, angle) combination used in the design
    specs = [
        ("74LVC1G04", "U", 0),
        ("74LVC1G08", "U", 0),
        ("74LVC1G00", "U", 0),
        ("74LVC1G11", "U", 0),
        ("74LVC1G79", "U", 0),
        ("74LVC1G125", "U", 0),
        ("R_Small", "R", 90),
        ("LED_Small", "D", 180),
        ("Conn_01x14", "J", 0),
    ]
    origin = (100.0, 100.0)
    offsets = {}

    for sym_name, prefix, angle in specs:
        temp_path = os.path.join(BOARD_DIR, "_pin_probe.kicad_sch")

        # Build a minimal schematic with just one component
        b = _MinimalBuilder()
        b.place(sym_name, origin[0], origin[1], prefix, angle)
        b.save(temp_path)

        pin_map = _run_erc_for_pins(temp_path)
        ref = f"{prefix}1"
        if ref in pin_map:
            offsets[(sym_name, angle)] = {
                pin: (round(px - origin[0], 2), round(py - origin[1], 2))
                for pin, (px, py) in pin_map[ref].items()
            }
        else:
            print(f"  WARNING: no pins found for {sym_name} angle={angle}")

    return offsets


class _MinimalBuilder:
    """Tiny helper that creates a one-component schematic for pin probing."""

    def __init__(self):
        self.sch = Schematic.create_new()
        self.sch.version = 20250114
        self.sch.uuid = uid()
        self.sch.paper = PageSettings(paperSize="A4")
        self._embedded = set()

    def place(self, sym_name, x, y, prefix, angle):
        if sym_name not in self._embedded:
            sym_copy = copy.deepcopy(get_lib_symbols()[sym_name])
            lib_pfx = SYMBOL_LIB_MAP.get(sym_name, "")
            if lib_pfx:
                sym_copy.libId = f"{lib_pfx}:{sym_name}"
            self.sch.libSymbols.append(sym_copy)
            self._embedded.add(sym_name)

        s = SchematicSymbol()
        lib_pfx = SYMBOL_LIB_MAP.get(sym_name, "")
        s.libId = f"{lib_pfx}:{sym_name}" if lib_pfx else sym_name
        s.position = Position(X=x, Y=y, angle=angle)
        s.unit = 1
        s.inBom = True
        s.onBoard = True
        s.uuid = uid()
        ref = f"{prefix}1"
        s.properties = [
            Property(key="Reference", value=ref, id=0,
                     position=Position(X=x, Y=y - 5, angle=0),
                     effects=Effects(font=Font(width=1.27, height=1.27))),
            Property(key="Value", value=sym_name, id=1,
                     position=Position(X=x, Y=y + 5, angle=0),
                     effects=Effects(font=Font(width=1.27, height=1.27), hide=True)),
        ]
        s.instances.append(SymbolProjectInstance(
            name="probe",
            paths=[SymbolProjectPath(
                sheetInstancePath=f"/{self.sch.uuid}/",
                reference=ref, unit=1,
            )]
        ))
        self.sch.schematicSymbols.append(s)

    def save(self, path):
        self.sch.to_file(path)


def _fallback_pin_offsets(sym_name, angle):
    """Get pin offsets from library symbol when ERC probe fails.

    Library coordinates use Y-up; schematic uses Y-down.
    KiCad rotation convention is CW in schematic space.
    """
    lib_sym = get_lib_symbols()[sym_name]
    pins = {}
    for unit in lib_sym.units:
        for pin in unit.pins:
            lx, ly = pin.position.X, pin.position.Y
            rad = math.radians(angle)
            # Library Y-up -> Schematic Y-down, then CW rotation
            bx, by = lx, -ly
            dx = round(math.cos(rad) * bx + math.sin(rad) * by, 2)
            dy = round(-math.sin(rad) * bx + math.cos(rad) * by, 2)
            pins[pin.number] = (dx, dy)
    return pins


def get_pin_offsets():
    """Return cached pin offsets, discovering them on first call."""
    global PIN_OFFSETS
    if PIN_OFFSETS is None:
        print("Discovering pin offsets via kicad-cli ERC...")
        PIN_OFFSETS = discover_pin_offsets()
        print(f"  Discovered offsets for {len(PIN_OFFSETS)} component/angle combos")

        # Fallback for any symbols where ERC didn't find pins
        specs = [
            ("74LVC1G04", 0), ("74LVC1G08", 0), ("74LVC1G00", 0),
            ("74LVC1G11", 0), ("74LVC1G79", 0), ("74LVC1G125", 0),
            ("R_Small", 90), ("LED_Small", 180), ("Conn_01x14", 0),
        ]
        for sym_name, angle in specs:
            key = (sym_name, angle)
            if key not in PIN_OFFSETS:
                fallback = _fallback_pin_offsets(sym_name, angle)
                if fallback:
                    print(f"  Using library fallback for {sym_name} angle={angle}: {fallback}")
                    PIN_OFFSETS[key] = fallback
    return PIN_OFFSETS


# --------------------------------------------------------------
# Schematic builder helpers
# --------------------------------------------------------------

class SchematicBuilder:
    """Convenience wrapper around a kiutils Schematic for building sheets."""

    def __init__(self, title="", page_size="A3"):
        self.sch = Schematic.create_new()
        self.sch.version = 20250114  # KiCad 9 format (required for wire connectivity)
        self.sch.generator = "eeschema"
        self.sch.uuid = uid()
        self.sch.paper = PageSettings(paperSize=page_size)
        self._ref_counters = {}   # prefix -> next number
        self._embedded_symbols = set()  # track which lib symbols we've embedded
        self._pin_offsets = get_pin_offsets()

    # -- reference designator allocation --

    def _next_ref(self, prefix):
        n = self._ref_counters.get(prefix, 1)
        self._ref_counters[prefix] = n + 1
        return f"{prefix}{n}"

    # -- embed a library symbol definition (once per symbol type) --

    def _ensure_lib_symbol(self, sym_name):
        """Embed a library symbol into this schematic's libSymbols if not already there."""
        if sym_name in self._embedded_symbols:
            return
        all_syms = get_lib_symbols()
        if sym_name not in all_syms:
            raise ValueError(f"Symbol '{sym_name}' not found in libraries")
        sym_copy = copy.deepcopy(all_syms[sym_name])
        lib_prefix = SYMBOL_LIB_MAP.get(sym_name, "")
        if lib_prefix:
            sym_copy.libId = f"{lib_prefix}:{sym_name}"
        self.sch.libSymbols.append(sym_copy)
        self._embedded_symbols.add(sym_name)

    # -- place a component --

    def place_symbol(self, lib_name, x, y, ref_prefix="U", value=None,
                     angle=0, mirror=None, extra_props=None):
        """Place a symbol instance in the schematic.

        Returns (reference_designator, pins_dict) where pins_dict maps
        pin_number -> (schematic_x, schematic_y).

        Pin positions come from kicad-cli ERC probing (done once at startup),
        so no manual Y-negation or rotation math is needed.
        """
        x, y = snap(x), snap(y)
        self._ensure_lib_symbol(lib_name)
        ref = self._next_ref(ref_prefix)
        if value is None:
            value = lib_name

        sym = SchematicSymbol()
        lib_prefix = SYMBOL_LIB_MAP.get(lib_name, "")
        sym.libId = f"{lib_prefix}:{lib_name}" if lib_prefix else lib_name
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

        # Hide reference for power symbols (#PWR, #FLG) to reduce clutter
        hide_ref = ref_prefix.startswith("#")

        sym.properties = [
            Property(key="Reference", value=ref, id=0,
                     position=Position(X=x, Y=y - 2 * GRID, angle=0),
                     effects=Effects(font=Font(width=1.27, height=1.27), hide=hide_ref)),
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

        # Pin UUIDs — required for KiCad 9 wire connectivity
        key = (lib_name, angle)
        if key in self._pin_offsets:
            sym.pins = {pin: uid() for pin in self._pin_offsets[key]}
        else:
            # For power symbols etc., get pins from the library symbol units
            all_syms = get_lib_symbols()
            if lib_name in all_syms:
                lib_sym = all_syms[lib_name]
                for unit in lib_sym.units:
                    for p in unit.pins:
                        sym.pins[p.number] = uid()

        # Instance data
        sym.instances.append(SymbolProjectInstance(
            name=PROJECT_NAME,
            paths=[SymbolProjectPath(
                sheetInstancePath=f"/{self.sch.uuid}/",
                reference=ref,
                unit=1,
            )]
        ))

        self.sch.schematicSymbols.append(sym)

        # Look up pin positions from pre-discovered offsets
        if key in self._pin_offsets:
            pins = {pin: (snap(x + dx), snap(y + dy))
                    for pin, (dx, dy) in self._pin_offsets[key].items()}
        else:
            pins = {}
        return ref, pins

    # -- power wiring helpers --

    def wire_power(self, power_name, pin_pos, offset_x=0, offset_y=0, angle=0):
        """Place power symbol near pin and wire to it."""
        px, py = pin_pos
        self.place_power(power_name, px + offset_x, py + offset_y, angle=angle)
        self.add_wire(px, py, px + offset_x, py + offset_y)

    def connect_power(self, pins, vcc_pin="5", gnd_pin="3"):
        """Connect IC power pins by placing VCC/GND symbols directly at pin positions.

        No wires needed — the power symbol's pin overlaps the IC's power pin
        at the exact same position, creating a direct connection.
        This avoids wire overlap issues between adjacent ICs.
        """
        self.place_power("VCC", *pins[vcc_pin])
        self.place_power("GND", *pins[gnd_pin])

    # -- place an LED + resistor pair (indicator for a gate output) --

    def place_led_indicator(self, x, y):
        """Place R + LED + GND chain at (x, y). Signal enters from left.

        Layout (left to right): signal → R Pin 1 → R Pin 2 → LED Pin 2 → LED Pin 1 → GND

        R_Small at angle=90:   Pin 1 at LEFT (dx=-2.54), Pin 2 at RIGHT (dx=+2.54)
        LED_Small at angle=180: Pin 2/Anode at LEFT (dx=-2.54), Pin 1/Cathode at RIGHT (dx=+2.54)

        Components are spaced apart so wires don't pass through symbol bodies.
        """
        _, r_pins = self.place_symbol("R_Small", x, y, ref_prefix="R",
                                      value="680R", angle=90)

        # LED placed 3 grid units right of R center — enough gap to avoid overlap
        led_x = x + 3 * GRID
        _, led_pins = self.place_symbol("LED_Small", led_x, y, ref_prefix="D",
                                        value="Red", angle=180)
        # Wire from R Pin 2 (right) to LED Pin 2 / Anode (left)
        self.add_wire(*r_pins["2"], *led_pins["2"])

        # GND below LED cathode (Pin 1, right side)
        self.wire_power("GND", led_pins["1"], offset_y=2 * GRID)

        # Signal enters at R Pin 1 (left side)
        return r_pins["1"]

    # -- net labels --

    def add_label(self, text, x, y, angle=0):
        """Add a local net label."""
        x, y = snap(x), snap(y)
        label = LocalLabel()
        label.text = text
        label.position = Position(X=x, Y=y, angle=angle)
        label.effects = Effects(font=Font(width=1.27, height=1.27))
        label.uuid = uid()
        self.sch.labels.append(label)
        return label

    def add_global_label(self, text, x, y, shape="bidirectional", angle=0):
        """Add a global net label."""
        x, y = snap(x), snap(y)
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
        x, y = snap(x), snap(y)
        label = HierarchicalLabel()
        label.text = text
        label.shape = shape
        label.position = Position(X=x, Y=y, angle=angle)
        label.effects = Effects(font=Font(width=1.27, height=1.27))
        label.uuid = uid()
        self.sch.hierarchicalLabels.append(label)
        return label

    # -- wires --

    def add_wire(self, x1, y1, x2, y2):
        """Add a wire between two points."""
        x1, y1, x2, y2 = snap(x1), snap(y1), snap(x2), snap(y2)
        conn = Connection()
        conn.type = "wire"
        conn.points = [Position(X=x1, Y=y1), Position(X=x2, Y=y2)]
        conn.uuid = uid()
        self.sch.graphicalItems.append(conn)
        return conn

    # -- power symbols --

    def place_power(self, symbol_name, x, y, angle=0):
        """Place a power symbol (VCC, GND, PWR_FLAG).

        Power symbols have their single pin at the component origin,
        so we always return pin "1" at (x, y) regardless of angle.
        """
        prefix = "#FLG" if symbol_name == "PWR_FLAG" else "#PWR"
        ref, _ = self.place_symbol(symbol_name, x, y, ref_prefix=prefix,
                                   value=symbol_name, angle=angle)
        return ref, {"1": (x, y)}

    # -- save --

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

    # Hierarchical labels for inputs -- with wire stubs connecting to local labels
    for i in range(3):
        hl_y = base_y + i * SYM_SPACING_Y
        b.add_hier_label(f"A{i}", base_x, hl_y, shape="input", angle=180)
        b.add_wire(base_x, hl_y, base_x + LABEL_STUB, hl_y)
        b.add_label(f"A{i}", base_x + LABEL_STUB, hl_y)

    # Three inverters for complemented address bits
    inv_x = base_x + 12 * GRID
    for i in range(3):
        y = base_y + i * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G04", inv_x, y)
        b.connect_power(pins)

        # Input: wire stub + label
        in_pin = pins["2"]
        b.add_wire(*in_pin, in_pin[0] - LABEL_STUB, in_pin[1])
        b.add_label(f"A{i}", in_pin[0] - LABEL_STUB, in_pin[1])

        # Output: wire to LED chain + label
        out_pin = pins["4"]
        led_in = b.place_led_indicator(out_pin[0] + LED_GAP_X, out_pin[1])
        b.add_wire(*out_pin, *led_in)
        b.add_label(f"A{i}_INV", *out_pin)

    # 8 three-input AND gates for decode
    and_x = base_x + 35 * GRID
    decode_table = [
        (0, 0, 0), (0, 0, 1), (0, 1, 0), (0, 1, 1),
        (1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1),
    ]

    for sel_idx, (a2, a1, a0) in enumerate(decode_table):
        y = base_y + sel_idx * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G11", and_x, y)
        b.connect_power(pins, gnd_pin="2")

        # Input wire stubs + labels
        # Pin 6 = top input (A2), Pin 1 = middle input (A1), Pin 3 = bottom input (A0)
        a2_net = "A2" if a2 else "A2_INV"
        a2_pin = pins["6"]
        b.add_wire(*a2_pin, a2_pin[0] - LABEL_STUB, a2_pin[1])
        b.add_label(a2_net, a2_pin[0] - LABEL_STUB, a2_pin[1])

        a1_net = "A1" if a1 else "A1_INV"
        a1_pin = pins["1"]
        b.add_wire(*a1_pin, a1_pin[0] - LABEL_STUB, a1_pin[1])
        b.add_label(a1_net, a1_pin[0] - LABEL_STUB, a1_pin[1])

        a0_net = "A0" if a0 else "A0_INV"
        a0_pin = pins["3"]
        b.add_wire(*a0_pin, a0_pin[0] - LABEL_STUB, a0_pin[1])
        b.add_label(a0_net, a0_pin[0] - LABEL_STUB, a0_pin[1])

        # Output: wire to LED + label
        out_pin = pins["4"]
        sel_net = f"SEL{sel_idx}"
        led_in = b.place_led_indicator(out_pin[0] + LED_GAP_X, out_pin[1])
        b.add_wire(*out_pin, *led_in)
        b.add_label(sel_net, *out_pin)

    # Hierarchical labels for outputs -- with wire stubs connecting to local labels
    hl_out_x = and_x + 25 * GRID
    for i in range(8):
        hl_y = base_y + i * SYM_SPACING_Y
        b.add_hier_label(f"SEL{i}", hl_out_x, hl_y, shape="output")
        b.add_wire(hl_out_x, hl_y, hl_out_x - LABEL_STUB, hl_y)
        b.add_label(f"SEL{i}", hl_out_x - LABEL_STUB, hl_y)

    # PWR_FLAG symbols on VCC and GND nets to satisfy ERC "power pin not driven"
    # Place at an open area, connected to power net via short wire
    pwr_flag_x = hl_out_x + 10 * GRID
    pwr_flag_y = base_y
    # VCC PWR_FLAG: VCC symbol + PWR_FLAG at same point
    _, vcc_pins = b.place_power("VCC", pwr_flag_x, pwr_flag_y)
    b.place_power("PWR_FLAG", pwr_flag_x, pwr_flag_y)
    # GND PWR_FLAG: GND symbol + PWR_FLAG at same point
    _, gnd_pins = b.place_power("GND", pwr_flag_x + 10 * GRID, pwr_flag_y)
    b.place_power("PWR_FLAG", pwr_flag_x + 10 * GRID, pwr_flag_y)

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
      1x 74LVC1G08 AND(CE_AND_OE, /WE) -> READ_EN
    """
    b = SchematicBuilder(title="Control Logic", page_size="A3")
    base_x, base_y = 25.4, 25.4

    # Hierarchical labels for inputs -- with wire stubs
    ctrl_signals = ["nCE", "nOE", "nWE"]
    for i, sig in enumerate(ctrl_signals):
        hl_y = base_y + i * SYM_SPACING_Y
        b.add_hier_label(sig, base_x, hl_y, shape="input", angle=180)
        b.add_wire(base_x, hl_y, base_x + LABEL_STUB, hl_y)
        b.add_label(sig, base_x + LABEL_STUB, hl_y)

    # Three inverters
    inv_x = base_x + 12 * GRID
    active_names = ["CE", "OE", "WE"]
    for i, (ctrl, active) in enumerate(zip(ctrl_signals, active_names)):
        y = base_y + i * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G04", inv_x, y)
        b.connect_power(pins)

        # Input: wire stub + label
        in_pin = pins["2"]
        b.add_wire(*in_pin, in_pin[0] - LABEL_STUB, in_pin[1])
        b.add_label(ctrl, in_pin[0] - LABEL_STUB, in_pin[1])

        # Output: wire to LED + label
        out_pin = pins["4"]
        led_in = b.place_led_indicator(out_pin[0] + LED_GAP_X, out_pin[1])
        b.add_wire(*out_pin, *led_in)
        b.add_label(active, *out_pin)

    # AND1: CE & WE -> WRITE_ACTIVE
    and1_x = base_x + 35 * GRID
    and1_y = base_y + 5 * GRID
    _, pins = b.place_symbol("74LVC1G08", and1_x, and1_y)
    b.connect_power(pins)

    in_a = pins["1"]
    b.add_wire(*in_a, in_a[0] - LABEL_STUB, in_a[1])
    b.add_label("CE", in_a[0] - LABEL_STUB, in_a[1])
    in_b = pins["2"]
    b.add_wire(*in_b, in_b[0] - LABEL_STUB, in_b[1])
    b.add_label("WE", in_b[0] - LABEL_STUB, in_b[1])

    out_pin = pins["4"]
    led_in = b.place_led_indicator(out_pin[0] + LED_GAP_X, out_pin[1])
    b.add_wire(*out_pin, *led_in)
    b.add_label("WRITE_ACTIVE", *out_pin)

    # AND2: CE & OE -> CE_AND_OE
    and2_x = base_x + 35 * GRID
    and2_y = base_y + 15 * GRID
    _, pins = b.place_symbol("74LVC1G08", and2_x, and2_y)
    b.connect_power(pins)

    in_a = pins["1"]
    b.add_wire(*in_a, in_a[0] - LABEL_STUB, in_a[1])
    b.add_label("CE", in_a[0] - LABEL_STUB, in_a[1])
    in_b = pins["2"]
    b.add_wire(*in_b, in_b[0] - LABEL_STUB, in_b[1])
    b.add_label("OE", in_b[0] - LABEL_STUB, in_b[1])

    out_pin = pins["4"]
    led_in = b.place_led_indicator(out_pin[0] + LED_GAP_X, out_pin[1])
    b.add_wire(*out_pin, *led_in)
    b.add_label("CE_AND_OE", *out_pin)

    # AND3: CE_AND_OE & /WE -> READ_EN
    and3_x = base_x + 55 * GRID
    and3_y = base_y + 15 * GRID
    _, pins = b.place_symbol("74LVC1G08", and3_x, and3_y)
    b.connect_power(pins)

    in_a = pins["1"]
    b.add_wire(*in_a, in_a[0] - LABEL_STUB, in_a[1])
    b.add_label("CE_AND_OE", in_a[0] - LABEL_STUB, in_a[1])
    in_b = pins["2"]
    b.add_wire(*in_b, in_b[0] - LABEL_STUB, in_b[1])
    b.add_label("nWE", in_b[0] - LABEL_STUB, in_b[1])

    out_pin = pins["4"]
    led_in = b.place_led_indicator(out_pin[0] + LED_GAP_X, out_pin[1])
    b.add_wire(*out_pin, *led_in)
    b.add_label("READ_EN", *out_pin)

    # Hierarchical labels for outputs -- with wire stubs
    out_label_x = and3_x + 25 * GRID
    wa_y = base_y + 5 * GRID
    b.add_hier_label("WRITE_ACTIVE", out_label_x, wa_y, shape="output")
    b.add_wire(out_label_x, wa_y, out_label_x - LABEL_STUB, wa_y)
    b.add_label("WRITE_ACTIVE", out_label_x - LABEL_STUB, wa_y)

    re_y = base_y + 15 * GRID
    b.add_hier_label("READ_EN", out_label_x, re_y, shape="output")
    b.add_wire(out_label_x, re_y, out_label_x - LABEL_STUB, re_y)
    b.add_label("READ_EN", out_label_x - LABEL_STUB, re_y)

    return b


def _generate_nand_bank(title, enable_signal, output_prefix):
    """Shared generator for write_clk_gen and read_oe_gen (8 NANDs each).

    74LVC1G00 has same pin layout as 74LVC1G08.
    """
    b = SchematicBuilder(title=title, page_size="A3")
    base_x, base_y = 25.4, 25.4

    # Input hier labels -- with wire stubs
    b.add_hier_label(enable_signal, base_x, base_y, shape="input", angle=180)
    b.add_wire(base_x, base_y, base_x + LABEL_STUB, base_y)
    b.add_label(enable_signal, base_x + LABEL_STUB, base_y)

    for i in range(8):
        hl_y = base_y + (i + 1) * SYM_SPACING_Y
        b.add_hier_label(f"SEL{i}", base_x, hl_y, shape="input", angle=180)
        b.add_wire(base_x, hl_y, base_x + LABEL_STUB, hl_y)
        b.add_label(f"SEL{i}", base_x + LABEL_STUB, hl_y)

    # 8 NAND gates
    nand_x = base_x + 18 * GRID
    for i in range(8):
        y = base_y + (i + 1) * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G00", nand_x, y)
        b.connect_power(pins)

        # Input wire stubs + labels
        in_a = pins["1"]
        b.add_wire(*in_a, in_a[0] - LABEL_STUB, in_a[1])
        b.add_label(enable_signal, in_a[0] - LABEL_STUB, in_a[1])

        in_b = pins["2"]
        b.add_wire(*in_b, in_b[0] - LABEL_STUB, in_b[1])
        b.add_label(f"SEL{i}", in_b[0] - LABEL_STUB, in_b[1])

        # Output: wire to LED + label
        out_net = f"{output_prefix}{i}"
        out_pin = pins["4"]
        led_in = b.place_led_indicator(out_pin[0] + LED_GAP_X, out_pin[1])
        b.add_wire(*out_pin, *led_in)
        b.add_label(out_net, *out_pin)

    # Output hier labels -- with wire stubs
    hl_out_x = nand_x + 28 * GRID
    for i in range(8):
        hl_y = base_y + (i + 1) * SYM_SPACING_Y
        out_net = f"{output_prefix}{i}"
        b.add_hier_label(out_net, hl_out_x, hl_y, shape="output")
        b.add_wire(hl_out_x, hl_y, hl_out_x - LABEL_STUB, hl_y)
        b.add_label(out_net, hl_out_x - LABEL_STUB, hl_y)

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

    # Hierarchical labels -- with wire stubs connecting to local labels
    for bit in range(8):
        hl_y = base_y + bit * DFF_SPACING_Y
        b.add_hier_label(f"D{bit}", base_x, hl_y, shape="bidirectional", angle=180)
        b.add_wire(base_x, hl_y, base_x + LABEL_STUB, hl_y)
        b.add_label(f"D{bit}", base_x + LABEL_STUB, hl_y)

    # Place WRITE_CLK and BUF_OE hier labels below all 8 bits
    # (bit 7 is at base_y + 7*DFF_SPACING_Y = base_y + 98*GRID, so start at 8*DFF_SPACING_Y)
    wclk_y = base_y + 8 * DFF_SPACING_Y + 4 * GRID
    b.add_hier_label("WRITE_CLK", base_x, wclk_y, shape="input", angle=180)
    b.add_wire(base_x, wclk_y, base_x + LABEL_STUB, wclk_y)
    b.add_label("WRITE_CLK", base_x + LABEL_STUB, wclk_y)

    boe_y = wclk_y + 2 * GRID
    b.add_hier_label("BUF_OE", base_x, boe_y, shape="input", angle=180)
    b.add_wire(base_x, boe_y, base_x + LABEL_STUB, boe_y)
    b.add_label("BUF_OE", base_x + LABEL_STUB, boe_y)

    dff_x = base_x + 15 * GRID
    buf_x = base_x + 50 * GRID

    for bit in range(8):
        y = base_y + bit * DFF_SPACING_Y
        q_net = f"Q_{bit}"

        # D flip-flop (74LVC1G79)
        _, dff_pins = b.place_symbol("74LVC1G79", dff_x, y)
        b.connect_power(dff_pins)

        # D input: wire stub + label
        d_pin = dff_pins["1"]
        b.add_wire(*d_pin, d_pin[0] - LABEL_STUB, d_pin[1])
        b.add_label(f"D{bit}", d_pin[0] - LABEL_STUB, d_pin[1])

        # CLK input: wire stub + label
        clk_pin = dff_pins["2"]
        b.add_wire(*clk_pin, clk_pin[0] - LABEL_STUB, clk_pin[1])
        b.add_label("WRITE_CLK", clk_pin[0] - LABEL_STUB, clk_pin[1])

        # Q output: wire to LED chain + label
        q_pin = dff_pins["4"]
        led_in = b.place_led_indicator(q_pin[0] + LED_GAP_X, q_pin[1])
        b.add_wire(*q_pin, *led_in)
        b.add_label(q_net, *q_pin)

        # Tri-state buffer (74LVC1G125)
        _, buf_pins = b.place_symbol("74LVC1G125", buf_x, y)
        b.connect_power(buf_pins)

        # A input: wire stub + label
        a_pin = buf_pins["2"]
        b.add_wire(*a_pin, a_pin[0] - LABEL_STUB, a_pin[1])
        b.add_label(q_net, a_pin[0] - LABEL_STUB, a_pin[1])

        # /OE input: wire stub going right + label (since /OE is above IC)
        oe_pin = buf_pins["1"]
        b.add_wire(*oe_pin, oe_pin[0] + LABEL_STUB, oe_pin[1])
        b.add_label("BUF_OE", oe_pin[0] + LABEL_STUB, oe_pin[1])

        # Y output: wire to LED + label back to data bus
        y_pin = buf_pins["4"]
        led_in = b.place_led_indicator(y_pin[0] + LED_GAP_X, y_pin[1])
        b.add_wire(*y_pin, *led_in)
        b.add_label(f"D{bit}", *y_pin)

    return b


# --------------------------------------------------------------
# Root sheet generator
# --------------------------------------------------------------

def generate_root_sheet():
    """
    Root sheet: connector, bus indicator LEDs, and hierarchical sheet references.

    Layout (left to right):
      1. Connector J1 + bus indicator LEDs
      2. Control hierarchy sheets (address decoder, control logic, write clk, read OE)
      3. Byte hierarchy sheets (2 columns of 4)
    """
    b = SchematicBuilder(title="8-Byte Discrete RAM", page_size="A2")
    base_x, base_y = 25.4, 25.4

    # -- External connector --
    conn_x = base_x
    conn_y = base_y + 5 * GRID
    _, conn_pins = b.place_symbol("Conn_01x14", conn_x, conn_y,
                                  ref_prefix="J", value="SRAM_Bus")

    # Wire connector pins to local labels
    signal_names = [
        "A0", "A1", "A2",
        "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7",
        "nCE", "nOE", "nWE",
    ]

    for pin_num_int, sig in enumerate(signal_names, start=1):
        pin_num = str(pin_num_int)
        pin_pos = conn_pins[pin_num]
        px, py = pin_pos
        label_x = px - LABEL_STUB
        b.add_wire(px, py, label_x, py)
        b.add_label(sig, label_x, py, angle=180)

    # -- Bus indicator LEDs (to the right of connector, grouped) --
    led_base_x = base_x + 25 * GRID
    led_y = base_y

    # Group label
    # Address LEDs (A0-A2)
    for i in range(3):
        y = led_y + i * 3 * GRID
        led_in = b.place_led_indicator(led_base_x, y)
        b.add_wire(*led_in, led_in[0] - LABEL_STUB, led_in[1])
        b.add_label(f"A{i}", led_in[0] - LABEL_STUB, led_in[1], angle=180)

    # Data bus LEDs (D0-D7) -- gap after address
    data_led_start = led_y + 3 * 3 * GRID + 2 * GRID
    for i in range(8):
        y = data_led_start + i * 3 * GRID
        led_in = b.place_led_indicator(led_base_x, y)
        b.add_wire(*led_in, led_in[0] - LABEL_STUB, led_in[1])
        b.add_label(f"D{i}", led_in[0] - LABEL_STUB, led_in[1], angle=180)

    # Control signal LEDs (nCE, nOE, nWE) -- gap after data
    ctrl_led_start = data_led_start + 8 * 3 * GRID + 2 * GRID
    ctrl_names = ["nCE", "nOE", "nWE"]
    for i, name in enumerate(ctrl_names):
        y = ctrl_led_start + i * 3 * GRID
        led_in = b.place_led_indicator(led_base_x, y)
        b.add_wire(*led_in, led_in[0] - LABEL_STUB, led_in[1])
        b.add_label(name, led_in[0] - LABEL_STUB, led_in[1], angle=180)

    # -- Hierarchical sheet references --
    # Column 1: control sheets (variable height based on pin count)
    ctrl_sheet_x = base_x + 60 * GRID
    ctrl_sheet_w = 28 * GRID
    sheet_gap = 5 * GRID
    wire_stub = 5.08

    def _sheet_height(num_pins):
        """Calculate sheet height to fit pins with 2.54mm spacing + margin."""
        return snap(num_pins * 2.54 + 5.08)

    def add_sheet_ref(name, filename, pins, sx, sy, sw, sh, fill_color):
        """Add a hierarchical sheet rectangle with pins."""
        sheet = HierarchicalSheet()
        sheet.position = Position(X=sx, Y=sy)
        sheet.width = sw
        sheet.height = sh
        sheet.stroke = Stroke(width=0.1)
        sheet.fill = fill_color
        sheet.uuid = uid()
        sheet.sheetName = Property(
            key="Sheet name", value=name, id=0,
            position=Position(X=sx, Y=sy - 1.27, angle=0),
            effects=Effects(font=Font(width=1.27, height=1.27)),
        )
        sheet.fileName = Property(
            key="Sheet file", value=filename, id=1,
            position=Position(X=sx + sw, Y=sy + sh + 1.27, angle=0),
            effects=Effects(font=Font(width=1.27, height=1.27)),
        )

        # Add hierarchical pins along left edge
        for pin_idx, (pin_name, pin_type) in enumerate(pins):
            pin = HierarchicalPin()
            pin.name = pin_name
            pin.connectionType = pin_type
            pin.position = Position(X=sx,
                                    Y=sy + 2.54 + pin_idx * 2.54, angle=180)
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

        # Wire + label for each pin
        for pin_idx, (pin_name, pin_type) in enumerate(pins):
            pin_y = sy + 2.54 + pin_idx * 2.54
            b.add_wire(sx, pin_y, sx - wire_stub, pin_y)
            b.add_label(pin_name, sx - wire_stub, pin_y, angle=0)

    yellow_fill = ColorRGBA(R=255, G=255, B=225, A=255, precision=4)
    green_fill = ColorRGBA(R=225, G=255, B=225, A=255, precision=4)

    # Address decoder (11 pins)
    addr_pins = [("A0", "input"), ("A1", "input"), ("A2", "input")]
    addr_pins += [(f"SEL{i}", "output") for i in range(8)]
    addr_h = _sheet_height(len(addr_pins))
    y_cursor = base_y
    add_sheet_ref("Address Decoder", "address_decoder.kicad_sch",
                  addr_pins, ctrl_sheet_x, y_cursor, ctrl_sheet_w, addr_h, yellow_fill)

    # Control logic (5 pins)
    y_cursor += addr_h + sheet_gap
    ctrl_pins = [("nCE", "input"), ("nOE", "input"), ("nWE", "input"),
                 ("WRITE_ACTIVE", "output"), ("READ_EN", "output")]
    ctrl_h = _sheet_height(len(ctrl_pins))
    add_sheet_ref("Control Logic", "control_logic.kicad_sch",
                  ctrl_pins, ctrl_sheet_x, y_cursor, ctrl_sheet_w, ctrl_h, yellow_fill)

    # Write clock gen (17 pins)
    y_cursor += ctrl_h + sheet_gap
    wclk_pins = [("WRITE_ACTIVE", "input")]
    wclk_pins += [(f"SEL{i}", "input") for i in range(8)]
    wclk_pins += [(f"WRITE_CLK_{i}", "output") for i in range(8)]
    wclk_h = _sheet_height(len(wclk_pins))
    add_sheet_ref("Write Clk Gen", "write_clk_gen.kicad_sch",
                  wclk_pins, ctrl_sheet_x, y_cursor, ctrl_sheet_w, wclk_h, yellow_fill)

    # Read OE gen (17 pins)
    y_cursor += wclk_h + sheet_gap
    roe_pins = [("READ_EN", "input")]
    roe_pins += [(f"SEL{i}", "input") for i in range(8)]
    roe_pins += [(f"BUF_OE_{i}", "output") for i in range(8)]
    roe_h = _sheet_height(len(roe_pins))
    add_sheet_ref("Read OE Gen", "read_oe_gen.kicad_sch",
                  roe_pins, ctrl_sheet_x, y_cursor, ctrl_sheet_w, roe_h, yellow_fill)

    # -- Byte sheets: 2 columns of 4 --
    byte_pin_defs = [("WRITE_CLK", "input"), ("BUF_OE", "input")]
    byte_pin_defs += [(f"D{bit}", "bidirectional") for bit in range(8)]
    byte_h = _sheet_height(len(byte_pin_defs))
    byte_w = 22 * GRID
    byte_col1_x = ctrl_sheet_x + ctrl_sheet_w + 18 * GRID
    byte_col2_x = byte_col1_x + byte_w + 18 * GRID

    for byte_idx in range(8):
        col = byte_idx // 4
        row = byte_idx % 4
        sx = byte_col1_x if col == 0 else byte_col2_x
        sy = base_y + row * (byte_h + sheet_gap)

        sheet = HierarchicalSheet()
        sheet.position = Position(X=sx, Y=sy)
        sheet.width = byte_w
        sheet.height = byte_h
        sheet.stroke = Stroke(width=0.1)
        sheet.fill = green_fill
        sheet.uuid = uid()
        sheet.sheetName = Property(
            key="Sheet name", value=f"Byte {byte_idx}", id=0,
            position=Position(X=sx, Y=sy - 1.27, angle=0),
            effects=Effects(font=Font(width=1.27, height=1.27)),
        )
        sheet.fileName = Property(
            key="Sheet file", value="byte.kicad_sch", id=1,
            position=Position(X=sx + byte_w, Y=sy + byte_h + 1.27, angle=0),
            effects=Effects(font=Font(width=1.27, height=1.27)),
        )

        for pin_idx, (pin_name, pin_type) in enumerate(byte_pin_defs):
            pin = HierarchicalPin()
            pin.name = pin_name
            pin.connectionType = pin_type
            pin.position = Position(X=sx,
                                    Y=sy + 2.54 + pin_idx * 2.54, angle=180)
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

        # Wire + labels (map generic pin names to instance-specific nets)
        for pin_idx, (pin_name, pin_type) in enumerate(byte_pin_defs):
            pin_y = sy + 2.54 + pin_idx * 2.54
            if pin_name == "WRITE_CLK":
                net_name = f"WRITE_CLK_{byte_idx}"
            elif pin_name == "BUF_OE":
                net_name = f"BUF_OE_{byte_idx}"
            else:
                net_name = pin_name
            b.add_wire(sx, pin_y, sx - wire_stub, pin_y)
            b.add_label(net_name, sx - wire_stub, pin_y, angle=0)

    return b


# --------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------

def count_components(builders):
    """Count total ICs, LEDs, resistors across all sheets."""
    totals = {"U": 0, "D": 0, "R": 0, "C": 0, "#PWR": 0, "J": 0, "#FLG": 0}
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


def fix_instance_paths(builders):
    """Fix sub-sheet symbol instance paths and assign globally unique references.

    KiCad requires:
    1. Symbol instances in child sheets use the full hierarchical path from root:
       /{root_uuid}/{sheet_block_uuid} — NOT the child sheet's own UUID.
    2. ALL reference designators are globally unique across the entire design.

    This function processes sheets in hierarchy order, maintaining a global counter
    per reference prefix (U, R, D, #PWR, #FLG) so every symbol gets a unique ref.
    """
    root_sch = builders["ram"].sch
    root_uuid = root_sch.uuid

    # Global reference counters — start after root sheet's refs
    global_counters = {}
    for sym in root_sch.schematicSymbols:
        for p in sym.properties:
            if p.key == "Reference":
                prefix = p.value.rstrip("0123456789")
                num_str = p.value[len(prefix):]
                num = int(num_str) if num_str else 0
                global_counters[prefix] = max(global_counters.get(prefix, 0), num)
                break

    # Process each hierarchy sheet in order (determines reference assignment order)
    for sheet in root_sch.sheets:
        fname = sheet.fileName.value
        sheet_block_uuid = sheet.uuid
        hier_path = f"/{root_uuid}/{sheet_block_uuid}"

        # Find the builder for this filename
        builder_name = fname.replace(".kicad_sch", "")
        if builder_name not in builders:
            continue
        builder = builders[builder_name]

        # Count how many of each prefix this sheet has (for multi-instance offset)
        sheet_ref_counts = {}
        for sym in builder.sch.schematicSymbols:
            for p in sym.properties:
                if p.key == "Reference":
                    prefix = p.value.rstrip("0123456789")
                    sheet_ref_counts[prefix] = sheet_ref_counts.get(prefix, 0) + 1
                    break

        # Check if this is a multi-instance sheet (multiple hierarchy blocks
        # reference the same filename). If so, only process on the FIRST one
        # and build all instance paths at once.
        all_sheet_blocks = [s for s in root_sch.sheets if s.fileName.value == fname]
        is_first_instance = (sheet is all_sheet_blocks[0])
        is_multi_instance = len(all_sheet_blocks) > 1

        if is_multi_instance and not is_first_instance:
            continue  # Already handled on first encounter

        if is_multi_instance:
            # Multi-instance sheet (byte.kicad_sch used 8 times)
            # Assign a block of refs for ALL instances at once
            for sym in builder.sch.schematicSymbols:
                prefix = ""
                base_num = 0
                for p in sym.properties:
                    if p.key == "Reference":
                        prefix = p.value.rstrip("0123456789")
                        base_num = int(p.value[len(prefix):])
                        break

                new_paths = []
                for inst_block in all_sheet_blocks:
                    inst_path = f"/{root_uuid}/{inst_block.uuid}"
                    # Allocate next global ref for this prefix
                    global_counters[prefix] = global_counters.get(prefix, 0) + 1
                    inst_ref = f"{prefix}{global_counters[prefix]}"
                    new_paths.append(SymbolProjectPath(
                        sheetInstancePath=inst_path,
                        reference=inst_ref,
                        unit=1,
                    ))

                sym.instances = [SymbolProjectInstance(
                    name=PROJECT_NAME,
                    paths=new_paths,
                )]
        else:
            # Single-instance sub-sheet: fix path and assign unique refs
            for sym in builder.sch.schematicSymbols:
                prefix = ""
                for p in sym.properties:
                    if p.key == "Reference":
                        prefix = p.value.rstrip("0123456789")
                        break

                global_counters[prefix] = global_counters.get(prefix, 0) + 1
                new_ref = f"{prefix}{global_counters[prefix]}"

                sym.instances = [SymbolProjectInstance(
                    name=PROJECT_NAME,
                    paths=[SymbolProjectPath(
                        sheetInstancePath=hier_path,
                        reference=new_ref,
                        unit=1,
                    )]
                )]


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

    # Fix sub-sheet symbol instance paths to use correct hierarchical paths
    fix_instance_paths(builders)
    print("  [*] Fixed hierarchical instance paths")

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
                if lib_id:
                    # Strip library prefix if present (e.g. "74xGxx:74LVC1G08" -> "74LVC1G08")
                    base_id = lib_id.split(":")[-1] if ":" in lib_id else lib_id
                    if base_id.startswith("74LVC"):
                        ic_types[base_id] = ic_types.get(base_id, 0) + multiplier

    if ic_types:
        print("IC Breakdown:")
        for ic, count in sorted(ic_types.items()):
            print(f"  {ic}: {count}")

    print("\nDone! Open ram.kicad_sch in KiCad to view the design.")


if __name__ == "__main__":
    main()
