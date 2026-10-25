"""Лінії електроживлення: таблиця показників по лінії (як у Google Таблиці), баланс ввід/сонце/лічильники."""
import json
import math

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


def resolve_src(src):
    """'m:12' / 'l:5' -> (kind, id, рядок лічильника, підпис «Офіс (Лінія 1)») або None, якщо лічильника вже нема."""
    try:
        kind, sid = src.split(":")
        sid = int(sid)
    except (ValueError, AttributeError):
        return None
    db = get_db()
    if kind == "m":
        r = db.execute("SELECT m.*, w.name AS wh, l.name AS line FROM meters m JOIN warehouses w ON w.id=m.warehouse_id "
                       "LEFT JOIN power_lines l ON l.id=m.line_id WHERE m.id=?", (sid,)).fetchone()
        if r:
            return "m", sid, r, (r["name"] or r["wh"]) + (f" №{r['serial']}" if r["serial"] else "") + \
                (f" ({r['line']})" if r["line"] else "")
    elif kind == "l":
        r = db.execute("SELECT lm.*, l.name AS line FROM line_meters lm JOIN power_lines l ON l.id=lm.line_id "
                       "WHERE lm.id=? AND lm.src IS NULL", (sid,)).fetchone()
        if r:
            return "l", sid, r, f"{r['name']} ({r['line']})"
    return None


def src_options(exclude_line=None):
    """Лічильники, з яких можна брати показники для ввідного лічильника лінії."""
    db = get_db()
    out = []
    for r in db.execute("SELECT m.id, m.name, m.serial, m.line_id, w.name AS wh, s.name AS svc, l.name AS line FROM meters m "
                        "JOIN warehouses w ON w.id=m.warehouse_id JOIN services s ON s.id=m.service_id "
                        "LEFT JOIN power_lines l ON l.id=m.line_id WHERE m.active=1 AND s.mode='meter' "
                        "ORDER BY l.sort, l.id, w.name").fetchall():
        out.append({"value": f"m:{r['id']}", "line_id": r["line_id"],
                    "label": f"{r['name'] or r['wh']} · {r['svc']}" + (f" №{r['serial']}" if r["serial"] else "")
                             + f" — {r['line'] or _('без линии')}"})
    for r in db.execute("SELECT lm.id, lm.name, lm.line_id, l.name AS line FROM line_meters lm JOIN power_lines l "
                        "ON l.id=lm.line_id WHERE lm.active=1 AND lm.src IS NULL ORDER BY l.sort, l.id, lm.name").fetchall():
        out.append({"value": f"l:{r['id']}", "line_id": r["line_id"], "label": f"{r['name']} — {r['line']}"})
    return out


