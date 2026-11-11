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


class Busy(BankError):
    """Браузер зайнятий іншою компанією — це не збій, просто спробуємо трохи пізніше."""


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
    tok = s.get("banks_tg_token") or ""
    return {"companies": out, "every": every, "auto": s.get("banks_auto", "1") == "1",
            "refresh_times": parse_times(s.get("banks_refresh_times") or DEFAULT_REFRESH),
            "tg_token": vault.decrypt(tok) if tok else "", "has_tg_token": bool(tok),
            "tg_chat": (s.get("banks_tg_chat") or "").strip(), "report": s.get("banks_report", "1") == "1",
            "report_times": parse_times(s.get("banks_report_times") or DEFAULT_TIMES)}


DEFAULT_TIMES = "09:10, 17:50"
DEFAULT_REFRESH = "09:00, 13:00, 17:00, 18:00"


def parse_times(txt):
    """'9:10, 17:50' -> ['09:10', '17:50']."""
    out = []
    for h, m in re.findall(r"(\d{1,2})[:.](\d{2})", txt or ""):
        if int(h) < 24 and int(m) < 60:
            out.append(f"{int(h):02d}:{m}")
    return sorted(set(out))


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
            raise Busy(_("Обновление уже выполняется"))
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


def _settle(page, ms=8000):
    """Дочекатися, поки сторінка домалюється. «Тиші в мережі» на сайті банку не буває (Akamai, аналітика
    шлють запити постійно), тому чекаємо подію load, але не довше ms, і далі працюємо з DOM."""
    try:
        page.wait_for_load_state("load", timeout=ms)
    except Exception:  # noqa
        pass
    page.wait_for_timeout(800)


SUBMIT = "#submitBtn, input[name='SignedLogon'], #SignedLogon"   # кнопка «Вхід»
PERSON = "img[src*='user-aunthenticated'], img[src*='user-authenticated']"


def _password_mode(page):
    """Іноді банк на вході просить ключ — тоді, як і людина, тиснемо іконку «людини» (вхід лише за паролем)."""
    ico = page.locator(PERSON)
    if ico.count():
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
                ico.first.click()
            return True
        except Exception:  # noqa — сторінка могла не перезавантажитись
            page.wait_for_load_state("domcontentloaded")
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
    with page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
        page.click(SUBMIT)
    _settle(page)


