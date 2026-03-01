"""
PCB layout generation utilities using kiutils.

Provides:
- create_dsbga_footprints() -- generate custom DSBGA footprints with numeric pad names
- PCBBuilder -- programmatic component placement for KiCad PCB files
"""

import copy
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from kiutils.board import Board
from kiutils.footprint import Footprint
from kiutils.items.brditems import GeneralSettings, LayerToken, Segment, SetupData, Via
from kiutils.items.common import Net, Position
from kiutils.items.zones import FillSettings, Hatch, Zone, ZonePolygon

from .common import (
    DSBGA5_BALL_TO_PIN,
    DSBGA6_BALL_TO_PIN,
    FOOTPRINT_MAP,
    KICAD_CLI,
    KICAD_FP_DIR,
    STOCK_DSBGA5_FP,
    STOCK_DSBGA6_FP,
    uid,
)


# ==============================================================
# Custom DSBGA Footprint Generation
# ==============================================================

def create_dsbga_footprints(output_dir: str) -> Tuple[str, str]:
    """Create DSBGA-5 and DSBGA-6 footprints with numeric pad names.

    Loads stock KiCad DSBGA footprints, renames BGA ball pads (A1/B1/C1/...)
    to numeric pin numbers (1/2/3/...) matching the KiCad 74xGxx symbols.

    Args:
        output_dir: Path to .pretty directory to write footprints into.

    Returns:
        Tuple of (dsbga5_path, dsbga6_path) for the created files.
    """
    os.makedirs(output_dir, exist_ok=True)

    # --- DSBGA-5 ---
    stock5_path = os.path.join(KICAD_FP_DIR, STOCK_DSBGA5_FP)
    fp5 = Footprint.from_file(stock5_path)
    fp5.entryName = "DSBGA-5_NumericPads"
    fp5.description = (
        "TI DSBGA-5 (YZP) with numeric pad names matching 74xGxx symbols. "
        "Based on Texas_DSBGA-5_0.8875x1.3875mm_Layout2x3_P0.5mm."
    )
    fp5.tags = "BGA 5 0.5 YZP DSBGA numeric"
    # Update Value property
    if "Value" in fp5.properties:
        fp5.properties["Value"] = "DSBGA-5_NumericPads"
    # Rename pads: ball name -> pin number
    for pad in fp5.pads:
        if pad.number in DSBGA5_BALL_TO_PIN:
            pad.number = DSBGA5_BALL_TO_PIN[pad.number]
    fp5.tstamp = uid()
    dsbga5_path = os.path.join(output_dir, "DSBGA-5_NumericPads.kicad_mod")
    fp5.to_file(dsbga5_path)
    _fix_footprint_file(dsbga5_path, stock5_path)

    # --- DSBGA-6 ---
    stock6_path = os.path.join(KICAD_FP_DIR, STOCK_DSBGA6_FP)
    fp6 = Footprint.from_file(stock6_path)
    fp6.entryName = "DSBGA-6_NumericPads"
    fp6.description = (
        "TI DSBGA-6 (YZP) with numeric pad names matching 74xGxx symbols. "
        "Based on Texas_DSBGA-6_0.9x1.4mm_Layout2x3_P0.5mm."
    )
    fp6.tags = "BGA 6 0.5 YZP DSBGA numeric"
    if "Value" in fp6.properties:
        fp6.properties["Value"] = "DSBGA-6_NumericPads"
    for pad in fp6.pads:
        if pad.number in DSBGA6_BALL_TO_PIN:
            pad.number = DSBGA6_BALL_TO_PIN[pad.number]
    fp6.tstamp = uid()
    dsbga6_path = os.path.join(output_dir, "DSBGA-6_NumericPads.kicad_mod")
    fp6.to_file(dsbga6_path)
    _fix_footprint_file(dsbga6_path, stock6_path)

    return dsbga5_path, dsbga6_path


# ==============================================================
# Netlist Parsing
# ==============================================================

def export_netlist(sch_path: str, output_path: str) -> str:
    """Export netlist from schematic using kicad-cli.

    Args:
        sch_path: Path to root .kicad_sch
        output_path: Path to write .xml netlist

    Returns:
        Path to the generated netlist file.
    """
    subprocess.run(
        [KICAD_CLI, "sch", "export", "netlist",
         "--format", "kicadxml", "-o", output_path, sch_path],
        capture_output=True, text=True, check=True,
    )
    return output_path


def parse_netlist(netlist_path: str) -> dict:
    """Parse a KiCad XML netlist file.

    Returns dict with:
        components: [{ref, value, lib, footprint, tstamp, pins: {pin_num: net_name}}, ...]
        nets: {net_name: net_number, ...}
        net_list: [(net_number, net_name, [(ref, pin)...]), ...]
    """
    tree = ET.parse(netlist_path)
    root = tree.getroot()

    # Parse components
    components = []
    comp_elements = root.find("components")
    if comp_elements is not None:
        for comp in comp_elements.findall("comp"):
            ref = comp.get("ref", "")
            value_el = comp.find("value")
            value = value_el.text if value_el is not None else ""
            lib_el = comp.find("libsource")
            lib = lib_el.get("lib", "") if lib_el is not None else ""
            part = lib_el.get("part", "") if lib_el is not None else ""
            fp_el = comp.find("footprint")
            footprint = fp_el.text if fp_el is not None else ""
            tstamp_el = comp.find("tstamps")
            tstamp = tstamp_el.text if tstamp_el is not None else ""
            # Extract hierarchy sheet path
            sheetpath_el = comp.find("sheetpath")
            sheetpath = ""
            if sheetpath_el is not None:
                sheetpath = sheetpath_el.get("names", "/")
            components.append({
                "ref": ref,
                "value": value,
                "lib": lib,
                "part": part,
                "footprint": footprint,
                "tstamp": tstamp,
                "sheetpath": sheetpath,
                "pins": {},
            })

    # Parse nets and build pin-to-net mapping
    nets_dict = OrderedDict()
    net_list = []
    nets_el = root.find("nets")
    if nets_el is not None:
        for net_el in nets_el.findall("net"):
            net_num = int(net_el.get("code", "0"))
            net_name = net_el.get("name", "")
            nets_dict[net_name] = net_num
            pin_refs = []
            for node in net_el.findall("node"):
                node_ref = node.get("ref", "")
                node_pin = node.get("pin", "")
                pin_refs.append((node_ref, node_pin))
                # Update component's pin-to-net mapping
                for comp in components:
                    if comp["ref"] == node_ref:
                        comp["pins"][node_pin] = net_name
                        break
            net_list.append((net_num, net_name, pin_refs))

    return {
        "components": components,
        "nets": nets_dict,
        "net_list": net_list,
    }