def line_data(line_id, periods):
    db = get_db()
    cols = []
    # субрахунки орендарів (лічильники складів з послугою «за лічильником», закріплені за лінією)
    for m in db.execute("SELECT m.*, w.name AS wh, t.id AS tid, t.name AS tenant, s.name AS svc FROM meters m "
                        "JOIN warehouses w ON w.id=m.warehouse_id LEFT JOIN tenants t ON t.id=w.tenant_id "
                        "JOIN services s ON s.id=m.service_id WHERE m.line_id=? AND m.active=1 "
                        "ORDER BY m.sort_order IS NULL, m.sort_order, t.name IS NULL, t.name COLLATE NOCASE, w.name, m.serial",
                        (line_id,)).fetchall():
        rows = db.execute("SELECT period, value, reactive FROM readings WHERE meter_id=?", (m["id"],)).fetchall()
        cols.append({"kind": "m", "id": m["id"], "title": m["wh"], "sub": m["tenant"] or _("свободен"),
                     "serial": m["serial"], "reactive": bool(m["has_reactive"]), "role": "tenant",
                     "tid": m["tid"], "tenant": m["tenant"], "svc": m["svc"], "name": m["name"], "wid": m["warehouse_id"],
                     "data": _series(rows, m["initial_value"], m["initial_reactive"] or 0, m["coef"] or 1, periods)})
    for m in db.execute("SELECT * FROM line_meters WHERE line_id=? AND active=1 ORDER BY sort, CASE role WHEN 'own' THEN 0 "
                        "WHEN 'input' THEN 1 ELSE 2 END, name", (line_id,)).fetchall():
        src = resolve_src(m["src"]) if m["src"] else None
        if src:            # показники беруться з наявного лічильника іншої лінії (і вносяться туди ж)
            kind, sid, base, label = src
            table, idcol = ("readings", "meter_id") if kind == "m" else ("line_readings", "line_meter_id")
            rows = db.execute(f"SELECT period, value, reactive FROM {table} WHERE {idcol}=?", (sid,)).fetchall()
            cols.append({"kind": kind, "id": sid, "dkind": "l", "did": m["id"], "title": m["name"],
                         "sub": _(ROLES[m["role"]]).split(" (")[0] + " · = " + label,
                         "serial": base["serial"], "reactive": bool(m["has_reactive"]), "role": m["role"], "name": None,
                         "linked": label,
                         "data": _series(rows, base["initial_value"], base["initial_reactive"] or 0, base["coef"] or 1,
                                         periods)})
            continue
        rows = db.execute("SELECT period, value, reactive FROM line_readings WHERE line_meter_id=?", (m["id"],)).fetchall()
        cols.append({"kind": "l", "id": m["id"], "title": m["name"], "sub": _(ROLES[m["role"]]).split(" (")[0],
                     "serial": m["serial"], "reactive": bool(m["has_reactive"]), "role": m["role"], "name": None,
                     "data": _series(rows, m["initial_value"], m["initial_reactive"] or 0, m["coef"] or 1, periods)})
    # сублінії, що живляться від цієї лінії: їхні ввідні лічильники тут — споживачі (показники ті самі)
    for sl in db.execute("SELECT id, name FROM power_lines WHERE parent_id=? AND active=1 ORDER BY sort, id",
                         (line_id,)).fetchall():
        for m in db.execute("SELECT * FROM line_meters WHERE line_id=? AND role='input' AND active=1 ORDER BY sort, id",
                            (sl["id"],)).fetchall():
            src = resolve_src(m["src"]) if m["src"] else None
            if src and src[2]["line_id"] == line_id:
                continue                              # ввід сублінії і так узятий з лічильника цієї лінії — не двічі
            kind, sid, base = (src[0], src[1], src[2]) if src else ("l", m["id"], m)
            table, idcol = ("readings", "meter_id") if kind == "m" else ("line_readings", "line_meter_id")
            rows = db.execute(f"SELECT period, value, reactive FROM {table} WHERE {idcol}=?", (sid,)).fetchall()
            cols.append({"kind": kind, "id": sid, "dkind": "sub", "did": m["id"], "title": m["name"],
                         "sub": _("ввод подлинии") + f" «{sl['name']}»", "serial": base["serial"],
                         "reactive": bool(m["has_reactive"]), "role": "own", "name": None, "linked": sl["name"],
                         "subline": sl["id"],
                         "data": _series(rows, base["initial_value"], base["initial_reactive"] or 0, base["coef"] or 1,
                                         periods)})
    # реактивна: вносять лише ввідні/сонячні лічильники лінії; споживачам з галочкою «реактивна» реактив лінії
    # розподіляється пропорційно їхній активній витраті й округлюється вгору до цілих кВт·год
    for c in cols:
        c["rmode"] = ("input" if c["role"] in ("input", "solar") else "alloc") if c["reactive"] else None
    for p in periods:
        total = [c["data"][p]["rcons"] for c in cols if c["rmode"] == "input" and c["role"] == "input"
                 and c["data"][p]["rcons"] is not None]
        total = calc.kwh_round(sum(total)) if total else None
        cons = {id(c): calc.kwh_round(c["data"][p]["cons"]) for c in cols if c["rmode"] == "alloc"}
        base = sum(v for v in cons.values() if v and v > 0)
        for c in cols:
            if c["rmode"] == "alloc":
                v = cons[id(c)]
                c["data"][p]["ralloc"] = math.ceil(calc.r2(total * v / base)) if total is not None and base and v and v > 0 else \
                    (0 if total is not None and v is not None else None)
        # stored reactive of consumers is ignored now
    # баланс по місяцях; сонце — з втратами (налаштування solar_loss_pct, типово 5%)
    from db import settings as _settings
    loss = float(_settings().get("solar_loss_pct") or 5)
    ln = db.execute("SELECT has_solar FROM power_lines WHERE id=?", (line_id,)).fetchone()
    solar_kwh = {r["period"]: r["kwh"] for r in db.execute("SELECT period, kwh FROM line_solar WHERE line_id=?",
                                                              (line_id,))} if not ln or ln["has_solar"] else {}
    bal = {}
    for p in periods:
        def s(role, key="cons"):                        # кВт·год — цілі: кожен лічильник округлюється, як в акті
            vals = [calc.kwh_round(c["data"][p][key]) for c in cols if c["role"] in role and c["data"][p][key] is not None]
            return sum(vals) if vals else None
        users = s(("tenant", "own"))
        al = [c["data"][p].get("ralloc") for c in cols if c["rmode"] == "alloc" and c["data"][p].get("ralloc") is not None]
        ureact = calc.kwh_round(sum(al)) if al else None
        inp = s(("input",))
        solar_m = s(("solar",))                              # сонячні лічильники (накопичувальні), якщо є
        sk = solar_kwh.get(p)                                # вироблено за місяць, внесено напряму
        solar_raw = None if solar_m is None and sk is None else (solar_m or 0) + (sk or 0)
        solar = calc.kwh_round(solar_raw * (1 - loss / 100)) if solar_raw is not None else None
        supply = (inp or 0) + (solar or 0) if inp is not None or solar is not None else None
        diff = supply - (users or 0) if supply is not None and users is not None else None
        bal[p] = {"users": users, "ureact": ureact, "input": inp, "solar": solar, "solar_raw": solar_raw, "solar_kwh": sk,
                  "supply": supply, "diff": diff,
                  "pct": calc.r2(diff / supply * 100) if diff is not None and supply else None}
    return cols, bal


