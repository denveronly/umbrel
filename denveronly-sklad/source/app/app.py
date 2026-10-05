import json
import os
import re
from datetime import date
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, url_for)

import auth
import calc
import documents
from db import (DATA_DIR, PAYMENT_TYPES, SERVICE_MODES, close_db, companies, get_db, init_db, price_for,
                services, set_setting, settings)
import audit
import i18n
import backup
import ai
import economics
import photos
from audit import log
from i18n import _

app = Flask(__name__)
app.teardown_appcontext(close_db)
UPLOADS = os.path.join(DATA_DIR, "uploads")
init_db()
auth.init_app(app)
i18n.init_app(app)
backup.init_app(app)
audit.init_app(app)
photos.init_app(app)
economics.init_app(app)
ai.init_app(app)


@app.template_filter("plain")
def plain(x):
    """Число для поля ввода: 1250.0 -> 1250, 9.10 -> 9.1, None -> ''."""
    if x is None or x == "":
        return ""
    return f"{float(x):.4f}".rstrip("0").rstrip(".")


@app.context_processor
def ctx():
    return {"SERVICE_MODES": SERVICE_MODES, "PAYMENT_TYPES": PAYMENT_TYPES, "money": calc.fmt_money,
            "num": calc.fmt_num, "period_label": calc.period_label, "S": settings()}


def current_period():
    """По умолчанию — прошлый месяц (показания снимаются в начале следующего)."""
    t = date.today()
    return calc.prev_period(f"{t.year}-{t.month:02d}")


def this_month():
    t = date.today()
    return f"{t.year}-{t.month:02d}"


def get_period(default=None):
    p = request.values.get("period") or default or current_period()
    if not re.fullmatch(r"\d{4}-\d{2}", p):
        abort(400)
    return p


def fnum(name, default=None):
    v = (request.form.get(name) or "").strip().replace(",", ".").replace(" ", "")
    if v == "":
        return default
    try:
        return float(v)
    except ValueError:
        abort(400, _("Некорректное число в поле") + f" {name}")


# ---------------- карта ----------------

@app.route("/")
def index():
    s = settings()
    return render_template("map.html", plan=s.get("plan_image"),
                           plan_w=s.get("plan_w"), plan_h=s.get("plan_h"))


@app.route("/api/warehouses")
def api_warehouses():
    db = get_db()
    s = settings()
    out = []
    for w in db.execute("SELECT w.*, t.name AS tenant_name, t.payment_type FROM warehouses w "
                        "LEFT JOIN tenants t ON t.id=w.tenant_id ORDER BY w.name"):
        d = dict(w)
        d["polygon"] = json.loads(w["polygon"]) if w["polygon"] else None
        d["price"] = calc.rent_price(w, w["payment_type"], s)
        d["photos"] = photos.photos_for(w["id"])
        out.append(d)
    return jsonify(out)


@app.route("/api/warehouses/<int:wid>/polygon", methods=["POST"])
def api_polygon(wid):
    poly = request.get_json(force=True).get("polygon")
    db = get_db()
    db.execute("UPDATE warehouses SET polygon=? WHERE id=?", (json.dumps(poly) if poly else None, wid))
    db.commit()
    return jsonify(ok=True)


@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOADS, name)


# ---------------- склады ----------------

def set_price(wid, new_price, kind="set"):
    """Записати нову ціну складу (за м²/міс) та зберегти зміну в історії (з % зміни)."""
    db = get_db()
    w = db.execute("SELECT w.price_m2, w.name, w.area, t.name AS tname, t.payment_type FROM warehouses w "
                   "LEFT JOIN tenants t ON t.id=w.tenant_id WHERE w.id=?", (wid,)).fetchone()
    old = w["price_m2"] if w else None
    if new_price is None or (old is not None and abs((old or 0) - new_price) < 1e-9):
        return False
    pct = round((new_price / old - 1) * 100, 2) if old else None
    db.execute("UPDATE warehouses SET price_m2=? WHERE id=?", (new_price, wid))
    db.execute("INSERT INTO price_history(warehouse_id, old_price, new_price, tenant_name, payment_type, kind, pct, area, "
               "username) VALUES (?,?,?,?,?,?,?,?,?)",
               (wid, old, new_price, w["tname"] if w else None, w["payment_type"] if w else None, kind, pct,
                w["area"] if w else None, g.user["username"] if g.get("user") else None))
    log("Повышение цены склада" if kind == "increase" else "Цена склада изменена",
        f"{w['name'] if w else wid}: {calc.fmt_money(old or 0)} → {calc.fmt_money(new_price)} грн/м²"
        + (f" ({pct:+.2f}%)" if pct is not None else ""), commit=False)
    return True


