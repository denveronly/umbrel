"""Таблиця орендарів «як в Excel»: рядок на кожен склад, групи за підкатегоріями (Ангари, Навіси…),
три вартості (з ПДВ / без ПДВ / готівка) і ціна за м². Клітинки редагуються прямо в таблиці.
Також: керування підкатегоріями та вивантаження в .xlsx."""
import io
from datetime import date

from flask import Blueprint, abort, flash, jsonify, redirect, request, send_file, url_for

import calc
from audit import log
from db import PAYMENT_TYPES, categories, get_db, settings
from i18n import _

bp = Blueprint("sheet", __name__)
NO_CAT = None
COST_COLS = ("bank_vat", "bank", "cash")          # порядок колонок: з ПДВ, без ПДВ, готівка
SUMS = ("area", "adj", "bank_vat", "bank", "cash")


def build(q="", cat=None):
    """Групи таблиці: [{"cat": row|None, "name", "rows": [...], "tot": {...}}] і загальні підсумки."""
    db = get_db()
    s = settings()
    cats = categories()
    groups = {c["id"]: {"cat": c, "id": c["id"], "name": c["name"], "rows": []} for c in cats}
    groups[NO_CAT] = {"cat": None, "id": "", "name": _("Без подкатегории"), "rows": []}

    tenants = {t["id"]: t for t in db.execute(
        "SELECT t.*, c.name AS company_name FROM tenants t LEFT JOIN companies c ON c.id=t.company_id "
        "WHERE t.active=1 ORDER BY t.name COLLATE NOCASE").fetchall()}
    contacts = {}
    for c in db.execute("SELECT * FROM tenant_contacts ORDER BY id"):
        contacts.setdefault(c["tenant_id"], []).append(c)
    whs = db.execute("SELECT * FROM warehouses ORDER BY name COLLATE NOCASE").fetchall()
    by_tenant = {}
    for w in whs:
        if w["tenant_id"] in tenants:
            by_tenant.setdefault(w["tenant_id"], []).append(w)

    def row(t, w):
        pt = t["payment_type"] if t else None
        price = calc.rent_price(w, pt, s) if w else None
        cost = price["total"] if (price and t) else 0
        cts = contacts.get(t["id"], []) if t else []
        hay = " ".join(str(x or "") for x in (
            [t["name"], t["edrpou"], t["contract_no"], t["company_name"], t["note"], t["contact"], t["activity"]]
            if t else []) + ([w["name"], w["note"]] if w else []) + [f"{c['name']} {c['phone']}" for c in cts]).lower()
        r = {"t": t, "w": w, "contacts": cts, "pt": pt, "price": price, "m2": (w["price_m2"] if w else None),
             "area": (w["area"] or 0) if w else 0, "adj": (w["adjacent_area"] or 0) if w else 0, "hay": hay}
        for k in COST_COLS:
            r[k] = cost if pt == k else 0
        return r

    for tid, t in tenants.items():
        for w in by_tenant.get(tid) or [None]:
            groups[w["category_id"] if w and w["category_id"] in groups else NO_CAT]["rows"].append(row(t, w))
    for w in whs:
        if w["tenant_id"] not in tenants:
            groups[w["category_id"] if w["category_id"] in groups else NO_CAT]["rows"].append(row(None, w))

    ql = (q or "").strip().lower()
    out = []
    for g in groups.values():
        rows = [r for r in g["rows"] if not ql or ql in r["hay"] or ql in g["name"].lower()]
        # орендарі за назвою, їхні склади поруч; вільні склади — в кінці групи
        rows.sort(key=lambda r: (r["t"] is None, (r["t"]["name"].lower() if r["t"] else ""),
                                 (r["w"]["name"].lower() if r["w"] else "")))
        if not rows and (g["cat"] is None or ql):
            continue
        if cat not in (None, "") and str(g["id"]) != str(cat):       # вкладка підкатегорії
            continue
        g["rows"] = rows
        g["tot"] = _totals(rows)
        out.append(g)

    # нумерація як у таблиці: 1, 2.1, 2.2, 3… (номер орендаря + номер його складу, якщо складів кілька)
    nwh = {}
    for g in out:
        for r in g["rows"]:
            if r["t"]:
                nwh[r["t"]["id"]] = nwh.get(r["t"]["id"], 0) + 1
    num, seen, sub = 0, {}, {}
    for g in out:
        prev = None
        for r in g["rows"]:
            t = r["t"]
            if t:
                if t["id"] not in seen:
                    num += 1
                    seen[t["id"]] = num
                sub[t["id"]] = sub.get(t["id"], 0) + 1
                r["no"] = f"{seen[t['id']]}.{sub[t['id']]}" if nwh[t["id"]] > 1 else str(seen[t["id"]])
                r["first"] = not prev or not prev["t"] or prev["t"]["id"] != t["id"]
            else:
                r["no"], r["first"] = "", True
            prev = r
    grand = _totals([r for g in out for r in g["rows"]])
    return out, grand


