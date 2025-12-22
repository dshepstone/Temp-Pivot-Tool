"""Temporary Pivot Tool for Autodesk Maya."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import maya.cmds as cmds
import maya.api.OpenMaya as om

WINDOW_NAME = "tempPivotToolWindow"
WINDOW_TITLE = "Temp Pivot Tool"
MANAGER_NODE_NAME = "tempPivotManager"

PIVOT_MODES = [
    "Pivot to Last Selected",
    "Pivot to Selection Center",
    "Pivot to World Origin",
    "Pivot to Locator (pick)",
]


# -----------------------------
# Node setup and storage
# -----------------------------

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


def get_or_create_manager() -> str:
    if cmds.objExists(MANAGER_NODE_NAME):
        node = MANAGER_NODE_NAME
    else:
        node = cmds.createNode("network", name=MANAGER_NODE_NAME)
    _add_string_attr(node, "lastUsedMode", PIVOT_MODES[0])
    _add_bool_attr(node, "lastUsedAffectScalePivot", True)
    _add_string_attr(node, "presetsJson", "")
    _add_string_attr(node, "lastLocator", "")
    if not cmds.attributeQuery("controlData", node=node, exists=True):
        cmds.addAttr(node, longName="controlData", attributeType="message", multi=True)
    return node


def _sanitize_name(name: str) -> str:
    return name.replace("|", "_").replace(":", "_")


def get_or_create_control_data_node(control: str) -> str:
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

    cmds.connectAttr(f"{control}.message", f"{node}.controlMessage", force=True)

    manager = get_or_create_manager()
    cmds.connectAttr(f"{manager}.controlData", f"{node}.manager", nextAvailable=True)

    return node


def _get_control_from_data_node(data_node: str) -> Optional[str]:
    conns = cmds.listConnections(f"{data_node}.controlMessage", s=True, d=False) or []
    return conns[0] if conns else None


def _get_preset_data(manager: str) -> List[Dict[str, Any]]:
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
    cmds.setAttr(f"{manager}.presetsJson", json.dumps(data), type="string")


# -----------------------------
# Pivot calculations
# -----------------------------


def _as_point(point: Tuple[float, float, float]) -> om.MPoint:
    return om.MPoint(point[0], point[1], point[2])


def _get_world_matrix(node: str) -> om.MMatrix:
    matrix = cmds.xform(node, query=True, worldSpace=True, matrix=True)
    return om.MMatrix(matrix)


def world_to_local_pivot(control: str, world_point: Tuple[float, float, float]) -> om.MPoint:
    world_matrix = _get_world_matrix(control)
    inv_matrix = world_matrix.inverse()
    return _as_point(world_point) * inv_matrix


def compute_world_target(mode: str, selection: List[str], locator_name: str) -> Optional[Tuple[float, float, float]]:
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

    if mode == "Pivot to Locator (pick)":
        if locator_name and cmds.objExists(locator_name):
            position = cmds.xform(locator_name, query=True, worldSpace=True, translation=True)
            return (position[0], position[1], position[2])
        return None

    return None


# -----------------------------
# Pivot apply/restore
# -----------------------------

def _is_pivot_editable(control: str, affect_scale: bool) -> Tuple[bool, List[str]]:
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
) -> Tuple[List[str], List[str]]:
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
        if not is_on:
            orig_rotate = cmds.getAttr(f"{control}.rotatePivot")[0]
            orig_scale = cmds.getAttr(f"{control}.scalePivot")[0]
            cmds.setAttr(f"{data_node}.origRotatePivot", *orig_rotate)
            cmds.setAttr(f"{data_node}.origScalePivot", *orig_scale)

        local_point = world_to_local_pivot(control, world_point)
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

        cmds.setAttr(f"{data_node}.lastTempWorldPivot", *world_point)
        cmds.setAttr(f"{data_node}.lastTempMode", mode, type="string")
        cmds.setAttr(f"{data_node}.affectScalePivot", affect_scale)
        cmds.setAttr(f"{data_node}.isOn", True)
        updated.append(control)

    return updated, warnings


def restore_temp_pivot(controls: List[str]) -> Tuple[List[str], List[str]]:
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
        orig_rotate = cmds.getAttr(f"{data_node}.origRotatePivot")[0]
        orig_scale = cmds.getAttr(f"{data_node}.origScalePivot")[0]
        cmds.setAttr(f"{control}.rotatePivot", *orig_rotate)
        if affect_scale:
            cmds.setAttr(f"{control}.scalePivot", *orig_scale)
        cmds.setAttr(f"{data_node}.isOn", False)
        restored.append(control)
    return restored, warnings


def toggle_temp_pivot(
    controls: List[str],
    mode: str,
    affect_scale: bool,
    world_point: Optional[Tuple[float, float, float]],
) -> Tuple[List[str], List[str], bool]:
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


# -----------------------------
# Presets
# -----------------------------

def preset_save(
    name: str,
    mode: str,
    affect_scale: bool,
    world_target: Tuple[float, float, float],
    selection: List[str],
    locator_name: str,
) -> bool:
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
# UI
# -----------------------------

def _get_selection() -> List[str]:
    return cmds.ls(selection=True, type="transform") or []


def _selection_count_text() -> str:
    count = len(_get_selection())
    return f"Selected: {count}"


def _set_status(status_field: str, message: str) -> None:
    cmds.text(status_field, edit=True, label=message)


def show() -> None:
    if cmds.window(WINDOW_NAME, exists=True):
        cmds.deleteUI(WINDOW_NAME)

    manager = get_or_create_manager()

    window = cmds.window(WINDOW_NAME, title=WINDOW_TITLE, sizeable=False)
    main_layout = cmds.columnLayout(adjustableColumn=True, rowSpacing=8)

    # Selection + Mode
    cmds.frameLayout(label="Selection + Mode", collapsable=False)
    selection_text = cmds.text(label=_selection_count_text())
    mode_menu = cmds.optionMenu(label="Pivot Mode")
    for mode in PIVOT_MODES:
        cmds.menuItem(label=mode)
    affect_scale_checkbox = cmds.checkBox(
        label="Affect Scale Pivot too",
        value=cmds.getAttr(f"{manager}.lastUsedAffectScalePivot"),
    )

    locator_row = cmds.rowLayout(numberOfColumns=3, adjustableColumn=2)
    cmds.text(label="Locator")
    locator_field = cmds.textField(text=cmds.getAttr(f"{manager}.lastLocator"))
    pick_button = cmds.button(label="Pick Locator")
    cmds.setParent("..")
    cmds.setParent("..")

    # Apply / Toggle / Reset
    cmds.frameLayout(label="Apply / Toggle / Reset", collapsable=False)
    apply_button = cmds.button(label="Apply Temp Pivot")
    toggle_button = cmds.button(label="Toggle Temp Pivot (On/Off)")
    reset_button = cmds.button(label="Reset (Restore Original Pivots)")
    cmds.setParent("..")

    # Presets
    cmds.frameLayout(label="Presets (Node-based storage)", collapsable=False)
    preset_name_field = cmds.textField(placeholderText="Preset Name")
    preset_button_row = cmds.rowLayout(numberOfColumns=4, adjustableColumn=1)
    save_button = cmds.button(label="Save Preset (from selection)")
    load_button = cmds.button(label="Load Preset (apply)")
    delete_button = cmds.button(label="Delete Preset")
    refresh_button = cmds.button(label="Refresh Preset List")
    cmds.setParent("..")
    preset_list = cmds.textScrollList(height=120)
    cmds.setParent("..")

    # Status
    cmds.frameLayout(label="Status / Warnings", collapsable=False)
    status_text = cmds.text(label="Ready.")
    cmds.setParent("..")

    def refresh_presets() -> None:
        data = _get_preset_data(manager)
        cmds.textScrollList(preset_list, edit=True, removeAll=True)
        for preset in sorted(data, key=lambda item: item.get("name", "")):
            cmds.textScrollList(preset_list, edit=True, append=preset.get("name"))

    def update_selection_text() -> None:
        cmds.text(selection_text, edit=True, label=_selection_count_text())

    def set_manager_last_values(mode: str, affect_scale: bool, locator: str) -> None:
        cmds.setAttr(f"{manager}.lastUsedMode", mode, type="string")
        cmds.setAttr(f"{manager}.lastUsedAffectScalePivot", affect_scale)
        cmds.setAttr(f"{manager}.lastLocator", locator, type="string")

    def _gather_mode_settings() -> Tuple[str, bool, str]:
        mode = cmds.optionMenu(mode_menu, query=True, value=True)
        affect_scale = cmds.checkBox(affect_scale_checkbox, query=True, value=True)
        locator_name = cmds.textField(locator_field, query=True, text=True)
        return mode, affect_scale, locator_name

    def _apply() -> None:
        selection = _get_selection()
        mode, affect_scale, locator_name = _gather_mode_settings()
        world_target = compute_world_target(mode, selection, locator_name)
        if not selection:
            _set_status(status_text, "No selection.")
            return
        if world_target is None:
            _set_status(status_text, "No valid pivot target found.")
            return
        applied, warnings = apply_temp_pivot(selection, world_target, affect_scale, mode)
        set_manager_last_values(mode, affect_scale, locator_name)
        message = f"Applied temp pivot to {len(applied)} control(s)."
        if warnings:
            message += " Warnings: " + "; ".join(warnings)
        _set_status(status_text, message)

    def _toggle() -> None:
        selection = _get_selection()
        if not selection:
            _set_status(status_text, "No selection.")
            return
        mode, affect_scale, locator_name = _gather_mode_settings()
        world_target = compute_world_target(mode, selection, locator_name)
        applied, warnings, turned_on = toggle_temp_pivot(
            selection, mode, affect_scale, world_target
        )
        set_manager_last_values(mode, affect_scale, locator_name)
        if turned_on:
            message = f"Applied temp pivot to {len(applied)} control(s)."
        else:
            message = f"Restored temp pivot on {len(applied)} control(s)."
        if warnings:
            message += " Warnings: " + "; ".join(warnings)
        _set_status(status_text, message)

    def _reset() -> None:
        selection = _get_selection()
        if not selection:
            _set_status(status_text, "No selection.")
            return
        restored, warnings = restore_temp_pivot(selection)
        message = f"Restored pivots on {len(restored)} control(s)."
        if warnings:
            message += " Warnings: " + "; ".join(warnings)
        _set_status(status_text, message)

    def _pick_locator() -> None:
        selection = cmds.ls(selection=True) or []
        locator = ""
        for node in selection:
            shapes = cmds.listRelatives(node, shapes=True) or []
            if cmds.nodeType(node) == "transform" and any(
                cmds.nodeType(shape) == "locator" for shape in shapes
            ):
                locator = node
                break
        if locator:
            cmds.textField(locator_field, edit=True, text=locator)
            _set_status(status_text, f"Picked locator: {locator}")
        else:
            _set_status(status_text, "Select a locator transform to pick.")

    def _save_preset() -> None:
        selection = _get_selection()
        name = cmds.textField(preset_name_field, query=True, text=True).strip()
        if not name:
            _set_status(status_text, "Preset name required.")
            return
        if not selection:
            _set_status(status_text, "No selection to save.")
            return
        mode, affect_scale, locator_name = _gather_mode_settings()
        world_target = compute_world_target(mode, selection, locator_name)
        if world_target is None:
            _set_status(status_text, "No valid pivot target found.")
            return
        preset_save(name, mode, affect_scale, world_target, selection, locator_name)
        refresh_presets()
        _set_status(status_text, f"Preset '{name}' saved.")

    def _load_preset() -> None:
        selected = cmds.textScrollList(preset_list, query=True, selectItem=True) or []
        if not selected:
            _set_status(status_text, "Select a preset to load.")
            return
        applied, warnings = preset_load(selected[0], selection_override=True)
        message = f"Loaded preset '{selected[0]}' on {len(applied)} control(s)."
        if warnings:
            message += " Warnings: " + "; ".join(warnings)
        _set_status(status_text, message)

    def _delete_preset() -> None:
        selected = cmds.textScrollList(preset_list, query=True, selectItem=True) or []
        if not selected:
            _set_status(status_text, "Select a preset to delete.")
            return
        success = preset_delete(selected[0])
        if success:
            refresh_presets()
            _set_status(status_text, f"Preset '{selected[0]}' deleted.")
        else:
            _set_status(status_text, "Preset not found.")

    cmds.button(apply_button, edit=True, command=lambda *_: _apply())
    cmds.button(toggle_button, edit=True, command=lambda *_: _toggle())
    cmds.button(reset_button, edit=True, command=lambda *_: _reset())
    cmds.button(pick_button, edit=True, command=lambda *_: _pick_locator())

    cmds.button(save_button, edit=True, command=lambda *_: _save_preset())
    cmds.button(load_button, edit=True, command=lambda *_: _load_preset())
    cmds.button(delete_button, edit=True, command=lambda *_: _delete_preset())
    cmds.button(refresh_button, edit=True, command=lambda *_: refresh_presets())

    refresh_presets()
    update_selection_text()
    cmds.optionMenu(mode_menu, edit=True, value=cmds.getAttr(f"{manager}.lastUsedMode"))

    cmds.scriptJob(
        event=["SelectionChanged", update_selection_text],
        parent=window,
    )

    cmds.showWindow(window)


if __name__ == "__main__":
    show()
