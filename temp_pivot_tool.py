"""
Temp Pivot Tool for Autodesk Maya — Refactored

A clean, matrix-aware temporary pivot positioning system for animation.
Uses a helper transform approach: a visible control is created at the
target's current pivot position, the user repositions it, and the result
is baked back as the target's rotatePivot.

Helper hierarchy:
    animBot_Temp_Pivot_xForm   (world-space container group, locked)
        └── animBot_Temp_Pivot (visible control with colored ring curves)

Workflow:
    1. Select a transform, click Toggle (or call enable())
    2. Helper appears at the object's current rotatePivot
    3. Move the helper to the desired pivot location
    4. Click Toggle again (or call disable()) to bake and clean up

Placement modes:
    - Default:      helper snaps to target's current rotatePivot
    - Center:       helper snaps to target's bounding box center
    - Last Object:  helper snaps to another selected object's pivot
    - World Space:  helper moves to world origin

Compatibility:
    - Standard transform controls
    - Prop controls
    - Character rig controls
    - Simple nurbs curves
    - Objects without custom rig logic

Author: David Shepstone
License: MIT
Version: 8.0.0
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import maya.cmds as cmds
import maya.mel as mel

try:
    import maya.api.OpenMaya as om2
    _HAS_OM2 = True
except ImportError:
    _HAS_OM2 = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HELPER_NAME = "animBot_Temp_Pivot"
HELPER_XFORM_NAME = "animBot_Temp_Pivot_xForm"

WINDOW_NAME = "tempPivotToolWindow"
WORKSPACE_CONTROL_NAME = "TempPivotToolWorkspaceControl"
WINDOW_TITLE = "Temp Pivot Tool"

UI_COLORS = {
    "accent":   (0.36, 0.68, 0.93),   # Blue — header
    "active":   (0.20, 0.75, 0.45),   # Green — active state
    "inactive": (0.45, 0.45, 0.48),   # Gray — inactive / ready
    "warning":  (0.95, 0.77, 0.26),   # Yellow — edit mode
    "helper":   (0.95, 0.65, 0.25),   # Orange — helper color / bake
    "error":    (0.95, 0.35, 0.35),   # Red — errors
}

# Global UI control registry and scriptJob tracker
_ui: Dict[str, str] = {}
_ui_script_jobs: List[int] = []


# ---------------------------------------------------------------------------
# Matrix utilities (Maya API 2.0 with cmds fallback)
# ---------------------------------------------------------------------------

def _get_dag_path(node: str) -> "om2.MDagPath":
    """Get the MDagPath for a named node using Maya API 2.0."""
    sel = om2.MSelectionList()
    sel.add(node)
    return sel.getDagPath(0)


def _get_world_position(node: str) -> List[float]:
    """Get a node's world-space translation.

    Uses MFnTransform for precision when available.
    """
    if _HAS_OM2 and cmds.objExists(node):
        try:
            dag = _get_dag_path(node)
            fn = om2.MFnTransform(dag)
            t = fn.translation(om2.MSpace.kWorld)
            return [t.x, t.y, t.z]
        except Exception:
            pass
    return cmds.xform(node, q=True, ws=True, t=True)


def _get_world_rotate_pivot(node: str) -> List[float]:
    """Get a node's rotatePivot in world space.

    Uses MFnTransform for precision when available.
    """
    if _HAS_OM2 and cmds.objExists(node):
        try:
            dag = _get_dag_path(node)
            fn = om2.MFnTransform(dag)
            rp = fn.rotatePivot(om2.MSpace.kWorld)
            return [rp.x, rp.y, rp.z]
        except Exception:
            pass
    return cmds.xform(node, q=True, ws=True, rotatePivot=True)


def _get_world_matrix(node: str) -> List[float]:
    """Get a node's world matrix as a flat 16-float list.

    Uses MMatrix for precision when available.
    """
    if _HAS_OM2 and cmds.objExists(node):
        try:
            dag = _get_dag_path(node)
            mat = dag.inclusiveMatrix()
            # MMatrix stores row-major; flatten to 16 floats
            result = []
            for row in range(4):
                for col in range(4):
                    result.append(mat.getElement(row, col))
            return result
        except Exception:
            pass
    return cmds.xform(node, q=True, ws=True, m=True)


def _get_bounding_box_center(node: str) -> List[float]:
    """Get the world-space bounding box center of a node."""
    bbox = cmds.exactWorldBoundingBox(node)
    return [
        (bbox[0] + bbox[3]) / 2.0,
        (bbox[1] + bbox[4]) / 2.0,
        (bbox[2] + bbox[5]) / 2.0,
    ]


def _align_world_matrix(source: str, target: str) -> None:
    """Align source to target's full world-space transform via matrix copy.

    Copies translate, rotate, and scale from the world matrix.
    Avoids Euler-angle gimbal issues by operating on the matrix directly.
    """
    matrix = _get_world_matrix(target)
    cmds.xform(source, ws=True, m=matrix)


def _align_position_rotation(source: str, target: str) -> None:
    """Align source to target's world position and rotation only (no scale).

    Uses separate translate/rotate queries to avoid scale-compensation
    issues when children are present under the aligned node.
    """
    pos = cmds.xform(target, q=True, ws=True, t=True)
    rot = cmds.xform(target, q=True, ws=True, ro=True)
    cmds.xform(source, ws=True, t=pos)
    cmds.xform(source, ws=True, ro=rot)


# ---------------------------------------------------------------------------
# Node utilities
# ---------------------------------------------------------------------------

def _resolve_transform(node: str) -> str:
    """Resolve a shape node to its parent transform.

    If *node* is already a transform, returns it unchanged.
    Handles nurbsCurve, locator, mesh, and other shape types.
    """
    if not cmds.objExists(node):
        return node
    if cmds.objectType(node, isAType="shape"):
        parents = cmds.listRelatives(node, parent=True, fullPath=True) or []
        if parents:
            return parents[0]
    return node


def _is_helper_node(node: str) -> bool:
    """Return True if *node* belongs to the temp pivot helper."""
    short = node.split("|")[-1]
    return short.startswith("animBot_Temp_Pivot")


def _create_visual_control(name: str, size: float = 1.0) -> str:
    """Create a visible control with axis-colored ring curves and a locator.

    Returns the name of the created group node.  The shapes are parented
    under the group for clean selection and transform behavior.
    """
    grp = cmds.group(empty=True, name=name)

    # Three axis-colored rings for orientation reference
    for axis, color, normal in [
        ("X", (1.0, 0.3, 0.3), (1, 0, 0)),
        ("Y", (0.3, 1.0, 0.3), (0, 1, 0)),
        ("Z", (0.3, 0.5, 1.0), (0, 0, 1)),
    ]:
        circle = cmds.circle(
            name=f"{name}_ring{axis}",
            normal=normal,
            radius=0.5 * size,
            degree=3,
            sections=24,
            constructionHistory=False,
        )[0]
        shape = cmds.listRelatives(circle, shapes=True)[0]
        cmds.setAttr(f"{shape}.overrideEnabled", 1)
        cmds.setAttr(f"{shape}.overrideRGBColors", 1)
        cmds.setAttr(f"{shape}.overrideColorR", color[0])
        cmds.setAttr(f"{shape}.overrideColorG", color[1])
        cmds.setAttr(f"{shape}.overrideColorB", color[2])
        cmds.parent(shape, grp, shape=True, relative=True)
        cmds.delete(circle)

    # Center locator for easy picking
    loc = cmds.spaceLocator(name=f"{name}_loc")[0]
    loc_shape = cmds.listRelatives(loc, shapes=True)[0]
    cmds.setAttr(f"{loc_shape}.overrideEnabled", 1)
    cmds.setAttr(f"{loc_shape}.overrideRGBColors", 1)
    cmds.setAttr(f"{loc_shape}.overrideColorR", UI_COLORS["helper"][0])
    cmds.setAttr(f"{loc_shape}.overrideColorG", UI_COLORS["helper"][1])
    cmds.setAttr(f"{loc_shape}.overrideColorB", UI_COLORS["helper"][2])
    for ax in ("X", "Y", "Z"):
        cmds.setAttr(f"{loc_shape}.localScale{ax}", 0.3 * size)
    cmds.parent(loc_shape, grp, shape=True, relative=True)
    cmds.delete(loc)

    # Defensive cleanup: remove any orphaned transforms from shape reparenting
    for pattern in (f"{name}_ring*", f"{name}_loc"):
        for orphan in cmds.ls(pattern, type="transform") or []:
            par = cmds.listRelatives(orphan, parent=True) or []
            if not par or par[0] != grp:
                try:
                    cmds.delete(orphan)
                except RuntimeError:
                    pass

    return grp


# ---------------------------------------------------------------------------
# TempPivotTool — central class
# ---------------------------------------------------------------------------

class TempPivotTool:
    """Manages the temporary pivot helper workflow.

    Singleton class that tracks the active target, helper nodes,
    callbacks, and placement mode.  All user-facing operations return
    a ``(success, message)`` tuple for easy UI integration.

    Usage::

        tool = TempPivotTool.get()
        tool.toggle()              # create or remove helper
        tool.set_to_center()       # snap helper to bbox center
        tool.set_to_last_object()  # snap to another object
        tool.set_world_space()     # snap to origin
        tool.reset()               # restore original pivot
        tool.enter_edit_mode()     # re-select helper with move tool
        tool.bake_to_target()      # apply pivot without cleanup
        tool.cleanup()             # remove helper and callbacks
    """

    _instance: Optional["TempPivotTool"] = None

    @classmethod
    def get(cls) -> "TempPivotTool":
        """Return the singleton instance, creating it if necessary."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._target: Optional[str] = None
        self._helper: Optional[str] = None
        self._helper_xform: Optional[str] = None
        self._script_job_ids: List[int] = []
        self._active: bool = False
        self._original_pivot: Optional[List[float]] = None
        self._placement_mode: str = "default"

    # -- Properties --------------------------------------------------------

    @property
    def active(self) -> bool:
        """Whether the temp pivot helper is currently active."""
        return self._active

    @property
    def target(self) -> Optional[str]:
        """The current target transform name."""
        return self._target

    @property
    def helper(self) -> Optional[str]:
        """The visible helper control name."""
        return self._helper

    # -- Public operations -------------------------------------------------

    def toggle(self) -> Tuple[bool, str]:
        """Toggle the temp pivot on or off.

        If inactive, creates the helper.  If active, bakes and cleans up.
        """
        if self._active:
            return self.disable()
        return self.enable()

    def enable(self) -> Tuple[bool, str]:
        """Create helper at the selected object's pivot position.

        Validates selection, creates the helper hierarchy, snaps it
        to the target's rotatePivot, registers callbacks, and enters
        the move tool so the user can immediately reposition the helper.
        """
        # --- Validate selection ---
        sel = cmds.ls(selection=True, long=True) or []
        if not sel:
            return False, "Select a transform first."

        target = _resolve_transform(sel[0])
        if not cmds.objExists(target):
            return False, f"'{target}' does not exist."
        if cmds.nodeType(target) != "transform":
            return False, f"'{target}' is not a transform node."
        if _is_helper_node(target):
            return (
                False,
                "Cannot create a pivot on the helper itself. "
                "Select the original object instead.",
            )

        # If already active on the same target, just re-enter edit mode
        if self._active and self._target == target:
            self.enter_edit_mode()
            return True, f"Re-entering edit mode for '{self._short_name(target)}'."

        cmds.undoInfo(openChunk=True, chunkName="TempPivot_Enable")
        try:
            # Clean up any previous session first
            self.cleanup()

            self._target = target
            self._original_pivot = _get_world_rotate_pivot(target)

            # Create helper hierarchy and snap to target's pivot
            self._create_helper()
            self._snap_helper_to_pivot()

            # Register scene-level callbacks for cleanup
            self._register_callbacks()

            self._active = True
            self._placement_mode = "default"

            # Select helper and enter move tool for immediate interaction
            cmds.select(self._helper, replace=True)
            mel.eval("MoveTool")

            name = self._short_name(target)
            return (
                True,
                f"Temp pivot active for '{name}'. "
                "Move the helper to the desired pivot position.",
            )
        except Exception as exc:
            self.cleanup()
            return False, f"Failed to enable temp pivot: {exc}"
        finally:
            cmds.undoInfo(closeChunk=True)

    def disable(self) -> Tuple[bool, str]:
        """Bake the helper position back to the target and clean up.

        Reads the helper's world position, applies it as the target's
        rotatePivot and scalePivot, then removes all helper nodes and
        callbacks.
        """
        if not self._active:
            return False, "No active temp pivot to disable."

        cmds.undoInfo(openChunk=True, chunkName="TempPivot_Disable")
        try:
            target_name = self._short_name(self._target)
            self.bake_to_target()
            self.cleanup()

            return True, f"Pivot baked and helper removed for '{target_name}'."
        finally:
            cmds.undoInfo(closeChunk=True)

    def set_to_center(self) -> Tuple[bool, str]:
        """Move the helper to the bounding box center of the target.

        Uses ``cmds.exactWorldBoundingBox`` for accurate world-space
        bounding box computation.
        """
        ok, msg = self._validate_active()
        if not ok:
            return False, msg

        cmds.undoInfo(openChunk=True, chunkName="TempPivot_SetCenter")
        try:
            center = _get_bounding_box_center(self._target)
            cmds.xform(self._helper, ws=True, t=center)
            self._placement_mode = "center"
            return (
                True,
                f"Helper → bbox center "
                f"({center[0]:.2f}, {center[1]:.2f}, {center[2]:.2f})",
            )
        finally:
            cmds.undoInfo(closeChunk=True)

    def set_to_last_object(self) -> Tuple[bool, str]:
        """Move the helper to the last selected non-helper transform.

        Scans the current selection for a transform that is neither the
        helper nor the target, and snaps the helper to that object's
        rotatePivot.
        """
        ok, msg = self._validate_active()
        if not ok:
            return False, msg

        # Find a usable source object in the selection
        sel = cmds.ls(selection=True, long=True) or []
        source = None
        for s in sel:
            resolved = _resolve_transform(s)
            if (
                cmds.objExists(resolved)
                and cmds.nodeType(resolved) == "transform"
                and not _is_helper_node(resolved)
                and resolved != self._target
            ):
                source = resolved

        if not source:
            return (
                False,
                "Select another object to use as pivot source.",
            )

        cmds.undoInfo(openChunk=True, chunkName="TempPivot_SetLastObject")
        try:
            pos = _get_world_rotate_pivot(source)
            cmds.xform(self._helper, ws=True, t=pos)
            self._placement_mode = "last_object"
            short = source.split("|")[-1]
            return True, f"Helper → '{short}' pivot position."
        finally:
            cmds.undoInfo(closeChunk=True)

    def set_world_space(self) -> Tuple[bool, str]:
        """Move the helper to the world origin with identity rotation.

        Places the helper at (0, 0, 0) with no rotation, useful for
        resetting to a known world-aligned state.
        """
        ok, msg = self._validate_active()
        if not ok:
            return False, msg

        cmds.undoInfo(openChunk=True, chunkName="TempPivot_SetWorldSpace")
        try:
            cmds.xform(self._helper, ws=True, t=[0, 0, 0])
            cmds.xform(self._helper, ws=True, ro=[0, 0, 0])
            self._placement_mode = "world"
            return True, "Helper → world origin."
        finally:
            cmds.undoInfo(closeChunk=True)

    def reset(self) -> Tuple[bool, str]:
        """Reset the target's pivot to its original position.

        If the helper is still active, re-snaps it to the restored pivot.
        If the tool captured an original pivot on enable(), that value is
        used; otherwise the pivot is zeroed in object space.
        """
        if not self._target or not cmds.objExists(self._target):
            return False, "No target to reset."

        cmds.undoInfo(openChunk=True, chunkName="TempPivot_Reset")
        try:
            if self._original_pivot:
                cmds.xform(
                    self._target, ws=True, rotatePivot=self._original_pivot
                )
                cmds.xform(
                    self._target, ws=True, scalePivot=self._original_pivot
                )
            else:
                cmds.xform(self._target, os=True, pivots=[0, 0, 0])

            # Re-snap helper if still active
            if (
                self._active
                and self._helper
                and cmds.objExists(self._helper)
            ):
                self._snap_helper_to_pivot()

            self._placement_mode = "default"
            return True, f"Pivot reset for '{self._short_name(self._target)}'."
        finally:
            cmds.undoInfo(closeChunk=True)

    def enter_edit_mode(self) -> Tuple[bool, str]:
        """Select the helper and activate the move tool.

        Allows the user to reposition the temp pivot interactively.
        This is the primary way to adjust the helper after initial
        placement or after using a placement mode.
        """
        if not self._active or not self._helper:
            return False, "No active temp pivot to edit."
        if not cmds.objExists(self._helper):
            return False, "Helper node no longer exists."

        cmds.select(self._helper, replace=True)
        mel.eval("MoveTool")
        return True, "Edit mode — move the helper to reposition the pivot."

    def bake_to_target(self) -> Tuple[bool, str]:
        """Apply the helper's world position as the target's rotatePivot.

        Uses world-space position from the helper and applies it to both
        rotatePivot and scalePivot on the target.  This does NOT move the
        target visually — it only changes the center of rotation/scaling
        for future transform operations.
        """
        if not self._target or not cmds.objExists(self._target):
            return False, "Target no longer exists."
        if not self._helper or not cmds.objExists(self._helper):
            return False, "Helper no longer exists."

        # Read helper world position — this is the desired pivot location
        helper_pos = _get_world_position(self._helper)

        # Apply as both rotatePivot and scalePivot in world space
        cmds.xform(self._target, ws=True, rotatePivot=helper_pos)
        cmds.xform(self._target, ws=True, scalePivot=helper_pos)

        return (
            True,
            f"Pivot set to "
            f"({helper_pos[0]:.3f}, {helper_pos[1]:.3f}, {helper_pos[2]:.3f})",
        )

    def cleanup(self) -> None:
        """Remove all helper nodes, callbacks, and reset internal state.

        Safe to call multiple times.  Handles missing nodes gracefully
        and sweeps for any orphaned helper nodes that might have been
        left behind by interrupted operations or undo.
        """
        self._remove_callbacks()

        # Delete the xform group (which contains the helper as a child)
        if self._helper_xform and cmds.objExists(self._helper_xform):
            try:
                cmds.delete(self._helper_xform)
            except RuntimeError:
                pass

        # Safety sweep: remove any orphan helper nodes
        for name in (HELPER_XFORM_NAME, HELPER_NAME):
            if cmds.objExists(name):
                try:
                    cmds.delete(name)
                except RuntimeError:
                    pass

        # Also clean up any leftover ring/loc transforms
        for pattern in (f"{HELPER_NAME}_ring*", f"{HELPER_NAME}_loc*"):
            for node in cmds.ls(pattern, type="transform") or []:
                try:
                    cmds.delete(node)
                except RuntimeError:
                    pass

        self._helper = None
        self._helper_xform = None
        self._active = False

    # -- Internal helpers --------------------------------------------------

    def _validate_active(self) -> Tuple[bool, str]:
        """Check that the tool is active with valid target and helper."""
        if not self._active or not self._helper:
            return False, "No active temp pivot."
        if not self._target or not cmds.objExists(self._target):
            return False, "Target no longer exists."
        if not cmds.objExists(self._helper):
            return False, "Helper no longer exists."
        return True, ""

    def _create_helper(self) -> None:
        """Create the helper xform group and visible control.

        The xform group sits at world origin with locked transforms,
        providing a clean world-space parent.  The visible control
        (with ring curves) is parented under it.
        """
        # Ensure no duplicate helpers exist
        for name in (HELPER_XFORM_NAME, HELPER_NAME):
            if cmds.objExists(name):
                cmds.delete(name)

        # Container group at world origin
        self._helper_xform = cmds.group(empty=True, name=HELPER_XFORM_NAME)

        # Visible control with ring curves
        self._helper = _create_visual_control(HELPER_NAME, size=1.0)

        # Parent control under xform
        cmds.parent(self._helper, self._helper_xform)

        # Hide and lock scale channels (not useful for pivot placement)
        for node in (self._helper, self._helper_xform):
            for attr in ("sx", "sy", "sz"):
                attr_path = f"{node}.{attr}"
                if cmds.objExists(attr_path):
                    try:
                        cmds.setAttr(attr_path, keyable=False, channelBox=False)
                    except RuntimeError:
                        pass

        # Lock xform transforms so user only moves the inner control
        for attr in ("tx", "ty", "tz", "rx", "ry", "rz"):
            attr_path = f"{self._helper_xform}.{attr}"
            if cmds.objExists(attr_path):
                try:
                    cmds.setAttr(attr_path, lock=True)
                except RuntimeError:
                    pass

    def _snap_helper_to_pivot(self) -> None:
        """Position the helper at the target's current rotatePivot.

        Uses world-space rotatePivot for position and the target's
        world rotation for orientation.
        """
        if not self._target or not self._helper:
            return

        pivot_pos = _get_world_rotate_pivot(self._target)
        target_rot = cmds.xform(self._target, q=True, ws=True, ro=True)

        cmds.xform(self._helper, ws=True, t=pivot_pos)
        cmds.xform(self._helper, ws=True, ro=target_rot)

    def _register_callbacks(self) -> None:
        """Register scriptJobs for scene events and node deletion.

        Handles cleanup when a new scene is opened/created, and detects
        external deletion of the helper node.
        """
        self._remove_callbacks()

        # Clean up on scene open / new scene
        def on_scene_change():
            self.cleanup()

        self._script_job_ids.append(
            cmds.scriptJob(
                event=["SceneOpened", on_scene_change], killWithScene=True
            )
        )
        self._script_job_ids.append(
            cmds.scriptJob(
                event=["NewSceneOpened", on_scene_change], killWithScene=True
            )
        )

        # Detect external deletion of the helper
        if self._helper and cmds.objExists(self._helper):
            def on_helper_deleted():
                self._active = False
                self._helper = None
                self._helper_xform = None

            self._script_job_ids.append(
                cmds.scriptJob(
                    nodeDeleted=[self._helper, on_helper_deleted],
                    killWithScene=True,
                )
            )

    def _remove_callbacks(self) -> None:
        """Remove all registered scriptJobs."""
        for jid in self._script_job_ids:
            try:
                if cmds.scriptJob(exists=jid):
                    cmds.scriptJob(kill=jid, force=True)
            except RuntimeError:
                pass
        self._script_job_ids = []

    @staticmethod
    def _short_name(node: Optional[str]) -> str:
        """Get the short display name (strip DAG path and namespace)."""
        if not node:
            return "?"
        return node.split("|")[-1].split(":")[-1]


