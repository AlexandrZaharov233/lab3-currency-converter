"""График валютной пары с выбором точки мышью и клавиатурой."""

from __future__ import annotations

import tkinter as tk
from bisect import bisect_left
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from app import theme
from app.history import RateSeries


def axis_ticks(series: RateSeries) -> tuple[tuple[Decimal, ...], int]:
    values = [point.value for point in series.points]
    low, high = min(values), max(values)
    padding = max((high - low) / 8, high / 1500)
    low, high = max(Decimal(0), low - padding), high + padding
    interval = (high - low) / 4
    unit = Decimal(1).scaleb(interval.adjusted())
    step = next(unit * factor for factor in (1, 2, 5, 10) if unit * factor >= interval)
    first = int((low / step).to_integral_value(rounding=ROUND_FLOOR))
    last = int((high / step).to_integral_value(rounding=ROUND_CEILING))
    return tuple(step * tick for tick in range(first, last + 1)), max(2, -step.normalize().as_tuple().exponent)


class RateChart(tk.Canvas):
    def __init__(self, parent):
        super().__init__(
            parent, height=280, width=240, background=theme.PANEL, borderwidth=0,
            highlightthickness=1, highlightbackground=theme.PANEL,
            highlightcolor=theme.GREEN_BORDER, takefocus=True,
        )
        self.series = RateSeries()
        self.start = self.end = date.today()
        self.currency = ""
        self.empty_heading = ""
        self.empty_detail = ""
        self.coords: list[tuple[float, float]] = []
        self.x_positions: list[float] = []
        self.selected: int | None = None
        self.bind("<Configure>", lambda _event: self.redraw())
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda _event: self._select(None))
        self.bind("<Button-1>", lambda _event: self.focus_set())
        self.bind("<FocusIn>", lambda _event: self._step(0))
        self.bind("<FocusOut>", lambda _event: self._select(None))
        self.bind("<Left>", lambda _event: self._step(-1))
        self.bind("<Right>", lambda _event: self._step(1))
        self.bind("<Home>", lambda _event: self._key_select(0))
        self.bind("<End>", lambda _event: self._key_select(len(self.coords) - 1))

    def show(self, series: RateSeries, start: date, end: date, currency: str) -> None:
        self.series, self.start, self.end, self.currency = series, start, end, currency
        self.selected = None
        self.redraw()

    def show_message(self, heading: str, detail: str) -> None:
        self.series = RateSeries()
        self.empty_heading, self.empty_detail = heading, detail
        self.selected = None
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        self.coords = []
        self.x_positions = []
        width, height = self.winfo_width(), self.winfo_height()
        self.left, self.top = 5, 15
        self.right, self.bottom = width - 72, height - 35
        if self.right <= self.left + 55 or self.bottom <= self.top + 45:
            return
        if not self.series.points:
            self._draw_empty(width, height)
            return

        ticks, decimals = axis_ticks(self.series)
        self.decimals = min(10, max(4, decimals + 1))
        low, high = ticks[0], ticks[-1]

        def position_x(day: date) -> float:
            interval = max(1, (self.end - self.start).days)
            return self.left + (self.right - self.left) * (day - self.start).days / interval

        def position_y(value: Decimal) -> float:
            return self.bottom - float((value - low) / (high - low)) * (self.bottom - self.top)

        self.coords = [(position_x(point.day), position_y(point.value)) for point in self.series.points]
        self.x_positions = [x for x, _ in self.coords]
        if len(self.coords) > 1:
            line = [component for pair in self.coords for component in pair]
            self.create_polygon(
                self.coords[0][0], self.bottom, *line, self.coords[-1][0], self.bottom,
                fill="#23382F", outline="",
            )

        for tick in ticks:
            y = position_y(tick)
            self.create_line(self.left, y, self.right, y, fill=theme.GRID)
            text = theme.number(tick, min(8, decimals))
            if len(text) > 10:
                text = f"{tick:.2E}"
            self.create_text(self.right + 11, y, text=text, anchor="w",
                             fill=theme.MUTED, font=(theme.MONO, -12))

        columns = 6 if width > 520 else 4
        span = (self.end - self.start).days
        offsets = sorted({round(index * span / (columns - 1)) for index in range(columns)})
        for index, offset in enumerate(offsets):
            day = date.fromordinal(self.start.toordinal() + offset)
            x = position_x(day)
            self.create_line(x, self.top, x, self.bottom, fill=theme.GRID, dash=(2, 5))
            anchor = "nw" if index == 0 else "n"
            self.create_text(x, self.bottom + 12, text=theme.short_date(day), anchor=anchor,
                             fill=theme.MUTED, font=theme.SMALL)

        if len(self.coords) > 1:
            self.create_line(*line, fill="#395643", width=6, capstyle="round", joinstyle="round")
            self.create_line(*line, fill=theme.LIME, width=2, capstyle="round", joinstyle="round")
        if len(self.coords) <= 7:
            for x, y in self.coords[:-1]:
                self.create_oval(x - 2, y - 2, x + 2, y + 2, fill=theme.LIME, outline="")
        x, y = self.coords[-1]
        self.create_oval(x - 7, y - 7, x + 7, y + 7, fill=theme.GREEN_SURFACE, outline="")
        self.create_oval(x - 4, y - 4, x + 4, y + 4, fill=theme.LIME, outline=theme.PANEL)
        self._draw_selection()

    def _draw_empty(self, width: int, height: int) -> None:
        for row in range(5):
            y = self.top + (self.bottom - self.top) * row / 4
            self.create_line(self.left, y, width - 5, y, fill=theme.GRID, dash=(3, 5))
        x, y = width / 2, height / 2 - 10
        self.create_rectangle(x - 147, y - 64, x + 147, y + 53, fill=theme.PANEL, outline="")
        theme.round_rect(self, x - 18, y - 52, x + 18, y - 16, 9,
                         fill=theme.GREEN_SURFACE, outline=theme.GREEN_BORDER)
        self.create_line(x - 9, y - 27, x - 9, y - 41, x - 9, y - 27, x + 9, y - 27,
                         fill=theme.GREEN_TEXT, width=1)
        self.create_line(x - 6, y - 33, x - 1, y - 36, x + 2, y - 34, x + 8, y - 41,
                         fill=theme.LIME, width=2, joinstyle="round")
        self.create_text(x, y + 4, text=self.empty_heading, fill=theme.INK,
                         font=(theme.UI, -17, "bold"))
        self.create_text(x, y + 34, text=self.empty_detail, fill=theme.MUTED,
                         font=theme.SMALL, width=max(200, min(370, width - 28)), justify="center")

    def _hover(self, event) -> None:
        if not self.x_positions:
            return
        if not (self.left - 5 <= event.x <= self.right + 5 and self.top <= event.y <= self.bottom):
            self._select(None)
            return
        index = bisect_left(self.x_positions, event.x)
        nearest = min(
            range(max(0, index - 1), min(len(self.coords), index + 1)),
            key=lambda candidate: abs(self.x_positions[candidate] - event.x),
        )
        self._select(nearest)

    def _step(self, change: int) -> str:
        if self.coords:
            index = self.selected if self.selected is not None else len(self.coords) - 1
            self._select(max(0, min(index + change, len(self.coords) - 1)))
        return "break"

    def _key_select(self, index: int) -> str:
        if self.coords:
            self._select(index)
        return "break"

    def _select(self, index: int | None) -> None:
        if index != self.selected:
            self.selected = index
            self._draw_selection()

    def _draw_selection(self) -> None:
        self.delete("cursor")
        if self.selected is None or not self.coords:
            return
        x, y = self.coords[self.selected]
        self.create_line(x, self.top, x, self.bottom, fill=theme.MUTED,
                         dash=(3, 4), tags="cursor")
        self.create_oval(x - 5, y - 5, x + 5, y + 5, fill=theme.LIME,
                         outline=theme.BG, width=2, tags="cursor")
        point = self.series.points[self.selected]
        tip_width = 150
        tip_left = x + 13 if x + 13 + tip_width <= self.winfo_width() - 6 else x - tip_width - 13
        tip_left = max(5, tip_left)
        tip_top = max(self.top, min(y - 55, self.bottom - 54))
        theme.round_rect(self, tip_left, tip_top, tip_left + tip_width, tip_top + 53, 8,
                         fill=theme.BG, outline=theme.GREEN_BORDER, tags="cursor")
        self.create_text(tip_left + 11, tip_top + 8, text=point.day.strftime("%d.%m.%Y"),
                         fill=theme.MUTED, font=theme.SMALL, anchor="nw", tags="cursor")
        self.create_text(tip_left + 11, tip_top + 26,
                         text=f"{theme.number(point.value, self.decimals)} {self.currency}",
                         fill=theme.LIME, font=theme.NUMBERS, anchor="nw", tags="cursor")
