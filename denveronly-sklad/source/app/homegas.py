"""Об'єкти типу «дім» (Дім, Страхолісся): облік газу — кілька лічильників, показники на кінець місяця,
витрата, ціна за м³ (діє з місяця й далі), сума. Показники можна брати з Home Assistant (ціль «g:<id>»)."""
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import calc
from audit import log
from db import get_db
from i18n import _

bp = Blueprint("gas", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS gas_meters (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, serial TEXT, initial REAL, sort INTEGER DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS gas_readings (
    meter_id INTEGER NOT NULL REFERENCES gas_meters(id) ON DELETE CASCADE,
    period TEXT NOT NULL, value REAL,
    PRIMARY KEY (meter_id, period)
);
CREATE TABLE IF NOT EXISTS gas_tariff (
    from_period TEXT PRIMARY KEY, price REAL
);
"""


def ensure_schema():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


def _num(v):
    v = str(v or "").strip().replace(",", ".").replace(" ", "").replace(" ", "")
    return float(v) if v else None


def price_for(period, db=None):
    db = db or get_db()
    r = db.execute("SELECT * FROM gas_tariff WHERE from_period<=? ORDER BY from_period DESC LIMIT 1", (period,)).fetchone()
    return (r["price"], r["from_period"]) if r else (None, None)


def meters(db=None):
    ensure_schema()
    return (db or get_db()).execute("SELECT * FROM gas_meters WHERE active=1 ORDER BY sort, id").fetchall()


def write_reading(meter_id, period, value):
    """З HA: показник лічильника за місяць, лише в порожню клітинку."""
    if value is None:
        return False
    ensure_schema()
    db = get_db()
    r = db.execute("SELECT value FROM gas_readings WHERE meter_id=? AND period=?", (meter_id, period)).fetchone()
    if r and r["value"] is not None:
        return False
    db.execute("INSERT INTO gas_readings(meter_id, period, value) VALUES (?,?,?) "
               "ON CONFLICT(meter_id, period) DO UPDATE SET value=excluded.value", (meter_id, period, value))
    db.commit()
    return True


def last_before(meter_id, period, db=None):
    db = db or get_db()
    r = db.execute("SELECT value FROM gas_readings WHERE meter_id=? AND period<? AND value IS NOT NULL "
                   "ORDER BY period DESC LIMIT 1", (meter_id, period)).fetchone()
    return r["value"] if r else None


@bp.route("/gas", methods=["GET", "POST"])
def page():
    from app import this_month
    ensure_schema()
    db = get_db()
    if request.method == "POST":
        f = request.form
        act = f.get("action", "save")
        if act == "tariff":
            per = f.get("from_period") or this_month()
            if not re.fullmatch(r"\d{4}-\d{2}", per):
                abort(400)
            try:
                pr = _num(f.get("price"))
            except ValueError:
                flash(_("Неверное число"))
                return redirect(url_for("gas.page"))
            db.execute("INSERT INTO gas_tariff(from_period, price) VALUES (?,?) "
                       "ON CONFLICT(from_period) DO UPDATE SET price=excluded.price", (per, pr))
            log("Цена газа изменена", f"с {calc.period_label(per)}: {pr}")
            flash(_("Тариф сохранён"))
        elif act == "meter":
            mid = f.get("meter_id", type=int)
            name = (f.get("name") or "").strip()
            try:
                ini = _num(f.get("initial"))
            except ValueError:
                ini = None
            if f.get("delete") and mid:
                db.execute("UPDATE gas_meters SET active=0 WHERE id=?", (mid,))
                log("Газовый счётчик отключён", name)
            elif mid:
                db.execute("UPDATE gas_meters SET name=?, serial=?, initial=?, sort=? WHERE id=?",
                           (name or "—", (f.get("serial") or "").strip(), ini, f.get("sort", type=int) or 100, mid))
            elif name:
                db.execute("INSERT INTO gas_meters(name, serial, initial, sort) VALUES (?,?,?,?)",
                           (name, (f.get("serial") or "").strip(), ini, f.get("sort", type=int) or 100))
                log("Газовый счётчик добавлен", name)
            flash(_("Сохранено"))
        else:
            n = 0
            for k, v in f.items():
                m = re.fullmatch(r"v_(\d+)_(\d{4}-\d{2})", k)
                if not m:
                    continue
                mid, per = int(m.group(1)), m.group(2)
                try:
                    val = _num(v)
                except ValueError:
                    flash(_("Неверное число") + f": {v}")
                    continue
                old = db.execute("SELECT value FROM gas_readings WHERE meter_id=? AND period=?", (mid, per)).fetchone()
                if (old["value"] if old else None) == val:
                    continue
                if val is None:
                    db.execute("DELETE FROM gas_readings WHERE meter_id=? AND period=?", (mid, per))
                else:
                    db.execute("INSERT INTO gas_readings(meter_id, period, value) VALUES (?,?,?) "
                               "ON CONFLICT(meter_id, period) DO UPDATE SET value=excluded.value", (mid, per, val))
                n += 1
            if n:
                log("Показания газа сохранены", str(n))
            flash(_("Сохранено") + f": {n}")
        db.commit()
        return redirect(url_for("gas.page", months=request.args.get("months", 12)))

    months = request.args.get("months", 12, type=int)
    months = months if months in (6, 12, 24, 36) else 12
    periods, p = [], this_month()
    for _i in range(months):
        periods.append(p)
        p = calc.prev_period(p)
    ms = meters(db)
    vals = {(r["meter_id"], r["period"]): r["value"] for r in db.execute("SELECT * FROM gas_readings")}
    rows = []
    for per in periods:
        price, _f = price_for(per, db)
        cells, tot = [], None
        for m in ms:
            v = vals.get((m["id"], per))
            prev = last_before(m["id"], per, db)
            prev = prev if prev is not None else m["initial"]
            cons = calc.r2(v - prev) if v is not None and prev is not None else None
            if cons is not None:
                tot = calc.r2((tot or 0) + cons)
            cells.append({"m": m, "v": v, "cons": cons})
        rows.append({"period": per, "cells": cells, "total": tot, "price": price,
                     "sum": calc.r2(tot * price) if tot is not None and price is not None else None})
    year = [r for r in rows if r["period"][:4] == periods[0][:4]]
    tariffs = db.execute("SELECT * FROM gas_tariff ORDER BY from_period DESC").fetchall()
    cur_price, cur_from = price_for(this_month(), db)
    return render_template("home_gas.html", ms=ms, rows=rows, months=months, cur=this_month(), tariffs=tariffs,
                           cur_price=cur_price, cur_from=cur_from,
                           year={"y": periods[0][:4], "m3": calc.r2(sum(r["total"] or 0 for r in year)),
                                 "sum": calc.r2(sum(r["sum"] or 0 for r in year))},
                           last=next((r for r in rows if r["sum"] is not None), None))


def init_app(app):
    app.register_blueprint(bp)
