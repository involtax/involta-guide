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
