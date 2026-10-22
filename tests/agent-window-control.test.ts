import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { test, type TestContext } from 'node:test';
import type { ExecutorContext } from '@midscene/core';
import type { LoadedConfiguration } from '../agent/config.js';
import type { CollectionDevice } from '../agent/device.js';
import { RunState, type WindowInfo } from '../agent/state.js';
import { createRegistry } from '../agent/tools.js';

/** @brief 创建使用当前窗口字段的匿名化浮动窗口。 */
function windowInfo(changes: Partial<WindowInfo> = {}): WindowInfo {
  return { window_id: 92, bundle_name: 'app.allowed', process_id: 202, display_id: 0,
    z_order: 10, window_mode: 102, bounding_box: { x: 100, y: 100, width: 800, height: 600 },
    ui_node_id: 920, is_system: false, focused: true, ...changes };
}

/** @brief 提供来自无障碍选择器的真实控件身份格式，不从窗口边界推算按钮。 */
function windowControl(changes: object = {}) {
  return { window_id: 92, control_id: 'EnhanceCompatibleMaximizeBtn', accessibility_id: '594',
    bounds: { x: 804, y: 103, width: 78, height: 76 }, center: { x: 843, y: 141 }, ...changes };
}

/** @brief 截获真实桥接请求并以独立本地进程返回指定结果，禁止访问设备。 */
function interceptBridge(t: TestContext, result: object, received: () => void) {
  const requests: any[] = [];
  const original = childProcess.spawn;
  const script = `let text='';process.stdin.on('data',part=>text+=part);process.stdin.on('end',()=>{
    const request=JSON.parse(text);if(request.operation!=='window_size_control'){
      process.stdout.write(JSON.stringify({error:{message:'Unexpected fixture operation'}}));
      process.exitCode=1;return;}
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
  return { requests, spawn };
}

/** @brief 建立真实注册器与观察约束，所有窗口读回及点击均由内存替身执行。 */
function fixture(t: TestContext, initial = [windowInfo()], control = windowControl()) {
  t.mock.method(console, 'error', () => {});
  const config = { project: { run: { max_duration_seconds: 60, max_actions: 10 },
    tools: { enabled: ['ToggleWindowSize'] }, tasks: [{ bundle_names: ['app.allowed'] }] } } as LoadedConfiguration;
  const state = new RunState(config, 'unused');
  const audit = t.mock.method(state, 'event', () => {});
  let current = structuredClone(initial);
  const after = windowInfo({ window_mode: 1, bounding_box: { x: 0, y: 0, width: 1600, height: 1000 } });
  const hooks = { received: () => {}, clicked: () => { current = [after]; } };
  const clicks: Array<[number, number]> = [];
  const click = t.mock.fn(async (x: number, y: number) => { clicks.push([x, y]); hooks.clicked(); });
  const getHdc = t.mock.fn(async () => ({ click }));
  const device = { state, connection: { python: 'unused', hdc: 'unused', serial: 'fixture' },
    planningObservation: { id: 'planned', revision: 0, windows: structuredClone(initial) },
    windows: async () => structuredClone(current), getHdc } as unknown as CollectionDevice;
  const action = createRegistry(() => device).actions(state)[0];
  const bridge = interceptBridge(t, control, () => hooks.received());
  return { action, device, state, audit, clicks, getHdc, hooks, after, control, ...bridge,
    setWindows: (windows: WindowInfo[]) => { current = structuredClone(windows); } };
}

/** @brief 确认失败未取得原生输入连接且未写入窗口变更。 */
function assertNoClick(f: ReturnType<typeof fixture>) {
  assert.equal(f.getHdc.mock.callCount(), 0);
  assert.deepEqual(f.clicks, []);
  assert.deepEqual(f.state.changes, []);
  assert.equal(f.state.revision, 0);
  assert.equal(f.state.signal.aborted, true);
  assert.ok(f.audit.mock.calls.some((call) => call.arguments[0] === 'action_failed'));
}

test('registered window toggle clicks the resolved control and preserves actual WMS modes and bounds', async (t) => {
  const before = windowInfo();
  const f = fixture(t, [before]);
  const context = { task: {} } as ExecutorContext;
  const result = await f.action.call({ window_id: before.window_id }, context);
  assert.deepEqual(f.requests, [{ operation: 'window_size_control', window_id: before.window_id }]);
  assert.deepEqual(f.clicks, [[843, 141]]);
  assert.deepEqual(result, { window_id: before.window_id, before, after: f.after, bounds_changed: true });
  assert.equal(result.before.window_mode, 102);
  assert.equal(result.after.window_mode, 1);
  assert.deepEqual(JSON.parse(context.task.planningFeedback!), result);
  assert.deepEqual(f.state.changes, [result]);
  assert.equal(f.state.revision, 1);
  const controlEvent = f.audit.mock.calls.find((call) => call.arguments[0] === 'window_control');
  assert.deepEqual(controlEvent?.arguments[1], { window_id: 92, control: f.control });
});

test('registered window toggle rejects missing, system and out-of-scope windows before bridge lookup', async (t) => {
  const cases = [
    { name: 'missing window', windows: [windowInfo({ window_id: 80 })] },
    { name: 'system window', windows: [windowInfo({ is_system: true })] },
    { name: 'different bundle', windows: [windowInfo({ bundle_name: 'app.other' })] },
  ];
  for (const entry of cases) {
    await t.test(entry.name, async (child) => {
      const f = fixture(child, entry.windows);
      await assert.rejects(f.action.call({ window_id: 92 }), /visible application window|outside task scope/);
      assert.equal(f.spawn.mock.callCount(), 0);
      assertNoClick(f);
    });
  }
});

test('window toggle rejects a higher ordinary window over the button even when its bundle is allowed', async (t) => {
  for (const bundle of ['app.other', 'app.allowed']) {
    for (const reversed of [false, true]) {
      await t.test(`${bundle} reversed=${reversed}`, async (child) => {
        const overlay = windowInfo({ window_id: 80, bundle_name: bundle, z_order: 20,
          bounding_box: { x: 800, y: 100, width: 200, height: 200 } });
        const windows = [windowInfo(), overlay];
        const f = fixture(child, reversed ? windows.reverse() : windows);
        await assert.rejects(f.action.call({ window_id: 92 }), /window|target|covered/i);
        assert.equal(f.requests.length, 1);
        assertNoClick(f);
      });
    }
  }
});

test('window toggle rejects stale planning and changed WMS state before bridge lookup', async (t) => {
  for (const variation of ['revision', 'window state']) {
    await t.test(variation, async (child) => {
      const f = fixture(child);
      if (variation === 'revision') f.state.revision = 1;
      else f.setWindows([windowInfo({ z_order: 11 })]);
      await assert.rejects(f.action.call({ window_id: 92 }), /stale|changed since planning/);
      assert.equal(f.spawn.mock.callCount(), 0);
      assert.equal(f.getHdc.mock.callCount(), 0);
      assert.deepEqual(f.clicks, []);
      assert.deepEqual(f.state.changes, []);
      assert.equal(f.state.signal.aborted, true);
    });
  }
});

test('window toggle rechecks the bound observation after resolving its button', async (t) => {
  const f = fixture(t);
  f.hooks.received = () => f.setWindows([windowInfo({ z_order: 11 })]);
  await assert.rejects(f.action.call({ window_id: 92 }), /changed since planning/);
  assert.equal(f.requests.length, 1);
  assertNoClick(f);
});

test('window toggle rejects a resolved button outside the target window including its right boundary', async (t) => {
  for (const center of [{ x: 900, y: 141 }, { x: 843, y: 99 }]) {
    await t.test(JSON.stringify(center), async (child) => {
      const f = fixture(child, [windowInfo()], windowControl({ center }));
      await assert.rejects(f.action.call({ window_id: 92 }), /outside.*target window|target.*window/i);
      assert.equal(f.requests.length, 1);
      assertNoClick(f);
    });
  }
});

test('window toggle cannot accept a replaced target after its single native click', async (t) => {
  const f = fixture(t);
  f.hooks.clicked = () => f.setWindows([{ ...f.after, process_id: 999 }]);
  await assert.rejects(f.action.call({ window_id: 92 }), /disappeared or changed identity/);
  assert.deepEqual(f.clicks, [[843, 141]]);
  assert.equal(f.requests.length, 1);
  assert.deepEqual(f.state.changes, []);
  assert.equal(f.state.revision, 0);
  assert.equal(f.state.signal.aborted, true);
});
