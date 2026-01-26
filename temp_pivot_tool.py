"""
Temp Pivot Tool for Autodesk Maya

A temporary pivot system for animation that allows you to rotate objects
around an arbitrary point in space.

Hierarchy Structure:
    null_GRP (root - aligned to object position)
      └ locator_2 (PIVOT - user moves this to set pivot point)
          └ locator_1 (DRIVER - aligned with object, constrains it)

Workflow:
1. Select a control (e.g., IK foot)
2. Click SETUP - creates the pivot rig at the control's position
3. Move locator_2 to where you want the pivot point
4. Click TOGGLE ON - locator_1 constrains the control
5. Rotate locator_2 - the control orbits around the pivot point
6. Click KEY to set keyframes on the control
7. Click TOGGLE OFF - constraint deleted, rig hidden, control stays in place
8. Move to new frame, click TOGGLE ON again - rig realigns to control
9. Repeat as needed

Features:
- Reference-safe (never reparents the control)
- Visibility toggles with ON/OFF state
- Rig realigns to control on re-toggle
- Clean constraint management

Author: David Shepstone
License: MIT
Version: 4.0.0
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
TOOL_PREFIX = "TMP"  # Temp pivot

# Node naming convention (clear names as per user spec)
NULL_GRP_SUFFIX = f"_{TOOL_PREFIX}_null_GRP"
LOCATOR_2_SUFFIX = f"_{TOOL_PREFIX}_locator_2"  # Pivot point
LOCATOR_1_SUFFIX = f"_{TOOL_PREFIX}_locator_1"  # Driver
SETTINGS_SUFFIX = f"_{TOOL_PREFIX}_settings"
CONSTRAINT_SUFFIX = f"_{TOOL_PREFIX}_parentConstraint"

# UI Colors
UI_COLORS = {
    "accent": (0.36, 0.68, 0.93),
    "success": (0.20, 0.75, 0.45),
    "warning": (0.95, 0.77, 0.26),
    "error": (0.95, 0.35, 0.35),
    "on_state": (0.20, 0.75, 0.45),
    "off_state": (0.45, 0.45, 0.48),
    "pivot_color": (0.95, 0.65, 0.25),
}

# Tooltips
TOOLTIPS = {
    "setup_btn": (
        "Create a temp pivot rig for the selected control.\n"
        "1. Creates locator_1 aligned with control\n"
        "2. Creates locator_2 at same position (pivot point)\n"
        "3. Parents locator_1 under locator_2\n"
        "4. Creates null_GRP aligned to locator_1\n"
        "5. Parents hierarchy under null_GRP\n\n"
        "After setup, move locator_2 to set your pivot point."
    ),
    "toggle_btn": (
        "Toggle the temp pivot ON/OFF.\n\n"
        "ON: Creates parentConstraint from locator_1 to control.\n"
        "    Rotating locator_2 orbits the control around it.\n\n"
        "OFF: Deletes constraint, hides rig.\n"
        "     Control stays in place (key it first!).\n\n"
        "Re-toggle ON: Realigns null_GRP to control position,\n"
        "              then recreates constraint."
    ),
    "key_btn": (
        "Set keyframes on the control's translate and rotate.\n"
        "Use this to 'commit' the pose while pivot is active."
    ),
    "delete_btn": (
        "Delete the temp pivot rig completely.\n"
        "Removes all rig nodes and cleans up constraints."
    ),
    "select_pivot_btn": (
        "Select locator_2 (the pivot locator).\n"
        "Move/rotate this to control the pivot point."
    ),
    "select_control_btn": (
        "Select the original control.\n"
        "Useful for keying or checking values."
    ),
    "refresh_btn": "Refresh the list of pivot rigs in the scene.",
}


# -----------------------------
# Utility Functions
# -----------------------------

def _sanitize_name(name: str) -> str:
    """Create a safe prefix from a control name."""
    # Remove namespace, replace invalid chars
    safe = name.split(":")[-1]  # Remove namespace
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


def _match_transform(source: str, target: str) -> None:
    """Match source node's world transform to target node."""
    translate, rotate = _get_world_xform(target)
    _set_world_xform(source, translate, rotate)


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


