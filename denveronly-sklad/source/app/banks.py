"""Банки: залишки й рух коштів по рахунках компаній у клієнт-банку ПУМБ (online.pumb.ua/biz).

Вхід — гостьовий користувач банку з правом лише перегляду (без ключа підпису), тож програма фізично не може
нічого сплатити. Логін/пароль кожної компанії — у налаштуваннях розділу, пароль зашифровано vault.py.

Сайт банку закритий захистом від ботів (Akamai) і рахує відбиток браузера, тому заходимо справжнім
Chromium (Playwright) так само, як людина: вводимо логін і пароль, натискаємо «Увійти», читаємо сторінки.
Результат (рахунки, залишки, операції за місяць) кешується в data/banks/<slot>.json — сторінка розділу
показує кеш миттєво, а оновлення йде у фоні. Останні сторінки кабінету (без пароля) зберігаються для
діагностики, як у «Інтернеті» та YASNO.

Компанії — спільні для всієї програми (не залежать від об'єкта «База/Офіс/Дім»): налаштування в базі «Бази».
"""
import fcntl
import io
import json
import os
import re
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, send_file, url_for

import auth
import vault
from db import DATA_DIR, MAIN_SITE, get_db, set_setting, settings, using_site
from i18n import _

bp = Blueprint("banks", __name__)
BASE = "https://online.pumb.ua"
LOGIN_URL = BASE + "/biz/"
SLOTS = ("1", "2", "3")
DIR = os.path.join(DATA_DIR, "banks")
IBAN = re.compile(r"UA\d{27}")


class BankError(Exception):
    pass


# ───────────────────────── налаштування ─────────────────────────

def config():
    """Три компанії: назва вкладки, логін, пароль (розшифрований), увімкнено. Зберігаються в базі «Бази»."""
    with using_site(MAIN_SITE):
        s = settings()
    out = []
    for n in SLOTS:
        pw = s.get(f"bank{n}_password") or ""
        out.append({"slot": n, "name": s.get(f"bank{n}_name") or "", "login": s.get(f"bank{n}_login") or "",
                    "password": vault.decrypt(pw) if pw else "", "has_password": bool(pw),
                    "enabled": s.get(f"bank{n}_enabled", "1") == "1"})
    every = int(s.get("banks_every") or 3)
    return {"companies": out, "every": every, "auto": s.get("banks_auto", "1") == "1"}


def active(c=None):
    c = c or config()
    return [x for x in c["companies"] if x["name"] and x["login"] and x["has_password"] and x["enabled"]]


# ───────────────────────── кеш і діагностика ─────────────────────────

def _path(slot, name="data.json"):
    d = os.path.join(DIR, slot)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def load(slot):
    try:
        with open(_path(slot), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(slot, data):
    tmp = _path(slot) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(slot))


def _dump_pages(slot, pages, secrets_):
    d = os.path.join(DIR, slot, "last")
    os.makedirs(d, exist_ok=True)
    for n in os.listdir(d):
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass
    for k, v in pages.items():
        v = v or ""
        for s in secrets_:
            if s:
                v = v.replace(s, "***")
        with open(os.path.join(d, re.sub(r"[^\w.\-]+", "_", k)[:80] + ".html"), "w", encoding="utf-8") as f:
            f.write(v)