# ==============================================================
# PCBBuilder
# ==============================================================

# Default grid spacing for DSBGA + LED + resistor cells
DSBGA_GRID_SPACING = (4.0, 4.0)  # mm (x, y)

# 4-layer stackup layer ordinals
LAYER_FCU = 0
LAYER_IN1CU = 1
LAYER_IN2CU = 2
LAYER_BCU = 31


class PCBBuilder:
    """Builder for KiCad PCB files with programmatic component placement.

    Handles:
    - Loading and placing footprints from libraries
    - Net assignment to pads
    - Board outline (Edge.Cuts)
    - 4-layer stackup configuration
    - Copper pour zones (GND/VCC planes)
    """

    def __init__(self, title: str = "Untitled"):
        """Initialize a new PCB.

        Args:
            title: Board title for the title block.
        """
        self.board = Board.create_new()
        self.board.version = 20241229
        self.board.generator = "pcb_builder"
        if self.board.general is None:
            self.board.general = GeneralSettings(thickness=1.6)
        else:
            self.board.general.thickness = 1.6

        # Title block
        if self.board.titleBlock is None:
            from kiutils.items.common import TitleBlock
            self.board.titleBlock = TitleBlock()
        self.board.titleBlock.title = title

        # Net tracking: name -> Net object
        self._nets = {"": Net(number=0, name="")}
        self._net_counter = 0

        # Footprint template cache
        self._fp_cache: Dict[str, Footprint] = {}

        # Custom footprint library paths (lib_name -> dir_path)
        self._fp_lib_paths: Dict[str, str] = {}

    def add_fp_lib_path(self, lib_name: str, dir_path: str):
        """Register a footprint library directory for loading.

        Args:
            lib_name: Library nickname (e.g., "DSBGA_Packages")
            dir_path: Path to the .pretty directory
        """
        self._fp_lib_paths[lib_name] = dir_path

    def add_net(self, name: str) -> Net:
        """Add a net to the board.

        Args:
            name: Net name (e.g., "GND", "/A0")

        Returns:
            The Net object.
        """
        if name in self._nets:
            return self._nets[name]
        self._net_counter += 1
        net = Net(number=self._net_counter, name=name)
        self._nets[name] = net
        self.board.nets.append(net)
        return net

    def add_nets_from_netlist(self, netlist_data: dict):
        """Add all nets from a parsed netlist.

        Args:
            netlist_data: Result from parse_netlist()
        """
        for net_num, net_name, _pins in netlist_data["net_list"]:
            if net_name and net_name not in self._nets:
                self.add_net(net_name)

    def _resolve_footprint_path(self, lib_fp: str) -> str:
        """Resolve a library:footprint reference to a file path.

        Args:
            lib_fp: "Library:Footprint" string

        Returns:
            Absolute file path to the .kicad_mod file.
        """
        lib_name, fp_name = lib_fp.split(":", 1)

        # Check custom libraries first
        if lib_name in self._fp_lib_paths:
            return os.path.join(
                self._fp_lib_paths[lib_name], f"{fp_name}.kicad_mod")

        # Fall back to KiCad stock libraries
        return os.path.join(
            KICAD_FP_DIR, f"{lib_name}.pretty", f"{fp_name}.kicad_mod")

    def load_footprint(self, lib_fp: str) -> Footprint:
        """Load a footprint template (cached).

        Args:
            lib_fp: "Library:Footprint" reference

        Returns:
            Footprint object (template — do not modify directly).
        """
        if lib_fp in self._fp_cache:
            return self._fp_cache[lib_fp]

        path = self._resolve_footprint_path(lib_fp)
        fp = Footprint.from_file(path)
        self._fp_cache[lib_fp] = fp
        return fp

    def place_component(
        self,
        ref: str,
        lib_fp: str,
        x: float,
        y: float,
        angle: float = 0,
        layer: str = "F.Cu",
        net_map: Optional[Dict[str, str]] = None,
        tstamp: Optional[str] = None,
    ) -> Footprint:
        """Place a component on the board.

        Args:
            ref: Reference designator (e.g., "U1")
            lib_fp: "Library:Footprint" reference
            x, y: Position in mm
            angle: Rotation in degrees
            layer: Layer ("F.Cu" or "B.Cu")
            net_map: {pad_number: net_name} for pad net assignment
            tstamp: Schematic hierarchy path (for back-annotation)

        Returns:
            The placed Footprint instance.
        """
        template = self.load_footprint(lib_fp)
        fp = copy.deepcopy(template)

        fp.entryName = ref
        fp.position = Position(X=x, Y=y, angle=angle)
        fp.layer = layer
        fp.libId = lib_fp
        fp.tstamp = tstamp or uid()
        fp.path = f"/{tstamp}" if tstamp else ""

        # Set pad orientation to match footprint rotation.
        # KiCad stores pad orientation as pad_local + parent_rotation (historical).
        # GetFPRelativeOrientation() = stored_angle - parent_rotation.
        # For library match: stored_angle must equal parent_rotation so relative = 0.
        if angle:
            for pad in fp.pads:
                pad.position = Position(
                    X=pad.position.X, Y=pad.position.Y, angle=angle
                )

        # Set reference property
        if "Reference" in fp.properties:
            fp.properties["Reference"] = ref

        # Assign nets to pads
        if net_map:
            for pad in fp.pads:
                if pad.number in net_map:
                    net_name = net_map[pad.number]
                    if net_name in self._nets:
                        net_obj = self._nets[net_name]
                        pad.net = Net(number=net_obj.number, name=net_obj.name)

        self.board.footprints.append(fp)
        return fp

    def set_board_outline(
        self,
        width: float,
        height: float,
        origin_x: float = 0,
        origin_y: float = 0,
        corner_radius: float = 0,
    ):
        """Set board outline on Edge.Cuts layer.

        Args:
            width: Board width in mm
            height: Board height in mm
            origin_x, origin_y: Top-left corner position
            corner_radius: Fillet radius for rounded corners (0 = sharp)
        """
        import math
        from kiutils.items.gritems import GrArc, GrLine

        ox, oy = origin_x, origin_y
        w, h = width, height
        r = min(corner_radius, w / 2, h / 2)  # clamp to half-dimension

        if r < 0.01:
            # Sharp rectangular corners
            corners = [
                (ox, oy), (ox + w, oy),
                (ox + w, oy + h), (ox, oy + h),
            ]
            for i in range(4):
                x1, y1 = corners[i]
                x2, y2 = corners[(i + 1) % 4]
                line = GrLine(
                    start=Position(X=x1, Y=y1),
                    end=Position(X=x2, Y=y2),
                    layer="Edge.Cuts", width=0.05, tstamp=uid(),
                )
                self.board.graphicItems.append(line)
            return

        # Rounded corners: 4 lines + 4 quarter-circle arcs
        cos45 = math.cos(math.radians(45))

        # Edge lines (shortened by corner radius)
        edges = [
            ((ox + r, oy),       (ox + w - r, oy)),       # top
            ((ox + w, oy + r),   (ox + w, oy + h - r)),   # right
            ((ox + w - r, oy + h), (ox + r, oy + h)),     # bottom
            ((ox, oy + h - r),   (ox, oy + r)),           # left
        ]
        for (x1, y1), (x2, y2) in edges:
            line = GrLine(
                start=Position(X=x1, Y=y1),
                end=Position(X=x2, Y=y2),
                layer="Edge.Cuts", width=0.05, tstamp=uid(),
            )
            self.board.graphicItems.append(line)

        # Quarter-circle arcs at each corner
        # Each arc: (start, mid_on_arc, end) — mid is at 45° on the fillet
        arcs = [
            # Top-left: center (ox+r, oy+r)
            ((ox, oy + r),
             (ox + r - r * cos45, oy + r - r * cos45),
             (ox + r, oy)),
            # Top-right: center (ox+w-r, oy+r)
            ((ox + w - r, oy),
             (ox + w - r + r * cos45, oy + r - r * cos45),
             (ox + w, oy + r)),
            # Bottom-right: center (ox+w-r, oy+h-r)
            ((ox + w, oy + h - r),
             (ox + w - r + r * cos45, oy + h - r + r * cos45),
             (ox + w - r, oy + h)),
            # Bottom-left: center (ox+r, oy+h-r)
            ((ox + r, oy + h),
             (ox + r - r * cos45, oy + h - r + r * cos45),
             (ox, oy + h - r)),
        ]
        for (sx, sy), (mx, my), (ex, ey) in arcs:
            arc = GrArc(
                start=Position(X=round(sx, 4), Y=round(sy, 4)),
                mid=Position(X=round(mx, 4), Y=round(my, 4)),
                end=Position(X=round(ex, 4), Y=round(ey, 4)),
                layer="Edge.Cuts", width=0.05, tstamp=uid(),
            )
            self.board.graphicItems.append(arc)

    def set_4layer_stackup(self):
        """Configure board for 4 copper layers: F.Cu, In1.Cu, In2.Cu, B.Cu.

        In1.Cu = GND plane, In2.Cu = VCC plane.
        """
        # Find existing layer list and add inner layers if not present
        layer_ordinals = {layer.ordinal for layer in self.board.layers}

        if LAYER_IN1CU not in layer_ordinals:
            self.board.layers.append(
                LayerToken(ordinal=LAYER_IN1CU, name="In1.Cu",
                           type="power", userName=None))
        if LAYER_IN2CU not in layer_ordinals:
            self.board.layers.append(
                LayerToken(ordinal=LAYER_IN2CU, name="In2.Cu",
                           type="power", userName=None))

        # Sort layers by ordinal for clean output
        self.board.layers.sort(key=lambda l: l.ordinal)

    def set_layer_type(self, layer_name: str, layer_type: str):
        """Change a copper layer's type.

        Args:
            layer_name: Layer name (e.g., "B.Cu")
            layer_type: One of "signal", "power", "mixed", "jumper"
        """
        for layer in self.board.layers:
            if layer.name == layer_name:
                layer.type = layer_type
                return
        raise ValueError(f"Layer {layer_name!r} not found in board")

    def add_zone(
        self,
        net_name: str,
        layer: str,
        outline: List[Tuple[float, float]],
        clearance: float = 0.3,
        min_thickness: float = 0.254,
        priority: int = 0,
    ):
        """Add a copper pour zone.

        Args:
            net_name: Net name for the zone (e.g., "GND")
            layer: Layer name (e.g., "In1.Cu")
            outline: List of (x, y) corner points
            clearance: Zone clearance in mm
            min_thickness: Minimum copper width in mm
            priority: Zone priority (higher = fills first)
        """
        net_obj = self._nets.get(net_name)
        if net_obj is None:
            net_obj = self.add_net(net_name)

        zone = Zone()
        zone.net = net_obj.number
        zone.netName = net_name
        zone.layers = [layer]
        zone.tstamp = uid()
        zone.clearance = clearance
        zone.minThickness = min_thickness
        zone.priority = priority
        zone.hatch = Hatch(style="edge", pitch=0.508)

        # Set fill settings
        zone.fillSettings = FillSettings(
            yes=True,
            mode=None,  # None = solid fill
            thermalGap=0.508,
            thermalBridgeWidth=0.508,
        )

        # Create outline polygon
        polygon = ZonePolygon()
        polygon.coordinates = [Position(X=x, Y=y) for x, y in outline]
        zone.polygons = [polygon]

        self.board.zones.append(zone)

    # ----------------------------------------------------------
    # Routing helpers
    # ----------------------------------------------------------

    def build_ref_index(self):
        """Build reference-to-footprint lookup from placed footprints.

        Call once after all components are placed and before routing.
        """
        self._ref_index: Dict[str, Footprint] = {}
        for fp in self.board.footprints:
            ref = fp.properties.get("Reference", "")
            if ref:
                self._ref_index[ref] = fp

    def get_pad_position(self, ref: str, pad_number: str) -> Tuple[float, float]:
        """Get absolute board position of a component pad.

        Args:
            ref: Reference designator (e.g., "U34")
            pad_number: Pad number as string (e.g., "4")

        Returns:
            (x, y) absolute position in mm, rounded to 2 decimals.
        """
        import math

        fp = self._ref_index.get(ref)
        if fp is None:
            raise ValueError(f"Reference {ref} not found in board")

        fp_x, fp_y = fp.position.X, fp.position.Y
        angle_rad = math.radians(fp.position.angle or 0)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)

        for pad in fp.pads:
            if pad.number == pad_number:
                px, py = pad.position.X, pad.position.Y
                # KiCad uses clockwise rotation (positive angle = CW in Y-down coords)
                abs_x = fp_x + px * cos_a + py * sin_a
                abs_y = fp_y - px * sin_a + py * cos_a
                return (round(abs_x, 2), round(abs_y, 2))

        raise ValueError(f"Pad {pad_number} not found on {ref}")

    def get_pad_net(self, ref: str, pad_number: str) -> Optional[int]:
        """Get the net number assigned to a component pad.

        Args:
            ref: Reference designator
            pad_number: Pad number as string

        Returns:
            Net number (int), or None if no net assigned.
        """
        fp = self._ref_index.get(ref)
        if fp is None:
            return None
        for pad in fp.pads:
            if pad.number == pad_number:
                if pad.net and pad.net.number:
                    return pad.net.number
                return None
        return None

    def get_net_number(self, net_name: str) -> Optional[int]:
        """Look up net number by name.

        Returns:
            Net number, or None if not found.
        """
        net_obj = self._nets.get(net_name)
        return net_obj.number if net_obj else None

    def add_trace(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        net: int,
        width: float = 0.2,
        layer: str = "F.Cu",
    ) -> Segment:
        """Add a copper trace segment.

        Args:
            start: (x, y) start position in mm
            end: (x, y) end position in mm
            net: Net number (int)
            width: Trace width in mm
            layer: Copper layer name

        Returns:
            The created Segment.
        """
        seg = Segment(
            start=Position(X=round(start[0], 2), Y=round(start[1], 2)),
            end=Position(X=round(end[0], 2), Y=round(end[1], 2)),
            width=width,
            layer=layer,
            net=net,
            tstamp=uid(),
        )
        self.board.traceItems.append(seg)
        return seg

    def add_via(
        self,
        position: Tuple[float, float],
        net: int,
        size: float = 0.6,
        drill: float = 0.3,
        layers: Optional[List[str]] = None,
    ) -> Via:
        """Add a via.

        Args:
            position: (x, y) in mm
            net: Net number (int)
            size: Via outer diameter in mm
            drill: Drill diameter in mm
            layers: Layer pair (default ["F.Cu", "B.Cu"])

        Returns:
            The created Via.
        """
        if layers is None:
            layers = ["F.Cu", "B.Cu"]
        v = Via(
            position=Position(X=round(position[0], 2), Y=round(position[1], 2)),
            size=size,
            drill=drill,
            layers=layers,
            net=net,
            tstamp=uid(),
        )
        self.board.traceItems.append(v)
        return v

    def add_l_trace(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        net: int,
        width: float = 0.2,
        layer: str = "F.Cu",
        horizontal_first: bool = True,
        chamfer: float = 0.3,
    ) -> List[Segment]:
        """Add an L-shaped trace with 45-degree chamfered bend.

        If start and end are aligned on one axis, creates a single segment.
        Otherwise creates 3 segments: straight, 45-degree diagonal, straight.
        The chamfer is clamped to the shorter leg to avoid overshooting.

        Args:
            start: (x, y) start position
            end: (x, y) end position
            net: Net number
            width: Trace width in mm
            layer: Copper layer
            horizontal_first: If True, route horizontal then vertical
            chamfer: 45-degree chamfer length at bend (mm)

        Returns:
            List of created Segments (1-3).
        """
        sx, sy = round(start[0], 2), round(start[1], 2)
        ex, ey = round(end[0], 2), round(end[1], 2)

        # Aligned — single segment
        if abs(sx - ex) < 0.01 or abs(sy - ey) < 0.01:
            return [self.add_trace((sx, sy), (ex, ey), net, width, layer)]

        h_len = abs(ex - sx)
        v_len = abs(ey - sy)
        d = min(chamfer, h_len * 0.5, v_len * 0.5)

        sign_h = 1 if ex > sx else -1
        sign_v = 1 if ey > sy else -1

        if horizontal_first:
            # horizontal → 45° chamfer → vertical
            p1 = (round(ex - sign_h * d, 2), sy)
            p2 = (ex, round(sy + sign_v * d, 2))
        else:
            # vertical → 45° chamfer → horizontal
            p1 = (sx, round(ey - sign_v * d, 2))
            p2 = (round(sx + sign_h * d, 2), ey)

        segs = []
        # Only add segment if length > 0
        if abs(sx - p1[0]) > 0.01 or abs(sy - p1[1]) > 0.01:
            segs.append(self.add_trace((sx, sy), p1, net, width, layer))
        segs.append(self.add_trace(p1, p2, net, width, layer))
        if abs(p2[0] - ex) > 0.01 or abs(p2[1] - ey) > 0.01:
            segs.append(self.add_trace(p2, (ex, ey), net, width, layer))
        return segs

    def add_u_trace(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        net: int,
        depth: float,
        width: float = 0.2,
        layer: str = "F.Cu",
        chamfer: float = 0.3,
    ) -> List[Segment]:
        """Add a U-shaped trace: vertical down, horizontal, vertical up.

        Used to route around obstacles by going below them.  Both corners
        get 45-degree chamfers.

        Args:
            start: (x, y) start position (top-left of U)
            end: (x, y) end position (top-right of U)
            depth: How far below start/end the U goes (positive = down in KiCad)
            net: Net number
            width: Trace width in mm
            layer: Copper layer
            chamfer: 45-degree chamfer length at each bend (mm)

        Returns:
            List of created Segments (3-5).
        """
        sx, sy = round(start[0], 2), round(start[1], 2)
        ex, ey = round(end[0], 2), round(end[1], 2)
        bottom_y = round(max(sy, ey) + depth, 2)

        # Clamp chamfer to available leg lengths
        left_leg = bottom_y - sy
        right_leg = bottom_y - ey
        h_span = abs(ex - sx)
        d = min(chamfer, left_leg * 0.5, right_leg * 0.5, h_span * 0.25)
        sign_h = 1 if ex > sx else -1

        segs = []

        # Leg 1: vertical down
        p_v1_end = (sx, round(bottom_y - d, 2))
        if abs(sy - p_v1_end[1]) > 0.01:
            segs.append(self.add_trace((sx, sy), p_v1_end, net, width, layer))

        # Chamfer 1: 45° from vertical to horizontal
        p_c1 = (round(sx + sign_h * d, 2), bottom_y)
        segs.append(self.add_trace(p_v1_end, p_c1, net, width, layer))

        # Horizontal bottom
        p_c2_start = (round(ex - sign_h * d, 2), bottom_y)
        if abs(p_c1[0] - p_c2_start[0]) > 0.01:
            segs.append(self.add_trace(p_c1, p_c2_start, net, width, layer))

        # Chamfer 2: 45° from horizontal to vertical
        p_v2_start = (ex, round(bottom_y - d, 2))
        segs.append(self.add_trace(p_c2_start, p_v2_start, net, width, layer))

        # Leg 2: vertical up
        if abs(p_v2_start[1] - ey) > 0.01:
            segs.append(self.add_trace(p_v2_start, (ex, ey), net, width, layer))

        return segs

    # ----------------------------------------------------------
    # Silkscreen helpers
    # ----------------------------------------------------------

    def add_silkscreen_rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        layer: str = "F.SilkS",
        stroke_width: float = 0.15,
    ):
        """Add a silkscreen rectangle.

        Args:
            x, y: Top-left corner position in mm
            width: Rectangle width in mm
            height: Rectangle height in mm
            layer: Silkscreen layer ("F.SilkS" or "B.SilkS")
            stroke_width: Line width in mm
        """
        from kiutils.items.gritems import GrRect

        rect = GrRect(
            start=Position(X=round(x, 2), Y=round(y, 2)),
            end=Position(X=round(x + width, 2), Y=round(y + height, 2)),
            layer=layer,
            width=stroke_width,
            tstamp=uid(),
        )
        self.board.graphicItems.append(rect)

    def add_silkscreen_text(
        self,
        text: str,
        x: float,
        y: float,
        size: float = 1.0,
        layer: str = "F.SilkS",
    ):
        """Add silkscreen text.

        Args:
            text: Text string to display
            x, y: Position in mm
            size: Font height and width in mm
            layer: Silkscreen layer ("F.SilkS" or "B.SilkS")
        """
        from kiutils.items.gritems import GrText
        from kiutils.items.common import Effects

        effects = Effects()
        effects.font.height = size
        effects.font.width = size

        gr_text = GrText(
            text=text,
            position=Position(X=round(x, 2), Y=round(y, 2)),
            layer=layer,
            effects=effects,
            tstamp=uid(),
        )
        self.board.graphicItems.append(gr_text)

    def save(self, filepath: str):
        """Save the PCB to a file.

        Args:
            filepath: Path to .kicad_pcb file
        """
        self.board.to_file(filepath)
        self._fix_footprints(filepath)

    def _fix_footprints(self, filepath: str):
        """Post-process saved PCB to fix kiutils footprint serialization.

        kiutils drops property position/layer/effects metadata and
        ``(embedded_fonts no)`` when round-tripping footprints.  This causes
        ``lib_footprint_mismatch`` DRC warnings.  Fix by reading the original
        .kicad_mod files and injecting correct property definitions.
        """
        text = open(filepath, "r", encoding="utf-8").read()

        # Collect unique library footprint references used in the PCB
        lib_fp_refs = set(re.findall(r'\(footprint "([^"]+)"', text))

        # For each library footprint, load the raw .kicad_mod and extract
        # property definitions and embedded_fonts presence.
        lib_info = {}  # lib_fp -> {props: {name: sexp}, embedded_fonts: bool}
        for lib_fp in lib_fp_refs:
            try:
                mod_path = self._resolve_footprint_path(lib_fp)
            except Exception:
                continue
            if not os.path.isfile(mod_path):
                continue

            raw = open(mod_path, "r", encoding="utf-8").read().replace("\r", "")
            info = {
                "props": {},
                "embedded_fonts": "(embedded_fonts" in raw,
                "has_tedit": bool(re.search(r"\(tedit\s", raw)),
            }

            for prop_name in ("Reference", "Value"):
                sexp = _extract_balanced_sexp(raw, f'(property "{prop_name}"')
                # Only store if the library has multi-line metadata (position,
                # layer, effects).  Bare single-line properties don't need
                # fixing (e.g., kiutils-generated custom footprints).
                if sexp and "\n" in sexp:
                    info["props"][prop_name] = sexp

            lib_info[lib_fp] = info

        if not any(info["props"] or info["embedded_fonts"]
                   for info in lib_info.values()):
            return  # Nothing to fix

        # Process the PCB text line-by-line
        lines = text.split("\n")
        result = []
        current_lib = None

        for line in lines:
            # Track which footprint block we are inside
            fp_m = re.match(r'(\s*)\(footprint "([^"]+)"', line)
            if fp_m:
                current_lib = fp_m.group(2)

            info = lib_info.get(current_lib) if current_lib else None

            # --- Remove (tedit ...) if library doesn't have it ---
            if info and not info.get("has_tedit") and "(tedit " in line:
                line = re.sub(r"\(tedit [0-9a-f]+\)\s*", "", line)

            # --- Fix unquoted (generator name) → (generator "name") ---
            # kiutils drops quotes around the generator value; KiCad
            # native format requires them.
            gen_m = re.search(
                r'\(generator ([^)"]\S+)\)', line)
            if gen_m:
                bare = gen_m.group(1)
                line = line.replace(
                    f"(generator {bare})",
                    f'(generator "{bare}")',
                )

            # --- Fix bare property lines ---
            if info and info["props"]:
                prop_m = re.match(
                    r'(\s+)\(property "(Reference|Value)" "([^"]*)"\)\s*$',
                    line,
                )
                if prop_m:
                    indent = prop_m.group(1)
                    prop_name = prop_m.group(2)
                    actual_value = prop_m.group(3)
                    template = info["props"].get(prop_name)
                    if template:
                        fixed = _reindent_sexp(template, indent)
                        # Substitute the actual value for the template value
                        fixed = re.sub(
                            r'(property "' + prop_name + r'" ")[^"]*"',
                            r"\g<1>" + re.escape(actual_value) + '"',
                            fixed,
                            count=1,
                        )
                        result.append(fixed)
                        continue

            # --- Insert (embedded_fonts no) before (model ---
            if info and info["embedded_fonts"] and "(model " in line:
                # Only insert if not already present nearby
                if not any("embedded_fonts" in r for r in result[-5:]):
                    ef_indent = re.match(r"(\s*)", line).group(1)
                    result.append(f"{ef_indent}(embedded_fonts no)")

            result.append(line)

        text = "\n".join(result)

        # --- Fix graphic element attribute ordering ---
        # kiutils puts (layer) before (stroke)/(fill); KiCad expects it after.
        text = _fix_graphic_attr_order(text)

        # --- Fix unquoted generator values ---
        text = _fix_unquoted_generator(text)

        # --- Fix bare (remove_unused_layers) → (remove_unused_layers no) ---
        # kiutils misinterprets "(remove_unused_layers no)" as boolean True and
        # serializes it as "(remove_unused_layers)" without value. KiCad sees
        # this as different from the library's "(remove_unused_layers no)".
        text = text.replace('(remove_unused_layers)', '(remove_unused_layers no)')

        open(filepath, "w", encoding="utf-8").write(text)


