"""ATS checks on the rendered PDF (research_ats.md section 9) with poppler-utils: page count, size,
extracted text, Type3 fonts / missing ToUnicode, ligature characters, hyphenated words split by a
line break, text missing or out of order versus the source. Without poppler -> ToolMissing, and the
orchestrator falls back to page_count() on the PDF bytes."""

import re
import subprocess
from pathlib import Path

LIGATURES = re.compile("[ﬀ-ﬆ]")
HYPHENATED = re.compile(r"\w+(?:-\w+)+")
MAX_BYTES = 2 * 1024 * 1024


class ToolMissing(Exception):
    pass


def run_tool(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60).stdout
    except FileNotFoundError:
        raise ToolMissing(f"{cmd[0]} not installed (apt install poppler-utils)")


RUN = run_tool  # tests replace this: RUN(cmd list) -> stdout


def page_count(data):
    """Pages from the PDF bytes (the /Type /Pages node), for machines without poppler."""
    counts = [int(n) for n in re.findall(rb"/Type\s*/Pages\b(?:(?!>>).)*?/Count\s+(\d+)", data, re.S)]
    counts += [int(n) for n in re.findall(rb"/Count\s+(\d+)(?:(?!>>).)*?/Type\s*/Pages\b", data, re.S)]
    if not counts:
        counts = [int(n) for n in re.findall(rb"/Count\s+(\d+)", data)]
    return max(counts) if counts else 0


def fonts(listing):
    """Parse `pdffonts` output into [{name, type, uni}] using the column widths of its dashes line."""
    lines = listing.splitlines()
    dash = next((i for i, l in enumerate(lines) if l.startswith("---")), None)
    if dash is None:
        return []
    spans, pos = [], 0
    for seg in lines[dash].split(" "):
        spans.append((pos, pos + len(seg)))
        pos += len(seg) + 1
    out = []
    for line in lines[dash + 1:]:
        if not line.strip():
            continue
        cols = [line[a:b + 1].strip() for a, b in spans]
        if len(cols) >= 6:
            out.append({"name": cols[0], "type": cols[1], "uni": cols[5]})
    return out


def _squash(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def text_issues(text, expected_text):
    """Hyphen splits, missing text and order problems, comparing extracted text with the source text."""
    issues, got = [], _squash(text)
    for word in dict.fromkeys(HYPHENATED.findall(expected_text)):
        if word.lower() not in got:
            issues.append(f"hyphenated word split or missing: {word}")
    pos = 0
    for line in expected_text.splitlines():
        probe = _squash(" ".join(line.split()[:3]))
        if len(probe) < 4:
            continue
        at = got.find(probe, pos)
        if at >= 0:
            pos = at + len(probe)
        elif probe in got:
            issues.append(f"text out of order: {probe}")
        else:
            issues.append(f"text missing from PDF: {probe}")
    return issues


def check(pdf, expected_text=None):
    """-> {pages, size, text, issues}; raises ToolMissing when poppler is absent."""
    pdf = Path(pdf)
    info = RUN(["pdfinfo", str(pdf)])
    m = re.search(r"^Pages:\s+(\d+)", info, re.M)
    pages = int(m.group(1)) if m else page_count(pdf.read_bytes())
    issues = []
    for f in fonts(RUN(["pdffonts", str(pdf)])):
        if f["type"].replace(" ", "").lower() == "type3":
            issues.append(f"Type3 font: {f['name']}")
        if f["uni"] == "no":
            issues.append(f"font without ToUnicode: {f['name']}")
    text = RUN(["pdftotext", "-enc", "UTF-8", str(pdf), "-"])
    if not text.strip():
        issues.append("no extractable text")
    if LIGATURES.search(text):
        issues.append("ligature characters in extracted text: " + "".join(sorted(set(LIGATURES.findall(text)))))
    if expected_text:
        issues += text_issues(text, expected_text)
    size = pdf.stat().st_size
    if size >= MAX_BYTES:
        issues.append(f"file is {size} bytes, limit 2 MB")
    return {"pages": pages, "size": size, "text": text, "issues": issues}
