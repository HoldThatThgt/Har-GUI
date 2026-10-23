import assert from 'node:assert/strict';
import { test } from 'node:test';
import type { DeviceAction, ExecutionTask } from '@midscene/core';
import { TaskExecutor } from '@midscene/core/agent';
import { getMidsceneLocationSchema, getModelRuntime, type ModelRuntime } from '@midscene/core/ai-model';
import { defineActionSleep } from '@midscene/core/device';
import { z } from 'zod';
import { projectLocate, projectPlan, safeDiagnosticParameters } from '../agent/planning-data.js';

const SECRET = 'sk-actual-private-value';

/** 创建实际公开 SDK 适配器，不构造客户端或调用模型。 */
function runtime(modelFamily = 'deepseek'): ModelRuntime {
  return getModelRuntime({ modelFamily, slot: 'default' } as Parameters<typeof getModelRuntime>[0]);
}

/** 使用 SDK 自身动作协议生成原文替身。 */
function planningTask(name: string, parameters: unknown, model: ModelRuntime) {
  assert.equal(model.adapter.planning.kind, 'standard');
  if (model.adapter.planning.kind !== 'standard') throw new Error('Expected standard adapter');
  const raw = model.adapter.planning.protocol.actionOutputProtocol.buildActionOutput({ actionName: name,
    param: parameters as Record<string, unknown>, locateFields: ['locate'],
    locateResultKey: model.adapter.planning.locateResultCodec?.promptSpec.resultKey });
  return { taskId: 'plan-task', type: 'Planning', subType: 'Plan', log: { rawResponse: raw } } as ExecutionTask;
}

/** 提供与 SDK 相同的公开定位字段标记。 */
function action(name: string): DeviceAction {
  return { name, paramSchema: z.object({ locate: getMidsceneLocationSchema() }), call: () => {} };
}

test('public SDK parsing retains raw normalized coordinates and the exact public prompt specification', () => {
  const model = runtime();
  const task = planningTask('Tap', { locate: { prompt: 'the Browse button', point: [573, 219] } }, model);
  const before = JSON.stringify(task);
  const result = projectPlan(task, model, [action('Tap')], []);
  assert.deepEqual(result.action, { name: 'Tap',
    parameters: { locate: { prompt: 'the Browse button', point: [573, 219] } } });
  assert.deepEqual(result.raw_locations, { locate: { prompt: 'the Browse button', point: [573, 219] } });
  assert.equal(model.adapter.planning.kind, 'standard');
  if (model.adapter.planning.kind === 'standard') {
    assert.deepEqual(result.prompt_spec, model.adapter.planning.locateResultCodec?.promptSpec);
  }
  assert.equal(result.raw_response_available, true);
  assert.equal(JSON.stringify(task), before);
  assert.equal('rawResponse' in result, false);
});

test('raw bounding boxes are retained without inferring a point or a coordinate system from pixels', () => {
  const model = runtime('gpt-5');
  const task = planningTask('Tap', { locate: { prompt: 'the control', bbox: [10, 20, 30, 40] } }, model);
  const result = projectPlan(task, model, [action('Tap')], []);
  assert.deepEqual(result.raw_locations, { locate: { prompt: 'the control', bbox: [10, 20, 30, 40] } });
  assert.equal(JSON.stringify(result).includes('locatedPixelResult'), false);
});

test('a JSON coordinate value extracted by the public planning protocol is projected as numeric coordinates', () => {
  const model = runtime();
  const task = planningTask('Tap', { locate: { prompt: 'the control', point: '[573,219]' } }, model);
  const result = projectPlan(task, model, [action('Tap')], []);
  assert.deepEqual(result.raw_locations, { locate: { prompt: 'the control', point: [573, 219] } });
});

test('planning diagnostics preserve authorized descriptions while removing secrets and input content', () => {
  const model = runtime();
  const task = planningTask('Input', { value: `private-document-${SECRET}`, mode: 'replace',
    locate: { prompt: `the control ${SECRET}`, point: [10, 20], secret: SECRET }, unknown: 'private-path' }, model);
  const result = projectPlan(task, model, [action('Input')], [SECRET]);
  assert.deepEqual(result.action, { name: 'Input', parameters: {
    locate: { prompt: 'the control [redacted]', point: [10, 20] }, mode: 'replace' } });
  const serialized = JSON.stringify(result);
  for (const forbidden of [SECRET, 'private-document', 'private-path', 'unknown', 'rawResponse']) {
    assert.equal(serialized.includes(forbidden), false);
  }
});

