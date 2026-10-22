import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, utimesSync, writeFileSync,
} from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { after, test, type TestContext } from 'node:test';
import sharp from 'sharp';
import { localBridge } from '../agent/bridge.js';
import type { LoadedConfiguration } from '../agent/config.js';

const sdkDirectory = mkdtempSync(join(tmpdir(), 'har-gui-finalize-sdk-'));
process.env.MIDSCENE_RUN_DIR = sdkDirectory;
process.env.MIDSCENE_RECORD_MODEL_CALL = 'false';
process.env.HAR_GUI_FINALIZE_TEST_KEY = 'local-fixture-key';
const { finalize, SavedScreenshotDevice } = await import('../agent/finalize.js');
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
from tests.support import fake_hdc
from har_capture.collector import capture
from har_capture.delivery_operations import prepare_delivery
root = Path(sys.argv[1])
source = capture(fake_hdc(),root.parent / 'collected')
root.rmdir()
source.rename(root)
metadata = json.loads((root / 'metadata.json').read_bytes())
policy = {'enabled':False,'reason':'Synthetic fixture'}
options = {'app':'editor','page_id':sys.argv[2] or metadata['capture_id'],'scene':'scene',
           'state_id':metadata['capture_id'],'canvas':{'width':3120,'height':2080},'stability_regions':[]}
def normalized(directory,window,canvas,scenes):
    tree = json.loads((directory / window['trees']['arkui']['file']).read_bytes())
    tree['coordinate_validation'] = {'status':'verified'}
    return {'window_id':window['window_id'],'bundle_name':window['bundle_name'],'tree':tree}
with patch('har_capture.export.load_scene_evidence',return_value={}), \\
        patch('har_capture.export.normalized_arkui',side_effect=normalized):
    prepare_delivery({'directory':str(root),'privacy':policy,'options':options})
`;

/** 在测试临时目录建立真实五件套及可重新核验的准备凭证。 */
function fixture(t: TestContext, candidate = true) {
  const directory = mkdtempSync(join(tmpdir(), 'har-gui-finalize-test-'));
  const capture = join(directory, 'capture');
  mkdirSync(capture);
  t.after(() => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    assert.ok(basename(directory).startsWith('har-gui-finalize-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  t.mock.method(console, 'warn', () => {});
  execFileSync('python', ['-c', PREPARED_FIXTURE, capture, candidate ? 'page' : ''], {
    windowsHide: true, env: { ...process.env, PYTHONUTF8: '1' },
  });
  const metadata = JSON.parse(readFileSync(join(capture, 'metadata.json'), 'utf8'));
  if (candidate) writeFileSync(join(capture, 'candidate.json'), JSON.stringify({ capture_id: metadata.capture_id,
    path: capture, task_name: 'editor', target_bundle_name: 'com.example.settings',
    page_name: 'page', window_changes: [] }));
  const config: LoadedConfiguration = { projectPath: join(directory, 'project.toml'),
    configPath: join(directory, 'config.toml'), outputPath: join(directory, 'runs'),
    datasetPath: join(directory, 'dataset'), digests: { project: 'a'.repeat(64), config: 'b'.repeat(64) },
    profile: { provider: 'openai_compatible', base_url: 'http://127.0.0.1:1/v1', model: 'fixture-vision',
      model_family: 'deepseek', api_key_env: 'HAR_GUI_FINALIZE_TEST_KEY', supports_vision: true },
    project: { schema_version: 1, run: { name: 'fixture', output: 'runs', max_actions: 1, max_duration_seconds: 60 },
      agent: { llm: 'vision' }, dataset: { root: 'dataset', canvas: { width: 3120, height: 2080 }, stability_regions: [] },
      tools: { enabled: ['Capture'] }, tasks: [{ name: 'editor', app: 'editor', scene: 'scene', target_count: 1,
        privacy: { enabled: false, reason: 'Synthetic fixture' }, bundle_names: ['com.example.settings'],
        instruction: 'Inspect the saved fixture', acceptance: 'The saved screenshot is white' }] } };
  return { directory, capture, config, request: { directory: capture, taskName: 'editor' },
    runs: () => readdirSync(config.outputPath).map((name) => JSON.parse(
      readFileSync(join(config.outputPath, name, 'run.json'), 'utf8'))) };
}

/** 记录全部原始证据、发布收据及正式五件套的字节摘要和精确修改时间。 */
function savedFiles(roots: Record<string, string>) {
  const result: Record<string, { sha256: string; modified: string }> = {};
  for (const [label, root] of Object.entries(roots)) {
    const pending = [root];
    while (pending.length) {
      const directory = pending.pop()!;
      for (const entry of readdirSync(directory, { withFileTypes: true })) {
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

/** 将已发布收据改为等价的不同键序、缩进、转义和换行，验证恢复不会重新序列化。 */
function varyReceipts(capture: string): void {
  for (const name of ['metadata.json', 'delivery-manifest.json', 'delivery-preparation.json', 'publication.json']) {
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

/** 使用真正 HTTP 和 OpenAI SDK 流程返回可控视觉验收响应，不连接外部服务。 */
async function model(t: TestContext, config: LoadedConfiguration, mode: 'pass' | 'fail' | 'error') {
  const requests: any[] = [];
  const server = createServer(async (request, response) => {
    let content = '';
    for await (const chunk of request) content += chunk;
    requests.push(JSON.parse(content));
    response.setHeader('content-type', 'application/json');
    if (mode === 'error') { response.statusCode = 400; response.end('{"error":{"message":"fixture"}}'); return; }
    response.end(JSON.stringify({ id: 'local-response', object: 'chat.completion', created: 1, model: 'fixture-vision',
      usage: { prompt_tokens: 100, completion_tokens: 10, total_tokens: 110,
        prompt_cache_hit_tokens: 40, prompt_cache_miss_tokens: 60 },
      choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant',
        content: '<observation>private-model-observation</observation><data-json>'
          + JSON.stringify({ StatementIsTruthy: mode === 'pass' }) + '</data-json>' } }] }));
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  config.profile.base_url = `http://127.0.0.1:${(server.address() as { port: number }).port}/v1`;
  t.after(async () => { server.closeAllConnections(); await new Promise<void>((resolve) => server.close(() => resolve())); });
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

