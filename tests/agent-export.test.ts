import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { syncBuiltinESMExports } from 'node:module';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import type { LoadedConfiguration } from '../agent/config.js';
import { exportSample } from '../agent/export.js';
import { writeFixtureConfiguration } from './agent-configuration.js';

const KEY_NAME = 'UNUSED_EXPORT_FIXTURE_KEY';

// 只有 Python 标准输入协议由匿名 Node 替身返回；导出入口、目录闭合和运行记录均走真实代码。
const BRIDGE = `
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');let input='';
process.stdin.on('data',part=>input+=part).on('end',()=>{
  const filename=process.argv[1],plan=JSON.parse(fs.readFileSync(filename,'utf8'));
  const request=JSON.parse(input);plan.requests.push(request);let result,error;
  if(request.operation!=='export_sample') error={message:'Unexpected fixture operation'};
  else if(plan.mode==='failure') error={message:'Fixture export source is unavailable'};
  else {
    const files={};
    for(const artifact of request.delivery.artifacts){
      const file=request.sample_id+'/'+artifact.path;
      const bytes=Buffer.from(JSON.stringify({synthetic:true,source:artifact.source})+'\\n');
      const target=path.join(request.destination,file);fs.mkdirSync(path.dirname(target),{recursive:true});
      fs.writeFileSync(target,bytes);
      files[artifact.source]={file,bytes:bytes.length,sha256:crypto.createHash('sha256').update(bytes).digest('hex')};
    }
    const exported={capture_id:request.sample_id,files,status:'complete'};
    const manifestPath=path.join(request.destination,'manifest.json');
    const manifest=JSON.parse(fs.readFileSync(manifestPath,'utf8'));
    manifest.samples.push({capture_id:request.sample_id,source_sha256:'a'.repeat(64),files});
    fs.writeFileSync(manifestPath,JSON.stringify(manifest)+'\\n');
    fs.writeFileSync(request.receipt,JSON.stringify(exported)+'\\n');
    result={sample:plan.sample,export:exported,new_samples:plan.mode==='invalid_count'?1:0};
  }
  fs.writeFileSync(filename,JSON.stringify(plan));
  process.stdout.write(JSON.stringify(error?{error}:{result}));
});`;

/** @brief 建立真实原始配置，模型只保存不存在的凭据环境变量名。 */
function configuration(directory: string): LoadedConfiguration {
  return { projectPath: join(directory, 'project.toml'), configPath: join(directory, 'config.toml'),
    outputPath: join(directory, 'output'), datasetPath: join(directory, 'dataset'),
    digests: { project: '', config: '' },
    profile: { provider: 'openai_compatible', base_url: 'https://unused.fixture.invalid/v1', model: 'unused-model',
      model_family: 'deepseek', api_key_env: KEY_NAME, supports_vision: true },
    project: { schema_version: 1,
      run: { name: 'export-fixture', output: 'output', max_actions: 10, max_duration_seconds: 60 },
      agent: { llm: 'vision' }, dataset: { root: 'dataset', canvas: { width: 4, height: 3 }, stability_regions: [] },
      delivery: { directory: '{capture_id}', artifacts: [
        { source: 'windows', path: 'windows.json' }, { source: 'capture_metadata', path: 'metadata.json' },
      ] }, tools: { enabled: ['Capture'] }, tasks: [{ name: 'fixture', app: 'Fixture', scene: 'Page',
        instruction: 'Existing accepted sample', acceptance: 'Already accepted', target_count: 20,
        privacy: { enabled: false, reason: 'Synthetic fixture' } }] } };
}

/** @brief 保存原始配置、匿名登记计数与已有证据的字节和精确修改时间。 */
async function savedFiles(paths: string[]) {
  return Object.fromEntries(await Promise.all(paths.map(async (path) => [path, {
    bytes: (await readFile(path)).toString('base64'), modified: (await stat(path, { bigint: true })).mtimeNs.toString(),
  }])));
}

/** @brief 所有进程调用只允许无设备参数的本地导出桥接，并禁止网络、凭据和环境文件读取。 */
function intercept(t: TestContext, planPath: string) {
  const originalSpawn = childProcess.spawn;
  const originalEnv = process.env;
  const environment = { ...originalEnv };
  delete environment[KEY_NAME];
  const metrics = { credentialReads: 0 };
  process.env = new Proxy(environment, { get(target, key) {
    if (key === KEY_NAME) { metrics.credentialReads += 1; throw new Error('Unexpected model credential read'); }
    return Reflect.get(target, key);
  } });
  const spawn = t.mock.method(childProcess, 'spawn', (executable: any, args: any, options: any) => {
    assert.equal(executable, 'fixture-python');
    assert.equal(args.length, 1, 'Offline export must not pass HDC or device arguments');
    assert.equal(basename(args[0]), 'agent_device.py');
    return originalSpawn(process.execPath, ['-e', BRIDGE, planPath], options);
  });
  syncBuiltinESMExports();
  t.after(() => { spawn.mock.restore(); syncBuiltinESMExports(); process.env = originalEnv; });
  const envFile = t.mock.method(process, 'loadEnvFile', () => { throw new Error('Unexpected environment file'); });
  const fetch = t.mock.method(globalThis, 'fetch', async () => {
    throw new Error('Unexpected model or network call');
  });
  return { metrics, spawn, envFile, fetch };
}

