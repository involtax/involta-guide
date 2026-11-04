(() => {
  const form = document.querySelector('[data-location-search]');
  if (!form || !window.autoComplete) return;
  const input = form.querySelector('input[name="q"]');
  const clear = form.querySelector('.search-clear');
  const panel = form.querySelector('.search-suggestions');
  const content = form.querySelector('.search-panel-content');
  const control = form.querySelector('.search-control');
  const surface = form.querySelector('.search-surface');
  const inputRow = form.querySelector('.search-input-row');
  const footer = form.querySelector('.search-menu-footer');
  const status = form.querySelector('[role="status"]');
  let rows = [], message = '', generation = 0, renderGeneration = -1, timer, request;
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
    // Ranking belongs to the server; the library must not insert catalogue HTML.
    searchEngine: () => ' ',
    resultsList: {
      class: 'destination-results', destination: () => footer, position: 'beforebegin',
      maxResults: 6, noResults: true,
      element: (list, data) => {
        if (!data.results.length) {
          const item = document.createElement('li');
          item.className = 'search-message'; item.role = 'presentation';
          item.textContent = message || 'Не нашли такую локацию. Попробуйте другое название.';
          list.append(item);
        }
        status.textContent = message || (data.results.length ? `Подсказок: ${data.results.length}` : 'Ничего не найдено');
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
      focus: () => { if (!composing) begin(); },
      blur: () => invalidate(),
      keydown: event => {
        if (composing || event.isComposing || event.keyCode === 229) return;
        if (event.key === 'Escape') { event.preventDefault(); invalidate(); return; }
        if (!widget.isOpen || !widget.feedback?.results.length) return;
        if (event.key === 'ArrowDown') { event.preventDefault(); widget.next(); }
        else if (event.key === 'ArrowUp') { event.preventDefault(); widget.previous(); }
        else if (event.key === 'Enter' && widget.cursor >= 0) { event.preventDefault(); widget.select(widget.cursor); }
      },
      selection: event => {
        const selected = event.detail.selection?.value;
        if (!selected) return;
        const url = selected.url;
        invalidate();
        // Only canonical Guide links from this origin can be selected.
        const target = new URL(url, location.href);
        if (target.origin === location.origin && target.pathname.startsWith(form.getAttribute('action'))) {
          location.assign(target.href);
        }
      },
      open: () => {
        if (renderGeneration !== generation || document.activeElement !== input || composing) { widget.close(); return; }
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
        panel.style.minHeight = '';
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

  function invalidate(close = true) {
    generation++;
    clearTimeout(timer);
    request?.abort(); request = undefined;
    if (close) widget.close();
    input.removeAttribute('aria-busy'); status.textContent = '';
  }
  function current(version) {
    return version === generation && document.activeElement === input && !composing;
  }
  function show(items, text = '', pending = false) {
    if (!pending) panel.style.minHeight = '';
    rows = items; message = text; renderGeneration = generation;
    footer.hidden = !items.length && !!text;
    widget.start(input.value);
  }
  function begin() {
    clear.hidden = !input.value;
    invalidate(false);
    if (composing) { widget.close(); return; }
    const length = input.value.trim().length;
    if (!length) { widget.close(); return; }
    // Preserve the current height while fetching, without leaving stale options selectable.
    if (expanded && length >= 2) panel.style.minHeight = panel.offsetHeight + 'px';
    show([], length < 2 ? 'Продолжайте ввод…' : 'Ищем…', length >= 2);
    if (length >= 2) schedule();
  }
  function schedule() {
    if (input.value.trim().length < 2) return;
    const version = generation;
    timer = setTimeout(() => fetchRows(version), 180);
  }
  async function fetchRows(version) {
    if (!current(version)) return;
    const controller = new AbortController(); request = controller;
    const timeout = setTimeout(() => controller.abort(), 4000);
    input.setAttribute('aria-busy', 'true');
    try {
      const url = new URL(form.dataset.suggestUrl, location.href);
      url.searchParams.set('q', input.value.trim());
      const response = await fetch(url, {signal: controller.signal, credentials: 'omit'});
      if (!response.ok) throw new Error('Search unavailable');
      const data = await response.json();
      if (!Array.isArray(data.items) || data.items.some(item =>
        typeof item.name !== 'string' || typeof item.context !== 'string' || typeof item.url !== 'string')) {
        throw new Error('Invalid search response');
      }
      if (current(version)) show(data.items);
    } catch {
      if (current(version)) show([], 'Подсказки временно недоступны.');
    } finally {
      clearTimeout(timeout);
      if (current(version)) {
        input.removeAttribute('aria-busy'); request = undefined;
      }
    }
  }
  function fitResults() {
    if (!expanded) return;
    const viewport = window.visualViewport;
    const bottom = (viewport?.offsetTop || 0) + (viewport?.height || innerHeight);
    if (innerWidth <= 650 && bottom - panel.getBoundingClientRect().top < 180) {
      control.scrollIntoView({block: 'start', behavior: 'instant'});
    }
    const list = form.querySelector('.destination-results');
    const room = bottom - list.getBoundingClientRect().top - footer.getBoundingClientRect().height - 24;
    form.style.setProperty('--search-results-height', Math.max(72, Math.min(456, room)) + 'px');
    resizeSurface();
  }
  function resizeSurface() {
    surface.style.height = inputRow.offsetHeight + 2 + (expanded ? panel.offsetHeight : 0) + 'px';
  }
  input.addEventListener('compositionstart', () => { composing = true; invalidate(); });
  input.addEventListener('compositionend', () => { composing = false; begin(); });
  clear.addEventListener('click', () => { invalidate(); input.value = ''; clear.hidden = true; input.focus(); });
  footer.addEventListener('mousedown', event => event.preventDefault());
  form.addEventListener('submit', () => invalidate());
  window.addEventListener('resize', fitResults);
  window.visualViewport?.addEventListener('resize', fitResults);
  new ResizeObserver(resizeSurface).observe(content);
  resizeSurface();
})();
