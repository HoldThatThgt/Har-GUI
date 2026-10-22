import assert from 'node:assert/strict';
import { test, type TestContext } from 'node:test';
import type { LoadedConfiguration } from '../agent/config.js';
import type { CollectionDevice } from '../agent/device.js';
import { overlapFraction, validateBounds, validateDragStart } from '../agent/geometry.js';
import { RunState, type WindowInfo } from '../agent/state.js';
import { createRegistry } from '../agent/tools.js';

const SCREEN = { width: 3120, height: 2080 };

/** 创建独立于真实设备的浮动应用窗口。 */
function windowInfo(overrides: Partial<WindowInfo> = {}): WindowInfo {
  return { window_id: 92, bundle_name: 'app.allowed', process_id: 202, display_id: 0,
    z_order: 10, window_mode: 102, bounding_box: { x: 100, y: 200, width: 400, height: 300 },
    ui_node_id: 920, is_system: false, focused: true, ...overrides };
}

/** 使用真实注册器和内存设备，记录每次输入及失败事件。 */
function fixture(t: TestContext, windows = [windowInfo()]) {
  t.mock.method(console, 'error', () => {});
  const config = { project: { run: { max_duration_seconds: 60, max_actions: 10 },
    tools: { enabled: ['MoveWindow', 'ResizeWindow', 'OccludeWindow'] },
    tasks: [{ bundle_names: ['app.allowed'] }] } } as LoadedConfiguration;
  const state = new RunState(config, 'unused');
  const events: Array<{ type: string; data: any }> = [];
  t.mock.method(state, 'event', (type: string, data: object) => { events.push({ type, data }); });
  const commands: string[] = [];
  let current = structuredClone(windows);
  let connections = 0;
  const hooks = { connected: () => {}, dragged: () => {} };
  const device = { state, planningObservation: { id: 'planned', revision: 0, windows: structuredClone(windows) },
    windows: async () => structuredClone(current), size: async () => SCREEN,
    getHdc: async () => {
      connections += 1;
      hooks.connected();
      return { shell: async (command: string) => { commands.push(command); hooks.dragged(); return ''; } };
    },
  } as unknown as CollectionDevice;
  const actions = createRegistry(() => device).actions(state);
  return { device, state, events, commands, hooks,
    action: (name: string) => actions.find((action) => action.name === name)!,
    connections: () => connections,
    setWindows: (changed: WindowInfo[]) => { current = structuredClone(changed); },
  };
}

test('bounds use physical pixels and accept exact screen edges', () => {
  for (const box of [{ x: 0, y: 0, width: 3120, height: 2080 },
    { x: 3119, y: 2079, width: 1, height: 1 }, { x: 125, y: 250, width: 600, height: 450 }]) {
    assert.doesNotThrow(() => validateBounds(box, SCREEN));
  }
});

test('bounds reject offscreen, empty and nonintegral rectangles', async (t) => {
  const valid = { x: 100, y: 200, width: 400, height: 300 };
  const cases: Array<[string, Partial<typeof valid>]> = [
    ['negative x', { x: -1 }], ['negative y', { y: -1 }],
    ['zero width', { width: 0 }], ['zero height', { height: 0 }],
    ['negative width', { width: -1 }], ['negative height', { height: -1 }],
    ['fractional x', { x: 1.5 }], ['fractional y', { y: 1.5 }],
    ['fractional width', { width: 1.5 }], ['fractional height', { height: 1.5 }],
    ['NaN', { x: NaN }], ['infinite', { width: Infinity }],
    ['unsafe integer', { height: Number.MAX_SAFE_INTEGER + 1 }],
    ['right overflow', { x: 2721 }], ['bottom overflow', { y: 1781 }],
  ];
  for (const [name, change] of cases) {
    await t.test(name, () => assert.throws(() => validateBounds({ ...valid, ...change }, SCREEN),
      /must fit within the physical screen/));
  }
});

