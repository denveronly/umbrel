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
import urllib.parse
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


def _csrf(cab):
    """CSRF-токен для API-входу (SPA «Цифри»): meta/скрипт сторінки, cookie або окремий ендпоінт із JS-бандла."""
    pat = r"""(?i)csrf[\w\-]*["']?\s*[:=]\s*["']([\w\-.:/+=]{16,})["']"""
    try:
        _f, html, _ct = cab.get("/login", keep_errors=True)
    except Exception:  # noqa
        html = ""
    cab.pages["api_login_page"] = html
    m = re.search(r'<meta[^>]+name=["\'](?:csrf-token|_csrf|csrf)["\'][^>]+content=["\']([^"\']+)', html, re.I) or \
        re.search(pat, html)
    if m:
        return m.group(1)
    for ck in cab.jar:
        if re.search(r"csrf|xsrf", ck.name, re.I):
            return urllib.parse.unquote(ck.value)
    for src in re.findall(r'<script[^>]+src=["\']([^"\']+\.js)', html)[:4]:
        try:
            _f, js, _ct = cab.get(src, keep_errors=True)
        except Exception:  # noqa
            continue
        for ep in dict.fromkeys(re.findall(r"""["'`](/api/[\w/\-]*csrf[\w/\-]*)["'`]""", js, re.I)):
            try:
                _f, body, _ct = cab.get(ep, keep_errors=True, headers={"Accept": "application/json"})
            except Exception:  # noqa
                continue
            cab.pages["api_csrf"] = body
            try:
                j = json.loads(body)
            except ValueError:
                j = None
            if isinstance(j, dict):
                for k, v in j.items():
                    if isinstance(v, str) and re.search(r"csrf|token", k, re.I) and len(v) >= 16:
                        return v
            for ck in cab.jar:
                if re.search(r"csrf|xsrf", ck.name, re.I):
                    return urllib.parse.unquote(ck.value)
    return ""


# ───────── Interlan (stat.interlan.com.ua) — Rails/Devise: форма #new_user, user[login] + user[password] ─────────

INTERLAN_HOSTS = ("stat.interlan.com.ua",)


def interlan_check(c, hold=None):
    """Вхід у кабінет Interlan так само, як людина: сторінка входу -> authenticity_token -> POST /users/sign_in,
    далі головна сторінка кабінету, де шукаємо баланс. -> (баланс, контекст, cab)."""
    cab = Cabinet("https://stat.interlan.com.ua")
    if hold is not None:
        hold["cab"] = cab
    final, html, _ct = cab.get("/users/sign_in", keep_errors=True)
    cab.pages["01_sign_in"] = html
    p = yasno.parse(html)
    form = next((f for f in p.forms if "sign_in" in (f.get("action") or "") and "by_ip" not in (f.get("action") or "")
                 and any(i["type"] == "password" for i in f["inputs"])), None)
    if not form:
        raise yasno.LoginError(_("кабинет") + ": " + _("на странице входа нет формы с паролем"))
    token = next((i["value"] for i in form["inputs"] if i["name"] == "authenticity_token"), "") or p.meta.get("csrf-token", "")
    data = {"utf8": "✓", "authenticity_token": token, "user[login]": c["login"], "user[password]": c["password"],
            "commit": "Увійти"}
    final, html, ctype = cab.get("/users/sign_in", data=data, referer=final, keep_errors=True)
    cab.pages["02_after_sign_in"] = f"<!-- HTTP {cab.status} {final} -->\n" + html
    if "/users/sign_in" in final and any(i["type"] == "password" for f in yasno.parse(html).forms for i in f["inputs"]):
        err = re.search(r'class="[^"]*(?:alert|flash|error|notice)[^"]*"[^>]*>\s*(.*?)\s*<', html, re.S)
        raise yasno.LoginError(_("кабинет") + ": " + (re.sub(r"<[^>]+>|\s+", " ", err.group(1)).strip()[:150]
                                                      if err else _("неверный логин или пароль")))
    interlan_parse.due = 0
    bal, ctx = interlan_parse(html)
    if bal is None:
        _f, html, _c = cab.get("/", keep_errors=True)
        cab.pages["03_home"] = html
        bal, ctx = interlan_parse(html)
    if bal is None:
        bal, ctx = parse_balance(html)
    if bal is None:
        for path in ("/profile", "/payments"):
            try:
                _f, h2, _c = cab.get(path, keep_errors=True)
            except Exception:  # noqa
                continue
            cab.pages[f"03_{path.strip('/') or 'home'}"] = h2
            bal, ctx = parse_balance(h2)
            if bal is not None:
                break
    if hold is not None:
        hold["due"] = getattr(interlan_parse, "due", 0) or 0
    return bal, ctx, cab


