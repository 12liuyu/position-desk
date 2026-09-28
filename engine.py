"""Local data validation and reminder state. No broker or network access."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))


def now_local():
    return datetime.now(TZ)


def stamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须含时区")
    return parsed.astimezone(TZ)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("数值必须是 JSON 数字")
    if not math.isfinite(value):
        raise ValueError("数值必须有限")
    return float(value)


def in_session(now):
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and (570 <= minute < 690 or 780 <= minute <= 900)


def read_bundle(path, *, demo=False, now=None):
    now = now or now_local()
    result = {"account": "local", "valid": False, "rows": [], "errors": [],
              "captured_at": "未知", "version": "", "assets": None, "cash": None}
    try:
        with Path(path).open("rb") as file:
            raw = file.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("文件超过 2 MB")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("不支持的数据格式")
        if type(data.get("demo")) is not bool or data["demo"] != demo:
            raise ValueError("演示与本地数据不能混用")
        if demo:
            now = stamp(data["demo_clock"])
        captured = stamp(data["captured_at"])
        if captured.date() != now.date() or captured > now + timedelta(seconds=30):
            raise ValueError("持仓快照不是当日有效时间")
        if type(data.get("session_open")) is not bool:
            raise ValueError("缺少显式 session_open 标记")
        reference_day = date.fromisoformat(data["reference_date"])
        target_day = date.fromisoformat(data["target_date"])
        if not reference_day < target_day or reference_day > now.date():
            raise ValueError("收盘基准日期或计划适用日期错误")
        if (now.date() - reference_day).days > 14:
            raise ValueError("收盘基准超过14日，需重新核对")
        if data["session_open"] and reference_day >= now.date():
            raise ValueError("盘中不能用未完成的当日收盘价作基准")
        if target_day < now.date() or (target_day-reference_day).days > 14:
            raise ValueError("计划适用日期已过期或跨度异常")
        rows = data["stocks"]
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("持仓必须是最多100行的列表")
        codes = set()
        computed = 0
        for row in rows:
            code = row["code"]
            pattern = r"DEMO[0-9]{2}" if demo else r"[0-9]{6}"
            if not isinstance(code, str) or not re.fullmatch(pattern, code) or code in codes:
                raise ValueError("证券代码不合法或重复")
            codes.add(code)
            if not isinstance(row["name"], str) or not row["name"].strip() or len(row["name"]) > 32:
                raise ValueError("证券名称缺失或过长")
            qty, sellable = number(row["quantity"]), number(row["sellable_quantity"])
            if qty <= 0 or qty != int(qty) or sellable != int(sellable) or not 0 <= sellable <= qty:
                raise ValueError("持仓或可卖数量不合法；空仓请提交空列表")
            cost, price, mv = [number(row[k]) for k in ("cost_price", "current_price", "market_value")]
            if cost <= 0 or price <= 0 or abs(qty*price-mv) > 0.01:
                raise ValueError("成本、现价或逐股市值不一致")
            if not 0 < number(row["stop_loss_pct"]) <= 20:
                raise ValueError("stop_loss_pct 必须是 (0,20] 的自定百分数")
            if number(row["price_tick"]) != 0.01:
                raise ValueError("当前版本仅支持价格精度为 0.01 的股票")
            quote = row["quote"]
            if quote["code"] != code or quote["name"] != row["name"]:
                raise ValueError("行情与持仓的代码名称不一致")
            if number(quote["price"]) <= 0 or number(row["reference_close"]) <= 0:
                raise ValueError("行情或收盘基准价格无效")
            quote_at = stamp(quote["at"])
            if quote_at.date() != now.date() or quote_at > now + timedelta(seconds=30):
                raise ValueError("行情不是当日有效时间")
            if in_session(now) and (now-quote_at).total_seconds() > 180:
                raise ValueError("盘中行情超过三分钟，暂停提示")
            computed += mv
        assets, cash, mv = [number(data[k]) for k in ("total_assets", "available_funds", "market_value")]
        if min(assets, cash, mv) < 0 or max(abs(computed-mv), abs(assets-cash-mv)) > 0.01:
            raise ValueError("逐股市值、汇总市值与资产减现金不一致")
        if not isinstance(data.get("plan"), dict):
            raise ValueError("plan 必须是对象；无预案请使用空对象")
        result.update(valid=True, rows=rows, captured_at=data["captured_at"],
                      version=hashlib.sha256(raw).hexdigest(), assets=assets, cash=cash,
                      bundle=data, clock=now, reminders_enabled=not demo and data["session_open"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        result["errors"] = [str(exc) if isinstance(exc, ValueError) else "数据缺失或结构错误，请检查输入文件"]
    return result


class ReminderBook:
    def __init__(self, state=None):
        self.state = state if isinstance(state, dict) else {}
        positions = self.state.get("positions", {})
        self.state["positions"] = {k: v for k, v in positions.items() if isinstance(k, str)
            and isinstance(v, dict) and "quantity" in v and "name" in v} if isinstance(positions, dict) else {}
        events = self.state.get("events", [])
        self.state["events"] = [e for e in events if isinstance(e, dict)
            and isinstance(e.get("at"), str) and isinstance(e.get("text"), str)][:200] if isinstance(events, list) else []
        until = self.state.get("snooze_until", 0)
        self.state["snooze_until"] = until if isinstance(until, (int, float)) and math.isfinite(until) else 0

    def event(self, text, now):
        self.state["events"].insert(0, {"at": now.isoformat(), "text": text})
        del self.state["events"][200:]

    def reconcile(self, snapshot, now):
        if not snapshot["valid"]:
            return
        current = {r["code"]: r for r in snapshot["rows"]}
        positions = self.state["positions"]
        for code, row in current.items():
            previous = positions.get(code, {})
            if not previous or previous.get("closed"):
                positions[code] = {"name": row["name"], "quantity": row["quantity"], "closed": False, "last_alert": 0}
                self.event(f"开始跟踪 {row['name']}", now)
            elif previous["quantity"] != row["quantity"]:
                previous["quantity"] = row["quantity"]
                self.event(f"{row['name']} 数量已更新", now)
        for code, row in positions.items():
            if code not in current and not row.get("closed"):
                row["closed"] = True
                self.event(f"导入数据已移除 {row['name']}，结束对应提醒", now)

    def apply_cost_stop_latches(self, advice, now):
        for item in advice:
            position = self.state["positions"].get(item["code"])
            if not position or position.get("closed"):
                continue
            basis = f"{item['cost']}:{item['stop_loss_pct']}:{item['cost_stop']}"
            if position.get("stop_basis") != basis:
                position.pop("stop_triggered_at", None)
                position["stop_basis"] = basis
            if item["price"] <= item["cost_stop"] and not position.get("stop_triggered_at"):
                position["stop_triggered_at"] = now.isoformat()
                self.event(f"{item['name']} 触及自定提醒线", now)
            if position.get("stop_triggered_at"):
                item.update(severity="risk", action="此前已触及提醒线，请核对可卖数量")

    def due(self, snapshot, advice, now, repeat_minutes=15):
        if not snapshot["valid"] or not snapshot.get("reminders_enabled") or not in_session(now):
            return []
        if now.timestamp() < self.state["snooze_until"]:
            return []
        due = []
        for item in advice:
            position = self.state["positions"].get(item["code"])
            if not position or position.get("closed") or item["severity"] != "risk":
                continue
            last = position.get("last_alert", 0)
            last = last if isinstance(last, (float, int)) and math.isfinite(last) else 0
            if position.get("fingerprint") != item["fingerprint"] or now.timestamp()-last >= max(5, repeat_minutes)*60:
                position.update(fingerprint=item["fingerprint"], last_alert=now.timestamp())
                self.event(f"{item['name']}：已提醒，继续跟踪", now)
                due.append(item)
        return due

    def snooze(self, minutes, now):
        self.state["snooze_until"] = now.timestamp() + minutes*60
        self.event(f"稍后提醒：{minutes}分钟", now)