test('drag starts include the left and top edges and exclude the right and bottom edges', () => {
  const window = windowInfo();
  for (const point of [{ x: 100, y: 200 }, { x: 499, y: 499 }]) {
    assert.doesNotThrow(() => validateDragStart(window, point, [window]));
  }
  for (const point of [{ x: 99, y: 200 }, { x: 100, y: 199 }, { x: 500, y: 250 }, { x: 150, y: 500 }]) {
    assert.throws(() => validateDragStart(window, point, [window]), /outside the target/);
  }
});

test('drag starts follow ordinary application z order regardless of array order', () => {
  const target = windowInfo();
  const overlay = windowInfo({ window_id: 80, z_order: 20,
    bounding_box: { x: 400, y: 400, width: 200, height: 200 } });
  for (const windows of [[target, overlay], [overlay, target]]) {
    assert.throws(() => validateDragStart(target, { x: 499, y: 499 }, windows), /covered by another application/);
    assert.doesNotThrow(() => validateDragStart(target, { x: 101, y: 201 }, windows));
  }
  assert.doesNotThrow(() => validateDragStart(target, { x: 499, y: 499 }, [target, { ...overlay, z_order: 5 }]));
  assert.doesNotThrow(() => validateDragStart(target, { x: 499, y: 499 }, [target, { ...overlay, is_system: true }]));
});

test('overlap measures the covered fraction of the target, including touching and complete cover', () => {
  const target = { x: 0, y: 0, width: 100, height: 80 };
  const cases = [
    { box: { x: 100, y: 0, width: 20, height: 80 }, fraction: 0 },
    { box: { x: 0, y: 80, width: 100, height: 20 }, fraction: 0 },
    { box: { x: 150, y: 100, width: 20, height: 20 }, fraction: 0 },
    { box: { x: 50, y: 40, width: 100, height: 80 }, fraction: 0.25 },
    { box: { x: 25, y: 20, width: 50, height: 40 }, fraction: 0.25 },
    { box: { x: -100, y: -80, width: 300, height: 240 }, fraction: 1 },
  ];
  for (const { box, fraction } of cases) assert.equal(overlapFraction(target, box), fraction);
  assert.equal(overlapFraction(cases[4].box, target), 1);
  assert.deepEqual(target, { x: 0, y: 0, width: 100, height: 80 });
});

test('all registered geometry tools reject targets outside task scope before obtaining native input', async (t) => {
  const cases = [
    ['MoveWindow', { window_id: 92, x: 200, y: 300 }],
    ['ResizeWindow', { window_id: 92, width: 500, height: 400 }],
    ['OccludeWindow', { window_id: 92, target_window_id: 80, x: 200, y: 300 }],
  ] as const;
  for (const [name, parameters] of cases) {
    await t.test(name, async (child) => {
      const f = fixture(child, [windowInfo({ bundle_name: 'app.outside' }), windowInfo({ window_id: 80 })]);
      await assert.rejects(async () => f.action(name).call(parameters), /outside task scope/);
      assert.equal(f.connections(), 0);
      assert.deepEqual(f.commands, []);
      assert.equal(f.state.signal.aborted, true);
      assert.equal(f.state.revision, 0);
      assert.equal(f.events.at(-1)?.type, 'action_failed');
    });
  }
});