test('parameter projection copies only known finite geometry and explicitly allowed descriptions', () => {
  const source = { locate: { prompt: { prompt: `Browse ${SECRET}`, images: ['private-image'], url: SECRET },
    description: `current control ${SECRET}`, center: [1788, 456], rect: {
      left: 10, top: 20, width: 30, height: 40, value: SECRET }, xpath: 'private-path', text: 'private-content' },
    value: 'private-content', nested: { center: [1, 2] }, command: SECRET };
  const before = JSON.stringify(source);
  assert.deepEqual(safeDiagnosticParameters('Tap', source, [SECRET], true), { locate: {
    prompt: 'Browse [redacted]', description: 'current control [redacted]', center: [1788, 456],
    rect: { left: 10, top: 20, width: 30, height: 40 } } });
  assert.equal(JSON.stringify(source), before);
  assert.deepEqual(safeDiagnosticParameters('RunHdcShell', source, [SECRET], true), {});
});

test('input values and unapproved text never enter parameter diagnostics', () => {
  assert.deepEqual(safeDiagnosticParameters('Input', { value: 'private', mode: 'append',
    autoDismissKeyboard: true, unknown: SECRET }, [SECRET], true), { mode: 'append', autoDismissKeyboard: true });
  assert.deepEqual(safeDiagnosticParameters('KeyboardPress', { keyName: 'A' }, [], true), {});
  assert.deepEqual(safeDiagnosticParameters('KeyboardPress', { keyName: 'Enter', value: 'private' }, [], true),
    { keyName: 'Enter' });
  assert.deepEqual(safeDiagnosticParameters('ListApplications', { query: 'private', offset: 0, limit: 10 }, [], true),
    { offset: 0, limit: 10 });
  assert.deepEqual(safeDiagnosticParameters('Capture', { target_window_id: 170, page_name: 'private',
    variation: SECRET, path: 'private' }, [SECRET], true), { target_window_id: 170 });
});

test('finite malformed coordinate strings and unknown fields are not interpreted or persisted', () => {
  assert.deepEqual(safeDiagnosticParameters('Tap', { locate: { center: [Infinity, 2],
    point: '[1, 2]', bbox: [1, 2, 3, NaN], rect: { left: 1, top: 2, width: 3 }, unknown: SECRET } },
  [SECRET], true), {});
  assert.deepEqual(safeDiagnosticParameters('MoveWindow', { window_id: 170, x: -1, y: 0 }, [SECRET], true),
    { window_id: 170, x: -1, y: 0 });
  assert.deepEqual(safeDiagnosticParameters('Scroll', { direction: 'down', scrollType: 'singleAction',
    distance: null, unknown: SECRET }, [SECRET], true),
  { direction: 'down', scrollType: 'singleAction', distance: null });
});

test('locate projection preserves prompt, plan hit geometry and final element but excludes opaque cache data', () => {
  const task = { taskId: 'locate-task', type: 'Planning', subType: 'Locate',
    param: { prompt: { prompt: `the Browse button ${SECRET}`, images: ['private-image'] }, xpath: SECRET },
    hitBy: { from: 'Plan', context: { locatedPixelResult: { center: [1788, 456],
      rect: { left: 1, top: 2, width: 3, height: 4 }, unknown: SECRET }, cacheEntry: SECRET } },
    output: { element: { center: [1788, 456], description: `Browse ${SECRET}`, value: SECRET, id: SECRET } },
    log: { rawResponse: SECRET }, reasoning_content: SECRET } as unknown as ExecutionTask;
  assert.deepEqual(projectLocate(task, runtime(), [SECRET], true), { task_id: 'locate-task', adapter_kind: 'standard',
    prompt: 'the Browse button [redacted]', hit_by: { from: 'Plan', locatedPixelResult: {
      center: [1788, 456], rect: { left: 1, top: 2, width: 3, height: 4 } } },
    element: { center: [1788, 456], description: 'Browse [redacted]' }, source: 'plan',
    element_coordinate_space: 'screenshot_pixels', raw_response_available: false,
    raw_coordinates: null, prompt_spec: null });
  const cached = { ...task, hitBy: { from: 'Cache', context: { cacheEntry: { secret: SECRET } } } };
  assert.deepEqual(projectLocate(cached, runtime(), [SECRET], true).hit_by, { from: 'Cache' });
});

