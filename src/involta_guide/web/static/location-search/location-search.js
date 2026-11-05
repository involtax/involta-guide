(() => {
  function mount(picker) {
    if (picker.dataset.searchMounted || !window.autoComplete || !window.GuideLocationIndex) return;
    picker.dataset.searchMounted = 'true';
    const lifecycle = new AbortController();
    const input = picker.querySelector('[data-location-input]');
    const choose = picker.querySelector('.search-choose');
    const clear = picker.querySelector('.search-clear');
    const panel = picker.querySelector('.search-suggestions');
    const content = picker.querySelector('.search-panel-content');
    const control = picker.querySelector('.search-control');
    const surface = picker.querySelector('.search-surface');
    const inputRow = picker.querySelector('.search-input-row');
    const status = picker.querySelector('[role="status"]');
    const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
    let catalogue = [], search, rows = [], loading = false, loadError = false;
    let composing = false, allowEmpty = false, expanded = false, closeTimer;

    const folded = value => value.toLowerCase().replaceAll('ё', 'е');
    function title(element, value) {
      const words = folded(input.value).match(/[\p{L}\p{N}]+/gu) || [];
      const normalized = folded(value);
      let offset = 0;
      while (offset < value.length) {
        const hits = words.map(word => ({at: normalized.indexOf(word, offset), size: word.length}))
          .filter(hit => hit.at >= 0).sort((a, b) => a.at - b.at || b.size - a.size);
        if (!hits.length) { element.append(document.createTextNode(value.slice(offset))); break; }
        const hit = hits[0];
        element.append(document.createTextNode(value.slice(offset, hit.at)));
        const mark = document.createElement('mark');
        mark.textContent = value.slice(hit.at, hit.at + hit.size);
        element.append(mark);
        offset = hit.at + hit.size;
      }
    }

    const widget = new autoComplete({
      selector: () => input, wrapper: false, threshold: 0, submit: true,
      data: {src: async () => rows, keys: ['name'], cache: false},
      searchEngine: () => ' ',
      resultsList: {
        class: 'destination-results', destination: () => content, position: 'beforeend',
        maxResults: 6, noResults: true,
        element: (list, data) => {
          if (!data.results.length) {
            const item = document.createElement('li');
            item.className = 'search-message'; item.role = 'presentation';
            item.textContent = loadError
              ? 'Не удалось загрузить локации. Нажмите «Выбери», чтобы повторить.'
              : 'Не нашли такую локацию. Попробуйте другое название.';
            list.append(item);
          }
          status.textContent = loadError ? 'Список локаций недоступен'
            : data.results.length ? `Подсказок: ${data.results.length}` : 'Ничего не найдено';
          requestAnimationFrame(fitResults);
        }
      },
      resultItem: {
        class: 'destination-result', element: (item, data) => {
          item.replaceChildren();
          const text = document.createElement('span');
          const name = document.createElement('span'); name.className = 'result-name';
          title(name, data.value.name);
          const context = document.createElement('span'); context.className = 'result-context';
          context.textContent = data.value.context;
          text.append(name, context);
          const arrow = document.createElement('span'); arrow.className = 'result-arrow';
          arrow.textContent = '→'; arrow.ariaHidden = 'true';
          item.append(text, arrow);
        }
      },
      events: {input: {
        input: () => begin(),
        focus: () => { if (!composing && !expanded) begin(); },
        blur: () => { allowEmpty = false; widget.close(); },
        keydown: event => {
          if (composing || event.isComposing || event.keyCode === 229) return;
          if (event.key === 'Escape') { event.preventDefault(); return; }
          if (event.key === 'Enter') {
            event.preventDefault();
            if (widget.isOpen && widget.feedback?.results.length && widget.cursor >= 0) widget.select(widget.cursor);
            else if (loadError) { allowEmpty = true; loadCatalogue(); }
            else begin(true);
            return;
          }
          if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && !widget.isOpen) {
            event.preventDefault(); begin(true); return;
          }
          if (!widget.isOpen || !widget.feedback?.results.length) return;
          if (event.key === 'ArrowDown') { event.preventDefault(); widget.next(); }
          else if (event.key === 'ArrowUp') { event.preventDefault(); widget.previous(); }
        },
        selection: event => {
          const selected = event.detail.selection?.value;
          if (!selected) return;
          if (picker.hasAttribute('data-location-add')) {
            if (!Number.isSafeInteger(selected.id) || selected.id < 1) return;
            picker.querySelector('[name=nearby_id]').value = selected.id;
            picker.requestSubmit();
            return;
          }
          const base = new URL(picker.dataset.locationBase, location.href);
          const target = new URL(encodeURIComponent(selected.location), base);
          if (target.origin === location.origin && target.pathname.startsWith(base.pathname)) location.assign(target.href);
        },
        open: () => {
          if ((!search && !loadError) || (!input.value.trim() && !allowEmpty) || document.activeElement !== input) {
            widget.close(); return;
          }
          panel.hidden = false; panel.inert = false;
          panel.setAttribute('aria-hidden', 'false'); choose.setAttribute('aria-expanded', 'true');
          clearTimeout(closeTimer);
          panel.getBoundingClientRect();
          expanded = true; control.classList.add('is-open');
          requestAnimationFrame(fitResults);
        },
        close: () => {
          expanded = false; panel.inert = true;
          panel.setAttribute('aria-hidden', 'true'); choose.setAttribute('aria-expanded', 'false');
          control.classList.remove('is-open');
          // Keep the library's list painted until the morph finishes.
          widget.list.hidden = false;
          resizeSurface(); clearTimeout(closeTimer);
          closeTimer = setTimeout(() => {
            if (!expanded) { panel.hidden = true; widget.list.hidden = true; }
          }, reducedMotion.matches ? 0 : 420);
        }
      }}
    });
    input.setAttribute('aria-autocomplete', 'list');
    choose.setAttribute('aria-controls', widget.list.id);
    clear.hidden = !input.value;

    function begin(showEmpty = false) {
      clear.hidden = !input.value;
      allowEmpty = showEmpty;
      if (!input.value.trim() && !showEmpty) { widget.close(); status.textContent = ''; return; }
      if (composing || (!search && !loadError)) return;
      rows = search ? (input.value.trim() ? search(input.value) : catalogue).slice(0, 6) : [];
      widget.start(input.value);
    }
    async function loadCatalogue() {
      if (loading || search) return;
      loading = true; loadError = false;
      widget.close();
      try {
        if (picker.hasAttribute('data-location-add')) {
          catalogue = JSON.parse(picker.querySelector('#location-catalogue').textContent);
        } else {
          const response = await fetch(picker.dataset.catalogueUrl, {credentials: 'omit', signal: AbortSignal.timeout(5000)});
          if (!response.ok) throw new Error('Catalogue unavailable');
          catalogue = await response.json();
          if (!Array.isArray(catalogue) || catalogue.some(item => !item ||
            typeof item.name !== 'string' || typeof item.context !== 'string' ||
            typeof item.location !== 'string' || !item.location)) throw new Error('Invalid catalogue');
        }
        if (!lifecycle.signal.aborted) search = GuideLocationIndex(catalogue);
      } catch { loadError = true; }
      finally {
        loading = false;
        if (!lifecycle.signal.aborted && document.activeElement === input && (input.value.trim() || allowEmpty)) begin(allowEmpty);
      }
    }
    function fitResults() {
      if (!expanded) return;
      const viewport = window.visualViewport;
      const bottom = (viewport?.offsetTop || 0) + (viewport?.height || innerHeight);
      if (document.activeElement === input && innerWidth <= 650 && bottom - panel.getBoundingClientRect().top < 180) {
        control.scrollIntoView({block: 'start', behavior: 'instant'});
      }
      const room = bottom - widget.list.getBoundingClientRect().top - 24;
      picker.style.setProperty('--search-results-height', Math.max(72, Math.min(456, room)) + 'px');
      resizeSurface();
    }
    function resizeSurface() {
      surface.style.height = inputRow.offsetHeight + 2 + (expanded ? panel.offsetHeight : 0) + 'px';
    }
    choose.addEventListener('mousedown', event => event.preventDefault());
    choose.addEventListener('click', () => {
      const wasOpen = expanded;
      input.focus();
      if (!search) { allowEmpty = true; loadCatalogue(); }
      else if (wasOpen) widget.close();
      else begin(true);
    });
    input.addEventListener('compositionstart', () => { composing = true; });
    input.addEventListener('compositionend', () => { composing = false; begin(); });
    clear.addEventListener('click', () => { input.value = ''; allowEmpty = false; clear.hidden = true; widget.close(); status.textContent = ''; input.focus(); });
    window.addEventListener('resize', fitResults, {signal: lifecycle.signal});
    window.visualViewport?.addEventListener('resize', fitResults, {signal: lifecycle.signal});
    const observer = new ResizeObserver(resizeSurface);
    observer.observe(content);
    document.addEventListener('htmx:beforeCleanupElement', event => {
      if (event.detail.elt === picker || event.detail.elt.contains(picker)) {
        lifecycle.abort(); observer.disconnect(); clearTimeout(closeTimer);
      }
    }, {signal: lifecycle.signal});
    resizeSurface();
    loadCatalogue();
  }
  function mountAll(root) {
    if (root.matches?.('[data-location-search]')) mount(root);
    root.querySelectorAll('[data-location-search]').forEach(mount);
  }
  mountAll(document);
  document.addEventListener('htmx:load', event => mountAll(event.detail.elt));
})();
