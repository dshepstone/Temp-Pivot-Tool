"""
Pivot Space Tool for Autodesk Maya

Creates a temporary pivot rig for a selected control without reparenting the control.
The rig is created outside referenced hierarchies and attaches via constraints.
"""

from __future__ import annotations

from typing import Optional, Tuple

import maya.cmds as cmds


WINDOW_NAME = "pivotSpaceToolWindow"
WINDOW_TITLE = "Pivot Space Tool"

SETTINGS_SUFFIX = "_PST_settings"
ROOT_SUFFIX = "_PST_root_GRP"
PIVOT_SUFFIX = "_PST_pivot_LOC"
DRIVER_SUFFIX = "_PST_driver_LOC"
CONSTRAINT_SUFFIX = "_PST_parentConstraint"

ACTIVE_SETTINGS_NODE: Optional[str] = None


def _sanitize_name(name: str) -> str:
    """Sanitize a node name to remove namespaces and DAG paths."""
    base = name.split("|")[-1]
    return base.replace(":", "_").replace(" ", "_")


def _add_string_attr(node: str, attr: str, value: str = "") -> None:
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, dataType="string")
    cmds.setAttr(f"{node}.{attr}", value, type="string")


def _add_bool_attr(node: str, attr: str, value: bool = False) -> None:
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, attributeType="bool")
    cmds.setAttr(f"{node}.{attr}", value)


def _add_double3_attr(node: str, attr: str) -> None:
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, attributeType="double3")
        for axis in "XYZ":
            cmds.addAttr(
                node,
                longName=f"{attr}{axis}",
                attributeType="double",
                parent=attr,
            )


def _add_message_attr(node: str, attr: str) -> None:
    if not cmds.attributeQuery(attr, node=node, exists=True):
        cmds.addAttr(node, longName=attr, attributeType="message")


def _connect_message(source: str, dest: str, attr: str) -> None:
    _add_message_attr(dest, attr)
    if cmds.isConnected(f"{source}.message", f"{dest}.{attr}"):
        return
    cmds.connectAttr(f"{source}.message", f"{dest}.{attr}", force=True)


def _get_connected_node(settings: str, attr: str) -> Optional[str]:
    if not cmds.objExists(settings):
        return None
    if not cmds.attributeQuery(attr, node=settings, exists=True):
        return None
    connections = cmds.listConnections(f"{settings}.{attr}", source=True, destination=False) or []
    return connections[0] if connections else None


def _get_settings_nodes() -> list[str]:
    nodes = cmds.ls(f"*{SETTINGS_SUFFIX}", type="network") or []
    return [node for node in nodes if cmds.attributeQuery("pstTargetControl", node=node, exists=True)]


def _get_active_settings(use_selection: bool = False) -> Optional[str]:
    global ACTIVE_SETTINGS_NODE

    if use_selection:
        selection = cmds.ls(selection=True, long=True) or []
        if len(selection) == 1:
            for node in _get_settings_nodes():
                target = cmds.getAttr(f"{node}.pstTargetControl") or ""
                if target == selection[0]:
                    ACTIVE_SETTINGS_NODE = node
                    return node

    if ACTIVE_SETTINGS_NODE and cmds.objExists(ACTIVE_SETTINGS_NODE):
        return ACTIVE_SETTINGS_NODE

    nodes = _get_settings_nodes()
    if nodes:
        ACTIVE_SETTINGS_NODE = nodes[0]
        return nodes[0]
    return None


def _get_world_translate_rotate(node: str) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    translate = cmds.xform(node, query=True, worldSpace=True, translation=True)
    rotate = cmds.xform(node, query=True, worldSpace=True, rotation=True)
    return (translate[0], translate[1], translate[2]), (rotate[0], rotate[1], rotate[2])


def _apply_world_translate_rotate(node: str, translate: Tuple[float, float, float], rotate: Tuple[float, float, float]) -> None:
    cmds.xform(node, worldSpace=True, translation=translate)
    cmds.xform(node, worldSpace=True, rotation=rotate)


