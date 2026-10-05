"""ШІ-помічник: Claude через Anthropic API з інструментами (tool use), що працюють з даними застосунку.

Користувач дає текст + файли (CSV/XLSX/TXT) або посилання на Google Таблицю; Claude читає дані застосунку
й вносить зміни через інструменти. Перед першою зміною в запиті робиться бекап — його можна відкотити.
Видаляти дані інструменти не вміють.
"""
import csv
import io
import json
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for)

import auth
import calc
from audit import log
from db import PAYMENT_TYPES, SERVICE_MODES, get_db, price_for, settings
from i18n import _

bp = Blueprint("ai", __name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5-20251001"]
DEFAULT_MODEL = "claude-sonnet-5-5"
MAX_STEPS = 40                # максимум звернень до моделі в одному запиті
MAX_INPUT_CHARS = 180_000     # обмеження на дані з файлів/таблиць

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_sessions (
    id INTEGER PRIMARY KEY,
    title TEXT,
    username TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS ai_messages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES ai_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,              -- user | assistant | event
    content TEXT NOT NULL,           -- JSON (формат Messages API) або текст події
    job_id TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS ai_jobs (
    id TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL,
    status TEXT NOT NULL,            -- running | done | error | undone
    backup TEXT,                     -- бекап перед першою зміною
    changes INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT DEFAULT (datetime('now', 'localtime')),
    finished_at TEXT
);
"""

SYSTEM_PROMPT = """Ти — помічник у веб-застосунку обліку складської бази в Україні (оренда складів, комунальні послуги, акти).
Користувач дає дані (таблиці з Google Sheets, CSV/XLSX, текст) і просить внести їх у застосунок. Ти робиш це через інструменти.

Як працювати:
1. Спочатку подивись, що вже є (list_* інструменти), щоб не створити дублікатів. Склад/орендаря з такою ж назвою — оновлюй, а не створюй новий.
2. Звір структуру даних користувача з тим, що просять. Якщо щось критично неоднозначне (незрозуміло, яка колонка — показник, а яка — номер лічильника; невідомий місяць) — спитай коротко, нічого не змінюючи.
3. Вноси зміни пакетами (інструменти приймають списки), не по одному рядку, якщо можна.
4. Наприкінці коротко підсумуй мовою користувача (українською чи російською): що створено/оновлено, що пропущено і чому. Без зайвої води.

Правила предметної області:
- Період — рядок YYYY-MM. Показники лічильників — на кінець місяця.
- Ціна складу одна: грн за м² на місяць у формі оплати орендаря (для «bank_vat» — вже з ПДВ). Можна передати price_month — суму за місяць.
- Форми оплати: bank (безготівка без ПДВ), bank_vat (безготівка з ПДВ), cash (готівка).
- Тип сторони орендаря: company (юрособа/ФОП, діє директор) або person (фізособа з паспортом).
- Послуги: meter (за лічильником), qty (кількість за місяць), area (за м²), fixed (фіксовано). Лічильник прив'язаний до складу й послуги типу meter.
- Видаляти дані ти не можеш і не намагайся. Не вигадуй даних, яких немає у джерелі.
- Числа з комою (1 234,56) перетворюй на 1234.56.
- Лінії електроенергії: на базі кілька вводів (ліній). У лінії є власні лічильники (role: input — ввідний лічильник лінії,
  solar — сонячна генерація, own — власні споживачі бази: охорона, котельня, освітлення) і лічильники складів орендарів,
  прив'язані до лінії (assign_meters_to_line). Показники складів — set_readings (можна з reactive — реактивна енергія),
  показники власних лічильників лінії — set_line_readings. Таблиця «по лінії» (як аркуш «База 76»): колонки — лічильники,
  рядки — місяці; кожен лічильник має показник і витрату, реактивну — якщо є. Переносиш показники, а не витрату.
- Окремий аркуш Google Таблиці читай через get_google_sheet (url + назва аркуша).
"""

# ---------------- інструменти ----------------

TOOLS = [
    {"name": "list_warehouses", "description": "Список складів: id, назва, площа, ціна за м², орендар.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_tenants", "description": "Список орендарів (контактів) з реквізитами, формою оплати, компанією та складами.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_companies", "description": "Компанії-отримувачі коштів (орендодавці).", "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_services", "description": "Послуги (електроенергія, вода, вивіз сміття…) з типом і останньою ціною.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "list_meters", "description": "Лічильники: id, склад, послуга, номер, коефіцієнт, початковий і останній показник.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_readings", "description": "Показники всіх лічильників за місяць.",
     "input_schema": {"type": "object", "properties": {"period": {"type": "string", "description": "YYYY-MM"}}, "required": ["period"]}},
    {"name": "upsert_warehouses", "description": "Створити або оновити склади (за назвою, без урахування регістру). Можна одразу задати ціну й орендаря.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "name": {"type": "string"}, "area": {"type": "number", "description": "м²"},
         "price_m2": {"type": "number", "description": "грн за м²/міс у формі оплати орендаря"},
         "price_month": {"type": "number", "description": "або сума за місяць"},
         "tenant": {"type": "string", "description": "назва орендаря, до якого прив'язати (має існувати)"},
         "note": {"type": "string"}}, "required": ["name"]}}}, "required": ["items"]}},
    {"name": "upsert_tenants", "description": "Створити або оновити орендарів (за назвою).",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "name": {"type": "string", "description": "назва ТОВ/ФОП або ПІБ фізособи"},
         "party_type": {"type": "string", "enum": ["company", "person"]},
         "edrpou": {"type": "string", "description": "ЄДРПОУ / РНОКПП"}, "iban": {"type": "string"}, "address": {"type": "string"},
         "director": {"type": "string", "description": "ПІБ директора (для company)"},
         "director_position": {"type": "string"}, "basis": {"type": "string", "description": "діє на підставі (Статуту…)"},
         "passport_series": {"type": "string"}, "passport_number": {"type": "string"}, "passport_issued": {"type": "string"},
         "payment_type": {"type": "string", "enum": ["bank", "bank_vat", "cash"]},
         "company": {"type": "string", "description": "назва компанії-отримувача (має існувати)"},
         "contract_no": {"type": "string"}, "contract_date": {"type": "string"}, "contract_end": {"type": "string"},
         "deposit_amount": {"type": "number"}, "deposit_date": {"type": "string"},
         "phone": {"type": "string", "description": "телефон контактної особи"}, "contact_person": {"type": "string"},
         "active": {"type": "boolean", "description": "false — орендар виїхав (склади звільняються)"},
         "note": {"type": "string"}}, "required": ["name"]}}}, "required": ["items"]}},
    {"name": "attach_warehouses", "description": "Закріпити склади за орендарями.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "warehouse": {"type": "string"}, "tenant": {"type": "string"}}, "required": ["warehouse", "tenant"]}}}, "required": ["items"]}},
    {"name": "upsert_companies", "description": "Створити або оновити компанії-отримувачі.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "name": {"type": "string"}, "edrpou": {"type": "string"}, "iban": {"type": "string"}, "bank": {"type": "string"},
         "address": {"type": "string"}, "signer": {"type": "string"}, "signer_position": {"type": "string"},
         "basis": {"type": "string"}, "city": {"type": "string"}, "vat_payer": {"type": "boolean"},
         "act_prefix": {"type": "string"}}, "required": ["name"]}}}, "required": ["items"]}},
    {"name": "upsert_services", "description": "Створити або оновити послуги.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "name": {"type": "string", "description": "назва українською, як в акті"}, "unit": {"type": "string"},
         "mode": {"type": "string", "enum": ["meter", "qty", "area", "fixed"]}}, "required": ["name", "mode"]}}}, "required": ["items"]}},
    {"name": "subscribe_services", "description": "Підключити орендарю послуги типу qty/area/fixed.",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "tenant": {"type": "string"}, "service": {"type": "string"}}, "required": ["tenant", "service"]}}}, "required": ["items"]}},
    {"name": "add_meters", "description": "Додати лічильники до складів (якщо лічильник з таким номером на складі вже є — оновити).",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "warehouse": {"type": "string"}, "service": {"type": "string", "description": "послуга типу meter"},
         "serial": {"type": "string"}, "coef": {"type": "number"}, "initial_value": {"type": "number"}},
         "required": ["warehouse", "service"]}}}, "required": ["items"]}},
    {"name": "set_readings", "description": "Внести показники лічильників за місяці. Лічильник: meter_id або склад+послуга(+номер).",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "period": {"type": "string", "description": "YYYY-MM"}, "value": {"type": "number"},
         "reactive": {"type": "number", "description": "реактивна енергія, якщо є"},
         "meter_id": {"type": "integer"}, "warehouse": {"type": "string"}, "service": {"type": "string"}, "serial": {"type": "string"}},
         "required": ["period", "value"]}}}, "required": ["items"]}},
    {"name": "set_service_prices", "description": "Ціни послуг за місяці (грн за одиницю, без ПДВ).",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "service": {"type": "string"}, "period": {"type": "string"}, "price": {"type": "number"}},
         "required": ["service", "period", "price"]}}}, "required": ["items"]}},
    {"name": "set_quantities", "description": "Кількість за місяць для послуг типу qty (напр. вивіз сміття).",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "tenant": {"type": "string"}, "service": {"type": "string"}, "period": {"type": "string"}, "qty": {"type": "number"}},
         "required": ["tenant", "service", "period", "qty"]}}}, "required": ["items"]}},
    {"name": "get_google_sheet", "description": "Прочитати аркуш Google Таблиці як CSV (таблиця має бути доступна за посиланням). sheet — назва аркуша, напр. «База 76».",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}, "sheet": {"type": "string"}}, "required": ["url"]}},
    {"name": "list_lines", "description": "Лінії електроенергії з їх власними лічильниками (ввід/сонце/власні) і прив'язаними лічильниками складів.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "upsert_lines", "description": "Створити або оновити лінії електроенергії (за назвою).",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "name": {"type": "string"}, "note": {"type": "string"}}, "required": ["name"]}}}, "required": ["items"]}},
    {"name": "upsert_line_meters", "description": "Створити або оновити власні лічильники лінії (за лінією + назвою).",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "line": {"type": "string"}, "name": {"type": "string"}, "role": {"type": "string", "enum": ["input", "solar", "own"]},
         "serial": {"type": "string"}, "coef": {"type": "number"}, "initial_value": {"type": "number"},
         "has_reactive": {"type": "boolean"}}, "required": ["line", "name"]}}}, "required": ["items"]}},
    {"name": "assign_meters_to_line", "description": "Прив'язати лічильники складів до лінії. Лічильник: meter_id або склад+послуга(+номер).",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "line": {"type": "string"}, "meter_id": {"type": "integer"}, "warehouse": {"type": "string"},
         "service": {"type": "string"}, "serial": {"type": "string"}, "has_reactive": {"type": "boolean"}},
         "required": ["line"]}}}, "required": ["items"]}},
    {"name": "set_line_readings", "description": "Показники власних лічильників лінії за місяці (активна і, якщо є, реактивна).",
     "input_schema": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "line": {"type": "string"}, "meter": {"type": "string", "description": "назва лічильника лінії"},
         "period": {"type": "string"}, "value": {"type": "number"}, "reactive": {"type": "number"}},
         "required": ["line", "meter", "period"]}}}, "required": ["items"]}},
]
WRITE_TOOLS = {t["name"] for t in TOOLS if not t["name"].startswith(("list_", "get_"))}
LINE_ROLES = ("input", "solar", "own")


class ToolError(Exception):
    pass


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower().replace("’", "'")


def _find(table, name, col="name"):
    if name is None or name == "":
        return None
    if isinstance(name, int) or str(name).isdigit():
        r = get_db().execute(f"SELECT * FROM {table} WHERE id=?", (int(name),)).fetchone()
        if r:
            return r
    for r in get_db().execute(f"SELECT * FROM {table}").fetchall():
        if _norm(r[col]) == _norm(name):
            return r
    return None


def _need(table, name, what):
    r = _find(table, name)
    if not r:
        raise ToolError(f"{what} «{name}» не знайдено")
    return r


def _period(p):
    p = str(p).strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})", p) or None
    if not m:
        raise ToolError(f"Невірний період «{p}», потрібно YYYY-MM")
    return f"{m.group(1)}-{int(m.group(2)):02d}"


def _meter(it):
    db = get_db()
    if it.get("meter_id"):
        m = db.execute("SELECT * FROM meters WHERE id=?", (it["meter_id"],)).fetchone()
        if not m:
            raise ToolError(f"Лічильник id={it['meter_id']} не знайдено")
        return m
    wh = _need("warehouses", it.get("warehouse"), "Склад")
    q, args = "SELECT m.* FROM meters m JOIN services s ON s.id=m.service_id WHERE m.warehouse_id=?", [wh["id"]]
    rows = db.execute(q, args).fetchall()
    if it.get("service"):
        svc = _need("services", it["service"], "Послугу")
        rows = [r for r in rows if r["service_id"] == svc["id"]]
    if it.get("serial"):
        same = [r for r in rows if _norm(r["serial"]) == _norm(it["serial"])]
        rows = same if same or len(rows) != 1 else rows
    if len(rows) != 1:
        raise ToolError(f"Лічильник для «{wh['name']}» {it.get('service') or ''} {it.get('serial') or ''}: "
                        f"{'не знайдено' if not rows else 'кілька — вкажіть meter_id або номер'}")
    return rows[0]


def run_tool(name, args):
    """Виконати інструмент. Повертає (результат для моделі, список подій для користувача)."""
    db = get_db()
    ev = []
    s = settings()
    items = args.get("items") or []

    if name == "list_warehouses":
        rows = db.execute("SELECT w.id, w.name, w.area, w.price_m2, t.name AS tenant, t.payment_type FROM warehouses w "
                          "LEFT JOIN tenants t ON t.id=w.tenant_id ORDER BY w.name").fetchall()
        return [dict(r) for r in rows], ev
    if name == "list_tenants":
        out = []
        for t in db.execute("SELECT t.*, c.name AS company FROM tenants t LEFT JOIN companies c ON c.id=t.company_id "
                            "ORDER BY t.name").fetchall():
            d = {k: t[k] for k in ("id", "name", "party_type", "edrpou", "payment_type", "company", "contact",
                                   "director_position", "contract_no", "contract_date", "active")}
            d["warehouses"] = [r["name"] for r in db.execute("SELECT name FROM warehouses WHERE tenant_id=?", (t["id"],))]
            d["services"] = [r["name"] for r in db.execute("SELECT s.name FROM tenant_services ts JOIN services s "
                                                           "ON s.id=ts.service_id WHERE ts.tenant_id=?", (t["id"],))]
            out.append(d)
        return out, ev
    if name == "list_companies":
        return [dict(r) for r in db.execute("SELECT id, name, edrpou, vat_payer, act_prefix, active FROM companies")], ev
    if name == "list_services":
        out = []
        for r in db.execute("SELECT * FROM services ORDER BY sort").fetchall():
            p, pp = price_for(r["id"], "9999-12")
            out.append({"id": r["id"], "name": r["name"], "unit": r["unit"], "mode": r["mode"], "active": r["active"],
                        "last_price": p, "last_price_period": pp})
        return out, ev
    if name == "list_meters":
        out = []
        for m in db.execute("SELECT m.*, w.name AS wh, s.name AS svc FROM meters m JOIN warehouses w ON w.id=m.warehouse_id "
                            "JOIN services s ON s.id=m.service_id ORDER BY w.name").fetchall():
            last = db.execute("SELECT period, value FROM readings WHERE meter_id=? ORDER BY period DESC LIMIT 1",
                              (m["id"],)).fetchone()
            out.append({"meter_id": m["id"], "warehouse": m["wh"], "service": m["svc"], "serial": m["serial"],
                        "coef": m["coef"], "initial_value": m["initial_value"],
                        "last_reading": dict(last) if last else None})
        return out, ev
    if name == "get_readings":
        p = _period(args.get("period"))
        rows = db.execute("SELECT r.meter_id, r.value, w.name AS warehouse, s.name AS service, m.serial FROM readings r "
                          "JOIN meters m ON m.id=r.meter_id JOIN warehouses w ON w.id=m.warehouse_id "
                          "JOIN services s ON s.id=m.service_id WHERE r.period=?", (p,)).fetchall()
        return [dict(r) for r in rows], ev

    if name == "get_google_sheet":
        try:
            return fetch_sheet(args.get("url", ""), args.get("sheet"))[:60000], ev
        except ValueError as e:
            raise ToolError(str(e))
    if name == "list_lines":
        out = []
        for l in db.execute("SELECT * FROM power_lines ORDER BY sort, id").fetchall():
            out.append({"id": l["id"], "name": l["name"], "note": l["note"],
                        "line_meters": [dict(r) for r in db.execute(
                            "SELECT id, name, role, serial, coef, initial_value, has_reactive FROM line_meters WHERE line_id=?",
                            (l["id"],))],
                        "warehouse_meters": [dict(r) for r in db.execute(
                            "SELECT m.id AS meter_id, w.name AS warehouse, s.name AS service, m.serial, m.has_reactive "
                            "FROM meters m JOIN warehouses w ON w.id=m.warehouse_id JOIN services s ON s.id=m.service_id "
                            "WHERE m.line_id=?", (l["id"],))]})
        return out, ev

    # ---------- зміни ----------
    from app import set_price          # спільна логіка ціни з історією
    done, errors = 0, []

    def each(fn):
        nonlocal done
        for i, it in enumerate(items):
            try:
                msg = fn(it)
                if msg:
                    ev.append(msg)
                done += 1
            except ToolError as e:
                errors.append(f"#{i + 1}: {e}")

    if name == "upsert_companies":
        cols = ["edrpou", "iban", "bank", "address", "signer", "signer_position", "basis", "city", "act_prefix"]

        def f(it):
            c = _find("companies", it["name"])
            vals = {k: it[k] for k in cols if it.get(k) not in (None, "")}
            if "vat_payer" in it:
                vals["vat_payer"] = 1 if it["vat_payer"] else 0
            if c:
                if vals:
                    db.execute(f"UPDATE companies SET {', '.join(k + '=?' for k in vals)} WHERE id=?", [*vals.values(), c["id"]])
                return f"✎ Компанія «{c['name']}» оновлена"
            vals["name"] = it["name"].strip()
            db.execute(f"INSERT INTO companies({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", list(vals.values()))
            return f"＋ Компанія «{it['name']}»"
        each(f)

    elif name == "upsert_tenants":
        def f(it):
            t = _find("tenants", it["name"])
            vals = {}
            mapping = {"party_type": "party_type", "edrpou": "edrpou", "iban": "iban", "address": "address",
                       "director": "contact", "director_position": "director_position", "basis": "basis",
                       "passport_series": "passport_series", "passport_number": "passport_number",
                       "passport_issued": "passport_issued", "payment_type": "payment_type",
                       "contract_no": "contract_no", "contract_date": "contract_date", "contract_end": "contract_end",
                       "deposit_amount": "deposit_amount", "deposit_date": "deposit_date", "note": "note"}
            for k, col in mapping.items():
                if it.get(k) not in (None, ""):
                    vals[col] = it[k]
            if vals.get("payment_type") and vals["payment_type"] not in PAYMENT_TYPES:
                raise ToolError(f"невідома форма оплати {vals['payment_type']}")
            if it.get("company"):
                vals["company_id"] = _need("companies", it["company"], "Компанію")["id"]
            if "active" in it:
                vals["active"] = 1 if it["active"] else 0
            if t:
                if vals:
                    db.execute(f"UPDATE tenants SET {', '.join(k + '=?' for k in vals)} WHERE id=?", [*vals.values(), t["id"]])
                tid, msg = t["id"], f"✎ Орендар «{t['name']}» оновлений"
                if t["active"] and vals.get("active") == 0:
                    db.execute("UPDATE warehouses SET tenant_id=NULL WHERE tenant_id=?", (tid,))
                    db.execute("UPDATE tenants SET moved_out=date('now','localtime') WHERE id=?", (tid,))
                    msg = f"⤓ Орендар «{t['name']}» виїхав, склади звільнено"
            else:
                vals["name"] = it["name"].strip()
                vals.setdefault("payment_type", "bank")
                if vals.get("party_type") != "person":
                    vals.setdefault("director_position", "Директор")
                    vals.setdefault("basis", "Статуту")
                tid = db.execute(f"INSERT INTO tenants({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                                 list(vals.values())).lastrowid
                msg = f"＋ Орендар «{it['name']}»"
            if it.get("phone") or it.get("contact_person"):
                ex = db.execute("SELECT 1 FROM tenant_contacts WHERE tenant_id=? AND (phone=? OR name=?)",
                                (tid, it.get("phone") or "—", it.get("contact_person") or "—")).fetchone()
                if not ex:
                    db.execute("INSERT INTO tenant_contacts(tenant_id, name, phone) VALUES (?,?,?)",
                               (tid, it.get("contact_person") or "", it.get("phone") or ""))
            return msg
        each(f)

    elif name == "upsert_warehouses":
        def f(it):
            w = _find("warehouses", it["name"])
            vals = {}
            if it.get("area") is not None:
                vals["area"] = float(it["area"])
            if it.get("note"):
                vals["note"] = it["note"]
            if it.get("tenant"):
                vals["tenant_id"] = _need("tenants", it["tenant"], "Орендаря")["id"]
            if w:
                if vals:
                    db.execute(f"UPDATE warehouses SET {', '.join(k + '=?' for k in vals)} WHERE id=?", [*vals.values(), w["id"]])
                wid, msg = w["id"], f"✎ Склад «{w['name']}» оновлений"
            else:
                vals["name"] = it["name"].strip()
                vals.setdefault("area", 0)
                wid = db.execute(f"INSERT INTO warehouses({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})",
                                 list(vals.values())).lastrowid
                msg = f"＋ Склад «{it['name']}»" + (f", {calc.fmt_num(vals['area'])} м²" if vals.get("area") else "")
            price = it.get("price_m2")
            if price is None and it.get("price_month") is not None:
                area = db.execute("SELECT area FROM warehouses WHERE id=?", (wid,)).fetchone()["area"]
                if not area:
                    raise ToolError(f"для «{it['name']}» потрібна площа, щоб перерахувати суму за місяць у ціну за м²")
                price = round(float(it["price_month"]) / area, 4)
            if price is not None and set_price(wid, float(price)):
                msg += f", ціна {calc.fmt_money(price)} грн/м²"
            return msg
        each(f)

    elif name == "attach_warehouses":
        def f(it):
            w = _need("warehouses", it["warehouse"], "Склад")
            t = _need("tenants", it["tenant"], "Орендаря")
            db.execute("UPDATE warehouses SET tenant_id=? WHERE id=?", (t["id"], w["id"]))
            return f"⇄ «{w['name']}» → {t['name']}"
        each(f)

    elif name == "upsert_services":
        def f(it):
            if it["mode"] not in SERVICE_MODES:
                raise ToolError(f"невідомий тип {it['mode']}")
            sv = _find("services", it["name"])
            if sv:
                db.execute("UPDATE services SET unit=COALESCE(?, unit), mode=?, active=1 WHERE id=?",
                           (it.get("unit"), it["mode"], sv["id"]))
                return f"✎ Послуга «{sv['name']}»"
            db.execute("INSERT INTO services(name, unit, mode) VALUES (?,?,?)",
                       (it["name"].strip(), it.get("unit") or "послуга", it["mode"]))
            return f"＋ Послуга «{it['name']}»"
        each(f)

    elif name == "subscribe_services":
        def f(it):
            t = _need("tenants", it["tenant"], "Орендаря")
            sv = _need("services", it["service"], "Послугу")
            db.execute("INSERT OR IGNORE INTO tenant_services(tenant_id, service_id) VALUES (?,?)", (t["id"], sv["id"]))
            return f"✓ {t['name']}: {sv['name']}"
        each(f)

    elif name == "add_meters":
        def f(it):
            w = _need("warehouses", it["warehouse"], "Склад")
            sv = _need("services", it["service"], "Послугу")
            if sv["mode"] != "meter":
                raise ToolError(f"послуга «{sv['name']}» не за лічильником")
            ex = None
            for m in db.execute("SELECT * FROM meters WHERE warehouse_id=? AND service_id=?", (w["id"], sv["id"])).fetchall():
                if _norm(m["serial"]) == _norm(it.get("serial")):
                    ex = m
            if ex:
                db.execute("UPDATE meters SET coef=COALESCE(?, coef), initial_value=COALESCE(?, initial_value) WHERE id=?",
                           (it.get("coef"), it.get("initial_value"), ex["id"]))
                return f"✎ Лічильник {sv['name']} «{w['name']}» №{ex['serial'] or '—'}"
            db.execute("INSERT INTO meters(warehouse_id, service_id, resource, serial, coef, initial_value) VALUES (?,?,'svc',?,?,?)",
                       (w["id"], sv["id"], it.get("serial") or "", it.get("coef") or 1, it.get("initial_value") or 0))
            return f"＋ Лічильник {sv['name']} «{w['name']}»" + (f" №{it['serial']}" if it.get("serial") else "")
        each(f)

    elif name == "set_readings":
        per = {}

        def f(it):
            m = _meter(it)
            p = _period(it["period"])
            db.execute("INSERT INTO readings(meter_id, period, value) VALUES (?,?,?) "
                       "ON CONFLICT(meter_id, period) DO UPDATE SET value=excluded.value", (m["id"], p, float(it["value"])))
            if it.get("reactive") is not None:
                db.execute("UPDATE readings SET reactive=? WHERE meter_id=? AND period=?", (float(it["reactive"]), m["id"], p))
                db.execute("UPDATE meters SET has_reactive=1 WHERE id=?", (m["id"],))
            per[p] = per.get(p, 0) + 1
        each(f)
        ev += [f"✓ Показники {calc.period_label(p)}: {n}" for p, n in sorted(per.items())]

    elif name == "set_service_prices":
        def f(it):
            sv = _need("services", it["service"], "Послугу")
            p = _period(it["period"])
            db.execute("INSERT INTO service_prices(service_id, period, price) VALUES (?,?,?) "
                       "ON CONFLICT(service_id, period) DO UPDATE SET price=excluded.price", (sv["id"], p, float(it["price"])))
            return f"✓ Ціна «{sv['name']}» {calc.period_label(p)}: {calc.fmt_money(it['price'])}"
        each(f)

    elif name == "set_quantities":
        def f(it):
            t = _need("tenants", it["tenant"], "Орендаря")
            sv = _need("services", it["service"], "Послугу")
            p = _period(it["period"])
            db.execute("INSERT OR IGNORE INTO tenant_services(tenant_id, service_id) VALUES (?,?)", (t["id"], sv["id"]))
            db.execute("INSERT INTO service_usage(tenant_id, service_id, period, qty) VALUES (?,?,?,?) "
                       "ON CONFLICT(tenant_id, service_id, period) DO UPDATE SET qty=excluded.qty",
                       (t["id"], sv["id"], p, float(it["qty"])))
            return f"✓ {t['name']} · {sv['name']} {calc.period_label(p)}: {calc.fmt_num(it['qty'])}"
        each(f)
    elif name == "upsert_lines":
        def f(it):
            l = _find("power_lines", it["name"])
            if l:
                if it.get("note") is not None:
                    db.execute("UPDATE power_lines SET note=? WHERE id=?", (it["note"], l["id"]))
                return f"✎ Лінія «{l['name']}»"
            db.execute("INSERT INTO power_lines(name, note) VALUES (?,?)", (it["name"].strip(), it.get("note") or ""))
            return f"＋ Лінія «{it['name']}»"
        each(f)

    elif name == "upsert_line_meters":
        def f(it):
            l = _need("power_lines", it["line"], "Лінію")
            role = it.get("role") or "own"
            if role not in LINE_ROLES:
                raise ToolError(f"невідома роль {role}")
            ex = next((m for m in db.execute("SELECT * FROM line_meters WHERE line_id=?", (l["id"],)).fetchall()
                       if _norm(m["name"]) == _norm(it["name"])), None)
            hr = None if "has_reactive" not in it else (1 if it["has_reactive"] else 0)
            if ex:
                db.execute("UPDATE line_meters SET role=?, serial=COALESCE(?, serial), coef=COALESCE(?, coef), "
                           "initial_value=COALESCE(?, initial_value), has_reactive=COALESCE(?, has_reactive) WHERE id=?",
                           (role, it.get("serial"), it.get("coef"), it.get("initial_value"), hr, ex["id"]))
                return f"✎ Лічильник лінії «{ex['name']}»"
            db.execute("INSERT INTO line_meters(line_id, name, role, serial, coef, initial_value, has_reactive) "
                       "VALUES (?,?,?,?,?,?,?)", (l["id"], it["name"].strip(), role, it.get("serial") or "",
                                                  it.get("coef") or 1, it.get("initial_value") or 0, hr or 0))
            return f"＋ Лічильник лінії «{it['name']}» ({l['name']})"
        each(f)

    elif name == "assign_meters_to_line":
        def f(it):
            l = _need("power_lines", it["line"], "Лінію")
            m = _meter(it)
            db.execute("UPDATE meters SET line_id=? WHERE id=?", (l["id"], m["id"]))
            if "has_reactive" in it:
                db.execute("UPDATE meters SET has_reactive=? WHERE id=?", (1 if it["has_reactive"] else 0, m["id"]))
            return f"⚡ Лічильник id={m['id']} → {l['name']}"
        each(f)

    elif name == "set_line_readings":
        per = {}

        def f(it):
            l = _need("power_lines", it["line"], "Лінію")
            lm = next((m for m in db.execute("SELECT * FROM line_meters WHERE line_id=?", (l["id"],)).fetchall()
                       if _norm(m["name"]) == _norm(it["meter"])), None)
            if not lm:
                raise ToolError(f"лічильник «{it['meter']}» на лінії «{l['name']}» не знайдено")
            p = _period(it["period"])
            v = None if it.get("value") is None else float(it["value"])
            rv = None if it.get("reactive") is None else float(it["reactive"])
            if v is None and rv is None:
                raise ToolError("немає значення")
            db.execute("INSERT INTO line_readings(line_meter_id, period, value, reactive) VALUES (?,?,?,?) "
                       "ON CONFLICT(line_meter_id, period) DO UPDATE SET value=COALESCE(excluded.value, value), "
                       "reactive=COALESCE(excluded.reactive, reactive)", (lm["id"], p, v, rv))
            if rv is not None:
                db.execute("UPDATE line_meters SET has_reactive=1 WHERE id=?", (lm["id"],))
            per[p] = per.get(p, 0) + 1
        each(f)
        ev += [f"✓ Показники лінії {calc.period_label(p)}: {n}" for p, n in sorted(per.items())]
    else:
        raise ToolError(f"невідомий інструмент {name}")

    db.commit()
    return {"ok": done, "errors": errors}, ev + [f"⚠ {e}" for e in errors]


# ---------------- вхідні дані ----------------

def sheet_csv_url(url, sheet=None):
    """Посилання на Google Таблицю -> URL експорту CSV (таблиця має бути доступна за посиланням)."""
    m = re.search(r"docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if not m:
        return None
    if sheet:
        return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/gviz/tq?tqx=out:csv&sheet="
                + urllib.parse.quote(sheet))
    gid = re.search(r"[#&?]gid=(\d+)", url)
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")


def fetch_sheet(url, sheet=None):
    u = sheet_csv_url(url, sheet)
    if not u:
        raise ValueError(_("Это не ссылка на Google Таблицу"))
    req = urllib.request.Request(u, headers={"User-Agent": "sklad-app/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raise ValueError(_("Google Таблица недоступна (откройте доступ «всем, у кого есть ссылка»)") + f" [{e.code}]")
    text = data.decode("utf-8", "replace")
    if "text/html" in ctype or text.lstrip().startswith("<"):
        raise ValueError(_("Google Таблица недоступна (откройте доступ «всем, у кого есть ссылка»)"))
    return text


def file_to_text(f):
    name = (f.filename or "").lower()
    raw = f.read()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            buf = io.StringIO()
            w = csv.writer(buf)
            for row in ws.iter_rows(values_only=True):
                if any(c not in (None, "") for c in row):
                    w.writerow(["" if c is None else c for c in row])
            parts.append(f"### Аркуш «{ws.title}»\n{buf.getvalue()}")
        return "\n".join(parts)
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ---------------- виклик API та цикл агента ----------------

def call_api(key, model, system, messages):
    body = json.dumps({"model": model, "max_tokens": 8000, "system": system, "tools": TOOLS,
                       "messages": messages}).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": API_VERSION, "content-type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(2 ** attempt * 3)
                continue
            if e.code == 401:
                raise RuntimeError(_("Неверный API-ключ Anthropic"))
            raise RuntimeError(f"Anthropic API {e.code}: {detail}")


def _add_msg(sid, role, content, job_id=None):
    db = get_db()
    db.execute("INSERT INTO ai_messages(session_id, role, content, job_id) VALUES (?,?,?,?)",
               (sid, role, json.dumps(content, ensure_ascii=False), job_id))
    db.commit()


def history(sid):
    """Повідомлення сесії у форматі API (без подій і службових полів).

    Якщо попередній запит обірвався між tool_use і tool_result, «висячий» tool_use відкидаємо,
    а сусідні повідомлення користувача зливаємо, щоб ролі чергувалися.
    """
    rows = get_db().execute("SELECT role, content FROM ai_messages WHERE session_id=? AND role IN ('user','assistant') "
                            "ORDER BY id", (sid,)).fetchall()
    msgs = []
    for r in rows:
        c = json.loads(r["content"])
        if isinstance(c, list):
            c = [{k: v for k, v in b.items() if k != "display"} for b in c]
        msgs.append({"role": r["role"], "content": c})
    clean = []
    for i, m in enumerate(msgs):
        if m["role"] == "assistant" and any(b.get("type") == "tool_use" for b in m["content"]):
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            ok = nxt and nxt["role"] == "user" and any(b.get("type") == "tool_result" for b in nxt["content"])
            if not ok:
                if i + 1 < len(msgs):      # обірвалось у минулому — прибираємо tool_use, лишаємо текст
                    m = {"role": "assistant", "content": [b for b in m["content"] if b.get("type") == "text"]
                         or [{"type": "text", "text": "(перервано)"}]}
                else:
                    continue
        if clean and clean[-1]["role"] == m["role"]:
            clean[-1] = {"role": m["role"], "content": clean[-1]["content"] + m["content"]}
        else:
            clean.append(m)
    return clean


def run_job(app, job_id, sid, user_row):
    with app.app_context():
        g.user = user_row
        db = get_db()
        s = settings()
        key, model = s.get("ai_api_key"), s.get("ai_model") or DEFAULT_MODEL
        backup_made = False
        try:
            for _step in range(MAX_STEPS):
                resp = call_api(key, model, SYSTEM_PROMPT, history(sid))
                content = resp.get("content", [])
                _add_msg(sid, "assistant", content, job_id)
                uses = [c for c in content if c.get("type") == "tool_use"]
                if resp.get("stop_reason") != "tool_use" or not uses:
                    break
                results = []
                for u in uses:
                    if u["name"] in WRITE_TOOLS and not backup_made:
                        import backup as bk
                        name = bk.create_backup("pre-ai")
                        db.execute("UPDATE ai_jobs SET backup=? WHERE id=?", (name, job_id))
                        db.commit()
                        backup_made = True
                    try:
                        out, events = run_tool(u["name"], u.get("input") or {})
                        if u["name"] in WRITE_TOOLS:
                            n = out.get("ok", 0) if isinstance(out, dict) else 0
                            db.execute("UPDATE ai_jobs SET changes=changes+? WHERE id=?", (n, job_id))
                            if n:
                                log("ШІ: " + u["name"], "; ".join(e for e in events if not e.startswith("⚠"))[:480],
                                    username=f"ШІ ({user_row['username']})")
                        results.append({"type": "tool_result", "tool_use_id": u["id"],
                                        "content": json.dumps(out, ensure_ascii=False, default=str)[:60000]})
                    except ToolError as e:
                        events = [f"⚠ {e}"]
                        results.append({"type": "tool_result", "tool_use_id": u["id"], "content": str(e), "is_error": True})
                    for e in events:
                        _add_msg(sid, "event", e, job_id)
                _add_msg(sid, "user", results, job_id)
            else:
                _add_msg(sid, "event", "⚠ Досягнуто ліміту кроків — розбийте завдання на частини", job_id)
            db.execute("UPDATE ai_jobs SET status='done', finished_at=datetime('now','localtime') WHERE id=?", (job_id,))
        except Exception as e:  # noqa
            traceback.print_exc()
            db.execute("UPDATE ai_jobs SET status='error', error=?, finished_at=datetime('now','localtime') WHERE id=?",
                       (str(e)[:1000], job_id))
        db.commit()


# ---------------- сторінки ----------------

def ensure_schema():
    con = get_db()
    con.executescript(SCHEMA)
    con.commit()


@bp.route("/ai")
@bp.route("/ai/<int:sid>")
@auth.admin_required
def chat(sid=None):
    ensure_schema()
    db = get_db()
    sessions = db.execute("SELECT s.*, (SELECT COUNT(*) FROM ai_messages m WHERE m.session_id=s.id AND m.role='event') AS ev "
                          "FROM ai_sessions s ORDER BY id DESC LIMIT 30").fetchall()
    msgs, jobs = [], {}
    if sid:
        if not db.execute("SELECT 1 FROM ai_sessions WHERE id=?", (sid,)).fetchone():
            abort(404)
        msgs = view_messages(sid)
        jobs = {r["id"]: dict(r) for r in db.execute("SELECT * FROM ai_jobs WHERE session_id=?", (sid,))}
    running = next((j for j in jobs.values() if j["status"] == "running"), None)
    s = settings()
    return render_template("ai.html", sessions=sessions, sid=sid, msgs=msgs, jobs=jobs, running=running,
                           has_key=bool(s.get("ai_api_key")), model=s.get("ai_model") or DEFAULT_MODEL)


def view_messages(sid):
    """Повідомлення для показу: текст користувача, відповіді, події дій."""
    out = []
    for r in get_db().execute("SELECT * FROM ai_messages WHERE session_id=? ORDER BY id", (sid,)).fetchall():
        c = json.loads(r["content"])
        if r["role"] == "event":
            out.append({"kind": "event", "text": c, "job": r["job_id"]})
        elif r["role"] == "assistant":
            text = "\n".join(b.get("text", "") for b in c if b.get("type") == "text").strip()
            if text:
                out.append({"kind": "assistant", "text": text, "job": r["job_id"]})
        elif r["role"] == "user" and isinstance(c, list) and c and c[0].get("type") == "text":
            out.append({"kind": "user", "text": c[0].get("display") or c[0]["text"], "job": r["job_id"]})
    return out


@bp.route("/ai/send", methods=["POST"])
@auth.admin_required
def send():
    ensure_schema()
    db = get_db()
    s = settings()
    if not s.get("ai_api_key"):
        flash(_("Сначала укажите API-ключ Anthropic в настройках"))
        return redirect(url_for("ai_settings_page"))
    sid = request.form.get("sid", type=int)
    text = request.form.get("text", "").strip()
    parts, notes = [], []
    for f in request.files.getlist("files"):
        if f and f.filename:
            try:
                parts.append(f"=== Файл «{f.filename}» ===\n{file_to_text(f)}")
                notes.append("📎 " + f.filename)
            except Exception as e:  # noqa
                flash(f"{f.filename}: {e}")
    for url in re.findall(r"https?://docs\.google\.com/spreadsheets/\S+", request.form.get("sheets", "") + " " + text):
        try:
            parts.append(f"=== Google Таблиця {url} ===\n{fetch_sheet(url)}")
            notes.append("📊 Google Таблиця")
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("ai.chat", sid=sid) if sid else url_for("ai.chat"))
    if not text and not parts:
        return redirect(url_for("ai.chat", sid=sid) if sid else url_for("ai.chat"))
    data = "\n\n".join(parts)
    if len(data) > MAX_INPUT_CHARS:
        data = data[:MAX_INPUT_CHARS] + "\n…[дані обрізано: забагато рядків — розбийте на частини]"
        flash(_("Данные слишком большие — переданы первые ~180 тыс. символов"))
    if not sid:
        sid = db.execute("INSERT INTO ai_sessions(title, username) VALUES (?,?)",
                         ((text or notes[0])[:80], g.user["username"])).lastrowid
        db.commit()
    import uuid
    job_id = uuid.uuid4().hex[:12]
    full = text + (f"\n\nДані:\n{data}" if data else "")
    display = text + ("\n" + " · ".join(notes) if notes else "")
    _add_msg(sid, "user", [{"type": "text", "text": full}], job_id)
    # поле display не передаємо в API — зберігаємо окремо в події
    db.execute("UPDATE ai_messages SET content=? WHERE id=(SELECT MAX(id) FROM ai_messages WHERE session_id=?)",
               (json.dumps([{"type": "text", "text": full, "display": display}], ensure_ascii=False), sid))
    db.execute("INSERT INTO ai_jobs(id, session_id, status) VALUES (?,?, 'running')", (job_id, sid))
    db.commit()
    threading.Thread(target=run_job, args=(current_app._get_current_object(), job_id, sid, g.user), daemon=True).start()
    return redirect(url_for("ai.chat", sid=sid))


@bp.route("/ai/job/<job_id>")
@auth.admin_required
def job_status(job_id):
    j = get_db().execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    if not j:
        abort(404)
    n = get_db().execute("SELECT COUNT(*) FROM ai_messages WHERE job_id=?", (job_id,)).fetchone()[0]
    return jsonify(status=j["status"], messages=n, changes=j["changes"], error=j["error"])


@bp.route("/ai/job/<job_id>/undo", methods=["POST"])
@auth.admin_required
def undo(job_id):
    db = get_db()
    j = db.execute("SELECT * FROM ai_jobs WHERE id=?", (job_id,)).fetchone()
    if not j or not j["backup"] or j["status"] != "done":
        abort(400)
    import os

    import backup as bk
    path = os.path.join(bk.BACKUP_DIR, j["backup"])
    sid = j["session_id"]
    # чат ШІ зберігаємо: відновлення бази не повинно стерти історію розмови
    keep = [dict(r) for r in db.execute("SELECT * FROM ai_sessions").fetchall()]
    keep_m = [dict(r) for r in db.execute("SELECT * FROM ai_messages").fetchall()]
    keep_j = [dict(r) for r in db.execute("SELECT * FROM ai_jobs").fetchall()]
    pre = bk.create_backup("pre-restore")
    db.close()
    g.pop("db", None)
    bk.restore_from_zip(path)
    from db import init_db
    init_db()
    db = get_db()
    db.executescript(SCHEMA)
    for t, rows in (("ai_sessions", keep), ("ai_messages", keep_m), ("ai_jobs", keep_j)):
        db.execute(f"DELETE FROM {t}")
        for r in rows:
            db.execute(f"INSERT INTO {t}({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", list(r.values()))
    db.execute("UPDATE ai_jobs SET status='undone' WHERE id=?", (job_id,))
    db.commit()
    _add_msg(sid, "event", "↶ Зміни цього запиту відкочено (база відновлена з бекапу перед запитом)", job_id)
    log("ШІ: зміни відкочено", f"{j['backup']} (поточний стан збережено як {pre})")
    flash(_("Изменения ИИ откачены"))
    return redirect(url_for("ai.chat", sid=sid))


@auth.admin_required
def ai_settings_page():
    db = get_db()
    if request.method == "POST":
        key = request.form.get("ai_api_key", "").strip()
        model = request.form.get("ai_model_custom", "").strip() or request.form.get("ai_model") or DEFAULT_MODEL
        if request.form.get("clear_key"):
            db.execute("DELETE FROM settings WHERE key='ai_api_key'")
            log("ШІ: ключ API видалено")
        elif key and not key.startswith("•"):
            db.execute("INSERT INTO settings(key, value) VALUES ('ai_api_key', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key,))
            log("ШІ: ключ API змінено")
        db.execute("INSERT INTO settings(key, value) VALUES ('ai_model', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (model,))
        db.commit()
        if request.form.get("test"):
            try:
                r = call_api(settings().get("ai_api_key"), model, "Відповідай одним словом.", [{"role": "user", "content": "Привіт"}])
                flash(_("Проверка связи: работает") + f" ({r.get('model', model)})")
            except Exception as e:  # noqa
                flash(_("Проверка связи не прошла") + f": {e}")
        else:
            flash(_("Настройки сохранены"))
        return redirect(url_for("ai_settings_page"))
    k = settings().get("ai_api_key") or ""
    masked = ("•" * 8 + k[-4:]) if k else ""
    return render_template("ai_settings.html", masked=masked, model=settings().get("ai_model") or DEFAULT_MODEL, MODELS=MODELS)


def init_app(app):
    app.register_blueprint(bp)
    # сторінка налаштувань ШІ — вкладка в «Налаштуваннях»
    app.add_url_rule("/settings/ai", "ai_settings_page", ai_settings_page, methods=["GET", "POST"])
    with app.app_context():
        ensure_schema()
