"""Об'єкт «Дім»: облік електроенергії за двозонним тарифом (день / ніч) по місяцях.

Показники лічильника (зона «день» і «ніч») на кінець місяця -> витрата кожної зони, ціна зони з тарифу,
що діє з місяця (переходить на наступні) -> сума. Показники можна вносити вручну або з Home Assistant
(сенсори «день» / «ніч» прив'язуються в Показники → HA).
"""
import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import calc
from audit import log
from db import get_db
from i18n import _

bp = Blueprint("home", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS home_el (
    period TEXT PRIMARY KEY,          -- YYYY-MM; показники на кінець місяця
    day REAL, night REAL,
    taken TEXT
);
CREATE TABLE IF NOT EXISTS home_tariff (
    from_period TEXT PRIMARY KEY,     -- діє з місяця і далі
    day REAL, night REAL
);
"""


def ensure_schema():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


def _num(v):
    v = str(v or "").strip().replace(",", ".").replace(" ", "").replace(" ", "")
    if not v:
        return None
    return float(v)


def tariff_for(period, db=None):
    db = db or get_db()
    r = db.execute("SELECT * FROM home_tariff WHERE from_period<=? ORDER BY from_period DESC LIMIT 1", (period,)).fetchone()
    return (r["day"], r["night"], r["from_period"]) if r else (None, None, None)


def write_reading(period, zone, value):
    """З HA: показник зони за місяць, лише в порожню клітинку. -> True, якщо вписано."""
    if zone not in ("day", "night") or value is None:
        return False
    ensure_schema()
    db = get_db()
    r = db.execute("SELECT * FROM home_el WHERE period=?", (period,)).fetchone()
    if r and r[zone] is not None:
        return False
    if r:
        db.execute(f"UPDATE home_el SET {zone}=? WHERE period=?", (value, period))
    else:
        db.execute(f"INSERT INTO home_el(period, {zone}) VALUES (?,?)", (period, value))
    db.commit()
    return True


def rows_for(periods):
    """Рядки таблиці (новіші зверху): показники, витрата, ціни, сума."""
    db = get_db()
    data = {r["period"]: r for r in db.execute("SELECT * FROM home_el")}
    allp = sorted(data)
    out = []
    for p in periods:
        r = data.get(p)
        prev = [x for x in allp if x < p]

        def last(zone):
            for x in reversed(prev):
                if data[x][zone] is not None:
                    return data[x][zone]
            return None
        cd = cn = None
        if r and r["day"] is not None and last("day") is not None:
            cd = calc.r2(r["day"] - last("day"))
        if r and r["night"] is not None and last("night") is not None:
            cn = calc.r2(r["night"] - last("night"))
        pd, pn, _f = tariff_for(p, db)
        sd = calc.r2(cd * pd) if cd is not None and pd is not None else None
        sn = calc.r2(cn * pn) if cn is not None and pn is not None else None
        out.append({"period": p, "day": r["day"] if r else None, "night": r["night"] if r else None,
                    "taken": r["taken"] if r else None, "cd": cd, "cn": cn,
                    "total_kwh": calc.r2((cd or 0) + (cn or 0)) if cd is not None or cn is not None else None,
                    "pd": pd, "pn": pn, "sd": sd, "sn": sn,
                    "sum": calc.r2((sd or 0) + (sn or 0)) if sd is not None or sn is not None else None})
    return out


@bp.route("/home", methods=["GET", "POST"])
def page():
    from app import this_month
    ensure_schema()
    db = get_db()
    if request.method == "POST":
        f = request.form
        if f.get("action") == "tariff":
            per = f.get("from_period") or this_month()
            if not re.fullmatch(r"\d{4}-\d{2}", per):
                abort(400)
            try:
                d, n = _num(f.get("t_day")), _num(f.get("t_night"))
            except ValueError:
                flash(_("Неверное число"))
                return redirect(url_for("home.page"))
            db.execute("INSERT INTO home_tariff(from_period, day, night) VALUES (?,?,?) "
                       "ON CONFLICT(from_period) DO UPDATE SET day=excluded.day, night=excluded.night", (per, d, n))
            db.commit()
            log("Тариф дома изменён", f"с {calc.period_label(per)}: день {d}, ночь {n}")
            flash(_("Тариф сохранён"))
            return redirect(url_for("home.page"))
        n = 0
        for k, v in f.items():
            m = re.fullmatch(r"(day|night|taken)_(\d{4}-\d{2})", k)
            if not m:
                continue
            zone, per = m.groups()
            if zone == "taken":
                from db import to_iso
                val = to_iso(v) if v.strip() else None
            else:
                try:
                    val = _num(v)
                except ValueError:
                    flash(_("Неверное число") + f": {v}")
                    continue
            r = db.execute("SELECT * FROM home_el WHERE period=?", (per,)).fetchone()
            if r and r[zone] == val:
                continue
            if r:
                db.execute(f"UPDATE home_el SET {zone}=? WHERE period=?", (val, per))
            elif val is not None:
                db.execute(f"INSERT INTO home_el(period, {zone}) VALUES (?,?)", (per, val))
            else:
                continue
            n += 1
        db.execute("DELETE FROM home_el WHERE day IS NULL AND night IS NULL AND taken IS NULL")
        db.commit()
        if n:
            log("Показания дома сохранены", str(n))
        flash(_("Сохранено") + f": {n}")
        return redirect(url_for("home.page", months=request.args.get("months", 12)))
    months = request.args.get("months", 12, type=int)
    months = months if months in (6, 12, 24, 36) else 12
    periods, p = [], this_month()
    for _i in range(months):
        periods.append(p)
        p = calc.prev_period(p)
    rows = rows_for(periods)
    year = [r for r in rows if r["period"][:4] == periods[0][:4]]
    tariffs = db.execute("SELECT * FROM home_tariff ORDER BY from_period DESC").fetchall()
    td, tn, tf = tariff_for(this_month(), db)
    last = next((r for r in rows if r["sum"] is not None), None)
    return render_template("home_el.html", rows=rows, months=months, tariffs=tariffs, td=td, tn=tn, tf=tf,
                           last=last, cur=this_month(),
                           year={"kwh": calc.r2(sum(r["total_kwh"] or 0 for r in year)),
                                 "sum": calc.r2(sum(r["sum"] or 0 for r in year)),
                                 "day": calc.r2(sum(r["cd"] or 0 for r in year)),
                                 "night": calc.r2(sum(r["cn"] or 0 for r in year)), "y": periods[0][:4]})


def init_app(app):
    app.register_blueprint(bp)
