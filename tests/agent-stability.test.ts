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
import { writeFixtureConfiguration } from './agent-configuration.js';

const REGIONS = [{ bundle_name: 'system.fixture', resource_id: 'clock-leaf' }];

/** 在真实桥接标准输入边界记录请求，并在准备前停止，不访问设备或模型。 */
function interceptBridge(t: TestContext, directory: string) {
  const log = join(directory, 'requests.jsonl');
  const original = childProcess.spawn;
  const script = `let text='';process.stdin.on('data',part=>text+=part);process.stdin.on('end',()=>{
    const request=JSON.parse(text);require('node:fs').appendFileSync(process.argv[1],JSON.stringify(request)+'\\n');
    const result=request.operation==='dataset_status'?{result:{samples:[],counts:{},remaining:{},failures:[]}}
      :{error:{message:'Fixture stopped after receiving request'}};process.stdout.write(JSON.stringify(result));});`;
  const mock = t.mock.method(childProcess, 'spawn', () => original(process.execPath, ['-e', script, log]));
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
  const files = Object.fromEntries(['screenshot.png', 'tree.json'].map((name) => {
    const data = Buffer.from(name === 'tree.json' ? '{}' : 'fixture-image-not-sent-to-model');
    writeFileSync(join(capture, name), data);
    return [name === 'screenshot.png' ? 'screenshot' : 'tree',
      { file: name, bytes: data.length, sha256: createHash('sha256').update(data).digest('hex') }];
  }));
  const window = { window_id: 1, process_id: 2, ui_node_id: 3, display_id: 0, z_order: 4,
    is_system: false, bundle_name: 'app.fixture', bounding_box: { x: 0, y: 0, width: 100, height: 100 },
    screen_bounds: { x: 0, y: 0, width: 100, height: 100 },
    trees: { arkui: { file: 'tree.json', node_count: 1 }, accessibility: { file: 'tree.json', node_count: 1 } } };
  const metadata = { capture_id: 'frame', status: 'complete', files, windows: [window], window_inventory: [window],
    coverage: { status: 'verified', covered_window_ids: [1], excluded_windows: [], unresolved_windows: [] },
    foreground_window_ids_without_tree: [], consistency: { status: 'verified', stable: true,
      pixels: { status: 'verified' } } };
  writeFileSync(join(capture, 'metadata.json'), JSON.stringify(metadata));
  const candidate = { capture_id: 'frame', path: capture, task_name: 'fixture', target_bundle_name: 'app.fixture',
    page_name: 'fixture-page', window_changes: [] };
  writeFileSync(join(capture, 'candidate.json'), JSON.stringify(candidate));
  const config = { projectPath: join(directory, 'project.toml'), configPath: join(directory, 'config.toml'),
    outputPath: join(directory, 'runs'), datasetPath: join(directory, 'dataset'),
    profile: { provider: 'openai_compatible', base_url: 'https://model.example.test/v1', model: 'fixture',
      model_family: 'deepseek', api_key_env: 'UNUSED_STABILITY_TEST_KEY', supports_vision: true },
    digests: { project: '', config: '' }, project: { schema_version: 1,
      run: { name: 'fixture', output: 'runs', max_duration_seconds: 60, max_actions: 5 },
      agent: { llm: 'vision' }, tools: { enabled: ['Capture'] },
      dataset: { root: 'dataset', canvas: { width: 100, height: 100 }, stability_regions: REGIONS },
      tasks: [{ name: 'fixture', app: 'Fixture', scene: 'Page', instruction: 'Inspect', acceptance: 'Pass',
        target_count: 1, bundle_names: ['app.fixture'], privacy: { enabled: false, reason: 'Synthetic fixture' } }],
    } } as LoadedConfiguration;
  writeFixtureConfiguration(config);
  return { directory, capture, candidate, config, window };
}

test('Capture passes project stability selectors to the raw capture request', async (t) => {
  const f = fixture(t);
  const requests = interceptBridge(t, f.directory);
  const state = new RunState(f.config, f.directory);
  const device = { state, connection: { python: 'unused', hdc: 'unused', serial: 'fixture' },
    planningObservation: { id: 'observation', revision: 0, windows: [f.window] },
    windows: async () => [f.window] } as unknown as CollectionDevice;
  const action = createRegistry(() => device).actions(state)[0];
  await assert.rejects(action.call({ page_name: 'Page', variation: 'original', target_bundle_name: 'app.fixture' }),
    /Fixture stopped/);
  assert.equal(requests()[0].operation, 'capture');
  assert.deepEqual(requests()[0].stability_regions, REGIONS);
});

test('online acceptance passes the unchanged stability declaration to delivery preparation', async (t) => {
  const f = fixture(t);
  const requests = interceptBridge(t, f.directory);
  const state = new RunState(f.config, f.directory);
  state.candidate = f.candidate;
  const device = { connection: { python: 'unused', hdc: 'unused', serial: 'fixture' } } as CollectionDevice;
  await assert.rejects(acceptCandidate(state, device, {} as CollectionAgent, () => ({})), /Fixture stopped/);
  assert.equal(requests()[0].operation, 'prepare_delivery');
  assert.deepEqual(requests()[0].options.stability_regions, REGIONS);
});

test('offline finalization carries the same stability declaration without connecting a device', async (t) => {
  const f = fixture(t);
  const requests = interceptBridge(t, f.directory);
  await assert.rejects(finalize(f.config, { python: 'unused' }, { directory: f.capture, taskName: 'fixture' }),
    /Fixture stopped/);
  assert.deepEqual(requests().map((request) => request.operation), ['dataset_status', 'prepare_delivery']);
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
      const requests = interceptBridge(child, f.directory);
      const state = new RunState(f.config, f.directory);
      state.candidate = f.candidate;
      const device = { connection: { python: 'unused', hdc: 'unused', serial: 'fixture' } } as CollectionDevice;
      const pending = operation === 'online'
        ? acceptCandidate(state, device, {} as CollectionAgent, () => ({}))
        : finalize(f.config, { python: 'unused' }, { directory: f.capture, taskName: 'fixture' });
      await assert.rejects(pending, /Fixture stopped/);
      const preparation = requests().filter((request) => request.operation === 'prepare_delivery');
      assert.equal(preparation.length, 1);
      assert.deepEqual(preparation[0].privacy, privacy);
      assert.equal(preparation[0].privacy.enabled, false);
    });
  }
});
