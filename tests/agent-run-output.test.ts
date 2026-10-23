import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import { withRunOutput } from '../agent/run-output.js';

/** @brief 每个用例只在明确归属的临时输出根工作。 */
async function outputRoot(t: TestContext): Promise<string> {
  const directory = await mkdtemp(join(tmpdir(), 'har-gui-run-output-test-'));
  t.after(async () => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    assert.ok(basename(directory).startsWith('har-gui-run-output-test-'));
    await rm(directory, { recursive: true, force: true });
  });
  return directory;
}

/** @brief 读取结果或固定证据目录中的完整运行摘要。 */
async function summary(directory: string) {
  return JSON.parse(await readFile(join(directory, 'run.json'), 'utf8'));
}

/** @brief 保留旧目录内每个文件的原始字节与精确修改时间。 */
async function savedFiles(root: string) {
  const files: Record<string, { bytes: string; modified: string }> = {};
  const pending = [root];
  while (pending.length) {
    const directory = pending.pop()!;
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const filename = join(directory, entry.name);
      if (entry.isDirectory()) pending.push(filename);
      else files[filename.slice(root.length)] = {
        bytes: (await readFile(filename)).toString('base64'),
        modified: (await stat(filename, { bigint: true })).mtimeNs.toString(),
      };
    }
  }
  return files;
}

test('result names change while captured files and full summary keep their fixed paths', async (t) => {
  const root = await outputRoot(t);
  const result = await withRunOutput(root, async (directory) => {
    assert.equal(directory, join(root, '.001'));
    const running = await summary(join(root, '001-running'));
    assert.equal(running.status, 'running');
    assert.equal(running.run_index, '001');
    assert.equal(running.artifacts_directory, directory);
    assert.deepEqual(await summary(directory), running);
    await mkdir(join(directory, 'captures'));
    const candidate = { path: join(directory, 'captures', 'raw.json') };
    await writeFile(candidate.path, '{"captured":true}\r\n');
    await writeFile(join(directory, 'events.jsonl'), '{"event":"captured"}\n');
    await writeFile(join(directory, 'run.json'), JSON.stringify({ status: 'complete', actions: 1,
      candidate, accepted: [{ sample_id: 'saved', path: candidate.path }], max_new_samples: 1 }));
  });
  assert.equal(result, join(root, '001-success'));
  assert.deepEqual(await readdir(result), ['run.json']);
  const complete = await summary(result);
  assert.equal(complete.run_index, '001');
  assert.equal(complete.artifacts_directory, join(root, '.001'));
  assert.equal(complete.actions, 1);
  assert.equal(complete.max_new_samples, 1);
  assert.equal(complete.accepted[0].path, complete.candidate.path);
  assert.equal(await readFile(complete.candidate.path, 'utf8'), '{"captured":true}\r\n');
  assert.deepEqual(await readFile(join(result, 'run.json')), await readFile(join(root, '.001', 'run.json')));
  assert.deepEqual((await readdir(root)).sort(), ['.001', '001-success']);
});

test('a failed run preserves its real error and partial result before the next success', async (t) => {
  const root = await outputRoot(t);
  const failure = new Error('Fixture publication failed after one committed sample');
  await assert.rejects(withRunOutput(root, async (directory) => {
    await writeFile(join(directory, 'raw.json'), '{"captured":true}');
    await writeFile(join(directory, 'run.json'), JSON.stringify({ status: 'failed', actions: 2,
      accepted: [{ sample_id: 'committed' }], new_samples: 1, candidate: { path: join(directory, 'raw.json') } }));
    throw failure;
  }), (error) => error === failure);
  const failed = join(root, '001-failed');
  const record = await summary(failed);
  assert.equal(record.status, 'failed');
  assert.match(record.error, /Fixture publication failed after one committed sample/);
  assert.equal(record.new_samples, 1);
  assert.equal(record.accepted[0].sample_id, 'committed');
  assert.equal(record.artifacts_directory, join(root, '.001'));
  assert.deepEqual(await readdir(failed), ['run.json']);
  const fixedFiles = await savedFiles(record.artifacts_directory);
  const failedFiles = await savedFiles(failed);
  const success = await withRunOutput(root, async () => {});
  assert.equal(success, join(root, '002-success'));
  assert.deepEqual(await savedFiles(record.artifacts_directory), fixedFiles);
  assert.deepEqual(await savedFiles(failed), failedFiles);
  assert.equal((await readdir(root)).some((name) => name.endsWith('-running')), false);
});

