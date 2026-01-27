"""
Temp Pivot Tool for Autodesk Maya

A non-destructive, reusable temporary pivot system for animation.

TWO-STAGE SETUP PROCESS:
  Stage 1: Create locator_1 (pivot) - user moves this to desired pivot location
  Stage 2: Complete setup - creates locator_2 (driver), null_GRP, parenting, constraint

Hierarchy Structure:
    null_GRP (anchor - at control position)
      └ locator_1 (PIVOT - user positioned this in Stage 1)
          └ locator_2 (DRIVER - at control position, constrains control)

Workflow:
1. Select control, click "Create Pivot Locator" (Stage 1)
2. Move locator_1 to where you want the pivot point
3. Click "Complete Setup" (Stage 2) - creates driver, hierarchy, constraint
4. Rotate locator_1 - control orbits around the pivot (auto-keys applied)
5. Toggle OFF - constraint deleted, rig hidden
6. Toggle ON - rig realigns to control, constraint recreated

Features:
- Auto-key: When you rotate locator_1, keyframes are automatically set on the control
- Matrix-based alignment: Proper rotation alignment across all axes (X, Y, Z)

Author: David Shepstone
License: MIT
Version: 5.2.1
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import maya.cmds as cmds

# -----------------------------
# Constants
# -----------------------------

WINDOW_NAME = "tempPivotToolWindow"
WINDOW_TITLE = "Temp Pivot Tool"
TOOL_PREFIX = "TMP"

# Node naming convention
NULL_GRP_SUFFIX = f"_{TOOL_PREFIX}_null_GRP"
LOCATOR_1_SUFFIX = f"_{TOOL_PREFIX}_locator_1"  # PIVOT - user positions this
LOCATOR_2_SUFFIX = f"_{TOOL_PREFIX}_locator_2"  # DRIVER - constrains control
SETTINGS_SUFFIX = f"_{TOOL_PREFIX}_settings"
CONSTRAINT_SUFFIX = f"_{TOOL_PREFIX}_parentConstraint"

# Auto-key scriptJob storage (keyed by settings node name)
_auto_key_jobs: Dict[str, List[int]] = {}

# UI Colors
UI_COLORS = {
    "accent": (0.36, 0.68, 0.93),
    "success": (0.20, 0.75, 0.45),
    "warning": (0.95, 0.77, 0.26),
    "error": (0.95, 0.35, 0.35),
    "stage1": (0.95, 0.65, 0.25),  # Orange - pivot positioning
    "stage2": (0.36, 0.68, 0.93),  # Blue - complete setup
    "on_state": (0.20, 0.75, 0.45),
    "off_state": (0.45, 0.45, 0.48),
}

# Tooltips
TOOLTIPS = {
    "create_pivot_btn": (
        "STAGE 1: Create the pivot locator (locator_1).\n\n"
        "1. Creates locator_1 at the control's position\n"
        "2. Move this locator to your desired pivot point\n"
        "3. Then click 'Complete Setup' to finish"
    ),
    "complete_setup_btn": (
        "STAGE 2: Complete the pivot rig setup.\n\n"
        "1. Creates locator_2 (driver) at control position\n"
        "2. Parents locator_2 under locator_1\n"
        "3. Creates null_GRP at control position\n"
        "4. Parents locator_1 under null_GRP\n"
        "5. Creates parentConstraint: locator_2 → control\n\n"
        "After this, rotating locator_1 will orbit the control."
    ),
    "toggle_btn": (
        "Toggle the temp pivot ON/OFF.\n\n"
        "OFF: Deletes constraint, hides rig.\n"
        "     Control stays in place (key it first!).\n\n"
        "ON: Realigns null_GRP to control position,\n"
        "    recreates constraint."
    ),
    "key_btn": (
        "Set keyframes on the control's translate and rotate.\n"
        "Note: Keys are set automatically when you rotate locator_1.\n"
        "Use this button for manual keying if needed."
    ),
    "delete_btn": (
        "Delete the temp pivot rig completely.\n"
        "Removes all rig nodes and cleans up constraints."
    ),
    "select_pivot_btn": (
        "Select locator_1 (the pivot locator).\n"
        "Rotate this to orbit the control around the pivot."
    ),
    "select_control_btn": (
        "Select the original control.\n"
        "Useful for keying or checking values."
    ),
}


# -----------------------------
# Utility Functions
# -----------------------------

def _sanitize_name(name: str) -> str:
    """Create a safe prefix from a control name."""
    safe = name.split(":")[-1]
    safe = safe.replace("|", "_").replace(" ", "_")
    return safe


def _get_world_xform(node: str) -> Tuple[List[float], List[float]]:
    """Get world-space translate and rotate for a node."""
    translate = cmds.xform(node, q=True, ws=True, t=True)
    rotate = cmds.xform(node, q=True, ws=True, ro=True)
    return translate, rotate


def _set_world_xform(node: str, translate: List[float], rotate: List[float]) -> None:
    """Set world-space translate and rotate for a node."""
    cmds.xform(node, ws=True, t=translate)
    cmds.xform(node, ws=True, ro=rotate)


def _get_world_matrix(node: str) -> List[float]:
    """Get the world matrix of a node as a flat list of 16 floats."""
    return cmds.xform(node, q=True, ws=True, matrix=True)


def _set_world_matrix(node: str, matrix: List[float]) -> None:
    """Set the world matrix of a node from a flat list of 16 floats."""
    cmds.xform(node, ws=True, matrix=matrix)


def _match_transform(source: str, target: str) -> None:
    """Match source node's world transform to target node using matrix."""
    # Use matrix-based matching which properly handles all rotation orders
    matrix = _get_world_matrix(target)
    _set_world_matrix(source, matrix)


