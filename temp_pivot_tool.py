"""
Temp Pivot Tool for Autodesk Maya

Create a temporary pivot for selected objects so you can rotate them from wherever you like.
It doesn't change the actual object pivot or create any permanent node in your scene -
everything happens on the fly with auto-keying support.

Features:
- Interactive pivot locator for visual manipulation
- Auto-keying of transforms when using temp pivot
- Stored pivot configurations per scene
- Modern dark UI with visual state indicators
- Smart Euler Filter integration tip

Author: David Shepstone
License: MIT
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from functools import partial

import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.mel as mel

# -----------------------------
# Constants
# -----------------------------

WINDOW_NAME = "tempPivotToolWindow"
WINDOW_TITLE = "Temp Pivot"
MANAGER_NODE_NAME = "tempPivotManager"
PIVOT_LOCATOR_PREFIX = "tempPivot_loc_"
PIVOT_GROUP_NAME = "tempPivot_grp"

PIVOT_MODES = [
    "Pivot to Last Selected",
    "Pivot to Selection Center",
    "Pivot to World Origin",
    "Pivot to Custom Locator",
    "Pivot to Component Center",
]

# UI Colors (Maya uses 0-1 range for RGB)
UI_COLORS = {
    "bg_dark": (0.18, 0.18, 0.20),
    "bg_medium": (0.22, 0.22, 0.24),
    "bg_light": (0.28, 0.28, 0.30),
    "accent": (0.36, 0.68, 0.93),
    "accent_hover": (0.46, 0.78, 1.0),
    "success": (0.30, 0.78, 0.48),
    "warning": (0.95, 0.77, 0.26),
    "error": (0.95, 0.35, 0.35),
    "text": (0.85, 0.85, 0.85),
    "text_dim": (0.55, 0.55, 0.55),
    "on_state": (0.20, 0.75, 0.45),
    "off_state": (0.45, 0.45, 0.48),
}

# -----------------------------
# Node Setup and Storage
# -----------------------------

def _add_string_attr(node: str, attr: str, value: str = "") -> None:
    """Add a string attribute to a node if it doesn't exist."""
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, dataType="string")
        cmds.setAttr(f"{node}.{attr}", value, type="string")


def _add_bool_attr(node: str, attr: str, value: bool = False) -> None:
    """Add a boolean attribute to a node if it doesn't exist."""
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, attributeType="bool")
        cmds.setAttr(f"{node}.{attr}", value)


def _add_int_attr(node: str, attr: str, value: int = 0) -> None:
    """Add an integer attribute to a node if it doesn't exist."""
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, attributeType="long")
        cmds.setAttr(f"{node}.{attr}", value)


def _add_double3_attr(node: str, attr: str) -> None:
    """Add a double3 (XYZ) attribute to a node if it doesn't exist."""
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, attributeType="double3")
        for axis in "XYZ":
            cmds.addAttr(
                node,
                longName=f"{attr}{axis}",
                attributeType="double",
                parent=attr,
            )


def get_or_create_manager() -> str:
    """Get or create the main manager node for storing tool state."""
    if cmds.objExists(MANAGER_NODE_NAME):
        node = MANAGER_NODE_NAME
    else:
        node = cmds.createNode("network", name=MANAGER_NODE_NAME)

    # Global settings
    _add_string_attr(node, "lastUsedMode", PIVOT_MODES[0])
    _add_bool_attr(node, "lastUsedAffectScalePivot", True)
    _add_bool_attr(node, "autoKeyEnabled", True)
    _add_bool_attr(node, "smartEulerFilter", False)
    _add_string_attr(node, "presetsJson", "")
    _add_string_attr(node, "storedPivotsJson", "")
    _add_string_attr(node, "lastLocator", "")
    _add_string_attr(node, "activePivotLocator", "")

    if not cmds.attributeQuery("controlData", node=node, exists=True):
        cmds.addAttr(node, longName="controlData", attributeType="message", multi=True)

    return node


def _sanitize_name(name: str) -> str:
    """Sanitize a node name for use in attribute/node names."""
    return name.replace("|", "_").replace(":", "_").replace(" ", "_")


def get_or_create_control_data_node(control: str) -> str:
    """Get or create a data node for storing per-control pivot state."""
    existing = cmds.listConnections(
        f"{control}.message", s=False, d=True, type="network"
    ) or []

    for node in existing:
        if cmds.attributeQuery("controlMessage", node=node, exists=True):
            return node

    safe_name = _sanitize_name(control)
    node = cmds.createNode("network", name=f"tempPivotData_{safe_name}")

    if not cmds.attributeQuery("controlMessage", node=node, exists=True):
        cmds.addAttr(node, longName="controlMessage", attributeType="message")
    if not cmds.attributeQuery("manager", node=node, exists=True):
        cmds.addAttr(node, longName="manager", attributeType="message")

    _add_bool_attr(node, "isOn", False)
    _add_double3_attr(node, "origRotatePivot")
    _add_double3_attr(node, "origScalePivot")
    _add_double3_attr(node, "lastTempWorldPivot")
    _add_string_attr(node, "lastTempMode", "")
    _add_bool_attr(node, "affectScalePivot", True)
    _add_string_attr(node, "linkedLocator", "")

    cmds.connectAttr(f"{control}.message", f"{node}.controlMessage", force=True)

    manager = get_or_create_manager()
    cmds.connectAttr(f"{manager}.controlData", f"{node}.manager", nextAvailable=True)

    return node


def _get_control_from_data_node(data_node: str) -> Optional[str]:
    """Get the control connected to a data node."""
    conns = cmds.listConnections(f"{data_node}.controlMessage", s=True, d=False) or []
    return conns[0] if conns else None


def _get_preset_data(manager: str) -> List[Dict[str, Any]]:
    """Get preset data from the manager node."""
    raw = cmds.getAttr(f"{manager}.presetsJson") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except ValueError:
        pass
    return []


def _set_preset_data(manager: str, data: List[Dict[str, Any]]) -> None:
    """Set preset data on the manager node."""
    cmds.setAttr(f"{manager}.presetsJson", json.dumps(data), type="string")


def _get_stored_pivots(manager: str) -> Dict[str, Any]:
    """Get stored pivot configurations from the manager node."""
    raw = cmds.getAttr(f"{manager}.storedPivotsJson") or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    return {}