def interlan_parse(html):
    """Плитки головної: «Залишок на рахунку», «За наступний місяць», «Рекомендуємо до оплати». -> (баланс, контекст)."""
    def amount(v):
        m = re.search(NUM, re.sub(r"<[^>]+>", " ", v or ""))
        return _num(m.group(1)) if m else None
    boxes = {re.sub(r"\s+", " ", k).strip().lower(): amount(v)
             for k, v in re.findall(r'info-box-text">\s*(.*?)\s*</span>\s*<span class="info-box-number">\s*(.*?)\s*</span>',
                                    html or "", re.S)}
    bal = boxes.get("залишок на рахунку")
    if bal is None:
        m = re.search(r"На рахунку\s*" + NUM, _text(html))
        bal = _num(m.group(1)) if m else None
    if bal is None:
        return None, ""
    parts = [(_("Задолженность") if bal < 0 else _("Баланс")) + f" {yasno.money(abs(bal))} грн"]
    if boxes.get("за наступний місяць") is not None:
        parts.append(f"наступний місяць {yasno.money(boxes['за наступний місяць'])} грн")
    if boxes.get("рекомендуємо до оплати"):
        parts.append(f"рекомендовано до оплати {yasno.money(boxes['рекомендуємо до оплати'])} грн")
    interlan_parse.due = boxes.get("рекомендуємо до оплати") or 0
    return bal, " · ".join(parts)


# ───────── Diamant (account.diamant.inf.ua) — білінг NoDeny: GET stat.pl?ses&uu&pp, pp = md5(ses + " " + пароль) ─────────

DIAMANT_HOSTS = ("account.diamant.inf.ua",)


def _cp1251(raw):
    try:
        return raw.decode("cp1251")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def diamant_parse(html):
    """«На вашому рахунку 335.00 грн», «Разом до сплати 350.00». -> (баланс, контекст, до оплати)."""
    t = _text(html).replace("&nbsp;", " ")
    m = re.search(r"На вашому рахунку\s*" + NUM, t, re.I)
    if not m:
        return None, "", 0
    bal = _num(m.group(1))
    m2 = re.search(r"Разом до сплати\s*,?\s*грн\.?\s*" + NUM, t, re.I)
    total = _num(m2.group(1)) if m2 else None
    acc = re.search(r"(дозволено|заблоковано|заборонено|відключено|blocked)", t, re.I)
    due = max(0.0, round(total - bal, 2)) if total is not None and bal is not None else 0
    parts = [(_("Задолженность") if bal < 0 else _("Баланс")) + f" {yasno.money(abs(bal))} грн"]
    if total is not None:
        parts.append(f"за місяць {yasno.money(total)} грн")
    if due:
        parts.append(f"рекомендовано до оплати {yasno.money(due)} грн")
    if acc and acc.group(1).lower() != "дозволено":
        parts.append(f"доступ: {acc.group(1)}")
    return bal, " · ".join(parts), due


def diamant_check(c, hold=None):
    """Вхід як у браузері: сторінка входу дає ses, пароль іде як md5(ses + " " + пароль)."""
    import hashlib
    cab = Cabinet("https://account.diamant.inf.ua")
    if hold is not None:
        hold["cab"] = cab
    _f, raw, _ct = cab.get("/cgi-bin/stat.pl", binary=True, keep_errors=True)
    html = _cp1251(raw)
    cab.pages["01_login"] = html
    m = re.search(r"name=['\"]?ses['\"]?\s+value=['\"]?(\d+)", html) or re.search(r"value=['\"]?(\d+)['\"]?\s+name=['\"]?ses", html)
    if not m:
        raise yasno.LoginError(_("кабинет") + ": " + _("на странице входа нет формы с паролем"))
    ses = m.group(1)
    pp = hashlib.md5(f"{ses} {c['password']}".encode("cp1251", "replace")).hexdigest()
    q = urllib.parse.urlencode({"ses": ses, "uu": c["login"], "pp": pp})
    _f, raw, _ct = cab.get("/cgi-bin/stat.pl?" + q, binary=True, keep_errors=True)
    html = _cp1251(raw)
    cab.pages["02_home"] = html.replace(pp, "***")
    bal, ctx, due = diamant_parse(html)
    if bal is None:
        if re.search(r"name=['\"]?pp['\"]?", html) and re.search(r"type=['\"]?password", html):
            raise yasno.LoginError(_("кабинет") + ": " + _("неверный логин или пароль"))
        raise yasno.LoginError(_("вход есть, но баланс на странице не найден — скачайте страницы для диагностики"))
    if hold is not None:
        hold["due"] = due
    return bal, ctx, cab


