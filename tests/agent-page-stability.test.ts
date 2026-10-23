import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import sharp from 'sharp';
import type { LoadedConfiguration } from '../agent/config.js';
import type { CollectionAgent, CollectionDevice } from '../agent/device.js';
import { assessPageStability, verifyPageStability } from '../agent/page-stability.js';
import { RunState } from '../agent/state.js';
import { writePageDecision, writePageObservation } from './agent-page-fixture.js';

/** @brief 构造仅用于管线绑定测试的小图，不把固定响应当成真实视觉推理。 */
async function image(width: number, height: number, point?: [number, number]) {
  const pixels = Buffer.alloc(width * height * 3, 255);
  if (point) pixels.fill(0, (point[1] * width + point[0]) * 3, (point[1] * width + point[0]) * 3 + 3);
  return sharp(pixels, { raw: { width, height, channels: 3 } }).png().toBuffer();
}

async function fixture(t: TestContext, variation = 'cursor blink') {
  const directory = mkdtempSync(join(tmpdir(), 'har-gui-page-stability-test-'));
  t.after(() => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    assert.ok(basename(directory).startsWith('har-gui-page-stability-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  const before = await image(8, 6);
  const afterScreen = await image(8, 6, variation === 'background update' ? [7, 5] : [2, 1]);
  const beforeCrop = await image(4, 3);
  const afterCrop = variation === 'background update' ? beforeCrop : await image(4, 3, [2, 1]);
  const contents = { 'screenshot.png': before, 'windows.txt': Buffer.from('same windows'),
    'closing-windows.txt': Buffer.from('same windows'), 'layout.json': Buffer.from('{"page":"before"}'),
    'closing-layout.json': Buffer.from(JSON.stringify({ page: variation })) };
  const files: Record<string, any> = {};
  for (const [file, bytes] of Object.entries(contents)) {
    writeFileSync(join(directory, file), bytes);
    files[file.replaceAll('-', '_').split('.')[0]] = { file, bytes: bytes.length,
      sha256: createHash('sha256').update(bytes).digest('hex') };
  }
  files.screenshot.width = 8;
  files.screenshot.height = 6;
  const target = { window_id: 1, bundle_name: 'app.fixture', process_id: 2, display_id: 0, is_system: false,
    screen_bounds: { x: 0, y: 0, width: 4, height: 3 } };
  const metadata = { capture_id: 'synthetic-frame', display: { id: 0, width: 8, height: 6 }, windows: [target], files,
    consistency: { status: 'observed', atomic: false, pixels: { status: 'observed', changed: true } } };
  const pageObservation = writePageObservation(directory, metadata, 1,
    { before: beforeCrop, after: afterCrop, afterScreen });
  const config = { project: { run: { max_duration_seconds: 60 }, tasks: [{ name: 'fixture',
    instruction: 'Inspect the requested page', acceptance: 'The requested page remains visible' }] } } as LoadedConfiguration;
  const state = new RunState(config, directory);
  state.candidate = { path: directory, capture_id: metadata.capture_id, target_window_id: 1,
    target_bundle_name: 'app.fixture' };
  const device = { pageObservation, frozenScreenshot: undefined } as unknown as CollectionDevice;
  const calls: any[][] = [];
  const answer: { value: any } = {
    value: JSON.stringify({ decision: 'same_page', evidence: 'Mocked same-page decision' }),
  };
  const agent = { aiAsk: async (...args: any[]) => {
    calls.push(args);
    assert.equal(device.frozenScreenshot, pageObservation.after_image);
    return answer.value;
  } } as unknown as CollectionAgent;
  return { directory, metadata, state, device, agent, calls, answer, pageObservation };
}

test('paired semantic mocks allow cursor blink, same-page updates and background updates', async (t) => {
  for (const variation of ['cursor blink', 'same-page update', 'background update']) {
    await t.test(variation, async (child) => {
      const f = await fixture(child, variation);
      const original = structuredClone(f.metadata.consistency);
      await assessPageStability(f.state, f.device, f.agent, f.metadata);
      const receipt = await verifyPageStability(f.directory, f.metadata, f.state.candidate);
      assert.equal(receipt.decision, 'same_page');
      assert.equal(f.calls.length, 1);
      const call = JSON.stringify(f.calls[0]);
      assert.ok(call.includes(f.pageObservation.before_image));
      assert.ok(call.includes(f.pageObservation.after_image));
      assert.deepEqual(f.calls[0][0].images.map((reference: any) => reference.name), ['before', 'after']);
      assert.deepEqual(f.calls[0][1], { screenshotIncluded: false, domIncluded: false });
      assert.deepEqual(f.metadata.consistency, original);
      assert.equal(Object.hasOwn(receipt, 'stable'), false);
      assert.doesNotMatch(readFileSync(join(f.directory, 'page-stability.json'), 'utf8'), /data:image|before_image/);
      assert.deepEqual(readdirSync(f.directory).filter((file) => file.endsWith('.png')), ['screenshot.png']);
      assert.equal(f.device.frozenScreenshot, undefined);
      assert.equal(f.device.pageObservation, undefined);
    });
  }
});

test('paired semantic mocks reject target navigation and target body blanking', async (t) => {
  for (const decision of ['page_changed', 'body_blank']) {
    await t.test(decision, async (child) => {
      const f = await fixture(child);
      f.answer.value = JSON.stringify({ decision, evidence: 'Mocked target navigation or blank-body decision' });
      await assert.rejects(assessPageStability(f.state, f.device, f.agent, f.metadata), /page|blank/i);
      assert.equal(f.calls.length, 1);
      assert.equal(JSON.parse(readFileSync(join(f.directory, 'page-stability.json'), 'utf8')).decision, decision);
      await assert.rejects(verifyPageStability(f.directory, f.metadata, f.state.candidate), /page|blank/i);
      assert.equal(f.device.frozenScreenshot, undefined);
    });
  }
});

test('unobservable is recorded honestly without claiming a stable page or zero pixel differences', async (t) => {
  const f = await fixture(t);
  f.answer.value = JSON.stringify({ decision: 'unobservable',
    evidence: 'Mocked insufficient visibility; no stability claim' });
  await assessPageStability(f.state, f.device, f.agent, f.metadata);
  const receipt = await verifyPageStability(f.directory, f.metadata, f.state.candidate);
  assert.equal(receipt.decision, 'unobservable');
  assert.equal(Object.hasOwn(receipt, 'stable'), false);
  assert.equal(f.metadata.consistency.pixels.changed, true);
  assert.equal(f.metadata.consistency.status, 'observed');
});

test('semantic decision responses must have the exact enum and nonempty evidence fields', async (t) => {
  const answers = [null, 'same_page', { decision: 'stable', evidence: 'invalid enum' },
    { decision: 'same_page', evidence: '' }, { decision: 'same_page' },
    { decision: 'same_page', evidence: 'fixture', stable: true }];
  for (const [index, answer] of answers.entries()) {
    await t.test(`invalid response ${index}`, async (child) => {
      const f = await fixture(child);
      f.answer.value = typeof answer === 'object' ? JSON.stringify(answer) : answer;
      await assert.rejects(assessPageStability(f.state, f.device, f.agent, f.metadata));
      assert.equal(existsSync(join(f.directory, 'page-stability.json')), false);
      assert.equal(f.device.frozenScreenshot, undefined);
    });
  }
});

test('missing transient pair or a changed in-memory crop never reaches semantic assessment', async (t) => {
  for (const corrupt of ['missing pair', 'before crop', 'after crop']) {
    await t.test(corrupt, async (child) => {
      const f = await fixture(child);
      if (corrupt === 'missing pair') f.device.pageObservation = undefined;
      else f.device.pageObservation![corrupt === 'before crop' ? 'before_image' : 'after_image']
        = `data:image/png;base64,${Buffer.from('changed').toString('base64')}`;
      await assert.rejects(assessPageStability(f.state, f.device, f.agent, f.metadata));
      assert.equal(f.calls.length, 0);
      assert.equal(existsSync(join(f.directory, 'page-stability.json')), false);
    });
  }
});

test('saved semantic evidence cannot be retargeted or detached from its indexed observation', async (t) => {
  const corruptions: Record<string, (value: any) => void> = {
    'target window': (value) => { value.target.window_id = 99; },
    'target bundle': (value) => { value.target.bundle_name = 'app.other'; },
    'target process': (value) => { value.target.process_id = 99; },
    'target display': (value) => { value.target.display_id = 99; },
    'capture identity': (value) => { value.capture_id = 'other-capture'; },
    'observation digest': (value) => { value.observation_sha256 = '0'.repeat(64); },
    'rule version': (value) => { value.rule_version = 2; },
    'extra field': (value) => { value.stable = true; },
  };
  for (const [name, corrupt] of Object.entries(corruptions)) {
    await t.test(name, async (child) => {
      const f = await fixture(child);
      const receipt = writePageDecision(f.directory, f.metadata, f.metadata.capture_id);
      corrupt(receipt);
      writeFileSync(join(f.directory, 'page-stability.json'), JSON.stringify(receipt));
      await assert.rejects(verifyPageStability(f.directory, f.metadata, f.state.candidate));
    });
  }
});

test('observation file corruption and explicit target-window mismatch cannot be hidden by a valid verdict', async (t) => {
  for (const corruption of ['file bytes', 'candidate window', 'candidate bundle', 'missing receipt']) {
    await t.test(corruption, async (child) => {
      const f = await fixture(child);
      if (corruption !== 'missing receipt') writePageDecision(f.directory, f.metadata, f.metadata.capture_id);
      if (corruption === 'file bytes') writeFileSync(join(f.directory, 'page-observation.json'), '{}');
      if (corruption === 'candidate window') f.state.candidate.target_window_id = 2;
      if (corruption === 'candidate bundle') f.state.candidate.target_bundle_name = 'app.other';
      await assert.rejects(verifyPageStability(f.directory, f.metadata, f.state.candidate));
      assert.equal(f.calls.length, 0);
    });
  }
});

test('source digests and exact target geometry remain independent of a same-page verdict', async (t) => {
  const corruptions: Record<string, (value: any) => void> = {
    'before window source': (value) => { value.before.windows_sha256 = '0'.repeat(64); },
    'after layout source': (value) => { value.after.layout_sha256 = '0'.repeat(64); },
    'captured screenshot': (value) => { value.before.screenshot_sha256 = '0'.repeat(64); },
    'target physical bounds': (value) => { value.before.screen_bounds.x += 1; },
    'closing physical bounds': (value) => { value.after.screen_bounds.width += 1; },
    'crop dimensions': (value) => { value.before.crop_bounds.width += 1; },
    'nonfinite bounds': (value) => { value.before.screen_bounds.x = null; },
  };
  for (const [name, corrupt] of Object.entries(corruptions)) {
    await t.test(name, async (child) => {
      const f = await fixture(child);
      const observation = structuredClone(f.pageObservation.receipt);
      corrupt(observation);
      const bytes = Buffer.from(JSON.stringify(observation));
      writeFileSync(join(f.directory, 'page-observation.json'), bytes);
      f.metadata.files.page_observation = { file: 'page-observation.json', bytes: bytes.length,
        sha256: createHash('sha256').update(bytes).digest('hex') };
      writePageDecision(f.directory, f.metadata, f.metadata.capture_id);
      await assert.rejects(verifyPageStability(f.directory, f.metadata, f.state.candidate));
      assert.equal(f.calls.length, 0);
    });
  }
});

test('a valid crop hash cannot disguise actual PNG dimensions that differ from its receipt', async (t) => {
  const f = await fixture(t);
  const wrong = await image(3, 3);
  f.pageObservation.before_image = `data:image/png;base64,${wrong.toString('base64')}`;
  f.pageObservation.receipt.before.crop_sha256 = createHash('sha256').update(wrong).digest('hex');
  const bytes = Buffer.from(JSON.stringify(f.pageObservation.receipt));
  writeFileSync(join(f.directory, 'page-observation.json'), bytes);
  f.metadata.files.page_observation = { file: 'page-observation.json', bytes: bytes.length,
    sha256: createHash('sha256').update(bytes).digest('hex') };
  await assert.rejects(assessPageStability(f.state, f.device, f.agent, f.metadata), /dimensions/i);
  assert.equal(f.calls.length, 0);
  assert.equal(f.device.pageObservation, undefined);
});