def _set_stored_pivots(manager: str, data: Dict[str, Any]) -> None:
    """Set stored pivot configurations on the manager node."""
    cmds.setAttr(f"{manager}.storedPivotsJson", json.dumps(data), type="string")


# -----------------------------
# Pivot Calculations
# -----------------------------

def _as_point(point: Tuple[float, float, float]) -> om.MPoint:
    """Convert a tuple to an MPoint."""
    return om.MPoint(point[0], point[1], point[2])


def _get_world_matrix(node: str) -> om.MMatrix:
    """Get the world transformation matrix of a node."""
    matrix = cmds.xform(node, query=True, worldSpace=True, matrix=True)
    return om.MMatrix(matrix)


def world_to_local_pivot(control: str, world_point: Tuple[float, float, float]) -> om.MPoint:
    """Convert a world-space point to local pivot space for a control."""
    world_matrix = _get_world_matrix(control)
    inv_matrix = world_matrix.inverse()
    return _as_point(world_point) * inv_matrix


def local_to_world_pivot(control: str, local_point: Tuple[float, float, float]) -> Tuple[float, float, float]:
    """Convert a local pivot point to world space."""
    world_matrix = _get_world_matrix(control)
    world_point = _as_point(local_point) * world_matrix
    return (world_point.x, world_point.y, world_point.z)


def compute_world_target(
    mode: str,
    selection: List[str],
    locator_name: str,
    components: Optional[List[str]] = None
) -> Optional[Tuple[float, float, float]]:
    """Compute the target pivot position in world space based on mode."""

    if mode == "Pivot to World Origin":
        return (0.0, 0.0, 0.0)

    if mode == "Pivot to Last Selected":
        if not selection:
            return None
        source = selection[-1]
        try:
            pivot = cmds.xform(source, query=True, worldSpace=True, rotatePivot=True)
        except RuntimeError:
            pivot = None
        if not pivot:
            pivot = cmds.xform(source, query=True, worldSpace=True, translation=True)
        return (pivot[0], pivot[1], pivot[2])

    if mode == "Pivot to Selection Center":
        if not selection:
            return None
        pivots = []
        for node in selection:
            try:
                pivot = cmds.xform(node, query=True, worldSpace=True, rotatePivot=True)
                pivots.append(pivot)
            except RuntimeError:
                continue
        if not pivots:
            return None
        avg = [sum(values) / len(pivots) for values in zip(*pivots)]
        return (avg[0], avg[1], avg[2])

    if mode == "Pivot to Custom Locator":
        if locator_name and cmds.objExists(locator_name):
            position = cmds.xform(locator_name, query=True, worldSpace=True, translation=True)
            return (position[0], position[1], position[2])
        return None

    if mode == "Pivot to Component Center":
        # Get selected components (vertices, edges, faces)
        component_sel = cmds.ls(selection=True, flatten=True) or []
        if not component_sel:
            return None

        positions = []
        for comp in component_sel:
            if '.vtx[' in comp or '.e[' in comp or '.f[' in comp:
                try:
                    pos = cmds.xform(comp, query=True, worldSpace=True, translation=True)
                    if pos:
                        positions.append(pos[:3])
                except RuntimeError:
                    continue

        if not positions:
            # Fallback to selection center
            return compute_world_target("Pivot to Selection Center", selection, locator_name)

        avg = [sum(values) / len(positions) for values in zip(*positions)]
        return (avg[0], avg[1], avg[2])

    return None


# -----------------------------
# Interactive Pivot Locator
# -----------------------------

def create_pivot_locator(
    world_position: Tuple[float, float, float],
    name_suffix: str = "main"
) -> str:
    """Create an interactive pivot locator at the specified world position."""

    # Create or get the pivot group
    if not cmds.objExists(PIVOT_GROUP_NAME):
        cmds.createNode("transform", name=PIVOT_GROUP_NAME)
        cmds.setAttr(f"{PIVOT_GROUP_NAME}.visibility", 1)

    locator_name = f"{PIVOT_LOCATOR_PREFIX}{name_suffix}"

    # Delete existing locator with same name
    if cmds.objExists(locator_name):
        cmds.delete(locator_name)

    # Create a custom pivot control shape
    locator = cmds.spaceLocator(name=locator_name)[0]

    # Style the locator
    shape = cmds.listRelatives(locator, shapes=True)[0]
    cmds.setAttr(f"{shape}.localScaleX", 0.5)
    cmds.setAttr(f"{shape}.localScaleY", 0.5)
    cmds.setAttr(f"{shape}.localScaleZ", 0.5)

    # Set locator color to accent color (blue)
    cmds.setAttr(f"{shape}.overrideEnabled", 1)
    cmds.setAttr(f"{shape}.overrideRGBColors", 1)
    cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["accent"][0])
    cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["accent"][1])
    cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["accent"][2])

    # Create circle indicators around the locator
    circles = []
    for axis, color in [("X", (1, 0.3, 0.3)), ("Y", (0.3, 1, 0.3)), ("Z", (0.3, 0.3, 1))]:
        normal = [0, 0, 0]
        normal["XYZ".index(axis)] = 1
        circle = cmds.circle(
            name=f"{locator_name}_ring_{axis}",
            normal=normal,
            radius=0.4,
            degree=3,
            sections=16,
            constructionHistory=False
        )[0]
        circle_shape = cmds.listRelatives(circle, shapes=True)[0]
        cmds.setAttr(f"{circle_shape}.overrideEnabled", 1)
        cmds.setAttr(f"{circle_shape}.overrideRGBColors", 1)
        cmds.setAttr(f"{circle_shape}.overrideColorR", color[0])
        cmds.setAttr(f"{circle_shape}.overrideColorG", color[1])
        cmds.setAttr(f"{circle_shape}.overrideColorB", color[2])
        circles.append(circle)

    # Parent circles under locator
    for circle in circles:
        circle_shape = cmds.listRelatives(circle, shapes=True)[0]
        cmds.parent(circle_shape, locator, shape=True, relative=True)
        cmds.delete(circle)

    # Position the locator
    cmds.xform(locator, worldSpace=True, translation=world_position)

    # Parent to pivot group
    cmds.parent(locator, PIVOT_GROUP_NAME)

    # Store reference in manager
    manager = get_or_create_manager()
    cmds.setAttr(f"{manager}.activePivotLocator", locator, type="string")

    return locator


