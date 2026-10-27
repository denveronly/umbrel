import os
import sqlite3
from contextlib import contextmanager

from flask import g, has_request_context, request

DATA_DIR = os.environ.get("DATA_DIR", "/data")

# ---------------- об'єкти (База, Офіс) ----------------
# Кожен об'єкт — окрема база SQLite зі своїми фото, планом і бекапами: дані не перетинаються.
# «База» живе в корені /data (як до появи об'єктів), решта — в /data/sites/<id>/.
# Користувачі для входу — спільні, в базі «База» (MAIN_SITE).
SITES = {
    "base": {"name": "База", "icon": "warehouse", "dir": ""},
    "office": {"name": "Офис", "icon": "house", "dir": os.path.join("sites", "office")},
}
MAIN_SITE = "base"
SITE_COOKIE = "site"


def site_dir(site=None):
    return os.path.join(DATA_DIR, SITES[site or current_site()]["dir"])


def db_path(site=None):
    return os.path.join(site_dir(site), "sklad.db")


def photos_dir(site=None):
    return os.path.join(site_dir(site), "photos")


def uploads_dir(site=None):
    return os.path.join(site_dir(site), "uploads")


def backup_dir(site=None):
    return os.path.join(site_dir(site), "backups")


def current_site():
    """Об'єкт запиту. Сторінка передає свій об'єкт (поле _site у формі, заголовок X-Site у fetch) — тож
    збереження з вкладки «База» піде в «Базу», навіть якщо в іншій вкладці вже перемкнули на «Офіс».
    Інакше — cookie вибору об'єкта; за замовчуванням «База»."""
    if "site" in g:
        return g.site
    s = None
    if has_request_context():
        s = request.headers.get("X-Site")
        if s not in SITES and request.method == "POST" and request.mimetype in (
                "application/x-www-form-urlencoded", "multipart/form-data"):
            s = request.form.get("_site")
        if s not in SITES:
            s = request.cookies.get(SITE_COOKIE)
    g.site = s if s in SITES else MAIN_SITE
    return g.site


@contextmanager
def using_site(site):
    """Тимчасово працювати з іншим об'єктом (автобекап усіх об'єктів, створення об'єкта)."""
    prev_site, prev_db = g.get("site"), g.pop("db", None)
    g.site = site
    try:
        yield
    finally:
        db = g.pop("db", None)
        if db is not None:
            db.close()
        g.site = prev_site
        if prev_site is None:
            g.pop("site", None)
        if prev_db is not None:
            g.db = prev_db


# сумісність зі старим кодом: шляхи «Бази»
DB_PATH = db_path(MAIN_SITE)
BACKUP_DIR = backup_dir(MAIN_SITE)
PHOTOS_DIR = photos_dir(MAIN_SITE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- компании-получатели денег (арендодатели)
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    edrpou TEXT,
    iban TEXT,
    bank TEXT,
    address TEXT,
    signer TEXT,
    signer_position TEXT,
    basis TEXT,
    city TEXT,
    vat_payer INTEGER NOT NULL DEFAULT 0,
    act_prefix TEXT,
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

-- контакты = арендаторы / договоры
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    edrpou TEXT,
    iban TEXT,
    address TEXT,
    party_type TEXT NOT NULL DEFAULT 'company',  -- company (юрособа, діє директор) | person (фізособа)
    passport_series TEXT,
    passport_number TEXT,
    passport_issued TEXT,                        -- ким і коли виданий
    contact TEXT,                                -- ПІБ директора / підписанта в акті
    director_position TEXT,                      -- посада підписанта (Директор)
    basis TEXT,                                  -- діє на підставі (Статуту / виписки з ЄДР)
    phone TEXT,
    payment_type TEXT NOT NULL DEFAULT 'bank',   -- bank | bank_vat | cash
    contract_no TEXT,
    contract_date TEXT,
    contract_end TEXT,
    company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    deposit_amount REAL,                         -- перший внесок за останній місяць
    deposit_date TEXT,
    deposit_note TEXT,
    moved_out TEXT,                              -- дата виїзду (коли зроблено неактивним)
    activity TEXT,                               -- вид діяльності (виробництво меблів, продаж морозива…)
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tenant_contacts (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT,
    position TEXT,
    phone TEXT,
    email TEXT
);

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    area REAL NOT NULL DEFAULT 0,
    rate_bank REAL NOT NULL DEFAULT 0,           -- грн/м² без ПДВ
    rate_vat REAL,                               -- грн/м² з ПДВ (NULL = без ПДВ × (1+ПДВ))
    rate_cash REAL,                              -- грн/м² готівка (NULL = з коефіцієнта)
    price_m2 REAL,                               -- ЄДИНА ціна за м²/міс у формі оплати орендаря (для «з ПДВ» — з ПДВ)
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
    category_id INTEGER REFERENCES warehouse_categories(id) ON DELETE SET NULL,
    adjacent_area REAL,                          -- прилегла територія, м² (довідково, в оренду не рахується)
    row_no TEXT,                                 -- № у таблиці орендарів, вписаний вручну (1, 2.1, 2.2…); сортування
    polygon TEXT,
    color TEXT,
    description TEXT,                            -- опис об'єкта (довільний текст)
    note TEXT
);

