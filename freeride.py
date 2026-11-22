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
HOURLY = ["temperature_2m", "precipitation", "snowfall", "freezing_level_height", "wind_speed_10m",
          "wind_gusts_10m", "wind_direction_10m", "cloud_cover", "snow_depth"]
BASE_HOURLY = ["temperature_2m", "precipitation"]  # tweede ronde, op de hoogte van het dal


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
            api_get({"latitude": 46.1, "longitude": 7.93, "hourly": "precipitation",
                     "models": m, "forecast_days": 1, "timezone": TZ}, tries=1)
            ok.append(m)
        except Exception as e:
            print(f"  model {m} niet beschikbaar, overgeslagen ({e})", file=sys.stderr)
    return ok


def fetch_all(resorts, days, models, demo=False, elev="top_m", hourly=None, batch=10):
    hourly = hourly or HOURLY
    out = []
    for i in range(0, len(resorts), batch):
        b = resorts[i:i + batch]
        if demo:
            data = mock_batch(b, days, models, hourly, elev)
        else:
            data = api_get({
                "latitude": ",".join(f"{r['lat']:.4f}" for r in b),
                "longitude": ",".join(f"{r['lon']:.4f}" for r in b),
                "elevation": ",".join(str(r[elev]) for r in b),
                "hourly": ",".join(hourly), "models": ",".join(models),
                "past_days": 2, "forecast_days": days, "timezone": TZ,
                "wind_speed_unit": "kmh",
            })
            if isinstance(data, dict):
                data = [data]
            time.sleep(1)
        out.extend(data)
    return out


def mock_batch(batch, days, models, hourly_vars, elev):
    # Synthetische data in exact het Open-Meteo-formaat (om offline te testen).
    start = datetime.combine(now_local().date(), datetime.min.time()) - timedelta(days=2)
    n = (days + 2) * 24
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    res = []
    for r in batch:
        rnd = random.Random(int(r["lat"] * 1000 + r["lon"] * 100))  # zelfde weer voor boven en dal
        west = max(0.15, 1.3 - (r["lon"] - 6.3) / 2.5)
        alt = (r["top_m"] - 1500) / 1500
        e = r[elev]
        hourly = {"time": times}
        for mi, m in enumerate(models):
            bias = 1 + 0.2 * (mi - (len(models) - 1) / 2)
            cols = {v: [] for v in HOURLY}
            depth = 0.2
            for i in range(n):
                d = i / 24
                p = math.exp(-((d - 2.6) / 0.6) ** 2) + 0.7 * math.exp(-((d - 5.9) / 0.5) ** 2)
                diurn = 2.5 * math.sin((i % 24 - 9) / 24 * 2 * math.pi)
                temp = -4.5 - 6.5 * (e - 2000) / 1000 + 3 * p + diurn
                precip = max(0, 2.2 * p * west * (0.5 + alt) * bias * (0.7 + 0.6 * rnd.random()))  # mm per uur
                depth += precip / 100 if temp < 1 else 0
                cols["temperature_2m"].append(round(temp, 1))
                cols["precipitation"].append(round(precip, 2))
                cols["snowfall"].append(round(precip * 0.7 if temp < 1.5 else 0.0, 2))
                cols["freezing_level_height"].append(round(1900 - 850 * p + rnd.uniform(-60, 60)))
                cols["wind_speed_10m"].append(round(12 + 35 * p + rnd.uniform(0, 6), 1))
                cols["wind_gusts_10m"].append(round(22 + 60 * p + rnd.uniform(0, 8), 1))
                cols["wind_direction_10m"].append(round(255 + 20 * p + rnd.uniform(-10, 10)))
                cols["cloud_cover"].append(round(min(100, 25 + 90 * min(1, p * 1.4) + rnd.uniform(0, 8))))
                cols["snow_depth"].append(round(depth, 2))
            for v in hourly_vars:
                hourly[f"{v}_{m}" if len(models) > 1 else v] = cols[v]
        res.append({"hourly": hourly})
    return res


# ---------------------------------------------------------------- analyse
def avg(x):
    return sum(x) / len(x) if x else None


