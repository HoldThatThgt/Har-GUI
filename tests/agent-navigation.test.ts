import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import type { ExecutorContext } from '@midscene/core';
import type { LoadedConfiguration } from '../agent/config.js';
import { CollectionDevice } from '../agent/device.js';
import { RunState, type WindowInfo } from '../agent/state.js';
import { createRegistry } from '../agent/tools.js';
import { writeFixtureConfiguration } from './agent-configuration.js';
import { PAGE_PNG, writePageObservation } from './agent-page-fixture.js';

const TARGET_BUNDLE = 'org.example.browser';
const ENABLED = ['ReadWindows', 'ListApplications', 'ReadApplication', 'LaunchApplication', 'Tap', 'Capture'];

// 子进程只返回匿名应用声明与窗口事实；动作注册、范围检查和观察版本仍由生产代码执行。
const BRIDGE = `
const fs=require('node:fs');let input='';
process.stdin.on('data',part=>input+=part).on('end',()=>{
  const filename=process.argv[1],plan=JSON.parse(fs.readFileSync(filename,'utf8'));
  const request=JSON.parse(input);plan.requests.push(request);let result,error;
  if(request.operation==='read_windows') result={windows:plan.launched?plan.after:plan.before};
  else if(request.operation==='list_applications') {
    const matches=plan.applications.filter(app=>!request.query||app.bundle_name.includes(request.query));
    const offset=request.offset??0,limit=request.limit??20,end=Math.min(offset+limit,matches.length);
    result={applications:matches.slice(offset,end),total:matches.length,offset,limit,
      next_offset:end<matches.length?end:null};
  } else if(request.operation==='read_application'&&request.bundle_name===plan.target_bundle) {
    result={bundle_name:plan.target_bundle,abilities:plan.abilities};
  } else if(request.operation==='launch_application'&&request.bundle_name===plan.target_bundle) {
    const ability=plan.abilities.find(item=>item.ability_name===request.ability_name&&item.launcher);
    if(!ability) error={message:'Fixture needs an explicitly returned launcher ability'};
    else {plan.launched=true;result={bundle_name:plan.target_bundle,...ability,launched:true};}
  } else if(request.operation==='capture') result=plan.capture;
  else error={message:'Unexpected navigation fixture operation'};
  fs.writeFileSync(filename,JSON.stringify(plan));
  process.stdout.write(JSON.stringify(error?{error}:{result}));
});`;

/** @brief 用不同屏幕区域表示目标、其他普通应用与系统 Dock。 */
function windowInfo(overrides: Partial<WindowInfo> = {}): WindowInfo {
  const box = { x: 0, y: 0, width: 4, height: 3 };
  return { window_id: 103, bundle_name: TARGET_BUNDLE, process_id: 203, display_id: 0,
    ui_node_id: 503, z_order: 10, window_mode: 102, is_system: false, focused: false,
    bounding_box: box, screen_bounds: box, ...overrides };
}

/** @brief 创建不与目标物理矩形重叠的其他应用和系统窗口。 */
function surroundings(): WindowInfo[] {
  const other = { x: 100, y: 0, width: 100, height: 100 };
  const dock = { x: 0, y: 180, width: 300, height: 20 };
  return [windowInfo({ window_id: 90, bundle_name: 'org.example.editor', focused: true, z_order: 20,
    bounding_box: other, screen_bounds: other }),
  windowInfo({ window_id: 500, bundle_name: 'system.dock', is_system: true, focused: false, z_order: 100,
    bounding_box: dock, screen_bounds: dock })];
}

/** @brief 按当前项目格式建立无需凭据的真实运行状态。 */
function configuration(directory: string, bundleNames: string[] | null): LoadedConfiguration {
  return { projectPath: join(directory, 'project.toml'), configPath: join(directory, 'config.toml'),
    outputPath: join(directory, 'runs'), datasetPath: join(directory, 'dataset'), digests: { project: '', config: '' },
    profile: { provider: 'openai_compatible', base_url: 'https://unused.fixture.invalid/v1', model: 'fixture',
      model_family: 'deepseek', api_key_env: 'UNUSED_NAVIGATION_TEST_KEY', supports_vision: true },
    project: { schema_version: 1,
      run: { name: 'navigation', output: 'runs', max_actions: 30, max_duration_seconds: 60 },
      agent: { llm: 'vision' }, tools: { enabled: [...ENABLED] },
      dataset: { root: 'dataset', canvas: { width: 4, height: 3 }, stability_regions: [] },
      tasks: [{ name: 'target-page', app: 'Browser', scene: 'Page', instruction: 'Collect the requested visible page',
        acceptance: 'The requested page is present', target_count: 1, privacy: { enabled: false, reason: 'Fixture' },
        ...(bundleNames === null ? {} : { bundle_names: [...bundleNames] }) }] } };
}