@bp.route("/lines")
def lines():
    """Стара сторінка лінії — тепер вкладка лінії в таблиці показників."""
    if request.args.get("old") != "1":
        return redirect(url_for("lines.table", line=request.args.get("line") or "", months=request.args.get("months") or 12))
    return lines_old()


def lines_old():
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
        if kind == "s":                                       # s|<line_id>|kwh|2026-09 — сонце за місяць
            v = _num(val)
            if v is None:
                db.execute("DELETE FROM line_solar WHERE line_id=? AND period=?", (int(mid), period))
            else:
                db.execute("INSERT INTO line_solar(line_id, period, kwh) VALUES (?,?,?) "
                           "ON CONFLICT(line_id, period) DO UPDATE SET kwh=excluded.kwh", (int(mid), period, v))
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

def meter_reactive(meter_id, line_id, period):
    """Частка реактиву лінії для лічильника складу за місяць: (кВт·год, частка 0..1, реактив лінії) або (None, …)."""
    cols, _bal = line_data(line_id, [period])
    total = [c["data"][period]["rcons"] for c in cols if c["role"] == "input" and c["reactive"]
             and c["data"][period]["rcons"] is not None]
    me = next((c for c in cols if c["kind"] == "m" and c["id"] == meter_id), None)
    if not me or not total:
        return None, None, None
    val = me["data"][period].get("ralloc")
    tot = sum(total)
    return val, (val / tot if tot and val is not None else None), tot


def _owners():
    """Орендарі зі складами (за абеткою) + «вільні склади»: [(tid, name, [whs])]."""
    db = get_db()
    out = []
    for t in db.execute("SELECT id, name FROM tenants WHERE active=1 ORDER BY name COLLATE NOCASE").fetchall():
        whs = [dict(w) for w in db.execute("SELECT id, name FROM warehouses WHERE tenant_id=? ORDER BY name", (t["id"],))]
        if whs:
            out.append((t["id"], t["name"], whs))
    free = [dict(w) for w in db.execute("SELECT id, name FROM warehouses WHERE tenant_id IS NULL ORDER BY name")]
    if free:
        out.append((None, None, free))
    return out


