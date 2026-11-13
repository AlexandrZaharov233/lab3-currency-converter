"""Рабочее окно конвертера и истории курсов BNM."""

from __future__ import annotations

import logging
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import date
from decimal import Decimal
from tkinter import ttk
from typing import Callable

from app import theme
from app.chart import RateChart
from app.core import (
    SUPPORTED_CURRENCIES,
    RateSnapshot,
    ValidationError,
    convert_with_details,
    format_date,
    parse_amount,
    parse_date,
    validate_currency,
)
from app.history import HistoryWindow, RateHistory, RateSeries
from app.rates import HttpError, NetworkError, RateResult, RatesFormatError, RatesService, RatesServiceError


PERIOD_LABELS = {7: "7 дней", 30: "30 дней", 90: "90 дней", 365: "1 год"}
POPULAR = ("EUR", "USD", "RON", "GBP")
BNM_PAGE = "https://www.bnm.md/ru/content/obmennye-stavki"
LOG = logging.getLogger(__name__)
ConversionInput = tuple[Decimal, str, str, date]


def button(parent, text, command, style="Quiet.TButton", **options):
    options.setdefault("width", 0)
    widget = ttk.Button(parent, text=text, command=command, style=style, **options)

    def invoke(_event):
        widget.invoke()
        return "break"

    widget.bind("<Return>", invoke)
    return widget


def panel(parent, background=theme.PANEL, border=theme.BORDER):
    return tk.Frame(parent, background=background, highlightbackground=border, highlightthickness=1)


class QuoteTile(tk.Frame):
    def __init__(self, parent, code: str, choose: Callable[[str], None]):
        super().__init__(parent, background=theme.PANEL, highlightbackground=theme.BORDER,
                         highlightthickness=1, takefocus=True, cursor="hand2")
        self.code = code
        self.choose = choose
        self.active = False
        self.enabled = True
        name, symbol, tile_bg, tile_fg = theme.CURRENCIES[code]
        badge = tk.Label(self, text=symbol, font=(theme.UI, -20, "bold"), width=2,
                         background=tile_bg, foreground=tile_fg, padx=3, pady=6)
        badge.grid(row=0, column=0, rowspan=2, padx=(13, 11), pady=13)
        title = theme.label(self, f"{code} / MDL", font=theme.LABEL)
        title.grid(row=0, column=1, sticky="sw", pady=(11, 0))
        self.value = theme.label(self, name, font=theme.SMALL, color=theme.MUTED)
        self.value.grid(row=1, column=1, sticky="nw", pady=(3, 11))
        self.sparkline = tk.Canvas(self, background=theme.PANEL, borderwidth=0,
                                   width=58, height=34, highlightthickness=0)
        self.sparkline.grid(row=0, column=2, rowspan=2, sticky="e", padx=(7, 14))
        self.columnconfigure(1, weight=1)
        for widget in (self, badge, title, self.value, self.sparkline):
            widget.bind("<Button-1>", self._activate)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<FocusIn>", lambda _event: self._outline())
        self.bind("<FocusOut>", lambda _event: self._outline())
        self.bind("<Enter>", lambda _event: self._outline(True))
        self.bind("<Leave>", lambda _event: self._outline())

    def _activate(self, _event):
        if self.enabled:
            self.focus_set()
            self.choose(self.code)
        return "break"

    def _outline(self, hover=False):
        border = theme.BORDER
        if self.active or self.focus_get() == self:
            border = theme.LIME
        elif hover and self.enabled:
            border = theme.MUTED
        self.configure(highlightbackground=border, highlightcolor=border)

    def set_active(self, active: bool) -> None:
        self.active = active
        self._outline()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "", takefocus=enabled)

    def show(self, series: RateSeries) -> None:
        points = series.points
        self.sparkline.delete("all")
        if not points:
            self.value.configure(text=theme.CURRENCIES[self.code][0], foreground=theme.MUTED)
            return
        change = series.change
        rate = theme.number(points[-1].value)
        change_text = "" if change is None else f"  ·  {'+' if change > 0 else ''}{theme.number(change, 2)}%"
        self.value.configure(text=rate + change_text, foreground=theme.SECONDARY)
        if len(points) < 2:
            return
        low, high = min(p.value for p in points), max(p.value for p in points)
        duration = (points[-1].day - points[0].day).days
        coords = []
        for point in points:
            x = 2 + 54 * (point.day - points[0].day).days / duration
            y = 17 if high == low else 29 - float((point.value - low) / (high - low)) * 24
            coords.extend((x, y))
        color = theme.DOWN if change < 0 else theme.LIME
        self.sparkline.create_line(1, 31, 57, 31, fill=theme.GRID)
        self.sparkline.create_line(*coords, width=2, fill=color, joinstyle="round")
        x, y = coords[-2:]
        self.sparkline.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")


