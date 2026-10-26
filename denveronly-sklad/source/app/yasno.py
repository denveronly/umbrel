"""Модуль YASNO: щодня заходить у бізнес-кабінет постачальника електроенергії (mybiz.kyiv.yasno.com.ua),
забирає нові рахунки (PDF), заборгованість і попередження про відключення, і шле пуші в Telegram.

Кабінет — звичайний сайт із формою входу (Rails, authenticity_token), тож браузер не потрібен:
сесія з cookie, форма розбирається автоматично (поля логіна/пароля шукаються за типом).
Пароль і токен бота — зашифровані (vault.py), ключ — поза базою та бекапами.
Дані й налаштування — в «Базі» (MAIN_SITE).
"""
import fcntl
import http.cookiejar
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import date, datetime, timedelta
from html.parser import HTMLParser

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import auth
import vault
from db import DATA_DIR, MAIN_SITE, get_db, set_setting, settings, using_site
from i18n import _

bp = Blueprint("yasno", __name__)

DEFAULT_URL = "https://mybiz.kyiv.yasno.com.ua"
TG_API = "https://api.telegram.org"
FILES_DIR = os.path.join(DATA_DIR, "yasno")
LAST_DIR = os.path.join(FILES_DIR, "last")           # останні сторінки кабінету — для діагностики
STATUS_FILE = os.path.join(DATA_DIR, "yasno_status.json")
LOCK_FILE = os.path.join(DATA_DIR, "yasno.lock")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TIMEOUT = 40
_run_lock = threading.Lock()

MONTHS = {"січ": 1, "лют": 2, "бер": 3, "кві": 4, "тра": 5, "чер": 6, "лип": 7, "сер": 8, "вер": 9, "жов": 10,
          "лис": 11, "гру": 12}
DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
BILL_RE = re.compile(r"\b(\d{5,}(?:/[0-9A-Za-zА-Яа-яІіЇїЄє]+)+)")


# ---------------- налаштування ----------------

def config():
    with using_site(MAIN_SITE):
        s = settings()
    return {
        "enabled": s.get("yasno_enabled") == "1",
        "url": (s.get("yasno_url") or DEFAULT_URL).rstrip("/"),
        "login": s.get("yasno_login") or "",
        "password": vault.decrypt(s.get("yasno_password") or ""),
        "has_password": bool(s.get("yasno_password")),
        "hour": int(s.get("yasno_hour") or 9),
        "tg_token": vault.decrypt(s.get("tg_token") or ""),
        "has_tg_token": bool(s.get("tg_token")),
        "tg_chat": (s.get("tg_chat") or "").strip(),
        "notify_bills": s.get("yasno_notify_bills", "1") == "1",
        "notify_debt": s.get("yasno_notify_debt", "1") == "1",
        "notify_warnings": s.get("yasno_notify_warnings", "1") == "1",
        "remind_days": int(s.get("yasno_remind_days") or 2),
    }


def read_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_status(**kw):
    st = read_status()
    st.update(kw)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATUS_FILE)


# ---------------- розбір HTML ----------------

def num(s):
    """'54 814,72' / '6 070' / '45 678.93' -> float; None якщо не число."""
    if s is None:
        return None
    t = re.sub(r"[\s  ]", "", str(s)).replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        return None
    return float(t)


def iso(s):
    m = DATE_RE.search(s or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def month_period(s):
    """'Жовтень 2026' -> '2026-10'."""
    m = re.search(r"([А-Яа-яІіЇїЄєҐґ]{3,})\s+(\d{4})", s or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1)[:3].lower())
    return f"{m.group(2)}-{mon:02d}" if mon else None


