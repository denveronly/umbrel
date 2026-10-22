// Поля дати завжди у форматі дд.мм.рррр (незалежно від мови браузера) + кнопка календаря.
// Кожне <input type="date"> замінюється: видиме текстове поле дд.мм.рррр, приховане поле з тим самим name у форматі
// YYYY-MM-DD (його і отримує сервер), і нативний календар, що відкривається кнопкою.
(function () {
  const toIso = s => {
    s = (s || '').trim();
    let m = s.match(/^(\d{1,2})[.\/\-\s](\d{1,2})[.\/\-\s](\d{2}|\d{4})$/);
    if (m) {
      let [, d, mo, y] = m; if (y.length === 2) y = '20' + y;
      const dt = new Date(+y, +mo - 1, +d);
      if (dt.getFullYear() === +y && dt.getMonth() === +mo - 1 && dt.getDate() === +d)
        return `${y}-${String(mo).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      return null;
    }
    m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (m) return s;
    m = s.match(/^(\d{2})(\d{2})(\d{4})$/);                       // 01032024
    return m ? toIso(`${m[1]}.${m[2]}.${m[3]}`) : (s ? null : '');
  };
  const toDmy = iso => { const m = (iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/); return m ? `${m[3]}.${m[2]}.${m[1]}` : (iso || ''); };
  const CAL = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>';

  function convert(inp) {
    if (inp.dataset.dmy) return;
    inp.dataset.dmy = '1';
    const iso0 = toIso(inp.value) ?? '';
    const wrap = document.createElement('span');
    wrap.className = 'dmy';
    inp.parentNode.insertBefore(wrap, inp);
    const hidden = document.createElement('input');
    hidden.type = 'hidden'; hidden.name = inp.name; hidden.value = iso0;
    const nat = document.createElement('input');                    // нативний календар (невидимий)
    nat.type = 'date'; nat.tabIndex = -1; nat.className = 'dmy-nat'; nat.value = iso0; nat.setAttribute('aria-hidden', 'true');
    inp.removeAttribute('name'); inp.type = 'text'; inp.value = toDmy(iso0);
    inp.placeholder = inp.placeholder || (window.t ? t('дд.мм.гггг') : 'дд.мм.рррр');
    inp.inputMode = 'numeric'; inp.autocomplete = 'off'; inp.maxLength = 10;
    const btn = document.createElement('button');
    btn.type = 'button'; btn.className = 'dmy-btn'; btn.innerHTML = CAL; btn.title = window.t ? t('Календарь') : '';
    wrap.append(inp, btn, nat, hidden);
    // властивість .value — щоб чужий код (inp.value = '2026-10-22') теж працював
    const set = (iso, fire) => {
      hidden.value = iso; nat.value = iso; inp.value = toDmy(iso); inp.classList.remove('bad');
      if (fire) { hidden.dispatchEvent(new Event('change', {bubbles: true})); }
    };
    inp._dmySet = set;
    inp.addEventListener('input', e => {                           // крапки ставляться самі: 22102026 -> 22.10.2026
      if (e.inputType && e.inputType.startsWith('delete')) return;
      let v = inp.value.replace(/[^\d.]/g, '');
      if (/^\d{3}$/.test(v)) v = v.slice(0, 2) + '.' + v.slice(2);
      if (/^\d{2}\.\d{3}$/.test(v)) v = v.slice(0, 5) + '.' + v.slice(5);
      inp.value = v;
    });
    inp.addEventListener('change', () => {
      const iso = toIso(inp.value);
      if (iso === null) { inp.classList.add('bad'); hidden.value = ''; return; }
      set(iso, true);
    });
    btn.addEventListener('click', () => {
      try { nat.showPicker(); } catch (e) { nat.focus(); nat.click(); }
    });
    nat.addEventListener('change', () => set(nat.value, true));
    inp.form?.addEventListener('submit', e => {
      const iso = toIso(inp.value);
      if (iso === null) { e.preventDefault(); inp.classList.add('bad'); inp.focus(); }
      else hidden.value = iso;
    });
  }
  window.dmyInit = root => (root || document).querySelectorAll('input[type=date]:not(.dmy-nat)').forEach(convert);
  window.dmySet = (inp, iso) => inp._dmySet ? inp._dmySet(iso || '') : (inp.value = iso || '');
  document.addEventListener('DOMContentLoaded', () => {
    dmyInit();
    new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => n.nodeType === 1 && dmyInit(n))))
      .observe(document.body, {childList: true, subtree: true});
  });
})();
