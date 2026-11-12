"""How many printed lines a CV line takes, from the bundled PT Serif metrics (cmap + hmtx read with
struct) and the widths in build.py's CSS. Used by the length guard before the PDF is built, so a
tailored CV that would spill onto a second page is shortened by code instead of by a retry.
Greedy wrap at spaces only (build.py keeps hyphenated words together); widths fitted on real PDFs."""

import re
import struct
from functools import lru_cache
from pathlib import Path

FONTS = Path(__file__).resolve().parents[1] / "source" / "fonts"
# Usable line widths in pt, fitted 08.10.2026 on Chrome 154 print-to-PDF (the base page is full, so a
# probe that adds one printed line gives 2 pages): 50 bullet probes fit for 486-493, 40 summary probes
# for 511-526; the approved base needs >= 491 for its 107-char bullet. The CSS box widths are 489.7 and
# 512.5, so print uses the raw font advances. Skills lines sit in the same box as the summary.
WIDTH = {
    "bullet": 492.0,
    "summary": 516.0,
    "skills": 516.0,
}
# A changed line is measured against a narrower box: glyph sums miss Chrome's own spacing by up to ~1%
# (two probes of 487 and 491 pt wrapped while the base's 490.6 pt line fits). Base lines use WIDTH.
SAFE_WIDTH = {"bullet": 478.0, "summary": 503.0, "skills": 503.0}
SIZE = 10.0  # pt: li, .summary, .lines


@lru_cache(maxsize=None)
def _font(style):
    data = (FONTS / f"PT_Serif-Web-{style}.ttf").read_bytes()
    tables = {}
    for i in range(struct.unpack(">H", data[4:6])[0]):
        tag, _, off, length = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
        tables[tag.decode("latin1")] = off
    upm = struct.unpack(">H", data[tables["head"] + 18: tables["head"] + 20])[0]
    n_metrics = struct.unpack(">H", data[tables["hhea"] + 34: tables["hhea"] + 36])[0]
    hm = tables["hmtx"]
    advances = [struct.unpack(">H", data[hm + 4 * i: hm + 4 * i + 2])[0] for i in range(n_metrics)]
    return upm, advances, _cmap(data, tables["cmap"])


def _cmap(data, base):
    """Unicode BMP -> glyph id from the (3,1) format-4 subtable."""
    count = struct.unpack(">H", data[base + 2: base + 4])[0]
    sub = None
    for i in range(count):
        pid, eid, off = struct.unpack(">HHI", data[base + 4 + 8 * i: base + 12 + 8 * i])
        if (pid, eid) == (3, 1):
            sub = base + off
    if sub is None or struct.unpack(">H", data[sub: sub + 2])[0] != 4:
        raise ValueError("no format-4 Unicode cmap")
    seg = struct.unpack(">H", data[sub + 6: sub + 8])[0] // 2
    arr = lambda start: struct.unpack(f">{seg}H", data[start: start + 2 * seg])
    ends = arr(sub + 14)
    starts = arr(sub + 16 + 2 * seg)
    deltas = arr(sub + 16 + 4 * seg)
    ro_at = sub + 16 + 6 * seg
    ranges = arr(ro_at)
    out = {}
    for k in range(seg):
        for code in range(starts[k], min(ends[k], 0xFFFE) + 1):
            if ranges[k] == 0:
                gid = (code + deltas[k]) & 0xFFFF
            else:
                at = ro_at + 2 * k + ranges[k] + 2 * (code - starts[k])
                gid = struct.unpack(">H", data[at: at + 2])[0]
                gid = (gid + deltas[k]) & 0xFFFF if gid else 0
            out[code] = gid
    return out


def width(text, bold=False, italic=False, size=SIZE):
    style = ("Bold" if bold else "") + ("Italic" if italic else "") or "Regular"
    upm, adv, cmap = _font(style)
    return sum(adv[min(cmap.get(ord(ch), 0), len(adv) - 1)] for ch in text) * size / upm


def _runs(text):
    """[(chars, bold)] from a string with <b>/<i> tags (italic is measured as regular: close enough)."""
    out, bold = [], False
    for part in re.split(r"(</?[bi]>)", text):
        if part in ("<b>", "</b>"):
            bold = part == "<b>"
        elif part and part not in ("<i>", "</i>"):
            out.append((part, bold))
    return out


def lines(text, kind="bullet", prefix_bold="", safe=False):
    """Printed lines for a summary, bullet or skills line ('Group: items' with the group in bold).
    safe=True measures a new line against the narrower SAFE_WIDTH box."""
    words, cur = [], None   # words as lists of (chars, bold)
    if prefix_bold:
        text = f"<b>{prefix_bold}:</b> {text}"
    for chunk, bold in _runs(text):
        for i, piece in enumerate(re.split(r"( +)", chunk)):
            if piece.startswith(" "):
                cur = None
            elif piece:
                if cur is None:
                    cur = []
                    words.append(cur)
                cur.append((piece, bold))
    space = width(" ")
    avail, n, used = (SAFE_WIDTH if safe else WIDTH)[kind], 1, 0.0
    for w in words:
        ww = sum(width(t, b) for t, b in w)
        if used and used + space + ww > avail:
            n, used = n + 1, ww
        else:
            used += (space if used else 0) + ww
    return n
