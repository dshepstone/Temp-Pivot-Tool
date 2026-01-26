"""
Temp Pivot Tool for Autodesk Maya

Create a temporary pivot for selected objects so you can rotate them from wherever you like.
The tool uses a constraint-based system to temporarily control object rotation from a
moveable pivot locator. When done, release control and the pivot is saved for future use.

Workflow:
1. Select the object/control you want to manipulate
2. Create a temp pivot (locator appears at selection)
3. Move the pivot locator to your desired rotation point
4. Click "Activate" or press Enter to take control
5. Rotate the temp pivot - the object follows
6. Toggle off to release control (auto-keys the object)
7. Pivot is saved and can be recalled later

Features:
- Constraint-based rotation control
- Auto-keying when releasing control
- Stored pivots per object with local positioning
- Interactive pivot placement
- Smart Euler Filter support
- Modern UI with helpful tooltips

Author: David Shepstone
License: MIT
Version: 2.1.0
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from functools import partial

import maya.cmds as cmds
import maya.api.OpenMaya as om

# -----------------------------
# Constants
# -----------------------------

WINDOW_NAME = "tempPivotToolWindow"
WINDOW_TITLE = "Temp Pivot"
MANAGER_NODE_NAME = "tempPivotManager"
PIVOT_LOCATOR_PREFIX = "tempPivot_"
PIVOT_GROUP_NAME = "tempPivotControls_grp"
CONSTRAINT_SUFFIX = "_tempPivotConstraint"

# Pivot modes for initial placement
PIVOT_MODES = [
    "At Selection Center",
    "At Object Pivot",
    "At World Origin",
    "At Custom Position",
]

# UI Colors
UI_COLORS = {
    "bg_dark": (0.18, 0.18, 0.20),
    "bg_medium": (0.22, 0.22, 0.24),
    "accent": (0.36, 0.68, 0.93),
    "success": (0.20, 0.75, 0.45),
    "warning": (0.95, 0.77, 0.26),
    "error": (0.95, 0.35, 0.35),
    "on_state": (0.20, 0.75, 0.45),
    "off_state": (0.45, 0.45, 0.48),
    "active": (0.95, 0.65, 0.25),
}

# Tooltips for UI elements
TOOLTIPS = {
    "apply_btn": "Create a new temp pivot for the selected object(s).\nThe pivot locator will appear at the selection center.\nMove it to your desired rotation point before activating.",
    "activate_btn": "Activate the temp pivot to control the object.\nThe object will ROTATE AROUND the pivot location.\nLike a door hinge - the pivot is the hinge point.\nShortcut: Press Enter when pivot is selected.",
    "deactivate_btn": "Release control and bake the transform.\nThe object's position and rotation will be keyed.\nThe pivot position is saved for future use.",
    "toggle_btn": "Toggle the temp pivot on/off.\nWhen ON: Object rotates AROUND the temp pivot point.\nWhen OFF: Object returns to normal control.",
    "reset_btn": "Reset the selected object to its original pivot.\nRemoves any active temp pivot control.",
    "create_locator_btn": "Create a new pivot locator at the current selection.\nMove this locator to set where the rotation point will be.\nThen click Activate to start controlling the object.",
    "delete_locator_btn": "Delete the selected temp pivot locator.\nThis also removes any stored data for this pivot.",
    "move_pivot_btn": "Enter move mode to reposition the pivot.\nMove the locator, then click Activate to apply.",
    "autokey_checkbox": "When enabled, automatically set keyframes on the\nobject's translate and rotate when releasing control.",
    "euler_checkbox": "Apply Euler filter to rotation curves after keying.\nHelps prevent gimbal flipping between keyframes.",
    "affect_scale_checkbox": "Also affect the scale pivot when applying temp pivot.\nUseful when scaling from the same point as rotation.",
    "stored_list": "List of all temp pivots created in this scene.\nDouble-click to select the pivot locator.\n[ACTIVE] means the pivot is currently controlling an object.",
    "recall_btn": "Move the pivot back to its saved position.\nUseful if the target object has moved and you want\nthe pivot to follow.",
    "delete_stored_btn": "Delete the selected stored pivot permanently.\nThis removes both the locator and saved data.",
    "refresh_btn": "Refresh the list of stored pivots.\nUse if pivots were created/deleted outside this tool.",
    "pivot_mode": "Choose where to initially place the pivot locator:\n- At Selection Center: Center of all selected objects\n- At Object Pivot: At the object's current pivot\n- At World Origin: At (0, 0, 0)\n- At Custom Position: Enter coordinates manually",
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
    _add_bool_attr(node, "autoKeyEnabled", True)
    _add_bool_attr(node, "smartEulerFilter", False)
    _add_bool_attr(node, "affectScalePivot", False)
    _add_string_attr(node, "storedPivotsJson", "{}")

    if not cmds.attributeQuery("pivotLocators", node=node, exists=True):
        cmds.addAttr(node, longName="pivotLocators", attributeType="message", multi=True)

    return node


def _sanitize_name(name: str) -> str:
    """Sanitize a node name for use in attribute/node names."""
    return name.replace("|", "_").replace(":", "_").replace(" ", "_")


def _get_stored_pivots() -> Dict[str, Any]:
    """Get stored pivot configurations from the manager node."""
    manager = get_or_create_manager()
    raw = cmds.getAttr(f"{manager}.storedPivotsJson") or "{}"
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (ValueError, TypeError):
        pass
    return {}


def _set_stored_pivots(data: Dict[str, Any]) -> None:
    """Set stored pivot configurations on the manager node."""
    manager = get_or_create_manager()
    cmds.setAttr(f"{manager}.storedPivotsJson", json.dumps(data), type="string")


# -----------------------------
# Pivot Locator Management
# -----------------------------

def get_pivot_group() -> str:
    """Get or create the pivot locators group."""
    if not cmds.objExists(PIVOT_GROUP_NAME):
        grp = cmds.createNode("transform", name=PIVOT_GROUP_NAME)
        cmds.setAttr(f"{grp}.visibility", 1)
        # Lock transforms on group
        for attr in ["tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz"]:
            cmds.setAttr(f"{grp}.{attr}", lock=True)
    return PIVOT_GROUP_NAME


def create_pivot_locator(
    name: str,
    position: Tuple[float, float, float],
    target_object: str,
) -> str:
    """
    Create a temp pivot locator for controlling an object.

    Args:
        name: Base name for the locator
        position: World position for the locator
        target_object: The object this pivot will control

    Returns:
        The name of the created locator
    """
    # Sanitize name
    safe_name = _sanitize_name(name)
    locator_name = f"{PIVOT_LOCATOR_PREFIX}{safe_name}"

    # Delete if exists
    if cmds.objExists(locator_name):
        delete_pivot_locator(locator_name)

    # Create locator with custom shape
    locator = cmds.spaceLocator(name=locator_name)[0]

    # Style the locator shape
    shape = cmds.listRelatives(locator, shapes=True)[0]
    cmds.setAttr(f"{shape}.localScaleX", 0.3)
    cmds.setAttr(f"{shape}.localScaleY", 0.3)
    cmds.setAttr(f"{shape}.localScaleZ", 0.3)

    # Set color (orange/yellow for visibility)
    cmds.setAttr(f"{shape}.overrideEnabled", 1)
    cmds.setAttr(f"{shape}.overrideRGBColors", 1)
    cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["active"][0])
    cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["active"][1])
    cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["active"][2])

    # Create visual rings for each axis
    for axis, color, normal in [
        ("X", (1, 0.3, 0.3), (1, 0, 0)),
        ("Y", (0.3, 1, 0.3), (0, 1, 0)),
        ("Z", (0.3, 0.5, 1), (0, 0, 1))
    ]:
        circle = cmds.circle(
            name=f"{locator_name}_ring{axis}",
            normal=normal,
            radius=0.5,
            degree=3,
            sections=24,
            constructionHistory=False
        )[0]
        circle_shape = cmds.listRelatives(circle, shapes=True)[0]
        cmds.setAttr(f"{circle_shape}.overrideEnabled", 1)
        cmds.setAttr(f"{circle_shape}.overrideRGBColors", 1)
        cmds.setAttr(f"{circle_shape}.overrideColorR", color[0])
        cmds.setAttr(f"{circle_shape}.overrideColorG", color[1])
        cmds.setAttr(f"{circle_shape}.overrideColorB", color[2])
        # Parent shape under locator
        cmds.parent(circle_shape, locator, shape=True, relative=True)
        cmds.delete(circle)

    # Add custom attributes to store pivot data
    _add_string_attr(locator, "targetObject", target_object)
    _add_bool_attr(locator, "isActive", False)
    _add_double3_attr(locator, "localOffset")
    _add_string_attr(locator, "constraintNode", "")

    # Calculate and store local offset from target
    if cmds.objExists(target_object):
        target_pos = cmds.xform(target_object, q=True, ws=True, rp=True)
        local_offset = (
            position[0] - target_pos[0],
            position[1] - target_pos[1],
            position[2] - target_pos[2]
        )
        cmds.setAttr(f"{locator}.localOffset", *local_offset)

    # Position the locator
    cmds.xform(locator, worldSpace=True, translation=position)

    # Match target rotation
    if cmds.objExists(target_object):
        rot = cmds.xform(target_object, q=True, ws=True, ro=True)
        cmds.xform(locator, worldSpace=True, rotation=rot)

    # Parent to pivot group
    grp = get_pivot_group()
    cmds.parent(locator, grp)

    # Connect to manager
    manager = get_or_create_manager()
    if not cmds.attributeQuery("manager", node=locator, exists=True):
        cmds.addAttr(locator, longName="manager", attributeType="message")

    # Find next available index
    indices = cmds.getAttr(f"{manager}.pivotLocators", multiIndices=True) or []
    next_idx = max(indices) + 1 if indices else 0
    cmds.connectAttr(f"{manager}.pivotLocators[{next_idx}]", f"{locator}.manager", force=True)

    # Store pivot data
    _store_pivot_data(locator, target_object, position)

    return locator


def delete_pivot_locator(locator_name: str) -> bool:
    """Delete a pivot locator and clean up all associated nodes."""
    if not cmds.objExists(locator_name):
        return False

    # Deactivate first if active (this will clean up constraints and helper nodes)
    if cmds.attributeQuery("isActive", node=locator_name, exists=True):
        if cmds.getAttr(f"{locator_name}.isActive"):
            deactivate_pivot(locator_name, auto_key=False)

    # Clean up any remaining helper nodes
    for suffix in ["_pivotDriver", "_targetOffset", "_pivotGrp"]:
        node = f"{locator_name}{suffix}"
        if cmds.objExists(node):
            cmds.delete(node)

    # Remove from stored data
    stored = _get_stored_pivots()
    if locator_name in stored:
        del stored[locator_name]
        _set_stored_pivots(stored)

    # Delete the locator
    if cmds.objExists(locator_name):
        cmds.delete(locator_name)

    # Clean up empty group
    if cmds.objExists(PIVOT_GROUP_NAME):
        children = cmds.listRelatives(PIVOT_GROUP_NAME, children=True) or []
        if not children:
            cmds.delete(PIVOT_GROUP_NAME)

    return True


def get_all_pivot_locators() -> List[str]:
    """Get all temp pivot locators in the scene."""
    locators = cmds.ls(f"{PIVOT_LOCATOR_PREFIX}*", type="transform") or []
    # Filter to only valid pivot locators (have our custom attributes)
    valid = []
    for loc in locators:
        if cmds.attributeQuery("targetObject", node=loc, exists=True):
            valid.append(loc)
    return valid


def _store_pivot_data(locator: str, target: str, position: Tuple[float, float, float]) -> None:
    """Store pivot data for persistence."""
    stored = _get_stored_pivots()

    # Calculate local offset from target
    local_offset = [0, 0, 0]
    if cmds.objExists(target):
        target_matrix = cmds.xform(target, q=True, ws=True, matrix=True)
        target_pos = cmds.xform(target, q=True, ws=True, rp=True)
        # Store offset in local space of target
        local_offset = [
            position[0] - target_pos[0],
            position[1] - target_pos[1],
            position[2] - target_pos[2]
        ]

    stored[locator] = {
        "target": target,
        "localOffset": local_offset,
        "worldPosition": list(position),
        "isActive": False,
    }
    _set_stored_pivots(stored)


# -----------------------------
# Constraint-Based Control System
# -----------------------------

def activate_pivot(locator: str) -> Tuple[bool, str]:
    """
    Activate the temp pivot to control the target object.
    Creates a parent constraint to make the object rotate AROUND the pivot point.

    When you rotate the temp pivot locator, the target object will orbit around
    the pivot location - like a planet orbiting the sun.

    Args:
        locator: The pivot locator to activate

    Returns:
        Tuple of (success, message)
    """
    if not cmds.objExists(locator):
        return False, f"Locator '{locator}' not found."

    # Check if already active
    if cmds.getAttr(f"{locator}.isActive"):
        return False, "Pivot is already active."

    # Get target object
    target = cmds.getAttr(f"{locator}.targetObject")
    if not target or not cmds.objExists(target):
        return False, f"Target object '{target}' not found."

    # Check for locked translation attributes (needed for orbiting)
    for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
        if cmds.getAttr(f"{target}.{attr}", lock=True):
            return False, f"Cannot activate: {target}.{attr} is locked."

    # Store original transform values BEFORE creating constraints
    orig_translate = cmds.getAttr(f"{target}.translate")[0]
    orig_rotate = cmds.getAttr(f"{target}.rotate")[0]

    # Add attributes to store original values if they don't exist
    if not cmds.attributeQuery("origTranslate", node=locator, exists=True):
        _add_double3_attr(locator, "origTranslate")
    if not cmds.attributeQuery("origRotation", node=locator, exists=True):
        _add_double3_attr(locator, "origRotation")

    cmds.setAttr(f"{locator}.origTranslate", *orig_translate)
    cmds.setAttr(f"{locator}.origRotation", *orig_rotate)

    # Create a pivot driver group at the locator position
    # This group will be the "pivot point" that the object rotates around
    pivot_driver = cmds.createNode("transform", name=f"{locator}_pivotDriver")

    # Match locator world position and rotation
    loc_pos = cmds.xform(locator, q=True, ws=True, t=True)
    loc_rot = cmds.xform(locator, q=True, ws=True, ro=True)
    cmds.xform(pivot_driver, ws=True, t=loc_pos)
    cmds.xform(pivot_driver, ws=True, ro=loc_rot)

    # Create a target offset group that maintains the target's position relative to pivot
    # This is parented under the pivot driver so when pivot rotates, this orbits
    target_offset = cmds.createNode("transform", name=f"{locator}_targetOffset")

    # Position the offset group at the target's current world position
    target_pos = cmds.xform(target, q=True, ws=True, t=True)
    target_rot = cmds.xform(target, q=True, ws=True, ro=True)
    cmds.xform(target_offset, ws=True, t=target_pos)
    cmds.xform(target_offset, ws=True, ro=target_rot)

    # Parent the offset under the pivot driver
    # Now when pivot_driver rotates, target_offset orbits around it
    cmds.parent(target_offset, pivot_driver)

    # Create parent constraint from target_offset to the actual target
    # This makes the target follow the offset group (which orbits the pivot)
    constraint = cmds.parentConstraint(
        target_offset, target,
        maintainOffset=False,  # We want exact following since offset is already positioned
        name=f"{target}{CONSTRAINT_SUFFIX}"
    )[0]

    # Parent the pivot driver under the locator
    # So when user rotates the locator, the whole system rotates
    cmds.parent(pivot_driver, locator)

    # Store references
    cmds.setAttr(f"{locator}.constraintNode", constraint, type="string")

    # Store additional node references
    if not cmds.attributeQuery("pivotDriver", node=locator, exists=True):
        _add_string_attr(locator, "pivotDriver", pivot_driver)
    else:
        cmds.setAttr(f"{locator}.pivotDriver", pivot_driver, type="string")

    if not cmds.attributeQuery("targetOffset", node=locator, exists=True):
        _add_string_attr(locator, "targetOffset", target_offset)
    else:
        cmds.setAttr(f"{locator}.targetOffset", target_offset, type="string")

    cmds.setAttr(f"{locator}.isActive", True)

    # Update stored data
    stored = _get_stored_pivots()
    if locator in stored:
        stored[locator]["isActive"] = True
        _set_stored_pivots(stored)

    # Change locator color to indicate active state (green)
    shapes = cmds.listRelatives(locator, shapes=True) or []
    for shape in shapes:
        if cmds.nodeType(shape) == "locator":
            cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["success"][0])
            cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["success"][1])
            cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["success"][2])

    return True, f"Activated temp pivot for '{target}'. Rotate the pivot locator - the object will rotate around it."


def deactivate_pivot(locator: str, auto_key: bool = True) -> Tuple[bool, str]:
    """
    Deactivate the temp pivot and return control to the object.
    Bakes the current world-space transform onto the target and optionally sets keyframes.

    Args:
        locator: The pivot locator to deactivate
        auto_key: Whether to set keyframes on the object

    Returns:
        Tuple of (success, message)
    """
    if not cmds.objExists(locator):
        return False, f"Locator '{locator}' not found."

    if not cmds.getAttr(f"{locator}.isActive"):
        return False, "Pivot is not active."

    target = cmds.getAttr(f"{locator}.targetObject")
    constraint_name = cmds.getAttr(f"{locator}.constraintNode")

    # Get current world-space transform BEFORE removing constraint
    current_translate = [0, 0, 0]
    current_rotate = [0, 0, 0]
    if target and cmds.objExists(target):
        current_translate = cmds.xform(target, q=True, ws=True, t=True)
        current_rotate = cmds.xform(target, q=True, ws=True, ro=True)

    # Delete constraint first
    if constraint_name and cmds.objExists(constraint_name):
        cmds.delete(constraint_name)

    # Delete pivot driver and target offset groups
    pivot_driver = f"{locator}_pivotDriver"
    if cmds.objExists(pivot_driver):
        cmds.delete(pivot_driver)

    # Legacy cleanup (in case old pivot groups exist)
    pivot_grp = f"{locator}_pivotGrp"
    if cmds.objExists(pivot_grp):
        cmds.delete(pivot_grp)

    # Apply the world-space transform directly to target
    if target and cmds.objExists(target):
        # Set the transform values
        cmds.xform(target, ws=True, t=current_translate)
        cmds.xform(target, ws=True, ro=current_rotate)

        if auto_key:
            manager = get_or_create_manager()
            if cmds.getAttr(f"{manager}.autoKeyEnabled"):
                current_time = cmds.currentTime(query=True)

                # Key both translation and rotation
                for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
                    if not cmds.getAttr(f"{target}.{attr}", lock=True):
                        try:
                            cmds.setKeyframe(target, attribute=attr, time=current_time)
                        except RuntimeError:
                            pass  # Skip if attribute can't be keyed

                # Apply euler filter if enabled
                if cmds.getAttr(f"{manager}.smartEulerFilter"):
                    _apply_euler_filter(target)

    # Clear stored references
    cmds.setAttr(f"{locator}.constraintNode", "", type="string")
    if cmds.attributeQuery("pivotDriver", node=locator, exists=True):
        cmds.setAttr(f"{locator}.pivotDriver", "", type="string")
    if cmds.attributeQuery("targetOffset", node=locator, exists=True):
        cmds.setAttr(f"{locator}.targetOffset", "", type="string")

    cmds.setAttr(f"{locator}.isActive", False)

    # Update stored position (save current locator position as local offset)
    _update_stored_position(locator)

    # Update stored data
    stored = _get_stored_pivots()
    if locator in stored:
        stored[locator]["isActive"] = False
        _set_stored_pivots(stored)

    # Reset locator color (back to orange)
    shapes = cmds.listRelatives(locator, shapes=True) or []
    for shape in shapes:
        if cmds.nodeType(shape) == "locator":
            cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["active"][0])
            cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["active"][1])
            cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["active"][2])

    keyed_msg = " Transform keyed." if auto_key else ""
    return True, f"Deactivated temp pivot for '{target}'.{keyed_msg} Pivot position saved."


def _update_stored_position(locator: str) -> None:
    """Update the stored local offset for a pivot."""
    if not cmds.objExists(locator):
        return

    target = cmds.getAttr(f"{locator}.targetObject")
    if not target or not cmds.objExists(target):
        return

    loc_pos = cmds.xform(locator, q=True, ws=True, t=True)
    target_pos = cmds.xform(target, q=True, ws=True, rp=True)

    local_offset = [
        loc_pos[0] - target_pos[0],
        loc_pos[1] - target_pos[1],
        loc_pos[2] - target_pos[2]
    ]

    cmds.setAttr(f"{locator}.localOffset", *local_offset)

    # Update stored data
    stored = _get_stored_pivots()
    if locator in stored:
        stored[locator]["localOffset"] = local_offset
        stored[locator]["worldPosition"] = loc_pos
        _set_stored_pivots(stored)


def toggle_pivot(locator: str) -> Tuple[bool, str, bool]:
    """
    Toggle pivot activation state.

    Returns:
        Tuple of (success, message, is_now_active)
    """
    if not cmds.objExists(locator):
        return False, f"Locator '{locator}' not found.", False

    is_active = cmds.getAttr(f"{locator}.isActive")

    if is_active:
        success, msg = deactivate_pivot(locator)
        return success, msg, False
    else:
        success, msg = activate_pivot(locator)
        return success, msg, True


def _apply_euler_filter(target: str) -> bool:
    """Apply Euler filter to rotation curves."""
    try:
        rotate_curves = []
        for axis in ["rx", "ry", "rz"]:
            curves = cmds.listConnections(f"{target}.{axis}", type="animCurve") or []
            rotate_curves.extend(curves)

        if rotate_curves:
            cmds.filterCurve(rotate_curves)
            return True
    except RuntimeError:
        pass
    return False


# -----------------------------
# Pivot Position Utilities
# -----------------------------

def compute_pivot_position(mode: str, objects: List[str], custom_pos: Optional[Tuple[float, float, float]] = None) -> Optional[Tuple[float, float, float]]:
    """Compute the initial position for a pivot based on mode."""

    if mode == "At World Origin":
        return (0.0, 0.0, 0.0)

    if mode == "At Custom Position" and custom_pos:
        return custom_pos

    if not objects:
        return None

    if mode == "At Object Pivot":
        # Use first object's pivot
        try:
            pos = cmds.xform(objects[0], q=True, ws=True, rp=True)
            return (pos[0], pos[1], pos[2])
        except RuntimeError:
            return None

    if mode == "At Selection Center":
        # Average of all object pivots
        positions = []
        for obj in objects:
            try:
                pos = cmds.xform(obj, q=True, ws=True, rp=True)
                positions.append(pos)
            except RuntimeError:
                continue

        if not positions:
            return None

        avg = [sum(p[i] for p in positions) / len(positions) for i in range(3)]
        return (avg[0], avg[1], avg[2])

    return None


def recall_pivot_position(locator: str) -> bool:
    """Move a pivot locator to its stored position relative to its target."""
    if not cmds.objExists(locator):
        return False

    target = cmds.getAttr(f"{locator}.targetObject")
    if not target or not cmds.objExists(target):
        return False

    # Get stored local offset
    local_offset = cmds.getAttr(f"{locator}.localOffset")[0]

    # Get current target position
    target_pos = cmds.xform(target, q=True, ws=True, rp=True)

    # Calculate world position
    world_pos = [
        target_pos[0] + local_offset[0],
        target_pos[1] + local_offset[1],
        target_pos[2] + local_offset[2]
    ]

    # Move locator
    cmds.xform(locator, ws=True, t=world_pos)

    return True


# -----------------------------
# UI Utilities
# -----------------------------

def _get_selection() -> List[str]:
    """Get current transform selection."""
    return cmds.ls(selection=True, type="transform") or []


def _get_selected_pivot_locators() -> List[str]:
    """Get selected pivot locators."""
    sel = _get_selection()
    pivots = []
    for item in sel:
        if item.startswith(PIVOT_LOCATOR_PREFIX):
            if cmds.attributeQuery("targetObject", node=item, exists=True):
                pivots.append(item)
    return pivots


# -----------------------------
# Modern UI Implementation
# -----------------------------

def show() -> None:
    """Show the Temp Pivot Tool window with modern UI."""

    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME)

    manager = get_or_create_manager()

    # Window setup
    window = cmds.window(
        WINDOW_NAME,
        title=WINDOW_TITLE,
        sizeable=True,
        minimizeButton=True,
        maximizeButton=False,
        width=340,
        height=680
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
        columnWidth2=(48, 280)
    )

    cmds.canvas(
        width=44,
        height=44,
        rgbValue=UI_COLORS["accent"]
    )

    title_col = cmds.columnLayout(adjustableColumn=True)
    cmds.text(
        label="Temp Pivot Tool",
        font="boldLabelFont",
        align="left",
        height=22
    )
    cmds.text(
        label="Create temporary rotation pivots for any object",
        align="left",
        font="smallPlainLabelFont",
        height=16
    )
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # Description text
    cmds.text(
        label="Select an object, create a pivot, move it to your desired\n"
              "rotation point, then Activate. The object will orbit around\n"
              "the pivot like a door rotating around its hinge.",
        align="left",
        wordWrap=True,
        height=44,
        font="smallPlainLabelFont"
    )

    cmds.separator(height=12, style="none")

    # ==========================================
    # STATUS SECTION
    # ==========================================

    cmds.frameLayout(
        label="Status",
        collapsable=False,
        marginWidth=8,
        marginHeight=8
    )

    status_row = cmds.rowLayout(
        numberOfColumns=3,
        adjustableColumn=2,
        columnWidth3=(65, 180, 70)
    )

    state_indicator = cmds.button(
        label="READY",
        width=60,
        height=28,
        backgroundColor=UI_COLORS["off_state"],
        enable=False,
        annotation="Current state of the temp pivot system"
    )

    selection_text = cmds.text(
        label="No object selected",
        align="left",
        annotation="Currently selected object(s)"
    )

    active_count_text = cmds.text(
        label="0 pivots",
        align="right",
        annotation="Number of temp pivots in scene"
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # CREATE PIVOT SECTION
    # ==========================================

    cmds.frameLayout(
        label="Create Temp Pivot",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8,
        annotation="Create a new temporary pivot for the selected object"
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=6)

    # Mode selection
    cmds.text(
        label="Initial Placement:",
        align="left",
        font="smallBoldLabelFont",
        annotation=TOOLTIPS["pivot_mode"]
    )

    mode_menu = cmds.optionMenu(
        height=26,
        annotation=TOOLTIPS["pivot_mode"]
    )
    for mode in PIVOT_MODES:
        cmds.menuItem(label=mode)

    # Set last used mode
    last_mode = cmds.getAttr(f"{manager}.lastUsedMode") or PIVOT_MODES[0]
    if last_mode in PIVOT_MODES:
        cmds.optionMenu(mode_menu, edit=True, value=last_mode)

    cmds.separator(height=4, style="none")

    # Create button
    create_btn = cmds.button(
        label="Create Temp Pivot",
        height=36,
        backgroundColor=UI_COLORS["accent"],
        annotation=TOOLTIPS["apply_btn"]
    )

    cmds.text(
        label="Creates a pivot locator at the selected object.\n"
              "Move the locator to where you want the rotation center.\n"
              "Example: For a foot, place it at the ball or heel.",
        align="left",
        font="smallPlainLabelFont",
        wordWrap=True,
        height=42
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # PIVOT CONTROL SECTION
    # ==========================================

    cmds.frameLayout(
        label="Pivot Control",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8,
        annotation="Activate, deactivate, and control temp pivots"
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    cmds.text(
        label="After positioning the pivot, activate it. Rotating the pivot\n"
              "will make the object orbit around the pivot point:",
        align="left",
        font="smallPlainLabelFont",
        height=30
    )

    # Activate/Deactivate row
    activate_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(160, 160)
    )

    activate_btn = cmds.button(
        label="Activate (Enter)",
        height=32,
        backgroundColor=UI_COLORS["success"],
        annotation=TOOLTIPS["activate_btn"]
    )

    deactivate_btn = cmds.button(
        label="Deactivate",
        height=32,
        annotation=TOOLTIPS["deactivate_btn"]
    )

    cmds.setParent("..")

    toggle_btn = cmds.button(
        label="Toggle On/Off",
        height=28,
        annotation=TOOLTIPS["toggle_btn"]
    )

    cmds.separator(height=8, style="in")

    cmds.text(
        label="Pivot Locator:",
        align="left",
        font="smallBoldLabelFont"
    )

    locator_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(160, 160)
    )

    select_pivot_btn = cmds.button(
        label="Select Pivot Locator",
        height=26,
        annotation="Select the temp pivot locator in the viewport"
    )

    select_target_btn = cmds.button(
        label="Select Target Object",
        height=26,
        annotation="Select the object controlled by the pivot"
    )

    cmds.setParent("..")

    delete_pivot_btn = cmds.button(
        label="Delete Selected Pivot",
        height=26,
        annotation=TOOLTIPS["delete_locator_btn"]
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # SETTINGS SECTION
    # ==========================================

    cmds.frameLayout(
        label="Settings",
        collapsable=True,
        collapse=True,
        marginWidth=8,
        marginHeight=8
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    autokey_checkbox = cmds.checkBox(
        label="Auto-Key on Deactivate",
        value=cmds.getAttr(f"{manager}.autoKeyEnabled"),
        annotation=TOOLTIPS["autokey_checkbox"]
    )

    euler_checkbox = cmds.checkBox(
        label="Smart Euler Filter",
        value=cmds.getAttr(f"{manager}.smartEulerFilter"),
        annotation=TOOLTIPS["euler_checkbox"]
    )

    affect_scale_checkbox = cmds.checkBox(
        label="Affect Scale Pivot",
        value=cmds.getAttr(f"{manager}.affectScalePivot"),
        annotation=TOOLTIPS["affect_scale_checkbox"]
    )

    cmds.separator(height=8, style="none")

    cmds.text(
        label="TIP: If you notice rotation weirdness between\n"
              "keyframes, enable Smart Euler Filter.",
        align="left",
        font="smallPlainLabelFont",
        height=30
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # STORED PIVOTS SECTION
    # ==========================================

    cmds.frameLayout(
        label="Stored Pivots",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8,
        annotation="View and manage all temp pivots in the scene"
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    cmds.text(
        label="All temp pivots in this scene:",
        align="left",
        font="smallBoldLabelFont",
        height=18
    )

    pivot_list = cmds.textScrollList(
        height=120,
        allowMultiSelection=False,
        annotation=TOOLTIPS["stored_list"]
    )

    # List buttons
    list_btns_row1 = cmds.rowLayout(
        numberOfColumns=3,
        adjustableColumn=1,
        columnWidth3=(105, 105, 105)
    )

    recall_btn = cmds.button(
        label="Recall Position",
        height=26,
        annotation=TOOLTIPS["recall_btn"]
    )

    rename_btn = cmds.button(
        label="Rename",
        height=26,
        annotation="Rename the selected pivot"
    )

    delete_stored_btn = cmds.button(
        label="Delete",
        height=26,
        annotation=TOOLTIPS["delete_stored_btn"]
    )

    cmds.setParent("..")

    list_btns_row2 = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(160, 160)
    )

    activate_stored_btn = cmds.button(
        label="Activate Selected",
        height=26,
        annotation="Activate the selected pivot from the list"
    )

    refresh_btn = cmds.button(
        label="Refresh List",
        height=26,
        annotation=TOOLTIPS["refresh_btn"]
    )

    cmds.setParent("..")

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # OUTPUT LOG SECTION
    # ==========================================

    cmds.frameLayout(
        label="Output Log",
        collapsable=True,
        collapse=True,
        marginWidth=8,
        marginHeight=8
    )

    log_field = cmds.scrollField(
        height=80,
        editable=False,
        wordWrap=True,
        text="Ready. Select an object and create a temp pivot."
    )

    cmds.setParent("..")

    cmds.separator(height=16, style="none")

    # ==========================================
    # CALLBACK FUNCTIONS
    # ==========================================

    def log_message(message: str, msg_type: str = "info") -> None:
        """Log a message to the output field."""
        prefix_map = {
            "warning": "[!] ",
            "error": "[X] ",
            "success": "[OK] ",
            "info": ""
        }
        prefix = prefix_map.get(msg_type, "")

        current = cmds.scrollField(log_field, query=True, text=True) or ""
        new_text = f"{prefix}{message}"
        if current and not current.startswith("Ready."):
            new_text = f"{current}\n{new_text}"
        cmds.scrollField(log_field, edit=True, text=new_text)
        cmds.scrollField(log_field, edit=True, insertionPosition=len(new_text))

    def update_status() -> None:
        """Update the status display."""
        sel = _get_selection()
        pivots = get_all_pivot_locators()
        selected_pivots = _get_selected_pivot_locators()

        # Update selection text
        if selected_pivots:
            pivot = selected_pivots[0]
            target = cmds.getAttr(f"{pivot}.targetObject") if cmds.objExists(pivot) else "Unknown"
            is_active = cmds.getAttr(f"{pivot}.isActive") if cmds.objExists(pivot) else False
            status = "ACTIVE" if is_active else "Ready"
            cmds.text(selection_text, edit=True, label=f"Pivot for: {target}")

            if is_active:
                cmds.button(state_indicator, edit=True, label="ACTIVE", backgroundColor=UI_COLORS["success"])
            else:
                cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["active"])
        elif sel:
            # Check if any selected object has an active pivot
            active_for_sel = False
            for pivot in pivots:
                target = cmds.getAttr(f"{pivot}.targetObject")
                if target in sel and cmds.getAttr(f"{pivot}.isActive"):
                    active_for_sel = True
                    break

            if active_for_sel:
                cmds.button(state_indicator, edit=True, label="ACTIVE", backgroundColor=UI_COLORS["success"])
            else:
                cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])

            obj_count = len(sel)
            cmds.text(selection_text, edit=True, label=f"{obj_count} object{'s' if obj_count != 1 else ''} selected")
        else:
            cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])
            cmds.text(selection_text, edit=True, label="No object selected")

        # Update pivot count
        cmds.text(active_count_text, edit=True, label=f"{len(pivots)} pivot{'s' if len(pivots) != 1 else ''}")

    def refresh_pivot_list() -> None:
        """Refresh the stored pivots list."""
        cmds.textScrollList(pivot_list, edit=True, removeAll=True)

        pivots = get_all_pivot_locators()
        for pivot in sorted(pivots):
            target = cmds.getAttr(f"{pivot}.targetObject") if cmds.objExists(pivot) else "?"
            is_active = cmds.getAttr(f"{pivot}.isActive") if cmds.objExists(pivot) else False
            status = " [ACTIVE]" if is_active else ""
            display_name = f"{pivot} -> {target}{status}"
            cmds.textScrollList(pivot_list, edit=True, append=display_name)

        update_status()

    def save_settings() -> None:
        """Save current settings to manager node."""
        mode = cmds.optionMenu(mode_menu, query=True, value=True)
        autokey = cmds.checkBox(autokey_checkbox, query=True, value=True)
        euler = cmds.checkBox(euler_checkbox, query=True, value=True)
        affect_scale = cmds.checkBox(affect_scale_checkbox, query=True, value=True)

        cmds.setAttr(f"{manager}.lastUsedMode", mode, type="string")
        cmds.setAttr(f"{manager}.autoKeyEnabled", autokey)
        cmds.setAttr(f"{manager}.smartEulerFilter", euler)
        cmds.setAttr(f"{manager}.affectScalePivot", affect_scale)

    # ----- Main Callbacks -----

    def on_create(*args) -> None:
        """Create a new temp pivot for the selected object."""
        sel = _get_selection()

        # Filter out existing pivot locators
        sel = [s for s in sel if not s.startswith(PIVOT_LOCATOR_PREFIX)]

        if not sel:
            log_message("Please select an object to create a temp pivot for.", "warning")
            return

        mode = cmds.optionMenu(mode_menu, query=True, value=True)
        save_settings()

        for obj in sel:
            position = compute_pivot_position(mode, [obj])
            if position is None:
                log_message(f"Could not compute position for '{obj}'.", "error")
                continue

            locator = create_pivot_locator(obj, position, obj)
            log_message(f"Created temp pivot '{locator}' for '{obj}'.", "success")

            # Select the new locator
            cmds.select(locator)

        refresh_pivot_list()
        log_message("Move the pivot locator, then click 'Activate' or press Enter.", "info")

    def on_activate(*args) -> None:
        """Activate the selected temp pivot."""
        pivots = _get_selected_pivot_locators()

        if not pivots:
            # Try to find pivot for selected object
            sel = _get_selection()
            all_pivots = get_all_pivot_locators()
            for pivot in all_pivots:
                target = cmds.getAttr(f"{pivot}.targetObject")
                if target in sel:
                    pivots.append(pivot)
                    break

        if not pivots:
            log_message("Select a temp pivot locator to activate.", "warning")
            return

        for pivot in pivots:
            success, msg = activate_pivot(pivot)
            log_message(msg, "success" if success else "error")

        refresh_pivot_list()

    def on_deactivate(*args) -> None:
        """Deactivate the selected temp pivot."""
        pivots = _get_selected_pivot_locators()

        if not pivots:
            # Try to find active pivot for selected object
            sel = _get_selection()
            all_pivots = get_all_pivot_locators()
            for pivot in all_pivots:
                target = cmds.getAttr(f"{pivot}.targetObject")
                if target in sel and cmds.getAttr(f"{pivot}.isActive"):
                    pivots.append(pivot)
                    break

        if not pivots:
            log_message("No active pivot selected.", "warning")
            return

        for pivot in pivots:
            success, msg = deactivate_pivot(pivot)
            log_message(msg, "success" if success else "error")

        refresh_pivot_list()

    def on_toggle(*args) -> None:
        """Toggle the selected pivot."""
        pivots = _get_selected_pivot_locators()

        if not pivots:
            sel = _get_selection()
            all_pivots = get_all_pivot_locators()
            for pivot in all_pivots:
                target = cmds.getAttr(f"{pivot}.targetObject")
                if target in sel:
                    pivots.append(pivot)
                    break

        if not pivots:
            log_message("Select a temp pivot or controlled object.", "warning")
            return

        for pivot in pivots:
            success, msg, is_active = toggle_pivot(pivot)
            log_message(msg, "success" if success else "error")

        refresh_pivot_list()

    def on_select_pivot(*args) -> None:
        """Select the pivot locator for the current selection."""
        sel = _get_selection()
        all_pivots = get_all_pivot_locators()

        found = []
        for pivot in all_pivots:
            target = cmds.getAttr(f"{pivot}.targetObject")
            if target in sel or pivot in sel:
                found.append(pivot)

        if found:
            cmds.select(found)
            log_message(f"Selected pivot(s): {', '.join(found)}", "info")
        else:
            log_message("No pivot found for selected object.", "warning")

        update_status()

    def on_select_target(*args) -> None:
        """Select the target object for the selected pivot."""
        pivots = _get_selected_pivot_locators()

        if not pivots:
            log_message("Select a temp pivot first.", "warning")
            return

        targets = []
        for pivot in pivots:
            target = cmds.getAttr(f"{pivot}.targetObject")
            if target and cmds.objExists(target):
                targets.append(target)

        if targets:
            cmds.select(targets)
            log_message(f"Selected target(s): {', '.join(targets)}", "info")
        else:
            log_message("Target object not found.", "warning")

    def on_delete_pivot(*args) -> None:
        """Delete the selected pivot."""
        pivots = _get_selected_pivot_locators()

        if not pivots:
            log_message("Select a temp pivot to delete.", "warning")
            return

        for pivot in pivots:
            if delete_pivot_locator(pivot):
                log_message(f"Deleted pivot '{pivot}'.", "success")
            else:
                log_message(f"Failed to delete pivot '{pivot}'.", "error")

        refresh_pivot_list()

    def on_recall_position(*args) -> None:
        """Recall the stored position for a pivot."""
        selected = cmds.textScrollList(pivot_list, query=True, selectItem=True) or []
        if not selected:
            log_message("Select a pivot from the list.", "warning")
            return

        # Extract pivot name from display string
        pivot_name = selected[0].split(" -> ")[0]

        if recall_pivot_position(pivot_name):
            log_message(f"Recalled position for '{pivot_name}'.", "success")
            cmds.select(pivot_name)
        else:
            log_message(f"Failed to recall position for '{pivot_name}'.", "error")

    def on_rename(*args) -> None:
        """Rename the selected pivot."""
        selected = cmds.textScrollList(pivot_list, query=True, selectItem=True) or []
        if not selected:
            log_message("Select a pivot from the list.", "warning")
            return

        pivot_name = selected[0].split(" -> ")[0]

        result = cmds.promptDialog(
            title="Rename Pivot",
            message="Enter new name:",
            button=["OK", "Cancel"],
            defaultButton="OK",
            cancelButton="Cancel",
            dismissString="Cancel"
        )

        if result == "OK":
            new_name = cmds.promptDialog(query=True, text=True)
            if new_name:
                new_name = f"{PIVOT_LOCATOR_PREFIX}{_sanitize_name(new_name)}"
                if cmds.objExists(pivot_name):
                    cmds.rename(pivot_name, new_name)
                    log_message(f"Renamed '{pivot_name}' to '{new_name}'.", "success")
                    refresh_pivot_list()

    def on_delete_stored(*args) -> None:
        """Delete the selected pivot from the list."""
        selected = cmds.textScrollList(pivot_list, query=True, selectItem=True) or []
        if not selected:
            log_message("Select a pivot from the list.", "warning")
            return

        pivot_name = selected[0].split(" -> ")[0]

        if delete_pivot_locator(pivot_name):
            log_message(f"Deleted pivot '{pivot_name}'.", "success")
        else:
            log_message(f"Failed to delete pivot '{pivot_name}'.", "error")

        refresh_pivot_list()

    def on_activate_stored(*args) -> None:
        """Activate the pivot selected in the list."""
        selected = cmds.textScrollList(pivot_list, query=True, selectItem=True) or []
        if not selected:
            log_message("Select a pivot from the list.", "warning")
            return

        pivot_name = selected[0].split(" -> ")[0]

        if cmds.objExists(pivot_name):
            is_active = cmds.getAttr(f"{pivot_name}.isActive")
            if is_active:
                success, msg = deactivate_pivot(pivot_name)
            else:
                success, msg = activate_pivot(pivot_name)

            log_message(msg, "success" if success else "error")
            cmds.select(pivot_name)
        else:
            log_message(f"Pivot '{pivot_name}' not found.", "error")

        refresh_pivot_list()

    def on_list_double_click(*args) -> None:
        """Handle double-click on pivot list - select and potentially activate."""
        selected = cmds.textScrollList(pivot_list, query=True, selectItem=True) or []
        if not selected:
            return

        pivot_name = selected[0].split(" -> ")[0]
        if cmds.objExists(pivot_name):
            cmds.select(pivot_name)
            update_status()

    def on_settings_changed(*args) -> None:
        """Handle settings checkbox changes."""
        save_settings()

    # ==========================================
    # CONNECT CALLBACKS
    # ==========================================

    cmds.button(create_btn, edit=True, command=on_create)
    cmds.button(activate_btn, edit=True, command=on_activate)
    cmds.button(deactivate_btn, edit=True, command=on_deactivate)
    cmds.button(toggle_btn, edit=True, command=on_toggle)
    cmds.button(select_pivot_btn, edit=True, command=on_select_pivot)
    cmds.button(select_target_btn, edit=True, command=on_select_target)
    cmds.button(delete_pivot_btn, edit=True, command=on_delete_pivot)

    cmds.button(recall_btn, edit=True, command=on_recall_position)
    cmds.button(rename_btn, edit=True, command=on_rename)
    cmds.button(delete_stored_btn, edit=True, command=on_delete_stored)
    cmds.button(activate_stored_btn, edit=True, command=on_activate_stored)
    cmds.button(refresh_btn, edit=True, command=lambda *_: refresh_pivot_list())

    cmds.checkBox(autokey_checkbox, edit=True, changeCommand=on_settings_changed)
    cmds.checkBox(euler_checkbox, edit=True, changeCommand=on_settings_changed)
    cmds.checkBox(affect_scale_checkbox, edit=True, changeCommand=on_settings_changed)

    cmds.textScrollList(pivot_list, edit=True, doubleClickCommand=on_list_double_click)
    cmds.textScrollList(pivot_list, edit=True, selectCommand=lambda *_: update_status())

    # ==========================================
    # HOTKEY FOR ENTER KEY
    # ==========================================

    # Create a script job to detect Enter key press (via nameCommand)
    def check_enter_key():
        """Check for Enter key activation."""
        # This is called via scriptJob on selection change
        pass

    # ==========================================
    # INITIALIZE
    # ==========================================

    refresh_pivot_list()

    # Selection change script job
    cmds.scriptJob(
        event=["SelectionChanged", update_status],
        parent=window
    )
    cmds.scriptJob(
        event=["SelectionChanged", lambda: refresh_pivot_list()],
        parent=window
    )

    # Show window
    cmds.showWindow(window)

    log_message("Temp Pivot Tool ready. Select an object and create a temp pivot.", "info")


# ==========================================
# HOTKEY SETUP (Optional)
# ==========================================

def setup_enter_hotkey():
    """Set up Enter key as hotkey for activating temp pivot."""
    # Create runtime command
    if not cmds.runTimeCommand("tempPivotActivate", exists=True):
        cmds.runTimeCommand(
            "tempPivotActivate",
            annotation="Activate/Toggle Temp Pivot",
            category="User",
            command="import temp_pivot_tool; temp_pivot_tool.activate_selected_pivot()"
        )

    # Create name command
    cmds.nameCommand(
        "tempPivotActivateNameCommand",
        annotation="Activate/Toggle Temp Pivot",
        command="tempPivotActivate"
    )


def activate_selected_pivot():
    """Activate the currently selected pivot (called via hotkey)."""
    pivots = _get_selected_pivot_locators()

    if not pivots:
        sel = _get_selection()
        all_pivots = get_all_pivot_locators()
        for pivot in all_pivots:
            target = cmds.getAttr(f"{pivot}.targetObject")
            if target in sel:
                pivots.append(pivot)
                break

    if pivots:
        for pivot in pivots:
            is_active = cmds.getAttr(f"{pivot}.isActive")
            if is_active:
                deactivate_pivot(pivot)
            else:
                activate_pivot(pivot)


# ==========================================
# ENTRY POINT
# ==========================================

if __name__ == "__main__":
    show()
