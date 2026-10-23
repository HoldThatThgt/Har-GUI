import assert from 'node:assert/strict';
import childProcess, { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, unlinkSync, utimesSync, writeFileSync,
} from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { after, test, type TestContext } from 'node:test';
import sharp from 'sharp';
import { localBridge } from '../agent/bridge.js';
import { exportSample } from '../agent/export.js';
import type { LoadedConfiguration } from '../agent/config.js';
import { FIXTURE_DELIVERY, writeFixtureConfiguration } from './agent-configuration.js';

const sdkDirectory = mkdtempSync(join(tmpdir(), 'har-gui-finalize-sdk-'));
process.env.MIDSCENE_RUN_DIR = sdkDirectory;
process.env.MIDSCENE_RECORD_MODEL_CALL = 'false';
process.env.HAR_GUI_FINALIZE_TEST_KEY = 'local-fixture-key';
const { finalize: finalizeSample, SavedScreenshotDevice } = await import('../agent/finalize.js');
const runArtifacts = new Map<string, string>();

/** @brief 仅在测试端记录一次调用新建的内部目录，不要求生产记录易变结果路径。 */
async function finalize(config: LoadedConfiguration, connection: Parameters<typeof finalizeSample>[1],
  options: Parameters<typeof finalizeSample>[2]) {
  const storage = join(config.datasetPath, 'runs');
  const before = new Set(existsSync(storage) ? readdirSync(storage) : []);
  const outputs = new Set(existsSync(config.outputPath) ? readdirSync(config.outputPath) : []);
  try { return await finalizeSample(config, connection, options); }
  finally {
    const created = readdirSync(storage).filter((name) => !before.has(name));
    const results = readdirSync(config.outputPath).filter((name) => !outputs.has(name));
    assert.equal(created.length, 1);
    assert.equal(results.length, 1);
    runArtifacts.set(join(config.outputPath, results[0]), join(storage, created[0]));
  }
}
after(() => {
  assert.equal(dirname(sdkDirectory), resolve(tmpdir()));
  assert.ok(basename(sdkDirectory).startsWith('har-gui-finalize-sdk-'));
  rmSync(sdkDirectory, { recursive: true, force: true });
  delete process.env.HAR_GUI_FINALIZE_TEST_KEY;
});

const PREPARED_FIXTURE = `
import json, sys
from pathlib import Path
from unittest.mock import patch
from tests.support import fake_hdc, synthetic_screen_geometry, layout_data, raw_layout, raw_accessibility
from har_capture.collector import capture
from har_capture.delivery_operations import prepare_capture
from har_capture.privacy import atomic_json
root = Path(sys.argv[1])
geometry = patch('har_capture.window_geometry.attach_captured_screen_geometry',side_effect=synthetic_screen_geometry)
geometry.start()
closing_geometry = patch('har_capture.window_geometry.read_window_geometry',side_effect=lambda hdc,windows: windows)
closing_geometry.start()
target = {'window_id':92,'bundle_name':'com.example.settings'}
redacted = len(sys.argv) > 4 and sys.argv[4] == 'redacted'
layout = layout_data()
if redacted:
    layout['children'][1]['attributes']['id'] = 'synthetic-body'
hdc = fake_hdc({'layout':raw_layout(layout),'accessibility':raw_accessibility(layout['children'])})
if len(sys.argv) > 3 and sys.argv[3] == 'agent_run':
    root = capture(hdc,root,0,[],target)['path']
else:
    source = capture(hdc,root.parent / 'collected',0,[],target)['path']
    root.rmdir()
    source.rename(root)
metadata = json.loads((root / 'metadata.json').read_bytes())
observation = json.loads((root / 'page-observation.json').read_bytes())
atomic_json(root / 'page-stability.json',{'rule_version':1,'capture_id':metadata['capture_id'],
    'target':observation['target'],'observation_sha256':metadata['files']['page_observation']['sha256'],
    'decision':'same_page','evidence':'Synthetic mocked target page comparison'})
policy = {'enabled':redacted,'reason':'Synthetic fixture'}
if redacted:
    policy['regions'] = [{'bundle_name':'com.example.settings','resource_id':'synthetic-body'}]
options = {'canvas':{'width':3120,'height':2080},'stability_regions':[]}
def normalized(directory,window,canvas,scenes):
    tree = json.loads((directory / window['trees']['arkui']['file']).read_bytes())
    tree['coordinate_validation'] = {'status':'verified'}
    return {'window_id':window['window_id'],'bundle_name':window['bundle_name'],'tree':tree}
with patch('har_capture.capture_facts.load_scene_evidence',return_value={}), \\
        patch('har_capture.capture_facts.normalized_arkui',side_effect=normalized):
    prepared = prepare_capture({'directory':str(root),'privacy':policy,'options':options})
print(json.dumps(prepared['manifest']))
`;