# ---------------------------------------------------------------------------
# Module-level convenience functions
#
# These provide a simple API for shelf commands and external scripts.
# Each delegates to the singleton TempPivotTool instance.
# ---------------------------------------------------------------------------

def _tool() -> TempPivotTool:
    """Access the singleton TempPivotTool instance."""
    return TempPivotTool.get()


def toggle() -> Tuple[bool, str]:
    """Toggle the temp pivot on or off."""
    return _tool().toggle()


def enable() -> Tuple[bool, str]:
    """Enable the temp pivot for the selected object."""
    return _tool().enable()


def disable() -> Tuple[bool, str]:
    """Disable the temp pivot (bake and clean up)."""
    return _tool().disable()


def set_to_center() -> Tuple[bool, str]:
    """Move helper to bounding box center."""
    return _tool().set_to_center()


def set_to_last_object() -> Tuple[bool, str]:
    """Move helper to last selected object's position."""
    return _tool().set_to_last_object()


def set_world_space() -> Tuple[bool, str]:
    """Move helper to world origin."""
    return _tool().set_world_space()


def reset() -> Tuple[bool, str]:
    """Reset pivot to original position."""
    return _tool().reset()


def enter_edit_mode() -> Tuple[bool, str]:
    """Re-select helper with move tool."""
    return _tool().enter_edit_mode()


