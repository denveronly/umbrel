"""Web UI around withings-sync: connect Withings + Garmin once, then sync on a schedule.

withings-sync asks for the Withings code and the Garmin MFA code on a terminal,
which an Umbrel app does not have. This app does both logins from the browser and
writes the token files where withings-sync expects them (its --config-folder),
so the scheduled withings-sync runs never need to prompt.
"""
import datetime
import importlib.resources
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

import requests
from flask import Flask, flash, redirect, render_template, request, url_for
from garminconnect import Garmin

DATA = os.environ.get("DATA_DIR", "/data")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
STATE_FILE = os.path.join(DATA, "state.json")
SECRET_FILE = os.path.join(DATA, ".flask_secret")
LOG_FILE = os.path.join(DATA, "sync.log")
# Paths withings-sync uses with --config-folder DATA
WITHINGS_USER = os.path.join(DATA, ".withings_user.json")
WITHINGS_APP_OVERRIDE = os.path.join(DATA, "withings_app.json")
GARMIN_TOKENS = os.path.join(DATA, ".garmin_session.json")

WITHINGS_AUTHORIZE = "https://account.withings.com/oauth2_user/authorize2"
WITHINGS_TOKEN = "https://wbsapi.withings.net/v2/oauth2"
LOG_MAX_BYTES = 300_000
MIN_INTERVAL = 15

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("withings-sync-ui")

os.makedirs(DATA, exist_ok=True)
app = Flask(__name__)


def _secret_key():
    try:
        with open(SECRET_FILE, "rb") as f:
            return f.read()
    except FileNotFoundError:
        key = os.urandom(32)
        with open(SECRET_FILE, "wb") as f:
            f.write(key)
        os.chmod(SECRET_FILE, 0o600)
        return key


app.secret_key = _secret_key()

# ---------------------------------------------------------------- storage


