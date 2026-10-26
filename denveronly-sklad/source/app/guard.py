"""Бланк показників для охорони + розпізнавання фото заповненого бланка через ШІ.

1) «Показники → 🖨 Шаблон для охорони»: друкований бланк (А4 альбомний) — ті самі лінії й лічильники, що в таблиці
   показників, рядки — місяці від поточного до грудня наступного року. Кожна колонка має номер (1, 2, 3…), а
   розкладка колонок зберігається в базі як «бланк № N» — ШІ потім знає, яка цифра до якого лічильника.
2) «ШІ → 📷 Розпізнати бланк»: фото з телефона -> Claude читає цифри як написані -> програма перевіряє їх з
   попередніми показниками (витрата не від'ємна, без різких стрибків, підозра на пропущену кому — десяті частки)
   -> сторінка перевірки, де людина підтверджує або виправляє, і лише тоді показники зберігаються.
"""
import base64
import io
import json
import re
import statistics
import threading
import traceback
import uuid

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for)

import auth
import calc
from audit import log
from db import current_site, get_db, settings
from i18n import _

bp = Blueprint("guard", __name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS guard_sheets (
    id INTEGER PRIMARY KEY,
    periods TEXT NOT NULL,           -- JSON: ["2026-10", …]
    cols TEXT NOT NULL,              -- JSON: [{n, kind, id, field, title, sub, serial, line}]
    username TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS guard_scans (
    id TEXT PRIMARY KEY,
    sheet_id INTEGER,
    status TEXT NOT NULL,            -- running | done | error | saved
    result TEXT,                     -- JSON відповіді ШІ
    error TEXT,
    photos INTEGER,
    username TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
"""

PER_PAGE = 10          # колонок лічильників на одному аркуші (А4 альбомний)
MAX_PHOTOS = 6


def ensure_schema():
    db = get_db()
    db.executescript(SCHEMA)
    db.commit()


# ---------------- бланк ----------------

def sheet_periods(today_period):
    """Від поточного місяця до грудня наступного року (жовтень 2026 -> 15 місяців)."""
    out, p = [], today_period
    end = f"{int(today_period[:4]) + 1}-12"
    while p <= end:
        out.append(p)
        p = calc.next_period(p)
    return out


def _last_value(kind, mid, field, before):
    tbl, col = ("readings", "meter_id") if kind == "m" else ("line_readings", "line_meter_id")
    r = get_db().execute(f"SELECT {field} AS v, period FROM {tbl} WHERE {col}=? AND period<? AND {field} IS NOT NULL "
                         "ORDER BY period DESC LIMIT 1", (mid, before)).fetchone()
    return (r["v"], r["period"]) if r else (None, None)


def build_sections(periods):
    """Секції бланка (лінії + «без лінії»): [{title, groups:[{title, cols:[…]}]}]; кожен лічильник — один раз."""
    import lines as L
    db = get_db()
    tabs = [(l["id"], l["name"]) for l in db.execute("SELECT id, name FROM power_lines WHERE active=1 ORDER BY sort, id")]
    tabs.append((None, _("Без линии")))
    seen, sections, n = set(), [], 0
    for lid, lname in tabs:
        groups, _bal = L.table_view(lid, periods[:1])
        sec = {"title": lname, "groups": []}
        for gr in groups:
            cols = []
            for c in gr["cols"]:
                fields = ["value"]                     # реактив на бланку не пишемо
                for f in fields:
                    key = (c["kind"], c["id"], f)
                    if key in seen:
                        continue
                    seen.add(key)
                    n += 1
                    last, lastp = _last_value(c["kind"], c["id"], f, periods[0])
                    title = (c.get("name") or c["title"]) + (" · " + _("реактив") if f == "reactive" else "")
                    sub = " · ".join(x for x in (c.get("svc") if c["kind"] == "m" else c.get("sub"),
                                                  f"№{c['serial']}" if c.get("serial") else "") if x)
                    cols.append({"n": n, "kind": c["kind"], "id": c["id"], "field": f, "title": title, "sub": sub,
                                 "serial": c.get("serial") or "", "line": lname, "last": last, "lastp": lastp})
            if cols:
                ttl = gr.get("title") if gr["kind"] == "tenant" else _("Счётчики линии")
                if gr["kind"] == "tenant" and not gr.get("tid"):
                    ttl = _("свободные склады")
                sec["groups"].append({"title": ttl, "cols": cols})
        if sec["groups"]:
            sections.append(sec)
    return sections


def _pages(sections):
    """Розбиття секцій на аркуші по PER_PAGE колонок (групи орендарів не розриваються, якщо влазять)."""
    pages = []
    for sec in sections:
        cur = []
        for gr in sec["groups"]:
            cols = gr["cols"]
            while cols:
                room = PER_PAGE - sum(len(x["cols"]) for x in cur)
                if room <= 0 or (len(cols) <= PER_PAGE and len(cols) > room and cur):
                    pages.append({"title": sec["title"], "groups": cur})
                    cur, room = [], PER_PAGE
                cur.append({"title": gr["title"], "cols": cols[:room]})
                cols = cols[room:]
        if cur:
            pages.append({"title": sec["title"], "groups": cur})
    for pg in pages:
        pg["ncols"] = sum(len(x["cols"]) for x in pg["groups"])
    return pages


def save_sheet(periods, sections):
    """Зберегти розкладку; той самий набір колонок і місяців — той самий № бланка."""
    db = get_db()
    cols = [{k: c[k] for k in ("n", "kind", "id", "field", "title", "sub", "serial", "line")}
            for s in sections for gr in s["groups"] for c in gr["cols"]]
    pj, cj = json.dumps(periods), json.dumps(cols, ensure_ascii=False)
    r = db.execute("SELECT id FROM guard_sheets WHERE periods=? AND cols=? ORDER BY id DESC LIMIT 1", (pj, cj)).fetchone()
    if r:
        return r["id"]
    sid = db.execute("INSERT INTO guard_sheets(periods, cols, username) VALUES (?,?,?)",
                     (pj, cj, g.user["username"] if g.get("user") else None)).lastrowid
    db.commit()
    return sid


@bp.route("/readings/guard")
def sheet():
    from app import this_month
    ensure_schema()
    periods = sheet_periods(request.args.get("from") or this_month())
    sections = build_sections(periods)
    sheet_id = save_sheet(periods, sections) if sections else None
    from db import SITES
    return render_template("guard_sheet.html", pages=_pages(sections), periods=periods, sheet_id=sheet_id,
                           site_name=_(SITES[current_site()]["name"]))


# ---------------- розпізнавання ----------------

def _prep_image(f):
    """Фото з телефона (JPEG/PNG/HEIC) -> JPEG ≤ 2400 px, повернутий за EXIF, base64."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:  # noqa
        pass
    im = Image.open(f.stream)
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((2400, 2400))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


PROMPT = """На фото — паперовий бланк показників лічильників, заповнений від руки охоронцем.
Бланк надрукований програмою: у шапці «Бланк № {sheet_id}», рядки — місяці, колонки — лічильники з номерами 1, 2, 3…
(номер колонки надрукований великими цифрами над колонкою, під ним — назва лічильника).
Можливо кілька фото (кілька аркушів) — колонки на них різні, номери колонок наскрізні.

Колонки бланка:
{cols}

Рядки (місяці) бланка:
{rows}

Завдання: перепиши рукописні числа ТОЧНО так, як вони написані (з комою/крапкою, якщо вона є; не виправляй, не округлюй,
не вгадуй за попередніми показниками). Нечітку цифру познач знаком «?» замість неї (напр. «2104?»). Порожні клітинки пропускай.
Якщо в рядку вписана дата зйому — теж перепиши її (ДД.ММ).
Відповідай ЛИШЕ JSON без пояснень:
{{"sheet_no": <номер бланка з фото або null>, "rows": [{{"period": "YYYY-MM", "date": "ДД.ММ або null",
  "cells": {{"<номер колонки>": "<число як написано>"}}}}], "notes": "<коротко: що не вдалося прочитати, закреслення тощо>"}}"""


def call_vision(key, model, images, text):
    import ai
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}} for b in images]
    content.append({"type": "text", "text": text})
    resp = ai.call_api(key, model, "Ти уважно розпізнаєш рукописні цифри з фото бланків. Відповідаєш лише JSON.",
                       [{"role": "user", "content": content}], tools=None)
    out = "".join(c.get("text", "") for c in resp.get("content", []) if c.get("type") == "text")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError(_("ИИ не вернул данные") + ": " + out[:300])
    return json.loads(m.group(0))