def login(page, c, pages):
    """Звичайна форма банку: логін -> пароль -> «Увійти» (сайт сам кодує пароль і шле відбиток браузера)."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
    _settle(page)
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


def _snapshot(page, pages):
    """При збої — що було на екрані: URL, HTML і скриншот (для «сторінок для діагностики»)."""
    if page is None:
        return
    try:
        pages["99_error_url"] = page.url
        pages["99_error_page"] = page.content()
    except Exception as e:  # noqa
        pages["99_error_page"] = f"<!-- page.content() failed: {e} -->"
    try:
        import base64
        page.evaluate("document.querySelectorAll('#UserName').forEach(e => e.value = '***')")   # логін не в скриншот
    except Exception:  # noqa
        pass
    try:
        import base64
        png = page.screenshot(full_page=True, timeout=15000)
        pages["99_error_screenshot"] = '<img src="data:image/png;base64,' + base64.b64encode(png).decode() + '">'
    except Exception:  # noqa
        pass


def fetch(c, months=2):
    """Зайти в банк однієї компанії -> {'accounts': [...], 'ops': [...]}; pages — для діагностики."""
    from playwright.sync_api import sync_playwright
    import pumb
    pages = {}
    try:
        with sync_playwright() as pw:
            browser = _launch(pw)
            page = None
            try:
                page = _context(browser).new_page()
                login(page, c, pages)
                res = pumb.read(page, pages, months)
            except Exception:
                _snapshot(page, pages)
                raise
            finally:
                browser.close()
        return res, pages
    except Exception as e:  # noqa
        pages["98_network_probe"] = _probe()
        e.pages = pages
        raise


def _probe():
    """Чи доходить контейнер до банку взагалі (без браузера) — щоб відрізнити мережу від блокування браузера."""
    import urllib.request
    t0 = time.time()
    try:
        req = urllib.request.Request(LOGIN_URL, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(400).decode("utf-8", "replace")
            return f"<!-- HTTP {r.status} за {time.time() - t0:.1f} с -->\n" + body
    except Exception as e:  # noqa
        return f"<!-- без браузера: {e.__class__.__name__}: {e} за {time.time() - t0:.1f} с -->"


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
    except Busy as e:
        pages = None                                    # і діагностику попередньої спроби не чіпаємо
        return False, f"{c['name']}: {e}"                 # нічого не записуємо: спробуємо за 30 с
    except Exception as e:  # noqa
        pages = pages or getattr(e, "pages", {})
        err = str(e).strip().splitlines()[0][:300] if str(e).strip() else e.__class__.__name__
        data.update(time=now, ok=False, error=err, kind=kind)
        _save(slot, data)
        return False, f"{c['name']}: {err}"
    finally:
        if pages is not None:
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


OFFSETS = {"1": 0, "2": 2, "3": 3}                    # хвилини: 1-й банк о 9:00, 2-й о 9:02, 3-й о 9:03


def schedule(c, day=None):
    """Розклад автооновлень на день: задані години, зі зсувом для кожної компанії (+0, +2, +3 хв).
    -> {slot: [datetime, ...]}"""
    day = day or date.today()
    out = {}
    for x in active(c):
        out[x["slot"]] = [datetime(day.year, day.month, day.day, int(t[:2]), int(t[3:]))
                          + timedelta(minutes=OFFSETS.get(x["slot"], 0)) for t in c["refresh_times"]]
    return out


def refresh_due(c):
    """Компанії, чий час за розкладом настав (не пізніше години тому), а після нього ще не оновлювались."""
    now = datetime.now()
    for slot, times in schedule(c).items():
        past = [t for t in times if t <= now < t + timedelta(hours=1)]
        if not past:
            continue
        last = load(slot).get("time")
        if not last or datetime.fromisoformat(last) < past[-1]:
            refresh(slot, "auto")


def _scheduler(app):
    """Щохвилини: звіт у Telegram у задані години; автооновлення компаній за розкладом (9:00, 9:02, 9:03 …)."""
    time.sleep(30)
    while True:
        try:
            with app.app_context():
                c = config()
                report_due(c)
                if c["auto"]:
                    refresh_due(c)
        except Exception:  # noqa
            pass
        time.sleep(30)


# ───────────────────────── звіт у Telegram ─────────────────────────

NB = "\u00a0"                                         # нерозривний пробіл: «20 000,00» не рветься в Telegram


def _fmt(x):
    return f"{x:,.2f}".replace(",", NB).replace(".", ",")


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def op_key(o):
    """Відбиток операції: щоб у звітах за розкладом кожен платіж був лише один раз."""
    return "|".join(str(o.get(k) or "") for k in ("iban", "date", "doc", "amount", "edrpou"))


def _reported_path():
    return os.path.join(DIR, "reported.json")


def _reported():
    try:
        with open(_reported_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _mark_reported(keys):
    """Запам'ятати надіслані операції (зберігаємо 45 днів)."""
    data = _reported()
    today = date.today().isoformat()
    for k in keys:
        data.setdefault(k, today)
    cutoff = (date.today() - timedelta(days=45)).isoformat()
    data = {k: v for k, v in data.items() if v >= cutoff}
    os.makedirs(DIR, exist_ok=True)
    tmp = _reported_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _reported_path())


def _group(ops, sign):
    """Сума за контрагентами (без призначення), від більшої. sign=1 — надходження, -1 — списання."""
    acc = {}
    for o in ops:
        a = o.get("amount", 0) * sign
        if a > 0:
            k = short_party(o.get("party")) or "—"
            acc[k] = acc.get(k, 0) + a
    return sorted(acc.items(), key=lambda kv: -kv[1])


def report_text(days, label, errors=None, skip=None):
    """Одне повідомлення по всіх компаніях: баланс, надходження й списання за дні days.
    skip — множина вже надісланих операцій (звіт за розкладом їх не повторює). -> (текст, нові ключі)."""
    errors, skip = errors or {}, skip or set()
    lines, keys = [f"🏦 <b>Стан рахунків</b> · {label}"], []
    for x in [x for x in config()["companies"] if x["name"] and x["enabled"]]:
        d = load(x["slot"])
        bal = sum(a.get("balance") or 0 for a in d.get("accounts", []) if a.get("currency", "UAH") == "UAH")
        lines.append("")
        lines.append(f"🟩 <b>{_esc(x['name'])}</b> — " + (f"<b>{_fmt(bal)}{NB}грн</b>" if d.get("accounts") else "—"))
        if x["slot"] in errors or (d.get("time") and not d.get("ok")):
            when = d.get("time", "")
            lines.append(f"⚠️ не вдалося оновити: {_esc(errors.get(x['slot']) or d.get('error', ''))[:150]}"
                         + (f" (дані на {when[8:10]}.{when[5:7]} {when[11:16]})" if when else ""))
        ops = [o for o in d.get("ops", []) if o.get("date") in days and op_key(o) not in skip]
        keys += [op_key(o) for o in ops]
        for sign, head in ((1, "🟢 <b>+</b> Надходження"), (-1, "🔴 <b>−</b> Списання")):
            g_ = _group(ops, sign)
            if g_:
                lines.append(f"{head} {_fmt(sum(v for _k, v in g_))}:")
                lines += [f"   • {_esc(k)}{NB}— {_fmt(v)}" for k, v in g_]
        if not ops and d.get("accounts"):
            lines.append("   нового руху коштів не було" if skip else "   руху коштів не було")
    return "\n".join(lines), keys


