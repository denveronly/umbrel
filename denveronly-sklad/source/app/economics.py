"""Вкладка «Економіка»: підсумки за формами оплати по місяцях, графіки, курс USD (НБУ)."""
import json
import urllib.request
from datetime import date, datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

import calc
from audit import log
from db import get_db, settings
from i18n import _

bp = Blueprint("economics", __name__)

NBU_RANGE = ("https://bank.gov.ua/NBU_Exchange/exchange_site?start={start}&end={end}"
             "&valcode=usd&sort=exchangedate&order=asc&json")
NBU_DAY = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&date={d}&json"
FORMS = ("bank", "bank_vat", "cash")


# ---------------- курс USD ----------------

def _get_json(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": "sklad-app/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _store(rows):
    db = get_db()
    n = 0
    for r in rows:
        try:
            d = datetime.strptime(r["exchangedate"], "%d.%m.%Y").date().isoformat()
            rate = float(r.get("rate_per_unit") or (float(r["rate"]) / float(r.get("units") or 1)))
        except (KeyError, ValueError, TypeError):
            continue
        db.execute("INSERT INTO fx_rates(date, usd) VALUES (?,?) ON CONFLICT(date) DO UPDATE SET usd=excluded.usd", (d, rate))
        n += 1
    db.commit()
    return n


def refresh_rates(start, end):
    """Підтягнути курси НБУ за період (один запит); якщо не вийшло — курс на кінцеву дату. Повертає (ok, помилка)."""
    try:
        n = _store(_get_json(NBU_RANGE.format(start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d"))))
        if n:
            return True, None
    except Exception as e:  # noqa
        err = str(e)
    else:
        err = "empty"
    try:
        n = _store(_get_json(NBU_DAY.format(d=end.strftime("%Y%m%d"))))
        return bool(n), None if n else err
    except Exception as e:  # noqa
        return False, str(e)


def ensure_rates(start, end):
    """Курси з кешу; до НБУ звертаємось, лише якщо бракує сьогоднішнього курсу (не частіше разу на годину)."""
    db = get_db()
    have_today = db.execute("SELECT 1 FROM fx_rates WHERE date=?", (end.isoformat(),)).fetchone()
    have_start = db.execute("SELECT 1 FROM fx_rates WHERE date<=?", (start.isoformat(),)).fetchone()
    if have_today and have_start:
        return None
    s = settings()
    last_try = s.get("fx_last_try")
    if last_try and datetime.now() - datetime.fromisoformat(last_try) < timedelta(hours=1):
        return s.get("fx_last_error")
    ok, err = refresh_rates(start, end)
    db.execute("INSERT INTO settings(key, value) VALUES ('fx_last_try', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (datetime.now().isoformat(timespec="seconds"),))
    db.execute("INSERT INTO settings(key, value) VALUES ('fx_last_error', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               ("" if ok else (err or "?"),))
    db.commit()
    return None if ok else err


def rate_on(d):
    """Курс на дату (або найближчий попередній відомий). (курс, дата курсу)"""
    row = get_db().execute("SELECT date, usd FROM fx_rates WHERE date<=? ORDER BY date DESC LIMIT 1",
                           (d.isoformat(),)).fetchone()
    if not row:
        row = get_db().execute("SELECT date, usd FROM fx_rates ORDER BY date ASC LIMIT 1").fetchone()
    return (row["usd"], row["date"]) if row else (None, None)


# ---------------- підсумки ----------------

def month_totals(period, today_period):
    """Суми за місяць за формами оплати: з актів; для поточного/майбутнього місяця — ще й розрахунок без акта."""
    db = get_db()
    res = {k: 0.0 for k in FORMS}
    res.update(vat=0.0, total=0.0, acts=0, estimated=0, rent=0.0, util=0.0)
    seen = set()
    for a in db.execute("SELECT tenant_id, payment_type, total, data FROM acts WHERE period=?", (period,)).fetchall():
        d = json.loads(a["data"])
        _add(res, a["payment_type"], d["lines"], d["totals"])
        res["acts"] += 1
        seen.add(a["tenant_id"])
    if period >= today_period:
        for t in db.execute("SELECT id, payment_type FROM tenants WHERE active=1").fetchall():
            if t["id"] in seen:
                continue
            lines, totals, _w = calc.build_act(t["id"], period)
            if totals["total"]:
                _add(res, t["payment_type"], lines, totals)
                res["estimated"] += 1
    for k in list(res):
        if isinstance(res[k], float):
            res[k] = calc.r2(res[k])
    return res


def _add(res, pt, lines, totals):
    res[pt] = res.get(pt, 0) + totals["total"]
    res["vat"] += totals.get("vat") or 0
    res["total"] += totals["total"]
    k = (1 + float(totals["vat_rate"]) / 100) if totals.get("vat_rate") else 1
    for l in lines:
        if l.get("group") == "rent":
            res["rent"] += l["sum"] * k
        else:
            res["util"] += l["sum"] * k


def last_months(end_period, n):
    out, p = [], end_period
    for _i in range(n):
        out.append(p)
        p = calc.prev_period(p)
    return list(reversed(out))


@bp.route("/economics", methods=["GET", "POST"])
def economics():
    db = get_db()
    today = date.today()
    cur = f"{today.year}-{today.month:02d}"
    if request.method == "POST":
        if request.form.get("action") == "refresh":
            db.execute("DELETE FROM settings WHERE key IN ('fx_last_try','fx_last_error')")
            db.commit()
            ok, err = refresh_rates(today - timedelta(days=800), today)
            flash(_("Курс НБУ обновлён") if ok else _("Не удалось получить курс НБУ") + f": {err}")
        elif request.form.get("action") == "manual":
            try:
                v = float(request.form.get("usd", "").replace(",", ".").strip())
                d = request.form.get("date") or today.isoformat()
                db.execute("INSERT INTO fx_rates(date, usd) VALUES (?,?) ON CONFLICT(date) DO UPDATE SET usd=excluded.usd", (d, v))
                db.commit()
                log("Курс USD задан вручную", f"{d}: {v}")
                flash(_("Курс сохранён"))
            except ValueError:
                flash(_("Некорректный курс"))
        return redirect(url_for("economics.economics", months=request.args.get("months", 12)))

    n = request.args.get("months", 12, type=int)
    n = n if n in (6, 12, 24, 36) else 12
    end = request.args.get("to") or cur
    periods = last_months(end, n)
    start_d = date(int(periods[0][:4]), int(periods[0][5:]), 1)
    fx_error = ensure_rates(start_d, today)
    s = settings()

    rows = []
    for p in periods:
        t = month_totals(p, cur)
        y, m = map(int, p.split("-"))
        first = date(y, m, 1)
        rate, rate_date = rate_on(min(first, today))
        rows.append({"period": p, "label": calc.period_label(p), **t, "rate": rate, "rate_date": rate_date,
                     "usd": calc.r2(t["total"] / rate) if rate else None})
    now_rate, now_rate_date = rate_on(today)
    sel = next((r for r in rows if r["period"] == cur), rows[-1])
    sums = {k: calc.r2(sum(r[k] for r in rows)) for k in (*FORMS, "vat", "total", "rent", "util")}
    sums["usd"] = calc.r2(sum(r["usd"] or 0 for r in rows))
    chart = {"labels": [r["label"] for r in rows], "periods": periods,
             "series": {k: [r[k] for r in rows] for k in FORMS},
             "total": [r["total"] for r in rows], "usd": [r["usd"] for r in rows],
             "rate": [r["rate"] for r in rows], "estimated": [bool(r["estimated"]) for r in rows]}
    return render_template("economics.html", rows=rows, sel=sel, sums=sums, chart=chart, months=n, cur=cur,
                           now_rate=now_rate, now_rate_date=now_rate_date, fx_error=fx_error,
                           util_shift=s.get("util_shift", "1") == "1", today=today.isoformat())


def init_app(app):
    app.register_blueprint(bp)
