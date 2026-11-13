(() => {
  document.querySelectorAll('time[data-local-time]').forEach(element => {
    const date = new Date(element.dateTime);
    if (!Number.isNaN(date.getTime())) {
      const format = element.dataset.timeFormat === 'short'
        ? {dateStyle: 'short', timeStyle: 'short'} : {timeZoneName: 'short'};
      element.textContent = date.toLocaleString('ru-RU', format);
    }
  });
  const groupPanel = document.getElementById('log-group-live');
  if (groupPanel?.dataset.active === 'true') {
    async function pollGroup() {
      try {
        const response = await fetch(groupPanel.dataset.url, {cache: 'no-store', credentials: 'same-origin'});
        if (!response.ok) throw new Error('group status unavailable');
        const {group} = await response.json();
        if (group && group.signature !== groupPanel.dataset.signature) {
          window.location.reload();
          return;
        }
        if (group && !['queued', 'running'].includes(group.status)) return;
      } catch { /* A lost connection must not stop observing the existing launch. */ }
      setTimeout(pollGroup, 10000);
    }
    setTimeout(pollGroup, 10000);
  }
  const panel = document.getElementById('log-live');
  if (!panel || panel.dataset.active !== 'true') return;
  async function poll() {
    let active = true;
    try {
      const response = await fetch(panel.dataset.url, {cache: 'no-store', credentials: 'same-origin'});
      if (!response.ok) throw new Error('status unavailable');
      const {job} = await response.json();
      panel.querySelector('[data-stage]').textContent = job.stage_label;
      panel.querySelector('[data-error]').textContent = job.error || '';
      active = ['queued', 'running'].includes(job.status);
      panel.querySelector('[data-progress]').textContent = active
        ? (job.response_id ? `Запрос принят OpenAI. Проверок состояния: ${job.poll_count}.` : 'Ожидаем отправки запроса.')
        : 'Обработка остановлена или завершена. Нажмите «Обновить полный лог ↻», чтобы увидеть все события и доступные действия.';
    } catch {
      panel.querySelector('[data-progress]').textContent = 'Не удалось обновить статус. Повторим проверку.';
    }
    if (active) setTimeout(poll, 10000);
  }
  setTimeout(poll, 10000);
})();
