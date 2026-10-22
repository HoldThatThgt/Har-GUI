import assert from 'node:assert/strict';
import { test, type TestContext } from 'node:test';
import type { ExecutorContext } from '@midscene/core';
import type { LoadedConfiguration } from '../agent/config.js';
import { CollectionDevice } from '../agent/device.js';
import { harmonyKeyboardContract } from '../agent/keyboard.js';
import { RunState, type WindowInfo } from '../agent/state.js';
import { createRegistry } from '../agent/tools.js';

/** @brief 使用真实注册器和 SDK，只替换设备通信与审计落盘。 */
function fixture(t: TestContext) {
  t.mock.method(console, 'error', () => {});
  const config = { project: { run: { max_duration_seconds: 60, max_actions: 100 },
    tools: { enabled: ['KeyboardPress'] }, tasks: [{ bundle_names: ['app.allowed'] }] } } as LoadedConfiguration;
  const state = new RunState(config, 'unused');
  const audit = t.mock.method(state, 'event', () => {});
  const device = new CollectionDevice({ serial: 'unused-test-device', hdc: 'unused-hdc',
    python: 'unused-python' }, state);
  const window: WindowInfo = { window_id: 1, bundle_name: 'app.allowed', process_id: 100, display_id: 0,
    z_order: 1, window_mode: 1, bounding_box: { x: 0, y: 0, width: 100, height: 100 },
    screen_bounds: { x: 0, y: 0, width: 100, height: 100 },
    ui_node_id: 10, is_system: false, focused: true };
  const windows = t.mock.method(device, 'windows', async () => [window]);
  const keyEvents: string[][] = [];
  const hdc = { keyEvent: async (...codes: string[]) => { keyEvents.push(codes); } };
  const connection = t.mock.method(device, 'getHdc', async () =>
    hdc as Awaited<ReturnType<CollectionDevice['getHdc']>>);
  const native = t.mock.method(device, 'nativeActions', device.nativeActions.bind(device));
  const getDevice = t.mock.fn(() => device);
  const action = createRegistry(getDevice).actions(state)[0];
  device.planningObservation = { id: 'planned', revision: 0, windows: [window] };
  return { action, device, state, getDevice, windows, native, connection, keyEvents, audit };
}

test('model action exposes only the supported single-key contract on current focus', (t) => {
  const { action } = fixture(t);
  assert.equal(action.paramSchema, harmonyKeyboardContract.paramSchema);
  assert.equal(action.interfaceAlias, 'aiKeyboardPress');
  assert.deepEqual(action.sample, { keyName: 'Enter' });
  assert.match(action.description!, /currently focused/);
  assert.match(action.description!, /combinations are unsupported/);
  assert.match(action.description!, /consecutive modifier\/key presses cannot form a shortcut/);
  assert.doesNotMatch(action.description!, /Control\+A|Shift\+Enter|optional element to click/);
  assert.deepEqual(Object.keys(harmonyKeyboardContract.paramSchema.shape), ['keyName']);
  for (const keyName of ['Control+A', 'Shift+Enter', 'Meta+ArrowLeft', '+', '', ' ', 'hello', 'End', 'PageUp']) {
    assert.equal(action.paramSchema.safeParse({ keyName }).success, false, keyName);
  }
  assert.equal(action.paramSchema.safeParse({ keyName: 'Enter', locate: { prompt: 'input' } }).success, false);
});

for (const keyName of ['Control+A', 'Shift+Enter', 'Control+L', '+', '', 'hello']) {
  test(`invalid key ${JSON.stringify(keyName)} is rejected before any device access`, async (t) => {
    const f = fixture(t);
    await assert.rejects(async () => f.action.call({ keyName }));
    assert.equal(f.getDevice.mock.callCount(), 0);
    assert.equal(f.windows.mock.callCount(), 0);
    assert.equal(f.native.mock.callCount(), 0);
    assert.equal(f.connection.mock.callCount(), 0);
    assert.equal(f.state.revision, 0);
    assert.equal(f.state.signal.aborted, true);
    assert.ok(f.audit.mock.calls.some((call) => call.arguments[0] === 'action_failed'));
  });
}

test('every declared named key and alphanumeric key uses the installed SDK key mapping', async (t) => {
  const f = fixture(t);
  assert.ok(f.action.paramSchema);
  const named: Record<string, string> = { Enter: '2054', Backspace: '2055', Tab: '2049', Escape: '2070',
    Home: 'Home', ArrowUp: '2012', ArrowDown: '2013', ArrowLeft: '2014', ArrowRight: '2015',
    Space: '2050', Delete: '2071', Alt: '2045', Shift: '2047', Control: '2072', Meta: '2076',
    Back: 'Back', Power: 'Power' };
  const cases = [...Object.entries(named), ...Array.from('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz',
    (key) => [key, String(2017 + key.toUpperCase().charCodeAt(0) - 65)]),
  ...Array.from('0123456789', (key) => [key, String(2000 + Number(key))])];
  for (const [keyName, code] of cases) {
    f.device.planningObservation!.revision = f.state.revision;
    assert.equal(f.action.paramSchema.safeParse({ keyName }).success, true, keyName);
    const context = { task: {}, skipDefaultWait: true } as ExecutorContext;
    const result = await f.action.call({ keyName }, context);
    assert.deepEqual(f.keyEvents.at(-1), [code], keyName);
    assert.deepEqual(result, { completed: true, resolved_parameters: { keyName } });
    assert.deepEqual(JSON.parse(context.task.planningFeedback!), result);
  }
  assert.equal(f.keyEvents.length, cases.length);
  assert.equal(f.native.mock.callCount(), cases.length);
  assert.equal(f.windows.mock.callCount(), cases.length);
  assert.equal(f.state.revision, cases.length);
  assert.equal(f.state.signal.aborted, false);
});

test('ignored locate parameters cannot reach the backend or silently imply focus changes', async (t) => {
  const f = fixture(t);
  await assert.rejects(async () => f.action.call({ keyName: 'Enter', locate: { center: [10, 20] } }));
  assert.equal(f.getDevice.mock.callCount(), 0);
  assert.equal(f.connection.mock.callCount(), 0);
});
