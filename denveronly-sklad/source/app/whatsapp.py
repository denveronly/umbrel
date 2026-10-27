"""Надсилання актів орендарям у WhatsApp (WhatsApp Cloud API від Meta) — за номером телефону, без ID.

Бізнес може першим написати людині лише затвердженим у Meta шаблоном. Шаблон для актів (WhatsApp Manager →
Шаблони повідомлень): категорія «Utility», мова «Ukrainian», заголовок — «Документ», текст із 4 параметрами:
  {{1}} — назва орендаря, {{2}} — № акта, {{3}} — місяць, {{4}} — сума до оплати, грн.
Програма завантажує PDF акта (з деталізацією) у WhatsApp і надсилає шаблон із цим документом кожному
контактному особі орендаря з галочкою «WhatsApp». Токен зберігається зашифрованим (vault.py).
"""
import json
import re
import threading
import traceback
import urllib.error
import urllib.request
import uuid

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

import auth
import calc
import vault
from audit import log
from db import current_site, get_db, set_setting, settings
from i18n import _

bp = Blueprint("wa", __name__)
GRAPH = "https://graph.facebook.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_log (
    id INTEGER PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    tenant_id INTEGER, tenant TEXT, act_id INTEGER, number TEXT, period TEXT,
    phone TEXT, status TEXT,          -- sent | error
    message_id TEXT, error TEXT, username TEXT
);
"""


def ensure_schema():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


def config():
    s = settings()
    tok = s.get("wa_token") or ""
    return {"phone_id": (s.get("wa_phone_id") or "").strip(), "has_token": bool(tok),
            "token": vault.decrypt(tok) if tok else "", "template": (s.get("wa_template") or "akt_orendy").strip(),
            "lang": (s.get("wa_lang") or "uk").strip(), "version": (s.get("wa_version") or "v23.0").strip(),
            "part": s.get("wa_part") or "all"}


def ready(c=None):
    c = c or config()
    return bool(c["phone_id"] and c["token"] and c["template"])


def norm_phone(p):
    """«+38 (050) 111-22-33» / «0501112233» -> «380501112233»; не схоже на номер -> ''."""
    d = re.sub(r"\D", "", p or "")
    if len(d) == 10 and d.startswith("0"):
        d = "38" + d
    elif len(d) == 9:
        d = "380" + d
    return d if 11 <= len(d) <= 15 else ""


class WAError(Exception):
    pass


def _call(c, path, data=None, files=None):
    url = f"{GRAPH}/{c['version']}/{path}"
    headers = {"Authorization": "Bearer " + c["token"]}
    if files:                                            # multipart для завантаження PDF
        boundary = uuid.uuid4().hex
        body = b""
        for k, v in (data or {}).items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for k, (fname, content, ctype) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
                     f"Content-Type: {ctype}\r\n\r\n").encode() + content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    else:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8")).get("error", {})
            msg = err.get("error_user_msg") or err.get("message") or str(e)
            if err.get("error_data", {}).get("details"):
                msg += " — " + err["error_data"]["details"]
        except Exception:  # noqa
            msg = str(e)
        if e.code == 401:
            msg = _("Токен WhatsApp недействителен или истёк") + f" ({msg})"
        raise WAError(f"HTTP {e.code}: {msg}")
    except (urllib.error.URLError, OSError) as e:
        raise WAError(_("Нет связи с WhatsApp (graph.facebook.com)") + f": {getattr(e, 'reason', e)}")


def upload_pdf(c, filename, content):
    r = _call(c, f"{c['phone_id']}/media", {"messaging_product": "whatsapp", "type": "application/pdf"},
              files={"file": (re.sub(r'[^\w.\- «»№()]+', "_", filename), content, "application/pdf")})
    return r["id"]


def send_template(c, phone, media_id, filename, params):
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": phone, "type": "template",
               "template": {"name": c["template"], "language": {"code": c["lang"]}, "components": [
                   {"type": "header", "parameters": [{"type": "document", "document": {"id": media_id, "filename": filename}}]},
                   {"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}]}}
    r = _call(c, f"{c['phone_id']}/messages", payload)
    return (r.get("messages") or [{}])[0].get("id", "")


def recipients(tenant_id, db=None):
    """Телефони для актів: контактні особи орендаря з галочкою «WhatsApp» (для додаткового договору — і основної картки)."""
    db = db or get_db()
    t = db.execute("SELECT id, parent_id FROM tenants WHERE id=?", (tenant_id,)).fetchone()
    ids = [tenant_id] + ([t["parent_id"]] if t and t["parent_id"] else [])
    out, seen = [], set()
    for tid in ids:
        for ct in db.execute("SELECT * FROM tenant_contacts WHERE tenant_id=? ORDER BY id", (tid,)).fetchall():
            ph = norm_phone(ct["phone"])
            if ph and (ct["wa"] if "wa" in ct.keys() else 1) and ph not in seen:
                seen.add(ph)
                out.append({"phone": ph, "name": ct["name"] or ""})
    return out


def send_act(act_id, username):
    """Надіслати акт усім отримувачам. -> (надіслано, [помилки])."""
    import app as A
    ensure_schema()
    c = config()
    if not ready(c):
        raise WAError(_("WhatsApp не настроен: Настройки → WhatsApp"))
    db = get_db()
    act = A.load_act(act_id)
    rec = recipients(act["tenant_id"], db)
    tname = act["data"]["tenant"].get("name") or ""
    if not rec:
        return 0, [f"{act['data']['tenant'].get('display_name') or tname}: " + _("нет телефона с галочкой WhatsApp")]
    part = c["part"] if c["part"] in ("all", "act") else "all"
    fname = A._fname(act, "pdf", part)
    media_id = upload_pdf(c, fname, A._render(act, "pdf", part))
    params = [tname, act["number"], calc.period_label(act["period"]), calc.fmt_money(act["data"]["totals"]["total"])]
    ok, errs = 0, []
    for r in rec:
        try:
            mid = send_template(c, r["phone"], media_id, fname, params)
            db.execute("INSERT INTO wa_log(tenant_id, tenant, act_id, number, period, phone, status, message_id, username) "
                       "VALUES (?,?,?,?,?,?, 'sent', ?, ?)", (act["tenant_id"], tname, act_id, act["number"], act["period"],
                                                            r["phone"], mid, username))
            ok += 1
        except WAError as e:
            db.execute("INSERT INTO wa_log(tenant_id, tenant, act_id, number, period, phone, status, error, username) "
                       "VALUES (?,?,?,?,?,?, 'error', ?, ?)", (act["tenant_id"], tname, act_id, act["number"], act["period"],
                                                             r["phone"], str(e)[:500], username))
            errs.append(f"{tname} +{r['phone']}: {e}")
        db.commit()
    log("WhatsApp: акт отправлен", f"№{act['number']} {tname}: {ok} из {len(rec)}", username=username)
    return ok, errs


# ---------------- маршрути ----------------

@bp.route("/acts/whatsapp", methods=["POST"])
def send_one():
    """Один орендар: сформувати (перерахувати чернетку) і надіслати акт."""
    tid = request.form.get("tenant_id", type=int)
    period = request.form.get("period") or ""
    if not tid or not re.fullmatch(r"\d{4}-\d{2}", period):
        abort(400)
    aid, _w = calc.save_act(tid, period)
    try:
        ok, errs = send_act(aid, g.user["username"])
        flash(_("WhatsApp: отправлено {n}").format(n=ok) + ("" if not errs else "; " + "; ".join(errs)))
    except WAError as e:
        flash("WhatsApp: " + str(e))
    return redirect(url_for("acts", period=period))


def _bulk(app, ids, period, user_row, site, job):
    with app.app_context():
        g.user = user_row
        g.site = site
        db = get_db()
        sent = errn = 0
        for tid in ids:
            try:
                aid, _w = calc.save_act(tid, period)
                db.commit()
                ok, errs = send_act(aid, user_row["username"])
                sent += ok
                errn += len(errs)
            except Exception as e:  # noqa
                traceback.print_exc()
                errn += 1
                db.execute("INSERT INTO wa_log(tenant_id, period, status, error, username) VALUES (?,?, 'error', ?, ?)",
                           (tid, period, str(e)[:500], user_row["username"]))
                db.commit()
        set_setting("wa_last_bulk", json.dumps({"job": job, "period": period, "sent": sent, "errors": errn, "done": True},
                                               ensure_ascii=False))
        db.commit()


@bp.route("/acts/whatsapp_bulk", methods=["POST"])
def send_bulk():
    """Усім орендарям місяця (або компанії) — у фоні; результат — у журналі WhatsApp."""
    db = get_db()
    period = request.form.get("period") or ""
    scope = request.form.get("company_id", "all")
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        abort(400)
    if not ready():
        flash(_("WhatsApp не настроен: Настройки → WhatsApp"))
        return redirect(url_for("acts", period=period))
    if scope == "all":
        rows = db.execute("SELECT id FROM tenants WHERE active=1").fetchall()
    elif scope.isdigit():
        rows = db.execute("SELECT id FROM tenants WHERE active=1 AND company_id=?", (int(scope),)).fetchall()
    else:
        rows = db.execute("SELECT id FROM tenants WHERE active=1 AND company_id IS NULL").fetchall()
    ids = [r["id"] for r in rows if recipients(r["id"], db)]
    job = uuid.uuid4().hex[:8]
    set_setting("wa_last_bulk", json.dumps({"job": job, "period": period, "total": len(ids), "done": False}))
    db.commit()
    threading.Thread(target=_bulk, args=(current_app._get_current_object(), ids, period, g.user, current_site(), job),
                     daemon=True).start()
    flash(_("WhatsApp: отправка запущена ({n} арендаторов). Результат — Настройки → WhatsApp → журнал.").format(n=len(ids)))
    return redirect(url_for("acts", period=period))


@bp.route("/settings/whatsapp", methods=["GET", "POST"])
@auth.perm_required("settings")
def settings_page():
    ensure_schema()
    db = get_db()
    if request.method == "POST":
        f = request.form
        action = f.get("action", "save")
        set_setting("wa_phone_id", f.get("phone_id", "").strip())
        set_setting("wa_template", f.get("template", "").strip() or "akt_orendy")
        set_setting("wa_lang", f.get("lang", "").strip() or "uk")
        set_setting("wa_version", f.get("version", "").strip() or "v23.0")
        set_setting("wa_part", "act" if f.get("part") == "act" else "all")
        if f.get("token", "").strip():
            set_setting("wa_token", vault.encrypt(f.get("token").strip()))
        if action == "clear_token":
            set_setting("wa_token", "")
        db.commit()
        log("WhatsApp: настройки изменены", action)
        if action == "test":
            phone = norm_phone(f.get("test_phone"))
            c = config()
            try:
                if not phone:
                    raise WAError(_("Укажите номер телефона для теста"))
                # hello_world — стандартний шаблон Meta (англ.), є в кожному новому акаунті
                r = _call(c, f"{c['phone_id']}/messages", {"messaging_product": "whatsapp", "to": phone, "type": "template",
                                                           "template": {"name": "hello_world", "language": {"code": "en_US"}}})
                flash(_("Тест отправлен") + f" (+{phone}): {(r.get('messages') or [{}])[0].get('id', '')}")
            except WAError as e:
                flash(_("Тест не прошёл") + f": {e}")
        else:
            flash(_("Настройки сохранены"))
        return redirect(url_for("wa.settings_page"))
    logs = db.execute("SELECT * FROM wa_log ORDER BY id DESC LIMIT 100").fetchall()
    try:
        bulk = json.loads(settings().get("wa_last_bulk") or "{}")
    except ValueError:
        bulk = {}
    return render_template("wa_settings.html", c=config(), logs=logs, bulk=bulk)


def init_app(app):
    app.register_blueprint(bp)
    with app.app_context():
        try:
            ensure_schema()
        except Exception:  # noqa
            pass