/** @brief 生成明确标识为合成的本地成对观察，只供真实 Capture 动作保存候选。 */
function captureReply(directory: string, target: WindowInfo) {
  const capture = join(directory, 'captures', 'scripted-frame');
  mkdirSync(capture, { recursive: true });
  const digest = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex');
  const raw = Buffer.from('{}');
  const metadata = { capture_id: 'scripted-frame', windows: [target], files: {
    screenshot: { file: 'screenshot.png', sha256: digest(PAGE_PNG) },
    windows: { sha256: digest(raw) }, closing_windows: { sha256: digest(raw) },
    layout: { sha256: digest(raw) }, closing_layout: { sha256: digest(raw) },
  } };
  writeFileSync(join(capture, 'screenshot.png'), PAGE_PNG);
  const paired = writePageObservation(capture, metadata, target.window_id,
    { before: PAGE_PNG, after: PAGE_PNG, afterScreen: PAGE_PNG });
  writeFileSync(join(capture, 'metadata.json'), JSON.stringify(metadata));
  return { capture_id: metadata.capture_id, path: capture, page_observation: paired };
}

/** @brief 将真实桥接的每次请求发给本地 Node 替身，禁止启动 Python、HDC 或网络。 */
function interceptBridge(t: TestContext, planPath: string): void {
  const original = childProcess.spawn;
  const spawn = t.mock.method(childProcess, 'spawn', (_executable: any, args: any, options: any) => {
    assert.equal(basename(String(args[0])), 'agent_device.py', 'Unexpected device or model process');
    return original(process.execPath, ['-e', BRIDGE, planPath], options);
  });
  syncBuiltinESMExports();
  t.after(() => { spawn.mock.restore(); syncBuiltinESMExports(); });
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('Unexpected navigation model or network call'); });
}