/** @brief 在每个真实离线 Python 进程内明确替换匿名测试几何证明，不改生产验证流程。 */
function mockSyntheticGeometry(t: TestContext): void {
  const original = childProcess.spawn;
  const script = `import json,sys
from unittest.mock import patch
from tests.support import synthetic_screen_geometry
import agent_device
def normalized(evidence,window,canvas,scenes):
    tree=json.loads((evidence/window['trees']['arkui']['file']).read_bytes())
    tree['coordinate_validation']={'status':'verified'}
    return {'window_id':window['window_id'],'bundle_name':window['bundle_name'],'tree':tree}
with patch('har_capture.window_geometry.attach_captured_screen_geometry',side_effect=synthetic_screen_geometry), \\
        patch('har_capture.capture_facts.load_scene_evidence',return_value={}), \\
        patch('har_capture.capture_facts.normalized_arkui',side_effect=normalized):
    sys.exit(agent_device.main())`;
  const spawn = t.mock.method(childProcess, 'spawn', (executable: any, args: any, options: any) => {
    if (Array.isArray(args) && basename(String(args[0])) === 'agent_device.py') {
      return original(executable, ['-B', '-c', script, ...args.slice(1)], options);
    }
    return original(executable, args, options);
  });
  syncBuiltinESMExports();
  t.after(() => { spawn.mock.restore(); syncBuiltinESMExports(); });
}

/** 在测试临时目录建立完整采集事实及可重新核验的准备凭证。 */
function fixture(t: TestContext, candidate = true, captureInRun = false, redacted = false) {
  mockSyntheticGeometry(t);
  const directory = mkdtempSync(join(tmpdir(), 'har-gui-finalize-test-'));
  let capture = captureInRun ? join(directory, 'dataset', 'runs', 'source-run', 'captures') : join(directory, 'capture');
  mkdirSync(capture, { recursive: true });
  t.after(() => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    assert.ok(basename(directory).startsWith('har-gui-finalize-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  t.mock.method(console, 'warn', () => {});
  const prepared = JSON.parse(execFileSync('python', ['-c', PREPARED_FIXTURE, capture, candidate ? 'page' : '',
    captureInRun ? 'agent_run' : '', redacted ? 'redacted' : ''], {
    windowsHide: true, env: { ...process.env, PYTHONUTF8: '1' },
  }).toString('utf8'));
  if (captureInRun) capture = join(capture, readdirSync(capture)[0]);
  const metadata = JSON.parse(readFileSync(join(capture, 'metadata.json'), 'utf8'));
  if (candidate) writeFileSync(join(capture, 'candidate.json'), JSON.stringify({ capture_id: metadata.capture_id,
    path: capture, task_name: 'editor', target_window_id: 92, target_bundle_name: 'com.example.settings',
    page_name: 'page', window_changes: [] }));
  const config: LoadedConfiguration = { projectPath: join(directory, 'project.toml'),
    configPath: join(directory, 'config.toml'), outputPath: join(directory, 'runs'),
    datasetPath: join(directory, 'dataset'), digests: { project: '', config: '' },
    profile: { provider: 'openai_compatible', base_url: 'http://127.0.0.1:1/v1', model: 'fixture-vision',
      model_family: 'deepseek', api_key_env: 'HAR_GUI_FINALIZE_TEST_KEY', supports_vision: true },
    project: { schema_version: 1, run: { name: 'fixture', output: 'runs', max_actions: 1, max_duration_seconds: 60 },
      delivery: structuredClone(FIXTURE_DELIVERY),
      agent: { llm: 'vision' }, dataset: { root: 'dataset', canvas: { width: 3120, height: 2080 }, stability_regions: [] },
      tools: { enabled: ['Capture'] }, tasks: [{ name: 'editor', app: 'editor', scene: 'scene', target_count: 1,
        privacy: { enabled: redacted, reason: 'Synthetic fixture', ...(redacted
          ? { regions: [{ bundle_name: 'com.example.settings', resource_id: 'synthetic-body' }] } : {}) },
        bundle_names: ['com.example.settings'],
        instruction: 'Inspect the saved fixture', acceptance: 'The saved screenshot is white' }] } };
  writeFixtureConfiguration(config);
  return { directory, capture, config, prepared, request: { directory: capture, taskName: 'editor' },
    runs: () => readdirSync(join(config.datasetPath, 'runs'))
      .filter((name) => existsSync(join(config.datasetPath, 'runs', name, 'run.json')))
      .map((name) => JSON.parse(readFileSync(join(config.datasetPath, 'runs', name, 'run.json'), 'utf8'))) };
}

/** @brief 从结果索引取得始终不改名的运行证据目录。 */
function artifacts(directory: string): string {
  const internal = runArtifacts.get(directory);
  assert.ok(internal, 'Test must bind the actual internal run created by this invocation');
  assert.equal(existsSync(join(directory, 'run.json')), false);
  assert.equal(existsSync(join(directory, 'manifest.json')), true);
  return internal;
}

/** @brief 读取内部详细状态，对外交付目录只含产物及实际清单。 */
function runSummary(directory: string) {
  return JSON.parse(readFileSync(join(artifacts(directory), 'run.json'), 'utf8'));
}

/** 记录原始证据、登记收据及声明交付的字节摘要和精确修改时间。 */
function savedFiles(roots: Record<string, string>) {
  const result: Record<string, { sha256: string; modified: string }> = {};
  for (const [label, root] of Object.entries(roots)) {
    const pending = [root];
    while (pending.length) {
      const directory = pending.pop()!;
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
        if (label === 'dataset' && directory === root && entry.name === 'runs') continue;
        const file = join(directory, entry.name);
        if (entry.isDirectory()) pending.push(file);
        else result[`${label}:${file.slice(root.length)}`] = {
          sha256: createHash('sha256').update(readFileSync(file)).digest('hex'),
          modified: statSync(file, { bigint: true }).mtimeNs.toString(),
        };
      }
    }
  }
  return result;
}

/** @brief 直接读取真实 SQLite 的持久来源与原清单，不经过桥接结果重建。 */
function publishedRows(directory: string) {
  const script = `import json,sqlite3,sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True) as database:
    rows = database.execute('SELECT source,manifest,status FROM samples').fetchall()
print(json.dumps([{'source':r[0],'manifest':json.loads(r[1]),'status':r[2]} for r in rows]))`;
  return JSON.parse(execFileSync('python', ['-B', '-c', script, join(directory, 'dataset.sqlite3')],
    { windowsHide: true, encoding: 'utf8', env: { ...process.env, PYTHONUTF8: '1' } }));
}

/** 将已发布收据改为等价的不同键序、缩进、转义和换行，验证恢复不会重新序列化。 */
function varyReceipts(capture: string): void {
  for (const name of ['metadata.json', 'capture-preparation.json', 'publication.json']) {
    const file = join(capture, name);
    const value = JSON.parse(readFileSync(file, 'utf8'));
    const ordered = Object.fromEntries(Object.entries(value).reverse());
    const content = JSON.stringify(ordered, null, 1).replace(/Synthetic fixture/g, '\\u0053ynthetic fixture')
      .replace(/\n/g, '\r\n') + '\r\n';
    writeFileSync(file, content);
    const modified = new Date('2020-01-01T00:00:00Z');
    utimesSync(file, modified, modified);
  }
}

/** 使用进程内 Fetch 替身返回固定视觉响应，保留 SDK 解析及用量流程，不建立网络连接。 */
async function model(t: TestContext, config: LoadedConfiguration,
  mode: 'pass' | 'fail' | 'error' | ('pass' | 'fail' | 'error')[]) {
  const requests: any[] = [];
  config.profile.base_url = 'https://model.fixture.invalid/v1';
  t.mock.method(globalThis, 'fetch', async (input: any, options: any) => {
    assert.match(String(input), /^https:\/\/model\.fixture\.invalid\/v1\//);
    requests.push(JSON.parse(options.body));
    const outcome = Array.isArray(mode) ? mode.shift() : mode;
    assert.ok(outcome, 'Every mocked model call needs an explicit response');
    const headers = { 'content-type': 'application/json' };
    if (outcome === 'error') return new Response('{"error":{"message":"fixture"}}', { status: 400, headers });
    return new Response(JSON.stringify({ id: 'local-response', object: 'chat.completion', created: 1,
      model: 'fixture-vision',
      usage: { prompt_tokens: 100, completion_tokens: 10, total_tokens: 110,
        prompt_cache_hit_tokens: 40, prompt_cache_miss_tokens: 60 },
      choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant',
        content: '<observation>private-model-observation</observation><data-json>'
          + JSON.stringify({ StatementIsTruthy: outcome === 'pass' }) + '</data-json>' } }] }), { status: 200, headers });
  });
  writeFixtureConfiguration(config);
  return requests;
}