-- підкатегорії складів: Ангари, Навіси… (групи в таблиці орендарів)
CREATE TABLE IF NOT EXISTS warehouse_categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 100
);

-- експлуатаційні послуги
--   meter — за лічильником (електрика, вода): споживання × ціна місяця
--   qty   — кількість вносимо щомісяця (вивіз сміття: 3 рази × ціна)
--   area  — за площею складів орендаря × ціна
--   fixed — фіксована сума на місяць
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'послуга',
    mode TEXT NOT NULL DEFAULT 'fixed',
    sort INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1
);

-- загальний лічильник послуги «частка» (напр. вода): витрата × ціна = сума, що ділиться між орендарями за %
CREATE TABLE IF NOT EXISTS service_readings (
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    value REAL,
    PRIMARY KEY (service_id, period)
);

-- частка орендаря в послузі «частка», %; діє з місяця from_period і далі, доки не змінять
CREATE TABLE IF NOT EXISTS tenant_shares (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    pct REAL NOT NULL,
    from_period TEXT NOT NULL,
    username TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    UNIQUE (tenant_id, service_id, from_period)
);

-- YASNO: рахунки постачальника електроенергії з кабінету (лише «База»)
CREATE TABLE IF NOT EXISTS yasno_bills (
    number TEXT PRIMARY KEY,
    planned INTEGER NOT NULL DEFAULT 0,
    period TEXT,                 -- YYYY-MM з колонки «Дата формування» («Жовтень 2026»)
    received TEXT,               -- ISO дата
    due TEXT,
    kwh REAL, amount REAL, paid REAL, to_pay REAL,
    pdf TEXT,                    -- ім'я файлу в data/yasno/
    pdf_url TEXT,
    first_seen TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS yasno_warnings (
    number TEXT PRIMARY KEY,
    contract TEXT, warn_date TEXT, recv_date TEXT, off_date TEXT, reason TEXT,
    resolved INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT DEFAULT (datetime('now', 'localtime'))
);
-- що вже надіслано в Telegram (щоб не дублювати)
CREATE TABLE IF NOT EXISTS yasno_notified (
    key TEXT PRIMARY KEY,
    at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- ціна послуги за конкретний місяць (без ПДВ)
CREATE TABLE IF NOT EXISTS service_prices (
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    price REAL NOT NULL,
    PRIMARY KEY (service_id, period)
);

-- які послуги підключені орендарю (для qty / area / fixed)
CREATE TABLE IF NOT EXISTS tenant_services (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    PRIMARY KEY (tenant_id, service_id)
);

-- кількість за місяць для послуг qty
CREATE TABLE IF NOT EXISTS service_usage (
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    service_id INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    qty REAL NOT NULL,
    PRIMARY KEY (tenant_id, service_id, period)
);

CREATE TABLE IF NOT EXISTS meters (
    id INTEGER PRIMARY KEY,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    service_id INTEGER REFERENCES services(id) ON DELETE CASCADE,
    resource TEXT,                               -- застаріле, лишилось для міграції
    serial TEXT,
    coef REAL NOT NULL DEFAULT 1,
    initial_value REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

-- дата фактичного зйому показників за місяць (одна на всю базу), для таблиці показників
CREATE TABLE IF NOT EXISTS reading_dates (
    period TEXT PRIMARY KEY,
    taken TEXT
);

-- сонячна генерація лінії за місяць: одразу вироблені кВт·год (не накопичувальний показник)
CREATE TABLE IF NOT EXISTS line_solar (
    line_id INTEGER NOT NULL REFERENCES power_lines(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    kwh REAL,
    PRIMARY KEY (line_id, period)
);

CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    meter_id INTEGER NOT NULL REFERENCES meters(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    value REAL NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(meter_id, period)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    active INTEGER NOT NULL DEFAULT 1,
    last_login TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS acts (
    id INTEGER PRIMARY KEY,
    number TEXT NOT NULL,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    period TEXT NOT NULL,
    payment_type TEXT NOT NULL,
    company_id INTEGER,
    data TEXT NOT NULL,
    total REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(tenant_id, period)
);

-- фото об'єктів (складів)
CREATE TABLE IF NOT EXISTS warehouse_photos (
    id INTEGER PRIMARY KEY,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,                      -- photos/<wid>/<uuid>.jpg
    caption TEXT,
    uploaded_by TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- лінії електроживлення бази (напр. «Лінія 1 (стара)», «Лінія 2 (нова)»)
CREATE TABLE IF NOT EXISTS power_lines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    note TEXT,
    sort INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1
);

-- лічильники лінії, що не належать складам: ввідний, сонячна генерація, власні споживачі (охорона, котельня)
CREATE TABLE IF NOT EXISTS line_meters (
    id INTEGER PRIMARY KEY,
    line_id INTEGER NOT NULL REFERENCES power_lines(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'own',            -- input | solar | own
    serial TEXT,
    coef REAL NOT NULL DEFAULT 1,
    initial_value REAL NOT NULL DEFAULT 0,
    initial_reactive REAL NOT NULL DEFAULT 0,
    has_reactive INTEGER NOT NULL DEFAULT 0,
    sort INTEGER NOT NULL DEFAULT 100,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS line_readings (
    id INTEGER PRIMARY KEY,
    line_meter_id INTEGER NOT NULL REFERENCES line_meters(id) ON DELETE CASCADE,
    period TEXT NOT NULL,
    value REAL,
    reactive REAL,
    UNIQUE(line_meter_id, period)
);

-- історія зміни ціни складу
-- документи й фото орендаря (PDF договорів, сканів, фото); файли — у photos/docs/<tenant_id>/
CREATE TABLE IF NOT EXISTS tenant_files (
    id INTEGER PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,                      -- docs/<tid>/<uuid>.pdf|.jpg
    orig_name TEXT,
    kind TEXT NOT NULL DEFAULT 'pdf',            -- pdf | image
    size INTEGER,
    title TEXT,
    uploaded_by TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- історія оренди складу: хто орендував, за яким договором, ціна і період (пишеться тригером при зміні орендаря)
CREATE TABLE IF NOT EXISTS warehouse_tenancy (
    id INTEGER PRIMARY KEY,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    tenant_id INTEGER,                           -- без FK: історія лишається, навіть якщо орендаря видалено
    tenant_name TEXT,
    contract_no TEXT,
    contract_date TEXT,
    payment_type TEXT,
    price_m2 REAL,
    area REAL,
    rent REAL,                                   -- сума оренди за місяць
    started TEXT,                                -- YYYY-MM-DD
    ended TEXT,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id) ON DELETE CASCADE,
    old_price REAL,
    new_price REAL,
    tenant_name TEXT,
    payment_type TEXT,
    kind TEXT NOT NULL DEFAULT 'set',            -- set (встановлення/виправлення) | increase (підвищення)
    pct REAL,                                    -- зміна у %
    area REAL,                                   -- площа на момент зміни (для суми за місяць)
    username TEXT,
    changed_at TEXT DEFAULT (datetime('now', 'localtime'))
);

-- курс USD (НБУ)
CREATE TABLE IF NOT EXISTS fx_rates (
    date TEXT PRIMARY KEY,                       -- YYYY-MM-DD
    usd REAL NOT NULL
);

-- журнал дій
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    username TEXT,
    ip TEXT,
    action TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
"""

DEFAULT_SETTINGS = {
    "solar_loss_pct": "5",                       # втрати сонячної генерації в балансі лінії, %
    "vat_rate": "20",
    "cash_coef": "1.0",
    "include_rent": "1",
    "util_shift": "1",          # 1 = в акті комунальні за попередній місяць (акт наперед)
    "act_date": "prev_last",    # prev_last | first | last — останній день попереднього місяця / 1-ше / останнє число
    "auto_backup": "1",
    "backup_keep": "30",
}

DEFAULT_SERVICES = [
    ("Електроенергія", "кВт·год", "meter", 10),
    ("Водопостачання та водовідведення", "м³", "meter", 20),
    ("Вивіз сміття", "вивіз", "qty", 30),
]

# (назва, порядок, початок назви складу для автоматичного розкладання)
DEFAULT_CATEGORIES = [
    ("Ангари", 10, ("ангар",)),
    ("Навіси", 20, ("навіс", "навес")),
]

SERVICE_MODES = {
    "meter": "по счётчику",
    "qty": "кол-во за месяц (вносим вручную)",
    "area": "за м² площади",
    "fixed": "фиксированно в месяц",
    "share": "доля от общей суммы (% в карточке арендатора)",
    "amount": "сумма вручную каждому арендатору за месяц",
}

PAYMENT_TYPES = {
    "bank": "Безготівка без ПДВ",
    "bank_vat": "Безготівка з ПДВ",
    "cash": "Готівка",
}


def _connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def get_db():
    """База даних поточного об'єкта."""
    if "db" not in g:
        g.db = _connect(db_path())
    return g.db


def get_main_db():
    """База «Бази» — там спільні користувачі (вхід однаковий для всіх об'єктів)."""
    if current_site() == MAIN_SITE:
        return get_db()
    if "main_db" not in g:
        g.main_db = _connect(db_path(MAIN_SITE))
    return g.main_db


def close_db(_e=None):
    for k in ("db", "main_db"):
        db = g.pop(k, None)
        if db is not None:
            db.close()


def to_iso(s):
    """'01.03.2024' / '1.3.24' / '2024-03-01' -> '2024-03-01'; нерозпізнане -> None."""
    import re
    from datetime import date as _d
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s[:10]) if len(s) >= 10 else None
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{2}|\d{4})", s)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        y += 2000 if y < 100 else 0
    try:
        return _d(y, mo, d).isoformat()
    except ValueError:
        return None


TENANCY_TRIGGERS = """
DROP TRIGGER IF EXISTS wh_tenant_change;
CREATE TRIGGER IF NOT EXISTS wh_tenant_change AFTER UPDATE OF tenant_id ON warehouses
WHEN OLD.tenant_id IS NOT NEW.tenant_id
BEGIN
    INSERT INTO warehouse_tenancy(warehouse_id, tenant_id, tenant_name, contract_no, contract_date, payment_type,
                                  price_m2, area, rent, started, ended)
    SELECT OLD.id, OLD.tenant_id, t.name, t.contract_no, t.contract_date, t.payment_type, OLD.price_m2, OLD.area,
           ROUND(COALESCE(OLD.price_m2, 0) * COALESCE(OLD.area, 0), 2),
           COALESCE(OLD.tenant_since, CASE WHEN t.contract_date GLOB '[0-9][0-9].[0-9][0-9].[0-9][0-9][0-9][0-9]'
               THEN substr(t.contract_date, 7, 4) || '-' || substr(t.contract_date, 4, 2) || '-' || substr(t.contract_date, 1, 2)
               ELSE t.contract_date END), date('now', 'localtime')
    FROM (SELECT 1) LEFT JOIN tenants t ON t.id = OLD.tenant_id
    WHERE OLD.tenant_id IS NOT NULL;
    UPDATE warehouses SET tenant_since = CASE WHEN NEW.tenant_id IS NULL THEN NULL ELSE date('now', 'localtime') END
    WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS wh_tenant_insert AFTER INSERT ON warehouses
WHEN NEW.tenant_id IS NOT NULL AND NEW.tenant_since IS NULL
BEGIN
    UPDATE warehouses SET tenant_since = date('now', 'localtime') WHERE id = NEW.id;
END;
"""

# колонки, які з'явились після першої версії
MIGRATIONS = {
    "services": {"has_main": "INTEGER NOT NULL DEFAULT 0", "main_serial": "TEXT", "main_coef": "REAL NOT NULL DEFAULT 1",
                 "main_initial": "REAL NOT NULL DEFAULT 0",
                 "main_src": "TEXT"},   # загальний лічильник для послуги «частка»; main_src — лічильник лінії "l:<id>" / "m:<id>"   # загальний лічильник для послуги «частка»
    "line_meters": {"src": "TEXT"},   # ввідний = наявний лічильник іншої лінії: "m:<id>" | "l:<id>"
    "power_lines": {"has_solar": "INTEGER NOT NULL DEFAULT 1",
                    "parent_id": "INTEGER", "legal_name": "TEXT"},   # legal_name — назва в актах (порожньо = як у програмі)   # сублінія: живиться від іншої лінії, її ввід — споживач на тій лінії   # сонячна генерація на лінії (для субліній — вимкнути)
    "users": {"perms": "TEXT NOT NULL DEFAULT 'economics'", "pin_hash": "TEXT"},   # pin_hash — PIN адміністратора для видалення   # права оператора (через кому); раніше Економіку бачили всі
    "warehouses": {"rate_vat": "REAL", "price_m2": "REAL", "category_id": "INTEGER", "adjacent_area": "REAL",
                   "row_no": "TEXT", "description": "TEXT", "tenant_since": "TEXT"},
    "tenants": {"rent_merge_wid": "INTEGER", "rent_merge_name": "TEXT", "bank": "TEXT", "mfo": "TEXT", "ipn": "TEXT",   # оренда всіх приміщень одним рядком під назвою цього складу
                "contract_end": "TEXT", "company_id": "INTEGER", "deposit_amount": "REAL",
                "deposit_date": "TEXT", "deposit_note": "TEXT", "director_position": "TEXT", "basis": "TEXT",
                "party_type": "TEXT NOT NULL DEFAULT 'company'", "passport_series": "TEXT", "passport_number": "TEXT",
                "passport_issued": "TEXT", "moved_out": "TEXT", "activity": "TEXT",
                "parent_id": "INTEGER", "legal_name": "TEXT"},   # legal_name — назва в актах (порожньо = як у програмі)   # parent_id — додатковий договір того ж орендаря (реквізити з основної картки)
    "price_history": {"kind": "TEXT NOT NULL DEFAULT 'set'", "pct": "REAL", "area": "REAL"},
    "readings": {"reactive": "REAL"},
    "companies": {"signer_position": "TEXT", "basis": "TEXT", "mfo": "TEXT", "ipn": "TEXT",
                  "pay_types": "TEXT", "email": "TEXT",
                  "contacts": "TEXT", "legal_name": "TEXT"},   # legal_name — повна назва в актах; name — скорочена (у програмі)   # contacts — JSON [{role, name, phone, email}] (бухгалтерія, адміністрація…)   # які форми оплати приймає компанія: "cash,bank,bank_vat"; порожньо — будь-які
    "acts": {"company_id": "INTEGER"},
    "meters": {"name": "TEXT", "sort_order": "INTEGER", "service_id": "INTEGER", "line_id": "INTEGER", "has_reactive": "INTEGER NOT NULL DEFAULT 0",
               "initial_reactive": "REAL NOT NULL DEFAULT 0"},
}
OLD_RESOURCES = {"electricity": "Електроенергія", "water": "Водопостачання та водовідведення",
                 "heating": "Теплопостачання"}
OLD_UNITS = {"electricity": "кВт·год", "water": "м³", "heating": "Гкал"}


def _migrate(con, site=MAIN_SITE):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # 1.38: акти датуються останнім днем попереднього місяця (одноразово переводимо зі старого «1-ше число»)
    if "settings" in tables and not con.execute("SELECT 1 FROM settings WHERE key='act_date_v2'").fetchone():
        con.execute("UPDATE settings SET value='prev_last' WHERE key='act_date' AND value='first'")
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('act_date_v2', '1')")
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    # історія оренди: тригери на зміну орендаря складу (ловлять усі шляхи — картка складу, орендаря, виїзд, ШІ)
    con.executescript(TENANCY_TRIGGERS)
    for wid, tsince, cdate in con.execute("SELECT w.id, w.tenant_since, t.contract_date FROM warehouses w "
                                          "JOIN tenants t ON t.id=w.tenant_id").fetchall():
        iso = to_iso(tsince) if tsince else to_iso(cdate)   # дата договору вписується вручну (01.03.2024)
        if iso and iso != tsince:
            con.execute("UPDATE warehouses SET tenant_since=? WHERE id=?", (iso, wid))
    for hid, st in con.execute("SELECT id, started FROM warehouse_tenancy WHERE started IS NOT NULL").fetchall():
        iso = to_iso(st)
        if iso and iso != st:
            con.execute("UPDATE warehouse_tenancy SET started=? WHERE id=?", (iso, hid))

    # 1.24: реактив вводу лінії — тепер кількість за місяць, а не накопичувальний показник: перерахувати один раз
    if not con.execute("SELECT 1 FROM settings WHERE key='reactive_direct'").fetchone():
        for lm_id, init_r in con.execute("SELECT id, initial_reactive FROM line_meters").fetchall():
            prev = init_r or 0
            for rid, rv in con.execute("SELECT id, reactive FROM line_readings WHERE line_meter_id=? AND reactive IS NOT NULL "
                                       "ORDER BY period", (lm_id,)).fetchall():
                con.execute("UPDATE line_readings SET reactive=? WHERE id=?", (round(rv - prev, 3), rid))
                prev = rv
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('reactive_direct', '1')")

    # реквізити з налаштувань першої версії -> компанія
    st = dict(con.execute("SELECT key, value FROM settings").fetchall())
    if st.get("company_name") and not con.execute("SELECT 1 FROM companies").fetchone() \
            and st["company_name"] != "ФОП / ТОВ Ваша назва":
        cid = con.execute(
            "INSERT INTO companies(name, edrpou, iban, bank, address, signer, city, vat_payer, act_prefix) "
            "VALUES (?,?,?,?,?,?,?,1,?)",
            (st["company_name"], st.get("company_edrpou"), st.get("company_iban"), st.get("company_bank"),
             st.get("company_address"), st.get("company_signer"), st.get("act_city"), st.get("act_prefix"))).lastrowid
        con.execute("UPDATE tenants SET company_id=? WHERE company_id IS NULL", (cid,))

    # старі контакт/телефон -> список контактів
    for t in con.execute("SELECT id, contact, phone FROM tenants WHERE (COALESCE(contact,'')<>'' OR "
                         "COALESCE(phone,'')<>'') AND id NOT IN (SELECT tenant_id FROM tenant_contacts)").fetchall():
        con.execute("INSERT INTO tenant_contacts(tenant_id, name, phone) VALUES (?,?,?)", tuple(t))

    # одна ціна складу (з версії 1.1): беремо ставку, що відповідає формі оплати орендаря
    st = dict(con.execute("SELECT key, value FROM settings").fetchall())
    vat = float(st.get("vat_rate") or 20) / 100
    coef = float(st.get("cash_coef") or 1)
    for w in con.execute("SELECT w.id, w.rate_bank, w.rate_vat, w.rate_cash, t.payment_type FROM warehouses w "
                         "LEFT JOIN tenants t ON t.id=w.tenant_id WHERE w.price_m2 IS NULL").fetchall():
        wid, rb, rv, rc, pt = w
        rb = rb or 0
        price = (rv if rv is not None else rb * (1 + vat)) if pt == "bank_vat" else \
                (rc if rc is not None else rb * coef) if pt == "cash" else rb
        con.execute("UPDATE warehouses SET price_m2=? WHERE id=?", (round(price, 4), wid))

    # підкатегорії складів (з 1.5.0): один раз створюємо Ангари й Навіси і розкладаємо склади за назвою
    if not con.execute("SELECT 1 FROM settings WHERE key='categories_seeded'").fetchone() and site == MAIN_SITE:
        if not con.execute("SELECT 1 FROM warehouse_categories").fetchone():
            for name, sort, prefixes in DEFAULT_CATEGORIES:
                cid = con.execute("INSERT INTO warehouse_categories(name, sort) VALUES (?,?)", (name, sort)).lastrowid
                for wid, wname in con.execute("SELECT id, name FROM warehouses WHERE category_id IS NULL").fetchall():
                    if (wname or "").strip().lower().startswith(prefixes):
                        con.execute("UPDATE warehouses SET category_id=? WHERE id=?", (cid, wid))
        con.execute("INSERT INTO settings(key, value) VALUES ('categories_seeded', '1')")

    # послуги за замовчуванням (тільки для нової бази)
    if not con.execute("SELECT 1 FROM services").fetchone():
        for name, unit, mode, sort in DEFAULT_SERVICES:
            con.execute("INSERT INTO services(name, unit, mode, sort) VALUES (?,?,?,?)", (name, unit, mode, sort))

    # лічильники першої версії (resource) -> послуги
    for res in [r[0] for r in con.execute("SELECT DISTINCT resource FROM meters WHERE service_id IS NULL "
                                          "AND resource IS NOT NULL")]:
        name = OLD_RESOURCES.get(res, res)
        row = con.execute("SELECT id FROM services WHERE name=? AND mode='meter'", (name,)).fetchone()
        sid = row[0] if row else con.execute(
            "INSERT INTO services(name, unit, mode, sort) VALUES (?,?, 'meter', 50)",
            (name, OLD_UNITS.get(res, "од."))).lastrowid
        con.execute("UPDATE meters SET service_id=? WHERE resource=? AND service_id IS NULL", (sid, res))
        # старі тарифи -> ціни по місяцях (з дати початку до поточного місяця)
        if "tariffs" in tables:
            for price, frm in con.execute("SELECT price, valid_from FROM tariffs WHERE resource=? ORDER BY valid_from",
                                          (res,)).fetchall():
                con.execute("INSERT OR REPLACE INTO service_prices(service_id, period, price) VALUES (?,?,?)",
                            (sid, frm, price))


# що копіюється в новий об'єкт з «Бази» при першому запуску: компанії-отримувачі та загальні налаштування
SEED_SKIP_SETTINGS = {"plan_image", "plan_w", "plan_h", "last_auto_backup", "fx_last_try", "fx_last_error",
                      "categories_seeded", "nas_enabled", "nas_server", "nas_share", "nas_path", "nas_user", "nas_password",
                      "nas_keep"}


def init_db(site=None):
    """Створити/оновити бази. Без аргументу — всі об'єкти («База» першою: з неї засіваються інші)."""
    for s in ([site] if site else [MAIN_SITE] + [k for k in SITES if k != MAIN_SITE]):
        _init_site(s)


def _init_site(site):
    for d in (site_dir(site), uploads_dir(site), backup_dir(site), photos_dir(site)):
        os.makedirs(d, exist_ok=True)
    path = db_path(site)
    new = not os.path.exists(path)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    if new and site != MAIN_SITE:
        _seed_from_main(con)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    _migrate(con, site)
    con.commit()
    con.close()


def _seed_from_main(con):
    """Новий об'єкт: ті самі компанії-отримувачі (з тими ж id) і налаштування, що в «Базі»; підкатегорій немає."""
    main = sqlite3.connect(db_path(MAIN_SITE))
    try:
        cols = [r[1] for r in main.execute("PRAGMA table_info(companies)")]
        have = {r[1] for r in con.execute("PRAGMA table_info(companies)")}
        cols = [c for c in cols if c in have]
        for row in main.execute(f"SELECT {', '.join(cols)} FROM companies"):
            con.execute(f"INSERT OR IGNORE INTO companies({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", row)
        for k, v in main.execute("SELECT key, value FROM settings"):
            if k not in SEED_SKIP_SETTINGS and not k.startswith(("yasno_", "tg_")):
                con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (k, v))
    finally:
        main.close()
    con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('categories_seeded', '1')")


def settings():
    rows = get_db().execute("SELECT key, value FROM settings").fetchall()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in rows})
    return s


def set_setting(key, value):
    get_db().execute("INSERT INTO settings(key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def companies(active_only=False):
    q = "SELECT * FROM companies" + (" WHERE active=1" if active_only else "") + " ORDER BY name"
    return get_db().execute(q).fetchall()


def categories():
    return get_db().execute("SELECT c.*, (SELECT COUNT(*) FROM warehouses w WHERE w.category_id=c.id) AS n "
                            "FROM warehouse_categories c ORDER BY c.sort, c.name").fetchall()


def services(active_only=True, mode=None):
    q, args = "SELECT * FROM services WHERE 1=1", []
    if active_only:
        q += " AND active=1"
    if mode:
        q += " AND mode=?"
        args.append(mode)
    return get_db().execute(q + " ORDER BY sort, name", args).fetchall()


def price_for(service_id, period):
    """(ціна, період ціни). Якщо на місяць ціни немає — остання попередня."""
    row = get_db().execute(
        "SELECT price, period FROM service_prices WHERE service_id=? AND period<=? ORDER BY period DESC LIMIT 1",
        (service_id, period)).fetchone()
    return (row["price"], row["period"]) if row else (None, None)
