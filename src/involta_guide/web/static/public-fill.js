// TEMPORARY initial fill: generation starts only on a deliberate form submission.
(() => {
  const panel = document.querySelector('[data-public-fill]');
  if (!panel) return;
  const form = panel.querySelector('form');
  const button = form.querySelector('button');
  const message = panel.querySelector('[role="status"]');
  let busy = panel.dataset.pending === 'true';
  let timer;
  const fail = error => {
    busy = false;
    button.disabled = false;
    message.textContent = error.message || 'Не удалось выполнить запрос. Попробуйте ещё раз.';
  };
  const display = data => {
    if (data.state === 'ready' || data.state === 'existing') {
      window.location.reload();
      return;
    }
    busy = data.state === 'queued' || data.state === 'running';
    button.disabled = busy;
    message.textContent = data.message;
    clearTimeout(timer);
    if (busy) timer = setTimeout(poll, 3000);
  };
  const read = async response => {
    const data = await response.json().catch(() => ({error: 'Сервис временно недоступен. Попробуйте ещё раз.'}));
    if (!response.ok) throw new Error(data.error || data.detail || 'Не удалось выполнить запрос.');
    return data;
  };
  async function poll() {
    try {
      const response = await fetch(panel.dataset.statusUrl, {cache: 'no-store'});
      display(await read(response));
    } catch (error) { fail(error); }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    button.disabled = true;
    message.textContent = 'Запускаем поиск мест…';
    try {
      const response = await fetch(form.action, {method: 'POST', headers: {'Accept': 'application/json'}});
      display(await read(response));
    } catch (error) { fail(error); }
  });
  if (busy) poll();
})();