test('offline finalization uses the saved final PNG, accounts real model usage, and publishes once', async (t) => {
  const f = fixture(t);
  const requests = await model(t, f.config, 'pass');
  const metadata = JSON.parse(readFileSync(join(f.capture, 'metadata.json'), 'utf8'));
  const expectedHash = metadata.delivery.files.screenshot.sha256;
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
  const result = JSON.parse(readFileSync(join(run, 'run.json'), 'utf8'));
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
  assert.equal(readdirSync(result.sample.path).length, 5);
  assert.equal(result.sample.files.screenshot.sha256, expectedHash);
  assert.equal(existsSync(join(f.capture, 'delivery')), false);
  assert.doesNotMatch(readFileSync(join(run, 'model-usage.jsonl'), 'utf8'), /private-model|data:image|local-fixture-key/);
  varyReceipts(f.capture);
  const roots = { evidence: f.capture, dataset: f.config.datasetPath };
  const preserved = savedFiles(roots);
  for (const repeat of ['published', 'repeated']) {
    const resumed = await finalize(f.config, { python: 'python' }, f.request);
    const receipt = JSON.parse(readFileSync(join(resumed, 'run.json'), 'utf8'));
    assert.equal(receipt.sample.sample_id, result.sample.sample_id, repeat);
    assert.equal(receipt.model_usage.calls, 0);
    assert.equal(receipt.accepted.length, 1);
    assert.deepEqual(savedFiles(roots), preserved);
  }
  const status = await localBridge('python', { operation: 'dataset_status', root: f.config.datasetPath,
    identity: { project_sha256: f.config.digests.project, config_sha256: f.config.digests.config,
      tasks: f.config.project.tasks.map(({ name, target_count }) => ({ name, target_count })) } });
  assert.deepEqual(status.counts, { editor: 1 });
  assert.deepEqual(status.remaining, { editor: 0 });
  assert.deepEqual(status.failures, []);
  assert.deepEqual(savedFiles(roots), preserved);
  assert.equal(requests.length, 1);
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
  assert.equal(existsSync(join(f.capture, 'delivery')), true);
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
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /target bundle is missing/);
  const requests = await model(t, f.config, 'pass');
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
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /digest mismatch/);
  writeFileSync(rawFile, original);
  const metadata = JSON.parse(readFileSync(join(f.capture, 'metadata.json'), 'utf8'));
  writeFileSync(join(f.capture, 'delivery', metadata.delivery.files.tree.file), '{}');
  await assert.rejects(finalize(f.config, { python: 'python' }, f.request), /digest mismatch/);
  assert.equal(requests.length, 0);
  assert.ok(f.runs().every((run) => run.status === 'failed' && run.model_usage.calls === 0));
});
