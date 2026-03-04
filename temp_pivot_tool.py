"""
Temp Pivot Tool for Autodesk Maya

A non-destructive, reusable temporary pivot system for animation.

TWO-NULL ARCHITECTURE (v7 - with pivot freezing):
  null_group_2: The POSITION ANCHOR - holds the pivot rig relative to control
  pivotOffsetGrp: Stores the offset from pivot positioning (created at Complete Setup)
  null_group_1: The TEMP PIVOT - animator-facing control with clean zeroed values

Hierarchy when ACTIVE (constraint ON):
    null_group_2 (aligned to control position)
      └ pivotOffsetGrp (baked pivot offset - translate only)
          └ null_group_1 (ANIMATOR PIVOT CTRL - rotatePivot=0, clean channels)
             └ [parentConstraint] → control

Workflow:
1. Select control, click "Create Pivot Locator" (Stage 1)
2. Tool automatically enters pivot adjust mode - move the pivot to desired position
3. Click "Complete Setup" (Stage 2) - freezes pivot offset, creates constraint
4. Rotate null_group_1 - control orbits around the custom pivot point (auto-keys applied)
5. Toggle OFF - anchor preserves position, constraint deleted, control free to move
6. Move control to new position
7. Toggle ON - anchor realigns to control, constraint recreated

Features:
- Pivot freezing: Offset group absorbs pivot position so animator sees clean 0 values
- Undo-safe: Major operations wrapped in undo chunks with undo/redo guard
- Auto-key: When you transform null_group_1, keyframes are automatically set on the control
- World-matrix alignment: Proper world-space alignment using full transformation matrix
- Robust selection: Works on shapes, namespaced nodes, props, locators
- Dockable UI: workspaceControl support for Maya 2017+
- Constraint validation: Warns if control has existing constraints

Author: David Shepstone
License: MIT
Version: 7.1.0

Upgrade notes from v6:
  - null_group_1 now has zeroed rotatePivot/scalePivot (pivot offset baked into pivotOffsetGrp)
  - New pivotOffsetGrp node tracked on settings as "pivotOffsetGrp"
  - UI uses workspaceControl for docking (falls back to window)
  - Undo chunks wrap all major operations
  - Selection resolves shape nodes to parent transforms automatically
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
WORKSPACE_CONTROL_NAME = "TempPivotToolWorkspaceControl"
WINDOW_TITLE = "Temp Pivot Tool"
TOOL_PREFIX = "TMP"

# Node naming convention
NULL_GRP_1_SUFFIX = f"_{TOOL_PREFIX}_pivot"      # TEMP PIVOT - animator-facing control
NULL_GRP_2_SUFFIX = f"_{TOOL_PREFIX}_anchor"     # POSITION ANCHOR - holds pivot relative to control
OFFSET_GRP_SUFFIX = f"_{TOOL_PREFIX}_pivotOffset"  # Offset group - stores baked pivot offset
SETTINGS_SUFFIX = f"_{TOOL_PREFIX}_settings"
CONSTRAINT_SUFFIX = f"_{TOOL_PREFIX}_parentConstraint"

# Message attribute name stored on the owner control pointing at the settings node
MSG_ATTR_SETTINGS = "tmpPivotSettings"

# All known TMP suffixes for legacy owner resolution (longest first)
_ALL_TMP_SUFFIXES = [
    CONSTRAINT_SUFFIX,       # _TMP_parentConstraint
    OFFSET_GRP_SUFFIX,       # _TMP_pivotOffset
    f"{NULL_GRP_1_SUFFIX}_ringX",
    f"{NULL_GRP_1_SUFFIX}_ringY",
    f"{NULL_GRP_1_SUFFIX}_ringZ",
    f"{NULL_GRP_1_SUFFIX}_loc",
    SETTINGS_SUFFIX,         # _TMP_settings
    NULL_GRP_2_SUFFIX,       # _TMP_anchor
    NULL_GRP_1_SUFFIX,       # _TMP_pivot
]

# Auto-key scriptJob storage (keyed by settings node name)
_auto_key_jobs: Dict[str, List[int]] = {}

# UI scriptJob IDs — killed and recreated on each _build_ui() call
_ui_script_jobs: List[int] = []

# Global UI control registry.  Callbacks resolve control paths from this
# dict instead of capturing local variables in closures.  This means a
# stale scriptJob can never reference a deleted control path — it will
# either see the *current* path (valid) or an empty dict (harmless).
_ui: Dict[str, str] = {}

# Undo guard - prevents auto-key from firing during undo/redo
_is_undoing: bool = False
_undo_guard_jobs: List[int] = []

# Debug logging — set to True for verbose output in the Script Editor
TMP_PIVOT_DEBUG = False


def _kill_ui_script_jobs() -> None:
    """Kill all tracked UI scriptJobs and clear the tracking list.

    Safe to call multiple times, from any context (show, on_close,
    _build_ui, _rebuild_workspace_ui).
    """
    global _ui_script_jobs
    for job_id in _ui_script_jobs:
        try:
            if cmds.scriptJob(exists=job_id):
                cmds.scriptJob(kill=job_id, force=True)
        except RuntimeError:
            pass
    _ui_script_jobs = []


def _ui_exists(*keys: str) -> bool:
    """Return True only if every *key* is in ``_ui`` and its control exists."""
    for k in keys:
        ctrl = _ui.get(k)
        if not ctrl or not cmds.objExists(ctrl):
            return False
    return True


def _debug_log(msg: str) -> None:
    """Print a debug message if TMP_PIVOT_DEBUG is enabled."""
    if TMP_PIVOT_DEBUG:
        print(f"[TMP_PIVOT_DEBUG] {msg}")

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
        "1. Freezes the pivot offset into an offset group\n"
        "2. Creates parentConstraint: null_group_1 → control\n"
        "3. After this, rotating null_group_1 will orbit the control\n"
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
    "adjust_pivot_btn": (
        "Adjust the pivot position on an existing rig.\n\n"
        "1. Toggles OFF (if active) so the control is free\n"
        "2. Enters pivot adjust mode (same as Stage 1 D-key mode)\n"
        "3. Move the pivot to a new position\n"
        "4. Click 'Apply Adjustment' to re-freeze and reactivate"
    ),
    "apply_adjustment_btn": (
        "Apply the repositioned pivot and reactivate the rig.\n\n"
        "Re-freezes the pivot offset into the offset group\n"
        "and toggles the constraint back ON so you can\n"
        "immediately rotate around the new pivot point."
    ),
}


# -----------------------------
# Undo Guard
# -----------------------------

def _on_undo_start():
    """Called when Maya begins an undo operation."""
    global _is_undoing
    _is_undoing = True


def _on_undo_end():
    """Called when Maya finishes an undo/redo operation."""
    global _is_undoing
    _is_undoing = False


def _setup_undo_guard():
    """Install global scriptJobs to detect undo/redo and set the guard flag."""
    global _undo_guard_jobs
    _teardown_undo_guard()

    job1 = cmds.scriptJob(event=["Undo", _on_undo_end], protected=True)
    job2 = cmds.scriptJob(event=["Redo", _on_undo_end], protected=True)
    # undoSuppress fires at the start of an undo chunk processing
    # We use timeChanged as a fallback reset since Maya doesn't have a direct "UndoStart" event
    _undo_guard_jobs = [job1, job2]


def _teardown_undo_guard():
    """Remove undo guard scriptJobs."""
    global _undo_guard_jobs
    for jid in _undo_guard_jobs:
        if cmds.scriptJob(exists=jid):
            cmds.scriptJob(kill=jid, force=True)
    _undo_guard_jobs = []


# -----------------------------
# Utility Functions
# -----------------------------

def _sanitize_name(name: str) -> str:
    """Create a safe prefix from a control name."""
    safe = name.split(":")[-1]
    safe = safe.replace("|", "_").replace(" ", "_")
    return safe


def _resolve_transform(node: str) -> str:
    """
    Resolve a node to its transform.

    If the user selected a shape node (nurbsCurve, locator, mesh, etc.),
    return the parent transform. Otherwise return the node itself.

    Handles:
    - nurbsCurve shapes
    - locator shapes
    - mesh shapes
    - any other shape node
    - namespaced nodes
    - full DAG paths
    """
    if not cmds.objExists(node):
        return node

    node_type = cmds.nodeType(node)

    # If it's a shape node, get the parent transform
    if cmds.objectType(node, isAType="shape"):
        parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
        if parents:
            return parents[0]

    return node


def _is_tmp_node(node: str) -> bool:
    """Return True if *node* belongs to a TMP pivot rig (by name convention)."""
    short = node.split("|")[-1]
    return f"_{TOOL_PREFIX}_" in short


def _find_settings_for_node(node: str) -> Optional[str]:
    """Find the TMP settings node associated with *any* node (TMP or control).

    Resolution order:
    1. If *node* itself is a settings node, return it.
    2. If *node* has ``targetControl`` attr (it's a pivot null), derive settings.
    3. Walk parent hierarchy looking for settings node or targetControl attr.
    4. Message attribute on the owner (``tmpPivotSettings``).
    5. Suffix-strip the name to guess the settings node.
    6. Scan all settings nodes in the scene for a matching rig member.
    """
    if not node or not cmds.objExists(node):
        return None

    node = _resolve_transform(node)
    short_name = node.split("|")[-1]

    # 1. Node IS the settings node
    if short_name.endswith(SETTINGS_SUFFIX):
        _debug_log(f"_find_settings_for_node: '{node}' is a settings node")
        return node

    # 2. Node has targetControl (pivot null or anchor)
    if cmds.attributeQuery("targetControl", node=node, exists=True):
        target = cmds.getAttr(f"{node}.targetControl") or ""
        if target:
            prefix = _sanitize_name(target)
            candidate = f"{prefix}{SETTINGS_SUFFIX}"
            resolved = _resolve_stored_name(candidate)
            if resolved:
                _debug_log(f"_find_settings_for_node: via targetControl → '{resolved}'")
                return resolved

    # 3. Walk parent hierarchy
    current = node
    for _ in range(10):
        parents = cmds.listRelatives(current, parent=True, fullPath=True) or []
        if not parents:
            break
        current = parents[0]
        current_short = current.split("|")[-1]
        if current_short.endswith(SETTINGS_SUFFIX):
            _debug_log(f"_find_settings_for_node: parent walk found '{current}'")
            return current
        if cmds.attributeQuery("targetControl", node=current, exists=True):
            target = cmds.getAttr(f"{current}.targetControl") or ""
            if target:
                prefix = _sanitize_name(target)
                candidate = f"{prefix}{SETTINGS_SUFFIX}"
                resolved = _resolve_stored_name(candidate)
                if resolved:
                    _debug_log(f"_find_settings_for_node: parent targetControl → '{resolved}'")
                    return resolved
        # Check siblings for settings node
        children = cmds.listRelatives(current, children=True, type="transform") or []
        for child in children:
            if child.endswith(SETTINGS_SUFFIX):
                _debug_log(f"_find_settings_for_node: sibling settings → '{child}'")
                return child

    # 4. Message attribute on owner
    owner = _resolve_owner_name_only(node)
    if owner and cmds.objExists(owner):
        if cmds.attributeQuery(MSG_ATTR_SETTINGS, node=owner, exists=True):
            connections = cmds.listConnections(
                f"{owner}.{MSG_ATTR_SETTINGS}", source=True, destination=False
            ) or []
            for conn in connections:
                if cmds.objExists(conn):
                    _debug_log(f"_find_settings_for_node: message attr → '{conn}'")
                    return conn

    # 5. Suffix-strip to guess settings name
    candidate_prefix = short_name
    for suffix in _ALL_TMP_SUFFIXES:
        if candidate_prefix.endswith(suffix):
            candidate_prefix = candidate_prefix[: -len(suffix)]
            break
    if candidate_prefix != short_name:
        possible = f"{candidate_prefix}{SETTINGS_SUFFIX}"
        resolved = _resolve_stored_name(possible)
        if resolved:
            _debug_log(f"_find_settings_for_node: suffix-strip → '{resolved}'")
            return resolved

    # 6. Control lookup — maybe *node* is the owner control itself
    settings = get_rig_for_control(node)
    if settings:
        return settings
    short_only = node.split("|")[-1]
    if short_only != node:
        settings = get_rig_for_control(short_only)
        if settings:
            return settings

    _debug_log(f"_find_settings_for_node: no settings found for '{node}'")
    return None


def _resolve_owner_name_only(node: str) -> Optional[str]:
    """Best-effort owner name derivation using only naming convention (no scene queries).

    This is a lightweight helper used by ``_find_settings_for_node`` to avoid
    infinite recursion (it does NOT call ``_find_settings_for_node``).
    """
    short = node.split("|")[-1]
    for suffix in _ALL_TMP_SUFFIXES:
        if short.endswith(suffix):
            candidate = short[: -len(suffix)]
            if candidate:
                return candidate
    return None


def _resolve_owner(node: str) -> Optional[str]:
    """Resolve any node (TMP, shape, or control) to the real owner control.

    Handles:
    - Shape nodes → parent transform first.
    - Settings nodes → read targetControl.
    - Any TMP hierarchy node → walk up / suffix-strip to find owner.
    - Controls → returned as-is if they exist.
    - Namespaced / referenced nodes.

    Returns the owner control name or ``None``.
    """
    if not node or not cmds.objExists(node):
        _debug_log(f"_resolve_owner: '{node}' does not exist")
        return None

    node = _resolve_transform(node)
    short_name = node.split("|")[-1]

    # Fast path: node is not a TMP node at all — it's likely the owner itself
    if not _is_tmp_node(node):
        _debug_log(f"_resolve_owner: '{node}' is not a TMP node → treating as owner")
        return node

    # --- TMP node path ---

    # A. Check if node has targetControl directly
    if cmds.attributeQuery("targetControl", node=node, exists=True):
        owner = cmds.getAttr(f"{node}.targetControl") or ""
        if owner and cmds.objExists(owner):
            _debug_log(f"_resolve_owner: targetControl on node → '{owner}'")
            return owner

    # B. Walk up parent hierarchy for targetControl
    current = node
    for _ in range(10):
        parents = cmds.listRelatives(current, parent=True, fullPath=True) or []
        if not parents:
            break
        current = parents[0]
        if cmds.attributeQuery("targetControl", node=current, exists=True):
            owner = cmds.getAttr(f"{current}.targetControl") or ""
            if owner and cmds.objExists(owner):
                _debug_log(f"_resolve_owner: parent walk targetControl → '{owner}'")
                return owner

    # C. Find the settings node and read targetControl from it
    settings = _find_settings_for_node(node)
    if settings and cmds.objExists(settings):
        if cmds.attributeQuery("targetControl", node=settings, exists=True):
            owner = cmds.getAttr(f"{settings}.targetControl") or ""
            if owner and cmds.objExists(owner):
                _debug_log(f"_resolve_owner: via settings '{settings}' → '{owner}'")
                return owner

    # D. Legacy fallback: strip known suffixes
    candidate = short_name
    for suffix in _ALL_TMP_SUFFIXES:
        if candidate.endswith(suffix):
            candidate = candidate[: -len(suffix)]
            break
    if candidate and candidate != short_name:
        if cmds.objExists(candidate):
            _debug_log(f"_resolve_owner: suffix-strip → '{candidate}'")
            return candidate
        # Try with wildcard namespace search
        matches = cmds.ls(f"*:{candidate}", type="transform") or []
        if len(matches) == 1:
            _debug_log(f"_resolve_owner: namespace search → '{matches[0]}'")
            return matches[0]
        if matches:
            _debug_log(f"_resolve_owner: ambiguous namespace, using first → '{matches[0]}'")
            return matches[0]

    _debug_log(f"_resolve_owner: could not resolve owner for '{node}'")
    return None


def _align_to_target_world_matrix(object_to_align: str, target: str) -> None:
    """
    Align object_to_align to target's world-space transform using world matrix.

    This method uses the full world transformation matrix to bypass rotation
    order issues and gimbal lock problems.

    Based on the MEL alignToFirstFixed() approach.
    """
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


def _align_translate_rotate(source: str, target: str) -> None:
    """
    Align *source* to *target*'s world-space position and rotation only.

    Unlike ``_align_to_target_world_matrix``, this does **not** copy scale,
    avoiding scale-compensation issues when children are reparented under
    the aligned node.
    """
    pos = cmds.xform(target, q=True, ws=True, t=True)
    rot = cmds.xform(target, q=True, ws=True, ro=True)
    cmds.xform(source, ws=True, t=pos)
    cmds.xform(source, ws=True, ro=rot)


def _match_translation_world(source: str, target: str) -> None:
    """Match only world-space translation using xform."""
    pos = cmds.xform(target, q=True, ws=True, t=True)
    cmds.xform(source, ws=True, t=pos)


def _has_constraints(node: str) -> Tuple[bool, List[str]]:
    """
    Check if a node has any constraints affecting it.

    Uses both listRelatives and listConnections for robust detection.

    Returns:
        Tuple of (has_constraints, list_of_constraint_names)
    """
    constraint_types = [
        "parentConstraint", "pointConstraint", "orientConstraint",
        "scaleConstraint", "aimConstraint"
    ]

    found_constraints = []

    # Method 1: Check children for constraint nodes
    for ctype in constraint_types:
        constraints = cmds.listRelatives(node, type=ctype) or []
        found_constraints.extend(constraints)

    # Method 2: Check connections to translate/rotate attributes
    for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
        attr_path = f"{node}.{attr}"
        if cmds.objExists(attr_path):
            connections = cmds.listConnections(
                attr_path, source=True, destination=False, plugs=False, type="constraint"
            ) or []
            for conn_node in connections:
                if conn_node not in found_constraints:
                    found_constraints.append(conn_node)

    # Method 3: Use listConnections with type filter for broader detection
    for ctype in constraint_types:
        conns = cmds.listConnections(node, type=ctype, source=True, destination=False) or []
        for c in conns:
            if c not in found_constraints:
                found_constraints.append(c)

    return len(found_constraints) > 0, found_constraints


def _safe_set_attr(node: str, attr: str, value) -> bool:
    """Set an attribute safely, skipping if locked or non-existent."""
    attr_path = f"{node}.{attr}"
    if not cmds.objExists(attr_path):
        return False
    if cmds.getAttr(attr_path, lock=True):
        return False
    try:
        cmds.setAttr(attr_path, value)
        return True
    except RuntimeError:
        return False


def _safe_set_key(node: str, attr: str, time=None) -> bool:
    """Set a keyframe safely, skipping if locked or non-keyable."""
    attr_path = f"{node}.{attr}"
    if not cmds.objExists(attr_path):
        return False
    if cmds.getAttr(attr_path, lock=True):
        return False
    # Check if the attribute is keyable
    if not cmds.getAttr(attr_path, keyable=True):
        return False
    try:
        kwargs = {"attribute": attr}
        if time is not None:
            kwargs["time"] = time
        cmds.setKeyframe(node, **kwargs)
        return True
    except RuntimeError:
        return False


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
    # Create the null group
    null_grp = cmds.group(empty=True, name=name)
    base_name = null_grp

    # Add visual circles for each axis
    for axis, axis_color, normal in [
        ("X", (1, 0.3, 0.3), (1, 0, 0)),
        ("Y", (0.3, 1, 0.3), (0, 1, 0)),
        ("Z", (0.3, 0.5, 1), (0, 0, 1))
    ]:
        circle = cmds.circle(
            name=f"{base_name}_ring{axis}",
            normal=normal,
            radius=0.5 * size,
            degree=3,
            sections=24,
            constructionHistory=False
        )[0]
        circle_shape = cmds.listRelatives(circle, shapes=True)[0]
        cmds.setAttr(f"{circle_shape}.overrideEnabled", 1)
        cmds.setAttr(f"{circle_shape}.overrideRGBColors", 1)
        cmds.setAttr(f"{circle_shape}.overrideColorR", axis_color[0])
        cmds.setAttr(f"{circle_shape}.overrideColorG", axis_color[1])
        cmds.setAttr(f"{circle_shape}.overrideColorB", axis_color[2])
        cmds.parent(circle_shape, null_grp, shape=True, relative=True)
        cmds.delete(circle)

    # Add a center locator shape for selection clarity
    loc = cmds.spaceLocator(name=f"{base_name}_loc")[0]
    loc_shape = cmds.listRelatives(loc, shapes=True)[0]
    cmds.setAttr(f"{loc_shape}.overrideEnabled", 1)
    cmds.setAttr(f"{loc_shape}.overrideRGBColors", 1)
    cmds.setAttr(f"{loc_shape}.overrideColorR", color[0])
    cmds.setAttr(f"{loc_shape}.overrideColorG", color[1])
    cmds.setAttr(f"{loc_shape}.overrideColorB", color[2])
    cmds.setAttr(f"{loc_shape}.localScaleX", 0.3 * size)
    cmds.setAttr(f"{loc_shape}.localScaleY", 0.3 * size)
    cmds.setAttr(f"{loc_shape}.localScaleZ", 0.3 * size)
    cmds.parent(loc_shape, null_grp, shape=True, relative=True)
    cmds.delete(loc)

    # Defensive cleanup: remove any leftover ring/locator transforms that failed to parent.
    for pattern in (f"{base_name}_ring*", f"{base_name}_loc"):
        for node in cmds.ls(pattern, type="transform") or []:
            parent = cmds.listRelatives(node, parent=True) or []
            if not parent or parent[0] != null_grp:
                cmds.delete(node)

    return null_grp


def _set_null_color(null_grp: str, color: Tuple[float, float, float]) -> None:
    """Set the color of the locator shape in a null group."""
    shapes = cmds.listRelatives(null_grp, shapes=True) or []
    for shape in shapes:
        if cmds.nodeType(shape) == "locator":
            cmds.setAttr(f"{shape}.overrideColorR", color[0])
            cmds.setAttr(f"{shape}.overrideColorG", color[1])
            cmds.setAttr(f"{shape}.overrideColorB", color[2])


def _set_shapes_visibility(node: str, visible: bool) -> None:
    """Show or hide all shape nodes under a transform.

    This hides the visual representation (ring curves, locator) while
    keeping the transform itself active so children remain visible.
    """
    if not node or not cmds.objExists(node):
        return
    shapes = cmds.listRelatives(node, shapes=True, fullPath=True) or []
    for shape in shapes:
        try:
            cmds.setAttr(f"{shape}.visibility", int(visible))
        except RuntimeError:
            pass


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
    """Find the settings node for a given control, if one exists.

    Checks the message attribute ``tmpPivotSettings`` first for O(1) lookup,
    then falls back to scanning all settings nodes (legacy scenes).
    """
    if not control or not cmds.objExists(control):
        return None

    # Fast path: message attribute on the owner
    if cmds.attributeQuery(MSG_ATTR_SETTINGS, node=control, exists=True):
        connections = cmds.listConnections(
            f"{control}.{MSG_ATTR_SETTINGS}", source=True, destination=False
        ) or []
        for conn in connections:
            if cmds.objExists(conn):
                _debug_log(f"get_rig_for_control: message attr → '{conn}'")
                return conn

    # Fallback: scan all settings nodes
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


def _resolve_stored_name(name: Optional[str]) -> Optional[str]:
    """Resolve a stored node name that may have become a stale DAG path.

    If *name* is a full DAG path that no longer exists but the short
    (leaf) name does exist uniquely, return the short name so the rest
    of the code can still find it.
    """
    if not name:
        return None
    if cmds.objExists(name):
        return name
    # Try the short (leaf) name
    short = name.split("|")[-1]
    matches = cmds.ls(short, long=True) or []
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Ambiguous — return first match (better than None)
        return matches[0]
    return None


def get_rig_nodes(settings_node: str) -> Dict[str, Optional[str]]:
    """Get all rig node names from a settings node.

    Stored names are resolved through ``_resolve_stored_name`` so that
    DAG-path changes caused by reparenting do not break look-ups.
    """
    result = {
        "settings": settings_node,
        "null_grp_1": None,  # Pivot (animator-facing control)
        "null_grp_2": None,  # Anchor (may not exist yet)
        "pivot_offset_grp": None,  # Offset group (created at complete_setup)
        "control": None,
        "constraint": None,
    }

    if not cmds.objExists(settings_node):
        return result

    if cmds.attributeQuery("nullGrp1", node=settings_node, exists=True):
        result["null_grp_1"] = _resolve_stored_name(
            cmds.getAttr(f"{settings_node}.nullGrp1") or None
        )
    if cmds.attributeQuery("nullGrp2", node=settings_node, exists=True):
        result["null_grp_2"] = _resolve_stored_name(
            cmds.getAttr(f"{settings_node}.nullGrp2") or None
        )
    if cmds.attributeQuery("pivotOffsetGrp", node=settings_node, exists=True):
        result["pivot_offset_grp"] = _resolve_stored_name(
            cmds.getAttr(f"{settings_node}.pivotOffsetGrp") or None
        )
    if cmds.attributeQuery("targetControl", node=settings_node, exists=True):
        result["control"] = _resolve_stored_name(
            cmds.getAttr(f"{settings_node}.targetControl") or None
        )
    if cmds.attributeQuery("constraintName", node=settings_node, exists=True):
        result["constraint"] = _resolve_stored_name(
            cmds.getAttr(f"{settings_node}.constraintName") or None
        )

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
    1. Resolve shape nodes to transforms
    2. Create null_group_1
    3. Align to the selected control using world matrix
    4. Automatically enter pivot adjust mode with Move tool active
    5. User moves the pivot to desired position
    6. Then user clicks "Complete Setup" for Stage 2

    Args:
        control: The control to create a pivot for

    Returns:
        Tuple of (success, message, null_grp_1_name)
    """
    # Resolve shape to transform
    control = _resolve_transform(control)

    if not cmds.objExists(control):
        return False, f"Control '{control}' not found.", None

    # Ensure it's a transform node
    if cmds.nodeType(control) != "transform":
        return False, f"'{control}' is not a transform node.", None

    # Guard: reject TMP nodes to prevent "pivot-of-pivot"
    if _is_tmp_node(control):
        owner = _resolve_owner(control)
        owner_msg = f" (owner: '{owner}')" if owner else ""
        _debug_log(f"create_pivot_locator: rejected TMP node '{control}'{owner_msg}")
        return False, f"'{control}' is a TMP rig node, not a control{owner_msg}. Select the original control instead.", None

    cmds.undoInfo(openChunk=True, chunkName="TMP_CreatePivotLocator")
    try:
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
    finally:
        cmds.undoInfo(closeChunk=True)


# =============================================================================
# STAGE 2: Complete Setup (with pivot freezing)
# =============================================================================

def complete_setup(null_grp_1: str) -> Tuple[bool, str, Optional[str]]:
    """
    STAGE 2: Complete the pivot rig setup with pivot freezing.

    Process:
    1. Get the target control from null_group_1
    2. Read the rotatePivot offset the user created during pivot positioning
    3. Create pivotOffsetGrp to absorb the offset
    4. Re-parent null_group_1 under pivotOffsetGrp with zeroed pivots
    5. Create parentConstraint: null_group_1 → control (maintainOffset)
    6. Create settings node

    The resulting hierarchy:
        pivotOffsetGrp (at world position of the pivot point)
            └ null_group_1 (zeroed pivots, clean channels)
                └ [parentConstraint] → control

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

    cmds.undoInfo(openChunk=True, chunkName="TMP_CompleteSetup")
    try:
        # Exit pivot adjust mode if active
        try:
            mel.eval('ctxEditMode;')
        except Exception:
            pass
        # Switch to select tool to clean up any modal state
        try:
            mel.eval('SelectTool;')
        except Exception:
            pass

        prefix = _sanitize_name(control)

        # =====================================================================
        # PIVOT FREEZING: Convert pivot offset into offset group hierarchy
        # =====================================================================

        # 1. Read the rotatePivot that the user set during Stage 1
        pivot_ws = cmds.xform(null_grp_1, q=True, ws=True, rotatePivot=True)

        # 2. Read null_grp_1's current world matrix (its position at control)
        null_grp_1_world_matrix = cmds.xform(null_grp_1, q=True, ws=True, m=True)

        # 3. Create the pivotOffsetGrp
        offset_grp = cmds.group(
            empty=True,
            name=f"{prefix}{OFFSET_GRP_SUFFIX}"
        )

        # 4. Position pivotOffsetGrp at the world-space pivot point
        #    This group sits at the exact location where the user placed the pivot
        cmds.xform(offset_grp, ws=True, t=pivot_ws)

        # Copy the rotation from null_grp_1 so the offset group is oriented
        # to match the control's rotation (same as null_grp_1's initial orientation)
        null_rot = cmds.xform(null_grp_1, q=True, ws=True, ro=True)
        cmds.xform(offset_grp, ws=True, ro=null_rot)

        # 5. Re-parent null_grp_1 under the offset group
        #    IMPORTANT: After parenting, the DAG path changes.  Re-query by
        #    listing children of offset_grp to get the new valid name.
        cmds.parent(null_grp_1, offset_grp)
        # Re-resolve null_grp_1's name after reparenting (DAG path changed)
        children = cmds.listRelatives(offset_grp, children=True, type="transform", fullPath=True) or []
        for child in children:
            short = child.split("|")[-1]
            if NULL_GRP_1_SUFFIX in short:
                null_grp_1 = child
                break

        # 6. Zero out null_grp_1's local transforms and pivots
        #    The offset is now stored in pivotOffsetGrp's position
        for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
            _safe_set_attr(null_grp_1, attr, 0)

        # Hide scale channels — they are not used by the pivot control
        # and hiding them keeps the Channel Box clean for the animator.
        for attr in ["sx", "sy", "sz"]:
            attr_path = f"{null_grp_1}.{attr}"
            if cmds.objExists(attr_path) and not cmds.getAttr(attr_path, lock=True):
                cmds.setAttr(attr_path, keyable=False, channelBox=False)

        # Zero the pivots - this is the key freeze operation
        cmds.xform(null_grp_1, objectSpace=True, pivots=[0, 0, 0])

        # =====================================================================
        # Create parentConstraint: null_group_1 → control (maintainOffset=ON)
        # =====================================================================
        constraint_name = f"{prefix}{CONSTRAINT_SUFFIX}"
        if cmds.objExists(constraint_name):
            cmds.delete(constraint_name)

        constraint = cmds.parentConstraint(
            null_grp_1, control,
            maintainOffset=True,
            name=constraint_name
        )[0]

        # =====================================================================
        # Create settings node
        # =====================================================================
        settings_node = cmds.createNode("transform", name=f"{prefix}{SETTINGS_SUFFIX}")
        cmds.setAttr(f"{settings_node}.visibility", 0)

        # Store references — use SHORT names (no pipe prefix) so they remain
        # valid after future reparenting operations (toggle on/off).
        null_grp_1_short = null_grp_1.split("|")[-1]
        offset_grp_short = offset_grp.split("|")[-1]
        _add_string_attr(settings_node, "targetControl", control)
        _add_string_attr(settings_node, "nullGrp1", null_grp_1_short)
        _add_string_attr(settings_node, "nullGrp2", "")  # Created on first toggle OFF
        _add_string_attr(settings_node, "pivotOffsetGrp", offset_grp_short)
        _add_string_attr(settings_node, "constraintName", constraint)
        _add_bool_attr(settings_node, "isActive", True)

        # Parent settings under offset_grp for organization
        cmds.parent(settings_node, offset_grp)
        # Re-resolve settings_node after reparenting (DAG path changed)
        settings_node = _resolve_stored_name(settings_node.split("|")[-1])

        # ------------------------------------------------------------------
        # Deterministic tracking: message attribute on owner → settings node
        # ------------------------------------------------------------------
        if not cmds.attributeQuery(MSG_ATTR_SETTINGS, node=control, exists=True):
            cmds.addAttr(control, longName=MSG_ATTR_SETTINGS, attributeType="message")
        # Disconnect any stale connection first
        existing_conns = cmds.listConnections(
            f"{control}.{MSG_ATTR_SETTINGS}", source=True, destination=False, plugs=True
        ) or []
        for plug in existing_conns:
            try:
                cmds.disconnectAttr(plug, f"{control}.{MSG_ATTR_SETTINGS}")
            except RuntimeError:
                pass
        cmds.connectAttr(f"{settings_node}.message", f"{control}.{MSG_ATTR_SETTINGS}", force=True)
        _debug_log(f"complete_setup: connected {settings_node}.message → {control}.{MSG_ATTR_SETTINGS}")

        # Mark null_grp_1 setup as complete
        cmds.setAttr(f"{null_grp_1}.setupComplete", True)

        # Update null_grp_1 color to indicate active (green)
        _set_null_color(null_grp_1, UI_COLORS["success"])

        # Set up auto-key for transform changes
        _setup_undo_guard()
        setup_auto_key(settings_node)

        # Select null_grp_1 with the Rotate manipulator so the user can
        # start orbiting immediately.  Double-deferred ensures it sticks
        # after all UI refreshes and SelectionChanged scriptJobs settle.
        cmds.select(null_grp_1)
        cmds.setToolTo("RotateSuperContext")
        _deferred_node = null_grp_1  # capture for lambda
        cmds.evalDeferred(
            lambda n=_deferred_node: cmds.evalDeferred(
                lambda: (
                    cmds.select(n, replace=True),
                    cmds.setToolTo("RotateSuperContext"),
                ) if cmds.objExists(n) else None
            )
        )

        return True, f"Setup complete! Rotate pivot null to orbit '{control}' around custom pivot. Auto-key enabled.", settings_node
    finally:
        cmds.undoInfo(closeChunk=True)


# =============================================================================
# TOGGLE ON (Reactivate)
# =============================================================================

def toggle_on(settings_node: str) -> Tuple[bool, str]:
    """
    Reactivate the temp pivot system.

    Process:
    1. Realign null_group_2 to control's current world position/rotation
    2. Move pivotOffsetGrp under null_group_2
    3. Reset null_group_1's LOCAL transforms to zero
    4. Recreate parentConstraint: null_group_1 → control (maintainOffset)
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
    null_grp_1 = nodes["null_grp_1"]
    null_grp_2 = nodes["null_grp_2"]
    offset_grp = nodes["pivot_offset_grp"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."
    if not null_grp_1 or not cmds.objExists(null_grp_1):
        return False, "Pivot null (null_group_1) not found."
    if not null_grp_2 or not cmds.objExists(null_grp_2):
        return False, "Anchor null (null_group_2) not found. Cannot toggle ON."

    cmds.undoInfo(openChunk=True, chunkName="TMP_ToggleOn")
    try:
        # =====================================================================
        # Realign null_group_2 to control's current world position/rotation.
        # Use position+rotation only (no scale) to avoid scale-compensation
        # issues when children are reparented under null_grp_2.
        # =====================================================================
        _align_translate_rotate(null_grp_2, control)
        cmds.xform(null_grp_2, ws=False, s=[1, 1, 1])

        # =====================================================================
        # Handle v7 (with offset group) or v6 (without) hierarchy
        # =====================================================================
        if offset_grp and cmds.objExists(offset_grp):
            # v7 architecture - ensure offset_grp is under null_grp_2
            current_parent = cmds.listRelatives(offset_grp, parent=True)
            if not current_parent or current_parent[0] != null_grp_2:
                cmds.parent(offset_grp, null_grp_2)
            # Re-resolve names after reparenting (DAG paths changed)
            offset_grp = _resolve_stored_name(offset_grp.split("|")[-1])
            null_grp_1 = _resolve_stored_name(null_grp_1.split("|")[-1])

            # Reset null_grp_1 local transforms (offset is in the offset group)
            for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
                _safe_set_attr(null_grp_1, attr, 0)
        else:
            # v6 compatibility - null_grp_1 directly under null_grp_2
            current_parent = cmds.listRelatives(null_grp_1, parent=True)
            if not current_parent or current_parent[0] != null_grp_2:
                cmds.parent(null_grp_1, null_grp_2)
            # Re-resolve after reparenting
            null_grp_1 = _resolve_stored_name(null_grp_1.split("|")[-1])

            for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
                _safe_set_attr(null_grp_1, attr, 0)

        # =====================================================================
        # Recreate parentConstraint: null_group_1 → control
        # =====================================================================
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

        # =====================================================================
        # Show visibility — but hide the anchor's shapes so the user only
        # sees the pivot control (null_grp_1), not the anchor rings.
        # =====================================================================
        if null_grp_2 and cmds.objExists(null_grp_2):
            cmds.setAttr(f"{null_grp_2}.visibility", 1)
            _set_shapes_visibility(null_grp_2, False)
        if offset_grp and cmds.objExists(offset_grp):
            cmds.setAttr(f"{offset_grp}.visibility", 1)

        # Update null_grp_1 color to active (green)
        _set_null_color(null_grp_1, UI_COLORS["success"])

        # Set up auto-key for transform changes
        _setup_undo_guard()
        setup_auto_key(settings_node)

        # Select null_grp_1
        cmds.select(null_grp_1)

        return True, f"Pivot ON. Rotate pivot null to orbit '{control}'. Auto-key enabled."
    finally:
        cmds.undoInfo(closeChunk=True)


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
    4. Parent offset group (or null_group_1) under null_group_2
    5. Delete the constraint
    6. Hide visibility

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
    offset_grp = nodes["pivot_offset_grp"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."
    if not null_grp_1 or not cmds.objExists(null_grp_1):
        return False, "Pivot null (null_group_1) not found."

    cmds.undoInfo(openChunk=True, chunkName="TMP_ToggleOff")
    try:
        # =====================================================================
        # Clean up auto-key scriptJobs
        # =====================================================================
        cleanup_auto_key(settings_node)

        # =====================================================================
        # Delete the constraint FIRST (before creating/moving anchor)
        # =====================================================================
        if constraint and cmds.objExists(constraint):
            cmds.delete(constraint)

        # Also clean any other constraints from this tool
        constraints = cmds.listRelatives(control, type="parentConstraint") or []
        for c in constraints:
            if CONSTRAINT_SUFFIX in c or TOOL_PREFIX in c:
                cmds.delete(c)

        prefix = _sanitize_name(control)

        # =====================================================================
        # Create null_group_2 if it doesn't exist (first toggle OFF)
        # =====================================================================
        if not null_grp_2 or not cmds.objExists(null_grp_2):
            null_grp_2 = _create_visual_null(
                f"{prefix}{NULL_GRP_2_SUFFIX}",
                UI_COLORS["stage2"],  # Blue for anchor
                size=1.2  # Slightly larger to distinguish
            )
            # Store reference in settings
            cmds.setAttr(f"{settings_node}.nullGrp2", null_grp_2, type="string")

        # =====================================================================
        # Align null_group_2 to control's current world position/rotation.
        # Use position+rotation only (no scale) to avoid scale-compensation
        # issues when children are reparented under null_grp_2.
        # =====================================================================
        _align_translate_rotate(null_grp_2, control)
        cmds.xform(null_grp_2, ws=False, s=[1, 1, 1])

        # =====================================================================
        # Parent hierarchy under null_group_2
        # =====================================================================
        if offset_grp and cmds.objExists(offset_grp):
            # v7: parent offset_grp under null_grp_2
            current_parent = cmds.listRelatives(offset_grp, parent=True)
            if not current_parent or current_parent[0] != null_grp_2:
                cmds.parent(offset_grp, null_grp_2)
            # Re-resolve names after reparenting (DAG paths changed)
            offset_grp = _resolve_stored_name(offset_grp.split("|")[-1])
            null_grp_1 = _resolve_stored_name(null_grp_1.split("|")[-1])
        else:
            # v6 compatibility: parent null_grp_1 directly under null_grp_2
            current_parent = cmds.listRelatives(null_grp_1, parent=True)
            if not current_parent or current_parent[0] != null_grp_2:
                cmds.parent(null_grp_1, null_grp_2)
            # Re-resolve after reparenting
            null_grp_1 = _resolve_stored_name(null_grp_1.split("|")[-1])

        # =====================================================================
        # Reset null_group_1 local transforms
        # =====================================================================
        for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
            _safe_set_attr(null_grp_1, attr, 0)

        # Clear constraint reference and set inactive
        cmds.setAttr(f"{settings_node}.constraintName", "", type="string")
        cmds.setAttr(f"{settings_node}.isActive", False)

        # =====================================================================
        # Hide visibility
        # =====================================================================
        cmds.setAttr(f"{null_grp_2}.visibility", 0)

        # Update null_grp_1 color to inactive (orange)
        _set_null_color(null_grp_1, UI_COLORS["stage1"])

        # Select the control now being manipulated
        cmds.select(control, replace=True)

        return True, f"Pivot OFF. '{control}' is now free to move. Key if needed."
    finally:
        cmds.undoInfo(closeChunk=True)


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
# ADJUST PIVOT POSITION
# =============================================================================

def adjust_pivot(settings_node: str) -> Tuple[bool, str]:
    """Enter pivot-adjustment mode on an existing rig.

    Allows the user to visually reposition the pivot point without
    deleting and recreating the rig.  Works exactly like Stage 1:
    selects ``null_grp_1``, activates Move tool in pivot-editing mode
    (the **D-key** mode) so the user drags the pivot manipulator.

    After repositioning, the user clicks **Apply Adjustment** to
    re-freeze the offset and reactivate the constraint.

    Args:
        settings_node: The settings node for this rig.

    Returns:
        Tuple of (success, message).
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]
    offset_grp = nodes["pivot_offset_grp"]
    null_grp_1 = nodes["null_grp_1"]
    null_grp_2 = nodes["null_grp_2"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."
    if not null_grp_1 or not cmds.objExists(null_grp_1):
        return False, "Pivot null not found. Cannot adjust."

    cmds.undoInfo(openChunk=True, chunkName="TMP_AdjustPivot")
    try:
        # 1. Toggle off if active (delete constraint so control is free)
        if is_rig_active(settings_node):
            toggle_off(settings_node)
            # Re-resolve after DAG path changes
            nodes = get_rig_nodes(settings_node)
            offset_grp = nodes["pivot_offset_grp"]
            null_grp_1 = nodes["null_grp_1"]
            null_grp_2 = nodes["null_grp_2"]
            if not null_grp_1 or not cmds.objExists(null_grp_1):
                return False, "Pivot null lost after toggle off."

        # 2. Make rig visible so user can see the pivot control
        if null_grp_2 and cmds.objExists(null_grp_2):
            cmds.setAttr(f"{null_grp_2}.visibility", 1)
            _set_shapes_visibility(null_grp_2, False)
        if offset_grp and cmds.objExists(offset_grp):
            cmds.setAttr(f"{offset_grp}.visibility", 1)

        # 3. Color the pivot null yellow to indicate adjustment mode
        if null_grp_1 and cmds.objExists(null_grp_1):
            _set_null_color(null_grp_1, UI_COLORS["warning"])

        # 4. Select null_grp_1 and enter pivot-editing mode
        #    (same as Stage 1 — D-key / ctxEditMode with Move tool)
        cmds.evalDeferred(lambda n=null_grp_1: _enter_pivot_adjust_mode(n))

        _debug_log(
            f"adjust_pivot: entered adjust mode for '{control}', "
            f"selected '{null_grp_1}'"
        )

        return True, (
            f"Adjust mode for '{control}'. "
            "Move the pivot to a new position, then click 'Apply Adjustment'."
        )
    finally:
        cmds.undoInfo(closeChunk=True)


def apply_pivot_adjustment(settings_node: str) -> Tuple[bool, str]:
    """Apply a pivot repositioning and reactivate the rig.

    Re-freezes the pivot offset (same maths as Stage 2) and then
    toggles the rig back ON so the user can immediately rotate
    around the new pivot.

    Args:
        settings_node: The settings node for this rig.

    Returns:
        Tuple of (success, message).
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]
    null_grp_1 = nodes["null_grp_1"]
    offset_grp = nodes["pivot_offset_grp"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."
    if not null_grp_1 or not cmds.objExists(null_grp_1):
        return False, "Pivot null not found."
    if not offset_grp or not cmds.objExists(offset_grp):
        return False, "Offset group not found."

    cmds.undoInfo(openChunk=True, chunkName="TMP_ApplyPivotAdjustment")
    try:
        # Exit pivot adjust mode if active
        try:
            mel.eval('ctxEditMode;')
        except Exception:
            pass
        try:
            mel.eval('SelectTool;')
        except Exception:
            pass

        # -----------------------------------------------------------------
        # Re-freeze: update pivotOffsetGrp position from the new pivot
        # -----------------------------------------------------------------
        pivot_ws = cmds.xform(null_grp_1, q=True, ws=True, rotatePivot=True)

        # Move offset_grp to the new world-space pivot position
        cmds.xform(offset_grp, ws=True, t=pivot_ws)

        # Copy rotation from null_grp_1 (same orient as before)
        null_rot = cmds.xform(null_grp_1, q=True, ws=True, ro=True)
        cmds.xform(offset_grp, ws=True, ro=null_rot)

        # Zero null_grp_1's local transforms and pivots (freeze)
        for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
            _safe_set_attr(null_grp_1, attr, 0)
        cmds.xform(null_grp_1, objectSpace=True, pivots=[0, 0, 0])

        _debug_log(
            f"apply_pivot_adjustment: re-froze pivot at {pivot_ws} "
            f"for '{control}'"
        )

        # -----------------------------------------------------------------
        # Toggle ON to reactivate constraint
        # -----------------------------------------------------------------
        success, msg = toggle_on(settings_node)
        if not success:
            return False, f"Re-freeze done but toggle ON failed: {msg}"

        return True, (
            f"Pivot adjusted and reactivated for '{control}'. "
            "Rotate the pivot null to orbit around the new pivot point."
        )
    finally:
        cmds.undoInfo(closeChunk=True)


# =============================================================================
# KEY CONTROL
# =============================================================================

def key_control(settings_node: str) -> Tuple[bool, str]:
    """Set keyframes on the control's translate and rotate, skipping locked/non-keyable attrs."""
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]

    if not control or not cmds.objExists(control):
        return False, f"Control '{control}' not found."

    current_time = cmds.currentTime(query=True)
    keyed_attrs = []

    for attr in ["tx", "ty", "tz", "rx", "ry", "rz"]:
        if _safe_set_key(control, attr, time=current_time):
            keyed_attrs.append(attr)

    if keyed_attrs:
        return True, f"Keyed {len(keyed_attrs)} attrs on '{control}' at frame {current_time}."
    else:
        return False, f"Could not key any attributes on '{control}' (locked or non-keyable)."


# =============================================================================
# AUTO-KEY MANAGEMENT
# =============================================================================

def _create_auto_key_callback(settings_node: str):
    """Create a callback function for auto-keying that captures the settings node.

    Undo is suppressed (``stateWithoutFlush=False``) so that the
    setKeyframe call does NOT create a separate undo entry.  This lets
    the user undo a single rotation in one step instead of having to
    undo twice (once for the key, once for the rotation).
    """
    def auto_key_callback():
        # Guard: skip if we are in an undo/redo operation
        global _is_undoing
        if _is_undoing:
            return

        # Only key if the rig is still active
        if cmds.objExists(settings_node) and is_rig_active(settings_node):
            # Suppress undo so the keyframe merges with the user's
            # manipulation command rather than creating a second entry.
            cmds.undoInfo(stateWithoutFlush=False)
            try:
                key_control(settings_node)
            finally:
                cmds.undoInfo(stateWithoutFlush=True)
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
    """Delete the pivot rig completely.

    Idempotent — safe to call even if some rig nodes are already gone.
    Handles referenced nodes gracefully (warns instead of erroring).
    Cleans up the message attribute on the owner control.
    """
    if not cmds.objExists(settings_node):
        return False, "Settings node not found."

    nodes = get_rig_nodes(settings_node)
    control = nodes["control"]
    _debug_log(f"delete_pivot_rig: settings='{settings_node}', control='{control}'")

    cmds.undoInfo(openChunk=True, chunkName="TMP_DeletePivotRig")
    try:
        # Clean up auto-key scriptJobs (in case they exist)
        cleanup_auto_key(settings_node)

        # Toggle off first (deletes constraint)
        if is_rig_active(settings_node):
            toggle_off(settings_node)

        # Helper to delete a node safely (handles referenced / locked nodes)
        def _safe_delete(node_name: str) -> None:
            if not node_name or not cmds.objExists(node_name):
                return
            try:
                if cmds.referenceQuery(node_name, isNodeReferenced=True):
                    _debug_log(f"delete_pivot_rig: '{node_name}' is referenced, skipping delete")
                    cmds.warning(f"Cannot delete referenced node '{node_name}'. Remove the reference first.")
                    return
            except RuntimeError:
                pass  # not referenced
            try:
                cmds.delete(node_name)
                _debug_log(f"delete_pivot_rig: deleted '{node_name}'")
            except RuntimeError as exc:
                cmds.warning(f"Could not delete '{node_name}': {exc}")

        # Re-query nodes after toggle_off (DAG paths may have changed)
        nodes = get_rig_nodes(settings_node) if cmds.objExists(settings_node) else nodes

        # Delete null_group_2 (which parents everything else in v7)
        _safe_delete(nodes.get("null_grp_2"))

        # Delete offset group if it wasn't parented under null_group_2
        _safe_delete(nodes.get("pivot_offset_grp"))

        # Delete null_group_1 if it wasn't parented under null_group_2 or offset_grp
        _safe_delete(nodes.get("null_grp_1"))

        # Clean up orphaned settings node
        _safe_delete(settings_node)

        # Remove any remaining TMP constraints on the control
        if control and cmds.objExists(control):
            constraints = cmds.listRelatives(control, type="parentConstraint") or []
            for c in constraints:
                if CONSTRAINT_SUFFIX in c or TOOL_PREFIX in c:
                    _safe_delete(c)

        # ------------------------------------------------------------------
        # Clean up message attribute on owner
        # ------------------------------------------------------------------
        if control and cmds.objExists(control):
            if cmds.attributeQuery(MSG_ATTR_SETTINGS, node=control, exists=True):
                try:
                    cmds.deleteAttr(f"{control}.{MSG_ATTR_SETTINGS}")
                    _debug_log(f"delete_pivot_rig: removed {MSG_ATTR_SETTINGS} from '{control}'")
                except RuntimeError:
                    pass  # may be locked / referenced

        return True, f"Deleted pivot rig for '{control}'."
    finally:
        cmds.undoInfo(closeChunk=True)


def delete_pending_pivot(null_grp_1: str) -> Tuple[bool, str]:
    """Delete a pending (Stage 1) pivot null."""
    if not cmds.objExists(null_grp_1):
        return False, "Pivot null not found."

    control = ""
    if cmds.attributeQuery("targetControl", node=null_grp_1, exists=True):
        control = cmds.getAttr(f"{null_grp_1}.targetControl")

    cmds.undoInfo(openChunk=True, chunkName="TMP_DeletePending")
    try:
        cmds.delete(null_grp_1)
        return True, f"Deleted pending pivot null for '{control}'."
    finally:
        cmds.undoInfo(closeChunk=True)


# =============================================================================
# UI IMPLEMENTATION (Dockable with workspaceControl)
# =============================================================================

def _build_ui(parent_layout: str) -> None:
    """
    Build the tool UI inside the given parent layout.

    This is separated from show() so the same UI can be placed inside
    either a workspaceControl or a plain window.

    Any existing children are removed first so the function is
    idempotent — safe to call from both show() and the uiScript
    callback without producing duplicates.
    """
    # Kill stale UI scriptJobs BEFORE deleting any UI elements.
    _kill_ui_script_jobs()

    # Invalidate the global UI registry so any in-flight deferred
    # callbacks see an empty dict and bail out.
    global _ui_script_jobs
    _ui.clear()

    # Clear existing children to prevent duplicate UI.  This handles
    # the case where Maya's uiScript and show() both call _build_ui,
    # or when the workspace control is restored on restart.
    cmds.setParent(parent_layout)
    existing = cmds.layout(parent_layout, query=True, childArray=True) or []
    for child in existing:
        try:
            cmds.deleteUI(child)
        except RuntimeError:
            pass

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
        "TMP_state_indicator",
        label="READY",
        width=60,
        height=28,
        backgroundColor=UI_COLORS["off_state"],
        enable=False
    )
    _ui["state_indicator"] = state_indicator

    selection_text = cmds.text(
        "TMP_selection_text",
        label="No control selected",
        align="left"
    )
    _ui["selection_text"] = selection_text

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
        "TMP_toggle_btn",
        label="Toggle ON / OFF",
        height=36,
        backgroundColor=UI_COLORS["success"],
        annotation=TOOLTIPS["toggle_btn"]
    )
    _ui["toggle_btn"] = toggle_btn

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

    adjust_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(160, 160)
    )

    adjust_pivot_btn = cmds.button(
        label="Adjust Pivot",
        height=28,
        backgroundColor=UI_COLORS["warning"],
        annotation=TOOLTIPS["adjust_pivot_btn"]
    )

    apply_adjustment_btn = cmds.button(
        label="Apply Adjustment",
        height=28,
        backgroundColor=UI_COLORS["success"],
        annotation=TOOLTIPS["apply_adjustment_btn"]
    )

    cmds.setParent("..")

    cmds.separator(height=4, style="none")

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
        "TMP_rig_list",
        height=100,
        allowMultiSelection=False
    )
    _ui["rig_list"] = rig_list

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
        "TMP_log_field",
        height=80,
        editable=False,
        wordWrap=True,
        text="Ready. Select a control and click 'Create Pivot Locator'."
    )
    _ui["log_field"] = log_field

    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # CLOSE TOOL
    # ==========================================

    close_btn = cmds.button(
        label="Close Tool",
        height=28,
        backgroundColor=(0.5, 0.5, 0.5),
        annotation="Close the Temp Pivot Tool window."
    )

    cmds.separator(height=8, style="none")

    # ==========================================
    # CALLBACKS
    # ==========================================

    # Flag to prevent list refresh during programmatic selection from the list
    # Using a list so we can modify it from nested functions
    _skip_list_refresh = [False]

    def log_message(message: str, msg_type: str = "info") -> None:
        _log = _ui.get("log_field")
        if not _log or not cmds.objExists(_log):
            return
        try:
            prefix_map = {"warning": "[!] ", "error": "[X] ", "success": "[OK] ", "info": ""}
            prefix = prefix_map.get(msg_type, "")
            current = cmds.scrollField(_log, query=True, text=True) or ""
            new_text = f"{prefix}{message}"
            if current and not current.startswith("Ready."):
                new_text = f"{current}\n{new_text}"
            cmds.scrollField(_log, edit=True, text=new_text)
            cmds.scrollField(_log, edit=True, insertionPosition=len(new_text))
        except RuntimeError:
            pass

    def refresh_rig_list(preserve_selection: bool = True) -> None:
        """Refresh the rig list, optionally preserving the current selection."""
        _rlist = _ui.get("rig_list")
        if not _rlist or not cmds.objExists(_rlist):
            return
        # Skip refresh if triggered by our own list selection
        if _skip_list_refresh[0]:
            return

        # --- Gather data (may raise, but we want to see those errors) ---
        rigs = get_all_pivot_rigs()
        entries: List[str] = []
        for settings in sorted(rigs):
            try:
                nodes = get_rig_nodes(settings)
                control = nodes["control"] or "?"
                active = is_rig_active(settings)
                status = " [ON]" if active else " [OFF]"
                entries.append(f"{control}{status}")
            except Exception:
                # Skip individual broken rigs rather than aborting the list
                entries.append(f"?{settings} [ERR]")

        # --- Update UI (guarded against stale controls) ---
        try:
            selected_control = None
            if preserve_selection:
                selected_items = cmds.textScrollList(_rlist, query=True, selectItem=True) or []
                if selected_items:
                    selected_control = selected_items[0].split(" [")[0]

            cmds.textScrollList(_rlist, edit=True, removeAll=True)
            for entry in entries:
                cmds.textScrollList(_rlist, edit=True, append=entry)

            if selected_control:
                all_items = cmds.textScrollList(_rlist, query=True, allItems=True) or []
                for item in all_items:
                    if item.startswith(selected_control + " ["):
                        cmds.textScrollList(_rlist, edit=True, selectItem=item)
                        break
        except RuntimeError:
            pass

    def _resolve_selection() -> List[str]:
        """
        Get the current selection resolved to transforms.

        Handles shape nodes, namespaced nodes, and full DAG paths.
        """
        raw_sel = cmds.ls(selection=True, long=True) or []
        resolved = []
        for item in raw_sel:
            resolved_node = _resolve_transform(item)
            if cmds.objExists(resolved_node) and cmds.nodeType(resolved_node) == "transform":
                resolved.append(resolved_node)
        return resolved

    def update_status() -> None:
        # Resolve controls from the global registry — never from closures.
        _sel_text = _ui.get("selection_text")
        _state_btn = _ui.get("state_indicator")
        if not _sel_text or not _state_btn:
            return
        if not cmds.objExists(_sel_text) or not cmds.objExists(_state_btn):
            return

        # --- Gather data (scene queries — errors should be visible) ---
        sel = _resolve_selection()

        selected_settings = None
        pending_pivot = None

        for item in sel:
            # --- TMP node: use robust resolution ---
            if _is_tmp_node(item):
                short_name = item.split("|")[-1]
                # Check for pending pivot (Stage 1)
                if short_name.endswith(NULL_GRP_1_SUFFIX):
                    if cmds.attributeQuery("setupComplete", node=item, exists=True):
                        if not cmds.getAttr(f"{item}.setupComplete"):
                            pending_pivot = item
                            break
                # Find settings via robust helper
                settings = _find_settings_for_node(item)
                if settings and cmds.objExists(settings):
                    selected_settings = settings
                    break
                # Last resort: resolve owner
                owner = _resolve_owner(item)
                if owner:
                    rig = get_rig_for_control(owner)
                    if rig:
                        selected_settings = rig
                        break
                continue

            # --- Normal control ---
            rig = get_rig_for_control(item)
            if rig:
                selected_settings = rig
                break
            short = item.split("|")[-1]
            if short != item:
                rig = get_rig_for_control(short)
                if rig:
                    selected_settings = rig
                    break
            pending = get_pending_pivot_for_control(item)
            if not pending and short != item:
                pending = get_pending_pivot_for_control(short)
            if pending:
                pending_pivot = pending

        # --- Update UI (guarded against stale controls) ---
        try:
            if selected_settings:
                nodes = get_rig_nodes(selected_settings)
                control = nodes["control"]
                active = is_rig_active(selected_settings)
                cmds.text(_sel_text, edit=True, label=f"Control: {control}")
                if active:
                    cmds.button(_state_btn, edit=True, label="ON", backgroundColor=UI_COLORS["success"])
                else:
                    cmds.button(_state_btn, edit=True, label="OFF", backgroundColor=UI_COLORS["stage1"])
            elif pending_pivot:
                short_pending = pending_pivot.split("|")[-1]
                if cmds.attributeQuery("targetControl", node=pending_pivot, exists=True):
                    control = cmds.getAttr(f"{pending_pivot}.targetControl")
                    cmds.text(_sel_text, edit=True, label=f"Pending: {control}")
                cmds.button(_state_btn, edit=True, label="STAGE1", backgroundColor=UI_COLORS["stage1"])
            elif sel:
                display_name = sel[0].split("|")[-1]
                cmds.text(_sel_text, edit=True, label=f"Selected: {display_name}")
                cmds.button(_state_btn, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])
            else:
                cmds.text(_sel_text, edit=True, label="No control selected")
                cmds.button(_state_btn, edit=True, label="READY", backgroundColor=UI_COLORS["off_state"])
        except RuntimeError:
            pass

    def get_current_context():
        """Get current rig settings or pending pivot.

        Uses ``_resolve_owner`` / ``_find_settings_for_node`` so that
        selecting *any* TMP rig node (pivot, anchor, offset, settings,
        ring shape, locator shape, etc.) correctly resolves to the
        owning control's rig — preventing "pivot-of-pivot" creation and
        ensuring toggle/delete always targets the right rig.
        """
        sel = _resolve_selection()

        for item in sel:
            # ----------------------------------------------------------
            # 1. If this IS a TMP node, resolve via robust helpers
            # ----------------------------------------------------------
            if _is_tmp_node(item):
                # Check for pending (Stage 1) pivot null first
                short_name = item.split("|")[-1]
                if short_name.endswith(NULL_GRP_1_SUFFIX):
                    if cmds.attributeQuery("setupComplete", node=item, exists=True):
                        if not cmds.getAttr(f"{item}.setupComplete"):
                            _debug_log(f"get_current_context: pending pivot '{item}'")
                            return ("pending", item)

                # Find the settings node for this TMP hierarchy
                settings = _find_settings_for_node(item)
                if settings and cmds.objExists(settings):
                    _debug_log(f"get_current_context: TMP node '{item}' → settings '{settings}'")
                    return ("rig", settings)

                # Last resort: resolve owner and look up their rig
                owner = _resolve_owner(item)
                if owner:
                    rig = get_rig_for_control(owner)
                    if rig:
                        _debug_log(f"get_current_context: owner '{owner}' → rig '{rig}'")
                        return ("rig", rig)

                _debug_log(f"get_current_context: TMP node '{item}' could not be resolved")
                continue

            # ----------------------------------------------------------
            # 2. Normal control — look up rig or pending pivot
            # ----------------------------------------------------------
            rig = get_rig_for_control(item)
            if rig:
                return ("rig", rig)
            # Try short name
            short = item.split("|")[-1]
            if short != item:
                rig = get_rig_for_control(short)
                if rig:
                    return ("rig", rig)

            pending = get_pending_pivot_for_control(item)
            if not pending and short != item:
                pending = get_pending_pivot_for_control(short)
            if pending:
                return ("pending", pending)

        return (None, None)

    # Button callbacks

    def on_create_pivot(*args):
        # Force Maya to process any pending events and refresh selection
        cmds.refresh(force=True)

        sel = _resolve_selection()

        # Resolve selection: if any item is a TMP node, resolve to its owner
        controls = []
        for s in sel:
            if _is_tmp_node(s):
                owner = _resolve_owner(s)
                if owner and not _is_tmp_node(owner):
                    _debug_log(f"on_create_pivot: resolved TMP '{s}' → owner '{owner}'")
                    controls.append(owner)
                else:
                    _debug_log(f"on_create_pivot: skipping unresolvable TMP node '{s}'")
            else:
                controls.append(s)

        if not controls:
            log_message("Select a control first (not a TMP rig node).", "warning")
            return

        # Use short name for the control (strip DAG path)
        control = controls[0]
        # Prefer short name if unambiguous
        short = control.split("|")[-1]
        if len(cmds.ls(short)) == 1:
            control = short

        success, msg, pivot = create_pivot_locator(control)
        log_message(msg, "success" if success else "warning")

        # Defer UI updates to avoid interfering with pivot adjust mode activation
        cmds.evalDeferred(refresh_rig_list)
        cmds.evalDeferred(update_status)

    def _deferred_select_pivot(node_name: str) -> None:
        """Select the pivot null via double-deferred to survive UI refreshes.

        A single evalDeferred can be overtaken by SelectionChanged
        scriptJob callbacks.  Double-deferring ensures we run AFTER
        those have settled.  Also activates the Rotate manipulator so
        the animator can immediately start rotating.
        """
        def _inner():
            if cmds.objExists(node_name):
                cmds.select(node_name, replace=True)
                cmds.setToolTo("RotateSuperContext")
        cmds.evalDeferred(lambda: cmds.evalDeferred(_inner))

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
            sel = _resolve_selection()
            found = False
            for item in sel:
                short = item.split("|")[-1]
                pending = get_pending_pivot_for_control(item)
                if not pending:
                    pending = get_pending_pivot_for_control(short)
                if pending:
                    success, msg, settings = complete_setup(pending)
                    log_message(msg, "success" if success else "error")
                    if success and settings:
                        nodes = get_rig_nodes(settings)
                        pivot_to_select = nodes["null_grp_1"]
                    found = True
                    break

            if not found:
                log_message("No pending pivot null found. Create one first.", "warning")

        refresh_rig_list()
        update_status()
        # Ensure pivot is selected after ALL UI updates have settled
        if pivot_to_select and cmds.objExists(pivot_to_select):
            _deferred_select_pivot(pivot_to_select)

    def on_toggle(*args):
        ctx_type, ctx_node = get_current_context()

        if ctx_type == "rig":
            success, msg, is_active = toggle_pivot(ctx_node)
            log_message(msg, "success" if success else "error")
            _tbtn = _ui.get("toggle_btn")
            if _tbtn and cmds.objExists(_tbtn):
                if is_active:
                    cmds.button(_tbtn, edit=True, label="Toggle OFF", backgroundColor=UI_COLORS["success"])
                else:
                    cmds.button(_tbtn, edit=True, label="Toggle ON", backgroundColor=UI_COLORS["stage1"])
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

    def on_adjust_pivot(*args):
        ctx_type, ctx_node = get_current_context()
        if ctx_type == "rig":
            success, msg = adjust_pivot(ctx_node)
            log_message(msg, "success" if success else "error")
            refresh_rig_list()
            update_status()
        elif ctx_type == "pending":
            log_message("Rig is still in Stage 1. Move the pivot directly, then Complete Setup.", "warning")
        else:
            log_message("No pivot rig found. Create one first.", "warning")

    def on_apply_adjustment(*args):
        ctx_type, ctx_node = get_current_context()
        if ctx_type == "rig":
            success, msg = apply_pivot_adjustment(ctx_node)
            log_message(msg, "success" if success else "error")
            refresh_rig_list()
            update_status()
        else:
            log_message("No pivot rig found to apply adjustment to.", "warning")

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
        _rlist = _ui.get("rig_list")
        if not _rlist or not cmds.objExists(_rlist):
            return
        selected_items = cmds.textScrollList(_rlist, query=True, selectItem=True) or []
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
        _rlist = _ui.get("rig_list")
        if not _rlist or not cmds.objExists(_rlist):
            return
        selected_items = cmds.textScrollList(_rlist, query=True, selectItem=True) or []
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
        _rlist = _ui.get("rig_list")
        if not _rlist or not cmds.objExists(_rlist):
            return
        selected_items = cmds.textScrollList(_rlist, query=True, selectItem=True) or []
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

    def on_close(*args):
        """Close the tool window / workspace control."""
        _kill_ui_script_jobs()
        _ui.clear()
        if (hasattr(cmds, "workspaceControl")
                and cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True)):
            cmds.deleteUI(WORKSPACE_CONTROL_NAME)
        elif cmds.window(WINDOW_NAME, exists=True):
            cmds.deleteUI(WINDOW_NAME)

    # Connect callbacks

    cmds.button(create_pivot_btn, edit=True, command=on_create_pivot)
    cmds.button(complete_setup_btn, edit=True, command=on_complete_setup)
    cmds.button(toggle_btn, edit=True, command=on_toggle)
    cmds.button(key_btn, edit=True, command=on_key)
    cmds.button(adjust_pivot_btn, edit=True, command=on_adjust_pivot)
    cmds.button(apply_adjustment_btn, edit=True, command=on_apply_adjustment)
    cmds.button(delete_btn, edit=True, command=on_delete)
    cmds.button(select_pivot_btn, edit=True, command=on_select_pivot)
    cmds.button(select_control_btn, edit=True, command=on_select_control)
    cmds.button(toggle_list_btn, edit=True, command=on_list_toggle)
    cmds.button(delete_list_btn, edit=True, command=on_list_delete)
    cmds.button(refresh_btn, edit=True, command=lambda *_: refresh_rig_list())
    cmds.button(close_btn, edit=True, command=on_close)

    cmds.textScrollList(rig_list, edit=True, selectCommand=on_list_select)
    cmds.textScrollList(rig_list, edit=True, doubleClickCommand=on_list_toggle)

    # Parent scriptJobs to main_scroll so they are automatically killed
    # when _build_ui() deletes existing children at the top of the function.
    # This is reimport-safe: even if the Python module is reloaded (which
    # resets _ui_script_jobs to []), the old scriptJobs die when their
    # parent UI element (main_scroll) is deleted by the rebuild.
    script_parent = main_scroll

    _ui_script_jobs.append(
        cmds.scriptJob(event=["SelectionChanged", update_status], parent=script_parent)
    )
    _ui_script_jobs.append(
        cmds.scriptJob(event=["SelectionChanged", refresh_rig_list], parent=script_parent)
    )
    # Keep the rig list current when scenes are opened / created.
    _ui_script_jobs.append(
        cmds.scriptJob(event=["SceneOpened", refresh_rig_list], parent=script_parent)
    )
    _ui_script_jobs.append(
        cmds.scriptJob(event=["NewSceneOpened", refresh_rig_list], parent=script_parent)
    )

    # Initialize — also defer so the list refreshes after the workspace
    # control is fully realized (handles uiScript rebuild timing).
    refresh_rig_list()
    update_status()
    cmds.evalDeferred(refresh_rig_list)
    cmds.evalDeferred(update_status)