def delete_pivot_locator(locator_name: Optional[str] = None) -> bool:
    """Delete the pivot locator."""
    manager = get_or_create_manager()

    if locator_name is None:
        locator_name = cmds.getAttr(f"{manager}.activePivotLocator") or ""

    if locator_name and cmds.objExists(locator_name):
        cmds.delete(locator_name)
        cmds.setAttr(f"{manager}.activePivotLocator", "", type="string")
        return True

    # Clean up any orphaned pivot locators
    all_locators = cmds.ls(f"{PIVOT_LOCATOR_PREFIX}*", type="transform") or []
    for loc in all_locators:
        cmds.delete(loc)

    # Clean up empty group
    if cmds.objExists(PIVOT_GROUP_NAME):
        children = cmds.listRelatives(PIVOT_GROUP_NAME, children=True) or []
        if not children:
            cmds.delete(PIVOT_GROUP_NAME)

    return False


def get_pivot_locator_position() -> Optional[Tuple[float, float, float]]:
    """Get the current position of the active pivot locator."""
    manager = get_or_create_manager()
    locator_name = cmds.getAttr(f"{manager}.activePivotLocator") or ""

    if locator_name and cmds.objExists(locator_name):
        pos = cmds.xform(locator_name, query=True, worldSpace=True, translation=True)
        return (pos[0], pos[1], pos[2])
    return None


def update_pivot_locator_position(world_position: Tuple[float, float, float]) -> bool:
    """Update the pivot locator position."""
    manager = get_or_create_manager()
    locator_name = cmds.getAttr(f"{manager}.activePivotLocator") or ""

    if locator_name and cmds.objExists(locator_name):
        cmds.xform(locator_name, worldSpace=True, translation=world_position)
        return True
    return False


# -----------------------------
# Auto-Keying System
# -----------------------------

def _is_auto_key_enabled() -> bool:
    """Check if auto-key is enabled in both Maya and the tool."""
    manager = get_or_create_manager()
    tool_autokey = cmds.getAttr(f"{manager}.autoKeyEnabled")
    maya_autokey = cmds.autoKeyframe(query=True, state=True)
    return tool_autokey and maya_autokey


def _get_current_time() -> float:
    """Get the current time in the timeline."""
    return cmds.currentTime(query=True)


def auto_key_transform(
    control: str,
    attributes: Optional[List[str]] = None
) -> List[str]:
    """Set keyframes on transform attributes if auto-key is enabled."""
    if not _is_auto_key_enabled():
        return []

    if attributes is None:
        attributes = ["tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz"]

    keyed = []
    current_time = _get_current_time()

    for attr in attributes:
        attr_path = f"{control}.{attr}"
        if cmds.objExists(attr_path):
            # Check if attribute is keyable and not locked
            if cmds.getAttr(attr_path, keyable=True) and not cmds.getAttr(attr_path, lock=True):
                try:
                    cmds.setKeyframe(control, attribute=attr, time=current_time)
                    keyed.append(attr)
                except RuntimeError:
                    pass

    return keyed


def apply_smart_euler_filter(control: str) -> bool:
    """Apply Euler filter to rotation curves to fix gimbal flipping."""
    manager = get_or_create_manager()
    if not cmds.getAttr(f"{manager}.smartEulerFilter"):
        return False

    try:
        # Get rotation animation curves
        rotate_curves = []
        for axis in ["rx", "ry", "rz"]:
            curves = cmds.listConnections(f"{control}.{axis}", type="animCurve") or []
            rotate_curves.extend(curves)

        if rotate_curves:
            cmds.filterCurve(rotate_curves)
            return True
    except RuntimeError:
        pass

    return False


# -----------------------------
# Pivot Apply/Restore with Auto-Key
# -----------------------------

def _is_pivot_editable(control: str, affect_scale: bool) -> Tuple[bool, List[str]]:
    """Check if pivot attributes can be edited on a control."""
    warnings = []

    if cmds.getAttr(f"{control}.rotatePivot", lock=True):
        warnings.append(f"{control}: rotatePivot is locked")
    if cmds.listConnections(f"{control}.rotatePivot", s=True, d=False):
        warnings.append(f"{control}: rotatePivot has incoming connection")

    if affect_scale:
        if cmds.getAttr(f"{control}.scalePivot", lock=True):
            warnings.append(f"{control}: scalePivot is locked")
        if cmds.listConnections(f"{control}.scalePivot", s=True, d=False):
            warnings.append(f"{control}: scalePivot has incoming connection")

    return (len(warnings) == 0, warnings)


def apply_temp_pivot(
    controls: List[str],
    world_point: Tuple[float, float, float],
    affect_scale: bool,
    mode: str,
    create_locator: bool = True,
) -> Tuple[List[str], List[str]]:
    """Apply a temporary pivot to controls."""
    updated = []
    warnings = []

    for control in controls:
        if cmds.nodeType(control) != "transform":
            warnings.append(f"{control}: not a transform")
            continue

        editable, issues = _is_pivot_editable(control, affect_scale)
        if not editable:
            warnings.extend(issues)
            continue

        data_node = get_or_create_control_data_node(control)
        is_on = cmds.getAttr(f"{data_node}.isOn")

        # Store original pivots on first activation
        if not is_on:
            orig_rotate = cmds.getAttr(f"{control}.rotatePivot")[0]
            orig_scale = cmds.getAttr(f"{control}.scalePivot")[0]
            cmds.setAttr(f"{data_node}.origRotatePivot", *orig_rotate)
            cmds.setAttr(f"{data_node}.origScalePivot", *orig_scale)

        # Convert world point to local space
        local_point = world_to_local_pivot(control, world_point)

        # Apply new pivot
        cmds.setAttr(
            f"{control}.rotatePivot",
            local_point.x,
            local_point.y,
            local_point.z,
        )
        if affect_scale:
            cmds.setAttr(
                f"{control}.scalePivot",
                local_point.x,
                local_point.y,
                local_point.z,
            )

        # Store state
        cmds.setAttr(f"{data_node}.lastTempWorldPivot", *world_point)
        cmds.setAttr(f"{data_node}.lastTempMode", mode, type="string")
        cmds.setAttr(f"{data_node}.affectScalePivot", affect_scale)
        cmds.setAttr(f"{data_node}.isOn", True)

        updated.append(control)

    # Create interactive pivot locator
    if create_locator and updated:
        create_pivot_locator(world_point, "active")

    return updated, warnings


