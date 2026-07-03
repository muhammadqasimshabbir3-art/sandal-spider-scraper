"""
parser_utils.py
~~~~~~~~~~~~~~~
Shared HTML / JSON parsing helpers used by every spider in the framework.

These utilities centralise the extraction patterns that appear repeatedly
across e-commerce sites — JSON-LD, embedded __NEXT_DATA__, Apollo cache
hydration, and common structured-data schemas.
"""

import json
import re
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# JSON-LD
# ─────────────────────────────────────────────────────────────────────────────

def extract_json_ld(response, type_filter: str | None = None) -> list[dict]:
    """
    Extract all JSON-LD blocks from the page.

    Parameters
    ----------
    response:
        Scrapy HtmlResponse.
    type_filter:
        If given, only return blocks whose ``@type`` matches this string
        (case-insensitive).  Examples: ``"Product"``, ``"BreadcrumbList"``.

    Returns
    -------
    list[dict]
        Parsed JSON-LD objects (may be empty).
    """
    results: list[dict] = []
    for text in response.css('script[type="application/ld+json"]::text').getall():
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        # JSON-LD may be a single object or a @graph list
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if "@graph" in node:
                nodes.extend(node["@graph"])
            if not isinstance(node, dict):
                continue
            if type_filter is None or (node.get("@type", "") or "").lower() == type_filter.lower():
                results.append(node)
    return results


def get_json_ld_product(response) -> dict:
    """
    Return the first JSON-LD ``Product`` block, or an empty dict.
    """
    blocks = extract_json_ld(response, "Product")
    return blocks[0] if blocks else {}


# ─────────────────────────────────────────────────────────────────────────────
# Next.js / React hydration data
# ─────────────────────────────────────────────────────────────────────────────

def extract_next_data(response) -> dict:
    """
    Extract the ``__NEXT_DATA__`` JSON payload embedded by Next.js.

    Returns an empty dict when the page is not a Next.js app or the JSON
    cannot be parsed.
    """
    text = response.css("script#__NEXT_DATA__::text").get() or ""
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}


def extract_nuxt_data(response) -> list[Any]:
    """
    Extract Nuxt.js ``__NUXT__`` hydration data from a script block.
    Returns an empty list when not found.
    """
    script = response.xpath(
        "//script[contains(., '__NUXT__')]/text()"
    ).get() or ""
    match = re.search(r"__NUXT__\s*=\s*(\{.*)", script, re.S)
    if not match:
        return []
    try:
        return [json.loads(match.group(1).rstrip(";"))]
    except (json.JSONDecodeError, ValueError):
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Embedded JSON patterns
# ─────────────────────────────────────────────────────────────────────────────

def extract_embedded_json(response, variable_name: str) -> Any:
    """
    Extract a JavaScript variable assignment from a script tag.

    Example::

        var meta = { "product": { … } };
        →  extract_embedded_json(response, "meta")

    Returns the parsed Python object or ``None`` on failure.
    """
    pattern = rf'(?:var|let|const)\s+{re.escape(variable_name)}\s*=\s*(\{{.*?\}});'
    script = response.xpath(
        f"//script[contains(., '{variable_name}')]/text()"
    ).get() or ""
    match = re.search(pattern, script, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def extract_window_var(response, variable_name: str) -> Any:
    """
    Extract ``window.varName = { … }`` or ``window["varName"] = { … }``
    from a script block.
    """
    patterns = [
        rf'window\.{re.escape(variable_name)}\s*=\s*(\{{.*?\}});',
        rf'window\["{re.escape(variable_name)}"\]\s*=\s*(\{{.*?\}});',
    ]
    scripts = response.css("script::text").getall()
    for script in scripts:
        for pat in patterns:
            match = re.search(pat, script, re.S)
            if match:
                try:
                    return json.loads(match.group(1))
                except (json.JSONDecodeError, ValueError):
                    continue
    return None


def find_json_in_script(response, key: str) -> Any:
    """
    Search all inline script tags for a JSON value associated with *key*.

    Useful when the data is embedded like::

        {"productData": { … }}
        "key": { … }

    Returns the parsed value or ``None``.
    """
    pattern = rf'"{re.escape(key)}"\s*:\s*(\{{.*?\}}|\[.*?\])'
    for script in response.css("script::text").getall():
        match = re.search(pattern, script, re.S)
        if match:
            try:
                return json.loads(match.group(1))
            except (json.JSONDecodeError, ValueError):
                continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Text helpers
# ─────────────────────────────────────────────────────────────────────────────

def first_text(*selectors_or_values: str) -> str:
    """
    Return the first non-empty string from a sequence of candidate values.
    Strips surrounding whitespace.

    Usage::

        title = first_text(
            response.css("h1.product-title::text").get(),
            response.css('meta[property="og:title"]::attr(content)').get(),
        )
    """
    for val in selectors_or_values:
        if val and val.strip():
            return val.strip()
    return ""


def clean_price(raw: str) -> str:
    """
    Normalise a raw price string to a plain decimal.

    Examples::

        "$49.99"  →  "49.99"
        "USD 49"  →  "49.00"
        "49,99"   →  "49.99"   (European decimal comma)
    """
    if not raw:
        return ""
    # Remove currency symbols and letter codes
    raw = re.sub(r"[£$€¥₹]", "", raw)
    raw = re.sub(r"\b[A-Z]{2,3}\b", "", raw)
    # European decimal comma
    raw = raw.replace(",", ".")
    # Keep only digits and the last dot
    digits = re.sub(r"[^\d.]", "", raw)
    if digits.count(".") > 1:
        parts = digits.split(".")
        digits = "".join(parts[:-1]) + "." + parts[-1]
    return digits.strip(".") or ""


def safe_slug(text: str, maxlen: int = 80) -> str:
    """
    Convert *text* to a filesystem-safe slug suitable for folder names.

    ``"Nike React Sandal (2024)"``  →  ``"Nike React Sandal 2024"``
    """
    _illegal = re.compile(r'[\\/*?:"<>|\r\n\t]')
    return _illegal.sub("", (text or "").strip())[:maxlen]
