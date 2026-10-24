import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { writeFile } from 'node:fs/promises';
import { syncBuiltinESMExports } from 'node:module';
import { basename, dirname, join, resolve, sep } from 'node:path';
import { test, type TestContext } from 'node:test';
import sharp from 'sharp';
import { cropByRect, scaleImage } from '@midscene/shared/img';
import type { LoadedConfiguration } from '../agent/config.js';
import { CollectionAgent, CollectionDevice } from '../agent/device.js';
import { PlanningEvidence } from '../agent/planning-evidence.js';
import { RunState, type WindowInfo } from '../agent/state.js';
import { createRegistry } from '../agent/tools.js';
import { UsageRecorder } from '../agent/usage.js';

const API_SECRET = 'PRIVATE_PLANNING_API_KEY';
const HEADER_SECRET = 'PRIVATE_PLANNING_FACTORY_HEADER';
const INPUT_SECRET = 'PRIVATE_PLANNING_INPUT_BODY';
const THOUGHT_SECRET = 'PRIVATE_PLANNING_THOUGHT';
const LOCATOR = 'anonymous editable control';
const COMPLETE = '<complete success="true">Anonymous offline fixture complete</complete>';
type FixtureViewport = { width: number; height: number; shrink: number };
const DEFAULT_VIEWPORT: FixtureViewport = { width: 320, height: 240, shrink: 2 };
const TARGET: WindowInfo = { window_id: 7, bundle_name: 'app.fixture', process_id: 123,
  display_id: 0, ui_node_id: 70, z_order: 10, window_mode: 102, is_system: false, focused: true,
  bounding_box: { x: 0, y: 0, width: 320, height: 240 },
  screen_bounds: { x: 0, y: 0, width: 320, height: 240 } };

/** @brief 构造真正经过 SDK 解析和坐标转换的匿名计划。 */
function action(name: string, point: [number, number], extra: object = {}) {
  return `<planning>${THOUGHT_SECRET}</planning><action-type>${name}</action-type><action-param-json>`
    + JSON.stringify({ ...extra, locate: { prompt: LOCATOR, point } }) + '</action-param-json>';
}

/** @brief 计算图片实际字节摘要，不从尺寸或 data URL 文本推算。 */
function digest(bytes: Buffer) { return createHash('sha256').update(bytes).digest('hex'); }

/** @brief 只创建匿名 PNG；模拟输入完成后改变画面，避免伪造真实 UI 效果。 */
async function image(applied: number, viewport: FixtureViewport) {
  return sharp({ create: { width: viewport.width, height: viewport.height, channels: 3,
    background: { r: applied * 70, g: 60, b: 120 } } }).png().toBuffer();
}

/** @brief 真实 Input 桥接仅连接本地 Node 子进程，不启动 Python、HDC 或 shell。 */
function inputBridge(t: TestContext, applied: { count: number }, failure?: object) {
  const requests: any[] = [];
  const original = childProcess.spawn;
  const script = `let text='';process.stdin.on('data',part=>text+=part);process.stdin.on('end',()=>{
    const request=JSON.parse(text);const failure=JSON.parse(process.argv[1]);
    if(failure){process.stdout.write(JSON.stringify({error:failure}));process.exitCode=1;return;}
    const result={window_id:request.window_id,mode:request.mode,verified:true,
      character_count:Array.from(request.value).length,
      text_sha256:require('node:crypto').createHash('sha256').update(request.value).digest('hex')};
    process.stdout.write(JSON.stringify({result}));});`;
  const spawn = t.mock.method(childProcess, 'spawn', (_program: any, args: any, options: any) => {
    assert.equal(basename(String(args[0])), 'agent_device.py');
    const child = original(process.execPath, ['-e', script, JSON.stringify(failure ?? null)], options);
    const end = child.stdin.end.bind(child.stdin);
    t.mock.method(child.stdin, 'end', (bytes: any) => {
      const request = JSON.parse(String(bytes));
      assert.equal(request.operation, 'input_text');
      requests.push(request);
      applied.count += 1;
      return end(bytes);
    });
    return child;
  });
  syncBuiltinESMExports();
  t.after(() => { spawn.mock.restore(); syncBuiltinESMExports(); });
  return requests;
}

/** @brief 公开设备 I/O 的替身保留真实 CollectionDevice 截图、观察和清理流程。 */
function deviceIO(t: TestContext, state: RunState, control: { count: number; missingAfter: boolean },
  viewport: FixtureViewport) {
  const native: Array<[number, number]> = [];
  const images: Buffer[] = [];
  const device = new CollectionDevice({ python: 'unused-python', hdc: 'unused-hdc', serial: 'fixture' }, state);
  t.mock.method(device, 'size', async () => ({ width: viewport.width, height: viewport.height }));
  t.mock.method(device, 'windows', async () => [{ ...structuredClone(TARGET),
    bounding_box: { x: 0, y: 0, width: viewport.width, height: viewport.height },
    screen_bounds: { x: 0, y: 0, width: viewport.width, height: viewport.height } }]);
  t.mock.method(device, 'getHdc', async () => ({
    async shell(command: string) {
      if (command.startsWith('rm -f ')) return '';
      assert.ok(command.startsWith('uitest screenCap -d 0 -p '));
      if (control.missingAfter && control.count) throw new Error('Anonymous after-action screenshot unavailable');
      return `ScreenCap saved to ${command.split(' -p ')[1]}`;
    },
    async fileRecv(_remote: string, local: string) {
      const bytes = await image(control.count, viewport);
      images.push(bytes);
      await writeFile(local, bytes);
    },
    async click(x: number, y: number) { native.push([x, y]); control.count += 1; },
  }) as any);
  return { device, native, images };
}

/** @brief 使用真实 SDK 和用量封装，仅在公开工厂处返回脚本化客户端。 */
function sdk(evidence: PlanningEvidence, device: CollectionDevice, directory: string,
  answers: string[], options: { modelOverrides: Record<string, string | number>; shrink: number }) {
  const requests: Array<{ hashes: string[]; images: Buffer[]; model: string; body: any }> = [];
  const usage = new UsageRecorder(join(directory, 'model-usage.jsonl'));
  const factory = evidence.clientFactory(usage.createOpenAIClient);
  const agent = new CollectionAgent(device, { cache: false, generateReport: false,
    persistExecutionDump: false, autoPrintReportMsg: false, screenshotShrinkFactor: options.shrink,
    waitAfterAction: 0, replanningCycleLimit: 5,
    modelConfig: { MIDSCENE_MODEL_FAMILY: 'deepseek', MIDSCENE_MODEL_NAME: 'anonymous-planning-fixture',
      MIDSCENE_MODEL_API_KEY: API_SECRET, MIDSCENE_MODEL_BASE_URL: 'https://fixture.invalid/v1',
      MIDSCENE_MODEL_STREAM_MODE: 'non-stream', MIDSCENE_MODEL_RETRY_COUNT: 0, ...options.modelOverrides },
    createOpenAIClient: async (client, options) => {
      const fake = { chat: { completions: { create: async (body: any) => {
        const parts = body.messages.flatMap((message: any) => Array.isArray(message.content) ? message.content : []);
        const images = parts.filter((part: any) => part.type === 'image_url')
          .map((part: any) => Buffer.from(part.image_url.url.split(',')[1], 'base64'));
        const content = answers[requests.length];
        assert.equal(typeof content, 'string', 'Unexpected extra model request');
        requests.push({ images, hashes: images.map(digest), model: body.model, body });
        return { id: `anonymous-${requests.length}`, object: 'chat.completion', created: 1,
          model: body.model, usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
          choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content } }] };
      } } } };
      return factory(fake as typeof client, { ...options, headers: { Authorization: HEADER_SECRET } } as any);
    } });
  return { agent, usage, requests };
}

