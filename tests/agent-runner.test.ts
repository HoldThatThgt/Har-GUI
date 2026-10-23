import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { after, test, type TestContext } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import type { LoadedConfiguration } from '../agent/config.js';
import type { RunState } from '../agent/state.js';
import { saveRunConfiguration } from '../agent/provenance.js';
import { FIXTURE_DELIVERY, writeFixtureConfiguration } from './agent-configuration.js';
import { writePageObservation } from './agent-page-fixture.js';

const sdkDirectory = mkdtempSync(join(tmpdir(), 'har-gui-runner-sdk-'));
process.env.MIDSCENE_RUN_DIR = sdkDirectory;
process.env.MIDSCENE_RECORD_MODEL_CALL = 'false';
process.env.HAR_GUI_RUNNER_TEST_KEY = 'local-fixture-key';
const { run } = await import('../agent/runner.js');
const { CollectionAgent, CollectionDevice } = await import('../agent/device.js');
after(() => {
  assert.equal(dirname(sdkDirectory), resolve(tmpdir()));
  assert.ok(basename(sdkDirectory).startsWith('har-gui-runner-sdk-'));
  rmSync(sdkDirectory, { recursive: true, force: true });
  delete process.env.HAR_GUI_RUNNER_TEST_KEY;
});

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAE0lEQVR4nGP8+u07AwwwwVnoHABrRALop6WreQAAAABJRU5ErkJggg==',
  'base64',
);

// 本地子进程仅替代数据集登记协议；配额与本轮上限仍由真实 runner 和 acceptance 执行。
const BRIDGE_STUB = `
const fs = require('node:fs');
const path = require('node:path');
let input = '';
process.stdin.setEncoding('utf8').on('data', chunk => input += chunk).on('end', () => {
  const filename = process.argv[1];
  const plan = JSON.parse(fs.readFileSync(filename, 'utf8'));
  const request = JSON.parse(input);
  plan.requests.push(request);
  let result;
  let error;
  if (request.operation === 'dataset_status') {
    result = { samples: plan.restored, counts: {}, remaining: {}, failures: [] };
  } else if (request.operation === 'prepare_capture') {
    const metadata = JSON.parse(fs.readFileSync(path.join(request.directory, 'metadata.json'), 'utf8'));
    result = { manifest: { path: request.directory,
      screenshot: { ...metadata.files.screenshot, file:path.join(request.directory,metadata.files.screenshot.file) } } };
  } else if (request.operation === 'publish_capture') {
    plan.publish_attempts += 1;
    if (plan.fail_publish_at === plan.publish_attempts) error = { message: 'Fixture publication failed' };
    else result = { sample_id: plan.publications[plan.publish_attempts - 1] || 'new-' + plan.publish_attempts,
      task_name: request.acceptance.task_name, path: request.directory, evidence_directory:request.evidence_directory };
    if(result) (plan.samples??=[]).push(result);
  } else if (request.operation === 'export_sample') {
    const sample=plan.samples.find(item=>item.sample_id===request.sample_id);
    const crypto=require('node:crypto'),files={};
    for(const artifact of request.delivery.artifacts){
      const name=artifact.path.replaceAll('{capture_id}',sample.sample_id);
      const target=path.join(request.destination,name);
      const bytes=artifact.source==='screenshot'?fs.readFileSync(path.join(sample.evidence_directory,'screenshot.png'))
        :Buffer.from(JSON.stringify({capture_id:sample.sample_id,source:artifact.source}));
      fs.writeFileSync(target,bytes);files[artifact.source]={file:name,bytes:bytes.length,
        sha256:crypto.createHash('sha256').update(bytes).digest('hex')};
    }
    const filename=path.join(request.destination,'manifest.json'),manifest=JSON.parse(fs.readFileSync(filename,'utf8'));
    const exported={capture_id:sample.sample_id,source_sha256:'fixture',files,naming:{capture_id:sample.sample_id}};
    if(!manifest.samples.some(item=>item.capture_id===sample.sample_id))manifest.samples.push(exported);
    fs.writeFileSync(filename,JSON.stringify(manifest));
    result={sample,export:exported,new_samples:0};
  } else error = { message: 'Unexpected fixture operation: ' + request.operation };
  fs.writeFileSync(filename, JSON.stringify(plan));
  process.stdout.write(JSON.stringify(error ? { error } : { result }));
});`;