def _read_json(path, default):
    try:
        with open(path, encoding="utf8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def _write_json(path, data, private=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


DEFAULT_SETTINGS = {
    "garmin_username": "",
    "garmin_password": "",
    "interval_min": 60,
    "enabled": True,
    "blood_pressure": False,
    "withings_client_id": "",
    "withings_secret": "",
    "withings_callback": "",
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(_read_json(SETTINGS_FILE, {}))
    return s


def save_settings(s):
    _write_json(SETTINGS_FILE, s, private=True)


def load_state():
    return _read_json(STATE_FILE, {})


def save_state(st):
    _write_json(STATE_FILE, st)


def withings_app_config():
    """Own Withings developer app if configured, else the one bundled with withings-sync."""
    s = load_settings()
    if s["withings_client_id"] and s["withings_secret"] and s["withings_callback"]:
        return {
            "client_id": s["withings_client_id"],
            "consumer_secret": s["withings_secret"],
            "callback_url": s["withings_callback"],
        }
    path = importlib.resources.files("withings_sync") / "config/withings_app.json"
    return json.loads(path.read_text(encoding="utf8"))


def sync_withings_app_override():
    """withings-sync reads <config-folder>/withings_app.json when present."""
    s = load_settings()
    if s["withings_client_id"] and s["withings_secret"] and s["withings_callback"]:
        _write_json(WITHINGS_APP_OVERRIDE, withings_app_config(), private=True)
    elif os.path.exists(WITHINGS_APP_OVERRIDE):
        os.remove(WITHINGS_APP_OVERRIDE)


def withings_connected():
    return bool(_read_json(WITHINGS_USER, {}).get("refresh_token"))


def garmin_connected():
    return os.path.exists(GARMIN_TOKENS) and bool(load_settings()["garmin_username"])


def append_log(text):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf8") as f:
        f.write(f"===== {stamp} =====\n{text.rstrip()}\n")
    if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
        with open(LOG_FILE, encoding="utf8", errors="replace") as f:
            tail = f.read()[-LOG_MAX_BYTES // 2:]
        with open(LOG_FILE, "w", encoding="utf8") as f:
            f.write(tail)


def read_log(max_chars=20_000):
    try:
        with open(LOG_FILE, encoding="utf8", errors="replace") as f:
            return f.read()[-max_chars:]
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------- sync

sync_lock = threading.Lock()


def run_sync(fromdate=None, reason="scheduled"):
    """Run withings-sync once. Returns False if a run is already in progress."""
    if not sync_lock.acquire(blocking=False):
        return False
    try:
        s = load_settings()
        sync_withings_app_override()
        exe = shutil.which("withings-sync") or os.path.join(
            os.path.dirname(sys.executable), "withings-sync"
        )
        cmd = [exe, "--config-folder", DATA]
        if fromdate:
            cmd += ["--fromdate", fromdate]
        if s["blood_pressure"]:
            cmd += ["--features", "BLOOD_PRESSURE"]
        env = dict(os.environ)
        env.update(
            HOME=DATA,
            GARMIN_USERNAME=s["garmin_username"],
            GARMIN_PASSWORD=s["garmin_password"],
        )
        started = time.time()
        try:
            proc = subprocess.run(
                cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, timeout=900,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            ok = proc.returncode == 0 and "Traceback" not in output
        except subprocess.TimeoutExpired as ex:
            output = f"Timed out after {ex.timeout}s"
            ok = False
        except OSError as ex:
            output = f"Could not start withings-sync: {ex}"
            ok = False
        header = f"[{reason}{' from ' + fromdate if fromdate else ''}] {'OK' if ok else 'FAILED'}"
        append_log(f"{header}\n{output}")
        last_line = next(
            (ln for ln in reversed(output.strip().splitlines()) if ln.strip()), ""
        )
        st = load_state()
        st.update(
            last_run_ts=started,
            last_ok=ok,
            last_message=last_line[-300:],
        )
        if ok:
            st["last_ok_ts"] = started
        save_state(st)
        log.info("%s", header)
        return True
    finally:
        sync_lock.release()


def scheduler():
    while True:
        try:
            s = load_settings()
            interval = max(MIN_INTERVAL, int(s.get("interval_min") or 60)) * 60
            last = load_state().get("last_run_ts", 0)
            if (
                s["enabled"]
                and withings_connected()
                and garmin_connected()
                and time.time() - last >= interval
            ):
                run_sync()
        except Exception:  # keep the scheduler alive no matter what
            log.exception("Scheduler error")
        time.sleep(30)


threading.Thread(target=scheduler, daemon=True, name="scheduler").start()

# ---------------------------------------------------------------- Garmin login

# A Garmin login waiting for its MFA code (state lives on the Garmin instance).
pending_mfa = {"client": None, "username": "", "password": "", "remember": True, "ts": 0}


def _finish_garmin_login(client, username, password, remember):
    if os.path.exists(GARMIN_TOKENS):
        os.remove(GARMIN_TOKENS)
    client.client.dump(GARMIN_TOKENS)
    s = load_settings()
    s["garmin_username"] = username
    s["garmin_password"] = password if remember else ""
    save_settings(s)


# ---------------------------------------------------------------- routes


def _fmt_ts(ts):
    if not ts:
        return "never"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


@app.get("/")
def index():
    s = load_settings()
    st = load_state()
    cfg = withings_app_config()
    auth_url = WITHINGS_AUTHORIZE + "?" + urlencode(
        {
            "response_type": "code",
            "client_id": cfg["client_id"],
            "state": "OK",
            "scope": "user.metrics",
            "redirect_uri": cfg["callback_url"],
        }
    )
    mfa_waiting = bool(pending_mfa["client"]) and time.time() - pending_mfa["ts"] < 600
    next_ts = None
    if s["enabled"] and withings_connected() and garmin_connected():
        interval = max(MIN_INTERVAL, int(s["interval_min"] or 60)) * 60
        next_ts = max(st.get("last_run_ts", 0) + interval, time.time())
    return render_template(
        "index.html",
        s=s,
        st=st,
        withings_ok=withings_connected(),
        garmin_ok=garmin_connected(),
        auth_url=auth_url,
        mfa_waiting=mfa_waiting,
        mfa_user=pending_mfa["username"],
        running=sync_lock.locked(),
        last_run=_fmt_ts(st.get("last_run_ts")),
        last_ok=_fmt_ts(st.get("last_ok_ts")),
        next_run=_fmt_ts(next_ts) if next_ts else "—",
        logtext=read_log(),
        today=datetime.date.today().isoformat(),
        own_app=bool(s["withings_client_id"]),
        min_interval=MIN_INTERVAL,
    )


@app.post("/withings/code")
def withings_code():
    raw = request.form.get("code", "").strip()
    # Accept either the bare code or the whole redirect URL
    if "code=" in raw:
        raw = raw.split("code=", 1)[1].split("&", 1)[0]
    if not raw:
        flash("Paste the code shown after you approve access in Withings.", "error")
        return redirect(url_for("index"))
    cfg = withings_app_config()
    try:
        resp = requests.post(
            WITHINGS_TOKEN,
            {
                "action": "requesttoken",
                "grant_type": "authorization_code",
                "client_id": cfg["client_id"],
                "client_secret": cfg["consumer_secret"],
                "code": raw,
                "redirect_uri": cfg["callback_url"],
            },
            timeout=30,
        ).json()
    except (requests.RequestException, ValueError) as ex:
        flash(f"Could not reach Withings: {ex}", "error")
        return redirect(url_for("index"))
    body = resp.get("body") or {}
    if resp.get("status") != 0 or not body.get("refresh_token"):
        flash(
            f"Withings rejected the code (status {resp.get('status')}: "
            f"{resp.get('error') or body.get('error') or 'unknown'}). "
            "Codes expire after ~30 seconds — open the link again and paste faster.",
            "error",
        )
        return redirect(url_for("index"))
    user = _read_json(WITHINGS_USER, {})
    user.update(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        userid=body.get("userid"),
    )
    user.pop("authentification_code", None)
    _write_json(WITHINGS_USER, user, private=True)
    flash("Withings connected.", "ok")
    return redirect(url_for("index"))


@app.post("/withings/disconnect")
def withings_disconnect():
    if os.path.exists(WITHINGS_USER):
        os.remove(WITHINGS_USER)
    flash("Withings disconnected.", "ok")
    return redirect(url_for("index"))


@app.post("/garmin/login")
def garmin_login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    remember = request.form.get("remember") == "on"
    if not username or not password:
        flash("Enter your Garmin Connect email and password.", "error")
        return redirect(url_for("index"))
    client = Garmin(username, password, return_on_mfa=True)
    try:
        status, _ = client.login()
    except Exception as ex:
        log.exception("Garmin login failed")
        flash(f"Garmin login failed: {ex}", "error")
        return redirect(url_for("index"))
    if status == "needs_mfa":
        pending_mfa.update(
            client=client, username=username, password=password,
            remember=remember, ts=time.time(),
        )
        flash("Garmin sent you a verification code — enter it below.", "ok")
        return redirect(url_for("index"))
    _finish_garmin_login(client, username, password, remember)
    flash("Garmin connected.", "ok")
    return redirect(url_for("index"))


@app.post("/garmin/mfa")
def garmin_mfa():
    code = request.form.get("code", "").strip()
    client = pending_mfa["client"]
    if not client:
        flash("The Garmin login expired — sign in again.", "error")
        return redirect(url_for("index"))
    try:
        client.resume_login(None, code)
    except Exception as ex:
        log.exception("Garmin MFA failed")
        flash(f"Code not accepted: {ex}", "error")
        return redirect(url_for("index"))
    _finish_garmin_login(
        client, pending_mfa["username"], pending_mfa["password"], pending_mfa["remember"]
    )
    pending_mfa.update(client=None, username="", password="", ts=0)
    flash("Garmin connected.", "ok")
    return redirect(url_for("index"))


@app.post("/garmin/disconnect")
def garmin_disconnect():
    if os.path.exists(GARMIN_TOKENS):
        os.remove(GARMIN_TOKENS)
    s = load_settings()
    s["garmin_username"] = ""
    s["garmin_password"] = ""
    save_settings(s)
    pending_mfa.update(client=None, username="", password="", ts=0)
    flash("Garmin disconnected.", "ok")
    return redirect(url_for("index"))


@app.post("/settings")
def settings():
    s = load_settings()
    try:
        s["interval_min"] = max(MIN_INTERVAL, int(request.form.get("interval_min", 60)))
    except ValueError:
        s["interval_min"] = 60
    s["enabled"] = request.form.get("enabled") == "on"
    s["blood_pressure"] = request.form.get("blood_pressure") == "on"
    for key in ("withings_client_id", "withings_secret", "withings_callback"):
        value = request.form.get(key, "").strip()
        if key == "withings_secret" and not value and s[key]:
            continue  # blank secret field keeps the saved one
        s[key] = value
    save_settings(s)
    flash("Settings saved.", "ok")
    return redirect(url_for("index"))


def _start_background(fromdate, reason):
    if sync_lock.locked():
        flash("A sync is already running.", "error")
        return
    threading.Thread(target=run_sync, args=(fromdate, reason), daemon=True).start()
    time.sleep(0.3)
    flash("Sync started — refresh in a minute to see the log.", "ok")


@app.post("/sync")
def sync_now():
    if not (withings_connected() and garmin_connected()):
        flash("Connect both Withings and Garmin first.", "error")
        return redirect(url_for("index"))
    fromdate = request.form.get("fromdate", "").strip() or None
    if fromdate:
        try:
            datetime.date.fromisoformat(fromdate)
        except ValueError:
            flash("Date must be YYYY-MM-DD.", "error")
            return redirect(url_for("index"))
    _start_background(fromdate, "manual")
    return redirect(url_for("index"))


@app.post("/log/clear")
def clear_log():
    open(LOG_FILE, "w").close()
    return redirect(url_for("index"))