def run_scan(app, scan_id, images, sheet_row, user_row, site):
    with app.app_context():
        g.user = user_row
        g.site = site
        db = get_db()
        try:
            s = settings()
            import ai
            periods = json.loads(sheet_row["periods"])
            cols = json.loads(sheet_row["cols"])
            text = PROMPT.format(
                sheet_id=sheet_row["id"],
                cols="\n".join(f"{c['n']}: {c['line']} — {c['title']}" + (f" ({c['sub']})" if c.get("sub") else "")
                               for c in cols),
                rows="\n".join(f"{p} — {calc.period_label(p)}" for p in periods))
            res = call_vision(s.get("ai_api_key"), s.get("ai_model") or ai.DEFAULT_MODEL, images, text)
            db.execute("UPDATE guard_scans SET status='done', result=? WHERE id=?",
                       (json.dumps(res, ensure_ascii=False), scan_id))
        except Exception as e:  # noqa
            traceback.print_exc()
            db.execute("UPDATE guard_scans SET status='error', error=? WHERE id=?", (str(e)[:1000], scan_id))
        db.commit()


@bp.route("/ai/guard", methods=["GET", "POST"])
@auth.perm_required("ai")
def scan_page():
    ensure_schema()
    db = get_db()
    sheets = db.execute("SELECT id, periods, created_at FROM guard_sheets ORDER BY id DESC LIMIT 10").fetchall()
    if request.method == "POST":
        s = settings()
        if not s.get("ai_api_key"):
            flash(_("Сначала укажите API-ключ Anthropic в настройках"))
            return redirect(url_for("ai_settings_page"))
        sh = db.execute("SELECT * FROM guard_sheets WHERE id=?", (request.form.get("sheet_id", type=int),)).fetchone()
        if not sh:
            flash(_("Сначала распечатайте бланк: Показания → «Шаблон для охраны»"))
            return redirect(url_for("guard.scan_page"))
        files = [f for f in request.files.getlist("photos") if f and f.filename][:MAX_PHOTOS]
        if not files:
            flash(_("Выберите фото бланка"))
            return redirect(url_for("guard.scan_page"))
        try:
            images = [_prep_image(f) for f in files]
        except Exception as e:  # noqa
            flash(_("Не удалось открыть фото") + f": {e}")
            return redirect(url_for("guard.scan_page"))
        scan_id = uuid.uuid4().hex[:12]
        db.execute("INSERT INTO guard_scans(id, sheet_id, status, photos, username) VALUES (?,?, 'running', ?, ?)",
                   (scan_id, sh["id"], len(images), g.user["username"]))
        db.commit()
        threading.Thread(target=run_scan, args=(current_app._get_current_object(), scan_id, images, sh, g.user,
                                                current_site()), daemon=True).start()
        return redirect(url_for("guard.review", scan_id=scan_id))
    scans = db.execute("SELECT * FROM guard_scans ORDER BY created_at DESC LIMIT 10").fetchall()
    return render_template("guard_scan.html", sheets=[dict(x, plist=json.loads(x["periods"])) for x in sheets],
                           scans=scans, has_key=bool(settings().get("ai_api_key")))


