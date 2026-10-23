import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import type { LoadedConfiguration } from '../agent/config.js';
import type { CollectionAgent, CollectionDevice } from '../agent/device.js';
import { acceptCandidate } from '../agent/acceptance.js';
import { finalize } from '../agent/finalize.js';
import { RunState } from '../agent/state.js';
import { createRegistry } from '../agent/tools.js';
import { FIXTURE_DELIVERY, writeFixtureConfiguration } from './agent-configuration.js';
import { PAGE_PNG, writePageDecision, writePageObservation } from './agent-page-fixture.js';

const REGIONS = [{ bundle_name: 'system.fixture', resource_id: 'clock-leaf' }];

/** 在真实桥接标准输入边界记录请求，并在准备前停止，不访问设备或模型。 */
function interceptBridge(t: TestContext, directory: string, captured?: object) {
  const log = join(directory, 'requests.jsonl');
  const original = childProcess.spawn;
  const script = `let text='';process.stdin.on('data',part=>text+=part);process.stdin.on('end',()=>{
    const request=JSON.parse(text);require('node:fs').appendFileSync(process.argv[1],JSON.stringify(request)+'\\n');
    const capture=JSON.parse(process.argv[2]);
    const result=request.operation==='capture'&&capture?{result:capture}
      :request.operation==='dataset_status'?{result:{samples:[],counts:{},remaining:{},failures:[]}}
      :{error:{message:'Fixture stopped after receiving request'}};process.stdout.write(JSON.stringify(result));});`;
  const mock = t.mock.method(childProcess, 'spawn', () => original(process.execPath,
    ['-e', script, log, JSON.stringify(captured ?? null)]));
  syncBuiltinESMExports();
  t.after(() => { mock.mock.restore(); syncBuiltinESMExports(); });
  return () => readFileSync(log, 'utf8').trim().split('\n').map((line) => JSON.parse(line));
}

