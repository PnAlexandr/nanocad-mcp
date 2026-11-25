import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nanocad-mcp"))
import cad_access
import nanocad_mcp as server
import tool_catalog as catalog
from extra_commands import invoke


class WindowTests(unittest.TestCase):
    def test_native_registration_wins_over_shared_clsid(self):
        app = Mock(HWND=55)
        rot = Mock()
        with patch.object(server.S, "access", (42, "started", "grant")), \
             patch.object(server.pythoncom, "GetRunningObjectTable", return_value=rot), \
             patch.object(server.pythoncom, "CreateItemMoniker", return_value="bound") as moniker, \
             patch.object(server.win32com.client, "Dispatch", return_value=app), \
             patch.object(server.win32process, "GetWindowThreadProcessId", return_value=(1, 42)):
            self.assertEqual(server._find_instances(), [("nanoCAD.Application", app)])
            moniker.assert_called_once_with("!", "NanoCAD-MCP.Window.42")
            rot.GetObject.assert_called_once_with("bound")
            rot.EnumRunning.assert_not_called()

    def test_two_instances_without_parent_never_chosen_by_focus(self):
        processes = {1: (0, "ncads.exe"), 2: (0, "ncad.exe"), 3: (2, "pythonw.exe")}
        with patch.object(cad_access, "_in_this_session", return_value=True):
            self.assertEqual(cad_access._calling_nanocad(processes, 3)[0], 2)
            self.assertIsNone(cad_access._calling_nanocad(processes, 4)[0])


class ToolTests(unittest.TestCase):
    def test_ribbon_packs_are_independent_and_restore_a_custom_selection(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(catalog, "SETTINGS", Path(tmp) / "tools.json"):
            # A profile created by the original 0.6 settings dialog.
            catalog.SETTINGS.write_text('{"enabled_tools":["status","extra_create_mesh"]}')
            self.assertEqual(catalog.update_tools(pack="3d"), {"status"})
            extras = catalog.update_tools(pack="extras")
            self.assertEqual(extras, {"status"} | catalog.PACKS["extras"])
            self.assertEqual(catalog.update_tools(pack="3d"), extras | {"extra_create_mesh"})
            catalog.update_tools(enable=["measure"], disable=["status"])
            self.assertEqual(catalog.update_tools(pack="extras"), {"measure", "extra_create_mesh"})
            self.assertEqual(catalog.enabled_tools(), {"measure", "extra_create_mesh"})
            self.assertFalse(catalog.PACKS["3d"] & catalog.PACKS["extras"])
            self.assertEqual(catalog.PACKS["3d"] | catalog.PACKS["extras"], set(catalog.EXTRA))

    def test_saved_profile_controls_discovery_and_direct_calls(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(catalog, "SETTINGS", Path(tmp) / "settings.json"):
            def names():
                return {t.name for t in asyncio.run(server.mcp.list_tools())}
            self.assertEqual(names(), catalog.BASE_NAMES)
            catalog.SETTINGS.write_text(json.dumps({"enabled_tools": ["status", "extra_get_distance"]}))
            self.assertEqual(names(), {"status", "extra_get_distance"})
            with self.assertRaises(ValueError):
                asyncio.run(server.mcp.call_tool("run_lisp", {"code": ""}))
            catalog.SETTINGS.write_text('{"enabled_tools":[]}')
            with self.assertRaises(ValueError):
                asyncio.run(server.mcp.call_tool("extra_get_distance", {}))

    def test_extra_dispatch_uses_bound_transport(self):
        request = Mock(return_value={"distance": 5})
        args = dict(x1=0, y1=0, z1=0, x2=3, y2=4, z2=0)
        with patch.object(catalog, "enabled_tools", return_value={"extra_get_distance"}):
            result = invoke("extra_get_distance", args, request, (1, "copy.dwg"), lambda: None)
        self.assertEqual(result["distance"], 5)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "POST")

    def test_replay_checks_each_command_after_preferences_change(self):
        names = {"extra_record_tool_call", "extra_replay_history", "extra_get_distance"}
        scope = (2, "copy.dwg")
        request = Mock()
        with patch.object(catalog, "enabled_tools", return_value=names):
            invoke("extra_record_tool_call", {"tool_name": "get_distance",
                   "params": dict(x1=0, y1=0, z1=0, x2=3, y2=4, z2=0)},
                   request, scope, lambda: None)
            names.remove("extra_get_distance")
            result = invoke("extra_replay_history", {}, request, scope, lambda: None)
            self.assertFalse(result["success"])
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
