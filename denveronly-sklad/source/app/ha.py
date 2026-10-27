"""Home Assistant: лічильники-сенсори (генератор, пелетний пальник, …) для кожного об'єкта.

У кожного об'єкта (База / Офіс) — свій Home Assistant: адреса + довготривалий токен (зашифрований vault.py,
ключ поза базою й бекапами). Програма лише ЧИТАЄ стани сенсорів через REST API (/api/states).

Сенсори двох видів:
  total   — накопичувальний показник (кВт·год, м³, кг, мотогодини… total_increasing) — як звичайний лічильник;
  monthly — utility_meter HA з циклом «місяць» (обнуляється 1-го): стан = витрата з початку місяця,
            атрибут last_period = витрата за минулий місяць.
Щогодини програма запам'ятовує значення (ha_daily), а на початку місяця фіксує минулий місяць (ha_months):
витрату і показник. Якщо сенсор прив'язаний до лічильника з таблиці показників — вписує туди показник за
минулий місяць (лише якщо клітинка порожня; вручну внесене не перезаписується).
"""
import fcntl
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

import auth
import calc
import vault
from audit import log
from db import DATA_DIR, SITES, current_site, get_db, set_setting, settings, using_site
from i18n import _

bp = Blueprint("ha", __name__)
LOCK_FILE = os.path.join(DATA_DIR, "ha.lock")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ha_daily (
    entity TEXT NOT NULL,
    day TEXT NOT NULL,               -- YYYY-MM-DD; значення — останнє за день
    value REAL,
    PRIMARY KEY (entity, day)
);
CREATE TABLE IF NOT EXISTS ha_months (
    entity TEXT NOT NULL,
    period TEXT NOT NULL,            -- YYYY-MM
    cons REAL,                       -- витрата за місяць
    reading REAL,                    -- показник на кінець місяця (для total)
    written TEXT,                    -- куди вписано в таблицю показників ("l:5", "m:3") або NULL
    PRIMARY KEY (entity, period)
);
"""
KINDS = ("total", "monthly")


def ensure_schema():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


# ---------------- налаштування ----------------

def config():
    s = settings()
    tok = s.get("ha_token") or ""
    try:
        sensors = json.loads(s.get("ha_sensors") or "[]")
    except ValueError:
        sensors = []
    return {"url": (s.get("ha_url") or "").rstrip("/"), "has_token": bool(tok), "token": vault.decrypt(tok) if tok else "",
            "insecure": s.get("ha_insecure") == "1", "enabled": s.get("ha_enabled") == "1", "sensors": sensors,
            "status": s.get("ha_status") or ""}


def _save_sensors(sensors):
    set_setting("ha_sensors", json.dumps(sensors, ensure_ascii=False))


# ---------------- API ----------------

class HAError(Exception):
    pass


def api(c, path):
    if not c["url"] or not c["token"]:
        raise HAError(_("Укажите адрес Home Assistant и токен"))
    if not re.match(r"https?://", c["url"]):
        raise HAError(_("Адрес должен начинаться с http:// или https://"))
    req = urllib.request.Request(c["url"] + path, headers={"Authorization": "Bearer " + c["token"],
                                                           "Content-Type": "application/json"})
    ctx = None
    if c["url"].startswith("https://") and c["insecure"]:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise HAError(_("Home Assistant отклонил токен (401)"))
        if e.code == 403:
            raise HAError(_("Home Assistant отклонил запрос (403) — у пользователя токена нет доступа"))
        if e.code == 404:
            raise HAError(_("Не найдено (404)") + f": {path}")
        raise HAError(f"HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise HAError(_("Нет связи с Home Assistant") + f" ({c['url']}): {getattr(e, 'reason', e)}")
    except ValueError:
        raise HAError(_("Home Assistant вернул не JSON — проверьте адрес"))


def fnum(v):
    try:
        x = float(v)
        return x if x == x else None          # nan
    except (TypeError, ValueError):
        return None


def candidate_sensors(c):
    """Сенсори HA, схожі на лічильники: state_class total/total_increasing або utility_meter."""
    out = []
    for st in api(c, "/api/states"):
        eid, a = st.get("entity_id", ""), st.get("attributes") or {}
        if not eid.startswith("sensor."):
            continue
        monthly = "last_period" in a or "meter_period" in a
        if not (monthly or a.get("state_class") in ("total", "total_increasing")):
            continue
        out.append({"entity": eid, "name": a.get("friendly_name") or eid, "unit": a.get("unit_of_measurement") or "",
                    "state": st.get("state"), "kind": "monthly" if monthly else "total",
                    "period": a.get("meter_period") or ""})
    out.sort(key=lambda x: x["name"].lower())
    return out


# ---------------- знімки й місячні підсумки ----------------

def _period(d):
    return f"{d.year}-{d.month:02d}"


def snapshot(c=None):
    """Поточні значення всіх сенсорів об'єкта -> ha_daily (останнє за сьогодні). -> {entity: state dict}."""
    c = c or config()
    db = get_db()
    states, today = {}, date.today().isoformat()
    for sn in c["sensors"]:
        try:
            st = api(c, "/api/states/" + urllib.parse.quote(sn["entity"]))
        except HAError as e:
            states[sn["entity"]] = {"error": str(e)}
            continue
        states[sn["entity"]] = st
        v = fnum(st.get("state"))
        if v is not None:
            db.execute("INSERT INTO ha_daily(entity, day, value) VALUES (?,?,?) "
                       "ON CONFLICT(entity, day) DO UPDATE SET value=excluded.value", (sn["entity"], today, v))
    db.commit()
    return states


