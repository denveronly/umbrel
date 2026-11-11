"""Інтернет-провайдер (особистий кабінет, напр. «Цифра» my.cyfra.ua): щодня заходить у кабінет, бере баланс;
якщо баланс у мінусі (заборгованість) — сповіщає в Telegram, як YASNO.

Вхід — логін/пароль із налаштувань (пароль зашифровано vault.py). Розбір сторінки — загальний: шукаємо
«Баланс/Залишок/На рахунку … -123,45». Останні сторінки кабінету зберігаються для діагностики (без пароля),
щоб, якщо провайдер змінить сайт, можна було поправити розбір.
"""
import io
import json
import os
import re
import threading
import time
import zipfile
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import auth
import vault
import yasno
from db import DATA_DIR, MAIN_SITE, SITES, current_site, get_db, set_setting, settings, site_dir, using_site
from i18n import _

bp = Blueprint("isp", __name__)
DEFAULT_URL = "https://my.cyfra.ua"
_locks = {s: threading.Lock() for s in SITES}


class Cabinet(yasno.Cabinet):
    """Той самий клієнт, що й для YASNO (форма з паролем, cookie, CSRF), але «увійшли» визначаємо ширше."""

    def _logged_in(self, html, url=""):
        return not self._is_login_page(url, html) and bool(
            re.search(r'logout|sign_out|exit|Вийти|Вихід|Выход|Особовий рахунок|Баланс', html, re.I)) and \
            not any(i["type"] == "password" for f in yasno.parse(html).forms for i in f["inputs"])


def _text(html):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"[ \t  ]+", " ", t)


NUM = r"([-−–]?\s?\d[\d   ]*(?:[.,]\d{1,2})?)"


def _num(s):
    s = re.sub(r"[\s  ]", "", s).replace("−", "-").replace("–", "-").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_balance(html):
    """-> (баланс або None, рядок контексту). Борг/заборгованість без знака мінус -> від'ємний баланс."""
    t = _text(html)
    m = re.search(r"(Заборгованість|Борг|Задолженность|До сплати|К оплате)\s*[:\-]?\s*" + NUM, t, re.I)
    if m and (_num(m.group(2)) or 0) > 0:
        return -abs(_num(m.group(2))), re.sub(r"\s+", " ", m.group(0)).strip()
    for kw in (r"Баланс(?: особового рахунку| рахунку)?", r"Залишок(?: на рахунку)?", r"На рахунку", r"На счету",
               r"Стан рахунку", r"Остаток"):
        m = re.search(r"(" + kw + r")\s*[:\-]?\s*" + NUM + r"\s*(грн|₴|UAH)?", t, re.I)
        if m and _num(m.group(2)) is not None:
            return _num(m.group(2)), re.sub(r"\s+", " ", m.group(0)).strip()
    return None, ""


def config():
    s = settings()
    pw = s.get("isp_password") or ""
    tok, chat = s.get("tg_token") or "", (s.get("tg_chat") or "").strip()
    if not (tok and chat) and current_site() != MAIN_SITE:          # Telegram — як у YASNO; інакше з «Бази»
        with using_site(MAIN_SITE):
            m = settings()
            tok, chat = m.get("tg_token") or "", (m.get("tg_chat") or "").strip()
    return {"enabled": s.get("isp_enabled") == "1", "name": s.get("isp_name") or "Цифра",
            "url": (s.get("isp_url") or DEFAULT_URL).rstrip("/"), "login": s.get("isp_login") or "",
            "password": vault.decrypt(pw) if pw else "", "has_password": bool(pw),
            "hour": int(s.get("isp_hour") or 10), "tg_token": vault.decrypt(tok) if tok else "", "tg_chat": chat}


def status():
    try:
        return json.loads(settings().get("isp_status") or "{}")
    except ValueError:
        return {}


def _last_dir():
    d = os.path.join(site_dir(), "isp_last")
    os.makedirs(d, exist_ok=True)
    return d


def _tg(c, text):
    if not (c["tg_token"] and c["tg_chat"]):
        return False
    yasno.tg_call(c["tg_token"], "sendMessage", {"chat_id": c["tg_chat"], "parse_mode": "HTML",
                                                 "text": f"🌐 <b>{yasno._esc(_(SITES[current_site()]['name']))}</b> · "
                                                         f"{yasno._esc(c['name'])}\n" + text})
    return True