def _rebuild_workspace_ui() -> None:
    """Rebuild the tool UI inside an existing workspaceControl.

    Called by Maya's ``uiScript`` mechanism whenever the workspace
    control is restored (e.g. after Maya restart or re-dock).
    """
    _setup_undo_guard()
    _build_ui(WORKSPACE_CONTROL_NAME)


def show() -> None:
    """
    Show the Temp Pivot Tool.

    Uses workspaceControl (Maya 2017+) so the tool can be docked, but
    launches as a **floating window** on first open.  If the user has
    previously docked it, re-running the script will restore and focus
    the existing panel instead of creating a duplicate.

    Falls back to a regular floating window if workspaceControl is
    unavailable (Maya < 2017).
    """
    # Install the undo guard
    _setup_undo_guard()

    # ------------------------------------------------------------------
    # Try workspaceControl (dockable) first
    # ------------------------------------------------------------------
    use_workspace = hasattr(cmds, "workspaceControl")

    if use_workspace:
        # Kill scriptJobs and invalidate UI registry BEFORE deleting the
        # workspace control so no stale callback can fire mid-teardown.
        _kill_ui_script_jobs()
        _ui.clear()

        # Always delete and recreate so the UI is fully rebuilt.
        # This guarantees the Close button (and everything else) is
        # present whether the panel is floating or docked.
        if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True):
            cmds.deleteUI(WORKSPACE_CONTROL_NAME)

        # Also clean up any leftover window with the old name
        if cmds.window(WINDOW_NAME, exists=True):
            cmds.deleteUI(WINDOW_NAME)

        # Create the workspace control — starts **floating** (not docked).
        # The user can dock it manually if desired; Maya remembers the
        # position on subsequent launches thanks to retain=True.
        #
        # uiScript is the command Maya calls to rebuild the UI contents
        # whenever the retained workspace control is restored (e.g.
        # after a Maya restart).  Without this, a retained workspace
        # control comes back as an empty shell with no buttons.
        cmds.workspaceControl(
            WORKSPACE_CONTROL_NAME,
            label=WINDOW_TITLE,
            retain=True,
            floating=True,
            initialWidth=340,
            initialHeight=620,
            minimumWidth=300,
            uiScript="import temp_pivot_tool; temp_pivot_tool._rebuild_workspace_ui()",
        )

        # Build the UI inside the workspace control.  _build_ui()
        # clears existing children first, so even if Maya's uiScript
        # already fired we won't get duplicates.
        _build_ui(WORKSPACE_CONTROL_NAME)

        # Raise / show
        cmds.workspaceControl(
            WORKSPACE_CONTROL_NAME, edit=True,
            visible=True,
        )
        return

    # ------------------------------------------------------------------
    # Fallback: plain floating window (Maya < 2017)
    # ------------------------------------------------------------------
    _kill_ui_script_jobs()
    _ui.clear()
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

    _build_ui(window)

    cmds.showWindow(window)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    show()
