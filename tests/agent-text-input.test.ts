import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { createHash } from 'node:crypto';
import { syncBuiltinESMExports } from 'node:module';
import { test, type TestContext } from 'node:test';
import type { ExecutorContext } from '@midscene/core';
import type { LoadedConfiguration } from '../agent/config.js';
import { CollectionDevice } from '../agent/device.js';
import { RunState, type WindowInfo } from '../agent/state.js';
import { createRegistry } from '../agent/tools.js';

/** @brief 在真实标准输入边界截获正文，使用进程替身提供不含正文的验证结果。 */
function interceptBridge(t: TestContext, changes: object = {}, failure?: object) {
  const requests: any[] = [];
  const original = childProcess.spawn;
  const script = `let text='';process.stdin.on('data',part=>text+=part);process.stdin.on('end',()=>{
    const request=JSON.parse(text);const failure=JSON.parse(process.argv[2]);
    if(failure){process.stdout.write(JSON.stringify({error:failure}));process.exitCode=1;return;}
    const result={window_id:request.window_id,mode:request.mode,verified:true,
      character_count:Array.from(request.value).length,
      text_sha256:require('node:crypto').createHash('sha256').update(request.value).digest('hex'),
      ...JSON.parse(process.argv[1])};process.stdout.write(JSON.stringify({result}));});`;
  const python = `import json,sys
import agent_device
from har_capture.native_input import NativeInputError
failure=json.loads(sys.argv[1]);sys.argv=[sys.argv[0]]
def reject(connection,request):
    raise NativeInputError(failure['message'],failure['diagnostics'])
agent_device.execute_operation=reject
sys.exit(agent_device.main())`;
  const spawn = t.mock.method(childProcess, 'spawn', () => {
    const child = failure
      ? original('python', ['-B', '-c', python, JSON.stringify(failure)])
      : original(process.execPath, ['-e', script, JSON.stringify(changes), 'null']);
    const end = child.stdin.end.bind(child.stdin);
    t.mock.method(child.stdin, 'end', (data: any) => {
      requests.push(JSON.parse(String(data)));
      return end(data);
    });
    return child;
  });
  syncBuiltinESMExports();
  t.after(() => { spawn.mock.restore(); syncBuiltinESMExports(); });
  return { requests, spawn };
}

/** @brief 建立真实注册器与窗口观察，仅替换通信、键盘关闭和审计落盘。 */
function fixture(t: TestContext, changes: object = {}, failure?: object) {
  const errors = t.mock.method(console, 'error', () => {});
  const config = { project: { run: { max_duration_seconds: 60, max_actions: 100 },
    tools: { enabled: ['Input'] }, tasks: [{ bundle_names: ['app.allowed'] }] } } as LoadedConfiguration;
  const state = new RunState(config, 'unused', 'unused');
  const audit = t.mock.method(state, 'event', () => {});
  const device = new CollectionDevice({ serial: 'unused-test-device', hdc: 'unused-hdc',
    python: 'unused-python' }, state);
  const window: WindowInfo = { window_id: 1, bundle_name: 'app.allowed', process_id: 100, display_id: 0,
    z_order: 1, window_mode: 1, bounding_box: { x: 0, y: 0, width: 100, height: 100 },
    screen_bounds: { x: 0, y: 0, width: 100, height: 100 },
    ui_node_id: 10, is_system: false, focused: true };
  const current = [window];
  const windows = t.mock.method(device, 'windows', async () => current);
  const hide = t.mock.method(device, 'hideKeyboard', async () => {});
  const native = t.mock.method(device, 'nativeActions', () => { throw new Error('Unexpected SDK input'); });
  const getDevice = t.mock.fn(() => device);
  const action = createRegistry(getDevice).actions(state)[0];
  device.planningObservation = { id: 'planned', revision: 0, windows: [window] };
  const bridge = interceptBridge(t, changes, failure);
  return { action, device, state, getDevice, windows, hide, native, current, window, audit, errors, ...bridge };
}

test('model Input requires the SDK locator and exposes only the current text modes', (t) => {
  const { action } = fixture(t);
  const schema = action.paramSchema!;
  const model = { value: 'new text', locate: { prompt: 'the editor' } };
  assert.equal(action.interfaceAlias, 'aiInput');
  assert.deepEqual(schema.parse(model), { ...model, mode: 'replace', autoDismissKeyboard: false });
  assert.match(action.description!, /Append always adds at the end/);
  assert.match(action.description!, /does not navigate or submit/);
  assert.match(action.description!, /IME preedit.*KeyboardPress Escape/);
  assert.equal(schema.safeParse({ value: 'text' }).success, false);
  assert.equal(schema.safeParse({ locate: model.locate }).success, false);
  assert.equal(schema.safeParse({ ...model, value: 123 }).success, false);
  for (const mode of ['replace', 'append', 'clear']) assert.equal(schema.safeParse({ ...model, mode }).success, true);
  for (const extra of [{ mode: 'typeOnly' }, { inputStrategy: 'bulk' }, { keyboardTypeDelay: 0 }]) {
    assert.equal(schema.safeParse({ ...model, ...extra }).success, false);
  }
});

