"""Модуль НАФТОГАЗ: заходить в особистий кабінет газопостачальника (my.gas.ua), забирає історію
розрахунків (щомісячні нарахування = рахунки за газ), поточний баланс і заборгованість по кожному
особовому рахунку.

Кабінет — звичайний серверний сайт на Laravel (сторінки віддаються готовим HTML, окремого JSON-API
немає), тож браузер не потрібен: сторінки розбираються стандартним HTMLParser.

ВХІД — за cookie сесії. my.gas.ua дозволяє вхід телефон+пароль АБО через Google/Facebook; щоб не
відтворювати Google OAuth із коду, користувач один раз копіює cookie своєї сесії з браузера (DevTools)
у налаштування. Модуль ходить із цією cookie. Коли сесія протухне — кабінет віддає сторінку входу,
модуль це помічає і просить оновити cookie. Cookie зберігається зашифрованою (vault.py), ключ — поза
базою та бекапами.

Стадія 1 (готово): клієнт кабінету + розбір сторінок + структуровані дані (fetch).
Стадія 2 (інтеграція): тумблер `naftogaz_enabled`, таблиці в БД, сторінка, розклад, сповіщення — за
зразком yasno.py.

Самостійна перевірка (лише стандартна бібліотека, без Flask):
    python naftogaz.py --parse accrual.html 2026        # розбір збереженої сторінки
    python naftogaz.py --fetch '<cookie>' [рік ...]      # жива перевірка cookie: зайти і зібрати
"""
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date
from html.parser import HTMLParser

DEFAULT_URL = "https://my.gas.ua"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 40

# повні назви місяців у кабінеті (нижній регістр; у таблиці подаються як «Жовтень» через CSS text-capitalize)
MONTHS = {"січень": 1, "лютий": 2, "березень": 3, "квітень": 4, "травень": 5, "червень": 6,
          "липень": 7, "серпень": 8, "вересень": 9, "жовтень": 10, "листопад": 11, "грудень": 12}
DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")


# ---------------- числа / дати ----------------

def num(s):
    """'7 554,15' / '-819.40' / '1 339.62' -> float; None якщо не число (порожньо або текст-заглушка)."""
    if s is None:
        return None
    t = re.sub(r"[\s   ]", "", str(s)).replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        return None
    return float(t)