def check(kind="manual"):
    """Зайти в кабінет, узяти баланс; при мінусі — сповістити (раз на день). -> (ok, повідомлення)."""
    lock = _locks.get(current_site()) or threading.Lock()
    if not lock.acquire(blocking=False):
        return False, _("Проверка уже выполняется")
    c = config()
    now = datetime.now().isoformat(timespec="seconds")
    st = status()
    try:
        if not (c["login"] and c["password"]):
            raise yasno.LoginError(_("укажите логин и пароль"))
        cab = Cabinet(c["url"])
        final = cab.login(c["login"], c["password"])
        _f, html, _ct = cab.get(final or "/", keep_errors=True)
        cab.pages["home"] = html
        bal, ctx = parse_balance(html)
        if bal is None:                               # баланс буває на окремій сторінці
            for path in ("/balance", "/finance", "/payments", "/account", "/profile", "/cabinet"):
                try:
                    _f, h2, _ct = cab.get(path, keep_errors=True)
                except Exception:  # noqa
                    continue
                cab.pages[path.strip("/")] = h2
                bal, ctx = parse_balance(h2)
                if bal is not None:
                    break
        for k, v in cab.pages.items():                # для діагностики (без пароля)
            with open(os.path.join(_last_dir(), re.sub(r"\W+", "_", k) + ".html"), "w", encoding="utf-8") as f:
                f.write(v.replace(c["password"], "***"))
        if bal is None:
            raise yasno.LoginError(_("вход есть, но баланс на странице не найден — скачайте страницы для диагностики"))
        sent = 0
        prev_debt = (st.get("balance") or 0) < 0
        if bal < 0 and st.get("notified") != date.today().isoformat():
            if _tg(c, f"🔴 <b>Заборгованість за інтернет: {yasno.money(-bal)} грн</b>\n{yasno._esc(ctx)}"):
                sent = 1
                st["notified"] = date.today().isoformat()
        elif bal >= 0 and prev_debt:
            if _tg(c, f"✅ Інтернет: заборгованість погашено, баланс {yasno.money(bal)} грн"):
                sent = 1
        st.update(time=now, ok=True, error="", balance=bal, context=ctx, kind=kind)
        if kind == "auto":
            st["last_auto"] = date.today().isoformat()
        set_setting("isp_status", json.dumps(st, ensure_ascii=False))
        get_db().commit()
        return True, f"{_('баланс')} {yasno.money(bal)} {_('грн')}" + (", Telegram ✓" if sent else "")
    except Exception as e:  # noqa
        err = str(e)[:300]
        st.update(time=now, ok=False, error=err, kind=kind)
        if kind == "auto":
            st["last_auto"] = date.today().isoformat()
        set_setting("isp_status", json.dumps(st, ensure_ascii=False))
        get_db().commit()
        return False, err
    finally:
        lock.release()


def _scheduler(app):
    while True:
        time.sleep(300)
        for site in SITES:
            try:
                with app.app_context(), using_site(site):
                    c = config()
                    st = status()
                    if not c["enabled"] or datetime.now().hour < c["hour"] or st.get("last_auto") == date.today().isoformat():
                        continue
                    if st.get("time") and datetime.now() - datetime.fromisoformat(st["time"]) < timedelta(hours=1):
                        continue
                    check("auto")
            except Exception:  # noqa
                pass


def _require():
    if not auth.can("yasno"):
        abort(403)


@bp.route("/isp")
def page():
    _require()
    return render_template("isp.html", c=config(), st=status(), is_admin=g.user["role"] == "admin")


@bp.route("/isp/run", methods=["POST"])
def run_now():
    _require()
    ok, msg = check("manual")
    flash(("✓ " if ok else "✗ ") + msg)
    return redirect(url_for("isp.page"))


@bp.route("/isp/settings", methods=["POST"])
def save_settings():
    if g.user["role"] != "admin":
        abort(403)
    f = request.form
    url = (f.get("url") or DEFAULT_URL).strip().rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?(/[\w\-./]*)?", url):
        flash(_("Адрес кабинета должен начинаться с https://"))
        return redirect(url_for("isp.page"))
    set_setting("isp_url", url)
    set_setting("isp_name", (f.get("name") or "Цифра").strip())
    set_setting("isp_login", (f.get("login") or "").strip())
    if (f.get("password") or "").strip():
        set_setting("isp_password", vault.encrypt(f.get("password").strip()))
    set_setting("isp_enabled", "1" if f.get("enabled") else "0")
    try:
        set_setting("isp_hour", str(min(23, max(0, int(f.get("hour") or 10)))))
    except ValueError:
        pass
    get_db().commit()
    try:
        from audit import log
        log("Настройки интернет-провайдера изменены", url)
        get_db().commit()
    except Exception:  # noqa
        pass
    if f.get("action") == "test":
        ok, msg = check("manual")
        flash(("✓ " if ok else "✗ ") + msg)
    else:
        flash(_("Настройки сохранены"))
    return redirect(url_for("isp.page"))


@bp.route("/isp/debug.zip")
def debug_zip():
    if g.user["role"] != "admin":
        abort(403)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in sorted(os.listdir(_last_dir())):
            z.write(os.path.join(_last_dir(), n), n)
        z.writestr("status.json", json.dumps(status(), ensure_ascii=False, indent=1))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"isp_debug_{datetime.now():%Y%m%d_%H%M}.zip")


def init_app(app):
    app.register_blueprint(bp)
    if os.environ.get("YASNO_SCHED", "1") != "0":
        threading.Thread(target=_scheduler, args=(app,), daemon=True).start()
