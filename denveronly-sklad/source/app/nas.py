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
            smb.makedirs(d, exist_ok=True)
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


def send_async(items, who="система"):
    """Відправити у фоні. Повертає False, якщо NAS не налаштовано."""
    c = config()
    if not ready(c) or not items:
        return False

    def run():
        with _lock:
            try:
                sent = upload(c, items)
                _write_status(ok=True, time=datetime.now().isoformat(timespec="seconds"), error="", sent=sent, who=who)
            except Exception as e:
                _write_status(ok=False, time=datetime.now().isoformat(timespec="seconds"),
                              error=f"{type(e).__name__}: {e}"[:400], who=who)
    threading.Thread(target=run, daemon=True).start()
    return True


def test_connection():
    """Перевірка: підключитися, створити папку, записати й видалити пробний файл. -> (ok, повідомлення)."""
    c = config()
    if not (c["server"] and c["share"] and c["user"]):
        return False, "Укажите сервер, общую папку и пользователя"
    try:
        smb = _session(c)
        d = _base(c)
        smb.makedirs(d, exist_ok=True)
        p = d + "\\.sklad-test"
        with smb.open_file(p, mode="wb") as f:
            f.write(b"ok")
        smb.remove(p)
        return True, d
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:400]
    finally:
        _close(c)


def site_keys():
    return list(SITES)
