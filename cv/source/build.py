"""Build the CV: content/<lang>.json -> out/<file>.html -> out/<file>.pdf (A4, via headless Chrome).

Layout follows Jake's Resume (github.com/jakegut/resume): single column, centered header,
small-caps section headings with a rule, entries as "bold left / dates right" + "italic left / italic right".
ATS rules applied: one text column in DOM order, no letter-spacing, no ligatures, no icons,
contacts in the body (not in page header/footer), visible link text.

Usage: python3 build.py <content.json> <out.pdf>
Variant of ~/Desktop/work search/cv-source/build.py (28.09.2026) for the job-digest CV tool:
same layout; adds an optional headline under the name; paths come from the command line.
Stage 5 (08.10.2026): PT Serif is bundled in fonts/ (OFL) and loaded from disk, so the server
renders without network and identically to the approved PDF; Chrome path from env CHROME_BIN.
"""
import html
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "out"          # generated HTML
FONTS = ROOT / "fonts"      # PT Serif TTFs from github.com/google/fonts (ofl/ptserif) + OFL.txt
CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
FONT_FACES = (("Regular", 400, "normal"), ("Bold", 700, "normal"),
              ("Italic", 400, "italic"), ("BoldItalic", 700, "italic"))


def chrome_bin():
    """Server: CHROME_BIN=/usr/bin/google-chrome; Mac: the default app path."""
    return os.environ.get("CHROME_BIN") or CHROME_MAC


def font_css():
    """@font-face rules pointing at the bundled TTFs. A private family name, so a system PT Serif
    (macOS ships one) can never stand in silently; build() checks the PDF embeds PTSerif."""
    return "".join(
        f'@font-face {{ font-family: "PT Serif CV"; font-weight: {w}; font-style: {st}; font-display: block;'
        f' src: url("{(FONTS / f"PT_Serif-Web-{name}.ttf").resolve().as_uri()}") format("truetype"); }}\n'
        for name, w, st in FONT_FACES)


REMOTE_FONT = re.compile(r"fonts\.(googleapis|gstatic)\.com|@import\s+url\(\s*['\"]?https?:"
                         r"|url\(\s*['\"]?https?://[^)]*\.(woff2?|ttf|otf)", re.I)


def check_offline(page):
    """CV_OFFLINE=1 guard (and a test hook): the HTML must not fetch fonts or styles over the network."""
    if REMOTE_FONT.search(page):
        raise RuntimeError("CV HTML references a remote font or stylesheet")

CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: #fff; }
body {
  font-family: "PT Serif CV", serif;
  font-size: 10.5pt; line-height: 1.25; color: #000;
  font-variant-ligatures: none; font-kerning: normal;
}
.page { width: 210mm; padding: 11mm 12.7mm 9mm; }
a { color: inherit; text-decoration: underline; text-underline-offset: 1.5pt; text-decoration-thickness: 0.5pt; }
b { font-weight: 700; }

header { text-align: center; }
.headline { font-size: 11pt; margin-top: 1pt; }
.nb { white-space: nowrap; }
h1 { font-size: 24pt; font-weight: 700; font-variant: small-caps; line-height: 1.05; }
.contacts { font-size: 10pt; margin-top: 3pt; }
.contacts .sep { margin: 0 5pt; }

