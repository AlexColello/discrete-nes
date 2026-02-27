"""
Library symbol loading and pin offset discovery for KiCad schematic generation.

Handles:
- Loading symbol definitions from KiCad stock libraries
- Extracting raw s-expression text for exact lib_symbol replacement
- Pin offset discovery via kicad-cli ERC probing
- Lazy-loaded global caches for symbols, raw texts, and pin offsets
"""

import copy
import json
import math
import os
import re
import subprocess

from kiutils.schematic import Schematic
from kiutils.symbol import SymbolLib
from kiutils.items.schitems import (
    SchematicSymbol, SymbolProjectInstance, SymbolProjectPath,
)
from kiutils.items.common import (
    Position, Property, Effects, Font, PageSettings,
)

from .common import KICAD_CLI, SYMBOL_LIB_MAP, uid, snap


# ==============================================================
# Library symbol loading
# ==============================================================

def _block_has_hide(text, keyword):
    """Check if a (keyword ...) s-expression block contains (hide yes).

    Finds the block starting with ``(keyword`` in *text*, extracts it by
    matching parentheses, and returns True if ``(hide yes)`` appears inside.
    """
    start = text.find(f"({keyword}")
    if start == -1:
        return False
    # Walk forward from '(' matching parens to find the block end
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                block = text[start:i + 1]
                return "(hide yes)" in block
    return False


def _parse_pin_hide_flags(lib_path, wanted):
    """Parse raw .kicad_sym text to detect (pin_numbers (hide yes)) and
    (pin_names ... (hide yes)) directives that kiutils doesn't read.

    Returns {sym_name: (hide_pin_numbers: bool, hide_pin_names: bool)}.
    """
    text = open(lib_path, "r", encoding="utf-8").read()
    result = {}
    for sym_name in wanted:
        # Match the top-level symbol block header (one-tab indent)
        pat = re.compile(r'^\t\(symbol "' + re.escape(sym_name) + r'"', re.MULTILINE)
        m = pat.search(text)
        if not m:
            continue
        # Grab enough of the block header to find pin_numbers/pin_names
        # (they appear right after the symbol name, before properties)
        block = text[m.start():m.start() + 500]
        hide_numbers = _block_has_hide(block, "pin_numbers")
        hide_names = _block_has_hide(block, "pin_names")
        result[sym_name] = (hide_numbers, hide_names)
    return result


def _extract_raw_symbol(text, sym_name):
    """Extract the raw s-expression block for a symbol from library file text.

    Returns the block as a string (one-tab indent, matching the library file
    format), or None if not found.
    """
    pat = re.compile(r'^\t\(symbol "' + re.escape(sym_name) + r'"', re.MULTILINE)
    m = pat.search(text)
    if not m:
        return None
    depth = 0
    for i in range(m.start(), len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[m.start():i + 1]
    return None


def load_lib_symbols():
    """Load symbol definitions from KiCad stock libraries.

    Also extracts the raw s-expression text for each symbol so that
    ``SchematicBuilder.save()`` can replace kiutils' lossy serialization
    with exact library text (fixing exclude_from_sim, property/pin hide
    flags, and other attributes that kiutils drops).
    """
    symbols = {}
    raw_texts = {}  # sym_name -> raw s-expression text from library file

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
        lib_text = open(lib_path, "r", encoding="utf-8").read()
        lib_prefix = ""
        # Determine the library prefix from SYMBOL_LIB_MAP
        for sn in wanted:
            if sn in SYMBOL_LIB_MAP:
                lib_prefix = SYMBOL_LIB_MAP[sn]
                break

        # Extract raw text and parse pin hide flags
        hide_flags = _parse_pin_hide_flags(lib_path, wanted)
        for sn in wanted:
            raw = _extract_raw_symbol(lib_text, sn)
            if raw:
                # Re-key with the "lib:name" prefix used in schematics
                qualified = f"{lib_prefix}:{sn}" if lib_prefix else sn
                # Replace the library indent with schematic indent (4 spaces)
                # and rename the symbol to include the library prefix
                fixed = raw.replace(
                    f'(symbol "{sn}"',
                    f'(symbol "{qualified}"',
                    1,
                )
                raw_texts[sn] = fixed

        lib = SymbolLib.from_file(lib_path)
        for sym in lib.symbols:
            if sym.libId in wanted:
                hn, hname = hide_flags.get(sym.libId, (False, False))
                if hn:
                    sym.hidePinNumbers = True
                if hname:
                    sym.pinNamesHide = True
                symbols[sym.libId] = sym

    return symbols, raw_texts


# Global caches (lazy-loaded)
ALL_SYMBOLS = None
RAW_LIB_TEXTS = None


def get_lib_symbols():
    global ALL_SYMBOLS, RAW_LIB_TEXTS
    if ALL_SYMBOLS is None:
        ALL_SYMBOLS, RAW_LIB_TEXTS = load_lib_symbols()
    return ALL_SYMBOLS


def get_raw_lib_texts():
    get_lib_symbols()  # ensure loaded
    return RAW_LIB_TEXTS


# ==============================================================
# Pin offset discovery via kicad-cli ERC
# ==============================================================

PIN_OFFSETS = None  # lazy-loaded: {(sym_name, angle): {pin_num: (dx, dy)}}


def _run_erc_for_pins(sch_path):
    """Run kicad-cli ERC and parse pin positions from the JSON output.

    Returns {ref: {pin_num: (x_mm, y_mm)}}.
    ERC reports coordinates that need x100 to convert to schematic mm.
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


def discover_pin_offsets(board_dir=None):
    """Discover pin position offsets for every (component, angle) we use.

    Places one component at a known origin in a temp schematic, runs
    kicad-cli ERC, and reads back absolute pin positions.  The offset is
    just (reported_pos - origin).  KiCad handles all Y-negation and
    rotation -- we never compute it ourselves.

    Args:
        board_dir: Directory to store temp probe file. If None, uses
                   a temp directory.

    Returns {(sym_name, angle): {pin_num: (dx, dy)}}
    """
    if board_dir is None:
        import tempfile
        board_dir = tempfile.gettempdir()

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
        temp_path = os.path.join(board_dir, "_pin_probe.kicad_sch")

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


def get_pin_offsets(board_dir=None):
    """Return cached pin offsets, discovering them on first call.

    Args:
        board_dir: Directory for temp probe file (passed to discover_pin_offsets).
    """
    global PIN_OFFSETS
    if PIN_OFFSETS is None:
        print("Discovering pin offsets via kicad-cli ERC...")
        PIN_OFFSETS = discover_pin_offsets(board_dir)
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
