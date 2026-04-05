"""
KiCad generation utilities for discrete NES project.

This package provides tools for programmatically generating KiCad schematics
and PCB layouts for large-scale discrete logic circuits with LED indicators.
"""

__version__ = "0.3.0"


def _patch_kiutils_for_kicad10() -> None:
    """Monkey-patch kiutils 1.4.8 to handle KiCad 10 net/pad format changes.

    KiCad 10's Specctra SES import emits pad net references as
    ``(net "name")`` -- a single argument, dropping the net number that
    KiCad 9 always included (``(net 105 "GND")``).  kiutils 1.4.8's
    ``Net.from_sexpr`` blindly indexes ``exp[1]`` (number) and ``exp[2]``
    (name), crashing with ``IndexError: list index out of range`` on the
    shorter form.

    This patch teaches ``Net.from_sexpr`` to accept both shapes.  It's a
    no-op on 3-arg expressions, so the pre-routing file (still written by
    our generator in the legacy 3-arg form) is unaffected.
    """
    from kiutils.items.common import Net

    original_from_sexpr = Net.from_sexpr

    def _from_sexpr(cls, exp):
        if isinstance(exp, list) and len(exp) >= 2 and exp[0] == "net":
            obj = cls()
            # Accept (net "name"), (net N), (net N "name")
            if len(exp) == 2:
                if isinstance(exp[1], str) and not exp[1].lstrip("-").isdigit():
                    obj.number = 0
                    obj.name = exp[1]
                else:
                    obj.number = int(exp[1])
                    obj.name = ""
                return obj
            if len(exp) >= 3:
                obj.number = int(exp[1]) if str(exp[1]).lstrip("-").isdigit() else 0
                obj.name = exp[2]
                return obj
        return original_from_sexpr.__func__(cls, exp)

    Net.from_sexpr = classmethod(_from_sexpr)


_patch_kiutils_for_kicad10()


from .schematic import SchematicBuilder
from .symbols import get_lib_symbols, get_raw_lib_texts, get_pin_offsets, discover_pin_offsets
from .verify import (
    parse_schematic, run_all_checks, run_erc, run_drc, UnionFind,
    _extract_lib_pins, _pin_schematic_offset, pts_close, TOLERANCE,
)
from .pcb import (
    PCBBuilder, create_dsbga_footprints,
    export_netlist, parse_netlist, get_footprint_for_part,
    fix_pcb_drc,
)
from .common import (
    snap, uid, GRID, SYM_SPACING_Y, KICAD_CLI, SYMBOL_LIB_MAP,
    FOOTPRINT_MAP, DSBGA5_PIN_TO_BALL, DSBGA6_PIN_TO_BALL,
)
from .snapshot import (
    find_board_outline, snapshot_region,
)