test('static screenshot device exposes fixed canvas and no action or connection capability', async (t) => {
  t.mock.method(console, 'error', () => {});
  const canvas = { width: 3120, height: 2080 };
  const device = new SavedScreenshotDevice(canvas);
  canvas.width = 1;
  assert.deepEqual(await device.size(), { width: 3120, height: 2080 });
  assert.deepEqual(device.actionSpace(), []);
  assert.equal('connection' in device, false);
  assert.equal('connect' in device, false);
  await assert.rejects(device.screenshotBase64(), /No saved screenshot/);
  device.frozenScreenshot = 'data:image/png;base64,fixture';
  assert.equal(await device.screenshotBase64(), device.frozenScreenshot);
});

test('offline finalization uses the saved final PNG, records mocked provider usage, and publishes once', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'pass');
  const originalProjectDigest = f.config.digests.project;
  const modelDigest = f.config.digests.config;
  const metadata = JSON.parse(readFileSync(join(f.capture, 'metadata.json'), 'utf8'));
  const originalObservation = JSON.parse(readFileSync(join(f.capture, 'page-observation.json'), 'utf8'));
  assert.equal(metadata.privacy.enabled, false);
  assert.equal(metadata.privacy.before_sha256, originalObservation.before.screenshot_sha256);
  assert.equal(metadata.privacy.before_sha256, metadata.privacy.after_sha256);
  const expectedHash = f.prepared.screenshot.sha256;
  const savedInputs: string[] = [];
  const original = SavedScreenshotDevice.prototype.screenshotBase64;
  t.mock.method(SavedScreenshotDevice.prototype, 'screenshotBase64', async function (
    this: InstanceType<typeof SavedScreenshotDevice>,
  ) {
    const image = await original.call(this);
    savedInputs.push(image);
    return image;
  });
  const run = await finalize(f.config, { python: 'python' }, f.request);
  const result = runSummary(run);
  assert.equal(result.status, 'complete');
  assert.equal(result.actions, 0);
  assert.equal(requests.length, 1);
  assert.equal(result.model_usage.calls, 1);
  assert.equal(result.model_usage.totals.total_tokens, 110);
  const images = requests[0].messages.flatMap((message: any) => Array.isArray(message.content)
    ? message.content.filter((part: any) => part.type === 'image_url') : []);
  assert.equal(images.length, 1);
  assert.ok(savedInputs.length > 0);
  for (const image of savedInputs) {
    assert.equal(createHash('sha256').update(Buffer.from(image.split(',')[1], 'base64')).digest('hex'), expectedHash);
  }
  // Midscene 会将已保存 PNG 在内存中编码成 JPEG；核对来源摘要及传送图片尺寸和实际像素。
  const sentImage = sharp(Buffer.from(images[0].image_url.url.split(',')[1], 'base64'));
  const sentMetadata = await sentImage.metadata();
  assert.deepEqual([sentMetadata.width, sentMetadata.height], [3120, 2080]);
  const sentPixels = await sentImage.removeAlpha().raw().toBuffer();
  assert.ok(sentPixels.every((value) => value === 255));
  assert.deepEqual(readdirSync(result.sample.path), ['capture.json']);
  const delivered = JSON.parse(readFileSync(join(run, 'manifest.json'), 'utf8'));
  assert.equal(Object.keys(delivered.samples[0].files).length, FIXTURE_DELIVERY.artifacts.length);
  assert.equal(readdirSync(run).length, FIXTURE_DELIVERY.artifacts.length + 1);
  assert.equal(result.sample.visual_acceptance.screenshot_sha256, expectedHash);
  assert.equal(existsSync(join(f.capture, 'delivery')), false);
  assert.equal(basename(run), '001-success');
  assert.doesNotMatch(readFileSync(join(artifacts(run), 'model-usage.jsonl'), 'utf8'),
    /private-model|data:image|local-fixture-key/);
  varyReceipts(f.capture);
  const roots = { evidence: f.capture, dataset: f.config.datasetPath, original_run: run,
    original_artifacts: artifacts(run) };
  const preserved = savedFiles(roots);
  f.config.project.tasks[0].instruction = 'Find further distinct pages for the expanded project';
  f.config.project.tasks[0].acceptance = 'The requested expanded-project page is visible';
  f.config.project.tasks[0].privacy.reason = 'Current project privacy documentation was clarified';
  f.config.project.tasks[0].target_count = 200;
  f.config.project.run.max_actions = 20;
  f.config.project.run.max_duration_seconds = 120;
  writeFixtureConfiguration(f.config);
  assert.notEqual(f.config.digests.project, originalProjectDigest);
  assert.equal(f.config.digests.config, modelDigest);
  for (const repeat of ['published', 'repeated']) {
    const resumed = await finalize(f.config, { python: 'python' }, f.request);
    const receipt = runSummary(resumed);
    assert.equal(receipt.sample.sample_id, result.sample.sample_id, repeat);
    assert.equal(receipt.model_usage.calls, 0);
    assert.equal(receipt.accepted.length, 1);
    assert.deepEqual(JSON.parse(readFileSync(join(artifacts(resumed), 'configuration.json'), 'utf8')), f.config);
    assert.deepEqual(savedFiles(roots), preserved);
  }
  const originalConfiguration = JSON.parse(readFileSync(join(artifacts(run), 'configuration.json'), 'utf8'));
  assert.equal(originalConfiguration.digests.project, originalProjectDigest);
  assert.equal(originalConfiguration.project.tasks[0].target_count, 1);
  assert.equal(createHash('sha256').update(readFileSync(join(artifacts(run), 'project.toml'))).digest('hex'),
    originalProjectDigest);
  const status = await localBridge('python', { operation: 'dataset_status', root: f.config.datasetPath,
    identity: { project_sha256: f.config.digests.project, config_sha256: f.config.digests.config,
      tasks: f.config.project.tasks.map(({ name, target_count }) => ({ name, target_count })),
      configuration: f.config, project_sources: [] } });
  assert.deepEqual(status.counts, { editor: 1 });
  assert.deepEqual(status.remaining, { editor: 199 });
  assert.deepEqual(status.failures, []);
  assert.deepEqual(savedFiles(roots), preserved);
  assert.equal(requests.length, 1);
});

