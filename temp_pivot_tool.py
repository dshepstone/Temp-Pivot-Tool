"""
Temp Pivot Tool for Autodesk Maya

A non-destructive, reusable temporary pivot system for animation.

SIMPLIFIED WORKFLOW:
1. Select control, click "Create Temp Pivot"
2. Pivot mode activates - move the pivot to desired location
3. Switch to Translate or Rotate tool - pivot mode exits, constraint created
4. Manipulate control via the temp pivot
5. Toggle OFF - keys the control, deletes constraint
6. Move control to new position
7. Toggle ON - temp pivot realigns to control (pivot location preserved)
8. Repeat as needed

Hierarchy Structure:
    align_grp (realigns to control on toggle)
      └── pivot_grp (user adjusts pivot here, constrains control)
            └── settings node

ALIGNMENT METHODOLOGY (Constraint-Based Snap):
    This tool uses constraint-based snapping instead of Maya's matchTransform
    or direct xform world-space operations. The reason:

    - matchTransform/xform ws=True can rewrite local transform values in ways
      that break expected parent-child offsets, especially in complex hierarchies.
    - Constraint-based snap creates a temporary constraint (maintainOffset=False),
      which "solves" the target's local matrix through its parent space, then
      immediately deletes the constraint.
    - This preserves the natural parent-child relationship and ensures children
      follow correctly after the snap.

Features:
- Two-group hierarchy preserves pivot offset naturally
- Constraint-based snap for reliable realignment
- Auto pivot mode with smart exit detection
- Auto-key on toggle off
- Edit Temp Pivot button to adjust pivot location

Author: David Shepstone
License: MIT
Version: 8.0.0
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import maya.cmds as cmds

# -----------------------------
# Constants
# -----------------------------

WINDOW_NAME = "tempPivotToolWindow"
WINDOW_TITLE = "Temp Pivot Tool"
TOOL_PREFIX = "TMP"

# Node naming convention
ALIGN_GRP_SUFFIX = f"_{TOOL_PREFIX}_align"
PIVOT_GRP_SUFFIX = f"_{TOOL_PREFIX}_pivot"
SETTINGS_SUFFIX = f"_{TOOL_PREFIX}_settings"
CONSTRAINT_SUFFIX = f"_{TOOL_PREFIX}_parentConstraint"

# Pivot mode scriptJob storage
_pivot_mode_jobs: Dict[str, int] = {}

# Auto-key scriptJob storage (keyed by settings node name)
_auto_key_jobs: Dict[str, List[int]] = {}

# UI Colors
UI_COLORS = {
    "accent": (0.36, 0.68, 0.93),
    "success": (0.20, 0.75, 0.45),
    "warning": (0.95, 0.77, 0.26),
    "error": (0.95, 0.35, 0.35),
    "pivot_mode": (0.95, 0.65, 0.25),  # Orange - pivot positioning
    "on_state": (0.20, 0.75, 0.45),
    "off_state": (0.45, 0.45, 0.48),
}

# Tooltips
TOOLTIPS = {
    "create_btn": (
        "Create a temp pivot at the selected control.\n\n"
        "1. Creates a pivot group at control's position\n"
        "2. Enters pivot mode - move pivot to desired location\n"
        "3. Switch to Translate/Rotate to exit pivot mode\n"
        "4. Constraint is automatically created"
    ),
    "toggle_btn": (
        "Toggle the temp pivot ON/OFF.\n\n"
        "OFF: Keys the control, deletes constraint.\n"
        "     Move control to new position.\n\n"
        "ON: Realigns pivot group to control position,\n"
        "    recreates constraint. Pivot location preserved."
    ),
    "edit_pivot_btn": (
        "Edit the temp pivot location.\n\n"
        "Enters pivot mode so you can reposition the pivot.\n"
        "Switch to Translate/Rotate to exit and apply."
    ),
    "delete_btn": (
        "Delete the temp pivot rig completely.\n"
        "Removes all rig nodes and cleans up constraints."
    ),
    "select_pivot_btn": (
        "Select the temp pivot group.\n"
        "Use this to manually adjust the pivot."
    ),
    "select_control_btn": (
        "Select the original control.\n"
        "Useful for keying or checking values."
    ),
}


# =============================================================================
# CONSTRAINT-BASED SNAP SYSTEM
# =============================================================================
#
# WHY CONSTRAINT-BASED SNAP?
#
# Maya's matchTransform and xform(ws=True) commands perform a one-time
# world-space "teleport" that directly overwrites local transform values.
# This can break expected parent-child offsets because:
#
# 1. The local matrix is recomputed to achieve the world position, but this
#    doesn't account for how the hierarchy should behave when the parent moves.
#
# 2. Children may not follow correctly because their relationship to the
#    parent is effectively "baked out."
#
# The constraint-based approach:
# 1. Creates a temporary constraint (parentConstraint or pointConstraint)
# 2. Sets maintainOffset=False so the driven object snaps exactly to driver
# 3. Maya's constraint system solves the driven object's local transform
#    values through its parent hierarchy
# 4. Immediately deletes the constraint
#
# Result: The local matrix is computed correctly relative to the parent,
# so when the parent moves later, children follow as expected.
# =============================================================================


def snap_transform(
    driver: str,
    driven: str,
    mode: str = "parent",
    translate: bool = True,
    rotate: bool = True,
    preserve_selection: bool = True,
    use_undo_chunk: bool = True
) -> Tuple[bool, str]:
    """
    Snap driven object to driver using temporary constraint.

    This is the core alignment function that replaces matchTransform/xform.
    It uses constraints to properly solve local transforms through the
    parent hierarchy, ensuring children follow correctly after snap.

    Args:
        driver: The object to snap TO (the target position)
        driven: The object being snapped (will move to driver's position)
        mode: "parent" for full transform, "point" for translation only,
              "orient" for rotation only
        translate: Whether to snap translation (used with mode="parent")
        rotate: Whether to snap rotation (used with mode="parent")
        preserve_selection: If True, restore original selection after snap
        use_undo_chunk: If True, wrap in undo chunk for clean undo

    Returns:
        Tuple of (success, message)

    Validation Tests:
        1. Create rig, offset pivot, toggle OFF, move control, toggle ON
           → align_grp snaps to control, pivot_grp retains local offset
        2. After snap, moving control → hierarchy follows correctly
        3. Repeated toggles do not accumulate drift
    """
    # Validate inputs
    if not driver or not cmds.objExists(driver):
        msg = f"Snap failed: driver '{driver}' does not exist."
        cmds.warning(msg)
        return False, msg

    if not driven or not cmds.objExists(driven):
        msg = f"Snap failed: driven '{driven}' does not exist."
        cmds.warning(msg)
        return False, msg

    # Store original selection if needed
    original_selection = None
    if preserve_selection:
        original_selection = cmds.ls(selection=True, long=True) or []

    # Open undo chunk if requested
    if use_undo_chunk:
        cmds.undoInfo(openChunk=True, chunkName="snap_transform")

    try:
        # Check for locked channels and determine what we can snap
        skip_translate = []
        skip_rotate = []

        # Check translate locks
        for axis in ["x", "y", "z"]:
            attr_path = f"{driven}.t{axis}"
            if cmds.objExists(attr_path):
                if cmds.getAttr(attr_path, lock=True):
                    skip_translate.append(axis)
                # Also check if connected (would fail constraint)
                conns = cmds.listConnections(attr_path, source=True, destination=False) or []
                if conns:
                    skip_translate.append(axis)

        # Check rotate locks
        for axis in ["x", "y", "z"]:
            attr_path = f"{driven}.r{axis}"
            if cmds.objExists(attr_path):
                if cmds.getAttr(attr_path, lock=True):
                    skip_rotate.append(axis)
                conns = cmds.listConnections(attr_path, source=True, destination=False) or []
                if conns:
                    skip_rotate.append(axis)

        # Remove duplicates
        skip_translate = list(set(skip_translate))
        skip_rotate = list(set(skip_rotate))

        # Determine if we can do any snapping
        can_translate = translate and len(skip_translate) < 3
        can_rotate = rotate and len(skip_rotate) < 3

        if not can_translate and not can_rotate:
            msg = f"Snap failed: all channels on '{driven}' are locked or connected."
            cmds.warning(msg)
            return False, msg

        # Warn about partial snapping
        if skip_translate or skip_rotate:
            skipped = []
            if skip_translate:
                skipped.append(f"translate {skip_translate}")
            if skip_rotate:
                skipped.append(f"rotate {skip_rotate}")
            cmds.warning(f"Snap: skipping locked/connected channels on '{driven}': {', '.join(skipped)}")

        # Perform the constraint-based snap
        constraint = None

        if mode == "point" or (can_translate and not can_rotate):
            # Point constraint for translation only
            try:
                constraint = cmds.pointConstraint(
                    driver, driven,
                    maintainOffset=False,
                    skip=skip_translate if skip_translate else []
                )[0]
            except RuntimeError as e:
                msg = f"Point constraint failed on '{driven}': {e}"
                cmds.warning(msg)
                return False, msg

        elif mode == "orient" or (can_rotate and not can_translate):
            # Orient constraint for rotation only
            try:
                constraint = cmds.orientConstraint(
                    driver, driven,
                    maintainOffset=False,
                    skip=skip_rotate if skip_rotate else []
                )[0]
            except RuntimeError as e:
                msg = f"Orient constraint failed on '{driven}': {e}"
                cmds.warning(msg)
                return False, msg

        else:
            # Parent constraint for full transform
            try:
                constraint = cmds.parentConstraint(
                    driver, driven,
                    maintainOffset=False,
                    skipTranslate=skip_translate if skip_translate else [],
                    skipRotate=skip_rotate if skip_rotate else []
                )[0]
            except RuntimeError as e:
                msg = f"Parent constraint failed on '{driven}': {e}"
                cmds.warning(msg)
                return False, msg

        # Immediately delete the constraint
        if constraint and cmds.objExists(constraint):
            cmds.delete(constraint)

        return True, f"Snapped '{driven}' to '{driver}'"

    except Exception as e:
        msg = f"Snap failed with error: {e}"
        cmds.warning(msg)
        return False, msg

    finally:
        # Close undo chunk
        if use_undo_chunk:
            cmds.undoInfo(closeChunk=True)

        # Restore selection
        if preserve_selection and original_selection is not None:
            try:
                if original_selection:
                    cmds.select(original_selection, replace=True)
                else:
                    cmds.select(clear=True)
            except:
                pass  # Selection restore is best-effort


def get_local_transform(node: str) -> Dict[str, List[float]]:
    """
    Get the local transform values of a node.

    Returns a dictionary with translate, rotate, scale, and rotatePivot
    in local space. This is used to store/restore pivot_grp's local
    position relative to align_grp.

    Args:
        node: The node to query

    Returns:
        Dictionary with 'translate', 'rotate', 'scale', 'rotatePivot', 'scalePivot'
    """
    result = {
        "translate": [0.0, 0.0, 0.0],
        "rotate": [0.0, 0.0, 0.0],
        "scale": [1.0, 1.0, 1.0],
        "rotatePivot": [0.0, 0.0, 0.0],
        "scalePivot": [0.0, 0.0, 0.0],
    }

    if not node or not cmds.objExists(node):
        return result

    try:
        result["translate"] = cmds.xform(node, q=True, os=True, translation=True)
        result["rotate"] = cmds.xform(node, q=True, os=True, rotation=True)
        result["scale"] = cmds.xform(node, q=True, os=True, scale=True)
        result["rotatePivot"] = cmds.xform(node, q=True, os=True, rotatePivot=True)
        result["scalePivot"] = cmds.xform(node, q=True, os=True, scalePivot=True)
    except:
        pass

    return result


def set_local_transform(node: str, transform_data: Dict[str, List[float]]) -> bool:
    """
    Set the local transform values of a node.

    Args:
        node: The node to set transforms on
        transform_data: Dictionary from get_local_transform()

    Returns:
        True if successful
    """
    if not node or not cmds.objExists(node):
        return False

    try:
        if "translate" in transform_data:
            cmds.xform(node, os=True, translation=transform_data["translate"])
        if "rotate" in transform_data:
            cmds.xform(node, os=True, rotation=transform_data["rotate"])
        if "scale" in transform_data:
            cmds.xform(node, os=True, scale=transform_data["scale"])
        if "rotatePivot" in transform_data:
            cmds.xform(node, os=True, rotatePivot=transform_data["rotatePivot"])
        if "scalePivot" in transform_data:
            cmds.xform(node, os=True, scalePivot=transform_data["scalePivot"])
        return True
    except Exception as e:
        cmds.warning(f"Failed to set local transform on '{node}': {e}")
        return False


# -----------------------------
# Utility Functions
# -----------------------------

def _sanitize_name(name: str) -> str:
    """Create a safe prefix from a control name, handling namespaces."""
    # Handle namespaces - take the last part after any colons
    safe = name.split(":")[-1]
    # Handle DAG paths - take the last part after any pipes
    safe = safe.split("|")[-1]
    # Replace other problematic characters
    safe = safe.replace(" ", "_")
    return safe


def _has_constraints(node: str) -> Tuple[bool, List[str]]:
    """Check if a node has any constraints affecting it."""
    constraint_types = [
        "parentConstraint", "pointConstraint", "orientConstraint",
        "scaleConstraint", "aimConstraint"
    ]

    found_constraints = []
    for ctype in constraint_types:
        constraints = cmds.listRelatives(node, type=ctype) or []
        # Filter out our own constraints
        for c in constraints:
            if TOOL_PREFIX not in c:
                found_constraints.append(c)

    # Also check connections to translate/rotate attributes
    for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
        attr_path = f"{node}.{attr}"
        if cmds.objExists(attr_path):
            connections = cmds.listConnections(attr_path, source=True, destination=False, plugs=True) or []
            for conn in connections:
                conn_node = conn.split(".")[0]
                node_type = cmds.nodeType(conn_node)
                if "Constraint" in node_type and conn_node not in found_constraints:
                    if TOOL_PREFIX not in conn_node:
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


def _get_current_tool() -> str:
    """Get the current tool context."""
    return cmds.currentCtx()


def _is_pivot_tool() -> bool:
    """Check if we're in pivot edit mode."""
    ctx = cmds.currentCtx()
    # Check various pivot/insert key mode contexts
    return "insert" in ctx.lower() or "pivot" in ctx.lower()


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
        "align_grp": None,
        "pivot_grp": None,
        "control": None,
        "constraint": None,
    }

    if not cmds.objExists(settings_node):
        return result

    if cmds.attributeQuery("alignGrp", node=settings_node, exists=True):
        result["align_grp"] = cmds.getAttr(f"{settings_node}.alignGrp") or None
    if cmds.attributeQuery("pivotGrp", node=settings_node, exists=True):
        result["pivot_grp"] = cmds.getAttr(f"{settings_node}.pivotGrp") or None
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