def _totals(rows):
    tot = {k: calc.r2(sum(r[k] for r in rows)) for k in SUMS}
    tot["rented"] = calc.r2(sum(r["area"] for r in rows if r["t"]))
    money = sum(tot[k] for k in COST_COLS)
    tot["m2"] = calc.r2(money / tot["rented"]) if tot["rented"] else None    # середня ціна здачі за м²
    return tot


# ---------------- збереження змін з таблиці ----------------

def _num(v):
    v = str(v if v is not None else "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        raise ValueError(_("Некорректное число") + f": {v}")


def _tenant_by_name(name, pt):
    """Активний орендар з такою назвою (без урахування регістру) або новий."""
    db = get_db()
    name = name.strip()
    for t in db.execute("SELECT * FROM tenants WHERE active=1").fetchall():
        if t["name"].strip().lower() == name.lower():
            return t["id"], False
    # якщо компанія-отримувач одна — одразу її, інакше вибирають у картці
    cos = db.execute("SELECT id FROM companies WHERE active=1").fetchall()
    tid = db.execute("INSERT INTO tenants(name, payment_type, company_id) VALUES (?,?,?)",
                     (name, pt or "bank", cos[0]["id"] if len(cos) == 1 else None)).lastrowid
    log("Арендатор создан из таблицы", name, commit=False)
    return tid, True


def _apply(wid, f, ev):
    """Змінити один склад: f — словник полів з таблиці (tenant, area, adj, m2, bank_vat, bank, cash)."""
    from app import set_price
    db = get_db()
    w = db.execute("SELECT * FROM warehouses WHERE id=?", (wid,)).fetchone()
    if not w:
        raise ValueError(_("Склад не найден"))
    costs = {k: _num(f[k]) for k in COST_COLS if k in f and _num(f[k]) is not None}
    attached = False                     # склад щойно закріплено за вже наявним орендарем
    if f.get("tenant", "").strip() and not w["tenant_id"]:
        tid, new = _tenant_by_name(f["tenant"], next(iter(costs), None))
        db.execute("UPDATE warehouses SET tenant_id=? WHERE id=?", (tid, wid))
        ev.append(("＋ " if new else "→ ") + f"{f['tenant'].strip()}: {w['name']}")
        attached = not new
    if "area" in f:
        a = _num(f["area"])
        if a is None or a < 0:
            raise ValueError(_("Площадь склада обязательна") + f": {w['name']}")
        db.execute("UPDATE warehouses SET area=? WHERE id=?", (a, wid))
    if "adj" in f:
        db.execute("UPDATE warehouses SET adjacent_area=? WHERE id=?", (_num(f["adj"]), wid))
    w = db.execute("SELECT w.*, t.payment_type, t.name AS tname FROM warehouses w LEFT JOIN tenants t ON t.id=w.tenant_id "
                   "WHERE w.id=?", (wid,)).fetchone()
    if costs and w["tenant_id"]:
        pt, total = next(iter(costs.items()))
        if pt != w["payment_type"] and attached:
            raise ValueError(_("У арендатора «{n}» форма оплаты «{p}» — впишите сумму в эту колонку или смените форму в карточке")
                             .format(n=w["tname"], p=PAYMENT_TYPES.get(w["payment_type"])))
        if pt != w["payment_type"]:
            db.execute("UPDATE tenants SET payment_type=? WHERE id=?", (pt, w["tenant_id"]))
            log("Форма оплаты изменена", f"{w['tname']}: {PAYMENT_TYPES.get(w['payment_type'])} → {PAYMENT_TYPES[pt]}",
                commit=False)
            ev.append(f"⇄ {w['tname']}: {PAYMENT_TYPES[pt]}")
        if not w["area"]:
            raise ValueError(_("Сначала укажите площадь") + f": {w['name']}")
        set_price(wid, round(total / w["area"], 4))
    elif "m2" in f and _num(f["m2"]) is not None:
        set_price(wid, _num(f["m2"]))


@bp.route("/tenants/sheet/save", methods=["POST"])
def save():
    db = get_db()
    data = request.get_json(force=True) or {}
    per, ev, errors = {}, [], []
    for key, val in (data.get("cells") or {}).items():
        kind, wid, field = (key.split("|") + ["", ""])[:3]       # w|12|area
        if kind != "w" or not wid.isdigit() or field not in ("tenant", "area", "adj", "m2") + COST_COLS:
            abort(400)
        per.setdefault(int(wid), {})[field] = val
    for wid, f in per.items():
        try:
            _apply(wid, f, ev)
        except ValueError as e:
            errors.append(str(e))
    # нові рядки: орендар + склад (новий або вільний з такою назвою) у підкатегорії групи
    for it in data.get("new") or []:
        try:
            tname, wname = (it.get("tenant") or "").strip(), (it.get("wh") or "").strip()
            if not wname:                       # лише орендар — склад закріпимо пізніше
                if tname:
                    tid, new = _tenant_by_name(tname, next((k for k in COST_COLS if it.get(k)), None))
                    ev.append(("＋ " if new else "= ") + tname)
                continue
            cat = int(it["cat"]) if str(it.get("cat") or "").isdigit() else None
            w = next((r for r in db.execute("SELECT * FROM warehouses").fetchall()
                      if r["name"].strip().lower() == wname.lower()), None)
            if w and w["tenant_id"] and tname:
                raise ValueError(_("Склад уже занят") + f": {w['name']}")
            if not w:
                wid = db.execute("INSERT INTO warehouses(name, area, category_id) VALUES (?,?,?)",
                                 (wname, _num(it.get("area")) or 0, cat)).lastrowid
                log("Склад создан из таблицы", wname, commit=False)
                ev.append(f"＋ {_('Склад')} {wname}")
            else:
                wid = w["id"]
                if cat and not w["category_id"]:
                    db.execute("UPDATE warehouses SET category_id=? WHERE id=?", (cat, wid))
            f = {k: it[k] for k in ("tenant", "area", "adj", "m2") + COST_COLS if it.get(k) not in (None, "")}
            _apply(wid, f, ev)
        except ValueError as e:
            errors.append(str(e))
    if errors:
        db.rollback()
        return jsonify(ok=False, errors=errors), 400
    db.commit()
    n = len(per) + len(data.get("new") or [])
    if n:
        log("Таблица арендаторов изменена", f"{n} " + _("строк") + ("; " + "; ".join(ev) if ev else "")[:400])
    return jsonify(ok=True, saved=n, events=ev)


# ---------------- підкатегорії ----------------

@bp.route("/categories", methods=["POST"])
def manage():
    db = get_db()
    a = request.form.get("action")
    cid = request.form.get("cid", type=int)
    name = request.form.get("name", "").strip()
    if a == "add" and name:
        db.execute("INSERT INTO warehouse_categories(name, sort) VALUES (?,?)",
                   (name, request.form.get("sort", type=int) or 100))
        log("Подкатегория добавлена", name, commit=False)
        flash(_("Подкатегория добавлена") + f": {name}")
    elif a == "save" and cid:
        if request.form.get("delete"):
            old = db.execute("SELECT name FROM warehouse_categories WHERE id=?", (cid,)).fetchone()
            db.execute("UPDATE warehouses SET category_id=NULL WHERE category_id=?", (cid,))
            db.execute("DELETE FROM warehouse_categories WHERE id=?", (cid,))
            log("Подкатегория удалена", old["name"] if old else cid, commit=False)
            flash(_("Подкатегория удалена, её склады — без подкатегории"))
        elif name:
            db.execute("UPDATE warehouse_categories SET name=?, sort=? WHERE id=?",
                       (name, request.form.get("sort", type=int) or 100, cid))
            log("Подкатегория изменена", name, commit=False)
            flash(_("Сохранено"))
    elif a == "assign":
        n = 0
        valid = {c["id"] for c in categories()}
        for k, v in request.form.items():
            if k.startswith("wc_"):
                new = int(v) if v and int(v) in valid else None
                cur = db.execute("SELECT category_id FROM warehouses WHERE id=?", (int(k[3:]),)).fetchone()
                if cur and cur["category_id"] != new:
                    db.execute("UPDATE warehouses SET category_id=? WHERE id=?", (new, int(k[3:])))
                    n += 1
        if n:
            log("Склады распределены по подкатегориям", str(n), commit=False)
        flash(_("Сохранено") + f": {n}")
    db.commit()
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(url_for("tenants", cats=1) + "#cats")


# ---------------- вивантаження в Excel ----------------

@bp.route("/tenants.xlsx")
def xlsx():
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    groups, grand = build(request.args.get("q", ""), request.args.get("cat"))
    wb = Workbook()
    ws = wb.active
    ws.title = _("Арендаторы")[:31]
    head = ["№", _("Арендатор"), _("Склад"), _("Площадь, м²"), _("Прилегающая, м²"), _("С ПДВ"), _("Без ПДВ"),
            _("Наличка"), _("Цена за м²"), _("Вид деятельности"), _("Договор"), _("до даты"), _("Контактное лицо"),
            _("Компания")]
    widths = [6, 32, 18, 11, 12, 14, 14, 14, 11, 24, 18, 14, 28, 20]
    money_cols = (6, 7, 8, 9)
    thin, thick = Side(style="thin", color="999999"), Side(style="medium", color="000000")
    blue = PatternFill("solid", fgColor="C9DAF8")
    wrap = Alignment(wrap_text=True, vertical="bottom")
    money = '#,##0.00'
    ws.append(head)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
        c = ws.cell(row=1, column=i)
        c.font = Font(bold=True)
        c.alignment = Alignment(wrap_text=True, vertical="bottom")
        c.border = Border(bottom=thick)
    ws.freeze_panes = "C2"

    def total_row(label, tot, fill=None, size=12):
        ws.append([None, label, None, tot["area"], tot["adj"] or None, tot["bank_vat"], tot["bank"], tot["cash"],
                   tot["m2"]] + [None] * 5)
        r = ws.max_row
        for i in range(1, len(head) + 1):
            c = ws.cell(row=r, column=i)
            if fill:
                c.fill = fill
            c.font = Font(bold=True, size=size if i in (2,) + money_cols else 11)
            c.border = Border(top=thick, bottom=thick)
        for i in money_cols:
            ws.cell(row=r, column=i).number_format = money

    for g in groups:
        total_row(g["name"], g["tot"], blue, 13)
        for x in g["rows"]:
            t, w = x["t"], x["w"]
            cts = "\n".join(f"{c['name'] or ''} {c['phone'] or ''}".strip() for c in x["contacts"])
            contract = " ".join(p for p in [f"№ {t['contract_no']}" if t and t["contract_no"] else "",
                                            (t["contract_date"] or "") if t else ""] if p)
            ws.append([x["no"], t["name"] if t else _("свободен"), w["name"] if w else "", x["area"] if w else None,
                       x["adj"] or None, x["bank_vat"] or None, x["bank"] or None, x["cash"] or None, x["m2"],
                       (t["activity"] or t["note"] or "") if t else "", contract,
                       (t["contract_end"] if t else "") or "", cts, (t["company_name"] if t else "") or ""])
            r = ws.max_row
            for i in range(1, len(head) + 1):
                c = ws.cell(row=r, column=i)
                c.alignment = wrap
                c.border = Border(top=thick if x["first"] else thin, bottom=thin, left=thin, right=thin)
                if not t:
                    c.font = Font(color="888888", italic=True)
            for i in money_cols:
                ws.cell(row=r, column=i).number_format = money
    total_row(_("Итого"), grand)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"Orendari_{date.today():%Y-%m-%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def init_app(app):
    app.register_blueprint(bp)