def restore_temp_pivot(
    controls: List[str],
    delete_locator: bool = True
) -> Tuple[List[str], List[str]]:
    """Restore original pivots on controls."""
    restored = []
    warnings = []

    for control in controls:
        data_node = get_or_create_control_data_node(control)

        if not cmds.getAttr(f"{data_node}.isOn"):
            continue

        affect_scale = cmds.getAttr(f"{data_node}.affectScalePivot")
        editable, issues = _is_pivot_editable(control, affect_scale)

        if not editable:
            warnings.extend(issues)
            continue

        # Restore original pivots
        orig_rotate = cmds.getAttr(f"{data_node}.origRotatePivot")[0]
        orig_scale = cmds.getAttr(f"{data_node}.origScalePivot")[0]

        cmds.setAttr(f"{control}.rotatePivot", *orig_rotate)
        if affect_scale:
            cmds.setAttr(f"{control}.scalePivot", *orig_scale)

        cmds.setAttr(f"{data_node}.isOn", False)
        restored.append(control)

    # Clean up locator
    if delete_locator:
        delete_pivot_locator()

    return restored, warnings


def toggle_temp_pivot(
    controls: List[str],
    mode: str,
    affect_scale: bool,
    world_point: Optional[Tuple[float, float, float]],
) -> Tuple[List[str], List[str], bool]:
    """Toggle temp pivot on/off for controls."""
    any_on = False

    for control in controls:
        data_node = get_or_create_control_data_node(control)
        if cmds.getAttr(f"{data_node}.isOn"):
            any_on = True
            break

    if any_on:
        restored, warnings = restore_temp_pivot(controls)
        return restored, warnings, False

    if world_point is None:
        return [], ["No valid pivot target found."], False

    applied, warnings = apply_temp_pivot(controls, world_point, affect_scale, mode)
    return applied, warnings, True


def update_pivot_from_locator(controls: List[str], affect_scale: bool = True) -> Tuple[List[str], List[str]]:
    """Update pivot positions from the current locator position with auto-keying."""
    world_point = get_pivot_locator_position()
    if world_point is None:
        return [], ["No active pivot locator found."]

    updated = []
    warnings = []

    for control in controls:
        data_node = get_or_create_control_data_node(control)

        if not cmds.getAttr(f"{data_node}.isOn"):
            warnings.append(f"{control}: temp pivot not active")
            continue

        # Convert world point to local space
        local_point = world_to_local_pivot(control, world_point)

        # Apply new pivot
        cmds.setAttr(
            f"{control}.rotatePivot",
            local_point.x,
            local_point.y,
            local_point.z,
        )
        if affect_scale:
            cmds.setAttr(
                f"{control}.scalePivot",
                local_point.x,
                local_point.y,
                local_point.z,
            )

        # Update stored world point
        cmds.setAttr(f"{data_node}.lastTempWorldPivot", *world_point)

        # Auto-key if enabled
        auto_key_transform(control, ["tx", "ty", "tz", "rx", "ry", "rz"])

        updated.append(control)

    return updated, warnings


# -----------------------------
# Stored Pivot Management
# -----------------------------

def store_pivot(
    name: str,
    controls: List[str],
    world_point: Tuple[float, float, float],
    mode: str,
    affect_scale: bool,
    locator_name: str = ""
) -> bool:
    """Store a pivot configuration for later recall."""
    manager = get_or_create_manager()
    stored = _get_stored_pivots(manager)

    stored[name] = {
        "controls": controls,
        "worldPoint": list(world_point),
        "mode": mode,
        "affectScalePivot": affect_scale,
        "locator": locator_name,
        "timestamp": cmds.currentTime(query=True)
    }

    _set_stored_pivots(manager, stored)
    return True


def recall_pivot(name: str, use_current_selection: bool = True) -> Tuple[List[str], List[str]]:
    """Recall a stored pivot configuration."""
    manager = get_or_create_manager()
    stored = _get_stored_pivots(manager)

    if name not in stored:
        return [], [f"Stored pivot '{name}' not found."]

    config = stored[name]
    world_point = tuple(config.get("worldPoint", [0, 0, 0]))
    mode = config.get("mode", PIVOT_MODES[0])
    affect_scale = config.get("affectScalePivot", True)

    if use_current_selection:
        controls = cmds.ls(selection=True, type="transform") or []
    else:
        controls = config.get("controls", [])
        # Filter to existing controls
        controls = [c for c in controls if cmds.objExists(c)]

    if not controls:
        return [], ["No valid controls to apply pivot."]

    applied, warnings = apply_temp_pivot(controls, world_point, affect_scale, mode)
    return applied, warnings


def delete_stored_pivot(name: str) -> bool:
    """Delete a stored pivot configuration."""
    manager = get_or_create_manager()
    stored = _get_stored_pivots(manager)

    if name not in stored:
        return False

    del stored[name]
    _set_stored_pivots(manager, stored)
    return True


def list_stored_pivots() -> List[str]:
    """List all stored pivot names."""
    manager = get_or_create_manager()
    stored = _get_stored_pivots(manager)
    return list(stored.keys())


# -----------------------------
# Presets (backwards compatible)
# -----------------------------

def preset_save(
    name: str,
    mode: str,
    affect_scale: bool,
    world_target: Tuple[float, float, float],
    selection: List[str],
    locator_name: str,
) -> bool:
    """Save current configuration as a preset."""
    manager = get_or_create_manager()
    presets = _get_preset_data(manager)
    presets = [preset for preset in presets if preset.get("name") != name]

    preset_data = {
        "name": name,
        "mode": mode,
        "affectScalePivot": affect_scale,
        "worldTarget": list(world_target),
        "controls": selection,
        "locator": locator_name,
    }
    presets.append(preset_data)
    _set_preset_data(manager, presets)
    return True


def preset_delete(name: str) -> bool:
    """Delete a preset by name."""
    manager = get_or_create_manager()
    presets = _get_preset_data(manager)
    new_presets = [preset for preset in presets if preset.get("name") != name]
    if len(new_presets) == len(presets):
        return False
    _set_preset_data(manager, new_presets)
    return True


