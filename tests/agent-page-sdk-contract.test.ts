import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import sharp from 'sharp';
import type { LoadedConfiguration } from '../agent/config.js';
import { CollectionAgent, CollectionDevice } from '../agent/device.js';
import { assessPageStability, verifyPageStability } from '../agent/page-stability.js';
import { RunState } from '../agent/state.js';
import { UsageRecorder } from '../agent/usage.js';
import { writePageObservation } from './agent-page-fixture.js';

const ANSWER = { decision: 'same_page', evidence: 'Mocked SDK object contract; no visual inference performed' };
const USAGE = { prompt_tokens: 41, completion_tokens: 9, total_tokens: 50,
  prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: 41 };

/** @brief 构造可区分顺序的匿名 PNG，不读取真实采集图片。 */
async function image(changed = false): Promise<Buffer> {
  const pixels = Buffer.alloc(8 * 6 * 3, 255);
  if (changed) pixels.fill(0, (1 * 8 + 2) * 3, (1 * 8 + 2) * 3 + 3);
  return sharp(pixels, { raw: { width: 8, height: 6, channels: 3 } }).png().toBuffer();
}

/** @brief 保存匿名采集索引并从对应全图实际裁出首尾窗口，末帧图片始终只在内存中。 */
async function captureFixture(directory: string) {
  const before = await image();
  const after = await image(true);
  const files: Record<string, any> = {};
  for (const [file, bytes] of Object.entries({ 'screenshot.png': before, 'windows.txt': Buffer.from('before windows'),
    'closing-windows.txt': Buffer.from('after windows'), 'layout.json': Buffer.from('{"fixture":"before"}'),
    'closing-layout.json': Buffer.from('{"fixture":"after"}') })) {
    writeFileSync(join(directory, file), bytes);
    files[file.replaceAll('-', '_').split('.')[0]] = { file, bytes: bytes.length,
      sha256: createHash('sha256').update(bytes).digest('hex') };
  }
  Object.assign(files.screenshot, { width: 8, height: 6 });
  const target = { window_id: 1, bundle_name: 'app.fixture', process_id: 2, display_id: 0, is_system: false,
    screen_bounds: { x: 0, y: 0, width: 4, height: 3 } };
  const metadata = { capture_id: 'sdk-synthetic-frame', display: { id: 0, width: 8, height: 6 },
    windows: [target], files, consistency: { status: 'observed', pixels: { status: 'observed', changed: true } } };
  const crop = { left: 0, top: 0, width: 4, height: 3 };
  const observation = writePageObservation(directory, metadata, 1, {
    before: await sharp(before).extract(crop).png().toBuffer(),
    after: await sharp(after).extract(crop).png().toBuffer(), afterScreen: after });
  return { metadata, observation };
}

