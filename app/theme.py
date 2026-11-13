"""Палитра, типографика и общие элементы интерфейса Tkinter."""

import tkinter as tk
from datetime import date
from decimal import Decimal
from tkinter import ttk


BG = "#0E1517"
HEADER = "#111B1D"
PANEL = "#152023"
FIELD = "#10191C"
BORDER = "#2B3C3F"
GRID = "#2A393C"
INK = "#EFF5F1"
SECONDARY = "#BCCBC7"
MUTED = "#91A6A1"
LIME = "#C4F482"
LIME_HOVER = "#D8FFA7"
GREEN_TEXT = "#CFF59B"
GREEN_SURFACE = "#243B2B"
GREEN_BORDER = "#3B5741"
DOWN = "#FF9B9D"
AMBER = "#F6CF91"

UI = "Segoe UI"
MONO = "Consolas"
SMALL = (UI, -13)
BODY = (UI, -15)
LABEL = (UI, -14, "bold")
SECTION = (UI, -21, "bold")
TITLE = (UI, -39, "bold")
FIGURE = (UI, -36, "bold")
NUMBERS = (MONO, -14)

MONTHS = ("янв", "фев", "мар", "апр", "май", "июн",
          "июл", "авг", "сен", "окт", "ноя", "дек")

CURRENCIES = {
    "EUR": ("Евро", "€", "#353245", "#E2D3FF"),
    "USD": ("Доллар США", "$", "#28423C", "#A8E2CC"),
    "RON": ("Румынский лей", "L", "#483824", "#F9D597"),
    "GBP": ("Фунт стерлингов", "£", "#42323A", "#F2C5D6"),
    "MDL": ("Молдавский лей", "L", "#29413B", "#BAEFCD"),
    "UAH": ("Украинская гривна", "₴", "#283A4B", "#B9DAFF"),
}


def number(value: Decimal, decimals: int = 4) -> str:
    return f"{value:,.{decimals}f}".translate(str.maketrans(",.", "\u202f,"))


def short_date(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]}"


def label(parent, text="", *, color=INK, bg=PANEL, font=BODY, **options):
    return tk.Label(
        parent, text=text, foreground=color, background=bg, font=font,
        anchor="w", padx=0, pady=0, borderwidth=0, **options,
    )


def round_rect(canvas, left, top, right, bottom, radius, **options):
    return canvas.create_polygon(
        left + radius, top, right - radius, top, right, top, right, top + radius,
        right, bottom - radius, right, bottom, right - radius, bottom,
        left + radius, bottom, left, bottom, left, bottom - radius,
        left, top + radius, left, top, smooth=True, **options,
    )


def style_widgets(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", INK)
    root.option_add("*TCombobox*Listbox.selectBackground", GREEN_SURFACE)
    root.option_add("*TCombobox*Listbox.selectForeground", INK)
    root.option_add("*TCombobox*Listbox.font", (UI, -16))

    style.configure(
        "Currency.TCombobox", fieldbackground=FIELD, background=FIELD,
        foreground=INK, arrowcolor=MUTED, arrowsize=14, bordercolor=BORDER,
        lightcolor=FIELD, darkcolor=FIELD, padding=(10, 8),
        selectbackground=FIELD, selectforeground=INK, relief="flat",
    )
    style.map(
        "Currency.TCombobox",
        fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
        background=[("readonly", FIELD), ("disabled", PANEL)],
        foreground=[("disabled", MUTED), ("readonly", INK)],
        bordercolor=[("focus", LIME)],
        arrowcolor=[("active", LIME)],
        selectbackground=[("readonly", FIELD)],
        selectforeground=[("readonly", INK)],
    )

    def button(style_name, surface, text_color, hover, padding, font=LABEL, border=BORDER):
        style.configure(
            style_name, font=font, padding=padding, foreground=text_color,
            background=surface, bordercolor=border, lightcolor=surface,
            darkcolor=surface, focuscolor=LIME, focusthickness=1,
            borderwidth=1, relief="flat", anchor="center",
        )
        style.map(
            style_name,
            background=[("disabled", PANEL), ("pressed", hover), ("active", hover)],
            foreground=[("disabled", MUTED)],
            lightcolor=[("pressed", hover), ("active", hover)],
            darkcolor=[("pressed", hover), ("active", hover)],
            bordercolor=[("focus", LIME)],
        )

    button("Accent.TButton", LIME, BG, LIME_HOVER, (14, 11), (UI, -16, "bold"), LIME)
    style.map("Accent.TButton", focuscolor=[("focus", BG)])
    button("Quiet.TButton", FIELD, SECONDARY, "#263639", (10, 7))
    button("Period.TButton", FIELD, MUTED, "#263639", (8, 6), SMALL, FIELD)
    button("ActivePeriod.TButton", GREEN_SURFACE, GREEN_TEXT, "#304936", (8, 6),
           (UI, -13, "bold"), GREEN_BORDER)
    button("Source.TButton", HEADER, SECONDARY, "#253538", (11, 7), SMALL)
    button("Copy.TButton", GREEN_SURFACE, GREEN_TEXT, "#304936", (7, 4), SMALL, GREEN_BORDER)
    style.configure(
        "Vertical.TScrollbar", background=BORDER, troughcolor=BG,
        bordercolor=BG, arrowcolor=MUTED, lightcolor=BORDER, darkcolor=BORDER,
        width=10, arrowsize=10,
    )
