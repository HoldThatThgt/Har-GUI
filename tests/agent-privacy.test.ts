import assert from 'node:assert/strict';
import { test } from 'node:test';
import { safeActionParameters } from '../agent/privacy.js';

test('input feedback preserves coordinates without persisting input body', () => {
  const input = { value: 'BODY_SENTINEL', text: 'BODY_SENTINEL', locate: { center: [100, 200] } };
  const safe = safeActionParameters('Input', input);
  assert.deepEqual(safe.locate, input.locate);
  assert.ok(!JSON.stringify(safe).includes('BODY_SENTINEL'));
  assert.equal(input.value, 'BODY_SENTINEL');
});

test('input feedback omits locator prose while preserving the original execution parameters', () => {
  for (const prompt of ['BODY_SENTINEL', { prompt: 'BODY_SENTINEL', images: [{ url: 'BODY_SENTINEL' }] }]) {
    const input = { value: 'BODY_SENTINEL', mode: 'replace', locate: {
      prompt, center: [100, 200], rect: { left: 90, top: 180, width: 20, height: 40 },
    } };
    const original = structuredClone(input);
    const safe = safeActionParameters('Input', input);
    assert.ok(!JSON.stringify(safe).includes('BODY_SENTINEL'));
    assert.deepEqual(safe.locate.center, input.locate.center);
    assert.deepEqual(safe.locate.rect, input.locate.rect);
    assert.equal(safe.mode, input.mode);
    assert.equal(safe.locate.prompt, '[redacted]');
    assert.deepEqual(input, original);
    assert.notEqual(safe.locate, input.locate);
  }
});

test('tap and drag locator prose cannot copy screen content into action records', () => {
  for (const name of ['Tap', 'DragAndDrop']) {
    const input = { from: { prompt: 'BODY_SENTINEL', center: [10, 20] },
      to: { prompt: { prompt: 'BODY_SENTINEL', images: ['BODY_SENTINEL'] }, center: [30, 40] } };
    const original = structuredClone(input);
    const safe = safeActionParameters(name, input);
    assert.ok(!JSON.stringify(safe).includes('BODY_SENTINEL'));
    assert.deepEqual(safe.from.center, [10, 20]);
    assert.deepEqual(safe.to.center, [30, 40]);
    assert.deepEqual(input, original);
  }
});