def _fix_unquoted_generator(text: str) -> str:
    """Fix unquoted (generator name) → (generator "name").

    kiutils serializes the generator field without quotes, but KiCad
    native format requires them.  This mismatch causes
    ``lib_footprint_mismatch`` DRC warnings.
    """
    return re.sub(
        r'\(generator ([^)"]\S+)\)',
        lambda m: f'(generator "{m.group(1)}")',
        text,
    )


def _fix_graphic_attr_order(text: str) -> str:
    """Fix attribute ordering in footprint graphic elements.

    kiutils serializes ``(layer ...)`` BEFORE ``(stroke ...)`` and
    ``(fill ...)``, but KiCad native format places ``(layer ...)`` AFTER
    them.  This ordering difference causes ``lib_footprint_mismatch`` DRC
    warnings on every footprint.

    Handles fp_line, fp_rect, fp_circle, and fp_poly elements in both
    single-line and multi-line formats.
    """
    # Pattern: find (layer "X") followed by (stroke ...) on the same line.
    # We need to move (layer "X") to after (stroke ...) and optional (fill ...).
    #
    # Works for both single-line and closing lines of multi-line elements:
    #   Single: (fp_line (start ...) (end ...) (layer "X") (stroke ...))
    #   Multi:  ) (layer "X") (stroke ...) (fill yes))   [closing line of fp_poly]

    def _reorder_line(line):
        # Quick skip: only process lines where (layer is followed by (stroke
        layer_idx = line.find("(layer ")
        stroke_idx = line.find("(stroke ")
        if layer_idx == -1 or stroke_idx == -1 or layer_idx > stroke_idx:
            return line

        # Extract the (layer "X") token
        layer_m = re.search(r'\(layer\s+"[^"]+"\)', line)
        if not layer_m:
            return line

        layer_sexp = layer_m.group(0)

        # Remove (layer "X") from its current position (and trailing space)
        before = line[:layer_m.start()]
        after = line[layer_m.end():]
        combined = before.rstrip() + " " + after.lstrip()

        # Find where to re-insert: after (stroke ...) and optional (fill ...)
        # Find the end of (stroke ...) by counting balanced parens
        s_idx = combined.find("(stroke ")
        if s_idx == -1:
            return line  # Shouldn't happen
        depth = 0
        insert_after = s_idx
        for i in range(s_idx, len(combined)):
            if combined[i] == "(":
                depth += 1
            elif combined[i] == ")":
                depth -= 1
                if depth == 0:
                    insert_after = i + 1
                    break

        # Check for (fill ...) immediately after stroke
        rest = combined[insert_after:].lstrip()
        if rest.startswith("(fill "):
            fill_m = re.match(r'\(fill\s+\w+\)', rest)
            if fill_m:
                insert_after = combined.index(rest, insert_after) + fill_m.end()

        # Re-insert (layer "X") at the found position
        return (combined[:insert_after] + " " + layer_sexp
                + combined[insert_after:])

    lines = text.split("\n")
    result = []
    for line in lines:
        result.append(_reorder_line(line))
    return "\n".join(result)