interface FixtureOptions {
  restored?: { sample_id: string; task_name: string }[];
  publications?: string[];
  failPublishAt?: number;
  passAcceptance?: boolean;
  tasks?: { name: string; target_count: number }[];
}

function configuration(directory: string, tasks: FixtureOptions['tasks']): LoadedConfiguration {
  return { projectPath: join(directory, 'project.toml'), configPath: join(directory, 'config.toml'),
    outputPath: join(directory, 'runs'), datasetPath: join(directory, 'dataset'),
    digests: { project: '', config: '' },
    profile: { provider: 'openai_compatible', base_url: 'http://127.0.0.1:1/v1', model: 'fixture-vision',
      model_family: 'deepseek', api_key_env: 'HAR_GUI_RUNNER_TEST_KEY', supports_vision: true },
    project: { schema_version: 1,
      run: { name: 'fixture', output: 'runs', max_actions: 100, max_duration_seconds: 60 },
      delivery: structuredClone(FIXTURE_DELIVERY),
      agent: { llm: 'vision' }, dataset: { root: 'dataset', canvas: { width: 4, height: 3 }, stability_regions: [] },
      tools: { enabled: ['Capture'] }, tasks: (tasks ?? [{ name: 'editor', target_count: 200 }])
        .map((task) => ({ ...task, app: task.name, scene: 'scene', privacy: { enabled: false, reason: 'Fixture' },
          bundle_names: ['org.example.editor'], instruction: 'Find a different page and capture it',
          acceptance: 'The saved page is visible' })) } };
}