test('one accepted frame exports five and six declared artifacts without models, recounting or old file changes', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'pass');
  const original = await finalize(f.config, { python: 'python' }, f.request);
  const accepted = runSummary(original).sample;
  const rows = publishedRows(f.config.datasetPath);
  const roots = { evidence: f.capture, dataset: f.config.datasetPath, original,
    original_artifacts: artifacts(original) };
  const before = savedFiles(roots);
  f.config.project.delivery.directory = '{app}/{capture_id}';
  f.config.project.delivery.artifacts.push({ source: 'capture_metadata', path: '{capture_id}_metadata.json' });
  writeFixtureConfiguration(f.config);
  const key = process.env.HAR_GUI_FINALIZE_TEST_KEY;
  delete process.env.HAR_GUI_FINALIZE_TEST_KEY;
  let exported: string;
  try { exported = await exportSample(f.config, 'python', { sampleId: accepted.sample_id }); }
  finally { process.env.HAR_GUI_FINALIZE_TEST_KEY = key; }
  assert.equal(basename(exported), '002-success');
  const manifest = JSON.parse(readFileSync(join(exported, 'manifest.json'), 'utf8'));
  assert.equal(manifest.samples.length, 1);
  const sample = manifest.samples[0];
  assert.equal(sample.capture_id, accepted.sample_id);
  assert.equal(Object.keys(sample.files).length, 6);
  for (const file of Object.values(sample.files) as any[]) {
    assert.ok(file.file.startsWith(`editor/${accepted.sample_id}/`));
    const bytes = readFileSync(join(exported, file.file));
    assert.equal(bytes.length, file.bytes);
    assert.equal(createHash('sha256').update(bytes).digest('hex'), file.sha256);
  }
  assert.equal(sample.files.screenshot.sha256, accepted.visual_acceptance.screenshot_sha256);
  assert.deepEqual(JSON.parse(readFileSync(join(exported, sample.files.capture_metadata.file), 'utf8')),
    JSON.parse(readFileSync(join(f.capture, 'metadata.json'), 'utf8')));
  assert.equal(requests.length, 1);
  assert.deepEqual(publishedRows(f.config.datasetPath), rows);
  assert.deepEqual(savedFiles(roots), before);
  assert.deepEqual(readdirSync(f.config.outputPath), ['001-success', '002-success']);
});

