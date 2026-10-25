"""Лінії електроживлення: таблиця показників по лінії (як у Google Таблиці), баланс ввід/сонце/лічильники."""
import json

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

import calc
from audit import log
from db import get_db
from i18n import _

bp = Blueprint("lines", __name__)
ROLES = {"input": "Вводный (счётчик линии)", "solar": "Солнечная генерация",
         "own": "Собственный потребитель (охрана, котельная…)"}


def _num(v):
    v = str(v or "").strip().replace(",", ".").replace(" ", "").replace(" ", "")
    if v == "":
        return None
    return float(v)


def periods_range(end, n):
    out, p = [], end
    for _i in range(n):
        out.append(p)
        p = calc.prev_period(p)
    return out                       # новіші зверху


def _series(rows, initial, initial_r, coef, periods):
    """Показники по місяцях -> {period: (value, cons, reactive, rcons)}; витрата від попереднього відомого показника."""
    by = {r["period"]: r for r in rows}
    allp = sorted(by)
    out = {}
    for p in periods:
        r = by.get(p)
        prev = [x for x in allp if x < p]
        pv = by[prev[-1]]["value"] if prev and by[prev[-1]]["value"] is not None else initial
        prv = None
        for x in reversed(prev):
            if by[x]["reactive"] is not None:
                prv = by[x]["reactive"]
                break
        prv = initial_r if prv is None else prv
        v = r["value"] if r else None
        rv = r["reactive"] if r else None
        out[p] = {"value": v, "cons": calc.r2((v - pv) * coef) if v is not None and pv is not None else None,
                  "reactive": rv, "rcons": calc.r2((rv - prv) * coef) if rv is not None and prv is not None else None}
    return out


def line_data(line_id, periods):
    db = get_db()
    cols = []
    # субрахунки орендарів (лічильники складів з послугою «за лічильником», закріплені за лінією)
    for m in db.execute("SELECT m.*, w.name AS wh, t.name AS tenant, s.name AS svc FROM meters m "
                        "JOIN warehouses w ON w.id=m.warehouse_id LEFT JOIN tenants t ON t.id=w.tenant_id "
                        "JOIN services s ON s.id=m.service_id WHERE m.line_id=? AND m.active=1 ORDER BY w.name, m.serial",
                        (line_id,)).fetchall():
        rows = db.execute("SELECT period, value, reactive FROM readings WHERE meter_id=?", (m["id"],)).fetchall()
        cols.append({"kind": "m", "id": m["id"], "title": m["wh"], "sub": m["tenant"] or _("свободен"),
                     "serial": m["serial"], "reactive": bool(m["has_reactive"]), "role": "tenant",
                     "data": _series(rows, m["initial_value"], m["initial_reactive"] or 0, m["coef"] or 1, periods)})
    for m in db.execute("SELECT * FROM line_meters WHERE line_id=? AND active=1 ORDER BY CASE role WHEN 'own' THEN 0 "
                        "WHEN 'input' THEN 1 ELSE 2 END, sort, name", (line_id,)).fetchall():
        rows = db.execute("SELECT period, value, reactive FROM line_readings WHERE line_meter_id=?", (m["id"],)).fetchall()
        cols.append({"kind": "l", "id": m["id"], "title": m["name"], "sub": _(ROLES[m["role"]]).split(" (")[0],
                     "serial": m["serial"], "reactive": bool(m["has_reactive"]), "role": m["role"],
                     "data": _series(rows, m["initial_value"], m["initial_reactive"] or 0, m["coef"] or 1, periods)})
    # баланс по місяцях
    bal = {}
    for p in periods:
        def s(role, key="cons"):
            vals = [c["data"][p][key] for c in cols if c["role"] in role and c["data"][p][key] is not None]
            return calc.r2(sum(vals)) if vals else None
        users = s(("tenant", "own"))
        ureact = s(("tenant", "own"), "rcons")
        inp = s(("input",))
        solar = s(("solar",))
        supply = (inp or 0) + (solar or 0) if inp is not None or solar is not None else None
        diff = calc.r2(supply - (users or 0)) if supply is not None and users is not None else None
        bal[p] = {"users": users, "ureact": ureact, "input": inp, "solar": solar, "supply": supply, "diff": diff,
                  "pct": calc.r2(diff / supply * 100) if diff is not None and supply else None}
    return cols, bal


