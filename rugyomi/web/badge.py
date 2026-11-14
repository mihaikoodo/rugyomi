"""RGY-067: the SVG badge of a launch's risk card (`/badge/{token}.svg`).

Two styles, both plain SVG: no script, no foreignObject, no external reference, no style element, no font file (system fonts are named; the width of
style a is set from the character count, style b has ONE fixed size, so nothing is measured).
  a  one pill in the band colour: "Risk 45 · Elevated"
  b  the lens of the logo (the ring of three arcs, the handle) with the number centred in the ring, the band word and "rugyomi" beside it;
     `?theme=light` is a light tile (dark texts, band words in a darker shade of their band colour); the default is the dark tile.
A card computed afterwards is drawn exactly like a live card (the JSON API keeps the `kind`). A launch we know that has no card: "pending", neutral.
The text is only the score, the band and fixed words; the band is drawn only when it is one of the three known names.
"""
from __future__ import annotations

import math
from xml.sax.saxutils import escape

COLORS = {"Low": "#52c08a", "Elevated": "#e5a93d", "High": "#f0716a"}
NEUTRAL = "#98a3b1"
BG, SURFACE, RAISED, LINE, TEXT = "#0e1217", "#151b22", "#1b232c", "#2a343f", "#e6eaf0"
LIGHT_TILE, LIGHT_LINE, LIGHT_TEXT, LIGHT_MUTED = "#f6f8fa", "#c9d1d9", "#0e1217", "#4a5563"
FONT = "system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'DejaVu Sans Mono','Liberation Mono',monospace"     # digits of one width: the number is centred the same for 5, 45 and 100
CHAR_W = 6.3                  # estimated width of one character at 11 px (style a only); the pill is padded, a few pixels of slack are fine
HEIGHT_A = 20
SIZE_B = (116, 40)            # style b: the same width and height for every band and for 1-3 digits
STYLES = ("a", "b")
THEMES = ("dark", "light")
MIN_CONTRAST = 4.5

# The arcs of the logo (brand/rugyomi-mark.svg): ring centre (26,26) r 15 in a 64 box, three arcs of 100 degrees with 20 degree gaps
ARCS = (("Low", 55, 155), ("Elevated", 175, 275), ("High", 295, 395))
K = 0.8                                                         # the logo's 64 box scaled for the lens (ring + handle about 35 px wide)
T = 3 - 7.75 * K                                                # the ring starts 7.75 units from the box edge: 3 px from the badge edge
RING_C = 26 * K + T                                             # the centre of the ring in badge coordinates (x and y)
NUM_FS = 10.0                                                   # 1-2 digits; three digits use NUM_FS_3 so "100" stays inside the ring
NUM_FS_3 = 8.6
TEXT_X = 46


def _lin(c: float) -> float:
    c /= 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _on(color: str, bg: str, floor: float = MIN_CONTRAST) -> str:
    """`color` darkened (towards black, keeping its hue) until it reads on `bg` with at least `floor`."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    f = 1.0
    while f > 0:
        c = "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))
        if contrast(c, bg) >= floor:
            return c
        f -= 0.02
    return "#000000"


LIGHT_BAND_TEXT = {name: _on(c, LIGHT_TILE) for name, c in COLORS.items()}     # the band word on the light tile: a darker shade of the band colour


def _w(text: str, pad: int = 0) -> int:
    return int(math.ceil(len(text) * CHAR_W)) + pad


def _label(card: dict | None) -> tuple[str, str, str]:
    """(headline, band, colour) of the badge. A card: its score and band; none yet: "pending"; unknown launch: "unknown"."""
    if not card:
        return ("pending", "", NEUTRAL)
    if card.get("kind") == "unknown":
        return ("unknown", "", NEUTRAL)
    band = card["band"] if card.get("band") in COLORS else ""
    return (f"Risk {int(card['score'])}", band, COLORS.get(band, NEUTRAL))


def _title(card: dict | None) -> str:
    if card and card.get("kind") == "unknown":
        return "rugyomi: no such launch"
    if not card:
        return "rugyomi: no risk card yet"
    return f"rugyomi risk score {int(card['score'])}, {card['band'] if card.get('band') in COLORS else 'no band'}"


def _frame(width: int, height: int, title: str, body: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{escape(title, {chr(34): "&quot;"})}"><title>{escape(title)}</title>{body}</svg>\n')


def style_a(card: dict | None) -> str:
    head, band, color = _label(card)
    text = head + (f" · {band}" if band else "")
    w, h = _w(text, 18), HEIGHT_A
    body = (f'<rect width="{w}" height="{h}" rx="3" fill="{color}"/>'
            f'<text x="{w / 2:.1f}" y="{h / 2 + 3.9:.1f}" font-family="{FONT}" font-size="11" font-weight="600" text-anchor="middle" fill="{BG}">{escape(text)}</text>')
    return _frame(w, h, _title(card), body)


def _arc(cx: float, cy: float, r: float, a0: int, a1: int) -> str:
    x0, y0 = cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0))
    x1, y1 = cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1))
    return f"M{x0:.2f} {y0:.2f}A{r} {r} 0 0 1 {x1:.2f} {y1:.2f}"


def style_b(card: dict | None, theme: str = "dark") -> str:
    light = theme == "light"
    head, band, color = _label(card)
    number = str(int(card["score"])) if card and "score" in card else "?"
    top = band or head
    w, h = SIZE_B
    tile, line = (LIGHT_TILE, LIGHT_LINE) if light else (RAISED, LINE)
    ink, muted, handle_c = (LIGHT_TEXT, LIGHT_MUTED, LIGHT_TEXT) if light else (TEXT, NEUTRAL, TEXT)
    word = (LIGHT_BAND_TEXT.get(band) or LIGHT_MUTED) if light else color
    arcs = "".join(f'<path d="{_arc(26, 26, 15, a0, a1)}" fill="none" stroke="{COLORS[b]}" stroke-width="6.5" '
                   f'stroke-opacity="{1 if (card and b == band) else 0.3}"/>' for b, a0, a1 in ARCS)
    handle = f'<line x1="42.26" y1="42.26" x2="47.92" y2="47.92" stroke="{handle_c}" stroke-width="7" stroke-linecap="round"/>'
    fs = NUM_FS_3 if len(number) >= 3 else NUM_FS
    base = RING_C + 0.36 * fs                                # the baseline: the digits' middle sits on the ring's centre without relying on dominant-baseline
    body = (f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="6" fill="{tile}" stroke="{line}" stroke-width="1"/>'
            f'<g transform="translate({T:.3f} {T:.3f}) scale({K})">{arcs}{handle}</g>'
            f'<text x="{RING_C:.2f}" y="{base:.2f}" font-family="{MONO}" font-size="{fs}" font-weight="700" text-anchor="middle" fill="{ink}">{escape(number)}</text>'
            f'<text x="{TEXT_X}" y="{h * 0.5 - 1.5:.1f}" font-family="{FONT}" font-size="12" font-weight="700" fill="{word}">{escape(top)}</text>'
            f'<text x="{TEXT_X}" y="{h * 0.5 + 12:.1f}" font-family="{FONT}" font-size="9" fill="{muted}">rugyomi</text>')
    return _frame(w, h, _title(card), body)


UNKNOWN = {"kind": "unknown"}


def render(style: str, card: dict | None, theme: str = "dark") -> str:
    return style_b(card, theme) if style == "b" else style_a(card)
