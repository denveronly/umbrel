import os
import sqlite3

from flask import g

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "sklad.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
PHOTOS_DIR = os.path.join(DATA_DIR, "photos")

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
    tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
    polygon TEXT,
    color TEXT,
    note TEXT
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
    "vat_rate": "20",
    "cash_coef": "1.0",
    "include_rent": "1",
    "util_shift": "1",          # 1 = в акті комунальні за попередній місяць (акт наперед)
    "act_date": "first",        # first | last — дата акта: 1-ше або останнє число місяця
    "auto_backup": "1",
    "backup_keep": "30",
}

DEFAULT_SERVICES = [
    ("Електроенергія", "кВт·год", "meter", 10),
    ("Водопостачання та водовідведення", "м³", "meter", 20),
    ("Вивіз сміття", "вивіз", "qty", 30),
]

SERVICE_MODES = {
    "meter": "по счётчику",
    "qty": "кол-во за месяц (вносим вручную)",
    "area": "за м² площади",
    "fixed": "фиксированно в месяц",
}

PAYMENT_TYPES = {
    "bank": "Безготівка без ПДВ",
    "bank_vat": "Безготівка з ПДВ",
    "cash": "Готівка",
}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# колонки, які з'явились після першої версії
MIGRATIONS = {
    "warehouses": {"rate_vat": "REAL"},
    "tenants": {"contract_end": "TEXT", "company_id": "INTEGER", "deposit_amount": "REAL",
                "deposit_date": "TEXT", "deposit_note": "TEXT", "director_position": "TEXT", "basis": "TEXT"},
    "companies": {"signer_position": "TEXT", "basis": "TEXT"},
    "acts": {"company_id": "INTEGER"},
    "meters": {"service_id": "INTEGER"},
}
OLD_RESOURCES = {"electricity": "Електроенергія", "water": "Водопостачання та водовідведення",
                 "heating": "Теплопостачання"}
OLD_UNITS = {"electricity": "кВт·год", "water": "м³", "heating": "Гкал"}


def _migrate(con):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

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


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    _migrate(con)
    con.commit()
    con.close()


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