def form_price(prefix=""):
    """Ціна за м² з форми: або явно за м², або з суми за місяць ÷ площа."""
    rate = fnum(prefix + "price_m2")
    month = fnum(prefix + "price_month")
    area = fnum(prefix + "area_hint") or fnum(prefix + "area")
    if rate is None and month is not None and area:
        rate = round(month / area, 4)
    return rate

@app.route("/warehouses")
def warehouses():
    db = get_db()
    s = settings()
    rows = db.execute("SELECT w.*, t.name AS tenant_name, t.payment_type FROM warehouses w "
                      "LEFT JOIN tenants t ON t.id=w.tenant_id ORDER BY w.name").fetchall()
    items = [(w, calc.rent_price(w, w["payment_type"], s)) for w in rows]
    tot = {"area": sum(w["area"] for w in rows),
           "rented": sum(w["area"] for w in rows if w["tenant_id"])}
    return render_template("warehouses.html", items=items, tot=tot)


@app.route("/warehouses/new", methods=["GET", "POST"])
@app.route("/warehouses/<int:wid>", methods=["GET", "POST"])
def warehouse_edit(wid=None):
    db = get_db()
    wh = db.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone() if wid else None
    if wid and not wh:
        abort(404)
    if request.method == "POST":
        if request.form.get("delete"):
            photos.remove_warehouse_photos(wid)
            db.execute("DELETE FROM warehouses WHERE id=?", (wid,))
            db.commit()
            flash(_("Склад удалён"))
            return redirect(url_for("warehouses"))
        vals = (request.form["name"].strip(), fnum("area", 0), request.form.get("tenant_id") or None,
                request.form.get("color") if request.form.get("use_color") else None,
                request.form.get("note", "").strip())
        if wh:
            db.execute("UPDATE warehouses SET name=?, area=?, tenant_id=?, color=?, note=? WHERE id=?", vals + (wid,))
        else:
            wid = db.execute("INSERT INTO warehouses(name, area, tenant_id, color, note) VALUES (?,?,?,?,?)",
                             vals).lastrowid
        set_price(wid, form_price())
        db.commit()
        flash(_("Сохранено"))
        return redirect(url_for("warehouse_edit", wid=wid))
    tenants = db.execute("SELECT id, name FROM tenants WHERE active=1 ORDER BY name").fetchall()
    meters = db.execute("SELECT m.*, s.name AS svc_name FROM meters m LEFT JOIN services s ON s.id=m.service_id "
                        "WHERE warehouse_id=? ORDER BY s.sort", (wid,)).fetchall() if wid else []
    pt = None
    if wh and wh["tenant_id"]:
        r = db.execute("SELECT payment_type FROM tenants WHERE id=?", (wh["tenant_id"],)).fetchone()
        pt = r["payment_type"] if r else None
    price = calc.rent_price(wh, pt) if wh else None
    history = db.execute("SELECT * FROM price_history WHERE warehouse_id=? ORDER BY id DESC LIMIT 30",
                         (wid,)).fetchall() if wid else []
    return render_template("warehouse_edit.html", wh=wh, tenants=tenants, meters=meters, price=price, pt=pt,
                           history=history, meter_services=services(mode="meter"))


@app.route("/warehouses/<int:wid>/raise", methods=["POST"])
def warehouse_raise(wid):
    """Підвищення ціни: на % або до нової ціни (за м² чи за місяць)."""
    db = get_db()
    wh = db.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone()
    if not wh:
        abort(404)
    old = wh["price_m2"] or 0
    pct = fnum("raise_pct")
    new = fnum("raise_m2")
    month = fnum("raise_month")
    if new is None and month is not None and wh["area"]:
        new = round(month / wh["area"], 4)
    if new is None and pct is not None:
        new = round(old * (1 + pct / 100), 4)
    if not new or new <= 0:
        flash(_("Укажите процент или новую цену"))
    elif set_price(wid, new, kind="increase"):
        db.commit()
        p = (new / old - 1) * 100 if old else 0
        flash(_("Цена повышена") + f": {calc.fmt_money(old)} → {calc.fmt_money(new)} грн/м² ({p:+.2f}%)")
    return redirect(url_for("warehouse_edit", wid=wid) + "#price")


