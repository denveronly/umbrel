"""Мови інтерфейсу: українська (за замовчуванням), російська, англійська.

Вихідні рядки в шаблонах і коді — російською; переклади — у translations_uk.py і translations_en.py.
Мова зберігається в cookie `lang` (Налаштування → Загальні або кнопка в шапці). Документи (акти) — завжди українською.
"""
from flask import g, request

LANGS = {"uk": "УКР", "ru": "РУС", "en": "ENG"}
LANG_NAMES = {"uk": "Українська", "ru": "Русский", "en": "English"}
DEFAULT = "uk"

try:
    from translations_uk import UK
except Exception:  # noqa
    UK = {}
try:
    from translations_en import EN
except Exception:  # noqa
    EN = {}

MONTHS = {
    "uk": ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень",
           "листопад", "грудень"],
    "ru": ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь",
           "декабрь"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
           "December"],
}


def period_label_ui(period):
    """Місяць для інтерфейсу — мовою користувача (в актах — завжди українською, calc.period_label)."""
    try:
        y, m = period.split("-")
        lang = get_lang()
    except (ValueError, AttributeError, RuntimeError):
        return period
    return f"{MONTHS.get(lang, MONTHS['uk'])[int(m) - 1]} {y}"


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
    if lang == "en":
        return EN.get(s) or s
    return s


JS_KEYS = None


def js_i18n():
    """Словник для JS (t('...')): лише рядки, що зустрічаються у скриптах."""
    global JS_KEYS
    lang = get_lang()
    if lang == "ru":
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
    src = UK if lang == "uk" else EN
    return {k: src[k] for k in JS_KEYS if k in src}


def init_app(app):
    app.jinja_env.globals["_"] = _
    app.jinja_env.globals["js_i18n"] = js_i18n

    @app.context_processor
    def ctx():
        return {"lang": get_lang(), "LANGS": LANGS, "LANG_NAMES": LANG_NAMES}
