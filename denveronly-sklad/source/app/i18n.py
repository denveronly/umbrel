"""Мови інтерфейсу: українська (за замовчуванням) і російська.

Вихідні рядки в шаблонах і коді — російською; переклад українською — у translations_uk.py.
Мова зберігається в cookie `lang` і перемикається кнопкою в шапці без перезапуску.
"""
from flask import g, request

LANGS = {"uk": "УКР", "ru": "РУС"}
DEFAULT = "uk"

try:
    from translations_uk import UK
except Exception:  # noqa
    UK = {}


def get_lang():
    if "lang" not in g:
        lang = request.cookies.get("lang") if request else None
        g.lang = lang if lang in LANGS else DEFAULT
    return g.lang


def _(s):
    """Переклад рядка на поточну мову (вихідна мова рядків — російська)."""
    if not s:
        return s
    try:
        lang = get_lang()
    except RuntimeError:          # поза запитом
        lang = DEFAULT
    if lang == "uk":
        return UK.get(s, s)
    return s


JS_KEYS = None


def js_i18n():
    """Словник для JS (t('...')): лише рядки, що зустрічаються у скриптах."""
    global JS_KEYS
    if get_lang() != "uk":
        return {}
    if JS_KEYS is None:
        import glob
        import os
        import re
        base = os.path.dirname(__file__)
        keys = set()
        for fn in glob.glob(os.path.join(base, "templates", "*.html")) + glob.glob(os.path.join(base, "static", "*.js")):
            keys.update(re.findall(r"t\('([^']+)'\)", open(fn, encoding="utf-8").read()))
        JS_KEYS = sorted(keys)
    return {k: UK[k] for k in JS_KEYS if k in UK}


def init_app(app):
    app.jinja_env.globals["_"] = _
    app.jinja_env.globals["js_i18n"] = js_i18n

    @app.context_processor
    def ctx():
        return {"lang": get_lang(), "LANGS": LANGS}