def _last_in(entity, period):
    r = get_db().execute("SELECT value FROM ha_daily WHERE entity=? AND day LIKE ? AND value IS NOT NULL "
                         "ORDER BY day DESC LIMIT 1", (entity, period + "-%")).fetchone()
    return r["value"] if r else None


def _target_prev(src, period):
    """Попередній показник лічильника з таблиці показників (до period) або початковий."""
    kind, mid = src.split(":")
    tbl, col, mt = ("line_readings", "line_meter_id", "line_meters") if kind == "l" else ("readings", "meter_id", "meters")
    db = get_db()
    r = db.execute(f"SELECT value FROM {tbl} WHERE {col}=? AND period<? AND value IS NOT NULL ORDER BY period DESC LIMIT 1",
                   (int(mid), period)).fetchone()
    if r:
        return r["value"]
    m = db.execute(f"SELECT initial_value FROM {mt} WHERE id=?", (int(mid),)).fetchone()
    return (m["initial_value"] or 0) if m else 0


def _write_target(src, period, value):
    """Вписати показник у таблицю показників, якщо клітинка порожня. -> True, якщо вписано."""
    if not src or not re.fullmatch(r"[lm]:\d+", src) or value is None:
        return False
    kind, mid = src.split(":")
    tbl, col = ("line_readings", "line_meter_id") if kind == "l" else ("readings", "meter_id")
    db = get_db()
    r = db.execute(f"SELECT id, value FROM {tbl} WHERE {col}=? AND period=?", (int(mid), period)).fetchone()
    if r and r["value"] is not None:
        return False
    if r:
        db.execute(f"UPDATE {tbl} SET value=? WHERE id=?", (value, r["id"]))
    else:
        db.execute(f"INSERT INTO {tbl}({col}, period, value) VALUES (?,?,?)", (int(mid), period, value))
    return True


def close_month(sn, period, state=None, force=False):
    """Зафіксувати місяць period для сенсора: витрата/показник -> ha_months, і в таблицю показників (якщо прив'язано).
    -> текст результату."""
    db = get_db()
    e = sn["entity"]
    have = db.execute("SELECT * FROM ha_months WHERE entity=? AND period=?", (e, period)).fetchone()
    if have and not force:
        return None
    cons = reading = None
    if sn.get("kind") == "monthly":
        attrs = (state or {}).get("attributes") or {}
        lp = fnum(attrs.get("last_period"))
        # last_period — за минулий цикл; годиться лише одразу після переходу місяця
        if lp is not None and _period(date.today()) == calc.next_period(period):
            cons = lp
        else:
            cons = _last_in(e, period)
    else:
        reading = _last_in(e, period)
        prev = _last_in(e, calc.prev_period(period))
        cons = calc.r2(reading - prev) if reading is not None and prev is not None else None
    if cons is None and reading is None:
        return None
    target, written = sn.get("target") or "", None
    if target in ("hd:0", "hn:0"):                    # «Дім»: показник зони день / ніч на кінець місяця
        import homeel
        zone = "day" if target == "hd:0" else "night"
        val = reading
        if sn.get("kind") == "monthly" and cons is not None:
            homeel.ensure_schema()
            prev = db.execute(f"SELECT {zone} AS v FROM home_el WHERE period<? AND {zone} IS NOT NULL ORDER BY period DESC LIMIT 1",
                              (period,)).fetchone()
            val = calc.r2((prev["v"] if prev else 0) + cons)
        if homeel.write_reading(period, zone, val):
            written = target
    elif target.startswith("s:"):                       # сонячна генерація лінії: кВт·год за місяць -> «Сонце» лінії
        lid = int(target[2:])
        r = db.execute("SELECT kwh FROM line_solar WHERE line_id=? AND period=?", (lid, period)).fetchone()
        if cons is not None and (not r or r["kwh"] is None):
            db.execute("INSERT INTO line_solar(line_id, period, kwh) VALUES (?,?,?) "
                       "ON CONFLICT(line_id, period) DO UPDATE SET kwh=excluded.kwh", (lid, period, calc.r2(cons)))
            written = target
    elif target:
        val = reading
        if sn.get("kind") == "monthly" and cons is not None:
            val = calc.r2(_target_prev(target, period) + cons)
        if _write_target(target, period, val):
            written = target
    db.execute("INSERT INTO ha_months(entity, period, cons, reading, written) VALUES (?,?,?,?,?) "
               "ON CONFLICT(entity, period) DO UPDATE SET cons=excluded.cons, reading=excluded.reading, "
               "written=COALESCE(excluded.written, ha_months.written)", (e, period, cons, reading, written))
    db.commit()
    if written:
        log("Home Assistant: показание записано", f"{sn.get('name') or e}: {calc.period_label(period)}", username="HA")
    return written


