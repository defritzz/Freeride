#!/usr/bin/env python3
"""Magic Pass freeride-prognose.

Haalt per gebied meerdere weermodellen op via Open-Meteo, berekent per dag een
freeride-score, maakt een HTML-rapport met sneeuwkaarten en stuurt een melding
(ntfy en/of Telegram) wanneer er sneeuw op komst is.

Alleen standaardbibliotheek, Python 3.9+.  Zie README.md.
"""
import argparse
import csv
import hashlib
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    TZINFO = ZoneInfo("Europe/Zurich")
except Exception:  # geen tzdata: val terug op lokale tijd
    TZINFO = None

API = "https://api.open-meteo.com/v1/forecast"
TZ = "Europe/Zurich"
# MeteoSwiss ICON is het fijnst voor de Alpen, ECMWF/ICON/GFS geven spreiding.
MODELS = ["meteoswiss_icon_seamless", "ecmwf_ifs025", "icon_seamless", "gfs_seamless"]
HOURLY = ["temperature_2m", "snowfall", "freezing_level_height", "wind_speed_10m",
          "wind_gusts_10m", "wind_direction_10m", "cloud_cover", "snow_depth"]


def now_local():
    return datetime.now(TZINFO) if TZINFO else datetime.now()


# ----------------------------------------------------------------- gegevens
def load_resorts(path, max_tier):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for k in ("lat", "lon"):
                r[k] = float(r[k])
            for k in ("top_m", "base_m", "tier"):
                r[k] = int(r[k])
            if r["tier"] <= max_tier:
                rows.append(r)
    return rows


def api_get(params, tries=3):
    url = API + "?" + urllib.parse.urlencode(params, safe=",")
    for t in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "magicpass-freeride/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and t < tries - 1:
                time.sleep(65)
                continue
            raise
        except urllib.error.URLError:
            if t < tries - 1:
                time.sleep(5)
                continue
            raise


def probe_models(wanted):
    ok = []
    for m in wanted:
        try:
            api_get({"latitude": 46.1, "longitude": 7.93, "hourly": "snowfall",
                     "models": m, "forecast_days": 1, "timezone": TZ}, tries=1)
            ok.append(m)
        except Exception as e:
            print(f"  model {m} niet beschikbaar, overgeslagen ({e})", file=sys.stderr)
    return ok


def fetch_all(resorts, days, models, demo=False, batch=10):
    out = []
    for i in range(0, len(resorts), batch):
        b = resorts[i:i + batch]
        if demo:
            data = mock_batch(b, days, models)
        else:
            data = api_get({
                "latitude": ",".join(f"{r['lat']:.4f}" for r in b),
                "longitude": ",".join(f"{r['lon']:.4f}" for r in b),
                "elevation": ",".join(str(r["top_m"]) for r in b),
                "hourly": ",".join(HOURLY), "models": ",".join(models),
                "past_days": 2, "forecast_days": days, "timezone": TZ,
                "wind_speed_unit": "kmh",
            })
            if isinstance(data, dict):
                data = [data]
            time.sleep(1)
        out.extend(data)
    return out


def mock_batch(batch, days, models):
    """Synthetische data in exact het Open-Meteo-formaat (om offline te testen)."""
    start = datetime.combine(now_local().date(), datetime.min.time()) - timedelta(days=2)
    n = (days + 2) * 24
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    res = []
    for r in batch:
        rnd = random.Random(int(r["lat"] * 1000 + r["lon"] * 100))
        west = max(0.15, 1.3 - (r["lon"] - 6.3) / 2.5)
        alt = (r["top_m"] - 1500) / 1500
        hourly = {"time": times}
        for mi, m in enumerate(models):
            bias = 1 + 0.2 * (mi - (len(models) - 1) / 2)
            cols = {v: [] for v in HOURLY}
            depth = 0.2
            for i in range(n):
                d = i / 24
                p = math.exp(-((d - 2.6) / 0.6) ** 2) + 0.7 * math.exp(-((d - 5.9) / 0.5) ** 2)
                diurn = 2.5 * math.sin((i % 24 - 9) / 24 * 2 * math.pi)
                temp = -5 - 3 * alt + 3 * p + diurn
                snow = max(0, 2.4 * p * west * (0.5 + alt) * bias * (0.7 + 0.6 * rnd.random())) if temp < 2 else 0
                depth += snow / 100 * 0.8
                cols["temperature_2m"].append(round(temp, 1))
                cols["snowfall"].append(round(snow, 2))
                cols["freezing_level_height"].append(round(1900 - 850 * p + rnd.uniform(-60, 60)))
                cols["wind_speed_10m"].append(round(12 + 35 * p + rnd.uniform(0, 6), 1))
                cols["wind_gusts_10m"].append(round(22 + 60 * p + rnd.uniform(0, 8), 1))
                cols["wind_direction_10m"].append(round(255 + 20 * p + rnd.uniform(-10, 10)))
                cols["cloud_cover"].append(round(min(100, 25 + 90 * min(1, p * 1.4) + rnd.uniform(0, 8))))
                cols["snow_depth"].append(round(depth, 2))
            for v, series in cols.items():
                hourly[f"{v}_{m}" if len(models) > 1 else v] = series
        res.append({"hourly": hourly})
    return res


