"""Standalone local execution cards. No network, screenshots or trade execution."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import queue
import re
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from engine import ReminderBook, now_local
from sources import FileSource
from planner import annotate_levels, level_percent

PAPER, WHITE, INK = "#fcfbf8", "#ffffff", "#14213d"
MUTED, LINE, BLUE = "#778297", "#dfe3e8", "#2385df"
AMBER, GREEN, RED = "#97652e", "#00815b", "#b32634"
SCENARIO_COLORS = {"positive": RED, "risk": GREEN, "watch": AMBER}
SCENARIO_LABELS = {"positive": "转强预案", "risk": "风险预案", "watch": "待确认预案"}
PERCENT_PATTERN = re.compile(r"(?<![\d.])[+-]?\d+(?:\.\d+)?%|涨幅待核")
PRICE_PERCENT_PATTERN = re.compile(r"(?<![\d.])\d+\.\d{2,3}\((?P<change>[+-]?\d+(?:\.\d+)?%|涨幅待核)\)")
PERCENT_COLORS = {"rise": RED, "fall": GREEN, "flat": MUTED, "missing": MUTED}


def percent_tag(text):
    if text == "涨幅待核":
        return "missing"
    value = float(text[:-1])
    return "rise" if value > 0 else "fall" if value < 0 else "flat"


def color_percentages(widget, text):
    for tag, color in PERCENT_COLORS.items():
        widget.tag_configure(tag, foreground=color)
        widget.tag_remove(tag, "1.0", "end")
    for match in PERCENT_PATTERN.finditer(text):
        widget.tag_add(percent_tag(match[0]), f"1.0+{match.start()}c", f"1.0+{match.end()}c")
    for match in PRICE_PERCENT_PATTERN.finditer(text):
        widget.tag_add(percent_tag(match["change"]), f"1.0+{match.start()}c", f"1.0+{match.end()}c")


class PercentText(tk.Text):
    """Read-only inline colors with height following wrapped display lines."""
    def __init__(self, parent, text, size, color, bold=False):
        super().__init__(parent, bg=parent.cget("bg"), fg=color, relief="flat", bd=0,
            highlightthickness=0, padx=0, pady=0, wrap="char", width=1, height=1,
            cursor="arrow", takefocus=False,
            font=("Microsoft YaHei UI", size, "bold" if bold else "normal"))
        self.insert("1.0", text)
        color_percentages(self, text)
        self.configure(state="disabled")
        self.bind("<Configure>", self.fit_lines)

    def fit_lines(self, _event=None):
        lines = (self.count("1.0", "end-1c", "displaylines") or (0,))[0] + 1
        if int(self.cget("height")) != lines:
            self.configure(height=lines)


def quick_view(scenario):
    view = scenario.get("quick_view")
    if not isinstance(view, dict):
        return None
    for key, limit in (("action", 24), ("otherwise", 64)):
        value = view.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit or "\n" in value:
            return None
    conditions = view.get("conditions")
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 3:
        return None
    if any(not isinstance(line, str) or not line.strip() or len(line) > 40 or "\n" in line for line in conditions):
        return None
    return view


class PositionDesk:
    def __init__(self, root, state_dir, source=None):
        self.root, self.source = root, source or FileSource()
        self.state_dir = Path(state_dir)
        self.state_path = self.state_dir / "state.json"
        try:
            self.settings = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(self.settings, dict):
                raise ValueError()
        except (OSError, ValueError):
            self.settings = {}
        if self.settings.get("repeat_minutes") not in (5, 15, 30, 60):
            self.settings["repeat_minutes"] = 15
        self.book = ReminderBook(self.settings.get("reminders"))
        self.output = queue.Queue()
        self.busy = self.compact = self.capture_pending = False
        self.capture_message = self.state_error = ""
        self.capture_blocked_version = None
        self.snapshot, self.analyses, self.problems = {}, {}, []
        self.selected = self.popup = None
        self.scenario_selection = {}
        self.scenario_buttons = []
        self.risk_scenario_shown = set()
        self.popup_keys = set()
        self.topmost = tk.BooleanVar(value=bool(self.settings.get("topmost", False)))
        self.sound = tk.BooleanVar(value=bool(self.settings.get("sound", False)))
        self.repeat = tk.StringVar(value=str(self.settings["repeat_minutes"]))
        self.selection = tk.StringVar()
        root.title("持仓执行卡 · " + self.source.label)
        root.configure(bg=PAPER)
        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        root.minsize(540, 600)
        self.full_height = min(940, root.winfo_screenheight() - 100)
        root.geometry(f"800x{self.full_height}")
        root.attributes("-topmost", self.topmost.get())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.style.configure("TCombobox", padding=3)
        self.build()
        root.after_idle(self.restore_position)
        root.after(150, self.refresh)
        root.after(200, self.collect)
        root.after(60000, self.poll)

    def label(self, parent, text="", size=10, color=INK, bold=False, **kw):
        return tk.Label(parent, text=text, bg=kw.pop("bg", parent.cget("bg")), fg=color,
            font=(kw.pop("family", "Microsoft YaHei UI"), size, "bold" if bold else "normal"), **kw)

    def button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, relief="flat", bd=0,
            padx=16, pady=11, cursor="hand2", font=("Microsoft YaHei UI", 11, "bold"),
            bg=BLUE if primary else "#f0f2f5", fg=WHITE if primary else INK,
            activebackground="#dceaff", highlightthickness=0)

    def rounded_corners(self, panel, fill, outline, width=1):
        radius = 10
        for anchor, rx, ry, ox, oy in (("nw", 0, 0, 0, 0), ("ne", 1, 0, -radius, 0),
                                      ("sw", 0, 1, 0, -radius), ("se", 1, 1, -radius, -radius)):
            corner = tk.Canvas(panel, width=radius, height=radius, bg=PAPER, bd=0, highlightthickness=0)
            corner.create_oval(ox, oy, ox+radius*2, oy+radius*2, fill=fill, outline=outline, width=width)
            corner.place(relx=rx, rely=ry, anchor=anchor, bordermode="outside")

    def restore_position(self):
        saved = self.settings.get("position")
        if os.name != "nt" or not isinstance(saved, list) or len(saved) != 2 or not all(isinstance(n, int) for n in saved):
            return
        user = ctypes.windll.user32
        left, top = user.GetSystemMetrics(76), user.GetSystemMetrics(77)
        width, height = user.GetSystemMetrics(78), user.GetSystemMetrics(79)
        x = min(max(saved[0], left), left + width - self.root.winfo_width())
        y = min(max(saved[1], top), top + height - min(self.root.winfo_height(), height))
        user.GetParent.restype = ctypes.c_void_p
        handle = user.GetParent(ctypes.c_void_p(self.root.winfo_id()))
        user.SetWindowPos(ctypes.c_void_p(handle), None, x, y, 0, 0, 0x0015)

    def build(self):
        head = tk.Frame(self.root, bg=PAPER, padx=24, pady=10)
        head.pack(fill="x")
        self.label(head, "持仓执行卡", 20, bold=True).grid(row=0, column=0, sticky="w")
        self.label(head, self.source.label, 10, AMBER, True, bg="#fff1d9", padx=9, pady=5).grid(row=0, column=1, padx=14)
        head.columnconfigure(2, weight=1)
        self.plan_date = self.label(head, "分析待核验", 13, bold=True)
        self.plan_date.grid(row=0, column=2, sticky="e")
        self.label(head, "离线演示 · 不连接账户" if self.source.demo else "本地校验 · 不代表券商已核验", 10, MUTED).grid(row=1, column=0, columnspan=2, sticky="w", pady=(7, 0))
        self.status_label = self.label(head, "正在读取数据", 9, MUTED)
        self.status_label.grid(row=1, column=2, sticky="e", pady=(7, 0))
        tk.Frame(self.root, bg=LINE, height=1).pack(fill="x", padx=24)
        bottom = tk.Frame(self.root, bg=PAPER, padx=24, pady=12)
        bottom.pack(side="bottom", fill="x")
        self.footer = self.label(bottom, "每60秒重读本地数据 · 不联网，不下单", 9, MUTED, anchor="w")
        self.footer.pack(side="bottom", fill="x", pady=(10, 0))
        self.capture_button = self.button(bottom, "重读数据", self.capture, True)
        self.capture_button.pack(side="left", fill="x", expand=True)
        self.button(bottom, "数据说明", self.show_source).pack(side="left", padx=10)
        self.button(bottom, "依据 / 设置", self.show_details).pack(side="left")
        self.compact_button = self.button(bottom, "小窗", self.toggle_compact)
        self.compact_button.pack(side="left", padx=(10, 0))
        self.body = tk.Frame(self.root, bg=PAPER, padx=24, pady=8)
        self.body.pack(fill="both", expand=True)
        self.notice = self.label(self.body, "", 10, AMBER, anchor="w", justify="left", wraplength=710)
        self.notice.pack(fill="x")
        self.selector = ttk.Combobox(self.body, textvariable=self.selection, state="readonly")
        self.selector.bind("<<ComboboxSelected>>", self.select_stock)
        metrics = tk.Frame(self.body, bg=PAPER, pady=2)
        metrics.pack(fill="x")
        for col, weight in enumerate((3, 2, 2, 2)):
            metrics.columnconfigure(col, weight=weight, uniform="metric")
        self.stock_title = self.label(metrics, "等待数据校验", 16, bold=True, anchor="w")
        self.stock_title.grid(row=0, column=0, sticky="w")
        self.stock_code = self.label(metrics, "不沿用旧持仓", 10, MUTED, anchor="w")
        self.stock_code.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.metric_values = []
        for col, caption in enumerate(("持仓 / 可卖", "成本价", "现价 / 浮盈亏"), 1):
            self.label(metrics, caption, 9, MUTED).grid(row=0, column=col, sticky="w")
            value = self.label(metrics, "--", 15, bold=True, family="Bahnschrift")
            value.grid(row=1, column=col, sticky="w", pady=(5, 0))
            self.metric_values.append(value)
        self.pnl_label = self.label(metrics, "", 9, MUTED)
        self.pnl_label.grid(row=2, column=3, sticky="w", pady=(4, 0))
        stop = tk.Frame(self.body, bg=WHITE, highlightbackground=LINE, highlightthickness=1, padx=12, pady=6)
        stop.pack(fill="x", pady=(8, 10))
        self.rounded_corners(stop, WHITE, LINE)
        for col in range(3):
            stop.columnconfigure(col, weight=1, uniform="stop")
        self.stop_heading = self.label(stop, "自定提醒线", 10, INK, True)
        self.stop_heading.grid(row=0, column=0, sticky="w")
        self.stop_value = self.label(stop, "--", 18, MUTED, True, family="Bahnschrift")
        self.stop_value.grid(row=0, column=1, padx=10)
        self.stop_percent = self.label(stop, "涨幅待核", 10, MUTED, family="Bahnschrift")
        self.stop_percent.grid(row=1, column=1)
        self.stop_state = self.label(stop, "输入数据校验后显示", 9, MUTED, anchor="w", justify="left", wraplength=240)
        self.stop_state.grid(row=0, column=2, sticky="w")
        self.stop_note = self.label(stop, "纪律按持仓成本计算，不随昨收或均线下移。", 9, MUTED, anchor="w")
        self.stop_note.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 2))
        self.stop_note.grid_remove()
        self.conclusion = self.label(self.body, "", 11, INK, True, anchor="w", justify="left", wraplength=710)
        self.operation_heading = tk.Frame(self.body, bg=PAPER)
        self.operation_heading.pack(fill="x", pady=(0, 6))
        self.label(self.operation_heading, "具体怎么操作", 16, bold=True).pack(side="left")
        self.percent_basis = self.label(self.operation_heading, "涨幅基准待核", 9, MUTED)
        self.percent_basis.pack(side="right")
        # Keep all scenario choices outside the scroll pane, especially in small mode.
        self.scenario_nav = tk.Frame(self.body, bg=PAPER)
        self.scenario_nav.pack(fill="x", pady=(0, 10))
        region = tk.Frame(self.body, bg=PAPER)
        region.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(region, bg=PAPER, bd=0, highlightthickness=0)
        scroll = ttk.Scrollbar(region, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scenarios = tk.Frame(self.canvas, bg=PAPER)
        self.scenario_window = self.canvas.create_window((0, 0), window=self.scenarios, anchor="nw")
        self.scenarios.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.resize_cards)
        self.root.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))

    def resize_cards(self, event):
        self.canvas.itemconfigure(self.scenario_window, width=event.width)
        width = max(450, self.root.winfo_width() - 70)
        self.conclusion.configure(wraplength=width)
        self.notice.configure(wraplength=width)
        self.stop_note.configure(wraplength=width-40)
        for card in self.scenarios.winfo_children():
            for child in card.winfo_children():
                if hasattr(child, "column_fraction"):
                    child.configure(wraplength=max(90, int((event.width - 38) * child.column_fraction)))

    def select_scenario(self, title):
        self.scenario_selection[self.selected] = title
        self.render_card()
        self.canvas.yview_moveto(0)

    def select_stock(self, _event=None):
        self.selected = self.selection.get().split(" ")[0]
        self.render_card()

    def capture(self):
        if self.capture_pending:
            return
        self.capture_pending = True
        self.capture_button.configure(text="正在读取…", state="disabled")
        self.footer.configure(text="只重读已指定文件，不采集账户、不获取行情、不下单。")
        if not self.busy:
            self.refresh()

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        capture = self.capture_pending
        def worker():
            capture_error = None
            try:
                snapshot = self.source.read()
                if capture_error:
                    snapshot = {**snapshot, "valid": False, "errors": snapshot["errors"] + [capture_error]}
                analyses, problems = self.source.analyze(snapshot)
                self.output.put((snapshot, analyses, problems, None, capture, capture_error))
            except Exception as exc:
                self.output.put(({}, {}, [], type(exc).__name__, capture, capture_error))
        threading.Thread(target=worker, daemon=True, name="position-desk-read-only").start()

    def collect(self):
        try:
            snapshot, analyses, problems, error, captured, capture_error = self.output.get_nowait()
        except queue.Empty:
            self.root.after(200, self.collect)
            return
        self.busy = False
        if captured:
            self.capture_pending = False
            self.capture_button.configure(text="重读数据", state="normal")
            self.capture_message = capture_error or ("本地结构与金额校验通过，不等于券商核验" if snapshot.get("valid") else "数据未通过校验")
            self.footer.configure(text=self.capture_message)
            self.capture_blocked_version = (snapshot.get("version") or self.snapshot.get("version")) if capture_error or error or not snapshot.get("valid") else None
        if not error and self.capture_blocked_version is not None:
            if snapshot.get("version") == self.capture_blocked_version:
                snapshot = {**snapshot, "valid": False, "errors": snapshot.get("errors", []) + [self.capture_message]}
                analyses = {}
            elif snapshot.get("valid"):
                self.capture_blocked_version = None
        if error:
            self.snapshot = {**self.snapshot, "valid": False,
                "errors": ["读取失败，不能把旧数据当作当前持仓；请检查数据文件。"]}
            self.analyses = {}
        else:
            self.snapshot, self.analyses, self.problems = snapshot, analyses, problems
            now = now_local()
            self.book.reconcile(snapshot, now)
            if snapshot["valid"]:
                self.book.apply_cost_stop_latches(list(analyses.values()), now)
            due = self.book.due(snapshot, list(analyses.values()), now, int(self.repeat.get()))
            if due:
                self.alert(due)
        active = {(x["code"], x["fingerprint"]) for x in self.analyses.values()
                  if self.snapshot.get("valid") and x["severity"] == "risk"}
        if self.popup and self.popup.winfo_exists() and not self.popup_keys.issubset(active):
            self.popup.destroy()
        self.render()
        self.save()
        if self.capture_pending:
            self.refresh()
        self.root.after(200, self.collect)

    def poll(self):
        self.refresh()
        self.root.after(60000, self.poll)

    def render(self):
        valid = self.snapshot.get("valid", False)
        rows = self.snapshot.get("rows", []) if valid else []
        self.selector.configure(values=[f"{r['code']} {r['name']}" for r in rows])
        if len(rows) > 1:
            self.selector.pack(before=self.stock_title.master, fill="x", pady=(5, 0))
        else:
            self.selector.pack_forget()
        if self.selected not in {str(r["code"]) for r in rows}:
            self.selected = str(rows[0]["code"]) if rows else None
        row = next((r for r in rows if str(r["code"]) == self.selected), None)
        if row:
            self.selection.set(f"{row['code']} {row['name']}")
        captured = str(self.snapshot.get("captured_at", "未知")).replace("T", " ")[:16]
        self.status_label.configure(text="文件 " + captured + (" · 校验通过" if valid else " · 待更新"))
        errors = self.snapshot.get("errors", []) + self.problems
        notice = errors[0] if errors else ("虚构价格与预案，只演示界面；不弹交易提醒。" if self.source.demo
                else ("导入文件为空仓，不代表券商核验；已结束对应提醒。" if not rows else "名称和价格仅作文件内一致性检查，请核对实际账户。"))
        self.notice.configure(text=notice)
        if notice:
            self.notice.pack(before=self.stock_title.master, fill="x")
        else:
            self.notice.pack_forget()
        self.render_card()

    def render_card(self):
        valid = self.snapshot.get("valid", False)
        row = next((r for r in self.snapshot.get("rows", []) if valid and str(r["code"]) == self.selected), None)
        item = self.analyses.get(self.selected) if valid else None
        self.stock_title.configure(text=row["name"] if row else ("导入空仓" if valid else "数据待更新"))
        self.stock_code.configure(text=str(row["code"]) if row else "不以历史记录代替当前持仓")
        values = (f"{int(row['quantity'])} / {int(row['sellable_quantity'])}", f"{row['cost_price']:.4f}",
                  f"{item['price']:.2f}" if item else "待核验") if row else ("--", "--", "--")
        for widget, value in zip(self.metric_values, values):
            widget.configure(text=value, fg=INK)
        self.pnl_label.configure(text="")
        if item:
            self.stop_heading.configure(text=f"自定 · 成本 -{item['stop_loss_pct']:g}%")
            color = RED if item["pnl"] > 0 else GREEN if item["pnl"] < 0 else MUTED
            self.metric_values[2].configure(fg=color)
            self.pnl_label.configure(text=f"{item['pnl']:+.2f}元 / {(item['price']/item['cost']-1)*100:+.2f}%", fg=color)
            reference_date = str(item.get("reference_date", ""))
            reference = item.get("reference_close")
            stop_change = level_percent(item["cost_stop"], reference)
            self.stop_value.configure(text=f"{item['cost_stop']:.2f}",
                fg=PERCENT_COLORS[percent_tag(stop_change)] if len(reference_date) == 8 else MUTED)
            self.stop_percent.configure(text=f"较{reference_date[4:6]}/{reference_date[6:8]} {stop_change}"
                if stop_change != "涨幅待核" and len(reference_date) == 8 else "涨幅待核",
                fg=PERCENT_COLORS[percent_tag(stop_change)] if len(reference_date) == 8 else MUTED)
            self.percent_basis.configure(text=f"较{reference_date[4:6]}/{reference_date[6:8]}收{reference:.2f}"
                if reference and len(reference_date) == 8 else "涨幅基准待核")
            self.stop_state.configure(text=("已触发" if item["severity"] == "risk" else "未触发") + f" · 距离 {item['distance']:+.2f}",
                                      fg=GREEN if item["severity"] == "risk" else AMBER)
            self.stop_note.configure(text=f"成本 {item['cost']:.4f} × (1-{item['stop_loss_pct']:g}%) · 触发价不是成交保证 · 估算亏损 {abs(item['loss_at_stop']):.2f}元（未计卖出费用）")
            self.plan_date.configure(text=("演示计划 " if self.source.demo else "适用日期 ") + item["target_day"][5:].replace("-", "/"))
        else:
            self.stop_heading.configure(text="自定提醒线")
            self.stop_value.configure(text="--", fg=MUTED)
            self.stop_percent.configure(text="涨幅待核", fg=MUTED)
            self.percent_basis.configure(text="涨幅基准待核")
            self.stop_state.configure(text="输入数据校验后显示", fg=MUTED)
            self.stop_note.configure(text="按输入的成本和提醒比例计算；数据未校验时暂停提醒。")
            self.plan_date.configure(text="分析待核验")
        plan = item.get("specific", {}) if item else {}
        self.conclusion.configure(text=plan.get("short_conclusion", plan.get("conclusion", item.get("plan_problem", "") if item else "先导入有效数据，再显示对应预案。")))
        previous_scroll = self.canvas.yview()[0]
        for child in self.scenarios.winfo_children():
            child.destroy()
        scenarios = plan.get("scenarios", [])
        for index in range(len(self.scenario_buttons)):
            self.scenario_nav.columnconfigure(index, weight=0, minsize=0, uniform="")
        for child in self.scenario_nav.winfo_children():
            child.destroy()
        self.scenario_buttons = []
        self.action_title = self.action_body = self.action_trigger = self.action_context = self.scenario_detail_button = None
        if scenarios:
            if item["severity"] == "risk" and self.selected not in self.risk_scenario_shown:
                self.scenario_selection.pop(self.selected, None)
                self.risk_scenario_shown.add(self.selected)
            elif item["severity"] != "risk":
                self.risk_scenario_shown.discard(self.selected)
            default = scenarios[0 if item["severity"] == "risk" else min(1, len(scenarios)-1)]
            selected_title = self.scenario_selection.get(self.selected, default["title"])
            selected = next((s for s in scenarios if s["title"] == selected_title), default)
            self.scenario_selection[self.selected] = selected["title"]
            for index, scenario in enumerate(scenarios):
                self.scenario_nav.columnconfigure(index, weight=1, uniform="choice")
                active = scenario is selected
                button = tk.Button(self.scenario_nav, text=scenario.get("short_title", scenario["title"]),
                    command=lambda s=scenario: self.select_scenario(s["title"]), relief="flat", bd=0,
                    padx=4, pady=10, bg=INK if active else "#e9edf2", fg=WHITE if active else INK,
                    font=("Microsoft YaHei UI", 10, "bold"), cursor="hand2")
                button.grid(row=0, column=index, sticky="ew", padx=(0, 4 if index < len(scenarios)-1 else 0))
                self.scenario_buttons.append(button)
            card = tk.Frame(self.scenarios, bg=WHITE, padx=16, pady=12, highlightthickness=1, highlightbackground=LINE)
            card.pack(fill="x")
            self.rounded_corners(card, WHITE, LINE)
            def paragraph(text, size, color=INK, bold=False, gap=6):
                if PERCENT_PATTERN.search(text):
                    widget = PercentText(card, text, size, color, bold)
                else:
                    widget = self.label(card, text, size, color, bold, anchor="w", justify="left",
                        wraplength=max(420, self.canvas.winfo_width()-38))
                    widget.column_fraction = 1.0
                widget.pack(fill="x", pady=(0, gap))
                return widget
            if item["severity"] == "risk":
                paragraph("已触及自定提醒线 · 先核对可卖", 12, GREEN, True)
            # Direction belongs to the authored scenario, not its title or selection state.
            tone = selected.get("tone")
            self.action_context = paragraph(SCENARIO_LABELS.get(tone, "情景预案") + " · " +
                "需盘中确认", 10, SCENARIO_COLORS.get(tone, MUTED))
            action_color = RED if tone == "positive" else INK
            view = quick_view(selected)
            if view:
                priced = lambda text: annotate_levels(text, plan.get("price_levels"), item.get("reference_close"))
                self.action_title = paragraph(priced(view["action"]), 23, action_color, True, 8)
                paragraph("以下条件同时满足", 9, MUTED, gap=5)
                self.action_trigger = paragraph(priced("\n".join(view["conditions"])), 13, INK, False, 8)
                self.action_body = paragraph(priced(view["otherwise"]), 11, INK, False, 10)
            else:
                self.action_title = paragraph("简版待核验", 23, MUTED, True, 12)
                self.action_trigger = paragraph("请查看完整条件，不按标题直接操作。", 12, MUTED)
                self.action_body = paragraph("", 11)
            paragraph(f"≤ {item['cost_stop']:.2f}({level_percent(item['cost_stop'], item.get('reference_close'))}) 止损优先 · 核对可卖", 10, AMBER, gap=8)
            self.scenario_detail_button = self.button(card, "完整分析", lambda s=selected: self.show_scenario(s))
            self.scenario_detail_button.configure(pady=3, padx=10, font=("Microsoft YaHei UI", 10))
            self.scenario_detail_button.pack(anchor="w")
        if not scenarios:
            self.label(self.scenarios, "分析待更新 · 暂无有效预案", 11, MUTED,
                anchor="w", justify="left", wraplength=430, pady=18).pack(fill="x")
        self.canvas.yview_moveto(previous_scroll)

    def text_dialog(self, title, text):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.configure(bg=PAPER)
        window.geometry("740x640")
        self.label(window, title, 18, bold=True).pack(anchor="w", padx=22, pady=18)
        panel = tk.Frame(window, bg=PAPER, padx=22, pady=12)
        panel.pack(fill="both", expand=True)
        window.content_panel = panel
        content = tk.Text(panel, wrap="word", bg=PAPER, fg=INK, relief="flat", padx=6, spacing3=10)
        scroll = ttk.Scrollbar(panel, command=content.yview)
        content.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        content.pack(side="left", fill="both", expand=True)
        content.insert("1.0", text)
        color_percentages(content, text)
        content.configure(state="disabled")
        return window

    def show_scenario(self, scenario):
        self.text_dialog(scenario["title"], "成立条件\n" + scenario["trigger"] + "\n\n具体应对\n" + scenario["action"] + "\n\n为什么\n" + scenario["reason"] + "\n\n这是条件预案，不代表盘中条件已经发生。")

    def show_details(self):
        item = self.analyses.get(self.selected, {}) if self.snapshot.get("valid") else {}
        plan = item.get("specific", {})
        text = "分析依据截至：" + str(item.get("analysis_asof") or "待更新")
        text += "\n行情时间：" + str(item.get("quote_at", "未核验"))
        text += "\n\n总体判断\n" + plan.get("conclusion", "待核验")
        text += "\n\n成本纪律\n" + self.stop_note.cget("text")
        text += "\n\n价位涨跌幅\n" + self.percent_basis.cget("text") + "；(目标价 / 基准收盘价 - 1) × 100%。\n这是目标价对应的涨跌幅，不是预测；与成本盈亏和自定提醒比例分开。"
        for title, key in (("逐股判断依据", "facts"), ("仍待确认", "unknowns"), ("数据来源", "sources")):
            text += "\n\n" + title + "\n" + "\n\n".join(plan.get(key, ["未核验，不推断。 "]))
        text += "\n\n每60秒重读指定文件，不联网、不生成分析。预案由用户提供，数值校验不代表市场判断成立；需自行核对名称、行情来源、交易日历及除权变化。持仓、成本、提醒比例或适用日变化后旧计划失效。"
        text += "\n\n提醒记录\n" + "\n".join(e["at"][5:16].replace("T", " ") + "  " + e["text"] for e in self.book.state["events"][:12])
        window = self.text_dialog("分析依据与提醒设置", text)
        bar = tk.Frame(window, bg=PAPER, padx=22, pady=12)
        window.content_panel.pack_forget()
        bar.pack(side="bottom", fill="x")
        window.content_panel.pack(fill="both", expand=True)
        for label, var in (("置顶", self.topmost), ("声音", self.sound)):
            tk.Checkbutton(bar, text=label, variable=var, command=self.options_changed, bg=PAPER).pack(side="left")
        self.label(bar, "重复间隔(分)", 9, MUTED).pack(side="left", padx=10)
        combo = ttk.Combobox(bar, textvariable=self.repeat, values=("5", "15", "30", "60"), width=3, state="readonly")
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _: self.save())
        self.button(bar, "稍后提醒", self.snooze).pack(side="right")

    def show_source(self):
        self.text_dialog("数据说明", "当前模式：" + self.source.label +
            "\n\n演示使用固定的虚构日期、证券和价格；不代表任何真实证券。\n\n"
            "本地模式只读取启动时明确指定的 account.json；不会扫描其他目录、启动券商终端或运行采集脚本。\n\n"
            "重读数据不等于重新采集。外部程序需先写临时文件并原子替换 account.json。\n\n"
            "本应用检查结构、日期、字段一致性及金额，不对券商截图、证券名称或行情来源作独立认证。\n\n"
            "不上传、不联网、不下单。分享问题时请只使用虚构演示数据。")

    def toggle_compact(self):
        self.compact = not self.compact
        self.root.geometry(f"560x{min(880, self.full_height)}" if self.compact else f"800x{self.full_height}")
        self.compact_button.configure(text="展开" if self.compact else "小窗")

    def options_changed(self):
        self.root.attributes("-topmost", self.topmost.get())
        self.save()

    def snooze(self):
        self.book.snooze(int(self.repeat.get()), now_local())
        if self.popup and self.popup.winfo_exists():
            self.popup.destroy()
        self.footer.configure(text=f"已稍后提醒 {self.repeat.get()} 分钟 · 持仓跟踪继续")
        self.save()

    def alert(self, items):
        if self.popup and self.popup.winfo_exists():
            self.popup.destroy()
        self.popup_keys = {(x["code"], x["fingerprint"]) for x in items}
        self.popup = self.text_dialog("成本止损条件已触发", "\n\n".join(
            f"{x['name']}：{x['action']}\n{x['conditions']}" for x in items))
        self.popup.attributes("-topmost", True)
        bar = tk.Frame(self.popup, bg=PAPER)
        self.popup.content_panel.pack_forget()
        bar.pack(side="bottom", pady=12)
        self.popup.content_panel.pack(fill="both", expand=True)
        self.button(bar, "已读，继续跟踪", self.popup.destroy, True).pack(side="left", padx=8)
        self.button(bar, "稍后提醒", self.snooze).pack(side="left")
        if self.sound.get():
            self.root.bell()

    def save(self):
        self.settings.update(topmost=self.topmost.get(), sound=self.sound.get(), repeat_minutes=int(self.repeat.get()), reminders=self.book.state,
            position=[self.root.winfo_x(), self.root.winfo_y()])
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=self.state_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    json.dump(self.settings, file, ensure_ascii=False, indent=2, allow_nan=False)
                os.replace(name, self.state_path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self.state_error = ""
        except OSError:
            self.state_error = "设置无法保存，重启后提醒记录可能丢失。"
            self.footer.configure(text=self.state_error)

    def close(self):
        answer = messagebox.askyesnocancel("持仓执行卡", "是否最小化并继续运行？\n\n是：继续运行\n否：退出应用\n取消：返回窗口", parent=self.root)
        if answer is True:
            self.root.iconify()
        elif answer is False:
            if self.busy and self.capture_pending:
                messagebox.showinfo("正在读取", "请等读取完成再退出。", parent=self.root)
                return
            self.save()
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="Use fictional offline data (default)")
    mode.add_argument("--data-dir", type=Path, help="Explicit directory containing account.json")
    parser.add_argument("--state-dir", type=Path, help="Override the isolated local settings directory")
    parser.add_argument("--check", action="store_true", help="Validate input without starting the UI")
    args = parser.parse_args()
    source = FileSource(args.data_dir)
    if args.check:
        snapshot = source.read()
        items, problems = source.analyze(snapshot)
        print(json.dumps({"mode": source.label, "valid": snapshot["valid"],
            "rows": len(snapshot["rows"]), "plans": sum(bool(x["specific"]) for x in items.values()),
            "errors": snapshot["errors"] + problems}, ensure_ascii=False))
        return 0 if snapshot["valid"] else 1
    source_id = hashlib.sha256(str(source.path.resolve()).encode()).hexdigest()[:16]
    state_dir = args.state_dir or Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PositionDeskPublic" / source_id
    mutex = None
    if os.name == "nt":
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        kernel = ctypes.windll.kernel32
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, "Local\\PositionDeskPublic-" + source_id)
        if kernel.GetLastError() == 183:
            ctypes.windll.user32.MessageBoxW(None, "此数据源已在运行，请从任务栏打开。", "持仓执行卡", 64)
            return
    root = tk.Tk()
    PositionDesk(root, state_dir, source)
    root.mainloop()
    if mutex:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))


if __name__ == "__main__":
    raise SystemExit(main())