def provider(url):
    host = urllib.parse.urlsplit(url or "").netloc.lower()
    if host in INTERLAN_HOSTS:
        return "interlan"
    if host in DIAMANT_HOSTS:
        return "diamant"
    if host in ("my.cyfra.ua", "cyfra.ua", "www.cyfra.ua"):
        return "cyfra"
    return "generic"


def api_check(c, hold=None):
    """Вхід через JSON API (POST /api/auth/login {login, password}) — так працює my.cyfra.ua.
    -> (баланс, контекст, cab) або None, якщо такого API на сайті немає (тоді — звичайна форма)."""
    p = urllib.parse.urlsplit(c["url"])
    host = "my.cyfra.ua" if p.netloc in ("cyfra.ua", "www.cyfra.ua") else p.netloc   # cyfra.ua/auth -> кабінет
    cab = Cabinet(f"{p.scheme}://{host}")
    if hold is not None:
        hold["cab"] = cab                             # щоб і при помилці зберегти сторінки для діагностики
    tok = _csrf(cab)
    h = {"Accept": "application/json, text/plain, */*", "Referer": f"{cab.base}/login", "Authorization": ""}
    if tok:
        h["CSRF-Token"] = tok
    _f, body, ctype = cab.get("/api/auth/login", data={"login": c["login"], "password": c["password"]},
                              json_body=True, headers=h, keep_errors=True)
    cab.pages["api_login"] = f"<!-- HTTP {cab.status} {ctype} csrf={'yes' if tok else 'no'} -->\n" + \
        re.sub(r'("token"\s*:\s*")[^"]+', r"\1***", body)
    if cab.status in (404, 405) or "json" not in ctype:
        return None
    try:
        j = json.loads(body)
    except ValueError:
        return None
    u = j.get("user") if isinstance(j, dict) else None
    if not isinstance(u, dict) or u.get("balance") is None:
        msg = (j.get("message") or j.get("error") or j.get("status")) if isinstance(j, dict) else ""
        if cab.status == 403 and not tok:
            msg = f"{msg or 'HTTP 403'} (CSRF)"
        raise yasno.LoginError(_("кабинет") + ": " + str(msg or f"HTTP {cab.status}")[:200])
    k = lambda v: (float(v) / 100) if v not in (None, "") else None   # сума в копійках: 50100 = 501,00 грн
    bal = k(u.get("balance"))
    parts = [(_("Задолженность") if bal < 0 else _("Баланс")) + f" {yasno.money(abs(bal))} грн"]
    if k(u.get("abonsum")):
        parts.append(_("абонплата") + f" {yasno.money(k(u['abonsum']))} грн")
    if u.get("contractnum"):
        parts.append(_("договор") + f" {u['contractnum']}")
    return bal, " · ".join(parts), cab


SLOTS = ("1", "2", "3", "4")                          # кабінети (спільні для всіх об'єктів: «База», «Офіс»…)


def _migrate(s):
    """Раніше кабінет був свій у кожного об'єкта (isp_* у «Базі» та «Офісі») — переносимо в спільні слоти."""
    if s.get("isp_migrated") == "1":
        return
    n = 0
    for site in SITES:
        with using_site(site):
            o = settings()
        if not o.get("isp_login"):
            continue
        n += 1
        slot = str(n)
        name = o.get("isp_name") or "Цифра"
        set_setting(f"isp{slot}_name", f"{name} · {SITES[site]['name']}")
        for k in ("url", "login", "password", "status"):
            if o.get(f"isp_{k}"):
                set_setting(f"isp{slot}_{k}", o[f"isp_{k}"])
        set_setting(f"isp{slot}_enabled", o.get("isp_enabled") or "0")
        if n == 1 and o.get("isp_hour"):
            set_setting("isp_hour", o["isp_hour"])
        if o.get("isp_tg_token") and not s.get("isp_tg_token"):
            set_setting("isp_tg_token", o["isp_tg_token"])
            set_setting("isp_tg_chat", o.get("isp_tg_chat") or "")
        if n >= len(SLOTS):
            break
    set_setting("isp_migrated", "1")
    get_db().commit()


