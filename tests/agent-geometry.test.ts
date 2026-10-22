import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { test, type TestContext } from 'node:test';
import type { LoadedConfiguration } from '../agent/config.js';
import type { CollectionDevice } from '../agent/device.js';
import { overlapFraction, validateBounds, validateDragStart } from '../agent/geometry.js';
import { RunState, type WindowInfo } from '../agent/state.js';
import { createRegistry } from '../agent/tools.js';

const SCREEN = { width: 3120, height: 2080 };

/** 创建独立于真实设备的浮动应用窗口。 */
function windowInfo(overrides: Partial<WindowInfo> = {}): WindowInfo {
  const box = overrides.bounding_box ?? { x: 100, y: 200, width: 400, height: 300 };
  return { window_id: 92, bundle_name: 'app.allowed', process_id: 202, display_id: 0,
    z_order: 10, window_mode: 102, bounding_box: box, screen_bounds: box,
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
    connection: { python: 'unused', hdc: 'unused', serial: 'fixture' },
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

/** @brief 用独立本地进程提供标题栏返回值，不执行任何真实桥接或设备命令。 */
function interceptTitleBar(t: TestContext, result: object, received = () => {}) {
  const requests: any[] = [];
  const original = childProcess.spawn;
  const script = `let data='';process.stdin.on('data',p=>data+=p);process.stdin.on('end',()=>{
    const request=JSON.parse(data);if(request.operation!=='window_title_bar')process.exit(1);
    process.stdout.write(JSON.stringify({result:JSON.parse(process.argv[1])}));});`;
  const spawn = t.mock.method(childProcess, 'spawn', () => {
    const child = original(process.execPath, ['-e', script, JSON.stringify(result)]);
    const end = child.stdin.end.bind(child.stdin);
    t.mock.method(child.stdin, 'end', (data: any) => {
      requests.push(JSON.parse(String(data)));
      received();
      return end(data);
    });
    return child;
  });
  syncBuiltinESMExports();
  t.after(() => { spawn.mock.restore(); syncBuiltinESMExports(); });
  return requests;
}

test('bounds use physical pixels and accept exact screen edges', () => {
  for (const box of [{ x: 0, y: 0, width: 3120, height: 2080 },
    { x: 3119, y: 2079, width: 1, height: 1 }, { x: 125, y: 250, width: 600, height: 450 },
    { x: 203.3, y: 203.3, width: 1470.885888, height: 1601.59938 }]) {
    assert.doesNotThrow(() => validateBounds(box, SCREEN));
  }
});

test('observed bounds reject offscreen, empty and nonfinite rectangles', async (t) => {
  const valid = { x: 100, y: 200, width: 400, height: 300 };
  const cases: Array<[string, Partial<typeof valid>]> = [
    ['negative x', { x: -1 }], ['negative y', { y: -1 }],
    ['zero width', { width: 0 }], ['zero height', { height: 0 }],
    ['negative width', { width: -1 }], ['negative height', { height: -1 }],
    ['NaN', { x: NaN }], ['infinite', { width: Infinity }],
    ['unsafe integer', { height: Number.MAX_SAFE_INTEGER + 1 }],
    ['right overflow', { x: 2721 }], ['bottom overflow', { y: 1781 }],
  ];
  for (const [name, change] of cases) {
    await t.test(name, () => assert.throws(() => validateBounds({ ...valid, ...change }, SCREEN),
      /must fit within the physical screen/));
  }
});

test('model geometry requests still require integer destination coordinates and sizes', (t) => {
  const f = fixture(t);
  for (const parameters of [{ window_id: 92, x: 203.3, y: 203 }, { window_id: 92, x: 203, y: 203.3 }]) {
    assert.equal(f.action('MoveWindow').paramSchema!.safeParse(parameters).success, false);
  }
  for (const parameters of [{ window_id: 92, width: 600.3, height: 500 },
    { window_id: 92, width: 600, height: 500.3 }]) {
    assert.equal(f.action('ResizeWindow').paramSchema!.safeParse(parameters).success, false);
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

test('drag starts use exact screen bounds and actual covering rectangles instead of WMS logical geometry', () => {
  const target = windowInfo({ bounding_box: { x: 203, y: 203, width: 2048, height: 2230 },
    screen_bounds: { x: 203.3, y: 203.3, width: 1470.885888, height: 1601.59938 } });
  assert.throws(() => validateDragStart(target, { x: 2184, y: 1872 }, [target]), /outside the target/);
  assert.doesNotThrow(() => validateDragStart(target, { x: 1674, y: 1804 }, [target]));
  assert.throws(() => validateDragStart(target, { x: 1675, y: 1804 }, [target]), /outside the target/);
  const overlay = windowInfo({ window_id: 80, z_order: 20,
    bounding_box: { x: 2500, y: 100, width: 40, height: 40 },
    screen_bounds: { x: 1660.3, y: 1790.3, width: 40.4, height: 40.4 } });
  for (const windows of [[target, overlay], [overlay, target]]) {
    assert.throws(() => validateDragStart(target, { x: 1674, y: 1804 }, windows), /covered by another application/);
  }
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

test('resize rejects an offscreen drag start even when the requested screen bounds fit', async (t) => {
  for (const box of [{ x: 3000, y: 100, width: 500, height: 300 },
    { x: 100, y: 2000, width: 400, height: 300 }]) {
    await t.test(box.x === 3000 ? 'right overflow' : 'bottom overflow', async (child) => {
      const f = fixture(child, [windowInfo({ screen_bounds: box })]);
      const sizes = child.mock.method(f.device, 'size');
      await assert.rejects(f.action('ResizeWindow').call({ window_id: 92, width: 100, height: 50 }),
        /Mouse coordinates must be safe integers within the physical screen/);
      assert.equal(sizes.mock.callCount(), 1);
      assert.equal(f.connections(), 0);
      assert.deepEqual(f.commands, []);
      assert.deepEqual(f.state.changes, []);
      assert.equal(f.state.revision, 0);
      assert.equal(f.events.at(-1)?.type, 'action_failed');
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

test('resize uses scaled border pixels and preserves fractional verified observations', async (t) => {
  const before = windowInfo({ bounding_box: { x: 203, y: 203, width: 2048, height: 2230 },
    screen_bounds: { x: 203.3, y: 203.3, width: 1470.885888, height: 1601.59938 } });
  const after = { ...before, screen_bounds: { x: 203.3, y: 203.3, width: 1599.8, height: 1700.2 } };
  const f = fixture(t, [before]);
  f.hooks.dragged = () => f.setWindows([after]);
  const result = await f.action('ResizeWindow').call({ window_id: 92, width: 1600, height: 1700 });
  assert.deepEqual(f.commands, ['uinput -M -g 1674 1804 1803 1902 800']);
  assert.deepEqual(result, { window_id: 92, before, after, bounds_changed: true });
  assert.deepEqual(result.before.bounding_box, result.after.bounding_box);
  assert.equal(result.after.screen_bounds.width, 1599.8);
  assert.equal(f.state.revision, 1);
});

test('move uses the physical title point with independent integer accessibility bounds', async (t) => {
  const before = windowInfo({ bounding_box: { x: 203, y: 203, width: 2048, height: 2230 },
    screen_bounds: { x: 203.3, y: 203.3, width: 1470.885888, height: 1601.59938 } });
  const after = { ...before, screen_bounds: { ...before.screen_bounds, x: 399.8, y: 300.2 } };
  const f = fixture(t, [before]);
  const requests = interceptTitleBar(t, { window_id: 92, drag_point: { x: 233, y: 220 },
    bounds: { x: 203, y: 203, width: 1471, height: 1601 } });
  f.hooks.dragged = () => f.setWindows([after]);
  const result = await f.action('MoveWindow').call({ window_id: 92, x: 400, y: 300 });
  assert.deepEqual(requests, [{ operation: 'window_title_bar', window_id: 92 }]);
  assert.deepEqual(f.commands, ['uinput -M -g 233 220 430 317 800']);
  assert.deepEqual(result, { window_id: 92, before, after, bounds_changed: true });
  assert.equal(result.after.screen_bounds.x, 399.8);
});

test('move rejects a rounded drag endpoint on the excluded screen edge before native input', async (t) => {
  const before = windowInfo({ screen_bounds: { x: 100.1, y: 200, width: 400, height: 300 } });
  const f = fixture(t, [before]);
  interceptTitleBar(t, { window_id: 92, drag_point: { x: 500, y: 220 } });
  const sizes = t.mock.method(f.device, 'size');
  await assert.rejects(f.action('MoveWindow').call({ window_id: 92, x: 2720, y: 200 }),
    /Mouse coordinates must be safe integers within the physical screen/);
  assert.equal(sizes.mock.callCount(), 1);
  assert.equal(f.connections(), 0);
  assert.deepEqual(f.commands, []);
  assert.deepEqual(f.state.changes, []);
  assert.equal(f.state.revision, 0);
});

test('move rejects logical-only title points and stale screen facts before native input', async (t) => {
  for (const variation of ['logical-only point', 'changed screen', 'wrong title window']) {
    await t.test(variation, async (child) => {
      const before = windowInfo({ bounding_box: { x: 203, y: 203, width: 2048, height: 2230 },
        screen_bounds: { x: 203.3, y: 203.3, width: 1470.885888, height: 1601.59938 } });
      const f = fixture(child, [before]);
      interceptTitleBar(child, { window_id: variation === 'wrong title window' ? 80 : 92,
        drag_point: variation === 'logical-only point' ? { x: 2184, y: 1872 } : { x: 233, y: 220 } }, () => {
        if (variation === 'changed screen') {
          f.setWindows([{ ...before, screen_bounds: { ...before.screen_bounds, x: 204.3 } }]);
        }
      });
      await assert.rejects(f.action('MoveWindow').call({ window_id: 92, x: 400, y: 300 }),
        /outside the target|changed since planning|title.*window|window.*identity/i);
      assert.equal(f.connections(), 0);
      assert.deepEqual(f.commands, []);
      assert.deepEqual(f.state.changes, []);
    });
  }
});

test('occlusion verifies physical partial overlap even when the WMS logical rectangles do not overlap', async (t) => {
  const target = windowInfo({ bounding_box: { x: 203, y: 203, width: 2048, height: 2230 },
    screen_bounds: { x: 203.3, y: 203.3, width: 1470.885888, height: 1601.59938 } });
  const foreground = windowInfo({ window_id: 80, z_order: 20, ui_node_id: 800,
    bounding_box: { x: 2500, y: 0, width: 2048, height: 2230 },
    screen_bounds: { x: 2000.3, y: 203.3, width: 400.4, height: 300.4 } });
  const after = { ...foreground, screen_bounds: { ...foreground.screen_bounds, x: 1500.2, y: 300.1 } };
  const f = fixture(t, [target, foreground]);
  interceptTitleBar(t, { window_id: 80, drag_point: { x: 2030, y: 220 },
    bounds: { x: 2000, y: 203, width: 401, height: 301 } });
  f.hooks.dragged = () => f.setWindows([target, after]);
  const result = await f.action('OccludeWindow').call({ window_id: 80, target_window_id: 92, x: 1500, y: 300 });
  assert.deepEqual(f.commands, ['uinput -M -g 2030 220 1530 317 800']);
  assert.equal(result.occlusion.bounding_box_overlap_fraction,
    overlapFraction(target.screen_bounds, after.screen_bounds));
  assert.equal(overlapFraction(target.bounding_box, after.bounding_box), 0);
  assert.equal(result.after.screen_bounds.x, 1500.2);
});

test('resize rejects an incorrect rounded physical result after three bounded verification reads', async (t) => {
  const before = windowInfo();
  const f = fixture(t, [before]);
  f.hooks.dragged = () => f.setWindows([{ ...before,
    screen_bounds: { x: 100, y: 200, width: 598.4, height: 350 } }]);
  const reads = t.mock.method(f.device, 'windows');
  await assert.rejects(f.action('ResizeWindow').call({ window_id: 92, width: 600, height: 350 }),
    /bounds were not reached|requested.*bounds|physical.*bounds/i);
  assert.equal(reads.mock.callCount(), 5);
  assert.equal(f.commands.length, 1);
  assert.deepEqual(f.state.changes, []);
  assert.equal(f.state.revision, 0);
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