/** @brief 使用真实 SDK 公开客户端工厂和用量记录器，仅替换本地响应，禁止任何外部请求。 */
function sdkAgent(t: TestContext, device: CollectionDevice, directory: string) {
  const recorder = new UsageRecorder(join(directory, 'model-usage.jsonl'));
  const requests: any[] = [];
  const outputs: unknown[] = [];
  const usages: unknown[] = [];
  const answer: { value: unknown } = { value: ANSWER };
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('Unexpected external network request'); });
  t.mock.method(device, 'getHdc', async () => { throw new Error('Unexpected device connection'); });
  t.mock.method(device, 'windows', async () => { throw new Error('Unexpected live window observation'); });
  const agent = new CollectionAgent(device, { cache: false, generateReport: false, persistExecutionDump: false,
    autoPrintReportMsg: false, screenshotShrinkFactor: 2, onLLMUsage: (usage) => { usages.push(usage); },
    modelConfig: { MIDSCENE_MODEL_FAMILY: 'deepseek', MIDSCENE_MODEL_NAME: 'fixture',
      MIDSCENE_MODEL_API_KEY: 'unused-sdk-fixture-key', MIDSCENE_MODEL_BASE_URL: 'https://fixture.invalid/v1',
      MIDSCENE_MODEL_STREAM_MODE: 'non-stream', MIDSCENE_MODEL_RETRY_COUNT: 0 },
    createOpenAIClient: async (client, options) => {
      const fake = { chat: { completions: { create: async (body: any) => {
        requests.push(structuredClone(body));
        const serialized = JSON.stringify(answer.value);
        // 合法 JSON 字符串使用 Unicode 花括号，避免 SDK 先截出内部对象而改变本例的 API 返回类型。
        const data = typeof answer.value === 'string'
          ? serialized.replaceAll('{', '\\u007b').replaceAll('}', '\\u007d') : serialized;
        return { id: 'sdk-fixture-response', object: 'chat.completion', created: 1, model: 'fixture', usage: USAGE,
          choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant',
            content: `<observation>Mocked offline SDK extraction</observation>`
              + `<data-json>${data}</data-json>` } }] };
      } } } };
      return recorder.createOpenAIClient(fake as typeof client, options);
    } });
  const query = agent.aiQuery.bind(agent);
  // 仅监听真正公开方法的返回值；请求和解析仍完整经过已安装 SDK。
  t.mock.method(agent, 'aiQuery', async (...args: Parameters<CollectionAgent['aiQuery']>) => {
    const value = await query(...args);
    outputs.push(value);
    return value;
  });
  return { agent, requests, outputs, usages, recorder, answer };
}

