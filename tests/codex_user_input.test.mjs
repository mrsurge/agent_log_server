import assert from 'node:assert/strict';
import test from 'node:test';
import { parseHTML } from 'linkedom';
import { renderRequestCard } from '../extensions/codex_ext/ui/request_cards/codex_ext_request_card.js';

async function fixture(count = 2, extra = {}) {
  const { window } = parseHTML('<html><body></body></html>');
  for (const key of ['document', 'HTMLElement', 'HTMLInputElement']) globalThis[key] = window[key];
  const body = window.document.body;
  const sent = [];
  const questions = Array.from({ length: count }, (_, i) => ({
    id: `q${i}`, header: `Question ${i}`, question: 'Choose',
    options: [{ label: 'Yes', description: 'Proceed' }, { label: 'No', description: 'Stop' }],
    ...extra,
  }));
  await renderRequestCard({ body, event: {
    request_method: 'item/tool/requestUserInput', request_params: { questions },
  }, helpers: {
    escapeHtml: String,
    submitResult: async (result) => { sent.push(result); return { ok: true }; },
  } });
  const click = async (element) => {
    element.click();
    await new Promise(resolve => setImmediate(resolve));
  };
  return { body, sent, click, window };
}

test('multiple choices wait for all questions and explicit Send', async () => {
  const { body, sent, click } = await fixture(2, { allowFreeform: false });
  const rows = body.querySelectorAll('.approval-question');
  await click(rows[0].querySelector('button'));
  assert.equal(sent.length, 0);
  await click(body.querySelector('.actions button'));
  assert.equal(sent.length, 0);
  await click(rows[1].querySelectorAll('button')[1]);
  await click(body.querySelector('.actions button'));
  assert.deepEqual(sent, [{ answers: { q0: { answers: ['Yes'] }, q1: { answers: ['No'] } } }]);
  assert.ok([...body.querySelectorAll('button')].every(button => button.disabled));
});

test('freeform replaces selection and later selection clears freeform', async () => {
  const { body, sent, click, window } = await fixture();
  const rows = body.querySelectorAll('.approval-question');
  await click(rows[0].querySelector('button'));
  const field = rows[0].querySelector('textarea');
  field.value = 'Custom';
  field.dispatchEvent(new window.Event('input'));
  const second = rows[1].querySelector('textarea');
  second.value = 'Old answer';
  second.dispatchEvent(new window.Event('input'));
  await click(rows[1].querySelector('button'));
  assert.equal(second.value, '');
  await click(body.querySelector('.actions button'));
  assert.deepEqual(sent[0], { answers: { q1: { answers: ['Yes'] }, q0: { answers: ['Custom'] } } });
});

test('single question still submits immediately', async () => {
  const { body, sent, click } = await fixture(1);
  await click(body.querySelector('.approval-option-list button'));
  assert.deepEqual(sent, [{ answers: { q0: { answers: ['Yes'] } } }]);
});
