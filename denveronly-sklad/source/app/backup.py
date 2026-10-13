"""Вкладка «Логи»: журнал действий, состояние базы, резервные копии."""
import io
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timedelta

from flask import (Blueprint, abort, flash, redirect, render_template, request, send_file, url_for)

import auth
from audit import log
from i18n import _
from db import (DATA_DIR, SITES, backup_dir, current_site, db_path, get_db, init_db, photos_dir, set_setting, settings,
                uploads_dir, using_site)

bp = Blueprint("logs", __name__)
NAME_RE = re.compile(r"^sklad_\d{4}-\d{2}-\d{2}_\d{6}_(manual|auto|pre-restore|pre-ai|upload)\.zip$")
KINDS = {"manual": "вручную", "auto": "авто", "pre-restore": "перед восстановлением", "pre-ai": "перед ИИ", "upload": "загружен"}

TABLES = [("warehouses", "Склады"), ("tenants", "Контакты"), ("tenant_contacts", "Контактные лица"),
          ("companies", "Компании"), ("warehouse_categories", "Подкатегории"), ("services", "Услуги"), ("meters", "Счётчики"),
          ("readings", "Показания"), ("service_prices", "Цены по месяцам"), ("service_usage", "Количества услуг"),
          ("acts", "Акты"), ("warehouse_photos", "Фото объектов"), ("users", "Пользователи"), ("audit", "Записи журнала")]


# ---------------- резервные копии ----------------

def create_backup(kind="manual"):
    """ZIP с консистентной копией базы (sqlite backup API) и загруженными файлами (план)."""
    os.makedirs(backup_dir(), exist_ok=True)
    name = f"sklad_{datetime.now():%Y-%m-%d_%H%M%S}_{kind}.zip"
    path = os.path.join(backup_dir(), name)
    with tempfile.TemporaryDirectory() as tmp:
        snap = os.path.join(tmp, "sklad.db")
        src, dst = sqlite3.connect(db_path()), sqlite3.connect(snap)
        src.backup(dst)
        src.close()
        dst.close()
        with zipfile.ZipFile(path + ".part", "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "sklad.db")
            if os.path.isdir(uploads_dir()):
                for f in os.listdir(uploads_dir()):
                    z.write(os.path.join(uploads_dir(), f), f"uploads/{f}")
            # фото об'єктів (вже стиснені JPEG — без повторного стиснення)
            for root, _dirs, files in os.walk(photos_dir()):
                for f in files:
                    p = os.path.join(root, f)
                    z.write(p, "photos/" + os.path.relpath(p, photos_dir()).replace(os.sep, "/"),
                            compress_type=zipfile.ZIP_STORED)
    os.replace(path + ".part", path)
    return name


def list_backups():
    out = []
    if os.path.isdir(backup_dir()):
        for f in sorted(os.listdir(backup_dir()), reverse=True):
            if NAME_RE.match(f):
                p = os.path.join(backup_dir(), f)
                kind = NAME_RE.match(f).group(1)
                out.append({"name": f, "size": os.path.getsize(p), "kind": KINDS[kind], "kind_code": kind,
                            "time": datetime.strptime(f[6:23], "%Y-%m-%d_%H%M%S")})
    return out


def prune(keep):
    """Оставляем последние N автоматических копий; ручные не трогаем."""
    autos = [b for b in list_backups() if b["kind_code"] in ("auto", "pre-restore", "pre-ai")]
    for b in autos[keep:]:
        os.remove(os.path.join(backup_dir(), b["name"]))


def _safe_path(name):
    if not NAME_RE.match(name or ""):
        abort(404)
    p = os.path.join(backup_dir(), name)
    if not os.path.exists(p):
        abort(404)
    return p


def restore_from_zip(path):
    """Проверяет архив и заливает базу поверх текущей (через sqlite backup API — безопасно при работе)."""
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "sklad.db" not in names:
                raise ValueError("В архиве нет sklad.db")
            z.extract("sklad.db", tmp)
            ups = [n for n in names if n.startswith(("uploads/", "photos/")) and not n.endswith("/") and ".." not in n]
            for n in ups:
                z.extract(n, tmp)
        snap = os.path.join(tmp, "sklad.db")
        con = sqlite3.connect(snap)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise ValueError("База в архиве повреждена")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"warehouses", "tenants", "users"} <= tables:
                raise ValueError("Это не база этого приложения")
            dst = sqlite3.connect(db_path())
            con.backup(dst)
            dst.close()
        finally:
            con.close()
        src_up = os.path.join(tmp, "uploads")
        if os.path.isdir(src_up):
            os.makedirs(uploads_dir(), exist_ok=True)
            for f in os.listdir(src_up):
                shutil.copy2(os.path.join(src_up, f), os.path.join(uploads_dir(), f))
        src_ph = os.path.join(tmp, "photos")
        if os.path.isdir(src_ph):                    # фото — точно як у бэкапі
            shutil.rmtree(photos_dir(), ignore_errors=True)
            shutil.copytree(src_ph, photos_dir())


def maybe_auto_backup():
    """Автобекап раз на добу — для кожного об'єкта окремо (у його папку backups)."""
    for site in SITES:
        with using_site(site):
            _auto_backup_site()


def _auto_backup_site():
    s = settings()
    if s.get("auto_backup") != "1":
        return
    last = s.get("last_auto_backup")
    if last and datetime.now() - datetime.fromisoformat(last) < timedelta(hours=24):
        return
    set_setting("last_auto_backup", datetime.now().isoformat(timespec="seconds"))
    get_db().commit()
    try:
        name = create_backup("auto")
        prune(int(s.get("backup_keep") or 30))
        log("Автоматический бэкап", name, username="система")
    except Exception as e:
        log("Ошибка автоматического бэкапа", e, username="система")