@contextmanager
def _browser_lock():
    """Один Chromium на весь контейнер (gunicorn має кілька воркерів, у кожного свій планувальник)."""
    os.makedirs(DIR, exist_ok=True)
    fd = os.open(os.path.join(DIR, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BankError(_("Обновление уже выполняется"))
        yield
    finally:
        os.close(fd)


# ───────────────────────── розбір сторінок ─────────────────────────

def num(s):
    """'1 234 567,89' / '-1 234.5' / '1,234.56' -> float."""
    if s is None:
        return None
    s = re.sub(r"[\s  ₴]|грн|UAH", "", str(s)).replace("−", "-").replace("–", "-")
    if not s:
        return None
    if "," in s and "." in s:                       # 1,234.56 або 1.234,56
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _date(s):
    m = re.search(r"(\d{2})[./](\d{2})[./](\d{4})", s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else ""


# ───────────────────────── браузер ─────────────────────────

def _launch(pw):
    return pw.chromium.launch(headless=True, channel="chromium", args=["--no-sandbox", "--disable-dev-shm-usage",
                                                   "--disable-blink-features=AutomationControlled"])


def _context(browser):
    ctx = browser.new_context(locale="uk-UA", timezone_id="Europe/Kyiv", viewport={"width": 1366, "height": 900},
                              user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                         "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    ctx.set_default_timeout(45000)
    return ctx


PERSON = "img[src*='user-aunthenticated'], img[src*='user-authenticated']"


def _password_mode(page):
    """Іноді банк на вході просить ключ — тоді, як і людина, тиснемо іконку «людини» (вхід лише за паролем)."""
    ico = page.locator(PERSON)
    if ico.count():
        try:
            with page.expect_navigation(wait_until="networkidle", timeout=30000):
                ico.first.click()
            return True
        except Exception:  # noqa — сторінка могла не перезавантажитись
            page.wait_for_load_state("networkidle")
            return True
    return False


def _asks_key(page):
    if page.locator("#UserName").count() and page.locator("#SignatureAndPasswordModel_Password").is_visible():
        return False
    return bool(re.search(r"ключ|носі[йя]|сертифікат|токен", page.inner_text("body")[:20000], re.I))


def _submit(page, c):
    page.wait_for_function("document.querySelector('#FpData') && document.querySelector('#FpData').value",
                           timeout=15000)                   # відбиток браузера, як у справжньому вході
    page.fill("#UserName", c["login"])
    page.fill("#SignatureAndPasswordModel_Password", c["password"])
    page.wait_for_timeout(600)
    with page.expect_navigation(wait_until="networkidle", timeout=60000):
        page.click("#SignedLogon")


def login(page, c, pages):
    """Звичайна форма банку: логін -> пароль -> «Увійти» (сайт сам кодує пароль і шле відбиток браузера)."""
    page.goto(LOGIN_URL, wait_until="networkidle")
    pages["01_login"] = page.content()
    if _asks_key(page) or not page.locator("#UserName").count():
        _password_mode(page)
        pages["01b_login_password_mode"] = page.content()
    if not page.locator("#UserName").count():
        raise BankError(_("страница входа банка не похожа на ожидаемую — скачайте страницы для диагностики"))
    _submit(page, c)
    pages["02_after_login"] = page.content()
    if page.locator("#UserName").count() and _asks_key(page) and _password_mode(page):
        pages["02b_password_mode"] = page.content()       # попросив ключ — переходимо на вхід за паролем
        _submit(page, c)
        pages["02c_after_login"] = page.content()
    if page.locator("#UserName").count():                  # форма входу на місці — не пустили
        err = page.locator(".validation-summary-errors, .field-validation-error, .error, .alert").all_inner_texts()
        msg = " ".join(t.strip() for t in err if t.strip())[:200]
        raise BankError(_("банк не пустил") + (": " + msg if msg else ""))


def fetch(c, months=2):
    """Зайти в банк однієї компанії -> {'accounts': [...], 'ops': [...]}; pages — для діагностики."""
    from playwright.sync_api import sync_playwright
    import pumb
    pages = {}
    try:
        with sync_playwright() as pw:
            browser = _launch(pw)
            try:
                page = _context(browser).new_page()
                login(page, c, pages)
                res = pumb.read(page, pages, months)
            finally:
                browser.close()
        return res, pages
    except Exception as e:  # noqa
        e.pages = pages
        raise


def refresh(slot, kind="manual"):
    """Оновити одну компанію. -> (ok, повідомлення)."""
    c = next((x for x in config()["companies"] if x["slot"] == slot), None)
    if not c or not (c["login"] and c["has_password"]):
        return False, _("укажите логин и пароль")
    if not c["password"]:
        return False, _("пароль не расшифровывается (сменился ключ) — введите его заново")
    data = load(slot)
    now = datetime.now().isoformat(timespec="seconds")
    pages = {}
    try:
        with _browser_lock():
            res, pages = fetch(c, 2 if data.get("ops") else 13)   # перший раз — рік історії
        fresh = set(res.get("periods", []))             # місяці, які щойно перечитали, — замінюємо цілком
        keep = [o for o in data.get("ops", []) if (o.get("date") or "")[:7] not in fresh]
        cutoff = (date.today().replace(day=1) - timedelta(days=400)).isoformat()
        data["ops"] = [o for o in keep if (o.get("date") or "") >= cutoff] + res.get("ops", [])
        data["accounts"] = res.get("accounts", [])
        data["periods"] = sorted(set(data.get("periods", [])) | fresh)
        data.update(time=now, ok=True, error="", kind=kind)
        _save(slot, data)
        bal = sum(a.get("balance") or 0 for a in res.get("accounts", []) if a.get("currency", "UAH") == "UAH")
        return True, f"{c['name']}: {_('остаток')} {bal:,.2f} {_('грн')}".replace(",", " ") + \
            f", {_('операций')}: {len(res.get('ops', []))}"
    except Exception as e:  # noqa
        pages = pages or getattr(e, "pages", {})
        err = str(e).strip().splitlines()[0][:300] if str(e).strip() else e.__class__.__name__
        data.update(time=now, ok=False, error=err, kind=kind)
        _save(slot, data)
        return False, f"{c['name']}: {err}"
    finally:
        _dump_pages(slot, pages, (c.get("password"), c.get("login")))


def refresh_all(kind="manual"):
    return [refresh(c["slot"], kind) for c in active()]


def run_async(slots, kind="manual"):
    def run():
        for s in slots:
            try:
                refresh(s, kind)
            except Exception:  # noqa
                pass
    threading.Thread(target=run, daemon=True).start()


def _scheduler(app):
    """Кожні N годин з 7:00 до 22:00 — оновлюємо всі увімкнені компанії."""
    time.sleep(90)
    while True:
        try:
            with app.app_context():
                c = config()
                h = datetime.now().hour
                if c["auto"] and 7 <= h <= 22:
                    for x in active(c):
                        t = load(x["slot"]).get("time")
                        if not t or datetime.now() - datetime.fromisoformat(t) >= timedelta(hours=c["every"]):
                            refresh(x["slot"], "auto")
        except Exception:  # noqa
            pass
        time.sleep(600)


# ───────────────────────── сторінки ─────────────────────────

def _require():
    if not auth.can("banks"):
        abort(403)


def month_ops(data, period):
    """Операції за місяць period ('YYYY-MM') + підсумки надходжень і списань."""
    ops = [o for o in data.get("ops", []) if (o.get("date") or "").startswith(period)]
    ops.sort(key=lambda o: (o.get("date") or "", o.get("time") or ""), reverse=True)
    inc = sum(o["amount"] for o in ops if o.get("amount", 0) > 0)
    out = -sum(o["amount"] for o in ops if o.get("amount", 0) < 0)
    return ops, inc, out


@bp.route("/banks")
def page():
    _require()
    c = config()
    tabs = [x for x in c["companies"] if x["name"]]
    slot = request.args.get("c") or (tabs[0]["slot"] if tabs else "1")
    period = request.args.get("m") or date.today().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        period = date.today().strftime("%Y-%m")
    data = load(slot)
    ops, inc, out = month_ops(data, period)
    y, m = map(int, period.split("-"))
    prev = f"{y - (m == 1)}-{12 if m == 1 else m - 1:02d}"
    nxt = f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}"
    q = (request.args.get("q") or "").strip().lower()
    if q:
        ops = [o for o in ops if q in (o.get("party", "") + " " + o.get("purpose", "") + " " +
                                       o.get("edrpou", "")).lower()]
    cur = next((x for x in c["companies"] if x["slot"] == slot), c["companies"][0])
    return render_template("banks.html", c=c, tabs=tabs, cur=cur, slot=slot, data=data, ops=ops, inc=inc, out=out,
                           period=period, prev=prev, nxt=nxt if nxt <= date.today().strftime("%Y-%m") else "",
                           q=q, is_admin=g.user["role"] == "admin",
                           summary={x["slot"]: load(x["slot"]) for x in tabs})


@bp.route("/banks/refresh", methods=["POST"])
def refresh_now():
    _require()
    slot = request.form.get("c") or ""
    slots = [x["slot"] for x in active()] if slot == "all" else [slot]
    if slot == "all" or request.form.get("bg"):
        run_async(slots)
        flash(_("Обновление запущено — обновите страницу через минуту"))
    else:
        ok, msg = refresh(slot)
        flash(("✓ " if ok else "✗ ") + msg)
    return redirect(url_for("banks.page", c=slot if slot != "all" else None, m=request.form.get("m") or None))


@bp.route("/banks/settings", methods=["POST"])
def save_settings():
    if g.user["role"] != "admin":
        abort(403)
    f = request.form
    with using_site(MAIN_SITE):
        for n in SLOTS:
            set_setting(f"bank{n}_name", (f.get(f"name{n}") or "").strip()[:60])
            set_setting(f"bank{n}_login", (f.get(f"login{n}") or "").strip())
            set_setting(f"bank{n}_enabled", "1" if f.get(f"enabled{n}") else "0")
            if (f.get(f"password{n}") or "").strip():
                set_setting(f"bank{n}_password", vault.encrypt(f.get(f"password{n}").strip()))
        set_setting("banks_auto", "1" if f.get("auto") else "0")
        try:
            set_setting("banks_every", str(min(24, max(1, int(f.get("every") or 3)))))
        except ValueError:
            pass
        get_db().commit()
        try:
            from audit import log
            log("Настройки банков изменены", ", ".join(x for x in (f.get(f"name{n}") for n in SLOTS) if x))
            get_db().commit()
        except Exception:  # noqa
            pass
    if f.get("action") == "test":
        run_async([x["slot"] for x in active()])
        flash(_("Сохранено. Проверка входа запущена — обновите страницу через минуту"))
    else:
        flash(_("Настройки сохранены"))
    return redirect(url_for("banks.page"))


@bp.route("/banks/<slot>/debug.zip")
def debug_zip(slot):
    if g.user["role"] != "admin" or slot not in SLOTS:
        abort(403)
    d = os.path.join(DIR, slot, "last")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                z.write(os.path.join(d, n), n)
        st = {k: v for k, v in load(slot).items() if k not in ("ops",)}
        z.writestr("status.json", json.dumps(st, ensure_ascii=False, indent=1))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"bank{slot}_debug_{datetime.now():%Y%m%d_%H%M}.zip")


def init_app(app):
    app.register_blueprint(bp)
    if os.environ.get("YASNO_SCHED", "1") != "0":
        threading.Thread(target=_scheduler, args=(app,), daemon=True).start()