/** @brief 临时目录内的旧登记只是协议替身，不代表真实 SQLite 校验或设备采集。 */
async function fixture(t: TestContext, mode = 'success') {
  const directory = await mkdtemp(join(tmpdir(), 'har-gui-export-test-'));
  t.after(async () => {
    assert.equal(dirname(directory), resolve(tmpdir()));
    assert.ok(basename(directory).startsWith('har-gui-export-test-'));
    await rm(directory, { recursive: true, force: true });
  });
  t.mock.method(console, 'error', () => {});
  const config = configuration(directory);
  writeFixtureConfiguration(config);
  const source = join(config.datasetPath, 'existing', 'capture');
  await mkdir(source, { recursive: true });
  const registry = join(config.datasetPath, 'existing', 'registration.json');
  const metadata = join(source, 'metadata.json');
  const historicalProject = join(directory, 'historical-project.toml');
  await writeFile(registry, '{"synthetic_saved_count":17}\r\n');
  await writeFile(metadata, '{"synthetic_existing_capture":true}\r\n');
  await writeFile(historicalProject, '# Anonymous historical project source\r\n');
  const sample = { sample_id: 'accepted-frame', task_name: 'fixture', path: source };
  const planPath = join(directory, 'bridge-plan.json');
  await writeFile(planPath, JSON.stringify({ mode, sample, requests: [] }));
  const paths = [registry, metadata, historicalProject, config.projectPath, config.configPath];
  const before = await savedFiles(paths);
  const traps = intercept(t, planPath);
  return { directory, config, planPath, sample, historicalProject, paths, before, ...traps };
}

/** @brief 读取实际保存的 JSON 记录。 */
async function document(path: string) { return JSON.parse(await readFile(path, 'utf8')); }

/** @brief 找到本用例唯一的内部运行目录，不从用户结果目录读取运行摘要。 */
async function internalDirectory(config: LoadedConfiguration): Promise<string> {
  const root = join(config.datasetPath, 'runs');
  const names = await readdir(root);
  assert.equal(names.length, 1);
  return join(root, names[0]);
}

test('offline export saves declared files without credentials, environment loading or new counts', async (t) => {
  const f = await fixture(t);
  const result = await exportSample(f.config, 'fixture-python', { sampleId: f.sample.sample_id,
    projectSources: [f.historicalProject] });
  assert.equal(result, join(f.config.outputPath, '001-success'));
  const plan = await document(f.planPath);
  assert.equal(plan.requests.length, 1);
  const request = plan.requests[0];
  assert.equal(request.operation, 'export_sample');
  assert.equal(request.root, f.config.datasetPath);
  assert.equal(request.sample_id, f.sample.sample_id);
  assert.deepEqual(request.delivery, f.config.project.delivery);
  assert.deepEqual(request.identity.project_sources, [f.historicalProject]);
  assert.equal(request.destination, join(f.config.outputPath, '001-running'));
  const internal = await internalDirectory(f.config);
  assert.equal(request.receipt, join(internal, 'exports', `${f.sample.sample_id}.json`));
  const manifest = await document(join(result, 'manifest.json'));
  assert.equal(manifest.status, 'complete');
  assert.equal(manifest.samples.length, 1);
  assert.deepEqual(Object.keys(manifest.samples[0].files).sort(), ['capture_metadata', 'windows']);
  for (const record of Object.values(manifest.samples[0].files) as any[]) {
    assert.equal(record.file.startsWith('accepted-frame/'), true);
    const bytes = await readFile(join(result, record.file));
    assert.equal(bytes.length, record.bytes);
    assert.equal(createHash('sha256').update(bytes).digest('hex'), record.sha256);
  }
  assert.deepEqual((await readdir(result)).sort(), ['accepted-frame', 'manifest.json']);
  assert.deepEqual((await readdir(join(result, 'accepted-frame'))).sort(), ['metadata.json', 'windows.json']);
  const summary = await document(join(internal, 'run.json'));
  assert.equal(summary.status, 'complete');
  assert.equal(summary.operation, 'export');
  assert.equal(summary.new_samples, 0);
  assert.deepEqual(summary.accepted, [f.sample]);
  assert.deepEqual(await savedFiles(f.paths), f.before);
  assert.equal(f.metrics.credentialReads, 0);
  assert.equal(f.spawn.mock.callCount(), 1);
  assert.equal(f.envFile.mock.callCount(), 0);
  assert.equal(f.fetch.mock.callCount(), 0);
});

test('a missing export source creates a failed result and preserves the original count and evidence', async (t) => {
  const f = await fixture(t, 'failure');
  await assert.rejects(exportSample(f.config, 'fixture-python', { sampleId: f.sample.sample_id }),
    /Fixture export source is unavailable/);
  const result = join(f.config.outputPath, '001-failed');
  const manifest = await document(join(result, 'manifest.json'));
  assert.equal(manifest.status, 'failed');
  assert.deepEqual(manifest.samples, []);
  assert.deepEqual(await readdir(result), ['manifest.json']);
  const summary = await document(join(await internalDirectory(f.config), 'run.json'));
  assert.equal(summary.status, 'failed');
  assert.equal(summary.new_samples, 0);
  assert.deepEqual(summary.accepted, []);
  assert.deepEqual(await savedFiles(f.paths), f.before);
  assert.equal((await document(f.planPath)).requests.length, 1);
  assert.equal(f.fetch.mock.callCount(), 0);
});

test('offline export rejects a bridge result that reports new collected samples', async (t) => {
  const f = await fixture(t, 'invalid_count');
  await assert.rejects(exportSample(f.config, 'fixture-python', { sampleId: f.sample.sample_id }),
    /inconsistent sample facts/);
  const summary = await document(join(await internalDirectory(f.config), 'run.json'));
  assert.equal(summary.status, 'failed');
  assert.equal(summary.new_samples, 0);
  assert.deepEqual(summary.accepted, []);
  assert.equal((await document(join(f.config.outputPath, '001-failed', 'manifest.json'))).status, 'failed');
  assert.deepEqual(await savedFiles(f.paths), f.before);
});
