import unittest
from unittest.mock import AsyncMock, patch

from omavoice import tools


class DesktopToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_failure_is_not_success(self):
        with self.assertRaises(RuntimeError):
            await tools._run(["/usr/bin/false"])

    async def test_timeout_is_reported(self):
        with self.assertRaises(TimeoutError):
            await tools._run(["/usr/bin/sleep", "1"], timeout=0.01)

    async def test_search_is_encoded_and_not_shell_code(self):
        with patch.object(tools, "_run", new_callable=AsyncMock) as run:
            await tools._browser_search({"query": "a&b $(touch /tmp/no)"})
            argv = run.call_args.args[0]
            self.assertEqual(argv[:3], ["omarchy", "launch", "browser"])
            self.assertTrue(argv[3].endswith("a%26b+%24%28touch+%2Ftmp%2Fno%29"))

    async def test_browser_never_targets_terminal(self):
        with patch.object(tools, "_clients", AsyncMock(return_value=[
            {"class": "foot", "address": "0x123"}])), \
                patch.object(tools, "_hypr", new_callable=AsyncMock) as hypr:
            result = await tools._browser_control({"action": "reload", "address": "0x123"})
            self.assertIn("не найдено", result)
            hypr.assert_not_awaited()

    async def test_ambiguous_browser_needs_selection(self):
        with patch.object(tools, "_clients", AsyncMock(return_value=[
            {"class": "chromium", "address": "0x123"},
            {"class": "firefox", "address": "0x456"}])), \
                patch.object(tools, "_hypr", new_callable=AsyncMock) as hypr:
            self.assertIn("несколько", await tools._browser_control({"action": "back"}))
            hypr.assert_not_awaited()

    async def test_invalid_workspace_cannot_inject_lua(self):
        with patch.object(tools, "_hypr", new_callable=AsyncMock) as hypr:
            await tools._workspace({"number": "1}); os.execute('bad')"})
            hypr.assert_not_awaited()

    def test_urls_preserve_case_and_reject_executable_scheme(self):
        self.assertEqual(tools._resolve_url("https://example.com/AbC?Q=X"),
                         "https://example.com/AbC?Q=X")
        self.assertIsNone(tools._resolve_url("javascript:alert(1)"))

    def test_desktop_changes_use_existing_confirmation(self):
        for name in ("browser_search", "browser_control", "focus_window", "workspace"):
            self.assertEqual(tools.classify(name), "confirm")