test('resize rejects fullscreen, offscreen, unchanged and covered requests before native input', async (t) => {
  const base = windowInfo();
  const cases = [
    { name: 'fullscreen', windows: [{ ...base, window_mode: 1 }], width: 600, height: 350, error: /must be floating/ },
    { name: 'offscreen', windows: [base], width: 3120, height: 350, error: /physical screen/ },
    { name: 'unchanged', windows: [base], width: 400, height: 300, error: /unchanged/ },
    { name: 'covered corner', windows: [base, windowInfo({ window_id: 80, z_order: 20,
      bounding_box: { x: 450, y: 450, width: 100, height: 100 } })],
    width: 600, height: 350, error: /covered by another application/ },
    { name: 'system target', windows: [{ ...base, is_system: true }],
      width: 600, height: 350, error: /visible application window/ },
  ];
  for (const entry of cases) {
    await t.test(entry.name, async (child) => {
      const f = fixture(child, entry.windows);
      await assert.rejects(async () => f.action('ResizeWindow').call({
        window_id: 92, width: entry.width, height: entry.height,
      }), entry.error);
      assert.equal(f.connections(), 0);
      assert.deepEqual(f.commands, []);
      assert.deepEqual(f.state.changes, []);
    });
  }
});

test('occlusion rejects identical windows, no overlap and complete cover before native input', async (t) => {
  const windows = [windowInfo(), windowInfo({ window_id: 80, z_order: 20,
    bounding_box: { x: 600, y: 200, width: 400, height: 300 } })];
  const cases = [
    { name: 'same window', parameters: { window_id: 92, target_window_id: 92, x: 200, y: 250 },
      error: /two different windows/ },
    { name: 'no overlap', parameters: { window_id: 80, target_window_id: 92, x: 700, y: 200 },
      error: /partially overlap/ },
    { name: 'touching edge', parameters: { window_id: 80, target_window_id: 92, x: 500, y: 200 },
      error: /partially overlap/ },
    { name: 'complete cover', parameters: { window_id: 80, target_window_id: 92, x: 100, y: 200 },
      error: /partially overlap/ },
  ];
  for (const entry of cases) {
    await t.test(entry.name, async (child) => {
      const f = fixture(child, windows);
      await assert.rejects(async () => f.action('OccludeWindow').call(entry.parameters), entry.error);
      assert.equal(f.connections(), 0);
      assert.deepEqual(f.commands, []);
      assert.deepEqual(f.state.changes, []);
    });
  }
});

test('resize records only the verified geometry after one native drag and two matching observations', async (t) => {
  const before = windowInfo();
  const after = windowInfo({ bounding_box: { x: 100, y: 200, width: 600, height: 350 } });
  const f = fixture(t, [before]);
  f.hooks.dragged = () => f.setWindows([after]);
  const reads = t.mock.method(f.device, 'windows');
  const result = await f.action('ResizeWindow').call({ window_id: 92, width: 600, height: 350 });
  assert.deepEqual(f.commands, ['uinput -M -g 499 499 699 549 800']);
  assert.equal(reads.mock.callCount(), 4);
  assert.deepEqual(result, { window_id: 92, before, after, bounds_changed: true });
  assert.deepEqual(f.state.changes, [result]);
  assert.equal(f.state.revision, 1);
  assert.equal(f.state.signal.aborted, false);
});

test('a replaced window after dragging cannot be reported as successful geometry', async (t) => {
  const f = fixture(t);
  f.hooks.dragged = () => f.setWindows([windowInfo({ process_id: 999,
    bounding_box: { x: 100, y: 200, width: 600, height: 350 } })]);
  await assert.rejects(async () => f.action('ResizeWindow').call({ window_id: 92, width: 600, height: 350 }),
    /disappeared or changed identity/);
  assert.equal(f.commands.length, 1);
  assert.deepEqual(f.state.changes, []);
  assert.equal(f.state.revision, 0);
  assert.equal(f.state.signal.aborted, true);
});

test('cancellation while obtaining the connection rejects before issuing the native drag', async (t) => {
  const f = fixture(t);
  f.hooks.connected = () => f.state.controller.abort(new Error('Fixture cancelled before input'));
  await assert.rejects(async () => f.action('ResizeWindow').call({ window_id: 92, width: 600, height: 350 }),
    /Fixture cancelled before input/);
  assert.deepEqual(f.commands, []);
  assert.deepEqual(f.state.changes, []);
  assert.equal(f.state.revision, 0);
});
