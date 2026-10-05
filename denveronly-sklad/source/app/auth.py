"""Аккаунты: первый запуск (создание админа), вход/выход, управление пользователями."""
import os
import secrets
import time
from urllib.parse import urlparse
from functools import wraps

from flask import (Blueprint, abort, flash, g, redirect, render_template, request, session,
                   url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from audit import log
from db import DATA_DIR, get_db

bp = Blueprint("auth", __name__)

ROLES = {"admin": "Администратор", "user": "Оператор"}
MIN_PASSWORD = 8
PUBLIC_ENDPOINTS = {"static", "auth.login", "auth.setup"}

# блокировка подбора пароля: ip -> [число неудач, время первой неудачи]
_FAILS = {}
MAX_FAILS, LOCK_SECONDS = 5, 300


def load_secret_key():
    """SECRET_KEY из окружения или автоматически созданный и сохранённый в /data."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(DATA_DIR, "secret_key")
    if not os.path.exists(path):
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


def users_exist():
    return get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def init_app(app):
    app.secret_key = load_secret_key()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
                      PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)
    app.register_blueprint(bp)

    @app.before_request
    def guard():
        g.user = None
        uid = session.get("uid")
        if uid:
            u = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
            # выход со всех устройств при смене пароля: сверяем «версию» хеша
            if u and session.get("pv") == u["password_hash"][-12:]:
                g.user = u
            else:
                session.clear()
        if request.endpoint in PUBLIC_ENDPOINTS:
            return
        if not users_exist():
            return redirect(url_for("auth.setup"))
        if g.user is None:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        # защита от CSRF: POST принимаем только со своего же сайта.
        # Sec-Fetch-Site ставит сам браузер и он не меняется прокси (Umbrel app_proxy, nginx);
        # для старых браузеров без него сверяем Origin с Host / X-Forwarded-Host.
        if request.method == "POST":
            sfs = request.headers.get("Sec-Fetch-Site")
            if sfs:
                if sfs == "cross-site":
                    abort(403)
            else:
                origin = request.headers.get("Origin") or request.headers.get("Referer")
                hosts = {request.host, request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()}
                if origin and urlparse(origin).netloc not in hosts:
                    abort(403)

    @app.context_processor
    def user_ctx():
        return {"current_user": g.get("user"), "ROLES": ROLES}


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user or g.user["role"] != "admin":
            abort(403)
        return f(*a, **kw)
    return wrapper


def _login(user):
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["pv"] = user["password_hash"][-12:]
    db = get_db()
    db.execute("UPDATE users SET last_login=datetime('now','localtime') WHERE id=?", (user["id"],))
    db.commit()


def _check_password(pw, pw2):
    if len(pw) < MIN_PASSWORD:
        return f"Пароль должен быть не короче {MIN_PASSWORD} символов"
    if pw != pw2:
        return "Пароли не совпадают"
    return None


def _safe_next(url):
    return url if url and url.startswith("/") and not url.startswith("//") else url_for("index")


# ---------------- первый запуск ----------------

@bp.route("/setup", methods=["GET", "POST"])
def setup():
    if users_exist():
        return redirect(url_for("auth.login"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        pw, pw2 = request.form.get("password", ""), request.form.get("password2", "")
        error = (None if username else "Укажите логин") or _check_password(pw, pw2)
        if not error:
            db = get_db()
            db.execute("INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?, 'admin')",
                       (username, request.form.get("full_name", "").strip(), generate_password_hash(pw)))
            db.commit()
            _login(db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())
            log("Создан главный администратор", username, username=username)
            flash("Главный администратор создан. Добро пожаловать!")
            return redirect(url_for("settings_page"))
    return render_template("auth_setup.html", error=error)


# ---------------- вход / выход ----------------

@bp.route("/login", methods=["GET", "POST"])
def login():
    if not users_exist():
        return redirect(url_for("auth.setup"))
    if g.get("user"):
        return redirect(_safe_next(request.args.get("next")))
    error = None
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        fails, since = _FAILS.get(ip, (0, time.time()))
        if time.time() - since > LOCK_SECONDS:
            fails, since = 0, time.time()
        if fails >= MAX_FAILS:
            error = "Слишком много неудачных попыток. Подождите 5 минут."
        else:
            u = get_db().execute("SELECT * FROM users WHERE username=?",
                                 (request.form.get("username", "").strip(),)).fetchone()
            if u and u["active"] and check_password_hash(u["password_hash"], request.form.get("password", "")):
                _FAILS.pop(ip, None)
                _login(u)
                log("Вход в систему", username=u["username"])
                return redirect(_safe_next(request.form.get("next")))
            _FAILS[ip] = (fails + 1, since)
            log("Неудачная попытка входа", f"логин: {request.form.get('username', '')[:50]}", username="—")
            time.sleep(0.5)
            error = "Неверный логин или пароль"
    return render_template("auth_login.html", error=error, next=request.values.get("next", ""))


@bp.route("/logout", methods=["POST"])
def logout():
    if g.get("user"):
        log("Выход из системы")
    session.clear()
    return redirect(url_for("auth.login"))


# ---------------- свой профиль ----------------

@bp.route("/account", methods=["GET", "POST"])
def account():
    error = None
    if request.method == "POST":
        u = g.user
        if not check_password_hash(u["password_hash"], request.form.get("current", "")):
            error = "Текущий пароль указан неверно"
        else:
            error = _check_password(request.form.get("password", ""), request.form.get("password2", ""))
        if not error:
            db = get_db()
            db.execute("UPDATE users SET password_hash=?, full_name=? WHERE id=?",
                       (generate_password_hash(request.form["password"]),
                        request.form.get("full_name", "").strip(), u["id"]))
            db.commit()
            _login(db.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())
            log("Сменил свой пароль")
            flash("Пароль изменён. На других устройствах нужно будет войти заново.")
            return redirect(url_for("auth.account"))
    return render_template("auth_account.html", error=error)


# ---------------- пользователи (только админ) ----------------

def _admins_left(exclude_id):
    return get_db().execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",
                            (exclude_id,)).fetchone()[0]


@bp.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    db = get_db()
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        uid = request.form.get("uid", type=int)
        target = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None

        if action == "create":
            username = request.form.get("username", "").strip()
            pw = request.form.get("password", "")
            error = (None if username else "Укажите логин") or _check_password(pw, request.form.get("password2", ""))
            if not error and db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                error = "Такой логин уже есть"
            if not error:
                role = request.form.get("role") if request.form.get("role") in ROLES else "user"
                db.execute("INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)",
                           (username, request.form.get("full_name", "").strip(), generate_password_hash(pw), role))
                log("Пользователь создан", f"{username} ({ROLES[role]})", commit=False)
                flash(f"Пользователь {username} создан")

        elif target and action == "reset":
            pw = request.form.get("password", "")
            error = _check_password(pw, pw)
            if not error:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
                log("Пароль пользователя сброшен", target["username"], commit=False)
                flash(f"Пароль для {target['username']} изменён")

        elif target and action == "role":
            role = request.form.get("role")
            if role not in ROLES:
                abort(400)
            if target["role"] == "admin" and role != "admin" and not _admins_left(uid):
                error = "Нельзя убрать последнего администратора"
            else:
                db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
                log("Роль изменена", f"{target['username']} → {ROLES[role]}", commit=False)
                flash(f"Роль {target['username']}: {ROLES[role]}")

        elif target and action == "toggle":
            if uid == g.user["id"]:
                error = "Нельзя отключить самого себя"
            elif target["role"] == "admin" and target["active"] and not _admins_left(uid):
                error = "Нельзя отключить последнего администратора"
            else:
                db.execute("UPDATE users SET active=1-active WHERE id=?", (uid,))
                log("Пользователь включён/отключён", f"{target['username']}: {'отключён' if target['active'] else 'включён'}", commit=False)
                flash(f"{target['username']}: {'отключён' if target['active'] else 'включён'}")

        elif target and action == "delete":
            if uid == g.user["id"]:
                error = "Нельзя удалить самого себя"
            elif target["role"] == "admin" and not _admins_left(uid):
                error = "Нельзя удалить последнего администратора"
            else:
                db.execute("DELETE FROM users WHERE id=?", (uid,))
                log("Пользователь удалён", target["username"], commit=False)
                flash(f"Пользователь {target['username']} удалён")
        db.commit()
        if not error:
            return redirect(url_for("auth.users"))

    rows = db.execute("SELECT * FROM users ORDER BY role, username").fetchall()
    return render_template("auth_users.html", rows=rows, error=error, MIN_PASSWORD=MIN_PASSWORD)