# ---------------------------------------------------------------- analyse
def avg(x):
    return sum(x) / len(x) if x else None


def stat(per_model):
    if not per_model:
        return (0.0, 0.0, 0.0)
    return (sum(per_model) / len(per_model), min(per_model), max(per_model))


def collect(h, var, models):
    lists = []
    for m in models:
        for key in (f"{var}_{m}", var):
            v = h.get(key)
            if v is not None and any(x is not None for x in v):
                lists.append(v)
                break
    return lists


def compass(deg):
    return ["N", "NO", "O", "ZO", "Z", "ZW", "W", "NW"][int(((deg % 360) + 22.5) // 45) % 8]


def circ_mean(pairs):
    sx = sum(w * math.sin(math.radians(d)) for d, w in pairs)
    sy = sum(w * math.cos(math.radians(d)) for d, w in pairs)
    if abs(sx) + abs(sy) < 1e-9:
        return None
    return math.degrees(math.atan2(sx, sy)) % 360


def score_day(d, top_m):
    """0-100. Poeder (45), zon (20), wind (20), temperatuur (10), sneeuwdek (5)."""
    flags = []
    fresh = d["fresh"][0]
    s = 45 * min(fresh, 40) / 40
    sun = 1 - (d["cloud"] or 50) / 100
    s += 20 * sun
    if d["during"][0] > 5:
        s -= 10
        flags.append("het sneeuwt tijdens de dag: slecht zicht")
    g = d["gust"] or 0
    s += 20 * max(0.0, min(1.0, (70 - g) / 45))
    if g >= 60:
        flags.append("zware windstoten: liften kunnen dicht")
    t = d["temp"]
    if t is not None:
        if -14 <= t <= -4:
            s += 10
        elif t > -4:
            s += 10 * max(0.0, (2 - t) / 6)
        else:
            s += 6
    if d["depth"] is not None:
        s += 5 * max(0.0, min(1.0, (d["depth"] - 20) / 30))
    fl = d["fl"]
    if fl is not None and fl > top_m + 150:
        s = min(s, 25)
        flags.append("sneeuwgrens boven de top: regen of natte sneeuw")
    f24 = d["fresh"][0]
    if f24 >= 50 or (f24 >= 30 and g >= 50):
        s -= 15
        flags.append("veel verse sneeuw en wind: lawinegevaar waarschijnlijk hoog, lees het SLF-bulletin")
    elif f24 >= 25 or (f24 >= 10 and g >= 45):
        s -= 5
        flags.append("verse sneeuw met wind: lawinegevaar waarschijnlijk verhoogd, check het SLF-bulletin")
    if d["lee"] and d["fresh"][0] >= 10:
        flags.append("stuifsneeuw vooral op " + "/".join(d["lee"]) + "-hellingen (lijzijde)")
    return max(0, min(100, round(s))), flags


def analyse(resort, raw, models, ndays):
    h = raw["hourly"]
    times = h["time"]
    n = len(times)
    L = {v: collect(h, v, models) for v in HOURLY}

    def vals(var, i):
        return [l[i] for l in L[var] if i < len(l) and l[i] is not None]

    def at(var, i):
        return avg(vals(var, i))

    T = [at("temperature_2m", i) for i in range(n)]
    cold = [t is not None and t <= 1.0 for t in T]
    S = [at("snowfall", i) or 0.0 for i in range(n)]

    def snow_sum(a, b):
        a, b = max(a, 0), min(b, n)
        per = [sum((l[i] or 0.0) for i in range(a, min(b, len(l))) if cold[i]) for l in L["snowfall"]]
        return stat(per)

    def window(var, a, b):
        return [x for x in (at(var, i) for i in range(max(a, 0), min(b, n))) if x is not None]

    base = {t[:10]: i for i, t in enumerate(times) if t.endswith("T00:00")}
    today = now_local().strftime("%Y-%m-%d")
    nowt = now_local().strftime("%Y-%m-%dT%H:00")
    now_i = next((i for i, t in enumerate(times) if t >= nowt), 0)
    days = []
    for ds in sorted(base):
        if ds < today or len(days) >= ndays:
            continue
        b = base[ds]
        w = [(wd, S[i]) for i in range(max(b - 48, 0), min(b + 24, n))
             if cold[i] and S[i] > 0.1 for wd in [at("wind_direction_10m", i)] if wd is not None]
        cm = circ_mean(w)
        lee = None
        if cm is not None:
            lee_d = (cm + 180) % 360
            lee = [compass(lee_d - 45), compass(lee_d), compass(lee_d + 45)]
        depth = avg(window("snow_depth", b + 9, b + 17))
        d = {
            "date": ds,
            "fresh": snow_sum(b - 48, b + 10),
            "during": snow_sum(b + 10, b + 17),
            "total": snow_sum(b, b + 24),
            "temp": avg(window("temperature_2m", b + 9, b + 17)),
            "gust": max(window("wind_gusts_10m", b + 9, b + 17) or [0]),
            "cloud": avg(window("cloud_cover", b + 9, b + 17)),
            "fl": avg(window("freezing_level_height", b + 9, b + 17)),
            "fl_min": min(window("freezing_level_height", b, b + 24) or [0]),
            "depth": depth * 100 if depth is not None else None,
            "lee": lee,
            "status": status_on(resort, ds),
        }
        d["score"], d["flags"] = score_day(d, resort["top_m"])
        days.append(d)
    return {"r": resort, "days": days, "ahead": None, "now_i": now_i, "snow_fn": snow_sum}


def status_on(r, ds):
    if r["opens"] and ds >= r["opens"]:
        return "open"
    if r["maybe"] and ds >= r["maybe"]:
        return "mogelijk"
    if not r["opens"] and not r["maybe"]:
        return "onbekend"
    return "dicht"


def attach_ahead(results, hours):
    for x in results:
        a = x["now_i"]
        x["ahead"] = x["snow_fn"](a, a + hours)
        x["snow_fn"] = None


# ----------------------------------------------------------------- melding
def summarize(results, hours):
    top = sorted(results, key=lambda x: -x["ahead"][0])[:8]
    cands = [(d["score"], x, d) for x in results for d in x["days"] if d["status"] != "dicht"]
    cands.sort(key=lambda c: -c[0])
    best_by_resort, seen = [], set()
    for sc, x, d in cands:
        if x["r"]["name"] not in seen:
            seen.add(x["r"]["name"])
            best_by_resort.append((sc, x, d))
    return top, best_by_resort[:6]


def build_message(top, best, hours, ndays):
    lines = [f"Verwachte nieuwe sneeuw komende {hours} uur (gemiddelde van de modellen, tussen haakjes de spreiding):"]
    for x in top:
        r, a = x["r"], x["ahead"]
        d0 = x["days"][0] if x["days"] else None
        extra = ""
        if d0:
            extra = f", sneeuwgrens ~{round(d0['fl_min'], -2):.0f} m, wind tot {d0['gust']:.0f} km/u"
            if d0["lee"]:
                extra += f", stuifsneeuw {'/'.join(d0['lee'])}"
        lines.append(f"- {r['name']} ({r['top_m']} m, {x['days'][0]['status'] if d0 else '?'}): "
                     f"+{a[0]:.0f} cm ({a[1]:.0f}-{a[2]:.0f}){extra}")
    lines.append("")
    lines.append(f"Beste freeride-dagen (open of mogelijk open) in de komende {ndays} dagen:")
    for sc, x, d in best:
        dt = datetime.strptime(d["date"], "%Y-%m-%d")
        dag = ["ma", "di", "wo", "do", "vr", "za", "zo"][dt.weekday()]
        lines.append(f"- {dag} {dt.day}/{dt.month}: {x['r']['name']}, score {sc}/100, "
                     f"{d['fresh'][0]:.0f} cm vers, {d['status']}")
    lines.append("")
    lines.append("Indicatie op basis van weermodellen. Lees altijd het SLF-lawinebulletin en check de liftstatus.")
    return "\n".join(lines)


def _latin1(s):
    return s.encode("latin-1", "replace").decode("latin-1")


def send_ntfy(server, topic, title, body, click=None, attach=None):
    headers = {"Title": _latin1(title), "Tags": "snowflake,ski", "Priority": "high"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"{server.rstrip('/')}/{topic}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
    urllib.request.urlopen(req, timeout=30).read()
    if attach and Path(attach).exists():
        req = urllib.request.Request(f"{server.rstrip('/')}/{topic}", data=Path(attach).read_bytes(),
                                     headers={"Filename": "freeride-rapport.html", "Title": "Rapport"},
                                     method="PUT")
        urllib.request.urlopen(req, timeout=60).read()


def send_telegram(token, chat, title, body):
    data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n\n{body}"[:4000]}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=30).read()


# ------------------------------------------------------------------ rapport
CITIES = [("Genève", 6.14, 46.20), ("Lausanne", 6.63, 46.52), ("Bern", 7.45, 46.95),
          ("Sion", 7.36, 46.23), ("Luzern", 8.31, 47.05), ("Zürich", 8.54, 47.37)]
LON0, LON1, LAT0, LAT1 = 5.8, 9.0, 45.8, 47.45


def svg_map(results, di, W=420):
    H = int(W * (LAT1 - LAT0) * 111 / ((LON1 - LON0) * 76))
    px = lambda lon: (lon - LON0) / (LON1 - LON0) * W
    py = lambda lat: (LAT1 - lat) / (LAT1 - LAT0) * H
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="sneeuwkaart">']
    for lon in (6, 7, 8, 9):
        out.append(f'<line x1="{px(lon):.1f}" y1="0" x2="{px(lon):.1f}" y2="{H}" class="ch"/>'
                   f'<text x="{px(lon) + 2:.1f}" y="{H - 3}" class="gl">{lon}°O</text>')
    for lat in (46, 47):
        out.append(f'<line x1="0" y1="{py(lat):.1f}" x2="{W}" y2="{py(lat):.1f}" class="ch"/>'
                   f'<text x="2" y="{py(lat) - 2:.1f}" class="gl">{lat}°N</text>')
    for nm, lo, la in CITIES:
        out.append(f'<rect x="{px(lo) - 2:.1f}" y="{py(la) - 2:.1f}" width="4" height="4" class="city"/>'
                   f'<text x="{px(lo) + 5:.1f}" y="{py(la) + 3:.1f}" class="gl">{nm}</text>')
    rows = [(x["days"][di]["total"][0], x) for x in results if di < len(x["days"])]
    for cm, x in sorted(rows, key=lambda t: t[0]):
        r = x["r"]
        rad = 2.0 + 1.1 * math.sqrt(cm)
        cls = "dot" if x["days"][di]["status"] != "dicht" else "dot closed"
        out.append(f'<circle cx="{px(r["lon"]):.1f}" cy="{py(r["lat"]):.1f}" r="{rad:.1f}" class="{cls}" '
                   f'style="opacity:{min(.9, .25 + cm / 40):.2f}"><title>{escape(r["name"])}: {cm:.0f} cm</title></circle>')
    for k, (cm, x) in enumerate(sorted(rows, key=lambda t: -t[0])[:2]):
        if cm >= 5:
            r = x["r"]
            out.append(f'<text x="{px(r["lon"]) + 8:.1f}" y="{py(r["lat"]) - 6 - 11 * k:.1f}" class="lb">'
                       f'{escape(r["name"].split(" (")[0])} {cm:.0f} cm</text>')
    out.append("</svg>")
    return "".join(out)


def cell_class(sc, status):
    if status == "dicht":
        return "c-x"
    return "c-g" if sc >= 70 else "c-y" if sc >= 50 else "c-o" if sc >= 30 else "c-r"


CSS = """
:root{--bg:#f4f7fa;--card:#fff;--tx:#14202b;--mu:#5b6b79;--ln:#dbe3ea;--ac:#1c6fd1;color-scheme:light;
--g:#c9ecd6;--y:#e7efbf;--o:#fbe1b4;--r:#f6c9c4;--x:#e8ecf0}
@media (prefers-color-scheme:dark){:root{--bg:#0f1720;--card:#18232f;--tx:#e8eef4;--mu:#9fb0bf;--ln:#2a3947;
--ac:#6aa9f5;color-scheme:dark;--g:#1f4a33;--y:#46502a;--o:#58431d;--r:#5a2a26;--x:#222d38}}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1000px;margin:0 auto;padding:18px 14px 40px}h1{margin:.2em 0}h2{font-size:1.1rem;margin:0 0 .6em}
.card{background:var(--card);border:1px solid var(--ln);border-radius:12px;padding:14px;margin:0 0 14px}
.fine{font-size:.82rem;color:var(--mu)}.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.86rem}
th,td{padding:6px 8px;border-bottom:1px solid var(--ln);text-align:left;vertical-align:top;white-space:nowrap}
.c-g{background:var(--g)}.c-y{background:var(--y)}.c-o{background:var(--o)}.c-r{background:var(--r)}.c-x{background:var(--x);color:var(--mu)}
.maps{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:10px}.maps h3{font-size:.85rem;margin:0}
svg{width:100%;height:auto}.ch{fill:none;stroke:var(--mu);stroke-width:1;stroke-dasharray:3 2}
.dot{fill:var(--ac)}.dot.closed{fill:none;stroke:var(--ac);stroke-dasharray:2 2}.gl{fill:var(--mu);font-size:9px}.city{fill:var(--mu)}.lb{fill:var(--tx);font-size:11px;font-weight:600}
ul{margin:.3em 0;padding-left:1.2em}
"""


def render(results, top, best, models, hours, out_path, ndays):
    gen = now_local().strftime("%d/%m/%Y %H:%M")
    dates = [d["date"] for d in results[0]["days"]]
    wd = ["ma", "di", "wo", "do", "vr", "za", "zo"]
    lab = [f"{wd[datetime.strptime(x, '%Y-%m-%d').weekday()]} {x[8:]}/{x[5:7]}" for x in dates]
    h = [f"<!DOCTYPE html><html lang='nl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>Freeride-prognose Magic Pass</title><style>{CSS}</style></head><body><main>",
         f"<h1>Freeride-prognose Magic Pass</h1><p class='fine'>Gegenereerd {gen}. Modellen: {escape(', '.join(models))}. "
         f"Gemiddelde van de modellen; de spreiding staat in de tooltips.</p>"]
    h.append("<section class='card'><h2>Beste dagen en gebieden (open of mogelijk open)</h2><ul>")
    for sc, x, d in best:
        i = dates.index(d["date"])
        h.append(f"<li><b>{lab[i]}: {escape(x['r']['name'])}</b> (score {sc}/100, {d['fresh'][0]:.0f} cm vers, {d['status']})"
                 + "".join(f"<br><span class='fine'>{escape(f)}</span>" for f in d["flags"]) + "</li>")
    if not best:
        h.append("<li>Geen enkel gebied is open of mogelijk open in deze periode.</li>")
    h.append("</ul></section>")
    h.append(f"<section class='card'><h2>Sneeuw op komst, komende {hours} uur</h2><div class='wrap'><table>"
             "<tr><th>Gebied</th><th>Status</th><th>Nieuwe sneeuw</th><th>Sneeuwgrens</th><th>Windstoten</th><th>Lijzijde</th></tr>")
    for x in top:
        d0 = x["days"][0]
        a = x["ahead"]
        h.append(f"<tr><td>{escape(x['r']['name'])}</td><td>{d0['status']}</td><td>+{a[0]:.0f} cm ({a[1]:.0f}-{a[2]:.0f})</td>"
                 f"<td>{d0['fl_min']:.0f} m</td><td>{d0['gust']:.0f} km/u</td><td>{'/'.join(d0['lee']) if d0['lee'] else '-'}</td></tr>")
    h.append("</table></div></section><section class='card'><h2>Sneeuwkaarten: nieuwe sneeuw per dag (cm)</h2><div class='maps'>")
    for i in range(min(5, len(dates))):
        h.append(f"<div><h3>{lab[i]}</h3>{svg_map(results, i)}</div>")
    h.append("</div><p class='fine'>Schematische kaart: stippen zijn gebieden, grootte = cm nieuwe sneeuw, "
             "gestippeld = gebied nog dicht. Gradenlijnen en enkele steden dienen als oriëntatie.</p></section>")
    h.append("<section class='card'><h2>Alle gebieden, score per dag</h2><div class='wrap'><table><tr><th>Gebied</th><th>Top</th><th>Status</th>"
             + "".join(f"<th>{l}</th>" for l in lab) + "</tr>")
    order = sorted(results, key=lambda x: -max((d["score"] for d in x["days"] if d["status"] != "dicht"), default=-1))
    for x in order:
        r = x["r"]
        tip = escape(r.get("note") or "")
        h.append(f"<tr><td title='{tip}'>{escape(r['name'])}</td><td>{r['top_m']}</td><td>{x['days'][0]['status']}</td>")
        for d in x["days"]:
            t = (f"{d['fresh'][0]:.0f} cm vers ({d['fresh'][1]:.0f}-{d['fresh'][2]:.0f}); dag +{d['total'][0]:.0f} cm; "
                 f"temp {d['temp'] if d['temp'] is None else round(d['temp'])} C; wind tot {d['gust']:.0f} km/u; "
                 f"sneeuwgrens {d['fl']:.0f} m. " + " | ".join(d["flags"]))
            h.append(f"<td class='{cell_class(d['score'], d['status'])}' title='{escape(t)}'>{d['score']} <span class='fine'>+{d['total'][0]:.0f}</span></td>")
        h.append("</tr>")
    h.append("</table></div><p class='fine'>Cel = score/100 en nieuwe sneeuw die dag. Grijs = gebied dicht volgens de openingsdata in resorts.csv. "
             "Score is een eigen heuristiek (poeder 45, zon 20, wind 20, temperatuur 10, sneeuwdek 5).</p></section>")
    h.append("<p class='fine'>Dit is een weermodelindicatie, geen lawineadvies. Lees het SLF-bulletin (slf.ch) en check liftstatus en openingen bij de bergbaan.</p>")
    h.append("</main></body></html>")
    Path(out_path).write_text("".join(h), encoding="utf-8")


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resorts", default=str(Path(__file__).with_name("resorts.csv")))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tier", type=int, default=2, help="1=grote freeride-gebieden, 2=+middelgrote, 3=alles")
    ap.add_argument("--out", default="report.html")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--threshold", type=float, default=20.0, help="cm in de horizon om te melden")
    ap.add_argument("--horizon", type=int, default=72, help="uren vooruit voor de sneeuwmelding")
    ap.add_argument("--ntfy-server", default=os.environ.get("NTFY_SERVER", "https://ntfy.sh"))
    ap.add_argument("--ntfy-topic", default=os.environ.get("NTFY_TOPIC"))
    ap.add_argument("--report-url", default=os.environ.get("REPORT_URL"), help="link naar het rapport (klikbaar in de melding)")
    ap.add_argument("--attach", action="store_true", help="stuur het HTML-rapport mee via ntfy")
    ap.add_argument("--always", action="store_true", help="altijd melden, ook als er niets nieuws is")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--demo", action="store_true", help="offline testdata, geen netwerk")
    a = ap.parse_args()

    resorts = load_resorts(a.resorts, a.tier)
    print(f"{len(resorts)} gebieden (tier <= {a.tier})")
    models = MODELS if a.demo else probe_models(MODELS)
    if not models:
        sys.exit("Geen enkel weermodel bereikbaar. Controleer je internetverbinding.")
    print("Modellen:", ", ".join(models))
    raw = fetch_all(resorts, a.days, models, demo=a.demo)
    results = [analyse(r, rw, models, a.days) for r, rw in zip(resorts, raw)]
    attach_ahead(results, a.horizon)
    top, best = summarize(results, a.horizon)
    render(results, top, best, models, a.horizon, a.out, a.days)
    print("Rapport:", a.out)

    peak = top[0]["ahead"][0] if top else 0
    if peak < a.threshold and not a.always:
        print(f"Geen melding: max {peak:.0f} cm in {a.horizon} u (drempel {a.threshold:.0f} cm).")
        return
    key = hashlib.md5(f"{top[0]['r']['name']}|{int(peak // 10)}|"
                      f"{best[0][2]['date'] if best else ''}|{best[0][1]['r']['name'] if best else ''}".encode()).hexdigest()
    state = json.loads(Path(a.state).read_text()) if Path(a.state).exists() else {}
    if state.get("key") == key and not a.always:
        print("Zelfde prognose als de vorige melding, niets verstuurd.")
        return
    title = f"Sneeuw op komst: {top[0]['r']['name']} +{peak:.0f} cm"
    body = build_message(top, best, a.horizon, a.days)
    print("\n" + title + "\n" + body)
    if a.no_notify:
        return
    sent = False
    if a.ntfy_topic:
        send_ntfy(a.ntfy_server, a.ntfy_topic, title, body, a.report_url, a.out if a.attach else None)
        sent = True
    if os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_CHAT"):
        send_telegram(os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT"], title, body)
        sent = True
    if sent:
        Path(a.state).write_text(json.dumps({"key": key, "sent": now_local().isoformat()}))
        print("Melding verstuurd.")
    else:
        print("Geen ntfy-topic of Telegram ingesteld, melding niet verstuurd.")


if __name__ == "__main__":
    main()
