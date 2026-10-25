"""Діагностика мережі в Налаштуваннях: ping, перевірка TCP-порту, DNS. Без довільних команд (не термінал):
фіксовані дії, адреса перевіряється, команди запускаються без оболонки."""
import re
import shutil
import socket
import subprocess
import time

from flask import Blueprint, render_template, request

import auth
from audit import log
from i18n import _

bp = Blueprint("diag", __name__)
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:]+\]?$")


def _ping(host):
    exe = shutil.which("ping")
    if not exe:
        return _("ping недоступен в контейнере — используйте проверку порта")
    try:
        r = subprocess.run([exe, "-c", "4", "-W", "2", host], capture_output=True, text=True, timeout=15)
        return (r.stdout + r.stderr).strip() or f"exit {r.returncode}"
    except subprocess.TimeoutExpired:
        return _("Нет ответа (таймаут)")


def _port(host, port):
    t = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=4):
            return f"✓ {host}:{port} — " + _("порт открыт") + f" ({(time.monotonic() - t) * 1000:.0f} ms)"
    except OSError as e:
        return f"✗ {host}:{port} — " + _("нет соединения") + f": {e}"


def _dns(host):
    try:
        infos = socket.getaddrinfo(host, None)
        return "\n".join(sorted({i[4][0] for i in infos})) or "—"
    except OSError as e:
        return _("Имя не найдено") + f": {e}"


@bp.route("/settings/diag", methods=["GET", "POST"])
@auth.admin_required
def page():
    import nas
    out, host, port, tool = None, "", 445, "ping"
    if request.method == "POST":
        host = (request.form.get("host") or "").strip()
        tool = request.form.get("tool") or "ping"
        port = request.form.get("port", 445, type=int) or 445
        if tool == "smb":
            ok, msg = nas.test_connection()
            out = ("✓ " if ok else "✗ ") + _(msg)
            host = nas.config()["server"]
        elif not HOST_RE.match(host):
            out = _("Неверный адрес: только имя или IP, без пробелов и спецсимволов")
        elif tool == "ping":
            out = _ping(host)
        elif tool == "port":
            out = _port(host, max(1, min(65535, port)))
        elif tool == "dns":
            out = _dns(host)
        log("Диагностика сети", f"{tool} {host}" + (f":{port}" if tool == "port" else ""))
    return render_template("diag.html", out=out, host=host or nas.config()["server"], port=port, tool=tool)


def init_app(app):
    app.register_blueprint(bp)