h2 { font-size: 12pt; font-weight: 400; font-variant: small-caps; line-height: 1.2;
     border-bottom: 0.5pt solid #000; padding-bottom: 1pt; margin: 7pt 0 3.5pt; }
.section > * + * { margin-top: 4pt; }
.indent { padding-left: 0.15in; }

.row { display: flex; justify-content: space-between; align-items: baseline; gap: 10pt; }
.row .r { white-space: nowrap; }
.entry { padding-left: 0.15in; }
.sub { font-size: 10pt; font-style: italic; }
ul { margin: 1.5pt 0 0 0; padding-left: calc(0.15in + 11pt); list-style: disc outside; }
li { font-size: 10pt; padding-left: 1pt; }
li + li { margin-top: 0.5pt; }
li::marker { font-size: 8pt; }
.summary { font-size: 10pt; }
.lines > div { font-size: 10pt; }
.lines > div + div { margin-top: 1.5pt; }
"""


def esc(text):
    return html.escape(text, quote=True)


def nobreak(html_text):
    """Keep hyphenated words on one line: a line break at the hyphen makes PDF text extract
    as "crosspromotion", which an ATS keyword search then misses."""
    return re.sub(r"(?<![\w<>/&;])(\w+(?:-\w+)+)", r'<span class="nb">\1</span>', html_text)


def rich(text):
    """Content strings may carry <b>/<i>; everything else is escaped."""
    out = esc(text)
    for tag in ("b", "i"):
        out = out.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return nobreak(out)


def link(text, href):
    return f'<a href="{esc(href)}">{esc(text)}</a>' if href else esc(text)


def row(left, right="", cls=""):
    return f'<div class="row {cls}"><span class="l">{left}</span><span class="r">{right}</span></div>'


def bullets(items):
    return "<ul>" + "".join(f"<li>{b}</li>" for b in items) + "</ul>" if items else ""


def entry(title, dates, sub_left="", sub_right="", items=None):
    html_ = row(f"<b>{esc(title)}</b>", esc(dates))
    if sub_left or sub_right:
        html_ += row(esc(sub_left), esc(sub_right), "sub")
    return f'<div class="entry">{html_}{bullets(items or [])}</div>'


def render_sections(c):
    L = c["labels"]
    s = {}
    if c.get("summary"):
        s["summary"] = f'<p class="summary indent">{rich(c["summary"])}</p>'
    s["experience"] = "".join(
        entry(e["role"], e["dates"], e["org"], e.get("location", ""), [rich(b) for b in e["bullets"]])
        for e in c["experience"])
    edu = ""
    for e in c["education"]:
        items = [f'<b>{esc(L["thesis"])}:</b> <i>{esc(e["thesis"])}</i>'] if e.get("thesis") else []
        edu += entry(e["school"], e["dates"], e["degree"], e.get("location", ""), items)
    s["education"] = edu
    s["certifications"] = '<div class="lines indent">' + "".join(
        "<div>" + row(f'<b>{esc(x["title"])}</b>, {esc(x["issuer"])} | {link(L["verify"], x["href"])}',
                      esc(x["dates"])) + "</div>"
        for x in c["certifications"]) + "</div>"
    s["skills"] = '<div class="lines indent">' + "".join(
        f'<div><b>{esc(k["group"])}:</b> {nobreak(esc(k["items"]))}</div>' for k in c["skills"]) + "</div>"
    s["languages"] = '<div class="lines indent"><div>' + ", ".join(
        f'<b>{esc(x["name"])}</b> ({esc(x["level"])})' for x in c["languages"]) + "</div></div>"
    if c.get("extra"):
        s["extra"] = '<div class="lines indent">' + "".join(
            "<div>" + row(f'<b>{esc(x["title"])}:</b> {esc(x["text"])}', esc(x["dates"])) + "</div>"
            for x in c["extra"]) + "</div>"
    return "".join(
        f'<section class="section"><h2>{esc(L[key])}</h2>{s[key]}</section>'
        for key in c["order"] if key in s)


def render(c):
    sep = '<span class="sep">|</span>'
    contacts = sep.join(link(x["text"], x.get("href")) for x in c["contacts"])
    headline = f'<div class="headline">{esc(c["headline"])}</div>' if c.get("headline") else ""
    return f"""<!doctype html>
<html lang="{c['lang']}"><head><meta charset="utf-8">
<title>{esc(c['name'])} - CV</title>
<style>{font_css()}{CSS}</style></head>
<body><div class="page">
<header><h1>{esc(c['name'])}</h1>{headline}<div class="contacts">{contacts}</div></header>
{render_sections(c)}
</div></body></html>"""


def build(content_path, pdf_path, html_dir=None):
    """Render content JSON to an A4 PDF; returns the PDF path. html_dir: where the intermediate HTML goes
    (default out/; the tailoring worker passes its version folder so parallel runs never share a file)."""
    content_path, pdf_path = pathlib.Path(content_path), pathlib.Path(pdf_path).resolve()
    content = json.loads(content_path.read_text(encoding="utf-8"))
    page = render(content)
    if os.environ.get("CV_OFFLINE") == "1":
        check_offline(page)
    html_dir = pathlib.Path(html_dir) if html_dir else OUT
    html_dir.mkdir(parents=True, exist_ok=True)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    html_path = html_dir / (pdf_path.stem + ".html")
    html_path.write_text(page, encoding="utf-8")
    cmd = [chrome_bin(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
           "--allow-file-access-from-files", "--virtual-time-budget=8000"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        cmd.append("--no-sandbox")  # Chrome refuses to start as root with its sandbox on (server cron/systemd)
    subprocess.run(cmd + [f"--print-to-pdf={pdf_path}", html_path.as_uri()],
                   check=True, capture_output=True, timeout=120)
    if b"PTSerif" not in pdf_path.read_bytes():
        raise RuntimeError("PDF does not embed PT Serif: bundled font failed to load")
    return pdf_path


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(build(sys.argv[1], sys.argv[2]))