def _update_status_text() -> None:
    if not cmds.window(WINDOW_NAME, exists=True):
        return
    settings = _get_active_settings()
    control = "None"
    state = "OFF"
    constraint_state = "Missing"
    if settings and cmds.objExists(settings):
        control = cmds.getAttr(f"{settings}.pstTargetControl") or "None"
        is_on = cmds.getAttr(f"{settings}.pstIsOn")
        state = "ON" if is_on else "OFF"
        constraint = _get_connected_node(settings, "pstConstraintMsg")
        constraint_state = "Exists" if constraint and cmds.objExists(constraint) else "Missing"
    cmds.text("pstStatusText", edit=True, label=f"Control: {control} | Pivot: {state} | Constraint: {constraint_state}")


def _create_settings_node(prefix: str, target: str) -> str:
    settings = cmds.createNode("network", name=f"{prefix}{SETTINGS_SUFFIX}")
    _add_string_attr(settings, "pstTargetControl", target)
    _add_string_attr(settings, "pstPrefix", prefix)
    _add_string_attr(settings, "pstRootName", "")
    _add_string_attr(settings, "pstPivotName", "")
    _add_string_attr(settings, "pstDriverName", "")
    _add_string_attr(settings, "pstConstraintName", "")
    _add_bool_attr(settings, "pstIsOn", False)
    _add_double3_attr(settings, "pstControlTranslate")
    _add_double3_attr(settings, "pstControlRotate")
    _add_double3_attr(settings, "pstRootTranslate")
    _add_double3_attr(settings, "pstRootRotate")
    _add_message_attr(settings, "pstRootMsg")
    _add_message_attr(settings, "pstPivotMsg")
    _add_message_attr(settings, "pstDriverMsg")
    _add_message_attr(settings, "pstConstraintMsg")
    return settings