@bp.route("/lines")
def lines():
    db = get_db()
    all_lines = db.execute("SELECT * FROM power_lines WHERE active=1 ORDER BY sort, id").fetchall()
    if not all_lines:
        return redirect(url_for("lines.manage"))
    lid = request.args.get("line", type=int) or all_lines[0]["id"]
    line = next((l for l in all_lines if l["id"] == lid), all_lines[0])
    n = request.args.get("months", 12, type=int)
    n = n if n in (6, 12, 24, 36) else 12
    from app import current_period
    end = request.args.get("to") or current_period()
    periods = periods_range(end, n)
    cols, bal = line_data(line["id"], periods)
    return render_template("lines.html", all_lines=all_lines, line=line, cols=cols, bal=bal, periods=periods, months=n,
                           end=end)


@bp.route("/lines/save", methods=["POST"])
def save():
    db = get_db()
    data = request.get_json(force=True) or {}
    n = 0
    cells = sorted((data.get("cells") or {}).items(), key=lambda kv: "|reactive|" in kv[0])   # спершу активна
    for key, val in cells:
        kind, mid, field, period = key.split("|")       # m|12|value|2026-09 ; d|0|taken|2026-09 — дата зйому
        if kind == "d":
            from db import to_iso
            import re as _re
            sm = _re.fullmatch(r"\s*(\d{1,2})[./](\d{1,2})\.?\s*", val or "")      # «26.10» — рік з місяця
            if sm:
                y, pm = int(period[:4]), int(period[5:7])
                y += 1 if int(sm.group(2)) < pm - 6 else 0                   # грудень → зйом 02.01 наступного року
                val = f"{sm.group(1)}.{sm.group(2)}.{y}"
            iso = to_iso(val)
            if val and not iso:
                abort(400, _("Неверная дата") + f": {val}")
            if iso:
                db.execute("INSERT INTO reading_dates(period, taken) VALUES (?,?) "
                           "ON CONFLICT(period) DO UPDATE SET taken=excluded.taken", (period, iso))
            else:
                db.execute("DELETE FROM reading_dates WHERE period=?", (period,))
            n += 1
            continue
        if field not in ("value", "reactive") or kind not in ("m", "l"):
            abort(400)
        v = _num(val)
        table, idcol = ("readings", "meter_id") if kind == "m" else ("line_readings", "line_meter_id")
        row = db.execute(f"SELECT id, value, reactive FROM {table} WHERE {idcol}=? AND period=?", (int(mid), period)).fetchone()
        if row:
            other = row["reactive"] if field == "value" else row["value"]
            if v is None and other is None or (kind == "m" and field == "value" and v is None):
                db.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
            else:
                db.execute(f"UPDATE {table} SET {field}=? WHERE id=?", (v, row["id"]))
        elif v is not None:
            if kind == "m" and field == "reactive":
                abort(400, _("Сначала внесите активный показатель"))
            db.execute(f"INSERT INTO {table}({idcol}, period, {field}) VALUES (?,?,?)", (int(mid), period, v))
        n += 1
    db.commit()
    if n:
        log("Показания сохранены" if data.get("table") else "Показания линии сохранены",
            f"{data.get('line_name', '') or _('таблица показаний')}: {n}")
    return jsonify(ok=True, saved=n)


# ---------------- таблиця показників: колонки — лічильники орендарів, рядки — місяці / дати зйому ----------------