test('cleanup failure after a complete body keeps committed counts and labels the entire run failed', async (t) => {
  const root = await outputRoot(t);
  const cleanup = new Error('Fixture device-lock cleanup failed');
  await assert.rejects(withRunOutput(root, async (directory) => {
    try {
      await writeFile(join(directory, 'run.json'), JSON.stringify({ status: 'complete', new_samples: 2,
        accepted: [{ sample_id: 'committed-a' }, { sample_id: 'committed-b' }], stop_reason: 'run_sample_limit' }));
    } finally {
      throw cleanup;
    }
  }), (error) => error === cleanup);
  const failed = join(root, '001-failed');
  const record = await summary(failed);
  assert.equal(record.status, 'failed');
  assert.equal(record.error, String(cleanup));
  assert.equal(record.new_samples, 2);
  assert.deepEqual(record.accepted.map((sample: any) => sample.sample_id), ['committed-a', 'committed-b']);
  assert.equal(record.artifacts_directory, join(root, '.001'));
  assert.deepEqual(await summary(record.artifacts_directory), record);
  assert.deepEqual(await readdir(failed), ['run.json']);
  assert.deepEqual((await readdir(root)).sort(), ['.001', '001-failed']);
});

test('concurrent runs reserve distinct increasing numbers with independent success and failure results', async (t) => {
  const root = await outputRoot(t);
  const entered: string[] = [];
  const results = await Promise.allSettled(Array.from({ length: 8 }, (_, operation) => withRunOutput(root,
    async (directory) => {
      entered.push(directory);
      await new Promise<void>((done) => setImmediate(done));
      await writeFile(join(directory, 'run.json'), JSON.stringify({ status: 'complete', operation }));
      if (operation % 2) throw new Error(`Fixture failure ${operation}`);
    })));
  assert.equal(new Set(entered).size, 8);
  assert.equal(results.filter((result) => result.status === 'fulfilled').length, 4);
  assert.equal(results.filter((result) => result.status === 'rejected').length, 4);
  const names = await readdir(root);
  assert.deepEqual(names.filter((name) => /^\.\d+$/.test(name)).sort(),
    Array.from({ length: 8 }, (_, index) => `.${String(index + 1).padStart(3, '0')}`));
  assert.equal(names.some((name) => name.endsWith('-running')), false);
  for (const directory of entered) {
    const record = await summary(directory);
    const outcome = record.operation % 2 ? 'failed' : 'success';
    const result = join(root, `${record.run_index}-${outcome}`);
    assert.equal(record.artifacts_directory, directory);
    assert.deepEqual(await summary(result), record);
    assert.deepEqual(await readdir(result), ['run.json']);
  }
});

test('old output and unfinished numbers remain unchanged while new runs continue above them', async (t) => {
  const root = await outputRoot(t);
  const legacy = join(root, '2026-10-22T11-10-57-580Z-old-run');
  const unfinished = join(root, '.006');
  const failed = join(root, '009-failed');
  for (const directory of [legacy, unfinished, failed]) await mkdir(directory);
  await writeFile(join(legacy, 'configuration.json'), '{"historical":"unchanged"}\r\n');
  await writeFile(join(unfinished, 'raw.bin'), Buffer.from([0, 1, 2, 255]));
  await writeFile(join(failed, 'run.json'), '{"status":"failed","preserved":true}\n');
  await writeFile(join(root, 'unrelated-999.txt'), 'unrelated output');
  const before = { legacy: await savedFiles(legacy), unfinished: await savedFiles(unfinished),
    failed: await savedFiles(failed) };
  const result = await withRunOutput(root, async () => {});
  assert.equal(result, join(root, '010-success'));
  assert.deepEqual(await savedFiles(legacy), before.legacy);
  assert.deepEqual(await savedFiles(unfinished), before.unfinished);
  assert.deepEqual(await savedFiles(failed), before.failed);
  assert.equal(await readFile(join(root, 'unrelated-999.txt'), 'utf8'), 'unrelated output');
});

test('numbering belongs to the output root and grows past three digits', async (t) => {
  const root = await outputRoot(t);
  const otherRoot = await outputRoot(t);
  await mkdir(join(root, '.999'));
  assert.equal(await withRunOutput(root, async () => {}), join(root, '1000-success'));
  assert.equal(await withRunOutput(otherRoot, async () => {}), join(otherRoot, '001-success'));
});
