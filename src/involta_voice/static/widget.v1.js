/* One standalone recorder. All VAD assets are pinned and served from this origin. */
(() => {
  const script = document.currentScript;
  if (!window.InvoltaVoice) {
    const instances = new Map();
    const assetBase = new URL("vad/", script?.src || location.href).href;
    let activeCapture = null,
      runtimePromise;
    const releasedVads = new WeakSet();
    async function destroyVad(vad) {
      if (!vad || releasedVads.has(vad)) return;
      releasedVads.add(vad);
      try {
        await vad.destroy();
      } catch {
        // vad-web 0.0.30 destroy assumes graph initialization; release a cancelled model too.
        try {
          await vad.model?.release();
        } catch {
          /* already released by the library */
        }
      }
    }
    const MIN_MS = 5000,
      MAX_MS = 120000,
      MAX_BYTES = 10 * 1024 * 1024;
    const TOO_SHORT =
      "Слишком коротко. Запиши хотя бы 5 секунд: назови ресторан и расскажи, что думаешь.";
    const allowed = {
      idle: ["starting", "retry"],
      starting: ["recording", "stopping", "idle", "error", "retry"],
      recording: ["stopping", "idle", "error"],
      stopping: ["uploading", "retry", "idle", "error"],
      uploading: ["accepted", "retry", "idle"],
      accepted: ["starting"],
      retry: ["starting", "uploading", "idle"],
      error: ["starting", "idle", "retry"],
    };
    class Machine {
      state = "idle";
      move(next) {
        if (!allowed[this.state].includes(next))
          throw new Error("Invalid recording transition");
        this.state = next;
      }
    }
    // Counts only processed, timely live frames. RMS never decides whether speech occurred.
    class SilenceGate {
      constructor() {
        this.last = null;
        this.processed = 0;
        this.positive = 0;
        this.silence = 0;
        this.spoken = false;
        this.disabled = false;
      }
      frame(probability, timestamp, frameMs = 32) {
        if (this.disabled) return {};
        if (
          !Number.isFinite(probability) ||
          !Number.isFinite(timestamp) ||
          !(frameMs > 0)
        ) {
          this.disabled = true;
          return { unavailable: true };
        }
        if (
          this.last !== null &&
          (timestamp - this.last > 250 || timestamp < this.last)
        ) {
          this.disabled = true;
          return { unavailable: true };
        }
        const elapsed =
          this.last === null
            ? frameMs
            : Math.min(frameMs, Math.max(0, timestamp - this.last));
        this.last = timestamp;
        this.processed += elapsed;
        let speech = false;
        if (probability >= 0.3) {
          this.positive += elapsed;
          this.silence = 0;
          if (!this.spoken && this.positive >= 400) {
            this.spoken = true;
            speech = true;
          }
        } else {
          this.positive = 0;
          this.silence =
            this.spoken && probability <= 0.25 ? this.silence + elapsed : 0;
        }
        return {
          speech,
          warning: this.spoken && this.silence >= 4500,
          stop:
            this.spoken && this.silence >= 5000
              ? "silence"
              : !this.spoken && this.processed >= 8000
                ? "no_speech"
                : null,
        };
      }
    }
    function loadScript(url) {
      return new Promise((resolve, reject) => {
        const node = document.createElement("script");
        node.src = url;
        node.onload = resolve;
        node.onerror = () => {
          node.remove();
          reject(new Error("VAD asset unavailable: " + url));
        };
        document.head.append(node);
      });
    }
    function loadRuntime() {
      return (runtimePromise ||= loadScript(assetBase + "ort.wasm.min.js")
        .then(() => loadScript(assetBase + "bundle.min.js"))
        .catch((e) => {
          runtimePromise = null;
          throw e;
        }));
    }
    class Diagnostics {
      constructor(root, { key, api, location = "" }) {
        this.key = `${key}:visitor:${location}`;
        this.api = api;
        this.location = location;
        this.epoch = null;
        this.report = {
          version: 2,
          browser: navigator.userAgent,
          location,
          events: [],
        };
        this.panel = document.createElement("details");
        this.panel.open = false;
        const title = document.createElement("summary");
        title.textContent = "Диагностика записи · бета";
        this.info = document.createElement("pre");
        this.info.style.cssText =
          "white-space:pre-wrap;overflow-wrap:anywhere;max-height:32em;overflow:auto";
        this.raw = document.createElement("pre");
        this.raw.style.cssText = this.info.style.cssText;
        this.download = document.createElement("a");
        this.download.textContent = "Скачать аудио";
        this.download.hidden = true;
        const copy = document.createElement("button");
        copy.type = "button";
        copy.textContent = "Скопировать отчёт";
        copy.onclick = async () => {
          try {
            await navigator.clipboard.writeText(
              JSON.stringify(this.report, null, 2),
            );
            copy.textContent = "Отчёт скопирован";
          } catch {
            copy.textContent = "Выделите и скопируйте отчёт ниже";
          }
        };
        const heading = document.createElement("p");
        heading.textContent = "Полный ответ сервера";
        this.panel.append(
          title,
          this.download,
          copy,
          this.info,
          heading,
          this.raw,
        );
        root.append(this.panel);
        this.origin = performance.now();
        this.disposed = false;
        this.render();
      }
      mark(name, detail = {}) {
        (this.report.timeline ||= []).push({
          name,
          ms: Math.round(performance.now() - this.origin),
          ...detail,
        });
        this.render();
      }
      reveal() {
        this.panel.open = true;
      }
      clean(text) {
        return text.replace(
          /("(?:csrf_token|_csrf|authorization|cookie|set-cookie|api_key|password)"\s*:\s*)"(?:[^"\\]|\\.)*"/gi,
          '$1"[REDACTED]"',
        );
      }
      render() {
        if (this.disposed) return;
        this.info.textContent = JSON.stringify(this.report, null, 2);
      }
      async db() {
        return await new Promise((resolve, reject) => {
          const open = indexedDB.open("involta-voice-drafts", 1);
          open.onupgradeneeded = () =>
            open.result.createObjectStore("drafts", { keyPath: "key" });
          open.onsuccess = () => resolve(open.result);
          open.onerror = () => reject(open.error);
        });
      }
      async store(mode, value) {
        const db = await this.db();
        try {
          return await new Promise((resolve, reject) => {
            const tx = db.transaction(
              "drafts",
              mode === "get" ? "readonly" : "readwrite",
            );
            const records = tx.objectStore("drafts");
            const operation =
              mode === "put" ? records.put(value) : records.get(this.key);
            if (mode === "delete" || mode === "block")
              operation.onsuccess = () => {
                if (operation.result?.id !== value) return;
                if (mode === "delete") records.delete(this.key);
                else records.put({ ...operation.result, blocked: true });
              };
            tx.oncomplete = () => resolve(operation.result);
            tx.onerror = () => reject(tx.error);
            tx.onabort = () => reject(tx.error);
          });
        } finally {
          db.close();
        }
      }
      async restore() {
        try {
          const saved = await this.store("get");
          if (saved?.blob) {
            this.showAudio(saved.blob, saved.id);
            this.report.draft = {
              id: saved.id,
              bytes: saved.blob.size,
              mime: saved.blob.type,
              durationMs: saved.durationMs ?? saved.seconds * 1000,
              local_copy_saved: true,
            };
            this.render();
          }
          return saved;
        } catch (e) {
          this.report.storage_error = e.message;
          this.render();
          return null;
        }
      }
      showAudio(blob, id) {
        if (this.disposed) return;
        if (this.audioURL) URL.revokeObjectURL(this.audioURL);
        this.audioURL = URL.createObjectURL(blob);
        this.download.href = this.audioURL;
        this.download.download = `voice-${id}.${blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm"}`;
        this.download.hidden = false;
      }
      async save(blob, id, durationMs, epoch) {
        this.epoch = epoch;
        this.showAudio(blob, id);
        this.report.draft = {
          id,
          bytes: blob.size,
          mime: blob.type,
          durationMs,
          content_epoch: epoch,
          local_copy_saved: false,
        };
        try {
          await this.store("put", {
            key: this.key,
            id,
            blob,
            seconds: durationMs / 1000,
            durationMs,
            location: this.location,
            content_epoch: epoch,
            updated: Date.now(),
          });
          this.report.draft.local_copy_saved = true;
        } catch (e) {
          this.report.storage_error = e.message;
        }
        this.render();
      }
      async clear(id) {
        try {
          await this.store("delete", id);
        } catch (e) {
          this.report.storage_error = e.message;
        }
        if (this.audioURL) URL.revokeObjectURL(this.audioURL);
        this.audioURL = null;
        this.download.hidden = true;
        this.report.draft = null;
        this.render();
      }
      async block(id) {
        try {
          await this.store("block", id);
        } catch (e) {
          this.report.storage_error = e.message;
        }
        if (this.report.draft?.id === id) this.report.draft.blocked = true;
        this.render();
      }
      begin() {
        this.id = crypto.randomUUID();
        this.report.request_id = this.id;
        this.report.events = [];
        this.render();
      }
      async request(url, options = {}) {
        const start = performance.now();
        const entry = {
          request_id: this.id,
          url: url.toString(),
          method: options.method || "GET",
          started: new Date().toISOString(),
        };
        this.report.events.push(entry);
        this.render();
        const headers = new Headers(options.headers || {});
        headers.set("X-Voice-Request-ID", this.id);
        try {
          const response = await fetch(url, {
            signal: AbortSignal.timeout(90000),
            ...options,
            headers,
          });
          const body = await response.text();
          entry.elapsed_ms = Math.round(performance.now() - start);
          entry.status = response.status;
          entry.status_text = response.statusText;
          entry.final_url = response.url;
          entry.response_headers = Object.fromEntries(
            [...response.headers].filter(
              ([key]) => !/cookie|authorization|csrf/i.test(key),
            ),
          );
          entry.response_body = this.clean(body);
          this.raw.textContent = entry.response_body;
          this.render();
          let data;
          try {
            data = JSON.parse(body);
          } catch {
            throw new Error(
              `Сервер вернул неожиданный ответ (HTTP ${response.status}). Запись сохранена; полный ответ — в диагностике ниже.`,
            );
          }
          if (!response.ok) {
            const error = new Error(
              (typeof data.error === "string"
                ? data.error
                : data.error?.message) ||
                (typeof data.detail === "string"
                  ? data.detail
                  : data.detail?.message ||
                    `Не удалось отправить запись (HTTP ${response.status}).`),
            );
            error.code = data.error?.code || data.detail?.code;
            error.status = response.status;
            throw error;
          }
          return data;
        } catch (error) {
          entry.elapsed_ms = Math.round(performance.now() - start);
          entry.exception = { name: error.name, message: error.message };
          this.render();
          throw error;
        }
      }
      async publish() {
        if (!this.id) return;
        try {
          const response = await fetch(
            `${this.api}/debug/${this.id}?location=${encodeURIComponent(this.location)}&content_epoch=${this.epoch || 0}`,
            {
              method: "POST",
              credentials: "same-origin",
              signal: AbortSignal.timeout(5000),
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ ...this.report, server: undefined }),
            },
          );
          this.report.diagnostics_saved = response.ok;
          this.report.diagnostics_status = response.status;
        } catch (e) {
          this.report.diagnostics_saved = false;
          this.report.diagnostics_error = e.message;
        }
        this.render();
      }
      async serverTrace() {
        if (!this.id) return;
        try {
          const r = await fetch(
            `${this.api}/debug/${this.id}?location=${encodeURIComponent(this.location)}&content_epoch=${this.epoch || 0}`,
            {
              credentials: "same-origin",
              signal: AbortSignal.timeout(5000),
            },
          );
          if (r.ok) this.report.server = await r.json();
          else this.report.server_trace_status = r.status;
        } catch (e) {
          this.report.server_trace_error = e.message;
        }
        this.render();
      }
      dispose() {
        this.disposed = true;
        if (this.audioURL) URL.revokeObjectURL(this.audioURL);
        this.panel.remove();
      }
    }

    function mount(target, options = {}) {
      if (typeof target === "string") target = document.getElementById(target);
      if (!target || instances.has(target)) return instances.get(target);
      const root = target.shadowRoot || target.attachShadow({ mode: "open" });
      const api = new URL(
        options.apiBase || "/guide/voice/api/v1",
        location.href,
      );
      if (api.origin !== location.origin)
        throw new Error("Voice gateway must share page origin");
      const machine = new Machine();
      let disposed = false,
        session = null,
        token = 0,
        blob,
        key,
        durationMs = 0,
        reference,
        poll,
        historyPoll,
        drag = null,
        delay = 2000,
        blocked = false;
      const style = document.createElement("style");
      style.textContent = `
        :host {
          display: block;
          inline-size: 100%;
          min-inline-size: 0;
          container-type: inline-size;
          font: 15px/1.6 Arial, Helvetica, sans-serif;
          color: #151515;
        }
        * {
          box-sizing: border-box;
        }
        [hidden] {
          display: none !important;
        }
        section {
          padding: 27px 0 42px;
          border-block-start: 1px solid #151515;
          background: #fff;
          text-align: start;
        }
        .kicker {
          margin: 0 0 19px;
          font-size: 10px;
          font-weight: 600;
          line-height: 1.6;
          letter-spacing: 2px;
          text-transform: uppercase;
        }
        h2 {
          font-size: 34px;
          font-weight: 500;
          line-height: 1.14;
          letter-spacing: -0.04em;
          max-inline-size: 620px;
          margin: 0;
          text-wrap: balance;
          overflow-wrap: anywhere;
        }
        h2 em {
          font: italic 1.06em/1 Georgia, "Times New Roman", serif;
          letter-spacing: -0.055em;
        }
        .stage {
          margin-block-start: 28px;
          display: flex;
          flex-direction: column;
          align-items: center;
        }
        .primary {
          position: relative;
          inline-size: 72px;
          block-size: 72px;
          border: 0;
          border-radius: 50%;
          background: #151515;
          color: white;
          padding: 0;
          display: grid;
          place-items: center;
          touch-action: none;
          -webkit-tap-highlight-color: transparent;
          -webkit-user-select: none;
          user-select: none;
          -webkit-touch-callout: none;
          transform: translate3d(var(--drag-x, 0px), var(--drag-y, 0px), 0);
          transition:
            transform 240ms cubic-bezier(0.2, 0.85, 0.3, 1.15),
            box-shadow 180ms ease;
          cursor: pointer;
          isolation: isolate;
        }
        .primary.dragging {
          transform: translate3d(var(--drag-x, 0px), var(--drag-y, 0px), 0) scale(1.03);
          box-shadow: 0 14px 30px #0002;
          transition: box-shadow 180ms ease;
          will-change: transform;
        }
        .primary::before {
          content: "";
          position: absolute;
          inset: -7px;
          border: 1px solid #bbb;
          border-radius: 50%;
          opacity: 0;
          transform: scale(1);
          pointer-events: none;
          transition:
            transform 0.1s,
            opacity 0.1s;
        }
        .primary.recording::before {
          opacity: 0.65;
          transform: scale(calc(1 + var(--level, 0) * 0.2));
        }
        .primary.waiting svg {
          animation: waiting 1.2s linear infinite;
        }
        .primary svg {
          width: 25px;
          height: 25px;
          fill: none;
          stroke: currentColor;
          stroke-width: 1.5;
          stroke-linecap: round;
          stroke-linejoin: round;
        }
        .primary:disabled {
          cursor: wait;
          opacity: 0.5;
        }
        .control-status {
          margin: 12px 0 0;
          font-size: 14px;
          text-align: center;
        }
        .timer {
          margin-block-start: 20px;
          font-variant-numeric: tabular-nums;
          font-size: 18px;
        }
        .hint {
          margin-block-start: 4px;
          font-size: 13px;
          color: #747474;
        }
        .cancel {
          font: inherit;
          background: none;
          border: 0;
          text-decoration: underline;
          text-underline-offset: 4px;
          cursor: pointer;
          padding: 10px 16px;
          min-block-size: 44px;
          color: inherit;
        }
        button:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }
        button:focus-visible,
        a:focus-visible,
        summary:focus-visible {
          outline: 2px solid #151515;
          outline-offset: 4px;
        }
        .status,
        .error,
        .vad-status,
        .current,
        .history {
          max-inline-size: 720px;
          margin: 12px 0 0;
          overflow-wrap: anywhere;
        }
        .error {
          color: #922626;
        }
        .vad-status {
          font-size: 13px;
          color: #666;
        }
        .history {
          text-align: start;
          white-space: pre-wrap;
        }
        .status:empty,
        .error:empty,
        .vad-status:empty,
        .current:empty,
        .history:empty {
          display: none;
        }
        .current time,
        .history time {
          color: #747474;
          font-size: 12px;
        }
        details {
          text-align: start;
          max-inline-size: 760px;
          margin: 0 0 28px;
          font-size: 13px;
        }
        summary {
          cursor: pointer;
          min-block-size: 44px;
          padding: 10px 0;
        }
        details button {
          padding: 10px;
          min-block-size: 44px;
          margin: 4px;
        }
        details a {
          display: inline-block;
          margin: 8px;
        }
        pre {
          max-inline-size: 100%;
          font-size: 12px;
        }
        @keyframes waiting {
          to {
            transform: rotate(360deg);
          }
        }
        @container (max-width: 500px) {
          section { padding-block-start: 28px; }
          h2 { font-size: 31px; line-height: 1.12; max-inline-size: 300px; }
          .stage { margin-block-start: 30px; }
        }
        @media (prefers-reduced-motion: reduce) {
          *,
          *::before {
            animation: none !important;
            transition: none !important;
          }
          .primary.recording::before {
            transform: none;
          }
          .primary,
          .primary.dragging {
            transition: none;
            box-shadow: none;
          }
          .primary.dragging {
            transform: translate3d(var(--drag-x, 0px), var(--drag-y, 0px), 0);
          }
          .primary.waiting svg {
            animation: none;
          }
        }
      `;
      const panel = document.createElement("section");
      panel.setAttribute("aria-label", "Голосовой отзыв");
      const kicker = document.createElement("p");
      kicker.className = "kicker";
      kicker.textContent = "Твой голос важен";
      const heading = document.createElement("h2");
      const emphasis = document.createElement("em");
      emphasis.textContent = "ресторане.";
      heading.append("Скажи, что ты думаешь и о каком ", emphasis);
      const stage = document.createElement("div");
      stage.className = "stage";
      const primary = document.createElement("button");
      primary.type = "button";
      primary.className = "primary";
      const controlStatus = document.createElement("p");
      controlStatus.className = "control-status";
      controlStatus.hidden = true;
      const timerText = document.createElement("span");
      timerText.className = "timer";
      timerText.setAttribute("aria-hidden", "true");
      const hint = document.createElement("span");
      hint.className = "hint";
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "cancel";
      cancel.textContent = "Отменить";
      stage.append(primary, controlStatus, timerText, hint, cancel);
      const status = document.createElement("p");
      status.className = "status";
      status.setAttribute("role", "status");
      const error = document.createElement("p");
      error.className = "error";
      error.setAttribute("role", "alert");
      const vadStatus = document.createElement("p");
      vadStatus.className = "vad-status";
      const history = document.createElement("div");
      history.className = "history";
      const currentText = document.createElement("p");
      currentText.className = "current";
      panel.append(
        kicker,
        heading,
        stage,
        status,
        error,
        vadStatus,
        currentText,
        history,
      );
      root.replaceChildren(style, panel);
      const diagnostics = new Diagnostics(root, {
        key: "voice",
        api: api.href,
        location: options.location || "",
      });
      let intentEpoch = null;
      let knownEpoch = Number(options.contentEpoch) || null;
      async function identity() {
        const response = await fetch(
          api.href +
            "/identity?location=" +
            encodeURIComponent(options.location || ""),
          {
            method: "POST",
            credentials: "same-origin",
            signal: AbortSignal.timeout(10000),
          },
        );
        const data = await response.json();
        if (
          !response.ok ||
          !data.ready ||
          !Number.isInteger(data.content_epoch)
        ) {
          const failure = new Error(
            data.detail?.message ||
              data.error?.message ||
              "Локация временно недоступна. Дождись завершения сброса.",
          );
          failure.code = data.detail?.code || data.error?.code;
          throw failure;
        }
        knownEpoch = Math.max(knownEpoch || 0, data.content_epoch);
        return knownEpoch;
      }
      async function discardStale(epoch) {
        try {
          const db = await diagnostics.db();
          try {
            await new Promise((resolve, reject) => {
              const tx = db.transaction("drafts", "readwrite");
              const store = tx.objectStore("drafts");
              const all = store.getAll();
              all.onsuccess = () => {
                for (const draft of all.result) {
                  if (
                    (draft.location === options.location ||
                      draft.key.endsWith(":" + options.location)) &&
                    (!Number.isInteger(draft.content_epoch) ||
                      draft.content_epoch < epoch)
                  )
                    store.delete(draft.key);
                }
              };
              tx.oncomplete = resolve;
              tx.onerror = () => reject(tx.error);
              tx.onabort = () => reject(tx.error);
            });
          } finally {
            db.close();
          }
        } catch (e) {
          diagnostics.report.storage_error = e.message;
          diagnostics.render();
        }
      }
      async function stale(id = key) {
        await diagnostics.clear(id);
        clearBlob();
        intentEpoch = null;
        if (disposed) return;
        error.textContent =
          "Локация сброшена. Устаревшая запись удалена из очереди браузера. Запиши новый отзыв.";
        if (machine.state !== "idle") transition("idle");
        else render();
      }
      const mic =
        '<svg aria-hidden="true" viewBox="0 0 24 24"><rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/></svg>';
      const square =
        '<svg aria-hidden="true" viewBox="0 0 28 28"><rect x="6" y="6" width="16" height="16" rx="1" fill="currentColor" stroke="none"/></svg>';
      const spinner =
        '<svg aria-hidden="true" viewBox="0 0 28 28"><path d="M24 14A10 10 0 1 1 14 4"/></svg>';
      const upload =
        '<svg aria-hidden="true" viewBox="0 0 28 28"><path d="M14 20V4M8 10l6-6 6 6M5 19v5h18v-5"/></svg>';
      function refreshAll() {
        for (const i of instances.values()) i.refresh();
      }
      function transition(next) {
        machine.move(next);
        render();
      }
      function clearBlob() {
        blob = null;
        key = null;
        durationMs = 0;
        blocked = false;
      }
      function render() {
        if (disposed) return;
        const state = machine.state,
          recording = state === "recording",
          retrying = state === "retry" && !blocked,
          waiting = ["starting", "stopping", "uploading"].includes(state);
        if (!["starting", "recording"].includes(state)) resetDrag(state);
        target.dataset.voiceState = state;
        primary.classList.toggle("recording", recording);
        primary.classList.toggle("waiting", waiting);
        primary.setAttribute(
          "aria-label",
          recording
            ? "Остановить запись"
            : state === "uploading"
              ? "Отправляем отзыв"
              : retrying
                ? "Повторить отправку"
                : "Записать отзыв",
        );
        primary.setAttribute("aria-busy", String(waiting));
        primary.disabled =
          state === "stopping" ||
          state === "uploading" ||
          !!(activeCapture && activeCapture !== instance);
        primary.setAttribute(
          "aria-disabled",
          String(waiting || primary.disabled),
        );
        const icon = recording
          ? square
          : waiting
            ? spinner
            : retrying
              ? upload
              : mic;
        const iconState = recording
          ? "stop"
          : waiting
            ? "waiting"
            : retrying
              ? "upload"
              : "microphone";
        if (primary.dataset.icon !== iconState) {
          primary.dataset.icon = iconState;
          primary.innerHTML = icon;
        }
        controlStatus.hidden = !waiting && !retrying;
        controlStatus.textContent = waiting
          ? state === "starting"
            ? "Разреши доступ к микрофону"
            : state === "uploading"
              ? "Отправляем отзыв…"
              : "Сохраняем запись"
          : retrying
            ? "Повторить отправку"
            : "";
        const elapsed =
          recording && session
            ? Math.max(0, performance.now() - session.started)
            : 0;
        const timerValue = recording
          ? `${String(Math.floor(elapsed / 60000)).padStart(2, "0")}:${String(Math.floor(elapsed / 1000) % 60).padStart(2, "0")}`
          : "";
        if (timerText.textContent !== timerValue)
          timerText.textContent = timerValue;
        timerText.hidden = !recording;
        hint.textContent = "Минимум 5 секунд";
        hint.hidden = !recording || elapsed >= MIN_MS;
        cancel.hidden = !["starting", "recording"].includes(state);
        cancel.disabled = !["starting", "recording"].includes(state);
      }
      function release(s, unlock = true) {
        clearInterval(s.timer);
        clearTimeout(s.vadTimeout);
        clearTimeout(s.finalizeTimeout);
        clearTimeout(s.analysisTimer);
        cancelAnimationFrame(s.analysisFrame);
        s.stream?.getTracks().forEach((t) => t.stop());
        s.source?.disconnect();
        if (s.vad) void destroyVad(s.vad);
        if (s.context && s.context.state !== "closed")
          void s.context.close().catch(() => {});
        if (unlock && activeCapture === instance) {
          activeCapture = null;
          refreshAll();
        }
      }
      function vadFallback(s, reason, cause = null) {
        if (session !== s || s.vadDisabled) return;
        s.vadDisabled = true;
        clearTimeout(s.vadTimeout);
        diagnostics.report.vad = {
          state: "unavailable",
          reason,
          error: cause
            ? { name: cause.name, message: cause.message, stack: cause.stack }
            : null,
        };
        diagnostics.mark("vad_unavailable", { reason });
        vadStatus.textContent =
          "Автостоп недоступен. Останови запись кнопкой «Стоп».";
        if (s.vad) void destroyVad(s.vad);
      }
      function initAnalysis(s) {
        if (session !== s || s.ending || disposed) return;
        try {
          const Audio = window.AudioContext || window.webkitAudioContext;
          if (!Audio) {
            vadFallback(s, "audio_context_unavailable");
            return;
          }
          s.context = new Audio();
          void s.context
            .resume()
            .catch((error) => vadFallback(s, "audio_context_suspended", error));
          s.source = s.context.createMediaStreamSource(s.stream);
          s.analyser = s.context.createAnalyser();
          s.analyser.fftSize = 512;
          s.source.connect(s.analyser);
          void startVad(s);
        } catch (error) {
          vadFallback(s, "audio_context_unavailable", error);
        }
      }
      async function startVad(s) {
        if (!s.context) {
          vadFallback(s, "audio_context_unavailable");
          return;
        }
        diagnostics.report.vad = {
          state: "loading",
          model: "Silero v5",
          positive: 0.3,
          negative: 0.25,
        };
        s.vadTimeout = setTimeout(() => vadFallback(s, "load_timeout"), 12000);
        try {
          await loadRuntime();
          if (session !== s || s.ending || s.vadDisabled) return;
          const vad = await window.vad.MicVAD.new({
            model: "v5",
            baseAssetPath: assetBase,
            onnxWASMBasePath: assetBase,
            audioContext: s.context,
            getStream: async () => s.stream,
            pauseStream: async () => {},
            resumeStream: async () => s.stream,
            startOnLoad: false,
            positiveSpeechThreshold: 0.3,
            negativeSpeechThreshold: 0.25,
            minSpeechMs: 400,
            ortConfig: (ort) => {
              ort.env.wasm.numThreads = 1;
              ort.env.wasm.proxy = false;
            },
            onFrameProcessed: (probabilities, frame) => {
              if (
                session !== s ||
                s.ending ||
                s.vadDisabled ||
                machine.state !== "recording"
              )
                return;
              if (s.context.state !== "running") {
                vadFallback(s, "audio_context_suspended");
                return;
              }
              const now = performance.now(),
                frameMs = frame.length / 16;
              s.firstFrame ??= now;
              s.frameClock = (s.frameClock || 0) + frameMs;
              if (
                (s.lastFrame && now - s.lastFrame > 250) ||
                Math.abs(now - s.firstFrame - s.frameClock) > 250
              ) {
                vadFallback(s, "frame_lag");
                return;
              }
              s.lastFrame = now;
              const result = s.gate.frame(
                probabilities.isSpeech,
                s.frameClock,
                frameMs,
              );
              if (result.unavailable) {
                vadFallback(s, "frame_lag");
                return;
              }
              if (!s.vadReady) {
                s.vadReady = true;
                clearTimeout(s.vadTimeout);
                diagnostics.report.vad.state = "ready";
                diagnostics.mark("vad_ready");
              }
              if (result.speech) diagnostics.mark("vad_speech_confirmed");
              if (s.gate.spoken) {
                const frameState =
                  probabilities.isSpeech >= 0.3
                    ? "speech"
                    : probabilities.isSpeech <= 0.25
                      ? "silence"
                      : "uncertain";
                if (frameState !== s.frameState)
                  diagnostics.mark("vad_" + frameState, {
                    processed_ms: s.gate.processed,
                  });
                s.frameState = frameState;
              }
              if (result.warning !== s.warning) {
                s.warning = result.warning;
                diagnostics.mark(
                  result.warning ? "vad_pause_warning" : "vad_pause_reset",
                );
              }
              vadStatus.textContent = result.warning
                ? "Пауза — сейчас запись остановится."
                : "";
              if (result.stop) stop(result.stop);
            },
          });
          if (session !== s || s.ending || s.vadDisabled) {
            await destroyVad(vad);
            return;
          }
          s.vad = vad;
          await vad.start();
        } catch (e) {
          if (session === s && !s.ending) vadFallback(s, "load_failure", e);
        }
      }
      function tick(s) {
        if (session !== s || machine.state !== "recording") return;
        render();
        if (performance.now() - s.started >= MAX_MS) {
          stop("limit");
          return;
        }
        if (
          s.vadReady &&
          (s.context?.state !== "running" ||
            performance.now() - s.lastFrame > 750)
        )
          vadFallback(s, "stream_stalled");
        if (s.analyser) {
          const values = new Float32Array(s.analyser.fftSize);
          s.analyser.getFloatTimeDomainData(values);
          const rms = Math.sqrt(
            values.reduce((sum, v) => sum + v * v, 0) / values.length,
          );
          primary.style.setProperty("--level", String(Math.min(1, rms * 8)));
        }
      }
      function canStart() {
        return (
          ["idle", "accepted", "error"].includes(machine.state) ||
          (machine.state === "retry" && blocked)
        );
      }
      async function start() {
        if (disposed || !canStart() || activeCapture) return;
        error.textContent = "";
        status.textContent = "";
        vadStatus.textContent = "";
        currentText.textContent = "";
        clearTimeout(poll);
        reference = null;
        const s = {
          token: ++token,
          id: crypto.randomUUID(),
          ending: false,
          chunks: [],
          bytes: 0,
          gate: new SilenceGate(),
          warning: false,
        };
        session = s;
        activeCapture = instance;
        diagnostics.origin = performance.now();
        diagnostics.report.timeline = [];
        diagnostics.mark("press");
        transition("starting");
        refreshAll();
        requestAnimationFrame(() => {
          if (session === s) diagnostics.mark("controls_painted");
        });
        if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
          failStart(s, "Запись недоступна. Нужен современный браузер и HTTPS.");
          return;
        }
        try {
          diagnostics.mark("permission_request");
          const microphone = navigator.mediaDevices.getUserMedia({
            audio: true,
          });
          const epochReady = identity().catch((problem) => {
            if (problem.code === "stale_epoch" || !knownEpoch)
              return { error: problem };
            return knownEpoch;
          });
          const incoming = await microphone;
          if (disposed || session !== s || s.token !== token) {
            incoming.getTracks().forEach((t) => t.stop());
            return;
          }
          diagnostics.mark("permission_resolved");
          s.stream = incoming;
          const epoch = await epochReady;
          if (disposed || session !== s || s.token !== token) {
            release(s);
            return;
          }
          if (epoch?.error) {
            failStart(s, epoch.error.message, epoch.error);
            return;
          }
          s.epoch = epoch;
          intentEpoch = epoch;
          diagnostics.epoch = epoch;
          await discardStale(epoch);
          // A late IndexedDB restore must never be overwritten by a short/cancelled new attempt.
          let saved = null;
          try {
            saved = await diagnostics.store("get");
          } catch {
            /* local storage failure is surfaced on save */
          }
          if (session !== s || disposed) {
            incoming.getTracks().forEach((t) => t.stop());
            return;
          }
          if (
            saved?.blob &&
            !blob &&
            !saved.blocked &&
            (saved.durationMs ?? saved.seconds * 1000) >= MIN_MS
          ) {
            release(s);
            session = null;
            restoreBlob(saved);
            transition("retry");
            return;
          }
          clearBlob();
          const mime = [
            "audio/webm;codecs=opus",
            "audio/mp4",
            "audio/ogg;codecs=opus",
          ].find((t) => MediaRecorder.isTypeSupported(t));
          s.recorder = new MediaRecorder(
            incoming,
            mime ? { mimeType: mime } : undefined,
          );
          incoming.getTracks().forEach((track) =>
            track.addEventListener("ended", () => {
              if (session === s && !s.ending) interrupt("track_ended");
            }),
          );
          s.recorder.ondataavailable = (event) => {
            if (session !== s || s.token !== token || !event.data.size) return;
            s.chunks.push(event.data);
            s.bytes += event.data.size;
            if (s.bytes > MAX_BYTES) stop("size");
          };
          s.recorder.onerror = (event) => {
            if (session === s) {
              diagnostics.mark("recorder_error", {
                name: event.error?.name,
                message: event.error?.message,
              });
              stop("recorder_error");
            }
          };
          s.recorder.onstart = () => {
            if (session !== s || s.token !== token || disposed) return;
            s.started = performance.now();
            diagnostics.mark("recorder_start");
            transition("recording");
            s.timer = setInterval(() => tick(s), 100);
            s.analysisFrame = requestAnimationFrame(() => {
              s.analysisTimer = setTimeout(() => initAnalysis(s), 0);
            });
          };
          s.recorder.onstop = () => {
            if (!s.ending && session === s) {
              s.ending = true;
              s.reason =
                machine.state === "recording"
                  ? "recorder_stopped"
                  : "recorder_error";
              s.duration = Math.max(0, performance.now() - s.started);
              diagnostics.mark("stop_requested", {
                reason: s.reason,
                duration_ms: s.duration,
              });
              transition("stopping");
            }
            void finalize(s);
          };
          s.started = performance.now();
          diagnostics.mark("recorder_start_requested");
          s.recorder.start(250);
        } catch (e) {
          if (session === s && s.token === token)
            failStart(
              s,
              e.name === "NotAllowedError"
                ? "Нет доступа к микрофону. Разреши запись в настройках браузера."
                : "Не удалось включить микрофон. Попробуй ещё раз.",
              e,
            );
        }
      }
      function failStart(s, message, cause = null) {
        release(s);
        session = null;
        error.textContent = message;
        diagnostics.mark("capture_error", {
          error: cause
            ? { name: cause.name, message: cause.message, stack: cause.stack }
            : null,
        });
        diagnostics.reveal();
        transition("error");
      }
      function stop(reason = "manual") {
        const s = session;
        if (
          !s ||
          s.ending ||
          !["recording", "starting"].includes(machine.state) ||
          !s.recorder
        )
          return;
        s.ending = true;
        s.reason = reason;
        s.stopped = performance.now();
        s.duration = Math.max(0, s.stopped - s.started);
        diagnostics.mark("stop_requested", { reason, duration_ms: s.duration });
        transition("stopping");
        clearInterval(s.timer);
        clearTimeout(s.vadTimeout);
        if (s.recorder.state !== "inactive") s.recorder.stop();
        s.stream.getTracks().forEach((t) => t.stop());
        s.finalizeTimeout = setTimeout(() => {
          if (session === s) {
            ++token;
            release(s);
            session = null;
            error.textContent =
              "Не удалось завершить кодирование записи. Запиши заново.";
            diagnostics.mark("encoding_timeout");
            diagnostics.reveal();
            transition("error");
          }
        }, 5000);
      }
      async function finalize(s) {
        if (s.finalized) return;
        s.finalized = true;
        release(s, false);
        diagnostics.mark("stop_completed", { reason: s.reason });
        if (session !== s || s.token !== token) {
          if (activeCapture === instance) {
            activeCapture = null;
            refreshAll();
          }
          return;
        }
        if (s.cancelled) {
          session = null;
          return;
        }
        const recorded = new Blob(s.chunks, {
          type: s.recorder.mimeType || "audio/webm",
        });
        diagnostics.mark("blob_ready", {
          bytes: recorded.size,
          mime: recorded.type,
          duration_ms: s.duration,
          reason: s.reason,
        });
        session = null;
        if (
          s.reason === "size" ||
          s.reason === "recorder_error" ||
          !Number.isFinite(s.duration) ||
          !recorded.size
        ) {
          error.textContent =
            s.reason === "size"
              ? "Запись больше 10 МБ. Запиши более короткий отзыв."
              : "Не удалось завершить аудиозапись. Запиши заново.";
          release(s);
          diagnostics.reveal();
          if (!disposed) transition("error");
          return;
        }
        if (s.duration < MIN_MS) {
          release(s);
          diagnostics.mark("too_short", { duration_ms: s.duration });
          if (!disposed) {
            status.textContent = TOO_SHORT;
            vadStatus.textContent = "";
            transition("idle");
          }
          return;
        }
        blob = recorded;
        key = s.id;
        durationMs = s.duration;
        intentEpoch = s.epoch;
        await diagnostics.save(blob, key, durationMs, s.epoch);
        release(s);
        if (disposed) return;
        vadStatus.textContent = "";
        if (["manual", "silence", "no_speech", "limit"].includes(s.reason)) {
          await send();
        } else {
          status.textContent =
            "Запись прервалась. Аудио сохранено — можно повторить отправку.";
          transition("retry");
        }
      }
      function cancelAttempt() {
        const s = session;
        ++token;
        if (s) {
          s.cancelled = true;
          s.ending = true;
          if (s.recorder?.state !== "inactive" && s.recorder) s.recorder.stop();
          release(s);
          session = null;
          diagnostics.mark("cancel");
        }
        status.textContent = "";
        error.textContent = "";
        vadStatus.textContent = "";
        transition("idle");
      }
      function interrupt(reason) {
        if (machine.state === "starting") cancelAttempt();
        else if (machine.state === "recording") stop(reason);
      }
      function primaryAction() {
        if (machine.state === "recording") stop("manual");
        else if (machine.state === "retry" && !blocked) void send();
        else if (canStart()) void start();
      }
      function resetDrag(reason = "released") {
        const previous = drag;
        drag = null;
        primary.classList.remove("dragging");
        primary.style.removeProperty("--drag-x");
        primary.style.removeProperty("--drag-y");
        if (previous && primary.hasPointerCapture(previous.id))
          primary.releasePointerCapture(previous.id);
        if (previous)
          diagnostics.mark("gesture_end", {
            reason,
            held_ms: performance.now() - previous.started,
            lift_px: previous.lift,
          });
      }
      primary.addEventListener("pointerdown", (event) => {
        if (!event.isPrimary || event.button !== 0 || primary.disabled || drag)
          return;
        if (canStart()) {
          // Only the original start gesture follows the finger. Its release must
          // never activate the stop that has appeared on this same button.
          drag = {
            id: event.pointerId,
            x: event.clientX,
            y: event.clientY,
            started: performance.now(),
            lift: 0,
            liftLimit: Math.min(
              80,
              Math.max(
                0,
                primary.getBoundingClientRect().top -
                  heading.getBoundingClientRect().bottom -
                  12,
              ),
            ),
          };
          primary.classList.add("dragging");
          try {
            primary.setPointerCapture(event.pointerId);
          } catch {
            // Synthetic activations have no native pointer to capture.
          }
        }
        primaryAction();
        if (drag)
          diagnostics.mark("gesture_start", { pointer: event.pointerType });
      });
      primary.addEventListener("pointermove", (event) => {
        if (!drag || event.pointerId !== drag.id) return;
        const dx = event.clientX - drag.x,
          dy = event.clientY - drag.y,
          verticalLimit = dy < 0 ? drag.liftLimit : 16;
        drag.lift = Math.max(drag.lift, -dy);
        // Follow the finger with resistance, within the space below the request.
        // Compact idle needs no invisible spacer to preserve the same gesture.
        primary.style.setProperty("--drag-x", `${36 * Math.tanh(dx / 36)}px`);
        primary.style.setProperty(
          "--drag-y",
          `${verticalLimit * Math.tanh(dy / verticalLimit)}px`,
        );
      });
      primary.addEventListener("pointerup", (event) => {
        if (drag?.id === event.pointerId) resetDrag();
      });
      primary.addEventListener("lostpointercapture", (event) => {
        if (drag?.id !== event.pointerId) return;
        resetDrag("capture_lost");
        interrupt("pointer_capture_lost");
      });
      primary.addEventListener("click", (event) => {
        if (event.detail === 0) primaryAction();
      });
      primary.addEventListener("keydown", (event) => {
        if (event.repeat && ["Enter", " "].includes(event.key))
          event.preventDefault();
      });
      primary.addEventListener("pointercancel", (event) => {
        if (drag && event.pointerId !== drag.id) return;
        resetDrag("pointercancel");
        interrupt("pointercancel");
      });
      cancel.onclick = cancelAttempt;
      const escape = (event) => {
        if (event.key === "Escape" && activeCapture === instance) {
          event.preventDefault();
          cancelAttempt();
        }
      };
      const visibility = () => {
        if (document.hidden && activeCapture === instance)
          interrupt("visibilitychange");
      };
      const pagehide = () => {
        if (activeCapture === instance) interrupt("pagehide");
      };
      document.addEventListener("keydown", escape);
      document.addEventListener("visibilitychange", visibility);
      window.addEventListener("pagehide", pagehide);
      async function send() {
        if (
          !blob ||
          !key ||
          blocked ||
          durationMs < MIN_MS ||
          disposed ||
          !["stopping", "retry"].includes(machine.state)
        )
          return;
        error.textContent = "";
        status.textContent = "";
        transition("uploading");
        diagnostics.begin();
        diagnostics.mark("upload_requested");
        const params = new URLSearchParams({
          location: options.location || "",
          idempotency_key: key,
          content_epoch: String(intentEpoch || 0),
          language: navigator.language || "",
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
        });
        try {
          const currentEpoch = await identity();
          if (disposed) return;
          if (!intentEpoch || currentEpoch !== intentEpoch) {
            await stale();
            return;
          }
          const data = await diagnostics.request(
            api.href + "/submissions?" + params,
            {
              method: "POST",
              credentials: "same-origin",
              body: blob,
              headers: {
                "Content-Type": blob.type || "audio/webm",
              },
            },
          );
          if (!data.reference || typeof data.reference !== "string")
            throw new Error(
              "Сервер не подтвердил сохранение. Запись остаётся в браузере.",
            );
          if (disposed) return;
          reference = data.reference;
          diagnostics.mark("upload_accepted");
          await diagnostics.publish();
          await diagnostics.clear(key);
          clearBlob();
          transition("accepted");
          status.textContent = "Запись доставлена. Расшифровываем твой отзыв.";
          delay = 2000;
          schedule();
        } catch (e) {
          if (disposed) return;
          if (e.code === "stale_epoch") {
            await stale();
            return;
          }
          blocked = e.code === "audio_too_short";
          if (blocked) void diagnostics.block(key);
          error.textContent =
            e.name === "TypeError"
              ? "Связь прервалась. Запись сохранена — повтори отправку."
              : e.message || "Не удалось отправить запись.";
          transition("retry");
          diagnostics.reveal();
          await diagnostics.publish();
          await diagnostics.serverTrace();
        }
      }
      function showReview(element, data) {
        element.replaceChildren();
        const created =
          typeof data.created_at === "string"
            ? new Date(data.created_at.replace(" ", "T"))
            : null;
        if (created && Number.isFinite(created.getTime())) {
          const time = document.createElement("time");
          time.dateTime = created.toISOString();
          time.textContent = new Intl.DateTimeFormat("ru-RU", {
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          }).format(created);
          element.append(time);
        }
        if (data.transcript) {
          if (element.childNodes.length) element.append(" · ");
          const text = document.createElement("span");
          text.textContent = data.transcript;
          element.append(text);
        }
      }
      function schedule() {
        clearTimeout(poll);
        if (!disposed) poll = setTimeout(pollStatus, delay);
      }
      async function pollStatus() {
        if (disposed || !reference) return;
        const pollingReference = reference;
        if (document.hidden) {
          schedule();
          return;
        }
        try {
          const response = await fetch(
            api.href + "/submissions/" + encodeURIComponent(pollingReference),
            { credentials: "same-origin" },
          );
          if (!response.ok) throw new Error("status");
          const data = await response.json();
          if (disposed || reference !== pollingReference) return;
          status.textContent = data.transcript
            ? "Текст отзыва сохранён."
            : "Запись доставлена. Обрабатываем.";
          showReview(currentText, data);
          if (data.state.includes("failed") || data.state.includes("unknown")) {
            status.textContent = data.transcript
              ? "Текст отзыва сохранён. Дополнительный анализ не завершён."
              : "Запись сохранена. Расшифровка не завершена; подробности в диагностике.";
            return;
          }
          if (
            ["approved", "published", "rejected", "fraud"].includes(
              data.editorial_state || data.moderation,
            )
          )
            return;
          delay = Math.min(delay * 1.5, 60000);
          schedule();
        } catch {
          if (disposed || reference !== pollingReference) return;
          delay = Math.min(delay * 2, 60000);
          schedule();
        }
      }
      async function loadHistory() {
        if (disposed) return;
        if (document.hidden) {
          historyPoll = setTimeout(loadHistory, 15000);
          return;
        }
        let ongoing = false;
        try {
          const response = await fetch(
            api.href +
              "/submissions/mine?location=" +
              encodeURIComponent(options.location || ""),
            { credentials: "same-origin" },
          );
          if (!response.ok) return;
          const data = await response.json();
          if (disposed) return;
          history.replaceChildren();
          if (data.rows?.length) {
            const title = document.createElement("h3");
            title.textContent = "Ваши отзывы";
            history.append(title);
          }
          for (const item of data.rows || []) {
            const paragraph = document.createElement("p");
            const state = item.editorial_state || item.moderation;
            showReview(paragraph, item);
            history.append(paragraph);
            ongoing ||= ![
              "approved",
              "published",
              "rejected",
              "fraud",
            ].includes(state);
          }
        } catch {
          ongoing = true;
        }
        if (ongoing && !disposed) historyPoll = setTimeout(loadHistory, 15000);
      }
      function restoreBlob(saved) {
        blob = saved.blob;
        key = saved.id;
        intentEpoch = saved.content_epoch;
        diagnostics.epoch = intentEpoch;
        durationMs = saved.durationMs ?? saved.seconds * 1000;
        blocked =
          !!saved.blocked ||
          !Number.isFinite(durationMs) ||
          durationMs < MIN_MS;
        status.textContent = blocked
          ? "Сохранённая запись не прошла проверку аудио. Её можно скачать в диагностике. Запиши новый отзыв."
          : "Восстановлена неотправленная запись. Нажми, чтобы повторить отправку.";
        if (blocked) diagnostics.reveal();
      }
      function unmount() {
        if (disposed) return;
        resetDrag("unmount");
        if (machine.state === "recording") stop("unmount");
        else if (machine.state === "starting") cancelAttempt();
        disposed = true;
        clearTimeout(poll);
        clearTimeout(historyPoll);
        document.removeEventListener("keydown", escape);
        document.removeEventListener("visibilitychange", visibility);
        window.removeEventListener("pagehide", pagehide);
        if (session) release(session);
        clearBlob();
        diagnostics.dispose();
        root.replaceChildren();
        instances.delete(target);
      }
      const instance = {
        unmount,
        refresh: render,
        get state() {
          return machine.state;
        },
      };
      instances.set(target, instance);
      render();
      loadHistory();
      const restored = diagnostics.restore();
      restored.then(async (saved) => {
        let epoch;
        try {
          epoch = await identity();
          await discardStale(epoch);
        } catch {
          epoch = knownEpoch;
        }
        if (!disposed && saved?.blob && machine.state === "idle") {
          if (
            !Number.isInteger(saved.content_epoch) ||
            (epoch && saved.content_epoch !== epoch)
          ) {
            await stale(saved.id);
            return;
          }
          restoreBlob(saved);
          transition("retry");
        }
      });
      return instance;
    }
    window.InvoltaVoice = {
      mount,
      unmount(target) {
        instances
          .get(
            typeof target === "string"
              ? document.getElementById(target)
              : target,
          )
          ?.unmount();
      },
      Machine,
      SilenceGate,
    };
  }
  if (script?.dataset.target)
    window.InvoltaVoice.mount(script.dataset.target, {
      apiBase: script.dataset.apiBase,
      location: script.dataset.location,
      contentEpoch: script.dataset.contentEpoch,
    });
})();