def preset_load(
    name: str,
    selection_override: bool = True,
) -> Tuple[List[str], List[str]]:
    """Load and apply a preset."""
    manager = get_or_create_manager()
    presets = _get_preset_data(manager)
    preset = next((item for item in presets if item.get("name") == name), None)

    if not preset:
        return [], [f"Preset '{name}' not found."]

    mode = preset.get("mode", PIVOT_MODES[0])
    affect_scale = preset.get("affectScalePivot", True)
    world_target = tuple(preset.get("worldTarget", [0.0, 0.0, 0.0]))
    locator_name = preset.get("locator", "")

    selection = cmds.ls(selection=True, type="transform") if selection_override else []
    if not selection:
        selection = preset.get("controls", [])

    if not selection:
        return [], ["No controls available to apply preset."]

    world_target = compute_world_target(mode, selection, locator_name) or world_target

    applied, warnings = apply_temp_pivot(selection, world_target, affect_scale, mode)
    cmds.setAttr(f"{manager}.lastUsedMode", mode, type="string")
    cmds.setAttr(f"{manager}.lastUsedAffectScalePivot", affect_scale)
    if locator_name:
        cmds.setAttr(f"{manager}.lastLocator", locator_name, type="string")
    return applied, warnings


# -----------------------------
# Script Jobs for Real-time Updates
# -----------------------------

_active_script_jobs = []


def _clear_script_jobs() -> None:
    """Clear all active script jobs."""
    global _active_script_jobs
    for job_id in _active_script_jobs:
        try:
            if cmds.scriptJob(exists=job_id):
                cmds.scriptJob(kill=job_id, force=True)
        except RuntimeError:
            pass
    _active_script_jobs = []


def _create_locator_update_job(controls: List[str], affect_scale: bool, callback=None) -> int:
    """Create a script job to update pivots when locator moves."""
    manager = get_or_create_manager()
    locator_name = cmds.getAttr(f"{manager}.activePivotLocator") or ""

    if not locator_name or not cmds.objExists(locator_name):
        return -1

    def on_locator_change():
        update_pivot_from_locator(controls, affect_scale)
        if callback:
            callback()

    job_id = cmds.scriptJob(
        attributeChange=[f"{locator_name}.translate", on_locator_change]
    )
    _active_script_jobs.append(job_id)
    return job_id


# -----------------------------
# UI Utilities
# -----------------------------

def _get_selection() -> List[str]:
    """Get current transform selection."""
    return cmds.ls(selection=True, type="transform") or []


def _selection_count_text() -> str:
    """Get formatted selection count text."""
    count = len(_get_selection())
    return f"{count} object{'s' if count != 1 else ''} selected"


def _get_active_pivot_controls() -> List[str]:
    """Get list of controls with active temp pivots."""
    active = []
    manager = get_or_create_manager()

    if not cmds.objExists(manager):
        return []

    # Find all connected data nodes
    connections = cmds.listConnections(f"{manager}.controlData", s=False, d=True) or []

    for data_node in connections:
        if cmds.objExists(data_node) and cmds.attributeQuery("isOn", node=data_node, exists=True):
            if cmds.getAttr(f"{data_node}.isOn"):
                control = _get_control_from_data_node(data_node)
                if control:
                    active.append(control)

    return active


def _rgb_to_hex(rgb: Tuple[float, float, float]) -> str:
    """Convert RGB tuple (0-1 range) to hex color string."""
    return "#{:02x}{:02x}{:02x}".format(
        int(rgb[0] * 255),
        int(rgb[1] * 255),
        int(rgb[2] * 255)
    )


# -----------------------------
# Modern UI Implementation
# -----------------------------