@app.route("/warehouses/<int:wid>/meters", methods=["POST"])
def meter_save(wid):
    db = get_db()
    mid = request.form.get("meter_id")
    if request.form.get("delete") and mid:
        db.execute("DELETE FROM meters WHERE id=? AND warehouse_id=?", (mid, wid))
    elif mid:
        db.execute("UPDATE meters SET service_id=?, serial=?, coef=?, initial_value=?, active=? "
                   "WHERE id=? AND warehouse_id=?",
                   (request.form.get("service_id", type=int), request.form.get("serial", "").strip(), fnum("coef", 1),
                    fnum("initial_value", 0), 1 if request.form.get("active") else 0, mid, wid))
    else:
        db.execute("INSERT INTO meters(warehouse_id, service_id, resource, serial, coef, initial_value) "
                   "VALUES (?,?,'svc',?,?,?)",
                   (wid, request.form.get("service_id", type=int), request.form.get("serial", "").strip(),
                    fnum("coef", 1), fnum("initial_value", 0)))
    db.commit()
    flash(_("Счётчики обновлены"))
    return redirect(url_for("warehouse_edit", wid=wid) + "#meters")


# ---------------- контакты (арендаторы) ----------------

@app.route("/tenants")
def tenants():
    db = get_db()
    st = settings()
    q = request.args.get("q", "").strip()
    rows = db.execute(
        "SELECT t.*, c.name AS company_name, c.vat_payer FROM tenants t "
        "LEFT JOIN companies c ON c.id=t.company_id ORDER BY t.active DESC, t.name").fetchall()
    items = []
    for t in rows:
        contacts = db.execute("SELECT * FROM tenant_contacts WHERE tenant_id=? ORDER BY id", (t["id"],)).fetchall()
        whs = db.execute("SELECT * FROM warehouses WHERE tenant_id=? ORDER BY name", (t["id"],)).fetchall()
        rent = calc.r2(sum(calc.rent_price(w, t["payment_type"], st)["total"] for w in whs))
        hay = " ".join(str(x or "") for x in [t["name"], t["edrpou"], t["contract_no"], t["company_name"]]
                       + [f"{c['name']} {c['phone']}" for c in contacts] + [w["name"] for w in whs]).lower()
        if q and q.lower() not in hay:
            continue
        items.append({"t": t, "contacts": contacts, "whs": whs, "rent": rent})
    active = [it for it in items if it["t"]["active"]]
    gone = [it for it in items if not it["t"]["active"]]
    return render_template("tenants.html", items=active, gone=gone, q=q)


TENANT_FIELDS = ["name", "edrpou", "iban", "address", "party_type", "passport_series", "passport_number",
                 "passport_issued", "contact", "director_position", "basis", "payment_type", "contract_no",
                 "contract_date", "contract_end", "deposit_date", "deposit_note", "note"]


