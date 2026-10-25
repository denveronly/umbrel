"""Копія бекапів на мережеву папку (SMB) — напр. Synology. Без Google/OAuth: окремий користувач NAS
з доступом лише до однієї папки. Відправка — у фоні, щоб не гальмувати сторінки; статус — у файлі."""
import json
import os
import shutil
import threading
from datetime import datetime

from db import DATA_DIR, MAIN_SITE, SITES, settings, using_site

STATUS_FILE = os.path.join(DATA_DIR, "nas_status.json")
_lock = threading.Lock()
KEYS = ("nas_enabled", "nas_server", "nas_share", "nas_path", "nas_user", "nas_password", "nas_keep")


def config():
    """Налаштування NAS зберігаються в «Базі» (спільні для всіх об'єктів)."""
    with using_site(MAIN_SITE):
        s = settings()
    return {"enabled": s.get("nas_enabled") == "1", "server": (s.get("nas_server") or "").strip(),
            "share": (s.get("nas_share") or "").strip().strip("\\/"), "path": (s.get("nas_path") or "").strip().strip("\\/"),
            "user": (s.get("nas_user") or "").strip(), "password": s.get("nas_password") or "",
            "keep": max(1, int(s.get("nas_keep") or 30))}


def ready(c):
    return c["enabled"] and c["server"] and c["share"] and c["user"]


def _base(c):
    p = "\\\\" + c["server"] + "\\" + c["share"]
    return p + ("\\" + c["path"].replace("/", "\\") if c["path"] else "")


def _dir(c, site):
    return _base(c) + "\\" + site


def _session(c):
    import smbclient
    smbclient.register_session(c["server"], username=c["user"], password=c["password"], connection_timeout=20)
    return smbclient


def _close(c):
    try:
        import smbclient
        smbclient.delete_session(c["server"])
    except Exception:
        pass


def read_status():
    try:
        with open(STATUS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_status(**kw):
    st = read_status()
    st.update(kw)
    try:
        with open(STATUS_FILE + ".part", "w") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(STATUS_FILE + ".part", STATUS_FILE)
    except OSError:
        pass


def _ensure_dir(smb, path):
    """Створити папку, лише якщо її ще немає. Synology на спробу створити наявну папку в місці, де користувачу
    не можна створювати (корінь спільної папки, права лише на підпапку), відповідає ACCESS_DENIED — тому спершу stat."""
    try:
        smb.stat(path)
        return
    except OSError:
        pass
    try:                                              # Synology може не віддавати атрибути, але дозволяти список
        smb.listdir(path)
        return
    except OSError:
        pass
    parent = path.rsplit("\\", 1)[0]
    if parent.count("\\") > 3:                       # \\server\share — корінь, далі не піднімаємось
        _ensure_dir(smb, parent)
    smb.mkdir(path)


def _hint(e):
    s = str(e)
    if "ACCESS_DENIED" in s or "0xc0000022" in s:
        return s + " — немає права на запис у цю папку: на Synology дайте користувачу «Читання/запис» саме на неї"
    if "LOGON_FAILURE" in s:
        return s + " — невірний користувач або пароль"
    if "BAD_NETWORK_NAME" in s:
        return s + " — немає такої спільної папки (перевірте назву)"
    return s


def _prune(smb, d, keep, name_re):
    files = sorted(f for f in smb.listdir(d) if name_re.match(f))
    for f in files[:-keep] if len(files) > keep else []:
        smb.remove(d + "\\" + f)


def upload(c, items):
    """items: [(локальний шлях, об'єкт)]. Копіює в \\\\server\\share\\path\\<об'єкт>\\ і лишає останні N копій."""
    from backup import NAME_RE
    smb = _session(c)
    sent = []
    try:
        for local, site in items:
            d = _dir(c, site)
            _ensure_dir(smb, d)
            name = os.path.basename(local)
            tmp, final = d + "\\" + name + ".part", d + "\\" + name
            with open(local, "rb") as src, smb.open_file(tmp, mode="wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            try:
                smb.remove(final)
            except Exception:
                pass
            smb.rename(tmp, final)
            _prune(smb, d, c["keep"], NAME_RE)
            sent.append(f"{site}/{name}")
    finally:
        _close(c)
    return sent


def _audit(app, action, details):
    """Запис у журнал дій з фонового потоку."""
    if app is None:
        return
    try:
        from audit import log
        with app.app_context(), using_site(MAIN_SITE):
            log(action, details, username="система")
    except Exception:
        pass


def send_async(items, who="система"):
    """Відправити у фоні. Повертає False, якщо NAS не налаштовано."""
    c = config()
    if not ready(c) or not items:
        return False
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except Exception:
        app = None

    def run():
        with _lock:
            try:
                sent = upload(c, items)
                _write_status(ok=True, time=datetime.now().isoformat(timespec="seconds"), error="", sent=sent, who=who)
                _audit(app, "Бэкап скопирован на сетевую папку", ", ".join(sent))
            except Exception as e:
                err = f"{type(e).__name__}: {_hint(e)}"[:500]
                _write_status(ok=False, time=datetime.now().isoformat(timespec="seconds"), error=err, who=who)
                _audit(app, "Ошибка копирования бэкапа на сетевую папку", err)
    threading.Thread(target=run, daemon=True).start()
    return True


def test_connection():
    """Покрокова перевірка, щоб було видно, на якому кроці відмова. -> (ok, звіт)."""
    c = config()
    if not (c["server"] and c["share"] and c["user"]):
        return False, "Укажите сервер, общую папку и пользователя"
    steps, root, d = [], "\\\\" + c["server"] + "\\" + c["share"], _base(c)

    def step(title, fn):
        try:
            r = fn()
            steps.append(f"✓ {title}" + (f": {r}" if r else ""))
            return True
        except Exception as e:
            steps.append(f"✗ {title}: {type(e).__name__}: {_hint(e)}")
            return False
    try:
        if not step(f"вхід на {c['server']} як {c['user']}", lambda: _session(c) and None):
            return False, "\n".join(steps)
        import smbclient as smb
        step(f"список спільної папки {root}", lambda: f"{len(smb.listdir(root))} елементів")
        if c["path"]:
            def st():
                smb.stat(d)
                return "є"
            if not step(f"підпапка {d}", st) and not step(f"список {d}", lambda: f"{len(smb.listdir(d))} елементів"):
                step(f"створити {d}", lambda: smb.mkdir(d))
            step(f"список {d}", lambda: f"{len(smb.listdir(d))} елементів")
        p = d + "\\.sklad-test"

        def wr():
            with smb.open_file(p, mode="wb") as f:
                f.write(b"ok")
            smb.remove(p)
        ok = step(f"запис пробного файлу в {d}", wr)
        if not ok and c["path"]:
            steps.append("Підказка: на Synology користувачу потрібне право хоча б «Тільки читання» на саму спільну папку "
                         f"«{c['share']}» (Панель керування → Спільна папка → {c['share']} → Дозволи), а «Читання/запис» — "
                         f"на підпапку «{c['path']}». «Немає доступу» на спільній папці блокує й підпапки.")
        return ok, "\n".join(steps)
    finally:
        _close(c)


def site_keys():
    return list(SITES)