def stat(per_model):
    # (mediaan, laagste, hoogste, lijst per model). Een model dat meer dan 2,5x de mediaan geeft
    # telt niet mee voor het bereik, zodat een uitschieter de cijfers niet vertekent.
    if not per_model:
        return (0.0, 0.0, 0.0, [])
    s = sorted(per_model)
    k = len(s)
    med = s[k // 2] if k % 2 else (s[k // 2 - 1] + s[k // 2]) / 2
    keep = [v for v in per_model if med <= 10 or v <= 2.5 * med] or per_model
    return (med, min(keep), max(keep), list(per_model))


def collect(h, var, models, names=None):
    lists = []
    for m in models:
        for key in (f"{var}_{m}", var):
            v = h.get(key)
            if v is not None and any(x is not None for x in v):
                lists.append(v)
                if names is not None:
                    names.append(m)
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


def ratio(t):
    # cm sneeuw per mm neerslag (1 mm water = ongeveer 1 cm sneeuw, droger bij kou, natter rond 0 graden).
    # Ter vergelijking: Open-Meteo zelf rekent met 0,7 cm per mm.
    return 1.3 if t <= -6 else 1.0 if t <= -2 else 0.8 if t <= 0.5 else 0.6


def snow_series(L, n):
    # Rekent neerslag om naar cm sneeuw (T <= 1 graad) op de hoogte waarvoor L is opgehaald.
    T = [avg([l[i] for l in L["temperature_2m"] if i < len(l) and l[i] is not None]) for i in range(n)]
    P = L["precipitation"]

    def one(l, i):
        t = T[i]
        return min(l[i] * ratio(t), 5.0) if (t is not None and t <= 1.0 and i < len(l) and l[i]) else 0.0

    def hour(i):
        return sum(one(l, i) for l in P) / len(P) if P else 0.0

    def sums(a, b):
        a, b = max(a, 0), min(b, n)
        return stat([sum(one(l, i) for i in range(a, b)) for l in P])

    def rain(a, b):
        a, b = max(a, 0), min(b, n)
        if not P:
            return 0.0
        return avg([sum(l[i] for i in range(a, min(b, len(l)))
                        if l[i] and T[i] is not None and T[i] > 1.0) for l in P])

    return T, hour, sums, rain


def analyse(resort, raw, raw_base, models, ndays):
    h = raw["hourly"]
    times = h["time"]
    n = len(times)
    names_P, names_S = [], []
    names_map = {"precipitation": names_P, "snowfall": names_S}
    L = {v: collect(h, v, models, names_map.get(v)) for v in HOURLY}
    T, hour, snow_sum, _ = snow_series(L, n)
    S = [hour(i) for i in range(n)]
    cold = [t is not None and t <= 1.0 for t in T]
    snow_sum_b = rain_b = None
    if raw_base:
        LB = {v: collect(raw_base["hourly"], v, models) for v in BASE_HOURLY}
        _, _, snow_sum_b, rain_b = snow_series(LB, n)

    def native(a, b):
        # Open-Meteo's eigen sneeuwcijfer per model (ter controle van mijn omrekening)
        a, b = max(a, 0), min(b, n)
        return [sum((l[i] or 0.0) for i in range(a, min(b, len(l)))) for l in L["snowfall"]]

    def vals(var, i):
        return [l[i] for l in L[var] if i < len(l) and l[i] is not None]

    def at(var, i):
        return avg(vals(var, i))

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
            "fresh_b": snow_sum_b(b - 48, b + 10) if snow_sum_b else None,
            "total_b": snow_sum_b(b, b + 24)[0] if snow_sum_b else None,
            "rain_b": rain_b(b + 8, b + 17) if rain_b else 0.0,
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
        if d["rain_b"] >= 3:
            d["flags"].append(f"onderaan regen (ca. {d['rain_b']:.0f} mm): natte, zware afdaling naar het dal")
        days.append(d)
    return {"r": resort, "days": days, "ahead": None, "ahead_b": None, "now_i": now_i, "models_P": names_P, "models_S": names_S, "native_fn": native,
            "snow_fn": snow_sum, "snow_fn_b": snow_sum_b}


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
        x["ahead_b"] = x["snow_fn_b"](a, a + hours) if x["snow_fn_b"] else None
        x["native"] = dict(zip(x["models_S"], x["native_fn"](a, a + hours)))
        x["snow_fn"] = x["snow_fn_b"] = x["native_fn"] = None


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
        r, a, ab = x["r"], x["ahead"], x["ahead_b"]
        d0 = x["days"][0] if x["days"] else None
        dal = f", onderaan ({r['base_m']} m) +{ab[0]:.0f} cm ({ab[1]:.0f}-{ab[2]:.0f})" if ab else ""
        extra = ""
        if d0:
            extra = f", sneeuwgrens ~{round(d0['fl_min'], -2):.0f} m, wind tot {d0['gust']:.0f} km/u"
            if d0["lee"]:
                extra += f", stuifsneeuw {'/'.join(d0['lee'])}"
        lines.append(f"- {r['name']} ({d0['status'] if d0 else '?'}): bovenaan ({r['top_m']} m) "
                     f"+{a[0]:.0f} cm ({a[1]:.0f}-{a[2]:.0f}){dal}{extra}")
    lines.append("")
    lines.append(f"Beste freeride-dagen (open of mogelijk open) in de komende {ndays} dagen:")
    for sc, x, d in best:
        dt = datetime.strptime(d["date"], "%Y-%m-%d")
        dag = ["ma", "di", "wo", "do", "vr", "za", "zo"][dt.weekday()]
        dal = f", dal {d['fresh_b'][0]:.0f} cm" if d["fresh_b"] else ""
        lines.append(f"- {dag} {dt.day}/{dt.month}: {x['r']['name']}, score {sc}/100, "
                     f"{d['fresh'][0]:.0f} cm vers boven{dal}, {d['status']}")
    if top and top[0]["ahead"][0] > 120:
        lines += ["", "Let op: zulke hoge cijfers kunnen een modelfout zijn. Kijk in het rapport bij Modelcontrole."]
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
BORDER = [tuple(float(v) for v in p.split(",")) for p in """
7.200,45.876 7.229,45.892 7.255,45.888 7.294,45.922 7.324,45.910 7.344,45.915 7.383,45.897 7.402,45.911
7.427,45.915 7.444,45.931 7.475,45.936 7.472,45.948 7.499,45.962 7.539,45.955 7.547,45.962 7.547,45.985
7.570,45.988 7.586,45.971 7.668,45.974 7.681,45.957 7.710,45.948 7.707,45.935 7.718,45.924 7.736,45.924
7.748,45.941 7.768,45.937 7.800,45.917 7.821,45.927 7.864,45.917 7.873,45.921 7.877,45.926 7.869,45.937
7.878,45.974 7.910,45.997 7.989,45.996 8.001,46.012 8.013,46.012 8.017,46.025 8.012,46.031 8.035,46.043
8.022,46.069 8.035,46.100 8.107,46.112 8.115,46.130 8.146,46.138 8.154,46.147 8.151,46.166 8.166,46.182
8.154,46.192 8.138,46.226 8.115,46.235 8.111,46.249 8.081,46.258 8.085,46.266 8.138,46.301 8.159,46.296
8.199,46.302 8.212,46.310 8.224,46.334 8.263,46.346 8.266,46.365 8.284,46.364 8.312,46.377 8.316,46.398
8.289,46.408 8.307,46.425 8.325,46.426 8.367,46.452 8.438,46.465 8.465,46.446 8.458,46.420 8.466,46.417
8.464,46.402 8.471,46.396 8.462,46.387 8.469,46.361 8.464,46.356 8.466,46.334 8.428,46.298 8.456,46.264
8.444,46.249 8.465,46.245 8.468,46.233 8.532,46.219 8.541,46.197 8.563,46.184 8.575,46.163 8.603,46.154
8.593,46.144 8.613,46.122 8.643,46.123 8.663,46.109 8.716,46.097 8.722,46.110 8.742,46.120 8.759,46.102
8.782,46.094 8.807,46.100 8.856,46.073 8.826,46.029 8.792,46.005 8.788,45.987 8.814,45.988 8.841,45.981
8.877,45.958 8.892,45.959 8.895,45.929 8.917,45.914 8.941,45.869 8.911,45.833 8.950,45.843 8.973,45.834
8.987,45.836 8.994,45.820 9.018,45.819 9.089,45.899 9.042,45.927 9.020,45.928 9.015,45.960 8.988,45.971
9.027,45.993 9.009,46.037 9.019,46.043 9.017,46.050 9.040,46.059 9.078,46.065 9.090,46.086 9.071,46.117
9.120,46.134 9.160,46.170 9.195,46.179 9.202,46.208 9.213,46.211 9.221,46.228 9.248,46.232 9.260,46.277
9.283,46.296 9.286,46.315 9.295,46.316 9.299,46.327 9.292,46.336 9.299,46.342 9.295,46.356 9.283,46.358
9.276,46.371 9.281,46.413 9.249,46.431 9.248,46.447 9.279,46.461 9.274,46.482 9.282,46.497 9.363,46.509
9.374,46.504 9.368,46.494 9.373,46.488 9.411,46.467 9.425,46.477 9.434,46.498 9.464,46.508 9.461,46.485
9.449,46.484 9.462,46.480 9.466,46.470 9.460,46.464 9.455,46.417 9.469,46.388 9.461,46.376 9.496,46.364
9.529,46.316 9.551,46.302 9.558,46.307 9.620,46.288 9.637,46.286 9.679,46.301 9.714,46.293 9.725,46.310
9.726,46.320 9.717,46.323 9.727,46.332 9.724,46.341 9.737,46.351 9.778,46.334 9.833,46.361 9.868,46.363
9.907,46.381 9.925,46.366 9.931,46.374 9.958,46.378 9.971,46.359 9.996,46.351 9.980,46.323 10.001,46.314
10.001,46.303 9.992,46.296 9.997,46.286 10.054,46.266 10.061,46.248 10.044,46.230 10.070,46.217 10.088,46.227
10.123,46.224 10.146,46.230 10.177,46.258 10.152,46.294 10.117,46.314 10.105,46.333 10.110,46.353
10.129,46.361 10.128,46.377 10.163,46.388 10.166,46.408 10.158,46.416 10.149,46.413 10.143,46.428
10.129,46.432 10.079,46.421 10.039,46.447 10.053,46.460 10.043,46.481 10.049,46.501 10.043,46.510
10.052,46.514 10.054,46.531 10.046,46.544 10.062,46.546 10.085,46.566 10.079,46.575 10.096,46.578
10.103,46.611 10.127,46.605 10.190,46.626 10.215,46.617 10.240,46.635 10.258,46.610 10.242,46.589
10.247,46.574 10.287,46.570 10.297,46.550 10.326,46.552 10.337,46.543 10.351,46.556 10.397,46.544
10.418,46.551 10.453,46.530 10.471,46.542 10.492,46.615 10.449,46.641 10.400,46.638 10.382,46.684
10.403,46.705 10.415,46.706 10.418,46.719 10.401,46.734 10.441,46.752 10.439,46.775 10.423,46.788
10.448,46.801 10.458,46.816 10.457,46.830 10.466,46.835 10.472,46.849 10.465,46.884 10.486,46.916
10.489,46.939 10.422,46.961 10.427,46.975 10.398,46.997 10.353,46.991 10.326,46.953 10.309,46.950
10.307,46.939 10.317,46.925 10.297,46.919 10.241,46.932 10.242,46.919 10.226,46.897 10.234,46.885
10.232,46.867 10.193,46.867 10.169,46.851 10.106,46.841 10.087,46.862 10.050,46.864 10.052,46.876
10.018,46.901 9.991,46.902 9.977,46.916 9.936,46.914 9.878,46.935 9.878,46.957 9.872,46.964 9.893,46.990
9.871,47.013 9.876,47.021 9.860,47.023 9.838,47.013 9.784,47.039 9.718,47.043 9.681,47.062 9.626,47.052
9.603,47.061 9.557,47.049 9.539,47.065 9.476,47.052 9.472,47.064 9.478,47.070 9.517,47.087 9.511,47.135
9.489,47.169 9.487,47.186 9.496,47.208 9.525,47.248 9.531,47.269 9.550,47.281 9.566,47.304 9.584,47.312
9.601,47.347 9.625,47.366 9.657,47.368 9.673,47.380 9.675,47.392 9.653,47.405 9.646,47.436 9.658,47.453
9.625,47.458 9.608,47.470 9.596,47.463 9.582,47.483 9.561,47.497 9.553,47.536 9.514,47.539 9.443,47.593
9.256,47.658 9.025,47.687 8.941,47.656 8.897,47.648 8.875,47.655 8.873,47.670 8.851,47.681 8.858,47.692
8.852,47.698 8.873,47.694 8.873,47.704 8.819,47.713 8.826,47.718 8.805,47.725 8.812,47.730 8.807,47.738
8.797,47.734 8.799,47.727 8.770,47.719 8.770,47.707 8.791,47.705 8.810,47.693 8.797,47.688 8.796,47.675
8.786,47.676 8.728,47.693 8.736,47.716 8.711,47.730 8.724,47.746 8.742,47.748 8.727,47.762 8.714,47.765
8.692,47.757 8.681,47.787 8.657,47.792 8.657,47.801 8.646,47.787 8.653,47.773 8.628,47.759 8.619,47.768
8.619,47.788 8.622,47.795 8.614,47.801 8.575,47.800 8.568,47.808 8.562,47.793 8.578,47.782 8.563,47.778
8.553,47.784 8.520,47.770 8.510,47.776 8.484,47.771 8.451,47.741 8.455,47.722 8.445,47.723 8.405,47.698
8.421,47.684 8.406,47.676 8.412,47.666 8.445,47.654 8.466,47.657 8.464,47.648 8.473,47.638 8.476,47.650
8.492,47.642 8.532,47.646 8.527,47.661 8.542,47.657 8.540,47.665 8.561,47.670 8.577,47.661 8.606,47.672
8.629,47.652 8.619,47.639 8.603,47.640 8.614,47.643 8.608,47.652 8.596,47.643 8.605,47.615 8.582,47.596
8.563,47.599 8.574,47.612 8.558,47.625 8.516,47.634 8.508,47.620 8.457,47.602 8.467,47.584 8.489,47.588
8.495,47.581 8.440,47.568 8.396,47.577 8.386,47.565 8.328,47.571 8.297,47.590 8.297,47.606 8.262,47.614
8.226,47.605 8.210,47.622 8.170,47.597 8.138,47.590 8.136,47.583 8.112,47.584 8.089,47.557 8.070,47.564
8.021,47.550 7.960,47.558 7.947,47.544 7.916,47.548 7.907,47.576 7.892,47.588 7.840,47.582 7.822,47.588
7.795,47.557 7.696,47.533 7.666,47.537 7.634,47.561 7.677,47.564 7.683,47.571 7.686,47.566 7.689,47.572
7.672,47.587 7.694,47.601 7.675,47.592 7.646,47.597 7.619,47.577 7.589,47.590 7.585,47.575 7.566,47.577
7.557,47.565 7.499,47.540 7.502,47.528 7.519,47.535 7.531,47.527 7.522,47.514 7.498,47.521 7.511,47.497
7.488,47.482 7.472,47.481 7.435,47.498 7.421,47.480 7.457,47.471 7.402,47.436 7.303,47.439 7.248,47.420
7.229,47.440 7.197,47.436 7.171,47.444 7.177,47.468 7.202,47.495 7.162,47.490 7.128,47.504 7.075,47.488
7.023,47.503 6.984,47.492 7.002,47.454 6.969,47.446 6.966,47.436 6.941,47.433 6.938,47.406 6.914,47.405
6.913,47.388 6.884,47.373 6.880,47.354 6.975,47.360 7.012,47.373 7.049,47.362 7.050,47.348 7.063,47.345
7.057,47.335 7.045,47.327 7.010,47.325 7.016,47.316 6.995,47.296 6.941,47.286 6.951,47.268 6.948,47.250
6.955,47.243 6.881,47.201 6.874,47.185 6.841,47.171 6.859,47.165 6.850,47.157 6.806,47.131 6.740,47.110
6.747,47.099 6.741,47.091 6.703,47.082 6.706,47.074 6.691,47.067 6.720,47.052 6.709,47.045 6.659,47.027
6.638,47.002 6.618,46.991 6.593,46.992 6.505,46.966 6.496,46.974 6.434,46.928 6.464,46.891 6.460,46.851
6.444,46.834 6.441,46.816 6.431,46.812 6.435,46.801 6.458,46.789 6.452,46.774 6.423,46.754 6.393,46.748
6.385,46.731 6.282,46.691 6.178,46.615 6.110,46.576 6.157,46.545 6.144,46.527 6.138,46.531 6.113,46.510
6.097,46.481 6.073,46.466 6.085,46.441 6.064,46.416 6.097,46.410 6.150,46.377 6.160,46.380 6.171,46.367
6.138,46.338 6.120,46.310 6.120,46.294 6.103,46.285 6.120,46.265 6.121,46.248 6.102,46.238 6.088,46.247
6.063,46.246 6.046,46.231 6.034,46.239 5.990,46.222 5.993,46.215 5.972,46.213 5.974,46.202 5.964,46.197
5.995,46.183 5.957,46.129 5.994,46.144 6.036,46.135 6.052,46.151 6.092,46.152 6.098,46.144 6.136,46.142
6.189,46.167 6.188,46.181 6.205,46.192 6.296,46.225 6.311,46.244 6.310,46.256 6.295,46.256 6.297,46.264
6.266,46.248 6.260,46.252 6.237,46.277 6.253,46.291 6.247,46.303 6.220,46.312 6.253,46.361 6.335,46.404
6.425,46.416 6.519,46.456 6.681,46.454 6.821,46.427 6.802,46.390 6.806,46.380 6.771,46.361 6.771,46.352
6.784,46.333 6.796,46.333 6.800,46.319 6.820,46.316 6.831,46.300 6.859,46.290 6.865,46.280 6.854,46.255
6.821,46.231 6.803,46.204 6.813,46.181 6.791,46.163 6.798,46.136 6.815,46.129 6.900,46.124 6.883,46.096
6.890,46.074 6.880,46.070 6.872,46.051 6.889,46.042 6.924,46.066 6.938,46.063 6.935,46.058 6.950,46.052
6.963,46.031 6.981,46.019 6.985,46.006 7.011,45.996 7.023,45.979 7.009,45.968 7.037,45.955 7.035,45.938
7.045,45.922 7.095,45.876 7.102,45.859 7.118,45.859 7.154,45.879 7.191,45.859 7.201,45.863 7.200,45.876
""".split()]
def _roads(enc):
    return [[tuple(float(v) for v in q.split(",")) for q in seg.split()] for seg in enc.split(";")]


ROADS_MAJOR = _roads("""
6.868,47.590 6.851,47.507 6.833,47.480 6.803,47.459 6.768,47.448 6.611,47.427 6.518,47.422 6.463,47.404
6.383,47.404 6.338,47.371 6.296,47.358 6.173,47.341 6.010,47.301 5.970,47.286 5.914,47.257 5.872,47.220
5.827,47.196 5.780,47.188 5.721,47.185;8.215,47.451 8.138,47.468 8.065,47.527 8.012,47.552 7.958,47.559
7.844,47.562 7.789,47.578;8.215,47.451 8.154,47.427 8.124,47.389 7.979,47.332 7.910,47.313;9.080,47.477
9.032,47.466 8.986,47.467 8.859,47.526 8.785,47.540 8.726,47.542 8.695,47.535 8.680,47.521 8.680,47.513
8.704,47.487 8.711,47.470 8.697,47.441 8.681,47.426 8.640,47.408 8.539,47.389 8.476,47.391 8.312,47.450
8.252,47.453 8.215,47.451;8.610,46.939 8.573,46.951 8.438,46.981 8.400,46.981 8.352,46.970 8.322,46.973
8.299,46.988 8.295,47.014;7.899,47.310 7.919,47.287 7.942,47.246 7.977,47.211 8.003,47.196 8.039,47.185
8.077,47.183 8.131,47.190 8.144,47.187 8.195,47.148 8.267,47.112 8.288,47.091 8.291,47.066;8.594,46.566
8.614,46.552 8.634,46.546 8.698,46.538 8.737,46.526 8.839,46.448 8.862,46.419 8.883,46.376 8.957,46.331
8.986,46.299 8.995,46.279 9.012,46.214;9.012,46.222 8.999,46.193 8.966,46.170 8.945,46.146 8.931,46.104
8.926,46.065 8.927,46.026 8.935,45.987 8.958,45.944 8.994,45.915 8.998,45.905 8.991,45.880 8.963,45.870
8.977,45.859 9.033,45.845 9.047,45.801 9.045,45.767 9.010,45.703;6.311,46.107 6.331,46.138 6.332,46.152
6.326,46.160 6.292,46.180 6.220,46.195 6.182,46.195 6.145,46.188 6.130,46.178 6.110,46.149
6.091,46.138;6.859,45.915 6.849,45.928 6.805,45.924 6.731,45.938 6.681,45.938 6.660,45.944 6.643,45.968
6.632,46.015 6.623,46.032 6.597,46.057 6.571,46.070 6.406,46.101 6.365,46.103 6.304,46.097;6.818,46.508
6.870,46.485 6.895,46.459 6.950,46.328 6.986,46.268 7.074,46.168 7.103,46.143;7.103,46.143 7.150,46.140
7.187,46.149 7.321,46.217 7.579,46.284;5.944,45.721 5.948,45.749 5.956,45.767 5.983,45.802 6.121,45.931
6.126,45.979 6.135,45.999 6.148,46.004 6.207,46.013 6.291,46.050 6.298,46.057 6.302,46.089
6.311,46.107;7.408,46.938 7.320,46.899 7.204,46.869 7.163,46.845 7.130,46.811 7.105,46.772 7.070,46.675
7.058,46.653 7.038,46.639 6.980,46.629 6.947,46.612 6.919,46.577 6.899,46.537 6.879,46.472;6.133,46.175
6.125,46.231 6.136,46.269 6.251,46.430 6.294,46.465 6.328,46.478 6.398,46.492 6.487,46.537 6.565,46.552
6.610,46.555 6.677,46.549 6.818,46.508;6.092,46.143 6.040,46.147 6.014,46.144 5.985,46.124 5.936,46.106
5.910,46.073 5.893,46.066 5.880,46.071 5.863,46.087 5.829,46.143 5.809,46.162 5.762,46.177
5.702,46.176;7.899,47.310 7.818,47.302 7.743,47.304 7.676,47.270 7.525,47.216 7.502,47.194
7.496,47.164;8.767,47.536 8.911,47.585;8.468,47.393 8.442,47.377 8.435,47.363 8.433,47.348 8.437,47.311
8.474,47.205;8.605,47.261 8.592,47.243 8.556,47.216 8.535,47.208 8.458,47.195 8.443,47.182 8.434,47.164
8.434,47.144 8.440,47.125 8.453,47.108 8.488,47.079 8.565,47.064 8.590,47.052 8.600,47.036
8.602,47.015;7.694,45.702 7.686,45.732 7.665,45.755 7.624,45.769 7.572,45.772 7.370,45.748 7.230,45.706
7.179,45.703 7.041,45.729 7.028,45.740 6.997,45.790 6.982,45.798 6.934,45.810;8.446,45.903 8.515,45.875
8.536,45.850 8.539,45.818 8.526,45.755 8.529,45.738 8.550,45.714 8.573,45.703 8.602,45.701 8.657,45.722
8.674,45.724 8.733,45.705;8.537,45.726 8.506,45.701;7.496,47.164 7.497,47.137 7.486,47.065 7.455,47.025
7.462,46.997 7.448,46.975 7.408,46.938;7.405,46.940 7.384,46.952 7.362,46.957 7.233,46.970 7.205,46.968
7.155,46.950 7.099,46.910 7.014,46.901 6.927,46.865 6.876,46.836 6.820,46.820 6.757,46.780 6.610,46.753
6.587,46.740 6.575,46.728;6.575,46.728 6.567,46.702 6.567,46.673 6.586,46.554;7.632,47.579 7.646,47.562
7.669,47.546 7.706,47.537 7.745,47.538 7.828,47.561;8.516,47.382 8.535,47.328 8.565,47.289 8.596,47.263
8.667,47.223 8.722,47.203 8.777,47.192 8.828,47.192 8.902,47.204 8.953,47.203 8.993,47.186 9.044,47.140
9.082,47.123;7.501,47.183 7.460,47.168 7.364,47.158;7.032,47.046 6.888,46.998 6.778,46.914 6.655,46.843
6.634,46.823 6.625,46.807 6.625,46.788 6.634,46.759;7.221,47.214 7.214,47.198 7.227,47.187 7.268,47.177
7.402,47.161;7.080,47.444 7.164,47.394 7.200,47.380 7.230,47.373 7.358,47.363 7.396,47.345;7.841,46.684
7.815,46.659 7.782,46.649 7.745,46.652 7.677,46.678 7.647,46.701 7.635,46.717 7.593,46.839 7.553,46.894
7.536,46.907 7.497,46.925 7.445,46.937 7.408,46.938;7.754,47.537 7.828,47.458 7.843,47.421 7.849,47.375
7.899,47.310;7.364,47.158 7.218,47.141;7.396,47.345 7.400,47.331 7.397,47.317 7.368,47.278 7.334,47.260
7.267,47.239 7.220,47.215;8.613,47.416 8.786,47.362 8.799,47.351 8.830,47.302;8.589,46.938 8.627,46.879
8.639,46.737 8.627,46.705 8.596,46.668;8.596,46.668 8.579,46.642 8.575,46.623 8.581,46.586
8.594,46.566;8.602,47.015 8.599,46.973 8.605,46.915
""")
ROADS_SEC = _roads("""
8.293,47.070 8.301,47.012;7.579,46.284 7.878,46.304 7.952,46.320 7.956,46.313;6.934,45.810 6.905,45.828
6.894,45.840 6.867,45.901 6.850,45.922;7.957,46.315 7.998,46.293 8.045,46.222 8.085,46.192 8.140,46.184
8.239,46.196 8.271,46.188 8.284,46.174 8.290,46.153 8.283,46.075 8.289,46.035 8.297,46.016 8.309,46.002
8.334,45.991 8.414,45.986 8.439,45.975 8.446,45.959 8.440,45.913 8.447,45.903 8.459,45.898;7.910,47.313
7.899,47.310;9.098,46.212 9.032,46.216;8.683,47.536 8.690,47.592;8.060,47.532 8.136,47.581 8.177,47.597
8.234,47.597 8.312,47.561 8.466,47.561 8.513,47.543 8.533,47.521 8.575,47.446 8.601,47.425
8.617,47.422;7.103,46.143 7.118,46.113 7.142,46.088 7.176,46.036 7.215,45.997 7.229,45.970 7.228,45.942
7.197,45.894 7.192,45.875;7.192,45.875 7.179,45.842 7.178,45.825 7.218,45.822 7.285,45.803 7.309,45.785
7.316,45.773 7.321,45.744;6.404,46.727 6.587,46.739;6.409,46.722 6.356,46.934 6.354,46.973 6.368,47.070
6.362,47.098 6.348,47.123 6.328,47.143 6.285,47.169 6.079,47.235 6.009,47.270 5.983,47.292;5.989,47.297
6.044,47.361 6.111,47.467 6.138,47.538 6.129,47.597;8.830,47.302 8.841,47.275 8.839,47.258 8.816,47.229
8.777,47.197;8.434,47.165 8.420,47.165 8.394,47.155 8.311,47.099 8.301,47.087 8.295,47.067;8.874,45.785
8.967,45.800 9.060,45.801;7.960,46.321 8.040,46.373 8.140,46.422 8.271,46.514 8.293,46.525 8.363,46.544
8.485,46.601 8.564,46.627;8.564,46.627 8.798,46.679 8.829,46.693 8.884,46.733 8.920,46.748;6.869,47.591
6.843,47.595 6.765,47.590 6.741,47.594;7.658,47.545 7.624,47.515 7.609,47.495 7.611,47.477
7.632,47.456;8.297,47.067 8.198,47.048 8.160,47.048 8.125,47.056;6.943,45.728 6.984,45.746
7.028,45.750;6.863,47.591 6.913,47.581 6.932,47.570 6.993,47.492;8.289,47.037 8.303,47.039 8.337,47.062
8.369,47.075 8.488,47.079;5.717,46.836 5.748,46.831 5.770,46.819 5.883,46.786 5.909,46.772 5.929,46.741
5.931,46.702 5.951,46.652 5.987,46.616 5.980,46.577 6.034,46.569 6.047,46.559 6.032,46.507 6.033,46.498
6.040,46.491;6.039,46.491 6.092,46.493 6.105,46.474 6.101,46.455 6.058,46.399 6.058,46.356 6.068,46.317
6.080,46.303 6.097,46.294 6.144,46.287;6.082,47.321 6.057,47.247 5.986,47.180 5.882,47.014 5.754,46.904
5.718,46.847 5.711,46.828;8.818,45.821 8.789,45.778 8.785,45.743 8.789,45.707;8.299,47.010 8.264,46.908
8.241,46.872 8.204,46.836 8.180,46.790 8.167,46.784 8.112,46.778 8.096,46.769 8.067,46.735 7.999,46.699
7.950,46.681 7.925,46.677 7.849,46.680;8.395,47.419 8.409,47.432 8.427,47.439 8.478,47.438 8.515,47.428
8.532,47.419 8.540,47.407 8.540,47.389
""")
CITIES = [("Genève", 6.14, 46.20), ("Lausanne", 6.63, 46.52), ("Bern", 7.45, 46.95), ("Sion", 7.36, 46.23),
          ("Luzern", 8.31, 47.05), ("Zürich", 8.54, 47.37), ("Montreux", 6.91, 46.43), ("Martigny", 7.07, 46.10),
          ("Brig", 7.99, 46.32), ("Interlaken", 7.86, 46.69), ("Fribourg", 7.15, 46.80)]
LON0, LON1, LAT0, LAT1 = 5.8, 9.0, 45.8, 47.5
STEPS = [(0.5, "h1"), (2, "h2"), (5, "h3"), (10, "h4"), (20, "h5"), (30, "h6"), (50, "h7"), (80, "h8")]
LEGEND = [("0,5", "h1"), ("2", "h2"), ("5", "h3"), ("10", "h4"), ("20", "h5"), ("30", "h6"), ("50", "h7"), ("80+", "h8")]


def klass(v):
    c = None
    for th, k in STEPS:
        if v >= th:
            c = k
    return c


def heat(pts, W, H, cols=48):
    # Interpolatie (IDW) van de waarden per gebied, enkel binnen ~22 km van een gebied.
    rows = max(1, round(cols * H / W))
    cw, ch = W / cols, H / rows
    paths = {}
    for r in range(rows):
        lat = LAT1 - (r + .5) / rows * (LAT1 - LAT0)
        for c in range(cols):
            lon = LON0 + (c + .5) / cols * (LON1 - LON0)
            num = den = 0.0
            dmin = 1e9
            for plon, plat, v in pts:
                d = math.hypot((lon - plon) * 76, (lat - plat) * 111)
                dmin = min(dmin, d)
                w = 1 / (d * d + 9)
                num += w * v
                den += w
            if dmin > 22 or den == 0:
                continue
            k = klass(num / den)
            if k:
                paths.setdefault(k, []).append(f"M{c * cw:.1f} {r * ch:.1f}h{cw + .3:.1f}v{ch + .3:.1f}h-{cw + .3:.1f}z")
    return "".join(f'<path class="{k}" d="{"".join(v)}"/>' for k, v in paths.items())


def svg_map(results, di, W=420):
    H = int(W * (LAT1 - LAT0) * 111 / ((LON1 - LON0) * 76))
    px = lambda lon: (lon - LON0) / (LON1 - LON0) * W
    py = lambda lat: (LAT1 - lat) / (LAT1 - LAT0) * H
    border = "M" + "L".join(f"{px(lo):.1f} {py(la):.1f}" for lo, la in BORDER) + "Z"
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="sneeuwkaart">']
    for lon in (6, 7, 8, 9):
        out.append(f'<line x1="{px(lon):.1f}" y1="0" x2="{px(lon):.1f}" y2="{H}" class="ch"/>'
                   f'<text x="{px(lon) + 2:.1f}" y="{H - 3}" class="gl">{lon}°O</text>')
    for lat in (46, 47):
        out.append(f'<line x1="0" y1="{py(lat):.1f}" x2="{W}" y2="{py(lat):.1f}" class="ch"/>'
                   f'<text x="2" y="{py(lat) - 2:.1f}" class="gl">{lat}°N</text>')
    for layer, key in (("top", "total"), ("base", "total_b")):
        rows = []
        for x in results:
            if di < len(x["days"]):
                v = x["days"][di][key]
                v = v[0] if isinstance(v, tuple) else v
                if v is not None:
                    rows.append((v, x))
        out.append(f'<g class="L {layer}">' + heat([(x["r"]["lon"], x["r"]["lat"], v) for v, x in rows], W, H))
        for v, x in rows:
            r = x["r"]
            cls = "dot" if x["days"][di]["status"] != "dicht" else "dot closed"
            out.append(f'<circle cx="{px(r["lon"]):.1f}" cy="{py(r["lat"]):.1f}" r="2.6" class="{cls}">'
                       f'<title>{escape(r["name"])}: {v:.0f} cm</title></circle>')
            if r["tier"] == 1 and v >= 3:
                out.append(f'<text x="{px(r["lon"]) + 4:.1f}" y="{py(r["lat"]) - 3:.1f}" class="vl">{v:.0f}</text>')
        for k, (v, x) in enumerate(sorted(rows, key=lambda t: -t[0])[:2]):
            if v >= 5:
                r = x["r"]
                out.append(f'<text x="{px(r["lon"]) + 9:.1f}" y="{py(r["lat"]) + 12 + 11 * k:.1f}" class="lb">'
                           f'{escape(r["name"].split(" (")[0])} {v:.0f} cm</text>')
        out.append("</g>")
    def road_d(lines):
        return "".join("M" + "L".join(f"{px(a):.1f} {py(b):.1f}" for a, b in ln) for ln in lines)
    out.append(f'<path d="{road_d(ROADS_SEC)}" class="rs"/><path d="{road_d(ROADS_MAJOR)}" class="rm"/>')
    out.append(f'<path d="{border}" class="bd"/>')
    for nm, lo, la in CITIES:
        out.append(f'<rect x="{px(lo) - 2:.1f}" y="{py(la) - 2:.1f}" width="4" height="4" class="city"/>'
                   f'<text x="{px(lo) + 5:.1f}" y="{py(la) + 3:.1f}" class="gl">{nm}</text>')
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
.maps{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}.maps h3{font-size:.9rem;margin:0 0 2px}
.maps:not(.show-base) .base,.maps.show-base .top{display:none}
svg{width:100%;height:auto;background:var(--bg);border-radius:8px}.ch{stroke:var(--ln);stroke-width:1;stroke-dasharray:3 3}
.h1{fill:#cfe6ff}.h2{fill:#9fcbff}.h3{fill:#6fb0ff}.h4{fill:#3f8bf0}.h5{fill:#2f5fd0}.h6{fill:#6a3fc8}.h7{fill:#a02fb5}.h8{fill:#d6336c}
svg path[class^="h"]{fill-opacity:.85}.bd{fill:none;stroke:var(--tx);stroke-width:1.3}.rm{fill:none;stroke:#c0392b;stroke-width:1.6;opacity:.9}.rs{fill:none;stroke:var(--mu);stroke-width:.9;opacity:.8}
.dot{fill:var(--card);stroke:var(--tx);stroke-width:1}.dot.closed{fill:none;stroke-dasharray:2 1.5;opacity:.7}
.gl{fill:var(--mu);font-size:9px}.city{fill:var(--mu)}.vl{fill:var(--tx);font-size:9px;font-weight:600}
.lb{fill:var(--tx);font-size:11px;font-weight:700;paint-order:stroke;stroke:var(--card);stroke-width:3px}
.tg button{border:1px solid var(--ln);background:var(--card);color:var(--tx);padding:6px 12px;border-radius:8px;font:inherit;margin-right:6px}
.tg button.on{background:var(--ac);color:#fff;border-color:var(--ac)}
.lg{display:flex;gap:0;margin:8px 0;font-size:.75rem}.lg span{padding:2px 8px;color:#fff;text-shadow:0 0 2px #000}
ul{margin:.3em 0;padding-left:1.2em}
"""


def fmt(st):
    return "-" if not st else f"+{st[0]:.0f} cm ({st[1]:.0f}-{st[2]:.0f})"


def render(results, top, best, models, hours, out_path, ndays):
    gen = now_local().strftime("%d/%m/%Y %H:%M")
    dates = [d["date"] for d in results[0]["days"]]
    wd = ["ma", "di", "wo", "do", "vr", "za", "zo"]
    lab = [f"{wd[datetime.strptime(x, '%Y-%m-%d').weekday()]} {x[8:]}/{x[5:7]}" for x in dates]
    h = [f"<!DOCTYPE html><html lang='nl'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>Freeride-prognose Magic Pass</title><style>{CSS}</style></head><body><main>",
         f"<h1>Freeride-prognose Magic Pass</h1><p class='fine'>Gegenereerd {gen}. Modellen: {escape(', '.join(models))}. "
         f"Gemiddelde van de modellen; de spreiding staat tussen haakjes of in de tooltips.</p>"]
    h.append("<section class='card'><h2>Beste dagen en gebieden (open of mogelijk open)</h2><ul>")
    for sc, x, d in best:
        i = dates.index(d["date"])
        dal = f", {d['fresh_b'][0]:.0f} cm vers in het dal" if d["fresh_b"] else ""
        h.append(f"<li><b>{lab[i]}: {escape(x['r']['name'])}</b> (score {sc}/100, {d['fresh'][0]:.0f} cm vers bovenaan{dal}, {d['status']})"
                 + "".join(f"<br><span class='fine'>{escape(f)}</span>" for f in d["flags"]) + "</li>")
    if not best:
        h.append("<li>Geen enkel gebied is open of mogelijk open in deze periode.</li>")
    h.append("</ul></section>")
    h.append(f"<section class='card'><h2>Sneeuw op komst, komende {hours} uur</h2><div class='wrap'><table>"
             "<tr><th>Gebied</th><th>Status</th><th>Bovenaan</th><th>Onderaan (dal)</th><th>Sneeuwgrens</th><th>Windstoten</th><th>Lijzijde</th></tr>")
    for x in top:
        d0 = x["days"][0]
        r = x["r"]
        h.append(f"<tr><td>{escape(r['name'])}</td><td>{d0['status']}</td><td>{r['top_m']} m: {fmt(x['ahead'])}</td>"
                 f"<td>{r['base_m']} m: {fmt(x['ahead_b'])}</td><td>{d0['fl_min']:.0f} m</td><td>{d0['gust']:.0f} km/u</td>"
                 f"<td>{'/'.join(d0['lee']) if d0['lee'] else '-'}</td></tr>")
    h.append("</table></div></section><section class='card'><h2>Sneeuwkaarten: nieuwe sneeuw per dag (cm)</h2>"
             "<div class='tg'><button id='bt' class='on' onclick=\"sw('top')\">Bovenaan</button>"
             "<button id='bb' onclick=\"sw('base')\">Onderaan (dal)</button></div>"
             "<div class='lg'>" + "".join(f"<span class='{k}' style='background:var(--lg)' data-k='{k}'>{t}</span>" for t, k in LEGEND) +
             "</div><div class='maps'>")
    for i in range(min(5, len(dates))):
        h.append(f"<div><h3>{lab[i]}</h3>{svg_map(results, i)}</div>")
    h.append("</div><p class='fine'>Kleur = geschatte nieuwe sneeuw (cm) over die dag, geïnterpoleerd tussen de gebieden en enkel getoond "
             "binnen ~22 km van een gebied. Stip = gebied (gestippeld = nog dicht), cijfer = cm bij de grote gebieden. Rode lijn = snelweg, grijze lijn = hoofdweg. "
             "Dit is een model, geen meting.</p>"
             "<script>function sw(l){document.querySelector('.maps').classList.toggle('show-base',l==='base');"
             "document.getElementById('bt').classList.toggle('on',l==='top');document.getElementById('bb').classList.toggle('on',l==='base');}"
             "var c={h1:'#cfe6ff',h2:'#9fcbff',h3:'#6fb0ff',h4:'#3f8bf0',h5:'#2f5fd0',h6:'#6a3fc8',h7:'#a02fb5',h8:'#d6336c'};"
             "document.querySelectorAll('.lg span').forEach(function(e){e.style.background=c[e.dataset.k];e.style.setProperty('--lg',c[e.dataset.k]);});"
             "</script></section>")
    h.append("<section class='card'><h2>Alle gebieden, score per dag</h2><div class='wrap'><table><tr><th>Gebied</th><th>Top</th><th>Status</th>"
             + "".join(f"<th>{l}</th>" for l in lab) + "</tr>")
    order = sorted(results, key=lambda x: -max((d["score"] for d in x["days"] if d["status"] != "dicht"), default=-1))
    for x in order:
        r = x["r"]
        tip = escape(r.get("note") or "")
        h.append(f"<tr><td title='{tip}'>{escape(r['name'])}</td><td>{r['top_m']}</td><td>{x['days'][0]['status']}</td>")
        for d in x["days"]:
            dal = f"; dal +{d['total_b']:.0f} cm" if d["total_b"] is not None else ""
            t = (f"{d['fresh'][0]:.0f} cm vers bovenaan ({d['fresh'][1]:.0f}-{d['fresh'][2]:.0f}); dag +{d['total'][0]:.0f} cm{dal}; "
                 f"temp {d['temp'] if d['temp'] is None else round(d['temp'])} C; wind tot {d['gust']:.0f} km/u; "
                 f"sneeuwgrens {d['fl']:.0f} m. " + " | ".join(d["flags"]))
            h.append(f"<td class='{cell_class(d['score'], d['status'])}' title='{escape(t)}'>{d['score']} <span class='fine'>+{d['total'][0]:.0f}</span></td>")
        h.append("</tr>")
    h.append("</table></div><p class='fine'>Cel = score/100 en nieuwe sneeuw bovenaan die dag. Grijs = gebied dicht volgens de openingsdata in resorts.csv. "
             "Score is een eigen heuristiek (poeder 45, zon 20, wind 20, temperatuur 10, sneeuwdek 5).</p></section>")
    h.append("<section class='card'><h2>Modelcontrole</h2><p class='fine' style='margin-top:0'>Nieuwe sneeuw (cm) in de komende "
             f"{hours} uur per weermodel, voor de 5 gebieden met de meeste sneeuw. Wijkt één model sterk af, dan telt dat niet mee in het bereik. "
             "De laatste kolom is het eigen sneeuwcijfer van Open-Meteo (gemeten op het rasterpunt van het model, vaak lager in de bergen): "
             "mijn berekening mag tot ongeveer twee keer hoger liggen (Open-Meteo rekent heel voorzichtig, 0,7 cm per mm), maar bij een groter verschil klopt er iets niet.</p>"
             "<div class='wrap'><table><tr><th>Gebied</th><th>Hoogte</th><th>Model</th><th>Berekend (cm)</th><th>Open-Meteo zelf (cm)</th></tr>")
    for x in top[:5]:
        names = x.get("models_P") or []
        for label, st, el in (("boven", x["ahead"], x["r"]["top_m"]), ("dal", x["ahead_b"], x["r"]["base_m"])):
            if not st:
                continue
            for k, v in enumerate(st[3]):
                nm = names[k] if k < len(names) else f"model {k + 1}"
                nat = x["native"].get(nm) if label == "boven" else None
                natt = "-" if nat is None else f"{nat:.0f}"
                h.append(f"<tr><td>{escape(x['r']['name'])}</td><td>{label} ({el} m)</td><td>{escape(nm)}</td><td>{v:.0f}</td><td>{natt}</td></tr>")
    h.append("</table></div></section>")
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
    try:
        raw_b = fetch_all(resorts, a.days, models, demo=a.demo, elev="base_m", hourly=BASE_HOURLY)
    except Exception as e:  # dal-prognose is een extraatje: rapport werkt ook zonder
        print(f"Dal-prognose mislukt, ga verder zonder ({e})", file=sys.stderr)
        raw_b = [None] * len(resorts)
    results = [analyse(r, rw, rb, models, a.days) for r, rw, rb in zip(resorts, raw, raw_b)]
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
