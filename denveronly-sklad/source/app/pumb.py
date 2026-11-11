"""Розбір сторінок ПУМБ Бізнес після входу: рахунки із залишками та операції за місяць.

Чернетка: структура сторінок кабінету ще не знята. read() зберігає сторінку після входу для діагностики
й поки що повертає лише рахунки, знайдені за IBAN, без операцій.
"""
import re

IBAN = re.compile(r"UA\d{27}")


def read(page, pages):
    html = page.content()
    pages["03_home"] = html
    accounts = [{"iban": i, "name": "", "currency": "UAH", "balance": None}
                for i in dict.fromkeys(IBAN.findall(html))]
    return {"accounts": accounts, "ops": []}
