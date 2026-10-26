"""Інтеграція з BAS Бухгалтерія 2.1 (платформа 1С 8.3, файлова база).

BAS сам забирає акти з програми: зовнішня обробка «Завантаження актів зі «Склади»» робить HTTP-запит
у локальній мережі до /bas/api/acts з токеном і створює в BAS «Реалізацію товарів і послуг» і «Рахунок на оплату
покупцю». Назовні BAS нічого не відкриває; токен лише читає акти одного об'єкта.
"""
import hmac
import io
import json
import os
import re
import secrets
import zipfile

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, request, send_file, url_for

import auth
import calc
import documents
from audit import log
from db import SITES, get_db, set_setting, settings, using_site
from i18n import _

bp = Blueprint("bas", __name__)
KIT_DIR = os.path.join(os.path.dirname(__file__), "bas_kit")


def _act_payload(act):
    """Акт у вигляді для BAS: шапка + рядки першого аркуша (оренда + компенсація), суми без ПДВ / ПДВ / з ПДВ."""
    d = act["data"]
    lay = calc.act_layout(d)
    rate = float(d["totals"].get("vat_rate") or 0)
    main = lay["main"]
    # суми з ПДВ — так само, як у PDF (оренда з ПДВ точна, компенсація = підсумок деталізації)
    other = [l for l in main if l.get("group") != "comp"]
    util_total = lay["util_total"] if rate else lay["util_sum"]
    gross_other = iter(documents._gross(other, rate, calc.r2(d["totals"]["total"] - (util_total if len(other) < len(main) else 0))))
    s = settings()
    lines = []
    for l in main:
        gp, gs = (util_total, util_total) if l.get("group") == "comp" else next(gross_other)
        is_rent = l.get("group") == "rent"
        lines.append({
            "nomenclature": (s.get("bas_nom_rent") or "Оренда приміщень") if is_rent
            else (s.get("bas_nom_util") or "Компенсація комунальних послуг"),
            "content": l["name"], "unit": l["unit"], "qty": l["qty"],
            "price": gp if rate else l["price"], "sum": gs if rate else l["sum"],
            "sum_net": l["sum"] if not rate else calc.r2(gs - calc.r2(gs * rate / (100 + rate))),
            "vat": calc.r2(gs * rate / (100 + rate)) if rate else 0,
        })
    if rate and lines:                       # ПДВ рядків разом = ПДВ акта (копійки — на найбільший рядок)
        diff = calc.r2(d["totals"]["vat"] - sum(x["vat"] for x in lines))
        if diff:
            big = max(lines, key=lambda x: abs(x["sum"]))
            big["vat"] = calc.r2(big["vat"] + diff)
            big["sum_net"] = calc.r2(big["sum"] - big["vat"])
    t, c = d["tenant"], d["company"]
    y, m = act["period"].split("-")
    return {
        "id": act["id"], "number": act["number"], "status": act["status"], "period": act["period"],
        "date": _iso_date(d.get("act_date") or ""),
        "vat_rate": rate, "prices_include_vat": bool(rate),
        "total": d["totals"]["total"], "vat": d["totals"]["vat"], "net": d["totals"]["net"],
        "payment": t.get("payment_type"),
        "organization": {"name": c.get("company_name"), "edrpou": c.get("company_edrpou")},
        "counterparty": {"name": t.get("name"), "edrpou": t.get("edrpou"), "ipn": t.get("ipn"),
                         "person": t.get("party_type") == "person", "address": t.get("address"),
                         "iban": t.get("iban"), "bank": t.get("bank"), "mfo": t.get("mfo")},
        "contract": {"number": t.get("contract_no") or "", "date": _iso_date(t.get("contract_date") or "")},
        "comment": f"Склади: акт № {act['number']} за {calc.period_label(act['period'])}",
        "lines": lines,
    }


MONTHS_GEN = {m: i + 1 for i, m in enumerate(calc.MONTHS_UK_GEN)}