def config():
    """Спільні налаштування «Інтернету» (у базі «Бази»): кабінети, година перевірки, свій Telegram."""
    with using_site(MAIN_SITE):
        s = settings()
        if s.get("isp_migrated") != "1":
            _migrate(s)
            s = settings()
        if s.get("isp_hour9") != "1":                 # звіт у Telegram — о 9:00 (так попросили)
            set_setting("isp_hour", "9")
            set_setting("isp_hour9", "1")
            get_db().commit()
            s = settings()
    own_tok, own_chat = s.get("isp_tg_token") or "", (s.get("isp_tg_chat") or "").strip()
    tok, chat = (own_tok, own_chat) if (own_tok and own_chat) else (s.get("tg_token") or "", (s.get("tg_chat") or "").strip())
    acc = []
    for n in SLOTS:
        pw = s.get(f"isp{n}_password") or ""
        acc.append({"slot": n, "name": s.get(f"isp{n}_name") or "", "url": (s.get(f"isp{n}_url") or DEFAULT_URL).rstrip("/"),
                    "login": s.get(f"isp{n}_login") or "", "password": vault.decrypt(pw) if pw else "",
                    "has_password": bool(pw), "enabled": s.get(f"isp{n}_enabled") == "1",
                    "ip": (s.get(f"isp{n}_ip") or "").strip()})
    return {"accounts": acc, "hour": int(s.get("isp_hour") or 9),
            "tg_token": vault.decrypt(tok) if tok else "", "tg_chat": chat,
            "own_tg": bool(own_tok and own_chat), "has_own_token": bool(own_tok), "own_chat": own_chat}


def account(slot, c=None):
    c = c or config()
    return next((a for a in c["accounts"] if a["slot"] == slot), None)


def used(c=None):
    """Кабінети з логіном (показуються на сторінці)."""
    return [a for a in (c or config())["accounts"] if a["login"]]


def status(slot):
    with using_site(MAIN_SITE):
        try:
            return json.loads(settings().get(f"isp{slot}_status") or "{}")
        except ValueError:
            return {}


def _save_status(slot, st):
    with using_site(MAIN_SITE):
        set_setting(f"isp{slot}_status", json.dumps(st, ensure_ascii=False))
        get_db().commit()


def _last_dir(slot):
    d = os.path.join(DATA_DIR, "isp", slot, "last")
    os.makedirs(d, exist_ok=True)
    return d


def _tg(c, a, text):
    if not (c["tg_token"] and c["tg_chat"]):
        return False
    yasno.tg_call(c["tg_token"], "sendMessage", {"chat_id": c["tg_chat"], "parse_mode": "HTML",
                                                 "text": f"🌐 <b>{yasno._esc(a['name'] or a['login'])}</b>\n" + text})
    return True


def _dump(cab, a):
    """Зберегти отримані сторінки кабінету для діагностики (пароль і логін замасковано)."""
    if not cab or not getattr(cab, "pages", None):
        return
    d = _last_dir(a["slot"])
    for n in os.listdir(d):
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass
    for k, v in cab.pages.items():
        v = v or ""
        for secret in (a.get("password"), a.get("login")):
            if secret:
                v = v.replace(secret, "***")
        with open(os.path.join(d, re.sub(r"\W+", "_", k) + ".html"), "w", encoding="utf-8") as f:
            f.write(v)


_slot_locks = {n: threading.Lock() for n in SLOTS}