/** @brief 所有证据与临时文件都位于本测试新建的匿名目录，绝不访问真实采集。 */
function fixture(t: TestContext, answers: string[], family = 'deepseek',
  modelOverrides: Record<string, string | number> = {}, viewport: FixtureViewport = DEFAULT_VIEWPORT) {
  const parent = resolve('artifacts/planning-evidence-engine-20261023/tests');
  mkdirSync(parent, { recursive: true });
  const directory = mkdtempSync(join(parent, 'anonymous-'));
  t.mock.method(console, 'error', () => {});
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('Unexpected network request'); });
  const config = { project: { run: { max_duration_seconds: 60, max_actions: 8 },
    tools: { enabled: ['Tap', 'Input'] }, tasks: [{ name: 'anonymous-planning',
      bundle_names: ['app.fixture'] }] }, profile: { api_key_env: 'HAR_GUI_PLANNING_FIXTURE_UNUSED' },
  } as LoadedConfiguration;
  const state = new RunState(config, directory, directory);
  const evidence = new PlanningEvidence(state);
  state.planningEvidence = evidence;
  const control = { count: 0, missingAfter: false };
  const io = deviceIO(t, state, control, viewport);
  io.device.actions = createRegistry(() => io.device).actions(state);
  const model = sdk(evidence, io.device, directory, answers,
    { modelOverrides: { MIDSCENE_MODEL_FAMILY: family, ...modelOverrides }, shrink: viewport.shrink });
  const snapshots: any[] = [];
  const contexts = new Map<string, { image: Buffer; width: number; height: number; ratio: number }>();
  const originalContext = model.agent.getUIContext.bind(model.agent);
  t.mock.method(model.agent, 'getUIContext', async (...args: Parameters<CollectionAgent['getUIContext']>) => {
    const context = await originalContext(...args);
    contexts.set(context.screenshot.id, { image: Buffer.from(context.screenshot.rawBase64, 'base64'),
      ...context.shotSize, ratio: context.shrunkShotToLogicalRatio });
    return context;
  });
  model.agent.addDumpUpdateListener((_raw, execution) => {
    if (!execution) return;
    snapshots.push(JSON.parse(JSON.stringify(execution.tasks.map((task: any) => ({
      id: task.taskId, type: task.type, sub_type: task.subType, status: task.status,
      actions: task.output?.actions, element: task.output?.element, hit_by: task.hitBy,
      screenshot_id: task.uiContext?.screenshot?.id,
      recorder: task.recorder?.map((item: any) => ({ timing: item.timing, screenshot_id: item.screenshot?.id })),
    })))));
  });
  let closed = false;
  const close = async () => {
    if (closed) return;
    closed = true;
    await model.agent.destroy();
    await evidence.flush();
  };
  t.after(async () => {
    await close();
    assert.equal(dirname(directory), parent);
    assert.ok(basename(directory).startsWith('anonymous-'));
    rmSync(directory, { recursive: true, force: true });
  });
  return { directory, state, evidence, control, close, snapshots, contexts, ...io, ...model };
}

/** @brief 读回正式内部证据接口，不依赖 SDK 私有对象。 */
function readTrace(directory: string): any[] {
  return readFileSync(join(directory, 'planning-evidence', 'trace.jsonl'), 'utf8').trim()
    .split('\n').map((line) => JSON.parse(line));
}

/** @brief 图片引用必须留在内部目录，摘要和尺寸都来自实际保存字节。 */
async function savedImage(directory: string, info: any) {
  const target = resolve(directory, info.file);
  assert.ok(target.startsWith(resolve(directory, 'planning-evidence') + sep));
  const bytes = readFileSync(target);
  assert.equal(bytes.length, info.bytes);
  assert.equal(digest(bytes), info.sha256);
  const metadata = await sharp(bytes).metadata();
  assert.deepEqual([metadata.width, metadata.height], [info.width, info.height]);
  assert.equal(info.mime_type, metadata.format === 'png' ? 'image/png' : 'image/jpeg');
  return bytes;
}

