import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, unlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { test, type TestContext } from 'node:test';
import { acquireDeviceLock } from '../agent/runner.js';
import { RunState } from '../agent/state.js';
import type { LoadedConfiguration } from '../agent/config.js';

async function directory(t: TestContext) {
  const parent = path.resolve(os.tmpdir());
  const result = await mkdtemp(path.join(parent, 'har-gui-resume-test-'));
  t.after(async () => {
    assert.equal(path.dirname(result), parent);
    assert.ok(path.basename(result).startsWith('har-gui-resume-test-'));
    await rm(result, { recursive: true, force: true });
  });
  return result;
}

test('device lock refuses an active process and preserves its record', async (t) => {
  const root = await directory(t);
  const filename = path.join(root, 'device.lock');
  const content = JSON.stringify({ pid: process.pid, directory: root });
  await writeFile(filename, content);
  await assert.rejects(acquireDeviceLock(filename, root), /Device is in use/);
  assert.equal(await readFile(filename, 'utf8'), content);
});

test('device lock only recovers after confirming its recorded process exited', async (t) => {
  const root = await directory(t);
  const filename = path.join(root, 'device.lock');
  await writeFile(filename, JSON.stringify({ pid: 123456789, directory: 'old-run' }));
  t.mock.method(process, 'kill', (pid: number, signal: number) => {
    assert.equal(pid, 123456789);
    assert.equal(signal, 0);
    throw Object.assign(new Error('No such process'), { code: 'ESRCH' });
  });
  const handle = await acquireDeviceLock(filename, root);
  await handle.close();
  assert.equal(JSON.parse(await readFile(filename, 'utf8')).pid, process.pid);
  await unlink(filename);
});

test('device lock does not delete a process it cannot inspect', async (t) => {
  const root = await directory(t);
  const filename = path.join(root, 'device.lock');
  const content = JSON.stringify({ pid: 123456789 });
  await writeFile(filename, content);
  t.mock.method(process, 'kill', () => { throw Object.assign(new Error('Permission denied'), { code: 'EPERM' }); });
  await assert.rejects(acquireDeviceLock(filename, root), /Permission denied/);
  assert.equal(await readFile(filename, 'utf8'), content);
});

test('device lock refuses an invalid process identity', async (t) => {
  const root = await directory(t);
  const filename = path.join(root, 'device.lock');
  await writeFile(filename, JSON.stringify({ pid: -1 }));
  await assert.rejects(acquireDeviceLock(filename, root), /invalid process ID/);
});

test('restored state counts only published samples without restoring old device coordinates', async (t) => {
  const root = await directory(t);
  const config = { digests: { project: 'a'.repeat(64), config: 'b'.repeat(64) }, project: {
    run: { max_duration_seconds: 100 }, tasks: [{ name: 'editor', target_count: 3 }, { name: 'browser', target_count: 2 }],
  } } as LoadedConfiguration;
  const state = new RunState(config, root);
  state.accepted = [{ task_name: 'editor', sample_id: 'saved' }];
  assert.equal(state.acceptedCount('editor'), 1);
  assert.equal(state.acceptedCount('browser'), 0);
  assert.equal(state.observations.size, 0);
  assert.equal(state.candidate, undefined);
  assert.equal(state.revision, 0);
  assert.deepEqual(state.datasetIdentity, { project_sha256: 'a'.repeat(64), config_sha256: 'b'.repeat(64),
    tasks: [{ name: 'editor', target_count: 3 }, { name: 'browser', target_count: 2 }] });
});