def check(slot, kind="manual"):
    """Зайти в кабінет, узяти баланс; при мінусі — сповістити (раз на день). -> (ok, повідомлення)."""
    lock = _slot_locks.get(slot) or threading.Lock()
    if not lock.acquire(blocking=False):
        return False, _("Проверка уже выполняется")
    c = config()
    a = account(slot, c)
    now = datetime.now().isoformat(timespec="seconds")
    st = status(slot)
    cab = None
    try:
        if not a or not (a["login"] and a["password"]):
            raise yasno.LoginError(_("укажите логин и пароль"))
        hold = {}
        try:
            prov = provider(a["url"])
            if prov == "interlan":                         # свій спосіб входу для Interlan
                res = interlan_check(a, hold)
            elif prov == "diamant":                        # NoDeny: md5(ses + пароль)
                res = diamant_check(a, hold)
            else:
                res = api_check(a, hold)                    # спершу JSON API (SPA-кабінети, як my.cyfra.ua)
        finally:
            cab = hold.get("cab")
        if res:
            bal, ctx, cab = res
        else:
            cab = Cabinet(a["url"])
            final = cab.login(a["login"], a["password"])
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
        _dump(cab, a)
        if bal is None:
            raise yasno.LoginError(_("вход есть, но баланс на странице не найден — скачайте страницы для диагностики"))
        st.update(time=now, ok=True, error="", balance=bal, context=ctx, kind=kind, due=hold.get("due") or 0)
        if kind == "auto":
            st["last_auto"] = date.today().isoformat()
        _save_status(slot, st)
        return True, f"{a['name'] or a['login']}: {_('баланс')} {yasno.money(bal)} {_('грн')}"
    except Exception as e:  # noqa
        err = str(e)[:300]
        if a:
            _dump(cab, a)                             # сторінку входу теж — щоб було видно, чому не зайшли
        st.update(time=now, ok=False, error=err, kind=kind)
        if kind == "auto":
            st["last_auto"] = date.today().isoformat()
        _save_status(slot, st)
        return False, f"{(a or {}).get('name') or slot}: {err}"
    finally:
        lock.release()


KEYCAPS = {"1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣"}


def report_text(c=None):
    """Одне повідомлення по всіх кабінетах: 1️⃣ назва — баланс · до оплати; борг — 🔴."""
    c = c or config()
    now = datetime.now()
    lines = [f"🌐 <b>Інтернет</b> · {now:%d.%m %H:%M}", ""]
    for a in used(c):
        s = status(a["slot"])
        name = yasno._esc(a["name"] or a["login"])
        bal = s.get("balance")
        if bal is None:
            line = f"{KEYCAPS.get(a['slot'], '▪️')} <b>{name}</b> — —"
        elif bal < 0:
            line = f"{KEYCAPS.get(a['slot'], '▪️')} <b>{name}</b> — 🔴 <b>борг {yasno.money(-bal)}\u00a0грн</b>"
        elif bal > 0:
            line = f"{KEYCAPS.get(a['slot'], '▪️')} <b>{name}</b> — 🟢 переплата {yasno.money(bal)}\u00a0грн"
        else:
            line = f"{KEYCAPS.get(a['slot'], '▪️')} <b>{name}</b> — 0,00\u00a0грн"
        if a.get("ip"):
            line += f"\n   🌍 IP {yasno._esc(a['ip'])}"
        if (s.get("due") or 0) > 0 and (bal is None or bal >= 0):
            line += f"\n   🟡 до оплати {yasno.money(s['due'])}\u00a0грн"
        if s.get("time") and not s.get("ok"):
            line += f"\n   ⚠️ не вдалося перевірити: {yasno._esc(s.get('error', ''))[:150]}"
        lines.append(line)
    return "\n".join(lines)


def send_report(c=None):
    c = c or config()
    if not (c["tg_token"] and c["tg_chat"]):
        return False, _("Telegram не настроен")
    yasno.tg_call(c["tg_token"], "sendMessage", {"chat_id": c["tg_chat"], "parse_mode": "HTML",
                                                 "text": report_text(c), "disable_web_page_preview": True})
    return True, _("Отчёт отправлен в Telegram")