def bake_to_target() -> Tuple[bool, str]:
    """Apply helper position as target's pivot."""
    return _tool().bake_to_target()


def cleanup() -> None:
    """Remove helper and clean up."""
    _tool().cleanup()


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _kill_ui_script_jobs() -> None:
    """Kill all tracked UI scriptJobs and clear the tracking list."""
    global _ui_script_jobs
    for jid in _ui_script_jobs:
        try:
            if cmds.scriptJob(exists=jid):
                cmds.scriptJob(kill=jid, force=True)
        except RuntimeError:
            pass
    _ui_script_jobs = []


def _ui_control_exists(ctrl: str) -> bool:
    """Return True if *ctrl* refers to an existing Maya UI control."""
    if not ctrl:
        return False
    try:
        return cmds.control(ctrl, exists=True)
    except RuntimeError:
        return False


# ---------------------------------------------------------------------------
# UI implementation
# ---------------------------------------------------------------------------

def _build_ui(parent_layout: str) -> None:
    """Build the tool UI inside the given parent layout.

    Designed to be called from both ``show()`` and the workspace control's
    ``uiScript`` callback.  Clears existing children first so the function
    is idempotent — safe to call multiple times without producing duplicates.
    """
    global _ui_script_jobs

    _kill_ui_script_jobs()
    _ui.clear()

    # Clear existing children to prevent duplicate UI
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
        verticalScrollBarThickness=8,
    )

    main_col = cmds.columnLayout(
        adjustableColumn=True,
        rowSpacing=2,
        columnAttach=("both", 8),
    )

    cmds.separator(height=8, style="none")

    # ==========================================
    # HEADER
    # ==========================================

    header_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=2,
        columnWidth2=(48, 280),
    )
    cmds.canvas(width=44, height=44, rgbValue=UI_COLORS["accent"])
    title_col = cmds.columnLayout(adjustableColumn=True)
    cmds.text(
        label="Temp Pivot Tool",
        font="boldLabelFont",
        align="left",
        height=22,
    )
    cmds.text(
        label="Matrix-aware temporary pivot system",
        align="left",
        font="smallPlainLabelFont",
        height=16,
    )
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    cmds.text(
        label="1. Select a transform, click 'Toggle Temp Pivot'\n"
              "2. Move the helper to desired pivot position\n"
              "3. Click 'Toggle Temp Pivot' again to bake",
        align="left",
        wordWrap=True,
        height=48,
        font="smallPlainLabelFont",
    )

    cmds.separator(height=12, style="none")

    # ==========================================
    # STATUS
    # ==========================================

    cmds.frameLayout(
        label="Status",
        collapsable=False,
        marginWidth=8,
        marginHeight=8,
    )
    status_row = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=2,
        columnWidth2=(65, 250),
    )
    state_btn = cmds.button(
        label="READY",
        width=60,
        height=28,
        backgroundColor=UI_COLORS["inactive"],
        enable=False,
    )
    _ui["state_btn"] = state_btn
    sel_text = cmds.text(label="No object selected", align="left")
    _ui["sel_text"] = sel_text
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # MAIN ACTION
    # ==========================================

    cmds.frameLayout(
        label="Main",
        collapsable=False,
        marginWidth=8,
        marginHeight=8,
    )
    cmds.columnLayout(adjustableColumn=True, rowSpacing=6)
    toggle_btn = cmds.button(
        label="Toggle Temp Pivot",
        height=40,
        backgroundColor=UI_COLORS["active"],
        annotation="Create or remove the temp pivot helper.",
    )
    _ui["toggle_btn"] = toggle_btn
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # PLACEMENT MODES
    # ==========================================

    cmds.frameLayout(
        label="Placement",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8,
    )
    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)
    cmds.text(
        label="Snap helper to a specific position:",
        align="left",
        font="smallPlainLabelFont",
        height=20,
    )

    placement_row1 = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(160, 160),
    )
    center_btn = cmds.button(
        label="Center (BBox)",
        height=28,
        annotation="Move helper to target's bounding box center.",
    )
    last_obj_btn = cmds.button(
        label="Last Object",
        height=28,
        annotation="Move helper to another selected object's pivot.",
    )
    cmds.setParent("..")

    placement_row2 = cmds.rowLayout(
        numberOfColumns=2,
        adjustableColumn=1,
        columnWidth2=(160, 160),
    )
    world_btn = cmds.button(
        label="World Space",
        height=28,
        annotation="Move helper to world origin.",
    )
    reset_btn = cmds.button(
        label="Reset Pivot",
        height=28,
        annotation="Reset target's pivot to original position.",
    )
    cmds.setParent("..")
    cmds.setParent("..")
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # ACTIONS
    # ==========================================

    cmds.frameLayout(
        label="Actions",
        collapsable=True,
        collapse=False,
        marginWidth=8,
        marginHeight=8,
    )
    cmds.columnLayout(adjustableColumn=True, rowSpacing=4)
    edit_btn = cmds.button(
        label="Edit Mode (Re-select Helper)",
        height=28,
        backgroundColor=UI_COLORS["warning"],
        annotation=(
            "Select the helper and activate move tool "
            "for repositioning the temp pivot."
        ),
    )
    bake_btn = cmds.button(
        label="Bake Pivot (Keep Helper)",
        height=28,
        annotation=(
            "Apply helper position as the target's rotatePivot "
            "without removing the helper."
        ),
    )
    cleanup_btn = cmds.button(
        label="Remove Helper Only",
        height=28,
        annotation="Remove the helper without baking the pivot.",
    )
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
        marginHeight=8,
    )
    log_field = cmds.scrollField(
        height=80,
        editable=False,
        wordWrap=True,
        text="Ready. Select an object and click 'Toggle Temp Pivot'.",
    )
    _ui["log_field"] = log_field
    cmds.setParent("..")

    cmds.separator(height=8, style="none")

    # ==========================================
    # CLOSE
    # ==========================================

    close_btn = cmds.button(
        label="Close Tool",
        height=28,
        backgroundColor=(0.5, 0.5, 0.5),
        annotation="Close the Temp Pivot Tool window.",
    )

    cmds.separator(height=8, style="none")

    # ==========================================
    # CALLBACKS
    # ==========================================

    tool = TempPivotTool.get()

    def log_msg(message: str, msg_type: str = "info") -> None:
        """Append a message to the output log."""
        lf = _ui.get("log_field")
        if not lf or not _ui_control_exists(lf):
            return
        try:
            prefix_map = {
                "warning": "[!] ",
                "error": "[X] ",
                "success": "[OK] ",
                "info": "",
            }
            prefix = prefix_map.get(msg_type, "")
            current = cmds.scrollField(lf, query=True, text=True) or ""
            new_text = f"{prefix}{message}"
            if current and not current.startswith("Ready."):
                new_text = f"{current}\n{new_text}"
            cmds.scrollField(lf, edit=True, text=new_text)
            cmds.scrollField(lf, edit=True, insertionPosition=len(new_text))
        except RuntimeError:
            pass

    def update_status() -> None:
        """Update the status bar to reflect the current tool state."""
        sb = _ui.get("state_btn")
        st = _ui.get("sel_text")
        if not sb or not st:
            return
        if not _ui_control_exists(sb) or not _ui_control_exists(st):
            return

        try:
            tb = _ui.get("toggle_btn")
            if tool.active:
                target_name = TempPivotTool._short_name(tool.target)
                cmds.button(
                    sb, edit=True,
                    label="ACTIVE",
                    backgroundColor=UI_COLORS["active"],
                )
                cmds.text(
                    st, edit=True,
                    label=f"Target: {target_name}",
                )
                if tb and _ui_control_exists(tb):
                    cmds.button(
                        tb, edit=True,
                        label="Bake & Disable",
                        backgroundColor=UI_COLORS["helper"],
                    )
            else:
                sel = cmds.ls(selection=True, long=True) or []
                if sel:
                    resolved = _resolve_transform(sel[0])
                    display = resolved.split("|")[-1].split(":")[-1]
                    cmds.text(st, edit=True, label=f"Selected: {display}")
                else:
                    cmds.text(st, edit=True, label="No object selected")
                cmds.button(
                    sb, edit=True,
                    label="READY",
                    backgroundColor=UI_COLORS["inactive"],
                )
                if tb and _ui_control_exists(tb):
                    cmds.button(
                        tb, edit=True,
                        label="Toggle Temp Pivot",
                        backgroundColor=UI_COLORS["active"],
                    )
        except RuntimeError:
            pass

    def on_toggle(*_args):
        success, msg = tool.toggle()
        log_msg(msg, "success" if success else "warning")
        update_status()

    def on_center(*_args):
        success, msg = tool.set_to_center()
        log_msg(msg, "success" if success else "warning")

    def on_last_object(*_args):
        success, msg = tool.set_to_last_object()
        log_msg(msg, "success" if success else "warning")

    def on_world_space(*_args):
        success, msg = tool.set_world_space()
        log_msg(msg, "success" if success else "warning")

    def on_reset(*_args):
        success, msg = tool.reset()
        log_msg(msg, "success" if success else "warning")
        update_status()

    def on_edit_mode(*_args):
        success, msg = tool.enter_edit_mode()
        log_msg(msg, "success" if success else "warning")

    def on_bake(*_args):
        success, msg = tool.bake_to_target()
        log_msg(msg, "success" if success else "warning")

    def on_cleanup(*_args):
        tool.cleanup()
        log_msg("Helper removed.", "info")
        update_status()

    def on_close(*_args):
        """Close the tool window / workspace control."""
        _kill_ui_script_jobs()
        _ui.clear()
        if (
            hasattr(cmds, "workspaceControl")
            and cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True)
        ):
            cmds.deleteUI(WORKSPACE_CONTROL_NAME)
        elif cmds.window(WINDOW_NAME, exists=True):
            cmds.deleteUI(WINDOW_NAME)

    # Connect button callbacks
    cmds.button(toggle_btn, edit=True, command=on_toggle)
    cmds.button(center_btn, edit=True, command=on_center)
    cmds.button(last_obj_btn, edit=True, command=on_last_object)
    cmds.button(world_btn, edit=True, command=on_world_space)
    cmds.button(reset_btn, edit=True, command=on_reset)
    cmds.button(edit_btn, edit=True, command=on_edit_mode)
    cmds.button(bake_btn, edit=True, command=on_bake)
    cmds.button(cleanup_btn, edit=True, command=on_cleanup)
    cmds.button(close_btn, edit=True, command=on_close)

    # UI scriptJobs — parented to main_scroll so they are automatically
    # killed when _build_ui() deletes existing children at the top.
    script_parent = main_scroll

    _ui_script_jobs.append(
        cmds.scriptJob(
            event=["SelectionChanged", update_status],
            parent=script_parent,
        )
    )

    # Initialize status display
    update_status()
    cmds.evalDeferred(update_status, lowestPriority=True)