def _group(cols):
    """Колонки лічильників складів -> групи за орендарями (порядок колонок зберігається)."""
    groups = []
    for c in cols:
        key = c.get("tid")
        if not groups or groups[-1]["tid"] != key or groups[-1]["kind"] != "tenant":
            groups.append({"kind": "tenant", "tid": key, "title": c.get("tenant"), "cols": []})
        groups[-1]["cols"].append(c)
    return groups


def _mark_past(groups, periods):
    """Місяці, коли склад орендував інший орендар (або стояв вільним): {period: ім'я} — у таблиці сірим."""
    cache = {}
    for g in groups:
        for c in g["cols"]:
            if c.get("kind") != "m" or not c.get("wid"):
                continue
            tens = cache.setdefault(c["wid"], calc.tenancies(c["wid"]))
            past = {}
            if len(tens) > 1 or (tens and not tens[-1]["current"]):
                for p in periods:
                    t = calc.tenancy_for(tens, p)
                    if (t["tid"] if t else None) != c.get("tid"):
                        past[p] = (t["name"] or "—") if t else _("свободен")
            c["past"] = past
    return groups


def table_view(line_id, periods, service_id=None):
    """Вкладка таблиці: line_id — лінія (лічильники орендарів + власні лічильники лінії + баланс) або None — без лінії."""
    db = get_db()
    if line_id:
        cols, bal = line_data(line_id, periods)
        groups = _group([c for c in cols if c.get("dkind", c["kind"]) == "m"])
        own = [c for c in cols if c.get("dkind", c["kind"]) in ("l", "sub")]
        _mark_past(groups, periods)
        if own:
            groups.append({"kind": "line", "tid": None, "title": None, "cols": own})
        return groups, bal
    known = {r["id"] for r in db.execute("SELECT id FROM power_lines WHERE active=1")}
    q = ("SELECT m.*, w.name AS wh, s.name AS svc, s.sort AS ssort FROM meters m JOIN warehouses w ON w.id=m.warehouse_id "
         "JOIN services s ON s.id=m.service_id WHERE m.active=1 AND s.active=1 AND w.id=?"
         + (" AND m.service_id=?" if service_id else ""))
    cols, empty = [], []
    for oi, (tid, name, whs) in enumerate(_owners()):
        all_m = [m for w in whs for m in db.execute(q, (w["id"], service_id) if service_id else (w["id"],)).fetchall()]
        ms = [m for m in all_m if m["line_id"] not in known]
        if not ms:
            has_any = db.execute("SELECT 1 FROM meters m JOIN warehouses w ON w.id=m.warehouse_id WHERE m.active=1 AND "
                                 + ("w.tenant_id=?" if tid else "w.tenant_id IS NULL") + " LIMIT 1",
                                 (tid,) if tid else ()).fetchone()
            if tid and not has_any:
                empty.append({"kind": "tenant", "tid": tid, "title": name, "cols": []})   # з кнопкою «＋»
            continue
        for m in ms:
            rows = db.execute("SELECT period, value, reactive FROM readings WHERE meter_id=?", (m["id"],)).fetchall()
            cols.append({"kind": "m", "id": m["id"], "title": m["wh"], "svc": m["svc"], "name": m["name"],
                         "serial": m["serial"], "reactive": bool(m["has_reactive"]), "role": "tenant",
                         "rmode": "alloc" if m["has_reactive"] else None, "tid": tid, "tenant": name,
                         "wid": m["warehouse_id"],
                         "_key": (m["sort_order"] is None, m["sort_order"] or 0, oi, m["ssort"], m["serial"] or ""),
                         "data": _series(rows, m["initial_value"], 0, m["coef"] or 1, periods)})
    cols.sort(key=lambda c: c["_key"])
    return _mark_past(_group(cols), periods) + empty, None


