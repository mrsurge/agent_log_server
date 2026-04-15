window.CodexAgentModules = window.CodexAgentModules || [];
window.CodexAgentModules.push((ctx) => {
  const modalEl = document.getElementById('warning-modal');
  const closeBtn = document.getElementById('warning-close');
  const cancelBtn = document.getElementById('warning-cancel');
  const confirmBtn = document.getElementById('warning-confirm');
  const bodyEl = document.getElementById('warning-body');
  const titleEl = modalEl?.querySelector('h3');

  let onConfirm = null;
  let onCancel = null;

  function close(reason = 'cancel') {
    if (!modalEl) return;
    modalEl.classList.add('hidden');
    const cancelHandler = reason === 'confirm' ? null : onCancel;
    onConfirm = null;
    onCancel = null;
    if (typeof cancelHandler === 'function') cancelHandler();
  }

  function open(opts) {
    if (!modalEl) return;
    if (titleEl && opts?.title) titleEl.textContent = opts.title;
    if (bodyEl && opts?.body) bodyEl.textContent = opts.body;
    if (confirmBtn && opts?.confirmText) confirmBtn.textContent = opts.confirmText;
    onConfirm = typeof opts?.onConfirm === 'function' ? opts.onConfirm : null;
    onCancel = typeof opts?.onCancel === 'function' ? opts.onCancel : null;
    modalEl.classList.remove('hidden');
  }

  closeBtn?.addEventListener('click', close);
  cancelBtn?.addEventListener('click', close);
  confirmBtn?.addEventListener('click', async () => {
    const handler = onConfirm;
    close('confirm');
    if (handler) await handler();
  });

  ctx.helpers.openWarningModal = open;
  ctx.helpers.closeWarningModal = close;
});