@bp.route("/ai/guard/<scan_id>/status")
@auth.perm_required("ai")
def scan_status(scan_id):
    r = get_db().execute("SELECT status, error FROM guard_scans WHERE id=?", (scan_id,)).fetchone()
    if not r:
        abort(404)
    return jsonify(status=r["status"], error=r["error"])


# ---------------- перевірка розпізнаного ----------------

def parse_num(raw):
    """«12 345,6» -> 12345.6; з «?» або нечислом -> None."""
    s = str(raw or "").strip().replace(" ", "").replace(" ", "")
    if not s or "?" in s:
        return None
    s = s.replace(",", ".")
    if s.count(".") > 1:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _plain(v):
    return ("%.4f" % v).rstrip("0").rstrip(".").replace(".", ",")


def _months_between(a, b):
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def history(kind, mid, field):
    tbl, col = ("readings", "meter_id") if kind == "m" else ("line_readings", "line_meter_id")
    return [(r["period"], r["v"]) for r in get_db().execute(
        f"SELECT period, {field} AS v FROM {tbl} WHERE {col}=? AND {field} IS NOT NULL ORDER BY period", (mid,))]


def typical(hist, before):
    """Типова місячна витрата: медіана останніх (до 12) місячних різниць до місяця before."""
    pts = [(p, v) for p, v in hist if p < before]
    diffs = []
    for (p1, v1), (p2, v2) in zip(pts, pts[1:]):
        k = _months_between(p1, p2)
        if k > 0 and v2 >= v1:
            diffs.append((v2 - v1) / k)
    diffs = diffs[-12:]
    return statistics.median(diffs) if len(diffs) >= 2 else (diffs[0] if diffs else None)