const invalidParameters = [
  { value: 'text' }, { locate: { center: [10, 20] } },
  { value: 123, locate: { center: [10, 20] } }, { value: 'text', locate: { prompt: 'unresolved editor' } },
  { value: 'text', locate: { center: [NaN, 20] } }, { value: 'text', locate: { center: [10, Infinity] } },
  { value: 'text', locate: { center: [10] } }, { value: 'text', locate: { center: [10, 20, 30] } },
  { value: 'text', mode: 'typeOnly', locate: { center: [10, 20] } },
  { value: 'text', mode: 'clear', locate: { center: [10, 20] } },
  { value: 'text', autoDismissKeyboard: 'true', locate: { center: [10, 20] } },
  { value: 'text', inputStrategy: 'bulk', locate: { center: [10, 20] } },
  { value: 'text', keyboardTypeDelay: 0, locate: { center: [10, 20] } },
];

test('invalid runtime parameters fail before device access and are audited', async (t) => {
  for (const parameters of invalidParameters) {
    await t.test(JSON.stringify(parameters), async (child) => {
      const f = fixture(child);
      await assert.rejects(async () => f.action.call(parameters));
      assert.equal(f.getDevice.mock.callCount(), 0);
      assert.equal(f.windows.mock.callCount(), 0);
      assert.equal(f.spawn.mock.callCount(), 0);
      assert.equal(f.native.mock.callCount(), 0);
      assert.equal(f.hide.mock.callCount(), 0);
      assert.equal(f.state.revision, 0);
      assert.equal(f.state.signal.aborted, true);
      assert.ok(f.audit.mock.calls.some((call) => call.arguments[0] === 'action_failed'));
    });
  }
});

test('resolved Input routes the full text to the highest app window and returns redacted evidence', async (t) => {
  const f = fixture(t);
  const top = { ...f.window, window_id: 8, bundle_name: 'app.editor', display_id: 2, z_order: 10,
    bounding_box: { x: 10, y: 20, width: 50, height: 50 }, screen_bounds: { x: 10, y: 20, width: 50, height: 50 } };
  f.state.task.bundle_names!.push('app.editor');
  f.current.push(top, { ...top, window_id: 9, is_system: true, z_order: 100 });
  f.device.planningObservation!.windows = [...f.current];
  const value = 'https://example.test/long/path?q=完整文本&space=two words&mark=😀#section';
  const locate = { center: [9.6, 20.4], id: 'resolved-editor', prompt: 'private field description' };
  const context = { task: {} } as ExecutorContext;
  const result = await f.action.call({ value, locate }, context);
  assert.deepEqual(f.requests, [{ operation: 'input_text', x: 10, y: 20, value, mode: 'replace',
    window_id: 8, bundle_name: 'app.editor', display_id: 2 }]);
  assert.deepEqual(result, { window_id: 8, mode: 'replace', verified: true,
    character_count: Array.from(value).length, text_sha256: createHash('sha256').update(value).digest('hex'),
    resolved_parameters: { value: '[redacted]', mode: 'replace', autoDismissKeyboard: false,
      locate: { center: locate.center, prompt: '[redacted]' } } });
  assert.deepEqual(JSON.parse(context.task.planningFeedback!), result);
  assert.equal(f.hide.mock.callCount(), 0);
  assert.equal(f.native.mock.callCount(), 0);
  assert.equal(f.state.revision, 1);
  assert.ok(!JSON.stringify(f.spawn.mock.calls[0].arguments).includes(value));
  for (const output of [result, f.audit.mock.calls, f.errors.mock.calls]) {
    assert.ok(!JSON.stringify(output).includes(value));
    assert.ok(!JSON.stringify(output).includes(locate.prompt));
  }
});

test('append and explicit empty values retain their requested meaning without dismissing the keyboard', async (t) => {
  for (const [mode, value] of [['append', ' added'], ['append', ''], ['replace', ''], ['clear', '']]) {
    await t.test(`${mode} ${JSON.stringify(value)}`, async (child) => {
      const f = fixture(child);
      const result = await f.action.call({ mode, value, locate: { center: [10, 20] } });
      assert.equal(f.requests[0].mode, mode);
      assert.equal(f.requests[0].value, value);
      assert.equal(result.mode, mode);
      assert.equal(f.hide.mock.callCount(), 0);
    });
  }
});

test('keyboard dismissal is opt-in and follows a successfully matched verification receipt', async (t) => {
  const f = fixture(t);
  await f.action.call({ value: 'text', autoDismissKeyboard: true, locate: { center: [10, 20] } });
  assert.equal(f.hide.mock.callCount(), 1);
  assert.equal(f.requests.length, 1);
  assert.ok(f.hide.mock.calls[0].arguments.length === 0);
});

