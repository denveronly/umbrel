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
from i18n import _
from db import DATA_DIR, get_main_db

bp = Blueprint("auth", __name__)

ROLES = {"admin": "Администратор", "user": "Оператор"}
# додаткові права оператора (адміністратор має всі). Користувачами керує лише адміністратор.
PERMS = {"economics": "Экономика", "logs": "Логи и бэкапы", "settings": "Настройки и компании", "ai": "ИИ-помощник",
         "yasno": "YASNO (счета за электричество)"}
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
    return get_main_db().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


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
            u = get_main_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
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
        # защита от CSRF: POST принимаем только со своих же страниц.
        if request.method == "POST":
            reason = csrf_problem()
            if reason:
                log("Запрос отклонён (защита CSRF)", f"{request.method} {request.path}: {reason}")
                abort(403, _("Запрос отклонён защитой от подделки. Обновите страницу (F5) и повторите."))

    @app.context_processor
    def user_ctx():
        return {"current_user": g.get("user"), "ROLES": ROLES, "PERMS": PERMS, "can": can, "csrf_token": csrf_token,
                "pin_configured": pin_configured}


def csrf_token():
    """Токен сесії: шаблон кладе його в <meta>, а скрипт у base.html — у кожну форму й fetch."""
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def csrf_problem():
    """None, якщо POST прийшов з нашої сторінки, інакше — причина відмови (пишеться в журнал).

    1) Токен з форми / заголовка X-CSRF-Token збігається з токеном сесії — точно наша сторінка.
       Це головна перевірка: працює за будь-яким проксі (Umbrel app_proxy міняє Host) і по http,
       де браузер не надсилає Sec-Fetch-Site.
    2) Сторінки, відкриті до оновлення (без токена): Sec-Fetch-Site від браузера, або Origin = Host.
    """
    sent = request.headers.get("X-CSRF-Token") or request.form.get("_csrf")
    if sent:
        return None if secrets.compare_digest(sent, session.get("csrf", "")) else "токен не совпал (страница устарела?)"
    sfs = request.headers.get("Sec-Fetch-Site")
    if sfs:
        return "Sec-Fetch-Site: cross-site" if sfs == "cross-site" else None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        return None
    hosts = {request.host, request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()}
    o = urlparse(origin).netloc
    return None if o in hosts else f"Origin {o} ≠ Host {', '.join(h for h in hosts if h)}"


def can(perm, user=None):
    """Чи має користувач право: адміністратор — завжди, оператор — якщо відмічено в його правах."""
    u = user if user is not None else g.get("user")
    if not u:
        return False
    if u["role"] == "admin":
        return True
    try:
        perms = u["perms"] or ""
    except (IndexError, KeyError):
        perms = ""
    return perm in {p.strip() for p in perms.split(",")}


def perm_required(perm):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not can(perm):
                abort(403, _("Нет доступа. Права выдаёт администратор в Настройки → Пользователи."))
            return f(*a, **kw)
        return wrapper
    return deco


def _form_perms():
    return ",".join(p for p in PERMS if request.form.get("perm_" + p))


# ---------- PIN адміністратора (видалення лічильників, складів) ----------
_PIN_FAILS = {}


def pin_configured():
    return get_main_db().execute("SELECT 1 FROM users WHERE role='admin' AND active=1 AND pin_hash IS NOT NULL "
                                 "LIMIT 1").fetchone() is not None


