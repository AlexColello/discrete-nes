"""
Common utilities for KiCad file generation.

Provides shared functions and constants used across schematic and PCB generation.
Configured for TI Little Logic (SN74LVC1G) in DSBGA (NanoFree) packages.
"""

from typing import Dict, Tuple

# Power supply voltages
VCC = 3.3  # Volts - LVC supports 1.65-5.5V, 3.3V typical for low power
VCC_MAX = 5.5  # Max supply for SN74LVC1G

# Logic family
LOGIC_FAMILY = "SN74LVC1G"  # TI Little Logic, single-gate packages

# DSBGA (NanoFree/YZP) package dimensions
DSBGA_WIDTH = 1.25   # mm (body width)
DSBGA_LENGTH = 1.75  # mm (body length)
DSBGA_BALL_PITCH = 0.5  # mm (solder ball pitch)
DSBGA_BALL_COUNT = 5    # Most YZP parts are 5-ball (A1, A2, A3, B1, B2)

# SMD LED (0402 / 1005 metric) dimensions
LED_0402_LENGTH = 1.0  # mm
LED_0402_WIDTH = 0.5   # mm

# SMD resistor (0402 / 1005 metric) dimensions
RES_0402_LENGTH = 1.0  # mm
RES_0402_WIDTH = 0.5   # mm

# Part number lookup: function name → TI part number
PART_LOOKUP: Dict[str, str] = {
    "nand":         "SN74LVC1G00",   # Single 2-input NAND
    "nor":          "SN74LVC1G02",   # Single 2-input NOR
    "not":          "SN74LVC1G04",   # Single inverter
    "buffer_od":    "SN74LVC1G07",   # Single buffer (open drain, good for LED drive)
    "and":          "SN74LVC1G08",   # Single 2-input AND
    "and3":         "SN74LVC1G11",   # Single 3-input AND
    "or":           "SN74LVC1G32",   # Single 2-input OR
    "dff":          "SN74LVC1G79",   # Single D flip-flop (Q only, no set/reset)
    "dff_sr":       "SN74LVC1G74",   # Single D flip-flop (Q, Q̄, preset, clear) — X2SON only
    "xor":          "SN74LVC1G86",   # Single 2-input XOR
    "tri_buffer":   "SN74LVC1G125",  # Single tri-state buffer
}

# DSBGA package codes for each part
PACKAGE_MAP: Dict[str, str] = {
    "SN74LVC1G00":  "YZP",   # DSBGA 5-ball
    "SN74LVC1G02":  "YZP",   # DSBGA 5-ball
    "SN74LVC1G04":  "YZP",   # DSBGA 5-ball (also YZV 4-ball variant)
    "SN74LVC1G07":  "YZP",   # DSBGA 5-ball (also YZV 4-ball variant)
    "SN74LVC1G08":  "YZP",   # DSBGA 5-ball
    "SN74LVC1G11":  "YZP",   # DSBGA 6-ball
    "SN74LVC1G32":  "YZP",   # DSBGA 5-ball
    "SN74LVC1G74":  "DQE",   # X2SON 8-pin (NOT DSBGA — no bare die)
    "SN74LVC1G79":  "YZP",   # DSBGA 5-ball
    "SN74LVC1G86":  "YZP",   # DSBGA 5-ball
    "SN74LVC1G125": "YZP",   # DSBGA 5-ball
}

# LVC output drive capability
LVC_IOH_MAX_MA = 24.0  # Max output high current (mA)
LVC_IOL_MAX_MA = 24.0  # Max output low current (mA)
LED_TARGET_CURRENT_MA = 2.0  # Target LED current for 0402 SMD (mA)

# LED forward voltages for 0402 SMD LEDs
LED_FORWARD_VOLTAGE: Dict[str, float] = {
    "red":    1.8,   # V
    "green":  2.2,   # V
    "yellow": 2.0,   # V
    "blue":   3.0,   # V
    "white":  3.0,   # V
}

# Standard LED resistor values for 0402 SMD at 3.3V supply, 2mA target
LED_RESISTOR_VALUES: Dict[str, int] = {
    "red":    680,    # (3.3 - 1.8) / 0.002 = 750 → nearest E12: 680
    "green":  560,    # (3.3 - 2.2) / 0.002 = 550 → nearest E12: 560
    "yellow": 680,    # (3.3 - 2.0) / 0.002 = 650 → nearest E12: 680
    "blue":   150,    # (3.3 - 3.0) / 0.002 = 150 → nearest E12: 150
    "white":  150,    # (3.3 - 3.0) / 0.002 = 150 → nearest E12: 150
}


def calculate_led_resistor(vcc: float, vf: float, if_ma: float) -> int:
    """
    Calculate current-limiting resistor for LED.

    Args:
        vcc: Supply voltage (V)
        vf: LED forward voltage (V)
        if_ma: Desired LED current (mA)

    Returns:
        Resistor value in ohms (rounded to nearest standard E12 value)
    """
    resistance = (vcc - vf) / (if_ma / 1000)

    # Round to nearest standard E12 resistor value
    standard_values = [100, 120, 150, 180, 220, 270, 330, 390, 470, 560, 680, 820, 1000,
                       1200, 1500, 1800, 2200, 2700, 3300]
    return min(standard_values, key=lambda x: abs(x - resistance))


def get_part_number(function: str) -> str:
    """
    Look up TI Little Logic part number by function name.

    Args:
        function: Gate function (e.g., "and", "nand", "not", "dff")

    Returns:
        TI part number (e.g., "SN74LVC1G08")

    Raises:
        ValueError: If function is not in the lookup table
    """
    part = PART_LOOKUP.get(function.lower())
    if part is None:
        raise ValueError(f"Unknown function: {function}. "
                         f"Available: {', '.join(sorted(PART_LOOKUP.keys()))}")
    return part


def get_package_code(part_number: str) -> str:
    """
    Get DSBGA/package code for a given part number.

    Args:
        part_number: TI part number (e.g., "SN74LVC1G08")

    Returns:
        Package code (e.g., "YZP" for DSBGA)
    """
    code = PACKAGE_MAP.get(part_number)
    if code is None:
        raise ValueError(f"Unknown part number: {part_number}")
    return code


def generate_reference_designator(component_type: str, index: int) -> str:
    """
    Generate standard reference designator for component.

    Args:
        component_type: Type of component ('U', 'R', 'LED', 'C', etc.)
        index: Component number

    Returns:
        Reference designator string (e.g., 'U1', 'R42', 'D100')
    """
    if component_type.upper() == "LED":
        return f"D{index}"  # LEDs use 'D' prefix in KiCad
    return f"{component_type.upper()}{index}"


def grid_position(row: int, col: int, spacing: Tuple[float, float] = (4.0, 4.0)) -> Tuple[float, float]:
    """
    Calculate position in a grid layout.

    Default spacing is 4mm — suitable for DSBGA (1.75mm) + 0402 LED + resistor
    with clearance for routing.

    Args:
        row: Row index
        col: Column index
        spacing: (x_spacing, y_spacing) in mm

    Returns:
        (x, y) position in mm
    """
    return (col * spacing[0], row * spacing[1])
