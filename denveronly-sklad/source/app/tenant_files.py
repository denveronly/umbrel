"""Документи й фото в картці орендаря: PDF (договори, скани) і фото — завантаження, перегляд, скачування, видалення."""
import os
import re
import uuid

from flask import Blueprint, abort, flash, g, redirect, request, send_from_directory, url_for

from audit import log
from db import get_db, photos_dir
from i18n import _

bp = Blueprint("tenant_files", __name__)
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp", ".tif", ".tiff"}
MAX_SIDE, THUMB_SIDE = 2400, 480


def files_for(tid):
    rows = get_db().execute("SELECT * FROM tenant_files WHERE tenant_id=? ORDER BY id DESC", (tid,)).fetchall()
    return [file_dict(r) for r in rows]


def file_dict(r):
    d = dict(r)
    d["url"] = url_for("tenant_files.view", fid=r["id"])
    d["download"] = url_for("tenant_files.download", fid=r["id"])
    d["thumb"] = url_for("tenant_files.thumb", fid=r["id"]) if r["kind"] == "image" else None
    d["caption"] = r["title"] or r["orig_name"] or ""
    return d


def _safe_name(name, default):
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    base = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", base)
    return base[:150] or default


def _save(tid, f):
    ext = os.path.splitext(f.filename or "")[1].lower()
    folder = os.path.join(photos_dir(), "docs", str(tid))
    os.makedirs(folder, exist_ok=True)
    name = uuid.uuid4().hex
    head = f.stream.read(5)
    f.stream.seek(0)
    if ext == ".pdf" or head.startswith(b"%PDF"):
        if not head.startswith(b"%PDF"):
            raise ValueError(f"{f.filename}: " + _("файл не похож на PDF"))
        path = os.path.join(folder, name + ".pdf")
        f.save(path)
        return f"docs/{tid}/{name}.pdf", "pdf", os.path.getsize(path)
    if ext and ext not in IMG_EXT:
        raise ValueError(f"{f.filename}: " + _("можно загружать только PDF и фото"))
    from PIL import Image, ImageOps
    try:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except Exception:
            pass
        im = ImageOps.exif_transpose(Image.open(f.stream))
    except Exception:
        raise ValueError(f"{f.filename}: " + _("не удалось открыть как изображение"))
    if im.mode not in ("RGB", "L"):
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = bg
    im = im.convert("RGB")
    full = im.copy()
    full.thumbnail((MAX_SIDE, MAX_SIDE))
    path = os.path.join(folder, name + ".jpg")
    full.save(path, "JPEG", quality=88, optimize=True)
    th = im.copy()
    th.thumbnail((THUMB_SIDE, THUMB_SIDE))
    th.save(os.path.join(folder, name + "_t.jpg"), "JPEG", quality=80, optimize=True)
    return f"docs/{tid}/{name}.jpg", "image", os.path.getsize(path)


def _back(tid):
    return url_for("tenant_edit", tid=tid) + "#files"


@bp.route("/tenants/<int:tid>/files", methods=["POST"])
def upload(tid):
    db = get_db()
    t = db.execute("SELECT name FROM tenants WHERE id=?", (tid,)).fetchone()
    if not t:
        abort(404)
    ok, errors = 0, []
    title = request.form.get("title", "").strip() or None
    for f in request.files.getlist("files"):
        if not f or not f.filename:
            continue
        try:
            fn, kind, size = _save(tid, f)
            db.execute("INSERT INTO tenant_files(tenant_id, filename, orig_name, kind, size, title, uploaded_by) "
                       "VALUES (?,?,?,?,?,?,?)", (tid, fn, _safe_name(f.filename, "file"), kind, size, title,
                                                  g.user["username"] if g.get("user") else None))
            ok += 1
        except ValueError as e:
            errors.append(str(e))
    db.commit()
    if ok:
        log("Документы арендатора загружены", f"{t['name']}: {ok} шт.")
    flash(_("Загружено файлов") + f": {ok}" + (f". {_('Ошибки')}: {'; '.join(errors)}" if errors else ""))
    return redirect(_back(tid))


def _row(fid):
    r = get_db().execute("SELECT * FROM tenant_files WHERE id=?", (fid,)).fetchone()
    if not r:
        abort(404)
    return r


def _dl_name(r):
    if r["kind"] == "pdf":
        n = r["orig_name"] or "document.pdf"
        return n if n.lower().endswith(".pdf") else n + ".pdf"
    n = os.path.splitext(r["orig_name"] or "foto")[0]
    return n + ".jpg"


@bp.route("/tenant-files/<int:fid>")
def view(fid):
    r = _row(fid)
    return send_from_directory(photos_dir(), r["filename"], max_age=86400, download_name=_dl_name(r))


@bp.route("/tenant-files/<int:fid>/download")
def download(fid):
    r = _row(fid)
    return send_from_directory(photos_dir(), r["filename"], as_attachment=True, download_name=_dl_name(r))


@bp.route("/tenant-files/<int:fid>/thumb")
def thumb(fid):
    r = _row(fid)
    t = r["filename"][:-4] + "_t.jpg"
    if r["kind"] != "image" or not os.path.exists(os.path.join(photos_dir(), t)):
        t = r["filename"]
    return send_from_directory(photos_dir(), t, max_age=86400)


@bp.route("/tenant-files/<int:fid>/edit", methods=["POST"])
def edit(fid):
    r = _row(fid)
    db = get_db()
    if request.form.get("delete"):
        remove_files(r["filename"])
        db.execute("DELETE FROM tenant_files WHERE id=?", (fid,))
        log("Документ арендатора удалён", r["orig_name"] or r["filename"], commit=False)
        flash(_("Файл удалён"))
    else:
        db.execute("UPDATE tenant_files SET title=? WHERE id=?", (request.form.get("title", "").strip() or None, fid))
    db.commit()
    return redirect(_back(r["tenant_id"]))


def remove_files(filename):
    for fn in (filename, filename[:-4] + "_t.jpg"):
        p = os.path.join(photos_dir(), fn)
        if os.path.isfile(p):
            os.remove(p)


def remove_tenant_files(tid):
    for r in get_db().execute("SELECT filename FROM tenant_files WHERE tenant_id=?", (tid,)).fetchall():
        remove_files(r["filename"])


def init_app(app):
    app.register_blueprint(bp)

    @app.context_processor
    def ctx():
        return {"tenant_files_for": files_for}
