"""
Schematic generation utilities using kiutils.

Provides SchematicBuilder -- the main class for programmatically creating
KiCad schematics with TI Little Logic (SN74LVC1G) single-gate ICs and
0402 SMD LED indicators.
"""

import copy
import math
import re

from kiutils.schematic import Schematic
from kiutils.items.schitems import (
    SchematicSymbol, Connection, LocalLabel, GlobalLabel,
    HierarchicalLabel, SymbolProjectInstance, SymbolProjectPath,
    Junction,
)
from kiutils.items.common import (
    Position, Property, Effects, Font, Justify, PageSettings, ColorRGBA,
)

from .common import GRID, SYMBOL_LIB_MAP, uid, snap
from .symbols import get_lib_symbols, get_raw_lib_texts, get_pin_offsets


class SchematicBuilder:
    """Convenience wrapper around a kiutils Schematic for building sheets."""

    def __init__(self, title="", page_size="A3", project_name="ram"):
        self.sch = Schematic.create_new()
        self.sch.version = 20250114  # KiCad 9 format (required for wire connectivity)
        self.sch.generator = "eeschema"
        self.sch.uuid = uid()
        self.sch.paper = PageSettings(paperSize=page_size)
        self._ref_counters = {}   # prefix -> next number
        self._embedded_symbols = set()  # track which lib symbols we've embedded
        self._pin_offsets = get_pin_offsets()
        self._project_name = project_name

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
        # pin_numbers/pin_names (hide yes) flags are now parsed from the raw
        # library files in load_lib_symbols() and already set on the symbol.
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

        # Properties: copy positions + effects from library symbol defaults,
        # transforming positions from library space to schematic space.
        # This matches what KiCad does when you place a component manually.
        all_syms = get_lib_symbols()
        lib_sym = all_syms[lib_name]

        # Library-only metadata -- not copied to instances
        _skip_keys = {"ki_keywords", "ki_fp_filters"}
        # Properties hidden by KiCad convention in instances
        _hide_keys = {"Footprint", "Datasheet", "Description", "Sim.Pins"}
        # Text value overrides
        _overrides = {"Reference": ref, "Value": value}

        rad = math.radians(angle)
        cos_a = round(math.cos(rad), 10)
        sin_a = round(math.sin(rad), 10)

        sym.properties = []
        for lib_prop in lib_sym.properties:
            if lib_prop.key in _skip_keys:
                continue

            prop_value = _overrides.get(lib_prop.key, lib_prop.value)

            # Transform library position to schematic position
            lx = lib_prop.position.X if lib_prop.position else 0
            ly = lib_prop.position.Y if lib_prop.position else 0
            prop_text_angle = (lib_prop.position.angle or 0) if lib_prop.position else 0
            bx, by = lx, -ly  # library Y-up -> schematic Y-down
            dx = snap(cos_a * bx + sin_a * by)
            dy = snap(-sin_a * bx + cos_a * by)

            # Copy effects from library
            effects = copy.deepcopy(lib_prop.effects) if lib_prop.effects else Effects()
            if lib_prop.key == "Reference" and ref_prefix.startswith("#"):
                effects.hide = True
            elif lib_prop.key in _hide_keys:
                effects.hide = True

            sym.properties.append(Property(
                key=lib_prop.key, value=prop_value,
                id=len(sym.properties),
                position=Position(X=snap(x + dx), Y=snap(y + dy),
                                  angle=prop_text_angle),
                effects=effects,
            ))

        if extra_props:
            for k, v in extra_props.items():
                sym.properties.append(
                    Property(key=k, value=v, id=len(sym.properties),
                             position=Position(X=x, Y=y, angle=0),
                             effects=Effects(font=Font(width=1.27, height=1.27),
                                             hide=True))
                )

        if mirror:
            sym.mirror = mirror

        # Pin UUIDs -- required for KiCad 9 wire connectivity
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
            name=self._project_name,
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

        No wires needed -- the power symbol's pin overlaps the IC's power pin
        at the exact same position, creating a direct connection.
        This avoids wire overlap issues between adjacent ICs.
        """
        self.place_power("VCC", *pins[vcc_pin])
        self.place_power("GND", *pins[gnd_pin])

    # -- place an LED + resistor pair (indicator for a gate output) --

    def place_led_indicator(self, x, y):
        """Place R + LED + GND chain at (x, y). Signal enters from left.

        Layout (left to right): signal -> R Pin 1 -> R Pin 2 -> LED Pin 2 -> LED Pin 1 -> GND

        R_Small at angle=90:   Pin 1 at LEFT (dx=-2.54), Pin 2 at RIGHT (dx=+2.54)
        LED_Small at angle=180: Pin 2/Anode at LEFT (dx=-2.54), Pin 1/Cathode at RIGHT (dx=+2.54)

        Components are spaced apart so wires don't pass through symbol bodies.
        """
        r_x = x + GRID  # shift R center right so pin 1 (at dx=-2.54) lands at x
        _, r_pins = self.place_symbol("R_Small", r_x, y, ref_prefix="R",
                                      value="680R", angle=90)

        # LED placed 3 grid units right of R center -- enough gap to avoid overlap
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

        Returns the junction point (x, y) -- caller wires through this point.
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

    @staticmethod
    def _fix_lib_symbols(filepath):
        """Replace kiutils-serialized lib_symbol blocks with exact library text.

        kiutils drops several attributes when serializing library symbols:
        - ``exclude_from_sim no``
        - ``(hide yes)`` on properties (Footprint, Datasheet, etc.)
        - ``(hide yes)`` on pins (e.g., 74LVC1G04 NC pin)
        - ``(embedded_fonts no)``

        This post-processing step replaces each embedded lib_symbol block
        with the raw s-expression text extracted from the KiCad stock library
        files, ensuring a byte-exact match and eliminating lib_symbol_mismatch
        ERC warnings.
        """
        raw_texts = get_raw_lib_texts()
        text = open(filepath, "r", encoding="utf-8").read()

        for sym_name, raw_block in raw_texts.items():
            qualified = SYMBOL_LIB_MAP.get(sym_name, "")
            qualified = f"{qualified}:{sym_name}" if qualified else sym_name

            # Find the kiutils-generated block for this symbol
            pat = re.compile(
                r'^(\s*)\(symbol "' + re.escape(qualified) + r'"',
                re.MULTILINE,
            )
            m = pat.search(text)
            if not m:
                continue

            # Find the end of this block by matching parens
            depth = 0
            start = m.start()
            end = start
            for i in range(start, len(text)):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break

            # Determine the indentation of the block in the schematic
            indent = m.group(1)
            # Re-indent the raw library block to match (library uses one tab)
            fixed = raw_block.replace("\n\t", "\n" + indent)
            # Fix the first line indent too
            if fixed.startswith("\t"):
                fixed = indent + fixed.lstrip("\t")

            text = text[:start] + fixed + text[end:]

        open(filepath, "w", encoding="utf-8").write(text)

    def save(self, filepath):
        self.sch.to_file(filepath)
        self._fix_lib_symbols(filepath)
        return filepath
