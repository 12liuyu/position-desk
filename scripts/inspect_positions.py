"""Headless, read-only entry for agents. Explicit data mode; never calls a broker."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planner import level_percent
from sources import FileSource


def inspect(source, *, require_plan=False, now=None):
    snapshot = source.read(now)
    result = {"mode": "demo" if source.demo else "local", "valid": snapshot["valid"],
              "status": "invalid", "errors": snapshot["errors"], "warnings": [], "positions": [],
              "source_verification": "file consistency only; not broker verification"}
    if not snapshot["valid"]:
        return result, 1
    result.update(captured_at=snapshot["captured_at"], total_assets=snapshot["assets"],
                  available_funds=snapshot["cash"], reference_date=snapshot["bundle"]["reference_date"],
                  target_date=snapshot["bundle"]["target_date"])
    if source.demo:
        result["warnings"].append("全部数据为固定日期的虚构示例，不是今日持仓。")
    try:
        analyses, problems = source.analyze(snapshot)
        if problems:
            raise ValueError("分析数据未就绪")
        for row in snapshot["rows"]:
            item = analyses[row["code"]]
            result["positions"].append({
                "code": row["code"], "name": row["name"], "quantity": row["quantity"],
                "sellable_quantity": row["sellable_quantity"], "cost_price": item["cost"],
                "quote_price": item["price"], "quote_at": item["quote_at"], "pnl": item["pnl"],
                "stop_loss_pct": item["stop_loss_pct"], "reminder_price": item["cost_stop"],
                "reminder_vs_reference": level_percent(item["cost_stop"], item["reference_close"]),
                "reference_close": item["reference_close"], "at_or_below_reminder": item["severity"] == "risk",
                "plan_valid": item["plan_valid"], "plan": item["specific"],
            })
            if not item["plan_valid"]:
                result["warnings"].append(f"{row['code']} 无匹配预案，不能生成或沿用操作结论。")
            if row["sellable_quantity"] == 0:
                result["warnings"].append(f"{row['code']} 导入可卖为0，不能假定现在可以卖出。")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        result.update(valid=False, status="invalid", positions=[], errors=["输入虽可对账，但无法完整计算执行卡，请检查数值和预案结构。"])
        return result, 1
    missing = sum(not item["plan_valid"] for item in result["positions"])
    result["status"] = "empty" if not result["positions"] else "plan_missing" if missing else "ready"
    return result, 2 if require_plan and missing else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="Explicitly use fictional data")
    mode.add_argument("--data-dir", type=Path, help="Directory containing the user's account.json")
    parser.add_argument("--require-plan", action="store_true", help="Exit 2 if a position lacks a matching plan")
    args = parser.parse_args(argv)
    result, code = inspect(FileSource(args.data_dir), require_plan=args.require_plan)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