test('missing hitBy does not invent a locate source and unavailable raw planning is explicit', () => {
  const task = { taskId: 'locate-task', param: { prompt: 'the control' },
    output: { element: { center: [1, 2] } } } as ExecutionTask;
  assert.deepEqual(projectLocate(task, runtime(), [], true), { task_id: 'locate-task', adapter_kind: 'standard',
    prompt: 'the control',
    hit_by: null, element: { center: [1, 2] }, source: null,
    element_coordinate_space: 'screenshot_pixels', raw_response_available: false,
    raw_coordinates: null, prompt_spec: null });
  const result = projectPlan({ taskId: 'plan-task' } as ExecutionTask, runtime(), [action('Tap')], []);
  assert.equal(result.raw_response_available, false);
  assert.equal(result.action, null);
  assert.deepEqual(result.raw_locations, {});
});

test('SDK parser failures produce only a bounded English error and never expose the model source', (t) => {
  const errors: unknown[] = [];
  t.mock.method(console, 'error', (...args: unknown[]) => { errors.push(args); });
  const model = runtime(), planning = model.adapter.planning;
  assert.equal(planning.kind, 'standard');
  if (planning.kind !== 'standard') throw new Error('Expected standard adapter');
  const failed = { ...model, adapter: { ...model.adapter, planning: { ...planning, protocol: {
    ...planning.protocol, actionOutputProtocol: { ...planning.protocol.actionOutputProtocol,
      parseActionOutput: () => { throw new Error(SECRET); } } } } } };
  const task = { taskId: 'plan-task', log: { rawResponse: SECRET } } as ExecutionTask;
  assert.throws(() => projectPlan(task, failed, [action('Tap')], [SECRET]),
    { message: 'Planning diagnostic projection failed' });
  assert.equal(JSON.stringify(errors).includes(SECRET), false);
  assert.deepEqual(errors, [['Planning diagnostic projection failed']]);
});

test('ordinary AI locate uses the public response protocol and retains original numeric coordinates', () => {
  const model = runtime();
  const task = { taskId: 'ai-locate-task', param: { prompt: `Browse ${SECRET}` },
    log: { dump: { type: 'locate', taskInfo: {
      rawResponse: '<｜｜point｜｜>[573,219]<｜｜/point｜｜>', rawAssistantOutput: SECRET }, unknown: SECRET } },
    output: { element: { center: [1788, 456], description: 'Browse', prompt: SECRET, point: [1, 2] } },
  } as unknown as ExecutionTask;
  const result = projectLocate(task, model, [SECRET], true);
  assert.equal(result.source, 'ai');
  assert.equal(result.raw_response_available, true);
  assert.deepEqual(result.raw_coordinates, { field: 'point', value: [573, 219] });
  assert.deepEqual(result.element, { center: [1788, 456], description: 'Browse' });
  assert.equal(result.prompt, 'Browse [redacted]');
  assert.equal(result.hit_by, null);
  if (model.adapter.locate.kind !== 'standard') throw new Error('Expected standard adapter');
  assert.deepEqual(result.prompt_spec, model.adapter.locate.element.resultCodec.promptSpec);
  assert.equal(JSON.stringify(result).includes(SECRET), false);
  assert.equal(JSON.stringify(result).includes('rawAssistantOutput'), false);
});