def table_data(periods, service_id=None):
    db = get_db()
    groups = []          # [{tenant, tid, whs:[...], cols:[...]}]
    q = ("SELECT m.*, w.name AS wh, w.id AS wid, s.name AS svc, s.unit, l.name AS line FROM meters m "
         "JOIN warehouses w ON w.id=m.warehouse_id JOIN services s ON s.id=m.service_id "
         "LEFT JOIN power_lines l ON l.id=m.line_id WHERE m.active=1 AND s.active=1 AND w.id=?"
         + (" AND m.service_id=?" if service_id else "") + " ORDER BY s.sort, m.serial")

    def cols_for(wh_ids):
        cols = []
        for wid in wh_ids:
            for m in db.execute(q, (wid, service_id) if service_id else (wid,)).fetchall():
                rows = db.execute("SELECT period, value, reactive FROM readings WHERE meter_id=?", (m["id"],)).fetchall()
                cols.append({"id": m["id"], "wh": m["wh"], "svc": m["svc"], "unit": m["unit"], "serial": m["serial"],
                             "name": m["name"], "line": m["line"], "reactive": bool(m["has_reactive"]),
                             "data": _series(rows, m["initial_value"], m["initial_reactive"] or 0, m["coef"] or 1, periods)})
        return cols
    for t in db.execute("SELECT id, name FROM tenants WHERE active=1 ORDER BY name COLLATE NOCASE").fetchall():
        whs = db.execute("SELECT id, name FROM warehouses WHERE tenant_id=? ORDER BY name", (t["id"],)).fetchall()
        if not whs:
            continue
        groups.append({"tid": t["id"], "tenant": t["name"], "whs": [dict(w) for w in whs],
                       "cols": cols_for([w["id"] for w in whs])})
    free = db.execute("SELECT id, name FROM warehouses WHERE tenant_id IS NULL ORDER BY name").fetchall()
    fcols = cols_for([w["id"] for w in free])
    if fcols:
        groups.append({"tid": None, "tenant": None, "whs": [dict(w) for w in free], "cols": fcols})
    dates = {r["period"]: r["taken"] for r in db.execute("SELECT period, taken FROM reading_dates")}
    return groups, dates


@bp.route("/readings/table")
def table():
    from app import this_month
    db = get_db()
    n = request.args.get("months", 12, type=int)
    n = n if n in (6, 12, 24, 36) else 12
    end = request.args.get("to") or this_month()
    sid = request.args.get("service", type=int)
    periods = periods_range(end, n)
    groups, dates = table_data(periods, sid)
    msvcs = db.execute("SELECT id, name FROM services WHERE mode='meter' AND active=1 ORDER BY sort, name").fetchall()
    plines = db.execute("SELECT id, name FROM power_lines WHERE active=1 ORDER BY sort, id").fetchall()
    return render_template("readings_table.html", groups=groups, dates=dates, periods=periods, months=n, end=end,
                           service=sid, msvcs=msvcs, plines=plines)


@bp.route("/readings/table/meter", methods=["POST"])
def quick_meter():
    """Швидке додавання лічильника з таблиці показників."""
    db = get_db()
    d = request.get_json(force=True) or {}
    wh = db.execute("SELECT id, name FROM warehouses WHERE id=?", (d.get("warehouse_id"),)).fetchone()
    svc = db.execute("SELECT id, name FROM services WHERE id=? AND mode='meter'", (d.get("service_id"),)).fetchone()
    if not wh or not svc:
        return jsonify(ok=False, error=_("Выберите склад и услугу")), 400
    try:
        coef = _num(d.get("coef")) or 1
        init = _num(d.get("initial_value")) or 0
        init_r = _num(d.get("initial_reactive")) or 0
    except ValueError:
        return jsonify(ok=False, error=_("Неверное число")), 400
    line_id = int(d["line_id"]) if str(d.get("line_id") or "").isdigit() else None
    mid = db.execute("INSERT INTO meters(warehouse_id, service_id, resource, name, serial, coef, initial_value, line_id, "
                     "has_reactive, initial_reactive) VALUES (?,?,'svc',?,?,?,?,?,?,?)",
                     (wh["id"], svc["id"], (d.get("name") or "").strip() or None, (d.get("serial") or "").strip(),
                      coef, init, line_id, 1 if d.get("has_reactive") else 0, init_r)).lastrowid
    log("Счётчик добавлен", f"{wh['name']}: {svc['name']} {d.get('serial') or ''}".strip(), commit=False)
    db.commit()
    return jsonify(ok=True, id=mid)