def _scheduler(app):
    """Раз на день з години c["hour"]: перевірити всі кабінети, потім одне повідомлення в Telegram.
    Кілька воркерів gunicorn — тож працює лише той, хто взяв файлове блокування."""
    import fcntl
    while True:
        time.sleep(120)
        fd = None
        try:
            d = os.path.join(DATA_DIR, "isp")
            os.makedirs(d, exist_ok=True)
            fd = os.open(os.path.join(d, ".sched.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue
            with app.app_context():
                c = config()
                if datetime.now().hour < c["hour"]:
                    continue
                today = date.today().isoformat()
                todo = [a for a in c["accounts"] if a["enabled"] and a["login"] and a["has_password"]]
                for a in todo:
                    if status(a["slot"]).get("last_auto") != today:
                        check(a["slot"], "auto")
                with using_site(MAIN_SITE):
                    sent = settings().get("isp_report_sent")
                if todo and sent != today:
                    try:
                        send_report(config())
                    except Exception:  # noqa
                        pass
                    with using_site(MAIN_SITE):
                        set_setting("isp_report_sent", today)
                        get_db().commit()
        except Exception:  # noqa
            pass
        finally:
            if fd is not None:
                os.close(fd)


def _require():
    if not auth.can("isp"):
        abort(403)


@bp.route("/isp")
def page():
    _require()
    c = config()
    return render_template("isp.html", c=c, accounts=used(c), st={a["slot"]: status(a["slot"]) for a in c["accounts"]},
                           is_admin=g.user["role"] == "admin")


@bp.route("/isp/run", methods=["POST"])
def run_now():
    _require()
    slots = [a["slot"] for a in used()] if request.form.get("slot") == "all" else [request.form.get("slot") or "1"]
    for sl in slots:
        ok, msg = check(sl, "manual")
        flash(("✓ " if ok else "✗ ") + msg)
    return redirect(url_for("isp.page"))


@bp.route("/isp/send", methods=["POST"])
def send_now():
    _require()
    try:
        ok, msg = send_report()
    except Exception as e:  # noqa
        ok, msg = False, "Telegram: " + str(e)[:200]
    flash(("✓ " if ok else "✗ ") + msg)
    return redirect(url_for("isp.page"))


@bp.route("/isp/settings", methods=["POST"])
def save_settings():
    if g.user["role"] != "admin":
        abort(403)
    f = request.form
    with using_site(MAIN_SITE):
        for n in SLOTS:
            url = (f.get(f"url{n}") or DEFAULT_URL).strip().rstrip("/")
            if not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?(/[\w\-./]*)?", url):
                flash(_("Адрес кабинета должен начинаться с https://"))
                return redirect(url_for("isp.page"))
            set_setting(f"isp{n}_url", url)
            set_setting(f"isp{n}_name", (f.get(f"name{n}") or "").strip()[:60])
            set_setting(f"isp{n}_login", (f.get(f"login{n}") or "").strip())
            if (f.get(f"password{n}") or "").strip():
                set_setting(f"isp{n}_password", vault.encrypt(f.get(f"password{n}").strip()))
            set_setting(f"isp{n}_enabled", "1" if f.get(f"enabled{n}") else "0")
            set_setting(f"isp{n}_ip", (f.get(f"ip{n}") or "").strip()[:100])
        try:
            set_setting("isp_hour", str(min(23, max(0, int(f.get("hour") or 10)))))
        except ValueError:
            pass
        set_setting("isp_tg_chat", (f.get("tg_chat") or "").strip())
        if (f.get("tg_token") or "").strip():
            set_setting("isp_tg_token", vault.encrypt(f.get("tg_token").strip()))
        get_db().commit()
        try:
            from audit import log
            log("Настройки интернет-провайдера изменены", ", ".join(x for x in (f.get(f"name{n}") for n in SLOTS) if x))
            get_db().commit()
        except Exception:  # noqa
            pass
    act = f.get("action")
    if act == "tgtest":
        c = config()
        try:
            ok = _tg(c, {"name": "Тест"}, "✅ Повідомлення з програми «Склади» (модуль «Інтернет»)")
            flash("✓ Telegram" if ok else "✗ " + _("Telegram не настроен"))
        except Exception as e:  # noqa
            flash("✗ Telegram: " + str(e)[:200])
    elif act == "test":
        for a in used():
            if a["has_password"]:
                ok, msg = check(a["slot"], "manual")
                flash(("✓ " if ok else "✗ ") + msg)
    else:
        flash(_("Настройки сохранены"))
    return redirect(url_for("isp.page"))


@bp.route("/isp/<slot>/debug.zip")
def debug_zip(slot):
    if g.user["role"] != "admin" or slot not in SLOTS:
        abort(403)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in sorted(os.listdir(_last_dir(slot))):
            z.write(os.path.join(_last_dir(slot), n), n)
        z.writestr("status.json", json.dumps(status(slot), ensure_ascii=False, indent=1))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"isp{slot}_debug_{datetime.now():%Y%m%d_%H%M}.zip")


PROVIDER_NAMES = {"interlan": "Interlan — вход user[login]/user[password] (Rails)",
                  "diamant": "Diamant — NoDeny, пароль md5(ses + пароль)",
                  "cyfra": "Цифра — вход через API", "generic": "обычная форма входа"}


def init_app(app):
    app.register_blueprint(bp)
    app.jinja_env.globals["isp_provider"] = lambda url: PROVIDER_NAMES[provider(url)]
    if os.environ.get("YASNO_SCHED", "1") != "0":
        threading.Thread(target=_scheduler, args=(app,), daemon=True).start()