def get_rig_nodes(settings_node: str) -> Dict[str, Optional[str]]:
    """Get all rig node names from a settings node."""
    result = {
        "settings": settings_node,
        "null_grp": None,
        "locator_2": None,  # Pivot
        "locator_1": None,  # Driver
        "control": None,
        "constraint": None,
    }

    if not cmds.objExists(settings_node):
        return result

    if cmds.attributeQuery("nullGrp", node=settings_node, exists=True):
        result["null_grp"] = cmds.getAttr(f"{settings_node}.nullGrp") or None
    if cmds.attributeQuery("locator2", node=settings_node, exists=True):
        result["locator_2"] = cmds.getAttr(f"{settings_node}.locator2") or None
    if cmds.attributeQuery("locator1", node=settings_node, exists=True):
        result["locator_1"] = cmds.getAttr(f"{settings_node}.locator1") or None
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


# -----------------------------
# Rig Setup
# -----------------------------

def setup_pivot_rig(control: str) -> Tuple[bool, str, Optional[str]]:
    """
    Create a temp pivot rig for the given control.

    Steps (as per user spec):
    1. Create locator_1 aligned with selected object (control)
    2. Create locator_2 at same position (this is the pivot point)
    3. Parent locator_1 under locator_2
    4. Deselect all
    5. Create empty null_GRP at world center
    6. Align null_GRP to locator_1's position
    7. Parent the hierarchy under null_GRP

    Result hierarchy:
        null_GRP (at control position)
          └ locator_2 (pivot - user moves this)
              └ locator_1 (driver - aligned with control)

    Args:
        control: The control to create a pivot rig for

    Returns:
        Tuple of (success, message, settings_node_name)
    """
    if not cmds.objExists(control):
        return False, f"Control '{control}' not found.", None

    # Check if rig already exists for this control
    existing = get_rig_for_control(control)
    if existing:
        return False, f"Pivot rig already exists for '{control}'.", existing

    # Create safe prefix from control name
    prefix = _sanitize_name(control)

    # Get control's current world transform
    ctrl_translate, ctrl_rotate = _get_world_xform(control)

    # =============================================
    # STEP 1: Create locator_1 aligned with control
    # =============================================
    locator_1 = cmds.spaceLocator(name=f"{prefix}{LOCATOR_1_SUFFIX}")[0]
    _set_world_xform(locator_1, ctrl_translate, ctrl_rotate)

    # Style locator_1 (green - indicates it drives the control)
    loc1_shape = cmds.listRelatives(locator_1, shapes=True)[0]
    cmds.setAttr(f"{loc1_shape}.overrideEnabled", 1)
    cmds.setAttr(f"{loc1_shape}.overrideRGBColors", 1)
    cmds.setAttr(f"{loc1_shape}.overrideColorR", 0.3)
    cmds.setAttr(f"{loc1_shape}.overrideColorG", 1.0)
    cmds.setAttr(f"{loc1_shape}.overrideColorB", 0.3)
    cmds.setAttr(f"{loc1_shape}.localScaleX", 0.3)
    cmds.setAttr(f"{loc1_shape}.localScaleY", 0.3)
    cmds.setAttr(f"{loc1_shape}.localScaleZ", 0.3)

    # =============================================
    # STEP 2: Create locator_2 at same position (pivot point)
    # =============================================
    locator_2 = cmds.spaceLocator(name=f"{prefix}{LOCATOR_2_SUFFIX}")[0]
    _set_world_xform(locator_2, ctrl_translate, ctrl_rotate)

    # Style locator_2 (orange - user interacts with this as pivot)
    loc2_shape = cmds.listRelatives(locator_2, shapes=True)[0]
    cmds.setAttr(f"{loc2_shape}.overrideEnabled", 1)
    cmds.setAttr(f"{loc2_shape}.overrideRGBColors", 1)
    cmds.setAttr(f"{loc2_shape}.overrideColorR", UI_COLORS["pivot_color"][0])
    cmds.setAttr(f"{loc2_shape}.overrideColorG", UI_COLORS["pivot_color"][1])
    cmds.setAttr(f"{loc2_shape}.overrideColorB", UI_COLORS["pivot_color"][2])
    cmds.setAttr(f"{loc2_shape}.localScaleX", 0.5)
    cmds.setAttr(f"{loc2_shape}.localScaleY", 0.5)
    cmds.setAttr(f"{loc2_shape}.localScaleZ", 0.5)

    # Add visual rings to locator_2 (pivot indicator)
    for axis, color, normal in [
        ("X", (1, 0.3, 0.3), (1, 0, 0)),
        ("Y", (0.3, 1, 0.3), (0, 1, 0)),
        ("Z", (0.3, 0.5, 1), (0, 0, 1))
    ]:
        circle = cmds.circle(
            name=f"{prefix}{LOCATOR_2_SUFFIX}_ring{axis}",
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
        cmds.parent(circle_shape, locator_2, shape=True, relative=True)
        cmds.delete(circle)

    # =============================================
    # STEP 3: Parent locator_1 under locator_2
    # =============================================
    cmds.parent(locator_1, locator_2)

    # =============================================
    # STEP 4: Deselect all
    # =============================================
    cmds.select(clear=True)

    # =============================================
    # STEP 5: Create empty null_GRP at world center
    # =============================================
    null_grp = cmds.createNode("transform", name=f"{prefix}{NULL_GRP_SUFFIX}")

    # =============================================
    # STEP 6: Align null_GRP to locator_1's position
    # =============================================
    _match_transform(null_grp, locator_1)

    # =============================================
    # STEP 7: Parent the hierarchy under null_GRP
    # =============================================
    cmds.parent(locator_2, null_grp)

    # =============================================
    # Create settings node to store rig data
    # =============================================
    settings_node = cmds.createNode("transform", name=f"{prefix}{SETTINGS_SUFFIX}")
    cmds.setAttr(f"{settings_node}.visibility", 0)  # Hide settings node

    # Store references in settings node
    _add_string_attr(settings_node, "targetControl", control)
    _add_string_attr(settings_node, "nullGrp", null_grp)
    _add_string_attr(settings_node, "locator2", locator_2)  # Pivot
    _add_string_attr(settings_node, "locator1", locator_1)  # Driver
    _add_string_attr(settings_node, "constraintName", "")
    _add_bool_attr(settings_node, "isActive", False)

    # Store initial world transform for potential use
    _add_string_attr(settings_node, "storedTranslate", json.dumps(ctrl_translate))
    _add_string_attr(settings_node, "storedRotate", json.dumps(ctrl_rotate))

    # Parent settings under null_grp for organization
    cmds.parent(settings_node, null_grp)

    # Select locator_2 so user can move the pivot point
    cmds.select(locator_2)

    return True, f"Created pivot rig for '{control}'. Move locator_2 to set pivot point, then Toggle ON.", settings_node


# -----------------------------
# Toggle ON (Attach)
# -----------------------------

def toggle_on(settings_node: str) -> Tuple[bool, str]:
    """
    Attach the pivot rig to the control via constraint.

    Process (as per user spec):
    1. Get xform coordinates from control and store in node
    2. Apply those xform coordinates to null_GRP (realigns rig to control)
    3. Create parentConstraint from locator_1 to control (maintainOffset=True)
    4. Store constraint name
    5. Set isActive = True
    6. Turn on visibility

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
    if not locator_1 or not cmds.objExists(locator_1):
        return False, "Locator_1 (driver) not found."
    if not null_grp or not cmds.objExists(null_grp):
        return False, "Null_GRP not found."

    # =============================================
    # STEP 1: Get xform from control and store
    # =============================================
    ctrl_translate, ctrl_rotate = _get_world_xform(control)
    cmds.setAttr(f"{settings_node}.storedTranslate", json.dumps(ctrl_translate), type="string")
    cmds.setAttr(f"{settings_node}.storedRotate", json.dumps(ctrl_rotate), type="string")

    # =============================================
    # STEP 2: Apply xform to null_GRP to realign rig
    # =============================================
    # Get current locator_1 world position
    loc1_translate, loc1_rotate = _get_world_xform(locator_1)

    # Calculate offset from locator_1 to control (how much we need to move)
    offset = [
        ctrl_translate[0] - loc1_translate[0],
        ctrl_translate[1] - loc1_translate[1],
        ctrl_translate[2] - loc1_translate[2]
    ]

    # Get current null_grp position
    null_translate, null_rotate = _get_world_xform(null_grp)

    # Move null_GRP by offset (this moves whole rig so locator_1 aligns with control)
    new_null_translate = [
        null_translate[0] + offset[0],
        null_translate[1] + offset[1],
        null_translate[2] + offset[2]
    ]
    cmds.xform(null_grp, ws=True, t=new_null_translate)

    # Also match rotation to control
    cmds.xform(null_grp, ws=True, ro=ctrl_rotate)

    # =============================================
    # STEP 3: Create parentConstraint from locator_1 to control
    # =============================================
    constraint_name = f"{_sanitize_name(control)}{CONSTRAINT_SUFFIX}"

    # Delete any existing constraint with this name
    if cmds.objExists(constraint_name):
        cmds.delete(constraint_name)

    constraint = cmds.parentConstraint(
        locator_1, control,
        maintainOffset=True,
        name=constraint_name
    )[0]

    # =============================================
    # STEP 4: Store constraint name and set active
    # =============================================
    cmds.setAttr(f"{settings_node}.constraintName", constraint, type="string")
    cmds.setAttr(f"{settings_node}.isActive", True)

    # =============================================
    # STEP 5: Turn on visibility
    # =============================================
    if cmds.objExists(null_grp):
        cmds.setAttr(f"{null_grp}.visibility", 1)

    # Update locator_2 color to indicate active (green tint)
    if locator_2 and cmds.objExists(locator_2):
        shapes = cmds.listRelatives(locator_2, shapes=True) or []
        for shape in shapes:
            if cmds.nodeType(shape) == "locator":
                cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["success"][0])
                cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["success"][1])
                cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["success"][2])

    return True, f"Pivot ON. Rotate locator_2 to orbit '{control}' around pivot point."


# -----------------------------
# Toggle OFF (Detach)
# -----------------------------

def toggle_off(settings_node: str) -> Tuple[bool, str]:
    """
    Detach the pivot rig from the control.

    Process (as per user spec):
    1. Delete the constraint from locator_1 to control
    2. Control stays in place (user should have keyed it)
    3. Set isActive = False
    4. Turn off visibility (hide rig)

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
    locator_2 = nodes["locator_2"]

    # =============================================
    # STEP 1: Delete the constraint
    # =============================================
    if constraint and cmds.objExists(constraint):
        cmds.delete(constraint)

    # Also check for any other constraints from our tool on the control
    if control and cmds.objExists(control):
        constraints = cmds.listRelatives(control, type="parentConstraint") or []
        for c in constraints:
            if CONSTRAINT_SUFFIX in c or TOOL_PREFIX in c:
                cmds.delete(c)

    # =============================================
    # STEP 2: Clear constraint reference and set inactive
    # =============================================
    cmds.setAttr(f"{settings_node}.constraintName", "", type="string")
    cmds.setAttr(f"{settings_node}.isActive", False)

    # =============================================
    # STEP 3: Turn off visibility (hide rig)
    # =============================================
    if null_grp and cmds.objExists(null_grp):
        cmds.setAttr(f"{null_grp}.visibility", 0)

    # Update locator_2 color to indicate inactive (orange)
    if locator_2 and cmds.objExists(locator_2):
        shapes = cmds.listRelatives(locator_2, shapes=True) or []
        for shape in shapes:
            if cmds.nodeType(shape) == "locator":
                cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["pivot_color"][0])
                cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["pivot_color"][1])
                cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["pivot_color"][2])

    return True, f"Pivot OFF. Constraint deleted, rig hidden. '{control}' stays in place."