test('an export source failure keeps the accepted database progress and retries without visual reassessment', async (t) => {
  const f = fixture(t);
  f.config.project.delivery = { directory: '.', artifacts: [{ source: 'raw:missing', path: 'missing.bin' }] };
  const requests = await model(t, f.config, 'pass');
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /not indexed/);
  const failed = join(f.config.outputPath, '001-failed');
  const summary = runSummary(failed);
  assert.equal(summary.accepted.length, 1);
  const rows = publishedRows(f.config.datasetPath);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].status, 'committed');
  assert.equal(requests.length, 1);
  const roots = { evidence: f.capture, dataset: f.config.datasetPath, failed, failed_artifacts: artifacts(failed) };
  const before = savedFiles(roots);
  f.config.project.delivery = structuredClone(FIXTURE_DELIVERY);
  writeFixtureConfiguration(f.config);
  const key = process.env.HAR_GUI_FINALIZE_TEST_KEY;
  delete process.env.HAR_GUI_FINALIZE_TEST_KEY;
  let retried: string;
  try { retried = await finalize(f.config, { python: 'python' }, f.request); }
  finally { process.env.HAR_GUI_FINALIZE_TEST_KEY = key; }
  assert.equal(basename(retried), '002-success');
  assert.equal(runSummary(retried).model_usage.calls, 0);
  assert.equal(runSummary(retried).sample.sample_id, rows[0].manifest.sample_id);
  assert.equal(requests.length, 1);
  assert.deepEqual(publishedRows(f.config.datasetPath), rows);
  assert.deepEqual(savedFiles(roots), before);
});

