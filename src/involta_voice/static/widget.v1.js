/* Standalone v1 embed: no Guide DOM, styles, cookies or implementation imports. */
(() => {
  const script = document.currentScript;
  if (!window.InvoltaVoice) {
    const instances = new Map();
    const allowed = {
      idle: ["permission_pending", "error"], permission_pending: ["recording", "error"],
      recording: ["preview", "error"], preview: ["permission_pending", "uploading", "error"],
      uploading: ["accepted", "error"], accepted: ["permission_pending"],
      error: ["permission_pending", "uploading", "preview"],
    };
    class Diagnostics {
      constructor(root, { key, api, location = '' }) {
        this.key = `${key}:visitor:${location}`; this.api = api; this.location = location; this.epoch = null;
        this.report = { version: 2, browser: navigator.userAgent, location, events: [] };
        this.panel = document.createElement('details'); this.panel.open = true;
        const title = document.createElement('summary'); title.textContent = 'Диагностика записи · бета';
        this.info = document.createElement('pre'); this.info.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;max-height:32em;overflow:auto';
        this.raw = document.createElement('pre'); this.raw.style.cssText = this.info.style.cssText;
        this.download = document.createElement('a'); this.download.textContent = 'Скачать аудио'; this.download.hidden = true;
        const copy = document.createElement('button'); copy.type = 'button'; copy.textContent = 'Скопировать отчёт';
        copy.onclick = async () => { try { await navigator.clipboard.writeText(JSON.stringify(this.report, null, 2)); copy.textContent = 'Отчёт скопирован'; } catch { copy.textContent = 'Выделите и скопируйте отчёт ниже'; } };
        const heading = document.createElement('p'); heading.textContent = 'Полный ответ сервера';
        this.panel.append(title, this.download, copy, this.info, heading, this.raw); root.append(this.panel);
        this.render();
      }
      clean(text) {
        return text.replace(/("(?:csrf_token|_csrf|authorization|cookie|set-cookie|api_key|password)"\s*:\s*)"(?:[^"\\]|\\.)*"/gi, '$1"[REDACTED]"');
      }
      render() { this.info.textContent = JSON.stringify(this.report, null, 2); }
      async db() {
        return await new Promise((resolve, reject) => {
          const open = indexedDB.open('involta-voice-drafts', 1);
          open.onupgradeneeded = () => open.result.createObjectStore('drafts', { keyPath: 'key' });
          open.onsuccess = () => resolve(open.result); open.onerror = () => reject(open.error);
        });
      }
      async store(mode, value) {
        const db = await this.db();
        try { return await new Promise((resolve, reject) => {
          const tx = db.transaction('drafts', mode === 'get' ? 'readonly' : 'readwrite');
          const records = tx.objectStore('drafts');
          const operation = mode === 'get' ? records.get(this.key) : mode === 'delete' ? records.delete(this.key) : records.put(value);
          tx.oncomplete = () => resolve(operation.result); tx.onerror = () => reject(tx.error); tx.onabort = () => reject(tx.error);
        }); } finally { db.close(); }
      }
      async restore() {
        try {
          const saved = await this.store('get');
          if (saved?.blob) { this.showAudio(saved.blob, saved.id); this.report.draft = { id: saved.id, bytes: saved.blob.size, mime: saved.blob.type, local_copy_saved: true }; this.render(); }
          return saved;
        } catch (e) { this.report.storage_error = e.message; this.render(); return null; }
      }
      showAudio(blob, id) {
        if (this.audioURL) URL.revokeObjectURL(this.audioURL);
        this.audioURL = URL.createObjectURL(blob); this.download.href = this.audioURL;
        this.download.download = `voice-${id}.${blob.type.includes('mp4') ? 'm4a' : blob.type.includes('ogg') ? 'ogg' : 'webm'}`;
        this.download.hidden = false;
      }
      async save(blob, id, seconds, epoch) {
        this.epoch = epoch; this.showAudio(blob, id);
        this.report.draft = { id, bytes: blob.size, mime: blob.type, seconds, content_epoch: epoch, local_copy_saved: false };
        try { await this.store('put', { key: this.key, id, blob, seconds, content_epoch: epoch, location: this.location, updated: Date.now() }); this.report.draft.local_copy_saved = true; }
        catch (e) { this.report.storage_error = e.message; }
        this.render();
      }
      async clear() {
        try { await this.store('delete'); } catch (e) { this.report.storage_error = e.message; }
        if (this.audioURL) URL.revokeObjectURL(this.audioURL); this.audioURL = null;
        this.download.hidden = true; this.report.draft = null; this.render();
      }
      begin() { this.id = crypto.randomUUID(); this.report.request_id = this.id; this.report.events = []; this.render(); }
      async request(url, options = {}) {
        const start = performance.now();
        const entry = { request_id: this.id, url: url.toString(), method: options.method || 'GET', started: new Date().toISOString() };
        this.report.events.push(entry); this.render();
        const headers = new Headers(options.headers || {}); headers.set('X-Voice-Request-ID', this.id);
        try {
          const response = await fetch(url, { signal: AbortSignal.timeout(90000), ...options, headers });
          const body = await response.text();
          entry.elapsed_ms = Math.round(performance.now() - start); entry.status = response.status;
          entry.status_text = response.statusText; entry.final_url = response.url;
          entry.response_headers = Object.fromEntries([...response.headers].filter(([key]) => !/cookie|authorization|csrf/i.test(key)));
          entry.response_body = this.clean(body); this.raw.textContent = entry.response_body; this.render();
          let data;
          try { data = JSON.parse(body); }
          catch { throw new Error(`Сервер вернул неожиданный ответ (HTTP ${response.status}). Запись сохранена; полный ответ — в диагностике ниже.`); }
          if (!response.ok) {
            const failure = new Error((typeof data.error === 'string' ? data.error : data.error?.message) || (typeof data.detail === 'string' ? data.detail : data.detail?.message || `Не удалось отправить запись (HTTP ${response.status}).`));
            failure.code = data.error?.code || data.detail?.code; throw failure;
          }
          return data;
        } catch (error) {
          entry.elapsed_ms = Math.round(performance.now() - start); entry.exception = { name: error.name, message: error.message };
          this.render(); throw error;
        }
      }
      async publish() {
        if (!this.id) return;
        try {
          const response = await fetch(`${this.api}/debug/${this.id}?location=${encodeURIComponent(this.location)}&content_epoch=${this.epoch || 0}`, {
            method: 'POST', credentials: 'same-origin', signal: AbortSignal.timeout(5000), headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...this.report, server: undefined })
          });
          this.report.diagnostics_saved = response.ok;
          this.report.diagnostics_status = response.status;
        } catch (e) { this.report.diagnostics_saved = false; this.report.diagnostics_error = e.message; }
        this.render();
      }
      async serverTrace() {
        if (!this.id) return;
        try {
          const r = await fetch(`${this.api}/debug/${this.id}?location=${encodeURIComponent(this.location)}&content_epoch=${this.epoch || 0}`, { credentials: 'same-origin', signal: AbortSignal.timeout(5000) });
          if (r.ok) this.report.server = await r.json();
          else this.report.server_trace_status = r.status;
        } catch (e) { this.report.server_trace_error = e.message; }
        this.render();
      }
      dispose() { if (this.audioURL) URL.revokeObjectURL(this.audioURL); this.panel.remove(); }
    }

    class Machine {
      state = "idle";
      move(next) {
        if (!allowed[this.state].includes(next)) throw new Error("Invalid recording transition");
        this.state = next;
      }
    }
    function mount(target, options = {}) {
      if (typeof target === "string") target = document.getElementById(target);
      if (!target || instances.has(target)) return instances.get(target);
      const root = target.shadowRoot || target.attachShadow({ mode: "open" });
      const api = new URL(options.apiBase || "/guide/voice/api/v1", location.href);
      if (api.origin !== location.origin) throw new Error("Voice gateway must share page origin");
      const machine = new Machine();
      let recorder, stream, timer, poll, historyPoll, previewUrl, blob, key, reference;
      let chunks = [], disposed = false, started = 0, delay = 2000, bytes = 0;
      const style = document.createElement("style");
      style.textContent = ":host{display:block;font:16px/1.5 system-ui;color:inherit}section{padding:24px;border:1px solid #ddd;border-radius:12px}h2{font-size:24px;margin:0 0 12px}button{font:inherit;padding:12px 18px;margin:6px 8px 6px 0;cursor:pointer;border:1px solid currentColor;border-radius:6px;background:transparent;color:inherit}button:disabled{opacity:.5;cursor:wait}audio{display:block;max-width:100%;margin:12px 0}.error{color:#a21d1d}.history{white-space:pre-wrap}small{display:block}button:focus-visible{outline:3px solid #5577cc}@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}";
      const panel = document.createElement("section");
      panel.setAttribute("aria-label", "Голосовой отзыв");
      const heading = document.createElement("h2"); heading.textContent = "Расскажите о своём опыте";
      const description = document.createElement("p"); description.textContent = "Запишите отзыв до двух минут. Можно прослушать запись перед отправкой.";
      const controls = document.createElement("div");
      const status = document.createElement("p"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
      const error = document.createElement("p"); error.className = "error"; error.setAttribute("role", "alert");
      const history = document.createElement("div"); history.className = "history";
      const currentText = document.createElement("p");
      panel.append(heading, description, controls, status, error, currentText, history);
      root.replaceChildren(style, panel);
      const diagnostics = new Diagnostics(panel, { key: 'voice', api: api.href, location: options.location || '' });
      let intentEpoch = null;
      let knownEpoch = Number(options.contentEpoch) || null;
      async function identity() {
        const response = await fetch(api.href + '/identity?location=' + encodeURIComponent(options.location || ''), {method:'POST', credentials:'same-origin'});
        const data = await response.json();
        if (!response.ok || !data.ready || !Number.isInteger(data.content_epoch)) {
          const failure = new Error(data.detail?.message || 'Локация временно недоступна. Дождитесь завершения сброса.');
          failure.code = data.detail?.code; throw failure;
        }
        knownEpoch = data.content_epoch; return knownEpoch;
      }
      async function discardStale(epoch) {
        const db = await diagnostics.db();
        try {
          await new Promise((resolve,reject) => {
            const tx = db.transaction('drafts','readwrite'); const store=tx.objectStore('drafts');
            const all=store.getAll(); all.onsuccess=()=>{for(const draft of all.result) {
              if ((draft.location===options.location || draft.key.endsWith(':'+options.location)) && draft.content_epoch!==epoch) store.delete(draft.key);
            }};
            tx.oncomplete=resolve; tx.onerror=()=>reject(tx.error);
          });
        } finally {db.close();}
      }
      async function stale() {
        await diagnostics.clear(); clearBlob(); intentEpoch = null;
        fail('Локация сброшена. Устаревшая запись удалена из очереди браузера. Запишите новый отзыв.');
      }
      function button(label, action, disabled = false) {
        const b = document.createElement("button"); b.type = "button"; b.textContent = label;
        b.disabled = disabled; b.addEventListener("click", action); return b;
      }
      function release() {
        clearInterval(timer); timer = null;
        stream?.getTracks().forEach(track => track.stop()); stream = null;
      }
      function clearBlob() {
        if (previewUrl) URL.revokeObjectURL(previewUrl);
        previewUrl = null; blob = null; key = null; chunks = []; bytes = 0;
      }
      function render() {
        if (disposed) return;
        target.dataset.voiceState = machine.state;
        controls.replaceChildren();
        if (machine.state === "recording") {
          controls.append(button("■ Остановить запись", stop));
        } else if (["permission_pending", "uploading"].includes(machine.state)) {
          controls.append(button(machine.state === "uploading" ? "Отправляем…" : "Ждём разрешения микрофона…", () => {}, true));
        } else {
          if (blob && ["preview", "error"].includes(machine.state)) {
            if (!previewUrl) previewUrl = URL.createObjectURL(blob);
            const player = document.createElement("audio"); player.controls = true; player.src = previewUrl;
            player.setAttribute("aria-label", "Прослушать запись");
            controls.append(player, button("Отправить отзыв", send));
          }
          controls.append(button(blob ? "Записать заново" : "● Записать отзыв", start));
        }
      }
      function transition(next) { machine.move(next); render(); }
      function fail(message) {
        error.textContent = message;
        if (machine.state !== "error") transition("error"); else render();
      }
      async function start() {
        error.textContent = ""; status.textContent = ""; transition("permission_pending");
        if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
          fail("Запись недоступна. Нужен современный браузер и HTTPS."); return;
        }
        try {
          try { intentEpoch = await identity(); await discardStale(intentEpoch); }
          catch (problem) {
            if (problem.code==='stale_epoch') {await stale(); return;}
            if (!knownEpoch) {fail(problem.message); return;}
            intentEpoch=knownEpoch;
          }
          const incoming = await navigator.mediaDevices.getUserMedia({ audio: true });
          if (disposed) { incoming.getTracks().forEach(t => t.stop()); return; }
          stream = incoming; clearBlob();
          const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].find(t => MediaRecorder.isTypeSupported(t));
          recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
          recorder.ondataavailable = event => {
            if (disposed || !event.data.size) return;
            chunks.push(event.data); bytes += event.data.size;
            if (bytes > 10 * 1024 * 1024) {
              recorder.onstop = async () => {}; if (recorder.state !== "inactive") recorder.stop();
              release(); clearBlob(); fail("Запись больше 10 МБ. Запишите более короткий отзыв.");
            }
          };
          recorder.onerror = () => { release(); fail("Не удалось записать звук. Попробуйте ещё раз."); };
          recorder.onstop = async () => {
            release(); if (disposed || machine.state !== "recording") return;
            blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
            key = crypto.randomUUID(); await diagnostics.save(blob, key, Math.floor((performance.now() - started) / 1000), intentEpoch);
            status.textContent = diagnostics.report.draft?.local_copy_saved ? "Запись сохранена в браузере и готова к прослушиванию." : "Запись готова. Не удалось сохранить её в браузере — скачайте аудио перед закрытием страницы."; transition("preview");
          };
          recorder.start(250); started = performance.now(); transition("recording");
          timer = setInterval(() => {
            const seconds = Math.floor((performance.now() - started) / 1000);
            status.textContent = `Запись: ${seconds} с из 120`;
            if (seconds >= 120) stop();
          }, 500);
        } catch {
          release(); fail("Нет доступа к микрофону. Разрешите запись в настройках браузера.");
        }
      }
      function stop() { if (recorder?.state === "recording") recorder.stop(); }
      async function send() {
        if (!blob || !key) return;
        error.textContent = ""; transition("uploading");
        diagnostics.begin();
        const params = new URLSearchParams({ location: options.location || '', idempotency_key: key, content_epoch: String(intentEpoch || 0), language: navigator.language || '', timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '' });
        try {
          const currentEpoch = await identity();
          if (!intentEpoch || currentEpoch!==intentEpoch) {await stale(); return;}
          const data = await diagnostics.request(api.href + "/submissions?" + params, {
            method: "POST", credentials: "same-origin", body: blob,
            headers: { 'Content-Type': blob.type || 'audio/webm' }
          });
          if (!data.reference || typeof data.reference !== 'string') throw new Error('Сервер не подтвердил сохранение. Запись остаётся в браузере.');
          if (disposed) return;
          reference = data.reference; await diagnostics.publish(); await diagnostics.clear(); clearBlob(); transition("accepted");
          status.textContent = "Запись доставлена. Расшифровываем ваш отзыв."; delay = 2000;
          schedule();
        } catch (e) {
          if (e.code==='stale_epoch') {await stale(); return;}
          if (!disposed) fail(e.name === 'TypeError' ? "Связь прервалась. Запись сохранена — повторите отправку." : e.message || "Не удалось отправить запись.");
          await diagnostics.publish(); await diagnostics.serverTrace();
        }
      }
      const labels = { pending: "Ожидает редактора", approved: "Принято", rejected: "Отклонено", fraud: "Помечено как фрод", published: "Опубликовано" };
      function schedule() { clearTimeout(poll); if (!disposed) poll = setTimeout(pollStatus, delay); }
      async function pollStatus() {
        if (disposed) return;
        if (document.hidden) { schedule(); return; }
        try {
          const response = await fetch(api.href + "/submissions/" + encodeURIComponent(reference), { credentials: "same-origin" });
          if (!response.ok) throw new Error("status");
          const data = await response.json();
          if (disposed) return;
          status.textContent = data.transcript ? "Текст отзыва сохранён. " + (labels[data.editorial_state || data.moderation] || "Ожидает редактора") : "Запись доставлена. Обрабатываем.";
          currentText.textContent = data.transcript || "";
          if (data.state.includes("failed") || data.state.includes("unknown")) {
            status.textContent = data.transcript ? "Текст отзыва сохранён и доступен редактору. Дополнительный анализ не завершён." : "Запись сохранена. Расшифровка не завершена; редактор может проверить ошибку.";
            return;
          }
          if (["approved", "published", "rejected", "fraud"].includes(data.editorial_state || data.moderation)) return;
          delay = Math.min(delay * 1.5, 60000); schedule();
        } catch { delay = Math.min(delay * 2, 60000); schedule(); }
      }
      async function loadHistory() {
        if (disposed) return;
        if (document.hidden) { historyPoll = setTimeout(loadHistory, 15000); return; }
        let ongoing = false;
        try {
          const response = await fetch(api.href + "/submissions/mine?location=" + encodeURIComponent(options.location || ""), { credentials: "same-origin" });
          if (!response.ok) return;
          const data = await response.json();
          if (disposed) return;
          history.replaceChildren();
          if (data.rows?.length) {
            const title = document.createElement("h3"); title.textContent = "Ваши отзывы"; history.append(title);
          }
          for (const item of data.rows || []) {
            const paragraph = document.createElement("p");
            const state = item.editorial_state || item.moderation;
            paragraph.textContent = (labels[state] || "Обрабатывается") + (item.transcript ? ": " + item.transcript : "");
            history.append(paragraph);
            ongoing ||= !["approved", "published", "rejected", "fraud"].includes(state);
          }
        } catch { ongoing = true; }
        if (ongoing && !disposed) historyPoll = setTimeout(loadHistory, 15000);
      }
      function unmount() {
        disposed = true; clearTimeout(poll); clearTimeout(historyPoll); if (recorder?.state === "recording") recorder.stop();
        release(); clearBlob(); diagnostics.dispose(); root.replaceChildren(); instances.delete(target);
      }
      const instance = { unmount, get state() { return machine.state; } };
      instances.set(target, instance); render(); loadHistory();
      diagnostics.restore().then(async saved => {
        if (disposed) return;
        let epoch;
        try {epoch=await identity(); await discardStale(epoch);}
        catch {epoch=knownEpoch;}
        if (saved?.blob && (!epoch || saved.content_epoch!==epoch)) {await stale(); return;}
        if (saved?.blob && machine.state==='idle') {
          blob=saved.blob; key=saved.id; intentEpoch=saved.content_epoch; diagnostics.epoch=intentEpoch;
          machine.state='preview'; status.textContent='Восстановлена неотправленная запись. Можно прослушать и отправить её.'; render();
        }
      });
      return instance;
    }
    window.InvoltaVoice = { mount, unmount(target) { instances.get(typeof target === "string" ? document.getElementById(target) : target)?.unmount(); }, Machine };
  }
  if (script?.dataset.target) window.InvoltaVoice.mount(script.dataset.target, { apiBase: script.dataset.apiBase, location: script.dataset.location, contentEpoch: script.dataset.contentEpoch });
})();
