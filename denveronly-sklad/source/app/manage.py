"""Сервисные команды.

  docker exec -it sklad python manage.py users
  docker exec -it sklad python manage.py reset-password <логин>
  docker exec -it sklad python manage.py make-admin <логин>
"""
import getpass
import sqlite3
import sys

from werkzeug.security import generate_password_hash

from db import DB_PATH, init_db


def main():
    init_db()
    con = sqlite3.connect(DB_PATH)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "users":
        for r in con.execute("SELECT username, role, active, last_login FROM users ORDER BY username"):
            print(f"{r[0]:20} {r[1]:6} {'активен' if r[2] else 'отключён':9} {r[3] or ''}")
    elif cmd in ("reset-password", "make-admin") and len(sys.argv) == 3:
        name = sys.argv[2]
        if not con.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            sys.exit(f"Пользователь {name} не найден")
        if cmd == "reset-password":
            pw = getpass.getpass("Новый пароль: ")
            if len(pw) < 8 or pw != getpass.getpass("Ещё раз: "):
                sys.exit("Пароль короче 8 символов или не совпадает")
            con.execute("UPDATE users SET password_hash=?, active=1 WHERE username=?",
                        (generate_password_hash(pw), name))
        else:
            con.execute("UPDATE users SET role='admin', active=1 WHERE username=?", (name,))
        con.commit()
        print("Готово")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