test('unpublished capture keeps its old run origin while a new project run finalizes and publishes it', async (t) => {
  const f = fixture(t, true, true);
  const requests = await model(t, f.config, 'pass');
  const sourceRun = join(f.config.datasetPath, 'runs', 'source-run');
  const sourceConfiguration = join(sourceRun, 'configuration.json');
  writeFileSync(sourceConfiguration, JSON.stringify(f.config, null, 2) + '\n');
  writeFileSync(join(sourceRun, 'project.toml'), readFileSync(f.config.projectPath));
  const originalProjectDigest = f.config.digests.project;
  const modelDigest = f.config.digests.config;
  const oldConfiguration = readFileSync(sourceConfiguration);
  const oldCandidate = readFileSync(join(f.capture, 'candidate.json'));
  const oldWindows = readFileSync(join(f.capture, 'windows.txt'));
  assert.equal(dirname(f.capture), join(sourceRun, 'captures'));
  const captureId = JSON.parse(oldCandidate.toString()).capture_id;
  assert.equal(basename(f.capture), captureId);
  f.config.project.tasks[0].target_count = 200;
  f.config.project.tasks[0].instruction = 'Continue finding distinct states for the expanded project';
  f.config.project.tasks[0].acceptance = 'The requested project page is visible';
  f.config.project.run.max_actions = 20;
  f.config.project.run.max_duration_seconds = 120;
  writeFixtureConfiguration(f.config);
  assert.equal(f.config.project.tasks[0].privacy.reason, 'Synthetic fixture');
  assert.notEqual(f.config.digests.project, originalProjectDigest);
  assert.equal(f.config.digests.config, modelDigest);
  const finalized = await finalize(f.config, { python: 'python' }, f.request);
  const result = runSummary(finalized);
  assert.equal(basename(finalized), '001-success');
  assert.equal(dirname(result.artifacts_directory), join(f.config.datasetPath, 'runs'));
  const expectedIdentity = createHash('sha256').update(JSON.stringify({ config_sha256: modelDigest,
    project_sha256: f.config.digests.project, tasks: [{ name: 'editor', target_count: 200 }] })).digest('hex');
  assert.equal(result.status, 'complete');
  assert.equal(result.sample.identity_sha256, expectedIdentity);
  assert.equal(result.sample.sample_id, captureId);
  assert.equal(result.sample.evidence_directory, f.capture);
  assert.equal(result.accepted.length, 1);
  assert.equal(readdirSync(join(f.config.datasetPath, 'samples')).length, 1);
  assert.equal(requests.length, 1);
  const sample = JSON.parse(readFileSync(join(f.capture, 'sample.json'), 'utf8'));
  assert.equal(sample.finalization_directory, artifacts(finalized));
  assert.equal(sample.path, f.capture);
  const [row] = publishedRows(f.config.datasetPath);
  assert.equal(row.status, 'committed');
  assert.equal(row.source, join(f.capture, 'accepted'));
  assert.equal(row.manifest.evidence_directory, f.capture);
  assert.equal(row.manifest.sample_id, captureId);
  assert.deepEqual(readFileSync(sourceConfiguration), oldConfiguration);
  assert.equal(createHash('sha256').update(readFileSync(join(sourceRun, 'project.toml'))).digest('hex'),
    originalProjectDigest);
  assert.deepEqual(readFileSync(join(f.capture, 'candidate.json')), oldCandidate);
  assert.deepEqual(readFileSync(join(f.capture, 'windows.txt')), oldWindows);
  const roots = { original_run: sourceRun, finalization_run: finalized, finalization_artifacts: artifacts(finalized),
    dataset: f.config.datasetPath };
  const preserved = savedFiles(roots);
  const repeated = await finalize(f.config, { python: 'python' }, f.request);
  assert.equal(basename(repeated), '002-success');
  const receipt = runSummary(repeated);
  assert.equal(receipt.sample.sample_id, result.sample.sample_id);
  assert.equal(receipt.sample.identity_sha256, expectedIdentity);
  assert.equal(receipt.accepted.length, 1);
  assert.equal(receipt.model_usage.calls, 0);
  assert.equal(readdirSync(join(f.config.datasetPath, 'samples')).length, 1);
  assert.equal(requests.length, 1);
  assert.deepEqual(savedFiles(roots), preserved);
  const { run } = await import('../agent/runner.js');
  const beforeRuns = new Set(readdirSync(join(f.config.datasetPath, 'runs')));
  const resumed = await run(f.config, { python: 'python', hdc: join(f.directory, 'missing-hdc.exe'), serial: 'fixture' },
    { maxNewSamples: 0 });
  const created = readdirSync(join(f.config.datasetPath, 'runs')).filter((name) => !beforeRuns.has(name));
  assert.equal(created.length, 1);
  runArtifacts.set(resumed, join(f.config.datasetPath, 'runs', created[0]));
  const restored = runSummary(resumed);
  assert.equal(basename(resumed), '003-success');
  assert.equal(restored.actions, 0);
  assert.equal(restored.accepted[0].sample_id, result.sample.sample_id);
  assert.equal(requests.length, 1);
  assert.deepEqual(savedFiles(roots), preserved);
});