def _fix_font_sizes(text: str) -> Tuple[str, int]:
    """Fix font sizes changed by KiCad from 1mm to 1.27mm.

    KiCad auto-applies 1.27mm font sizes when opening/saving a board.
    Stock library footprints use 1mm.  This difference causes
    ``lib_footprint_mismatch`` and ``silk_overlap`` DRC warnings.

    Returns:
        (fixed_text, count) tuple.
    """
    count = text.count("(size 1.27 1.27)")
    return text.replace("(size 1.27 1.27)", "(size 1 1)"), count


def _remove_extra_properties(text: str) -> Tuple[str, int]:
    """Remove Datasheet and Description properties auto-added by KiCad.

    KiCad adds these to footprints that don't have them in the stock
    library.  Their presence causes ``lib_footprint_mismatch`` warnings.

    Returns:
        (fixed_text, count_removed)
    """
    count = 0
    for prop_name in ("Datasheet", "Description"):
        result_lines = []
        lines = text.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]
            if re.match(r'\s*\(property\s+"' + re.escape(prop_name) + r'"\s',
                        line):
                # Found property start -- skip until balanced
                depth = line.count('(') - line.count(')')
                while depth > 0 and i + 1 < len(lines):
                    i += 1
                    depth += lines[i].count('(') - lines[i].count(')')
                count += 1
                i += 1
                continue
            result_lines.append(line)
            i += 1
        text = '\n'.join(result_lines)
    return text, count