test('invalid extracted coordinate JSON is rejected and never recovered from final element pixels', (t) => {
  const errors: unknown[] = [];
  t.mock.method(console, 'error', (...args: unknown[]) => { errors.push(args); });
  const task = { taskId: 'failed-ai-locate', param: { prompt: 'the control' },
    log: { dump: { type: 'locate', taskInfo: {
      rawResponse: `<｜｜point｜｜>[573,${SECRET}]<｜｜/point｜｜>` } } },
    output: { element: { center: [1788, 456] } } } as unknown as ExecutionTask;
  assert.throws(() => projectLocate(task, runtime(), [SECRET], true),
    { message: 'Locate diagnostic projection failed' });
  assert.equal(JSON.stringify(errors).includes(SECRET), false);
});

test('SDK parsing cannot persist an unregistered action name carrying model text or a path', (t) => {
  const errors: unknown[] = [];
  t.mock.method(console, 'error', (...args: unknown[]) => { errors.push(args); });
  const model = runtime();
  const task = planningTask(`C:\\private-document-${SECRET}`, { value: 'private-content' }, model);
  assert.throws(() => projectPlan(task, model, [action('Tap')], [SECRET]),
    { message: 'Planning diagnostic projection failed' });
  assert.deepEqual(errors, [['Planning diagnostic projection failed']]);
});

test('real custom adapters retain every SDK normalized action and their public coordinate declaration', () => {
  for (const family of ['vlm-ui-tars', 'auto-glm', 'auto-glm-multilingual']) {
    const model = runtime(family);
    assert.equal(model.adapter.planning.kind, 'custom');
    const task = { taskId: 'custom-plan', log: { rawResponse: `not a standard action ${SECRET}` }, output: {
      actions: [{ type: 'Tap', thought: 'private-thought', param: { locate: {
        prompt: 'private-thought', description: 'private-description', locatedPixelResult: {
          center: [40, 60], rect: { left: 30, top: 50, width: 20, height: 20 }, unknown: SECRET } } } },
      { type: 'Tap', param: { locate: { locatedPixelResult: { center: [80, 120] } } } }],
    } } as unknown as ExecutionTask;
    const before = JSON.stringify(task);
    const result = projectPlan(task, model, [action('Tap')], [SECRET]);
    assert.equal(result.adapter_kind, 'custom');
    assert.equal(result.normalized_coordinate_space, 'screenshot_pixels');
    assert.deepEqual(result.normalized_actions, [
      { name: 'Tap', parameters: { locate: { locatedPixelResult: {
        center: [40, 60], rect: { left: 30, top: 50, width: 20, height: 20 } } } } },
      { name: 'Tap', parameters: { locate: { locatedPixelResult: { center: [80, 120] } } } },
    ]);
    if (model.adapter.planning.kind !== 'custom') throw new Error('Expected custom adapter');
    assert.deepEqual(result.coordinate_system, model.adapter.planning.coordinateSystem);
    assert.equal(result.raw_response_available, true);
    assert.equal(result.raw_coordinates_available, false);
    assert.equal(result.raw_unavailable_reason, 'public_custom_adapter_has_no_raw_parser');
    for (const forbidden of ['private-thought', 'private-description', SECRET]) {
      assert.equal(JSON.stringify(result).includes(forbidden), false);
    }
    for (const absent of ['raw_locations', 'prompt_spec', 'action']) assert.equal(absent in result, false);
    assert.equal(JSON.stringify(task), before);
  }
});

test('custom prompt-only actions do not invent pixel coordinates or disclose derived model thought', () => {
  const model = runtime('vlm-ui-tars');
  const task = { taskId: 'custom-prompt-only', output: { actions: [
    { type: 'Tap', param: { locate: { prompt: 'private-thought' } } },
  ] } } as unknown as ExecutionTask;
  const result = projectPlan(task, model, [action('Tap')], []);
  assert.deepEqual(result.normalized_actions, [{ name: 'Tap', parameters: {} }]);
  assert.equal(result.raw_response_available, false);
  assert.equal(JSON.stringify(result).includes('private-thought'), false);
});

