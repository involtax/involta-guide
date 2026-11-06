// Network, result merging and telemetry stay separate from the capsule/ARIA adapter.
window.GuideHybridSearch = ({searchUrl, eventUrl, onChange}) => {
  const cache = new Map();
  let current = null, timer, controller, localReady = false, generation = 0, destroyed = false;
  const keyOf = value => (value.normalize('NFKC').toLowerCase().replaceAll('ё', 'е').match(/[\p{L}\p{N}]+/gu) || []).join(' ');
  const publicResult = row => Object.fromEntries(['id', 'kind', 'name', 'location', 'context', 'anchor', 'match'].map(key => [key, row[key] || '']));
  const compare = (a, b) => {
    for (let i = 0; i < 4; i++) {
      const diff = (a.rank?.[i] || 0) - (b.rank?.[i] || 0);
      if (diff) return diff;
    }
    return 0;
  };
  function emit() { if (!destroyed) onChange(); }
  function send(reason = null, clicked = null) {
    const attempt = current;
    if (!attempt || !attempt.settled || (attempt.clicked && reason !== 'select')) return;
    if (!attempt.shown && !reason) return;
    const body = {
      token: attempt.token, query: attempt.raw, seq: ++attempt.seq,
      shown: attempt.shown?.map(publicResult) ?? null,
      version: attempt.version, complete: complete(), cache_hit: attempt.cached, client_error: attempt.phase === 'error',
      reason, clicked_id: clicked?.id || null
    };
    fetch(eventUrl, {method: 'POST', headers: {'Content-Type': 'application/json'},
      credentials: 'same-origin', body: JSON.stringify(body), keepalive: true}).catch(() => {});
  }
  function complete() {
    return !!current && (current.phase === 'ready' || (current.phase === 'local' && localReady));
  }
  async function request(attempt, revision) {
    controller = new AbortController();
    const pending = controller;
    try {
      const response = await fetch(searchUrl, {
        method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: attempt.token, query: attempt.raw}),
        signal: AbortSignal.any([pending.signal, AbortSignal.timeout(5000)])
      });
      if (!response.ok) throw new Error('Search unavailable');
      const data = await response.json();
      if (typeof data.version !== 'string' || !/^[a-z0-9._-]{1,80}$/.test(data.version) || !Array.isArray(data.results) || data.results.length > 8 || data.results.some(row => !row ||
        !['location', 'restaurant'].includes(row.kind) || typeof row.id !== 'string' ||
        typeof row.name !== 'string' || typeof row.location !== 'string' || !row.location ||
        typeof row.context !== 'string' || typeof row.anchor !== 'string' ||
        !Array.isArray(row.rank) || row.rank.some(value => !Number.isFinite(value)))) throw new Error('Invalid search response');
      if (destroyed || revision !== generation || attempt !== current || attempt.clicked) return;
      cache.set(attempt.key, {results: data.results, version: data.version});
      if (cache.size > 50) cache.delete(cache.keys().next().value);
      attempt.results = data.results; attempt.version = data.version;
      attempt.phase = 'ready';
    } catch {
      if (destroyed || pending.signal.aborted || revision !== generation || attempt !== current || attempt.clicked) return;
      attempt.phase = 'error';
    } finally {
      if (controller === pending) controller = null;
      if (!destroyed && revision === generation && attempt === current && !attempt.clicked) emit();
    }
  }
  function settle() {
    if (!current || destroyed || current.clicked) return;
    current.settled = true;
    send();
    if (current.phase === 'waiting' && !controller) request(current, generation);
  }
  function change(value) {
    const key = keyOf(value) || value.trim();
    if (key === current?.key) return;
    clearTimeout(timer);
    send(key ? 'refined' : 'clear');
    controller?.abort(); controller = null;
    generation++;
    current = key ? {key, raw: value.trim().slice(0, 100), token: crypto.randomUUID(), seq: 0,
      shown: null, settled: false, clicked: false, cached: cache.has(key),
      phase: cache.has(key) ? 'ready' : Array.from(key).length < 2 ? 'local' : 'waiting',
      results: cache.get(key)?.results || [], version: cache.get(key)?.version || 'local-names-v1'} : null;
    if (current) timer = setTimeout(settle, 300);
  }
  function localRows(rows) {
    const query = current?.key || '';
    return rows.map(row => {
      const name = keyOf(row.name);
      const words = query.split(' ');
      const rank = name === query ? 0 : words.every(word => name.split(' ').some(part => part.startsWith(word))) ? 1
        : words.every(word => name.includes(word)) ? 3 : 4;
      return {...row, id: 'location:' + row.location, kind: 'location', anchor: '', match: 'local', rank: [rank, 0, 0, 0]};
    });
  }
  function merge(local, previous, locked) {
    const all = new Map(localRows(local).map(row => [row.id, row]));
    for (const row of current?.results || []) all.set(row.id, row);
    const sorted = [...all.values()].sort(compare);
    const ranked = [];
    for (let start = 0; start < sorted.length;) {
      let end = start + 1;
      while (end < sorted.length && compare(sorted[start], sorted[end]) === 0) end++;
      const peers = sorted.slice(start, end);
      const locations = peers.filter(row => row.kind === 'location');
      const restaurants = peers.filter(row => row.kind === 'restaurant');
      for (let i = 0; i < Math.max(locations.length, restaurants.length * 2); i += 2) {
        ranked.push(...locations.slice(i, i + 2), ...restaurants.slice(i / 2, i / 2 + 1));
      }
      start = end;
    }
    if (!locked) return ranked.slice(0, 8);
    // Preserve every displayed slot while someone is navigating or pointing at it.
    const stable = previous.map(row => all.get(row.id) || row);
    const seen = new Set(stable.map(row => row.id));
    return [...stable, ...ranked.filter(row => !seen.has(row.id))].slice(0, 8);
  }
  return {
    change, merge,
    get key() { return current?.key || ''; },
    get waiting() { return !!current && !complete() && current.phase !== 'error'; },
    get failed() { return current?.phase === 'error'; },
    get canRenderEmpty() { return complete() || current?.phase === 'error'; },
    localLoaded() { localReady = true; emit(); },
    shown(rows) {
      if (!current || current.clicked) return;
      const snapshot = rows.map(publicResult);
      const signature = JSON.stringify([snapshot, complete(), current.phase]);
      if (signature === current.signature) return;
      current.signature = signature;
      current.shown = snapshot;
      if (current.settled) send();
    },
    end(reason = 'blur') { clearTimeout(timer); send(reason); },
    resume() { clearTimeout(timer); if (current?.settled) send(); else if (current) timer = setTimeout(settle, 300); },
    suspend() { clearTimeout(timer); },
    select(row) {
      if (!current) return;
      clearTimeout(timer);
      current.settled = true;
      send('select', row);
      current.clicked = true;
      controller?.abort();
    },
    retry() {
      if (!current || controller) return;
      current.phase = Array.from(current.key).length < 2 ? 'local' : 'waiting';
      current.settled = true;
      if (current.phase === 'waiting') request(current, generation);
    },
    destroy() { send('leave'); destroyed = true; clearTimeout(timer); controller?.abort(); }
  };
};
