import assert from 'node:assert/strict';
import { test, type TestContext } from 'node:test';
import type { ExecutorContext } from '@midscene/core';
import type { LoadedConfiguration } from '../agent/config.js';
import type { CollectionDevice } from '../agent/device.js';
import { assertInputScope } from '../agent/scope.js';
import { RunState, type WindowInfo } from '../agent/state.js';
import { createRegistry } from '../agent/tools.js';

/** 创建可以重叠、聚焦或属于系统的窗口事实。 */
function windowInfo(overrides: Partial<WindowInfo> = {}): WindowInfo {
  return { window_id: 1, bundle_name: 'app.allowed', process_id: 100, display_id: 0,
    z_order: 1, window_mode: 1, bounding_box: { x: 0, y: 0, width: 100, height: 100 },
    ui_node_id: 10, is_system: false, focused: true, ...overrides };
}

/** 构造已由 SDK 定位的中心点。 */
function locate(x: number, y: number) { return { center: [x, y] as [number, number] }; }

test('unrestricted tasks do not impose an application scope', () => {
  assert.doesNotThrow(() => assertInputScope('Input', {}, [], undefined));
  assert.doesNotThrow(() => assertInputScope('KeyboardPress', {}, [], undefined));
});

test('all located point actions use the highest z_order ordinary application rectangle', (t) => {
  t.mock.method(console, 'error', () => {});
  const allowed = windowInfo();
  const other = windowInfo({ window_id: 2, bundle_name: 'app.other', z_order: 8, focused: false,
    bounding_box: { x: 50, y: 0, width: 100, height: 100 } });
  for (const name of ['Tap', 'DoubleClick', 'Input', 'Scroll']) {
    assert.doesNotThrow(() => assertInputScope(name, { locate: locate(20, 20) }, [other, allowed], ['app.allowed']));
    assert.throws(() => assertInputScope(name, { locate: locate(60, 20) }, [allowed, other], ['app.allowed']),
      new RegExp(`^Error: ${name} target is outside task scope: app.other$`));
    assert.doesNotThrow(() => assertInputScope(name, { locate: locate(60, 20) }, [allowed, other], ['app.other']));
  }
});

test('system rectangles do not obscure ordinary apps but system-only regions are rejected', (t) => {
  t.mock.method(console, 'error', () => {});
  const app = windowInfo();
  const system = windowInfo({ window_id: 9, bundle_name: 'system.dock', is_system: true, z_order: 100,
    bounding_box: { x: 0, y: 0, width: 200, height: 200 } });
  assert.doesNotThrow(() => assertInputScope('Tap', { locate: locate(30, 30) }, [system, app], ['app.allowed']));
  assert.throws(() => assertInputScope('Tap', { locate: locate(150, 150) }, [app, system], ['app.allowed']),
    /outside known application window rectangles/);
});

test('drag requires both actual SDK from and to centers to belong to allowed applications', (t) => {
  t.mock.method(console, 'error', () => {});
  const app = windowInfo();
  const other = windowInfo({ window_id: 2, bundle_name: 'app.other', z_order: 2,
    bounding_box: { x: 100, y: 0, width: 100, height: 100 } });
  const windows = [app, other];
  assert.doesNotThrow(() => assertInputScope('DragAndDrop', { from: locate(20, 20), to: locate(40, 40) },
    windows, ['app.allowed']));
  for (const parameters of [{ from: locate(20, 20), to: locate(120, 20) },
    { from: locate(120, 20), to: locate(20, 20) }]) {
    assert.throws(() => assertInputScope('DragAndDrop', parameters, windows, ['app.allowed']), /outside task scope/);
    assert.doesNotThrow(() => assertInputScope('DragAndDrop', parameters, windows, ['app.allowed', 'app.other']));
  }
});

test('scope uses rounded SDK coordinates and excludes right and bottom rectangle edges', (t) => {
  t.mock.method(console, 'error', () => {});
  const windows = [windowInfo({ bounding_box: { x: -20, y: -20, width: 120, height: 120 } })];
  assert.doesNotThrow(() => assertInputScope('Tap', { locate: locate(0, 0) }, windows, ['app.allowed']));
  assert.doesNotThrow(() => assertInputScope('Tap', { locate: locate(99.4, 99.4) }, windows, ['app.allowed']));
  for (const [x, y] of [[99.6, 20], [20, 99.6], [100, 20], [20, 100]]) {
    assert.throws(() => assertInputScope('Tap', { locate: locate(x, y) }, windows, ['app.allowed']),
      /outside known application window rectangles/);
  }
});

