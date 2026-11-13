"""
Точка входа в приложение «Конвертер валют».

Запуск:
    python main.py
"""

from __future__ import annotations

import tkinter as tk
from app.ui import CurrencyConverterApp


def main() -> None:
    root = tk.Tk()
    CurrencyConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