def _iso_date(s):
    """«31 жовтня 2026 р.» / «01.01.2026» -> «2026-10-31»; не розпізнано — ''."""
    s = (s or "").strip()
    mt = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if mt:
        return f"{mt.group(3)}-{int(mt.group(2)):02d}-{int(mt.group(1)):02d}"
    mt = re.match(r"(\d{1,2})\s+([а-яіїєґ']+)\s+(\d{4})", s, re.I)
    if mt and mt.group(2).lower() in MONTHS_GEN:
        return f"{mt.group(3)}-{MONTHS_GEN[mt.group(2).lower()]:02d}-{int(mt.group(1)):02d}"
    return ""


@bp.route("/bas/api/acts")
def api_acts():
    """Акти місяця для BAS. ?token=…&period=YYYY-MM&edrpou=<ЄДРПОУ організації>[&site=base|office].
    Акти (чернетки) формуються/перераховуються так само, як кнопкою в «Актах»; готівкові — не віддаються."""
    site = request.args.get("site") or "base"
    if site not in SITES:
        abort(404)
    with using_site(site):
        s = settings()
        tok = s.get("bas_token") or ""
        if not tok or not hmac.compare_digest(tok, request.args.get("token") or ""):
            abort(403)
        period = request.args.get("period") or ""
        if not re.fullmatch(r"\d{4}-\d{2}", period):
            abort(400)
        edrpou = (request.args.get("edrpou") or "").strip()
        db = get_db()
        cos = db.execute("SELECT * FROM companies WHERE active=1").fetchall()
        cos = [c for c in cos if not edrpou or (c["edrpou"] or "").strip() == edrpou]
        out = []
        include_cash = s.get("bas_cash") == "1"
        for co in cos:
            for t in db.execute("SELECT id, payment_type FROM tenants WHERE active=1 AND company_id=?", (co["id"],)).fetchall():
                if t["payment_type"] == "cash" and not include_cash:
                    continue
                aid, _w = calc.save_act(t["id"], period)
                a = db.execute("SELECT * FROM acts WHERE id=?", (aid,)).fetchone()
                act = dict(a)
                act["data"] = json.loads(a["data"])
                if act["data"]["totals"]["total"]:
                    out.append(_act_payload(act))
        log("BAS забрал акты", f"{calc.period_label(period)}: {len(out)} шт." + (f", ЄДРПОУ {edrpou}" if edrpou else ""),
            username="BAS")
        return Response(json.dumps({"period": period, "site": site, "acts": out}, ensure_ascii=False),
                        mimetype="application/json; charset=utf-8")


@bp.route("/bas/settings", methods=["POST"])
@auth.perm_required("settings")
def save_settings():
    f = request.form
    if f.get("action") == "token":
        set_setting("bas_token", secrets.token_urlsafe(24))
        flash(_("Новый токен BAS создан — обновите его в обработке BAS"))
    else:
        set_setting("bas_nom_rent", (f.get("bas_nom_rent") or "Оренда приміщень").strip())
        set_setting("bas_nom_util", (f.get("bas_nom_util") or "Компенсація комунальних послуг").strip())
        set_setting("bas_cash", "1" if f.get("bas_cash") else "0")
        flash(_("Настройки сохранены"))
    get_db().commit()
    log("Настройки BAS изменены", f.get("action") or "save")
    return redirect(url_for("companies_page") + "#bas")


@bp.route("/bas/kit.zip")
@auth.perm_required("settings")
def kit():
    """Код обробки для BAS + інструкція + приклад даних."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in sorted(os.listdir(KIT_DIR)):
            z.write(os.path.join(KIT_DIR, n), n)
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name="BAS_obrobka_sklady.zip")


def init_app(app):
    app.register_blueprint(bp)
    auth.PUBLIC_ENDPOINTS.add("bas.api_acts")        # доступ — лише за токеном (перевірка в самому запиті)