@bp.route("/readings/table/order", methods=["POST"])
def table_order():
    """Порядок колонок після перетягування: {kind: 'm'|'l', ids: [...]}."""
    db = get_db()
    d = request.get_json(force=True) or {}
    ids = [int(x) for x in d.get("ids") or [] if str(x).isdigit()]
    table = {"m": ("meters", "sort_order"), "l": ("line_meters", "sort")}.get(d.get("kind"))
    if not table or not ids:
        abort(400)
    for i, mid in enumerate(ids):
        db.execute(f"UPDATE {table[0]} SET {table[1]}=? WHERE id=?", ((i + 1) * 10, mid))
    db.commit()
    return jsonify(ok=True)


def line_warnings(line_id):
    """Типові помилки налаштування субліній — показуємо над таблицею."""
    if not line_id:
        return []
    db = get_db()
    out = []
    ln = db.execute("SELECT * FROM power_lines WHERE id=?", (line_id,)).fetchone()
    subs = db.execute("SELECT id, name FROM power_lines WHERE parent_id=? AND active=1", (line_id,)).fetchall()
    for sl in subs:
        ins = db.execute("SELECT * FROM line_meters WHERE line_id=? AND role='input' AND active=1", (sl["id"],)).fetchall()
        if not ins:
            out.append(_("Подлиния «{s}» без вводного счётчика — добавьте его на вкладке «{s}» (роль «Вводный», свои показания), "
                         "и он появится здесь как потребитель.").format(s=sl["name"]))
        for m in ins:
            src = resolve_src(m["src"]) if m["src"] else None
            if src and src[2]["line_id"] == line_id:
                out.append(_("Вводный счётчик «{m}» подлинии «{s}» берёт показания со счётчика «{x}» этой же линии — "
                             "поэтому здесь его нет. В «Настроить линии» поставьте ему «Показания берутся с» → «свои показания».")
                           .format(m=m["name"], s=sl["name"], x=src[3]))
    if ln and ln["parent_id"]:
        for m in db.execute("SELECT * FROM line_meters WHERE line_id=? AND role='input' AND active=1 AND src IS NOT NULL",
                            (line_id,)).fetchall():
            src = resolve_src(m["src"])
            if src and src[2]["line_id"] == ln["parent_id"]:
                out.append(_("Вводный счётчик «{m}» берёт показания с «{x}» родительской линии — так подлиния повторяет "
                             "чужие показания. Поставьте «свои показания»: он сам появится на родительской линии как потребитель.")
                           .format(m=m["name"], x=src[3]))
    return out


@bp.route("/readings/table")
def table():
    from app import this_month
    db = get_db()
    n = request.args.get("months", 12, type=int)
    n = n if n in (6, 12, 24, 36) else 12
    end = request.args.get("to") or this_month()
    periods = periods_range(end, n)
    all_lines = db.execute("SELECT * FROM power_lines WHERE active=1 ORDER BY sort, id").fetchall()
    tab = request.args.get("line", "")
    line = next((l for l in all_lines if str(l["id"]) == tab), None)
    if not line and tab != "none":
        line = all_lines[0] if all_lines else None
    sid = request.args.get("service", type=int) if not line else None
    groups, bal = table_view(line["id"] if line else None, periods, sid)
    for g in groups:                                  # для шестерні: що можна видалити в групі
        g["gear"] = [{"kind": c.get("dkind", c["kind"]), "id": c.get("did", c["id"]),
                      "label": c["title"] + (f" · {c['svc']}" if c.get("svc") else ""),
                      "sub": " · ".join(x for x in (c.get("name"), f"№{c['serial']}" if c.get("serial") else "",
                                                     c.get("sub") if c["kind"] == "l" or c.get("linked") else "") if x)}
                     for c in g["cols"]]
    dates = {r["period"]: r["taken"] for r in db.execute("SELECT period, taken FROM reading_dates")}
    msvcs = db.execute("SELECT id, name FROM services WHERE mode='meter' AND active=1 ORDER BY sort, name").fetchall()
    owners = _owners()
    from db import settings as _settings
    return render_template("readings_table.html", groups=groups, bal=bal, line=line, all_lines=all_lines,
                           warns=line_warnings(line["id"] if line else None),
                           solar_loss=float(_settings().get("solar_loss_pct") or 5),
                           dates=dates, periods=periods, months=n, end=end, service=sid, msvcs=msvcs,
                           owners=[{"tid": t, "name": nm, "whs": w} for t, nm, w in owners], srcs=src_options(),
                           LROLES=ROLES)