def is_in_pivot_mode(settings_node: str) -> bool:
    """Check if a rig is currently in pivot adjust mode."""
    if not cmds.objExists(settings_node):
        return False
    if cmds.attributeQuery("inPivotMode", node=settings_node, exists=True):
        return cmds.getAttr(f"{settings_node}.inPivotMode")
    return False


# =============================================================================
# PIVOT MODE MANAGEMENT
# =============================================================================

def enter_pivot_mode(settings_node: str) -> Tuple[bool, str]:
    """
    Enter pivot adjust mode for the pivot group.

    This activates Maya's insert key (pivot) mode and sets up monitoring
    for when the user switches to translate/rotate mode.
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    nodes = get_rig_nodes(settings_node)
    pivot_grp = nodes["pivot_grp"]
    align_grp = nodes["align_grp"]

    if not pivot_grp or not cmds.objExists(pivot_grp):
        return False, "Pivot group not found."

    # Delete any existing constraint while in pivot mode
    constraint = nodes["constraint"]
    if constraint and cmds.objExists(constraint):
        cmds.delete(constraint)
        cmds.setAttr(f"{settings_node}.constraintName", "", type="string")

    # Select the pivot group
    cmds.select(pivot_grp)

    # Enter pivot/insert mode
    cmds.ctxEditMode()  # This toggles insert mode for the current tool

    # Mark as in pivot mode
    cmds.setAttr(f"{settings_node}.inPivotMode", True)
    cmds.setAttr(f"{settings_node}.isActive", False)

    # Set up scriptJob to detect when user exits pivot mode
    _setup_pivot_mode_monitor(settings_node)

    # Update visual feedback - orange color for pivot mode
    _update_pivot_visual(pivot_grp, "pivot_mode")

    # Make sure align_grp is visible during pivot editing
    if align_grp and cmds.objExists(align_grp):
        cmds.setAttr(f"{align_grp}.visibility", 1)

    return True, "Pivot mode active. Move the pivot, then switch to Translate/Rotate to apply."


def exit_pivot_mode(settings_node: str) -> Tuple[bool, str]:
    """
    Exit pivot adjust mode and create the constraint.

    This is called automatically when the user switches from pivot mode
    to translate or rotate mode.
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    if not is_in_pivot_mode(settings_node):
        return False, "Not in pivot mode."

    nodes = get_rig_nodes(settings_node)
    pivot_grp = nodes["pivot_grp"]
    control = nodes["control"]

    if not pivot_grp or not cmds.objExists(pivot_grp):
        return False, "Pivot group not found."
    if not control or not cmds.objExists(control):
        return False, "Control not found."

    # Clean up pivot mode monitor
    _cleanup_pivot_mode_monitor(settings_node)

    # Mark as no longer in pivot mode
    cmds.setAttr(f"{settings_node}.inPivotMode", False)

    # Create the parent constraint
    prefix = _sanitize_name(control)
    constraint_name = f"{prefix}{CONSTRAINT_SUFFIX}"

    if cmds.objExists(constraint_name):
        cmds.delete(constraint_name)

    constraint = cmds.parentConstraint(
        pivot_grp, control,
        maintainOffset=True,
        name=constraint_name
    )[0]

    # Update settings
    cmds.setAttr(f"{settings_node}.constraintName", constraint, type="string")
    cmds.setAttr(f"{settings_node}.isActive", True)

    # Update visual feedback - green for active
    _update_pivot_visual(pivot_grp, "on_state")

    # Set up auto-key
    setup_auto_key(settings_node)

    # Select the pivot group so user can manipulate
    cmds.select(pivot_grp)

    return True, f"Pivot mode exited. Constraint created. Manipulate pivot to move '{control}'."