/** @brief 所有文件只创建在隔离工作区的输出目录，测试结束后移除自己的匿名目录。 */
async function fixture(t: TestContext) {
  const parent = resolve('artifacts/memo-semantic-engine-20261023/sdk-tests');
  mkdirSync(parent, { recursive: true });
  const directory = mkdtempSync(join(parent, 'anonymous-'));
  t.after(() => {
    assert.equal(dirname(directory), parent);
    assert.ok(basename(directory).startsWith('anonymous-'));
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  const { metadata, observation } = await captureFixture(directory);
  const config = { project: { run: { max_duration_seconds: 60, max_actions: 5 }, tasks: [{ name: 'fixture',
    instruction: 'Inspect synthetic page', acceptance: 'Synthetic page remains visible' }] } } as LoadedConfiguration;
  const state = new RunState(config, directory, directory);
  state.candidate = { capture_id: metadata.capture_id, path: directory,
    target_window_id: 1, target_bundle_name: 'app.fixture' };
  const device = new CollectionDevice({ python: 'unused', hdc: 'unused', serial: 'fixture' }, state);
  device.pageObservation = observation;
  return { directory, metadata, observation, state, device, ...sdkAgent(t, device, directory) };
}

/** @brief 检查发给客户端的真实 SDK 请求，证明有两张原始 PNG 且输出需求没有 String 包装。 */
function assertRequest(request: any, observation: Awaited<ReturnType<typeof captureFixture>>['observation']) {
  assert.equal(request.stream, false);
  const parts = request.messages.flatMap((message: any) => Array.isArray(message.content) ? message.content : []);
  const images = parts.filter((part: any) => part.type === 'image_url').map((part: any) => part.image_url.url);
  assert.deepEqual(images, [observation.before_image, observation.after_image]);
  const hashes = images.map((url: string) => createHash('sha256')
    .update(Buffer.from(url.slice('data:image/png;base64,'.length), 'base64')).digest('hex'));
  assert.deepEqual(hashes, [observation.receipt.before.crop_sha256, observation.receipt.after.crop_sha256]);
  const demandText = parts.find((part: any) => part.type === 'text' && part.text.includes('<PageDescription>'))?.text;
  assert.equal(typeof demandText, 'string');
  const demand = JSON.parse(demandText.match(/<DATA_DEMAND>\s*([\s\S]*?)\s*<\/DATA_DEMAND>/)[1]);
  assert.deepEqual(Object.keys(demand).sort(), ['decision', 'evidence']);
  assert.equal(typeof demand.decision, 'string');
  assert.equal(typeof demand.evidence, 'string');
  assert.equal(Object.hasOwn(demand, 'String'), false);
}

test('real SDK structured extraction sends both exact PNGs once and records object decision and usage', async (t) => {
  const f = await fixture(t);
  const before = new Map(readdirSync(f.directory).map((file) => [file, readFileSync(join(f.directory, file))]));
  const receipt = await assessPageStability(f.state, f.device, f.agent, f.metadata);
  assert.equal(receipt.decision, 'same_page');
  assert.equal(f.requests.length, 1);
  assert.deepEqual(f.outputs, [ANSWER]);
  assertRequest(f.requests[0], f.observation);
  assert.equal(f.recorder.summary().calls, 1);
  assert.equal(f.recorder.summary().completed_calls, 1);
  assert.deepEqual(f.recorder.summary().totals, USAGE);
  assert.equal(f.usages.length, 1);
  assert.deepEqual(await verifyPageStability(f.directory, f.metadata, f.state.candidate), receipt);
  for (const [file, bytes] of before) {
    if (file !== 'model-usage.jsonl') assert.deepEqual(readFileSync(join(f.directory, file)), bytes);
  }
  assert.deepEqual(readdirSync(f.directory).filter((file) => file.endsWith('.png')), ['screenshot.png']);
  assert.doesNotMatch(readFileSync(join(f.directory, 'events.jsonl'), 'utf8'), /data:image|before_image|after_image/);
  assert.equal(f.device.pageObservation, undefined);
  assert.equal(f.device.frozenScreenshot, undefined);
});

test('real SDK rejects nonobject and malformed structured results without fallback', async (t) => {
  const answers = { serialized: JSON.stringify(ANSWER), array: ['same_page'], null: null,
    extra: { ...ANSWER, stable: true }, missing: { decision: 'same_page' },
    enum: { ...ANSWER, decision: 'stable' }, blank: { ...ANSWER, evidence: ' ' } };
  for (const [name, answer] of Object.entries(answers)) {
    await t.test(name, async (child) => {
      const f = await fixture(child);
      f.answer.value = answer;
      await assert.rejects(assessPageStability(f.state, f.device, f.agent, f.metadata));
      assert.equal(f.requests.length, 1);
      assert.equal(f.outputs.length, 1);
      if (name === 'serialized') {
        assert.equal(typeof f.outputs[0], 'string');
        assert.equal(f.outputs[0], answer);
      } else if (name === 'array') {
        assert.equal(Array.isArray(f.outputs[0]), true);
        assert.deepEqual(f.outputs[0], answer);
      } else if (name === 'null') assert.equal(f.outputs[0], null);
      else assert.equal(typeof f.outputs[0], 'object');
      assertRequest(f.requests[0], f.observation);
      assert.equal(f.recorder.summary().calls, 1);
      assert.equal(f.recorder.summary().completed_calls, 1);
      assert.equal(f.usages.length, 1);
      assert.equal(existsSync(join(f.directory, 'page-stability.json')), false);
      assert.equal(f.device.pageObservation, undefined);
      assert.equal(f.device.frozenScreenshot, undefined);
    });
  }
});

test('real SDK path retains rejection of changed target identity or crop hash before the client factory', async (t) => {
  for (const corruption of ['target', 'hash']) {
    await t.test(corruption, async (child) => {
      const f = await fixture(child);
      if (corruption === 'target') f.state.candidate.target_window_id = 99;
      else f.device.pageObservation!.before_image = f.observation.after_image;
      await assert.rejects(assessPageStability(f.state, f.device, f.agent, f.metadata));
      assert.equal(f.requests.length, 0);
      assert.deepEqual(f.outputs, []);
      assert.equal(f.recorder.summary().calls, 0);
      assert.equal(f.usages.length, 0);
      assert.equal(existsSync(join(f.directory, 'page-stability.json')), false);
      assert.equal(f.device.pageObservation, undefined);
      assert.equal(f.device.frozenScreenshot, undefined);
    });
  }
});