def run_site():
    """Щогодинний прохід по об'єкту: знімок + закриття минулого місяця."""
    c = config()
    if not c["enabled"] or not c["sensors"] or not c["token"]:
        return
    ensure_schema()
    states = snapshot(c)
    prevp = calc.prev_period(_period(date.today()))
    for sn in c["sensors"]:
        st = states.get(sn["entity"])
        if st and not st.get("error"):
            close_month(sn, prevp, st)
    errs = [f"{k}: {v['error']}" for k, v in states.items() if v.get("error")]
    set_setting("ha_status", json.dumps({"time": datetime.now().isoformat(timespec="minutes"), "errors": errs},
                                        ensure_ascii=False))
    get_db().commit()


def _scheduler(app):
    lockf, last_slot = None, None
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
            now = datetime.now()
            slot = now.strftime("%Y-%m-%d %H")
            if now.minute < 55 or slot == last_slot:
                continue                      # раз на годину о :55 — останнє значення дня буде з 23:55
            last_slot = slot
            for site in SITES:
                try:
                    with app.app_context(), using_site(site):
                        run_site()
                except Exception:
                    pass
        except Exception:
            pass


# ---------------- сторінка ----------------

def _require():
    if not auth.can("settings"):
        abort(403)


def _months_list(n=12):
    out, p = [], _period(date.today())
    for _i in range(n):
        out.append(p)
        p = calc.prev_period(p)
    return out


@bp.route("/ha")
def page():
    _require()
    ensure_schema()
    c = config()
    db = get_db()
    states, err = {}, None
    if c["url"] and c["token"] and c["sensors"]:
        try:
            states = snapshot(c)
        except HAError as e:
            err = str(e)
    cands, cand_err = None, None
    if request.args.get("pick") and c["url"] and c["token"]:    # список сенсорів — на сервері, без fetch зі сторінки
        try:
            have = {x["entity"] for x in c["sensors"]}
            cands = [x for x in candidate_sensors(c) if x["entity"] not in have]
        except HAError as e:
            cand_err = str(e)
        except Exception as e:  # noqa
            cand_err = f"{type(e).__name__}: {e}"
    cur = _period(date.today())
    rows = []
    import lines as L
    for sn in c["sensors"]:
        st = states.get(sn["entity"]) or {}
        a = st.get("attributes") or {}
        v = fnum(st.get("state"))
        if sn.get("kind") == "monthly":
            month = v
        else:
            base = _last_in(sn["entity"], calc.prev_period(cur))
            month = calc.r2(v - base) if v is not None and base is not None else None
        tg = sn.get("target") or ""
        if tg in ("hd:0", "hn:0"):
            tlabel = ("☀ " + _("Дом: день")) if tg == "hd:0" else ("🌙 " + _("Дом: ночь"))
        elif tg.startswith("s:"):
            ln = db.execute("SELECT name FROM power_lines WHERE id=?", (int(tg[2:]),)).fetchone()
            tlabel = "☀ " + _("Солнце") + f" — {ln['name']}" if ln else None
        else:
            src = L.resolve_src(tg) if tg else None
            tlabel = src[3] if src else None
        rows.append({"s": sn, "value": v, "unit": a.get("unit_of_measurement") or sn.get("unit") or "",
                     "updated": (st.get("last_updated") or "")[:16].replace("T", " "), "error": st.get("error"),
                     "month": month, "target_label": tlabel})
    periods = _months_list(12)
    hist = {}
    for r in db.execute("SELECT * FROM ha_months WHERE period>=?", (periods[-1],)):
        hist[(r["entity"], r["period"])] = dict(r)
    try:
        status = json.loads(c["status"]) if c["status"] else {}
    except ValueError:
        status = {}
    return render_template("ha.html", c=c, rows=rows, err=err, periods=periods[1:], hist=hist, status=status,
                           srcs=([{"value": "hd:0", "label": "☀ " + _("Дом: день")}, {"value": "hn:0", "label": "🌙 " + _("Дом: ночь")}]
                                 if SITES[current_site()].get("kind") == "home" else [])
                           + [{"value": f"s:{l['id']}", "label": "☀ " + _("Солнце (выработка за месяц)") + f" — {l['name']}"}
                                 for l in db.execute("SELECT id, name FROM power_lines WHERE active=1 AND has_solar=1 ORDER BY sort, id")]
                           + L.src_options(), cur=cur, cands=cands, cand_err=cand_err, site_name=_(SITES[current_site()]["name"]))