def judge(v, prev, prevp, period, med):
    """-> (рівень ok|warn|bad, повідомлення) для показника v."""
    if prev is None:
        return "ok", ""
    d = v - prev
    k = max(1, _months_between(prevp, period)) if prevp else 1
    if d < 0:
        return "bad", _("меньше предыдущего ({p})").format(p=calc.fmt_num(prev))
    if med is None:
        if prev > 0 and v > prev * 3 and v - prev > 100:
            return "bad", _("в {x} раз больше предыдущего").format(x=calc.fmt_num(round(v / prev, 1)))
        return "ok", ""
    exp = med * k
    if d > exp * 2.5 + 2:
        return ("bad" if d > exp * 5 + 5 else "warn"), _("скачок: расход {d} при обычном ~{e}").format(
            d=calc.fmt_num(round(d, 1)), e=calc.fmt_num(round(exp, 1)))
    if exp >= 5 and d < exp * 0.35:
        return "warn", _("резкое падение: расход {d} при обычном ~{e}").format(
            d=calc.fmt_num(round(d, 1)), e=calc.fmt_num(round(exp, 1)))
    return "ok", ""


def suggest(v, prev, prevp, period, med):
    """Підозра на зсув коми (охоронці дописують десяті частки або гублять кому): кандидат з найправдоподібнішою витратою."""
    if prev is None:
        return None
    best = None
    k = max(1, _months_between(prevp, period)) if prevp else 1
    for c in (v / 10, v / 100, v / 1000, v * 10):
        lv, _m = judge(c, prev, prevp, period, med)
        if lv == "bad":
            continue
        # спершу кандидати без зауважень, далі — найближчі до звичної витрати
        score = (lv != "ok", abs((c - prev) - (med or 0) * k))
        if best is None or score < best[0]:
            best = (score, round(c, 3))
    return best[1] if best else None


