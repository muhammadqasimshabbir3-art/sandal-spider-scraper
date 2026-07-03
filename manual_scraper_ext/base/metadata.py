"""
metadata.py
~~~~~~~~~~~
Canonical metadata schema and item builder for the multi-site sandal dataset.

Every spider yields a :class:`SandalItem` populated via :func:`build_item`.
The pipeline then writes ``metadata.json`` and downloads images into::

    dataset/<Brand>/<Product Name>/<Color>/
        metadata.json
        001.jpg
        002.jpg
        …
"""

from __future__ import annotations

import json
import os
import re
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Canonical metadata schema
# ─────────────────────────────────────────────────────────────────────────────

METADATA_FIELDS: tuple[str, ...] = (
    "brand",
    "product_name",
    "source",
    "gender",
    "category",
    "sku",
    "variant",
    "color",
    "price",
    "availability",
    "product_url",
    "images",
)


def empty_metadata() -> dict[str, Any]:
    """Return a blank metadata record with all canonical fields set to ``""``."""
    return {
        "brand": "",
        "product_name": "",        "source": "",        "gender": "Men",
        "category": "",
        "sku": "",
        "variant": "",
        "color": "",
        "price": "",
        "availability": "",
        "product_url": "",
        "images": [],
    }


def build_metadata(
    *,
    brand: str = "",
    product_name: str = "",
    source: str = "",
    gender: str = "Men",
    category: str = "",
    sku: str = "",
    variant: str = "",
    color: str = "",
    price: str = "",
    availability: str = "",
    product_url: str = "",
    images: list[str] | None = None,
) -> dict[str, Any]:
    """
    Construct a validated metadata dict from keyword arguments.

    All string fields are stripped and truncated to safe lengths.
    Missing images default to an empty list.
    """
    def _s(v: str, n: int = 200) -> str:
        return (v or "").strip()[:n]

    return {
        "brand":        _s(brand, 100),
        "product_name": _s(product_name, 200),
        "source":       _s(source, 100),
        "gender":       _s(gender, 20) or "Men",
        "category":     _s(category, 100),
        "sku":          _s(sku, 100),
        "variant":      _s(variant, 200),
        "color":        _s(color, 100),
        "price":        _s(price, 50),
        "availability": _s(availability, 50),
        "product_url":  _s(product_url, 500),
        "images":       list(images) if images else [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem helpers
# ─────────────────────────────────────────────────────────────────────────────

_ILLEGAL = re.compile(r'[\\/*?:"<>|\r\n\t]')


def safe_name(text: str, maxlen: int = 80) -> str:
    """Strip filesystem-illegal characters and truncate to *maxlen*."""
    return _ILLEGAL.sub("", (text or "").strip())[:maxlen]


def dataset_folder(
    base_store: str,
    brand: str,
    product_name: str,
    color: str = "",
) -> str:
    """
    Return the canonical folder path for a product / colour variant.

    Layout::

        <base_store>/<Brand>/<Product Name>/          (no colour)
        <base_store>/<Brand>/<Product Name>/<Color>/  (with colour)
    """
    parts = [
        base_store,
        safe_name(brand, 60),
        safe_name(product_name, 80),
    ]
    if color:
        parts.append(safe_name(color, 40))
    return os.path.join(*parts)


def write_metadata_json(folder: str, metadata: dict[str, Any]) -> None:
    """
    Write *metadata* as pretty-printed JSON to ``<folder>/metadata.json``.

    Creates *folder* (and any parents) if they do not exist.
    """
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "metadata.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, ensure_ascii=False, indent=2)