test('real SDK retains planning image coordinates, direct-hit locate and actual native dispatch', async (t) => {
  const f = fixture(t, [action('Tap', [250, 500]), action('Tap', [750, 250]), COMPLETE]);
  await f.agent.aiAct('Perform the two anonymous taps, then finish.',
    { cacheable: false, abortSignal: f.state.signal });
  await f.close();
  assert.deepEqual(f.native, [[80, 120], [240, 60]]);
  assert.equal(f.requests.length, 3);
  assert.equal(f.usage.summary().calls, 3);
  assert.equal(f.usage.summary().completed_calls, 3);
  assert.equal(f.state.revision, 2);
  const firstPlan = f.snapshots.flat().find((task) => task.type === 'Planning' && task.sub_type === 'Plan'
    && task.status === 'finished' && task.actions?.length);
  assert.deepEqual(firstPlan.actions[0].param.locate.locatedPixelResult.center, [40, 60]);
  const latestPlan = f.snapshots.at(-1).find((task: any) => task.id === firstPlan.id);
  assert.deepEqual(latestPlan.actions[0].param.locate.center, [40, 60]);
  assert.equal(latestPlan.actions[0].param.locate.locatedPixelResult, undefined);
  const firstLocate = f.snapshots.flat().find((task) => task.sub_type === 'Locate' && task.status === 'finished');
  assert.equal(firstLocate.hit_by.from, 'Plan');
  assert.deepEqual(firstLocate.element.center, [40, 60]);
  const context = f.contexts.get(firstPlan.screenshot_id)!;
  assert.deepEqual({ width: context.width, height: context.height, ratio: context.ratio },
    { width: 160, height: 120, ratio: 0.5 });
  assert.equal(f.requests[0].hashes[0], digest(context.image));
  assert.notEqual(digest(f.images[0]), digest(context.image));
  const trace = readTrace(f.directory);
  assert.deepEqual(trace.map((record) => record.sequence), trace.map((_record, index) => index + 1));
  assert.ok(trace.every((record) => !Number.isNaN(Date.parse(record.time))));
  assert.equal(new Set(trace.map((record) => record.invocation_id)).size, 1);
  const pendingPlans = trace.filter((record) => record.type === 'sdk_task' && record.sdk_task_type === 'Planning'
    && record.sub_type === 'Plan' && record.status === 'pending');
  assert.ok(pendingPlans.every((record) => record.plan_index === null));
  const planned = trace.find((record) => record.type === 'sdk_task' && record.task_id === firstPlan.id
    && record.status === 'finished');
  assert.deepEqual(planned.plan.raw_locations.locate.point, [250, 500]);
  assert.deepEqual(planned.plan.action.parameters.locate.point, [250, 500]);
  assert.match(planned.plan.prompt_spec.resultValueDescription, /normalized to 0-1000/);
  const located = trace.find((record) => record.type === 'sdk_task' && record.task_id === firstLocate.id
    && record.status === 'finished');
  assert.equal(located.locate.source, 'plan');
  assert.equal(located.locate.hit_by.from, 'Plan');
  assert.deepEqual(located.locate.element.center, [40, 60]);
  assert.deepEqual(located.locate.hit_by.locatedPixelResult.center, [40, 60]);
  assert.equal(located.locate.raw_response_available, false);
  const plannedContext = trace.find((record) => record.type === 'context'
    && record.screenshot_id === firstPlan.screenshot_id);
  assert.deepEqual(plannedContext.shot_size, { width: 160, height: 120 });
  assert.equal(plannedContext.shrunk_shot_to_logical_ratio, 0.5);
  assert.equal(plannedContext.image.sha256, digest(context.image));
  const original = trace.find((record) => record.type === 'observation'
    && record.observation_id === plannedContext.observation_id);
  assert.equal(original.image.sha256, digest(f.images[0]));
  assert.deepEqual([original.image.width, original.image.height], [320, 240]);
  const thinking = trace.find((record) => record.type === 'planning' && record.phase === 'plan_thinking');
  assert.equal(thinking.screenshot_id, firstPlan.screenshot_id);
  assert.equal(thinking.task_id, firstPlan.id);
  assert.equal(thinking.context.observation_id, original.observation_id);
  const dispatches = trace.filter((record) => record.type === 'action_dispatch' && record.status === 'native_returned');
  assert.deepEqual(dispatches.map((record) => record.parameters.locate.center), f.native);
  assert.ok(dispatches.every((record) => record.coordinate_system === 'device_logical_pixels'
    && record.os_delivery_confirmed === false && record.ui_effect === 'not_evaluated'));
  const modelRequests = trace.filter((record) => record.type === 'model_request' && record.status === 'started');
  assert.equal(modelRequests.length, 3);
  for (const context of trace.filter((record) => record.type === 'context')) {
    const plan = trace.find((record) => record.type === 'planning' && record.phase === 'plan_thinking'
      && record.task_id === context.task_id);
    if (plan && context.sequence < plan.sequence) assert.equal(context.plan_index, null);
    if (plan && context.plan_index !== null) assert.equal(context.plan_index, plan.plan_index);
  }
  for (const [index, request] of modelRequests.entries()) {
    assert.deepEqual(request.images.map((item: any) => item.image.sha256), f.requests[index].hashes);
    assert.ok(trace.some((record) => record.type === 'sdk_task' && record.task_id === request.task_id
      && record.status === 'running' && record.sequence < request.sequence));
    assert.equal(request.sdk_task_type, 'Planning');
    assert.equal(request.sub_type, 'Plan');
    const thinking = trace.find((record) => record.type === 'planning' && record.phase === 'plan_thinking'
      && record.task_id === request.task_id);
    assert.equal(request.plan_index, thinking.plan_index);
    const returned = trace.filter((record) => record.type === 'model_request'
      && record.request_id === request.request_id && record.status === 'returned');
    assert.equal(returned.length, 1);
    assert.equal(returned[0].task_id, request.task_id);
  }
  const observed = trace.filter((record) => record.type === 'action_observation');
  assert.equal(observed.length, 2);
  assert.ok(observed.every((record) => record.sdk_task_type === 'Action Space' && record.sub_type === 'Tap'
    && record.observation_status === 'recorded' && record.ui_effect === 'not_evaluated'));
  for (const observation of observed) {
    const task = f.snapshots.at(-1).find((item: any) => item.id === observation.task_id);
    assert.deepEqual(observation.after.map((item: any) => item.screenshot_id),
      task.recorder.filter((item: any) => item.timing === 'after-calling').map((item: any) => item.screenshot_id));
    assert.ok(!observation.after.some((item: any) => firstPlan.recorder
      .some((planImage: any) => planImage.screenshot_id === item.screenshot_id)));
  }
  const images = [
    ...trace.filter((record) => ['context', 'observation'].includes(record.type)).map((record) => record.image),
    ...modelRequests.flatMap((request) => request.images.map((item: any) => item.image)),
    ...observed.flatMap((observation) => observation.after.map((item: any) => item.image))];
  for (const info of images) await savedImage(f.directory, info);
  assert.doesNotMatch(readFileSync(join(f.directory, 'planning-evidence', 'trace.jsonl'), 'utf8'), /data:image/);
});

test('real SDK Input keeps locator text without persisting input, thought, key or factory headers', async (t) => {
  const f = fixture(t, [action('Input', [250, 500], { value: INPUT_SECRET, mode: 'replace' }), COMPLETE]);
  const requests = inputBridge(t, f.control);
  await f.agent.aiAct('Edit the anonymous field, then finish.', { cacheable: false, abortSignal: f.state.signal });
  await f.close();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].value, INPUT_SECRET);
  assert.deepEqual([requests[0].x, requests[0].y], [80, 120]);
  assert.equal(f.requests.length, 2);
  const dispatches = readTrace(f.directory).filter((record) => record.type === 'action_dispatch');
  assert.equal(readTrace(f.directory).filter((record) => record.type === 'native_input_failure_observation').length, 0);
  assert.deepEqual(dispatches.map((record) => record.status), ['invoked', 'native_returned']);
  assert.ok(dispatches.every((record) => record.name === 'Input'));
  assert.deepEqual(dispatches[0].parameters.locate.center, [80, 120]);
  assert.equal(dispatches[0].parameters.value, undefined);
  assert.equal(dispatches[1].task_id, dispatches[0].task_id);
  const trace = readFileSync(join(f.directory, 'planning-evidence', 'trace.jsonl'), 'utf8');
  for (const secret of [API_SECRET, HEADER_SECRET, INPUT_SECRET, THOUGHT_SECRET]) assert.ok(!trace.includes(secret));
  assert.ok(trace.includes(LOCATOR));
  const events = readFileSync(join(f.directory, 'events.jsonl'), 'utf8');
  for (const secret of [API_SECRET, HEADER_SECRET, INPUT_SECRET, THOUGHT_SECRET, LOCATOR]) {
    assert.ok(!events.includes(secret));
  }
});