def _extract_balanced_sexp(text: str, prefix: str) -> Optional[str]:
    """Extract a balanced s-expression starting with *prefix* from *text*.

    Returns the full s-expression string (from opening ``(`` to matching
    ``)``), or ``None`` if *prefix* is not found.
    """
    idx = text.find(prefix)
    if idx == -1:
        return None
    depth = 0
    for i in range(idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[idx : i + 1]
    return None


def _reindent_sexp(sexp: str, base_indent: str) -> str:
    """Re-indent an s-expression to start at *base_indent*.

    Strips all leading whitespace (tabs or spaces) and re-adds it
    proportionally: the first line gets *base_indent*, and subsequent
    lines get *base_indent* plus the extra whitespace they had relative
    to the first line (converted to spaces, 2 per tab level).
    """
    lines = sexp.split("\n")
    if not lines:
        return sexp

    # Measure leading whitespace width of the first line
    first_ws = len(lines[0]) - len(lines[0].lstrip())

    result = []
    for line in lines:
        ws = len(line) - len(line.lstrip())
        extra = max(0, ws - first_ws)
        result.append(base_indent + " " * extra + line.lstrip())

    return "\n".join(result)


def _fix_footprint_file(fp_path: str, stock_path: str):
    """Post-process a kiutils-saved .kicad_mod to restore metadata from stock.

    Fixes property position/layer/effects and ``(embedded_fonts no)`` that
    kiutils drops when round-tripping footprints.  Used by
    :func:`create_dsbga_footprints` to keep custom library files correct.
    """
    raw_stock = open(stock_path, "r", encoding="utf-8").read().replace("\r", "")
    text = open(fp_path, "r", encoding="utf-8").read()

    for prop_name in ("Reference", "Value"):
        stock_sexp = _extract_balanced_sexp(raw_stock, f'(property "{prop_name}"')
        if stock_sexp is None or "\n" not in stock_sexp:
            continue

        # Find the bare property in the generated file
        pat = re.compile(
            r'([ \t]*)\(property "' + prop_name + r'" "([^"]*)"\)',
        )
        m = pat.search(text)
        if not m:
            continue

        indent = m.group(1)
        actual_value = m.group(2)

        fixed = _reindent_sexp(stock_sexp, indent)
        # Substitute the actual value for the stock template value
        fixed = re.sub(
            r'(property "' + prop_name + r'" ")[^"]*"',
            r"\g<1>" + re.escape(actual_value) + '"',
            fixed,
            count=1,
        )
        text = text[: m.start()] + fixed + text[m.end() :]

    # Add (embedded_fonts no) if stock has it and generated file doesn't
    if "(embedded_fonts" in raw_stock and "(embedded_fonts" not in text:
        # Insert before (model line
        model_m = re.search(r"^(\s*)\(model ", text, re.MULTILINE)
        if model_m:
            ef_line = f"{model_m.group(1)}(embedded_fonts no)\n"
            text = text[: model_m.start()] + ef_line + text[model_m.start() :]

    # Fix graphic element attribute ordering (layer before stroke)
    text = _fix_graphic_attr_order(text)

    # Fix unquoted generator values
    text = _fix_unquoted_generator(text)

    open(fp_path, "w", encoding="utf-8").write(text)


def _fix_pad_orientations(text: str) -> Tuple[str, int]:
    """Fix pad orientations to match their parent footprint rotation.

    KiCad stores pad orientation as pad_local_angle + parent_rotation (historical).
    GetFPRelativeOrientation() = stored_angle - parent_rotation.
    For pads to match the library, stored_angle must equal parent_rotation
    so that the relative orientation = 0.

    Without this fix, rotated footprints trigger lib_footprint_mismatch DRC
    warnings with "Pad N orientation differs."

    Returns:
        (fixed_text, count_of_pads_fixed)
    """
    lines = text.split("\n")
    result = []
    fp_angle = 0.0  # current footprint rotation
    got_fp_at = False  # whether we've seen the footprint's own (at ...)
    in_pad = False  # currently inside a (pad ...) block
    fix_count = 0

    for line in lines:
        stripped = line.strip()

        # Detect footprint start — reset state
        if stripped.startswith("(footprint "):
            fp_angle = 0.0
            got_fp_at = False
            in_pad = False

        # Capture ONLY the footprint-level (at x y angle).
        # This is the first (at ...) after (footprint ...) and before any (pad ...).
        if not got_fp_at and not in_pad and stripped.startswith("(at "):
            m = re.match(r'\(at\s+[-\d.]+\s+[-\d.]+(?:\s+([-\d.]+))?\)', stripped)
            if m:
                fp_angle = float(m.group(1)) if m.group(1) else 0.0
                got_fp_at = True

        # Track entry into pad blocks
        if stripped.startswith("(pad "):
            in_pad = True

        # Fix pad (at x y) -> (at x y fp_angle) inside pad blocks
        if in_pad and fp_angle != 0 and stripped.startswith("(at "):
            m = re.match(r'(\s*)\(at\s+([-\d.]+)\s+([-\d.]+)\)(\s*)$', line)
            if m:
                indent, x, y, trail = m.group(1), m.group(2), m.group(3), m.group(4)
                result.append(f"{indent}(at {x} {y} {fp_angle:g}){trail}")
                fix_count += 1
                continue

        # Also handle single-line pads: (pad ... (at X Y) (size ...) ...)
        if in_pad and fp_angle != 0 and "(at " in stripped and "(size " in stripped:
            def fix_pad_at(match):
                nonlocal fix_count
                fix_count += 1
                return f"(at {match.group(1)} {match.group(2)} {fp_angle:g})"

            new_line = re.sub(
                r'\(at\s+([-\d.]+)\s+([-\d.]+)\)(?=\s*\(size\b)',
                fix_pad_at,
                line,
            )
            if new_line != line:
                result.append(new_line)
                in_pad = False
                continue

        # Detect end of pad block
        if in_pad and stripped == ")":
            in_pad = False

        result.append(line)

    return "\n".join(result), fix_count


def fix_pcb_drc(filepath: str) -> dict:
    """Apply DRC fixes to an existing PCB file (e.g., after manual routing).

    Fixes:
    - Pad orientations to match footprint rotation (lib_footprint_mismatch)
    - Graphic element attribute ordering (layer before stroke)
    - Font sizes changed by KiCad (1.27mm -> 1mm)
    - Extra Datasheet/Description properties added by KiCad

    Args:
        filepath: Path to .kicad_pcb file

    Returns:
        dict with counts of fixes applied.
    """
    text = open(filepath, "r", encoding="utf-8").read()
    stats = {}

    # Fix pad orientations (must match footprint rotation)
    text, n = _fix_pad_orientations(text)
    stats["pad_orientations"] = n

    # Note: KiCad adds remove_unused_layers/keep_end_layers/zone_layer_connections
    # to thru-hole pads on multi-layer boards. These cause lib_footprint_mismatch
    # on the connector but are needed for correct DRC on internal layers.
    # The single connector mismatch is acceptable.

    # Fix font sizes (KiCad auto-changes 1mm to 1.27mm on save)
    text, n = _fix_font_sizes(text)
    stats["font_fixes"] = n

    # Remove extra Datasheet/Description properties
    text, n = _remove_extra_properties(text)
    stats["props_removed"] = n

    # Fix graphic element attribute ordering
    old = text
    text = _fix_graphic_attr_order(text)
    stats["attr_reordered"] = sum(
        1 for a, b in zip(old.split("\n"), text.split("\n")) if a != b
    )

    # Fix unquoted generator values
    old = text
    text = _fix_unquoted_generator(text)
    stats["generator_fixed"] = sum(
        1 for a, b in zip(old.split("\n"), text.split("\n")) if a != b
    )

    open(filepath, "w", encoding="utf-8").write(text)
    return stats


def hide_footprint_text(filepath: str) -> int:
    """Hide all footprint Reference/Value text and fab text in a PCB file.

    Adds ``(hide yes)`` to effects blocks of:
    - ``(property "Reference" ...)`` on F.SilkS
    - ``(property "Value" ...)`` on F.SilkS
    - ``(fp_text user ...)`` on F.Fab

    This eliminates silk_over_copper and silk_overlap DRC warnings caused
    by tiny 0402/DSBGA reference text overlapping at high density.

    Args:
        filepath: Path to .kicad_pcb file (modified in place)

    Returns:
        Number of text items hidden.
    """
    text = open(filepath, "r", encoding="utf-8").read()
    count = 0

    # Hide (property "Reference"/"Value" ...) blocks that have (effects ...)
    # Insert (hide yes) after (font ...) inside (effects ...)
    def _hide_property(match):
        nonlocal count
        block = match.group(0)
        if "(hide yes)" in block:
            return block  # already hidden
        # Insert (hide yes) after the closing paren of (font ...)
        # Find the (effects block and add hide after font
        font_end = block.rfind(")")  # last ) is the property close
        effects_end = block.rfind(")", 0, font_end)  # second-to-last ) is effects close
        if effects_end > 0:
            count += 1
            return block[:effects_end] + "\n                (hide yes)\n            " + block[effects_end:]
        return block

    # Match property blocks with effects (multiline)
    text = re.sub(
        r'\(property\s+"(?:Reference|Value)"\s+"[^"]*"[^)]*'
        r'\(effects\s*\n[^)]*\(font[^)]*\)[^)]*\)',
        _hide_property,
        text,
        flags=re.DOTALL,
    )

    # Hide (fp_text ...) blocks on F.Fab
    def _hide_fp_text(match):
        nonlocal count
        block = match.group(0)
        if "(hide yes)" in block:
            return block
        font_end = block.rfind(")")
        effects_end = block.rfind(")", 0, font_end)
        if effects_end > 0:
            count += 1
            return block[:effects_end] + "\n                (hide yes)\n            " + block[effects_end:]
        return block

    text = re.sub(
        r'\(fp_text\s+\w+\s+"[^"]*"[^)]*\(layer\s+"F\.Fab"\)[^)]*'
        r'\(effects\s*\n[^)]*\(font[^)]*\)[^)]*\)',
        _hide_fp_text,
        text,
        flags=re.DOTALL,
    )

    open(filepath, "w", encoding="utf-8").write(text)
    return count


def _patch_project_severity(pro_path: str, rule: str, severity: str) -> bool:
    """Set a DRC rule severity in a .kicad_pro file, handling duplicates."""
    with open(pro_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    severities = (
        data.get("board", {}).get("design_settings", {}).get("rule_severities", {})
    )
    if severities.get(rule) == severity:
        return False  # already correct
    severities[rule] = severity
    with open(pro_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    return True


def get_footprint_for_part(part_name: str) -> Optional[str]:
    """Look up the footprint reference for a schematic part name.

    Args:
        part_name: Symbol base name (e.g., "74LVC1G08", "R_Small")

    Returns:
        "Library:Footprint" string, or None if not mapped.
    """
    return FOOTPRINT_MAP.get(part_name)
