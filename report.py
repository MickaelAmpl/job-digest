"""Misha's own record of the search as one self-contained HTML file: the funnel from cards to offers,
what is marked Интересно but not sent yet, where the CV went and what happened, why jobs did not fit,
which sources and companies work. Inline CSS and a few lines of JS only: it opens offline on a phone
or a Mac, light or dark. data/state.db stays the source of truth; this file is a snapshot.
The bot's /report sends it; also runnable for tests: python3 report.py out/report.html"""

import html
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import store
from digest import LABELS, MANUAL_APPLIED, MONTHS, REASONS, STATUS_LABELS

REASON_LABELS = {k: dict(v) for k, v in REASONS.items()}
SOURCE_LABELS = {"company": "сайт компании", "adzuna": "Adzuna", "linkedin": "LinkedIn", "stepstone": "StepStone",
                 "derstandard": "derStandard", "karriere": "karriere.at", "uniwien": "Uni Wien", "manual": "вручную"}
# Vacancies delivered by the bot with buttons. Applications Misha added by hand (source 'manual') are not cards.
CARDS = "sent_at is not null and coalesce(listed,0)=0 and " + store.NOT_MANUAL


def link(row):
    return store.usable_url(row["open_url"]) or store.usable_url(row["url"])


def days_since(iso, today):
    try:
        return (today - datetime.fromisoformat(iso).date()).days
    except (TypeError, ValueError):
        return None


def short(iso):
    """'2026-10-08T07:30:00' -> '08.10.2026' (shown) and the ISO string (sorted by)."""
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return "-"


def reasons_text(row):
    try:
        keys = json.loads(row["reasons"] or "[]")
    except ValueError:
        keys = []
    return ", ".join(REASON_LABELS.get(row["verdict"], {}).get(k, k) for k in keys)


def stats(con):
    """Counts shared by /stats and the report, so both show the same numbers."""
    one = lambda sql: con.execute(sql).fetchone()[0]
    stage = store.stages(con)
    status = Counter(r[0] for r in con.execute("select status from jobs where verdict='interested' and status is not null"))
    reasons = {v: Counter() for v in REASONS}
    no_reason = Counter()
    for r in con.execute("select verdict, reasons from jobs where verdict in ('cant_fit','bad_terms')"):
        try:
            keys = json.loads(r["reasons"] or "[]")
        except ValueError:
            keys = []
        reasons[r["verdict"]].update(keys)
        if not keys:
            no_reason[r["verdict"]] += 1
    return {
        "cards": one(f"select count(*) from jobs where {CARDS}"),
        "verdicts": dict(con.execute(f"select coalesce(verdict,''), count(*) from jobs where {CARDS} group by verdict").fetchall()),
        "marked": one(f"select count(*) from jobs where verdict is not null and {store.NOT_MANUAL}"),
        "interested": one(f"select count(*) from jobs where verdict='interested' and {store.NOT_MANUAL}"),
        # applications Misha added by hand, plus bot cards he attached one to (digest.MANUAL_APPLIED)
        "manual": one(f"select count(*) from jobs where {MANUAL_APPLIED}"),
        "applied": sum(1 for s in stage.values() if s >= 1),
        "replied": sum(1 for s in stage.values() if s >= 2),
        "interview": sum(1 for s in stage.values() if s >= 3),
        "offer": sum(1 for s in stage.values() if s >= 4),
        "rejected": status["rejected"],
        "no_answer": status["no_answer"],
        "reasons": reasons,
        "no_reason": no_reason,
        "stage": stage,
    }


def funnel_rows(st):
    """Funnel steps for the report and /stats. Applications (Отправил резюме and further) include the manual ones;
    'Интересно' and the cards are the bot's only, so the manual line is shown on its own."""
    return [("Прислано карточек", st["cards"]), ("Интересно", st["interested"]), ("Отправил резюме", st["applied"]),
            ("Ответили", st["replied"]), ("Интервью", st["interview"]), ("Оффер", st["offer"]),
            ("Добавлено вручную", st["manual"])]


CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1b1b1a;--muted:#6b6b66;--line:#e3e2dd;--bar:#2f6f5e;--bar2:#c9792b;--soft:#eef3f1;--link:#1f5f9e}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--panel:#1d1d1c;--ink:#ecebe6;--muted:#a3a29b;--line:#33332f;--bar:#5fb39a;--bar2:#e0a15e;--soft:#22302b;--link:#8cbcf0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;-webkit-text-size-adjust:100%}
main{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:32px 0 10px}
.sub{color:var(--muted);margin:0 0 16px}
.totals{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 0}
.totals div{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:120px}
.totals b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.totals span{color:var(--muted);font-size:13px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}
.bars{display:grid;grid-template-columns:minmax(110px,max-content) 1fr 3.5em;gap:6px 10px;align-items:center}
.bars .track{background:var(--soft);border-radius:4px;height:14px;overflow:hidden}
.bars .fill{background:var(--bar);height:100%;min-width:2px}
.bars .fill.alt{background:var(--bar2)}
.bars .n{text-align:right;font-variant-numeric:tabular-nums}
.note{color:var(--muted);font-size:13px;margin:10px 0 0}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.cols h3{font-size:15px;margin:0 0 8px}
.filter{width:100%;max-width:360px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--ink);font:inherit;margin:0 0 8px}
.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
tbody td:first-child{min-width:170px}
th{white-space:nowrap;font-weight:600;color:var(--muted)}
th button{all:unset;cursor:pointer}
th button:focus-visible{outline:2px solid var(--link);outline-offset:2px}
th[aria-sort=ascending] button::after{content:" \\2191"}
th[aria-sort=descending] button::after{content:" \\2193"}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.date{white-space:nowrap}
a{color:var(--link)}
.empty td{color:var(--muted)}
"""

JS = """
document.querySelectorAll('table.sortable').forEach(function(t){
  t.querySelectorAll('th').forEach(function(th,i){
    var b=th.querySelector('button'); if(!b) return;
    b.addEventListener('click',function(){
      var dir=th.getAttribute('aria-sort')==='ascending'?'descending':'ascending';
      t.querySelectorAll('th').forEach(function(h){h.removeAttribute('aria-sort')});
      th.setAttribute('aria-sort',dir);
      var body=t.tBodies[0], rows=[].slice.call(body.rows).filter(function(r){return !r.classList.contains('empty')});
      var key=function(r){var c=r.cells[i], v=c.getAttribute('data-v'); if(v===null) v=c.textContent.trim();
        return /^-?\\d+(\\.\\d+)?$/.test(v)?parseFloat(v):v.toLowerCase()};
      rows.sort(function(a,b){var x=key(a), y=key(b);
        var r=(typeof x==='number'&&typeof y==='number')?x-y:String(x).localeCompare(String(y),'ru');
        return dir==='ascending'?r:-r});
      rows.forEach(function(r){body.appendChild(r)});
    });
  });
});
document.querySelectorAll('input[data-filter]').forEach(function(inp){
  inp.addEventListener('input',function(){
    var q=inp.value.trim().toLowerCase(), t=document.getElementById(inp.getAttribute('data-filter'));
    [].forEach.call(t.tBodies[0].rows,function(r){
      if(!r.classList.contains('empty')) r.hidden=!!q&&r.textContent.toLowerCase().indexOf(q)<0;
    });
  });
});
"""

e = html.escape


def cell(text, sort=None, cls=""):
    attrs = (f' class="{cls}"' if cls else "") + (f' data-v="{e(str(sort), quote=True)}"' if sort is not None else "")
    return f"<td{attrs}>{text}</td>"


def manual_application(row):
    """The rows digest.MANUAL_APPLIED counts: a manual application or an attached card, with a status. A card whose
    status was reset is no application any more, so it is not marked either."""
    return row["status"] is not None and store.is_manual(row)


def title_cell(row):
    """The title, linked when there is a link, and '(вручную)' after it for a manual application (manual_application)."""
    url, title = link(row), e(row["title"] or "")
    text = f'<a href="{e(url, quote=True)}" target="_blank" rel="noopener">{title}</a>' if url else title
    if manual_application(row):
        text += " (вручную)"
    return cell(text, sort=(row["title"] or "").lower())


def table(tid, heads, rows, empty):
    """A sortable table with a filter box above it. rows: lists of ready <td> strings."""
    th = "".join(f'<th scope="col"><button type="button">{e(h)}</button></th>' for h in heads)
    body = "".join("<tr>" + "".join(r) + "</tr>" for r in rows) or \
        f'<tr class="empty"><td colspan="{len(heads)}">{e(empty)}</td></tr>'
    box = (f'<input class="filter" type="search" data-filter="{tid}" placeholder="Фильтр: название, компания..."'
           f' aria-label="Фильтр таблицы">') if rows else ""
    return f'{box}<div class="wrap"><table id="{tid}" class="sortable"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def bars(items, top=None, alt=False):
    top = top or max([n for _, n in items] + [1])
    out = []
    for label, n in items:
        width = round(100 * n / top, 1) if top else 0
        out.append(f'<div>{e(label)}</div><div class="track"><div class="fill{" alt" if alt else ""}"'
                   f' style="width:{width}%"></div></div><div class="n">{n}</div>')
    return '<div class="bars">' + "".join(out) + "</div>"


def build(con, today=None):
    """The whole report as one HTML string. `today` (a date) is injectable for tests."""
    today = today or date.today()
    st = stats(con)
    stage = st["stage"]
    num = lambda v: cell(str(v) if v is not None else "-", sort=v if v is not None else -1, cls="num")
    datec = lambda iso: cell(short(iso), sort=iso or "", cls="date")
    src = lambda r: cell(e(SOURCE_LABELS.get(r["source"], r["source"] or "-")))
    company = lambda r: cell(e(r["company"] or ""))

    # (a) Интересно, резюме ещё не отправлено - oldest mark first, as in /interesting
    # same rows as /interesting: open vacancies only (a closed one is in the liveness lines, not here)
    todo = con.execute("select * from jobs where verdict='interested' and status is null and coalesce(closed,0)=0"
                       " order by verdict_at").fetchall()
    rows_a = [[title_cell(r), company(r), num(r["score"]), src(r), datec(r["verdict_at"])] for r in todo]

    # (b) Отправлено: everything with a status, in chain order then by date sent
    order = {s: i for i, s in enumerate(store.STATUSES)}
    sent = sorted(con.execute("select * from jobs where verdict='interested' and status is not null").fetchall(),
                  key=lambda r: (order.get(r["status"], 9), r["applied_at"] or ""))
    rows_b = []
    for r in sent:
        days = days_since(r["applied_at"], today)
        rows_b.append([title_cell(r), company(r), cell(e(STATUS_LABELS.get(r["status"], r["status"])),
                       sort=order.get(r["status"], 9)), datec(r["applied_at"]), num(days), datec(r["status_at"])])

    # (c) Все отмеченные
    marked = con.execute(f"select * from jobs where verdict is not null and {store.NOT_MANUAL}"
                         " order by verdict_at desc").fetchall()
    rows_c = [[title_cell(r), company(r), cell(e(LABELS.get(r["verdict"], r["verdict"]))), cell(e(reasons_text(r))),
               num(r["score"]), src(r), datec(r["verdict_at"])] for r in marked]

    # Sources: cards sent, bot's 'interesting' marks, applications (manual ones are their own source, 'вручную')
    per = {}  # source -> [cards, interesting, applied]
    for r in con.execute(f"select id, source, verdict, {CARDS} as card, {store.NOT_MANUAL} as bot_row from jobs"
                         f" where {CARDS} or verdict is not null"):
        p = per.setdefault(r["source"] or "-", [0, 0, 0])
        p[0] += bool(r["card"])
        p[1] += bool(r["bot_row"] and r["verdict"] == "interested")
        p[2] += stage.get(r["id"], 0) >= 1
    rows_s = [[cell(e(SOURCE_LABELS.get(s, s))), num(v[0]), num(v[1]), num(v[2])]
              for s, v in sorted(per.items(), key=lambda kv: -kv[1][0])]

    # Companies that replied (Ответили or further)
    replied = [r for r in sent if stage.get(r["id"], 0) >= 2]
    rows_r = [[company(r), title_cell(r), cell(e(STATUS_LABELS.get(r["status"], r["status"]))), datec(r["status_at"])]
              for r in replied]

    # Reasons: "чего чаще всего не хватает"
    reason_blocks = []
    for verdict, counter in st["reasons"].items():
        items = [(REASON_LABELS[verdict].get(k, k), n) for k, n in counter.most_common()]
        if st["no_reason"][verdict]:
            items.append(("без причины", st["no_reason"][verdict]))
        body = bars(items, alt=verdict == "bad_terms") if items else '<p class="note">Пока нет отметок.</p>'
        reason_blocks.append(f'<div class="panel"><h3>{e(LABELS[verdict])}</h3>{body}</div>')

    rate = f"{round(100 * st['replied'] / st['applied'])}%" if st["applied"] else "-"
    totals = [("карточек прислано", st["cards"]), ("отмечено", st["marked"]), ("интересно", st["interested"]),
              ("резюме отправлено", st["applied"]), ("ответили", st["replied"])]
    head = (f"<h1>Поиск работы: отчёт</h1><p class=\"sub\">На {today.day} {MONTHS[today.month - 1]} {today.year}."
            f" Ответили на {rate} отправленных резюме.</p><div class=\"totals\">" +
            "".join(f"<div><b>{n}</b><span>{e(t)}</span></div>" for t, n in totals) + "</div>")
    funnel = (f'<div class="panel">{bars(funnel_rows(st))}<p class="note">Отказ: {st["rejected"]} · '
              f'Нет ответа: {st["no_answer"]}. Отказ после интервью считается и в «Ответили», и в «Интервью».</p></div>')

    sections = [
        head,
        "<h2>Воронка</h2>" + funnel,
        f"<h2>Интересно, резюме ещё не отправлено ({len(rows_a)})</h2>" +
        table("t-todo", ["Вакансия", "Компания", "Оценка", "Источник", "Отмечено"], rows_a,
              "Нет вакансий, отмеченных «Интересно» без отправленного резюме."),
        f"<h2>Отправлено ({len(rows_b)})</h2>" +
        table("t-sent", ["Вакансия", "Компания", "Статус", "Резюме отправлено", "Дней прошло", "Последнее изменение"],
              rows_b, "Пока ни одного отправленного резюме."),
        "<h2>Чего чаще всего не хватает</h2><div class=\"cols\">" + "".join(reason_blocks) + "</div>",
        "<h2>Источники</h2>" + table("t-src", ["Источник", "Карточек", "Интересно", "Отправил резюме"], rows_s,
                                     "Карточек ещё не было."),
        f"<h2>Компании, которые ответили ({len(rows_r)})</h2>" +
        table("t-replied", ["Компания", "Вакансия", "Статус", "Когда"], rows_r, "Пока никто не ответил."),
        f"<h2>Все отмеченные вакансии ({len(rows_c)})</h2>" +
        table("t-all", ["Вакансия", "Компания", "Отметка", "Причины", "Оценка", "Источник", "Дата"], rows_c,
              "Ещё ничего не отмечено."),
    ]
    return ("<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"color-scheme\" content=\"light dark\">"
            f"<title>Отчёт по поиску {today:%d.%m.%Y}</title><style>{CSS}</style></head>"
            f"<body><main>{''.join(sections)}</main><script>{JS}</script></body></html>")


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python3 report.py out/report.html")
    path = Path(sys.argv[1])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build(store.connect()), encoding="utf-8")
    print("report:", path)


if __name__ == "__main__":
    main()