# -----------------------------
# Toggle (Smart)
# -----------------------------

def toggle_pivot(settings_node: str) -> Tuple[bool, str, bool]:
    """
    Smart toggle - turns rig ON if OFF, or OFF if ON.

    Returns:
        Tuple of (success, message, is_now_active)
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found.", False

    if is_rig_active(settings_node):
        success, msg = toggle_off(settings_node)
        return success, msg, False
    else:
        success, msg = toggle_on(settings_node)
        return success, msg, True


# -----------------------------
# Key Control
# -----------------------------

def key_control(settings_node: str) -> Tuple[bool, str]:
    """
    Set keyframes on the control's translate and rotate.
    This is how the animator 'commits' the pose while pivot is active.

    Args:
        settings_node: The settings node for this rig

    Returns:
        Tuple of (success, message)
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."

    current_time = cmds.currentTime(query=True)
    keyed_attrs = []

    # Key translate and rotate
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
        return True, f"Keyed {len(keyed_attrs)} attributes on '{control}' at frame {current_time}."
    else:
        return False, f"Could not key any attributes on '{control}'."


# -----------------------------
# Delete Rig
# -----------------------------

def delete_pivot_rig(settings_node: str) -> Tuple[bool, str]:
    """
    Delete the pivot rig completely.

    Process:
    1. Toggle OFF first (removes constraint)
    2. Delete all rig nodes
    3. Control is left clean

    Args:
        settings_node: The settings node for this rig

    Returns:
        Tuple of (success, message)
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]

    # 1. Toggle off first to clean constraint
    if is_rig_active(settings_node):
        toggle_off(settings_node)

    # 2. Delete all rig nodes
    # Delete null_GRP (this will delete all children including locator_2, locator_1, settings)
    null_grp = nodes["null_grp"]
    if null_grp and cmds.objExists(null_grp):
        cmds.delete(null_grp)

    # Clean up any orphaned nodes (in case hierarchy was broken)
    for node_name in [nodes["locator_2"], nodes["locator_1"], settings_node]:
        if node_name and cmds.objExists(node_name):
            cmds.delete(node_name)

    # Double-check: remove any remaining constraints on control from this tool
    if control and cmds.objExists(control):
        constraints = cmds.listRelatives(control, type="parentConstraint") or []
        for c in constraints:
            if CONSTRAINT_SUFFIX in c or TOOL_PREFIX in c:
                cmds.delete(c)

    return True, f"Deleted pivot rig for '{control}'."


# -----------------------------
# UI Implementation
# -----------------------------

def show() -> None:
    """Show the Temp Pivot Tool window."""

    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME)

    # Window setup
    window = cmds.window(
        WINDOW_NAME,
        title=WINDOW_TITLE,
        sizeable=True,
        minimizeButton=True,
        maximizeButton=False,
        width=340,
        height=580
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
        label="Temporary pivot system for animation",
        align="left",
        font="smallPlainLabelFont",
        height=16
    )
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # Description
    cmds.text(
        label="Select a control, click Setup, move locator_2 (pivot),\n"
              "then Toggle ON. Rotate locator_2 to orbit the control.\n"
              "Key the control, then Toggle OFF when done.",
        align="left",
        wordWrap=True,
        height=50,
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
    # SETUP SECTION
    # ==========================================

    cmds.frameLayout(
        label="Setup",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=6)

    cmds.text(
        label="Select a control and click Setup to create a pivot rig:",
        align="left",
        font="smallPlainLabelFont",
        height=20
    )

    setup_btn = cmds.button(
        label="Setup Pivot Rig",
        height=36,
        backgroundColor=UI_COLORS["accent"],
        annotation=TOOLTIPS["setup_btn"]
    )

    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # CONTROL SECTION
    # ==========================================

    cmds.frameLayout(
        label="Pivot Control",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)

    cmds.text(
        label="After setup, move locator_2 (pivot) then toggle ON.\n"
              "Rotating locator_2 makes the control orbit around it:",
        align="left",
        font="smallPlainLabelFont",
        height=32
    )

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
        label="Select Pivot (locator_2)",
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

    cmds.text(
        label="All pivot rigs in this scene:",
        align="left",
        font="smallBoldLabelFont",
        height=18
    )

    rig_list = cmds.textScrollList(
        height=100,
        allowMultiSelection=False
    )

    list_btns = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(160, 160)
    )

    activate_list_btn = cmds.button(
        label="Toggle Selected",
        height=26
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
        text="Ready. Select a control and click Setup."
    )

    cmds.setParent("..")

    cmds.separator(height=16, style="none")

    # ==========================================
    # CALLBACKS
    # ==========================================

    def log_message(message: str, msg_type: str = "info") -> None:
        """Log a message to the output field."""
        prefix_map = {"warning": "[!] ", "error": "[X] ", "success": "[OK] ", "info": ""}
        prefix = prefix_map.get(msg_type, "")
        current = cmds.scrollField(log_field, query=True, text=True) or ""
        new_text = f"{prefix}{message}"
        if current and not current.startswith("Ready."):
            new_text = f"{current}\n{new_text}"
        cmds.scrollField(log_field, edit=True, text=new_text)
        cmds.scrollField(log_field, edit=True, insertionPosition=len(new_text))

    def refresh_rig_list() -> None:
        """Refresh the list of pivot rigs."""
        cmds.textScrollList(rig_list, edit=True, removeAll=True)
        rigs = get_all_pivot_rigs()
        for settings in sorted(rigs):
            nodes = get_rig_nodes(settings)
            control = nodes["control"] or "?"
            active = is_rig_active(settings)
            status = " [ON]" if active else " [OFF]"
            display = f"{control}{status}"
            cmds.textScrollList(rig_list, edit=True, append=display)

    def update_status() -> None:
        """Update the status display."""
        sel = cmds.ls(selection=True, type="transform") or []

        # Check if selection is a pivot rig node
        selected_settings = None
        for item in sel:
            # Check if this is locator_2 (pivot)
            if LOCATOR_2_SUFFIX in item:
                prefix = item.replace(LOCATOR_2_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    selected_settings = possible_settings
                    break
            # Check if this is locator_1 (driver)
            if LOCATOR_1_SUFFIX in item:
                prefix = item.replace(LOCATOR_1_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    selected_settings = possible_settings
                    break
            # Check if this is the control
            rig = get_rig_for_control(item)
            if rig:
                selected_settings = rig
                break

        if selected_settings:
            nodes = get_rig_nodes(selected_settings)
            control = nodes["control"]
            active = is_rig_active(selected_settings)

            cmds.text(selection_text, edit=True, label=f"Control: {control}")
            if active:
                cmds.button(state_indicator, edit=True, label="ON", backgroundColor=UI_COLORS["success"])
            else:
                cmds.button(state_indicator, edit=True, label="OFF", backgroundColor=UI_COLORS["pivot_color"])
        elif sel:
            cmds.text(selection_text, edit=True, label=f"Selected: {sel[0]}")
            cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])
        else:
            cmds.text(selection_text, edit=True, label="No control selected")
            cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])

    def get_current_rig() -> Optional[str]:
        """Get the settings node for the current selection context."""
        sel = cmds.ls(selection=True, type="transform") or []

        for item in sel:
            # Check if this is locator_2 (pivot)
            if LOCATOR_2_SUFFIX in item:
                prefix = item.replace(LOCATOR_2_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    return possible_settings
            # Check if this is locator_1 (driver)
            if LOCATOR_1_SUFFIX in item:
                prefix = item.replace(LOCATOR_1_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    return possible_settings
            # Check if this is the control
            rig = get_rig_for_control(item)
            if rig:
                return rig

        return None

    # ----- Button Callbacks -----

    def on_setup(*args) -> None:
        """Setup button callback."""
        sel = cmds.ls(selection=True, type="transform") or []

        # Filter out our rig nodes
        controls = [s for s in sel if TOOL_PREFIX not in s]

        if not controls:
            log_message("Please select a control to create a pivot rig for.", "warning")
            return

        control = controls[0]
        success, msg, settings = setup_pivot_rig(control)
        log_message(msg, "success" if success else "error")

        refresh_rig_list()
        update_status()

    def on_toggle(*args) -> None:
        """Toggle button callback."""
        settings = get_current_rig()

        if not settings:
            log_message("No pivot rig found for selection. Setup first.", "warning")
            return

        success, msg, is_active = toggle_pivot(settings)
        log_message(msg, "success" if success else "error")

        # Update toggle button appearance
        if is_active:
            cmds.button(toggle_btn, edit=True, label="Toggle OFF", backgroundColor=UI_COLORS["success"])
        else:
            cmds.button(toggle_btn, edit=True, label="Toggle ON", backgroundColor=UI_COLORS["pivot_color"])

        refresh_rig_list()
        update_status()

    def on_key(*args) -> None:
        """Key button callback."""
        settings = get_current_rig()

        if not settings:
            log_message("No pivot rig found for selection.", "warning")
            return

        success, msg = key_control(settings)
        log_message(msg, "success" if success else "error")

    def on_delete(*args) -> None:
        """Delete button callback."""
        settings = get_current_rig()

        if not settings:
            log_message("No pivot rig found for selection.", "warning")
            return

        success, msg = delete_pivot_rig(settings)
        log_message(msg, "success" if success else "error")

        refresh_rig_list()
        update_status()

    def on_select_pivot(*args) -> None:
        """Select pivot locator (locator_2) callback."""
        settings = get_current_rig()

        if not settings:
            # Try to get from list
            selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
            if selected_items:
                control_name = selected_items[0].split(" [")[0]
                settings = get_rig_for_control(control_name)

        if not settings:
            log_message("No pivot rig found.", "warning")
            return

        nodes = get_rig_nodes(settings)
        locator_2 = nodes["locator_2"]

        if locator_2 and cmds.objExists(locator_2):
            cmds.select(locator_2)
            log_message(f"Selected pivot: {locator_2}", "info")
        else:
            log_message("Pivot locator (locator_2) not found.", "error")

    def on_select_control(*args) -> None:
        """Select control callback."""
        settings = get_current_rig()

        if not settings:
            # Try to get from list
            selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
            if selected_items:
                control_name = selected_items[0].split(" [")[0]
                settings = get_rig_for_control(control_name)

        if not settings:
            log_message("No pivot rig found.", "warning")
            return

        nodes = get_rig_nodes(settings)
        control = nodes["control"]

        if control and cmds.objExists(control):
            cmds.select(control)
            log_message(f"Selected control: {control}", "info")
        else:
            log_message("Control not found.", "error")

    def on_list_select(*args) -> None:
        """List selection callback."""
        selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
        if selected_items:
            control_name = selected_items[0].split(" [")[0]
            settings = get_rig_for_control(control_name)
            if settings:
                nodes = get_rig_nodes(settings)
                locator_2 = nodes["locator_2"]
                if locator_2 and cmds.objExists(locator_2):
                    cmds.select(locator_2)
        update_status()

    def on_list_toggle(*args) -> None:
        """Toggle the rig selected in the list."""
        selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
        if not selected_items:
            log_message("Select a rig from the list first.", "warning")
            return

        control_name = selected_items[0].split(" [")[0]
        settings = get_rig_for_control(control_name)

        if settings:
            success, msg, is_active = toggle_pivot(settings)
            log_message(msg, "success" if success else "error")
            refresh_rig_list()
            update_status()

    # ==========================================
    # CONNECT CALLBACKS
    # ==========================================

    cmds.button(setup_btn, edit=True, command=on_setup)
    cmds.button(toggle_btn, edit=True, command=on_toggle)
    cmds.button(key_btn, edit=True, command=on_key)
    cmds.button(delete_btn, edit=True, command=on_delete)
    cmds.button(select_pivot_btn, edit=True, command=on_select_pivot)
    cmds.button(select_control_btn, edit=True, command=on_select_control)
    cmds.button(activate_list_btn, edit=True, command=on_list_toggle)
    cmds.button(refresh_btn, edit=True, command=lambda *_: refresh_rig_list())

    cmds.textScrollList(rig_list, edit=True, selectCommand=on_list_select)
    cmds.textScrollList(rig_list, edit=True, doubleClickCommand=on_list_toggle)

    # Selection change script job
    cmds.scriptJob(event=["SelectionChanged", update_status], parent=window)
    cmds.scriptJob(event=["SelectionChanged", refresh_rig_list], parent=window)

    # ==========================================
    # INITIALIZE
    # ==========================================

    refresh_rig_list()
    update_status()

    cmds.showWindow(window)
    log_message("Temp Pivot Tool ready. Select a control and click Setup.", "info")


# ==========================================
# ENTRY POINT
# ==========================================

if __name__ == "__main__":
    show()