def _match_translation(source: str, target: str) -> None:
    """Match only translation."""
    translate, _ = _get_world_xform(target)
    cmds.xform(source, ws=True, t=translate)


def _match_rotation(source: str, target: str) -> None:
    """Match only rotation using matrix decomposition for accuracy."""
    # Get target's world matrix and extract rotation properly
    # by applying just the rotation component to the source
    target_matrix = _get_world_matrix(target)
    source_translate = cmds.xform(source, q=True, ws=True, t=True)

    # Build a new matrix with source's translation but target's rotation/scale
    # Matrix layout: [r00,r01,r02,0, r10,r11,r12,0, r20,r21,r22,0, tx,ty,tz,1]
    new_matrix = target_matrix[:12] + source_translate + [1.0]
    _set_world_matrix(source, new_matrix)


def _add_string_attr(node: str, attr: str, value: str = "") -> None:
    """Add a string attribute if it doesn't exist."""
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, dataType="string")
    cmds.setAttr(f"{node}.{attr}", value, type="string")


def _add_bool_attr(node: str, attr: str, value: bool = False) -> None:
    """Add a boolean attribute if it doesn't exist."""
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, attributeType="bool")
    cmds.setAttr(f"{node}.{attr}", value)


# -----------------------------
# Rig Discovery Functions
# -----------------------------

def get_all_pivot_rigs() -> List[str]:
    """Find all temp pivot rigs in the scene by finding settings nodes."""
    settings_nodes = cmds.ls(f"*{SETTINGS_SUFFIX}", type="transform") or []
    return settings_nodes


def get_rig_for_control(control: str) -> Optional[str]:
    """Find the settings node for a given control, if one exists."""
    settings_nodes = get_all_pivot_rigs()
    for settings in settings_nodes:
        if cmds.attributeQuery("targetControl", node=settings, exists=True):
            target = cmds.getAttr(f"{settings}.targetControl")
            if target == control:
                return settings
    return None


def get_pending_pivot_for_control(control: str) -> Optional[str]:
    """Find a pending (stage 1) pivot locator for a control."""
    # Look for locator_1 that has a targetControl attr but no nullGrp yet
    locators = cmds.ls(f"*{LOCATOR_1_SUFFIX}", type="transform") or []
    for loc in locators:
        if cmds.attributeQuery("targetControl", node=loc, exists=True):
            target = cmds.getAttr(f"{loc}.targetControl")
            if target == control:
                # Check if setup is complete (has nullGrp)
                if cmds.attributeQuery("setupComplete", node=loc, exists=True):
                    if not cmds.getAttr(f"{loc}.setupComplete"):
                        return loc
    return None


def get_rig_nodes(settings_node: str) -> Dict[str, Optional[str]]:
    """Get all rig node names from a settings node."""
    result = {
        "settings": settings_node,
        "null_grp": None,
        "locator_1": None,  # Pivot
        "locator_2": None,  # Driver
        "control": None,
        "constraint": None,
    }

    if not cmds.objExists(settings_node):
        return result

    if cmds.attributeQuery("nullGrp", node=settings_node, exists=True):
        result["null_grp"] = cmds.getAttr(f"{settings_node}.nullGrp") or None
    if cmds.attributeQuery("locator1", node=settings_node, exists=True):
        result["locator_1"] = cmds.getAttr(f"{settings_node}.locator1") or None
    if cmds.attributeQuery("locator2", node=settings_node, exists=True):
        result["locator_2"] = cmds.getAttr(f"{settings_node}.locator2") or None
    if cmds.attributeQuery("targetControl", node=settings_node, exists=True):
        result["control"] = cmds.getAttr(f"{settings_node}.targetControl") or None
    if cmds.attributeQuery("constraintName", node=settings_node, exists=True):
        result["constraint"] = cmds.getAttr(f"{settings_node}.constraintName") or None

    return result


def is_rig_active(settings_node: str) -> bool:
    """Check if a rig is currently active (constraint exists)."""
    if not cmds.objExists(settings_node):
        return False
    if cmds.attributeQuery("isActive", node=settings_node, exists=True):
        return cmds.getAttr(f"{settings_node}.isActive")
    return False


# =============================================================================
# STAGE 1: Create Pivot Locator
# =============================================================================

