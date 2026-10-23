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
  const box = overrides.bounding_box ?? { x: 0, y: 0, width: 100, height: 100 };
  return { window_id: 1, bundle_name: 'app.allowed', process_id: 100, display_id: 0,
    z_order: 1, window_mode: 1, bounding_box: box, screen_bounds: box,
    ui_node_id: 10, is_system: false, focused: true, ...overrides };
}

/** 构造已由 SDK 定位的中心点。 */
function locate(x: number, y: number) { return { center: [x, y] as [number, number] }; }

/** @brief 使用真实缩放后的 UC 矩形，并刻意保留不同的 WMS 逻辑矩形。 */
function scaledWindow(): WindowInfo {
  return windowInfo({ window_id: 106, window_mode: 102,
    bounding_box: { x: 203, y: 203, width: 2048, height: 2230 },
    screen_bounds: { x: 203.3, y: 203.3, width: 1470.885888, height: 1601.59938 } });
}

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

test('point scope rejects the logical-only UC region for every located input action', (t) => {
  t.mock.method(console, 'error', () => {});
  const windows = [scaledWindow()];
  for (const name of ['Tap', 'DoubleClick', 'Input', 'Scroll']) {
    assert.throws(() => assertInputScope(name, { locate: locate(2184, 1872) }, windows, ['app.allowed']),
      /outside known application window rectangles/);
  }
  for (const parameters of [{ from: locate(500, 500), to: locate(2184, 1872) },
    { from: locate(2184, 1872), to: locate(500, 500) }]) {
    assert.throws(() => assertInputScope('DragAndDrop', parameters, windows, ['app.allowed']),
      /outside known application window rectangles/);
  }
});

test('fractional screen bounds are tested against the actual rounded SDK point', (t) => {
  t.mock.method(console, 'error', () => {});
  const windows = [scaledWindow()];
  for (const [x, y] of [[204, 204], [1674, 1804], [203.5, 300], [1674.49, 300], [300, 1804.49]]) {
    assert.doesNotThrow(() => assertInputScope('Tap', { locate: locate(x, y) }, windows, ['app.allowed']));
  }
  for (const [x, y] of [[203, 300], [300, 203], [1675, 300], [300, 1805],
    [203.49, 300], [1674.5, 300], [300, 1804.5]]) {
    assert.throws(() => assertInputScope('Tap', { locate: locate(x, y) }, windows, ['app.allowed']),
      /outside known application window rectangles/);
  }
});

test('actual background rectangles determine routing even when a higher logical rectangle covers the point', (t) => {
  t.mock.method(console, 'error', () => {});
  const target = scaledWindow();
  const background = windowInfo({ window_id: 105, bundle_name: 'app.other', z_order: 0, focused: false,
    bounding_box: { x: 2500, y: 100, width: 500, height: 300 },
    screen_bounds: { x: 2000, y: 1800, width: 500, height: 200 } });
  for (const windows of [[target, background], [background, target]]) {
    assert.throws(() => assertInputScope('Tap', { locate: locate(2184, 1872) }, windows, ['app.allowed']),
      /outside task scope: app.other/);
    assert.doesNotThrow(() => assertInputScope('Tap', { locate: locate(500, 500) }, windows, ['app.allowed']));
  }
});

test('higher actual screen rectangles cover the target regardless of logical geometry or array order', (t) => {
  t.mock.method(console, 'error', () => {});
  const target = scaledWindow();
  const overlay = windowInfo({ window_id: 105, bundle_name: 'app.other', z_order: 8, focused: false,
    bounding_box: { x: 2500, y: 100, width: 500, height: 300 },
    screen_bounds: { x: 400.3, y: 400.3, width: 200.4, height: 200.4 } });
  for (const windows of [[target, overlay], [overlay, target]]) {
    assert.throws(() => assertInputScope('Tap', { locate: locate(500, 500) }, windows, ['app.allowed']),
      /outside task scope: app.other/);
    assert.doesNotThrow(() => assertInputScope('Tap', { locate: locate(700, 700) }, windows, ['app.allowed']));
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
  const state = new RunState(config, 'unused', 'unused');
  t.mock.method(state, 'event', () => {});
  let nativeCalls = 0;
  let nativeParameters: unknown;
  let current = structuredClone(windows);
  const device = { state, planningObservation: { id: 'planned', revision: 0, windows },
    windows: async () => current,
    nativeActions: () => [{ name: 'Tap', call: async (parameters: unknown) => {
      nativeCalls += 1;
      nativeParameters = parameters;
    } }],
  } as unknown as CollectionDevice;
  const action = createRegistry(() => device).actions(state)[0];
  return { action, state, nativeCalls: () => nativeCalls, nativeParameters: () => nativeParameters,
    setWindows: (changed: WindowInfo[]) => { current = structuredClone(changed); } };
}

test('registry rejects an out-of-scope target before native input and aborts the run', async (t) => {
  const f = registeredInput(t, [windowInfo({ bundle_name: 'app.other' })]);
  await assert.rejects(async () => f.action.call({ locate: locate(20, 20) }), /outside task scope/);
  assert.equal(f.nativeCalls(), 0);
  assert.equal(f.state.signal.aborted, true);
  assert.equal(f.state.revision, 0);
});

test('registered input rejects logical-only screen points before obtaining the native action', async (t) => {
  const f = registeredInput(t, [scaledWindow()]);
  await assert.rejects(f.action.call({ locate: locate(2184, 1872) }), /outside known application window rectangles/);
  assert.equal(f.nativeCalls(), 0);
  assert.equal(f.state.revision, 0);
  assert.equal(f.state.signal.aborted, true);
});

test('an exact screen change invalidates planning even when WMS logical bounds are unchanged', async (t) => {
  const before = scaledWindow();
  const f = registeredInput(t, [before]);
  f.setWindows([{ ...before, screen_bounds: { ...before.screen_bounds, x: 204.3 } }]);
  await assert.rejects(f.action.call({ locate: locate(500, 500) }), /changed since planning/);
  assert.equal(f.nativeCalls(), 0);
  assert.equal(f.state.revision, 0);
  assert.equal(f.state.signal.aborted, true);
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
