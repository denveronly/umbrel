"""Фото об'єктів: завантаження, мініатюри, перегляд, видалення."""
import os
import uuid

from flask import (Blueprint, abort, flash, g, jsonify, redirect, request, send_from_directory, url_for)

from audit import log
from db import PHOTOS_DIR, get_db

bp = Blueprint("photos", __name__)
MAX_SIDE, THUMB_SIDE = 2000, 480
ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff"}

try:  # фото з iPhone (HEIC), якщо пакет встановлено
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass


def photos_for(wid):
    rows = get_db().execute("SELECT * FROM warehouse_photos WHERE warehouse_id=? ORDER BY id DESC", (wid,)).fetchall()
    return [photo_dict(r) for r in rows]


def photo_dict(r):
    return {"id": r["id"], "caption": r["caption"] or "", "created_at": r["created_at"],
            "url": url_for("photos.full", pid=r["id"]), "thumb": url_for("photos.thumb", pid=r["id"])}


def _save(wid, f):
    from PIL import Image, ImageOps
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext and ext not in ALLOWED:
        raise ValueError(f"{f.filename}: не изображение")
    folder = os.path.join(PHOTOS_DIR, str(wid))
    os.makedirs(folder, exist_ok=True)
    name = uuid.uuid4().hex
    try:
        im = Image.open(f.stream)
        im = ImageOps.exif_transpose(im)          # поворот за EXIF (фото з телефона)
    except Exception:
        raise ValueError(f"{f.filename}: не удалось открыть как изображение")
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = bg
    im = im.convert("RGB")
    full = im.copy()
    full.thumbnail((MAX_SIDE, MAX_SIDE))
    full.save(os.path.join(folder, name + ".jpg"), "JPEG", quality=85, optimize=True)
    th = im.copy()
    th.thumbnail((THUMB_SIDE, THUMB_SIDE))
    th.save(os.path.join(folder, name + "_t.jpg"), "JPEG", quality=80, optimize=True)
    return f"{wid}/{name}.jpg"


@bp.route("/warehouses/<int:wid>/photos", methods=["POST"])
def upload(wid):
    db = get_db()
    wh = db.execute("SELECT name FROM warehouses WHERE id=?", (wid,)).fetchone()
    if not wh:
        abort(404)
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    ok, errors = 0, []
    for f in files:
        try:
            fn = _save(wid, f)
            db.execute("INSERT INTO warehouse_photos(warehouse_id, filename, caption, uploaded_by) VALUES (?,?,?,?)",
                       (wid, fn, request.form.get("caption", "").strip() or None,
                        g.user["username"] if g.get("user") else None))
            ok += 1
        except ValueError as e:
            errors.append(str(e))
    db.commit()
    if ok:
        log("Фото загружены", f"{wh['name']}: {ok} шт.")
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=ok, errors=errors, photos=photos_for(wid))
    flash(f"Загружено фото: {ok}" + (f". Ошибки: {'; '.join(errors)}" if errors else ""))
    return redirect(_back(wid))


def _back(wid):
    nxt = request.form.get("next") or request.args.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("warehouse_edit", wid=wid)


def _row(pid):
    r = get_db().execute("SELECT * FROM warehouse_photos WHERE id=?", (pid,)).fetchone()
    if not r:
        abort(404)
    return r


@bp.route("/photos/<int:pid>")
def full(pid):
    r = _row(pid)
    return send_from_directory(PHOTOS_DIR, r["filename"], max_age=86400 * 30)


@bp.route("/photos/<int:pid>/thumb")
def thumb(pid):
    r = _row(pid)
    t = r["filename"][:-4] + "_t.jpg"
    if not os.path.exists(os.path.join(PHOTOS_DIR, t)):
        t = r["filename"]
    return send_from_directory(PHOTOS_DIR, t, max_age=86400 * 30)


@bp.route("/photos/<int:pid>/delete", methods=["POST"])
def delete(pid):
    r = _row(pid)
    remove_files(r["filename"])
    db = get_db()
    db.execute("DELETE FROM warehouse_photos WHERE id=?", (pid,))
    db.commit()
    wh = db.execute("SELECT name FROM warehouses WHERE id=?", (r["warehouse_id"],)).fetchone()
    log("Фото удалено", wh["name"] if wh else r["warehouse_id"])
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, photos=photos_for(r["warehouse_id"]))
    return redirect(_back(r["warehouse_id"]))


@bp.route("/photos/<int:pid>/caption", methods=["POST"])
def caption(pid):
    r = _row(pid)
    db = get_db()
    db.execute("UPDATE warehouse_photos SET caption=? WHERE id=?", (request.form.get("caption", "").strip() or None, pid))
    db.commit()
    return jsonify(ok=True) if request.headers.get("X-Requested-With") == "fetch" else redirect(_back(r["warehouse_id"]))


def remove_files(filename):
    for fn in (filename, filename[:-4] + "_t.jpg"):
        p = os.path.join(PHOTOS_DIR, fn)
        if os.path.exists(p):
            os.remove(p)


def remove_warehouse_photos(wid):
    for r in get_db().execute("SELECT filename FROM warehouse_photos WHERE warehouse_id=?", (wid,)).fetchall():
        remove_files(r["filename"])


def init_app(app):
    app.config.setdefault("MAX_CONTENT_LENGTH", 200 * 1024 * 1024)
    app.register_blueprint(bp)

    @app.context_processor
    def ctx():
        return {"photos_for": photos_for}
