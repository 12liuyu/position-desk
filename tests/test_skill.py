import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import stamp
from scripts.inspect_positions import inspect
from sources import FileSource
from test_core import ROOT, local_data


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "account.json"
        self.now = stamp("2030-01-03T10:00:00+08:00")

    def read(self, data, require_plan=False):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        before = self.path.read_bytes()
        result = inspect(FileSource(self.temp.name), now=self.now, require_plan=require_plan)
        self.assertEqual(before, self.path.read_bytes())
        return result

    def test_installed_style_absolute_entry_works_from_an_unrelated_cwd(self):
        command = [sys.executable, "-S", "-B", "-X", "utf8", str(ROOT / "scripts/inspect_positions.py"), "--demo", "--require-plan"]
        result = subprocess.run(command, cwd=self.temp.name, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["mode"], "demo")
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["positions"][0]["code"], "DEMO01")

    def test_no_mode_does_not_silently_show_demo(self):
        result = subprocess.run([sys.executable, "-S", "-B", str(ROOT / "scripts/inspect_positions.py")], capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(result.stdout)

    def test_demo_has_no_tk_or_network_dependency(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            result, code = inspect(FileSource())
        self.assertEqual(code, 0)
        probe = "import sys; from scripts.inspect_positions import inspect; from sources import FileSource; inspect(FileSource()); assert 'tkinter' not in sys.modules"
        ran = subprocess.run([sys.executable, "-S", "-B", "-c", probe], cwd=ROOT, capture_output=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_missing_plan_is_distinguished_from_bad_input(self):
        data = local_data()
        data["plan"] = {}
        for required, expected_code in ((False, 0), (True, 2)):
            result, code = self.read(data, required)
            self.assertEqual(code, expected_code)
            self.assertTrue(result["valid"])
            self.assertEqual(result["status"], "plan_missing")
            self.assertEqual(result["positions"][0]["plan"], {})

    def test_explicit_empty_positions_are_not_an_error_or_a_fake_plan(self):
        data = local_data()
        data.update(stocks=[], market_value=0, total_assets=4900)
        result, code = self.read(data, True)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["positions"], [])

    def test_stale_or_bad_data_returns_no_position_advice(self):
        data = local_data()
        data["captured_at"] = "2030-01-02T15:00:00+08:00"
        result, code = self.read(data)
        self.assertEqual(code, 1)
        self.assertFalse(result["valid"])
        self.assertEqual(result["positions"], [])

    def test_valid_data_preserves_full_plan_and_correct_price_base(self):
        data = local_data()
        data["stocks"][0]["sellable_quantity"] = 0
        result, code = self.read(data, True)
        self.assertEqual(code, 0)
        item = result["positions"][0]
        self.assertEqual(item["reminder_price"], 24.25)
        self.assertEqual(item["reminder_vs_reference"], "-4.90%")
        self.assertEqual(item["plan"], data["plan"]["stocks"]["000000"])
        self.assertTrue(any("可卖为0" in w for w in result["warnings"]))

    def test_missing_directory_never_falls_back_to_demo(self):
        result, code = inspect(FileSource(Path(self.temp.name) / "missing"))
        self.assertEqual(code, 1)
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["positions"], [])


if __name__ == "__main__":
    unittest.main()