class CurrencyConverterApp:
    def __init__(
        self,
        root: tk.Tk,
        service: RatesService | None = None,
        ask_consent_fn: Callable[[str, str], bool] | None = None,
        *,
        history: RateHistory | None = None,
    ):
        self.root = root
        self.service = service or RatesService()
        self.history = history or RateHistory(
            self.service.read_snapshot, self.service.cache.path.with_name("history.json")
        )
        self._ask_consent_fn = ask_consent_fn
        self.queue = queue.Queue()
        self.is_busy = False
        self._closed = False
        self._period = 30
        self._history_job = None
        self._history_after = None
        self._title_after = None
        self._history_pending = None
        self._history_cancel = None
        self._history_window: HistoryWindow | None = None
        self._history_key: tuple[date, int] | None = None
        self._pending_cache: tuple[RateSnapshot, ConversionInput] | None = None
        self._copied_text = ""
        self._compact = None

        self.amount_var = tk.StringVar(root, "100")
        self.from_currency_var = tk.StringVar(root, "EUR")
        self.to_currency_var = tk.StringVar(root, "MDL")
        self.date_var = tk.StringVar(root, format_date(date.today()))
        self._init_window()
        self._build()
        self.amount_var.trace_add("write", self._amount_edited)
        self.date_var.trace_add("write", self._market_edited)
        self.from_currency_var.trace_add("write", self._market_edited)
        self.to_currency_var.trace_add("write", self._market_edited)
        self._render_pair()
        self._validate_inputs(show=False)
        self._queue_after = self.root.after(35, self._drain_queue)
        self._history_after = self.root.after_idle(self._request_history)

    def _init_window(self) -> None:
        self.root.title("Курсограф · Конвертер валют BNM")
        self.root.configure(background=theme.BG)
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width, height = min(1270, screen_w - 80), min(920, screen_h - 100)
        self.root.geometry(f"{width}x{height}+{(screen_w-width)//2}+{(screen_h-height)//2}")
        self.root.minsize(610, 520)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        theme.style_widgets(self.root)
        if sys.platform == "win32":
            self._title_after = self.root.after_idle(self._dark_title_bar)

    def _dark_title_bar(self) -> None:
        import ctypes

        self._title_after = None
        try:
            handle = ctypes.windll.user32.GetParent(self.root.winfo_id())
            enabled = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(handle, 20, ctypes.byref(enabled), 4)
        except (AttributeError, OSError):
            pass

    def _build(self) -> None:
        header = tk.Frame(self.root, background=theme.HEADER)
        header.pack(fill="x")
        brand = tk.Canvas(header, width=37, height=37, background=theme.HEADER, highlightthickness=0)
        brand.pack(side="left", padx=(28, 12), pady=13)
        theme.round_rect(brand, 0, 0, 37, 37, 9, fill=theme.LIME, outline="")
        brand.create_line(8, 24, 14, 18, 19, 22, 28, 12, width=2.5, fill=theme.BG, joinstyle="round")
        brand.create_line(21, 12, 28, 12, 28, 19, width=2.5, fill=theme.BG, joinstyle="round")
        theme.label(header, "Курсограф", bg=theme.HEADER, font=(theme.UI, -20, "bold")).pack(side="left")
        button(header, "Источник BNM  ↗", lambda: webbrowser.open(BNM_PAGE), "Source.TButton").pack(
            side="right", padx=28, pady=12,
        )
        tk.Frame(self.root, background=theme.BORDER, height=1).pack(fill="x")

        viewport_frame = tk.Frame(self.root, background=theme.BG)
        viewport_frame.pack(fill="both", expand=True)
        viewport_frame.columnconfigure(0, weight=1)
        viewport_frame.rowconfigure(0, weight=1)
        self.viewport = tk.Canvas(viewport_frame, background=theme.BG, highlightthickness=0,
                                  borderwidth=0, yscrollincrement=18)
        self.viewport.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(viewport_frame, orient="vertical", command=self.viewport.yview)
        self.viewport.configure(yscrollcommand=self.scrollbar.set)
        self.page = tk.Frame(self.viewport, background=theme.BG, padx=27)
        self.page_window = self.viewport.create_window(0, 0, window=self.page, anchor="nw")
        self.viewport.bind("<Configure>", self._layout)
        self.page.bind("<Configure>", self._update_scroll)
        self.root.bind("<MouseWheel>", self._scroll_wheel, add="+")
        self.root.bind("<Button-4>", self._scroll_wheel, add="+")
        self.root.bind("<Button-5>", self._scroll_wheel, add="+")
        self.root.bind("<FocusIn>", self._scroll_to_focus, add="+")

        intro = tk.Frame(self.page, background=theme.BG)
        intro.pack(fill="x", pady=(20, 21))
        theme.label(intro, "Конвертер валют", bg=theme.BG, font=theme.TITLE).pack(anchor="w")
        theme.label(
            intro, "Расчёт и история официальных курсов Национального банка Молдовы",
            bg=theme.BG, color=theme.SECONDARY,
        ).pack(anchor="w", pady=(3, 0))
        self.workspace = tk.Frame(self.page, background=theme.BG)
        self.workspace.pack(fill="x")
        self.converter = panel(self.workspace)
        self.market = panel(self.workspace)
        self._build_converter()
        self._build_market()
        self._build_quotes()

    def _build_converter(self) -> None:
        content = tk.Frame(self.converter, background=theme.PANEL, padx=20, pady=18)
        content.pack(fill="both", expand=True)
        theme.label(content, "Конвертация", font=theme.SECTION).pack(anchor="w")

        theme.label(content, "Сумма", font=theme.LABEL, color=theme.SECONDARY).pack(anchor="w", pady=(16, 5))
        amount_box, self.amount_entry = self._entry_box(content, self.amount_var, (theme.UI, -24, "bold"))
        amount_box.pack(fill="x")
        self.amount_code = theme.label(amount_box, bg=theme.FIELD, color=theme.MUTED, font=theme.LABEL)
        self.amount_code.pack(side="right", padx=(0, 13))

        pair = tk.Frame(content, background=theme.PANEL)
        pair.pack(fill="x", pady=(13, 0))
        pair.columnconfigure(0, weight=1, uniform="pair")
        pair.columnconfigure(2, weight=1, uniform="pair")
        self.from_combo, self.from_symbol = self._currency_box(pair, 0, "Из валюты", self.from_currency_var)
        self.to_combo, self.to_symbol = self._currency_box(pair, 2, "В валюту", self.to_currency_var)
        self.swap_btn = button(pair, "⇄", self._swap_currencies, width=2)
        self.swap_btn.grid(row=1, column=1, padx=8, sticky="ns")

        theme.label(content, "Дата курса", font=theme.LABEL, color=theme.SECONDARY).pack(anchor="w", pady=(13, 5))
        date_row = tk.Frame(content, background=theme.PANEL)
        date_row.pack(fill="x")
        date_box, self.date_entry = self._entry_box(date_row, self.date_var, theme.BODY, pady=8)
        self.today_btn = button(date_row, "Сегодня", self._set_today)
        self.today_btn.pack(side="right", padx=(8, 0), fill="y")
        date_box.pack(side="left", fill="x", expand=True)

        self.convert_btn = button(content, "Конвертировать  →", self.start_conversion, "Accent.TButton")
        self.convert_btn.pack(fill="x", pady=(14, 0))
        self.amount_entry.bind("<Return>", self._enter_conversion)
        self.date_entry.bind("<Return>", self._enter_conversion)

        result = panel(content, theme.GREEN_SURFACE, theme.GREEN_BORDER)
        result.pack(fill="x", pady=(15, 0))
        result_inner = tk.Frame(result, background=theme.GREEN_SURFACE, padx=13, pady=10)
        result_inner.pack(fill="x")
        heading = tk.Frame(result_inner, background=theme.GREEN_SURFACE)
        heading.pack(fill="x")
        theme.label(heading, "Результат", bg=theme.GREEN_SURFACE, color=theme.GREEN_TEXT,
                    font=theme.LABEL).pack(side="left")
        self.copy_btn = button(heading, "Копировать", self._copy_result, "Copy.TButton")
        self.copy_btn.pack(side="right")
        self.copy_btn.state(["disabled"])

        value_line = tk.Frame(result_inner, background=theme.GREEN_SURFACE)
        value_line.pack(fill="x", pady=(0, 4))
        self.result_value = theme.label(value_line, "—", bg=theme.GREEN_SURFACE,
                                        color=theme.INK, font=(theme.UI, -33, "bold"))
        self.result_value.pack(side="left")
        self.result_code = theme.label(value_line, bg=theme.GREEN_SURFACE,
                                       color=theme.GREEN_TEXT, font=theme.LABEL)
        self.result_code.pack(side="left", padx=(9, 0), pady=(8, 0))
        self.result_rate = theme.label(result_inner, "Курс за выбранную дату", bg=theme.GREEN_SURFACE,
                                       color=theme.SECONDARY, font=theme.SMALL)
        self.result_rate.pack(anchor="w")
        self.result_source = theme.label(result_inner, "", bg=theme.GREEN_SURFACE,
                                         color=theme.SECONDARY, font=theme.SMALL)

        self.status_area = tk.Frame(content, background=theme.PANEL)
        self.status_area.pack(fill="x")
        self.status_label = theme.label(self.status_area, color=theme.MUTED, font=theme.SMALL, justify="left")
        self.status_area.bind("<Configure>", lambda event: self.status_label.configure(wraplength=event.width))
        self.cache_actions = tk.Frame(self.status_area, background=theme.PANEL)
        button(self.cache_actions, "Использовать кэш", self._use_cache).pack(side="left")
        button(self.cache_actions, "Отмена", self._discard_cache).pack(side="left", padx=(7, 0))

    def _entry_box(self, parent, variable, font, pady=7):
        frame = panel(parent, theme.FIELD)
        entry = tk.Entry(frame, textvariable=variable, width=6, font=font, background=theme.FIELD,
                         foreground=theme.INK, disabledbackground=theme.FIELD, disabledforeground=theme.MUTED,
                         insertbackground=theme.LIME, selectbackground=theme.GREEN_BORDER,
                         selectforeground=theme.INK, borderwidth=0, highlightthickness=0)
        entry.pack(side="left", padx=(11, 4), pady=pady, fill="x", expand=True)
        entry.bind("<FocusIn>", lambda _event: frame.configure(highlightbackground=theme.LIME))
        entry.bind("<FocusOut>", lambda _event: frame.configure(highlightbackground=theme.BORDER))
        return frame, entry

    def _currency_box(self, parent, column, name, variable):
        theme.label(parent, name, font=theme.LABEL, color=theme.SECONDARY).grid(
            row=0, column=column, sticky="w", pady=(0, 5),
        )
        frame = panel(parent, theme.FIELD)
        frame.grid(row=1, column=column, sticky="nsew")
        symbol = tk.Label(frame, background=theme.FIELD, foreground=theme.INK,
                          width=2, font=(theme.UI, -15, "bold"))
        symbol.pack(side="left", padx=(6, 0), pady=5)
        combo = ttk.Combobox(
            frame, style="Currency.TCombobox", textvariable=variable, values=SUPPORTED_CURRENCIES,
            state="readonly", width=4, font=(theme.UI, -15, "bold"), height=len(SUPPORTED_CURRENCIES),
        )
        combo.pack(side="left", fill="both", expand=True)
        return combo, symbol

    def _build_market(self) -> None:
        content = tk.Frame(self.market, background=theme.PANEL, padx=20, pady=18)
        content.pack(fill="both", expand=True)
        header = tk.Frame(content, background=theme.PANEL)
        header.pack(fill="x")
        theme.label(header, "Динамика курса", font=theme.SECTION).pack(side="left")
        periods = panel(header, theme.FIELD)
        periods.pack(side="right")
        self.period_buttons = {}
        for days, title in PERIOD_LABELS.items():
            period_button = button(periods, title, lambda value=days: self._choose_period(value), "Period.TButton")
            period_button.pack(side="left", padx=2, pady=2)
            self.period_buttons[days] = period_button
        self.period_buttons[self._period].configure(style="ActivePeriod.TButton")

        market_summary = tk.Frame(content, background=theme.PANEL)
        market_summary.pack(fill="x", pady=(17, 3))
        self.market_pair = theme.label(market_summary, font=theme.LABEL, color=theme.SECONDARY)
        self.market_pair.pack(anchor="w")
        market_numbers = tk.Frame(market_summary, background=theme.PANEL)
        market_numbers.pack(fill="x", pady=(2, 0))
        self.market_value = theme.label(market_numbers, "—", font=theme.FIGURE)
        self.market_value.pack(side="left")
        self.market_unit = theme.label(market_numbers, font=theme.LABEL, color=theme.MUTED)
        self.market_unit.pack(side="left", padx=(8, 12), pady=(9, 0))
        self.change_badge = tk.Label(market_numbers, font=theme.LABEL, padx=8, pady=4, borderwidth=0)
        self.market_date = theme.label(market_numbers, font=theme.SMALL, color=theme.MUTED)
        self.market_date.pack(side="right", pady=(9, 0))

        self.chart = RateChart(content)
        self.chart.pack(fill="both", expand=True, pady=(1, 3))
        tk.Frame(content, background=theme.BORDER, height=1).pack(fill="x", pady=(4, 10))
        metrics = tk.Frame(content, background=theme.PANEL)
        metrics.pack(fill="x")
        self.metrics = {}
        for column, (key, caption) in enumerate((
            ("low", "Минимум"), ("high", "Максимум"), ("count", "Наблюдения"),
        )):
            cell = tk.Frame(metrics, background=theme.PANEL)
            metrics.columnconfigure(column, weight=1, uniform="metrics")
            cell.grid(row=0, column=column, sticky="ew")
            theme.label(cell, caption, font=theme.SMALL, color=theme.MUTED).pack(anchor="w")
            self.metrics[key] = theme.label(cell, "—", font=(theme.MONO, -17, "bold"))
            self.metrics[key].pack(anchor="w", pady=(2, 0))

        footer = tk.Frame(content, background=theme.PANEL)
        footer.pack(fill="x", pady=(8, 0))
        self.refresh_btn = button(footer, "Обновить  ↻", lambda: self._request_history(force=True), "Period.TButton")
        self.refresh_btn.pack(side="right")
        self.market_status = theme.label(footer, font=theme.SMALL, color=theme.MUTED, justify="left")
        self.market_status.pack(side="left", fill="x", expand=True)
        self.market_status.bind("<Configure>", lambda event: self.market_status.configure(wraplength=event.width))

    def _build_quotes(self) -> None:
        heading = tk.Frame(self.page, background=theme.BG)
        heading.pack(fill="x", pady=(17, 9))
        theme.label(heading, "Популярные пары", bg=theme.BG, font=theme.SECTION).pack(side="left")
        theme.label(heading, "Курс за 1 единицу валюты", bg=theme.BG, font=theme.SMALL,
                    color=theme.MUTED).pack(side="right")
        self.quotes = tk.Frame(self.page, background=theme.BG)
        self.quotes.pack(fill="x", pady=(0, 20))
        self.quote_tiles = {code: QuoteTile(self.quotes, code, self._choose_pair) for code in POPULAR}

    def _layout(self, event) -> None:
        width = min(1390, event.width)
        self.viewport.coords(self.page_window, (event.width - width) // 2, 0)
        self.viewport.itemconfigure(self.page_window, width=width)
        compact = width < 980
        if compact != self._compact:
            self._compact = compact
            self.workspace.columnconfigure(0, weight=1 if compact else 0, minsize=0 if compact else 366)
            self.workspace.columnconfigure(1, weight=0 if compact else 1, minsize=0)
            self.converter.grid(row=0, column=0, sticky="nsew", padx=(0, 0 if compact else 13))
            self.market.grid(row=1 if compact else 0, column=0 if compact else 1, sticky="nsew",
                             pady=(13 if compact else 0, 0))
            columns = 2 if compact else 4
            for index in range(4):
                self.quotes.columnconfigure(index, weight=1 if index < columns else 0, uniform="quotes")
            for index, tile in enumerate(self.quote_tiles.values()):
                tile.grid(row=index // columns, column=index % columns, sticky="nsew",
                          padx=(0, 0 if index % columns == columns - 1 else 9), pady=(0, 7))
        self._update_scroll()

    def _update_scroll(self, _event=None) -> None:
        page_height = self.page.winfo_height()
        self.viewport.configure(scrollregion=(0, 0, self.viewport.winfo_width(), page_height))
        if page_height > self.viewport.winfo_height():
            self.scrollbar.grid(row=0, column=1, sticky="ns")
        else:
            self.scrollbar.grid_remove()
            self.viewport.yview_moveto(0)

    def _scroll_wheel(self, event) -> None:
        if self.scrollbar.winfo_ismapped() and event.widget.winfo_toplevel() == self.root:
            delta = event.delta
            units = -3 if event.num == 4 else 3 if event.num == 5 else -int(delta / 40)
            self.viewport.yview_scroll(units, "units")

    def _scroll_to_focus(self, event) -> None:
        if not str(event.widget).startswith(str(self.page) + "."):
            return
        y = event.widget.winfo_rooty() - self.viewport.winfo_rooty()
        top = self.viewport.canvasy(0) + y
        widget_height = event.widget.winfo_height()
        view_height = self.viewport.winfo_height()
        if y < 0:
            self.viewport.yview_moveto(max(0, top - 12) / self.page.winfo_height())
        elif y + widget_height > view_height:
            self.viewport.yview_moveto((top + widget_height - view_height + 12) / self.page.winfo_height())

    def _pair(self) -> tuple[str, str]:
        return self.from_currency_var.get(), self.to_currency_var.get()

    def _render_pair(self) -> None:
        base, target = self._pair()
        self.amount_code.configure(text=base)
        self.result_code.configure(text=target)
        self.market_pair.configure(text=f"{base} / {target}")
        self.market_unit.configure(text=target)
        for control, code in ((self.from_symbol, base), (self.to_symbol, target)):
            if code in theme.CURRENCIES:
                _, symbol, background, foreground = theme.CURRENCIES[code]
                control.configure(text=symbol, background=background, foreground=foreground)
        for code, tile in self.quote_tiles.items():
            tile.set_active((base, target) == (code, "MDL"))

    def _swap_currencies(self) -> None:
        base, target = self._pair()
        self.from_currency_var.set(target)
        self.to_currency_var.set(base)

    def _choose_pair(self, base: str) -> None:
        self.from_currency_var.set(base)
        self.to_currency_var.set("MDL")

    def _set_today(self) -> None:
        self.date_var.set(format_date(date.today()))

    def _amount_edited(self, *_args) -> None:
        self._clear_result()
        self._validate_inputs()

    def _market_edited(self, *_args) -> None:
        self._amount_edited()
        self._render_pair()
        if self._history_key is not None and self.date_var.get().strip() == format_date(self._history_key[0]):
            self._request_history()
            return
        self._show_no_history("Выбрана новая дата", "Котировки обновятся после проверки даты.")
        if self._history_after is not None:
            self.root.after_cancel(self._history_after)
        self._history_after = self.root.after(260, self._request_history)

    def _validate_inputs(self, show=True) -> ConversionInput | None:
        if self.is_busy:
            return None
        try:
            amount = parse_amount(self.amount_var.get())
            base, target = (validate_currency(code) for code in self._pair())
            day = parse_date(self.date_var.get())
        except ValidationError as error:
            self.convert_btn.state(["disabled"])
            if show:
                self._set_status(str(error), theme.AMBER)
            return None
        self.convert_btn.state(["!disabled"])
        return amount, base, target, day

    def _enter_conversion(self, _event) -> str:
        self.start_conversion()
        return "break"

    def _set_status(self, text="", color=theme.MUTED) -> None:
        if text:
            self.status_label.configure(text=text, foreground=color)
            self.status_label.pack(anchor="w", fill="x", pady=(8, 0))
        else:
            self.status_label.pack_forget()

    def _clear_result(self) -> None:
        self._pending_cache = None
        self.cache_actions.pack_forget()
        self._copied_text = ""
        self.copy_btn.configure(text="Копировать")
        self.copy_btn.state(["disabled"])
        self.result_value.configure(text="—", font=(theme.UI, -33, "bold"))
        self.result_rate.configure(text="Курс за выбранную дату")
        self.result_source.pack_forget()
        self._set_status()

    def _set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        for widget in (self.amount_entry, self.date_entry, self.swap_btn, self.today_btn):
            widget.configure(state="disabled" if busy else "normal")
        for combo in (self.from_combo, self.to_combo):
            combo.configure(state="disabled" if busy else "readonly")
        for tile in self.quote_tiles.values():
            tile.set_enabled(not busy)
        self.convert_btn.configure(text="Загрузка курса…" if busy else "Конвертировать  →")
        if busy:
            self.convert_btn.state(["disabled"])
        else:
            self._validate_inputs(show=False)

    def start_conversion(self) -> None:
        fields = self._validate_inputs()
        if fields is None:
            return
        self._clear_result()
        amount, base, target, day = fields
        if base == target:
            self._display_result(fields, None)
            return
        self._set_busy(True)
        self._set_status(f"Запрашиваем курс BNM за {format_date(day)}…")
        threading.Thread(target=self._fetch_conversion, args=(fields,), daemon=True).start()

    def _fetch_conversion(self, fields: ConversionInput) -> None:
        _, base, target, day = fields
        try:
            outcome = self.service.fetch_rates(day, (base, target))
        except RatesServiceError as error:
            outcome = error
        except Exception:
            LOG.exception("Не удалось загрузить курс")
            outcome = RatesServiceError("Не удалось получить курс.")
        self.queue.put(("conversion", fields, outcome))

    def _finish_conversion(self, fields: ConversionInput, outcome: RateResult | RatesServiceError) -> None:
        self._set_busy(False)
        if isinstance(outcome, RateResult):
            self._display_result(fields, outcome)
            return
        if isinstance(outcome, NetworkError):
            text = "Нет связи с BNM."
        elif isinstance(outcome, (HttpError, RatesFormatError)):
            text = "BNM вернул ответ, который не удалось прочитать."
        else:
            text = str(outcome)
        if outcome.suitable_cache is None:
            self._set_status(f"{text} Подходящего сохранённого курса нет.", theme.AMBER)
            return
        saved = outcome.suitable_cache
        self._pending_cache = saved, fields
        self._set_status(f"{text} Сохранён курс за {saved.date}.", theme.AMBER)
        if self._ask_consent_fn is not None:
            agreed = self._ask_consent_fn("Использовать сохранённый курс?", f"{text} Курс за {saved.date}.")
            if agreed:
                self._use_cache()
            else:
                self._discard_cache()
        else:
            self.cache_actions.pack(anchor="w", pady=(6, 0))

    def _use_cache(self) -> None:
        if self._pending_cache is not None:
            snapshot, fields = self._pending_cache
            self.cache_actions.pack_forget()
            self._pending_cache = None
            self._display_result(fields, self.service.apply_cache(snapshot, fields[-1]))

    def _discard_cache(self) -> None:
        self._pending_cache = None
        self.cache_actions.pack_forget()
        self._set_status("Конвертация по сохранённому курсу отменена.")

    def _display_result(self, fields: ConversionInput, result: RateResult | None) -> None:
        amount, base, target, _ = fields
        rates = result.snapshot.rates if result is not None else None
        try:
            conversion = convert_with_details(amount, base, target, rates)
        except (ArithmeticError, KeyError, ValidationError):
            self._set_status("Не удалось рассчитать сумму по этому курсу.", theme.AMBER)
            return
        value = theme.number(conversion.result_amount, 2)
        size = 33 if len(value) <= 12 else 23 if len(value) <= 20 else 17
        self.result_value.configure(text=value, font=(theme.UI, -size, "bold"))
        self.result_code.configure(text=target)
        self._copied_text = f"{value} {target}"
        self.copy_btn.state(["!disabled"])
        self._set_status()
        if result is None:
            self.result_rate.configure(text=f"1 {base} = 1 {target}")
            self.result_source.configure(text="Валюты совпадают. Внешний курс не применялся.")
        else:
            rate = conversion.rate_from / conversion.rate_to
            self.result_rate.configure(text=f"1 {base} = {theme.number(rate)} {target}")
            source = "Локальный кэш BNM" if result.source == "Локальный кэш" else "BNM"
            self.result_source.configure(text=f"{source}  ·  курс за {result.actual_date}")
            if result.is_fallback_date:
                self._set_status(f"Курс на {result.requested_date} недоступен. Применён курс за {result.actual_date}.",
                                 theme.AMBER)
            if result.warning:
                status = self.status_label.cget("text") if self.status_label.winfo_manager() else ""
                self._set_status((status + " Не удалось сохранить кэш курса.").strip(), theme.AMBER)
        self.result_source.pack(anchor="w", pady=(3, 0))

    def _copy_result(self) -> None:
        if self._copied_text:
            self.root.clipboard_clear()
            self.root.clipboard_append(self._copied_text)
            self.copy_btn.configure(text="Скопировано")

    def _choose_period(self, days: int) -> None:
        self.period_buttons[self._period].configure(style="Period.TButton")
        self._period = days
        self.period_buttons[days].configure(style="ActivePeriod.TButton")
        self._request_history()

    def _request_history(self, force=False) -> None:
        if self._history_after is not None:
            self.root.after_cancel(self._history_after)
            self._history_after = None
        base, target = self._pair()
        try:
            end = parse_date(self.date_var.get())
        except ValidationError:
            self._show_no_history("Проверьте дату курса", "Введите дату в формате ДД.ММ.ГГГГ.")
            self.refresh_btn.state(["disabled"])
            return
        if base == target:
            self._show_no_history("Валюты совпадают", "Выберите разные валюты, чтобы увидеть динамику.")
            self.refresh_btn.state(["disabled"])
            return
        key = end, self._period
        if key == self._history_key and not force:
            self._render_history()
            return
        self._history_key = key
        self._history_window = None
        if self._history_cancel is not None:
            self._history_cancel.set()
        self._history_pending = key, force
        self._render_history()
        self._start_history_job()

    def _start_history_job(self) -> None:
        if self._history_job is not None or self._history_pending is None:
            return
        key, force = self._history_pending
        self._history_pending = None
        cancel = threading.Event()
        self._history_cancel = cancel
        self._history_job = threading.Thread(target=self._fetch_history, args=(key, cancel, force), daemon=True)
        self._history_job.start()

    def _fetch_history(self, key, cancel, force) -> None:
        day, days = key
        try:
            history = self.history.load(day, days, cancel, refresh=force)
        except Exception:
            LOG.exception("Не удалось построить график")
            history = HistoryWindow(day, day, (), source_unavailable=True)
        self.queue.put(("history", cancel, history))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, request, result = self.queue.get_nowait()
                if kind == "conversion":
                    self._finish_conversion(request, result)
                else:
                    self._history_job = None
                    if not request.is_set():
                        self._history_window = result
                        self._request_history()
                    self._start_history_job()
        except queue.Empty:
            pass
        if not self._closed:
            self._queue_after = self.root.after(35, self._drain_queue)

    def _show_no_history(self, heading: str, detail: str, status="") -> None:
        self.chart.show_message(heading, detail)
        self.market_value.configure(text="—")
        self.market_date.configure(text="")
        self.change_badge.pack_forget()
        self.market_status.configure(text=status)
        for label in self.metrics.values():
            label.configure(text="—")
        for tile in self.quote_tiles.values():
            tile.show(RateSeries())

    def _render_history(self) -> None:
        history = self._history_window
        if history is None:
            self._show_no_history("Загружаем историю", "Запрашиваем архивные котировки BNM.")
            self.refresh_btn.state(["disabled"])
            return
        self.refresh_btn.state(["!disabled"])
        base, target = self._pair()
        series = history.series(base, target)
        points = series.points
        status = "Архив BNM · сохранённые данные" if history.from_archive else "Источник · архив курсов BNM"
        if history.source_unavailable:
            status = "BNM недоступен · сохранённые данные" if history.from_archive else "Доступна часть наблюдений BNM"
        if history.archive_write_failed:
            status = "Историю не удалось сохранить локально"
        if not points:
            detail = "Проверьте соединение и нажмите «Обновить»." if history.source_unavailable else "Выберите другую дату, пару или период."
            self._show_no_history("История недоступна" if history.source_unavailable else "Нет котировок", detail)
        else:
            self.chart.show(series, history.start, history.end, target)
            self.market_value.configure(text=theme.number(points[-1].value))
            self.market_date.configure(text=f"на {format_date(points[-1].day)}")
            self.metrics["low"].configure(text=theme.number(min(point.value for point in points)))
            self.metrics["high"].configure(text=theme.number(max(point.value for point in points)))
            self.metrics["count"].configure(text=str(len(points)))
            self.market_status.configure(text=status)
            change = series.change
            if change is None:
                self.change_badge.pack_forget()
            else:
                glyph = "↗ +" if change > 0 else "↘ " if change < 0 else ""
                self.change_badge.configure(
                    text=f"{glyph}{theme.number(change, 2)}%",
                    background=theme.GREEN_SURFACE if change >= 0 else "#422A2D",
                    foreground=theme.GREEN_TEXT if change >= 0 else theme.DOWN,
                )
                self.change_badge.pack(side="left", pady=(6, 0))
        for code, tile in self.quote_tiles.items():
            tile.show(history.series(code, "MDL"))

    def _close(self) -> None:
        self._closed = True
        if self._history_cancel is not None:
            self._history_cancel.set()
        for timer in (self._history_after, self._title_after, self._queue_after):
            if timer is not None:
                self.root.after_cancel(timer)
        self.root.destroy()