def create_pivot_locator(control: str) -> Tuple[bool, str, Optional[str]]:
    """
    STAGE 1: Create the pivot locator (locator_1) for user positioning.

    Process:
    1. Create locator_1
    2. Match position/rotation to the selected control
    3. User will move this locator to desired pivot position
    4. Then user clicks "Complete Setup" for Stage 2

    Args:
        control: The control to create a pivot for

    Returns:
        Tuple of (success, message, locator_1_name)
    """
    if not cmds.objExists(control):
        return False, f"Control '{control}' not found.", None

    # Check if rig already exists
    existing = get_rig_for_control(control)
    if existing:
        return False, f"Pivot rig already exists for '{control}'.", None

    # Check if pending pivot exists
    pending = get_pending_pivot_for_control(control)
    if pending:
        cmds.select(pending)
        return False, f"Pivot locator already created. Move it, then click 'Complete Setup'.", pending

    # Create safe prefix
    prefix = _sanitize_name(control)

    # Get control's world transform
    ctrl_translate, ctrl_rotate = _get_world_xform(control)

    # =========================================================================
    # Create locator_1 (the PIVOT - user will position this)
    # =========================================================================
    locator_1 = cmds.spaceLocator(name=f"{prefix}{LOCATOR_1_SUFFIX}")[0]

    # Match to control position initially
    _set_world_xform(locator_1, ctrl_translate, ctrl_rotate)

    # Style locator_1 (orange - indicates pivot point)
    loc1_shape = cmds.listRelatives(locator_1, shapes=True)[0]
    cmds.setAttr(f"{loc1_shape}.overrideEnabled", 1)
    cmds.setAttr(f"{loc1_shape}.overrideRGBColors", 1)
    cmds.setAttr(f"{loc1_shape}.overrideColorR", UI_COLORS["stage1"][0])
    cmds.setAttr(f"{loc1_shape}.overrideColorG", UI_COLORS["stage1"][1])
    cmds.setAttr(f"{loc1_shape}.overrideColorB", UI_COLORS["stage1"][2])
    cmds.setAttr(f"{loc1_shape}.localScaleX", 0.5)
    cmds.setAttr(f"{loc1_shape}.localScaleY", 0.5)
    cmds.setAttr(f"{loc1_shape}.localScaleZ", 0.5)

    # Add visual rings to locator_1 (pivot indicator)
    for axis, color, normal in [
        ("X", (1, 0.3, 0.3), (1, 0, 0)),
        ("Y", (0.3, 1, 0.3), (0, 1, 0)),
        ("Z", (0.3, 0.5, 1), (0, 0, 1))
    ]:
        circle = cmds.circle(
            name=f"{prefix}{LOCATOR_1_SUFFIX}_ring{axis}",
            normal=normal,
            radius=0.6,
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
        cmds.parent(circle_shape, locator_1, shape=True, relative=True)
        cmds.delete(circle)

    # Store target control reference on the locator (for Stage 2)
    _add_string_attr(locator_1, "targetControl", control)
    _add_bool_attr(locator_1, "setupComplete", False)

    # Select the locator so user can move it
    cmds.select(locator_1)

    return True, f"Stage 1 complete. Move locator_1 to pivot position, then click 'Complete Setup'.", locator_1


# =============================================================================
# STAGE 2: Complete Setup
# =============================================================================

def complete_setup(locator_1: str) -> Tuple[bool, str, Optional[str]]:
    """
    STAGE 2: Complete the pivot rig setup.

    Process:
    1. Get the target control from locator_1
    2. Create locator_2 (driver) at control position
    3. Parent locator_2 under locator_1
    4. Create null_GRP
    5. Match null_GRP to locator_2 (control position)
    6. Parent locator_1 under null_GRP
    7. Create parentConstraint: locator_2 → control (maintainOffset)
    8. Create settings node

    Resulting hierarchy:
        null_GRP (at control position)
          └ locator_1 (PIVOT - user positioned)
              └ locator_2 (DRIVER - constrains control)

    Args:
        locator_1: The pivot locator from Stage 1

    Returns:
        Tuple of (success, message, settings_node_name)
    """
    if not cmds.objExists(locator_1):
        return False, "Locator_1 not found.", None

    # Get target control
    if not cmds.attributeQuery("targetControl", node=locator_1, exists=True):
        return False, "Locator_1 is not a valid pivot locator (missing targetControl).", None

    control = cmds.getAttr(f"{locator_1}.targetControl")
    if not cmds.objExists(control):
        return False, f"Target control '{control}' not found.", None

    # Check if already complete
    if cmds.attributeQuery("setupComplete", node=locator_1, exists=True):
        if cmds.getAttr(f"{locator_1}.setupComplete"):
            return False, "Setup already complete for this locator.", None

    prefix = _sanitize_name(control)

    # Get control's current world transform
    ctrl_translate, ctrl_rotate = _get_world_xform(control)

    # =========================================================================
    # Create locator_2 (the DRIVER) at control position
    # =========================================================================
    locator_2 = cmds.spaceLocator(name=f"{prefix}{LOCATOR_2_SUFFIX}")[0]

    # Match to control position
    _set_world_xform(locator_2, ctrl_translate, ctrl_rotate)

    # Style locator_2 (green - indicates driver)
    loc2_shape = cmds.listRelatives(locator_2, shapes=True)[0]
    cmds.setAttr(f"{loc2_shape}.overrideEnabled", 1)
    cmds.setAttr(f"{loc2_shape}.overrideRGBColors", 1)
    cmds.setAttr(f"{loc2_shape}.overrideColorR", 0.3)
    cmds.setAttr(f"{loc2_shape}.overrideColorG", 1.0)
    cmds.setAttr(f"{loc2_shape}.overrideColorB", 0.3)
    cmds.setAttr(f"{loc2_shape}.localScaleX", 0.3)
    cmds.setAttr(f"{loc2_shape}.localScaleY", 0.3)
    cmds.setAttr(f"{loc2_shape}.localScaleZ", 0.3)

    # =========================================================================
    # Parent locator_2 under locator_1
    # =========================================================================
    cmds.parent(locator_2, locator_1)

    # =========================================================================
    # Create null_GRP
    # =========================================================================
    null_grp = cmds.group(empty=True, name=f"{prefix}{NULL_GRP_SUFFIX}")

    # =========================================================================
    # Match null_GRP to locator_2 (which is at control position)
    # =========================================================================
    _match_transform(null_grp, locator_2)

    # =========================================================================
    # Parent locator_1 under null_GRP
    # =========================================================================
    cmds.parent(locator_1, null_grp)

    # =========================================================================
    # Create parentConstraint: locator_2 → control (maintainOffset=ON)
    # =========================================================================
    constraint_name = f"{prefix}{CONSTRAINT_SUFFIX}"
    if cmds.objExists(constraint_name):
        cmds.delete(constraint_name)

    constraint = cmds.parentConstraint(
        locator_2, control,
        maintainOffset=True,
        name=constraint_name
    )[0]

    # =========================================================================
    # Create settings node
    # =========================================================================
    settings_node = cmds.createNode("transform", name=f"{prefix}{SETTINGS_SUFFIX}")
    cmds.setAttr(f"{settings_node}.visibility", 0)

    # Store references
    _add_string_attr(settings_node, "targetControl", control)
    _add_string_attr(settings_node, "nullGrp", null_grp)
    _add_string_attr(settings_node, "locator1", locator_1)
    _add_string_attr(settings_node, "locator2", locator_2)
    _add_string_attr(settings_node, "constraintName", constraint)
    _add_bool_attr(settings_node, "isActive", True)

    # Parent settings under null_grp
    cmds.parent(settings_node, null_grp)

    # Mark locator_1 setup as complete
    cmds.setAttr(f"{locator_1}.setupComplete", True)

    # Update locator_1 color to indicate active (green)
    shapes = cmds.listRelatives(locator_1, shapes=True) or []
    for shape in shapes:
        if cmds.nodeType(shape) == "locator":
            cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["success"][0])
            cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["success"][1])
            cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["success"][2])

    # Set up auto-key for rotation changes
    setup_auto_key(settings_node)

    # Select locator_1 so user can start using it
    cmds.select(locator_1)

    return True, f"Setup complete! Rotate locator_1 to orbit '{control}' around pivot. Auto-key enabled.", settings_node