test('native completion without a successor screenshot does not become verified UI effect', async (t) => {
  const f = fixture(t, [action('Tap', [250, 500]), COMPLETE]);
  f.control.missingAfter = true;
  await assert.rejects(f.agent.aiAct('Tap the anonymous control, then finish.',
    { cacheable: false, abortSignal: f.state.signal }));
  await f.close();
  assert.deepEqual(f.native, [[80, 120]]);
  assert.equal(f.state.revision, 1);
  const tap = f.snapshots.at(-1).find((task: any) => task.sub_type === 'Tap');
  assert.equal(tap.status, 'finished');
  assert.equal(tap.recorder, undefined);
  const trace = readTrace(f.directory);
  const dispatch = trace.find((record) => record.type === 'action_dispatch' && record.status === 'native_returned');
  assert.equal(dispatch.task_id, tap.id);
  const observed = trace.find((record) => record.type === 'action_observation' && record.task_id === tap.id);
  assert.equal(observed.observation_status, 'unavailable');
  assert.equal(observed.native_invocation_recorded, true);
  assert.equal(observed.ui_effect, 'not_evaluated');
  assert.deepEqual(observed.after, []);
});

/** @brief 匿名桥接失败保留首次比较阶段，后验组件读数仅作为独立证据。 */
function inputFailure() {
  return { message: 'Native input text does not exactly match the requested content', diagnostics: {
    phase: 'compare_text', failure_observations: { primary_phase: 'compare_text',
      properties: { status: 'completed', value: { type: 'dict', focused: true, editable: true } },
      text: { status: 'completed', value: { type: 'str', length: 1, sha256: 'a'.repeat(64) } } } } };
}

test('real SDK failed Input captures one independent observation before abort without a success record', async (t) => {
  const f = fixture(t, [action('Input', [250, 500], { value: INPUT_SECRET, mode: 'replace' })]);
  const requests = inputBridge(t, f.control, inputFailure());
  const capture = f.device.screenshotBase64.bind(f.device);
  let failuresObserved = 0;
  t.mock.method(f.device, 'screenshotBase64', async () => {
    if (f.control.count) { failuresObserved += 1; assert.equal(f.state.signal.aborted, false); }
    return capture();
  });
  await assert.rejects(f.agent.aiAct('Input anonymous text.', { cacheable: false, abortSignal: f.state.signal }),
    /Native input text does not exactly match the requested content/);
  await f.close();
  assert.equal(requests.length, 1);
  assert.deepEqual([requests[0].x, requests[0].y], [80, 120]);
  assert.equal(failuresObserved, 1);
  assert.equal(f.state.revision, 0);
  const trace = readTrace(f.directory);
  const records = trace.filter((record) => record.type === 'native_input_failure_observation');
  assert.equal(records.length, 1);
  const recorded = records[0];
  assert.equal(recorded.phase, 'compare_text');
  assert.equal(recorded.action_status, 'failed');
  assert.equal(recorded.ui_effect, 'not_evaluated');
  assert.equal(recorded.observation_status, 'recorded');
  const dispatch = trace.find((record) => record.type === 'action_dispatch'
    && record.status === 'failed_or_interrupted');
  assert.deepEqual([recorded.task_id, recorded.execution_id], [dispatch.task_id, dispatch.execution_id]);
  const observed = trace.find((record) => record.type === 'observation'
    && record.observation_id === recorded.observation_id);
  assert.deepEqual(recorded.image, observed.image);
  assert.deepEqual(recorded.windows, observed.windows);
  assert.ok(Date.parse(recorded.capture_started_at) >= Date.parse(dispatch.time));
  assert.ok(Date.parse(recorded.capture_finished_at) >= Date.parse(recorded.capture_started_at));
  assert.equal(digest(await savedImage(f.directory, recorded.image)), digest(f.images.at(-1)!));
  assert.notEqual(recorded.image.sha256, digest(f.images[0]));
  const after = trace.find((record) => record.type === 'action_observation' && record.task_id === recorded.task_id);
  assert.equal(after.action_status, 'failed');
  assert.deepEqual(after.after, []);
  assert.equal(after.observation_status, 'unavailable');
  const task = f.snapshots.at(-1).find((item: any) => item.id === recorded.task_id);
  assert.equal(task.status, 'failed');
  assert.ok(!task.recorder?.some((item: any) => item.timing === 'after-calling'));
  for (const file of ['planning-evidence/trace.jsonl', 'events.jsonl']) {
    const text = readFileSync(join(f.directory, file), 'utf8');
    for (const secret of [API_SECRET, HEADER_SECRET, INPUT_SECRET, THOUGHT_SECRET]) assert.ok(!text.includes(secret));
    assert.ok(!text.includes('data:image/'));
  }
});

test('failure observation errors stay safe and do not replace the failed Input', async (t) => {
  for (const condition of ['screenshot', 'cancelled', 'frozen']) {
    await t.test(condition, async (child) => {
      const f = fixture(child, [action('Input', [250, 500], { value: INPUT_SECRET })]);
      inputBridge(child, f.control, inputFailure());
      const input = f.device.actions.find((item) => item.name === 'Input')!;
      const call = input.call.bind(input);
      let primary: unknown;
      let settle: () => void = () => {};
      const settled = new Promise<void>((resolve) => { settle = resolve; });
      child.mock.method(input, 'call', async (...args: Parameters<typeof input.call>) => {
        try { return await call(...args); } catch (error) { primary = error; throw error; } finally { settle(); }
      });
      const capture = f.device.screenshotBase64.bind(f.device);
      let attempts = 0;
      let beforeCount = 0;
      child.mock.method(f.device, 'screenshotBase64', async () => {
        if (f.control.count) {
          attempts += 1;
          beforeCount = f.images.length;
          if (condition === 'screenshot') throw new TypeError(INPUT_SECRET);
          if (condition === 'cancelled') {
            f.state.controller.abort(new DOMException('Anonymous cancellation', 'AbortError'));
          }
          if (condition === 'frozen') {
            f.device.frozenScreenshot = `data:image/png;base64,${f.images[0].toString('base64')}`;
          }
        }
        return capture();
      });
      await assert.rejects(f.agent.aiAct('Input anonymous text.', { cacheable: false, abortSignal: f.state.signal }),
        condition === 'cancelled' ? /Anonymous cancellation/ : /does not exactly match/);
      await settled;
      assert.equal((primary as Error).message, inputFailure().message);
      await f.close();
      assert.equal(attempts, 1);
      assert.equal(f.images.length, beforeCount);
      assert.equal(f.state.revision, 0);
      const records = readTrace(f.directory).filter((record) => record.type === 'native_input_failure_observation');
      assert.equal(records.length, 1);
      assert.equal(records[0].observation_status, 'unavailable');
      assert.equal(records[0].error_type, condition === 'screenshot' ? 'TypeError'
        : condition === 'cancelled' ? 'DOMException' : 'Error');
      assert.equal(records[0].image, undefined);
      assert.equal(records[0].observation_id, undefined);
      assert.ok(!JSON.stringify(records).includes(INPUT_SECRET));
      const events = readFileSync(join(f.directory, 'events.jsonl'), 'utf8');
      assert.ok(!events.includes(INPUT_SECRET));
      assert.ok(events.includes(inputFailure().message));
    });
  }
});