@bp.route("/readings/table/meter", methods=["POST"])
def quick_meter():
    """Швидке додавання лічильника з таблиці показників."""
    db = get_db()
    d = request.get_json(force=True) or {}
    if d.get("type") == "line":                    # лічильник лінії без орендаря: ввідний / власний споживач / сонце
        lid = int(d["line_id"]) if str(d.get("line_id") or "").isdigit() else None
        name = (d.get("name") or "").strip()
        role = d.get("role") if d.get("role") in ROLES else "own"
        if not lid or not name:
            return jsonify(ok=False, error=_("Укажите название и линию")), 400
        src = d.get("src") or None
        if src:
            r = resolve_src(src)
            if not r or (r[0] == "m" and r[2]["line_id"] == lid) or (r[0] == "l" and r[2]["line_id"] == lid):
                return jsonify(ok=False, error=_("Нельзя взять показания со счётчика этой же линии")), 400
        try:
            vals = (_num(d.get("coef")) or 1, _num(d.get("initial_value")) or 0, _num(d.get("initial_reactive")) or 0)
        except ValueError:
            return jsonify(ok=False, error=_("Неверное число")), 400
        mid = db.execute("INSERT INTO line_meters(line_id, name, role, serial, coef, initial_value, initial_reactive, "
                         "has_reactive, src) VALUES (?,?,?,?,?,?,?,?,?)",
                         (lid, name, role, (d.get("serial") or "").strip(), *vals, 1 if d.get("has_reactive") else 0,
                          src)).lastrowid
        log("Счётчик линии добавлен", f"{name} ({_(ROLES[role]).split(' (')[0]})", commit=False)
        db.commit()
        return jsonify(ok=True, id=mid)
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


@bp.route("/readings/table/meter/delete", methods=["POST"])
def delete_meter():
    """Видалення лічильника з таблиці (шестерня) — лише з PIN адміністратора."""
    import auth
    db = get_db()
    d = request.get_json(force=True) or {}
    err = auth.check_pin(d.get("pin"))
    if err:
        return jsonify(ok=False, error=err), 403
    kind, mid = d.get("kind"), int(d.get("id") or 0)
    if kind == "m":
        m = db.execute("SELECT m.serial, w.name FROM meters m JOIN warehouses w ON w.id=m.warehouse_id WHERE m.id=?",
                       (mid,)).fetchone()
        if not m:
            abort(404)
        db.execute("DELETE FROM readings WHERE meter_id=?", (mid,))
        db.execute("DELETE FROM meters WHERE id=?", (mid,))
        log("Счётчик удалён", f"{m['name']}" + (f" №{m['serial']}" if m["serial"] else ""), commit=False)
    elif kind == "l":
        m = db.execute("SELECT name FROM line_meters WHERE id=?", (mid,)).fetchone()
        if not m:
            abort(404)
        db.execute("DELETE FROM line_readings WHERE line_meter_id=?", (mid,))
        db.execute("DELETE FROM line_meters WHERE id=?", (mid,))
        log("Счётчик линии удалён", m["name"], commit=False)
    else:
        abort(400)
    db.commit()
    return jsonify(ok=True)


