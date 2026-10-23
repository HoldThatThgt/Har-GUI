import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { test, type TestContext } from 'node:test';
import type { DeviceAction, ExecutorContext } from '@midscene/core';
import { z } from 'zod';
import type { LoadedConfiguration } from '../agent/config.js';
import { ToolRegistry, type RegisteredTool } from '../agent/registry.js';
import { RunState, sameWindow, windowSchema, type WindowInfo } from '../agent/state.js';
import { FIXTURE_DELIVERY } from './agent-configuration.js';

/** 构造不需要凭据、模型或设备的实际运行状态。 */
function fixture(t: TestContext, enabled = ['Inspect'], maxActions = 5, duration = 60) {
  const parent = resolve(tmpdir());
  const directory = mkdtempSync(join(parent, 'har-gui-registry-test-'));
  t.after(() => {
    assert.equal(dirname(directory), parent);
    assert.ok(basename(directory).startsWith('har-gui-registry-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  const config: LoadedConfiguration = {
    project: {
      schema_version: 1,
      run: { name: 'isolated-test', output: directory, max_actions: maxActions, max_duration_seconds: duration },
      agent: { llm: 'test' }, tools: { enabled },
      dataset: { root: directory, canvas: { width: 3120, height: 2080 }, stability_regions: [] },
      delivery: structuredClone(FIXTURE_DELIVERY),
      tasks: [{ name: 'generic-task', instruction: 'Inspect the fixture', acceptance: 'Fixture is returned',
        target_count: 1, app: 'Fixture', scene: 'Fixture', privacy: { enabled: false, reason: 'Synthetic fixture' } }],
    },
    profile: { provider: 'openai_compatible', base_url: 'https://model.example.test/v1', model: 'fixture',
      model_family: 'deepseek', api_key_env: 'UNUSED_REGISTRY_TEST_KEY', supports_vision: true },
    projectPath: join(directory, 'project.toml'), configPath: join(directory, 'config.toml'),
    outputPath: directory, datasetPath: directory, digests: { project: 'fixture-project', config: 'fixture-config' },
  };
  return { state: new RunState(config, directory, directory), directory };
}

/** 创建只供测试使用的最小动作上下文。 */
function context(): ExecutorContext {
  return { task: {} } as ExecutorContext;
}

/** 保持可变和只读工具共同走真实注册器。 */
function registered(name: string, call: DeviceAction['call'], mutates = false): RegisteredTool {
  return { action: { name, description: `Fixture ${name}`, paramSchema: z.object({}), call },
    resultSchema: z.object({ value: z.number().int() }), mutates };
}

/** 读取真实追加的审计事件。 */
function events(directory: string): Array<Record<string, any>> {
  return readFileSync(join(directory, 'events.jsonl'), 'utf8').trim().split('\n').map((line) => JSON.parse(line));
}

test('readonly results are validated, returned, recorded and explicitly fed to the planner', async (t) => {
  const { state, directory } = fixture(t);
  const registry = new ToolRegistry();
  registry.register(registered('Inspect', () => ({ value: 42 })));
  const runtime = context();
  const result = await registry.actions(state)[0].call({}, runtime);
  assert.deepEqual(result, { value: 42 });
  assert.deepEqual(JSON.parse(runtime.task.planningFeedback!), result);
  assert.equal(state.revision, 0);
  assert.equal(state.actions, 1);
  assert.equal(state.signal.aborted, false);
  const recorded = events(directory);
  assert.deepEqual(recorded.map((item) => item.type), ['action_started', 'action_finished']);
  assert.equal(recorded[0].action_count, 1);
  assert.deepEqual(recorded[1].result, result);
  assert.ok(recorded.every((item) => !Number.isNaN(Date.parse(item.time))));
});

test('only successful mutations advance the observation revision', async (t) => {
  const { state } = fixture(t, ['Change', 'Inspect']);
  const registry = new ToolRegistry();
  registry.register(registered('Change', () => ({ value: 2 }), true));
  registry.register(registered('Inspect', () => ({ value: 2 })));
  const [change, inspect] = registry.actions(state);
  await change.call({});
  assert.equal(state.revision, 1);
  await inspect.call({});
  assert.equal(state.revision, 1);
  assert.equal(state.actions, 2);
});

test('action budget rejects further execution, aborts the run and records the rejection', async (t) => {
  const { state, directory } = fixture(t, ['Inspect'], 1);
  let calls = 0;
  const registry = new ToolRegistry();
  registry.register(registered('Inspect', () => ({ value: ++calls })));
  const action = registry.actions(state)[0];
  await action.call({});
  await assert.rejects(async () => action.call({}), /Action budget exhausted/);
  assert.equal(calls, 1);
  assert.equal(state.actions, 1);
  assert.equal(state.signal.aborted, true);
  const recorded = events(directory);
  assert.deepEqual(recorded.map((item) => item.type), ['action_started', 'action_finished', 'action_failed']);
  assert.match(recorded[2].error, /Action budget exhausted/);
});

test('an expired time budget blocks the handler before any action is counted', async (t) => {
  const { state, directory } = fixture(t, ['Inspect'], 5, 0.005);
  const registry = new ToolRegistry();
  let calls = 0;
  registry.register(registered('Inspect', () => ({ value: ++calls })));
  await delay(15);
  assert.equal(state.signal.aborted, true);
  await assert.rejects(async () => registry.actions(state)[0].call({}), /timeout/i);
  assert.equal(calls, 0);
  assert.equal(state.actions, 0);
  assert.equal(events(directory)[0].type, 'action_failed');
});

test('an execution failure aborts the shared signal and never tries another registered tool', async (t) => {
  const { state, directory } = fixture(t, ['Failing', 'Alternative']);
  const registry = new ToolRegistry();
  const failure = new Error('Device refused the requested operation');
  let alternativeCalls = 0;
  registry.register(registered('Failing', () => { throw failure; }, true));
  registry.register(registered('Alternative', () => ({ value: ++alternativeCalls }), true));
  const [failing, alternative] = registry.actions(state);
  await assert.rejects(async () => failing.call({}), (error) => error === failure);
  assert.equal(state.controller.signal.aborted, true);
  await assert.rejects(async () => alternative.call({}), /Device refused/);
  assert.equal(alternativeCalls, 0);
  assert.equal(state.actions, 1);
  assert.equal(state.revision, 0);
  assert.deepEqual(events(directory).map((item) => item.type),
    ['action_started', 'action_failed', 'action_failed']);
});

test('invalid tool results never reach planner feedback or a successful audit event', async (t) => {
  const { state, directory } = fixture(t, ['BrokenResult']);
  const registry = new ToolRegistry();
  registry.register(registered('BrokenResult', () => ({ value: 'not-an-integer' }), true));
  const runtime = context();
  await assert.rejects(async () => registry.actions(state)[0].call({}, runtime), z.ZodError);
  assert.equal(runtime.task.planningFeedback, undefined);
  assert.equal(state.revision, 0);
  assert.equal(state.signal.aborted, true);
  assert.deepEqual(events(directory).map((item) => item.type), ['action_started', 'action_failed']);
});

test('explicit registration extends the same execution boundary and exposes only selected tools', async (t) => {
  const { state } = fixture(t, ['ReadCalendar', 'ReadDocument']);
  const registry = new ToolRegistry();
  registry.register(registered('UnusedTool', () => { throw new Error('Must not execute'); }));
  registry.register(registered('ReadCalendar', () => ({ value: 7 })));
  registry.register(registered('ReadDocument', () => ({ value: 12 })));
  const actions = registry.actions(state);
  assert.deepEqual(actions.map((action) => action.name), ['ReadCalendar', 'ReadDocument']);
  assert.deepEqual(registry.names(), ['UnusedTool', 'ReadCalendar', 'ReadDocument']);
  const observed = [];
  for (const action of actions) {
    const runtime = context();
    const result = await action.call({}, runtime);
    observed.push({ result, feedback: JSON.parse(runtime.task.planningFeedback!) });
  }
  assert.deepEqual(observed, [{ result: { value: 7 }, feedback: { value: 7 } },
    { result: { value: 12 }, feedback: { value: 12 } }]);
  assert.equal(state.actions, 2);
});

test('duplicate registration and unknown enabled tools fail explicitly', (t) => {
  const { state } = fixture(t, ['Missing']);
  const registry = new ToolRegistry();
  registry.register(registered('Inspect', () => ({ value: 1 })));
  assert.throws(() => registry.register(registered('Inspect', () => ({ value: 2 }))), /Duplicate tool/);
  assert.throws(() => registry.actions(state), /Unknown tool: Missing/);
  assert.equal(state.actions, 0);
});

test('captured candidates prevent further device operations before task acceptance', async (t) => {
  const { state } = fixture(t);
  state.candidate = { capture_id: 'pending' };
  const registry = new ToolRegistry();
  let calls = 0;
  registry.register(registered('Inspect', () => ({ value: ++calls })));
  await assert.rejects(async () => registry.actions(state)[0].call({}), /Sample already captured/);
  assert.equal(calls, 0);
  assert.equal(state.actions, 0);
  assert.equal(state.signal.aborted, true);
});

test('run state rejects simultaneous entry and permits entry after release', (t) => {
  const { state } = fixture(t);
  state.enter('First');
  assert.throws(() => state.enter('Second'), /already running/);
  assert.equal(state.actions, 1);
  state.leave();
  state.enter('Second');
  state.leave();
  assert.equal(state.actions, 2);
});

test('run summary persists accepted evidence and current action count', (t) => {
  const { state, directory } = fixture(t);
  state.accepted.push({ capture_id: 'accepted-frame', path: 'captures/accepted-frame' });
  state.enter('Inspect');
  state.leave();
  state.save('complete', { completed_tasks: ['generic-task'] });
  const saved = JSON.parse(readFileSync(join(directory, 'run.json'), 'utf8'));
  assert.equal(saved.status, 'complete');
  assert.equal(saved.actions, 1);
  assert.deepEqual(saved.accepted, state.accepted);
  assert.deepEqual(saved.completed_tasks, ['generic-task']);
  assert.ok(!Number.isNaN(Date.parse(saved.updated_at)));
  assert.equal(state.task.name, 'generic-task');
});

test('window identity comparison rejects replaced windows and changed routing geometry', () => {
  const initial: WindowInfo = { window_id: 92, bundle_name: 'com.example.notes', process_id: 202,
    display_id: 0, z_order: 112, window_mode: 102, bounding_box: { x: 10, y: 20, width: 800, height: 600 },
    screen_bounds: { x: 10.3, y: 20.3, width: 800.4, height: 600.4 },
    transform: { offset_x: 0, offset_y: 0, scale_x: 1, scale_y: 1, pivot_x: 0, pivot_y: 0 },
    session_visibility: { is_visible: true, rs_visible: true, visibility_state: 0 },
    ui_node_id: 920, is_system: false, focused: true };
  assert.equal(sameWindow(initial, structuredClone(initial)), true);
  const changes: Partial<WindowInfo>[] = [{ window_id: 93 }, { bundle_name: 'com.example.other' },
    { process_id: 203 }, { display_id: 1 }, { ui_node_id: 921 }, { window_mode: 1 }, { z_order: 113 }, { focused: false },
    { bounding_box: { ...initial.bounding_box, width: 1000 } }, { is_system: true },
    { screen_bounds: { ...initial.screen_bounds, x: 10.4 } },
    { screen_bounds: { ...initial.screen_bounds, y: 20.4 } },
    { screen_bounds: { ...initial.screen_bounds, width: 800.5 } },
    { screen_bounds: { ...initial.screen_bounds, height: 600.5 } },
    { session_visibility: { is_visible: true, rs_visible: true, visibility_state: 1 } },
    { transform: { offset_x: 0, offset_y: 0, scale_x: 0.9, scale_y: 1, pivot_x: 0, pivot_y: 0 } }];
  for (const change of changes) assert.equal(sameWindow(initial, { ...initial, ...change }), false);
  assert.equal(sameWindow(initial, { ...initial, observed_at: 'a later read',
    screen_bounds_source: { raw_sha256: 'a new clock value changes this source digest' } }), true);
});

test('window facts require finite exact screen bounds instead of substituting WMS logical bounds', () => {
  const base = { window_id: 106, bundle_name: 'app.allowed', process_id: 100, display_id: 0,
    ui_node_id: 7818, is_system: false, focused: true, z_order: 1, window_mode: 102,
    bounding_box: { x: 203, y: 203, width: 2048, height: 2230 } };
  const screen = { x: 203.3, y: 203.3, width: 1470.885888, height: 1601.59938 };
  assert.equal(windowSchema.safeParse(base).success, false);
  assert.deepEqual(windowSchema.parse({ ...base, screen_bounds: screen }).screen_bounds, screen);
  for (const field of ['x', 'y', 'width', 'height'] as const) {
    for (const value of [NaN, Infinity, -Infinity]) {
      assert.equal(windowSchema.safeParse({ ...base, screen_bounds: { ...screen, [field]: value } }).success, false);
    }
  }
});

test('failed actions retain their own resolved parameters and native phases without input or locator prose', async (t) => {
  const { state, directory } = fixture(t, ['Input']);
  const registry = new ToolRegistry();
  const diagnostics = { phase: 'focus_leaf_selection', request: {
    x: 41, y: 72, mode: 'append', window_id: 100, bundle_name: 'app.browser', display_id: 0 } };
  const failure = Object.assign(new Error('Native input binding failed'), { diagnostics });
  let received: unknown;
  registry.register(registered('Input', (parameters) => { received = parameters; throw failure; }, true));
  const parameters = { mode: 'append', value: 'PRIVATE_INPUT_BODY',
    locate: { center: [40.6, 72.1], prompt: 'PRIVATE_LOCATOR_PROSE' } };
  const original = structuredClone(parameters);
  await assert.rejects(async () => registry.actions(state)[0].call(parameters), (error) => error === failure);
  const failed = events(directory).find((item) => item.type === 'action_failed')!;
  assert.deepEqual(failed.diagnostics, diagnostics);
  assert.deepEqual(failed.parameters, { ...original, value: '[redacted]',
    locate: { center: [40.6, 72.1], prompt: '[redacted]' } });
  assert.deepEqual(received, original);
  assert.deepEqual(parameters, original);
  assert.ok(!readFileSync(join(directory, 'events.jsonl'), 'utf8').includes('PRIVATE'));
  assert.equal(state.revision, 0);
});
