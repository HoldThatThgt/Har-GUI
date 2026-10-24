import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test, type TestContext } from 'node:test';
import { HarmonyAgent } from '@midscene/harmony';
import { ScreenshotItem, type AgentProgressListener, type UIContext } from '@midscene/core';
import type { LoadedConfiguration } from '../agent/config.js';
import { CollectionAgent, CollectionDevice } from '../agent/device.js';
import { RunState, type Observation } from '../agent/state.js';
import { PAGE_PNG } from './agent-page-fixture.js';

/** 创建隔离目录与无设备状态；临时截图只能生成在本测试目录内。 */
function fixture(t: TestContext) {
  const parent = path.resolve(os.tmpdir());
  const directory = mkdtempSync(path.join(parent, 'har-gui-device-test-'));
  t.after(() => {
    assert.equal(path.dirname(directory), parent);
    assert.ok(path.basename(directory).startsWith('har-gui-device-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(os, 'tmpdir', () => directory);
  t.mock.method(console, 'error', () => {});
  const events: Array<{ type: string; data: any }> = [];
  const config = { project: { run: { max_duration_seconds: 60, max_actions: 10 } } } as LoadedConfiguration;
  const state = new RunState(config, directory, directory);
  t.mock.method(state, 'event', (type: string, data: object) => { events.push({ type, data }); });
  const device = new CollectionDevice({ python: 'unused', hdc: 'unused', serial: 'fixture' }, state);
  t.mock.method(device, 'windows', async () => []);
  return { device, state, directory, events };
}

/** 提供仅操作本地测试文件的 HDC 替身，绝不运行任何设备命令。 */
function mockHdc(t: TestContext, device: CollectionDevice, options: {
  captureError?: Error; receiveError?: Error; cleanupError?: Error; abortAfterCapture?: boolean;
} = {}) {
  const commands: string[] = [];
  const bytes = Buffer.from('fixture-image-bytes');
  const hdc = {
    async shell(command: string) {
      commands.push(command);
      if (command.startsWith('rm -f ')) {
        if (options.cleanupError) throw options.cleanupError;
        return '';
      }
      if (options.captureError) throw options.captureError;
      if (options.abortAfterCapture) device.state.controller.abort(new Error('Fixture aborted'));
      return `ScreenCap saved to ${command.split(' -p ')[1]}`;
    },
    async fileRecv(_remote: string, local: string) {
      await writeFile(local, bytes);
      if (options.receiveError) throw options.receiveError;
    },
  };
  t.mock.method(device, 'getHdc', async () => hdc as Awaited<ReturnType<CollectionDevice['getHdc']>>);
  return { commands, bytes };
}

/** 截取公开进度订阅回调，测试无需调用模型或 SDK 私有执行接口。 */
function mockAgent(t: TestContext, device: CollectionDevice) {
  let listener!: AgentProgressListener;
  t.mock.method(HarmonyAgent.prototype, 'addProgressListener', (callback: AgentProgressListener) => {
    listener = callback;
    return () => {};
  });
  const agent = new CollectionAgent(device, { cache: false, generateReport: false,
    persistExecutionDump: false, autoPrintReportMsg: false, modelConfig: {
      MIDSCENE_MODEL_FAMILY: 'deepseek', MIDSCENE_MODEL_NAME: 'fixture', MIDSCENE_MODEL_API_KEY: 'unused',
      MIDSCENE_MODEL_BASE_URL: 'https://model.fixture.invalid/v1',
    } });
  return { agent, emit: (phase: string, data: unknown = {}) => listener({
    scope: 'aiAct', phase, data, sequence: 1,
  }) };
}

test('observation screenshots are returned in memory and both temporary locations are cleaned', async (t) => {
  const f = fixture(t);
  const { commands, bytes } = mockHdc(t, f.device);
  const screenshot = await f.device.screenshotBase64();
  assert.equal(screenshot, `data:image/png;base64,${bytes.toString('base64')}`);
  assert.deepEqual(readdirSync(f.directory), []);
  assert.equal(commands.length, 2);
  assert.equal(commands[1], `rm -f ${commands[0].split(' -p ')[1]}`);
  assert.equal(f.events[0].type, 'observation');
  assert.equal(f.events[0].data.sha256.length, 64);
  assert.equal(f.device.latestObservation?.revision, 0);
});

test('a connection failure creates no temporary screenshot directory', async (t) => {
  const f = fixture(t);
  t.mock.method(f.device, 'getHdc', async () => { throw new Error('Connection failed'); });
  await assert.rejects(f.device.screenshotBase64(), /Connection failed/);
  assert.deepEqual(readdirSync(f.directory), []);
  assert.equal(f.device.latestObservation, undefined);
});

test('capture failure cleans the directory even when no host file was received', async (t) => {
  const f = fixture(t);
  const failure = new Error('Capture failed');
  const { commands } = mockHdc(t, f.device, { captureError: failure });
  await assert.rejects(f.device.screenshotBase64(), (error) => error === failure);
  assert.deepEqual(readdirSync(f.directory), []);
  assert.ok(commands[1].startsWith('rm -f '));
  assert.equal(f.device.latestObservation, undefined);
});

test('remote cleanup failure does not leave the received host screenshot behind', async (t) => {
  const f = fixture(t);
  const cleanupError = new Error('Remote cleanup failed');
  mockHdc(t, f.device, { cleanupError });
  await assert.rejects(f.device.screenshotBase64(), (error) => {
    assert.ok(error instanceof AggregateError);
    assert.deepEqual(error.errors, [cleanupError]);
    return true;
  });
  assert.deepEqual(readdirSync(f.directory), []);
});

test('receive and cleanup failures retain both causes while removing the partial host screenshot', async (t) => {
  const f = fixture(t);
  const receiveError = new Error('Transfer failed');
  const cleanupError = new Error('Remote cleanup failed');
  mockHdc(t, f.device, { receiveError, cleanupError });
  await assert.rejects(f.device.screenshotBase64(), (error) => {
    assert.ok(error instanceof AggregateError);
    assert.deepEqual(error.errors, [receiveError, cleanupError]);
    return true;
  });
  assert.deepEqual(readdirSync(f.directory), []);
  assert.equal(f.device.latestObservation, undefined);
});

test('observation and cleanup logs retain error types without exposing exception text', async (t) => {
  const f = fixture(t);
  const logs = t.mock.method(console, 'error', () => {});
  const receiveError = new TypeError('PRIVATE_SCREEN_CONTENT');
  const cleanupError = new Error('PRIVATE_CLEANUP_CONTENT');
  mockHdc(t, f.device, { receiveError, cleanupError });
  await assert.rejects(f.device.screenshotBase64(), (error) => {
    assert.ok(error instanceof AggregateError);
    assert.deepEqual(error.errors, [receiveError, cleanupError]);
    return true;
  });
  assert.deepEqual(logs.mock.calls.map((call) => call.arguments), [
    ['Observation failed:', 'TypeError'], ['Observation cleanup failed:', 'Error'],
  ]);
  assert.deepEqual(readdirSync(f.directory), []);
});

test('abort after native capture prevents transfer and still removes temporary files', async (t) => {
  const f = fixture(t);
  mockHdc(t, f.device, { abortAfterCapture: true });
  await assert.rejects(f.device.screenshotBase64(), /Fixture aborted/);
  assert.deepEqual(readdirSync(f.directory), []);
  assert.equal(f.device.latestObservation, undefined);
});

test('frozen acceptance screenshots respect an already aborted run', async (t) => {
  const f = fixture(t);
  f.device.frozenScreenshot = 'data:image/png;base64,fixture';
  f.state.controller.abort(new Error('Fixture cancelled'));
  await assert.rejects(f.device.screenshotBase64(), /Fixture cancelled/);
  assert.equal(f.device.latestObservation, undefined);
});

test('a frozen target crop reports its own dimensions without connecting to HDC', async (t) => {
  const f = fixture(t);
  const hdc = t.mock.method(f.device, 'getHdc', async () => { throw new Error('Unexpected device connection'); });
  f.device.frozenScreenshot = `data:image/png;base64,${PAGE_PNG.toString('base64')}`;
  assert.deepEqual(await f.device.size(), { width: 4, height: 3 });
  assert.equal(hdc.mock.callCount(), 0);
});

test('the real SDK context parser uses only the frozen target crop without live window or HDC reads', async (t) => {
  const f = fixture(t);
  const hdc = t.mock.method(f.device, 'getHdc', async () => { throw new Error('Unexpected device connection'); });
  const windows = t.mock.method(f.device, 'windows', async () => { throw new Error('Unexpected live window read'); });
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('Unexpected network request'); });
  f.device.frozenScreenshot = `data:image/png;base64,${PAGE_PNG.toString('base64')}`;
  const { agent } = mockAgent(t, f.device);
  const context = await agent.getUIContext('assert');
  assert.deepEqual(context.shotSize, { width: 4, height: 3 });
  assert.equal(hdc.mock.callCount(), 0);
  assert.equal(windows.mock.callCount(), 0);
  assert.equal(f.device.latestObservation, undefined);
  assert.equal(f.state.observations.size, 0);
});

test('execution screenshots cannot replace the observation actually sent to the planning model', async (t) => {
  const f = fixture(t);
  const { agent, emit } = mockAgent(t, f.device);
  const planned: Observation = { id: 'planned-observation', revision: 0, windows: [] };
  const execution: Observation = { id: 'execution-observation', revision: 1, windows: [] };
  let screenshotId = 'planning-screenshot';
  t.mock.method(HarmonyAgent.prototype, 'getUIContext', async () => ({
    screenshot: { id: screenshotId },
  }) as UIContext);
  f.device.latestObservation = planned;
  await agent.getUIContext();
  await emit('plan_thinking', { screenshot: { id: screenshotId }, planIndex: 0 });
  screenshotId = 'execution-screenshot';
  f.device.latestObservation = execution;
  await agent.getUIContext();
  assert.equal(f.state.observations.get(screenshotId), execution);
  assert.equal(f.device.planningObservation, planned);
  await emit('plan_thinking', { screenshot: { id: screenshotId }, planIndex: 1 });
  assert.equal(f.device.planningObservation, execution);
  assert.deepEqual(f.events.map((event) => event.type), ['planning_observation', 'planning_observation']);
  assert.equal(f.events[0].data.screenshot_id, 'planning-screenshot');
});

test('an unmapped planning screenshot clears the old binding and aborts rather than continuing', async (t) => {
  const f = fixture(t);
  const { emit } = mockAgent(t, f.device);
  f.device.planningObservation = { id: 'previous', revision: 0, windows: [] };
  await emit('plan_thinking', { screenshot: { id: 'unknown' }, planIndex: 0 });
  assert.equal(f.device.planningObservation, undefined);
  assert.equal(f.state.signal.aborted, true);
  assert.match(String(f.state.signal.reason), /Planning screenshot has no bound observation/);
});

test('each new aiAct clears its preceding planning binding', async (t) => {
  const f = fixture(t);
  const { emit } = mockAgent(t, f.device);
  f.device.planningObservation = { id: 'previous', revision: 0, windows: [] };
  await emit('start');
  assert.equal(f.device.planningObservation, undefined);
  assert.equal(f.state.signal.aborted, false);
});

test('semantic context keeps paired PNGs and reuses the validated public context timestamp', async (t) => {
  const f = fixture(t);
  const { agent } = mockAgent(t, f.device);
  const image = `data:image/png;base64,${PAGE_PNG.toString('base64')}`;
  f.device.pageObservation = { before_image: image, after_image: image,
    receipt: { after: { crop_bounds: { width: 4, height: 3 } } } } as CollectionDevice['pageObservation'];
  f.device.frozenScreenshot = image;
  const timestamp = 123456789;
  const context = { screenshot: ScreenshotItem.create(image, timestamp), shotSize: { width: 2, height: 1 },
    shrunkShotToLogicalRatio: 0.5 } as UIContext;
  const original = t.mock.method(HarmonyAgent.prototype, 'getUIContext', async () => context);
  const result = await agent.getUIContext();
  assert.equal(original.mock.callCount(), 1);
  assert.deepEqual(result.screenshotSequence?.map((frame) => frame.base64), [image, image]);
  assert.deepEqual(result.screenshotSequence?.map((frame) => frame.capturedAt), [timestamp, timestamp]);
  assert.equal(result.screenshot.base64, image);
  assert.equal(result.screenshot, result.screenshotSequence![1]);
  assert.deepEqual(result.shotSize, { width: 4, height: 3 });
  assert.equal(result.shrunkShotToLogicalRatio, 1);
});

test('ordinary planning and saved screenshot acceptance do not inherit semantic screenshot sequences', async (t) => {
  for (const phase of ['planning', 'saved screenshot']) {
    await t.test(phase, async (child) => {
      const f = fixture(child);
      const { agent } = mockAgent(child, f.device);
      const image = `data:image/png;base64,${PAGE_PNG.toString('base64')}`;
      if (phase === 'planning') {
        f.device.pageObservation = { before_image: image, after_image: image } as CollectionDevice['pageObservation'];
      } else f.device.frozenScreenshot = image;
      const context = { screenshot: ScreenshotItem.create(image, 123), shotSize: { width: 4, height: 3 },
        shrunkShotToLogicalRatio: 1 } as UIContext;
      child.mock.method(HarmonyAgent.prototype, 'getUIContext', async () => context);
      const result = await agent.getUIContext();
      assert.equal(result, context);
      assert.equal(result.screenshotSequence, undefined);
    });
  }
});

test('semantic pairing cannot bypass an error from the SDK public context validation', async (t) => {
  const f = fixture(t);
  const { agent } = mockAgent(t, f.device);
  const image = `data:image/png;base64,${PAGE_PNG.toString('base64')}`;
  f.device.pageObservation = { before_image: image, after_image: image } as CollectionDevice['pageObservation'];
  f.device.frozenScreenshot = image;
  const failure = new Error('Mocked SDK context validation failed');
  t.mock.method(HarmonyAgent.prototype, 'getUIContext', async () => { throw failure; });
  await assert.rejects(agent.getUIContext(), (error) => error === failure);
});