test('custom locate retains SDK results but does not call a missing raw parser or disclose unknown text', () => {
  const task = { taskId: 'custom-locate', param: { prompt: 'private-thought' }, log: { dump: {
    type: 'locate', taskInfo: { rawResponse: `not a standard response ${SECRET}` } } },
    output: { element: { center: [40, 60], description: 'private-description' } } } as unknown as ExecutionTask;
  const result = projectLocate(task, runtime('auto-glm'), [SECRET], true);
  assert.equal(result.adapter_kind, 'custom');
  assert.equal(result.raw_response_available, true);
  assert.equal(result.raw_unavailable_reason, 'public_custom_adapter_has_no_raw_parser');
  assert.equal(result.prompt, null);
  assert.equal(result.prompt_spec, null);
  assert.equal(result.raw_coordinates, null);
  assert.deepEqual(result.element, { center: [40, 60] });
  assert.equal(result.element_coordinate_space, 'screenshot_pixels');
  for (const forbidden of ['private-thought', 'private-description', SECRET]) {
    assert.equal(JSON.stringify(result).includes(forbidden), false);
  }
});

test('the actual planner controls description retention even when the insight adapter is standard', () => {
  const task = { taskId: 'separate-roles', param: { prompt: 'private-thought' }, log: { dump: {
    type: 'locate', taskInfo: { rawResponse: '<｜｜point｜｜>[10,20]<｜｜/point｜｜>' } } },
    output: { element: { center: [40, 60], description: 'private-description' } } } as unknown as ExecutionTask;
  const result = projectLocate(task, runtime(), [], false);
  assert.equal(result.adapter_kind, 'standard');
  assert.deepEqual(result.raw_coordinates, { field: 'point', value: [10, 20] });
  assert.equal(result.prompt, null);
  assert.deepEqual(result.element, { center: [40, 60] });
  const parameters = safeDiagnosticParameters('Tap', { locate: { center: [40, 60],
    prompt: 'private-thought', description: 'private-description' } }, [], false);
  assert.deepEqual(parameters, { locate: { center: [40, 60] } });
});

test('custom actions still reject unregistered names and invalid normalized geometry', (t) => {
  t.mock.method(console, 'error', () => {});
  const model = runtime('auto-glm');
  for (const value of [
    { type: `C:\\private-${SECRET}`, param: {} },
    { type: 'Tap', param: { locate: { locatedPixelResult: { center: [NaN, 60] } } } },
    { type: 'Tap', param: { locate: { locatedPixelResult: { center: [40] } } } },
  ]) {
    const task = { taskId: 'invalid-custom', output: { actions: [value] } } as unknown as ExecutionTask;
    assert.throws(() => projectPlan(task, model, [action('Tap')], [SECRET]),
      { message: 'Planning diagnostic projection failed' });
  }
  const task = { taskId: 'invalid-locate', output: { element: { center: [Infinity, 60] } } } as ExecutionTask;
  assert.throws(() => projectLocate(task, model, [], false), { message: 'Locate diagnostic projection failed' });
});

test('public SDK Sleep and Finished controls work without a registered device action', async () => {
  const model = runtime('vlm-ui-tars'), sleep = defineActionSleep();
  const executor = new TaskExecutor({} as ConstructorParameters<typeof TaskExecutor>[0],
    {} as ConstructorParameters<typeof TaskExecutor>[1], { actionSpace: [sleep], getDefaultModelRuntime: () => model });
  const converted = await executor.convertPlanToExecutable([{ type: 'Finished' }], model, model);
  assert.equal(converted.tasks[0].subType, 'Finished');
  assert.equal(converted.tasks[0].param, null);
  const task = { taskId: 'sdk-controls', output: { actions: [
    { type: sleep.name, param: { timeMs: 1000, value: SECRET } },
    { type: 'Finished', param: { value: SECRET }, thought: 'private-thought' },
  ] } } as unknown as ExecutionTask;
  assert.deepEqual(projectPlan(task, model, [], [SECRET]).normalized_actions, [
    { name: sleep.name, parameters: { timeMs: 1000 } }, { name: 'Finished', parameters: {} },
  ]);
  const standard = runtime();
  assert.deepEqual(projectPlan(planningTask(sleep.name, { timeMs: 1000 }, standard), standard, [], []).action,
    { name: sleep.name, parameters: { timeMs: 1000 } });
  assert.deepEqual(projectPlan(planningTask('Finished', { value: SECRET }, standard), standard, [], [SECRET]).action,
    { name: 'Finished', parameters: {} });
});
