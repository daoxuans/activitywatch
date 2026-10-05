"""Keep the fork's static Tauri sharing boundary visible in Python CI.

These tests catch accidental capability/menu drift; Rust compilation and the
actual WebView interaction still require the Windows GitHub Actions build.
"""

import json
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "share-client" / "tauri"


class TauriBridgeContractTests(unittest.TestCase):
    def test_summary_window_has_a_separate_capability(self):
        summary = json.loads(
            (ASSETS / "capabilities" / "summary-share.json").read_text(encoding="utf-8")
        )
        dashboard = json.loads(
            (ROOT / "aw-tauri" / "src-tauri" / "capabilities" / "default.json").read_text(
                encoding="utf-8"
            )
        )
        permission = tomllib.loads(
            (ASSETS / "permissions" / "summary-share.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(summary["windows"], ["summary-share"])
        self.assertEqual(summary["platforms"], ["windows"])
        self.assertIn("allow-summary-share-action", summary["permissions"])
        self.assertNotIn("allow-summary-share-action", dashboard["permissions"])
        self.assertEqual(
            permission["permission"][0]["commands"]["allow"], ["summary_share_action"]
        )

    def test_page_requires_explicit_confirmation_and_renders_untrusted_text(self):
        page = (ASSETS / "assets" / "summary-share.html").read_text(encoding="utf-8")
        self.assertIn("invoke('status', null)", page)
        for action, phrase in (
            ("local-preview", "我同意本机预览"),
            ("pause", "暂停分享"),
            ("revoke", "撤回授权"),
        ):
            self.assertIn(f"name: '{action}', confirmation: '{phrase}'", page)
        self.assertIn("item.input.value !== item.confirmation", page)
        self.assertIn("item.result.textContent = String(result)", page)
        self.assertNotIn("innerHTML", page)
        self.assertIn("不会自动上传", page)

    def test_bridge_whitelists_actions_and_binds_preview_to_its_own_server(self):
        source = (ASSETS / "src" / "summary_share_ui.rs").read_text(encoding="utf-8")
        self.assertIn('if window.label() != WINDOW_LABEL', source)
        self.assertIn("let expected = action_confirmation(action)?;", source)
        self.assertIn('"local-preview" => Ok(Some("我同意本机预览"))', source)
        self.assertIn("crate::profile::is_default(profile)", source)
        self.assertIn('"http://127.0.0.1:{}/api/0"', source)
        self.assertIn('.env_remove("AW_SHARE_AW_API_KEY")', source)
        self.assertIn('.join("share-client")', source)
        self.assertIn('.join("aw-share.exe")', source)
        self.assertNotIn("cmd.exe", source)
        self.assertNotIn("powershell.exe", source)


if __name__ == "__main__":
    unittest.main()
