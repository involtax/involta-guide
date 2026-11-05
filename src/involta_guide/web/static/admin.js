// Local list ordering only; business validation remains on the server.
document.addEventListener('click',event=>{const button=event.target.closest('button');if(!button)return;const card=button.closest('[data-card]');if(!card)return;if(button.hasAttribute('data-remove'))card.remove();if(button.dataset.move==='-1'&&card.previousElementSibling)card.parentNode.insertBefore(card,card.previousElementSibling);if(button.dataset.move==='1'&&card.nextElementSibling)card.parentNode.insertBefore(card.nextElementSibling,card);});
// Serialization has completed before this event: disabling the submitted button
// cannot remove its name/value from moderation or other multi-action forms.
document.addEventListener('htmx:beforeSend', event => {
  const detail = event.detail;
  if (!detail || detail.requestConfig?.verb !== 'post') return;
  const form = detail.elt?.closest('form');
  if (!form || !form.closest('.admin-workspace')) return;
  const button = detail.requestConfig.triggeringEvent?.submitter
    || form.querySelector('button[type="submit"], button:not([type])');
  if (!button || button.disabled) return;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  detail.xhr.addEventListener('loadend', () => {
    button.disabled = false;
    button.removeAttribute('aria-busy');
  }, {once: true});
});

// HTMX does not swap 4xx/5xx responses. Keep the form and show a safe text notice.
function adminRequestError(event, fallback) {
  const form = event.detail?.elt?.closest('form');
  const workspace = form?.closest('.admin-workspace');
  if (!workspace) return;
  let message = fallback;
  try {
    const body = JSON.parse(event.detail.xhr.responseText);
    const detail = body.error || body.detail;
    if (typeof detail === 'string') message = detail;
  } catch { /* Proxy errors can be HTML; never inject them into the page. */ }
  let notice = workspace.querySelector('#admin-request-error');
  if (!notice) {
    notice = document.createElement('p');
    notice.id = 'admin-request-error';
    notice.className = 'notice admin-error';
    notice.setAttribute('role', 'alert');
    workspace.prepend(notice);
  }
  notice.textContent = message.slice(0, 500);
  notice.scrollIntoView({block: 'nearest'});
}
document.addEventListener('htmx:responseError', event => {
  adminRequestError(event, `Не удалось выполнить действие (HTTP ${event.detail.xhr.status}). Обновите страницу и попробуйте снова.`);
});
for (const name of ['htmx:sendError', 'htmx:timeout']) {
  document.addEventListener(name, event => adminRequestError(event, 'Не удалось связаться с сервером. Попробуйте снова.'));
}
