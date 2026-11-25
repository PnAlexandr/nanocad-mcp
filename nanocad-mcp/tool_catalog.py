"""Command visibility shared by the MCP handlers and Windows settings dialog."""
import json
import msvcrt
import os
import sys
import tempfile
from contextvars import ContextVar
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent, Tool
from evans.presentation.tool_defs import TOOL_DEFS
from evans.routes import ROUTES


SETTINGS = Path(os.environ["LOCALAPPDATA"]) / "nanoCAD-MCP" / "tool-settings.json"
current_tool = ContextVar("current_tool", default=None)
BASE = {
    "Чертёж и измерения": "status use_instance drawing_summary get_selection measure_selection query get_texts get_blocks measure styles audit_underlay",
    "Редактирование и оформление": "cleanup_underlay layers dim_chain dim_linear add_text edit_entities zoom",
    "Файлы": "documents backup save",
    "Выполнение кода": "run_lisp exec_python",
}
GROUPS = {
    "Геометрия 2D": "entity primitive teo transform",
    "Блоки и слои": "block block_mgmt layer layer_mgmt",
    "Размеры и оформление": "dimension linear_dim symbol hatch table mleader",
    "Измерения и выборка": "measurement selection",
    "Геометрия 3D": "solid feature edge_op nurb_ifc",
    "Оси и помещения": "multicad",
    "Документы и система": "document system doc_mgmt",
    "Параметры": "parameter",
    "История": "history",
}
# No native implementation in the pinned source, or requires changing the
# active document while the in-document LispFunction is returning.
UNAVAILABLE = set("""
export_pdf import_step export_step export_ifc import_ifc export_stl set_viewport render
create_shell create_circular_pattern create_note_comb create_dim_number create_mleader
mirror_3d sweep_solid loft_solid open_document close_document new_document create_project
open_project save_project save_document undo redo purge execute_command
create_custom_object modify_custom_object create_parametric_object update_parametric
create_reactor start_motion_preview stop_motion_preview create_body_contour
""".split())

BASE_NAMES = {name for names in BASE.values() for name in names.split()}
_owners = {owner: group for group, owners in GROUPS.items() for owner in owners.split()}
EXTRA = {}
COMMAND_GROUP = {name: group for group, names in BASE.items() for name in names.split()}
for definition in TOOL_DEFS:
    name = definition["name"]
    owner = ROUTES.get(name, ("history",))[0]
    if name in UNAVAILABLE or owner not in _owners:
        continue
    public_name = "extra_" + name
    group = _owners[owner]
    if name in {"array_3d", "align_3d", "create_helix", "create_mesh", "edit_mesh", "check_3d_faces"}:
        group = "Геометрия 3D"
    # The source lists rotation but its create_text handler accepts no such argument.
    properties = dict(definition["properties"])
    if name == "create_text":
        properties.pop("rotation", None)
    EXTRA[public_name] = Tool(name=public_name, description=definition["description"],
                             inputSchema={"type": "object", "properties": properties,
                                          "required": definition["required"], "additionalProperties": False})
    COMMAND_GROUP[public_name] = group

_THREE_D = {n for n in EXTRA if COMMAND_GROUP[n] == "Геометрия 3D"}
PACKS = {"3d": _THREE_D, "extras": EXTRA.keys() - _THREE_D}


def _settings():
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"enabled_tools": sorted(BASE_NAMES)}
    names = data.get("enabled_tools") if isinstance(data, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("Некорректный список enabled_tools в настройках nanoCAD MCP")
    return data


def enabled_tools():
    return set(_settings()["enabled_tools"])


def update_tools(*, pack=None, enable=(), disable=()):
    """Merge each button click or settings edit with the last committed profile."""
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    with SETTINGS.with_suffix(".lock").open("a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            data = _settings()
            active = set(data["enabled_tools"])
            if pack is not None:
                members = PACKS[pack]
                saved = data.setdefault("remembered_packs", {})
                selected = active & members
                if selected:
                    saved[pack] = sorted(selected)
                    active -= members
                else:
                    active |= (set(saved.get(pack, ())) & members) or members
            else:
                active -= set(disable) & COMMAND_GROUP.keys()
                active |= set(enable) & COMMAND_GROUP.keys()
            data["enabled_tools"] = sorted(active)
            with tempfile.NamedTemporaryFile(dir=SETTINGS.parent, delete=False) as tmp:
                pending = Path(tmp.name)
            try:
                pending.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
                pending.replace(SETTINGS)
            finally:
                pending.unlink(missing_ok=True)
            return active
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def require_enabled(name):
    if name not in COMMAND_GROUP or name not in enabled_tools():
        raise ValueError(f"Команда отключена в настройках nanoCAD MCP: {name}")


class ConfigurableMCP(FastMCP):
    async def list_tools(self):
        enabled = enabled_tools()
        return [tool for tool in [*await super().list_tools(), *EXTRA.values()]
                if tool.name in enabled]

    async def call_tool(self, name, arguments):
        require_enabled(name)
        token = current_tool.set(name)
        try:
            if name in EXTRA:
                from jsonschema import validate
                validate(arguments, EXTRA[name].inputSchema)
                result = await self.call_extra(name, arguments)
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            return await super().call_tool(name, arguments)
        finally:
            current_tool.reset(token)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--toggle-pack", choices=PACKS)
    action.add_argument("--apply-selection", action="store_true")
    parser.add_argument("--notify", action="store_true")
    opts = parser.parse_args()
    try:
        if opts.toggle_pack:
            pack = opts.toggle_pack
            count = len(update_tools(pack=pack) & PACKS[pack])
            label = "3D" if pack == "3d" else "Доп."
            message = f"{label} · включено команд: {count}" if count else f"{label} · выключено"
        else:
            delta = json.load(sys.stdin)
            update_tools(enable=delta["enable"], disable=delta["disable"])
            message = "Настройки команд сохранены"
        code = 0
    except (OSError, ValueError, TypeError, KeyError) as ex:
        message, code = f"Не удалось изменить набор команд: {ex}", 1
    if opts.notify:
        import win32com.client
        win32com.client.Dispatch("WScript.Shell").Popup(message, 4 if not code else 10, "nanoCAD MCP", 64 if not code else 48)
    elif sys.stdout:
        print(json.dumps({"ok": not code, "message": message}, ensure_ascii=True))
    sys.exit(code)