def show() -> None:
    """Show the Temp Pivot Tool window with modern UI."""
    global _active_script_jobs

    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME)

    _clear_script_jobs()

    manager = get_or_create_manager()

    # Window setup
    window = cmds.window(
        WINDOW_NAME,
        title=WINDOW_TITLE,
        sizeable=True,
        minimizeButton=True,
        maximizeButton=False,
        width=320,
        height=620
    )

    # Main scrollable layout
    main_scroll = cmds.scrollLayout(
        childResizable=True,
        horizontalScrollBarThickness=0,
        verticalScrollBarThickness=8
    )

    main_layout = cmds.columnLayout(
        adjustableColumn=True,
        rowSpacing=2,
        columnAttach=("both", 8)
    )

    cmds.separator(height=8, style="none")

    # ==========================================
    # HEADER SECTION
    # ==========================================

    header_layout = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=2,
        columnWidth2=(48, 260)
    )

    # Title icon placeholder (could add actual icon)
    cmds.text(
        label="",
        width=48,
        height=48,
        backgroundColor=UI_COLORS["accent"]
    )

    title_col = cmds.columnLayout(adjustableColumn=True)
    cmds.text(
        label="Temp Pivot",
        font="boldLabelFont",
        align="left",
        height=24
    )
    cmds.text(
        label="Create temporary pivots for rotation",
        align="left",
        font="smallPlainLabelFont",
        height=16
    )
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=12, style="none")

    # ==========================================
    # STATUS INDICATOR
    # ==========================================

    status_frame = cmds.frameLayout(
        label="Status",
        collapsable=False,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    status_row = cmds.rowLayout(
        numberOfColumns=3,
        adjustableColumn=2,
        columnWidth3=(60, 180, 50)
    )

    # On/Off indicator
    state_indicator = cmds.button(
        label="OFF",
        width=55,
        height=28,
        backgroundColor=UI_COLORS["off_state"],
        enable=False
    )

    # Selection info
    selection_text = cmds.text(
        label=_selection_count_text(),
        align="left"
    )

    # Active count
    active_count_text = cmds.text(
        label="0 active",
        align="right"
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # PIVOT MODE SECTION
    # ==========================================

    mode_frame = cmds.frameLayout(
        label="Pivot Mode",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    mode_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=6)

    # Mode dropdown
    mode_menu = cmds.optionMenu(label="Mode: ", height=26)
    for mode in PIVOT_MODES:
        cmds.menuItem(label=mode)

    # Set last used mode
    last_mode = cmds.getAttr(f"{manager}.lastUsedMode") or PIVOT_MODES[0]
    if last_mode in PIVOT_MODES:
        cmds.optionMenu(mode_menu, edit=True, value=last_mode)

    # Affect scale pivot checkbox
    affect_scale_checkbox = cmds.checkBox(
        label="Affect Scale Pivot Too",
        value=cmds.getAttr(f"{manager}.lastUsedAffectScalePivot"),
        height=24
    )

    cmds.separator(height=4, style="none")

    # Custom locator row
    cmds.text(label="Custom Locator:", align="left", height=18)
    locator_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(200, 80)
    )
    locator_field = cmds.textField(
        text=cmds.getAttr(f"{manager}.lastLocator") or "",
        placeholderText="Select or pick locator...",
        height=24
    )
    pick_locator_btn = cmds.button(
        label="Pick",
        height=24,
        width=75
    )
    cmds.setParent("..")

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # MAIN CONTROLS SECTION
    # ==========================================

    controls_frame = cmds.frameLayout(
        label="Controls",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    controls_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    # Primary action buttons
    apply_btn = cmds.button(
        label="Apply Temp Pivot",
        height=36,
        backgroundColor=UI_COLORS["accent"]
    )

    toggle_btn = cmds.button(
        label="Toggle On/Off",
        height=32
    )

    reset_btn = cmds.button(
        label="Reset to Original",
        height=32
    )

    cmds.separator(height=8, style="in")

    # Locator controls row
    cmds.text(label="Interactive Pivot:", align="left", height=18)
    locator_controls_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(150, 150)
    )
    create_locator_btn = cmds.button(
        label="Create Locator",
        height=28
    )
    update_from_locator_btn = cmds.button(
        label="Update from Locator",
        height=28
    )
    cmds.setParent("..")

    delete_locator_btn = cmds.button(
        label="Delete Pivot Locator",
        height=26
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # AUTO-KEY SECTION
    # ==========================================

    autokey_frame = cmds.frameLayout(
        label="Auto-Key Settings",
        collapsable=True,
        collapse=True,
        marginWidth=8,
        marginHeight=8
    )

    autokey_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=6)

    autokey_checkbox = cmds.checkBox(
        label="Enable Tool Auto-Key",
        value=cmds.getAttr(f"{manager}.autoKeyEnabled"),
        annotation="Auto-key transforms when manipulating with temp pivot",
        height=24
    )

    euler_checkbox = cmds.checkBox(
        label="Smart Euler Filter",
        value=cmds.getAttr(f"{manager}.smartEulerFilter"),
        annotation="Apply Euler filter to prevent rotation flipping",
        height=24
    )

    cmds.separator(height=4, style="none")

    # Euler filter tip
    tip_text = cmds.text(
        label="TIP: If you notice rotation weirdness between\ntwo keys after using this tool, consider\nenabling Smart Euler Filter while working.",
        align="left",
        font="smallPlainLabelFont",
        wordWrap=True,
        height=48
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # STORED PIVOTS SECTION
    # ==========================================

    stored_frame = cmds.frameLayout(
        label="Stored Pivots",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    stored_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    # Name input row
    cmds.text(label="Pivot Name:", align="left", height=18)
    stored_name_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(200, 80)
    )
    pivot_name_field = cmds.textField(
        placeholderText="Enter pivot name...",
        height=24
    )
    store_btn = cmds.button(
        label="Store",
        height=24,
        width=75
    )
    cmds.setParent("..")

    cmds.separator(height=4, style="none")

    # Stored pivots list
    stored_list = cmds.textScrollList(
        height=100,
        allowMultiSelection=False
    )

    # List action buttons
    stored_btns_row = cmds.rowLayout(
        numberOfColumns=3,
        adjustableColumn=1,
        columnWidth3=(100, 100, 90)
    )
    recall_btn = cmds.button(
        label="Recall",
        height=26
    )
    delete_stored_btn = cmds.button(
        label="Delete",
        height=26
    )
    refresh_stored_btn = cmds.button(
        label="Refresh",
        height=26
    )
    cmds.setParent("..")

    # Use current selection checkbox
    use_selection_checkbox = cmds.checkBox(
        label="Apply to current selection",
        value=True,
        height=22
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # PRESETS SECTION (Legacy compatible)
    # ==========================================

    presets_frame = cmds.frameLayout(
        label="Presets (Quick Access)",
        collapsable=True,
        collapse=True,
        marginWidth=8,
        marginHeight=8
    )

    presets_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    preset_name_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(200, 80)
    )
    preset_name_field = cmds.textField(
        placeholderText="Preset name...",
        height=24
    )
    save_preset_btn = cmds.button(
        label="Save",
        height=24,
        width=75
    )
    cmds.setParent("..")

    preset_list = cmds.textScrollList(
        height=80,
        allowMultiSelection=False
    )

    preset_btns_row = cmds.rowLayout(
        numberOfColumns=3,
        adjustableColumn=1,
        columnWidth3=(100, 100, 90)
    )
    load_preset_btn = cmds.button(
        label="Load",
        height=26
    )
    delete_preset_btn = cmds.button(
        label="Delete",
        height=26
    )
    refresh_preset_btn = cmds.button(
        label="Refresh",
        height=26
    )
    cmds.setParent("..")

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # OUTPUT/LOG SECTION
    # ==========================================

    log_frame = cmds.frameLayout(
        label="Output",
        collapsable=True,
        collapse=True,
        marginWidth=8,
        marginHeight=8
    )

    log_field = cmds.scrollField(
        height=80,
        editable=False,
        wordWrap=True,
        text="Ready."
    )

    cmds.setParent("..")

    cmds.separator(height=16, style="none")

    # ==========================================
    # CALLBACK FUNCTIONS
    # ==========================================

    def log_message(message: str, msg_type: str = "info") -> None:
        """Log a message to the output field."""
        prefix = ""
        if msg_type == "warning":
            prefix = "[!] "
        elif msg_type == "error":
            prefix = "[X] "
        elif msg_type == "success":
            prefix = "[OK] "

        current = cmds.scrollField(log_field, query=True, text=True) or ""
        new_text = f"{prefix}{message}"
        if current and current != "Ready.":
            new_text = f"{current}\n{new_text}"
        cmds.scrollField(log_field, edit=True, text=new_text)

        # Auto-scroll to bottom
        cmds.scrollField(log_field, edit=True, insertionPosition=len(new_text))

    def update_status_indicator() -> None:
        """Update the ON/OFF status indicator."""
        active = _get_active_pivot_controls()
        count = len(active)

        if count > 0:
            cmds.button(state_indicator, edit=True, label="ON", backgroundColor=UI_COLORS["on_state"])
            cmds.text(active_count_text, edit=True, label=f"{count} active")
        else:
            cmds.button(state_indicator, edit=True, label="OFF", backgroundColor=UI_COLORS["off_state"])
            cmds.text(active_count_text, edit=True, label="0 active")

    def update_selection_display() -> None:
        """Update selection text display."""
        cmds.text(selection_text, edit=True, label=_selection_count_text())
        update_status_indicator()

    def refresh_stored_list() -> None:
        """Refresh the stored pivots list."""
        cmds.textScrollList(stored_list, edit=True, removeAll=True)
        for name in sorted(list_stored_pivots()):
            cmds.textScrollList(stored_list, edit=True, append=name)

    def refresh_preset_list() -> None:
        """Refresh the presets list."""
        cmds.textScrollList(preset_list, edit=True, removeAll=True)
        presets = _get_preset_data(manager)
        for preset in sorted(presets, key=lambda x: x.get("name", "")):
            cmds.textScrollList(preset_list, edit=True, append=preset.get("name"))

    def save_manager_state() -> None:
        """Save current UI state to manager node."""
        mode = cmds.optionMenu(mode_menu, query=True, value=True)
        affect_scale = cmds.checkBox(affect_scale_checkbox, query=True, value=True)
        locator = cmds.textField(locator_field, query=True, text=True)
        autokey = cmds.checkBox(autokey_checkbox, query=True, value=True)
        euler = cmds.checkBox(euler_checkbox, query=True, value=True)

        cmds.setAttr(f"{manager}.lastUsedMode", mode, type="string")
        cmds.setAttr(f"{manager}.lastUsedAffectScalePivot", affect_scale)
        cmds.setAttr(f"{manager}.lastLocator", locator, type="string")
        cmds.setAttr(f"{manager}.autoKeyEnabled", autokey)
        cmds.setAttr(f"{manager}.smartEulerFilter", euler)

    def gather_settings() -> Tuple[str, bool, str]:
        """Gather current mode settings from UI."""
        mode = cmds.optionMenu(mode_menu, query=True, value=True)
        affect_scale = cmds.checkBox(affect_scale_checkbox, query=True, value=True)
        locator = cmds.textField(locator_field, query=True, text=True)
        return mode, affect_scale, locator

    # ----- Main Action Callbacks -----

    def on_apply(*args) -> None:
        """Apply temp pivot to selection."""
        selection = _get_selection()
        if not selection:
            log_message("No transforms selected.", "warning")
            return

        mode, affect_scale, locator = gather_settings()
        world_target = compute_world_target(mode, selection, locator)

        if world_target is None:
            log_message("Could not determine pivot target.", "error")
            return

        applied, warnings = apply_temp_pivot(selection, world_target, affect_scale, mode)
        save_manager_state()

        if applied:
            log_message(f"Applied temp pivot to {len(applied)} object(s).", "success")

        for warn in warnings:
            log_message(warn, "warning")

        update_status_indicator()

    def on_toggle(*args) -> None:
        """Toggle temp pivot on/off."""
        selection = _get_selection()
        if not selection:
            log_message("No transforms selected.", "warning")
            return

        mode, affect_scale, locator = gather_settings()
        world_target = compute_world_target(mode, selection, locator)

        applied, warnings, turned_on = toggle_temp_pivot(selection, mode, affect_scale, world_target)
        save_manager_state()

        if turned_on:
            log_message(f"Temp pivot ON for {len(applied)} object(s).", "success")
        else:
            log_message(f"Temp pivot OFF for {len(applied)} object(s).", "success")

        for warn in warnings:
            log_message(warn, "warning")

        update_status_indicator()

    def on_reset(*args) -> None:
        """Reset to original pivots."""
        selection = _get_selection()
        if not selection:
            log_message("No transforms selected.", "warning")
            return

        restored, warnings = restore_temp_pivot(selection)

        if restored:
            log_message(f"Restored original pivots on {len(restored)} object(s).", "success")
        else:
            log_message("No active temp pivots to restore.", "info")

        for warn in warnings:
            log_message(warn, "warning")

        update_status_indicator()

    def on_pick_locator(*args) -> None:
        """Pick a locator from selection."""
        selection = cmds.ls(selection=True) or []

        for node in selection:
            if cmds.nodeType(node) == "transform":
                shapes = cmds.listRelatives(node, shapes=True) or []
                if any(cmds.nodeType(s) == "locator" for s in shapes):
                    cmds.textField(locator_field, edit=True, text=node)
                    log_message(f"Picked locator: {node}", "success")
                    return

        # If no locator found, just use first transform
        if selection:
            first = selection[0]
            if cmds.nodeType(first) == "transform" or cmds.objectType(first, isAType="transform"):
                cmds.textField(locator_field, edit=True, text=first)
                log_message(f"Picked transform: {first}", "success")
                return

        log_message("Select a locator or transform to pick.", "warning")

    def on_create_locator(*args) -> None:
        """Create an interactive pivot locator."""
        selection = _get_selection()
        mode, affect_scale, locator = gather_settings()

        if selection:
            world_target = compute_world_target(mode, selection, locator)
        else:
            world_target = (0, 0, 0)

        if world_target is None:
            world_target = (0, 0, 0)

        loc = create_pivot_locator(world_target, "interactive")
        log_message(f"Created pivot locator: {loc}", "success")
        cmds.select(loc)

    def on_update_from_locator(*args) -> None:
        """Update pivots from locator position."""
        selection = _get_selection()
        if not selection:
            # Try to get controls with active pivots
            selection = _get_active_pivot_controls()

        if not selection:
            log_message("No controls with active temp pivots.", "warning")
            return

        affect_scale = cmds.checkBox(affect_scale_checkbox, query=True, value=True)
        updated, warnings = update_pivot_from_locator(selection, affect_scale)

        if updated:
            log_message(f"Updated pivot on {len(updated)} object(s).", "success")

        for warn in warnings:
            log_message(warn, "warning")

    def on_delete_locator(*args) -> None:
        """Delete the pivot locator."""
        if delete_pivot_locator():
            log_message("Pivot locator deleted.", "success")
        else:
            log_message("No pivot locator to delete.", "info")

    # ----- Stored Pivots Callbacks -----

    def on_store_pivot(*args) -> None:
        """Store current pivot configuration."""
        name = cmds.textField(pivot_name_field, query=True, text=True).strip()
        if not name:
            log_message("Enter a name for the stored pivot.", "warning")
            return

        selection = _get_selection()
        if not selection:
            log_message("No transforms selected to store.", "warning")
            return

        mode, affect_scale, locator = gather_settings()
        world_target = compute_world_target(mode, selection, locator)

        if world_target is None:
            log_message("Could not determine pivot target to store.", "error")
            return

        store_pivot(name, selection, world_target, mode, affect_scale, locator)
        refresh_stored_list()
        log_message(f"Stored pivot '{name}'.", "success")

    def on_recall_pivot(*args) -> None:
        """Recall a stored pivot."""
        selected = cmds.textScrollList(stored_list, query=True, selectItem=True) or []
        if not selected:
            log_message("Select a stored pivot to recall.", "warning")
            return

        use_selection = cmds.checkBox(use_selection_checkbox, query=True, value=True)
        applied, warnings = recall_pivot(selected[0], use_current_selection=use_selection)

        if applied:
            log_message(f"Recalled pivot '{selected[0]}' on {len(applied)} object(s).", "success")

        for warn in warnings:
            log_message(warn, "warning")

        update_status_indicator()

    def on_delete_stored(*args) -> None:
        """Delete a stored pivot."""
        selected = cmds.textScrollList(stored_list, query=True, selectItem=True) or []
        if not selected:
            log_message("Select a stored pivot to delete.", "warning")
            return

        if delete_stored_pivot(selected[0]):
            refresh_stored_list()
            log_message(f"Deleted stored pivot '{selected[0]}'.", "success")
        else:
            log_message("Could not delete stored pivot.", "error")

    # ----- Preset Callbacks -----

    def on_save_preset(*args) -> None:
        """Save current configuration as preset."""
        name = cmds.textField(preset_name_field, query=True, text=True).strip()
        if not name:
            log_message("Enter a preset name.", "warning")
            return

        selection = _get_selection()
        if not selection:
            log_message("No transforms selected.", "warning")
            return

        mode, affect_scale, locator = gather_settings()
        world_target = compute_world_target(mode, selection, locator)

        if world_target is None:
            log_message("Could not determine pivot target.", "error")
            return

        preset_save(name, mode, affect_scale, world_target, selection, locator)
        refresh_preset_list()
        log_message(f"Saved preset '{name}'.", "success")

    def on_load_preset(*args) -> None:
        """Load and apply a preset."""
        selected = cmds.textScrollList(preset_list, query=True, selectItem=True) or []
        if not selected:
            log_message("Select a preset to load.", "warning")
            return

        applied, warnings = preset_load(selected[0], selection_override=True)

        if applied:
            log_message(f"Loaded preset '{selected[0]}' on {len(applied)} object(s).", "success")

        for warn in warnings:
            log_message(warn, "warning")

        update_status_indicator()

    def on_delete_preset(*args) -> None:
        """Delete a preset."""
        selected = cmds.textScrollList(preset_list, query=True, selectItem=True) or []
        if not selected:
            log_message("Select a preset to delete.", "warning")
            return

        if preset_delete(selected[0]):
            refresh_preset_list()
            log_message(f"Deleted preset '{selected[0]}'.", "success")
        else:
            log_message("Could not delete preset.", "error")

    # ----- Auto-Key Callbacks -----

    def on_autokey_changed(*args) -> None:
        """Handle auto-key checkbox change."""
        value = cmds.checkBox(autokey_checkbox, query=True, value=True)
        cmds.setAttr(f"{manager}.autoKeyEnabled", value)
        state = "enabled" if value else "disabled"
        log_message(f"Tool auto-key {state}.", "info")

    def on_euler_changed(*args) -> None:
        """Handle euler filter checkbox change."""
        value = cmds.checkBox(euler_checkbox, query=True, value=True)
        cmds.setAttr(f"{manager}.smartEulerFilter", value)
        state = "enabled" if value else "disabled"
        log_message(f"Smart Euler filter {state}.", "info")

    # ==========================================
    # CONNECT CALLBACKS
    # ==========================================

    # Main controls
    cmds.button(apply_btn, edit=True, command=on_apply)
    cmds.button(toggle_btn, edit=True, command=on_toggle)
    cmds.button(reset_btn, edit=True, command=on_reset)
    cmds.button(pick_locator_btn, edit=True, command=on_pick_locator)

    # Locator controls
    cmds.button(create_locator_btn, edit=True, command=on_create_locator)
    cmds.button(update_from_locator_btn, edit=True, command=on_update_from_locator)
    cmds.button(delete_locator_btn, edit=True, command=on_delete_locator)

    # Stored pivots
    cmds.button(store_btn, edit=True, command=on_store_pivot)
    cmds.button(recall_btn, edit=True, command=on_recall_pivot)
    cmds.button(delete_stored_btn, edit=True, command=on_delete_stored)
    cmds.button(refresh_stored_btn, edit=True, command=lambda *_: refresh_stored_list())

    # Presets
    cmds.button(save_preset_btn, edit=True, command=on_save_preset)
    cmds.button(load_preset_btn, edit=True, command=on_load_preset)
    cmds.button(delete_preset_btn, edit=True, command=on_delete_preset)
    cmds.button(refresh_preset_btn, edit=True, command=lambda *_: refresh_preset_list())

    # Auto-key settings
    cmds.checkBox(autokey_checkbox, edit=True, changeCommand=on_autokey_changed)
    cmds.checkBox(euler_checkbox, edit=True, changeCommand=on_euler_changed)

    # Double-click handlers for lists
    cmds.textScrollList(stored_list, edit=True, doubleClickCommand=on_recall_pivot)
    cmds.textScrollList(preset_list, edit=True, doubleClickCommand=on_load_preset)

    # ==========================================
    # INITIALIZE
    # ==========================================

    refresh_stored_list()
    refresh_preset_list()
    update_status_indicator()

    # Selection change script job
    cmds.scriptJob(
        event=["SelectionChanged", update_selection_display],
        parent=window
    )

    # Show window
    cmds.showWindow(window)

    log_message("Temp Pivot Tool ready.", "info")


# ==========================================
# ENTRY POINT
# ==========================================

if __name__ == "__main__":
    show()
