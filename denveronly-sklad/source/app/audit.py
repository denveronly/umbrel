"""Журнал действий пользователей."""
from flask import g, request

from db import get_db

# понятные названия для действий, которые не логируются явно
LABELS = {
    "warehouse_edit": "Склад сохранён",
    "meter_save": "Счётчики склада изменены",
    "api_polygon": "Контур склада на карте изменён",
    "tenant_edit": "Контакт сохранён",
    "tenant_contacts": "Контактное лицо изменено",
    "tenant_warehouses": "Склады/цены контакта изменены",
    "tenant_services": "Услуги контакта изменены",
    "companies_page": "Компания изменена",
    "settings_page": "Настройки изменены",
    "auth.account": "Свой профиль изменён",
}


def client_ip():
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()


def log(action, details=None, username=None, commit=True):
    try:
        g.audit_done = True
        if username is None:
            u = g.get("user")
            username = u["username"] if u else None
        db = get_db()
        db.execute("INSERT INTO audit(username, ip, action, details) VALUES (?,?,?,?)",
                   (username, client_ip(), action, None if details is None else str(details)[:500]))
        if commit:
            db.commit()
    except Exception:  # журнал не должен ломать основную работу
        pass


def _describe():
    f = request.form
    parts = []
    for key in ("name", "username"):
        if f.get(key):
            parts.append(f.get(key))
    if f.get("delete"):
        parts.append("удаление")
    if f.get("action"):
        parts.append(f.get("action"))
    for k, v in (request.view_args or {}).items():
        parts.append(f"{k}={v}")
    return ", ".join(parts) or None


def init_app(app):
    @app.after_request
    def auto_log(resp):
        if request.method == "POST" and resp.status_code < 400 and not g.get("audit_done") \
                and request.endpoint not in ("auth.login", "auth.logout", "auth.setup") and g.get("user"):
            label = LABELS.get(request.endpoint, request.endpoint or request.path)
            if request.endpoint == "tenant_edit" and request.form.get("delete"):
                label = "Контакт удалён"
            elif request.endpoint == "warehouse_edit" and request.form.get("delete"):
                label = "Склад удалён"
            log(label, _describe())
        return resp
