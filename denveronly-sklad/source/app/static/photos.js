// Галерея фото объекта: миниатюры, загрузка (несколько файлов / камера телефона), удаление, просмотр на весь экран.
(function () {
  const t = window.t || (s => s);
  const esc = s => (s ?? '').toString().replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

  function render(el, wid, photos, opts = {}) {
    el.classList.add('gallery');
    el.dataset.wid = wid;
    el._photos = photos || [];
    const thumbs = el._photos.map((p, i) => `
      <figure class="ph" data-i="${i}">
        <img src="${p.thumb}" alt="${esc(p.caption)}" loading="lazy">
        ${opts.readonly ? '' : `<button type="button" class="phdel" title="${t('Удалить фото')}" data-id="${p.id}">×</button>`}
        ${p.caption ? `<figcaption>${esc(p.caption)}</figcaption>` : ''}
      </figure>`).join('');
    el.innerHTML = `
      <div class="phgrid">${thumbs}
        ${opts.readonly ? '' : `
        <label class="phadd" title="${t('Добавить фото')}">
          <input type="file" accept="image/*" multiple hidden>
          <span class="plus">＋</span><span class="muted">${el._photos.length ? t('Ещё фото') : t('Добавить фото')}</span>
        </label>`}
      </div>
      <div class="phstatus muted"></div>`;
    el.querySelectorAll('.ph img').forEach(img => img.addEventListener('click', () =>
      open(el._photos, +img.parentElement.dataset.i)));
    el.querySelectorAll('.phdel').forEach(b => b.addEventListener('click', async e => {
      e.stopPropagation();
      if (!confirm(t('Удалить это фото?'))) return;
      const r = await fetch(`/photos/${b.dataset.id}/delete`, {method: 'POST', headers: {'X-Requested-With': 'fetch'}});
      if (r.ok) { const j = await r.json(); render(el, wid, j.photos, opts); changed(el, j.photos); }
    }));
    const inp = el.querySelector('input[type=file]');
    if (inp) inp.addEventListener('change', () => upload(el, wid, inp.files, opts));
  }

  function upload(el, wid, files, opts) {
    if (!files.length) return;
    const fd = new FormData();
    [...files].forEach(f => fd.append('photos', f));
    const st = el.querySelector('.phstatus');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/warehouses/${wid}/photos`);
    xhr.setRequestHeader('X-Requested-With', 'fetch');
    xhr.upload.onprogress = e => { if (e.lengthComputable) st.textContent = `${t('Загрузка…')} ${Math.round(e.loaded / e.total * 100)}%`; };
    xhr.onload = () => {
      if (xhr.status === 200) {
        const j = JSON.parse(xhr.responseText);
        render(el, wid, j.photos, opts); changed(el, j.photos);
        el.querySelector('.phstatus').textContent = `${t('Загружено')}: ${j.ok}` + (j.errors.length ? ` · ${t('ошибки')}: ${j.errors.join('; ')}` : '');
      } else if (xhr.status === 413) st.textContent = t('Слишком большие файлы — загрузите поменьше за раз');
      else st.textContent = t('Ошибка загрузки') + ' (' + xhr.status + ')';
    };
    xhr.onerror = () => { st.textContent = t('Нет связи с сервером'); };
    st.textContent = t('Загрузка…');
    xhr.send(fd);
  }

  function changed(el, photos) {
    el.dispatchEvent(new CustomEvent('photoschange', {bubbles: true, detail: {wid: +el.dataset.wid, photos}}));
  }

  // ---- просмотр на весь экран ----
  let box, list = [], idx = 0;
  function open(photos, i) {
    list = photos; idx = i;
    if (!box) {
      box = document.createElement('div');
      box.className = 'lightbox';
      box.innerHTML = `<button class="lbx lbclose" title="${t('Закрыть')}">×</button>
        <button class="lbx lbprev" title="${t('Назад')}">‹</button><img alt=""><button class="lbx lbnext" title="${t('Вперёд')}">›</button>
        <div class="lbcap"></div>`;
      document.body.appendChild(box);
      box.querySelector('.lbclose').onclick = close;
      box.querySelector('.lbprev').onclick = e => { e.stopPropagation(); go(-1); };
      box.querySelector('.lbnext').onclick = e => { e.stopPropagation(); go(1); };
      box.addEventListener('click', e => { if (e.target === box) close(); });
      let x0 = null;
      box.addEventListener('touchstart', e => { x0 = e.touches[0].clientX; }, {passive: true});
      box.addEventListener('touchend', e => { if (x0 === null) return; const dx = e.changedTouches[0].clientX - x0; if (Math.abs(dx) > 40) go(dx < 0 ? 1 : -1); x0 = null; });
      document.addEventListener('keydown', e => {
        if (!box.classList.contains('on')) return;
        if (e.key === 'Escape') close(); if (e.key === 'ArrowLeft') go(-1); if (e.key === 'ArrowRight') go(1);
      });
    }
    show(); box.classList.add('on'); document.body.style.overflow = 'hidden';
  }
  function go(d) { idx = (idx + d + list.length) % list.length; show(); }
  function show() {
    const p = list[idx];
    box.querySelector('img').src = p.url;
    box.querySelector('.lbcap').textContent = (p.caption ? p.caption + ' · ' : '') + `${idx + 1} / ${list.length}` + (p.created_at ? ' · ' + p.created_at.slice(0, 16) : '');
    box.querySelectorAll('.lbprev,.lbnext').forEach(b => b.style.display = list.length > 1 ? '' : 'none');
  }
  function close() { box.classList.remove('on'); document.body.style.overflow = ''; }

  window.Gallery = {render, open};
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-gallery]').forEach(el =>
      render(el, +el.dataset.wid, JSON.parse(el.dataset.photos || '[]'), {readonly: el.dataset.readonly === '1'}));
  });
})();