class Page(HTMLParser):
    """Збирає форми (з полями) і таблиці (рядки -> клітинки з текстом і посиланнями).
    Для кожної таблиці запам'ятовує текст перед нею (заголовок розділу)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.tables, self._tstack, self._form, self.meta = [], [], [], None, {}
        self._recent = ""          # останній видимий текст поза таблицями
        self._skip = 0             # script/style

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
                                         "value": a.get("value") or "", "tag": tag, "id": a.get("id") or "",
                                         "hidden_css": bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden",
                                                                      a.get("style") or "", re.I))})
        elif tag == "table":
            self._tstack.append({"rows": [], "before": self._recent.strip()[-160:], "row": None, "cell": None})
        elif tag == "tr" and self._tstack:
            t = self._tstack[-1]
            t["row"] = []
            t["rows"].append(t["row"])
        elif tag in ("td", "th") and self._tstack and self._tstack[-1]["row"] is not None:
            t = self._tstack[-1]
            t["cell"] = {"text": "", "links": [], "th": tag == "th"}
            t["row"].append(t["cell"])
        elif tag == "a" and self._tstack and self._tstack[-1]["cell"] is not None and a.get("href"):
            self._tstack[-1]["cell"]["links"].append({"href": a["href"], "text": ""})
        elif tag == "br" and self._tstack and self._tstack[-1]["cell"] is not None:
            self._tstack[-1]["cell"]["text"] += " "

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "form":
            self._form = None
        elif tag == "table" and self._tstack:
            t = self._tstack.pop()
            rows = [[{"text": re.sub(r"\s+", " ", c["text"]).strip(), "links": c["links"], "th": c["th"]} for c in r]
                    for r in t["rows"] if r]
            self.tables.append({"rows": rows, "before": t["before"]})
        elif tag in ("td", "th") and self._tstack:
            self._tstack[-1]["cell"] = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._tstack and self._tstack[-1]["cell"] is not None:
            c = self._tstack[-1]["cell"]
            c["text"] += data
            if c["links"]:
                c["links"][-1]["text"] += data
        elif not self._tstack and data.strip():
            self._recent = (self._recent + " " + data.strip())[-400:]


def parse(html):
    p = Page()
    p.feed(html or "")
    p.close()
    return p


def header_text(table):
    return " ".join(c["text"] for r in table["rows"][:2] for c in r).lower()


def parse_overview(html):
    """Головна сторінка: заборгованість за типами і неоплачені рахунки."""
    p = parse(html)
    debts, unpaid, topay = [], [], []
    for t in p.tables:
        h = header_text(t)
        if "заборгованіст" in h and "тип" in h:
            for r in t["rows"]:
                if len(r) >= 2 and num(r[1]["text"]) is not None:
                    debts.append({"type": r[0]["text"], "debt": num(r[1]["text"]),
                                  "saved": num(r[2]["text"]) if len(r) > 2 else None})
        elif "номер рахунку" in h and "термін" in h:
            target = topay if "все на оплату" in t["before"].lower() else unpaid
            for r in t["rows"]:
                txt = " ".join(c["text"] for c in r)
                m = BILL_RE.search(txt)
                if not m or r[0]["th"]:
                    continue
                nums = [num(c["text"]) for c in r]
                amount = next((x for x in nums if x is not None and x >= 0 and not BILL_RE.search(str(x))), None)
                target.append({"type": r[0]["text"], "number": m.group(1), "amount": amount, "due": iso(txt)})
    return {"debts": debts, "unpaid": unpaid, "topay": topay}


def _pdf_link(cells):
    links = [l for c in cells for l in c["links"]]
    for l in links:
        if "pdf" in (l["href"] + " " + l["text"]).lower():
            return l["href"]
    for l in links:
        if re.search(r"download|print|bills?/\d", l["href"], re.I) and "sign" not in l["href"].lower():
            return l["href"]
    return None


def parse_bills(html):
    """Сторінка «Рахунки»: номер, місяць, дати, кВт·год, грн, оплачено, до сплати, посилання на PDF."""
    out = []
    for t in parse(html).tables:
        if "номер рахунку" not in header_text(t):
            continue
        for r in t["rows"]:
            if not r or r[0]["th"]:
                continue
            m = BILL_RE.search(r[0]["text"])
            if not m:
                continue
            texts = [c["text"] for c in r]
            dates = [iso(x) for x in texts if DATE_RE.search(x)]
            # числа — після колонок з датами: кВт·год, грн, оплачено, до сплати
            last_date = max((i for i, x in enumerate(texts) if DATE_RE.search(x)), default=0)
            nums = [num(x) for x in texts[last_date + 1:]]
            nums = [x for x in nums if x is not None][:4] + [None] * 4
            out.append({"number": m.group(1), "planned": 1 if "планов" in r[0]["text"].lower() else 0,
                        "period": next((month_period(x) for x in texts[1:3] if month_period(x)), None),
                        "received": dates[0] if dates else None, "due": dates[1] if len(dates) > 1 else None,
                        "kwh": nums[0], "amount": nums[1], "paid": nums[2], "to_pay": nums[3],
                        "pdf_url": _pdf_link(r)})
    return out


def parse_warnings(html):
    out = []
    for t in parse(html).tables:
        h = header_text(t)
        if "попередження" not in h:
            continue
        for r in t["rows"]:
            if not r or r[0]["th"] or not re.fullmatch(r"\d{5,}", r[0]["text"]):
                continue
            texts = [c["text"] for c in r]
            dates = [iso(x) for x in texts if DATE_RE.fullmatch(x.strip() or "-")]
            reason = max(texts[1:], key=len) if len(texts) > 1 else ""
            reason = next((x for x in texts if "причина" in x.lower() or "заборгован" in x.lower()), reason)
            out.append({"number": texts[0], "contract": texts[1] if len(texts) > 1 else "",
                        "warn_date": dates[0] if dates else None, "recv_date": dates[1] if len(dates) > 1 else None,
                        "off_date": dates[2] if len(dates) > 2 else None, "reason": reason,
                        "resolved": 1 if re.search(r"сплачен|скасован|виконан", reason, re.I) else 0})
    return out


# ---------------- кабінет ----------------

class LoginError(Exception):
    pass


class RateLimited(LoginError):
    pass


class Cabinet:
    def __init__(self, base, fp=None):
        self.base = base.rstrip("/")
        self.fp = fp or uuid.uuid4().hex
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.pages = {}

    def get(self, path, data=None, binary=False, referer=None, headers=None, json_body=False, keep_errors=False):
        url = urllib.parse.urljoin(self.base + "/", path)
        if data is None:
            body = None
        elif json_body:
            body = json.dumps(data).encode()
        else:
            body = urllib.parse.urlencode(data).encode()
        h = {"User-Agent": UA, "Accept-Language": "uk-UA,uk;q=0.9",
             "Accept": "application/pdf,*/*" if binary else "text/html,application/xhtml+xml,*/*;q=0.8"}
        if referer:
            h["Referer"] = referer
        if body:
            h["Content-Type"] = "application/json" if json_body else "application/x-www-form-urlencoded"
            p = urllib.parse.urlsplit(self.base)
            h["Origin"] = f"{p.scheme}://{p.netloc}"
        h.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=h)
        try:
            with self.op.open(req, timeout=TIMEOUT) as r:
                raw = r.read()
                final = r.geturl()
                ctype = r.headers.get("Content-Type", "")
                self.status = r.status
        except urllib.error.HTTPError as e:
            if not keep_errors:
                raise
            raw, final, ctype, self.status = e.read(), url, e.headers.get("Content-Type", ""), e.code
        if binary:
            return final, raw, ctype
        cs = re.search(r"charset=([\w-]+)", ctype)
        return final, raw.decode(cs.group(1) if cs else "utf-8", errors="replace"), ctype

    def _form_data(self, form, login, password):
        """Поле логіна — видиме текстове поле з іменем на кшталт username/login/email. Приховані поля-пастки для
        ботів (напр. «website» з display:none) лишаються порожніми, інакше сайт відхиляє вхід (HTTP 422)."""
        texts = [i for i in form["inputs"] if i["tag"] == "input" and i["type"] in ("text", "email", "tel") and i["name"]
                 and not i["hidden_css"] and not re.search(r"website|url|homepage|honey|hp_", i["name"] + i["id"], re.I)]
        user = next((i for i in texts if re.search(r"login|user|email|phone|account", i["name"] + " " + i["id"], re.I)),
                    texts[0] if texts else None)
        data = {}
        for i in form["inputs"]:
            if not i["name"]:
                continue
            if i["type"] == "password":
                data[i["name"]] = password
            elif user is not None and i is user:
                data[i["name"]] = login
            elif i["type"] in ("checkbox", "radio", "submit", "button", "image", "file", "reset"):
                continue
            elif re.search(r"fingerprint", i["name"], re.I) and not i["value"]:
                data[i["name"]] = self.fp      # сайт заповнює його скриптом у браузері
            else:
                data[i["name"]] = i["value"]
        submit = next((i for i in form["inputs"] if i["type"] == "submit" and i["name"]), None)
        if submit:
            data[submit["name"]] = submit["value"]
        return data, user is not None

    def login(self, login, password):
        """Кілька способів по черзі (звичайна форма; форма з CSRF-заголовком як у Rails UJS/Turbo; AJAX),
        бо сайт може приймати вхід лише одним із них. Кожна відповідь зберігається для діагностики."""
        last_err = None
        for n, mode in enumerate(("form", "ujs"), 1):
            final, html, _ct = self.get("/")
            self.pages[f"login_{n}"] = html
            p = parse(html)
            form = next((f for f in p.forms if any(i["type"] == "password" for i in f["inputs"])), None)
            if not form:
                if self._logged_in(html, final):
                    return final
                raise LoginError("на сторінці входу не знайдено форму з паролем")
            data, user_done = self._form_data(form, login, password)
            if not user_done:
                raise LoginError("не знайдено поле логіна")
            token = p.meta.get("csrf-token")
            param = p.meta.get("csrf-param") or "authenticity_token"
            if token and param not in data:
                data[param] = token
            headers = {}
            if mode in ("ujs", "xhr") and token:
                headers["X-CSRF-Token"] = token
            if mode == "xhr":
                headers.update({"X-Requested-With": "XMLHttpRequest",
                                "Accept": "text/javascript, application/json, text/html, */*; q=0.01"})
            action = urllib.parse.urljoin(final, form["action"] or final)
            final, html, ctype = self.get(action, data=data, referer=final, headers=headers, keep_errors=True)
            self.pages[f"after_login_{n}"] = f"<!-- HTTP {self.status} {ctype} {final} -->\n" + html
            # AJAX-відповідь з адресою переходу
            if "json" in ctype or "javascript" in ctype:
                loc = re.search(r'(?:location|redirect(?:_to|_url)?|url)"?\s*[:=]\s*"([^"]+)"', html) or \
                    re.search(r"(?:location\.href|window\.location|Turbo\.visit)\s*(?:=|\()\s*['\"]([^'\"]+)", html)
                if loc:
                    final, html, ctype = self.get(loc.group(1), keep_errors=True)
                    self.pages[f"after_login_{n}_next"] = html
            if "rate_limited" in final or re.search(r"занадто багато спроб|too many", html, re.I):
                raise RateLimited("кабінет тимчасово заблокував вхід: «Занадто багато спроб». Зачекайте 1–2 години "
                                  "і не натискайте перевірку повторно — кожна спроба продовжує блокування")
            if self.status < 400 and self._logged_in(html, final):
                return final
            # перевірити сесію: чи відкривається кабінет
            if self.status < 400:
                f2, h2, _c = self.get("/", keep_errors=True)
                if self._logged_in(h2, f2):
                    return f2
            # повідомлення сайту: <div class="page-error">…</div> (або flash/alert), інакше — ключові слова
            box = re.search(r'class="[^"]*(?:page-error|flash|alert|error|notice)[^"]*"[^>]*>(.*?)</(?:div|p|span)>', html,
                            re.I | re.S)
            box_txt = re.sub(r"<[^>]+>|\s+", " ", box.group(1)).strip() if box else ""
            msg = re.search(r"(невірн[^<.]{0,80}|неправильн[^<.]{0,80}|не знайден[^<.]{0,60}|заблоковано[^<.]{0,60})",
                            html, re.I)
            if box_txt:
                msg = re.match(r"(.*)", box_txt)
            last_err = ("кабінет: " + msg.group(1).strip()) if msg else \
                f"кабінет не прийняв вхід (HTTP {self.status})"
            if msg or self.status < 400:   # сайт відповів сторінкою (не помилкою) — інший спосіб не допоможе
                break
        raise LoginError(last_err + ". Якщо логін і пароль вірні — натисніть «Завантажити сторінки для діагностики» "
                                    "і надішліть файл розробнику")

    def _logged_in(self, html, url=""):
        return not self._is_login_page(url, html) and bool(re.search(r'href="/logout"|sign_out|Вийти', html, re.I))

    def page(self, name, path):
        final, html, _ct = self.get(path)
        self.pages[name] = html
        if self._is_login_page(final, html):
            raise LoginError(f"сесія закрилась на сторінці {path}")
        return html

    @staticmethod
    def _is_login_page(url, html):
        """Сторінка входу — форма на /login з логіном і паролем (на інших сторінках бувають поля пароля
        ключа КЕП для підпису документів — це не вихід із кабінету)."""
        if re.search(r"/login(\?|$)", urllib.parse.urlsplit(url).path + ("?" if "?" in url else "")):
            return True
        for f in parse(html).forms:
            names = {i["name"] for i in f["inputs"]}
            if re.search(r"/(login|sign_in|session)", f["action"] or "") and any(i["type"] == "password" for i in f["inputs"]) \
                    and ({"username", "login", "user[login]", "user[email]", "email"} & names):
                return True
        return False


def device_fingerprint():
    """Постійний «відбиток пристрою» програми (як FingerprintJS у браузері) — щоб кабінет бачив той самий пристрій."""
    with using_site(MAIN_SITE):
        fp = settings().get("yasno_fp")
        if not fp:
            fp = uuid.uuid4().hex
            set_setting("yasno_fp", fp)
            get_db().commit()
    return fp


def fetch(c):
    """Зайти в кабінет і зібрати все. -> (data, cabinet)."""
    if not (c["login"] and c["password"]):
        raise LoginError("не задано логін або пароль (або пароль треба ввести знову після відновлення бекапу)")
    cab = Cabinet(c["url"], device_fingerprint())
    try:
        cab.login(c["login"], c["password"])
        data = parse_overview(cab.page("overview", "/"))
        data["bills"] = parse_bills(cab.page("bills", "/bills"))
        try:
            data["warnings"] = parse_warnings(cab.page("warnings", "/warnings"))
        except urllib.error.HTTPError:
            data["warnings"] = []
        return data, cab
    finally:
        _save_pages(cab)


def _save_pages(cab):
    os.makedirs(LAST_DIR, exist_ok=True)
    for name, html in cab.pages.items():
        with open(os.path.join(LAST_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(html)


def download_pdf(cab, bill):
    if not bill.get("pdf_url"):
        return None
    _f, raw, ctype = cab.get(bill["pdf_url"], binary=True, referer=urllib.parse.urljoin(cab.base + "/", "bills"))
    if not raw.startswith(b"%PDF"):
        return None
    os.makedirs(FILES_DIR, exist_ok=True)
    name = re.sub(r"[^\w.-]+", "_", bill["number"]) + ".pdf"
    with open(os.path.join(FILES_DIR, name), "wb") as f:
        f.write(raw)
    return name


# ---------------- Telegram ----------------

def tg_call(token, method, fields=None, file=None):
    url = f"{TG_API}/bot{token}/{method}"
    if file:
        boundary = uuid.uuid4().hex
        buf = io.BytesIO()
        for k, v in (fields or {}).items():
            buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        fname, content = file
        buf.write(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{fname}"\r\n'
                  f'Content-Type: application/pdf\r\n\r\n'.encode())
        buf.write(content + f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(url, data=buf.getvalue(),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=json.dumps(fields or {}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            res = json.loads(e.read().decode())
        except ValueError:
            res = {"ok": False, "description": f"HTTP {e.code}"}
    if not res.get("ok"):
        raise RuntimeError(res.get("description") or "Telegram error")
    return res.get("result")


def tg_send(c, text, pdf=None):
    if not (c["tg_token"] and c["tg_chat"]):
        return False
    if pdf and os.path.exists(os.path.join(FILES_DIR, pdf)):
        with open(os.path.join(FILES_DIR, pdf), "rb") as f:
            tg_call(c["tg_token"], "sendDocument", {"chat_id": c["tg_chat"], "caption": text[:1000], "parse_mode": "HTML"},
                    file=(pdf, f.read()))
    else:
        tg_call(c["tg_token"], "sendMessage", {"chat_id": c["tg_chat"], "text": text, "parse_mode": "HTML",
                                                "disable_web_page_preview": True})
    return True


def money(x):
    return "—" if x is None else f"{x:,.2f}".replace(",", " ").replace(".", ",")


def kwh(x):
    return "—" if x is None else f"{round(x):,}".replace(",", " ")


def dmy(s):
    return f"{s[8:10]}.{s[5:7]}.{s[:4]}" if s and len(s) >= 10 else (s or "—")


# ---------------- синхронізація ----------------

def _notified(db, key):
    return db.execute("SELECT 1 FROM yasno_notified WHERE key=?", (key,)).fetchone() is not None


def _mark(db, key):
    db.execute("INSERT OR IGNORE INTO yasno_notified(key) VALUES (?)", (key,))


def sync(kind="manual", app=None):
    """Повний цикл: вхід, рахунки, PDF, заборгованість, попередження, пуші. -> (ok, повідомлення)."""
    if not _run_lock.acquire(blocking=False):
        return False, "перевірка вже виконується"
    try:
        return _sync(kind)
    finally:
        _run_lock.release()


def _sync(kind):
    c = config()
    now = datetime.now()
    _write_status(last_attempt=now.isoformat(timespec="seconds"), running=True)
    try:
        data, cab = fetch(c)
    except Exception as e:
        err = f"{type(e).__name__}: {e}" if not isinstance(e, LoginError) else str(e)
        err = err[:400]
        _write_status(running=False, ok=False, error=err, time=now.isoformat(timespec="seconds"), kind=kind,
                      rate_limited=isinstance(e, RateLimited))
        with using_site(MAIN_SITE):
            db = get_db()
            key = f"err:{date.today().isoformat()}"
            if not _notified(db, key):
                try:
                    if tg_send(c, f"⚠️ <b>YASNO</b>: не вдалося перевірити кабінет.\n{_esc(err)}"):
                        _mark(db, key)
                except Exception:
                    pass
            _audit("Ошибка проверки кабинета YASNO", err)
            db.commit()
        return False, err

    msgs, new_bills, sent = [], [], 0
    with using_site(MAIN_SITE):
        db = get_db()
        first = settings().get("yasno_initialized") != "1"
        for b in data["bills"]:
            old = db.execute("SELECT * FROM yasno_bills WHERE number=?", (b["number"],)).fetchone()
            vals = (b["planned"], b["period"], b["received"], b["due"], b["kwh"], b["amount"], b["paid"], b["to_pay"],
                    b["pdf_url"], now.isoformat(timespec="seconds"), b["number"])
            if old:
                db.execute("UPDATE yasno_bills SET planned=?, period=?, received=?, due=?, kwh=?, amount=?, paid=?, "
                           "to_pay=?, pdf_url=?, updated_at=? WHERE number=?", vals)
            else:
                db.execute("INSERT INTO yasno_bills(planned, period, received, due, kwh, amount, paid, to_pay, pdf_url, "
                           "updated_at, number) VALUES (?,?,?,?,?,?,?,?,?,?,?)", vals)
                new_bills.append(b)
            if not (old and old["pdf"]):
                try:
                    pdf = download_pdf(cab, b)
                except Exception:
                    pdf = None
                if pdf:
                    db.execute("UPDATE yasno_bills SET pdf=? WHERE number=?", (pdf, b["number"]))
                    b["pdf"] = pdf
        for w in data["warnings"]:
            old = db.execute("SELECT 1 FROM yasno_warnings WHERE number=?", (w["number"],)).fetchone()
            if old:
                db.execute("UPDATE yasno_warnings SET reason=?, resolved=?, off_date=? WHERE number=?",
                           (w["reason"], w["resolved"], w["off_date"], w["number"]))
            else:
                db.execute("INSERT INTO yasno_warnings(number, contract, warn_date, recv_date, off_date, reason, resolved) "
                           "VALUES (?,?,?,?,?,?,?)", (w["number"], w["contract"], w["warn_date"], w["recv_date"],
                                                      w["off_date"], w["reason"], w["resolved"]))
        db.commit()

        debt = round(sum(d["debt"] or 0 for d in data["debts"]), 2)
        overdue = [u for u in data["unpaid"] if u["due"] and u["due"] < date.today().isoformat()]
        msgs_to_send = []        # (key, text, pdf)
        if first:
            # перший запуск — без лавини старих рахунків: одне зведення
            for b in data["bills"]:
                _mark(db, f"bill:{b['number']}")
            for w in data["warnings"]:
                _mark(db, f"warn:{w['number']}")
            last = data["bills"][0] if data["bills"] else None
            t = ["✅ <b>YASNO підключено</b>", f"Рахунків у кабінеті: {len(data['bills'])}"]
            if last:
                t.append(f"Останній: №{_esc(last['number'])} — {kwh(last['kwh'])} кВт·год, {money(last['amount'])} грн"
                         + (f", до сплати {money(last['to_pay'])} грн до {dmy(last['due'])}" if last["to_pay"] else ""))
            t.append(f"Заборгованість: {money(debt)} грн")
            if overdue:
                t.append("Прострочені рахунки: " + ", ".join(f"№{_esc(u['number'])} {money(u['amount'])} грн (до {dmy(u['due'])})"
                                                          for u in overdue))
            msgs_to_send.append((None, "\n".join(t), None))
            for u in overdue:
                _mark(db, f"overdue:{u['number']}")
            set_setting("yasno_initialized", "1")
        else:
            if c["notify_bills"]:
                for b in new_bills:
                    msgs_to_send.append((f"bill:{b['number']}",
                                         f"🧾 <b>Новий рахунок YASNO</b> №{_esc(b['number'])}"
                                         + (" (плановий)" if b["planned"] else "") + "\n"
                                         f"{kwh(b['kwh'])} кВт·год · {money(b['amount'])} грн\n"
                                         f"До сплати: <b>{money(b['to_pay'])} грн</b> до {dmy(b['due'])}"
                                         + (f"\nТариф ≈ {money(b['amount'] / b['kwh'])} грн/кВт·год з ПДВ"
                                            if b["kwh"] and b["amount"] else ""),
                                         b.get("pdf")))
            if c["notify_debt"]:
                if debt > 0:
                    lines = "\n".join(f"• {_esc(d['type'])}: {money(d['debt'])} грн" for d in data["debts"] if d["debt"])
                    msgs_to_send.append((f"debt:{date.today().isoformat()}",
                                         f"🔴 <b>Заборгованість у кабінеті YASNO: {money(debt)} грн</b>\n{lines}", None))
                for u in overdue:
                    msgs_to_send.append((f"overdue:{u['number']}",
                                         f"⏰ <b>Прострочено рахунок YASNO</b> №{_esc(u['number'])}: {money(u['amount'])} грн, "
                                         f"термін сплати був {dmy(u['due'])}", None))
                soon = (date.today() + timedelta(days=c["remind_days"])).isoformat()
                for b in data["bills"]:
                    if (b["to_pay"] or 0) > 0 and b["due"] and date.today().isoformat() <= b["due"] <= soon:
                        msgs_to_send.append((f"due:{b['number']}",
                                             f"💳 Нагадування YASNO: рахунок №{_esc(b['number'])} — "
                                             f"<b>{money(b['to_pay'])} грн</b> сплатити до {dmy(b['due'])}", None))
            if c["notify_warnings"]:
                for w in data["warnings"]:
                    if not w["resolved"]:
                        msgs_to_send.append((f"warn:{w['number']}",
                                             f"🚨 <b>Попередження YASNO про відключення</b> №{_esc(w['number'])}\n"
                                             f"Дата відключення: {dmy(w['off_date'])}\n{_esc(w['reason'][:300])}", None))
        tg_err = None
        for key, text, pdf in msgs_to_send:
            if key and _notified(db, key):
                continue
            try:
                if tg_send(c, text, pdf):
                    sent += 1
                    if key:
                        _mark(db, key)
            except Exception as e:
                tg_err = str(e)[:200]
        db.commit()
        msg = (f"рахунків {len(data['bills'])}, нових {len(new_bills)}, заборгованість {money(debt)} грн, "
               f"попереджень {len(data['warnings'])}, повідомлень у Telegram {sent}")
        if tg_err:
            msg += f"; помилка Telegram: {tg_err}"
        _audit("Кабинет YASNO проверен", msg)
        db.commit()
    _write_status(running=False, ok=True, error=tg_err or "", rate_limited=False, time=now.isoformat(timespec="seconds"), kind=kind,
                  summary=msg, debt=debt, debts=data["debts"], unpaid=data["unpaid"], topay=data["topay"],
                  last_auto=date.today().isoformat() if kind == "auto" else read_status().get("last_auto"))
    return True, msg


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _audit(action, details):
    try:
        from audit import log
        log(action, details, username="система")
    except Exception:
        pass


def run_async(kind="manual"):
    from flask import current_app
    app = current_app._get_current_object()

    def run():
        with app.app_context():
            sync(kind)
    threading.Thread(target=run, daemon=True).start()


# ---------------- щоденний запуск ----------------

def _scheduler(app):
    """Один потік на весь контейнер (файлове блокування між воркерами gunicorn)."""
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
            with app.app_context():
                c = config()
                if not c["enabled"]:
                    continue
                st, now = read_status(), datetime.now()
                if now.hour < c["hour"] or st.get("last_auto") == date.today().isoformat() or st.get("running"):
                    continue
                last = st.get("last_attempt")
                wait = timedelta(hours=3 if st.get("rate_limited") else 1)
                if last and now - datetime.fromisoformat(last) < wait:
                    continue          # після невдачі — повтор не частіше раз на годину
                sync("auto")
        except Exception:
            pass


def init_app(app):
    app.register_blueprint(bp)
    if os.environ.get("YASNO_SCHED", "1") != "0":
        threading.Thread(target=_scheduler, args=(app,), daemon=True).start()
    st = read_status()
    if st.get("running"):          # процес перезапустився посеред перевірки
        try:
            _write_status(running=False)
        except OSError:
            pass


# ---------------- сторінка ----------------

def _require():
    if not auth.can("yasno"):
        abort(403, _("Нет доступа. Права выдаёт администратор в Настройки → Пользователи."))


@bp.route("/yasno")
def page():
    _require()
    with using_site(MAIN_SITE):
        db = get_db()
        bills = db.execute("SELECT * FROM yasno_bills ORDER BY COALESCE(received, '') DESC, number DESC LIMIT 60").fetchall()
        warns = db.execute("SELECT * FROM yasno_warnings ORDER BY COALESCE(warn_date, '') DESC").fetchall()
    c = config()
    return render_template("yasno.html", c=c, st=read_status(), bills=bills, warns=warns, today_iso=date.today().isoformat(),
                           is_admin=_is_admin(),
                           key_lost=(c["has_password"] and c["password"] is None) or (c["has_tg_token"] and c["tg_token"] is None))


@bp.route("/yasno/run", methods=["POST"])
def run_now():
    _require()
    if read_status().get("running"):
        flash(_("Проверка уже выполняется"))
    else:
        run_async("manual")
        flash(_("Проверка запущена — обновите страницу через полминуты"))
    return redirect(url_for("yasno.page"))


@bp.route("/yasno/status")
def status():
    _require()
    st = read_status()
    return jsonify(running=bool(st.get("running")), time=st.get("time"))


@bp.route("/yasno/pdf/<path:number>")
def pdf(number):
    _require()
    with using_site(MAIN_SITE):
        b = get_db().execute("SELECT pdf FROM yasno_bills WHERE number=?", (number,)).fetchone()
    if not b or not b["pdf"] or not os.path.exists(os.path.join(FILES_DIR, b["pdf"])):
        abort(404)
    return send_file(os.path.join(FILES_DIR, b["pdf"]), mimetype="application/pdf",
                     download_name=f"YASNO_{b['pdf']}", as_attachment=request.args.get("dl") == "1")


def _is_admin():
    u = g.get("user")
    return bool(u) and u["role"] == "admin"


def _admin():
    if not _is_admin():
        abort(403)


@bp.route("/yasno/settings", methods=["POST"])
def save_settings():
    _admin()
    f = request.form
    action = f.get("action", "save")
    with using_site(MAIN_SITE):
        set_setting("yasno_enabled", "1" if f.get("enabled") else "0")
        url = (f.get("url") or DEFAULT_URL).strip().rstrip("/")
        if not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?(/[\w\-./]*)?", url):
            flash(_("Адрес кабинета должен начинаться с https://"))
            return redirect(url_for("yasno.page") + "#settings")
        set_setting("yasno_url", url)
        set_setting("yasno_login", (f.get("login") or "").strip())
        if f.get("password"):
            set_setting("yasno_password", vault.encrypt(f.get("password")))
        try:
            set_setting("yasno_hour", str(min(23, max(0, int(f.get("hour") or 9)))))
            set_setting("yasno_remind_days", str(min(10, max(0, int(f.get("remind_days") or 2)))))
        except ValueError:
            pass
        if f.get("tg_token"):
            tok = f.get("tg_token").strip()
            if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", tok):
                flash(_("Токен бота выглядит неверно — скопируйте его из @BotFather целиком"))
                return redirect(url_for("yasno.page") + "#settings")
            set_setting("tg_token", vault.encrypt(tok))
        set_setting("tg_chat", (f.get("tg_chat") or "").strip())
        for k in ("notify_bills", "notify_debt", "notify_warnings"):
            set_setting("yasno_" + k, "1" if f.get(k) else "0")
        get_db().commit()
        _audit("Настройки YASNO изменены", "пароль изменён" if f.get("password") else None)
        get_db().commit()
    c = config()
    if action == "tg_test":
        try:
            ok = tg_send(c, "✅ Тест: повідомлення з програми «Склади» (модуль YASNO)")
            flash(_("Сообщение отправлено в Telegram") if ok else _("Укажите токен бота и chat_id"))
        except Exception as e:
            flash(_("Ошибка Telegram") + f": {e}")
    elif action == "tg_chat":
        try:
            if not c["tg_token"]:
                raise RuntimeError(_("сначала сохраните токен бота"))
            ups = tg_call(c["tg_token"], "getUpdates", {"limit": 20}) or []
            chats = []
            for u in ups:
                m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
                ch = m.get("chat") or {}
                if ch.get("id") and ch["id"] not in [x[0] for x in chats]:
                    chats.append((ch["id"], ch.get("title") or " ".join(filter(None, [ch.get("first_name"), ch.get("last_name")]))
                                  or ch.get("username") or ""))
            if not chats:
                flash(_("Боту ещё никто не писал: откройте бота в Telegram, нажмите «Start» (или напишите любое сообщение) и повторите"))
            else:
                if not c["tg_chat"]:
                    with using_site(MAIN_SITE):
                        set_setting("tg_chat", str(chats[-1][0]))
                        get_db().commit()
                flash(_("Найдены чаты") + ": " + ", ".join(f"{cid} ({name})" for cid, name in chats))
        except Exception as e:
            flash(_("Ошибка Telegram") + f": {e}")
    elif action == "test":
        try:
            cab = Cabinet(c["url"], device_fingerprint())
            if not (c["login"] and c["password"]):
                raise LoginError(_("укажите логин и пароль"))
            try:
                cab.login(c["login"], c["password"])
                n = len(parse_bills(cab.page("bills", "/bills")))
            finally:
                _save_pages(cab)
            flash(_("Вход в кабинет успешен") + f", {_('счетов на странице')}: {n}")
        except Exception as e:
            flash(_("Не удалось войти") + f": {e}")
    else:
        flash(_("Настройки сохранены"))
    return redirect(url_for("yasno.page") + "#settings")


@bp.route("/yasno/debug.zip")
def debug_zip():
    """Останні сторінки кабінету (без пароля) — щоб розробник міг підлаштувати розбір, якщо YASNO змінить сайт."""
    _admin()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.isdir(LAST_DIR):
            for n in sorted(os.listdir(LAST_DIR)):
                z.write(os.path.join(LAST_DIR, n), n)
        st = dict(read_status())
        z.writestr("status.json", json.dumps(st, ensure_ascii=False, indent=1))
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"yasno_debug_{datetime.now():%Y%m%d_%H%M}.zip")