/** @brief 建立真实 CollectionDevice、注册器、审计事件及可重新绑定的观察。 */
function fixture(t: TestContext, before: WindowInfo[], bundleNames: string[] | null = [TARGET_BUNDLE]) {
  const directory = mkdtempSync(join(tmpdir(), 'har-gui-navigation-test-'));
  t.after(() => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    assert.ok(basename(directory).startsWith('har-gui-navigation-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  const target = windowInfo();
  const after = [target, ...surroundings()];
  const config = configuration(directory, bundleNames);
  writeFixtureConfiguration(config);
  const state = new RunState(config, directory);
  const device = new CollectionDevice({ python: 'unused-python', hdc: 'unused-hdc', serial: 'fixture' }, state);
  const native = t.mock.method(device, 'nativeActions', () => { throw new Error('Unexpected native input lookup'); });
  const hdc = t.mock.method(device, 'getHdc', async () => { throw new Error('Unexpected HDC connection'); });
  const planPath = join(directory, 'bridge.json');
  writeFileSync(planPath, JSON.stringify({ before, after, requests: [], launched: false, target_bundle: TARGET_BUNDLE,
    applications: [{ bundle_name: TARGET_BUNDLE, user_id: 100 }, { bundle_name: 'org.example.editor', user_id: 100 }],
    abilities: [{ ability_name: 'ReaderAbility', module_name: 'reader', launcher: true },
      { ability_name: 'WorkspaceAbility', module_name: 'desktop', launcher: true }],
    capture: captureReply(directory, target) }));
  interceptBridge(t, planPath);
  const actions = createRegistry(() => device).actions(state);
  const invoke = async (name: string, parameters: object = {}) => {
    const action = actions.find((item) => item.name === name);
    assert.ok(action, `Action was not enabled: ${name}`);
    const context = { task: {} } as ExecutorContext;
    const result = await action.call(parameters, context);
    assert.deepEqual(JSON.parse(context.task.planningFeedback!), result);
    return result;
  };
  const bind = (windows: WindowInfo[]) => {
    device.planningObservation = { id: `planning-${state.revision}`, revision: state.revision,
      windows: structuredClone(windows) };
  };
  bind(before);
  return { directory, state, device, config, invoke, bind, before, after, target, native, hdc,
    requests: () => JSON.parse(readFileSync(planPath, 'utf8')).requests as any[] };
}

/** @brief 只使用前一动作实际返回的应用标识与声明入口执行启动。 */
async function launchReturnedEntry(f: ReturnType<typeof fixture>, absent: string) {
  const listed = await f.invoke('ListApplications', { query: absent });
  assert.equal(listed.total, 1);
  const details = await f.invoke('ReadApplication', { bundle_name: listed.applications[0].bundle_name });
  assert.equal(details.abilities.filter((ability: any) => ability.launcher).length, 2);
  const entry = details.abilities.find((ability: any) => ability.module_name === 'desktop' && ability.launcher);
  assert.ok(entry);
  return f.invoke('LaunchApplication', { bundle_name: details.bundle_name, ability_name: entry.ability_name });
}

/** @brief 采集身份只来自 ReadWindows 返回的任务窗口与完整窗口事实。 */
async function captureReturnedWindow(f: ReturnType<typeof fixture>, read: any) {
  const target = read.windows.find((window: WindowInfo) =>
    read.task_scope.visible_window_ids.includes(window.window_id));
  assert.ok(target);
  return f.invoke('Capture', { target_window_id: target.window_id, target_bundle_name: target.bundle_name,
    page_name: 'requested-page', variation: 'original visible page' });
}

test('a visible target can be captured directly without native input or application launch', async (t) => {
  const f = fixture(t, [windowInfo(), ...surroundings()]);
  const allowed = structuredClone(f.state.task.bundle_names);
  const read = await f.invoke('ReadWindows');
  const result = await captureReturnedWindow(f, read);
  assert.equal(result.pending_acceptance, true);
  assert.deepEqual(f.requests().map((request) => request.operation), ['read_windows', 'read_windows', 'capture']);
  assert.equal(f.native.mock.callCount(), 0);
  assert.equal(f.hdc.mock.callCount(), 0);
  assert.equal(f.state.revision, 0);
  assert.equal(f.state.signal.aborted, false);
  const candidate = JSON.parse(readFileSync(join(result.path, 'candidate.json'), 'utf8'));
  assert.equal(candidate.target_window_id, f.target.window_id);
  assert.equal(candidate.target_bundle_name, f.target.bundle_name);
  assert.ok(f.device.pageObservation);
  assert.deepEqual(f.state.task.bundle_names, allowed);
  assert.deepEqual(f.config.project.tools.enabled, ENABLED);
});

test('a missing target uses a returned entry then a new observation before capture', async (t) => {
  const f = fixture(t, surroundings());
  const absent = await f.invoke('ReadWindows');
  assert.deepEqual(absent.task_scope.visible_window_ids, []);
  assert.deepEqual(absent.task_scope.absent_bundle_names, [TARGET_BUNDLE]);
  const launched = await launchReturnedEntry(f, absent.task_scope.absent_bundle_names[0]);
  assert.equal(launched.launched, true);
  assert.equal(launched.module_name, 'desktop');
  assert.equal(f.state.revision, 1);
  f.bind(f.after);
  const current = await f.invoke('ReadWindows');
  assert.notEqual(current.observation_id, absent.observation_id);
  assert.deepEqual(current.task_scope.absent_bundle_names, []);
  const result = await captureReturnedWindow(f, current);
  const requests = f.requests();
  const navigation = requests.filter((request) => request.operation !== 'read_windows');
  assert.deepEqual(navigation.map((request) => request.operation),
    ['list_applications', 'read_application', 'launch_application', 'capture']);
  assert.deepEqual(requests.find((request) => request.operation === 'launch_application'),
    { operation: 'launch_application', bundle_name: launched.bundle_name, ability_name: launched.ability_name });
  assert.equal(f.state.candidate.path, result.path);
  assert.equal(f.state.candidate.target_window_id, current.task_scope.visible_window_ids[0]);
  assert.equal(f.native.mock.callCount(), 0);
  assert.equal(f.hdc.mock.callCount(), 0);
  assert.equal(f.state.signal.aborted, false);
});

test('launch invalidates the old planning observation before another window query or capture can run', async (t) => {
  const f = fixture(t, surroundings());
  const absent = await f.invoke('ReadWindows');
  await launchReturnedEntry(f, absent.task_scope.absent_bundle_names[0]);
  const count = f.requests().length;
  await assert.rejects(f.invoke('ReadWindows'), /Planning observation is stale/);
  assert.equal(f.requests().length, count);
  assert.equal(f.state.candidate, undefined);
  assert.equal(f.state.revision, 1);
  assert.equal(f.state.signal.aborted, true);
  assert.equal(f.native.mock.callCount(), 0);
});

test('a Dock point remains outside task scope and is rejected before native action lookup', async (t) => {
  const f = fixture(t, surroundings());
  const allowed = structuredClone(f.state.task.bundle_names);
  const read = await f.invoke('ReadWindows');
  assert.deepEqual(read.task_scope.visible_window_ids, []);
  await assert.rejects(f.invoke('Tap', { locate: { center: [150, 190] } }),
    /outside known application window rectangles/);
  assert.equal(f.native.mock.callCount(), 0);
  assert.equal(f.hdc.mock.callCount(), 0);
  assert.equal(f.state.signal.aborted, true);
  assert.equal(f.state.revision, 0);
  assert.deepEqual(f.state.changes, []);
  assert.deepEqual(f.state.task.bundle_names, allowed);
  assert.deepEqual(f.config.project.tools.enabled, ENABLED);
  assert.deepEqual(f.requests().map((request) => request.operation), ['read_windows', 'read_windows']);
  const lines = readFileSync(join(f.directory, 'events.jsonl'), 'utf8').trim().split('\n');
  const events = lines.map((line) => JSON.parse(line));
  const failure = events.find((event) => event.type === 'action_failed');
  assert.equal(failure.name, 'Tap');
  assert.equal(failure.diagnostics.phase, 'task_scope');
});

test('scoped feedback keeps background targets and reports absent bundles without changing windows', async (t) => {
  const allowed = [TARGET_BUNDLE, 'org.example.notes'];
  const windows = [windowInfo(), ...surroundings()];
  const f = fixture(t, windows, allowed);
  const result = await f.invoke('ReadWindows');
  assert.deepEqual(result.task_scope, { bundle_names: allowed, visible_window_ids: [103],
    absent_bundle_names: ['org.example.notes'] });
  assert.deepEqual(result.windows, windows);
  assert.equal(result.windows.find((window: WindowInfo) => window.window_id === 103).focused, false);
  assert.equal(result.windows.find((window: WindowInfo) => window.window_id === 90).focused, true);
  assert.deepEqual(f.state.task.bundle_names, allowed);
  assert.deepEqual(f.requests().map((request) => request.operation), ['read_windows']);
  assert.equal(f.state.revision, 0);
});

test('unrestricted feedback includes all ordinary apps and excludes system windows from task IDs', async (t) => {
  const windows = [windowInfo(), ...surroundings()];
  const f = fixture(t, windows, null);
  const result = await f.invoke('ReadWindows');
  assert.deepEqual(result.task_scope, { bundle_names: null, visible_window_ids: [103, 90], absent_bundle_names: [] });
  assert.deepEqual(result.windows, windows);
  assert.equal(f.state.task.bundle_names, undefined);
  assert.equal(f.native.mock.callCount(), 0);
  assert.equal(f.hdc.mock.callCount(), 0);
});

test('launching an application outside the unchanged allowlist fails before any bridge or native action', async (t) => {
  const f = fixture(t, surroundings());
  const allowed = structuredClone(f.state.task.bundle_names);
  await assert.rejects(f.invoke('LaunchApplication', {
    bundle_name: 'org.example.editor', ability_name: 'WorkspaceAbility' }),
    /Bundle is outside task scope/);
  assert.deepEqual(f.requests(), []);
  assert.deepEqual(f.state.task.bundle_names, allowed);
  assert.deepEqual(f.config.project.tools.enabled, ENABLED);
  assert.equal(f.state.revision, 0);
  assert.equal(f.state.signal.aborted, true);
  assert.equal(f.native.mock.callCount(), 0);
  assert.equal(f.hdc.mock.callCount(), 0);
});
