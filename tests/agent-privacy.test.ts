import assert from 'node:assert/strict';
import { test } from 'node:test';
import { safeActionParameters, safeFailedActionParameters } from '../agent/privacy.js';

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
      prompt, description: 'BODY_SENTINEL', center: [100, 200],
      rect: { left: 90, top: 180, width: 20, height: 40 },
    } };
    const original = structuredClone(input);
    const safe = safeActionParameters('Input', input);
    assert.ok(!JSON.stringify(safe).includes('BODY_SENTINEL'));
    assert.deepEqual(safe.locate.center, input.locate.center);
    assert.deepEqual(safe.locate.rect, input.locate.rect);
    assert.equal(safe.mode, input.mode);
    assert.equal(safe.locate.prompt, '[redacted]');
    assert.equal(safe.locate.description, '[redacted]');
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

test('invalid Input parameters cannot expose scalar bodies, invalid modes or unknown top-level fields', () => {
  for (const parameters of ['PRIVATE_BODY', ['PRIVATE_BODY'], { mode: 'PRIVATE_BODY',
    autoDismissKeyboard: 'PRIVATE_BODY', locate: 'PRIVATE_BODY', unknown: 'PRIVATE_BODY', value: 'PRIVATE_BODY' }]) {
    const original = structuredClone(parameters);
    const safe = safeActionParameters('Input', parameters);
    assert.ok(!JSON.stringify(safe).includes('PRIVATE_BODY'));
    assert.deepEqual(parameters, original);
  }
});

test('failed action geometry never persists malformed centers, rectangles or arbitrary locator attributes', () => {
  for (const name of ['Input', 'Tap', 'DragAndDrop']) {
    const parameters = { mode: 'replace', value: 'PRIVATE_BODY', unknown: 'PRIVATE_BODY',
      locate: { center: [10, 'PRIVATE_BODY'], id: 'PRIVATE_BODY', extra: 'PRIVATE_BODY',
        rect: { left: 'PRIVATE_BODY', top: 2, width: 3, height: 4 }, prompt: 'PRIVATE_BODY' },
      from: { center: [30, 40], id: 'PRIVATE_BODY' }, to: 'PRIVATE_BODY' };
    const original = structuredClone(parameters);
    const safe = safeFailedActionParameters(name, parameters);
    assert.ok(!JSON.stringify(safe).includes('PRIVATE_BODY'));
    assert.deepEqual(parameters, original);
    if (name !== 'Input') assert.deepEqual(safe.from.center, [30, 40]);
  }
});
