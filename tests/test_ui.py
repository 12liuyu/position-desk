import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock

from app import GREEN, INK, MUTED, RED, PercentText, PositionDesk, quick_view
from sources import FileSource


def text(widget):
    return widget.get("1.0", "end-1c") if isinstance(widget, tk.Text) else widget.cget("text")


class UITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = PositionDesk(self.root, self.temp.name, FileSource())
        for job in self.root.tk.call("after", "info"):
            self.root.after_cancel(job)
        self.app.snapshot = self.app.source.read()
        self.app.analyses, self.app.problems = self.app.source.analyze(self.app.snapshot)
        self.app.render()

    def tearDown(self):
        for job in self.root.tk.call("after", "info"):
            self.root.after_cancel(job)
        self.root.destroy()
        self.temp.cleanup()

    def test_demo_identity_and_no_claim_of_broker_verification(self):
        self.assertIn("虚构演示", self.root.title())
        self.assertEqual(self.app.stock_code.cget("text"), "DEMO01")
        self.assertIn("不弹交易提醒", self.app.notice.cget("text"))
        self.assertNotIn("已核验", self.app.status_label.cget("text"))

    def test_scenarios_and_primary_action_remain_in_small_window(self):
        self.app.toggle_compact()
        self.assertEqual(len(self.app.scenario_buttons), 4)
        self.assertEqual(self.app.scenario_nav.winfo_manager(), "pack")
        self.assertIn("减100股", text(self.app.action_title))
        self.assertIn("26.00(+1.96%)", text(self.app.action_trigger))
        self.assertEqual(self.app.capture_button.cget("text"), "重读数据")

    def test_full_price_percent_pair_colors_and_neutral_quantity(self):
        content = "26.00(+1.96%) 24.25(-4.90%) 25.50(0.00%) 200股"
        widget = PercentText(self.root, content, 11, INK)
        for token, tag, color in (("26.00(+1.96%)", "rise", RED), ("24.25(-4.90%)", "fall", GREEN), ("25.50(0.00%)", "flat", MUTED)):
            for offset in range(content.index(token), content.index(token)+len(token)):
                self.assertIn(tag, widget.tag_names(f"1.0+{offset}c"))
            self.assertEqual(widget.tag_cget(tag, "foreground"), color)
        self.assertFalse(widget.tag_names(f"1.0+{content.index('200股')}c"))

    def test_details_preserve_conditions_and_reason(self):
        scenario = self.app.analyses["DEMO01"]["specific"]["scenarios"][1]
        self.app.text_dialog = Mock()
        self.app.show_scenario(scenario)
        detail = self.app.text_dialog.call_args.args[1]
        for key in ("trigger", "action", "reason"):
            self.assertIn(scenario[key], detail)

    def test_bad_short_view_is_not_truncated_into_instruction(self):
        self.assertIsNone(quick_view({"quick_view": {"action": "卖"*25, "conditions": ["条件"], "otherwise": "否则"}}))
        scenario = self.app.analyses["DEMO01"]["specific"]["scenarios"][1]
        scenario.pop("quick_view")
        self.app.render_card()
        self.assertEqual(text(self.app.action_title), "简版待核验")

    def test_failed_refresh_hides_old_cards(self):
        self.app.output.put(({}, {}, [], "ValueError", False, None))
        self.app.collect()
        self.assertFalse(self.app.snapshot["valid"])
        self.assertEqual(self.app.analyses, {})
        self.assertEqual(self.app.metric_values[0].cget("text"), "--")


if __name__ == "__main__":
    unittest.main()
