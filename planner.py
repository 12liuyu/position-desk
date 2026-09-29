"""Cost-based reminders and dated authored scenarios, not AI predictions."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP

from engine import number


def level_percent(price, reference_close):
    try:
        price, base = Decimal(str(price)), Decimal(str(reference_close))
        if not price.is_finite() or not base.is_finite() or price <= 0 or base <= 0:
            return "涨幅待核"
        change = ((price/base - 1)*100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return "0.00%" if not change else f"{change:+.2f}%"
    except (InvalidOperation, ValueError, TypeError):
        return "涨幅待核"


def annotate_levels(text, levels, reference_close):
    if not isinstance(levels, list):
        return text
    prices = [p for p in levels if isinstance(p, str) and re.fullmatch(r"\d+\.\d{2,3}", p)]
    if not prices:
        return text
    pattern = r"(?<![\d.])(?:" + "|".join(re.escape(p) for p in sorted(set(prices), key=len, reverse=True)) + r")(?![\d.%])(?!\([+-]?\d|\(涨幅待核)"
    return re.sub(pattern, lambda m: f"{m[0]}({level_percent(m[0], reference_close)})", text)


def cost_stop(cost, loss_pct, tick):
    return float((Decimal(str(cost)) * (1-Decimal(str(loss_pct))/100)).quantize(Decimal(str(tick)), rounding=ROUND_DOWN))


def matching_plan(row, bundle):
    plan = bundle["plan"]
    try:
        specific = plan["stocks"][row["code"]]
        if not (plan["asof_date"] == bundle["reference_date"] and plan["trade_date"] == bundle["target_date"]
                and specific["name"] == row["name"] and specific["quantity"] == row["quantity"]
                and number(specific["cost_price"]) == number(row["cost_price"])
                and number(specific["stop_loss_pct"]) == number(row["stop_loss_pct"])):
            return {}
        scenarios = specific["scenarios"]
        if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 12:
            return {}
        if any(not isinstance(s, dict) or any(not isinstance(s.get(k), str) or not s[k].strip()
                or len(s[k]) > 3000 for k in ("title", "trigger", "action", "reason")) for s in scenarios):
            return {}
        if len({s["title"] for s in scenarios}) != len(scenarios):
            return {}
        for key in ("facts", "unknowns", "sources"):
            if key in specific and (not isinstance(specific[key], list)
                    or any(not isinstance(s, str) for s in specific[key])):
                return {}
        return specific
    except (KeyError, TypeError, ValueError):
        return {}


def execution_card(row, bundle):
    cost, price, quantity = row["cost_price"], row["quote"]["price"], row["quantity"]
    stop = cost_stop(cost, row["stop_loss_pct"], row["price_tick"])
    specific = matching_plan(row, bundle)
    breached = price <= stop
    return {"code": row["code"], "name": row["name"], "price": price, "cost": cost,
            "cost_stop": stop, "stop_loss_pct": row["stop_loss_pct"], "quantity": quantity,
            "reference_close": row["reference_close"], "reference_date": bundle["reference_date"].replace("-", ""),
            "pnl": round((price-cost)*quantity, 2), "loss_at_stop": round((stop-cost)*quantity, 2),
            "distance": round(price-stop, 3), "target_day": bundle["target_date"],
            "specific": specific, "analysis_asof": bundle["reference_date"] if specific else None,
            "quote_at": row["quote"]["at"], "plan_valid": bool(specific),
            "plan_problem": "预案未提供或与持仓、日期、纪律不符；不套用旧建议。" if not specific else "",
            "severity": "risk" if breached else "watch",
            "action": "触及自定提醒线，请核对可卖数量" if breached else "未触及自定提醒线",
            "conditions": f"现价 ≤ {stop:.2f}；导入可卖 {row['sellable_quantity']} 股。触发价不保证成交。",
            "fingerprint": f"{row['code']}:{cost}:{row['stop_loss_pct']}:{quantity}"}