/** 建立只用于传参验证的本地候选，所有请求均由上面的进程替身截获。 */
function fixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), 'har-gui-stability-test-'));
  t.after(() => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    assert.ok(basename(directory).startsWith('har-gui-stability-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  const capture = join(directory, 'capture');
  mkdirSync(capture);
  const files: Record<string, any> = Object.fromEntries(['screenshot.png', 'tree.json', 'windows.txt', 'closing-windows.txt',
    'layout.json', 'closing-layout.json'].map((name) => {
    const data = name === 'screenshot.png' ? PAGE_PNG : Buffer.from('{}');
    writeFileSync(join(capture, name), data);
    return [name.replaceAll('-', '_').split('.')[0],
      { file: name, bytes: data.length, sha256: createHash('sha256').update(data).digest('hex') }];
  }));
  const window = { window_id: 1, process_id: 2, ui_node_id: 3, display_id: 0, z_order: 4,
    is_system: false, focused: true, window_mode: 1,
    bundle_name: 'app.fixture', bounding_box: { x: 0, y: 0, width: 4, height: 3 },
    screen_bounds: { x: 0, y: 0, width: 4, height: 3 },
    trees: { arkui: { file: 'tree.json', node_count: 1 }, accessibility: { file: 'tree.json', node_count: 1 } } };
  const metadata = { capture_id: 'frame', status: 'complete', display: { id: 0, width: 4, height: 3 },
    files, windows: [window], window_inventory: [window],
    coverage: { status: 'verified', covered_window_ids: [1], excluded_windows: [], unresolved_windows: [] },
    foreground_window_ids_without_tree: [], consistency: { status: 'observed',
      pixels: { status: 'observed', changed: true } } };
  metadata.files.screenshot.width = 4;
  metadata.files.screenshot.height = 3;
  const pageObservation = writePageObservation(capture, metadata, 1,
    { before: PAGE_PNG, after: PAGE_PNG, afterScreen: PAGE_PNG });
  writeFileSync(join(capture, 'metadata.json'), JSON.stringify(metadata));
  const candidate = { capture_id: 'frame', path: capture, task_name: 'fixture', target_window_id: 1,
    target_bundle_name: 'app.fixture',
    page_name: 'fixture-page', window_changes: [] };
  writeFileSync(join(capture, 'candidate.json'), JSON.stringify(candidate));
  const config = { projectPath: join(directory, 'project.toml'), configPath: join(directory, 'config.toml'),
    outputPath: join(directory, 'runs'), datasetPath: join(directory, 'dataset'),
    profile: { provider: 'openai_compatible', base_url: 'https://model.example.test/v1', model: 'fixture',
      model_family: 'deepseek', api_key_env: 'UNUSED_STABILITY_TEST_KEY', supports_vision: true },
    digests: { project: '', config: '' }, project: { schema_version: 1,
      run: { name: 'fixture', output: 'runs', max_duration_seconds: 60, max_actions: 5 },
      agent: { llm: 'vision' }, tools: { enabled: ['Capture'] },
      dataset: { root: 'dataset', canvas: { width: 4, height: 3 }, stability_regions: REGIONS },
      delivery: structuredClone(FIXTURE_DELIVERY),
      tasks: [{ name: 'fixture', app: 'Fixture', scene: 'Page', instruction: 'Inspect', acceptance: 'Pass',
        target_count: 1, bundle_names: ['app.fixture'], privacy: { enabled: false, reason: 'Synthetic fixture' } }],
    } } as LoadedConfiguration;
  writeFixtureConfiguration(config);
  const agent = { aiAsk: async () => JSON.stringify({ decision: 'same_page',
    evidence: 'Mocked parameter propagation fixture' }) } as unknown as CollectionAgent;
  return { directory, capture, candidate, config, window, metadata, pageObservation, agent };
}

test('Capture passes project stability selectors to the raw capture request', async (t) => {
  const f = fixture(t);
  const requests = interceptBridge(t, f.directory);
  const state = new RunState(f.config, f.directory, f.directory);
  const device = { state, connection: { python: 'unused', hdc: 'unused', serial: 'fixture' },
    planningObservation: { id: 'observation', revision: 0, windows: [f.window] },
    windows: async () => [f.window] } as unknown as CollectionDevice;
  const action = createRegistry(() => device).actions(state)[0];
  await assert.rejects(action.call({ page_name: 'Page', variation: 'original', target_window_id: 1,
    target_bundle_name: 'app.fixture' }),
    /Fixture stopped/);
  assert.equal(requests()[0].operation, 'capture');
  assert.deepEqual(requests()[0].stability_regions, REGIONS);
  assert.equal(requests()[0].target_window_id, 1);
  assert.equal(requests()[0].target_bundle_name, 'app.fixture');
});

test('Capture stores paired images only in device memory and persists an explicit target identity', async (t) => {
  const f = fixture(t);
  const requests = interceptBridge(t, f.directory, { capture_id: 'frame', path: f.capture,
    page_observation: f.pageObservation });
  const state = new RunState(f.config, f.directory, f.directory);
  const device = { state, connection: { python: 'unused', hdc: 'unused', serial: 'fixture' },
    planningObservation: { id: 'observation', revision: 0, windows: [f.window] },
    windows: async () => [f.window], pageObservation: undefined } as unknown as CollectionDevice;
  const action = createRegistry(() => device).actions(state)[0];
  const result = await action.call({ page_name: 'Page', variation: 'cursor blink', target_window_id: 1,
    target_bundle_name: 'app.fixture' });
  assert.equal(requests()[0].target_window_id, 1);
  assert.equal(state.candidate.target_window_id, 1);
  assert.deepEqual(device.pageObservation, f.pageObservation);
  for (const text of [JSON.stringify(result), JSON.stringify(state.candidate),
    readFileSync(join(f.capture, 'candidate.json'), 'utf8'), readFileSync(join(f.directory, 'events.jsonl'), 'utf8')]) {
    assert.doesNotMatch(text, /data:image|before_image|after_image|page_observation/);
  }
});

test('Capture rejects missing or mismatched explicit target windows before the bridge', async (t) => {
  for (const parameters of [{ target_bundle_name: 'app.fixture' },
    { target_window_id: 2, target_bundle_name: 'app.fixture' },
    { target_window_id: 1, target_bundle_name: 'app.other' }]) {
    await t.test(JSON.stringify(parameters), async (child) => {
      const f = fixture(child);
      const state = new RunState(f.config, f.directory, f.directory);
      const device = { state, connection: { python: 'no-device-process', hdc: 'unused', serial: 'fixture' },
        planningObservation: { id: 'observation', revision: 0, windows: [f.window] },
        windows: async () => [f.window] } as unknown as CollectionDevice;
      const spawn = child.mock.method(childProcess, 'spawn', () => { throw new Error('Unexpected bridge call'); });
      syncBuiltinESMExports();
      child.after(() => { spawn.mock.restore(); syncBuiltinESMExports(); });
      const action = createRegistry(() => device).actions(state)[0];
      await assert.rejects(action.call({ page_name: 'Page', variation: 'original', ...parameters }));
      assert.equal(spawn.mock.callCount(), 0);
    });
  }
});

test('online acceptance passes the unchanged stability declaration to delivery preparation', async (t) => {
  const f = fixture(t);
  const requests = interceptBridge(t, f.directory);
  const state = new RunState(f.config, f.directory, f.directory);
  state.candidate = f.candidate;
  const device = { connection: { python: 'unused', hdc: 'unused', serial: 'fixture' },
    pageObservation: f.pageObservation } as CollectionDevice;
  await assert.rejects(acceptCandidate(state, device, f.agent, () => ({})), /Fixture stopped/);
  assert.equal(requests()[0].operation, 'prepare_capture');
  assert.deepEqual(requests()[0].options.stability_regions, REGIONS);
});

test('offline finalization carries the same stability declaration without connecting a device', async (t) => {
  const f = fixture(t);
  writePageDecision(f.capture, f.metadata, 'frame');
  const requests = interceptBridge(t, f.directory);
  await assert.rejects(finalize(f.config, { python: 'unused' }, { directory: f.capture, taskName: 'fixture' }),
    /Fixture stopped/);
  assert.deepEqual(requests().map((request) => request.operation), ['dataset_status', 'prepare_capture']);
  assert.deepEqual(requests()[1].options.stability_regions, REGIONS);
});

test('online and offline preparation retain disabled privacy and its saved selectors unchanged', async (t) => {
  for (const operation of ['online', 'offline']) {
    await t.test(operation, async (child) => {
      const f = fixture(child);
      const privacy = { enabled: false, reason: 'Redaction deliberately switched off',
        regions: [{ bundle_name: 'app.fixture', resource_id: 'mail-body' }] };
      f.config.project.tasks[0].privacy = privacy;
      writeFixtureConfiguration(f.config);
      if (operation === 'offline') writePageDecision(f.capture, f.metadata, 'frame');
      const requests = interceptBridge(child, f.directory);
      const state = new RunState(f.config, f.directory, f.directory);
      state.candidate = f.candidate;
      const device = { connection: { python: 'unused', hdc: 'unused', serial: 'fixture' },
        pageObservation: f.pageObservation } as CollectionDevice;
      const pending = operation === 'online'
        ? acceptCandidate(state, device, f.agent, () => ({}))
        : finalize(f.config, { python: 'unused' }, { directory: f.capture, taskName: 'fixture' });
      await assert.rejects(pending, /Fixture stopped/);
      const preparation = requests().filter((request) => request.operation === 'prepare_capture');
      assert.equal(preparation.length, 1);
      assert.deepEqual(preparation[0].privacy, privacy);
      assert.equal(preparation[0].privacy.enabled, false);
    });
  }
});