def _get_nodes_from_settings(settings: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    root = _get_connected_node(settings, "pstRootMsg")
    pivot = _get_connected_node(settings, "pstPivotMsg")
    driver = _get_connected_node(settings, "pstDriverMsg")

    if not root:
        root = cmds.getAttr(f"{settings}.pstRootName") or None
    if not pivot:
        pivot = cmds.getAttr(f"{settings}.pstPivotName") or None
    if not driver:
        driver = cmds.getAttr(f"{settings}.pstDriverName") or None

    if root and not cmds.objExists(root):
        root = None
    if pivot and not cmds.objExists(pivot):
        pivot = None
    if driver and not cmds.objExists(driver):
        driver = None
    return root, pivot, driver


def setup_pivot_rig() -> None:
    selection = cmds.ls(selection=True, long=True) or []
    if len(selection) != 1:
        cmds.warning("Select exactly one control to set up the pivot rig.")
        return

    target = selection[0]
    prefix = _sanitize_name(target)

    driver = cmds.spaceLocator(name=f"{prefix}{DRIVER_SUFFIX}")[0]
    pivot = cmds.spaceLocator(name=f"{prefix}{PIVOT_SUFFIX}")[0]

    target_translate, target_rotate = _get_world_translate_rotate(target)
    _apply_world_translate_rotate(driver, target_translate, target_rotate)
    _apply_world_translate_rotate(pivot, target_translate, target_rotate)

    cmds.parent(driver, pivot, absolute=True)
    cmds.select(clear=True)

    root = cmds.group(empty=True, name=f"{prefix}{ROOT_SUFFIX}")
    _apply_world_translate_rotate(root, target_translate, target_rotate)

    cmds.parent(pivot, root, absolute=True)

    settings = _create_settings_node(prefix, target)
    _connect_message(root, settings, "pstRootMsg")
    _connect_message(pivot, settings, "pstPivotMsg")
    _connect_message(driver, settings, "pstDriverMsg")

    _add_string_attr(settings, "pstRootName", root)
    _add_string_attr(settings, "pstPivotName", pivot)
    _add_string_attr(settings, "pstDriverName", driver)

    _add_double3_attr(settings, "pstRootTranslate")
    _add_double3_attr(settings, "pstRootRotate")
    _add_double3_attr(settings, "pstControlTranslate")
    _add_double3_attr(settings, "pstControlRotate")

    cmds.setAttr(f"{settings}.pstRootTranslate", *target_translate)
    cmds.setAttr(f"{settings}.pstRootRotate", *target_rotate)

    cmds.setAttr(f"{root}.visibility", 1)
    cmds.select(pivot, replace=True)

    global ACTIVE_SETTINGS_NODE
    ACTIVE_SETTINGS_NODE = settings
    _update_status_text()


def toggle_on() -> None:
    settings = _get_active_settings(use_selection=True)
    if not settings:
        cmds.warning("No pivot rig found. Use SETUP first.")
        return

    target = cmds.getAttr(f"{settings}.pstTargetControl") or ""
    if not target or not cmds.objExists(target):
        cmds.warning("Target control is missing.")
        return

    root, _, driver = _get_nodes_from_settings(settings)
    if not root or not driver:
        cmds.warning("Pivot rig nodes are missing.")
        return

    control_translate, control_rotate = _get_world_translate_rotate(target)
    cmds.setAttr(f"{settings}.pstControlTranslate", *control_translate)
    cmds.setAttr(f"{settings}.pstControlRotate", *control_rotate)

    _apply_world_translate_rotate(root, control_translate, control_rotate)
    cmds.setAttr(f"{settings}.pstRootTranslate", *control_translate)
    cmds.setAttr(f"{settings}.pstRootRotate", *control_rotate)

    existing_constraint = _get_connected_node(settings, "pstConstraintMsg")
    if existing_constraint and cmds.objExists(existing_constraint):
        cmds.warning("Pivot is already ON.")
        return

    prefix = cmds.getAttr(f"{settings}.pstPrefix") or _sanitize_name(target)
    constraint = cmds.parentConstraint(driver, target, maintainOffset=True, name=f"{prefix}{CONSTRAINT_SUFFIX}")[0]
    _connect_message(constraint, settings, "pstConstraintMsg")
    _add_string_attr(settings, "pstConstraintName", constraint)

    cmds.setAttr(f"{settings}.pstIsOn", True)
    cmds.setAttr(f"{root}.visibility", 1)
    cmds.select(_get_connected_node(settings, "pstPivotMsg") or root, replace=True)
    _update_status_text()


def toggle_off() -> None:
    settings = _get_active_settings()
    if not settings:
        cmds.warning("No pivot rig found.")
        return

    constraint = _get_connected_node(settings, "pstConstraintMsg")
    if not constraint:
        constraint_name = cmds.getAttr(f"{settings}.pstConstraintName") or ""
        if constraint_name and cmds.objExists(constraint_name):
            constraint = constraint_name

    if constraint and cmds.objExists(constraint):
        cmds.delete(constraint)

    root, _, _ = _get_nodes_from_settings(settings)
    if root and cmds.objExists(root):
        cmds.setAttr(f"{root}.visibility", 0)

    cmds.setAttr(f"{settings}.pstIsOn", False)
    _update_status_text()


def key_control() -> None:
    settings = _get_active_settings()
    if not settings:
        cmds.warning("No pivot rig found.")
        return

    target = cmds.getAttr(f"{settings}.pstTargetControl") or ""
    if not target or not cmds.objExists(target):
        cmds.warning("Target control is missing.")
        return

    cmds.setKeyframe(
        target,
        attribute=[
            "translateX",
            "translateY",
            "translateZ",
            "rotateX",
            "rotateY",
            "rotateZ",
        ],
    )


def delete_rig() -> None:
    settings = _get_active_settings()
    if not settings:
        cmds.warning("No pivot rig found.")
        return

    toggle_off()

    root, pivot, driver = _get_nodes_from_settings(settings)
    for node in [driver, pivot, root, settings]:
        if node and cmds.objExists(node):
            cmds.delete(node)

    global ACTIVE_SETTINGS_NODE
    ACTIVE_SETTINGS_NODE = None
    _update_status_text()


def show() -> None:
    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME)

    cmds.window(WINDOW_NAME, title=WINDOW_TITLE, sizeable=False, widthHeight=(260, 220))
    cmds.columnLayout(adjustableColumn=True, rowSpacing=6, columnAlign="center")

    cmds.button(label="SETUP", height=28, command=lambda *_: setup_pivot_rig())
    cmds.button(label="TOGGLE ON", height=28, command=lambda *_: toggle_on())
    cmds.button(label="TOGGLE OFF", height=28, command=lambda *_: toggle_off())
    cmds.button(label="KEY CONTROL", height=28, command=lambda *_: key_control())
    cmds.button(label="DELETE", height=28, command=lambda *_: delete_rig())

    cmds.separator(height=8, style="in")
    cmds.text(name="pstStatusText", label="Control: None | Pivot: OFF | Constraint: Missing", align="center")

    cmds.showWindow(WINDOW_NAME)
    _update_status_text()


if __name__ == "__main__":
    show()