def _setup_pivot_mode_monitor(settings_node: str) -> None:
    """Set up a scriptJob to monitor for tool changes to exit pivot mode."""
    global _pivot_mode_jobs

    # Clean up any existing monitor
    _cleanup_pivot_mode_monitor(settings_node)

    def check_tool_change():
        """Check if user has switched out of pivot mode."""
        if not cmds.objExists(settings_node):
            _cleanup_pivot_mode_monitor(settings_node)
            return

        if not is_in_pivot_mode(settings_node):
            return

        # Check if we're no longer in insert/pivot mode
        if not _is_pivot_tool():
            # User switched to translate/rotate - exit pivot mode
            cmds.evalDeferred(lambda: exit_pivot_mode(settings_node))

    # Monitor tool changes
    job_id = cmds.scriptJob(
        event=["ToolChanged", check_tool_change],
        killWithScene=True
    )

    _pivot_mode_jobs[settings_node] = job_id


def _cleanup_pivot_mode_monitor(settings_node: str) -> None:
    """Clean up the pivot mode monitor scriptJob."""
    global _pivot_mode_jobs

    if settings_node in _pivot_mode_jobs:
        job_id = _pivot_mode_jobs[settings_node]
        if cmds.scriptJob(exists=job_id):
            cmds.scriptJob(kill=job_id, force=True)
        del _pivot_mode_jobs[settings_node]