test('KeyboardPress checks actual focus even when its ignored locate points into an allowed app', (t) => {
  t.mock.method(console, 'error', () => {});
  const app = windowInfo({ focused: false });
  const other = windowInfo({ window_id: 2, bundle_name: 'app.other', focused: true, z_order: 2,
    bounding_box: { x: 100, y: 0, width: 100, height: 100 } });
  assert.throws(() => assertInputScope('KeyboardPress', { locate: locate(20, 20) }, [app, other], ['app.allowed']),
    /outside task scope: app.other/);
  assert.doesNotThrow(() => assertInputScope('KeyboardPress', {}, [app, other], ['app.other']));
});

test('KeyboardPress rejects missing or ambiguous ordinary application focus', (t) => {
  t.mock.method(console, 'error', () => {});
  for (const windows of [[windowInfo({ focused: false })],
    [windowInfo(), windowInfo({ window_id: 2 })], [windowInfo({ is_system: true })]]) {
    assert.throws(() => assertInputScope('KeyboardPress', {}, windows, ['app.allowed']),
      /requires exactly one focused application window/);
  }
  assert.doesNotThrow(() => assertInputScope('KeyboardPress', {},
    [windowInfo(), windowInfo({ window_id: 9, is_system: true })], ['app.allowed']));
});

test('scoped Input and Scroll require locate instead of trusting unrelated focus', (t) => {
  t.mock.method(console, 'error', () => {});
  for (const name of ['Input', 'Scroll']) {
    assert.throws(() => assertInputScope(name, {}, [windowInfo()], ['app.allowed']), /requires located targets/);
  }
});

/** 使用真实注册器和替身设备验证检查发生在原生输入之前。 */
function registeredInput(t: TestContext, windows: WindowInfo[]) {
  t.mock.method(console, 'error', () => {});
  const config = { project: { run: { max_duration_seconds: 60, max_actions: 10 },
    tools: { enabled: ['Tap'] }, tasks: [{ bundle_names: ['app.allowed'] }] } } as LoadedConfiguration;
  const state = new RunState(config, 'unused');
  t.mock.method(state, 'event', () => {});
  let nativeCalls = 0;
  let nativeParameters: unknown;
  const device = { state, planningObservation: { id: 'planned', revision: 0, windows },
    windows: async () => windows,
    nativeActions: () => [{ name: 'Tap', call: async (parameters: unknown) => {
      nativeCalls += 1;
      nativeParameters = parameters;
    } }],
  } as unknown as CollectionDevice;
  const action = createRegistry(() => device).actions(state)[0];
  return { action, state, nativeCalls: () => nativeCalls, nativeParameters: () => nativeParameters };
}

test('registry rejects an out-of-scope target before native input and aborts the run', async (t) => {
  const f = registeredInput(t, [windowInfo({ bundle_name: 'app.other' })]);
  await assert.rejects(async () => f.action.call({ locate: locate(20, 20) }), /outside task scope/);
  assert.equal(f.nativeCalls(), 0);
  assert.equal(f.state.signal.aborted, true);
  assert.equal(f.state.revision, 0);
});

test('registry preserves the actual native input coordinates in planner feedback and advances the revision', async (t) => {
  const f = registeredInput(t, [windowInfo()]);
  const parameters = { locate: locate(24, 36) };
  const context = { task: {} } as ExecutorContext;
  const result = await f.action.call(parameters, context);
  assert.deepEqual(result, { completed: true, resolved_parameters: parameters });
  const feedback = JSON.parse(context.task.planningFeedback!);
  assert.deepEqual(feedback, result);
  assert.deepEqual(feedback.resolved_parameters, f.nativeParameters());
  assert.deepEqual(feedback.resolved_parameters.locate.center, [24, 36]);
  assert.equal(f.nativeCalls(), 1);
  assert.equal(f.state.signal.aborted, false);
  assert.equal(f.state.revision, 1);
});
