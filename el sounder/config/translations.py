"""
config/translations.py  –  el sounder
========================================
Loads translations.json and exposes:
  tr(lang, key)   – looks up a translation key
  TRANSLATIONS    – full dict
  LANG_ORDER      – ordered list of language keys

translations.json must live next to this file (or in sys._MEIPASS for
PyInstaller bundles). Format:
{
  "lang_order": ["en_us", "de_de", ...],
  "translations": {
    "en_us": { "lang_name": "English", "quit": "Quit", ... },
    ...
  }
}
"""

from __future__ import annotations

import json
import os
import sys


def _load() -> tuple[dict, list]:
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS                                         # type: ignore[attr-defined]
    else:
        # config/ → project root → translations.json
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(base, "translations.json")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data["translations"], data["lang_order"]


TRANSLATIONS: dict
LANG_ORDER: list
TRANSLATIONS, LANG_ORDER = _load()


def tr(lang: str, key: str) -> str:
    """Return the translation for *key* in *lang*; falls back to en_us."""
    return (
        TRANSLATIONS.get(lang, TRANSLATIONS["en_us"])
        .get(key, TRANSLATIONS["en_us"].get(key, key))
    )
