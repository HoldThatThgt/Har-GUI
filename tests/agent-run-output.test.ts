import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import { withRunOutput } from '../agent/run-output.js';

/** @brief 每个用例只在同一临时工作区的输出与内部存储目录中操作。 */
async function fixture(t: TestContext) {
  const workspace = await mkdtemp(join(tmpdir(), 'har-gui-run-output-test-'));
  t.after(async () => {
    assert.equal(dirname(workspace), resolve(tmpdir()));
    assert.ok(basename(workspace).startsWith('har-gui-run-output-test-'));
    await rm(workspace, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  return { root: join(workspace, 'output'), storageRoot: join(workspace, 'dataset') };
}

/** @brief 读取明确指定的运行摘要或交付清单，不推断它们保存于同一目录。 */
async function document(directory: string, filename: string) {
  return JSON.parse(await readFile(join(directory, filename), 'utf8'));
}

/** @brief 保留文件原始字节与精确修改时间，用于证明状态改名没有重写内部证据。 */
async function savedFiles(root: string, ignoreSummary = false) {
  const files: Record<string, { bytes: string; modified: string }> = {};
  const pending = [root];
  while (pending.length) {
    const directory = pending.pop()!;
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const filename = join(directory, entry.name);
      if (entry.isDirectory()) pending.push(filename);
      else if (!ignoreSummary || filename !== join(root, 'run.json')) files[filename.slice(root.length)] = {
        bytes: (await readFile(filename)).toString('base64'),
        modified: (await stat(filename, { bigint: true })).mtimeNs.toString(),
      };
    }
  }
  return files;
}

/** @brief 模拟 Python 只把本次选择的文件和相对引用追加到运行清单。 */
async function deliver(directory: string, captureId: string) {
  const bytes = Buffer.from('{"synthetic_accessibility":true}\r\n');
  const file = `${captureId}_tree.json`;
  const sha256 = createHash('sha256').update(bytes).digest('hex');
  await writeFile(join(directory, file), bytes);
  const sample = { capture_id: captureId, source_sha256: 'a'.repeat(64), naming: { capture_id: captureId },
    files: { accessibility: { file, bytes: bytes.length, sha256 } } };
  const manifest = await document(directory, 'manifest.json');
  manifest.samples.push(sample);
  await writeFile(join(directory, 'manifest.json'), JSON.stringify(manifest) + '\n');
  return sample;
}

test('result names change while evidence stays fixed and selected files keep valid relative references', async (t) => {
  const f = await fixture(t);
  let internal = '';
  let evidenceBefore = {};
  let selected: Awaited<ReturnType<typeof deliver>>;
  const result = await withRunOutput(f.root, f.storageRoot, async (directory, delivery) => {
    internal = directory;
    assert.equal(dirname(directory), join(f.storageRoot, 'runs'));
    assert.equal(delivery, join(f.root, '001-running'));
    assert.deepEqual(await document(delivery, 'manifest.json'), { status: 'running', run_index: '001', samples: [] });
    assert.deepEqual(await document(directory, 'run.json'), { status: 'running', run_index: '001',
      artifacts_directory: directory });
    assert.deepEqual(await readdir(directory), ['run.json']);
    const capture = join(directory, 'captures', 'saved');
    await mkdir(capture, { recursive: true });
    await writeFile(join(capture, 'metadata.json'), '{"captured":true}\r\n');
    await writeFile(join(directory, 'project.toml'), 'schema_version = 1\r\n');
    await writeFile(join(directory, 'configuration.json'), '{"synthetic":true}\n');
    await writeFile(join(directory, 'events.jsonl'), '{"event":"captured"}\n');
    await writeFile(join(directory, 'run.json'), JSON.stringify({ status: 'complete', actions: 1,
      candidate: { path: capture }, accepted: [{ sample_id: 'saved', path: capture }], new_samples: 1 }));
    selected = await deliver(delivery, 'saved');
    evidenceBefore = await savedFiles(directory, true);
  });
  assert.equal(result, join(f.root, '001-success'));
  assert.deepEqual((await readdir(result)).sort(), ['manifest.json', 'saved_tree.json']);
  assert.deepEqual(await document(result, 'manifest.json'), {
    status: 'complete', run_index: '001', samples: [selected!],
  });
  const complete = await document(internal, 'run.json');
  const capture = join(internal, 'captures', 'saved');
  assert.deepEqual(complete, { status: 'complete', actions: 1, candidate: { path: capture },
    accepted: [{ sample_id: 'saved', path: capture }], new_samples: 1,
    run_index: '001', artifacts_directory: internal });
  assert.equal(await readFile(join(complete.candidate.path, 'metadata.json'), 'utf8'), '{"captured":true}\r\n');
  assert.deepEqual(await savedFiles(internal, true), evidenceBefore);
  assert.equal(createHash('sha256').update(await readFile(join(result, selected!.files.accessibility.file)))
    .digest('hex'), selected!.files.accessibility.sha256);
  assert.deepEqual(await readdir(f.root), ['001-success']);
  assert.deepEqual(await readdir(join(f.storageRoot, 'runs')), [basename(internal)]);
});

test('a failed run preserves its error, partial delivery and committed count before the next success', async (t) => {
  const f = await fixture(t);
  const failure = new Error('Fixture publication failed after one committed sample');
  let internal = '';
  await assert.rejects(withRunOutput(f.root, f.storageRoot, async (directory, delivery) => {
    internal = directory;
    await writeFile(join(directory, 'raw.json'), '{"captured":true}');
    await writeFile(join(directory, 'run.json'), JSON.stringify({ status: 'failed', actions: 2,
      accepted: [{ sample_id: 'committed' }], new_samples: 1, candidate: { path: join(directory, 'raw.json') } }));
    await deliver(delivery, 'committed');
    throw failure;
  }), (error) => error === failure);
  const failed = join(f.root, '001-failed');
  const manifest = await document(failed, 'manifest.json');
  assert.equal(manifest.status, 'failed');
  assert.equal(manifest.error, String(failure));
  assert.equal(manifest.samples[0].capture_id, 'committed');
  assert.deepEqual((await readdir(failed)).sort(), ['committed_tree.json', 'manifest.json']);
  const record = await document(internal, 'run.json');
  assert.equal(record.run_index, '001');
  assert.equal(record.artifacts_directory, internal);
  assert.equal(record.new_samples, 1);
  assert.equal(record.accepted[0].sample_id, 'committed');
  assert.equal(await readFile(record.candidate.path, 'utf8'), '{"captured":true}');
  const fixedFiles = await savedFiles(internal);
  const failedFiles = await savedFiles(failed);
  assert.equal(await withRunOutput(f.root, f.storageRoot, async () => {}), join(f.root, '002-success'));
  assert.deepEqual(await savedFiles(internal), fixedFiles);
  assert.deepEqual(await savedFiles(failed), failedFiles);
  assert.deepEqual((await readdir(f.root)).sort(), ['001-failed', '002-success']);
});

test('cleanup failure marks both run records failed and retains committed counts and evidence', async (t) => {
  const f = await fixture(t);
  const cleanup = new Error('Fixture device-lock cleanup failed');
  let internal = '';
  let evidenceBefore = {};
  await assert.rejects(withRunOutput(f.root, f.storageRoot, async (directory, delivery) => {
    internal = directory;
    try {
      await writeFile(join(directory, 'run.json'), JSON.stringify({ status: 'complete', new_samples: 2,
        accepted: [{ sample_id: 'committed-a' }, { sample_id: 'committed-b' }], stop_reason: 'run_sample_limit' }));
      await deliver(delivery, 'committed-a');
      await deliver(delivery, 'committed-b');
      evidenceBefore = await savedFiles(directory, true);
    } finally {
      throw cleanup;
    }
  }), (error) => error === cleanup);
  const failed = join(f.root, '001-failed');
  const manifest = await document(failed, 'manifest.json');
  assert.equal(manifest.status, 'failed');
  assert.equal(manifest.error, String(cleanup));
  assert.deepEqual(manifest.samples.map((sample: any) => sample.capture_id), ['committed-a', 'committed-b']);
  const record = await document(internal, 'run.json');
  assert.deepEqual(record, { status: 'failed', error: String(cleanup), new_samples: 2,
    accepted: [{ sample_id: 'committed-a' }, { sample_id: 'committed-b' }], stop_reason: 'run_sample_limit',
    run_index: '001', artifacts_directory: internal });
  assert.deepEqual(await savedFiles(internal, true), evidenceBefore);
  assert.deepEqual((await readdir(failed)).sort(), ['committed-a_tree.json', 'committed-b_tree.json', 'manifest.json']);
  assert.deepEqual(await readdir(f.root), ['001-failed']);
});

test('concurrent runs reserve distinct increasing numbers with separate evidence and delivery results', async (t) => {
  const f = await fixture(t);
  const entered: Array<{ internal: string; index: string; operation: number }> = [];
  const results = await Promise.allSettled(Array.from({ length: 8 }, (_, operation) =>
    withRunOutput(f.root, f.storageRoot, async (internal, delivery) => {
      const manifest = await document(delivery, 'manifest.json');
      entered.push({ internal, index: manifest.run_index, operation });
      await new Promise<void>((done) => setImmediate(done));
      await writeFile(join(internal, 'run.json'), JSON.stringify({ status: 'complete', operation }));
      await deliver(delivery, `operation-${operation}`);
      if (operation % 2) throw new Error(`Fixture failure ${operation}`);
    })));
  assert.equal(new Set(entered.map((entry) => entry.internal)).size, 8);
  assert.equal(new Set(entered.map((entry) => entry.index)).size, 8);
  assert.equal(results.filter((result) => result.status === 'fulfilled').length, 4);
  assert.equal(results.filter((result) => result.status === 'rejected').length, 4);
  assert.deepEqual(entered.map((entry) => entry.index).sort(),
    Array.from({ length: 8 }, (_, index) => String(index + 1).padStart(3, '0')));
  assert.equal((await readdir(f.root)).some((name) => name.endsWith('-running') || name.startsWith('.')), false);
  assert.equal((await readdir(join(f.storageRoot, 'runs'))).length, 8);
  for (const entry of entered) {
    const outcome = entry.operation % 2 ? 'failed' : 'success';
    const result = join(f.root, `${entry.index}-${outcome}`);
    const manifest = await document(result, 'manifest.json');
    assert.equal(manifest.status, entry.operation % 2 ? 'failed' : 'complete');
    assert.equal(manifest.samples[0].capture_id, `operation-${entry.operation}`);
    assert.deepEqual((await readdir(result)).sort(), ['manifest.json', `operation-${entry.operation}_tree.json`]);
    assert.deepEqual(await document(entry.internal, 'run.json'), {
      status: entry.operation % 2 ? 'failed' : 'complete', operation: entry.operation,
      ...(entry.operation % 2 ? { error: `Error: Fixture failure ${entry.operation}` } : {}),
      run_index: entry.index, artifacts_directory: entry.internal });
  }
});

test('a callback cannot turn its failed detailed summary into a successful delivery', async (t) => {
  const f = await fixture(t);
  let internal = '';
  await assert.rejects(withRunOutput(f.root, f.storageRoot, async (directory) => {
    internal = directory;
    await writeFile(join(directory, 'run.json'), JSON.stringify({ status: 'failed', new_samples: 1,
      accepted: [{ sample_id: 'already-committed' }] }));
  }), /Execution returned with a failed internal run summary/);
  const record = await document(internal, 'run.json');
  const manifest = await document(join(f.root, '001-failed'), 'manifest.json');
  assert.equal(record.status, 'failed');
  assert.equal(record.new_samples, 1);
  assert.equal(record.accepted[0].sample_id, 'already-committed');
  assert.match(record.error, /failed internal run summary/);
  assert.equal(manifest.status, 'failed');
  assert.equal(manifest.error, record.error);
  assert.deepEqual(await readdir(f.root), ['001-failed']);
});

test('old output and unfinished numbers remain unchanged while new runs continue above them', async (t) => {
  const f = await fixture(t);
  const legacy = join(f.root, '2026-10-22T11-10-57-580Z-old-run');
  const unfinished = join(f.root, '.006');
  const failed = join(f.root, '009-failed');
  for (const directory of [legacy, unfinished, failed]) await mkdir(directory, { recursive: true });
  await writeFile(join(legacy, 'configuration.json'), '{"historical":"unchanged"}\r\n');
  await writeFile(join(unfinished, 'raw.bin'), Buffer.from([0, 1, 2, 255]));
  await writeFile(join(failed, 'run.json'), '{"status":"failed","preserved":true}\n');
  await writeFile(join(f.root, 'unrelated-999.txt'), 'unrelated output');
  const before = { legacy: await savedFiles(legacy), unfinished: await savedFiles(unfinished),
    failed: await savedFiles(failed) };
  assert.equal(await withRunOutput(f.root, f.storageRoot, async () => {}), join(f.root, '010-success'));
  assert.deepEqual(await savedFiles(legacy), before.legacy);
  assert.deepEqual(await savedFiles(unfinished), before.unfinished);
  assert.deepEqual(await savedFiles(failed), before.failed);
  assert.equal(await readFile(join(f.root, 'unrelated-999.txt'), 'utf8'), 'unrelated output');
});

test('numbering belongs to the output root and grows past three digits', async (t) => {
  const f = await fixture(t);
  const other = await fixture(t);
  await mkdir(join(f.root, '.999'), { recursive: true });
  assert.equal(await withRunOutput(f.root, f.storageRoot, async () => {}), join(f.root, '1000-success'));
  assert.equal(await withRunOutput(other.root, other.storageRoot, async () => {}), join(other.root, '001-success'));
});