def check(sheet_row, res):
    """Розпізнане -> рядки для сторінки перевірки + список попереджень."""
    periods = json.loads(sheet_row["periods"])
    cols = json.loads(sheet_row["cols"])
    byn = {str(c["n"]): c for c in cols}
    rows_in = {r.get("period"): r for r in (res.get("rows") or []) if r.get("period") in periods}
    hist = {c["n"]: history(c["kind"], c["id"], c["field"]) for c in cols}
    accepted = {c["n"]: [] for c in cols}            # значення з цього ж бланка (попередні рядки) — теж «попередні»
    rows, warns = [], []
    for p in periods:
        r = rows_in.get(p)
        if not r:
            continue
        cells = {}
        for n_s, raw in (r.get("cells") or {}).items():
            c = byn.get(str(n_s).strip())
            if not c or raw in (None, ""):
                continue
            n = c["n"]
            h = [x for x in hist[n] if x[0] < p] + accepted[n]
            h.sort()
            prevp, prev = (h[-1] if h else (None, None))
            existing = next((v for pp, v in hist[n] if pp == p), None)
            v = parse_num(raw)
            cell = {"raw": str(raw), "value": "", "level": "ok", "msg": "", "prev": prev, "existing": existing}
            if v is None:
                cell.update(level="bad", msg=_("не удалось прочитать"))
            else:
                med = typical(h, p)
                lv, msg = judge(v, prev, prevp, p, med)
                cell.update(value=_plain(v), level=lv, msg=msg)
                if lv != "ok":
                    sg = suggest(v, prev, prevp, p, med)
                    if sg is not None:
                        cell["value"] = _plain(sg)
                        cell["msg"] = msg + " — " + _("похоже на ошибку в запятой, исправлено с {raw}").format(raw=raw)
                        cell["level"] = "fix"
                        v = sg
                if existing is not None and abs(existing - v) < 1e-9:
                    cell.update(level="same", msg=_("уже есть в базе"))
                elif existing is not None:
                    cell["msg"] = (cell["msg"] + "; " if cell["msg"] else "") + _("в базе сейчас {v}").format(
                        v=calc.fmt_num(existing))
                    if cell["level"] == "ok":
                        cell["level"] = "warn"
                if cell["level"] in ("ok", "warn", "fix", "same"):
                    accepted[n].append((p, v))
            if cell["level"] in ("bad", "warn", "fix"):
                warns.append({"level": cell["level"], "text": f"{calc.period_label(p)} · №{n} {c['title']}: "
                              f"«{raw}» — {cell['msg']}"})
            cells[n] = cell
        if cells or r.get("date"):
            rows.append({"period": p, "date": r.get("date") or "", "cells": cells})
    return cols, rows, warns


@bp.route("/ai/guard/<scan_id>")
@auth.perm_required("ai")
def review(scan_id):
    ensure_schema()
    db = get_db()
    sc = db.execute("SELECT * FROM guard_scans WHERE id=?", (scan_id,)).fetchone()
    if not sc:
        abort(404)
    sh = db.execute("SELECT * FROM guard_sheets WHERE id=?", (sc["sheet_id"],)).fetchone()
    cols, rows, warns, res = [], [], [], {}
    if sc["status"] in ("done", "saved") and sh:
        res = json.loads(sc["result"] or "{}")
        cols, rows, warns = check(sh, res)
        used = {n for r in rows for n in r["cells"]}
        cols = [c for c in cols if c["n"] in used]
    return render_template("guard_review.html", sc=sc, sh=sh, cols=cols, rows=rows, warns=warns, res=res)


@bp.route("/ai/guard/<scan_id>/saved", methods=["POST"])
@auth.perm_required("ai")
def mark_saved(scan_id):
    db = get_db()
    db.execute("UPDATE guard_scans SET status='saved' WHERE id=?", (scan_id,))
    db.commit()
    n = (request.get_json(silent=True) or {}).get("n", 0)
    log("Показания с бланка охраны сохранены", f"{n} шт.")
    return jsonify(ok=True)


def init_app(app):
    app.register_blueprint(bp)
