"""
Temp Pivot Tool for Autodesk Maya

A non-destructive, reusable temporary pivot system for animation.

TWO-NULL ARCHITECTURE:
  null_group_1: The TEMP PIVOT - user adjusts its pivot position using Maya's "Adjust Pivot" tool
  null_group_2: The POSITION ANCHOR - created on toggle OFF, holds null_group_1 relative to control

Hierarchy when ACTIVE (constraint ON):
    null_group_2 (aligned to control position)
      └ null_group_1 (TEMP PIVOT - user has set custom pivot point)
         └ [parentConstraint] → control

Workflow:
1. Select control, click "Create Pivot Locator" (Stage 1)
2. Tool automatically enters pivot adjust mode - move the pivot to desired position
3. Click "Complete Setup" (Stage 2) - creates constraint to control
4. Rotate null_group_1 - control orbits around the custom pivot point (auto-keys applied)
5. Toggle OFF - creates null_group_2 as anchor, constraint deleted, control free to move
6. Move control to new position
7. Toggle ON - null_group_2 realigns to control, constraint recreated

Features:
- Auto-key: When you transform null_group_1, keyframes are automatically set on the control
- World-matrix alignment: Proper world-space alignment using full transformation matrix
- Constraint validation: Warns if control has existing constraints

Author: David Shepstone
License: MIT
Version: 6.0.0
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import maya.cmds as cmds
import maya.mel as mel

# -----------------------------
# Constants
# -----------------------------

WINDOW_NAME = "tempPivotToolWindow"
WINDOW_TITLE = "Temp Pivot Tool"
TOOL_PREFIX = "TMP"

# Node naming convention (new two-null architecture)
NULL_GRP_1_SUFFIX = f"_{TOOL_PREFIX}_pivot"      # TEMP PIVOT - user adjusts pivot point
NULL_GRP_2_SUFFIX = f"_{TOOL_PREFIX}_anchor"     # POSITION ANCHOR - holds pivot relative to control
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
        "STAGE 1: Create the pivot null (null_group_1).\n\n"
        "1. Creates null_group_1 aligned to the control\n"
        "2. Automatically enters pivot adjust mode with Move tool\n"
        "3. Move the pivot to your desired position\n"
        "4. Then click 'Complete Setup' to finish"
    ),
    "complete_setup_btn": (
        "STAGE 2: Complete the pivot rig setup.\n\n"
        "1. Creates parentConstraint: null_group_1 → control\n"
        "2. After this, rotating null_group_1 will orbit the control\n"
        "   around the custom pivot point you set."
    ),
    "toggle_btn": (
        "Toggle the temp pivot ON/OFF.\n\n"
        "OFF: Creates null_group_2 as anchor,\n"
        "     deletes constraint, control free to move.\n\n"
        "ON: Realigns null_group_2 to control position,\n"
        "    recreates constraint."
    ),
    "key_btn": (
        "Set keyframes on the control's translate and rotate.\n"
        "Note: Keys are set automatically when you transform null_group_1.\n"
        "Use this button for manual keying if needed."
    ),
    "delete_btn": (
        "Delete the temp pivot rig completely.\n"
        "Removes all rig nodes and cleans up constraints."
    ),
    "select_pivot_btn": (
        "Select null_group_1 (the pivot null).\n"
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


def _align_to_target_world_matrix(object_to_align: str, target: str) -> None:
    """
    Align object_to_align to target's world-space transform using world matrix.

    This method uses the full world transformation matrix to bypass rotation
    order issues and gimbal lock problems.

    Based on the MEL alignToFirstFixed() approach.
    """
    # Get target's world position
    pos = cmds.xform(target, q=True, ws=True, t=True)

    # Get target's world matrix (16 values)
    matrix = cmds.xform(target, q=True, ws=True, m=True)

    # Apply the full world matrix to the object
    # This bypasses rotation order issues
    cmds.xform(
        object_to_align, ws=True, m=[
            matrix[0], matrix[1], matrix[2], matrix[3],
            matrix[4], matrix[5], matrix[6], matrix[7],
            matrix[8], matrix[9], matrix[10], matrix[11],
            matrix[12], matrix[13], matrix[14], matrix[15]
        ]
    )


def _match_translation_world(source: str, target: str) -> None:
    """Match only world-space translation using xform."""
    pos = cmds.xform(target, q=True, ws=True, t=True)
    cmds.xform(source, ws=True, t=pos)


def _has_constraints(node: str) -> Tuple[bool, List[str]]:
    """
    Check if a node has any constraints affecting it.

    Returns:
        Tuple of (has_constraints, list_of_constraint_names)
    """
    constraint_types = [
        "parentConstraint", "pointConstraint", "orientConstraint",
        "scaleConstraint", "aimConstraint"
    ]

    found_constraints = []
    for ctype in constraint_types:
        constraints = cmds.listRelatives(node, type=ctype) or []
        found_constraints.extend(constraints)

    # Also check connections to translate/rotate attributes
    for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
        attr_path = f"{node}.{attr}"
        if cmds.objExists(attr_path):
            connections = cmds.listConnections(attr_path, source=True, destination=False, plugs=True) or []
            for conn in connections:
                # Check if connection is from a constraint
                conn_node = conn.split(".")[0]
                node_type = cmds.nodeType(conn_node)
                if "Constraint" in node_type and conn_node not in found_constraints:
                    found_constraints.append(conn_node)

    return len(found_constraints) > 0, found_constraints


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


def _create_visual_null(name: str, color: Tuple[float, float, float], size: float = 1.0) -> str:
    """
    Create a null group with visual circle indicators.

    Args:
        name: Name for the null group
        color: RGB color tuple (0-1 range)
        size: Scale factor for the visual indicators

    Returns:
        The name of the created null group
    """
    # Clean up any existing nodes with conflicting names first
    # This prevents Maya auto-renaming and creating orphaned nodes
    nodes_to_clean = [
        name,
        f"{name}_ringX", f"{name}_ringY", f"{name}_ringZ",
        f"{name}_loc"
    ]
    for node_name in nodes_to_clean:
        if cmds.objExists(node_name):
            try:
                cmds.delete(node_name)
            except Exception:
                pass

    # Create the null group
    null_grp = cmds.group(empty=True, name=name)

    # Add visual circles for each axis
    for axis, axis_color, normal in [
        ("X", (1, 0.3, 0.3), (1, 0, 0)),
        ("Y", (0.3, 1, 0.3), (0, 1, 0)),
        ("Z", (0.3, 0.5, 1), (0, 0, 1))
    ]:
        temp_name = f"{name}_ring{axis}"
        circle = cmds.circle(
            name=temp_name,
            normal=normal,
            radius=0.5 * size,
            degree=3,
            sections=24,
            constructionHistory=False
        )[0]

        # Get the shape from the actual created node (in case Maya renamed it)
        shapes = cmds.listRelatives(circle, shapes=True) or []
        if shapes:
            circle_shape = shapes[0]
            cmds.setAttr(f"{circle_shape}.overrideEnabled", 1)
            cmds.setAttr(f"{circle_shape}.overrideRGBColors", 1)
            cmds.setAttr(f"{circle_shape}.overrideColorR", axis_color[0])
            cmds.setAttr(f"{circle_shape}.overrideColorG", axis_color[1])
            cmds.setAttr(f"{circle_shape}.overrideColorB", axis_color[2])
            cmds.parent(circle_shape, null_grp, shape=True, relative=True)

        # Delete the transform node (use the actual created name)
        if cmds.objExists(circle):
            cmds.delete(circle)

    # Add a center locator shape for selection clarity
    loc_temp_name = f"{name}_loc"
    loc = cmds.spaceLocator(name=loc_temp_name)[0]
    loc_shapes = cmds.listRelatives(loc, shapes=True) or []
    if loc_shapes:
        loc_shape = loc_shapes[0]
        cmds.setAttr(f"{loc_shape}.overrideEnabled", 1)
        cmds.setAttr(f"{loc_shape}.overrideRGBColors", 1)
        cmds.setAttr(f"{loc_shape}.overrideColorR", color[0])
        cmds.setAttr(f"{loc_shape}.overrideColorG", color[1])
        cmds.setAttr(f"{loc_shape}.overrideColorB", color[2])
        cmds.setAttr(f"{loc_shape}.localScaleX", 0.3 * size)
        cmds.setAttr(f"{loc_shape}.localScaleY", 0.3 * size)
        cmds.setAttr(f"{loc_shape}.localScaleZ", 0.3 * size)
        cmds.parent(loc_shape, null_grp, shape=True, relative=True)

    # Delete the locator transform (use the actual created name)
    if cmds.objExists(loc):
        cmds.delete(loc)

    return null_grp


def _set_null_color(null_grp: str, color: Tuple[float, float, float]) -> None:
    """Set the color of the locator shape in a null group."""
    shapes = cmds.listRelatives(null_grp, shapes=True) or []
    for shape in shapes:
        if cmds.nodeType(shape) == "locator":
            cmds.setAttr(f"{shape}.overrideColorR", color[0])
            cmds.setAttr(f"{shape}.overrideColorG", color[1])
            cmds.setAttr(f"{shape}.overrideColorB", color[2])


def _enter_pivot_adjust_mode(node: str) -> None:
    """
    Enter custom pivot editing mode with the translate tool active on the given node.

    This selects the node, activates the Move tool, and enters custom pivot editing
    mode (equivalent to pressing D or Insert key) so the user can immediately
    adjust the pivot position.

    See: https://help.autodesk.com/view/MAYAUL/2026/ENU/?guid=GUID-6BCE41D8-07CB-4A99-99CD-1D3986896157
    """
    # Ensure the node is selected
    cmds.select(node, replace=True)

    # Activate the Move tool and enter custom pivot editing mode
    # ctxEditMode is the MEL command equivalent to pressing D or Insert key
    mel.eval('MoveTool; ctxEditMode;')


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
    """Find a pending (stage 1) pivot null for a control."""
    # Look for null_group_1 that has a targetControl attr but no setupComplete=True yet
    pivot_nulls = cmds.ls(f"*{NULL_GRP_1_SUFFIX}", type="transform") or []
    for pivot in pivot_nulls:
        if cmds.attributeQuery("targetControl", node=pivot, exists=True):
            target = cmds.getAttr(f"{pivot}.targetControl")
            if target == control:
                # Check if setup is complete
                if cmds.attributeQuery("setupComplete", node=pivot, exists=True):
                    if not cmds.getAttr(f"{pivot}.setupComplete"):
                        return pivot
    return None


def get_rig_nodes(settings_node: str) -> Dict[str, Optional[str]]:
    """Get all rig node names from a settings node."""
    result = {
        "settings": settings_node,
        "null_grp_1": None,  # Pivot
        "null_grp_2": None,  # Anchor (may not exist yet)
        "control": None,
        "constraint": None,
    }

    if not cmds.objExists(settings_node):
        return result

    if cmds.attributeQuery("nullGrp1", node=settings_node, exists=True):
        result["null_grp_1"] = cmds.getAttr(f"{settings_node}.nullGrp1") or None
    if cmds.attributeQuery("nullGrp2", node=settings_node, exists=True):
        result["null_grp_2"] = cmds.getAttr(f"{settings_node}.nullGrp2") or None
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
# STAGE 1: Create Pivot Null
# =============================================================================

def create_pivot_locator(control: str) -> Tuple[bool, str, Optional[str]]:
    """
    STAGE 1: Create the pivot null (null_group_1) for user pivot positioning.

    Process:
    1. Create null_group_1
    2. Align to the selected control using world matrix
    3. Automatically enter pivot adjust mode with Move tool active
    4. User moves the pivot to desired position
    5. Then user clicks "Complete Setup" for Stage 2

    Args:
        control: The control to create a pivot for

    Returns:
        Tuple of (success, message, null_grp_1_name)
    """
    if not cmds.objExists(control):
        return False, f"Control '{control}' not found.", None

    # Check if rig already exists for this control
    existing = get_rig_for_control(control)
    if existing:
        return False, f"Pivot rig already exists for '{control}'. Delete it first or use Toggle.", None

    # Check if pending pivot exists
    pending = get_pending_pivot_for_control(control)
    if pending:
        cmds.select(pending)
        return False, f"Pivot null already created. Adjust its pivot, then click 'Complete Setup'.", pending

    # Check for existing constraints on the control (could cause double offset)
    has_constraints, constraint_list = _has_constraints(control)
    if has_constraints:
        constraint_names = ", ".join(constraint_list[:3])  # Show first 3
        if len(constraint_list) > 3:
            constraint_names += f"... (+{len(constraint_list) - 3} more)"
        return False, f"Control '{control}' has existing constraints: {constraint_names}. This may cause double transforms.", None

    # Create safe prefix
    prefix = _sanitize_name(control)

    # =========================================================================
    # Create null_group_1 (the PIVOT - user will adjust its pivot point)
    # =========================================================================
    null_grp_1 = _create_visual_null(
        f"{prefix}{NULL_GRP_1_SUFFIX}",
        UI_COLORS["stage1"],  # Orange for Stage 1
        size=1.0
    )

    # Align to control's world position and rotation using world matrix
    _align_to_target_world_matrix(null_grp_1, control)

    # Store target control reference on the null (for Stage 2)
    _add_string_attr(null_grp_1, "targetControl", control)
    _add_bool_attr(null_grp_1, "setupComplete", False)

    # Select the null and enter pivot adjust mode with translate tool
    # Use evalDeferred to ensure proper initialization timing
    cmds.evalDeferred(lambda: _enter_pivot_adjust_mode(null_grp_1))

    return True, f"Stage 1 complete. Move the PIVOT to your desired position, then click 'Complete Setup'.", null_grp_1


# =============================================================================
# STAGE 2: Complete Setup
# =============================================================================

def complete_setup(null_grp_1: str) -> Tuple[bool, str, Optional[str]]:
    """
    STAGE 2: Complete the pivot rig setup.

    Process:
    1. Get the target control from null_group_1
    2. Create parentConstraint: null_group_1 → control (maintainOffset)
    3. Create settings node

    Note: null_group_2 is NOT created here - it's created on first toggle OFF.

    Args:
        null_grp_1: The pivot null from Stage 1

    Returns:
        Tuple of (success, message, settings_node_name)
    """
    if not cmds.objExists(null_grp_1):
        return False, "Pivot null not found.", None

    # Get target control
    if not cmds.attributeQuery("targetControl", node=null_grp_1, exists=True):
        return False, "Pivot null is not valid (missing targetControl).", None

    control = cmds.getAttr(f"{null_grp_1}.targetControl")
    if not cmds.objExists(control):
        return False, f"Target control '{control}' not found.", None

    # Check if already complete
    if cmds.attributeQuery("setupComplete", node=null_grp_1, exists=True):
        if cmds.getAttr(f"{null_grp_1}.setupComplete"):
            return False, "Setup already complete for this pivot.", None

    prefix = _sanitize_name(control)

    # =========================================================================
    # Create parentConstraint: null_group_1 → control (maintainOffset=ON)
    # =========================================================================
    constraint_name = f"{prefix}{CONSTRAINT_SUFFIX}"
    if cmds.objExists(constraint_name):
        cmds.delete(constraint_name)

    constraint = cmds.parentConstraint(
        null_grp_1, control,
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
    _add_string_attr(settings_node, "nullGrp1", null_grp_1)
    _add_string_attr(settings_node, "nullGrp2", "")  # Created on first toggle OFF
    _add_string_attr(settings_node, "constraintName", constraint)
    _add_bool_attr(settings_node, "isActive", True)

    # Parent settings under null_grp_1 for organization
    cmds.parent(settings_node, null_grp_1)

    # Mark null_grp_1 setup as complete
    cmds.setAttr(f"{null_grp_1}.setupComplete", True)

    # Update null_grp_1 color to indicate active (green)
    _set_null_color(null_grp_1, UI_COLORS["success"])

    # Set up auto-key for transform changes
    setup_auto_key(settings_node)

    # Select null_grp_1 so user can start using it
    cmds.select(null_grp_1)

    return True, f"Setup complete! Rotate pivot null to orbit '{control}' around custom pivot. Auto-key enabled.", settings_node


# =============================================================================
# TOGGLE ON (Reactivate)
# =============================================================================

def toggle_on(settings_node: str) -> Tuple[bool, str]:
    """
    Reactivate the temp pivot system.

    Process:
    1. Realign null_group_2 to control's current world position/rotation
    2. Reset null_group_1's LOCAL transforms to zero
    3. Recreate parentConstraint: null_group_1 → control (maintainOffset)
    4. Show visibility

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
    null_grp_1 = nodes["null_grp_1"]
    null_grp_2 = nodes["null_grp_2"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."
    if not null_grp_1 or not cmds.objExists(null_grp_1):
        return False, "Pivot null (null_group_1) not found."
    if not null_grp_2 or not cmds.objExists(null_grp_2):
        return False, "Anchor null (null_group_2) not found. Cannot toggle ON."

    # =========================================================================
    # Realign null_group_2 to control's current world position/rotation
    # Using world matrix for accurate alignment
    # =========================================================================
    _align_to_target_world_matrix(null_grp_2, control)

    # =========================================================================
    # Reset null_group_1's LOCAL transforms to zero
    # This resets the orbital rotation while the pivot offset is maintained
    # by the pivot point position (rotatePivot/scalePivot)
    # =========================================================================
    cmds.setAttr(f"{null_grp_1}.tx", 0)
    cmds.setAttr(f"{null_grp_1}.ty", 0)
    cmds.setAttr(f"{null_grp_1}.tz", 0)
    cmds.setAttr(f"{null_grp_1}.rx", 0)
    cmds.setAttr(f"{null_grp_1}.ry", 0)
    cmds.setAttr(f"{null_grp_1}.rz", 0)

    # =========================================================================
    # Recreate parentConstraint: null_group_1 → control
    # =========================================================================
    prefix = _sanitize_name(control)
    constraint_name = f"{prefix}{CONSTRAINT_SUFFIX}"

    if cmds.objExists(constraint_name):
        cmds.delete(constraint_name)

    constraint = cmds.parentConstraint(
        null_grp_1, control,
        maintainOffset=True,
        name=constraint_name
    )[0]

    # Update settings
    cmds.setAttr(f"{settings_node}.constraintName", constraint, type="string")
    cmds.setAttr(f"{settings_node}.isActive", True)

    # =========================================================================
    # Show visibility
    # =========================================================================
    cmds.setAttr(f"{null_grp_2}.visibility", 1)

    # Update null_grp_1 color to active (green)
    _set_null_color(null_grp_1, UI_COLORS["success"])

    # Set up auto-key for transform changes
    setup_auto_key(settings_node)

    # Select null_grp_1
    cmds.select(null_grp_1)

    return True, f"Pivot ON. Rotate pivot null to orbit '{control}'. Auto-key enabled."


# =============================================================================
# TOGGLE OFF (Deactivate)
# =============================================================================

def toggle_off(settings_node: str) -> Tuple[bool, str]:
    """
    Deactivate the temp pivot system.

    Process:
    1. Clean up auto-key scriptJobs
    2. Create null_group_2 if it doesn't exist (first toggle OFF)
    3. Align null_group_2 to control's current position
    4. Parent null_group_1 under null_group_2 (if not already)
    5. Delete the constraint
    6. Hide visibility (optional - keep visible for reference)

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
    null_grp_1 = nodes["null_grp_1"]
    null_grp_2 = nodes["null_grp_2"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."
    if not null_grp_1 or not cmds.objExists(null_grp_1):
        return False, "Pivot null (null_group_1) not found."

    # =========================================================================
    # Clean up auto-key scriptJobs
    # =========================================================================
    cleanup_auto_key(settings_node)

    # =========================================================================
    # Delete the constraint FIRST (before creating/moving anchor)
    # =========================================================================
    if constraint and cmds.objExists(constraint):
        cmds.delete(constraint)

    # Also clean any other constraints from this tool
    constraints = cmds.listRelatives(control, type="parentConstraint") or []
    for c in constraints:
        if CONSTRAINT_SUFFIX in c or TOOL_PREFIX in c:
            cmds.delete(c)

    prefix = _sanitize_name(control)
    expected_anchor_name = f"{prefix}{NULL_GRP_2_SUFFIX}"

    # =========================================================================
    # Create null_group_2 if it doesn't exist (first toggle OFF)
    # =========================================================================
    # Check if we have a valid reference, or if the expected anchor already exists
    anchor_exists = False
    if null_grp_2 and cmds.objExists(null_grp_2):
        anchor_exists = True
    elif cmds.objExists(expected_anchor_name):
        # The anchor exists but settings might have outdated reference
        null_grp_2 = expected_anchor_name
        cmds.setAttr(f"{settings_node}.nullGrp2", null_grp_2, type="string")
        anchor_exists = True

    if not anchor_exists:
        null_grp_2 = _create_visual_null(
            expected_anchor_name,
            UI_COLORS["stage2"],  # Blue for anchor
            size=1.2  # Slightly larger to distinguish
        )
        # Store reference in settings
        cmds.setAttr(f"{settings_node}.nullGrp2", null_grp_2, type="string")

    # =========================================================================
    # Align null_group_2 to control's current world position/rotation
    # Using world matrix for accurate alignment
    # =========================================================================
    _align_to_target_world_matrix(null_grp_2, control)

    # =========================================================================
    # Parent null_group_1 under null_group_2 (if not already)
    # =========================================================================
    current_parent = cmds.listRelatives(null_grp_1, parent=True)
    if not current_parent or current_parent[0] != null_grp_2:
        cmds.parent(null_grp_1, null_grp_2)

    # =========================================================================
    # Reset null_group_1 local transforms (keep pivot offset via rotatePivot)
    # =========================================================================
    cmds.setAttr(f"{null_grp_1}.tx", 0)
    cmds.setAttr(f"{null_grp_1}.ty", 0)
    cmds.setAttr(f"{null_grp_1}.tz", 0)
    cmds.setAttr(f"{null_grp_1}.rx", 0)
    cmds.setAttr(f"{null_grp_1}.ry", 0)
    cmds.setAttr(f"{null_grp_1}.rz", 0)

    # Clear constraint reference and set inactive
    cmds.setAttr(f"{settings_node}.constraintName", "", type="string")
    cmds.setAttr(f"{settings_node}.isActive", False)

    # =========================================================================
    # Hide visibility
    # =========================================================================
    cmds.setAttr(f"{null_grp_2}.visibility", 0)

    # Update null_grp_1 color to inactive (orange)
    _set_null_color(null_grp_1, UI_COLORS["stage1"])

    return True, f"Pivot OFF. '{control}' is now free to move. Key if needed."


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
    """Set up scriptJobs to auto-key the control when null_group_1 is transformed."""
    global _auto_key_jobs

    # Clean up any existing jobs for this rig
    cleanup_auto_key(settings_node)

    if not cmds.objExists(settings_node):
        return

    nodes = get_rig_nodes(settings_node)
    null_grp_1 = nodes["null_grp_1"]

    if not null_grp_1 or not cmds.objExists(null_grp_1):
        return

    # Create callback function
    callback = _create_auto_key_callback(settings_node)

    # Set up scriptJobs for BOTH translation AND rotation attribute changes
    job_ids = []
    for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
        attr_path = f"{null_grp_1}.{attr}"
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

    # Delete null_group_2 (which parents null_group_1 and everything else)
    null_grp_2 = nodes["null_grp_2"]
    if null_grp_2 and cmds.objExists(null_grp_2):
        cmds.delete(null_grp_2)

    # Delete null_group_1 if it wasn't parented under null_group_2
    null_grp_1 = nodes["null_grp_1"]
    if null_grp_1 and cmds.objExists(null_grp_1):
        cmds.delete(null_grp_1)

    # Clean up orphaned settings node
    if cmds.objExists(settings_node):
        cmds.delete(settings_node)

    # Remove any remaining constraints
    if control and cmds.objExists(control):
        constraints = cmds.listRelatives(control, type="parentConstraint") or []
        for c in constraints:
            if CONSTRAINT_SUFFIX in c or TOOL_PREFIX in c:
                cmds.delete(c)

    return True, f"Deleted pivot rig for '{control}'."


def delete_pending_pivot(null_grp_1: str) -> Tuple[bool, str]:
    """Delete a pending (Stage 1) pivot null."""
    if not cmds.objExists(null_grp_1):
        return False, "Pivot null not found."

    control = ""
    if cmds.attributeQuery("targetControl", node=null_grp_1, exists=True):
        control = cmds.getAttr(f"{null_grp_1}.targetControl")

    cmds.delete(null_grp_1)
    return True, f"Deleted pending pivot null for '{control}'."


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
        label="Two-null pivot system for animation",
        align="left",
        font="smallPlainLabelFont",
        height=16
    )
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    cmds.text(
        label="1. Select control, click 'Create Pivot Locator'\n"
              "2. Move the pivot to your desired position\n"
              "3. Click 'Complete Setup'\n"
              "4. Rotate pivot null, Key, Toggle OFF when done",
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
        label="Stage 1: Create Pivot Null",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=6)

    cmds.text(
        label="Select a control, then create the pivot null.\n"
              "Move the pivot to your desired position:",
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
        label="After setting the pivot point, complete the setup.\n"
              "This creates the constraint to the control:",
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
        label="Select Pivot",
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
        pending_pivot = None

        for item in sel:
            # Check for null_grp_1 (pivot)
            if NULL_GRP_1_SUFFIX in item:
                # Check if it's pending or complete
                if cmds.attributeQuery("setupComplete", node=item, exists=True):
                    if cmds.getAttr(f"{item}.setupComplete"):
                        # Complete - find settings
                        prefix = item.replace(NULL_GRP_1_SUFFIX, "")
                        possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                        if cmds.objExists(possible_settings):
                            selected_settings = possible_settings
                    else:
                        pending_pivot = item
                break
            # Check for null_grp_2 (anchor)
            if NULL_GRP_2_SUFFIX in item:
                prefix = item.replace(NULL_GRP_2_SUFFIX, "")
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
                pending_pivot = pending

        if selected_settings:
            nodes = get_rig_nodes(selected_settings)
            control = nodes["control"]
            active = is_rig_active(selected_settings)
            cmds.text(selection_text, edit=True, label=f"Control: {control}")
            if active:
                cmds.button(state_indicator, edit=True, label="ON", backgroundColor=UI_COLORS["success"])
            else:
                cmds.button(state_indicator, edit=True, label="OFF", backgroundColor=UI_COLORS["stage1"])
        elif pending_pivot:
            if cmds.attributeQuery("targetControl", node=pending_pivot, exists=True):
                control = cmds.getAttr(f"{pending_pivot}.targetControl")
                cmds.text(selection_text, edit=True, label=f"Pending: {control}")
            cmds.button(state_indicator, edit=True, label="STAGE1", backgroundColor=UI_COLORS["stage1"])
        elif sel:
            cmds.text(selection_text, edit=True, label=f"Selected: {sel[0]}")
            cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])
        else:
            cmds.text(selection_text, edit=True, label="No control selected")
            cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])

    def get_current_context():
        """Get current rig settings or pending pivot."""
        sel = cmds.ls(selection=True, type="transform") or []

        for item in sel:
            if NULL_GRP_1_SUFFIX in item:
                if cmds.attributeQuery("setupComplete", node=item, exists=True):
                    if cmds.getAttr(f"{item}.setupComplete"):
                        prefix = item.replace(NULL_GRP_1_SUFFIX, "")
                        possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                        if cmds.objExists(possible_settings):
                            return ("rig", possible_settings)
                    else:
                        return ("pending", item)
            if NULL_GRP_2_SUFFIX in item:
                prefix = item.replace(NULL_GRP_2_SUFFIX, "")
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
        # Force Maya to process any pending events and refresh selection
        cmds.refresh(force=True)

        sel = cmds.ls(selection=True, type="transform") or []
        controls = [s for s in sel if TOOL_PREFIX not in s]

        if not controls:
            log_message("Select a control first.", "warning")
            return

        control = controls[0]
        success, msg, pivot = create_pivot_locator(control)
        log_message(msg, "success" if success else "warning")

        # Defer UI updates to avoid interfering with pivot adjust mode activation
        cmds.evalDeferred(refresh_rig_list)
        cmds.evalDeferred(update_status)

    def on_complete_setup(*args):
        ctx_type, ctx_node = get_current_context()
        pivot_to_select = None

        if ctx_type == "pending":
            success, msg, settings = complete_setup(ctx_node)
            log_message(msg, "success" if success else "error")
            if success and settings:
                nodes = get_rig_nodes(settings)
                pivot_to_select = nodes["null_grp_1"]
        elif ctx_type == "rig":
            log_message("Setup already complete. Use Toggle to activate.", "warning")
        else:
            # Try to find pending pivot for selected control
            sel = cmds.ls(selection=True, type="transform") or []
            for item in sel:
                pending = get_pending_pivot_for_control(item)
                if pending:
                    success, msg, settings = complete_setup(pending)
                    log_message(msg, "success" if success else "error")
                    if success and settings:
                        nodes = get_rig_nodes(settings)
                        pivot_to_select = nodes["null_grp_1"]
                    refresh_rig_list()
                    update_status()
                    # Ensure pivot is selected after UI updates
                    if pivot_to_select and cmds.objExists(pivot_to_select):
                        cmds.evalDeferred(lambda loc=pivot_to_select: cmds.select(loc))
                    return

            log_message("No pending pivot null found. Create one first.", "warning")

        refresh_rig_list()
        update_status()
        # Ensure pivot is selected after UI updates
        if pivot_to_select and cmds.objExists(pivot_to_select):
            cmds.evalDeferred(lambda loc=pivot_to_select: cmds.select(loc))

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
            pivot = nodes["null_grp_1"]
            if pivot and cmds.objExists(pivot):
                cmds.select(pivot)
                log_message(f"Selected: {pivot}", "info")
        elif ctx_type == "pending":
            cmds.select(ctx_node)
            log_message(f"Selected: {ctx_node}", "info")
        else:
            log_message("No pivot null found.", "warning")

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
        """Handle selection in the rig list - select the pivot in viewport."""
        selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
        if selected_items:
            control_name = selected_items[0].split(" [")[0]
            settings = get_rig_for_control(control_name)
            if settings:
                nodes = get_rig_nodes(settings)
                pivot = nodes["null_grp_1"]
                if pivot and cmds.objExists(pivot):
                    # Set flag to prevent refresh from wiping out our list selection
                    _skip_list_refresh[0] = True
                    try:
                        cmds.select(pivot)
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
