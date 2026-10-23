import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { writeFile } from 'node:fs/promises';
import { syncBuiltinESMExports } from 'node:module';
import { basename, dirname, join, resolve, sep } from 'node:path';
import { test, type TestContext } from 'node:test';
import sharp from 'sharp';
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
async function image(applied: number) {
  return sharp({ create: { width: 320, height: 240, channels: 3,
    background: { r: applied * 70, g: 60, b: 120 } } }).png().toBuffer();
}

/** @brief 真实 Input 桥接仅连接本地 Node 子进程，不启动 Python、HDC 或 shell。 */
function inputBridge(t: TestContext, applied: { count: number }) {
  const requests: any[] = [];
  const original = childProcess.spawn;
  const script = `let text='';process.stdin.on('data',part=>text+=part);process.stdin.on('end',()=>{
    const request=JSON.parse(text);const result={window_id:request.window_id,mode:request.mode,verified:true,
      character_count:Array.from(request.value).length,
      text_sha256:require('node:crypto').createHash('sha256').update(request.value).digest('hex')};
    process.stdout.write(JSON.stringify({result}));});`;
  const spawn = t.mock.method(childProcess, 'spawn', (_program: any, args: any, options: any) => {
    assert.equal(basename(String(args[0])), 'agent_device.py');
    const child = original(process.execPath, ['-e', script], options);
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
function deviceIO(t: TestContext, state: RunState, control: { count: number; missingAfter: boolean }) {
  const native: Array<[number, number]> = [];
  const images: Buffer[] = [];
  const device = new CollectionDevice({ python: 'unused-python', hdc: 'unused-hdc', serial: 'fixture' }, state);
  t.mock.method(device, 'size', async () => ({ width: 320, height: 240 }));
  t.mock.method(device, 'windows', async () => [structuredClone(TARGET)]);
  t.mock.method(device, 'getHdc', async () => ({
    async shell(command: string) {
      if (command.startsWith('rm -f ')) return '';
      assert.ok(command.startsWith('uitest screenCap -d 0 -p '));
      if (control.missingAfter && control.count) throw new Error('Anonymous after-action screenshot unavailable');
      return `ScreenCap saved to ${command.split(' -p ')[1]}`;
    },
    async fileRecv(_remote: string, local: string) {
      const bytes = await image(control.count);
      images.push(bytes);
      await writeFile(local, bytes);
    },
    async click(x: number, y: number) { native.push([x, y]); control.count += 1; },
  }) as any);
  return { device, native, images };
}

/** @brief 使用真实 SDK 和用量封装，仅在公开工厂处返回脚本化客户端。 */
function sdk(evidence: PlanningEvidence, device: CollectionDevice, directory: string,
  answers: string[], family: string) {
  const requests: Array<{ hashes: string[]; images: Buffer[] }> = [];
  const usage = new UsageRecorder(join(directory, 'model-usage.jsonl'));
  const factory = evidence.clientFactory(usage.createOpenAIClient);
  const agent = new CollectionAgent(device, { cache: false, generateReport: false,
    persistExecutionDump: false, autoPrintReportMsg: false, screenshotShrinkFactor: 2,
    waitAfterAction: 0, replanningCycleLimit: 5,
    modelConfig: { MIDSCENE_MODEL_FAMILY: family, MIDSCENE_MODEL_NAME: 'anonymous-planning-fixture',
      MIDSCENE_MODEL_API_KEY: API_SECRET, MIDSCENE_MODEL_BASE_URL: 'https://fixture.invalid/v1',
      MIDSCENE_MODEL_STREAM_MODE: 'non-stream', MIDSCENE_MODEL_RETRY_COUNT: 0 },
    createOpenAIClient: async (client, options) => {
      const fake = { chat: { completions: { create: async (body: any) => {
        const parts = body.messages.flatMap((message: any) => Array.isArray(message.content) ? message.content : []);
        const images = parts.filter((part: any) => part.type === 'image_url')
          .map((part: any) => Buffer.from(part.image_url.url.split(',')[1], 'base64'));
        const content = answers[requests.length];
        assert.equal(typeof content, 'string', 'Unexpected extra model request');
        requests.push({ images, hashes: images.map(digest) });
        return { id: `anonymous-${requests.length}`, object: 'chat.completion', created: 1,
          model: 'anonymous-planning-fixture', usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
          choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content } }] };
      } } } };
      return factory(fake as typeof client, { ...options, headers: { Authorization: HEADER_SECRET } } as any);
    } });
  return { agent, usage, requests };
}

/** @brief 所有证据与临时文件都位于本测试新建的匿名目录，绝不访问真实采集。 */
function fixture(t: TestContext, answers: string[], family = 'deepseek') {
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
  const io = deviceIO(t, state, control);
  io.device.actions = createRegistry(() => io.device).actions(state);
  const model = sdk(evidence, io.device, directory, answers, family);
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
  await f.agent.aiAct('Perform the two anonymous taps, then finish.', { cacheable: false, abortSignal: f.state.signal });
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
  const images = [...trace.filter((record) => ['context', 'observation'].includes(record.type)).map((record) => record.image),
    ...modelRequests.flatMap((request) => request.images.map((item: any) => item.image)),
    ...observed.flatMap((observation) => observation.after.map((item: any) => item.image))];
  for (const info of images) await savedImage(f.directory, info);
  assert.doesNotMatch(readFileSync(join(f.directory, 'planning-evidence', 'trace.jsonl'), 'utf8'), /data:image/);
});

test('real SDK Input keeps diagnostic locator text without persisting input, thought, key or factory headers', async (t) => {
  const f = fixture(t, [action('Input', [250, 500], { value: INPUT_SECRET, mode: 'replace' }), COMPLETE]);
  const requests = inputBridge(t, f.control);
  await f.agent.aiAct('Edit the anonymous field, then finish.', { cacheable: false, abortSignal: f.state.signal });
  await f.close();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].value, INPUT_SECRET);
  assert.deepEqual([requests[0].x, requests[0].y], [80, 120]);
  assert.equal(f.requests.length, 2);
  const dispatches = readTrace(f.directory).filter((record) => record.type === 'action_dispatch');
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

test('real custom SDK planning preserves pixel evidence and completion without copying derived thought prompts', async (t) => {
  const f = fixture(t, [`Thought: ${THOUGHT_SECRET}\nAction: click(start_box='(250,500)')`,
    `Thought: ${THOUGHT_SECRET}\nAction: finished(content='Anonymous fixture complete')`], 'vlm-ui-tars');
  await f.agent.aiAct('Tap the anonymous control, then finish.', { cacheable: false, abortSignal: f.state.signal });
  await f.close();
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
  const dispatch = trace.find((record) => record.type === 'action_dispatch' && record.status === 'native_returned');
  assert.deepEqual(dispatch.parameters.locate.center, [80, 120]);
  assert.equal(dispatch.parameters.locate.prompt, undefined);
  assert.equal(dispatch.parameters.locate.description, undefined);
});