# =============================================================================
# TOGGLE ON (Reactivate)
# =============================================================================

def toggle_on(settings_node: str) -> Tuple[bool, str]:
    """
    Reactivate the temp pivot system.

    Process:
    1. Get control's current world transform
    2. Match null_GRP to control (realigns rig)
    3. Match locator_1 rotation to null_GRP (reset relative rotation)
    4. Recreate parentConstraint: locator_2 → control (maintainOffset)
    5. Show visibility

    Args:
        settings_node: The settings node for this rig

    Returns:
        Tuple of (success, message)
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    if is_rig_active(settings_node):
        return False, "Rig is already active."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]
    null_grp = nodes["null_grp"]
    locator_1 = nodes["locator_1"]
    locator_2 = nodes["locator_2"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."
    if not null_grp or not cmds.objExists(null_grp):
        return False, "Null_GRP not found."
    if not locator_1 or not cmds.objExists(locator_1):
        return False, "Locator_1 (pivot) not found."
    if not locator_2 or not cmds.objExists(locator_2):
        return False, "Locator_2 (driver) not found."

    # =========================================================================
    # Get control's current world transform (translation and rotation only)
    # =========================================================================
    ctrl_translate, ctrl_rotate = _get_world_xform(control)

    # =========================================================================
    # Match null_GRP to control position and rotation (NOT scale)
    # Using euler-based xform to avoid transferring scale from control
    # =========================================================================
    _set_world_xform(null_grp, ctrl_translate, ctrl_rotate)

    # =========================================================================
    # Match locator_1 rotation to null_GRP (reset relative rotation)
    # Uses matrix-based rotation matching for accuracy
    # =========================================================================
    _match_rotation(locator_1, null_grp)

    # =========================================================================
    # Recreate parentConstraint: locator_2 → control
    # =========================================================================
    prefix = _sanitize_name(control)
    constraint_name = f"{prefix}{CONSTRAINT_SUFFIX}"

    if cmds.objExists(constraint_name):
        cmds.delete(constraint_name)

    constraint = cmds.parentConstraint(
        locator_2, control,
        maintainOffset=True,
        name=constraint_name
    )[0]

    # Update settings
    cmds.setAttr(f"{settings_node}.constraintName", constraint, type="string")
    cmds.setAttr(f"{settings_node}.isActive", True)

    # =========================================================================
    # Show visibility
    # =========================================================================
    cmds.setAttr(f"{null_grp}.visibility", 1)

    # Update locator_1 color to active (green)
    shapes = cmds.listRelatives(locator_1, shapes=True) or []
    for shape in shapes:
        if cmds.nodeType(shape) == "locator":
            cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["success"][0])
            cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["success"][1])
            cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["success"][2])

    # Set up auto-key for rotation changes
    setup_auto_key(settings_node)

    # Select locator_1
    cmds.select(locator_1)

    return True, f"Pivot ON. Rotate locator_1 to orbit '{control}'. Auto-key enabled."


# =============================================================================
# TOGGLE OFF (Deactivate)
# =============================================================================

def toggle_off(settings_node: str) -> Tuple[bool, str]:
    """
    Deactivate the temp pivot system.

    Process:
    1. Clean up auto-key scriptJobs
    2. Delete the constraint
    3. Hide visibility
    4. Control stays in place (user should have keyed it)

    Args:
        settings_node: The settings node for this rig

    Returns:
        Tuple of (success, message)
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    if not is_rig_active(settings_node):
        return False, "Rig is not active."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]
    constraint = nodes["constraint"]
    null_grp = nodes["null_grp"]
    locator_1 = nodes["locator_1"]

    # =========================================================================
    # Clean up auto-key scriptJobs
    # =========================================================================
    cleanup_auto_key(settings_node)

    # =========================================================================
    # Delete the constraint
    # =========================================================================
    if constraint and cmds.objExists(constraint):
        cmds.delete(constraint)

    # Also clean any other constraints from this tool
    if control and cmds.objExists(control):
        constraints = cmds.listRelatives(control, type="parentConstraint") or []
        for c in constraints:
            if CONSTRAINT_SUFFIX in c or TOOL_PREFIX in c:
                cmds.delete(c)

    # Clear constraint reference and set inactive
    cmds.setAttr(f"{settings_node}.constraintName", "", type="string")
    cmds.setAttr(f"{settings_node}.isActive", False)

    # =========================================================================
    # Hide visibility
    # =========================================================================
    if null_grp and cmds.objExists(null_grp):
        cmds.setAttr(f"{null_grp}.visibility", 0)

    # Update locator_1 color to inactive (orange)
    if locator_1 and cmds.objExists(locator_1):
        shapes = cmds.listRelatives(locator_1, shapes=True) or []
        for shape in shapes:
            if cmds.nodeType(shape) == "locator":
                cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["stage1"][0])
                cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["stage1"][1])
                cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["stage1"][2])

    return True, f"Pivot OFF. '{control}' stays in place. Key if needed."