@app.route("/tenants/new", methods=["GET", "POST"])
@app.route("/tenants/<int:tid>", methods=["GET", "POST"])
def tenant_edit(tid=None):
    db = get_db()
    t = db.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone() if tid else None
    if tid and not t:
        abort(404)
    if request.method == "POST":
        if request.form.get("delete"):
            if db.execute("SELECT 1 FROM acts WHERE tenant_id=?", (tid,)).fetchone():
                db.execute("UPDATE tenants SET active=0 WHERE id=?", (tid,))
                flash(_("У контакта есть акты — он помечен неактивным"))
            else:
                db.execute("DELETE FROM tenants WHERE id=?", (tid,))
                flash(_("Контакт удалён"))
            db.execute("UPDATE warehouses SET tenant_id=NULL WHERE tenant_id=?", (tid,))
            db.commit()
            return redirect(url_for("tenants"))
        vals = [request.form.get(f, "").strip() for f in TENANT_FIELDS]
        active = 1 if request.form.get("active") else 0
        vals += [request.form.get("company_id") or None, fnum("deposit_amount"), active]
        cols = TENANT_FIELDS + ["company_id", "deposit_amount", "active"]
        if t:
            db.execute(f"UPDATE tenants SET {', '.join(f + '=?' for f in cols)} WHERE id=?", vals + [tid])
            if t["active"] and not active:              # виїхав: звільняємо склади, фіксуємо дату
                freed = [r["name"] for r in db.execute("SELECT name FROM warehouses WHERE tenant_id=?", (tid,))]
                db.execute("UPDATE warehouses SET tenant_id=NULL WHERE tenant_id=?", (tid,))
                db.execute("UPDATE tenants SET moved_out=date('now','localtime') WHERE id=?", (tid,))
                log("Арендатор выехал", f"{t['name']}" + (f"; освобождены: {', '.join(freed)}" if freed else ""), commit=False)
                if freed:
                    flash(_("Арендатор выехал, склады освобождены") + ": " + ", ".join(freed))
            elif not t["active"] and active:
                db.execute("UPDATE tenants SET moved_out=NULL WHERE id=?", (tid,))
        else:
            tid = db.execute(f"INSERT INTO tenants({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             vals).lastrowid
            # склад і ціна оренди за місяць одразу при створенні
            new_wid = request.form.get("new_wid", type=int)
            if new_wid:
                db.execute("UPDATE warehouses SET tenant_id=? WHERE id=? AND tenant_id IS NULL", (tid, new_wid))
                month = fnum("new_rent_month")
                w = db.execute("SELECT area FROM warehouses WHERE id=?", (new_wid,)).fetchone()
                if month is not None and w and w["area"]:
                    set_price(new_wid, round(month / w["area"], 4))
            # контактные лица из формы создания
            if request.form.get("person_name") or request.form.get("person_phone"):
                db.execute("INSERT INTO tenant_contacts(tenant_id, name, position, phone, email) VALUES (?,?,?,?,?)",
                           (tid, request.form.get("person_name", "").strip(), request.form.get("person_position", "").strip(),
                            request.form.get("person_phone", "").strip(), request.form.get("person_email", "").strip()))
        db.commit()
        flash(_("Сохранено"))
        return redirect(url_for("tenant_edit", tid=tid))

    st = settings()
    whs = db.execute("SELECT * FROM warehouses WHERE tenant_id=? ORDER BY name", (tid,)).fetchall() if tid else []
    pt = t["payment_type"] if t else None
    wh_items = [(w, calc.rent_price(w, pt, st)) for w in whs]
    free = db.execute("SELECT * FROM warehouses WHERE tenant_id IS NULL ORDER BY name").fetchall()
    free_prices = {w["id"]: calc.rent_price(w, None, st)["total"] for w in free}
    contacts = db.execute("SELECT * FROM tenant_contacts WHERE tenant_id=? ORDER BY id", (tid,)).fetchall() if tid else []
    acts = db.execute("SELECT * FROM acts WHERE tenant_id=? ORDER BY period DESC", (tid,)).fetchall() if tid else []
    tot = {"total": calc.r2(sum(p["total"] for _w, p in wh_items)), "vat": calc.r2(sum(p["vat"] for _w, p in wh_items))}
    subs = {r[0] for r in db.execute("SELECT service_id FROM tenant_services WHERE tenant_id=?", (tid,))} if tid else set()
    all_svcs = [sv for sv in services() if sv["mode"] != "meter"]
    meter_svcs = db.execute("SELECT DISTINCT s.name FROM meters m JOIN services s ON s.id=m.service_id "
                            "JOIN warehouses w ON w.id=m.warehouse_id WHERE w.tenant_id=? AND m.active=1",
                            (tid,)).fetchall() if tid else []
    return render_template("tenant_edit.html", t=t, wh_items=wh_items, free=free, contacts=contacts, acts=acts,
                           subs=subs, all_svcs=all_svcs, meter_svcs=meter_svcs,
                           tot=tot, companies=companies(), vat=float(st["vat_rate"]), free_prices=free_prices)


@app.route("/tenants/<int:tid>/contacts", methods=["POST"])
def tenant_contacts(tid):
    db = get_db()
    cid = request.form.get("contact_id")
    vals = [request.form.get(f, "").strip() for f in ("name", "position", "phone", "email")]
    if request.form.get("delete") and cid:
        db.execute("DELETE FROM tenant_contacts WHERE id=? AND tenant_id=?", (cid, tid))
    elif cid:
        db.execute("UPDATE tenant_contacts SET name=?, position=?, phone=?, email=? WHERE id=? AND tenant_id=?",
                   vals + [cid, tid])
    elif any(vals):
        db.execute("INSERT INTO tenant_contacts(tenant_id, name, position, phone, email) VALUES (?,?,?,?,?)",
                   [tid] + vals)
    db.commit()
    return redirect(url_for("tenant_edit", tid=tid) + "#people")


@app.route("/tenants/<int:tid>/warehouses", methods=["POST"])
def tenant_warehouses(tid):
    """Цены по договору для каждого склада контакта + закрепить/открепить склад."""
    db = get_db()
    action = request.form.get("action")
    wid = request.form.get("wid", type=int)
    if request.form.get("detach"):
        action, wid = "detach", request.form.get("detach", type=int)
    if action == "attach" and wid:
        db.execute("UPDATE warehouses SET tenant_id=? WHERE id=? AND tenant_id IS NULL", (tid, wid))
        flash(_("Склад закреплён"))
    elif action == "detach" and wid:
        db.execute("UPDATE warehouses SET tenant_id=NULL WHERE id=? AND tenant_id=?", (wid, tid))
        flash(_("Склад откреплён и стал свободным"))
    elif action == "prices":
        for w in db.execute("SELECT id FROM warehouses WHERE tenant_id=?", (tid,)).fetchall():
            set_price(w["id"], form_price(f"w{w['id']}_"))
        flash(_("Цены сохранены"))
    db.commit()
    return redirect(url_for("tenant_edit", tid=tid) + "#prices")


@app.route("/tenants/<int:tid>/services", methods=["POST"])
def tenant_services(tid):
    db = get_db()
    db.execute("DELETE FROM tenant_services WHERE tenant_id=?", (tid,))
    for sid in request.form.getlist("service_id"):
        db.execute("INSERT INTO tenant_services(tenant_id, service_id) VALUES (?,?)", (tid, int(sid)))
    db.commit()
    flash(_("Услуги сохранены"))
    return redirect(url_for("tenant_edit", tid=tid) + "#services")


# ---------------- компании-получатели ----------------

COMPANY_FIELDS = ["name", "edrpou", "iban", "bank", "address", "signer", "signer_position", "basis", "city",
                  "act_prefix", "note"]


@app.route("/companies", methods=["GET", "POST"])
@auth.admin_required
def companies_page():
    db = get_db()
    if request.method == "POST":
        cid = request.form.get("cid", type=int)
        if request.form.get("delete") and cid:
            used = db.execute("SELECT COUNT(*) FROM tenants WHERE company_id=?", (cid,)).fetchone()[0]
            if used:
                flash(_("Компания используется в {n} договорах — сначала переназначьте их или отключите компанию").format(n=used))
            else:
                db.execute("DELETE FROM companies WHERE id=?", (cid,))
                flash(_("Компания удалена"))
        else:
            vals = [request.form.get(f, "").strip() for f in COMPANY_FIELDS]
            vals += [1 if request.form.get("vat_payer") else 0, 1 if request.form.get("active") else 0]
            cols = COMPANY_FIELDS + ["vat_payer", "active"]
            if not vals[0]:
                abort(400, _("Название обязательно"))
            if cid:
                db.execute(f"UPDATE companies SET {', '.join(c + '=?' for c in cols)} WHERE id=?", vals + [cid])
            else:
                db.execute(f"INSERT INTO companies({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
            flash(_("Компания сохранена"))
        db.commit()
        return redirect(url_for("companies_page"))
    rows = db.execute("SELECT c.*, (SELECT COUNT(*) FROM tenants t WHERE t.company_id=c.id AND t.active=1) AS used "
                      "FROM companies c ORDER BY c.active DESC, c.name").fetchall()
    return render_template("companies.html", rows=rows)


# ---------------- показания, цены месяца, услуги ----------------

@app.route("/readings", methods=["GET", "POST"])
def readings():
    db = get_db()
    period = get_period()
    if request.method == "POST":
        n_read = n_price = n_qty = 0
        for k, v in request.form.items():
            v = v.strip().replace(",", ".").replace(" ", "")
            if k.startswith("m_"):                                   # показание счётчика
                mid = int(k[2:])
                if v == "":
                    db.execute("DELETE FROM readings WHERE meter_id=? AND period=?", (mid, period))
                else:
                    db.execute("INSERT INTO readings(meter_id, period, value) VALUES (?,?,?) "
                               "ON CONFLICT(meter_id, period) DO UPDATE SET value=excluded.value",
                               (mid, period, float(v)))
                    n_read += 1
            elif k.startswith("p_"):                                 # цена услуги за месяц
                sid = int(k[2:])
                if v == "":
                    db.execute("DELETE FROM service_prices WHERE service_id=? AND period=?", (sid, period))
                else:
                    db.execute("INSERT INTO service_prices(service_id, period, price) VALUES (?,?,?) "
                               "ON CONFLICT(service_id, period) DO UPDATE SET price=excluded.price",
                               (sid, period, float(v)))
                    n_price += 1
            elif k.startswith("q_"):                                 # кол-во (вывозы мусора и т.п.)
                tid, sid = map(int, k[2:].split("_"))
                if v in ("", "0"):
                    db.execute("DELETE FROM service_usage WHERE tenant_id=? AND service_id=? AND period=?",
                               (tid, sid, period))
                else:
                    db.execute("INSERT INTO service_usage(tenant_id, service_id, period, qty) VALUES (?,?,?,?) "
                               "ON CONFLICT(tenant_id, service_id, period) DO UPDATE SET qty=excluded.qty",
                               (tid, sid, period, float(v)))
                    n_qty += 1
        db.commit()
        log("Показания сохранены", f"{calc.period_label(period)}: показаний {n_read}, цен {n_price}, количеств {n_qty}")
        flash(_("Сохранено: показаний {a}, цен {b}, количеств {c}").format(a=n_read, b=n_price, c=n_qty))
        return redirect(url_for("readings", period=period))

    # цены месяца
    svcs = services()
    price_rows = []
    for sv in svcs:
        own = db.execute("SELECT price FROM service_prices WHERE service_id=? AND period=?", (sv["id"], period)).fetchone()
        last, last_p = price_for(sv["id"], calc.prev_period(period))
        price_rows.append({"s": sv, "price": own["price"] if own else None, "last": last, "last_p": last_p})

    # счётчики
    meters = db.execute("SELECT m.*, w.name AS wh_name, t.name AS tenant_name, s.name AS svc_name, s.unit "
                        "FROM meters m JOIN warehouses w ON w.id=m.warehouse_id "
                        "JOIN services s ON s.id=m.service_id AND s.active=1 "
                        "LEFT JOIN tenants t ON t.id=w.tenant_id "
                        "WHERE m.active=1 ORDER BY w.name, s.sort").fetchall()
    rows = []
    for m in meters:
        prev, prev_p = calc.previous_value(m, period)
        cur = calc.current_value(m["id"], period)
        cons = (cur - prev) * (m["coef"] or 1) if cur is not None else None
        rows.append({"m": m, "prev": prev, "prev_p": prev_p, "cur": cur, "cons": cons})

    # количества по договорам (услуги qty)
    qty_svcs = [sv for sv in svcs if sv["mode"] == "qty"]
    tenants = db.execute("SELECT id, name FROM tenants WHERE active=1 ORDER BY name").fetchall()
    qty_rows = []
    for t in tenants:
        cells = []
        for sv in qty_svcs:
            sub = db.execute("SELECT 1 FROM tenant_services WHERE tenant_id=? AND service_id=?", (t["id"], sv["id"])).fetchone()
            u = db.execute("SELECT qty FROM service_usage WHERE tenant_id=? AND service_id=? AND period=?",
                           (t["id"], sv["id"], period)).fetchone()
            cells.append({"s": sv, "sub": bool(sub), "qty": u["qty"] if u else None})
        if any(c["sub"] for c in cells):
            qty_rows.append({"t": t, "cells": cells})

    return render_template("readings.html", period=period, rows=rows, price_rows=price_rows,
                           acts_period=calc.next_period(period) if settings().get("util_shift", "1") == "1" else period,
                           qty_svcs=qty_svcs, qty_rows=qty_rows,
                           prev_p=calc.prev_period(period), next_p=calc.next_period(period))


@app.route("/services", methods=["GET", "POST"])
def services_page():
    db = get_db()
    if request.method == "POST":
        sid = request.form.get("sid", type=int)
        if request.form.get("delete") and sid:
            name = db.execute("SELECT name FROM services WHERE id=?", (sid,)).fetchone()
            used = db.execute("SELECT COUNT(*) FROM meters WHERE service_id=?", (sid,)).fetchone()[0]
            if used:
                flash(_("У услуги {n} счётчиков — сначала удалите их или просто выключите услугу").format(n=used))
            else:
                db.execute("DELETE FROM services WHERE id=?", (sid,))
                log("Услуга удалена", name["name"] if name else sid)
                flash(_("Услуга удалена"))
        else:
            mode = request.form.get("mode")
            if mode not in SERVICE_MODES:
                abort(400)
            vals = [request.form["name"].strip(), request.form.get("unit", "").strip() or "послуга", mode,
                    request.form.get("sort", type=int) or 100, 1 if request.form.get("active") else 0]
            if sid:
                db.execute("UPDATE services SET name=?, unit=?, mode=?, sort=?, active=? WHERE id=?", vals + [sid])
            else:
                sid = db.execute("INSERT INTO services(name, unit, mode, sort, active) VALUES (?,?,?,?,?)", vals).lastrowid
                if request.form.get("subscribe_all") and mode != "meter":
                    db.execute("INSERT OR IGNORE INTO tenant_services(tenant_id, service_id) "
                               "SELECT id, ? FROM tenants WHERE active=1", (sid,))
            log("Услуга сохранена", vals[0])
            flash(_("Услуга сохранена"))
        db.commit()
        return redirect(url_for("services_page"))
    rows = db.execute("SELECT s.*, (SELECT COUNT(*) FROM meters m WHERE m.service_id=s.id) AS meters, "
                      "(SELECT COUNT(*) FROM tenant_services ts WHERE ts.service_id=s.id) AS subs "
                      "FROM services s ORDER BY s.active DESC, s.sort, s.name").fetchall()
    return render_template("services.html", rows=rows)


# ---------------- акты ----------------

@app.route("/acts")
def acts():
    """Расчёт за месяц по каждой компании: что программа насчитала, перед формированием акта."""
    db = get_db()
    period = get_period(this_month())
    tenants = db.execute("SELECT t.*, c.name AS company_name FROM tenants t LEFT JOIN companies c "
                         "ON c.id=t.company_id WHERE t.active=1 ORDER BY t.name").fetchall()
    groups = {}
    for c in companies():
        groups[c["id"]] = {"c": c, "rows": [], "tot": {"net": 0, "vat": 0, "total": 0}, "by_pt": {}}
    groups[None] = {"c": None, "rows": [], "tot": {"net": 0, "vat": 0, "total": 0}, "by_pt": {}}
    for t in tenants:
        act = db.execute("SELECT * FROM acts WHERE tenant_id=? AND period=?", (t["id"], period)).fetchone()
        lines, totals, warnings = calc.build_act(t["id"], period)
        if act and act["status"] == "final":                 # утверждённый — показываем зафиксированные цифры
            d = json.loads(act["data"])
            lines, totals, warnings = d["lines"], d["totals"], []
        g = groups.get(t["company_id"]) or groups[None]
        g["rows"].append({"t": t, "act": act, "lines": lines, "totals": totals, "warnings": warnings,
                           "changed": bool(act and act["status"] == "draft" and abs(act["total"] - totals["total"]) > 0.005)})
        for k in g["tot"]:
            g["tot"][k] = calc.r2(g["tot"][k] + totals[k])
        g["by_pt"][t["payment_type"]] = calc.r2(g["by_pt"].get(t["payment_type"], 0) + totals["total"])
    groups = [g for g in groups.values() if g["rows"]]
    grand = calc.r2(sum(g["tot"]["total"] for g in groups))
    return render_template("acts.html", period=period, groups=groups, grand=grand,
                           util_period=calc.util_period_for(period),
                           prev_p=calc.prev_period(period), next_p=calc.next_period(period))


@app.route("/acts/make", methods=["POST"])
def act_make():
    """Сформировать (или пересчитать черновик) и сразу скачать акт."""
    import io
    period = get_period()
    tid = request.form.get("tenant_id", type=int)
    fmt = request.form.get("fmt", "pdf")
    aid, warnings = calc.save_act(tid, period, final=bool(request.form.get("final")))
    act = load_act(aid)
    log("Акт сформирован", f"№{act['number']} {act['data']['tenant']['name']} за {calc.period_label(period)}, "
        f"{calc.fmt_money(act['total'])} грн")
    return _send_act(act, fmt)


@app.route("/acts/make_company", methods=["POST"])
def acts_make_company():
    """Сформировать акты всех контактов компании за месяц и скачать архивом."""
    db = get_db()
    period = get_period()
    cid = request.form.get("company_id", type=int)
    fmt = request.form.get("fmt", "pdf")
    q = "SELECT id FROM tenants WHERE active=1 AND " + ("company_id=?" if cid else "company_id IS NULL")
    ids = [r["id"] for r in db.execute(q, (cid,) if cid else ()).fetchall()]
    act_ids = [calc.save_act(tid, period)[0] for tid in ids]
    log("Акты компании сформированы", f"{len(act_ids)} шт. за {calc.period_label(period)}")
    return _zip_acts(act_ids, fmt, f"Akty_{period}_{fmt}.zip")


def load_act(aid):
    a = get_db().execute("SELECT * FROM acts WHERE id=?", (aid,)).fetchone()
    if not a:
        abort(404)
    d = dict(a)
    d["data"] = json.loads(a["data"])
    return d


@app.route("/acts/<int:aid>")
def act_view(aid):
    act = load_act(aid)
    lay = calc.act_layout(act["data"])
    return render_template("act_view.html", act=act, lay=lay,
                           words=calc.amount_in_words(act["data"]["totals"]["total"]),
                           util_words=calc.amount_in_words(lay["util_total"]))


@app.route("/acts/<int:aid>/status", methods=["POST"])
def act_status(aid):
    act = load_act(aid)
    db = get_db()
    action = request.form["action"]
    log({"final": "Акт утверждён", "draft": "Акт возвращён в черновик", "recalc": "Акт пересчитан",
         "delete": "Акт удалён"}.get(action, action), f"№{act['number']} {act['data']['tenant']['name']}")
    if action == "final":
        db.execute("UPDATE acts SET status='final' WHERE id=?", (aid,))
    elif action == "draft":
        db.execute("UPDATE acts SET status='draft' WHERE id=?", (aid,))
    elif action == "recalc":
        db.commit()
        calc.save_act(act["tenant_id"], act["period"])
    elif action == "delete":
        db.execute("DELETE FROM acts WHERE id=?", (aid,))
        db.commit()
        return redirect(url_for("acts", period=act["period"]))
    db.commit()
    return redirect(url_for("act_view", aid=aid))


def _fname(act, ext):
    name = re.sub(r"[^\w\-]+", "_", act["data"]["tenant"]["name"])[:40]
    return f"Akt_{act['number']}_{name}.{ext}"


def _send_act(act, fmt, inline=False):
    import io
    if fmt == "docx":
        return send_file(io.BytesIO(documents.act_docx(act)), as_attachment=True, download_name=_fname(act, "docx"),
                         mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return send_file(io.BytesIO(documents.act_pdf(act)), mimetype="application/pdf",
                     download_name=_fname(act, "pdf"), as_attachment=not inline)


def _zip_acts(act_ids, fmt, name):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for aid in act_ids:
            act = load_act(aid)
            data = documents.act_pdf(act) if fmt == "pdf" else documents.act_docx(act)
            folder = re.sub(r'[\\/:*?"<>|]+', "_", act["data"]["company"].get("company_name") or "Без компании")
            z.writestr(f"{folder}/{_fname(act, fmt)}", data)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=name)


@app.route("/acts/<int:aid>.pdf")
def act_pdf(aid):
    return _send_act(load_act(aid), "pdf", inline=request.args.get("dl") != "1")


@app.route("/acts/<int:aid>.docx")
def act_docx(aid):
    return _send_act(load_act(aid), "docx")


@app.route("/acts/zip")
def acts_zip():
    period = get_period()
    fmt = request.args.get("fmt", "pdf")
    ids = [r["id"] for r in get_db().execute("SELECT id FROM acts WHERE period=?", (period,)).fetchall()]
    return _zip_acts(ids, fmt, f"Akty_{period}_{fmt}.zip")


# ---------------- настройки ----------------

@app.route("/settings", methods=["GET", "POST"])
@auth.admin_required
def settings_page():
    db = get_db()
    if request.method == "POST":
        for k, v in request.form.items():
            if k.startswith("s_"):
                db.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k[2:], v.strip()))
        f = request.files.get("plan")
        if f and f.filename:
            from PIL import Image
            ext = os.path.splitext(f.filename)[1].lower() or ".png"
            name = f"plan{ext}"
            path = os.path.join(UPLOADS, name)
            f.save(path)
            with Image.open(path) as im:
                w, h = im.size
            for k, v in {"plan_image": name, "plan_w": str(w), "plan_h": str(h)}.items():
                db.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        db.commit()
        log("Настройки изменены")
        flash(_("Настройки сохранены"))
        return redirect(url_for("settings_page"))
    return render_template("settings.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