def check_pin(pin):
    """None — PIN вірний (будь-якого активного адміністратора), інакше текст помилки. Не більше 5 спроб за 5 хв."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    fails, since = _PIN_FAILS.get(ip, (0, time.time()))
    if time.time() - since > LOCK_SECONDS:
        fails, since = 0, time.time()
    if fails >= MAX_FAILS:
        return _("Слишком много неверных PIN. Подождите 5 минут.")
    rows = get_main_db().execute("SELECT pin_hash FROM users WHERE role='admin' AND active=1 AND pin_hash IS NOT NULL"
                                 ).fetchall()
    if not rows:
        return _("PIN не задан. Администратор задаёт его в Настройки → Пользователи.")
    if pin and any(check_password_hash(r["pin_hash"], str(pin)) for r in rows):
        _PIN_FAILS.pop(ip, None)
        return None
    _PIN_FAILS[ip] = (fails + 1, since)
    log("Неверный PIN при удалении", request.path)
    time.sleep(0.4)
    return _("Неверный PIN")


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not g.user or g.user["role"] != "admin":
            abort(403)
        return f(*a, **kw)
    return wrapper


def _login(user):
    csrf = session.get("csrf")
    session.clear()
    if csrf:                       # токен сторінки входу лишається дійсним
        session["csrf"] = csrf
    session.permanent = True
    session["uid"] = user["id"]
    session["pv"] = user["password_hash"][-12:]
    db = get_main_db()
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
        error = (None if username else _("Укажите логин")) or _check_password(pw, pw2)
        if not error:
            db = get_main_db()
            db.execute("INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?, 'admin')",
                       (username, request.form.get("full_name", "").strip(), generate_password_hash(pw)))
            db.commit()
            _login(db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())
            log("Создан главный администратор", username, username=username)
            flash(_("Главный администратор создан. Добро пожаловать!"))
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
            error = _("Слишком много неудачных попыток. Подождите 5 минут.")
        else:
            u = get_main_db().execute("SELECT * FROM users WHERE username=?",
                                 (request.form.get("username", "").strip(),)).fetchone()
            if u and u["active"] and check_password_hash(u["password_hash"], request.form.get("password", "")):
                _FAILS.pop(ip, None)
                _login(u)
                log("Вход в систему", username=u["username"])
                return redirect(_safe_next(request.form.get("next")))
            _FAILS[ip] = (fails + 1, since)
            log("Неудачная попытка входа", f"логин: {request.form.get('username', '')[:50]}", username="—")
            time.sleep(0.5)
            error = _("Неверный логин или пароль")
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
            error = _("Текущий пароль указан неверно")
        else:
            error = _check_password(request.form.get("password", ""), request.form.get("password2", ""))
        if not error:
            db = get_main_db()
            db.execute("UPDATE users SET password_hash=?, full_name=? WHERE id=?",
                       (generate_password_hash(request.form["password"]),
                        request.form.get("full_name", "").strip(), u["id"]))
            db.commit()
            _login(db.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone())
            log("Сменил свой пароль")
            flash(_("Пароль изменён. На других устройствах нужно будет войти заново."))
            return redirect(url_for("auth.account"))
    return render_template("auth_account.html", error=error)


# ---------------- пользователи (только админ) ----------------

def _admins_left(exclude_id):
    return get_main_db().execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",
                            (exclude_id,)).fetchone()[0]


@bp.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    db = get_main_db()
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        uid = request.form.get("uid", type=int)
        target = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None

        if action == "create":
            username = request.form.get("username", "").strip()
            pw = request.form.get("password", "")
            error = (None if username else _("Укажите логин")) or _check_password(pw, request.form.get("password2", ""))
            if not error and db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                error = _("Такой логин уже есть")
            if not error:
                role = request.form.get("role") if request.form.get("role") in ROLES else "user"
                perms = _form_perms()
                db.execute("INSERT INTO users(username, full_name, password_hash, role, perms) VALUES (?,?,?,?,?)",
                           (username, request.form.get("full_name", "").strip(), generate_password_hash(pw), role, perms))
                log("Пользователь создан", f"{username} ({ROLES[role]}" + (f"; права: {perms}" if role != "admin" and perms else "") + ")",
                    commit=False)
                flash(_("Пользователь {u} создан").format(u=username))

        elif target and action == "reset":
            pw = request.form.get("password", "")
            error = _check_password(pw, pw)
            if not error:
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
                log("Пароль пользователя сброшен", target["username"], commit=False)
                flash(_("Пароль для {u} изменён").format(u=target["username"]))

        elif target and action == "role":
            role = request.form.get("role")
            if role not in ROLES:
                abort(400)
            if target["role"] == "admin" and role != "admin" and not _admins_left(uid):
                error = _("Нельзя убрать последнего администратора")
            else:
                db.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
                log("Роль изменена", f"{target['username']} → {ROLES[role]}", commit=False)
                flash(_("Роль") + f" {target['username']}: " + _(ROLES[role]))

        elif target and action == "perms":
            perms = _form_perms()
            db.execute("UPDATE users SET perms=? WHERE id=?", (perms, uid))
            log("Права пользователя изменены", f"{target['username']}: {perms or '—'}", commit=False)
            flash(_("Права сохранены") + f": {target['username']}")

        elif target and action == "pin":
            pin = request.form.get("pin", "").strip()
            if target["role"] != "admin":
                error = _("PIN бывает только у администратора")
            elif request.form.get("clear"):
                db.execute("UPDATE users SET pin_hash=NULL WHERE id=?", (uid,))
                log("PIN администратора удалён", target["username"], commit=False)
                flash(_("PIN удалён") + f": {target['username']}")
            elif not (pin.isdigit() and 4 <= len(pin) <= 8):
                error = _("PIN — от 4 до 8 цифр")
            else:
                db.execute("UPDATE users SET pin_hash=? WHERE id=?", (generate_password_hash(pin), uid))
                log("PIN администратора задан", target["username"], commit=False)
                flash(_("PIN задан") + f": {target['username']}")

        elif target and action == "toggle":
            if uid == g.user["id"]:
                error = _("Нельзя отключить самого себя")
            elif target["role"] == "admin" and target["active"] and not _admins_left(uid):
                error = _("Нельзя отключить последнего администратора")
            else:
                db.execute("UPDATE users SET active=1-active WHERE id=?", (uid,))
                log("Пользователь включён/отключён", f"{target['username']}: {'отключён' if target['active'] else 'включён'}", commit=False)
                flash(f"{target['username']}: " + (_("отключён") if target["active"] else _("включён")))

        elif target and action == "delete":
            if uid == g.user["id"]:
                error = _("Нельзя удалить самого себя")
            elif target["role"] == "admin" and not _admins_left(uid):
                error = _("Нельзя удалить последнего администратора")
            else:
                db.execute("DELETE FROM users WHERE id=?", (uid,))
                log("Пользователь удалён", target["username"], commit=False)
                flash(_("Пользователь {u} удалён").format(u=target["username"]))
        db.commit()
        if not error:
            return redirect(url_for("auth.users"))

    rows = db.execute("SELECT * FROM users ORDER BY role, username").fetchall()
    return render_template("auth_users.html", rows=rows, error=error, MIN_PASSWORD=MIN_PASSWORD)
