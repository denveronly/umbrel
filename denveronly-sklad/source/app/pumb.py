"""Розбір сторінок ПУМБ Бізнес (online.pumb.ua/biz) після входу.

- /biz/account/balance/ — таблиця «Банківські рахунки»: назва, IBAN, валюта, баланс, доступні кошти, AID рахунку;
- /biz/account/previewtransaction/?…&AccountID=<AID>&From=MM/DD/YYYY… — HTML-реєстр операцій за період
  (та сама виписка, що «Виписки» → PDF): рядок операції (дата, № док., дебет, кредит, банк, рахунок,
  платник/отримувач, ЄДРПОУ) + наступний рядок із призначенням платежу; вхідне/вихідне сальдо.

Дебет — списання з рахунку (мінус), кредит — надходження (плюс). Розбираємо прямо в сторінці (DOMParser),
запити йдуть зі сторінки банку — з тими самими cookie та захистом, що й у людини.
"""
from datetime import date, timedelta

JS_BALANCE = r"""
async () => {
  const d = new DOMParser().parseFromString(await (await fetch('/biz/account/balance/')).text(), 'text/html');
  const tb = [...d.querySelectorAll('table')].find(t => /Назва рахунку/.test(t.innerText) && t.querySelector('td.iban'));
  const out = [];
  if (!tb) return {accounts: out, html: d.documentElement.outerHTML};
  for (const tr of tb.querySelectorAll('tbody tr')) {
    const c = [...tr.cells];
    const ib = tr.querySelector('td.iban');
    if (!ib || c.length < 5) continue;
    const href = [...tr.querySelectorAll('a[href]')].map(a => a.getAttribute('href')).join(' ');
    const m = href.match(/(?:AID|accountId)=(\d+)/);
    out.push({name: c[0].innerText.trim(), iban: ib.innerText.replace(/\s+/g, ''), currency: c[2].innerText.trim(),
              balance: c[3].innerText.trim(), available: c[4].innerText.trim(),
              last_move: (c[5] ? c[5].innerText.trim() : ''), aid: m ? m[1] : ''});
  }
  return {accounts: out, html: tb.outerHTML};
}
"""

JS_STATEMENT = r"""
async ([aid, from, to]) => {
  const q = 'Identity=' + encodeURIComponent('HostID: 0.\tDocID: 0.\tDocSystem: LOC') +
            '&From=' + encodeURIComponent(from + ' 00:00:00') + '&To=' + encodeURIComponent(to + ' 00:00:00') +
            '&TargetType=PDF&AccountID=' + aid + '&SortOrder=ByDate&SortDirection=ByAscending&Amount=0&Condition=MoreThan';
  const html = await (await fetch('/biz/account/previewtransaction/?' + q)).text();
  const d = new DOMParser().parseFromString(html, 'text/html');
  const tb = [...d.querySelectorAll('table')].find(t => /Дата операції/.test(t.innerText));
  const res = {ops: [], opening: null, closing: null, found: !!tb};
  if (!tb) { res.html = html; return res; }
  const rows = [...tb.rows];
  const txt = el => el.innerText.replace(/\s+/g, ' ').trim();
  for (let i = 0; i < rows.length; i++) {
    const c = [...rows[i].cells];
    const first = c[0] ? txt(c[0]) : '';
    if (/Вх[iі]дне сальдо/.test(first)) { res.opening = [txt(c[1]), txt(c[2])]; continue; }
    if (/Вих[iі]дне сальдо/.test(first)) { res.closing = [txt(c[1]), txt(c[2])]; continue; }
    if (c.length >= 10 && /^\d{2}\.\d{2}\.\d{4}/.test(first)) {
      const nx = rows[i + 1] ? [...rows[i + 1].cells] : [];
      const purpose = (nx.length && nx.length <= 3) ? txt(nx[nx.length - 1]) : '';
      res.ops.push({date: first, time: txt(c[1]), doc: txt(c[2]), debit: txt(c[3]), credit: txt(c[4]),
                    bank_code: txt(c[5]), bank: txt(c[6]), account: txt(c[7]).replace(/\s+/g, ''),
                    party: txt(c[8]), edrpou: txt(c[9]), purpose});
    }
  }
  return res;
}
"""


def _num(s):
    import banks
    return banks.num(s)


def _iso(dmy):
    p = (dmy or "")[:10].split(".")
    return f"{p[2]}-{p[1]}-{p[0]}" if len(p) == 3 else ""


def _us(d):
    return d.strftime("%m/%d/%Y")


def months_back(n=2, today=None):
    """[(перший день, останній день)] для поточного і n-1 попередніх місяців (поточний — до сьогодні)."""
    today = today or date.today()
    out, first = [], today.replace(day=1)
    for _i in range(n):
        last = today if first.month == today.month and first.year == today.year else \
            (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        out.append((first, last))
        first = (first - timedelta(days=1)).replace(day=1)
    return out


def read(page, pages, months=2):
    """-> {'accounts': [...], 'ops': [...], 'periods': [...]} по всіх рахунках компанії."""
    b = page.evaluate(JS_BALANCE)
    pages["03_balance"] = b.get("html", "")
    if not b["accounts"]:
        from banks import BankError
        from i18n import _
        raise BankError(_("вход есть, но таблица счетов не найдена — скачайте страницы для диагностики"))
    accounts, ops, periods = [], [], []
    for a in b["accounts"]:
        accounts.append({"name": a["name"], "iban": a["iban"], "currency": a["currency"] or "UAH",
                         "balance": _num(a["balance"]), "available": _num(a["available"]),
                         "last_move": _iso(a["last_move"]), "aid": a["aid"]})
        if not a["aid"]:
            continue
        for first, last in months_back(months):
            st = page.evaluate(JS_STATEMENT, [a["aid"], _us(first), _us(last)])
            if not st.get("found"):
                pages[f"04_statement_{a['aid']}_{first:%Y%m}"] = st.get("html", "")
                continue
            periods.append(first.strftime("%Y-%m"))
            for o in st["ops"]:
                deb, cred = _num(o["debit"]) or 0, _num(o["credit"]) or 0
                ops.append({"date": _iso(o["date"]), "time": o["time"] if ":" in o["time"] else "",
                            "doc": o["doc"], "amount": round(cred - deb, 2), "iban": a["iban"],
                            "currency": a["currency"] or "UAH", "party": o["party"], "edrpou": o["edrpou"],
                            "party_account": o["account"], "party_bank": o["bank"], "purpose": o["purpose"]})
    return {"accounts": accounts, "ops": ops, "periods": sorted(set(periods))}