def iso(s):
    m = DATE_RE.search(s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


# ---------------- розбір HTML (таблиці + форми) ----------------

class Page(HTMLParser):
    """Збирає форми (з полями), таблиці (рядки -> клітинки) і <meta>. Текст перед таблицею — як заголовок."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.tables, self._tstack, self._form, self.meta = [], [], [], None, {}
        self._recent = ""
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "meta" and a.get("name"):
            self.meta[a["name"].lower()] = a.get("content") or ""
        elif tag == "form":
            self._form = {"action": a.get("action") or "", "method": (a.get("method") or "get").lower(), "inputs": []}
            self.forms.append(self._form)
        elif tag in ("input", "textarea", "select") and self._form is not None:
            self._form["inputs"].append({"name": a.get("name"), "type": (a.get("type") or "text").lower(),
                                         "value": a.get("value") or "", "tag": tag, "id": a.get("id") or ""})
        elif tag == "table":
            self._tstack.append({"rows": [], "before": self._recent.strip()[-160:], "row": None, "cell": None})
        elif tag == "tr" and self._tstack:
            t = self._tstack[-1]
            t["row"] = []
            t["rows"].append(t["row"])
        elif tag in ("td", "th") and self._tstack and self._tstack[-1]["row"] is not None:
            t = self._tstack[-1]
            t["cell"] = {"text": "", "links": [], "th": tag == "th", "cls": a.get("class") or "",
                         "cs": int(a.get("colspan") or 1), "rs": int(a.get("rowspan") or 1)}
            t["row"].append(t["cell"])
        elif tag == "a" and self._tstack and self._tstack[-1]["cell"] is not None and a.get("href"):
            self._tstack[-1]["cell"]["links"].append(a["href"])
        elif tag == "br" and self._tstack and self._tstack[-1]["cell"] is not None:
            self._tstack[-1]["cell"]["text"] += " "

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "form":
            self._form = None
        elif tag == "table" and self._tstack:
            t = self._tstack.pop()
            rows = [[{"text": re.sub(r"\s+", " ", c["text"]).strip(), "links": c["links"], "th": c["th"],
                      "cs": c["cs"], "rs": c["rs"]} for c in r] for r in t["rows"] if r]
            self.tables.append({"rows": rows, "before": t["before"]})
        elif tag in ("td", "th") and self._tstack:
            self._tstack[-1]["cell"] = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._tstack and self._tstack[-1]["cell"] is not None:
            self._tstack[-1]["cell"]["text"] += data
        elif not self._tstack and data.strip():
            self._recent = (self._recent + " " + data.strip())[-400:]


def parse(html):
    p = Page()
    p.feed(html or "")
    p.close()
    return p


# ---------------- розбір «Історія розрахунків» (/accrual-and-payment) ----------------

# колонки таблиці -> ключі (за заголовком другого рядка thead); зіставляємо за підрядком
COLS = [
    ("місяць", "month"), ("тарифний", "tariff"), ("ціна", "price"),
    ("сплачено", "paid_total"), ("показання", "reading"), ("спожито", "consumed"),
    ("об", "volume"), ("нараховано", "accrued"), ("оплата", "payment"), ("баланс", "balance"),
]


def _flatten_header(head_rows):
    """Розгортає багаторядкову шапку (colspan/rowspan) у плоский список підписів колонок.
    Напр. «Сплачено всього» має rowspan=2 і займає свою колонку між «Ціна» і «Показання»."""
    label, span = {}, {}        # col -> підпис; col -> (підпис, скільки рядків ще зайнято)
    ncols = 0
    for row in head_rows:
        ci = 0
        for cell in row:
            while span.get(ci, (None, 0))[1] > 0:     # пропустити колонки, зайняті rowspan згори
                ci += 1
            for k in range(cell["cs"]):
                label[ci + k] = cell["text"]          # найглибший рядок перезаписує груповий заголовок
                if cell["rs"] > 1:
                    span[ci + k] = (cell["text"], cell["rs"])
            ci += cell["cs"]
            ncols = max(ncols, ci)
        for col in list(span):
            lbl, rem = span[col]
            span[col] = (lbl, rem - 1)
    return [label.get(i, "") for i in range(ncols)]


def _col_map(labels):
    """Плоскі підписи колонок -> {індекс: ключ}."""
    idx = {}
    for i, lab in enumerate(labels):
        low = lab.lower()
        for sub, key in COLS:
            if sub in low:
                idx[i] = key
                break
    return idx


def parse_accruals(html, year=None):
    """Повертає список місяців (новіші першими, як у кабінеті):
       {period:'YYYY-MM', tariff, price, paid_total, reading, consumed, volume,
        accrued (нараховано, грн — сума рахунку), payment, balance (- = борг),
        has_invoice (чи є PDF-рахунок), status (текст у колонці дій)}.
    year — рік сторінки (кабінет показує один рік); якщо None, береться поточний."""
    year = year or date.today().year
    p = parse(html)
    out = []
    for t in p.tables:
        # потрібна таблиця з заголовками «Нараховано» і «Баланс»
        head_text = " ".join(c["text"].lower() for r in t["rows"][:2] for c in r)
        if "нараховано" not in head_text or "баланс" not in head_text:
            continue
        # шапка — провідні рядки лише з <th>; розгортаємо з урахуванням colspan/rowspan
        head_rows = []
        for r in t["rows"]:
            if r and all(c["th"] for c in r):
                head_rows.append(r)
            else:
                break
        if not any("місяць" in c["text"].lower() for r in head_rows for c in r):
            continue
        cmap = _col_map(_flatten_header(head_rows))
        for r in t["rows"]:
            if not r or r[0]["th"]:
                continue
            month = MONTHS.get(r[0]["text"].strip().lower())
            if not month:
                continue
            row = {"period": f"{year}-{month:02d}"}
            for i, cell in enumerate(r):
                key = cmap.get(i)
                if not key or key == "month":
                    continue
                if key in ("tariff",):
                    row[key] = cell["text"]
                else:
                    row[key] = num(cell["text"])
            action = r[-1]
            row["has_invoice"] = "рахунок" in action["text"].lower() and "очікуйте" not in action["text"].lower()
            row["status"] = action["text"]
            out.append(row)
        break
    return out


def summarize(accruals):
    """Зведення по особовому рахунку: поточний баланс/борг і останній виставлений рахунок."""
    with_balance = [a for a in accruals if a.get("balance") is not None]
    balance = with_balance[0]["balance"] if with_balance else None   # верхній рядок = найсвіжіший
    billed = [a for a in accruals if a.get("accrued") is not None]
    last = billed[0] if billed else None
    return {
        "balance": balance,
        "debt": round(-balance, 2) if (balance is not None and balance < 0) else 0.0,
        "last_period": last["period"] if last else None,
        "last_accrued": last["accrued"] if last else None,
        "months": len(accruals),
    }


# ---------------- розбір головної (/home): О/Р, власник, адреса, тариф ----------------

def _text(html):
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return re.sub(r"[ \t ]+", " ", t)


def parse_home(html):
    """Best-effort: особовий рахунок, власник, EIC, адреса, тариф, ціна. Не критично — порожнє, якщо не знайдено.
    Авторитетні баланс/борг беруться зі сторінки розрахунків (summarize), не звідси."""
    t = _text(html)

    def after(label, pat=r"([^\n]{2,80}?)\s{2,}"):
        m = re.search(re.escape(label) + r"\s*" + pat, t)
        return m.group(1).strip() if m else None

    acc = re.search(r"Особовий рахунок\s*(\d{4,})", t)
    eic = re.search(r"EIC[-\s]?код\s*([A-Z0-9]{8,})", t)
    price = re.search(r"Ціна за 1 куб\. м,?\s*грн\s*([\d ,.]+)", t)
    return {
        "account": acc.group(1) if acc else None,
        "owner": after("Власник особового рахунку"),
        "eic": eic.group(1) if eic else None,
        "address": after("Адреса"),
        "tariff": after("Тарифний план"),
        "price": num(price.group(1)) if price else None,
    }


# ---------------- кабінет ----------------

class LoginError(Exception):
    pass


class CookieExpired(LoginError):
    """Cookie недійсна/протухла — кабінет віддає сторінку входу. Треба оновити cookie в налаштуваннях."""


def clean_cookie(raw):
    """Нормалізує вставлене користувачем у рядок заголовка Cookie. Приймає:
      • сирий заголовок «name=v; name2=v2»  • з префіксом «Cookie:»
      • цілу команду з DevTools «Copy as cURL» (витягує cookie з -H 'cookie: …' або -b/--cookie '…')."""
    if not raw:
        return ""
    s = raw.strip()
    # з команди cURL: шукаємо -H 'cookie: …' або -b/--cookie '…'
    m = re.search(r"""-H\s+['"]cookie:\s*([^'"]+)['"]""", s, re.I) or \
        re.search(r"""(?:-b|--cookie)\s+['"]([^'"]+)['"]""", s, re.I)
    if m:
        s = m.group(1)
    s = re.sub(r"^\s*cookie\s*:\s*", "", s, flags=re.I)           # прибрати префікс «Cookie:»
    parts = [p.strip() for p in s.replace("\n", ";").split(";") if "=" in p]
    return "; ".join(parts)


class Cabinet:
    """Доступ до my.gas.ua за cookie сесії (без логіна/пароля). cookie — рядок заголовка Cookie з браузера."""

    def __init__(self, cookie, base=DEFAULT_URL):
        self.base = base.rstrip("/")
        self.cookie = clean_cookie(cookie)
        self.pages = {}
        self.status = None

    def get(self, path, binary=False, keep_errors=True):
        url = urllib.parse.urljoin(self.base + "/", path)
        h = {"User-Agent": UA, "Accept-Language": "uk-UA,uk;q=0.9", "Cookie": self.cookie,
             "Accept": "application/pdf,*/*" if binary else "text/html,application/xhtml+xml,*/*;q=0.8"}
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw, final, self.status = r.read(), r.geturl(), r.status
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            if not keep_errors:
                raise
            raw, final, ctype, self.status = e.read(), url, e.headers.get("Content-Type", ""), e.code
        if binary:
            return final, raw, ctype
        cs = re.search(r"charset=([\w-]+)", ctype)
        return final, raw.decode(cs.group(1) if cs else "utf-8", errors="replace"), ctype

    @staticmethod
    def _is_login(final, html):
        return "/login" in urllib.parse.urlsplit(final).path or bool(
            re.search(r'name="phone"[^>]*>\s*<[^>]*name="password"|Увійдіть в особистий кабінет', html, re.I))

    def check(self):
        """Чи дійсна cookie. -> дані профілю з головної (баланс, О/Р, власник) або CookieExpired."""
        final, html, _c = self.get("home")
        self.pages["home"] = html
        if not self.cookie:
            raise LoginError("не задано cookie сесії")
        if self.status and self.status >= 400 or self._is_login(final, html):
            raise CookieExpired("cookie сесії недійсна або протухла — оновіть її в налаштуваннях")
        return parse_home(html)

    def accruals(self, year=None):
        path = "accrual-and-payment" + (f"?filter%5Byear%5D={year}" if year else "")
        final, html, _c = self.get(path)
        self.pages[f"accrual_{year or 'cur'}"] = html
        if self._is_login(final, html):
            raise CookieExpired("сесія закрилась на сторінці розрахунків — оновіть cookie")
        return parse_accruals(html, year)


def fetch(cookie, base=DEFAULT_URL, years=None):
    """Зайти в кабінет за cookie і зібрати розрахунки за вказані роки (за замовчуванням — поточний).
       -> (data, cabinet). data = {profile, accruals, summary}."""
    cab = Cabinet(cookie, base)
    profile = cab.check()
    all_rows = []
    for y in (years or [date.today().year]):
        all_rows += cab.accruals(y)
    return {"profile": profile, "accruals": all_rows, "summary": summarize(all_rows)}, cab


# ================= інтеграція у застосунок: об'єкти типу «дім» (Страхолісся), кілька особових рахунків =================
# Кожен особовий рахунок (газовий лічильник) — своя cookie сесії my.gas.ua. Дані: нарахування, оплати, баланс/борг;
# показання облгазу вписуються в «Газ» відповідного лічильника (лише в порожні клітинки).
import fcntl
import os
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

import auth
import vault
from db import DATA_DIR, MAIN_SITE, SITES, current_site, get_db, set_setting, settings, using_site
from i18n import _

bp = Blueprint("naftogaz", __name__)
LOCK_FILE = os.path.join(DATA_DIR, "naftogaz.lock")
_run_locks = {s: threading.Lock() for s in SITES}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ng_accounts (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, gas_meter_id INTEGER, cookie TEXT,
    url TEXT, account_no TEXT, owner TEXT, address TEXT,
    balance REAL, debt REAL, last_period TEXT, last_accrued REAL,
    checked_at TEXT, ok INTEGER, error TEXT, cookie_expired INTEGER DEFAULT 0, sort INTEGER DEFAULT 100
);
CREATE TABLE IF NOT EXISTS ng_bills (
    acc_id INTEGER NOT NULL REFERENCES ng_accounts(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    tariff TEXT, price REAL, reading REAL, consumed REAL, volume REAL,
    accrued REAL, payment REAL, paid_total REAL, balance REAL,
    has_invoice INTEGER NOT NULL DEFAULT 0, status TEXT, updated_at TEXT,
    PRIMARY KEY (acc_id, period)
);
CREATE TABLE IF NOT EXISTS ng_notified (key TEXT PRIMARY KEY, at TEXT DEFAULT (datetime('now', 'localtime')));
"""


def ensure_schema():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


def is_home_site():
    return SITES[current_site()].get("kind") == "home"


def enabled():
    try:
        return is_home_site() and settings().get("naftogaz_enabled") == "1"
    except Exception:  # noqa
        return False


def config():
    s = settings()
    tg_tok, tg_chat = s.get("tg_token") or "", (s.get("tg_chat") or "").strip()
    if not (tg_tok and tg_chat) and current_site() != MAIN_SITE:       # Telegram — як у YASNO; інакше з «Бази»
        with using_site(MAIN_SITE):
            m = settings()
            tg_tok, tg_chat = m.get("tg_token") or "", (m.get("tg_chat") or "").strip()
    return {"enabled": s.get("naftogaz_enabled") == "1", "hour": int(s.get("naftogaz_hour") or 9),
            "years": int(s.get("naftogaz_years") or 2),
            "notify_bills": s.get("naftogaz_notify_bills", "1") == "1", "notify_debt": s.get("naftogaz_notify_debt", "1") == "1",
            "tg_token": vault.decrypt(tg_tok) if tg_tok else "", "tg_chat": tg_chat}


def money(x):
    return "—" if x is None else f"{x:,.2f}".replace(",", " ").replace(".", ",")


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_send(c, text):
    if not (c["tg_token"] and c["tg_chat"]):
        return False
    import yasno
    yasno.tg_call(c["tg_token"], "sendMessage", {"chat_id": c["tg_chat"], "parse_mode": "HTML",
                                                 "text": f"🔥 <b>{_esc(_(SITES[current_site()]['name']))}</b>\n" + text,
                                                 "disable_web_page_preview": True})
    return True


def _years(c):
    y = date.today().year
    return list(range(y, y - max(1, c["years"]), -1))


def sync_account(acc, c, db, first=False):
    """Один особовий рахунок: забрати розрахунки, оновити таблицю й показання газу. -> (нові рахунки, борг)."""
    now = datetime.now().isoformat(timespec="seconds")
    cookie = vault.decrypt(acc["cookie"] or "") if acc["cookie"] else ""
    try:
        if not cookie:
            raise LoginError(_("не задан cookie сессии"))
        data, _cab = fetch(cookie, base=acc["url"] or DEFAULT_URL, years=_years(c))
    except Exception as e:  # noqa
        err = (str(e) if isinstance(e, LoginError) else f"{type(e).__name__}: {e}")[:400]
        db.execute("UPDATE ng_accounts SET checked_at=?, ok=0, error=?, cookie_expired=? WHERE id=?",
                   (now, err, 1 if isinstance(e, CookieExpired) else 0, acc["id"]))
        db.commit()
        return [], None, err
    rows, summ, prof = data["accruals"], data["summary"], data["profile"]
    new = []
    for b in rows:
        old = db.execute("SELECT 1 FROM ng_bills WHERE acc_id=? AND period=?", (acc["id"], b["period"])).fetchone()
        db.execute("INSERT INTO ng_bills(acc_id, period, tariff, price, reading, consumed, volume, accrued, payment, paid_total, "
                   "balance, has_invoice, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(acc_id, period) DO UPDATE SET tariff=excluded.tariff, price=excluded.price, "
                   "reading=excluded.reading, consumed=excluded.consumed, volume=excluded.volume, accrued=excluded.accrued, "
                   "payment=excluded.payment, paid_total=excluded.paid_total, balance=excluded.balance, "
                   "has_invoice=excluded.has_invoice, status=excluded.status, updated_at=excluded.updated_at",
                   (acc["id"], b["period"], b.get("tariff"), b.get("price"), b.get("reading"), b.get("consumed"),
                    b.get("volume"), b.get("accrued"), b.get("payment"), b.get("paid_total"), b.get("balance"),
                    1 if b.get("has_invoice") else 0, b.get("status"), now))
        if not old and b.get("has_invoice") and b.get("accrued") is not None:
            new.append(b)
        if acc["gas_meter_id"] and b.get("reading") is not None:          # показання облгазу -> «Газ»
            import homegas
            homegas.write_reading(acc["gas_meter_id"], b["period"], b["reading"])
    db.execute("UPDATE ng_accounts SET checked_at=?, ok=1, error=NULL, cookie_expired=0, balance=?, debt=?, last_period=?, "
               "last_accrued=?, account_no=COALESCE(?, account_no), owner=COALESCE(?, owner), address=COALESCE(?, address) "
               "WHERE id=?", (now, summ["balance"], summ["debt"], summ.get("last_period"), summ.get("last_accrued"),
                              prof.get("account"), prof.get("owner"), prof.get("address"), acc["id"]))
    db.commit()
    return ([] if first else new), summ["debt"], None


def sync(kind="manual"):
    lock = _run_locks.get(current_site()) or threading.Lock()
    if not lock.acquire(blocking=False):
        return False, _("Проверка уже выполняется")
    try:
        ensure_schema()
        db = get_db()
        c = config()
        first = settings().get("naftogaz_initialized") != "1"
        msgs, total_debt, errs, n = [], 0.0, [], 0
        for acc in db.execute("SELECT * FROM ng_accounts ORDER BY sort, id").fetchall():
            new, debt, err = sync_account(acc, c, db, first)
            if err:
                errs.append(f"{acc['name']}: {err}")
                continue
            n += 1
            total_debt += debt or 0
            if c["notify_bills"]:
                for b in new:
                    msgs.append((f"bill:{acc['id']}:{b['period']}",
                                 f"🧾 <b>Новий рахунок Нафтогаз</b> — {_esc(acc['name'])}, {b['period']}\n"
                                 f"Нараховано: <b>{money(b.get('accrued'))} грн</b>"
                                 + (f"\nОб'єм: {money(b.get('volume'))} м³ × {money(b.get('price'))} грн" if b.get("volume") else "")))
            if c["notify_debt"] and (debt or 0) > 0 and not first:
                msgs.append((f"debt:{acc['id']}:{date.today().isoformat()}",
                             f"🔴 <b>Заборгованість Нафтогаз</b> — {_esc(acc['name'])}: <b>{money(debt)} грн</b>"))
        if first and n:
            set_setting("naftogaz_initialized", "1")
            msgs.append((None, f"✅ <b>Нафтогаз підключено</b>: рахунків {n}, заборгованість {money(total_debt)} грн"))
        sent = 0
        for key, text in msgs:
            if key and db.execute("SELECT 1 FROM ng_notified WHERE key=?", (key,)).fetchone():
                continue
            try:
                if tg_send(c, text):
                    sent += 1
                    if key:
                        db.execute("INSERT OR IGNORE INTO ng_notified(key) VALUES (?)", (key,))
            except Exception as e:  # noqa
                errs.append(f"Telegram: {e}"[:200])
        st = {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "ok": not errs, "errors": errs,
              "debt": round(total_debt, 2), "sent": sent}
        if kind == "auto":
            st["last_auto"] = date.today().isoformat()
        else:
            st["last_auto"] = json.loads(settings().get("naftogaz_status") or "{}").get("last_auto")
        set_setting("naftogaz_status", json.dumps(st, ensure_ascii=False))
        db.commit()
        try:
            from audit import log
            log("Кабинет Нафтогаз проверен", f"рахунків {n}, борг {money(total_debt)}" + (f"; помилки: {'; '.join(errs)}"[:400] if errs else ""),
                username="система")
            db.commit()
        except Exception:  # noqa
            pass
        return not errs, "; ".join(errs) or _("успешно")
    finally:
        lock.release()


def run_async(kind="manual"):
    app = current_app._get_current_object()
    site = current_site()

    def run():
        with app.app_context(), using_site(site):
            sync(kind)
    threading.Thread(target=run, daemon=True).start()


def _scheduler(app):
    lockf = None
    while True:
        time.sleep(60)
        try:
            if lockf is None:
                f = open(LOCK_FILE, "a")
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    lockf = f
                except OSError:
                    f.close()
                    continue
            for site in SITES:
                if SITES[site].get("kind") != "home":
                    continue
                try:
                    with app.app_context(), using_site(site):
                        c = config()
                        if not c["enabled"]:
                            continue
                        st = json.loads(settings().get("naftogaz_status") or "{}")
                        now = datetime.now()
                        if now.hour < c["hour"] or st.get("last_auto") == date.today().isoformat():
                            continue
                        if st.get("time") and now - datetime.fromisoformat(st["time"]) < timedelta(hours=1):
                            continue
                        sync("auto")
                except Exception:  # noqa
                    pass
        except Exception:  # noqa
            pass


# ---------------- сторінка ----------------

def _require():
    if not is_home_site():
        abort(404)
    if not auth.can("naftogaz"):
        abort(403, _("Нет доступа. Права выдаёт администратор в Настройки → Пользователи."))


@bp.route("/naftogaz")
def page():
    _require()
    ensure_schema()
    db = get_db()
    import homegas
    accs = db.execute("SELECT a.*, m.name AS meter FROM ng_accounts a LEFT JOIN gas_meters m ON m.id=a.gas_meter_id "
                      "ORDER BY a.sort, a.id").fetchall() if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='gas_meters'").fetchone() else \
        db.execute("SELECT a.*, NULL AS meter FROM ng_accounts a ORDER BY a.sort, a.id").fetchall()
    bills = {}
    for a in accs:
        bills[a["id"]] = db.execute("SELECT * FROM ng_bills WHERE acc_id=? ORDER BY period DESC LIMIT 24", (a["id"],)).fetchall()
    try:
        st = json.loads(settings().get("naftogaz_status") or "{}")
    except ValueError:
        st = {}
    return render_template("naftogaz.html", c=config(), st=st, accs=accs, bills=bills, meters=homegas.meters(db),
                           total_debt=round(sum(a["debt"] or 0 for a in accs), 2),
                           is_admin=g.user["role"] == "admin" or auth.can("settings"))


@bp.route("/naftogaz/run", methods=["POST"])
def run_now():
    _require()
    run_async("manual")
    flash(_("Проверка запущена — обновите страницу через полминуты"))
    return redirect(url_for("naftogaz.page"))


@bp.route("/naftogaz/settings", methods=["POST"])
def save_settings():
    _require()
    if not (g.user["role"] == "admin" or auth.can("settings")):
        abort(403)
    ensure_schema()
    db = get_db()
    f = request.form
    act = f.get("action", "save")
    if act == "account":
        aid = f.get("acc_id", type=int)
        mid = f.get("gas_meter_id", type=int)
        name = (f.get("name") or "").strip()
        url = (f.get("url") or DEFAULT_URL).strip().rstrip("/")
        if not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?(/[\w\-./]*)?", url):
            flash(_("Адрес кабинета должен начинаться с https://"))
            return redirect(url_for("naftogaz.page") + "#settings")
        cookie = vault.encrypt(clean_cookie(f.get("cookie"))) if (f.get("cookie") or "").strip() else None
        if f.get("delete") and aid:
            db.execute("DELETE FROM ng_accounts WHERE id=?", (aid,))
            flash(_("Удалено"))
        elif aid:
            db.execute("UPDATE ng_accounts SET name=?, gas_meter_id=?, url=?, cookie=COALESCE(?, cookie) WHERE id=?",
                       (name or "—", mid, url, cookie, aid))
            flash(_("Сохранено"))
        elif name:
            aid = db.execute("INSERT INTO ng_accounts(name, gas_meter_id, url, cookie) VALUES (?,?,?,?)",
                             (name, mid, url, cookie)).lastrowid
            flash(_("Сохранено"))
        db.commit()
        if f.get("test") and aid:
            acc = db.execute("SELECT * FROM ng_accounts WHERE id=?", (aid,)).fetchone()
            new, debt, err = sync_account(acc, config(), db, first=True)
            flash((_("Не удалось войти") + f": {err}") if err else
                  (_("Вход в кабинет успешен") + f" — {acc['name']}, " + _("задолженность") + f" {money(debt)} " + _("грн")))
        return redirect(url_for("naftogaz.page") + "#settings")
    set_setting("naftogaz_enabled", "1" if f.get("enabled") else "0")
    try:
        set_setting("naftogaz_hour", str(min(23, max(0, int(f.get("hour") or 9)))))
        set_setting("naftogaz_years", str(min(9, max(1, int(f.get("years") or 2)))))
    except ValueError:
        pass
    for k in ("notify_bills", "notify_debt"):
        set_setting("naftogaz_" + k, "1" if f.get(k) else "0")
    db.commit()
    flash(_("Настройки сохранены"))
    return redirect(request.form.get("back") or (url_for("naftogaz.page") + "#settings"))


def init_app(app):
    app.register_blueprint(bp)
    if os.environ.get("YASNO_SCHED", "1") != "0":
        threading.Thread(target=_scheduler, args=(app,), daemon=True).start()
