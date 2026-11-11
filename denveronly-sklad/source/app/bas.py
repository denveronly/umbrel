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
        "key": f"T{act['tenant_id']}-{act['period']}",       # стабільний ключ: орендар + місяць (не залежить від №)
        "comment": f"Склади: акт № {act['number']} за {calc.period_label(act['period'])} [T{act['tenant_id']}-{act['period']}]",
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


def collect(site, period, edrpou="", company_id=None, tenant_id=None):
    """Акти місяця для BAS (чернетки перераховуються так само, як кнопкою в «Актах»; готівкові — лише якщо дозволено)."""
    with using_site(site):
        s = settings()
        db = get_db()
        cos = db.execute("SELECT * FROM companies WHERE active=1").fetchall()
        cos = [c for c in cos if (not edrpou or (c["edrpou"] or "").strip() == edrpou)
               and (company_id is None or c["id"] == company_id)]
        out = []
        include_cash = s.get("bas_cash") == "1"
        for co in cos:
            for t in db.execute("SELECT id, payment_type FROM tenants WHERE active=1 AND company_id=?", (co["id"],)).fetchall():
                if t["payment_type"] == "cash" and not include_cash:
                    continue
                if tenant_id and t["id"] != tenant_id:
                    continue
                aid, _w = calc.save_act(t["id"], period)
                a = db.execute("SELECT * FROM acts WHERE id=?", (aid,)).fetchone()
                act = dict(a)
                act["data"] = json.loads(a["data"])
                if act["data"]["totals"]["total"]:
                    out.append(_act_payload(act))
        return out, cos


@bp.route("/bas/api/acts")
def api_acts():
    """Мережевий варіант (з токеном). ?token=…&period=YYYY-MM&edrpou=<ЄДРПОУ організації>[&site=base|office]."""
    site = request.args.get("site") or "base"
    if site not in SITES:
        abort(404)
    with using_site(site):
        tok = settings().get("bas_token") or ""
    if not tok or not hmac.compare_digest(tok, request.args.get("token") or ""):
        abort(403)
    period = request.args.get("period") or ""
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        abort(400)
    edrpou = (request.args.get("edrpou") or "").strip()
    out, _cos = collect(site, period, edrpou)
    with using_site(site):
        log("BAS забрал акты", f"{calc.period_label(period)}: {len(out)} шт." + (f", ЄДРПОУ {edrpou}" if edrpou else ""),
            username="BAS")
    return Response(json.dumps({"period": period, "site": site, "acts": out}, ensure_ascii=False),
                    mimetype="application/json; charset=utf-8")


@bp.route("/bas/export")
def export_file():
    """Файл для BAS без токена й мережі: «Акти» → «Для BAS» → файл -> обробка в BAS «Завантажити з файлу»."""
    from db import current_site
    site = current_site()
    period = request.args.get("period") or ""
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        abort(400)
    cid = request.args.get("company_id", type=int)
    out, cos = collect(site, period, company_id=cid)
    name = re.sub(r"[^\w\-]+", "_", (cos[0]["name"] if cid and cos else "usi"), flags=re.U).strip("_")
    log("Акты выгружены в файл для BAS", f"{calc.period_label(period)}: {len(out)} шт., {name}")
    get_db().commit()
    body = json.dumps({"format": "sklady-bas-acts", "version": 1, "period": period, "site": site, "acts": out},
                      ensure_ascii=False, indent=1).encode("utf-8")
    return send_file(io.BytesIO(body), mimetype="application/json", as_attachment=True,
                     download_name=f"BAS_akty_{period}_{name}.json")


# ───────── Автозавантаження: кнопка в «Склади» -> черга -> агент на ПК бухгалтерії -> BAS (фоновий сеанс) ─────────

import fcntl
import time as _time
from datetime import datetime as _dt

from db import DATA_DIR, MAIN_SITE

AG_DIR = os.path.join(DATA_DIR, "bas")


def _jpath(name):
    os.makedirs(AG_DIR, exist_ok=True)
    return os.path.join(AG_DIR, name)