test('real SDK pre-write Input failure does not capture a native failure observation', async (t) => {
  const f = fixture(t, [action('Input', [250, 500], { value: INPUT_SECRET })]);
  const failure = { message: 'Native input target is outside the editable component',
    diagnostics: { phase: 'find_component' } };
  const requests = inputBridge(t, f.control, failure);
  const capture = f.device.screenshotBase64.bind(f.device);
  let afterFailure = 0;
  t.mock.method(f.device, 'screenshotBase64', async () => {
    if (f.control.count) afterFailure += 1;
    return capture();
  });
  await assert.rejects(f.agent.aiAct('Input anonymous text.', { cacheable: false, abortSignal: f.state.signal }),
    /outside the editable component/);
  await f.close();
  assert.equal(requests.length, 1);
  assert.equal(afterFailure, 0);
  assert.equal(f.state.revision, 0);
  assert.equal(readTrace(f.directory).filter((record) => record.type === 'native_input_failure_observation').length, 0);
});

test('failure observation persistence errors keep the Input error while evidence flush remains fatal', async (t) => {
  for (const cancelled of [false, true]) {
    await t.test(`external cancellation: ${cancelled}`, async (child) => {
      const f = fixture(child, [action('Input', [250, 500], { value: INPUT_SECRET })]);
      inputBridge(child, f.control, inputFailure());
      const input = f.device.actions.find((item) => item.name === 'Input')!;
      const call = input.call.bind(input);
      let primary: unknown, propagated: unknown;
      let settle: () => void = () => {};
      const settled = new Promise<void>((resolve) => { settle = resolve; });
      child.mock.method(input, 'call', async (...args: Parameters<typeof input.call>) => {
        try { return await call(...args); } catch (error) { primary = error; throw error; } finally { settle(); }
      });
      const cancellation = new DOMException('Anonymous cancellation', 'AbortError');
      const store = (f.evidence as any).image.bind(f.evidence);
      child.mock.method(f.evidence as any, 'image', async (...args: any[]) => {
        if (f.control.count) {
          if (cancelled) f.state.controller.abort(cancellation);
          throw new TypeError(INPUT_SECRET);
        }
        return store(...args);
      });
      const expected = cancelled ? cancellation.message : inputFailure().message;
      await assert.rejects(f.agent.aiAct('Input anonymous text.', { cacheable: false, abortSignal: f.state.signal }),
        (error: unknown) => { propagated = error; return (error as Error).message === expected; });
      await settled;
      assert.equal((primary as Error).message, inputFailure().message);
      assert.equal(propagated, cancelled ? cancellation : primary);
      assert.equal(f.state.signal.reason, propagated);
      await assert.rejects(f.close(), /Planning evidence persistence failed/);
      const observations = readTrace(f.directory)
        .filter((record) => record.type === 'native_input_failure_observation');
      assert.equal(observations.length, 1);
      assert.equal(observations[0].observation_status, 'unavailable');
      assert.equal(observations[0].image, undefined);
      assert.equal(observations[0].error_type, 'Error');
      assert.equal(f.state.revision, 0);
      const events = readFileSync(join(f.directory, 'events.jsonl'), 'utf8');
      assert.ok(events.includes(inputFailure().message));
      assert.ok(!events.includes(INPUT_SECRET));
      assert.ok(!JSON.stringify(observations).includes(INPUT_SECRET));
    });
  }
});

test('a swallowed SDK listener persistence failure aborts before native dispatch and survives cleanup', async (t) => {
  const f = fixture(t, [action('Tap', [250, 500]), COMPLETE]);
  let disrupted = false;
  f.agent.addDumpUpdateListener((_raw, execution) => {
    if (disrupted || !execution?.tasks.some((task: any) => task.type === 'Planning'
      && task.status === 'finished' && task.output?.actions?.length)) return;
    disrupted = true;
    const trace = join(f.directory, 'planning-evidence', 'trace.jsonl');
    rmSync(trace);
    mkdirSync(trace);
  });
  await assert.rejects(f.agent.aiAct('Tap the anonymous control, then finish.',
    { cacheable: false, abortSignal: f.state.signal }));
  await assert.rejects(f.close(), /Planning evidence persistence failed/);
  assert.equal(disrupted, true);
  assert.equal(f.state.signal.aborted, true);
  assert.equal(f.requests.length, 1);
  assert.deepEqual(f.native, []);
});

test('unsupported custom SDK keeps direct pixel evidence with both requested deep options', async (t) => {
  t.mock.method(console, 'warn', () => {});
  const f = fixture(t, [`Thought: ${THOUGHT_SECRET}\nAction: click(start_box='(250,500)')`,
    `Thought: ${THOUGHT_SECRET}\nAction: finished(content='Anonymous fixture complete')`], 'vlm-ui-tars');
  await f.agent.aiAct('Tap the anonymous control, then finish.',
    { effort: 'deepThink', deepLocate: true, cacheable: false, abortSignal: f.state.signal });
  await f.close();
  assert.equal(f.requests.length, 2);
  assert.deepEqual(f.native, [[80, 120]]);
  const traceText = readFileSync(join(f.directory, 'planning-evidence', 'trace.jsonl'), 'utf8');
  assert.ok(!traceText.includes(THOUGHT_SECRET));
  const trace = readTrace(f.directory);
  const plans = trace.filter((record) => record.type === 'sdk_task' && record.plan && record.status === 'finished');
  assert.equal(plans.length, 2);
  assert.ok(plans.every((record) => record.plan.adapter_kind === 'custom'
    && record.plan.raw_coordinates_available === false));
  assert.deepEqual(plans[0].plan.normalized_actions[0].parameters.locate.locatedPixelResult.center, [40, 60]);
  assert.deepEqual(plans[1].plan.normalized_actions, [{ name: 'Finished', parameters: {} }]);
  const locate = trace.find((record) => record.type === 'sdk_task' && record.sub_type === 'Locate'
    && record.status === 'finished');
  assert.equal(locate.locate.source, 'plan');
  assert.equal(locate.locate.search_area, null);
  assert.equal(locate.locate.raw_reference, null);
  assert.deepEqual(locate.locate.model_requests, []);
  const dispatch = trace.find((record) => record.type === 'action_dispatch' && record.status === 'native_returned');
  assert.deepEqual(dispatch.parameters.locate.center, [80, 120]);
  assert.equal(dispatch.parameters.locate.prompt, undefined);
  assert.equal(dispatch.parameters.locate.description, undefined);
});