def _report_period(now, t):
    """Вручну: до 12:00 — рух за вчора (виписка за день уже повна), після — за сьогодні."""
    if int(t[:2]) < 12:
        y = now.date() - timedelta(days=1)
        return [y.isoformat()], f"{now:%d.%m} {t} · рух за вчора, {y:%d.%m}"
    return [now.date().isoformat()], f"{now:%d.%m} {t} · рух за сьогодні"


def send_report(t=None, refresh_first=True, scheduled=False):
    """Оновити компанії й надіслати звіт. -> (ok, повідомлення).
    За розкладом — лише нові операції за вчора й сьогодні (кожен платіж один раз), баланс — завжди.
    Вручну (кнопка) — усе за день, без позначок «надіслано»."""
    c = config()
    if not (c["tg_token"] and c["tg_chat"]):
        return False, _("Telegram не настроен")
    now = datetime.now()
    t = t or now.strftime("%H:%M")
    errors = {}
    if refresh_first:
        for x in active(c):
            for _w in range(20):                            # браузер зайнятий автооновленням — чекаємо
                ok, msg = refresh(x["slot"], "report")
                if ok or _("Обновление уже выполняется") not in msg:
                    break
                time.sleep(15)
            if not ok:
                errors[x["slot"]] = msg.split(": ", 1)[-1]
    if scheduled:
        days = [(now.date() - timedelta(days=1)).isoformat(), now.date().isoformat()]
        text, keys = report_text(days, f"{now:%d.%m} {t} · нові операції", errors, set(_reported()))
    else:
        days, label = _report_period(now, t)
        text, keys = report_text(days, label, errors)
    import yasno
    for i in range(0, len(text), 3900):                    # ліміт Telegram — 4096 символів
        yasno.tg_call(c["tg_token"], "sendMessage", {"chat_id": c["tg_chat"], "parse_mode": "HTML",
                                                     "text": text[i:i + 3900], "disable_web_page_preview": True})
    if scheduled:
        _mark_reported(keys)
    return True, _("Отчёт отправлен в Telegram")