# =============================================================================
# TOGGLE (Smart)
# =============================================================================

def toggle_pivot(settings_node: str) -> Tuple[bool, str, bool]:
    """Smart toggle - ON if OFF, OFF if ON."""
    if not cmds.objExists(settings_node):
        return False, "Settings node not found.", False

    if is_rig_active(settings_node):
        success, msg = toggle_off(settings_node)
        return success, msg, False
    else:
        success, msg = toggle_on(settings_node)
        return success, msg, True


# =============================================================================
# KEY CONTROL
# =============================================================================

def key_control(settings_node: str) -> Tuple[bool, str]:
    """Set keyframes on the control's translate and rotate."""
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."

    current_time = cmds.currentTime(query=True)
    keyed_attrs = []

    for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
        attr_path = f"{control}.{attr}"
        if cmds.objExists(attr_path):
            if not cmds.getAttr(attr_path, lock=True):
                try:
                    cmds.setKeyframe(control, attribute=attr, time=current_time)
                    keyed_attrs.append(attr)
                except RuntimeError:
                    pass

    if keyed_attrs:
        return True, f"Keyed {len(keyed_attrs)} attrs on '{control}' at frame {current_time}."
    else:
        return False, f"Could not key any attributes on '{control}'."


# =============================================================================
# AUTO-KEY MANAGEMENT
# =============================================================================

def _create_auto_key_callback(settings_node: str):
    """Create a callback function for auto-keying that captures the settings node."""
    def auto_key_callback():
        # Only key if the rig is still active
        if cmds.objExists(settings_node) and is_rig_active(settings_node):
            key_control(settings_node)
    return auto_key_callback


def setup_auto_key(settings_node: str) -> None:
    """Set up scriptJobs to auto-key the control when locator_1 is rotated."""
    global _auto_key_jobs

    # Clean up any existing jobs for this rig
    cleanup_auto_key(settings_node)

    if not cmds.objExists(settings_node):
        return

    nodes = get_rig_nodes(settings_node)
    locator_1 = nodes["locator_1"]

    if not locator_1 or not cmds.objExists(locator_1):
        return

    # Create callback function
    callback = _create_auto_key_callback(settings_node)

    # Set up scriptJobs for rotation attribute changes
    job_ids = []
    for attr in ["rx", "ry", "rz"]:
        attr_path = f"{locator_1}.{attr}"
        if cmds.objExists(attr_path):
            job_id = cmds.scriptJob(
                attributeChange=[attr_path, callback],
                killWithScene=True
            )
            job_ids.append(job_id)

    _auto_key_jobs[settings_node] = job_ids


def cleanup_auto_key(settings_node: str) -> None:
    """Remove auto-key scriptJobs for a rig."""
    global _auto_key_jobs

    if settings_node in _auto_key_jobs:
        for job_id in _auto_key_jobs[settings_node]:
            if cmds.scriptJob(exists=job_id):
                cmds.scriptJob(kill=job_id, force=True)
        del _auto_key_jobs[settings_node]


# =============================================================================
# DELETE RIG
# =============================================================================

def delete_pivot_rig(settings_node: str) -> Tuple[bool, str]:
    """Delete the pivot rig completely."""
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]

    # Clean up auto-key scriptJobs (in case they exist)
    cleanup_auto_key(settings_node)

    # Toggle off first
    if is_rig_active(settings_node):
        toggle_off(settings_node)

    # Delete null_GRP (deletes all children)
    null_grp = nodes["null_grp"]
    if null_grp and cmds.objExists(null_grp):
        cmds.delete(null_grp)

    # Clean up orphaned nodes
    for node_name in [nodes["locator_1"], nodes["locator_2"], settings_node]:
        if node_name and cmds.objExists(node_name):
            cmds.delete(node_name)

    # Remove any remaining constraints
    if control and cmds.objExists(control):
        constraints = cmds.listRelatives(control, type="parentConstraint") or []
        for c in constraints:
            if CONSTRAINT_SUFFIX in c or TOOL_PREFIX in c:
                cmds.delete(c)

    return True, f"Deleted pivot rig for '{control}'."


def delete_pending_pivot(locator_1: str) -> Tuple[bool, str]:
    """Delete a pending (Stage 1) pivot locator."""
    if not cmds.objExists(locator_1):
        return False, "Locator not found."

    control = ""
    if cmds.attributeQuery("targetControl", node=locator_1, exists=True):
        control = cmds.getAttr(f"{locator_1}.targetControl")

    cmds.delete(locator_1)
    return True, f"Deleted pending pivot locator for '{control}'."


# =============================================================================
# UI IMPLEMENTATION
# =============================================================================

