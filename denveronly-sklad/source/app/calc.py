"""Розрахунки: оренда, комунальні, акти, сума прописом."""
import json
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

def rent_prices(wh, s=None):
    """Вартість оренди складу у трьох варіантах оплати (ставки за м² і сума за місяць).

    Кожну ставку можна задати окремо; не задані рахуються автоматично:
    з ПДВ = безнал × (1 + ПДВ), готівка = безнал × коефіцієнт з налаштувань.
    """
    s = s or settings()
    vat = float(s["vat_rate"]) / 100
    area = wh["area"] or 0
    rate_bank = wh["rate_bank"] or 0
    rate_vat = wh["rate_vat"] if wh["rate_vat"] is not None else rate_bank * (1 + vat)
    rate_cash = wh["rate_cash"] if wh["rate_cash"] is not None else rate_bank * float(s["cash_coef"])
    gross = r2(area * rate_vat)
    net = r2(gross / (1 + vat)) if vat else gross
    return {
        "bank": {"rate": r2(rate_bank), "total": r2(area * rate_bank)},
        "bank_vat": {"rate": r2(rate_vat), "net": net, "vat": r2(gross - net), "total": gross,
                     "manual": wh["rate_vat"] is not None},
        "cash": {"rate": r2(rate_cash), "total": r2(area * rate_cash), "manual": wh["rate_cash"] is not None},
    }


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


def build_act(tenant_id, period):
    """Повертає (lines, totals, warnings) для орендаря за місяць акта.

    Оренда — за `period`, комунальні та експлуатаційні послуги — за util_period_for(period).
    """
    db = get_db()
    s = settings()
    tenant = db.execute("SELECT * FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    pt = tenant["payment_type"]
    vat = float(s["vat_rate"]) / 100 if pt == "bank_vat" else 0
    whs = db.execute("SELECT * FROM warehouses WHERE tenant_id=? ORDER BY name", (tenant_id,)).fetchall()
    area = sum(w["area"] or 0 for w in whs)
    plabel = period_label(period)
    uperiod = util_period_for(period, s)
    ulabel = period_label(uperiod)
    lines, warnings = [], []

    company = db.execute("SELECT * FROM companies WHERE id=?", (tenant["company_id"],)).fetchone() \
        if tenant["company_id"] else None
    if not company:
        warnings.append("Не вказано компанію-отримувача")
    elif pt == "bank_vat" and not company["vat_payer"]:
        warnings.append(f"Оплата з ПДВ, але «{company['name']}» не платник ПДВ")
    if not whs:
        warnings.append("За орендарем не закріплено жодного складу")

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
            p = rent_prices(wh, s)
            if pt == "bank_vat":
                rent_sum = p["bank_vat"]["net"]          # без ПДВ, ПДВ додається в кінці акта
                rate = r2(rent_sum / wh["area"]) if wh["area"] else 0
            else:
                rate, rent_sum = p[pt]["rate"], p[pt]["total"]
            lines.append({"group": "rent", "name": f"Оренда складу «{wh['name']}» за {plabel}",
                          "unit": "м²", "qty": wh["area"], "price": rate, "sum": rent_sum})

    # --- послуги за лічильниками ---
    for wh in whs:
        meters = db.execute("SELECT m.*, s.name AS svc_name, s.unit, s.id AS sid, s.active AS svc_active "
                            "FROM meters m JOIN services s ON s.id=m.service_id "
                            "WHERE m.warehouse_id=? AND m.active=1 ORDER BY s.sort, s.name", (wh["id"],)).fetchall()
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

    # --- підключені послуги: кількість / площа / фіксовано ---
    subs = db.execute("SELECT s.* FROM tenant_services ts JOIN services s ON s.id=ts.service_id "
                      "WHERE ts.tenant_id=? AND s.active=1 AND s.mode<>'meter' ORDER BY s.sort, s.name",
                      (tenant_id,)).fetchall()
    for svc in subs:
        if svc["mode"] == "qty":
            row = db.execute("SELECT qty FROM service_usage WHERE tenant_id=? AND service_id=? AND period=?",
                             (tenant_id, svc["id"], uperiod)).fetchone()
            qty = row["qty"] if row else 0
            if not qty:
                continue
        elif svc["mode"] == "area":
            qty = area
        else:
            qty = 1
        pr = price(svc)
        lines.append({"group": "service", "name": f"{svc['name']} за {ulabel}",
                      "unit": svc["unit"], "qty": qty, "price": pr, "sum": r2(qty * pr),
                      "service": svc["name"], "wh": "", "serial": ""})

    net = r2(sum(l["sum"] for l in lines))
    vat_sum = r2(net * vat)
    totals = {"net": net, "vat": vat_sum, "total": r2(net + vat_sum),
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
        rent_net = r2(sum(l["sum"] for l in data["lines"] if l.get("group") not in UTIL_GROUPS))
        util_vat = r2(data["totals"]["vat"] - r2(rent_net * float(vat_rate) / 100))
    return {"main": main, "util": util, "util_sum": util_sum, "util_vat": util_vat,
            "util_total": r2(util_sum + util_vat)}


def act_date_text(period, mode=None):
    """Дата акта: перше число місяця (за замовчуванням — акт наперед) або останнє."""
    import calendar
    mode = mode or settings().get("act_date", "first")
    y, m = map(int, period.split("-"))
    d = 1 if mode == "first" else calendar.monthrange(y, m)[1]
    return f"{d:02d} {MONTHS_UK_GEN[m - 1]} {y} р."


def company_snapshot(c):
    if not c:
        return {}
    return {"company_name": c["name"], "company_edrpou": c["edrpou"], "company_iban": c["iban"],
            "company_bank": c["bank"], "company_address": c["address"], "company_signer": c["signer"],
            "company_signer_position": c["signer_position"], "company_basis": c["basis"],
            "company_vat_payer": c["vat_payer"]}


def next_act_number(company, period):
    """Нумерація окремо для кожної компанії: <префікс>ММРР-NNN."""
    y, m = period.split("-")
    prefix = (company["act_prefix"] if company else "") or ""
    base = f"{prefix}{m}{y[2:]}-"
    rows = get_db().execute("SELECT number FROM acts WHERE number LIKE ? AND period=?",
                            (base + "%", period)).fetchall()
    used = [int(r["number"][len(base):]) for r in rows if r["number"][len(base):].isdigit()]
    return f"{base}{(max(used) if used else 0) + 1:03d}"


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
        "tenant": dict(tenant),
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
        if existing["company_id"] != cid:          # змінилась компанія — новий номер у її нумерації
            number = next_act_number(company, period)
        db.execute("UPDATE acts SET number=?, company_id=?, data=?, total=?, payment_type=?, status=? WHERE id=?",
                   (number, cid, data, totals["total"], tenant["payment_type"], status, existing["id"]))
        act_id = existing["id"]
    else:
        cur = db.execute(
            "INSERT INTO acts(number, tenant_id, company_id, period, payment_type, data, total, status) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (next_act_number(company, period), tenant_id, cid, period, tenant["payment_type"],
             data, totals["total"], status))
        act_id = cur.lastrowid
    db.commit()
    return act_id, warnings


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