@bp.route("/lines/manage", methods=["GET", "POST"])
def manage():
    db = get_db()
    if request.method == "POST":
        a = request.form.get("action")
        if a == "line":
            lid = request.form.get("lid", type=int)
            name = request.form.get("name", "").strip()
            if request.form.get("delete") and lid:
                db.execute("UPDATE meters SET line_id=NULL WHERE line_id=?", (lid,))
                db.execute("DELETE FROM line_readings WHERE line_meter_id IN (SELECT id FROM line_meters WHERE line_id=?)",
                           (lid,))
                db.execute("DELETE FROM line_meters WHERE line_id=?", (lid,))
                db.execute("DELETE FROM power_lines WHERE id=?", (lid,))
            elif lid:
                db.execute("UPDATE power_lines SET name=?, note=?, sort=? WHERE id=?",
                           (name, request.form.get("note", "").strip(), request.form.get("sort", type=int) or 100, lid))
            elif name:
                db.execute("INSERT INTO power_lines(name, note, sort) VALUES (?,?,?)",
                           (name, request.form.get("note", "").strip(), request.form.get("sort", type=int) or 100))
        elif a == "lmeter":
            mid = request.form.get("mid", type=int)
            vals = [request.form.get("line_id", type=int), request.form.get("name", "").strip(),
                    request.form.get("role") if request.form.get("role") in ROLES else "own",
                    request.form.get("serial", "").strip(), _num(request.form.get("coef")) or 1,
                    _num(request.form.get("initial_value")) or 0, _num(request.form.get("initial_reactive")) or 0,
                    1 if request.form.get("has_reactive") else 0]
            if request.form.get("delete") and mid:
                db.execute("DELETE FROM line_readings WHERE line_meter_id=?", (mid,))
                db.execute("DELETE FROM line_meters WHERE id=?", (mid,))
            elif mid:
                db.execute("UPDATE line_meters SET line_id=?, name=?, role=?, serial=?, coef=?, initial_value=?, "
                           "initial_reactive=?, has_reactive=? WHERE id=?", vals + [mid])
            elif vals[1]:
                db.execute("INSERT INTO line_meters(line_id, name, role, serial, coef, initial_value, initial_reactive, "
                           "has_reactive) VALUES (?,?,?,?,?,?,?,?)", vals)
        elif a == "assign":
            for k, v in request.form.items():
                if k.startswith("ml_"):
                    mid = int(k[3:])
                    db.execute("UPDATE meters SET line_id=?, has_reactive=? WHERE id=?",
                               (int(v) if v else None, 1 if request.form.get(f"mr_{mid}") else 0, mid))
        db.commit()
        log("Линии электроэнергии изменены", a or "")
        flash(_("Сохранено"))
        return redirect(url_for("lines.manage"))
    all_lines = db.execute("SELECT * FROM power_lines ORDER BY sort, id").fetchall()
    lmeters = db.execute("SELECT * FROM line_meters ORDER BY line_id, role, sort, name").fetchall()
    tmeters = db.execute("SELECT m.*, w.name AS wh, t.name AS tenant, s.name AS svc FROM meters m "
                         "JOIN warehouses w ON w.id=m.warehouse_id LEFT JOIN tenants t ON t.id=w.tenant_id "
                         "JOIN services s ON s.id=m.service_id WHERE m.active=1 ORDER BY s.sort, w.name").fetchall()
    return render_template("lines_manage.html", all_lines=all_lines, lmeters=lmeters, tmeters=tmeters, LROLES=ROLES)


def init_app(app):
    app.register_blueprint(bp)