def _jload(name, default):
    try:
        with open(_jpath(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _jsave(name, data):
    tmp = _jpath(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _jpath(name))


class _Lock:
    def __enter__(self):
        self.fd = os.open(_jpath(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        os.close(self.fd)


def agent_token():
    with using_site(MAIN_SITE):
        return settings().get("bas_agent_token") or ""


def agent_seen():
    a = _jload("agent.json", {})
    if a.get("time"):
        a["online"] = (_dt.now() - _dt.fromisoformat(a["time"])).total_seconds() < 180
    return a


def statuses():
    """{ключ акта T<орендар>-<місяць>: {status, doc, msg, time}} — що сталося з актом у BAS."""
    return _jload("status.json", {})


def queue():
    return _jload("queue.json", [])


def enqueue(site, period, company_id=None, tenant_id=None, who=""):
    with _Lock():
        q = queue()
        q = [j for j in q if not (j["site"] == site and j["period"] == period and j.get("company_id") == company_id
                                  and j.get("tenant_id") == tenant_id)]            # ту саму вивантаження — один раз
        jid = f"{int(_time.time() * 1000)}"
        q.append({"id": jid, "site": site, "period": period, "company_id": company_id, "tenant_id": tenant_id,
                  "created": _dt.now().isoformat(timespec="seconds"), "who": who, "taken": ""})
        _jsave("queue.json", q)
        st = statuses()
        for a in collect(site, period, company_id=company_id, tenant_id=tenant_id)[0]:
            st[a["key"]] = {"status": "queued", "doc": st.get(a["key"], {}).get("doc", ""), "msg": "",
                            "time": _dt.now().isoformat(timespec="seconds")}
        _jsave("status.json", st)
    return jid


def _agent_auth():
    tok = agent_token()
    if not tok or not hmac.compare_digest(tok, request.args.get("token") or request.headers.get("X-Token") or ""):
        abort(403)
    _jsave("agent.json", {"time": _dt.now().isoformat(timespec="seconds"), "ip": request.remote_addr,
                          "host": request.headers.get("X-Agent-Host", ""), "version": request.headers.get("X-Agent-Version", "")})


@bp.route("/bas/agent/jobs")
def agent_jobs():
    """Агент питає раз на 30 с: є завдання? Взяте, але не підтверджене за 15 хв — віддаємо знову."""
    _agent_auth()
    now = _time.time()
    with _Lock():
        q = queue()
        out = []
        for j in q:
            if j.get("taken") and now - float(j["taken"]) < 900:
                continue
            j["taken"] = str(now)
            out.append({"id": j["id"], "period": j["period"]})
        _jsave("queue.json", q)
    return jsonify({"jobs": out})


@bp.route("/bas/agent/job/<jid>.json")
def agent_job_file(jid):
    """Файл завдання для обробки BAS — той самий формат, що й «Для BAS» з «Актів»."""
    _agent_auth()
    j = next((x for x in queue() if x["id"] == jid), None)
    if not j:
        abort(404)
    out, _c = collect(j["site"], j["period"], company_id=j.get("company_id"), tenant_id=j.get("tenant_id"))
    body = json.dumps({"format": "sklady-bas-acts", "version": 1, "period": j["period"], "site": j["site"],
                       "job": jid, "acts": out}, ensure_ascii=False)
    return Response(body.encode("utf-8"), mimetype="application/json; charset=utf-8")


@bp.route("/bas/agent/result", methods=["POST"])
def agent_result():
    """Результат однієї бази BAS: [{key, status, doc, sum, msg}]; last=1 — завдання виконано, прибираємо з черги."""
    _agent_auth()
    jid = request.args.get("job") or ""
    base = (request.args.get("base") or "")[:80]
    raw = request.get_data() or b"[]"
    try:
        res = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        res = []
    if isinstance(res, dict):
        res = res.get("results") or []
    now = _dt.now().isoformat(timespec="seconds")
    with _Lock():
        st = statuses()
        for r in res if isinstance(res, list) else []:
            k = str(r.get("key") or "")
            if not k:
                continue
            st[k] = {"status": str(r.get("status") or "ok")[:20], "doc": str(r.get("doc") or "")[:60],
                     "msg": str(r.get("msg") or "")[:300], "time": now, "base": base}
        if request.args.get("last") == "1":
            q = queue()
            done = [x for x in q if x["id"] == jid]
            for x in done:                                  # акти завдання, яких не прийняла жодна база
                for a in collect(x["site"], x["period"], company_id=x.get("company_id"), tenant_id=x.get("tenant_id"))[0]:
                    if st.get(a["key"], {}).get("status") == "queued":
                        st[a["key"]] = {"status": "nobase", "doc": "", "time": now,
                                        "msg": f"у BAS немає організації з ЄДРПОУ {a['organization']['edrpou']}"}
            _jsave("queue.json", [x for x in q if x["id"] != jid])
        _jsave("status.json", st)
    return jsonify({"ok": True})


@bp.route("/bas/push", methods=["POST"])
def push():
    """Кнопка «→ BAS» в «Актах»: акти компанії (або одного орендаря) за місяць — у чергу агента."""
    from flask import g as _g
    from db import current_site
    period = request.form.get("period") or ""
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        abort(400)
    cid = request.form.get("company_id", type=int)
    tid = request.form.get("tenant_id", type=int)
    enqueue(current_site(), period, cid, tid, who=_g.user["username"] if _g.get("user") else "")
    log("Акты отправлены в BAS", f"{calc.period_label(period)}" + (f", компания {cid}" if cid else "") + (f", арендатор {tid}" if tid else ""))
    get_db().commit()
    seen = agent_seen().get("time")
    online = seen and (_dt.now() - _dt.fromisoformat(seen)).total_seconds() < 180
    flash(_("Поставлено в очередь для BAS") + (" — " + _("агент на связи, загрузится в течение минуты") if online
                                              else " — ⚠ " + _("агент BAS не на связи: загрузится, когда включится ПК бухгалтерии")))
    return redirect(url_for("acts", period=period) + (f"#t{tid}" if tid else ""))


def agent_kit_files():
    return [os.path.join(KIT_DIR, n) for n in sorted(os.listdir(KIT_DIR))]


@bp.route("/bas/settings", methods=["POST"])
@auth.perm_required("settings")
def save_settings():
    f = request.form
    if f.get("action") == "token":
        set_setting("bas_token", secrets.token_urlsafe(24))
        flash(_("Новый токен BAS создан — обновите его в обработке BAS"))
    elif f.get("action") == "agent_token":
        with using_site(MAIN_SITE):
            set_setting("bas_agent_token", secrets.token_urlsafe(24))
            get_db().commit()
        flash(_("Токен агента создан — впишите его в agent.json на ПК бухгалтерии"))
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
    for ep in ("bas.agent_jobs", "bas.agent_job_file", "bas.agent_result"):
        auth.PUBLIC_ENDPOINTS.add(ep)

    @app.context_processor
    def _bas_ctx():
        return {"bas_status": statuses, "bas_agent": agent_seen, "bas_agent_token": agent_token}
