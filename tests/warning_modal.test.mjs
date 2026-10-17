import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { parseHTML } from 'linkedom';

test('optional warning secondary action is distinct from Cancel and resets for ordinary confirmations', async () => {
  const { window, document } = parseHTML('<html><body><div id="warning-modal" class="hidden"><h3>Confirm</h3><p id="warning-body"></p><button id="warning-close"></button><button id="warning-cancel">Cancel</button><button id="warning-confirm">Continue</button></div></body></html>');
  const previous = { window: globalThis.window, document: globalThis.document };
  Object.assign(globalThis, { window, document });
  try {
    const compiled = await build({ entryPoints: ['rust/crates/als-server/src/static/modals/warning_modal.ts'], bundle: true, format: 'esm', write: false });
    await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);
    const ctx = { helpers: {}, state: {} }; window.CodexAgentModules[0](ctx);
    const events = [];
    ctx.helpers.openWarningModal({ secondaryText: 'Open without switching',
      onSecondary: () => events.push('external'), onCancel: () => events.push('cancel') });
    const secondary = document.getElementById('warning-confirm').previousElementSibling;
    assert.equal(secondary.hidden, false); secondary.click();
    assert.deepEqual(events, ['external']);
    ctx.helpers.openWarningModal({ onConfirm: () => events.push('confirm') });
    assert.equal(secondary.hidden, true);
    assert.equal(document.getElementById('warning-confirm').textContent, 'Continue');
    document.getElementById('warning-confirm').click();
    assert.deepEqual(events, ['external', 'confirm']);
    ctx.helpers.openWarningModal({ onCancel: () => events.push('cancel') });
    document.getElementById('warning-close').click();
    assert.deepEqual(events, ['external', 'confirm', 'cancel']);
  } finally { Object.assign(globalThis, previous); }
});