test('real independent Locate uses its own protocol while Insight remains the default model', async (t) => {
  const f = fixture(t, [action('Tap', [900, 900]), '{"bbox_2d":[200,400,300,600]}', COMPLETE], 'deepseek', {
    MIDSCENE_LOCATE_MODEL_NAME: 'anonymous-locate-fixture', MIDSCENE_LOCATE_MODEL_FAMILY: 'qwen3-vl',
    MIDSCENE_LOCATE_MODEL_API_KEY: API_SECRET, MIDSCENE_LOCATE_MODEL_BASE_URL: 'https://fixture.invalid/v1',
    MIDSCENE_LOCATE_MODEL_STREAM_MODE: 'non-stream', MIDSCENE_LOCATE_MODEL_RETRY_COUNT: 0,
  });
  assert.equal(f.agent.modelConfigManager.getModelConfig('insight').modelFamily, 'deepseek');
  assert.equal(f.agent.modelConfigManager.getModelConfig('locate').modelFamily, 'qwen3-vl');
  await f.agent.aiAct('Tap the anonymous control, then finish.', { cacheable: false, abortSignal: f.state.signal });
  await f.close();
  assert.deepEqual(f.requests.map((request) => request.model),
    ['anonymous-planning-fixture', 'anonymous-locate-fixture', 'anonymous-planning-fixture']);
  assert.deepEqual(f.native, [[80, 120]]);
  const trace = readTrace(f.directory);
  const locate = trace.find((record) => record.type === 'sdk_task' && record.sub_type === 'Locate'
    && record.status === 'finished');
  assert.equal(locate.locate.source, 'ai');
  assert.notEqual(locate.locate.hit_by?.from, 'Plan');
  assert.deepEqual(locate.locate.raw_coordinates, { field: 'bbox_2d', value: [200, 400, 300, 600] });
  assert.equal(locate.locate.prompt_spec.resultKey, 'bbox_2d');
  assert.deepEqual(locate.locate.element.center, [40, 60]);
  const request = trace.find((record) => record.type === 'model_request' && record.status === 'started'
    && record.task_id === locate.task_id);
  assert.equal(request.model, 'anonymous-locate-fixture');
  assert.equal(request.sub_type, 'Locate');
});

test('SDK failure completion stops before Capture or native input and remains a failed plan in evidence', async (t) => {
  const f = fixture(t, ['<complete success="false">The task stop condition was reached</complete>']);
  await assert.rejects(f.agent.aiAct('Stop with failure if the required path is unavailable.',
    { effort: 'deepThink', cacheable: false, abortSignal: f.state.signal }),
  /Task failed: The task stop condition was reached/);
  await f.close();
  assert.equal(f.requests.length, 1);
  assert.deepEqual(f.native, []);
  assert.equal(f.state.candidate, undefined);
  const trace = readTrace(f.directory);
  assert.ok(trace.some((record) => record.type === 'sdk_task' && record.sub_type === 'Plan'
    && record.status === 'failed'));
  assert.equal(trace.some((record) => ['action_dispatch', 'tool_call'].includes(record.type)), false);
});

test('deepThink discards a wrong planning point and dispatches independent Locate through task scope', async (t) => {
  const f = fixture(t, [action('Tap', [900, 900]), '<|point|>[250,500]<|/point|>', COMPLETE]);
  await f.agent.aiAct('Tap the anonymous control, then finish.',
    { effort: 'deepThink', cacheable: false, abortSignal: f.state.signal });
  await f.close();
  assert.deepEqual(f.native, [[80, 120]]);
  assert.equal(f.requests.length, 3);
  assert.ok(f.requests.every((request) => request.model === 'anonymous-planning-fixture'));
  const plan = f.snapshots.flat().find((task) => task.sub_type === 'Plan'
    && task.status === 'finished' && task.actions?.length);
  assert.deepEqual(plan.actions[0].param.locate, { prompt: LOCATOR });
  const trace = readTrace(f.directory);
  const requests = trace.filter((record) => record.type === 'model_request' && record.status === 'started');
  assert.deepEqual(requests.map((request) => request.sub_type), ['Plan', 'Locate', 'Plan']);
  const locate = trace.find((record) => record.type === 'sdk_task' && record.sub_type === 'Locate'
    && record.status === 'finished');
  assert.equal(locate.locate.source, 'ai');
  assert.notEqual(locate.locate.hit_by?.from, 'Plan');
  assert.deepEqual(locate.locate.raw_coordinates, { field: 'point', value: [250, 500] });
  assert.deepEqual(locate.locate.element.center, [40, 60]);
  assert.equal(requests[1].task_id, locate.task_id);
  const dispatch = trace.find((record) => record.type === 'action_dispatch' && record.status === 'native_returned');
  assert.deepEqual(dispatch.parameters.locate.center, [80, 120]);
  assert.equal(f.state.revision, 1);
  assert.equal(f.usage.summary().calls, 3);
  assert.equal(f.requests[2].images.length, 2);
});

test('deepThink independent Locate outside application bounds aborts without native input or replanning', async (t) => {
  const f = fixture(t, [action('Tap', [250, 500]), '<|point|>[900,900]<|/point|>']);
  t.mock.method(f.device, 'windows', async () => [{ ...structuredClone(TARGET),
    bounding_box: { x: 0, y: 0, width: 160, height: 240 },
    screen_bounds: { x: 0, y: 0, width: 160, height: 240 } }]);
  await assert.rejects(f.agent.aiAct('Tap the anonymous control, then finish.',
    { effort: 'deepThink', cacheable: false, abortSignal: f.state.signal }),
  /Tap target is outside known application window rectangles/);
  await f.close();
  assert.deepEqual(f.native, []);
  assert.equal(f.requests.length, 2);
  assert.equal(f.state.revision, 0);
  assert.equal(f.state.signal.aborted, true);
  const trace = readTrace(f.directory);
  assert.deepEqual(trace.filter((record) => record.type === 'model_request' && record.status === 'started')
    .map((record) => record.sub_type), ['Plan', 'Locate']);
  assert.equal(trace.some((record) => record.type === 'action_dispatch'), false);
  const locate = trace.find((record) => record.type === 'sdk_task' && record.sub_type === 'Locate'
    && record.status === 'finished');
  assert.equal(locate.locate.source, 'ai');
  assert.deepEqual(locate.locate.element.center, [144, 108]);
});

