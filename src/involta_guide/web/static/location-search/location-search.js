(() => {
  async function mount(form) {
  if (form.dataset.searchMounted || !window.autoComplete || !window.GuideLocationIndex) return;
  form.dataset.searchMounted = 'true';
  const lifecycle = new AbortController();
  let catalogue;
  try {
    if (form.hasAttribute('data-location-add')) {
      catalogue = JSON.parse(form.querySelector('#location-catalogue').textContent);
    } else {
      const response = await fetch(form.dataset.catalogueUrl, {credentials: 'omit', signal: AbortSignal.timeout(5000)});
      if (!response.ok) return;
      catalogue = await response.json();
      if (!Array.isArray(catalogue) || catalogue.some(item => !item ||
        typeof item.name !== 'string' || typeof item.context !== 'string' ||
        typeof item.location !== 'string' || !item.location)) return;
    }
  }
  catch { return; }
  const search = GuideLocationIndex(catalogue);
  const input = form.querySelector('input[name="q"]');
  const clear = form.querySelector('.search-clear');
  const panel = form.querySelector('.search-suggestions');
  const content = form.querySelector('.search-panel-content');
  const control = form.querySelector('.search-control');
  const surface = form.querySelector('.search-surface');
  const inputRow = form.querySelector('.search-input-row');
  const status = form.querySelector('[role="status"]');
  let rows = [];
  let composing = false;
  let expanded = false, closeTimer;
  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');

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
    // The in-memory index ranks names; the library only renders safe DOM nodes.
    searchEngine: () => ' ',
    resultsList: {
      class: 'destination-results', destination: () => content, position: 'beforeend',
      maxResults: 6, noResults: true,
      element: (list, data) => {
        if (!data.results.length) {
          const item = document.createElement('li');
          item.className = 'search-message'; item.role = 'presentation';
          item.textContent = 'Не нашли такую локацию. Попробуйте другое название.';
          list.append(item);
        }
        status.textContent = data.results.length ? `Подсказок: ${data.results.length}` : 'Ничего не найдено';
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
      // Keep the query, but fold the panel when focus leaves the input.
      blur: () => widget.close(),
      keydown: event => {
        if (composing || event.isComposing || event.keyCode === 229) return;
        if (event.key === 'Escape') { event.preventDefault(); return; }
        if (!widget.isOpen || !widget.feedback?.results.length) return;
        if (event.key === 'ArrowDown') { event.preventDefault(); widget.next(); }
        else if (event.key === 'ArrowUp') { event.preventDefault(); widget.previous(); }
        else if (event.key === 'Enter' && widget.cursor >= 0) { event.preventDefault(); widget.select(widget.cursor); }
      },
      selection: event => {
        const selected = event.detail.selection?.value;
        if (!selected) return;
        if (form.hasAttribute('data-location-add')) {
          if (!Number.isSafeInteger(selected.id) || selected.id < 1) return;
          form.querySelector('[name=nearby_id]').value = selected.id;
          form.requestSubmit();
          return;
        }
        // The catalogue stores a slug; the Guide prefix belongs to the form.
        const target = new URL(encodeURIComponent(selected.location), form.action);
        if (target.origin === location.origin && target.pathname.startsWith(new URL(form.action).pathname)) {
          location.assign(target.href);
        }
      },
      open: () => {
        if (!input.value.trim() || document.activeElement !== input) { widget.close(); return; }
        panel.hidden = false;
        panel.inert = false;
        panel.setAttribute('aria-hidden', 'false');
        clearTimeout(closeTimer);
        // Establish the closed panel style before starting its opacity/transform transition.
        panel.getBoundingClientRect();
        expanded = true;
        control.classList.add('is-open');
        requestAnimationFrame(fitResults);
      },
      close: () => {
        expanded = false;
        panel.inert = true;
        panel.setAttribute('aria-hidden', 'true');
        control.classList.remove('is-open');
        // The library hides its list immediately; keep it painted while it fades out.
        widget.list.hidden = false;
        resizeSurface();
        clearTimeout(closeTimer);
        closeTimer = setTimeout(() => {
          if (!expanded) { panel.hidden = true; widget.list.hidden = true; }
        }, reducedMotion.matches ? 0 : 420);
      }
    }}
  });
  input.setAttribute('aria-autocomplete', 'list');
  clear.hidden = !input.value;

  function begin() {
    clear.hidden = !input.value;
    if (!input.value.trim()) { widget.close(); status.textContent = ''; return; }
    if (composing) return;
    rows = search(input.value).slice(0, 6);
    widget.start(input.value);
  }
  function fitResults() {
    if (!expanded) return;
    const viewport = window.visualViewport;
    const bottom = (viewport?.offsetTop || 0) + (viewport?.height || innerHeight);
    if (document.activeElement === input && innerWidth <= 650 && bottom - panel.getBoundingClientRect().top < 180) {
      control.scrollIntoView({block: 'start', behavior: 'instant'});
    }
    const list = form.querySelector('.destination-results');
    const room = bottom - list.getBoundingClientRect().top - 24;
    form.style.setProperty('--search-results-height', Math.max(72, Math.min(456, room)) + 'px');
    resizeSurface();
  }
  function resizeSurface() {
    surface.style.height = inputRow.offsetHeight + 2 + (expanded ? panel.offsetHeight : 0) + 'px';
  }
  input.addEventListener('compositionstart', () => { composing = true; });
  input.addEventListener('compositionend', () => { composing = false; begin(); });
  clear.addEventListener('click', () => { input.value = ''; clear.hidden = true; widget.close(); status.textContent = ''; input.focus(); });
  window.addEventListener('resize', fitResults, {signal: lifecycle.signal});
  window.visualViewport?.addEventListener('resize', fitResults, {signal: lifecycle.signal});
  const observer = new ResizeObserver(resizeSurface);
  observer.observe(content);
  document.addEventListener('htmx:beforeCleanupElement', event => {
    if (event.detail.elt === form || event.detail.elt.contains(form)) {
      lifecycle.abort(); observer.disconnect(); clearTimeout(closeTimer);
    }
  }, {signal: lifecycle.signal});
  resizeSurface();
  if (input.value.trim()) begin();
  }
  function mountAll(root) {
    if (root.matches?.('[data-location-search]')) mount(root);
    root.querySelectorAll('[data-location-search]').forEach(mount);
  }
  mountAll(document);
  document.addEventListener('htmx:load', event => mountAll(event.detail.elt));
})();