test('invalid or mismatched receipts reject without keyboard dismissal or success revision', async (t) => {
  for (const changes of [{ window_id: 2 }, { mode: 'append' }, { mode: 'typeOnly' }, { verified: false },
    { character_count: -1 }, { text_sha256: 'not-a-digest' }, { value: 'must-not-be-returned' }]) {
    await t.test(JSON.stringify(changes), async (child) => {
      const f = fixture(child, changes);
      await assert.rejects(async () => f.action.call({ value: 'text', autoDismissKeyboard: true,
        locate: { center: [10, 20] } }));
      assert.equal(f.requests.length, 1);
      assert.equal(f.hide.mock.callCount(), 0);
      assert.equal(f.state.revision, 0);
      assert.ok(f.audit.mock.calls.some((call) => call.arguments[0] === 'action_failed'));
    });
  }
});

test('Input rejects stale observations and out-of-scope targets before the text bridge', async (t) => {
  for (const condition of ['revision', 'window moved', 'disallowed app', 'no app']) {
    await t.test(condition, async (child) => {
      const f = fixture(child);
      if (condition === 'revision') f.state.revision = 1;
      if (condition === 'window moved') f.current[0] = { ...f.window, z_order: 2 };
      if (condition === 'disallowed app') f.state.task.bundle_names = ['app.other'];
      if (condition === 'no app') f.state.task.bundle_names = undefined;
      const center = condition === 'no app' ? [100, 20] : [10, 20];
      await assert.rejects(async () => f.action.call({ value: 'text', locate: { center } }));
      assert.equal(f.spawn.mock.callCount(), 0);
      assert.equal(f.hide.mock.callCount(), 0);
    });
  }
});

test('Input rejects the UC logical-only region before the text bridge without changing allowed bundles', async (t) => {
  const f = fixture(t);
  const window = { ...f.window, window_id: 106, window_mode: 102,
    bounding_box: { x: 203, y: 203, width: 2048, height: 2230 },
    screen_bounds: { x: 203.3, y: 203.3, width: 1470.885888, height: 1601.59938 } };
  f.current.splice(0, f.current.length, window);
  f.device.planningObservation!.windows = [structuredClone(window)];
  const allowed = structuredClone(f.state.task.bundle_names);
  await assert.rejects(f.action.call({ value: 'fixture text', locate: { center: [2184, 1872] } }),
    /outside known application window rectangles/);
  assert.equal(f.spawn.mock.callCount(), 0);
  assert.equal(f.native.mock.callCount(), 0);
  assert.equal(f.hide.mock.callCount(), 0);
  assert.equal(f.state.revision, 0);
  assert.deepEqual(f.state.task.bundle_names, allowed);
});

test('Input rejects stale exact screen bounds before the text bridge', async (t) => {
  const f = fixture(t);
  const window = { ...f.window, screen_bounds: { x: 0.3, y: 0.3, width: 99.4, height: 99.4 } };
  f.device.planningObservation!.windows = [structuredClone(window)];
  f.current[0] = { ...window, screen_bounds: { ...window.screen_bounds, width: 99.3 } };
  await assert.rejects(f.action.call({ value: 'fixture text', locate: { center: [20, 20] } }),
    /changed since planning/);
  assert.equal(f.spawn.mock.callCount(), 0);
  assert.equal(f.native.mock.callCount(), 0);
  assert.equal(f.hide.mock.callCount(), 0);
  assert.equal(f.state.revision, 0);
});

test('failed native Input records exact request scope and safe phases through the nonzero bridge response', async (t) => {
  const diagnostics = { request: { x: 10, y: 20, mode: 'replace', window_id: 1,
    bundle_name: 'app.allowed', display_id: 0 }, phase: 'compare_text',
    comparison: { expected: { type: 'str', length: 18, sha256: 'a'.repeat(64) },
      actual: { type: 'NoneType', length: null, sha256: null }, equal: false, actual_equals_before: false },
    input: { status: 'failed', error_type: 'CaptureError' }, close: { status: 'completed' } };
  const f = fixture(t, {}, { message: 'Native input text does not exactly match the requested content', diagnostics });
  const parameters = { value: 'PRIVATE_INPUT_BODY', locate: { center: [9.6, 20.4], prompt: 'PRIVATE_LOCATOR' } };
  await assert.rejects(async () => f.action.call(parameters), /does not exactly match/);
  const failed = f.audit.mock.calls.find((call) => call.arguments[0] === 'action_failed')!
    .arguments[1] as Record<string, unknown>;
  assert.deepEqual(failed.diagnostics, diagnostics);
  assert.deepEqual(failed.parameters, { value: '[redacted]',
    locate: { center: [9.6, 20.4], prompt: '[redacted]' } });
  assert.equal(f.requests[0].value, parameters.value);
  assert.equal(f.requests[0].mode, 'replace');
  assert.equal(f.requests.length, 1);
  assert.equal(f.hide.mock.callCount(), 0);
  assert.equal(f.state.revision, 0);
  assert.ok(!JSON.stringify(f.audit.mock.calls).includes('PRIVATE'));
  assert.ok(!JSON.stringify(f.errors.mock.calls).includes('PRIVATE'));
});