test('deepThink deepLocate keeps both actual locate request images and SDK final coordinates', async (t) => {
  const f = fixture(t, [action('Tap', [900, 900]),
    '<|ref|>target: anonymous control<|/ref|><|box|>[200,200,600,600]<|/box|>',
    '<|point|>[250,500]<|/point|>', COMPLETE], 'deepseek', {}, { width: 1200, height: 800, shrink: 1 });
  await f.agent.aiAct('Tap the anonymous control, then finish.',
    { effort: 'deepThink', deepLocate: true, cacheable: false, abortSignal: f.state.signal });
  await f.close();
  assert.equal(f.requests.length, 4);
  const plan = f.snapshots.flat().find((task) => task.sub_type === 'Plan' && task.status === 'finished'
    && task.actions?.length);
  assert.deepEqual(plan.actions[0].param.locate, { prompt: LOCATOR });
  const trace = readTrace(f.directory);
  const requests = trace.filter((record) => record.type === 'model_request' && record.status === 'started');
  assert.deepEqual(requests.map((record) => record.sub_type), ['Plan', 'Locate', 'Locate', 'Plan']);
  const locate = trace.find((record) => record.type === 'sdk_task' && record.sub_type === 'Locate'
    && record.status === 'finished');
  const located = f.snapshots.flat().find((task) => task.id === locate.task_id && task.status === 'finished');
  assert.equal(requests[1].task_id, locate.task_id);
  assert.equal(requests[2].task_id, locate.task_id);
  assert.ok(requests.every((record) => record.execution_id === locate.execution_id));
  assert.deepEqual(locate.locate.model_requests, requests.slice(1, 3).map((record) => ({
    request_id: record.request_id, sequence: record.sequence, images: record.images,
  })));
  assert.deepEqual(locate.locate.search_area, { left: 140, top: 60, width: 681, height: 521 });
  assert.equal(locate.locate.search_area_coordinate_space, 'screenshot_pixels');
  assert.equal(locate.locate.raw_reference, 'enlarged_search_area');
  assert.deepEqual(locate.locate.raw_coordinates, { field: 'point', value: [250, 500] });
  assert.deepEqual(locate.locate.element.center, [310.5, 320.5]);
  assert.equal(locate.locate.element_coordinate_space, 'screenshot_pixels');
  const context = f.contexts.get(located.screenshot_id)!;
  const sourceMetadata = await sharp(context.image).metadata();
  const sourceUrl = `data:image/${sourceMetadata.format};base64,${context.image.toString('base64')}`;
  const cropped = await cropByRect(sourceUrl, locate.locate.search_area);
  const zoomed = await scaleImage(cropped.imageBase64, 2);
  const zoomBytes = Buffer.from(zoomed.imageBase64.split(',')[1], 'base64');
  assert.equal(f.requests[1].hashes[0], digest(context.image));
  assert.equal(f.requests[2].hashes[0], digest(zoomBytes));
  for (const [request, size] of [[requests[1], [1200, 800]], [requests[2], [1362, 1042]]] as const) {
    const bytes = await savedImage(f.directory, request.images[0].image);
    assert.equal(digest(bytes), request.images[0].image.sha256);
    assert.deepEqual([request.images[0].image.width, request.images[0].image.height], size);
  }
  const dispatch = trace.find((record) => record.type === 'action_dispatch' && record.status === 'native_returned');
  assert.deepEqual(dispatch.parameters.locate.center, [311, 321]);
  assert.deepEqual(f.native, [[311, 321]]);
  assert.equal(f.usage.summary().calls, 4);
  for (const secret of [API_SECRET, HEADER_SECRET, THOUGHT_SECRET]) {
    assert.equal(JSON.stringify(trace).includes(secret), false);
  }
});

test('deepLocate final SDK coordinates still fail the original application scope before native dispatch', async (t) => {
  const f = fixture(t, [action('Tap', [250, 500]),
    '<|ref|>target: anonymous control<|/ref|><|box|>[200,200,600,600]<|/box|>',
    '<|point|>[900,900]<|/point|>'], 'deepseek', {}, { width: 1200, height: 800, shrink: 1 });
  t.mock.method(f.device, 'windows', async () => [{ ...structuredClone(TARGET),
    bounding_box: { x: 0, y: 0, width: 500, height: 800 },
    screen_bounds: { x: 0, y: 0, width: 500, height: 800 } }]);
  await assert.rejects(f.agent.aiAct('Tap the anonymous control, then finish.',
    { effort: 'deepThink', deepLocate: true, cacheable: false, abortSignal: f.state.signal }),
  /Tap target is outside known application window rectangles/);
  await f.close();
  assert.deepEqual(f.native, []);
  assert.equal(f.requests.length, 3);
  assert.equal(f.state.signal.aborted, true);
  const trace = readTrace(f.directory);
  assert.equal(trace.some((record) => record.type === 'action_dispatch'), false);
  const locate = trace.find((record) => record.type === 'sdk_task' && record.sub_type === 'Locate'
    && record.status === 'finished');
  assert.equal(locate.locate.raw_reference, 'enlarged_search_area');
  assert.equal(locate.locate.model_requests.length, 2);
  assert.deepEqual(locate.locate.element.center, [753, 529]);
});

for (const reasoning of ['false', 'true'] as const) {
  test(`deepThink preserves explicitly configured DeepSeek thinking=${reasoning} on Plan and Locate`, async (t) => {
    const f = fixture(t, [action('Tap', [900, 900]), '<|point|>[250,500]<|/point|>', COMPLETE], 'deepseek', {
      MIDSCENE_MODEL_REASONING_ENABLED: reasoning, MIDSCENE_MODEL_REASONING_EFFORT: 'high',
    });
    await f.agent.aiAct('Tap the anonymous control, then finish.',
      { effort: 'deepThink', cacheable: false, abortSignal: f.state.signal });
    await f.close();
    assert.equal(f.requests.length, 3);
    for (const request of f.requests) {
      assert.equal(request.body.model, 'anonymous-planning-fixture');
      assert.deepEqual(request.body.thinking, { type: reasoning === 'true' ? 'enabled' : 'disabled' });
      assert.equal(request.body.reasoning_effort, 'high');
      assert.equal(request.body.effort, undefined);
    }
  });
}

// 尺寸取自冻结来源；坐标是匿名测试值，不代表历史模型实际响应。
for (const scenario of [
  { shrink: 1, point: [573, 219], element: [1788, 456], logical: [1788, 456] },
  { shrink: 2, point: [501, 251], element: [782, 261], logical: [1564, 522] },
]) {
  test(`deepThink nested point preserves SDK rounding at 3120x2080 shrink=${scenario.shrink}`, async (t) => {
    const response = `<｜｜point｜｜>${JSON.stringify([scenario.point])}<｜｜/point｜｜>`;
    const f = fixture(t, [action('Tap', [900, 900]), response, COMPLETE], 'deepseek', {},
      { width: 3120, height: 2080, shrink: scenario.shrink });
    await f.agent.aiAct('Tap the anonymous control, then finish.',
      { effort: 'deepThink', cacheable: false, abortSignal: f.state.signal });
    await f.close();
    assert.deepEqual(f.native, [scenario.logical]);
    assert.equal(f.requests.length, 3);
    const plan = f.snapshots.flat().find((task) => task.sub_type === 'Plan'
      && task.status === 'finished' && task.actions?.length);
    assert.deepEqual(plan.actions[0].param.locate, { prompt: LOCATOR });
    const trace = readTrace(f.directory);
    const locate = trace.find((record) => record.type === 'sdk_task' && record.sub_type === 'Locate'
      && record.status === 'finished');
    assert.equal(locate.locate.source, 'ai');
    assert.notEqual(locate.locate.hit_by?.from, 'Plan');
    assert.deepEqual(locate.locate.raw_coordinates, { field: 'point', value: scenario.point });
    assert.deepEqual(locate.locate.element.center, scenario.element);
    const requests = trace.filter((record) => record.type === 'model_request' && record.status === 'started');
    assert.deepEqual(requests.map((record) => record.sub_type), ['Plan', 'Locate', 'Plan']);
    assert.equal(requests[1].task_id, locate.task_id);
    const locateImage = requests[1].images[0].image;
    assert.deepEqual([locateImage.width, locateImage.height], [3120 / scenario.shrink, 2080 / scenario.shrink]);
    const context = trace.find((record) => record.type === 'context' && record.image.sha256 === locateImage.sha256);
    assert.equal(context.shrunk_shot_to_logical_ratio, 1 / scenario.shrink);
    assert.deepEqual(context.shot_size, { width: 3120 / scenario.shrink, height: 2080 / scenario.shrink });
    const observed = trace.find((record) => record.type === 'observation'
      && record.observation_id === context.observation_id);
    assert.deepEqual([observed.image.width, observed.image.height], [3120, 2080]);
    assert.equal(locateImage.sha256, f.requests[1].hashes[0]);
    await savedImage(f.directory, locateImage);
    const dispatch = trace.find((record) => record.type === 'action_dispatch' && record.status === 'native_returned');
    assert.deepEqual(dispatch.parameters.locate.center, scenario.logical);
    assert.ok(dispatch.sequence > locate.sequence);
    assert.equal(trace.some((record) => record.type === 'diagnostic_failure'), false);
    assert.equal(f.state.revision, 1);
    if (scenario.shrink === 2) {
      assert.deepEqual(scenario.logical, [1564, 522]);
      assert.notDeepEqual(f.native[0], [1563, 522]);
    }
  });
}