def _rebuild_workspace_ui() -> None:
    """Rebuild the tool UI inside an existing workspaceControl.

    Called by Maya's ``uiScript`` mechanism whenever the workspace
    control is restored (e.g. after Maya restart or re-dock).
    """
    _build_ui(WORKSPACE_CONTROL_NAME)


def show() -> None:
    """Show the Temp Pivot Tool.

    Uses workspaceControl (Maya 2017+) so the tool can be docked, but
    launches as a **floating window** on first open.  If the user has
    previously docked it, re-running the script will restore and focus
    the existing panel instead of creating a duplicate.

    Falls back to a regular floating window if workspaceControl is
    unavailable (Maya < 2017).
    """
    use_workspace = hasattr(cmds, "workspaceControl")

    if use_workspace:
        _kill_ui_script_jobs()
        _ui.clear()

        if cmds.workspaceControl(WORKSPACE_CONTROL_NAME, exists=True):
            cmds.deleteUI(WORKSPACE_CONTROL_NAME)
        if cmds.window(WINDOW_NAME, exists=True):
            cmds.deleteUI(WINDOW_NAME)

        cmds.workspaceControl(
            WORKSPACE_CONTROL_NAME,
            label=WINDOW_TITLE,
            retain=True,
            floating=True,
            initialWidth=340,
            initialHeight=520,
            minimumWidth=300,
            uiScript=(
                "import temp_pivot_tool; "
                "temp_pivot_tool._rebuild_workspace_ui()"
            ),
        )

        _build_ui(WORKSPACE_CONTROL_NAME)

        cmds.workspaceControl(
            WORKSPACE_CONTROL_NAME, edit=True, visible=True
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
        height=520,
    )

    _build_ui(window)
    cmds.showWindow(window)


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    show()
