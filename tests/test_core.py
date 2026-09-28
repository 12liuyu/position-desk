import copy
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from engine import ReminderBook, read_bundle, stamp
from planner import annotate_levels, cost_stop, execution_card, level_percent
from sources import FileSource

ROOT = Path(__file__).resolve().parents[1]


def demo_data():
    return json.loads((ROOT / "examples/demo.json").read_text(encoding="utf-8"))


def local_data():
    data = demo_data()
    data.update(demo=False, captured_at="2030-01-03T09:59:00+08:00", session_open=True)
    row = data["stocks"][0]
    row["code"] = row["quote"]["code"] = "000000"
    row["quote"]["at"] = "2030-01-03T10:00:00+08:00"
    data["plan"]["stocks"]["000000"] = data["plan"]["stocks"].pop("DEMO01")
    return data


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "account.json"
        self.now = stamp("2030-01-03T10:00:00+08:00")

    def read(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return read_bundle(self.path, now=self.now)

    def test_demo_is_offline_and_fixed_dated(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            source = FileSource()
            snapshot = source.read()
            items, problems = source.analyze(snapshot)
        self.assertTrue(snapshot["valid"], snapshot["errors"])
        self.assertFalse(snapshot["reminders_enabled"])
        self.assertEqual(snapshot["captured_at"][:10], "2030-01-02")
        self.assertEqual(list(items), ["DEMO01"])
        self.assertFalse(problems)
        self.assertTrue(items["DEMO01"]["plan_valid"])

    def test_missing_file_never_falls_back_to_demo(self):
        snapshot = FileSource(self.temp.name).read(self.now)
        self.assertFalse(snapshot["valid"])
        self.assertEqual(snapshot["rows"], [])

    def test_local_data_valid_without_writing_input(self):
        data = local_data()
        snapshot = self.read(data)
        before = self.path.read_bytes()
        self.assertTrue(snapshot["valid"], snapshot["errors"])
        source = FileSource(self.temp.name)
        source.analyze(source.read(self.now))
        self.assertEqual(self.path.read_bytes(), before)

    def test_demo_cannot_be_used_as_local_data(self):
        self.assertFalse(self.read(demo_data())["valid"])

    def test_invalid_account_fields_are_rejected(self):
        mutations = [
            lambda d: d.update(schema_version=True),
            lambda d: d.update(captured_at="2030-01-02T15:00:00+08:00"),
            lambda d: d.update(captured_at="2030-01-03T12:00:00+08:00"),
            lambda d: d.update(session_open=1),
            lambda d: d.update(reference_date="2030-01-03"),
            lambda d: d.update(target_date="2030-01-02"),
            lambda d: d.update(total_assets=99999),
            lambda d: d["stocks"].append(copy.deepcopy(d["stocks"][0])),
            lambda d: d["stocks"][0].update(quantity=True),
            lambda d: d["stocks"][0].update(quantity=-1),
            lambda d: d["stocks"][0].update(sellable_quantity=999),
            lambda d: d["stocks"][0].update(cost_price=float("nan")),
            lambda d: d["stocks"][0].update(price_tick=0.001),
            lambda d: d["stocks"][0].update(stop_loss_pct=0),
            lambda d: d["stocks"][0].update(stop_loss_pct=21),
            lambda d: d["stocks"][0].update(reference_close=0),
            lambda d: d["stocks"][0].update(market_value=1),
            lambda d: d["stocks"][0]["quote"].update(name="另一个虚构名称"),
            lambda d: d["stocks"][0]["quote"].update(at="2030-01-03T09:55:00+08:00"),
            lambda d: d["stocks"][0]["quote"].update(at="2030-01-03T10:00:00"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                data = local_data()
                mutate(data)
                self.assertFalse(self.read(data)["valid"])

    def test_bad_json_and_oversized_input(self):
        for text in ("{", "null", "[]", "x"*2_000_001):
            self.path.write_text(text, encoding="utf-8")
            self.assertFalse(read_bundle(self.path, now=self.now)["valid"])

    def test_plan_drift_is_not_silently_accepted(self):
        for field, value in (("cost_price", 30), ("quantity", 400), ("name", "不匹配"), ("stop_loss_pct", 5)):
            with self.subTest(field=field):
                data = local_data()
                data["plan"]["stocks"]["000000"][field] = value
                snapshot = self.read(data)
                self.assertTrue(snapshot["valid"])
                item = execution_card(data["stocks"][0], data)
                self.assertFalse(item["plan_valid"])
                self.assertEqual(item["specific"], {})

    def test_missing_plan_keeps_calculation_but_no_scenario(self):
        data = local_data()
        data["plan"] = {}
        item = execution_card(data["stocks"][0], data)
        self.assertEqual(item["cost_stop"], 24.25)
        self.assertFalse(item["plan_valid"])

    def test_prices_use_declared_base_and_do_not_double_annotate(self):
        text = annotate_levels("26.00 / 24.25 / 25.50 / 200股", ["26.00", "24.25", "25.50"], 25.5)
        self.assertEqual(text, "26.00(+1.96%) / 24.25(-4.90%) / 25.50(0.00%) / 200股")
        self.assertEqual(annotate_levels(text, ["26.00", "24.25"], 25.5), text)
        self.assertEqual(level_percent(1, 0), "涨幅待核")
        self.assertEqual(cost_stop(25, 3, .01), 24.25)

    def test_read_alert_does_not_end_tracking_and_empty_valid_data_does(self):
        snapshot = self.read(local_data())
        book = ReminderBook()
        book.reconcile(snapshot, self.now)
        book.reconcile({"valid": False, "rows": []}, self.now)
        self.assertFalse(book.state["positions"]["000000"]["closed"])
        data = local_data()
        data.update(stocks=[], market_value=0, total_assets=4900)
        empty = self.read(data)
        self.assertTrue(empty["valid"])
        book.reconcile(empty, self.now)
        self.assertTrue(book.state["positions"]["000000"]["closed"])

    def test_repeat_snooze_and_stop_latch(self):
        data = local_data()
        data["stocks"][0]["quote"]["price"] = 24
        snapshot = self.read(data)
        item = execution_card(data["stocks"][0], data)
        book = ReminderBook()
        book.reconcile(snapshot, self.now)
        book.apply_cost_stop_latches([item], self.now)
        self.assertEqual(len(book.due(snapshot, [item], self.now)), 1)
        self.assertFalse(book.due(snapshot, [item], self.now+timedelta(minutes=1)))
        data["stocks"][0]["quote"]["price"] = 26
        rebound = execution_card(data["stocks"][0], data)
        book.apply_cost_stop_latches([rebound], self.now)
        self.assertEqual(rebound["severity"], "risk")
        book.snooze(30, self.now)
        self.assertFalse(book.due(snapshot, [rebound], self.now+timedelta(minutes=20)))
        self.assertEqual(len(book.due(snapshot, [rebound], self.now+timedelta(minutes=31))), 1)

    def test_demo_and_closed_session_never_popup(self):
        snapshot = self.read(local_data())
        data = snapshot["bundle"]
        data["stocks"][0]["quote"]["price"] = 24
        item = execution_card(data["stocks"][0], data)
        book = ReminderBook()
        book.reconcile(snapshot, self.now)
        snapshot["reminders_enabled"] = False
        self.assertFalse(book.due(snapshot, [item], self.now))


if __name__ == "__main__":
    unittest.main()