test('failed finalization keeps its numbered evidence while a successful retry publishes and resumes once', async (t) => {
  const f = fixture(t, true, true);
  const requests = await model(t, f.config, ['fail', 'pass']);
  const source = join(f.config.datasetPath, 'runs', 'source-run');
  writeFileSync(join(source, 'configuration.json'), JSON.stringify(f.config, null, 2) + '\n');
  writeFileSync(join(source, 'project.toml'), readFileSync(f.config.projectPath));
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /Project acceptance failed/);
  const failed = join(f.config.outputPath, '001-failed');
  const summary = runSummary(failed);
  assert.equal(summary.status, 'failed');
  assert.equal(summary.candidate.path, f.capture);
  assert.equal(summary.model_usage.completed_calls, 1);
  assert.equal(existsSync(join(f.config.datasetPath, 'dataset.sqlite3')), false);
  const failedRoots = { failed, failed_artifacts: artifacts(failed) };
  const failedFiles = savedFiles(failedRoots);
  const success = await finalize(f.config, { python: 'python' }, f.request);
  assert.equal(basename(success), '002-success');
  const result = runSummary(success);
  assert.equal(result.sample.evidence_directory, f.capture);
  assert.equal(JSON.parse(readFileSync(join(f.capture, 'sample.json'), 'utf8')).finalization_directory,
    artifacts(success));
  assert.equal(publishedRows(f.config.datasetPath).length, 1);
  assert.deepEqual(savedFiles(failedRoots), failedFiles);
  const roots = { ...failedRoots, source, success, success_artifacts: artifacts(success), dataset: f.config.datasetPath };
  const preserved = savedFiles(roots);
  const repeated = await finalize(f.config, { python: 'python' }, f.request);
  assert.equal(basename(repeated), '003-success');
  assert.equal(runSummary(repeated).model_usage.calls, 0);
  assert.equal(requests.length, 2);
  assert.equal(publishedRows(f.config.datasetPath).length, 1);
  assert.deepEqual(savedFiles(roots), preserved);
});

test('published recovery requires matching raw project evidence from a saved or explicit source', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'pass');
  const original = await finalize(f.config, { python: 'python' }, f.request);
  const result = runSummary(original);
  const archive = join(f.directory, 'project-archives');
  mkdirSync(join(archive, 'nested'), { recursive: true });
  const source = join(archive, 'nested', 'original-project.toml');
  writeFileSync(source, readFileSync(join(artifacts(original), 'project.toml')));
  unlinkSync(join(artifacts(original), 'project.toml'));
  f.config.project.tasks[0].target_count = 200;
  f.config.project.tasks[0].instruction = 'Collect further distinct project states';
  writeFixtureConfiguration(f.config);
  const roots = { evidence: f.capture, dataset: f.config.datasetPath, original_run: original,
    original_artifacts: artifacts(original), archive };
  const preserved = savedFiles(roots);
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request),
    /Raw project source is missing for the saved project digest/);
  assert.equal(requests.length, 1);
  assert.deepEqual(savedFiles(roots), preserved);
  for (const projectSource of [source, archive]) {
    const repeated = await finalize(f.config, { python: 'python' }, { ...f.request, projectSources: [projectSource] });
    const receipt = runSummary(repeated);
    assert.equal(receipt.sample.sample_id, result.sample.sample_id);
    assert.equal(receipt.model_usage.calls, 0);
    assert.equal(receipt.accepted.length, 1);
    assert.equal(requests.length, 1);
    assert.deepEqual(savedFiles(roots), preserved);
  }
});

test('finalization refuses a changed current raw project before preserving a snapshot or calling the model', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'pass');
  const raw = Buffer.concat([readFileSync(f.config.projectPath), Buffer.from('\n# Changed after loading\n')]);
  writeFileSync(f.config.projectPath, raw);
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /Project source digest changed/);
  const directory = f.runs()[0].artifacts_directory;
  assert.equal(existsSync(join(directory, 'project.toml')), false);
  assert.equal(existsSync(join(directory, 'configuration.json')), false);
  assert.equal(existsSync(join(f.config.outputPath, '001-failed', 'manifest.json')), true);
  assert.equal(f.runs()[0].status, 'failed');
  assert.equal(requests.length, 0);
  assert.deepEqual(readFileSync(f.config.projectPath), raw);
});

test('finalization refuses a snapshot changed without matching its original project TOML', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'pass');
  f.config.project.tasks[0].acceptance = 'A forged acceptance that was never present in the project source';
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request),
    /Saved project snapshot differs from its raw TOML source/);
  assert.equal(requests.length, 0);
  assert.ok(f.runs().every((entry) => entry.status === 'failed' && entry.model_usage.calls === 0));
});

test('unpublished offline finalization cannot invent a missing paired-page verdict', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'pass');
  unlinkSync(join(f.capture, 'page-stability.json'));
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /page-stability\.json|semantic/i);
  assert.equal(requests.length, 0);
  assert.equal(existsSync(join(f.capture, 'page-stability.json')), false);
  assert.equal(existsSync(join(f.capture, 'publication.json')), false);
  assert.ok(f.runs().every((entry) => entry.status === 'failed' && entry.model_usage.calls === 0));
});

