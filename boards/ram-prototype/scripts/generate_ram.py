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
    Junction,
)
from kiutils.items.common import (
    Position, Property, Effects, Font, Justify, Stroke, Fill, ColorRGBA,
    PageSettings,
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
    "Conn_01x16": "Connector_Generic",
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
        "Connector_Generic.kicad_sym": ["Conn_01x16"],
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
        ("Conn_01x16", "J", 0),
        ("Conn_01x16", "J", 180),
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
            ("R_Small", 90), ("LED_Small", 180), ("Conn_01x16", 0),
            ("Conn_01x16", 180),
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
        # Hide pin names on LED symbols (they show "K" and "A" text on schematic)
        if sym_name in ("LED_Small",):
            sym_copy.pinNamesHide = True
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

        # Hide reference for power symbols (#PWR, #FLG) and passive LED chain
        # components (R, D) to reduce clutter — their sequential numbers aren't
        # helpful.  IC refs (U##) and connector (J##) remain visible.
        hide_ref = ref_prefix.startswith("#") or ref_prefix in ("R", "D")

        sym.properties = [
            Property(key="Reference", value=ref, id=0,
                     position=Position(X=x, Y=y - 5 * GRID, angle=0),
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
        r_x = x + GRID  # shift R center right so pin 1 (at dx=-2.54) lands at x
        _, r_pins = self.place_symbol("R_Small", r_x, y, ref_prefix="R",
                                      value="680R", angle=90)

        # LED placed 3 grid units right of R center — enough gap to avoid overlap
        led_x = r_x + 3 * GRID
        _, led_pins = self.place_symbol("LED_Small", led_x, y, ref_prefix="D",
                                        value="Red", angle=180)
        # Wire from R Pin 2 (right) to LED Pin 2 / Anode (left)
        self.add_wire(*r_pins["2"], *led_pins["2"])

        # GND below LED cathode (Pin 1, right side)
        self.wire_power("GND", led_pins["1"], offset_y=2 * GRID)

        # Signal enters at R Pin 1 (left side)
        return r_pins["1"]

    # -- net labels --

    def _label_effects(self, justify=None):
        """Build Effects for a label, optionally with justify."""
        if justify:
            return Effects(font=Font(width=1.27, height=1.27),
                           justify=Justify(horizontally=justify))
        return Effects(font=Font(width=1.27, height=1.27))

    def add_label(self, text, x, y, angle=0, justify=None):
        """Add a local net label."""
        x, y = snap(x), snap(y)
        label = LocalLabel()
        label.text = text
        label.position = Position(X=x, Y=y, angle=angle)
        label.effects = self._label_effects(justify)
        label.uuid = uid()
        self.sch.labels.append(label)
        return label

    def add_global_label(self, text, x, y, shape="bidirectional", angle=0,
                         justify=None):
        """Add a global net label."""
        x, y = snap(x), snap(y)
        label = GlobalLabel()
        label.text = text
        label.shape = shape
        label.position = Position(X=x, Y=y, angle=angle)
        label.effects = self._label_effects(justify)
        label.uuid = uid()
        self.sch.globalLabels.append(label)
        return label

    def add_hier_label(self, text, x, y, shape="bidirectional", angle=0,
                       justify=None):
        """Add a hierarchical label (connects to parent sheet pin)."""
        x, y = snap(x), snap(y)
        label = HierarchicalLabel()
        label.text = text
        label.shape = shape
        label.position = Position(X=x, Y=y, angle=angle)
        label.effects = self._label_effects(justify)
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

    def add_junction(self, x, y):
        """Add a junction dot at (x, y) for T-connections."""
        x, y = snap(x), snap(y)
        j = Junction()
        j.position = Position(X=x, Y=y)
        j.diameter = 0  # use default
        j.color = ColorRGBA()
        j.uuid = uid()
        self.sch.junctions.append(j)

    def add_segmented_trunk(self, x, ys):
        """Create a segmented vertical trunk wire at x with connections at each y.

        Instead of one long wire from min to max Y, creates separate wire
        segments between consecutive Y values.  This ensures that every
        branch point is a wire endpoint, which KiCad 9 requires for
        reliable T-connection recognition.

        Junctions are placed at all intermediate Y values (not first/last).
        """
        sorted_ys = sorted(set(snap(y) for y in ys))
        if len(sorted_ys) < 2:
            return
        for i in range(len(sorted_ys) - 1):
            self.add_wire(x, sorted_ys[i], x, sorted_ys[i + 1])
        for y in sorted_ys[1:-1]:
            self.add_junction(x, y)

    def place_led_below(self, x, y, drop=None):
        """Branch an LED indicator below a main horizontal wire.

        Places the LED chain at (x, y + drop), wires down from (x, y),
        and adds a junction at (x, y) so the main wire can continue through.

        Returns the junction point (x, y) — caller wires through this point.
        """
        if drop is None:
            drop = 3 * GRID
        x, y = snap(x), snap(y)
        led_y = snap(y + drop)
        led_in = self.place_led_indicator(x, led_y)
        # L-wire: vertical from junction down, then horizontal to LED entry
        self.add_wire(x, y, x, led_in[1])
        if snap(led_in[0]) != x:
            self.add_wire(x, led_in[1], led_in[0], led_in[1])
        # Junction at the branch point on the main wire
        self.add_junction(x, y)
        return (x, y)

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

    Wiring approach:
      - A0/A1/A2: hier label → wire → vertical trunk, branches to inverter + AND inputs
      - A0_INV/A1_INV/A2_INV: inverter output → LED T-junction → vertical trunk → AND inputs
      - SEL0-SEL7: AND output → LED T-junction → wire → hier label
    """
    b = SchematicBuilder(title="Address Decoder", page_size="A3")
    base_x, base_y = 25.4, 30.48

    inv_x = base_x + 18 * GRID
    and_x = base_x + 42 * GRID
    hl_out_x = and_x + 22 * GRID

    # X positions for 6 vertical trunks (A0, A1, A2, A0_INV, A1_INV, A2_INV)
    # True address trunks: placed LEFT of inverter inputs (X=55.88) so that
    # branch wires go rightward to the pin — away from the stub (which goes
    # rightward from tip into body).  Stagger A0 closest, A2 furthest left.
    inv_in_x = inv_x - 15.24  # 55.88: inverter input pin X (74LVC1G04 pin 2)
    addr_trunk_x = [snap(inv_in_x - (1.5 + i) * GRID) for i in range(3)]   # 52.07, 49.53, 46.99
    inv_trunk_x = [base_x + 31 * GRID + i * 2 * GRID for i in range(3)]  # A0_INV, A1_INV, A2_INV (shifted +GRID to avoid LED cathode GND overlap)

    # The AND gates span 8 rows (SEL0-SEL7)
    and_top_y = base_y
    and_bot_y = base_y + 7 * SYM_SPACING_Y

    # -- Hierarchical labels for A0-A2, wired to trunk tops --
    for i in range(3):
        trunk_top_y = snap(base_y - (4 - i) * GRID)  # stagger slightly
        b.add_hier_label(f"A{i}", base_x, trunk_top_y, shape="input", justify="right")
        b.add_wire(base_x, trunk_top_y, addr_trunk_x[i], trunk_top_y)

    # -- Three inverters for complemented address bits --
    # Offset inverter Y by +4*GRID so NEITHER the inverter center Y NOR
    # the NC pin Y (= inv_y - 2.54) coincides with any AND gate pin Y.
    # AND pins sit at row_y + {0, ±5.08}. Forbidden offsets (mod 25.4):
    #   0, ±2.54, ±5.08, 7.62  →  first safe grid-aligned offset = 4*GRID.
    inv_in_pins = []   # inverter input pin positions
    inv_out_pins = []  # inverter output pin positions
    for i in range(3):
        y = base_y + i * SYM_SPACING_Y + 4 * GRID
        _, pins = b.place_symbol("74LVC1G04", inv_x, y)
        b.connect_power(pins)
        inv_in_pins.append(pins["2"])
        inv_out_pins.append(pins["4"])

    # Wire address trunks: each A{i} trunk connects to inverter input + AND gate inputs
    decode_table = [
        (0, 0, 0), (0, 0, 1), (0, 1, 0), (0, 1, 1),
        (1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1),
    ]
    # Pin mapping for 74LVC1G11: pin 6=A2, pin 1=A1, pin 3=A0
    addr_pin_map = {0: "3", 1: "1", 2: "6"}  # address bit index → AND gate pin number

    # Place 8 AND gates first to get pin positions
    and_gate_pins = []
    for sel_idx in range(8):
        y = base_y + sel_idx * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G11", and_x, y)
        b.connect_power(pins, gnd_pin="2")
        and_gate_pins.append(pins)

    # -- Wire each address bit trunk (A0, A1, A2) --
    for addr_i in range(3):
        trunk_x = addr_trunk_x[addr_i]
        trunk_top_y = snap(base_y - (4 - addr_i) * GRID)
        inv_in = inv_in_pins[addr_i]
        pin_num = addr_pin_map[addr_i]

        # Collect Y positions where this trunk branches: inverter input + AND gates using A{i}
        trunk_ys = [trunk_top_y, inv_in[1]]
        branch_targets = [(inv_in[0], inv_in[1])]
        for sel_idx, bits in enumerate(decode_table):
            if bits[2 - addr_i] == 1:  # this AND uses the true (non-inverted) signal
                target = and_gate_pins[sel_idx][pin_num]
                trunk_ys.append(target[1])
                branch_targets.append((target[0], target[1]))

        # Segmented vertical trunk (splits at every branch point)
        b.add_segmented_trunk(trunk_x, trunk_ys)

        # Horizontal branches
        for tx, ty in branch_targets:
            b.add_wire(trunk_x, ty, tx, ty)

    # -- Wire each inverted bit trunk (A0_INV, A1_INV, A2_INV) --
    for addr_i in range(3):
        trunk_x = inv_trunk_x[addr_i]
        out_pin = inv_out_pins[addr_i]
        pin_num = addr_pin_map[addr_i]

        # Inverter output → LED T-junction → horizontal wire to trunk
        led_jct_x = out_pin[0] + 2 * GRID
        # Wire from inverter output to trunk (split at LED junction point)
        b.add_wire(out_pin[0], out_pin[1], led_jct_x, out_pin[1])
        b.add_wire(led_jct_x, out_pin[1], trunk_x, out_pin[1])
        # LED branches down from a point on this wire
        b.place_led_below(led_jct_x, out_pin[1])

        # Collect AND gate inputs using inverted signal
        branch_targets = []
        trunk_ys = [out_pin[1]]
        for sel_idx, bits in enumerate(decode_table):
            if bits[2 - addr_i] == 0:  # this AND uses the inverted signal
                target = and_gate_pins[sel_idx][pin_num]
                branch_targets.append((target[0], target[1]))
                trunk_ys.append(target[1])

        if branch_targets:
            # Segmented vertical trunk
            b.add_segmented_trunk(trunk_x, trunk_ys)
            # Horizontal branches to AND inputs
            for tx, ty in branch_targets:
                b.add_wire(trunk_x, ty, tx, ty)

    # -- SEL outputs: AND output → LED T-junction → wire → hier label --
    for sel_idx in range(8):
        pins = and_gate_pins[sel_idx]
        out_pin = pins["4"]

        # Wire from AND output to hier label, split at LED junction
        led_jct_x = out_pin[0] + 2 * GRID
        b.add_wire(out_pin[0], out_pin[1], led_jct_x, out_pin[1])
        b.add_wire(led_jct_x, out_pin[1], hl_out_x, out_pin[1])
        # LED branches below from junction on this wire
        b.place_led_below(led_jct_x, out_pin[1])
        # Hier label at end of wire
        b.add_hier_label(f"SEL{sel_idx}", hl_out_x, out_pin[1], shape="output", justify="right")

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

    All connections are direct wires — no local labels.
    """
    b = SchematicBuilder(title="Control Logic", page_size="A3")
    base_x, base_y = 25.4, 30.48

    inv_x = base_x + 15 * GRID
    and1_x = base_x + 42 * GRID    # AND(CE, WE) → WRITE_ACTIVE
    and2_x = base_x + 42 * GRID    # AND(CE, OE) → CE_AND_OE
    and3_x = base_x + 62 * GRID    # AND(CE_AND_OE, /WE) → READ_EN

    # Inverter row Y positions
    ce_y = base_y
    oe_y = base_y + SYM_SPACING_Y
    we_y = base_y + 2 * SYM_SPACING_Y

    # AND gate Y positions (between inverter rows for clean routing)
    and1_y = base_y + 4 * GRID      # CE & WE
    and2_y = base_y + 14 * GRID     # CE & OE
    and3_y = and2_y                  # CE_AND_OE & /WE (same row, further right)

    # -- Hier labels for inputs → wire to inverter inputs --
    # nCE
    b.add_hier_label("nCE", base_x, ce_y, shape="input", justify="right")
    _, ce_inv_pins = b.place_symbol("74LVC1G04", inv_x, ce_y)
    b.connect_power(ce_inv_pins)
    ce_inv_in = ce_inv_pins["2"]
    # nCE also fans out to nowhere else, just wire hier label to inverter input
    b.add_wire(base_x, ce_y, ce_inv_in[0], ce_inv_in[1])

    # nOE
    b.add_hier_label("nOE", base_x, oe_y, shape="input", justify="right")
    _, oe_inv_pins = b.place_symbol("74LVC1G04", inv_x, oe_y)
    b.connect_power(oe_inv_pins)
    oe_inv_in = oe_inv_pins["2"]
    b.add_wire(base_x, oe_y, oe_inv_in[0], oe_inv_in[1])

    # nWE — fans out to inverter input AND AND3 input B (via junction)
    nwe_jct_x = base_x + 8 * GRID
    b.add_hier_label("nWE", base_x, we_y, shape="input", justify="right")
    _, we_inv_pins = b.place_symbol("74LVC1G04", inv_x, we_y)
    b.connect_power(we_inv_pins)
    we_inv_in = we_inv_pins["2"]
    # nWE wire to inverter is created below (split at junction for AND3.B tap)

    # -- Inverter outputs with LED T-junctions --

    # CE inverter output → fans out to AND1 pin A and AND2 pin A
    ce_out = ce_inv_pins["4"]

    # OE inverter output → fans out to AND2 pin B only
    oe_out = oe_inv_pins["4"]

    # WE inverter output → fans out to AND1 pin B only
    we_out = we_inv_pins["4"]

    # -- AND1: CE & WE → WRITE_ACTIVE --
    _, and1_pins = b.place_symbol("74LVC1G08", and1_x, and1_y)
    b.connect_power(and1_pins)
    and1_a = and1_pins["1"]   # CE
    and1_b = and1_pins["2"]   # WE
    and1_out = and1_pins["4"]

    # -- AND2: CE & OE → CE_AND_OE --
    _, and2_pins = b.place_symbol("74LVC1G08", and2_x, and2_y)
    b.connect_power(and2_pins)
    and2_a = and2_pins["1"]   # CE
    and2_b = and2_pins["2"]   # OE
    and2_out = and2_pins["4"]

    # -- AND3: CE_AND_OE & /WE → READ_EN --
    _, and3_pins = b.place_symbol("74LVC1G08", and3_x, and3_y)
    b.connect_power(and3_pins)
    and3_a = and3_pins["1"]   # CE_AND_OE
    and3_b = and3_pins["2"]   # /WE
    and3_out = and3_pins["4"]

    # -- Wire CE output to AND1.A and AND2.A (fanout=2 via vertical trunk) --
    # CE trunk must be to the RIGHT of LED cathode GND wires (at led_x + 5*GRID
    # from ce_out) to avoid horizontal overlap between R-to-LED wires and CE
    # branch wires.  After Change 3, LED cathode is at ce_out[0] + 7*GRID,
    # GND wire extends 2*GRID below.  Trunk at +9*GRID clears everything.
    ce_led_x = snap(ce_out[0] + 2 * GRID)
    ce_trunk_x = snap(ce_out[0] + 9 * GRID)
    # Split wire at LED junction point
    b.add_wire(ce_out[0], ce_out[1], ce_led_x, ce_out[1])
    b.add_wire(ce_led_x, ce_out[1], ce_trunk_x, ce_out[1])
    b.place_led_below(ce_led_x, ce_out[1])
    # Segmented vertical trunk from CE output Y through AND1.A Y to AND2.A Y
    b.add_segmented_trunk(ce_trunk_x, [ce_out[1], and1_a[1], and2_a[1]])
    # Branch to AND1.A
    b.add_wire(ce_trunk_x, and1_a[1], and1_a[0], and1_a[1])
    # Branch to AND2.A
    b.add_wire(ce_trunk_x, and2_a[1], and2_a[0], and2_a[1])

    # -- Wire OE output to AND2.B --
    # Route OE vertical between LED cathode GND wires and CE trunk to avoid
    # crossing either.  oe_vert_x = ce_trunk_x - GRID sits in the gap.
    oe_led_x = snap(oe_out[0] + 2 * GRID)
    oe_vert_x = snap(ce_trunk_x - GRID)
    b.add_wire(oe_out[0], oe_out[1], oe_led_x, oe_out[1])
    b.add_wire(oe_led_x, oe_out[1], oe_vert_x, oe_out[1])
    b.add_wire(oe_vert_x, oe_out[1], oe_vert_x, and2_b[1])
    b.add_wire(oe_vert_x, and2_b[1], and2_b[0], and2_b[1])
    b.place_led_below(oe_led_x, oe_out[1])

    # -- Wire WE output to AND1.B --
    # Route WE vertical to the LEFT of AND input pins so the branch wire
    # approaches from the left — away from the pin stub direction.
    we_led_x = snap(we_out[0] + 2 * GRID)
    we_vert_x = snap(and1_b[0] - 2 * GRID)
    b.add_wire(we_out[0], we_out[1], we_led_x, we_out[1])
    b.add_wire(we_led_x, we_out[1], we_vert_x, we_out[1])
    b.add_wire(we_vert_x, we_out[1], we_vert_x, and1_b[1])
    b.add_wire(we_vert_x, and1_b[1], and1_b[0], and1_b[1])
    b.place_led_below(we_led_x, we_out[1])

    # -- Wire CE_AND_OE (AND2 output) to AND3.A with LED (L-shaped) --
    and2_led_x = snap(and2_out[0] + 2 * GRID)
    b.add_wire(and2_out[0], and2_out[1], and2_led_x, and2_out[1])
    b.add_wire(and2_led_x, and2_out[1], and3_a[0], and2_out[1])
    b.add_wire(and3_a[0], and2_out[1], and3_a[0], and3_a[1])
    b.place_led_below(and2_led_x, and2_out[1])

    # -- Wire /WE to AND3.B --
    # Route nWE BELOW all vertical wires (LED GND endpoints max at we_y+5*GRID,
    # WE vert max at we_y) to avoid crossing any signal or GND wires.
    nwe_route_y = snap(we_y + 7 * GRID)
    b.add_wire(base_x, we_y, nwe_jct_x, we_y)
    b.add_wire(nwe_jct_x, we_y, we_inv_in[0], we_inv_in[1])
    b.add_wire(nwe_jct_x, we_y, nwe_jct_x, nwe_route_y)
    b.add_wire(nwe_jct_x, nwe_route_y, and3_b[0], nwe_route_y)
    b.add_wire(and3_b[0], nwe_route_y, and3_b[0], and3_b[1])
    b.add_junction(nwe_jct_x, we_y)

    # -- AND1 output → LED T-junction → hier label WRITE_ACTIVE --
    out_label_x = and3_x + 22 * GRID
    and1_led_x = snap(and1_out[0] + 2 * GRID)
    b.add_wire(and1_out[0], and1_out[1], and1_led_x, and1_out[1])
    b.add_wire(and1_led_x, and1_out[1], out_label_x, and1_out[1])
    b.place_led_below(and1_led_x, and1_out[1])
    b.add_hier_label("WRITE_ACTIVE", out_label_x, and1_out[1], shape="output", justify="right")

    # -- AND3 output → LED T-junction → hier label READ_EN --
    and3_led_x = snap(and3_out[0] + 2 * GRID)
    b.add_wire(and3_out[0], and3_out[1], and3_led_x, and3_out[1])
    b.add_wire(and3_led_x, and3_out[1], out_label_x, and3_out[1])
    b.place_led_below(and3_led_x, and3_out[1])
    b.add_hier_label("READ_EN", out_label_x, and3_out[1], shape="output", justify="right")

    return b


def _generate_nand_bank(title, enable_signal, output_prefix):
    """Shared generator for write_clk_gen and read_oe_gen (8 NANDs each).

    74LVC1G00 has same pin layout as 74LVC1G08.

    Wiring approach:
      - Enable signal: hier label → vertical trunk → branches to each NAND A pin
      - SEL0-SEL7: hier label → wire → NAND B pin (1:1)
      - Outputs: NAND output → LED T-junction → wire → hier label (1:1)
    """
    b = SchematicBuilder(title=title, page_size="A3")
    base_x, base_y = 25.4, 30.48

    nand_x = base_x + 18 * GRID
    hl_out_x = nand_x + 22 * GRID
    enable_trunk_x = base_x + 10 * GRID

    # Enable signal hier label at top → horizontal wire → trunk
    enable_hier_y = snap(base_y - 2 * GRID)
    b.add_hier_label(enable_signal, base_x, enable_hier_y, shape="input", justify="right")
    b.add_wire(base_x, enable_hier_y, enable_trunk_x, enable_hier_y)

    # Place 8 NANDs and collect pin positions
    nand_a_pins = []   # enable input pins (pin A)
    nand_b_pins = []   # SEL input pins (pin B)
    nand_out_pins = []
    for i in range(8):
        y = base_y + i * SYM_SPACING_Y
        _, pins = b.place_symbol("74LVC1G00", nand_x, y)
        b.connect_power(pins)
        nand_a_pins.append(pins["1"])
        nand_b_pins.append(pins["2"])
        nand_out_pins.append(pins["4"])

    # -- Enable vertical trunk: segmented from hier label Y through all NAND A pins --
    trunk_ys = [enable_hier_y] + [nand_a_pins[i][1] for i in range(8)]
    b.add_segmented_trunk(enable_trunk_x, trunk_ys)
    for i in range(8):
        a_pin = nand_a_pins[i]
        b.add_wire(enable_trunk_x, a_pin[1], a_pin[0], a_pin[1])

    # -- SEL inputs: hier label → wire → NAND B pin (1:1) --
    for i in range(8):
        b_pin = nand_b_pins[i]
        b.add_hier_label(f"SEL{i}", base_x, b_pin[1], shape="input", justify="right")
        b.add_wire(base_x, b_pin[1], b_pin[0], b_pin[1])

    # -- Outputs: NAND output → LED T-junction → wire → hier label --
    for i in range(8):
        out_pin = nand_out_pins[i]
        out_net = f"{output_prefix}{i}"
        # Split wire at LED junction point
        led_jct_x = snap(out_pin[0] + 2 * GRID)
        b.add_wire(out_pin[0], out_pin[1], led_jct_x, out_pin[1])
        b.add_wire(led_jct_x, out_pin[1], hl_out_x, out_pin[1])
        b.place_led_below(led_jct_x, out_pin[1])
        b.add_hier_label(out_net, hl_out_x, out_pin[1], shape="output", justify="right")

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
      - LED on DFF Q output (branches below via T-junction)
      - LED on buffer Y output

    Wiring approach:
      - D0-D7: labels (3-way fanout — hier label, DFF D, buffer Y)
      - Q outputs: direct wire from DFF Q → buffer A, LED branches below
      - WRITE_CLK: vertical trunk wire with horizontal branches to each DFF CLK
      - BUF_OE: vertical trunk wire with horizontal branches to each buffer /OE
    """
    b = SchematicBuilder(title="Memory Byte", page_size="A3")
    base_x, base_y = 25.4, 30.48  # extra top margin for trunk headers

    dff_x = base_x + 18 * GRID
    buf_x = base_x + 52 * GRID
    wclk_trunk_x = base_x + 10 * GRID   # WRITE_CLK vertical trunk
    boe_trunk_x = buf_x - 8 * GRID      # BUF_OE vertical trunk (avoid buffer A pin X)

    # -- WRITE_CLK hier label at top → horizontal wire → trunk --
    wclk_hier_y = snap(base_y - 4 * GRID)
    b.add_hier_label("WRITE_CLK", base_x, wclk_hier_y, shape="input", justify="right")
    b.add_wire(base_x, wclk_hier_y, wclk_trunk_x, wclk_hier_y)

    # -- BUF_OE hier label at top → horizontal wire → trunk --
    # Must avoid Y=25.4 (DFF D pin 1 Y for bit 0) to prevent net merging
    boe_hier_y = snap(base_y - 6 * GRID)
    b.add_hier_label("BUF_OE", base_x, boe_hier_y, shape="input", justify="right")
    b.add_wire(base_x, boe_hier_y, boe_trunk_x, boe_hier_y)

    # -- Place components and wire each bit --
    clk_pin_positions = []
    oe_pin_positions = []

    for bit in range(8):
        y = base_y + bit * DFF_SPACING_Y

        # D flip-flop (74LVC1G79)
        _, dff_pins = b.place_symbol("74LVC1G79", dff_x, y)
        b.connect_power(dff_pins)

        # D input: hier label wires directly to DFF D pin
        d_pin = dff_pins["1"]
        hl_y = base_y + bit * DFF_SPACING_Y
        b.add_hier_label(f"D{bit}", base_x, hl_y, shape="bidirectional", justify="right")
        # L-wire from hier label to D pin (handle possible Y offset)
        if snap(hl_y) != snap(d_pin[1]):
            b.add_wire(base_x, hl_y, base_x, d_pin[1])
        b.add_wire(base_x, d_pin[1], d_pin[0], d_pin[1])

        # CLK input: horizontal wire from WRITE_CLK trunk
        clk_pin = dff_pins["2"]
        clk_pin_positions.append(clk_pin)
        b.add_wire(wclk_trunk_x, clk_pin[1], clk_pin[0], clk_pin[1])

        # Q output → direct wire to buffer A input, LED branches below
        q_pin = dff_pins["4"]

        # Tri-state buffer (74LVC1G125)
        _, buf_pins = b.place_symbol("74LVC1G125", buf_x, y)
        b.connect_power(buf_pins)

        a_pin = buf_pins["2"]
        vcc_pin = buf_pins["5"]
        # L-wire from DFF Q → buffer A (Q and A may have different Y offsets)
        # Split horizontal wire at LED junction point
        q_led_x = snap(q_pin[0] + 2 * GRID)
        b.add_wire(q_pin[0], q_pin[1], q_led_x, q_pin[1])

        # Check if horizontal wire at q_pin[1] would pass through VCC pin 5
        wire_y = snap(q_pin[1])
        x_lo = min(q_led_x, a_pin[0])
        x_hi = max(q_led_x, a_pin[0])
        vcc_on_path = (abs(snap(vcc_pin[1]) - wire_y) < 0.01 and
                       x_lo + 0.01 < snap(vcc_pin[0]) < x_hi - 0.01)

        if vcc_on_path:
            # Route around VCC pin: vertical to a_pin Y, then horizontal
            b.add_wire(q_led_x, q_pin[1], q_led_x, a_pin[1])
            b.add_wire(q_led_x, a_pin[1], a_pin[0], a_pin[1])
        else:
            b.add_wire(q_led_x, q_pin[1], a_pin[0], q_pin[1])
            if snap(q_pin[1]) != snap(a_pin[1]):
                b.add_wire(a_pin[0], q_pin[1], a_pin[0], a_pin[1])
        # LED branches down from junction on the horizontal segment
        b.place_led_below(q_led_x, q_pin[1])

        # /OE input: horizontal wire from BUF_OE trunk
        oe_pin = buf_pins["1"]
        oe_pin_positions.append(oe_pin)
        b.add_wire(boe_trunk_x, oe_pin[1], oe_pin[0], oe_pin[1])

        # Y output: wire to LED + label back to data bus
        y_pin = buf_pins["4"]
        led_in = b.place_led_indicator(y_pin[0] + LED_GAP_X, y_pin[1])
        b.add_wire(*y_pin, *led_in)
        b.add_label(f"D{bit}", *y_pin, justify="right")

    # -- WRITE_CLK vertical trunk wire (segmented at each branch) --
    clk_ys = [p[1] for p in clk_pin_positions]
    wclk_all_ys = sorted([wclk_hier_y] + clk_ys)
    b.add_segmented_trunk(wclk_trunk_x, wclk_all_ys)

    # -- BUF_OE vertical trunk wire (segmented at each branch) --
    oe_ys = [p[1] for p in oe_pin_positions]
    boe_all_ys = sorted([boe_hier_y] + oe_ys)
    b.add_segmented_trunk(boe_trunk_x, boe_all_ys)

    return b


# --------------------------------------------------------------
# Root sheet generator
# --------------------------------------------------------------

def generate_root_sheet():
    """
    Root sheet: connector, bus indicator LEDs, and hierarchical sheet references.

    Layout (left to right):
      1. Connector J1 (16-pin, with VCC/GND) + bus indicator LEDs
      2. Column 1: Address Decoder (top) + Control Logic (bottom)
      3. Column 2: Write Clk Gen (top) + Read OE Gen (bottom)
      4. Byte sheets: 2 columns of 4

    Wiring strategy:
      - D0-D7: local labels (high fanout — connector + LEDs + 8 byte sheets)
      - All other signals: direct wires between sheet pins
      - Each control block has inputs on LEFT, outputs on RIGHT
    """
    b = SchematicBuilder(title="8-Byte Discrete RAM", page_size="A2")
    base_x, base_y = 25.4, 25.4

    # -- External connector (flipped 180° so pins face RIGHT) --
    conn_x = base_x
    conn_y = base_y + 5 * GRID
    _, conn_pins = b.place_symbol("Conn_01x16", conn_x, conn_y,
                                  ref_prefix="J", value="SRAM_Bus", angle=180)

    # -- Layout constants --
    sheet_gap = 5 * GRID
    wire_stub = 5.08

    def _sheet_height(num_pins):
        return snap(num_pins * 2.54 + 5.08)

    def _pin_y(sy, pin_idx):
        return snap(sy + 2.54 + pin_idx * 2.54)

    # -- Helper to create a sheet block --
    def _add_sheet_block(name, filename, pins, sx, sy, sw, sh, fill_color,
                         right_pins=None):
        """Create sheet block, return dict mapping pin_name -> (x, y).

        Args:
            right_pins: set of pin names to place on the RIGHT edge (facing right).
                        All other pins go on the LEFT edge (facing left).
        """
        if right_pins is None:
            right_pins = set()
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
        # Count left and right pins separately for Y positioning
        left_pins_list = [(pn, pt) for pn, pt in pins if pn not in right_pins]
        right_pins_list = [(pn, pt) for pn, pt in pins if pn in right_pins]

        pin_positions = {}
        # Place left pins
        for pin_idx, (pin_name, pin_type) in enumerate(left_pins_list):
            pin = HierarchicalPin()
            pin.name = pin_name
            pin.connectionType = pin_type
            py = _pin_y(sy, pin_idx)
            pin.position = Position(X=sx, Y=py, angle=180)
            pin.effects = Effects(font=Font(width=1.27, height=1.27),
                                  justify=Justify(horizontally="left"))
            pin_positions[pin_name] = (sx, py)
            pin.uuid = uid()
            sheet.pins.append(pin)
        # Place right pins
        for pin_idx, (pin_name, pin_type) in enumerate(right_pins_list):
            pin = HierarchicalPin()
            pin.name = pin_name
            pin.connectionType = pin_type
            py = _pin_y(sy, pin_idx)
            pin.position = Position(X=sx + sw, Y=py, angle=0)
            pin.effects = Effects(font=Font(width=1.27, height=1.27),
                                  justify=Justify(horizontally="right"))
            pin_positions[pin_name] = (sx + sw, py)
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
        return pin_positions

    yellow_fill = ColorRGBA(R=255, G=255, B=225, A=255, precision=4)
    green_fill = ColorRGBA(R=225, G=255, B=225, A=255, precision=4)

    # ================================================================
    # 2-Column layout positions
    # ================================================================
    col1_x = snap(base_x + 25 * GRID)
    col1_w = snap(28 * GRID)
    inter_col_gap = snap(15 * GRID)
    col2_x = snap(col1_x + col1_w + inter_col_gap)
    col2_w = snap(28 * GRID)
    col2_byte_gap = snap(15 * GRID)
    byte_col1_x = snap(col2_x + col2_w + col2_byte_gap)
    byte_w = snap(22 * GRID)
    byte_inter_gap = snap(20 * GRID)
    byte_col2_x = snap(byte_col1_x + byte_w + byte_inter_gap)

    # ================================================================
    # Place all sheet blocks, collecting pin positions
    # ================================================================

    # -- Column 1 top: Address Decoder (3 input + 8 output = 11 pins) --
    addr_left_defs = [("A0", "input"), ("A1", "input"), ("A2", "input")]
    addr_right_defs = [(f"SEL{i}", "output") for i in range(8)]
    addr_pin_defs = addr_left_defs + addr_right_defs
    addr_right_names = {f"SEL{i}" for i in range(8)}
    addr_num_left = len(addr_left_defs)
    addr_num_right = len(addr_right_defs)
    addr_h = _sheet_height(max(addr_num_left, addr_num_right))
    addr_sy = base_y
    addr_pp = _add_sheet_block("Address Decoder", "address_decoder.kicad_sch",
                               addr_pin_defs, col1_x, addr_sy,
                               col1_w, addr_h, yellow_fill,
                               right_pins=addr_right_names)

    # -- Column 1 bottom: Control Logic (3 input + 2 output = 5 pins) --
    ctrl_left_defs = [("nCE", "input"), ("nOE", "input"), ("nWE", "input")]
    ctrl_right_defs = [("WRITE_ACTIVE", "output"), ("READ_EN", "output")]
    ctrl_pin_defs = ctrl_left_defs + ctrl_right_defs
    ctrl_right_names = {"WRITE_ACTIVE", "READ_EN"}
    ctrl_num_left = len(ctrl_left_defs)
    ctrl_num_right = len(ctrl_right_defs)
    ctrl_h = _sheet_height(max(ctrl_num_left, ctrl_num_right))
    ctrl_sy = snap(addr_sy + addr_h + sheet_gap)
    ctrl_pp = _add_sheet_block("Control Logic", "control_logic.kicad_sch",
                               ctrl_pin_defs, col1_x, ctrl_sy,
                               col1_w, ctrl_h, yellow_fill,
                               right_pins=ctrl_right_names)

    # -- Column 2 top: Write Clk Gen (9 input + 8 output = 17 pins) --
    wclk_left_defs = [("WRITE_ACTIVE", "input")]
    wclk_left_defs += [(f"SEL{i}", "input") for i in range(8)]
    wclk_right_defs = [(f"WRITE_CLK_{i}", "output") for i in range(8)]
    wclk_pin_defs = wclk_left_defs + wclk_right_defs
    wclk_right_names = {f"WRITE_CLK_{i}" for i in range(8)}
    wclk_num_left = len(wclk_left_defs)
    wclk_num_right = len(wclk_right_defs)
    wclk_h = _sheet_height(max(wclk_num_left, wclk_num_right))
    wclk_sy = base_y
    wclk_pp = _add_sheet_block("Write Clk Gen", "write_clk_gen.kicad_sch",
                               wclk_pin_defs, col2_x, wclk_sy,
                               col2_w, wclk_h, yellow_fill,
                               right_pins=wclk_right_names)

    # -- Column 2 bottom: Read OE Gen (9 input + 8 output = 17 pins) --
    roe_left_defs = [("READ_EN", "input")]
    roe_left_defs += [(f"SEL{i}", "input") for i in range(8)]
    roe_right_defs = [(f"BUF_OE_{i}", "output") for i in range(8)]
    roe_pin_defs = roe_left_defs + roe_right_defs
    roe_right_names = {f"BUF_OE_{i}" for i in range(8)}
    roe_num_left = len(roe_left_defs)
    roe_num_right = len(roe_right_defs)
    roe_h = _sheet_height(max(roe_num_left, roe_num_right))
    roe_sy = snap(wclk_sy + wclk_h + sheet_gap)
    roe_pp = _add_sheet_block("Read OE Gen", "read_oe_gen.kicad_sch",
                              roe_pin_defs, col2_x, roe_sy,
                              col2_w, roe_h, yellow_fill,
                              right_pins=roe_right_names)

    # -- Byte sheets (2 columns of 4, all pins on LEFT) --
    byte_pin_defs = [("WRITE_CLK", "input"), ("BUF_OE", "input")]
    byte_pin_defs += [(f"D{bit}", "bidirectional") for bit in range(8)]
    byte_h = _sheet_height(len(byte_pin_defs))

    byte_pp = []
    for byte_idx in range(8):
        col = byte_idx // 4
        row = byte_idx % 4
        sx = byte_col1_x if col == 0 else byte_col2_x
        sy = snap(base_y + row * (byte_h + sheet_gap))
        pp = _add_sheet_block(f"Byte {byte_idx}", "byte.kicad_sch",
                              byte_pin_defs, sx, sy, byte_w, byte_h, green_fill)
        byte_pp.append(pp)

    # ================================================================
    # Connector pin positions (16-pin, flipped 180°, pins face RIGHT)
    # Pin 1: GND (bottom), Pins 2-15: signals, Pin 16: VCC (top)
    # ================================================================
    signal_names = ["A0", "A1", "A2",
                    "D0", "D1", "D2", "D3", "D4",
                    "D5", "D6", "D7",
                    "nCE", "nOE", "nWE"]
    conn_signal_pos = {}
    for pin_num_int, sig in enumerate(signal_names, start=2):
        conn_signal_pos[sig] = conn_pins[str(pin_num_int)]

    # ================================================================
    # Connector power pins (VCC at pin 16 = top, GND at pin 1 = bottom)
    # ================================================================
    vcc_pos = conn_pins["16"]
    gnd_pos = conn_pins["1"]
    # Power symbols + PWR_FLAG at connector power pins.
    # PWR_FLAG marks these nets as externally driven (required for ERC).
    pwr_wire_len = snap(3 * GRID)
    # VCC: wire from connector pin rightward, power symbol + PWR_FLAG at end
    vcc_sym_x = snap(vcc_pos[0] + pwr_wire_len)
    b.add_wire(vcc_pos[0], vcc_pos[1], vcc_sym_x, vcc_pos[1])
    b.place_power("VCC", vcc_sym_x, vcc_pos[1], angle=90)
    b.place_power("PWR_FLAG", vcc_sym_x, vcc_pos[1])
    # GND: wire from connector pin rightward, power symbol + PWR_FLAG at end
    gnd_sym_x = snap(gnd_pos[0] + pwr_wire_len)
    b.add_wire(gnd_pos[0], gnd_pos[1], gnd_sym_x, gnd_pos[1])
    b.place_power("GND", gnd_sym_x, gnd_pos[1], angle=90)
    b.place_power("PWR_FLAG", gnd_sym_x, gnd_pos[1])

    # ================================================================
    # Connector signal labels (short stubs from each pin)
    # ================================================================
    conn_pin_x = conn_signal_pos[signal_names[0]][0]
    label_x = snap(conn_pin_x + 2 * GRID)

    for sig in signal_names:
        cx, cy = conn_signal_pos[sig]
        b.add_wire(cx, cy, label_x, cy)
        b.add_label(sig, label_x, cy)

    # ================================================================
    # LED bank — separate area below connector, labels tap signals
    # ================================================================
    # LEDs are placed in their own section to avoid T-junctions between
    # connector horizontal wires and LED vertical drops.
    led_bank_x = snap(conn_pin_x + 6 * GRID)
    led_bank_y = snap(conn_signal_pos[signal_names[-1]][1] + 10 * GRID)
    led_spacing_y = snap(3 * GRID)

    for pin_idx, sig in enumerate(signal_names):
        ly = snap(led_bank_y + pin_idx * led_spacing_y)
        b.add_label(sig, led_bank_x, ly, justify="right")
        led_in = b.place_led_indicator(led_bank_x + GRID, ly)
        b.add_wire(led_bank_x, ly, led_in[0], led_in[1])

    # ================================================================
    # Labels on sheet block input pins (connector signals)
    # ================================================================

    # -- A0-A2 labels on Address Decoder LEFT pins --
    for i in range(3):
        sig = f"A{i}"
        px, py = addr_pp[sig]
        b.add_wire(px, py, px - wire_stub, py)
        b.add_label(sig, px - wire_stub, py, justify="right")

    # -- nCE/nOE/nWE labels on Control Logic LEFT pins --
    for sig in ["nCE", "nOE", "nWE"]:
        px, py = ctrl_pp[sig]
        b.add_wire(px, py, px - wire_stub, py)
        b.add_label(sig, px - wire_stub, py, justify="right")

    # -- D0-D7 labels on byte sheet pins (LEFT side) --
    for byte_idx in range(8):
        pp = byte_pp[byte_idx]
        for bit in range(8):
            sig = f"D{bit}"
            px, py = pp[sig]
            b.add_wire(px, py, px - wire_stub, py)
            b.add_label(sig, px - wire_stub, py, justify="right")

    # ================================================================
    # Route C: Col1 RIGHT → Col2 LEFT (SEL0-7 trunks)
    # ================================================================
    # 8 vertical trunks in the inter-column gap
    sel_trunk_base_x = snap(col2_x - 3 * GRID)
    sel_trunk_x = [snap(sel_trunk_base_x - i * GRID) for i in range(8)]

    for i in range(8):
        sig = f"SEL{i}"
        # Source: Address Decoder RIGHT pin
        ax, ay = addr_pp[sig]
        # Destinations: Write Clk Gen LEFT pin + Read OE Gen LEFT pin
        wx, wy = wclk_pp[sig]
        rx, ry = roe_pp[sig]
        tx = sel_trunk_x[i]

        # Horizontal from addr decoder RIGHT to trunk
        b.add_wire(ax, ay, tx, ay)
        # Segmented vertical trunk
        trunk_ys = sorted(set([snap(ay), snap(wy), snap(ry)]))
        b.add_segmented_trunk(tx, trunk_ys)
        # Horizontal branches to col2 LEFT pins
        b.add_wire(tx, wy, wx, wy)
        b.add_wire(tx, ry, rx, ry)

    # ================================================================
    # Route D: Col1 RIGHT → Col2 LEFT (WRITE_ACTIVE, READ_EN)
    # ================================================================
    # Trunk X positions BETWEEN sel_trunk_x[0] and col2_x to avoid
    # horizontal overlaps with SEL wires at the same Y
    wa_trunk_x = snap(sel_trunk_base_x + 1 * GRID)
    re_trunk_x = snap(sel_trunk_base_x + 2 * GRID)

    # WRITE_ACTIVE: Control Logic RIGHT → Write Clk Gen LEFT
    wa_src = ctrl_pp["WRITE_ACTIVE"]
    wa_dst = wclk_pp["WRITE_ACTIVE"]
    b.add_wire(wa_src[0], wa_src[1], wa_trunk_x, wa_src[1])
    b.add_wire(wa_trunk_x, wa_src[1], wa_trunk_x, wa_dst[1])
    b.add_wire(wa_trunk_x, wa_dst[1], wa_dst[0], wa_dst[1])

    # READ_EN: Control Logic RIGHT → Read OE Gen LEFT
    re_src = ctrl_pp["READ_EN"]
    re_dst = roe_pp["READ_EN"]
    b.add_wire(re_src[0], re_src[1], re_trunk_x, re_src[1])
    b.add_wire(re_trunk_x, re_src[1], re_trunk_x, re_dst[1])
    b.add_wire(re_trunk_x, re_dst[1], re_dst[0], re_dst[1])

    # ================================================================
    # Route E: Col2 RIGHT → Byte Col1 (WRITE_CLK_0-3) — direct wires
    # ================================================================
    route_e_base_x = snap(col2_x + col2_w + 2 * GRID)
    route_e_sp = GRID
    wclk_route_x = [snap(route_e_base_x + i * route_e_sp) for i in range(4)]

    for i in range(4):
        sig = f"WRITE_CLK_{i}"
        src_x, src_y = wclk_pp[sig]
        dst_x, dst_y = byte_pp[i]["WRITE_CLK"]
        rx = wclk_route_x[i]
        b.add_wire(src_x, src_y, rx, src_y)
        if abs(src_y - dst_y) > 0.01:
            b.add_wire(rx, src_y, rx, dst_y)
        b.add_wire(rx, dst_y, dst_x, dst_y)

    # ================================================================
    # Route F: Col2 RIGHT → Byte Col2 (WRITE_CLK_4-7) — direct wires
    # ================================================================
    # Route via transit Y above all blocks to avoid crossing hierarchy pins.
    transit_y_base = snap(base_y - 3 * GRID)
    route_f_gap1_x = [snap(route_e_base_x + (4 + i) * route_e_sp)
                      for i in range(4)]
    byte_gap_base_x = snap(byte_col1_x + byte_w + 2 * GRID)
    byte_gap_sp = GRID
    route_f_gap2_x = [snap(byte_gap_base_x + i * byte_gap_sp)
                      for i in range(4)]

    for i in range(4):
        byte_idx = 4 + i
        w_transit_y = snap(transit_y_base - i * GRID)

        sig = f"WRITE_CLK_{byte_idx}"
        src_x, src_y = wclk_pp[sig]
        dst_x, dst_y = byte_pp[byte_idx]["WRITE_CLK"]
        g1x = route_f_gap1_x[i]
        g2x = route_f_gap2_x[i]
        b.add_wire(src_x, src_y, g1x, src_y)
        b.add_wire(g1x, src_y, g1x, w_transit_y)
        b.add_wire(g1x, w_transit_y, g2x, w_transit_y)
        b.add_wire(g2x, w_transit_y, g2x, dst_y)
        b.add_wire(g2x, dst_y, dst_x, dst_y)

    # ================================================================
    # BUF_OE_0-7: labels (roe RIGHT pin Y values systematically
    # coincide with byte D-signal and WRITE_CLK pin Y values,
    # making direct wires impractical without net merges)
    # ================================================================
    for i in range(8):
        sig = f"BUF_OE_{i}"
        src_x, src_y = roe_pp[sig]
        b.add_wire(src_x, src_y, src_x + wire_stub, src_y)
        b.add_label(sig, src_x + wire_stub, src_y)
        dst_x, dst_y = byte_pp[i]["BUF_OE"]
        b.add_wire(dst_x, dst_y, dst_x - wire_stub, dst_y)
        b.add_label(sig, dst_x - wire_stub, dst_y, justify="right")

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

    IMPORTANT: The original local instance path (/{sheet_uuid}/) is preserved so
    that sub-sheets pass ERC when opened standalone.  The hierarchy path is appended
    alongside it.  KiCad uses whichever path matches the current context.
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
            # Keep local path, append one hierarchy path per instance
            for sym in builder.sch.schematicSymbols:
                prefix = ""
                for p in sym.properties:
                    if p.key == "Reference":
                        prefix = p.value.rstrip("0123456789")
                        break

                # Append hierarchy paths to the existing instance's path list
                existing = sym.instances[0] if sym.instances else None
                if existing is None:
                    existing = SymbolProjectInstance(name=PROJECT_NAME, paths=[])
                    sym.instances = [existing]

                for inst_block in all_sheet_blocks:
                    inst_path = f"/{root_uuid}/{inst_block.uuid}"
                    global_counters[prefix] = global_counters.get(prefix, 0) + 1
                    inst_ref = f"{prefix}{global_counters[prefix]}"
                    existing.paths.append(SymbolProjectPath(
                        sheetInstancePath=inst_path,
                        reference=inst_ref,
                        unit=1,
                    ))
        else:
            # Single-instance sub-sheet: keep local path, append hierarchy path
            for sym in builder.sch.schematicSymbols:
                prefix = ""
                for p in sym.properties:
                    if p.key == "Reference":
                        prefix = p.value.rstrip("0123456789")
                        break

                global_counters[prefix] = global_counters.get(prefix, 0) + 1
                new_ref = f"{prefix}{global_counters[prefix]}"

                existing = sym.instances[0] if sym.instances else None
                if existing is None:
                    existing = SymbolProjectInstance(name=PROJECT_NAME, paths=[])
                    sym.instances = [existing]

                existing.paths.append(SymbolProjectPath(
                    sheetInstancePath=hier_path,
                    reference=new_ref,
                    unit=1,
                ))


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
    saved_paths = []
    for name, builder in builders.items():
        filepath = os.path.join(BOARD_DIR, f"{name}.kicad_sch")
        builder.save(filepath)
        saved_paths.append(filepath)
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
