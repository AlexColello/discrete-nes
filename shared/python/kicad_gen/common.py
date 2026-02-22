"""
Common utilities for KiCad file generation.

Provides shared functions and constants used across schematic and PCB generation.
"""

from typing import Tuple

# Standard component values
LED_RESISTOR_VALUES = {
    "red": 330,      # Ohms, for red LEDs at 5V
    "green": 330,    # Ohms, for green LEDs at 5V
    "yellow": 330,   # Ohms, for yellow LEDs at 5V
    "blue": 470,     # Ohms, for blue LEDs at 5V (higher Vf)
}

# Power supply voltages
VCC = 5.0  # Volts

# Logic families
LOGIC_FAMILY_74HC = "74HC"
LOGIC_FAMILY_74HCT = "74HCT"
LOGIC_FAMILY_74AC = "74AC"


def calculate_led_resistor(vcc: float, vf: float, if_ma: float) -> int:
    """
    Calculate current-limiting resistor for LED.

    Args:
        vcc: Supply voltage (V)
        vf: LED forward voltage (V)
        if_ma: Desired LED current (mA)

    Returns:
        Resistor value in ohms (rounded to nearest standard value)
    """
    resistance = (vcc - vf) / (if_ma / 1000)

    # Round to nearest standard E12 resistor value
    standard_values = [100, 120, 150, 180, 220, 270, 330, 390, 470, 560, 680, 820, 1000]
    return min(standard_values, key=lambda x: abs(x - resistance))


def generate_reference_designator(component_type: str, index: int) -> str:
    """
    Generate standard reference designator for component.

    Args:
        component_type: Type of component ('U', 'R', 'LED', 'C', etc.)
        index: Component number

    Returns:
        Reference designator string (e.g., 'U1', 'R42', 'LED100')
    """
    if component_type.upper() == "LED":
        return f"D{index}"  # LEDs use 'D' prefix in KiCad
    return f"{component_type.upper()}{index}"


def grid_position(row: int, col: int, spacing: Tuple[float, float] = (10.0, 10.0)) -> Tuple[float, float]:
    """
    Calculate position in a grid layout.

    Args:
        row: Row index
        col: Column index
        spacing: (x_spacing, y_spacing) in mm

    Returns:
        (x, y) position in mm
    """
    return (col * spacing[0], row * spacing[1])
