"""
PCB layout generation utilities using kiutils.

Provides:
- create_dsbga_footprints() -- generate custom DSBGA footprints with numeric pad names
- PCBBuilder -- programmatic component placement for KiCad PCB files
"""

import copy
import os
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
    ):
        """Set rectangular board outline on Edge.Cuts layer.

        Args:
            width: Board width in mm
            height: Board height in mm
            origin_x, origin_y: Top-left corner position
        """
        from kiutils.items.gritems import GrLine

        corners = [
            (origin_x, origin_y),
            (origin_x + width, origin_y),
            (origin_x + width, origin_y + height),
            (origin_x, origin_y + height),
        ]

        for i in range(4):
            x1, y1 = corners[i]
            x2, y2 = corners[(i + 1) % 4]
            line = GrLine(
                start=Position(X=x1, Y=y1),
                end=Position(X=x2, Y=y2),
                layer="Edge.Cuts",
                width=0.05,
                tstamp=uid(),
            )
            self.board.graphicItems.append(line)

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

    def save(self, filepath: str):
        """Save the PCB to a file.

        Args:
            filepath: Path to .kicad_pcb file
        """
        self.board.to_file(filepath)


def get_footprint_for_part(part_name: str) -> Optional[str]:
    """Look up the footprint reference for a schematic part name.

    Args:
        part_name: Symbol base name (e.g., "74LVC1G08", "R_Small")

    Returns:
        "Library:Footprint" string, or None if not mapped.
    """
    return FOOTPRINT_MAP.get(part_name)