def report_due(c):
    """Чи настав час звіту (у межах 2 годин після заданого) і його ще не надсилали сьогодні.
    Кілька воркерів gunicorn — тому блокування файлом і позначка «надіслано» в базі."""
    if not (c["report"] and c["tg_token"] and c["tg_chat"] and active(c)):
        return
    now = datetime.now()
    for t in c["report_times"]:
        at = now.replace(hour=int(t[:2]), minute=int(t[3:]), second=0, microsecond=0)
        if not (at <= now < at + timedelta(hours=2)):
            continue
        key = f"{now:%Y-%m-%d} {t}"
        os.makedirs(DIR, exist_ok=True)
        fd = os.open(os.path.join(DIR, ".report.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return                                    # інший воркер уже надсилає
            with using_site(MAIN_SITE):
                if settings().get("banks_report_sent") == key:
                    continue
            ok, msg = False, ""
            for _try in range(3):                           # браузер міг бути зайнятий автооновленням
                try:
                    ok, msg = send_report(t, scheduled=True)
                    break
                except BankError:
                    time.sleep(60)
                except Exception as e:  # noqa
                    msg = str(e)
                    break
            with using_site(MAIN_SITE):
                set_setting("banks_report_sent", key)
                set_setting("banks_report_status", json.dumps({"time": now.isoformat(timespec="seconds"),
                                                               "ok": ok, "msg": msg[:200]}, ensure_ascii=False))
                get_db().commit()
        finally:
            os.close(fd)


def report_status():
    with using_site(MAIN_SITE):
        try:
            return json.loads(settings().get("banks_report_status") or "{}")
        except ValueError:
            return {}


# ───────────────────────── сторінки ─────────────────────────

def _require():
    if not auth.can("banks"):
        abort(403)


FORMS = re.compile(r"^(?:товариство з обмеженою відповідальністю|приватне акціонерне товариство|акціонерне товариство|"
                   r"комунальне підприємство|приватне підприємство|фізична особа[- ]підприємець|тов|прат|пат|ат|кп|пп|"
                   r"фоп|фо-п|дп|сп)\b[\s.]*", re.I)


SHORT_FORMS = [(re.compile(p, re.I), r) for p, r in (
    (r"^товариство з обмеженою відповідальністю\b", "ТОВ"), (r"^приватне акціонерне товариство\b", "ПрАТ"),
    (r"^публічне акціонерне товариство\b", "ПАТ"), (r"^акціонерне товариство\b", "АТ"),
    (r"^комунальне підприємство\b", "КП"), (r"^приватне підприємство\b", "ПП"),
    (r"^фізична особа[- ]підприємець\b", "ФОП"))]


def short_party(name):
    """«ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ "ДЕЛІВЕРІ"» -> «ТОВ "ДЕЛІВЕРІ"» (для таблиці й Telegram)."""
    s = (name or "").strip()
    for rx, rep in SHORT_FORMS:
        s = rx.sub(rep, s)
    return s


def party_key(name):
    """Ключ сортування за назвою: без «ТОВ/ФОП/АТ…» і лапок — «ТОВ "УМТ"» стоїть на «У»."""
    s = re.sub(r"[\"'«»„“”`]", " ", (name or "").strip())
    return FORMS.sub("", s).strip().lower()


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
    sort = request.args.get("s") if request.args.get("s") in ("date", "name", "in", "out") else "date"
    desc = request.args.get("d", "1" if sort != "name" else "0") == "1"
    if sort == "name":
        ops.sort(key=lambda o: party_key(o.get("party")), reverse=desc)
    elif sort in ("in", "out"):                      # спершу потрібний бік (за сумою), решта — за датою
        sign = 1 if sort == "in" else -1
        side = [o for o in ops if o.get("amount", 0) * sign > 0]
        rest = [o for o in ops if o.get("amount", 0) * sign <= 0]
        side.sort(key=lambda o: abs(o.get("amount", 0)), reverse=desc)
        ops = side + rest
    elif not desc:
        ops.reverse()
    cur = next((x for x in c["companies"] if x["slot"] == slot), c["companies"][0])
    return render_template("banks.html", c=c, tabs=tabs, cur=cur, slot=slot, data=data, ops=ops, inc=inc, out=out,
                           period=period, prev=prev, nxt=nxt if nxt <= date.today().strftime("%Y-%m") else "",
                           q=q, sort=sort, desc=desc, rep=report_status(), sched=schedule(c), is_admin=g.user["role"] == "admin",
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


@bp.route("/banks/send", methods=["POST"])
def send_now():
    """Кнопка вгорі: надіслати в Telegram те, що зараз на сторінці (без повторного входу в банк)."""
    _require()
    try:
        ok, msg = send_report(refresh_first=False)
    except Exception as e:  # noqa
        ok, msg = False, "Telegram: " + str(e)[:200]
    flash(("✓ " if ok else "✗ ") + msg)
    return redirect(url_for("banks.page", c=request.form.get("c") or None, m=request.form.get("m") or None))


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
        set_setting("banks_report", "1" if f.get("report") else "0")
        set_setting("banks_report_times", ", ".join(parse_times(f.get("report_times"))) or DEFAULT_TIMES)
        set_setting("banks_tg_chat", (f.get("tg_chat") or "").strip())
        if (f.get("tg_token") or "").strip():
            set_setting("banks_tg_token", vault.encrypt(f.get("tg_token").strip()))
        try:
            set_setting("banks_refresh_times", ", ".join(parse_times(f.get("refresh_times"))) or DEFAULT_REFRESH)
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
    elif f.get("action") == "report":
        def run():
            try:
                send_report(refresh_first=True)
            except Exception:  # noqa
                pass
        threading.Thread(target=run, daemon=True).start()
        flash(_("Сохранено. Отчёт придёт в Telegram через 1–2 минуты (сначала обновляются все компании)"))
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
    app.jinja_env.filters["short_party"] = short_party
    if os.environ.get("YASNO_SCHED", "1") != "0":
        threading.Thread(target=_scheduler, args=(app,), daemon=True).start()
