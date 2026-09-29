import tempfile
import copy
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from datetime import timedelta

from app import GREEN, INK, MUTED, RED, PercentText, PositionDesk, SlimScroll, quick_view
from sources import FileSource
from engine import stamp


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
        self.app.references = {code: {**item, 'severity': 'watch'} for code, item in self.app.analyses.items()}
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
        self.assertEqual(len(self.app.scenario_buttons), 7)
        self.assertEqual(self.app.scenario_nav.winfo_manager(), "canvas")
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

    def test_results_only_keep_reasons_in_source_not_extra_buttons(self):
        scenario = self.app.analyses["DEMO01"]["specific"]["scenarios"][1]
        for key in ("trigger", "action", "reason"):
            self.assertTrue(scenario[key])
        self.assertEqual(text(self.app.action_context), scenario['quick_view']['outlook'])
        self.assertIsNone(self.app.scenario_detail_button)
        self.assertFalse(hasattr(self.app, 'show_details'))
        self.assertFalse(hasattr(self.app, 'show_scenario'))

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

    def test_left_nav_has_seven_independent_selectors_and_keeps_action_large(self):
        scenarios = self.app.analyses['DEMO01']['specific']['scenarios']
        for index, scenario in enumerate(scenarios):
            button = self.app.scenario_buttons[index]
            self.assertEqual(button.grid_info()['column'], 0)
            self.assertEqual(button.grid_info()['row'], index)
            button.invoke()
            self.assertEqual(text(self.app.action_title), scenario['quick_view']['action'])
        self.app.resize_cards(SimpleNamespace(width=310))
        self.assertEqual(int(self.app.action_title.cget('wraplength')), 272)
        self.assertEqual(self.app.nav_region.pack_info()['side'], 'left')

    def test_slim_scroll_is_overflow_only_and_thumb_keeps_grab_offset(self):
        bar = self.app.nav_scrollbar
        self.assertIsInstance(bar, SlimScroll)
        bar.winfo_height = Mock(return_value=100)
        bar.target = Mock()
        bar.set(0, 1)
        self.assertEqual(bar.winfo_manager(), '')
        bar.set(.25, .75)
        self.assertEqual(bar.winfo_manager(), 'pack')
        bar.seek(SimpleNamespace(y=30))
        bar.target.yview_moveto.assert_not_called()
        bar.drag(SimpleNamespace(y=40))
        self.assertAlmostEqual(bar.target.yview_moveto.call_args.args[0], .35)

    def test_mousewheel_moves_only_the_hovered_pane(self):
        with patch.object(self.app.nav_canvas, 'yview', return_value=(0, .5)), \
             patch.object(self.app.nav_canvas, 'yview_scroll') as nav, \
             patch.object(self.app.canvas, 'yview_scroll') as detail:
            self.app.scroll_card(SimpleNamespace(widget=self.app.scenario_buttons[-1], delta=-120))
            nav.assert_called_once_with(1, 'units')
            detail.assert_not_called()

    def test_refresh_preserves_reference_and_selection_but_hides_live_values(self):
        title = self.app.analyses['DEMO01']['specific']['scenarios'][-1]['title']
        self.app.select_scenario(title)
        self.app.refresh = Mock()
        self.app.capture_button.invoke()
        self.app.busy = True
        self.app.read_started = time.monotonic()-26
        self.app.health_tick()
        self.assertEqual(len(self.app.scenario_buttons), 7)
        self.assertEqual(self.app.scenario_selection['DEMO01'], title)
        self.assertIn('原预案', self.app.plan_date.cget('text'))
        self.assertIn('更新中', self.app.status_label.cget('text'))
        self.assertFalse(self.app.analyses)
        self.assertEqual(self.app.metric_values[2].cget('text'), '待核验')
        self.app.capture()
        self.app.refresh.assert_called_once()

    def test_queued_read_cannot_replace_pending_reference_or_alert(self):
        self.app.busy = True
        self.app.refresh = Mock()
        self.app.book.due = Mock()
        self.app.capture()
        original = self.app.snapshot['version']
        self.app.output.put(({**self.app.snapshot, 'rows': [], 'version': 'late'}, {}, [], None, False, None))
        self.app.collect()
        self.assertTrue(self.app.capture_pending)
        self.assertEqual(self.app.snapshot['version'], original)
        self.assertEqual(len(self.app.scenario_buttons), 7)
        self.app.book.due.assert_not_called()
        self.app.refresh.assert_called_once()

    def test_successful_refresh_retains_all_scenarios_and_completion_message(self):
        self.app.refresh = Mock()
        self.app.capture()
        snap = self.app.source.read()
        items, problems = self.app.source.analyze(snap)
        self.app.output.put((snap, items, problems, None, True, None))
        self.app.collect()
        self.app.health_tick()
        self.assertFalse(self.app.capture_pending)
        self.assertEqual(len(self.app.scenario_buttons), 7)
        self.assertIn('校验通过', self.app.footer.cget('text'))
        self.assertEqual(str(self.app.capture_button.cget('state')), 'normal')

    def test_invalid_refresh_and_reread_cannot_revive_same_reference(self):
        snap = copy.deepcopy(self.app.snapshot)
        self.app.refresh = Mock()
        self.app.capture()
        self.app.output.put((snap, {}, [], None, True, 'fixture failure'))
        self.app.collect()
        self.app.output.put((snap, {}, [], None, False, None))
        self.app.collect()
        self.assertFalse(self.app.references)
        self.assertFalse(self.app.scenario_buttons)
        self.assertFalse(self.app.snapshot['valid'])

    def test_confirmed_empty_refresh_removes_reference(self):
        self.app.refresh = Mock()
        self.app.capture()
        snap = {**self.app.snapshot, 'rows': [], 'version': 'empty'}
        self.app.output.put((snap, {}, [], None, True, None))
        self.app.collect()
        self.assertEqual(self.app.stock_title.cget('text'), '导入空仓')
        self.assertFalse(self.app.references)

    def test_watchdog_expires_quotes_and_then_account_without_worker_completion(self):
        self.app.source.demo = False
        moment = stamp('2030-01-02T16:00:00+08:00')
        # Move only the isolated test fixture into a trading session.
        moment = moment.replace(hour=10)
        self.app.snapshot['captured_at'] = moment.isoformat()
        self.app.analyses['DEMO01']['quote_at'] = moment.isoformat()
        with patch('app.now_local', return_value=moment+timedelta(minutes=4)):
            self.app.health_tick()
        self.assertFalse(self.app.analyses)
        self.assertTrue(self.app.references)
        self.assertEqual(self.app.metric_values[2].cget('text'), '待核验')
        with patch('app.now_local', return_value=moment+timedelta(days=1)):
            self.app.health_tick()
        self.assertFalse(self.app.snapshot['valid'])
        self.assertFalse(self.app.scenario_buttons)

    def test_pending_refresh_cannot_emit_popup(self):
        self.app.capture_pending = True
        self.app.text_dialog = Mock()
        self.app.alert(list(self.app.analyses.values()))
        self.app.text_dialog.assert_not_called()

    def test_pending_reference_expires_at_midnight_instead_of_becoming_current(self):
        self.app.source.demo = False
        self.app.refresh = Mock()
        with patch('app.now_local', return_value=stamp('2030-01-02T23:59:00+08:00')):
            self.app.capture()
        self.assertEqual(len(self.app.scenario_buttons), 7)
        with patch('app.now_local', return_value=stamp('2030-01-03T00:00:00+08:00')):
            self.app.health_tick()
        self.assertFalse(self.app.snapshot['valid'])
        self.assertFalse(self.app.references)

    def test_late_risk_quote_is_removed_before_latch_or_popup(self):
        self.app.source.demo = False
        moment = stamp('2030-01-02T10:00:00+08:00')
        snap = copy.deepcopy(self.app.snapshot)
        snap['captured_at'] = moment.isoformat()
        items = copy.deepcopy(self.app.analyses)
        items['DEMO01'].update(quote_at=moment.isoformat(), severity='risk', price=20)
        self.app.book.apply_cost_stop_latches = Mock()
        self.app.alert = Mock()
        self.app.output.put((snap, items, [], None, False, None))
        with patch('app.now_local', return_value=moment+timedelta(minutes=4)):
            self.app.collect()
        self.app.book.apply_cost_stop_latches.assert_called_once_with([], moment+timedelta(minutes=4))
        self.app.alert.assert_not_called()
        self.assertFalse(self.app.analyses)

    def test_legacy_quick_view_without_outlook_does_not_gain_fixed_copy(self):
        scenario = self.app.analyses['DEMO01']['specific']['scenarios'][1]
        scenario['quick_view'].pop('outlook')
        self.app.render_card()
        self.assertEqual(text(self.app.action_context), '')
        self.assertEqual(text(self.app.action_title), '受阻减100股')


if __name__ == "__main__":
    unittest.main()
