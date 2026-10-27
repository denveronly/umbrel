"""Вкладка «Економіка»: підсумки за формами оплати по місяцях, графіки, курс USD (НБУ)."""
import json
import urllib.request
from datetime import date, datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for

import auth
import calc
import i18n
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

def month_totals(period, today_period, detail=False):
    """Суми за місяць за формами оплати.

    Затверджений акт — його зафіксовані цифри. Чернетка акта за поточний/майбутній місяць — перерахунок
    з актуальних даних (як на сторінці «Акти»: ціни, склади, форма оплати могли змінитись після формування);
    за минулі місяці — як збережено. Для поточного/майбутнього місяця орендарі без акта — розрахунок («оцінка»).
    detail=True — ще й розшифровка по кожному орендарю (res["items"]).
    """
    db = get_db()
    res = {k: 0.0 for k in FORMS}
    res.update(vat=0.0, total=0.0, acts=0, estimated=0, rent=0.0, util=0.0)
    res["svc"] = {}                                   # комунальні по послугах: {назва: {sum, qty, unit}}
    items = []
    seen = set()
    future = period >= today_period
    active = {t["id"]: t for t in db.execute("SELECT id, name, payment_type FROM tenants WHERE active=1").fetchall()}
    for a in db.execute("SELECT a.id, a.number, a.status, a.tenant_id, a.payment_type, a.data FROM acts a "
                        "WHERE a.period=?", (period,)).fetchall():
        d = json.loads(a["data"])
        pt, lines, totals, src = a["payment_type"], d["lines"], d["totals"], "final"
        if a["status"] != "final":
            src = "draft"
            if future and a["tenant_id"] in active:
                lines, totals, _w = calc.build_act(a["tenant_id"], period)
                pt, src = active[a["tenant_id"]]["payment_type"], "draft_recalc"
        it = _add(res, pt, lines, totals)
        it.update(tenant_id=a["tenant_id"], name=(d.get("tenant") or {}).get("display_name") or (d.get("tenant") or {}).get("name") or "?", src=src,
                  act_id=a["id"], number=a["number"])
        items.append(it)
        res["acts"] += 1
        seen.add(a["tenant_id"])
    if future:
        everyone = {t["id"]: t for t in db.execute("SELECT id, name, payment_type FROM tenants").fetchall()}
        for t in everyone.values():                 # і колишні — у місяці після виїзду ще можуть бути комунальні
            if t["id"] in seen:
                continue
            lines, totals, _w = calc.build_act(t["id"], period)
            if totals["total"]:
                it = _add(res, t["payment_type"], lines, totals)
                it.update(tenant_id=t["id"], name=t["name"], src="estimate", act_id=None, number=None)
                items.append(it)
                res["estimated"] += 1
    for k in list(res):
        if isinstance(res[k], float):
            res[k] = calc.r2(res[k])
    for v in res["svc"].values():
        v["sum"], v["qty"] = calc.r2(v["sum"]), calc.r2(v["qty"])
    if detail:
        order = {"cash": 0, "bank": 1, "bank_vat": 2}
        res["items"] = sorted(items, key=lambda i: (order.get(i["pt"], 9), i["name"].lower()))
    return res


def _add(res, pt, lines, totals):
    """Додати суму одного акта до підсумків місяця; повертає рядок розшифровки."""
    res[pt] = res.get(pt, 0) + totals["total"]
    res["vat"] += totals.get("vat") or 0
    res["total"] += totals["total"]
    k = (1 + float(totals["vat_rate"]) / 100) if totals.get("vat_rate") else 1
    rent = util = 0
    for l in lines:
        if l.get("group") == "rent":
            rent += l["sum"] * k
        else:
            util += l["sum"] * k
            name = l.get("service") or l.get("name") or "?"
            sv = res["svc"].setdefault(name, {"sum": 0.0, "qty": 0.0, "unit": l.get("unit") or ""})
            sv["sum"] += l["sum"] * k
            if l.get("unit") and l.get("unit") != "послуга":
                sv["qty"] += l.get("qty") or 0
    res["rent"] += rent
    res["util"] += util
    return {"pt": pt, "rent": calc.r2(rent), "util": calc.r2(util), "total": totals["total"]}


def last_months(end_period, n):
    out, p = [], end_period
    for _i in range(n):
        out.append(p)
        p = calc.prev_period(p)
    return list(reversed(out))


@bp.route("/economics", methods=["GET", "POST"])
@auth.perm_required("economics")
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
    # після 15-го — уже наступний місяць (акти за нього готуються наперед: оренда наступного + комунальні поточного)
    show = calc.next_period(cur) if today.day > 15 else cur
    end = request.args.get("to") or show
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
        rows.append({"period": p, "label": i18n.period_label_ui(p), **t, "rate": rate, "rate_date": rate_date,
                     "usd": calc.r2(t["total"] / rate) if rate else None})
    # ---- комунальні: по послугах і місяцях + порівняння з рахунками YASNO за електрику ----
    order = {r["name"]: (r["sort"], r["name"]) for r in db.execute("SELECT name, sort FROM services")}
    svc_names = sorted({n for r in rows for n in r["svc"]}, key=lambda n: order.get(n, (9999, n)))
    util_rows = [{"name": n, "unit": next((r["svc"][n]["unit"] for r in rows if n in r["svc"]), ""),
                  "cells": [r["svc"].get(n) for r in rows]} for n in svc_names]
    util_shift = s.get("util_shift", "1") == "1"
    yasno = {}
    try:
        for b in db.execute("SELECT period, SUM(amount) AS amount, SUM(kwh) AS kwh FROM yasno_bills "
                            "WHERE planned=0 AND period IS NOT NULL GROUP BY period"):
            yasno[b["period"]] = {"amount": b["amount"], "kwh": b["kwh"]}
    except Exception:  # noqa — таблиці ще немає
        pass
    for r in rows:
        up = calc.prev_period(r["period"]) if util_shift else r["period"]
        r["uperiod"] = up
        el = [v for v in r["svc"].values() if "квт" in (v["unit"] or "").lower()]
        r["el_sum"] = calc.r2(sum(v["sum"] for v in el))
        r["el_kwh"] = calc.r2(sum(v["qty"] for v in el))
        r["yasno"] = yasno.get(up)

    now_rate, now_rate_date = rate_on(today)
    sel = next((r for r in rows if r["period"] == show), rows[-1])
    sel["items"] = month_totals(sel["period"], cur, detail=True)["items"]
    sums = {k: calc.r2(sum(r[k] for r in rows)) for k in (*FORMS, "vat", "total", "rent", "util")}
    sums["usd"] = calc.r2(sum(r["usd"] or 0 for r in rows))
    chart = {"labels": [r["label"] for r in rows], "periods": periods,
             "series": {k: [r[k] for r in rows] for k in FORMS},
             "total": [r["total"] for r in rows], "usd": [r["usd"] for r in rows],
             "rate": [r["rate"] for r in rows], "estimated": [bool(r["estimated"]) for r in rows]}
    return render_template("economics.html", rows=rows, sel=sel, sums=sums, chart=chart, months=n, cur=cur,
                           util_rows=util_rows,
                           now_rate=now_rate, now_rate_date=now_rate_date, fx_error=fx_error,
                           util_shift=s.get("util_shift", "1") == "1", today=today.isoformat())


def init_app(app):
    app.register_blueprint(bp)