@bp.route("/lines/manage", methods=["GET", "POST"])
def manage():
    db = get_db()
    if request.method == "POST":
        a = request.form.get("action")
        if request.form.get("delete") and a in ("line", "lmeter"):
            import auth
            err = auth.check_pin(request.form.get("pin"))
            if err:
                flash(err)
                return redirect(url_for("lines.manage"))
        if a == "line":
            lid = request.form.get("lid", type=int)
            name = request.form.get("name", "").strip()
            if request.form.get("delete") and lid:
                db.execute("UPDATE power_lines SET parent_id=NULL WHERE parent_id=?", (lid,))
                db.execute("UPDATE meters SET line_id=NULL WHERE line_id=?", (lid,))
                db.execute("DELETE FROM line_readings WHERE line_meter_id IN (SELECT id FROM line_meters WHERE line_id=?)",
                           (lid,))
                db.execute("DELETE FROM line_meters WHERE line_id=?", (lid,))
                db.execute("DELETE FROM power_lines WHERE id=?", (lid,))
            elif lid:
                parent = request.form.get("parent_id", type=int)
                p = parent
                while p:                                   # без циклів: лінія не може живитись від своєї сублінії
                    if p == lid:
                        flash(_("Линия не может питаться от самой себя или своей подлинии"))
                        return redirect(url_for("lines.manage"))
                    r_ = db.execute("SELECT parent_id FROM power_lines WHERE id=?", (p,)).fetchone()
                    p = r_["parent_id"] if r_ else None
                db.execute("UPDATE power_lines SET name=?, note=?, sort=?, has_solar=?, parent_id=? WHERE id=?",
                           (name, request.form.get("note", "").strip(), request.form.get("sort", type=int) or 100,
                            1 if request.form.get("has_solar") else 0, parent, lid))
            elif name:
                db.execute("INSERT INTO power_lines(name, note, sort, has_solar) VALUES (?,?,?,?)",
                           (name, request.form.get("note", "").strip(), request.form.get("sort", type=int) or 100,
                            1 if request.form.get("has_solar") else 0))
        elif a == "lmeter":
            mid = request.form.get("mid", type=int)
            vals = [request.form.get("line_id", type=int), request.form.get("name", "").strip(),
                    request.form.get("role") if request.form.get("role") in ROLES else "own",
                    request.form.get("serial", "").strip(), _num(request.form.get("coef")) or 1,
                    _num(request.form.get("initial_value")) or 0, _num(request.form.get("initial_reactive")) or 0,
                    1 if request.form.get("has_reactive") else 0]
            src = request.form.get("src") or None
            if src and (not resolve_src(src) or (src.startswith("m:") and db.execute(
                    "SELECT 1 FROM meters WHERE id=? AND line_id=?", (int(src[2:]), vals[0])).fetchone())
                    or (src.startswith("l:") and db.execute(
                    "SELECT 1 FROM line_meters WHERE id=? AND line_id=?", (int(src[2:]), vals[0])).fetchone())):
                flash(_("Нельзя взять показания со счётчика этой же линии"))
                return redirect(url_for("lines.manage"))
            if request.form.get("delete") and mid:
                db.execute("DELETE FROM line_readings WHERE line_meter_id=?", (mid,))
                db.execute("DELETE FROM line_meters WHERE id=?", (mid,))
            elif mid:
                db.execute("UPDATE line_meters SET line_id=?, name=?, role=?, serial=?, coef=?, initial_value=?, "
                           "initial_reactive=?, has_reactive=?, src=? WHERE id=?", vals + [src, mid])
            elif vals[1]:
                db.execute("INSERT INTO line_meters(line_id, name, role, serial, coef, initial_value, initial_reactive, "
                           "has_reactive, src) VALUES (?,?,?,?,?,?,?,?,?)", vals + [src])
        elif a == "solar_loss":
            from db import set_setting
            v = _num(request.form.get("solar_loss_pct"))
            if v is not None and 0 <= v < 100:
                set_setting("solar_loss_pct", str(v))
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
    from db import settings as _settings
    return render_template("lines_manage.html", all_lines=all_lines, lmeters=lmeters, tmeters=tmeters, LROLES=ROLES,
                           srcs=src_options(),
                           solar_loss=float(_settings().get("solar_loss_pct") or 5))


def init_app(app):
    app.register_blueprint(bp)