def _update_pivot_visual(pivot_grp: str, state: str) -> None:
    """Update the visual appearance of the pivot group based on state."""
    shapes = cmds.listRelatives(pivot_grp, shapes=True) or []
    color = UI_COLORS.get(state, UI_COLORS["off_state"])

    for shape in shapes:
        if cmds.nodeType(shape) in ["locator", "nurbsCurve"]:
            cmds.setAttr(f"{shape}.overrideEnabled", 1)
            cmds.setAttr(f"{shape}.overrideRGBColors", 1)
            cmds.setAttr(f"{shape}.overrideColorR", color[0])
            cmds.setAttr(f"{shape}.overrideColorG", color[1])
            cmds.setAttr(f"{shape}.overrideColorB", color[2])


# =============================================================================
# CREATE TEMP PIVOT
# =============================================================================

def create_temp_pivot(control: str) -> Tuple[bool, str, Optional[str]]:
    """
    Create a temp pivot at the selected control.

    Creates two groups:
    - align_grp: Parent group that realigns to control on toggle
    - pivot_grp: Child group where user adjusts pivot, constrains control

    Hierarchy:
        align_grp (realigns to control)
          └── pivot_grp (user moves pivot here)
                └── settings node

    Uses constraint-based snap for initial alignment to ensure proper
    local transform values that will behave correctly in the hierarchy.

    Args:
        control: The control to create a temp pivot for

    Returns:
        Tuple of (success, message, settings_node_name)
    """
    if not cmds.objExists(control):
        return False, f"Control '{control}' not found.", None

    # Check if rig already exists for this control
    existing = get_rig_for_control(control)
    if existing:
        return False, f"Temp pivot already exists for '{control}'. Delete it first or use Toggle.", None

    # Check for existing constraints on the control
    has_const, constraint_list = _has_constraints(control)
    if has_const:
        constraint_names = ", ".join(constraint_list[:3])
        if len(constraint_list) > 3:
            constraint_names += f"... (+{len(constraint_list) - 3} more)"
        return False, f"Control '{control}' has existing constraints: {constraint_names}. This may cause double transforms.", None

    # Create safe prefix
    prefix = _sanitize_name(control)

    # =========================================================================
    # Create align_grp (parent - this realigns to control on toggle)
    # =========================================================================
    align_grp = cmds.group(empty=True, name=f"{prefix}{ALIGN_GRP_SUFFIX}")

    # Use constraint-based snap to align to control
    success, msg = snap_transform(control, align_grp, mode="parent", use_undo_chunk=False)
    if not success:
        cmds.delete(align_grp)
        return False, f"Failed to create temp pivot: {msg}", None

    # =========================================================================
    # Create pivot_grp (child - user adjusts pivot here)
    # =========================================================================
    pivot_grp = cmds.group(empty=True, name=f"{prefix}{PIVOT_GRP_SUFFIX}")

    # Snap to control position before parenting
    success, msg = snap_transform(control, pivot_grp, mode="parent", use_undo_chunk=False)
    if not success:
        cmds.delete(align_grp)
        cmds.delete(pivot_grp)
        return False, f"Failed to create temp pivot: {msg}", None

    # Parent pivot_grp under align_grp
    # After parenting, pivot_grp's local transform should be identity (0,0,0)
    # because both were snapped to the same position
    cmds.parent(pivot_grp, align_grp)

    # Add a locator shape for visibility
    loc = cmds.spaceLocator()[0]
    loc_shape = cmds.listRelatives(loc, shapes=True)[0]
    cmds.parent(loc_shape, pivot_grp, shape=True, relative=True)
    cmds.delete(loc)

    # Style the locator
    shapes = cmds.listRelatives(pivot_grp, shapes=True) or []
    for shape in shapes:
        if cmds.nodeType(shape) == "locator":
            cmds.setAttr(f"{shape}.overrideEnabled", 1)
            cmds.setAttr(f"{shape}.overrideRGBColors", 1)
            cmds.setAttr(f"{shape}.overrideColorR", UI_COLORS["pivot_mode"][0])
            cmds.setAttr(f"{shape}.overrideColorG", UI_COLORS["pivot_mode"][1])
            cmds.setAttr(f"{shape}.overrideColorB", UI_COLORS["pivot_mode"][2])
            cmds.setAttr(f"{shape}.localScaleX", 0.5)
            cmds.setAttr(f"{shape}.localScaleY", 0.5)
            cmds.setAttr(f"{shape}.localScaleZ", 0.5)

    # Add visual rings to indicate pivot
    for axis, color, normal in [
        ("X", (1, 0.3, 0.3), (1, 0, 0)),
        ("Y", (0.3, 1, 0.3), (0, 1, 0)),
        ("Z", (0.3, 0.5, 1), (0, 0, 1))
    ]:
        circle = cmds.circle(
            name=f"{prefix}{PIVOT_GRP_SUFFIX}_ring{axis}",
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
        cmds.parent(circle_shape, pivot_grp, shape=True, relative=True)
        cmds.delete(circle)

    # =========================================================================
    # Create settings node
    # =========================================================================
    settings_node = cmds.createNode("transform", name=f"{prefix}{SETTINGS_SUFFIX}")
    cmds.setAttr(f"{settings_node}.visibility", 0)

    # Store references
    _add_string_attr(settings_node, "targetControl", control)
    _add_string_attr(settings_node, "alignGrp", align_grp)
    _add_string_attr(settings_node, "pivotGrp", pivot_grp)
    _add_string_attr(settings_node, "constraintName", "")
    _add_bool_attr(settings_node, "isActive", False)
    _add_bool_attr(settings_node, "inPivotMode", False)

    # Parent settings under pivot_grp
    cmds.parent(settings_node, pivot_grp)

    # =========================================================================
    # Enter pivot mode
    # =========================================================================
    enter_pivot_mode(settings_node)

    return True, f"Temp pivot created. Move the pivot, then switch to Translate/Rotate to apply.", settings_node


# =============================================================================
# TOGGLE ON (Reactivate)
# =============================================================================

def toggle_on(settings_node: str) -> Tuple[bool, str]:
    """
    Reactivate the temp pivot system.

    Process:
    1. Store pivot_grp's local transform (the pivot offset relative to align_grp)
    2. Snap align_grp to the control's current world position using constraint
    3. Restore pivot_grp's local transform (maintains pivot offset)
    4. Create parentConstraint: pivot_grp → control

    The constraint-based snap ensures align_grp's local values are computed
    correctly through its parent hierarchy. The pivot offset is preserved
    because we store and restore pivot_grp's LOCAL transform, not world.

    Validation Tests:
        1. Create rig, offset pivot, toggle OFF, move control, toggle ON
           → align_grp snaps to control, pivot_grp retains rig-space offset
        2. After toggle ON, moving control → children follow correctly
        3. Repeated toggles do not drift or accumulate error

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
    align_grp = nodes["align_grp"]
    pivot_grp = nodes["pivot_grp"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."
    if not align_grp or not cmds.objExists(align_grp):
        return False, "Align group not found."
    if not pivot_grp or not cmds.objExists(pivot_grp):
        return False, "Pivot group not found."

    # =========================================================================
    # Store pivot_grp's local transform BEFORE snapping align_grp
    # This is the "pivot offset" - the user-defined pivot position relative
    # to where the align_grp is positioned.
    # =========================================================================
    pivot_local_transform = get_local_transform(pivot_grp)

    # =========================================================================
    # Snap align_grp to the control's world position using constraint
    # This properly solves the local matrix through parent hierarchy
    # =========================================================================
    success, msg = snap_transform(
        control, align_grp,
        mode="parent",
        preserve_selection=True,
        use_undo_chunk=False
    )

    if not success:
        return False, f"Failed to realign temp pivot: {msg}"

    # =========================================================================
    # Restore pivot_grp's local transform
    # Because we're setting LOCAL values (not world), the pivot stays at
    # the same offset relative to align_grp's new position
    # =========================================================================
    set_local_transform(pivot_grp, pivot_local_transform)

    # =========================================================================
    # Create parentConstraint: pivot_grp → control
    # =========================================================================
    prefix = _sanitize_name(control)
    constraint_name = f"{prefix}{CONSTRAINT_SUFFIX}"

    if cmds.objExists(constraint_name):
        cmds.delete(constraint_name)

    constraint = cmds.parentConstraint(
        pivot_grp, control,
        maintainOffset=True,
        name=constraint_name
    )[0]

    # Update settings
    cmds.setAttr(f"{settings_node}.constraintName", constraint, type="string")
    cmds.setAttr(f"{settings_node}.isActive", True)

    # =========================================================================
    # Show visibility
    # =========================================================================
    cmds.setAttr(f"{align_grp}.visibility", 1)

    # Update visual - green for active
    _update_pivot_visual(pivot_grp, "on_state")

    # Set up auto-key
    setup_auto_key(settings_node)

    # Select pivot group
    cmds.select(pivot_grp)

    return True, f"Pivot ON. Manipulate pivot to move '{control}'."


# =============================================================================
# TOGGLE OFF (Deactivate)
# =============================================================================

def toggle_off(settings_node: str) -> Tuple[bool, str]:
    """
    Deactivate the temp pivot system.

    Process:
    1. Key the control at current position
    2. Delete the constraint
    3. Hide the align group (and children)

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
    align_grp = nodes["align_grp"]
    pivot_grp = nodes["pivot_grp"]

    # =========================================================================
    # Clean up auto-key scriptJobs
    # =========================================================================
    cleanup_auto_key(settings_node)

    # =========================================================================
    # KEY THE CONTROL before deleting constraint
    # =========================================================================
    if control and cmds.objExists(control):
        key_control(settings_node)

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
    if align_grp and cmds.objExists(align_grp):
        cmds.setAttr(f"{align_grp}.visibility", 0)

    # Update visual - orange for inactive
    if pivot_grp and cmds.objExists(pivot_grp):
        _update_pivot_visual(pivot_grp, "pivot_mode")

    return True, f"Pivot OFF. '{control}' keyed at current position."


# =============================================================================
# TOGGLE (Smart)
# =============================================================================

def toggle_pivot(settings_node: str) -> Tuple[bool, str, bool]:
    """Smart toggle - ON if OFF, OFF if ON."""
    if not cmds.objExists(settings_node):
        return False, "Settings node not found.", False

    # If in pivot mode, exit it first
    if is_in_pivot_mode(settings_node):
        exit_pivot_mode(settings_node)
        return True, "Exited pivot mode. Constraint created.", True

    if is_rig_active(settings_node):
        success, msg = toggle_off(settings_node)
        return success, msg, False
    else:
        success, msg = toggle_on(settings_node)
        return success, msg, True


# =============================================================================
# EDIT TEMP PIVOT
# =============================================================================

def edit_temp_pivot(settings_node: str) -> Tuple[bool, str]:
    """
    Enter pivot edit mode for an existing temp pivot.

    This allows the user to reposition the pivot location.
    When they switch to translate/rotate, the constraint is recreated.
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    # If active, deactivate first (but don't key - user just wants to edit pivot)
    if is_rig_active(settings_node):
        nodes = get_rig_nodes(settings_node)
        constraint = nodes["constraint"]

        # Clean up auto-key
        cleanup_auto_key(settings_node)

        # Delete constraint
        if constraint and cmds.objExists(constraint):
            cmds.delete(constraint)

        cmds.setAttr(f"{settings_node}.constraintName", "", type="string")
        cmds.setAttr(f"{settings_node}.isActive", False)

    # Enter pivot mode
    return enter_pivot_mode(settings_node)


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
        if cmds.objExists(settings_node) and is_rig_active(settings_node):
            key_control(settings_node)
    return auto_key_callback


def setup_auto_key(settings_node: str) -> None:
    """Set up scriptJobs to auto-key the control when pivot group is transformed."""
    global _auto_key_jobs

    cleanup_auto_key(settings_node)

    if not cmds.objExists(settings_node):
        return

    nodes = get_rig_nodes(settings_node)
    pivot_grp = nodes["pivot_grp"]

    if not pivot_grp or not cmds.objExists(pivot_grp):
        return

    callback = _create_auto_key_callback(settings_node)

    job_ids = []
    for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
        attr_path = f"{pivot_grp}.{attr}"
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

    # Clean up scriptJobs
    cleanup_auto_key(settings_node)
    _cleanup_pivot_mode_monitor(settings_node)

    # Toggle off first if active
    if is_rig_active(settings_node):
        toggle_off(settings_node)

    # Delete align_grp (deletes pivot_grp and settings node too since they're children)
    align_grp = nodes["align_grp"]
    if align_grp and cmds.objExists(align_grp):
        cmds.delete(align_grp)

    # Clean up any orphaned nodes
    pivot_grp = nodes["pivot_grp"]
    if pivot_grp and cmds.objExists(pivot_grp):
        cmds.delete(pivot_grp)

    if cmds.objExists(settings_node):
        cmds.delete(settings_node)

    # Remove any remaining constraints on control
    if control and cmds.objExists(control):
        constraints = cmds.listRelatives(control, type="parentConstraint") or []
        for c in constraints:
            if CONSTRAINT_SUFFIX in c or TOOL_PREFIX in c:
                cmds.delete(c)

    return True, f"Deleted temp pivot for '{control}'."


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
        height=520
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
        label="Simplified pivot system for animation",
        align="left",
        font="smallPlainLabelFont",
        height=16
    )
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    cmds.text(
        label="1. Select control, click 'Create Temp Pivot'\n"
              "2. Move pivot to desired location\n"
              "3. Switch to Translate/Rotate to apply\n"
              "4. Toggle OFF to key control, Toggle ON to reuse",
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
        columnWidth2=(80, 235)
    )

    state_indicator = cmds.button(
        label="READY",
        width=75,
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
    # CREATE TEMP PIVOT
    # ==========================================

    cmds.frameLayout(
        label="Create Temp Pivot",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8
    )

    cmds.columnLayout(adjustableColumn=True, rowSpacing=6)

    cmds.text(
        label="Select a control, then create the temp pivot.\n"
              "Pivot mode will activate automatically:",
        align="left",
        font="smallPlainLabelFont",
        height=32
    )

    create_btn = cmds.button(
        label="Create Temp Pivot",
        height=36,
        backgroundColor=UI_COLORS["accent"],
        annotation=TOOLTIPS["create_btn"]
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

    edit_pivot_btn = cmds.button(
        label="Edit Temp Pivot",
        height=32,
        backgroundColor=UI_COLORS["pivot_mode"],
        annotation=TOOLTIPS["edit_pivot_btn"]
    )

    cmds.separator(height=8, style="in")

    cmds.text(label="Selection:", align="left", font="smallBoldLabelFont", height=18)

    select_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(160, 160)
    )

    select_pivot_btn = cmds.button(
        label="Select Temp Pivot",
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
        label="Delete Temp Pivot",
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
        label="Active Temp Pivots",
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
        text="Ready. Select a control and click 'Create Temp Pivot'."
    )

    cmds.setParent("..")

    cmds.separator(height=16, style="none")

    # ==========================================
    # CALLBACKS
    # ==========================================

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
        if _skip_list_refresh[0]:
            return

        selected_control = None
        if preserve_selection:
            selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
            if selected_items:
                selected_control = selected_items[0].split(" [")[0]

        cmds.textScrollList(rig_list, edit=True, removeAll=True)
        rigs = get_all_pivot_rigs()
        for settings in sorted(rigs):
            nodes = get_rig_nodes(settings)
            control = nodes["control"] or "?"

            if is_in_pivot_mode(settings):
                status = " [PIVOT]"
            elif is_rig_active(settings):
                status = " [ON]"
            else:
                status = " [OFF]"

            cmds.textScrollList(rig_list, edit=True, append=f"{control}{status}")

        if selected_control:
            all_items = cmds.textScrollList(rig_list, query=True, allItems=True) or []
            for item in all_items:
                if item.startswith(selected_control + " ["):
                    cmds.textScrollList(rig_list, edit=True, selectItem=item)
                    break

    def update_status() -> None:
        sel = cmds.ls(selection=True, type="transform") or []

        selected_settings = None

        for item in sel:
            # Check for pivot group
            if PIVOT_GRP_SUFFIX in item:
                prefix = item.replace(PIVOT_GRP_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    selected_settings = possible_settings
                break
            # Check for align group
            if ALIGN_GRP_SUFFIX in item:
                prefix = item.replace(ALIGN_GRP_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    selected_settings = possible_settings
                break
            # Check if it's a control with a rig
            rig = get_rig_for_control(item)
            if rig:
                selected_settings = rig
                break

        if selected_settings:
            nodes = get_rig_nodes(selected_settings)
            control = nodes["control"]
            cmds.text(selection_text, edit=True, label=f"Control: {control}")

            if is_in_pivot_mode(selected_settings):
                cmds.button(state_indicator, edit=True, label="PIVOT", backgroundColor=UI_COLORS["pivot_mode"])
            elif is_rig_active(selected_settings):
                cmds.button(state_indicator, edit=True, label="ON", backgroundColor=UI_COLORS["success"])
            else:
                cmds.button(state_indicator, edit=True, label="OFF", backgroundColor=UI_COLORS["off_state"])
        elif sel:
            cmds.text(selection_text, edit=True, label=f"Selected: {sel[0]}")
            cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])
        else:
            cmds.text(selection_text, edit=True, label="No control selected")
            cmds.button(state_indicator, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])

    def get_current_context():
        """Get current rig settings from selection."""
        sel = cmds.ls(selection=True, type="transform") or []

        for item in sel:
            if PIVOT_GRP_SUFFIX in item:
                prefix = item.replace(PIVOT_GRP_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    return ("rig", possible_settings)
            if ALIGN_GRP_SUFFIX in item:
                prefix = item.replace(ALIGN_GRP_SUFFIX, "")
                possible_settings = f"{prefix}{SETTINGS_SUFFIX}"
                if cmds.objExists(possible_settings):
                    return ("rig", possible_settings)
            rig = get_rig_for_control(item)
            if rig:
                return ("rig", rig)

        return (None, None)

    # Button callbacks

    def on_create(*args):
        sel = cmds.ls(selection=True, type="transform") or []
        controls = [s for s in sel if TOOL_PREFIX not in s]

        if not controls:
            log_message("Select a control first.", "warning")
            return

        control = controls[0]
        success, msg, settings = create_temp_pivot(control)
        log_message(msg, "success" if success else "warning")
        refresh_rig_list()
        update_status()

    def on_toggle(*args):
        ctx_type, ctx_node = get_current_context()

        if ctx_type == "rig":
            success, msg, is_active = toggle_pivot(ctx_node)
            log_message(msg, "success" if success else "error")
            if is_active:
                cmds.button(toggle_btn, edit=True, label="Toggle OFF", backgroundColor=UI_COLORS["success"])
            else:
                cmds.button(toggle_btn, edit=True, label="Toggle ON", backgroundColor=UI_COLORS["off_state"])
        else:
            log_message("No temp pivot found. Create one first.", "warning")

        refresh_rig_list()
        update_status()

    def on_edit_pivot(*args):
        ctx_type, ctx_node = get_current_context()

        if ctx_type == "rig":
            success, msg = edit_temp_pivot(ctx_node)
            log_message(msg, "success" if success else "error")
        else:
            log_message("No temp pivot found. Create one first.", "warning")

        refresh_rig_list()
        update_status()

    def on_delete(*args):
        ctx_type, ctx_node = get_current_context()
        if ctx_type == "rig":
            success, msg = delete_pivot_rig(ctx_node)
            log_message(msg, "success" if success else "error")
        else:
            log_message("No temp pivot found.", "warning")
        refresh_rig_list()
        update_status()

    def on_select_pivot(*args):
        ctx_type, ctx_node = get_current_context()
        if ctx_type == "rig":
            nodes = get_rig_nodes(ctx_node)
            pivot_grp = nodes["pivot_grp"]
            if pivot_grp and cmds.objExists(pivot_grp):
                cmds.select(pivot_grp)
                log_message(f"Selected: {pivot_grp}", "info")
        else:
            log_message("No temp pivot found.", "warning")

    def on_select_control(*args):
        ctx_type, ctx_node = get_current_context()
        if ctx_type == "rig":
            nodes = get_rig_nodes(ctx_node)
            control = nodes["control"]
            if control and cmds.objExists(control):
                cmds.select(control)
                log_message(f"Selected: {control}", "info")
        else:
            log_message("No control found.", "warning")

    def on_list_select(*args):
        selected_items = cmds.textScrollList(rig_list, query=True, selectItem=True) or []
        if selected_items:
            control_name = selected_items[0].split(" [")[0]
            settings = get_rig_for_control(control_name)
            if settings:
                nodes = get_rig_nodes(settings)
                pivot_grp = nodes["pivot_grp"]
                if pivot_grp and cmds.objExists(pivot_grp):
                    _skip_list_refresh[0] = True
                    try:
                        cmds.select(pivot_grp)
                    finally:
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

    cmds.button(create_btn, edit=True, command=on_create)
    cmds.button(toggle_btn, edit=True, command=on_toggle)
    cmds.button(edit_pivot_btn, edit=True, command=on_edit_pivot)
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
    log_message("Ready. Select a control and click 'Create Temp Pivot'.", "info")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    show()