# ---------------- страница ----------------

def db_stats():
    db = get_db()
    st = {"size": os.path.getsize(db_path()),
          "wal": os.path.getsize(db_path() + "-wal") if os.path.exists(db_path() + "-wal") else 0,
          "uploads": sum(os.path.getsize(os.path.join(uploads_dir(), f)) for f in os.listdir(uploads_dir()))
          if os.path.isdir(uploads_dir()) else 0,
          "photos": sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(photos_dir()) for f in fs),
          "sqlite": sqlite3.sqlite_version,
          "tables": [(label, db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t, label in TABLES]}
    r = db.execute("SELECT MIN(period), MAX(period) FROM readings").fetchone()
    st["periods"] = (r[0], r[1])
    st["acts_final"] = db.execute("SELECT COUNT(*) FROM acts WHERE status='final'").fetchone()[0]
    st["acts_sum"] = db.execute("SELECT COALESCE(SUM(total),0) FROM acts").fetchone()[0]
    du = shutil.disk_usage(DATA_DIR)
    st["disk_free"], st["disk_total"] = du.free, du.total
    return st


@bp.route("/logs")
@auth.admin_required
def logs():
    db = get_db()
    q = request.args.get("q", "").strip()
    user = request.args.get("user", "")
    page = max(1, request.args.get("page", 1, type=int))
    per = 100
    where, args = ["1=1"], []
    if q:
        where.append("(action LIKE ? OR details LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if user:
        where.append("username=?")
        args.append(user)
    w = " AND ".join(where)
    total = db.execute(f"SELECT COUNT(*) FROM audit WHERE {w}", args).fetchone()[0]
    rows = db.execute(f"SELECT * FROM audit WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?",
                      args + [per, (page - 1) * per]).fetchall()
    users = [r[0] for r in db.execute("SELECT DISTINCT username FROM audit WHERE username IS NOT NULL ORDER BY 1")]
    return render_template("logs.html", rows=rows, q=q, user=user, users=users, page=page,
                           pages=max(1, (total + per - 1) // per), total=total, tab="journal")


@bp.route("/logs/db", methods=["GET", "POST"])
@auth.admin_required
def database():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "backup":
            name = create_backup("manual")
            log("Бэкап создан вручную", name)
            flash(_("Бэкап создан") + f": {name}")
        elif action == "settings":
            set_setting("auto_backup", "1" if request.form.get("auto_backup") else "0")
            set_setting("backup_keep", str(max(1, request.form.get("backup_keep", 30, type=int))))
            db.commit()
            log("Настройки бэкапа изменены")
            flash(_("Настройки бэкапа сохранены"))
        elif action == "delete":
            p = _safe_path(request.form.get("name"))
            os.remove(p)
            log("Бэкап удалён", request.form.get("name"))
            flash(_("Бэкап удалён"))
        elif action in ("restore", "upload"):
            if action == "restore":
                path = _safe_path(request.form.get("name"))
                src_name = request.form.get("name")
            else:
                f = request.files.get("file")
                if not f or not f.filename:
                    abort(400)
                src_name = f"sklad_{datetime.now():%Y-%m-%d_%H%M%S}_upload.zip"
                path = os.path.join(backup_dir(), src_name)
                f.save(path)
                if not zipfile.is_zipfile(path):
                    os.remove(path)
                    flash(_("Это не ZIP-архив бэкапа"))
                    return redirect(url_for("logs.database"))
            pre = create_backup("pre-restore")
            try:
                db.close()
                from flask import g
                g.pop("db", None)
                restore_from_zip(path)
            except Exception as e:
                flash(_("Восстановление не выполнено") + f": {_(str(e))}")
                return redirect(url_for("logs.database"))
            init_db(current_site())  # досоздаём новые таблицы/колонки, если бэкап старой версии
            log("База восстановлена из бэкапа", f"{src_name} (текущая сохранена как {pre})")
            flash(_("База восстановлена из {a}. Предыдущее состояние сохранено: {b}").format(a=src_name, b=pre))
        elif action == "check":
            res = db.execute("PRAGMA integrity_check").fetchone()[0]
            log("Проверка целостности базы", res)
            flash(_("Проверка целостности: всё в порядке") if res == "ok" else _("Проблема") + f": {res}")
        elif action == "vacuum":
            before = os.path.getsize(db_path())
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
            log("Оптимизация базы (VACUUM)")
            flash(_("База оптимизирована") + f": {before // 1024} КБ → {os.path.getsize(db_path()) // 1024} КБ")
        elif action == "clear_log":
            days = max(30, request.form.get("days", 365, type=int))
            n = db.execute("DELETE FROM audit WHERE ts < datetime('now', 'localtime', ?)", (f"-{days} days",)).rowcount
            db.commit()
            log("Журнал очищен", f"удалено {n} записей старше {days} дней")
            flash(_("Удалено записей журнала") + f": {n}")
        return redirect(url_for("logs.database"))
    return render_template("logs_db.html", st=db_stats(), backups=list_backups(), tab="db",
                           last_auto=settings().get("last_auto_backup"))


@bp.route("/logs/backup/<name>")
@auth.admin_required
def download(name):
    log("Бэкап скачан", name)
    return send_file(_safe_path(name), as_attachment=True, download_name=name)


def init_app(app):
    app.register_blueprint(bp)

    @app.before_request
    def _auto():
        if request.endpoint and request.endpoint != "static":
            try:
                maybe_auto_backup()
            except Exception:
                pass

    @app.template_filter("filesize")
    def filesize(n):
        for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
            if n < 1024 or unit == "ТБ":
                return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}".replace(".", ",")
            n /= 1024