function fileInfo(file: string, bytes: Buffer) {
  return { file, bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
}

function capture(directory: string, captureId: string) {
  mkdirSync(directory, { recursive: true });
  const tree = Buffer.from(JSON.stringify({ attributes: { type: 'root' }, children: [] }));
  const windows = Buffer.from(JSON.stringify({ window_id: 92 }));
  const contents = { 'screenshot.png': PNG, 'arkui.json': tree, 'accessibility.json': tree,
    'layout.json': tree, 'closing-layout.json': tree, 'windows.txt': windows, 'closing-windows.txt': windows };
  for (const [file, bytes] of Object.entries(contents)) writeFileSync(join(directory, file), bytes);
  const bounds = { x: 0, y: 0, width: 4, height: 3 };
  const metadata = { capture_id: captureId, status: 'complete', display: { id: 0, width: 4, height: 3 },
    coverage: { status: 'verified', covered_window_ids: [92], excluded_windows: [], unresolved_windows: [] },
    consistency: { status: 'observed', atomic: false, pixels: { status: 'observed', changed: false } },
    window_inventory: [{ window_id: 92 }], foreground_window_ids_without_tree: [],
    files: { screenshot: { ...fileInfo('screenshot.png', PNG), width: 4, height: 3 },
      arkui: fileInfo('arkui.json', tree), accessibility: fileInfo('accessibility.json', tree),
      windows: fileInfo('windows.txt', windows), closing_windows: fileInfo('closing-windows.txt', windows),
      layout: fileInfo('layout.json', tree), closing_layout: fileInfo('closing-layout.json', tree) },
    windows: [{ window_id: 92, process_id: 202, display_id: 0, is_system: false,
      bundle_name: 'org.example.editor', z_order: 17,
      bounding_box: bounds, screen_bounds: bounds,
      trees: { arkui: { file: 'arkui.json', node_count: 1 },
        accessibility: { file: 'accessibility.json', node_count: 1 } } }] };
  const observation = writePageObservation(directory, metadata, 92,
    { before: PNG, after: PNG, afterScreen: PNG });
  writeFileSync(join(directory, 'metadata.json'), JSON.stringify(metadata));
  return observation;
}

function fixture(t: TestContext, options: FixtureOptions = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'har-gui-runner-test-'));
  t.after(() => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    assert.ok(basename(directory).startsWith('har-gui-runner-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  const planPath = join(directory, 'bridge.json');
  writeFileSync(planPath, JSON.stringify({ restored: options.restored ?? [], publications: options.publications ?? [],
    fail_publish_at: options.failPublishAt, publish_attempts: 0, requests: [] }));
  const config = configuration(directory, options.tasks);
  writeFixtureConfiguration(config);
  const events: string[] = [];
  const prompts: string[] = [];
  const connection = { python: 'unused-fixture-python', hdc: join(directory, 'unused-hdc.exe'), serial: 'fixture' };
  t.mock.method(console, 'error', () => {});
  return { directory, config, events, prompts, connection, planPath, passAcceptance: options.passAcceptance ?? true,
    plan: () => JSON.parse(readFileSync(planPath, 'utf8')),
    artifacts: () => join(config.datasetPath, 'runs', readdirSync(join(config.datasetPath, 'runs'))[0]),
    summaries: () => readdirSync(join(config.datasetPath, 'runs')).map((name) => JSON.parse(
      readFileSync(join(config.datasetPath, 'runs', name, 'run.json'), 'utf8'))) };
}

function mockBridge(t: TestContext, f: ReturnType<typeof fixture>): void {
  const original = childProcess.spawn;
  const originalExecFile = childProcess.execFile;
  const spawn = t.mock.method(childProcess, 'spawn', (_executable: any, args: any, options: any) => {
    assert.equal(basename(String(args[0])), 'agent_device.py', 'No real device process is permitted');
    return original(process.execPath, ['--input-type=commonjs', '-e', BRIDGE_STUB, f.planPath], options);
  });
  childProcess.execFile = ((_executable: any, args: any, _options: any,
    callback: (error: Error | null, stdout: string, stderr: string) => void) => {
    assert.equal(basename(String(args[0])), 'visualize.py');
    f.events.push('visualize');
    process.nextTick(() => callback(null, '', ''));
    return {};
  }) as any;
  syncBuiltinESMExports();
  t.after(() => { spawn.mock.restore(); childProcess.execFile = originalExecFile; syncBuiltinESMExports(); });
}

function mockAgent(t: TestContext, f: ReturnType<typeof fixture>): void {
  let state: RunState;
  let device: InstanceType<typeof CollectionDevice>;
  let count = 0;
  t.mock.method(CollectionDevice.prototype, 'connect', async function (this: InstanceType<typeof CollectionDevice>) {
    f.events.push('connect');
    state = this.state;
    device = this;
  });
  t.mock.method(CollectionAgent.prototype, 'aiAct', async (prompt: string, options: any) => {
    assert.equal(options.effort, 'deepThink');
    assert.equal(options.cacheable, false);
    assert.equal(options.abortSignal, state.signal);
    f.events.push('aiAct');
    f.prompts.push(prompt);
    count += 1;
    assert.equal(state.observations.size, 0);
    assert.equal(state.candidate, undefined);
    assert.equal(device.pageObservation, undefined);
    state.enter('Capture');
    state.leave();
    const captureId = `capture-${count}`;
    const directory = join(state.directory, 'captures', captureId);
    device.pageObservation = capture(directory, captureId);
    state.candidate = { capture_id: captureId, path: directory, task_name: state.task.name,
      page_name: `page-${count}`, target_window_id: 92, target_bundle_name: 'org.example.editor', window_changes: [] };
    writeFileSync(join(directory, 'candidate.json'), JSON.stringify(state.candidate));
  });
  t.mock.method(CollectionAgent.prototype, 'aiQuery', async () => {
    f.events.push('aiQuery');
    return { decision: 'same_page', evidence: 'Mocked cap-test page decision' };
  });
  t.mock.method(CollectionAgent.prototype, 'aiAssert', async () => {
    f.events.push('aiAssert');
    return { pass: f.passAcceptance } as any;
  });
  t.mock.method(CollectionAgent.prototype, 'destroy', async () => { f.events.push('destroy'); });
}

function mockRun(t: TestContext, options: FixtureOptions = {}) {
  const f = fixture(t, options);
  mockBridge(t, f);
  mockAgent(t, f);
  return f;
}

test('run cap adds one to a restored 200-target dataset and stops before another action', async (t) => {
  const f = mockRun(t, { restored: [{ sample_id: 'saved', task_name: 'editor' }] });
  const directory = await run(f.config, f.connection, { maxNewSamples: 1 });
  const summary = f.summaries()[0];
  assert.equal(directory, join(f.config.outputPath, '001-success'));
  const manifest = JSON.parse(readFileSync(join(directory, 'manifest.json'), 'utf8'));
  assert.equal(manifest.run_index, '001');
  assert.equal(manifest.samples.length, 1);
  assert.equal(readdirSync(directory).length, FIXTURE_DELIVERY.artifacts.length + 1);
  assert.deepEqual(readdirSync(f.config.outputPath), ['001-success']);
  assert.equal(summary.status, 'complete');
  assert.equal(summary.stop_reason, 'run_sample_limit');
  assert.equal(summary.max_new_samples, 1);
  assert.equal(summary.new_samples, 1);
  assert.equal(summary.accepted.length, 2);
  assert.equal(summary.actions, 1);
  assert.match(f.prompts[0], /199 samples remain/);
  assert.deepEqual(f.events, ['connect', 'aiAct', 'aiQuery', 'aiAssert', 'visualize', 'destroy']);
  assert.deepEqual(f.plan().requests.map((request: any) => request.operation),
    ['dataset_status', 'prepare_capture', 'publish_capture', 'export_sample']);
  assert.deepEqual(f.plan().requests[0].identity.tasks, [{ name: 'editor', target_count: 200 }]);
  assert.deepEqual(f.plan().requests[0].identity.project_sources, []);
  assert.deepEqual(readFileSync(join(f.artifacts(), 'project.toml')), readFileSync(f.config.projectPath));
  assert.equal(summary.accepted[1].path, join(f.artifacts(), 'captures', 'capture-1'));
  assert.equal(existsSync(join(summary.accepted[1].path, 'candidate.json')), true);
  assert.equal(f.config.project.tasks[0].target_count, 200);
  assert.deepEqual(summary.model_usage.call_ids, []);
});

test('zero cap restores saved samples and completes without device or model initialization', async (t) => {
  const f = mockRun(t, { restored: [{ sample_id: 'saved', task_name: 'editor' }] });
  f.config.profile.api_key_env = 'HAR_GUI_RUNNER_MISSING_KEY';
  writeFixtureConfiguration(f.config);
  delete process.env.HAR_GUI_RUNNER_MISSING_KEY;
  await run(f.config, f.connection, { maxNewSamples: 0 });
  assert.deepEqual(f.events, []);
  assert.deepEqual(f.plan().requests.map((request: any) => request.operation), ['dataset_status']);
  const summary = f.summaries()[0];
  assert.equal(summary.status, 'complete');
  assert.equal(summary.stop_reason, 'run_sample_limit');
  assert.equal(summary.new_samples, 0);
  assert.equal(summary.max_new_samples, 0);
  assert.equal(summary.actions, 0);
  assert.equal(summary.accepted.length, 1);
});

test('run passes explicit project source files and directories as absolute provenance paths', async (t) => {
  const f = mockRun(t);
  const sources = [join(f.directory, 'project-archive.toml'), join(f.directory, 'project-archives')];
  writeFileSync(sources[0], readFileSync(f.config.projectPath));
  mkdirSync(sources[1]);
  await run(f.config, f.connection, { maxNewSamples: 0, projectSources: sources });
  assert.deepEqual(f.plan().requests[0].identity.project_sources, sources.map((source) => resolve(source)));
  assert.equal(Object.keys(f.config).length, 7);
  assert.deepEqual(f.events, []);
});

test('run rejects changed raw project bytes before saving provenance or touching dataset and device', async (t) => {
  const f = mockRun(t);
  const changed = Buffer.concat([readFileSync(f.config.projectPath), Buffer.from('\n# Changed after loading\n')]);
  writeFileSync(f.config.projectPath, changed);
  await assert.rejects(run(f.config, f.connection, { maxNewSamples: 0 }), /Project source digest changed/);
  const summary = f.summaries()[0];
  const directory = f.artifacts();
  assert.equal(summary.status, 'failed');
  assert.equal(existsSync(join(f.config.outputPath, '001-failed', 'manifest.json')), true);
  assert.equal(existsSync(join(directory, 'project.toml')), false);
  assert.equal(existsSync(join(directory, 'configuration.json')), false);
  assert.deepEqual(f.plan().requests, []);
  assert.deepEqual(f.events, []);
  assert.deepEqual(readFileSync(f.config.projectPath), changed);
});

test('run preserves exact raw project bytes including comments and CRLF', async (t) => {
  const f = mockRun(t);
  const raw = Buffer.from('# 原始项目说明\r\n' + readFileSync(f.config.projectPath, 'utf8').replaceAll('\n', '\r\n'));
  writeFileSync(f.config.projectPath, raw);
  f.config.digests.project = createHash('sha256').update(raw).digest('hex');
  const directory = await run(f.config, f.connection, { maxNewSamples: 0 });
  const artifacts = f.artifacts();
  assert.deepEqual(readFileSync(join(artifacts, 'project.toml')), raw);
  assert.equal(JSON.parse(readFileSync(join(artifacts, 'configuration.json'), 'utf8')).digests.project,
    f.config.digests.project);
});

test('saving provenance refuses to overwrite an existing raw project snapshot', async (t) => {
  const f = fixture(t);
  const directory = join(f.directory, 'existing-run');
  mkdirSync(directory);
  const preserved = Buffer.from('# Preserve the exact historical source\r\n');
  writeFileSync(join(directory, 'project.toml'), preserved);
  await assert.rejects(saveRunConfiguration(f.config, directory), { code: 'EEXIST' });
  assert.deepEqual(readFileSync(join(directory, 'project.toml')), preserved);
  assert.equal(existsSync(join(directory, 'configuration.json')), false);
});

test('already satisfied quota completes before device or model even when the run cap is zero', async (t) => {
  const f = mockRun(t, { tasks: [{ name: 'editor', target_count: 1 }],
    restored: [{ sample_id: 'saved', task_name: 'editor' }] });
  f.config.profile.api_key_env = 'HAR_GUI_RUNNER_MISSING_KEY';
  writeFixtureConfiguration(f.config);
  await run(f.config, f.connection, { maxNewSamples: 0 });
  const summary = f.summaries()[0];
  assert.deepEqual(f.events, []);
  assert.equal(summary.status, 'complete');
  assert.equal(summary.stop_reason, 'quota_complete');
  assert.equal(summary.new_samples, 0);
});

test('omitted cap fills remaining quota and records an unlimited run', async (t) => {
  const f = mockRun(t, { tasks: [{ name: 'editor', target_count: 2 }],
    restored: [{ sample_id: 'saved', task_name: 'editor' }] });
  await run(f.config, f.connection);
  const summary = f.summaries()[0];
  assert.equal(summary.status, 'complete');
  assert.equal(summary.stop_reason, 'quota_complete');
  assert.equal(summary.max_new_samples, null);
  assert.equal(summary.new_samples, 1);
  assert.equal(summary.accepted.length, 2);
});

test('run cap counts unique new publication IDs and excludes restored or repeated IDs', async (t) => {
  const f = mockRun(t, { restored: [{ sample_id: 'saved', task_name: 'editor' }],
    publications: ['saved', 'new-a', 'new-a', 'new-b'] });
  await run(f.config, f.connection, { maxNewSamples: 2 });
  const summary = f.summaries()[0];
  assert.equal(summary.stop_reason, 'run_sample_limit');
  assert.equal(summary.new_samples, 2);
  assert.equal(summary.actions, 4);
  assert.equal(f.plan().publish_attempts, 4);
  assert.equal(f.events.filter((event) => event === 'aiAct').length, 4);
});

test('run cap is shared across tasks and stops while the second task still has quota', async (t) => {
  const f = mockRun(t, { tasks: [{ name: 'editor', target_count: 1 }, { name: 'browser', target_count: 200 }] });
  await run(f.config, f.connection, { maxNewSamples: 2 });
  const summary = f.summaries()[0];
  assert.equal(summary.stop_reason, 'run_sample_limit');
  assert.equal(summary.new_samples, 2);
  assert.deepEqual(summary.accepted.map((sample: any) => sample.task_name), ['editor', 'browser']);
  assert.equal(f.events.filter((event) => event === 'aiAct').length, 2);
  assert.match(f.prompts[1], /^Task: browser\n/);
});

test('quota completion takes precedence when the last new sample also reaches the cap', async (t) => {
  const f = mockRun(t, { tasks: [{ name: 'editor', target_count: 1 }] });
  await run(f.config, f.connection, { maxNewSamples: 1 });
  assert.equal(f.summaries()[0].stop_reason, 'quota_complete');
});

test('failed publication is not a new sample and does not simulate normal cap completion', async (t) => {
  const f = mockRun(t, { failPublishAt: 2 });
  await assert.rejects(run(f.config, f.connection, { maxNewSamples: 2 }), /Fixture publication failed/);
  const summary = f.summaries()[0];
  assert.equal(summary.status, 'failed');
  assert.equal(summary.new_samples, 1);
  assert.equal(summary.max_new_samples, 2);
  assert.equal(summary.stop_reason, undefined);
  assert.equal(summary.accepted.length, 1);
  assert.equal(f.plan().publish_attempts, 2);
  assert.equal(f.events.filter((event) => event === 'aiAct').length, 2);
  assert.equal(f.events.at(-1), 'destroy');
  const failed = JSON.parse(readFileSync(join(f.config.outputPath, '001-failed', 'manifest.json'), 'utf8'));
  assert.equal(failed.samples.length, 1);
  assert.equal(summary.candidate.path, join(f.artifacts(), 'captures', 'capture-2'));
  assert.equal(existsSync(join(summary.candidate.path, 'metadata.json')), true);
});

test('failed visual acceptance does not count a capture as a new publication', async (t) => {
  const f = mockRun(t, { passAcceptance: false });
  await assert.rejects(run(f.config, f.connection, { maxNewSamples: 1 }), /Project acceptance failed/);
  const summary = f.summaries()[0];
  assert.equal(summary.status, 'failed');
  assert.equal(summary.new_samples, 0);
  assert.equal(summary.stop_reason, undefined);
  assert.equal(f.plan().publish_attempts, 0);
});

test('run rejects every invalid API cap before dataset or device access', async (t) => {
  for (const maxNewSamples of [-1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    await t.test(String(maxNewSamples), async (child) => {
      const f = mockRun(child);
      await assert.rejects(run(f.config, f.connection, { maxNewSamples }), /non-negative safe integer/);
      assert.deepEqual(f.events, []);
      assert.deepEqual(f.plan().requests, []);
      assert.equal(f.summaries()[0].status, 'failed');
    });
  }
});

function cli(args: string[], env = process.env) {
  const filename = fileURLToPath(new URL('../agent/cli.ts', import.meta.url));
  return childProcess.spawnSync(process.execPath, ['--import', 'tsx', '--', filename, ...args],
    { cwd: resolve(dirname(filename), '..'), windowsHide: true, encoding: 'utf8', env });
}

test('CLI rejects invalid cap values before reading configuration or connecting', async (t) => {
  for (const value of ['-1', '0.5', 'NaN', 'Infinity', '9007199254740992', '', '1e2']) {
    await t.test(value || 'empty', () => {
      const result = cli(['run', `--max-new-samples=${value}`, '--project', 'missing-fixture-project.toml']);
      assert.equal(result.status, 1);
      assert.match(result.stderr, /--max-new-samples must be a non-negative safe integer/);
      assert.doesNotMatch(result.stderr, /Cannot read configuration|device|credential/i);
    });
  }
});

test('CLI accepts decimal safe-integer limits and only exposes the option for run', async (t) => {
  for (const value of ['0', '1', String(Number.MAX_SAFE_INTEGER)]) {
    await t.test(value, () => {
      const result = cli(['run', `--max-new-samples=${value}`, '--project', 'missing-fixture-project.toml']);
      assert.equal(result.status, 1);
      assert.match(result.stderr, /Cannot read configuration/);
      assert.doesNotMatch(result.stderr, /max-new-samples must/);
    });
  }
  for (const command of ['validate', 'tools', 'finalize', 'export']) {
    await t.test(command, () => {
      const result = cli([command, '--max-new-samples=0']);
      assert.equal(result.status, 1);
      assert.match(result.stderr, /--max-new-samples is only available for run/);
    });
  }
});

test('CLI restricts project sources to execution commands and rejects empty paths', async (t) => {
  for (const command of ['validate', 'tools']) {
    await t.test(command, () => {
      const result = cli([command, '--project-source=archive.toml']);
      assert.equal(result.status, 1);
      assert.match(result.stderr, /--project-source is only available for run, finalize and export/);
    });
  }
  for (const command of ['run', 'finalize', 'export']) {
    await t.test(`${command} empty`, () => {
      const result = cli([command, '--project-source=']);
      assert.equal(result.status, 1);
      assert.match(result.stderr, /--project-source requires a nonempty file or directory path/);
    });
    await t.test(`${command} repeated`, () => {
      const result = cli([command, '--project-source=archive.toml', '--project-source=archive-directory',
        '--project', 'missing-fixture-project.toml']);
      assert.equal(result.status, 1);
      assert.match(result.stderr, /Cannot read configuration/);
      assert.doesNotMatch(result.stderr, /project-source is only|project-source requires/);
    });
  }
});

test('CLI exports an accepted identity without a device, model key or environment-file read', (t) => {
  const f = fixture(t);
  const evidence = join(f.directory, 'accepted-source');
  capture(evidence, 'accepted-frame');
  const original = readFileSync(join(evidence, 'screenshot.png'));
  const plan = f.plan();
  plan.samples = [{ sample_id: 'accepted-frame', task_name: 'editor', path: evidence, evidence_directory: evidence }];
  writeFileSync(f.planPath, JSON.stringify(plan));
  const source = join(f.directory, 'project-archive.toml');
  writeFileSync(source, readFileSync(f.config.projectPath));
  const hook = join(f.directory, 'export-bridge-hook.mjs');
  writeFileSync(hook, `import cp from 'node:child_process';
import { basename } from 'node:path';
import { syncBuiltinESMExports } from 'node:module';
const original=cp.spawn;
cp.spawn=(_executable,args,options)=>{
  if(args.length!==1||basename(String(args[0]))!=='agent_device.py')throw new Error('Unexpected device parameters');
  return original(process.execPath,['--input-type=commonjs','-e',${JSON.stringify(BRIDGE_STUB)},
    ${JSON.stringify(f.planPath)}],{...options,env:{...options.env,NODE_OPTIONS:''}});
};
syncBuiltinESMExports();`);
  const result = cli(['export', '--project', f.config.projectPath, '--config', f.config.configPath,
    '--sample-id', 'accepted-frame', '--env-file', join(f.directory, 'missing.env'), '--project-source', source],
  { ...process.env, HAR_GUI_RUNNER_TEST_KEY: '', NODE_OPTIONS: `--import=${pathToFileURL(hook).href}` });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(f.plan().requests.map((request: any) => request.operation), ['export_sample']);
  assert.deepEqual(f.plan().requests[0].identity.project_sources, [source]);
  assert.equal(f.plan().requests[0].sample_id, 'accepted-frame');
  assert.equal(f.summaries()[0].operation, 'export');
  assert.equal(f.summaries()[0].new_samples, 0);
  const output = join(f.config.outputPath, '001-success');
  assert.equal(readdirSync(output).length, FIXTURE_DELIVERY.artifacts.length + 1);
  assert.deepEqual(readFileSync(join(output, 'accepted-frame_shot.png')), original);
  assert.deepEqual(readFileSync(join(evidence, 'screenshot.png')), original);
});

test('CLI passes zero cap to the runner and resumes without a connected device or model key', (t) => {
  const f = fixture(t, { restored: [{ sample_id: 'saved', task_name: 'editor' }] });
  f.config.profile.api_key_env = 'HAR_GUI_RUNNER_MISSING_KEY';
  writeFixtureConfiguration(f.config);
  const sources = [join(f.directory, 'old-project.toml'), join(f.directory, 'old-projects')];
  writeFileSync(sources[0], readFileSync(f.config.projectPath));
  mkdirSync(sources[1]);
  const hook = join(f.directory, 'bridge-hook.mjs');
  const source = `import cp from 'node:child_process';
import { basename } from 'node:path';
import { syncBuiltinESMExports } from 'node:module';
const original = cp.spawn;
cp.spawn = (_executable, args, options) => {
  if (basename(String(args[0])) !== 'agent_device.py') throw new Error('No real device process is permitted');
  return original(process.execPath, ['--input-type=commonjs', '-e', ${JSON.stringify(BRIDGE_STUB)},
    ${JSON.stringify(f.planPath)}], { ...options, env: { ...options.env, NODE_OPTIONS: '' } });
};
syncBuiltinESMExports();`;
  writeFileSync(hook, source);
  const result = cli(['run', '--project', f.config.projectPath, '--config', f.config.configPath,
    '--device', 'fixture', '--hdc', f.connection.hdc, '--max-new-samples=0',
    '--project-source', sources[0], '--project-source', sources[1]],
  { ...process.env, HAR_GUI_RUNNER_MISSING_KEY: '', NODE_OPTIONS: `--import=${pathToFileURL(hook).href}` });
  assert.equal(result.status, 0, result.stderr);
  const summary = f.summaries()[0];
  assert.equal(summary.status, 'complete');
  assert.equal(summary.stop_reason, 'run_sample_limit');
  assert.equal(summary.max_new_samples, 0);
  assert.equal(summary.new_samples, 0);
  assert.equal(summary.accepted.length, 1);
  assert.deepEqual(f.plan().requests.map((request: any) => request.operation), ['dataset_status']);
  assert.deepEqual(f.plan().requests[0].identity.project_sources, sources);
});
