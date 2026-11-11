"""Нотатки: простий блокнот у кожному об'єкті («База», «Офіс», «Дім»…). Текст зберігається в базі об'єкта
(налаштування notes_text), автозбереження з браузера; видно, хто й коли змінив останнім."""
import os
import re
from datetime import datetime

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

from db import get_db, set_setting, settings, site_dir
from i18n import _

bp = Blueprint("notes", __name__)
MAX = 200_000
MAX_FILE = 50 * 1024 * 1024


def files_dir(site=None):
    d = os.path.join(site_dir(site), "notes_files")
    os.makedirs(d, exist_ok=True)
    return d


def _safe(name):
    """Ім'я файлу без шляхів і небезпечних символів (кирилиця лишається)."""
    name = os.path.basename((name or "").replace("\\", "/")).strip().strip(".")
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", name)[:120]
    return name or "file"


def list_files():
    d = files_dir()
    out = []
    for n in sorted(os.listdir(d), key=lambda n: -os.path.getmtime(os.path.join(d, n))):
        p = os.path.join(d, n)
        if os.path.isfile(p):
            out.append({"name": n, "size": os.path.getsize(p),
                        "time": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%d.%m.%Y %H:%M")})
    return out


@bp.route("/notes")
def page():
    s = settings()
    return render_template("notes.html", text=s.get("notes_text") or "", who=s.get("notes_who") or "",
                           when=s.get("notes_when") or "", files=list_files())


@bp.route("/notes/upload", methods=["POST"])
def upload():
    from audit import log
    n_ok = 0
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        name = _safe(f.filename)
        d = files_dir()
        base, ext = os.path.splitext(name)
        i = 1
        while os.path.exists(os.path.join(d, name)):        # не перезаписуємо: «файл (2).pdf»
            i += 1
            name = f"{base} ({i}){ext}"
        p = os.path.join(d, name)
        f.save(p)
        if os.path.getsize(p) > MAX_FILE:
            os.remove(p)
            flash(_("Файл больше 50 МБ") + f": {name}")
            continue
        n_ok += 1
        log("Файл добавлен в блокнот", name)
    get_db().commit()
    if n_ok:
        flash(_("Добавлено файлов") + f": {n_ok}")
    return redirect(url_for("notes.page"))


@bp.route("/notes/file/<path:name>")
def download(name):
    p = os.path.join(files_dir(), _safe(name))
    if not os.path.isfile(p):
        abort(404)
    return send_file(p, as_attachment=request.args.get("dl") == "1", download_name=os.path.basename(p))


@bp.route("/notes/file/<path:name>/delete", methods=["POST"])
def delete(name):
    from audit import log
    p = os.path.join(files_dir(), _safe(name))
    if os.path.isfile(p):
        os.remove(p)
        log("Файл удалён из блокнота", os.path.basename(p))
        get_db().commit()
        flash(_("Файл удалён"))
    return redirect(url_for("notes.page"))


@bp.route("/notes/save", methods=["POST"])
def save():
    text = (request.form.get("text") or "")[:MAX]
    now = datetime.now().isoformat(timespec="seconds")
    who = g.user["full_name"] or g.user["username"]
    set_setting("notes_text", text)
    set_setting("notes_who", who)
    set_setting("notes_when", now)
    get_db().commit()
    return jsonify({"ok": True, "who": who, "when": now})


def init_app(app):
    app.register_blueprint(bp)