def show() -> None:
    """Show the Temp Pivot Tool window."""

    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME)

    window = cmds.window(
        WINDOW_NAME,
        title=WINDOW_TITLE,
        sizeable=True,
        minimizeButton=True,
        maximizeButton=False,
        width=340,
        height=620
    )

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
    # HEADER
    # ==========================================

    header_layout = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=2,
        columnWidth2=(48, 280)
    )

    cmds.canvas(width=44, height=44, rgbValue=UI_COLORS["accent"])

    title_col = cmds.columnLayout(adjustableColumn=True)
    cmds.text(label="Temp Pivot Tool", font="boldLabelFont", align="left", height=22)
    cmds.text(
        label="Two-stage pivot system for animation",
        align="left",
        font="smallPlainLabelFont",
        height=16
    )
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    cmds.text(
        label="1. Select control, click 'Create Pivot Locator'\n"
              "2. Move locator_1 to desired pivot point\n"
              "3. Click 'Complete Setup'\n"
              "4. Rotate locator_1, Key, Toggle OFF when done",
        align="left",
        wordWrap=True,
        height=60,
        font="smallPlainLabelFont"
    )

    cmds.separator(height=12, style="none")

    # ==========================================
    # STATUS
    # ==========================================

    cmds.frameLayout(
        label="Status",
        collapsable=False,
        marginWidth=8,
        marginHeight=8
    )

    status_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=2,
        columnWidth2=(65, 250)
    )

    state_indicator = cmds.button(
        label="READY",
        width=60,
        height=28,
        backgroundColor=UI_COLORS["off_state"],
        enable=False
    )

    selection_text = cmds.text(
        label="No control selected",
        align="left"
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # STAGE 1: CREATE PIVOT
    # ==========================================

    cmds.frameLayout(
        label="Stage 1: Create Pivot Locator",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=6)

    cmds.text(
        label="Select a control, then create the pivot locator.\n"
              "Move it to your desired pivot position:",
        align="left",
        font="smallPlainLabelFont",
        height=32
    )

    create_pivot_btn = cmds.button(
        label="Create Pivot Locator",
        height=36,
        backgroundColor=UI_COLORS["stage1"],
        annotation=TOOLTIPS["create_pivot_btn"]
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # STAGE 2: COMPLETE SETUP
    # ==========================================

    cmds.frameLayout(
        label="Stage 2: Complete Setup",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=6)

    cmds.text(
        label="After positioning locator_1, complete the setup.\n"
              "This creates the driver and constraint:",
        align="left",
        font="smallPlainLabelFont",
        height=32
    )

    complete_setup_btn = cmds.button(
        label="Complete Setup",
        height=36,
        backgroundColor=UI_COLORS["stage2"],
        annotation=TOOLTIPS["complete_setup_btn"]
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # PIVOT CONTROL
    # ==========================================

    cmds.frameLayout(
        label="Pivot Control",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    toggle_btn = cmds.button(
        label="Toggle ON / OFF",
        height=36,
        backgroundColor=UI_COLORS["success"],
        annotation=TOOLTIPS["toggle_btn"]
    )

    key_btn = cmds.button(
        label="Key Control",
        height=32,
        annotation=TOOLTIPS["key_btn"]
    )

    cmds.separator(height=8, style="in")

    cmds.text(label="Selection:", align="left", font="smallBoldLabelFont", height=18)

    select_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(160, 160)
    )

    select_pivot_btn = cmds.button(
        label="Select Pivot (locator_1)",
        height=26,
        annotation=TOOLTIPS["select_pivot_btn"]
    )

    select_control_btn = cmds.button(
        label="Select Control",
        height=26,
        annotation=TOOLTIPS["select_control_btn"]
    )

    cmds.setParent("..")

    delete_btn = cmds.button(
        label="Delete Pivot Rig",
        height=26,
        annotation=TOOLTIPS["delete_btn"]
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # PIVOT RIGS LIST
    # ==========================================

    cmds.frameLayout(
        label="Active Pivot Rigs",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    rig_list = cmds.textScrollList(
        height=100,
        allowMultiSelection=False
    )

    list_btns = cmds.rowLayout(
        numberOfColumns=3,
        adjustableColumn=1,
        columnWidth3=(105, 105, 105)
    )

    toggle_list_btn = cmds.button(
        label="Toggle Selected",
        height=26
    )

    delete_list_btn = cmds.button(
        label="Delete Selected",
        height=26
    )

    refresh_btn = cmds.button(
        label="Refresh List",
        height=26
    )

    cmds.setParent("..")
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # OUTPUT LOG
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
        text="Ready. Select a control and click 'Create Pivot Locator'."
    )

    cmds.setParent("..")

    cmds.separator(height=16, style="none")

    # ==========================================
    # CALLBACKS
    # ==========================================

    # Flag to prevent list refresh during programmatic selection from the list
    # Using a list so we can modify it from nested functions
    _skip_list_refresh = [False]

    def log_message(message: str, msg_type: str = "info") -> None:
        prefix_map = {"warning": "[!] ", "error": "[X] ", "success": "[OK] ", "info": ""}
        prefix = prefix_map.get(msg_type, "")
        current = cmds.scrollField(log_field, query=True, text=True) or ""
        new_text = f"{prefix}{message}"
        if current and not current.startswith("Ready."):
            new_text = f"{current}\n{new_text}"
        cmds.scrollField(log_field, edit=True, text=new_text)
        cmds.scrollField(log_field, edit=True, insertionPosition=len(new_text))

    def refresh_rig_list(preserve_selection: bool = True) -> None:
        """Refresh the rig list, optionally preserving the current selection."""
        # Skip refresh if triggered by our own list selection
        if _skip_list_refresh[0]:
            return

        # Save current selection before clearing
        selected_control = None
        if preserve_selection:
            selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
            if selected_items:
                # Extract control name (without status suffix)
                selected_control = selected_items[0].split(" [")[0]

        cmds.textScrollList(rig_list, edit=True, removeAll=True)
        rigs = get_all_pivot_rigs()
        for settings in sorted(rigs):
            nodes = get_rig_nodes(settings)
            control = nodes["control"] or "?"
            active = is_rig_active(settings)
            status = " [ON]" if active else " [OFF]"
            cmds.textScrollList(rig_list, edit=True, append=f"{control}{status}")

        # Restore selection if we had one
        if selected_control:
            all_items = cmds.textScrollList(rig_list, query=True, allItems=True) or []
            for item in all_items:
                if item.startswith(selected_control + " ["):
                    cmds.textScrollList(rig_list, edit=True, selectItem=item)
                    break

    def update_status() -> None:
        sel = cmds.ls(selection=True, type="transform") or []

        selected_settings = None
        pending_locator = None

        for item in sel:
            # Check for locator_1 (pivot)
            if LOCATOR_1_SUFFIX in item:
                # Check if it's pending or complete
                if cmds.attributeQuery("setupComplete", node=item, exists=True):
                    if cmds.getAttr(f"{item}.setupComplete"):
                        # Complete - find settings
                        prefix = item.replace(LOCATOR_1_SUFFIX, "")
                        possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                        if cmds.objExists(possible_settings):
                            selected_settings = possible_settings
                    else:
                        pending_locator = item
                break
            # Check for locator_2
            if LOCATOR_2_SUFFIX in item:
                prefix = item.replace(LOCATOR_2_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    selected_settings = possible_settings
                break
            # Check if control
            rig = get_rig_for_control(item)
            if rig:
                selected_settings = rig
                break
            # Check for pending pivot
            pending = get_pending_pivot_for_control(item)
            if pending:
                pending_locator = pending

        if selected_settings:
            nodes = get_rig_nodes(selected_settings)
            control = nodes["control"]
            active = is_rig_active(selected_settings)
            cmds.text(selection_text, edit=True, label=f"Control: {control}")
            if active:
                cmds.button(state_indicator, edit=True, label="ON", backgroundColor=UI_COLORS["success"])
            else:
                cmds.button(state_indicator, edit=True, label="OFF", backgroundColor=UI_COLORS["stage1"])
        elif pending_locator:
            if cmds.attributeQuery("targetControl", node=pending_locator, exists=True):
                control = cmds.getAttr(f"{pending_locator}.targetControl")
                cmds.text(selection_text, edit=True, label=f"Pending: {control}")
            cmds.button(state_indicator, edit=True, label="STAGE1", backgroundColor=UI_COLORS["stage1"])
        elif sel:
            cmds.text(selection_text, edit=True, label=f"Selected: {sel[0]}")
            cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])
        else:
            cmds.text(selection_text, edit=True, label="No control selected")
            cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])

    def get_current_context():
        """Get current rig settings or pending locator."""
        sel = cmds.ls(selection=True, type="transform") or []

        for item in sel:
            if LOCATOR_1_SUFFIX in item:
                if cmds.attributeQuery("setupComplete", node=item, exists=True):
                    if cmds.getAttr(f"{item}.setupComplete"):
                        prefix = item.replace(LOCATOR_1_SUFFIX, "")
                        possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                        if cmds.objExists(possible_settings):
                            return ("rig", possible_settings)
                    else:
                        return ("pending", item)
            if LOCATOR_2_SUFFIX in item:
                prefix = item.replace(LOCATOR_2_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    return ("rig", possible_settings)
            rig = get_rig_for_control(item)
            if rig:
                return ("rig", rig)
            pending = get_pending_pivot_for_control(item)
            if pending:
                return ("pending", pending)

        return (None, None)

    # Button callbacks

    def on_create_pivot(*args):
        sel = cmds.ls(selection=True, type="transform") or []
        controls = [s for s in sel if TOOL_PREFIX not in s]

        if not controls:
            log_message("Select a control first.", "warning")
            return

        control = controls[0]
        success, msg, loc = create_pivot_locator(control)
        log_message(msg, "success" if success else "warning")
        refresh_rig_list()
        update_status()

    def on_complete_setup(*args):
        ctx_type, ctx_node = get_current_context()
        locator_to_select = None

        if ctx_type == "pending":
            success, msg, settings = complete_setup(ctx_node)
            log_message(msg, "success" if success else "error")
            if success and settings:
                nodes = get_rig_nodes(settings)
                locator_to_select = nodes["locator_1"]
        elif ctx_type == "rig":
            log_message("Setup already complete. Use Toggle to activate.", "warning")
        else:
            # Try to find pending locator for selected control
            sel = cmds.ls(selection=True, type="transform") or []
            for item in sel:
                pending = get_pending_pivot_for_control(item)
                if pending:
                    success, msg, settings = complete_setup(pending)
                    log_message(msg, "success" if success else "error")
                    if success and settings:
                        nodes = get_rig_nodes(settings)
                        locator_to_select = nodes["locator_1"]
                    refresh_rig_list()
                    update_status()
                    # Ensure locator_1 is selected after UI updates
                    if locator_to_select and cmds.objExists(locator_to_select):
                        cmds.evalDeferred(lambda loc=locator_to_select: cmds.select(loc))
                    return

            log_message("No pending pivot locator found. Create one first.", "warning")

        refresh_rig_list()
        update_status()
        # Ensure locator_1 is selected after UI updates
        if locator_to_select and cmds.objExists(locator_to_select):
            cmds.evalDeferred(lambda loc=locator_to_select: cmds.select(loc))

    def on_toggle(*args):
        ctx_type, ctx_node = get_current_context()

        if ctx_type == "rig":
            success, msg, is_active = toggle_pivot(ctx_node)
            log_message(msg, "success" if success else "error")
            if is_active:
                cmds.button(toggle_btn, edit=True, label="Toggle OFF", backgroundColor=UI_COLORS["success"])
            else:
                cmds.button(toggle_btn, edit=True, label="Toggle ON", backgroundColor=UI_COLORS["stage1"])
        elif ctx_type == "pending":
            log_message("Complete setup first before toggling.", "warning")
        else:
            log_message("No pivot rig found. Create and complete setup first.", "warning")

        refresh_rig_list()
        update_status()

    def on_key(*args):
        ctx_type, ctx_node = get_current_context()
        if ctx_type == "rig":
            success, msg = key_control(ctx_node)
            log_message(msg, "success" if success else "error")
        else:
            log_message("No active pivot rig found.", "warning")

    def on_delete(*args):
        ctx_type, ctx_node = get_current_context()
        if ctx_type == "rig":
            success, msg = delete_pivot_rig(ctx_node)
            log_message(msg, "success" if success else "error")
        elif ctx_type == "pending":
            success, msg = delete_pending_pivot(ctx_node)
            log_message(msg, "success" if success else "error")
        else:
            log_message("No pivot rig found.", "warning")
        refresh_rig_list()
        update_status()

    def on_select_pivot(*args):
        ctx_type, ctx_node = get_current_context()
        if ctx_type == "rig":
            nodes = get_rig_nodes(ctx_node)
            loc1 = nodes["locator_1"]
            if loc1 and cmds.objExists(loc1):
                cmds.select(loc1)
                log_message(f"Selected: {loc1}", "info")
        elif ctx_type == "pending":
            cmds.select(ctx_node)
            log_message(f"Selected: {ctx_node}", "info")
        else:
            log_message("No pivot locator found.", "warning")

    def on_select_control(*args):
        ctx_type, ctx_node = get_current_context()
        if ctx_type == "rig":
            nodes = get_rig_nodes(ctx_node)
            control = nodes["control"]
            if control and cmds.objExists(control):
                cmds.select(control)
                log_message(f"Selected: {control}", "info")
        elif ctx_type == "pending":
            if cmds.attributeQuery("targetControl", node=ctx_node, exists=True):
                control = cmds.getAttr(f"{ctx_node}.targetControl")
                if cmds.objExists(control):
                    cmds.select(control)
                    log_message(f"Selected: {control}", "info")
        else:
            log_message("No control found.", "warning")

    def on_list_select(*args):
        """Handle selection in the rig list - select the locator in viewport."""
        selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
        if selected_items:
            control_name = selected_items[0].split(" [")[0]
            settings = get_rig_for_control(control_name)
            if settings:
                nodes = get_rig_nodes(settings)
                loc1 = nodes["locator_1"]
                if loc1 and cmds.objExists(loc1):
                    # Set flag to prevent refresh from wiping out our list selection
                    _skip_list_refresh[0] = True
                    try:
                        cmds.select(loc1)
                    finally:
                        # Use evalDeferred to reset flag after Maya processes the selection
                        cmds.evalDeferred(lambda: _skip_list_refresh.__setitem__(0, False))
        update_status()

    def on_list_toggle(*args):
        selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
        if not selected_items:
            log_message("Select a rig from the list.", "warning")
            return
        control_name = selected_items[0].split(" [")[0]
        settings = get_rig_for_control(control_name)
        if settings:
            success, msg, is_active = toggle_pivot(settings)
            log_message(msg, "success" if success else "error")
            refresh_rig_list()
            update_status()

    def on_list_delete(*args):
        """Delete the rig selected in the list."""
        selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
        if not selected_items:
            log_message("Select a rig from the list to delete.", "warning")
            return
        control_name = selected_items[0].split(" [")[0]
        settings = get_rig_for_control(control_name)
        if settings:
            success, msg = delete_pivot_rig(settings)
            log_message(msg, "success" if success else "error")
            refresh_rig_list(preserve_selection=False)
            update_status()

    # Connect callbacks

    cmds.button(create_pivot_btn, edit=True, command=on_create_pivot)
    cmds.button(complete_setup_btn, edit=True, command=on_complete_setup)
    cmds.button(toggle_btn, edit=True, command=on_toggle)
    cmds.button(key_btn, edit=True, command=on_key)
    cmds.button(delete_btn, edit=True, command=on_delete)
    cmds.button(select_pivot_btn, edit=True, command=on_select_pivot)
    cmds.button(select_control_btn, edit=True, command=on_select_control)
    cmds.button(toggle_list_btn, edit=True, command=on_list_toggle)
    cmds.button(delete_list_btn, edit=True, command=on_list_delete)
    cmds.button(refresh_btn, edit=True, command=lambda *_: refresh_rig_list())

    cmds.textScrollList(rig_list, edit=True, selectCommand=on_list_select)
    cmds.textScrollList(rig_list, edit=True, doubleClickCommand=on_list_toggle)

    cmds.scriptJob(event=["SelectionChanged", update_status], parent=window)
    cmds.scriptJob(event=["SelectionChanged", refresh_rig_list], parent=window)

    # Initialize

    refresh_rig_list()
    update_status()

    cmds.showWindow(window)
    log_message("Ready. Select a control and click 'Create Pivot Locator'.", "info")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    show()
