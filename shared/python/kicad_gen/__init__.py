"""
KiCad generation utilities for discrete NES project.

This package provides tools for programmatically generating KiCad schematics
and PCB layouts for large-scale discrete logic circuits with LED indicators.
"""

__version__ = "0.2.0"

from .schematic import SchematicBuilder
from .symbols import get_lib_symbols, get_raw_lib_texts, get_pin_offsets, discover_pin_offsets
from .verify import (
    parse_schematic, run_all_checks, run_erc, UnionFind,
    _extract_lib_pins, _pin_schematic_offset, pts_close, TOLERANCE,
)
from .common import snap, uid, GRID, SYM_SPACING_Y, KICAD_CLI, SYMBOL_LIB_MAP