@bp.route("/ha/sensors")
def sensors_json():
    _require()
    try:
        return jsonify(ok=True, sensors=candidate_sensors(config()))
    except HAError as e:
        return jsonify(ok=False, error=str(e))


@bp.route("/ha/settings", methods=["POST"])
def save_settings():
    _require()
    f = request.form
    action = f.get("action", "save")
    if action in ("save", "test"):
        url = (f.get("url") or "").strip().rstrip("/")
        set_setting("ha_url", url)
        if f.get("token", "").strip():
            set_setting("ha_token", vault.encrypt(f.get("token").strip()))
        set_setting("ha_insecure", "1" if f.get("insecure") else "0")
        set_setting("ha_enabled", "1" if f.get("enabled") else "0")
        get_db().commit()
        log("Home Assistant: настройки изменены", url)
        if action == "test":
            try:
                r = api(config(), "/api/")
                flash(_("Связь с Home Assistant есть") + f": {r.get('message', 'OK')}")
            except HAError as e:
                flash(str(e))
        else:
            flash(_("Настройки сохранены"))
    elif action == "clear_token":
        set_setting("ha_token", "")
        get_db().commit()
        log("Home Assistant: токен удалён", "")
        flash(_("Токен удалён"))
    return redirect(url_for("ha.page"))


@bp.route("/ha/sensor", methods=["POST"])
def sensor_save():
    _require()
    f = request.form
    c = config()
    sensors = c["sensors"]
    eid = (f.get("entity") or "").strip()
    if f.get("delete"):
        sensors = [s for s in sensors if s["entity"] != eid]
        log("Home Assistant: сенсор удалён", eid)
    else:
        if not re.fullmatch(r"sensor\.[a-z0-9_]+", eid):
            flash(_("Неверный сенсор") + f": {eid}")
            return redirect(url_for("ha.page"))
        tgt = f.get("target") or ""
        item = {"entity": eid, "name": (f.get("name") or eid).strip(), "kind": f.get("kind") if f.get("kind") in KINDS else "total",
                "unit": (f.get("unit") or "").strip(), "target": tgt if re.fullmatch(r"[lms]:\d+|h[dn]:0", tgt) else ""}
        for i, s in enumerate(sensors):
            if s["entity"] == eid:
                sensors[i] = item
                break
        else:
            sensors.append(item)
        log("Home Assistant: сенсор сохранён", f"{item['name']} ({eid})")
    _save_sensors(sensors)
    get_db().commit()
    flash(_("Сохранено"))
    return redirect(url_for("ha.page"))


@bp.route("/ha/close", methods=["POST"])
def close_now():
    """Зафіксувати вибраний місяць вручну (з уже збережених знімків)."""
    _require()
    ensure_schema()
    period = request.form.get("period") or ""
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        abort(400)
    c = config()
    try:
        states = snapshot(c)
    except HAError:
        states = {}
    n = 0
    for sn in c["sensors"]:
        if close_month(sn, period, states.get(sn["entity"]), force=True):
            n += 1
    flash(_("Месяц {p} зафиксирован; в таблицу показаний вписано: {n}").format(p=calc.period_label(period), n=n))
    return redirect(url_for("ha.page"))


def init_app(app):
    app.register_blueprint(bp)
    if os.environ.get("YASNO_SCHED", "1") != "0":
        threading.Thread(target=_scheduler, args=(app,), daemon=True).start()
