(() => {
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