test('nested independent Locate outside scope never dispatches or reuses the safe coarse plan point', async (t) => {
  const f = fixture(t, [action('Tap', [250, 500]), '<｜｜point｜｜>[[900,900]]<｜｜/point｜｜>'],
    'deepseek', {}, { width: 3120, height: 2080, shrink: 1 });
  t.mock.method(f.device, 'windows', async () => [{ ...structuredClone(TARGET),
    bounding_box: { x: 0, y: 0, width: 1560, height: 2080 },
    screen_bounds: { x: 0, y: 0, width: 1560, height: 2080 } }]);
  await assert.rejects(f.agent.aiAct('Tap the anonymous control, then finish.',
    { effort: 'deepThink', cacheable: false, abortSignal: f.state.signal }),
  /Tap target is outside known application window rectangles/);
  await f.close();
  assert.deepEqual(f.native, []);
  assert.equal(f.requests.length, 2);
  assert.equal(f.state.revision, 0);
  assert.equal(f.state.signal.aborted, true);
  const trace = readTrace(f.directory);
  const locate = trace.find((record) => record.type === 'sdk_task' && record.sub_type === 'Locate'
    && record.status === 'finished');
  assert.equal(locate.locate.source, 'ai');
  assert.deepEqual(locate.locate.raw_coordinates, { field: 'point', value: [900, 900] });
  assert.deepEqual(locate.locate.element.center, [2808, 1872]);
  assert.equal(trace.some((record) => record.type === 'action_dispatch'), false);
  assert.equal(trace.some((record) => record.type === 'diagnostic_failure'), false);
  assert.deepEqual(trace.filter((record) => record.type === 'model_request' && record.status === 'started')
    .map((record) => record.sub_type), ['Plan', 'Locate']);
});

/** @brief 仅在真实 SDK 的公开转储通知前篡改匿名终态，用于检验诊断失败的完整收尾。 */
function invalidLocateSnapshot(t: TestContext, f: ReturnType<typeof fixture>, stage: string) {
  const original = f.agent.writeOutActionDumps.bind(f.agent);
  const injected: { binding?: Record<string, unknown>; persistedSynchronously?: boolean } = {};
  t.mock.method(f.agent, 'writeOutActionDumps', (execution: any) => {
    const task = execution?.tasks.find((item: any) => item.subType === 'Locate' && item.status === 'finished');
    if (!task || injected.binding) return original(execution);
    injected.binding = { execution_id: execution.id, task_id: task.taskId, sdk_task_type: task.type,
      sub_type: task.subType, status: task.status, screenshot_id: task.uiContext?.screenshot.id ?? null };
    task.param = { prompt: INPUT_SECRET, private_input: THOUGHT_SECRET };
    task.output.element.description = HEADER_SECRET;
    task.output.element.prompt = THOUGHT_SECRET;
    if (stage === 'element_geometry') task.output.element.center = [API_SECRET, 20];
    if (stage === 'search_area_geometry') {
      task.searchArea = { left: INPUT_SECRET, top: 0, width: 1, height: 1, private_field: THOUGHT_SECRET };
    }
    if (stage === 'raw_protocol') task.log.dump.taskInfo.rawResponse = `${INPUT_SECRET} ${HEADER_SECRET}`;
    if (stage === 'raw_coordinates') {
      task.log.dump.taskInfo.rawResponse = `<｜｜point｜｜>[[1,2],[3,"${INPUT_SECRET}"]]<｜｜/point｜｜>`;
    }
    return original(execution);
  });
  f.agent.addDumpUpdateListener((_raw, execution) => {
    if (!injected.binding || !execution?.tasks.some((task) => task.taskId === injected.binding!.task_id
      && task.status === 'finished')) return;
    // 同一次 SDK 通知中，后续监听器不等待任何异步写入就应读到失败记录。
    injected.persistedSynchronously = readTrace(f.directory).some((record) => record.type === 'diagnostic_failure'
      && record.task_id === injected.binding!.task_id);
  });
  return injected;
}

for (const stage of ['element_geometry', 'search_area_geometry', 'raw_protocol', 'raw_coordinates']) {
  test(`Locate ${stage} failure preserves bounded diagnostic binding and remains fatal`, async (t) => {
    const f = fixture(t, [action('Tap', [900, 900]), '<｜｜point｜｜>[250,500]<｜｜/point｜｜>']);
    const injected = invalidLocateSnapshot(t, f, stage);
    await assert.rejects(f.agent.aiAct('Tap the anonymous control, then finish.',
      { effort: 'deepThink', cacheable: false, abortSignal: f.state.signal }));
    await assert.rejects(f.evidence.flush(), /Planning evidence persistence failed/);
    await assert.rejects(f.close(), /Planning evidence persistence failed/);
    assert.ok(injected.binding);
    assert.equal(injected.persistedSynchronously, true);
    assert.equal(f.state.signal.aborted, true);
    assert.equal(f.requests.length, 2);
    assert.deepEqual(f.native, []);
    const trace = readTrace(f.directory);
    const failures = trace.filter((record) => record.type === 'diagnostic_failure');
    assert.equal(failures.length, 1);
    const failed = failures[0];
    assert.equal(failed.projection, 'locate');
    assert.equal(failed.projection_stage, stage);
    assert.equal(failed.raw_response_available, true);
    assert.deepEqual(failed.sdk_element, stage === 'element_geometry' ? null : { center: [40, 60] });
    for (const [key, value] of Object.entries(injected.binding!)) assert.deepEqual(failed[key], value);
    assert.ok(['element_geometry', 'search_area_geometry', 'raw_protocol', 'raw_coordinates']
      .includes(failed.projection_stage));
    assert.equal(trace.some((record) => record.type === 'action_dispatch'), false);
    const text = readFileSync(join(f.directory, 'planning-evidence', 'trace.jsonl'), 'utf8');
    for (const secret of [API_SECRET, HEADER_SECRET, INPUT_SECRET, THOUGHT_SECRET]) assert.ok(!text.includes(secret));
    for (const key of ['rawResponse', 'rawAssistantOutput', 'error', 'message', 'stack', 'param', 'output']) {
      assert.equal(failed[key], undefined);
    }
  });
}
