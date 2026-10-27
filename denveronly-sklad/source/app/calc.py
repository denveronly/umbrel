"""Розрахунки: оренда, комунальні, акти, сума прописом."""
import json
import math
import re
from decimal import Decimal, ROUND_HALF_UP

from db import get_db, settings, price_for, PAYMENT_TYPES

MONTHS_UK = ["січень", "лютий", "березень", "квітень", "травень", "червень",
             "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
MONTHS_UK_GEN = ["січня", "лютого", "березня", "квітня", "травня", "червня",
                 "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]


def r2(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def period_label(period):
    y, m = period.split("-")
    return f"{MONTHS_UK[int(m) - 1]} {y}"


def prev_period(period):
    y, m = map(int, period.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


# ---------- оренда ----------

def rent_price(wh, pt=None, s=None):
    """Оренда складу: одна ціна за м²/міс у формі оплати орендаря (для «з ПДВ» ціна вже з ПДВ)."""
    s = s or settings()
    vat = float(s["vat_rate"]) / 100
    area = wh["area"] or 0
    rate = wh["price_m2"] or 0
    total = r2(area * rate)
    # ціна за м² зберігається з 4 знаками (з суми за місяць) — прибираємо копійчаний «хвіст» округлення:
    # 252,4 м² × 170,3645 = 42 999,99 → 43 000,00
    whole = round(area * rate)
    if total != whole and abs(area * rate - whole) <= area * 0.00005 + 0.001:
        total = float(whole)
    if pt == "bank_vat" and vat:
        net = r2(total / (1 + vat))
        return {"rate": r2(rate), "total": total, "net": net, "vat": r2(total - net), "pt": pt}
    return {"rate": r2(rate), "total": total, "net": total, "vat": 0, "pt": pt}


# ---------- лічильники ----------

def previous_value(meter, period):
    row = get_db().execute(
        "SELECT value, period FROM readings WHERE meter_id=? AND period<? "
        "ORDER BY period DESC LIMIT 1", (meter["id"], period)).fetchone()
    if row:
        return row["value"], row["period"]
    return meter["initial_value"], None


def current_value(meter_id, period):
    row = get_db().execute(
        "SELECT value FROM readings WHERE meter_id=? AND period=?", (meter_id, period)).fetchone()
    return row["value"] if row else None


# ---------- акт ----------

def next_period(period):
    y, m = map(int, period.split("-"))
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def util_period_for(period, s=None):
    """Акт виставляється наперед: оренда за місяць акта, комунальні — за попередній місяць."""
    s = s or settings()
    return prev_period(period) if s.get("util_shift", "1") == "1" else period


# ---------- хто орендував склад у місяці (історія оренди) ----------

def _month_bounds(period):
    import calendar
    y, m = map(int, period.split("-"))
    return f"{period}-01", f"{period}-{calendar.monthrange(y, m)[1]:02d}"


def tenancies(wid, db=None):
    """Усі оренди складу: минулі (warehouse_tenancy) + поточна. [{tid, name, start, end, price_m2, area, current}]."""
    from db import to_iso
    db = db or get_db()
    out = []
    for h in db.execute("SELECT * FROM warehouse_tenancy WHERE warehouse_id=? ORDER BY id", (wid,)).fetchall():
        out.append({"tid": h["tenant_id"], "name": h["tenant_name"], "start": to_iso(h["started"]),
                    "end": to_iso(h["ended"]), "price_m2": h["price_m2"], "area": h["area"], "current": False})
    w = db.execute("SELECT w.*, t.name AS tname FROM warehouses w LEFT JOIN tenants t ON t.id=w.tenant_id WHERE w.id=?",
                   (wid,)).fetchone()
    if w and w["tenant_id"]:
        out.append({"tid": w["tenant_id"], "name": w["tname"], "start": to_iso(w["tenant_since"]), "end": None,
                    "price_m2": w["price_m2"], "area": w["area"], "current": True})
    if out:
        out[0]["start"] = None      # до першого відомого орендаря вважаємо, що склад був у нього (дані до ведення історії)
    return out


def tenancy_for(tens, period):
    """Оренда, що найбільше покриває місяць (за кількістю днів); при рівності — пізніша."""
    from datetime import date as _d
    ms, me = _month_bounds(period)
    best, best_days = None, 0
    for i, t in enumerate(tens):
        s = max(ms, t["start"] or "0000-01-01")
        e = min(me, t["end"] or "9999-12-31")
        if s > e:
            continue
        days = (_d.fromisoformat(e) - _d.fromisoformat(s)).days + 1
        if days > best_days or (days == best_days and best is not None):
            best, best_days = t, days
    return best


def _tenancy_index(db):
    """Склади й їхні оренди — один раз за запит (акти й економіка рахують багато орендарів/місяців)."""
    try:
        from flask import g, has_app_context
        if has_app_context():
            if getattr(g, "_tenancy_idx", None) is None:
                ws = db.execute("SELECT * FROM warehouses ORDER BY name").fetchall()
                g._tenancy_idx = [(w, tenancies(w["id"], db)) for w in ws]
            return g._tenancy_idx
    except Exception:
        pass
    return [(w, tenancies(w["id"], db)) for w in db.execute("SELECT * FROM warehouses ORDER BY name").fetchall()]


def reset_tenancy_cache():
    try:
        from flask import g
        g._tenancy_idx = None
    except Exception:
        pass


def warehouses_of(tenant_id, period, db=None):
    """Склади, які орендар мав у цьому місяці (з площею й ціною тієї оренди)."""
    db = db or get_db()
    out = []
    for w, tens in _tenancy_index(db):
        t = tenancy_for(tens, period)
        if t and t["tid"] == tenant_id:
            d = dict(w)
            if not t["current"]:
                d["area"] = t["area"] if t["area"] is not None else w["area"]
                d["price_m2"] = t["price_m2"]
            out.append(d)
    return out


# ---------- послуги «частка від загальної суми» ----------

def tenant_share(tenant_id, service_id, period, db=None):
    """% орендаря в послузі на місяць: останнє значення, що діє з місяця <= period (переходить на наступні)."""
    db = db or get_db()
    r = db.execute("SELECT pct FROM tenant_shares WHERE tenant_id=? AND service_id=? AND from_period<=? "
                   "ORDER BY from_period DESC, id DESC LIMIT 1", (tenant_id, service_id, period)).fetchone()
    return r["pct"] if r else 0


def main_values(svc, period, db=None):
    """Загальний лічильник послуги «частка» за місяць -> (показник, попередній, місяць попереднього, коеф.).
    main_src = "l:<id>" / "m:<id>" — показники беруться з лічильника лінії / складу (таблиця показників),
    інакше — свої показники послуги (вкладка «Ціни та кількості»)."""
    db = db or get_db()
    src = svc["main_src"] if "main_src" in svc.keys() else None
    if src and re.fullmatch(r"[lm]:\d+", src):
        kind, mid = src.split(":")
        tbl, col, mt = ("line_readings", "line_meter_id", "line_meters") if kind == "l" else ("readings", "meter_id", "meters")
        m = db.execute(f"SELECT * FROM {mt} WHERE id=?", (int(mid),)).fetchone()
        if m:
            cur = db.execute(f"SELECT value FROM {tbl} WHERE {col}=? AND period=?", (m["id"], period)).fetchone()
            prev = db.execute(f"SELECT value, period FROM {tbl} WHERE {col}=? AND period<? AND value IS NOT NULL "
                              "ORDER BY period DESC LIMIT 1", (m["id"], period)).fetchone()
            return (cur["value"] if cur else None, prev["value"] if prev else (m["initial_value"] or 0),
                    prev["period"] if prev else None, m["coef"] or 1)
    cur = db.execute("SELECT value FROM service_readings WHERE service_id=? AND period=?", (svc["id"], period)).fetchone()
    if svc["has_main"] == 2:                # кількість за місяць на весь комплекс (тонни пелет) — не накопичувальний
        return (cur["value"] if cur else None, 0, None, 1)
    prev = db.execute("SELECT value, period FROM service_readings WHERE service_id=? AND period<? AND value IS NOT NULL "
                      "ORDER BY period DESC LIMIT 1", (svc["id"], period)).fetchone()
    return (cur["value"] if cur else None, prev["value"] if prev else (svc["main_initial"] or 0),
            prev["period"] if prev else None, svc["main_coef"] or 1)


def share_total(svc, period, db=None):
    """Сума послуги за місяць, що ділиться за %. -> (сума або None, пояснення, витрата або None, ціна або None).
    Із загальним лічильником: витрата × ціна; без нього — ціна місяця і є вся сума."""
    db = db or get_db()
    p, _pp = price_for(svc["id"], period)
    if not svc["has_main"]:
        return (r2(p) if p is not None else None), (f"{fmt_money(p)} грн" if p is not None else ""), None, p
    cv, pv, _pp, coef = main_values(svc, period, db)
    if cv is None:
        return None, "", None, p
    cons = round((cv - pv) * coef, 3)
    if is_kwh(svc["unit"]):
        cons = kwh_round(cons)
    if p is None:
        return None, "", cons, None
    return r2(cons * p), f"{fmt_num(cons)} {svc['unit']} × {fmt_money(p)}", cons, p


def amount_for(tenant_id, service_id, period, db=None):
    """Сума вручну (прибиральниця) на місяць: внесена за цей місяць або остання раніша — переходить на наступні
    місяці, доки не змінять (зупинити — зняти галочку послуги в картці орендаря)."""
    db = db or get_db()
    row = db.execute("SELECT qty FROM service_usage WHERE tenant_id=? AND service_id=? AND period<=? "
                     "ORDER BY period DESC LIMIT 1", (tenant_id, service_id, period)).fetchone()
    return r2(row["qty"]) if row and row["qty"] else 0


def build_act(tenant_id, period):
    """Повертає (lines, totals, warnings) для орендаря за місяць акта.

    Оренда — за `period`, комунальні та експлуатаційні послуги — за util_period_for(period).
    """
    db = get_db()
    s = settings()
    tenant = db.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    pt = tenant["payment_type"]
    vat = float(s["vat_rate"]) / 100 if pt == "bank_vat" else 0
    plabel = period_label(period)
    uperiod = util_period_for(period, s)
    # склади — ті, що орендар мав саме в цих місяцях (оренда — за місяць акта, комунальні — за місяць споживання)
    whs = warehouses_of(tenant_id, period, db)
    uwhs = warehouses_of(tenant_id, uperiod, db) if uperiod != period else whs
    area = sum(w["area"] or 0 for w in uwhs)
    ulabel = period_label(uperiod)
    lines, warnings = [], []

    company = db.execute("SELECT * FROM companies WHERE id=?", (tenant["company_id"],)).fetchone() \
        if tenant["company_id"] else None
    if not company:
        warnings.append("Не вказано компанію-отримувача")
    elif pt == "bank_vat" and not company["vat_payer"]:
        warnings.append(f"Оплата з ПДВ, але «{company['name']}» не платник ПДВ")
    elif company["pay_types"] and pt not in company["pay_types"].split(","):
        warnings.append(f"«{company['name']}» не приймає форму оплати «{PAYMENT_TYPES.get(pt, pt)}»")
    if not whs and not uwhs:
        warnings.append("За орендарем не закріплено жодного складу")
    elif not whs:
        warnings.append(f"У {plabel} орендар уже не орендував склад — в акті лише комунальні за {period_label(uperiod)}")

    def price(svc):
        p, p_period = price_for(svc["id"], uperiod)
        if p is None:
            warnings.append(f"Не задано ціну: {svc['name']} за {ulabel}")
            return 0
        if p_period != uperiod:
            warnings.append(f"Ціна «{svc['name']}» взята за {period_label(p_period)}")
        return p

    # --- оренда ---
    if s.get("include_rent") == "1":
        for wh in whs:
            p = rent_price(wh, pt, s)
            rent_sum = p["net"]                              # без ПДВ; ПДВ додається в кінці акта
            rate = r2(rent_sum / wh["area"]) if wh["area"] and pt == "bank_vat" else p["rate"]
            lines.append({"group": "rent", "name": f"Оренда «{wh['name']}» за {plabel}",
                          "unit": "м²", "qty": wh["area"], "price": rate, "sum": rent_sum,
                          **({"gross": p["total"], "gross_price": p["rate"]} if pt == "bank_vat" and vat else {})})
        # об'єднати оренду кількох приміщень в один рядок під назвою обраного приміщення
        merge = tenant["rent_merge_wid"] if "rent_merge_wid" in tenant.keys() else None
        rent = [l for l in lines if l["group"] == "rent"]
        if merge and len(rent) > 1:
            main = next((w for w in whs if w["id"] == merge), whs[0])
            total = r2(sum(l["sum"] for l in rent))
            lines = [l for l in lines if l["group"] != "rent"]
            custom = ((tenant["rent_merge_name"] if "rent_merge_name" in tenant.keys() else "") or "").strip()
            gross = r2(sum(l.get("gross", 0) for l in rent)) if all("gross" in l for l in rent) else None
            lines.insert(0, {"group": "rent", "name": f"{custom or 'Оренда «' + main['name'] + '»'} за {plabel}",
                             "unit": "міс.", "qty": 1, "price": total, "sum": total, "merged": len(rent),
                             **({"gross": gross, "gross_price": gross} if gross is not None else {})})

    # --- послуги за лічильниками ---
    for wh in uwhs:
        meters = db.execute("SELECT m.*, s.name AS svc_name, s.unit, s.id AS sid, s.active AS svc_active "
                            "FROM meters m JOIN services s ON s.id=m.service_id "
                            "WHERE m.warehouse_id=? AND m.active=1 AND s.mode='meter' ORDER BY s.sort, s.name",
                            (wh["id"],)).fetchall()
        for m in meters:
            if not m["svc_active"]:
                continue
            cur = current_value(m["id"], uperiod)
            if cur is None:
                warnings.append(f"Немає показника за {ulabel}: {wh['name']} / {m['svc_name']}"
                                + (f" №{m['serial']}" if m["serial"] else ""))
                continue
            prev, _ = previous_value(m, uperiod)
            qty = r2((cur - prev) * (m["coef"] or 1))
            if is_kwh(m["unit"]):
                qty = kwh_round(qty)                      # кВт·год в актах — тільки цілі
            if qty < 0:
                warnings.append(f"Від'ємне споживання: {wh['name']} / {m['svc_name']}")
            pr = price({"id": m["sid"], "name": m["svc_name"]})
            serial = f", лічильник №{m['serial']}" if m["serial"] else ""
            lines.append({
                "group": "util",
                "name": f"{m['svc_name']} ({wh['name']}{serial}), показники {fmt_num(prev)} → {fmt_num(cur)}"
                        + (f", коеф. {fmt_num(m['coef'])}" if (m["coef"] or 1) != 1 else ""),
                "unit": m["unit"], "qty": qty, "price": pr, "sum": r2(qty * pr),
                "service": m["svc_name"], "wh": wh["name"], "serial": m["serial"] or "",
                "prev": prev, "cur": cur, "coef": m["coef"] or 1})
            # реактивна: частка реактиву лінії пропорційно активному споживанню, в кВт·год за ціною послуги
            if m["has_reactive"]:
                if not m["line_id"]:
                    warnings.append(f"Реактивна: лічильник не прив'язаний до лінії — {wh['name']}{serial}")
                    continue
                import lines as _lines
                rq, share, rtot = _lines.meter_reactive(m["id"], m["line_id"], uperiod)
                if rq is None:
                    warnings.append(f"Немає показника реактиву на вводі лінії за {ulabel} ({wh['name']}{serial})")
                elif rq > 0:
                    lines.append({
                        "group": "util",
                        "name": f"Реактивна електроенергія та втрати на лінії ({wh['name']}{serial})",
                        "unit": "кВт·год", "qty": rq, "price": pr, "sum": r2(rq * pr),
                        "service": "Реактивна електроенергія та втрати на лінії", "wh": wh["name"], "serial": m["serial"] or "",
                        "rnote": ""})

    # --- підключені послуги: кількість / площа / фіксовано ---
    subs = db.execute("SELECT s.* FROM tenant_services ts JOIN services s ON s.id=ts.service_id "
                      "WHERE ts.tenant_id=? AND s.active=1 AND s.mode NOT IN ('meter', 'share') ORDER BY s.sort, s.name",
                      (tenant_id,)).fetchall()
    for svc in subs:
        if svc["mode"] == "amount":                   # сума вручну цьому орендарю за місяць (напр. прибиральниця)
            amt = amount_for(tenant_id, svc["id"], uperiod, db)
            if amt:
                lines.append({"group": "service", "name": f"{svc['name']} за {ulabel}", "unit": "послуга", "qty": 1,
                              "price": amt, "sum": amt, "service": svc["name"], "wh": "", "serial": "", "rnote": "",
                              "only_sum": True})
            continue
        if svc["mode"] == "qty":
            row = db.execute("SELECT qty FROM service_usage WHERE tenant_id=? AND service_id=? AND period=?",
                             (tenant_id, svc["id"], uperiod)).fetchone()
            qty = row["qty"] if row else 0
            if not qty:
                continue
        elif not uwhs:
            continue                                  # складу в місяці споживання вже не було — без «за м²» і фіксованих
        elif svc["mode"] == "area":
            qty = area
        else:
            qty = 1
        pr = price(svc)
        lines.append({"group": "service", "name": f"{svc['name']} за {ulabel}",
                      "unit": svc["unit"], "qty": qty, "price": pr, "sum": r2(qty * pr),
                      "service": svc["name"], "wh": "", "serial": ""})

    # --- частки від загальної суми (вода за загальним лічильником, сміття, охорона…): % з картки орендаря ---
    if uwhs:
        for svc in db.execute("SELECT * FROM services WHERE mode='share' AND active=1 ORDER BY sort, name").fetchall():
            pct = tenant_share(tenant_id, svc["id"], uperiod, db)
            if not pct:
                continue
            total, how, cons, p = share_total(svc, uperiod, db)
            if total is None:
                warnings.append(f"«{svc['name']}» за {ulabel}: " + ("немає показника загального лічильника"
                                if svc["has_main"] and cons is None else "не задано ціну"))
                continue
            pct_s = f"{pct:g}".replace(".", ",")
            # кількість — частка орендаря: м³ від загального лічильника (вода) або частка однієї послуги (сміття)
            qty = round((cons if svc["has_main"] else 1) * pct / 100, 3)
            unit = (svc["unit"] or "м³") if svc["has_main"] else "послуга"
            lname = (f"{svc['name']} за {ulabel} — {pct_s}% від {fmt_num(cons)} {unit} × {fmt_money(p)} грн/{unit}"
                     if svc["has_main"] else f"{svc['name']} за {ulabel} — {pct_s}% × {fmt_money(p)}")
            lines.append({"group": "service", "name": lname,
                          "unit": (svc["unit"] or "м³") if svc["has_main"] else "послуга", "qty": qty, "price": p, "sum": r2(total * pct / 100),
                          "service": svc["name"], "svc_id": svc["id"], "pct": pct, "wh": "", "serial": "", "rnote": f"{pct_s}% від {fmt_num(cons)}\u00a0{unit}" if svc["has_main"] else f"{pct_s}%",
                          # без загального лічильника в деталізації — лише % і сума (без «0,20 × 9 000»)
                          "only_sum": not svc["has_main"]})

    net = r2(sum(l["sum"] for l in lines))
    if vat:
        # оренда з ПДВ — точна сума з договору (ціна за м² вже з ПДВ), решта — без ПДВ + ПДВ; без копійчаних розбіжностей
        rent_gross = sum(l["gross"] for l in lines if "gross" in l)
        rest = r2(sum(l["sum"] for l in lines if "gross" not in l))
        total = r2(rent_gross + rest + r2(rest * vat))
        vat_sum = r2(total - net)
    else:
        vat_sum, total = 0, net
    totals = {"net": net, "vat": vat_sum, "total": total,
              "vat_rate": s["vat_rate"] if vat else None}
    return lines, totals, warnings


UTIL_GROUPS = ("util", "service")


def act_layout(data):
    """Структура документа: строки першого аркуша (оренда + одна компенсація) і деталізація."""
    util = [l for l in data["lines"] if l.get("group") in UTIL_GROUPS]
    main = [l for l in data["lines"] if l.get("group") not in UTIL_GROUPS]
    util_sum = r2(sum(l["sum"] for l in util))
    if util:
        ulabel = data.get("util_period_label") or data["period_label"]
        main.append({"group": "comp", "name": f"Компенсація комунальних послуг за {ulabel}",
                     "unit": "послуга", "qty": 1, "price": util_sum, "sum": util_sum})
    vat_rate = data["totals"].get("vat_rate")
    util_vat = 0
    if vat_rate and util:
        # ПДВ деталізації = ПДВ акта − ПДВ оренди, щоб суми сходились до копійки
        rent = [l for l in data["lines"] if l.get("group") not in UTIL_GROUPS]
        rent_net = r2(sum(l["sum"] for l in rent))
        rent_vat = r2(sum((l["gross"] - l["sum"]) if "gross" in l else l["sum"] * float(vat_rate) / 100 for l in rent))
        util_vat = r2(data["totals"]["vat"] - rent_vat)
    return {"main": main, "util": util, "util_sum": util_sum, "util_vat": util_vat,
            "util_total": r2(util_sum + util_vat)}


def act_date_text(period, mode=None):
    """Дата акта: перше число місяця (за замовчуванням — акт наперед) або останнє."""
    import calendar
    mode = mode or settings().get("act_date", "first")
    y, m = map(int, period.split("-"))
    if mode == "prev_last":                      # останній день попереднього місяця (акт за листопад — 31 жовтня)
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        d = calendar.monthrange(y, m)[1]
    else:
        d = 1 if mode == "first" else calendar.monthrange(y, m)[1]
    return f"{d:02d} {MONTHS_UK_GEN[m - 1]} {y} р."


def company_snapshot(c):
    if not c:
        return {}
    return {"company_name": c["name"], "company_edrpou": c["edrpou"], "company_iban": c["iban"],
            "company_bank": c["bank"], "company_mfo": c["mfo"] if "mfo" in c.keys() else None,
            "company_ipn": c["ipn"] if "ipn" in c.keys() else None, "company_address": c["address"], "company_signer": c["signer"],
            "company_signer_position": c["signer_position"], "company_basis": c["basis"],
            "company_vat_payer": c["vat_payer"]}


NUM_DEFAULT = "{П}{ММ}{РР}-{NNN}"
_TOKEN_ALIASES = {"{P}": "{П}", "{MM}": "{ММ}", "{YY}": "{РР}", "{YYYY}": "{РРРР}", "{M}": "{М}"}


def num_format(s=None):
    s = s or settings()
    f = (s.get("act_num_format") or NUM_DEFAULT).strip() or NUM_DEFAULT
    for k, v in _TOKEN_ALIASES.items():
        f = f.replace(k, v)
    if not re.search(r"\{N+\}|\{ДОГ\}", f):
        f += "-{NNN}"
    return f, s.get("act_num_reset") or "month", max(1, int(s.get("act_num_start") or 1))


def act_date_short(period, mode=None):
    """Дата акта коротко: 31/10/26 (без нулів попереду, рік двома цифрами)."""
    import calendar
    mode = mode or settings().get("act_date", "prev_last")
    y, m = map(int, period.split("-"))
    if mode == "prev_last":
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        d = calendar.monthrange(y, m)[1]
    else:
        d = 1 if mode == "first" else calendar.monthrange(y, m)[1]
    return f"{d}/{m}/{str(y)[2:]}"


def format_act_number(fmt, prefix, period, n, contract=None):
    y, m = period.split("-")
    out = (fmt.replace("{П}", prefix or "").replace("{РРРР}", y).replace("{РР}", y[2:]).replace("{ММ}", m)
           .replace("{М}", str(int(m))))
    if "{ДОГ}" in out:
        out = out.replace("{ДОГ}", (contract or "").strip() or "б/н")
    if "{ДАТА}" in out:
        out = out.replace("{ДАТА}", act_date_short(period))
    return re.sub(r"\{(N+)\}", lambda mt: f"{n:0{len(mt.group(1))}d}", out)


def act_number_fits(number, company, period, tenant=None, exclude_id=None):
    """Чи відповідає вже виданий номер чернетки поточному шаблону (інакше при перерахунку — новий номер)."""
    fmt, _reset, _start = num_format()
    if "{ДОГ}" in fmt and not re.search(r"\{N+\}", fmt):
        want = next_act_number(company, period, tenant, exclude_id=exclude_id)
        return number == want or re.fullmatch(re.escape(want.rsplit("-", 1)[0] if re.search(r"-\d+$", want) else want)
                                               + r"(-\d+)?", number or "") is not None
    return _num_regex(fmt, company, period, tenant).match(number or "") is not None


def _num_regex(fmt, company, period, tenant=None, reset=None):
    reset = reset or num_format()[1]
    y, m = period.split("-")
    prefix = (company["act_prefix"] if company else "") or ""
    rx, i = "", 0
    while i < len(fmt):
        mt = re.match(r"\{П\}|\{РРРР\}|\{РР\}|\{ММ\}|\{М\}|\{N+\}|\{ДОГ\}|\{ДАТА\}", fmt[i:])
        if not mt:
            rx += re.escape(fmt[i])
            i += 1
            continue
        tok = mt.group(0)
        if tok == "{П}":
            rx += re.escape(prefix)
        elif tok == "{ДОГ}":
            rx += re.escape(((tenant["contract_no"] if tenant else "") or "").strip() or "б/н")
        elif tok == "{ДАТА}":
            rx += re.escape(act_date_short(period))
        elif tok == "{РРРР}":
            rx += y if reset in ("month", "year") else r"\d{4}"
        elif tok == "{РР}":
            rx += y[2:] if reset in ("month", "year") else r"\d{2}"
        elif tok == "{М}":
            rx += str(int(m)) if reset == "month" else r"\d{1,2}"
        elif tok == "{ММ}":
            rx += m if reset == "month" else r"\d{2}"
        else:
            rx += r"(?P<n>\d+)"
        i += len(tok)
    return re.compile("^" + rx + "$")


def next_act_number(company, period, tenant=None, exclude_id=None):
    """Нумерація окремо для кожної компанії за шаблоном з Налаштувань (за замовчуванням <префікс>ММРР-NNN).
    Лічильник починається заново щомісяця / щороку / ніколи — теж у налаштуваннях.
    Шаблон з {ДОГ} (номер договору) і {ДАТА} (дата акта): «26/10 31/10/26» — без лічильника."""
    fmt, reset, start = num_format()
    y, m = period.split("-")
    prefix = (company["act_prefix"] if company else "") or ""
    if "{ДОГ}" in fmt and not re.search(r"\{N+\}", fmt):
        number = format_act_number(fmt, prefix, period, 0, tenant["contract_no"] if tenant else None)
        taken = {r["number"] for r in get_db().execute("SELECT number FROM acts WHERE period=? AND tenant_id IS NOT ? AND id IS NOT ?",
                                                        (period, tenant["id"] if tenant else None, exclude_id)).fetchall()}
        base, k = number, 2
        while number in taken:                       # однаковий № договору в різних орендарів
            number = f"{base}-{k}"
            k += 1
        return number
    # регулярний вираз для пошуку вже виданих номерів у межах «періоду обнулення»
    rx = _num_regex(fmt, company, period, tenant, reset)
    q, args = "SELECT number, period FROM acts WHERE id IS NOT ? AND ", [exclude_id]
    if company:
        q += "company_id=?"
        args.append(company["id"])
    else:
        q += "company_id IS NULL"
    if reset == "month":
        q += " AND period=?"
        args.append(period)
    elif reset == "year":
        q += " AND substr(period, 1, 4)=?"
        args.append(y)
    used = [int(mt.group("n")) for r in get_db().execute(q, args).fetchall() if (mt := rx.match(r["number"] or ""))]
    n = max(max(used) + 1 if used else start, start)
    return format_act_number(fmt, prefix, period, n, tenant["contract_no"] if tenant else None)


PARTY_FIELDS = ("name", "edrpou", "ipn", "iban", "bank", "mfo", "address", "party_type", "passport_series",
                "passport_number", "passport_issued", "director_position", "basis")


def tenant_snapshot(tenant, db=None):
    """Орендар для акта. Додатковий договір (parent_id) — реквізити з основної картки, договір і склади — свої."""
    d = dict(tenant)
    base = tenant
    if "parent_id" in tenant.keys() and tenant["parent_id"]:
        db = db or get_db()
        p = db.execute("SELECT * FROM tenants WHERE id=?", (tenant["parent_id"],)).fetchone()
        if p:
            base = p
            for f in PARTY_FIELDS:
                if f in p.keys():
                    d[f] = p[f]
    d["display_name"] = tenant["name"]                       # назва в програмі
    own = (tenant["legal_name"] or "").strip() if "legal_name" in tenant.keys() else ""
    legal = (base["legal_name"] or "").strip() if "legal_name" in base.keys() else ""
    d["name"] = own or legal or base["name"]                   # в акт — повна назва ТОВ / ФОП (своя або з основної картки)
    return d


def save_act(tenant_id, period, final=False):
    db = get_db()
    tenant = db.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    existing = db.execute("SELECT * FROM acts WHERE tenant_id=? AND period=?", (tenant_id, period)).fetchone()
    if existing and existing["status"] == "final":
        return existing["id"], ["Акт уже затверджено — не перераховується"]

    company = db.execute("SELECT * FROM companies WHERE id=?", (tenant["company_id"],)).fetchone() \
        if tenant["company_id"] else None
    lines, totals, warnings = build_act(tenant_id, period)
    snapshot = {
        "lines": lines, "totals": totals,
        "tenant": tenant_snapshot(tenant, db),
        "company": company_snapshot(company),
        "city": (company["city"] if company else "") or "",
        "period_label": period_label(period),
        "util_period_label": period_label(util_period_for(period)),
        "act_date": act_date_text(period),
        "payment_label": PAYMENT_TYPES[tenant["payment_type"]],
    }
    status = "final" if final else "draft"
    data = json.dumps(snapshot, ensure_ascii=False)
    cid = company["id"] if company else None
    if existing:
        number = existing["number"]
        # змінилась компанія або шаблон нумерації (Налаштування → Компанії) — чернетка отримує новий номер
        if existing["company_id"] != cid or not act_number_fits(number, company, period, tenant, existing["id"]):
            number = next_act_number(company, period, tenant, exclude_id=existing["id"])
        db.execute("UPDATE acts SET number=?, company_id=?, data=?, total=?, payment_type=?, status=? WHERE id=?",
                   (number, cid, data, totals["total"], tenant["payment_type"], status, existing["id"]))
        act_id = existing["id"]
    else:
        cur = db.execute(
            "INSERT INTO acts(number, tenant_id, company_id, period, payment_type, data, total, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (next_act_number(company, period, tenant), tenant_id, cid, period, tenant["payment_type"],
             data, totals["total"], status))
        act_id = cur.lastrowid
    db.commit()
    return act_id, warnings


def kwh_round(x):
    """кВт·год — завжди цілі (половина — вгору)."""
    if x is None:
        return None
    x = float(x)
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def is_kwh(unit):
    return "квт" in (unit or "").lower()


def fmt_num(x, digits=None):
    if x is None:
        return ""
    if digits is None:
        if float(x) == int(float(x)):
            return f"{int(float(x)):,}".replace(",", " ")
        digits = 3 if abs(float(x) - round(float(x), 2)) > 1e-9 else 2
    return f"{float(x):,.{digits}f}".replace(",", " ").replace(".", ",")


def fmt_money(x):
    return f"{float(x):,.2f}".replace(",", " ").replace(".", ",")


# ---------- сума прописом ----------

_ONES_M = ["", "один", "два", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]
_ONES_F = ["", "одна", "дві", "три", "чотири", "п'ять", "шість", "сім", "вісім", "дев'ять"]
_TEENS = ["десять", "одинадцять", "дванадцять", "тринадцять", "чотирнадцять", "п'ятнадцять",
          "шістнадцять", "сімнадцять", "вісімнадцять", "дев'ятнадцять"]
_TENS = ["", "", "двадцять", "тридцять", "сорок", "п'ятдесят", "шістдесят", "сімдесят",
         "вісімдесят", "дев'яносто"]
_HUNDREDS = ["", "сто", "двісті", "триста", "чотириста", "п'ятсот", "шістсот", "сімсот",
             "вісімсот", "дев'ятсот"]
# (форми для 1, 2-4, 5+), рід
_SCALES = [
    (("гривня", "гривні", "гривень"), "f"),
    (("тисяча", "тисячі", "тисяч"), "f"),
    (("мільйон", "мільйони", "мільйонів"), "m"),
    (("мільярд", "мільярди", "мільярдів"), "m"),
]


def _plural(n, forms):
    n = n % 100
    if 11 <= n <= 19:
        return forms[2]
    n = n % 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]


def _triad(n, gender):
    words = []
    h, rest = divmod(n, 100)
    if h:
        words.append(_HUNDREDS[h])
    if 10 <= rest <= 19:
        words.append(_TEENS[rest - 10])
    else:
        t, o = divmod(rest, 10)
        if t:
            words.append(_TENS[t])
        if o:
            words.append((_ONES_F if gender == "f" else _ONES_M)[o])
    return words


def amount_in_words(amount):
    amount = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    uah = int(amount)
    kop = int((amount - uah) * 100)
    if uah == 0:
        words = ["нуль", "гривень"]
    else:
        words, i, n = [], 0, uah
        parts = []
        while n > 0:
            n, tri = divmod(n, 1000)
            parts.append(tri)
        for i in range(len(parts) - 1, -1, -1):
            tri = parts[i]
            forms, gender = _SCALES[i]
            if tri == 0 and i != 0:
                continue
            words += _triad(tri, gender)
            words.append(_plural(tri, forms))
    text = " ".join(words)
    text = text[0].upper() + text[1:]
    return f"{text} {kop:02d} {_plural(kop, ('копійка', 'копійки', 'копійок'))}"
