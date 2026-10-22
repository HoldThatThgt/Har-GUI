import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import OpenAI from 'openai';
import { UsageRecorder, estimateUsageCost, type ModelUsageRecord, type UsageRates } from '../agent/usage.js';

const PEAK: UsageRates = { currency: 'USD', input_cache_hit_per_million: 0.006,
  input_cache_miss_per_million: 0.3, output_per_million: 1.2 };
const OFF_PEAK: UsageRates = { currency: 'USD', input_cache_hit_per_million: 0.003,
  input_cache_miss_per_million: 0.15, output_per_million: 0.6 };
const USAGE = { prompt_tokens: 1_000_000, completion_tokens: 100_000, total_tokens: 1_100_000,
  prompt_cache_hit_tokens: 800_000, prompt_cache_miss_tokens: 200_000 };

/** 只创建本地审计目录；测试完全不访问模型服务。 */
function fixture(t: TestContext) {
  const parent = resolve(tmpdir());
  const directory = mkdtempSync(join(parent, 'har-gui-usage-test-'));
  t.after(() => {
    assert.equal(dirname(directory), parent);
    assert.ok(basename(directory).startsWith('har-gui-usage-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  const file = join(directory, 'model-usage.jsonl');
  const recorder = new UsageRecorder(file);
  const entries = () => readFileSync(file, 'utf8').trim().split('\n').filter(Boolean)
    .map((line) => JSON.parse(line) as ModelUsageRecord);
  const rows = () => [...new Map(entries().map((record) => [record.call_id, record])).values()];
  return { file, recorder, rows, entries };
}

/** 创建服务商响应替身，包含必须排除的响应正文。 */
function response(usage: unknown = USAGE, model = 'reported-model') {
  return { id: 'provider-response', object: 'chat.completion', created: 1, model, usage,
    choices: [{ index: 0, finish_reason: 'stop', message: { role: 'assistant', content: 'private-response' } }] };
}

/** 将替身调用接入真正公开的 client factory，不伪造 SDK 内部事件。 */
async function wrap(recorder: UsageRecorder, invoke: (...args: any[]) => Promise<any>) {
  const client = { chat: { completions: { create: invoke } } };
  return recorder.createOpenAIClient(client, {});
}

test('records real provider usage through the official OpenAI client without persisting content or keys', async (t) => {
  const f = fixture(t);
  let requests = 0;
  const client = new OpenAI({ apiKey: 'private-api-key', baseURL: 'https://fixture.invalid/v1', maxRetries: 0,
    fetch: async (input, init) => {
      requests += 1;
      const request = input instanceof Request ? input : new Request(input, init);
      assert.equal(request.headers.get('authorization'), 'Bearer private-api-key');
      const body = await request.json() as any;
      assert.equal(body.model, 'requested-model');
      assert.equal(body.stream, false);
      return new Response(JSON.stringify(response({ ...USAGE, private_extra: 'private-usage-text' })), {
        status: 200, headers: { 'content-type': 'application/json' },
      });
    } });
  const wrapped = await f.recorder.createOpenAIClient(client, { apiKey: 'private-api-key' });
  const result = await wrapped.chat.completions.create({ model: 'requested-model', stream: false,
    messages: [{ role: 'user', content: [{ type: 'text', text: 'private-prompt' },
      { type: 'image_url', image_url: { url: 'data:image/png;base64,private-image' } }] }] });
  assert.equal(requests, 1);
  assert.equal(result.choices[0].message.content, 'private-response');
  const [record] = f.rows();
  assert.equal(record.requested_model, 'requested-model');
  assert.equal(record.model, 'reported-model');
  assert.equal(record.status, 'completed');
  assert.deepEqual(record.usage, USAGE);
  assert.ok(Date.parse(record.finished_at!) >= Date.parse(record.started_at));
  assert.ok(record.started_at.endsWith('Z') && record.finished_at?.endsWith('Z'));
  assert.doesNotMatch(readFileSync(f.file, 'utf8'), /private-|data:image|messages|choices|apiKey/);
  assert.deepEqual(f.recorder.summary().totals, USAGE);
});

test('wrapper preserves this, request options, response identity and AbortSignal', async (t) => {
  const f = fixture(t);
  const signal = new AbortController().signal;
  const options = { signal };
  const expected = response();
  let completion: object;
  const client = { chat: { completions: { async create(this: object, body: any, actualOptions: any) {
    assert.equal(this, completion);
    assert.equal(body.model, 'fixture');
    assert.equal(actualOptions, options);
    assert.equal(actualOptions.signal, signal);
    return expected;
  } } } };
  completion = client.chat.completions;
  const wrapped = await f.recorder.createOpenAIClient(client, {});
  assert.equal(await wrapped.chat.completions.create({ model: 'fixture' }, options), expected);
  assert.equal(f.rows().length, 1);
});

test('missing provider usage remains null and cannot produce a zero-token or zero-cost claim', async (t) => {
  const f = fixture(t);
  const client = await wrap(f.recorder, async () => response(null));
  await client.chat.completions.create({ model: 'fixture' });
  assert.equal(f.rows()[0].usage, null);
  const summary = f.recorder.summary();
  assert.equal(summary.missing_usage_calls, 1);
  assert.equal(summary.partial_usage_calls, 0);
  assert.ok(Object.values(summary.totals).every((value) => value === null));
  const price = estimateUsageCost(f.recorder.records, PEAK);
  assert.equal(price.total_cost_usd, null);
  assert.equal(price.calls[0].cost_usd, null);
  assert.equal(price.unpriced_calls, 1);
  assert.match(JSON.stringify(price), /"total_cost_usd":null/);
});

test('partial usage does not infer DeepSeek cache hit or miss from totals or another API field', async (t) => {
  const f = fixture(t);
  const client = await wrap(f.recorder, async () => response({ prompt_tokens: 100, completion_tokens: 10,
    total_tokens: 110, prompt_tokens_details: { cached_tokens: 80 } }));
  await client.chat.completions.create({ model: 'fixture' });
  const summary = f.recorder.summary();
  assert.equal(summary.partial_usage_calls, 1);
  assert.equal(summary.missing_usage_calls, 0);
  assert.equal(summary.totals.prompt_tokens, 100);
  assert.equal(summary.totals.prompt_cache_hit_tokens, null);
  assert.equal(summary.totals.prompt_cache_miss_tokens, null);
  assert.equal(estimateUsageCost(f.recorder.records, PEAK).total_cost_usd, null);
});

test('invalid counters remain unknown while an explicit provider zero remains zero', async (t) => {
  const f = fixture(t);
  const client = await wrap(f.recorder, async () => response({ prompt_tokens: '100', completion_tokens: -1,
    total_tokens: 1.5, prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: null }));
  await client.chat.completions.create({ model: 'fixture' });
  assert.deepEqual(f.rows()[0].usage, { prompt_tokens: null, completion_tokens: null, total_tokens: null,
    prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: null });
  assert.equal(f.recorder.summary().partial_usage_calls, 1);
});

test('failed and aborted calls retain their original exception without recording error payloads', async (t) => {
  const f = fixture(t);
  for (const error of [new Error('private-service-error'), new DOMException('private-abort-reason', 'AbortError')]) {
    const client = await wrap(f.recorder, async () => { throw error; });
    await assert.rejects(client.chat.completions.create({ model: 'fixture' }), (actual) => actual === error);
  }
  assert.equal(f.rows().length, 2);
  assert.ok(f.rows().every((record) => record.status === 'failed' && record.usage === null && record.finished_at));
  assert.equal(f.recorder.summary().failed_calls, 2);
  assert.equal(f.recorder.summary().missing_usage_calls, 2);
  assert.equal(estimateUsageCost(f.recorder.records, PEAK).total_cost_usd, null);
  assert.doesNotMatch(readFileSync(f.file, 'utf8'), /private-/);
});

test('simultaneous calls retain independent times, IDs and model usage', async (t) => {
  const f = fixture(t);
  let finishFirst!: (value: unknown) => void;
  const first = new Promise((resolve) => { finishFirst = resolve; });
  const client = await wrap(f.recorder, async (body) => body.model === 'first' ? first : response(USAGE, 'second'));
  const pending = client.chat.completions.create({ model: 'first' });
  assert.equal(f.recorder.summary().pending_calls, 1);
  assert.equal(f.recorder.summary().totals.prompt_tokens, null);
  await client.chat.completions.create({ model: 'second' });
  finishFirst(response(USAGE, 'first'));
  await pending;
  assert.equal(f.recorder.summary().completed_calls, 2);
  assert.equal(f.recorder.summary().totals.prompt_tokens, USAGE.prompt_tokens * 2);
  assert.deepEqual(f.rows().map((record) => record.model), ['first', 'second']);
  assert.equal(new Set(f.rows().map((record) => record.call_id)).size, 2);
  assert.deepEqual(f.entries().map((record) => record.status), ['running', 'running', 'completed', 'completed']);
});

test('a pending request is persisted before invocation and completes under the same call_id', async (t) => {
  const f = fixture(t);
  let finish!: (value: unknown) => void;
  const delayed = new Promise((resolve) => { finish = resolve; });
  const client = await wrap(f.recorder, async () => {
    const [started] = f.entries();
    assert.equal(started.status, 'running');
    assert.equal(started.usage, null);
    assert.equal(started.finished_at, null);
    return delayed;
  });
  const pending = client.chat.completions.create({ model: 'pending-model' });
  assert.equal(f.entries().length, 1);
  assert.equal(f.rows()[0].requested_model, 'pending-model');
  assert.equal(f.recorder.records.length, 1);
  assert.equal(f.recorder.summary().pending_calls, 1);
  assert.equal(f.recorder.summary().totals.prompt_tokens, null);
  finish(response());
  await pending;
  const [started, completed] = f.entries();
  assert.equal(started.call_id, completed.call_id);
  assert.equal(started.started_at, completed.started_at);
  assert.equal(started.status, 'running');
  assert.equal(started.usage, null);
  assert.equal(completed.status, 'completed');
  assert.deepEqual(completed.usage, USAGE);
  assert.equal(f.rows().length, 1);
  assert.equal(f.recorder.records.length, 1);
  assert.equal(f.recorder.summary().calls, 1);
  assert.equal(f.recorder.summary().pending_calls, 0);
});

test('failure to persist request start prevents the actual provider invocation', async (t) => {
  const f = fixture(t);
  unlinkSync(f.file);
  mkdirSync(f.file);
  let requests = 0;
  const client = await wrap(f.recorder, async () => { requests += 1; return response(); });
  await assert.rejects(client.chat.completions.create({ model: 'fixture' }));
  assert.equal(requests, 0);
  assert.equal(f.recorder.records.length, 0);
  assert.equal(f.recorder.summary().calls, 0);
});

test('sample summaries retain startIndex semantics and do not count lifecycle entries twice', async (t) => {
  const f = fixture(t);
  const client = await wrap(f.recorder, async () => response());
  await client.chat.completions.create({ model: 'earlier-sample' });
  const sampleStart = f.recorder.records.length;
  await client.chat.completions.create({ model: 'current-sample' });
  assert.equal(f.entries().length, 4);
  assert.equal(f.rows().length, 2);
  assert.equal(f.recorder.summary().calls, 2);
  assert.equal(f.recorder.summary(sampleStart).calls, 1);
  assert.deepEqual(f.recorder.summary(sampleStart).totals, USAGE);
});

test('streaming paths fail before invoking a provider instead of silently bypassing usage recording', async (t) => {
  const f = fixture(t);
  let requests = 0;
  const client = await wrap(f.recorder, async () => { requests += 1; return response(); });
  assert.throws(() => client.chat.completions.create({ model: 'fixture', stream: true }), /non-stream/);
  assert.throws(() => client.chat.completions.stream({ model: 'fixture' }), /non-stream/);
  assert.equal(requests, 0);
  assert.equal(f.recorder.summary().calls, 0);
});

test('explicit peak and off-peak price snapshots calculate independently from stored provider counts', async (t) => {
  const f = fixture(t);
  const client = await wrap(f.recorder, async () => response());
  await client.chat.completions.create({ model: 'fixture' });
  const peak = estimateUsageCost(f.recorder.records, PEAK);
  const offPeak = estimateUsageCost(f.recorder.records, OFF_PEAK);
  assert.ok(Math.abs(peak.total_cost_usd! - 0.1848) < 1e-12);
  assert.ok(Math.abs(offPeak.total_cost_usd! - 0.0924) < 1e-12);
  assert.equal(peak.priced_calls, 1);
  assert.equal(peak.unpriced_calls, 0);
  assert.deepEqual(peak.rates, PEAK);
  assert.equal(peak.currency, 'USD');
  assert.equal(peak.is_estimate, true);
  assert.notEqual(peak.rates, PEAK);
});

test('one unknown call keeps total cost unknown without erasing a fully measured call', async (t) => {
  const f = fixture(t);
  const client = await wrap(f.recorder, async (body) => response(body.model === 'known' ? USAGE : null));
  await client.chat.completions.create({ model: 'known' });
  await client.chat.completions.create({ model: 'unknown' });
  const cost = estimateUsageCost(f.recorder.records, PEAK);
  assert.equal(cost.priced_calls, 1);
  assert.equal(cost.unpriced_calls, 1);
  assert.equal(cost.total_cost_usd, null);
  assert.equal(cost.calls[1].cost_usd, null);
  assert.ok(cost.calls[0].cost_usd! > 0);
  assert.equal(f.recorder.summary().totals.prompt_tokens, null);
});

test('record snapshots cannot alter recorded counts or subsequent pricing', async (t) => {
  const f = fixture(t);
  const client = await wrap(f.recorder, async () => response());
  await client.chat.completions.create({ model: 'fixture' });
  const snapshot = f.recorder.records;
  snapshot[0].usage!.prompt_tokens = 0;
  assert.equal(f.recorder.records[0].usage!.prompt_tokens, USAGE.prompt_tokens);
  assert.equal(f.rows()[0].usage!.prompt_tokens, USAGE.prompt_tokens);
});

test('an existing usage file is never overwritten and invalid prices fail explicitly', (t) => {
  const f = fixture(t);
  assert.throws(() => new UsageRecorder(f.file), /EEXIST/);
  for (const price of [-1, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.throws(() => estimateUsageCost([], { ...PEAK, output_per_million: price }), /finite nonnegative/);
  }
  assert.equal(estimateUsageCost([], PEAK).total_cost_usd, 0);
});