test('prepared redaction preserves the original paired-page receipt and accepts only the final masked image', async (t) => {
  const f = fixture(t, true, false, true);
  const requests = await model(t, f.config, 'pass');
  const metadata = JSON.parse(readFileSync(join(f.capture, 'metadata.json'), 'utf8'));
  const observation = JSON.parse(readFileSync(join(f.capture, 'page-observation.json'), 'utf8'));
  const semantic = readFileSync(join(f.capture, 'page-stability.json'));
  assert.equal(metadata.privacy.enabled, true);
  assert.notEqual(metadata.privacy.before_sha256, metadata.privacy.after_sha256);
  assert.equal(observation.before.screenshot_sha256, metadata.privacy.before_sha256);
  assert.equal(metadata.files.screenshot.sha256, metadata.privacy.after_sha256);
  const directory = await finalize(f.config, { python: 'python' }, f.request);
  const result = runSummary(directory);
  assert.equal(result.status, 'complete');
  assert.equal(result.sample.visual_acceptance.screenshot_sha256, metadata.privacy.after_sha256);
  assert.equal(requests.length, 1);
  const image = sharp(readFileSync(f.prepared.screenshot.file));
  const pixel = await image.extract({ left: 1034, top: 153, width: 1, height: 1 }).removeAlpha().raw().toBuffer();
  assert.deepEqual([...pixel], [0, 0, 0]);
  assert.deepEqual(readFileSync(join(f.capture, 'page-stability.json')), semantic);
  const roots = { evidence: f.capture, dataset: f.config.datasetPath, original_run: directory,
    original_artifacts: artifacts(directory) };
  const preserved = savedFiles(roots);
  const repeated = await finalize(f.config, { python: 'python' }, f.request);
  assert.equal(runSummary(repeated).model_usage.calls, 0);
  assert.equal(requests.length, 1);
  assert.deepEqual(savedFiles(roots), preserved);
});

test('offline paired-page verification rejects missing or changed privacy provenance before any model request', async (t) => {
  for (const enabled of [false, true]) {
    for (const corruption of ['missing privacy receipt', 'changed privacy receipt', 'metadata privacy conflict']) {
      await t.test(`enabled ${enabled}: ${corruption}`, async (child) => {
        const f = fixture(child, true, false, enabled);
        const requests = await model(child, f.config, 'pass');
        const privacyPath = join(f.capture, 'privacy.json');
        if (corruption === 'missing privacy receipt') unlinkSync(privacyPath);
        if (corruption === 'changed privacy receipt') {
          const privacy = JSON.parse(readFileSync(privacyPath, 'utf8'));
          privacy.before_sha256 = '0'.repeat(64);
          writeFileSync(privacyPath, JSON.stringify(privacy));
        }
        if (corruption === 'metadata privacy conflict') {
          const metadataPath = join(f.capture, 'metadata.json');
          const metadata = JSON.parse(readFileSync(metadataPath, 'utf8'));
          metadata.privacy.before_sha256 = '0'.repeat(64);
          writeFileSync(metadataPath, JSON.stringify(metadata));
        }
        await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /privacy|redact|screenshot/i);
        assert.equal(requests.length, 0);
        assert.equal(existsSync(join(f.capture, 'publication.json')), false);
      });
    }
  }
});

test('rejected saved-image assertion keeps usage and leaves the dataset uncounted', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'fail');
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /Project acceptance failed/);
  assert.equal(requests.length, 1);
  const [run] = f.runs();
  assert.equal(run.status, 'failed');
  assert.equal(run.model_usage.completed_calls, 1);
  assert.equal(run.model_usage.totals.total_tokens, 110);
  assert.equal(existsSync(join(f.config.datasetPath, 'dataset.sqlite3')), false);
  assert.equal(existsSync(join(f.capture, 'accepted')), true);
});

test('provider failure is recorded as failed usage without manufacturing tokens or publishing', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'error');
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /acceptance/);
  assert.equal(requests.length, 1);
  const [run] = f.runs();
  assert.equal(run.status, 'failed');
  assert.equal(run.model_usage.failed_calls, 1);
  assert.equal(run.model_usage.totals.total_tokens, null);
  assert.equal(existsSync(join(f.config.datasetPath, 'dataset.sqlite3')), false);
});

test('direct capture requires explicit target and finalizes locally without candidate history', async (t) => {
  const f = fixture(t, false);
  const requests = await model(t, f.config, 'pass');
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /target bundle is missing/);
  assert.equal(requests.length, 0);
  await finalize(f.config, { python: 'python' }, { ...f.request, targetBundleName: 'com.example.settings' });
  assert.equal(requests.length, 1);
  const sample = JSON.parse(readFileSync(join(f.capture, 'sample.json'), 'utf8'));
  assert.equal(sample.origin, 'direct_capture');
  assert.deepEqual(sample.window_changes, []);
});

test('task, target identity and prepared file tampering fail before any model request', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'pass');
  await assert.rejects(finalize(f.config, { python: 'python' }, { ...f.request, taskName: 'missing' }), /Unknown/);
  await assert.rejects(finalize(f.config, { python: 'python' }, { ...f.request, targetBundleName: 'app.other' }), /differs/);
  const rawFile = join(f.capture, 'windows.txt');
  const original = readFileSync(rawFile);
  writeFileSync(rawFile, 'tampered raw window evidence');
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request),
    /Page observation raw WMS or layout source differs from its captured index/);
  writeFileSync(rawFile, original);
  writeFileSync(join(f.prepared.path, f.prepared.files.capture.file), '{}');
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request),
    /Prepared capture facts changed after preparation/);
  assert.equal(requests.length, 0);
  assert.ok(f.runs().every((run) => run.status === 'failed' && run.model_usage.calls === 0));
});
