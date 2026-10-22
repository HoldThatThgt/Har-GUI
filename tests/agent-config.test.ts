import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import { loadConfiguration, resolveModelConfig } from '../agent/config.js';

const KEY_NAME = 'HAR_GUI_CONFIG_TEST_KEY';
const TOOLS = ['Observe', 'Capture'];
const TASK = `
[[tasks]]
name = "calendar-week"
app = "Calendar"
scene = "Week"
privacy = { enabled = false, reason = "Synthetic fixture" }
instruction = "寻找日历周视图并改变窗口大小"
acceptance = "显示可读的日期和活动"
target_count = 3
bundle_names = ["org.example.calendar"]
`;
const PROJECT = `schema_version = 1
[run]
name = "general-collection"
output = "./results"
max_actions = 12
max_duration_seconds = 3.5
[agent]
llm = "chosen"
[dataset]
root = "./dataset"
canvas = { width = 3120, height = 2080 }
[tools]
enabled = ["Observe", "Capture"]
${TASK}`;
const CONFIG = `schema_version = 1
[llm.chosen]
provider = "openai_compatible"
base_url = "https://model.example.test/v1"
model = "deployment-specific-model"
model_family = "deepseek"
api_key_env = "${KEY_NAME}"
supports_vision = true
`;
const PRICING = `
[llm.chosen.pricing]
currency = "USD"
input_cache_hit_per_million = 0.006
input_cache_miss_per_million = 0.3
output_per_million = 1.2
`;

function fixture(t: TestContext, project = PROJECT, config = CONFIG) {
  const tempRoot = resolve(tmpdir());
  const root = mkdtempSync(join(tempRoot, 'har-gui-config-test-'));
  t.after(() => {
    assert.equal(dirname(root), tempRoot);
    assert.ok(basename(root).startsWith('har-gui-config-test-'));
    rmSync(root, { recursive: true, force: true });
  });
  mkdirSync(join(root, 'project'));
  mkdirSync(join(root, 'deployment'));
  const projectPath = join(root, 'project', 'project.toml');
  const configPath = join(root, 'deployment', 'config.toml');
  writeFileSync(projectPath, project, 'utf8');
  writeFileSync(configPath, config, 'utf8');
  return { root, projectPath, configPath };
}

test('loads generic tasks without reading credentials and preserves original file digests', (t) => {
  const f = fixture(t, PROJECT.replaceAll('\n', '\r\n'));
  const originalEnv = process.env;
  process.env = new Proxy(originalEnv, {
    get(target, property) {
      if (property === KEY_NAME) throw new Error('Static configuration read a credential');
      return Reflect.get(target, property);
    },
  });
  t.after(() => { process.env = originalEnv; });
  const loaded = loadConfiguration(f.projectPath, f.configPath, TOOLS);
  assert.equal(loaded.outputPath, join(f.root, 'project', 'results'));
  assert.equal(loaded.projectPath, f.projectPath);
  assert.equal(loaded.configPath, f.configPath);
  assert.equal(loaded.project.tasks[0].name, 'calendar-week');
  assert.equal(loaded.project.tasks[0].instruction, '寻找日历周视图并改变窗口大小');
  assert.equal(loaded.profile.model, 'deployment-specific-model');
  assert.equal(loaded.profile.pricing, undefined);
  assert.deepEqual(loaded.project.dataset.stability_regions, []);
  for (const [name, path] of [['project', f.projectPath], ['config', f.configPath]] as const) {
    assert.equal(loaded.digests[name], createHash('sha256').update(readFileSync(path)).digest('hex'));
  }
  assert.ok(Object.isFrozen(loaded.project.tasks[0]));
  assert.ok(Object.isFrozen(loaded.project.tools.enabled));
  assert.throws(() => { loaded.project.tasks[0].name = 'changed'; }, TypeError);
});

test('explicit stability selectors are frozen in the project without device-specific defaults', (t) => {
  const selectors = 'stability_regions = [{ bundle_name = "system.fixture", resource_id = "clock" }]\n';
  const f = fixture(t, PROJECT.replace('[tools]', selectors + '[tools]'));
  const config = loadConfiguration(f.projectPath, f.configPath, TOOLS);
  assert.deepEqual(config.project.dataset.stability_regions, [{ bundle_name: 'system.fixture', resource_id: 'clock' }]);
  assert.ok(Object.isFrozen(config.project.dataset.stability_regions));
  assert.ok(Object.isFrozen(config.project.dataset.stability_regions[0]));
});

test('privacy requires an explicit switch and preserves configured regions when switched off', async (t) => {
  for (const enabled of [false, true]) {
    for (const withRegions of [false, true]) {
      await t.test(`enabled ${enabled}, regions ${withRegions}`, (child) => {
        const regions = [{ bundle_name: 'app.fixture', resource_id: 'mail-body' }];
        const selector = withRegions ? ', regions = [{ bundle_name = "app.fixture", resource_id = "mail-body" }]' : '';
        const policy = `{ enabled = ${enabled}, reason = "Synthetic fixture"${selector} }`;
        const f = fixture(child, PROJECT.replace(/^privacy = .+$/m, `privacy = ${policy}`));
        const loaded = loadConfiguration(f.projectPath, f.configPath, TOOLS);
        assert.deepEqual(loaded.project.tasks[0].privacy,
          { enabled, reason: 'Synthetic fixture', ...(withRegions ? { regions } : {}) });
        assert.ok(Object.isFrozen(loaded.project.tasks[0].privacy));
        if (withRegions) assert.ok(Object.isFrozen(loaded.project.tasks[0].privacy.regions));
      });
    }
  }
});

test('privacy rejects legacy modes, missing or non-boolean switches, and invalid region declarations', async (t) => {
  const values = [
    '{ mode = "none", reason = "fixture" }',
    '{ enabled = false, mode = "none", reason = "fixture" }',
    '{ reason = "fixture" }', '{ enabled = "false", reason = "fixture" }',
    '{ enabled = 0, reason = "fixture" }', '{ enabled = false, reason = " " }',
    '{ enabled = false, reason = "fixture", extra = true }',
    '{ enabled = false, reason = "fixture", regions = [] }',
    '{ enabled = true, reason = "fixture", regions = {} }',
    '{ enabled = false, reason = "fixture", regions = [{}] }',
    '{ enabled = true, reason = "fixture", regions = [{ bundle_name = "app", resource_id = " " }] }',
    '{ enabled = true, reason = "fixture", regions = [{ bundle_name = " ", resource_id = "body" }] }',
    '{ enabled = false, reason = "fixture", regions = [{ bundle_name = "app", resource_id = "body", x = 0 }] }',
  ];
  for (const value of values) {
    await t.test(value, (child) => {
      const log = child.mock.method(console, 'error', () => {});
      const f = fixture(child, PROJECT.replace(/^privacy = .+$/m, `privacy = ${value}`));
      assert.throws(() => loadConfiguration(f.projectPath, f.configPath, TOOLS), /tasks.0.privacy/);
      assert.equal(log.mock.callCount(), 1);
    });
  }
});

test('stability selectors reject blank, missing, unknown fields and non-array declarations', async (t) => {
  const values = ['{}', '[{}]', '[{ bundle_name = "system.fixture" }]',
    '[{ bundle_name = " ", resource_id = "clock" }]', '[{ bundle_name = "system.fixture", resource_id = "" }]',
    '[{ bundle_name = "system.fixture", resource_id = "clock", tolerance = 1 }]'];
  for (const value of values) {
    await t.test(value, (child) => {
      child.mock.method(console, 'error', () => {});
      const f = fixture(child, PROJECT.replace('[tools]', `stability_regions = ${value}\n[tools]`));
      assert.throws(() => loadConfiguration(f.projectPath, f.configPath, TOOLS), /dataset.stability_regions/);
    });
  }
});

test('an unrestricted task has no invented application filter or fixed sample count', (t) => {
  const f = fixture(t, PROJECT.replace('bundle_names = ["org.example.calendar"]\n', ''));
  const loaded = loadConfiguration(f.projectPath, f.configPath, TOOLS);
  assert.equal(loaded.project.tasks[0].bundle_names, undefined);
  assert.equal(loaded.project.tasks[0].target_count, 3);
});

test('optional pricing preserves explicit rates and allows zero without inventing defaults', (t) => {
  const f = fixture(t, PROJECT, CONFIG + PRICING);
  const loaded = loadConfiguration(f.projectPath, f.configPath, TOOLS);
  assert.deepEqual(loaded.profile.pricing, {
    currency: 'USD',
    input_cache_hit_per_million: 0.006,
    input_cache_miss_per_million: 0.3,
    output_per_million: 1.2,
  });
  assert.ok(Object.isFrozen(loaded.profile.pricing));
  writeFileSync(f.configPath, CONFIG + PRICING.replaceAll(/= (?:0\.006|0\.3|1\.2)/g, '= 0'));
  const free = loadConfiguration(f.projectPath, f.configPath, TOOLS);
  assert.deepEqual(free.profile.pricing, {
    currency: 'USD',
    input_cache_hit_per_million: 0,
    input_cache_miss_per_million: 0,
    output_per_million: 0,
  });
  assert.notEqual(free.digests.config, loaded.digests.config);
});

test('pricing rejects invalid numbers, incomplete rates and unsupported currencies', async (t) => {
  const cases: [string, string, RegExp][] = [
    ['unsupported currency', PRICING.replace('"USD"', '"CNY"'), /pricing.currency/],
    ['missing rate', PRICING.replace('output_per_million = 1.2\n', ''), /pricing.output_per_million/],
    ['string rate', PRICING.replace('= 1.2', '= "1.2"'), /pricing.output_per_million/],
  ];
  for (const field of ['input_cache_hit_per_million', 'input_cache_miss_per_million', 'output_per_million']) {
    for (const value of ['-0.01', 'inf', '-inf', 'nan']) {
      const changed = PRICING.replace(new RegExp(`${field} = [^\\n]+`), `${field} = ${value}`);
      cases.push([`${field} ${value}`, changed, new RegExp(`pricing\\.${field}`)]);
    }
  }
  for (const [name, pricing, expected] of cases) {
    await t.test(name, (child) => {
      const log = child.mock.method(console, 'error', () => {});
      const f = fixture(child, PROJECT, CONFIG + pricing);
      assert.throws(() => loadConfiguration(f.projectPath, f.configPath, TOOLS), expected);
      assert.equal(log.mock.callCount(), 1);
    });
  }
});

test('rejects unknown fields at every configuration boundary', async (t) => {
  const cases = [
    ['project root', `legacy = true\n${PROJECT}`, CONFIG],
    ['run', PROJECT.replace('max_actions = 12', 'max_actions = 12\nfallback = true'), CONFIG],
    ['agent', PROJECT.replace('llm = "chosen"', 'llm = "chosen"\nmodel = "hidden"'), CONFIG],
    ['tools', PROJECT.replace('[tools]', '[tools]\ncommand = "shell"'), CONFIG],
    ['task', `${PROJECT}\nunknown = "ignored?"`, CONFIG],
    ['config root', PROJECT, `tasks = []\n${CONFIG}`],
    ['profile', PROJECT, `${CONFIG}\nmax_retries = 5`],
    ['pricing', PROJECT, `${CONFIG}${PRICING}\npeak_hours = "automatic"`],
  ];
  for (const [name, project, config] of cases) {
    await t.test(name, (child) => {
      child.mock.method(console, 'error', () => {});
      const f = fixture(child, project, config);
      assert.throws(() => loadConfiguration(f.projectPath, f.configPath, TOOLS), /Unrecognized key/);
    });
  }
});

test('rejects ambiguous identities, dangling references and invalid budgets', async (t) => {
  const cases: [string, string, string, RegExp][] = [
    ['duplicate tools', PROJECT.replace('"Observe", "Capture"', '"Observe", "Observe"'), CONFIG, /Duplicate tool/],
    ['duplicate tasks', PROJECT + TASK, CONFIG, /Duplicate task/],
    ['unknown tool', PROJECT.replace('"Capture"', '"RunShell"'), CONFIG, /Unknown tool/],
    ['unknown profile', PROJECT.replace('llm = "chosen"', 'llm = "absent"'), CONFIG, /Unknown model profile/],
    ['inherited profile name', PROJECT.replace('llm = "chosen"', 'llm = "toString"'), CONFIG, /Unknown model profile/],
    ['zero actions', PROJECT.replace('max_actions = 12', 'max_actions = 0'), CONFIG, /max_actions/],
    ['negative duration', PROJECT.replace('= 3.5', '= -1'), CONFIG, /max_duration_seconds/],
    ['infinite duration', PROJECT.replace('= 3.5', '= inf'), CONFIG, /max_duration_seconds/],
    ['fractional count', PROJECT.replace('target_count = 3', 'target_count = 1.5'), CONFIG, /target_count/],
    ['empty tasks', PROJECT.replace(TASK, ''), CONFIG, /tasks/],
    ['empty instruction', PROJECT.replace('寻找日历周视图并改变窗口大小', '   '), CONFIG, /instruction/],
    ['old schema', PROJECT.replace('schema_version = 1', 'schema_version = 0'), CONFIG, /schema_version/],
    ['nonvisual profile', PROJECT, CONFIG.replace('supports_vision = true', 'supports_vision = false'), /vision/],
    ['unsupported provider', PROJECT, CONFIG.replace('openai_compatible', 'anthropic'), /provider/],
    ['unknown family', PROJECT, CONFIG.replace('"deepseek"', '"imaginary-vl"'), /model_family/],
    ['nondefault family', PROJECT, CONFIG.replace('"deepseek"', '"jev"'), /model_family/],
    ['invalid endpoint', PROJECT, CONFIG.replace('https://model.example.test/v1', 'file:///tmp/model'), /base_url/],
  ];
  for (const [name, project, config, expected] of cases) {
    await t.test(name, (child) => {
      child.mock.method(console, 'error', () => {});
      const f = fixture(child, project, config);
      assert.throws(() => loadConfiguration(f.projectPath, f.configPath, TOOLS), expected);
    });
  }
});

test('invalid TOML and missing files produce logged errors without echoing source text', (t) => {
  const log = t.mock.method(console, 'error', () => {});
  const sentinel = 'do-not-echo-this-fixture-value';
  const f = fixture(t, PROJECT, `password = "${sentinel}`);
  assert.throws(() => loadConfiguration(f.projectPath, f.configPath, TOOLS), /Invalid TOML/);
  assert.throws(() => loadConfiguration(join(f.root, 'absent.toml'), f.configPath, TOOLS), /Cannot read/);
  assert.equal(log.mock.callCount(), 2);
  assert.ok(!JSON.stringify(log.mock.calls).includes(sentinel));
});

test('runtime resolution rejects missing credentials instead of using ambient SDK keys', (t) => {
  const log = t.mock.method(console, 'error', () => {});
  const f = fixture(t);
  const { profile } = loadConfiguration(f.projectPath, f.configPath, TOOLS);
  const originalEnv = process.env;
  process.env = { ...originalEnv, MIDSCENE_MODEL_API_KEY: 'unrelated-fixture-key' };
  delete process.env[KEY_NAME];
  t.after(() => { process.env = originalEnv; });
  assert.throws(() => resolveModelConfig(profile), /Missing model credential environment variable/);
  process.env[KEY_NAME] = '   ';
  assert.throws(() => resolveModelConfig(profile), /Missing model credential environment variable/);
  assert.equal(log.mock.callCount(), 2);
  assert.ok(!JSON.stringify(log.mock.calls).includes('unrelated-fixture-key'));
});

test('the installed SDK consumes the explicit model mapping without ambient model overrides', async (t) => {
  const { ModelConfigManager } = await import('@midscene/shared/env/model-config-manager');
  const f = fixture(t);
  const loaded = loadConfiguration(f.projectPath, f.configPath, TOOLS);
  const originalEnv = process.env;
  process.env = {
    ...originalEnv,
    [KEY_NAME]: 'synthetic-config-test-key',
    MIDSCENE_MODEL_NAME: 'wrong-ambient-model',
    MIDSCENE_MODEL_BASE_URL: 'https://wrong.example.test',
    MIDSCENE_MODEL_REASONING_ENABLED: 'true',
  };
  t.after(() => { process.env = originalEnv; });
  const runtime = resolveModelConfig(loaded.profile);
  const manager = new ModelConfigManager(runtime);
  for (const intent of ['default', 'planning', 'locate', 'insight'] as const) {
    const actual = manager.getModelConfig(intent);
    assert.equal(actual.modelName, 'deployment-specific-model');
    assert.equal(actual.modelFamily, 'deepseek');
    assert.equal(actual.openaiBaseURL, 'https://model.example.test/v1');
    assert.equal(actual.openaiApiKey, 'synthetic-config-test-key');
    assert.equal(actual.protocol, 'openai-chat');
    assert.equal(actual.reasoningEnabled, false);
    assert.equal(actual.retryCount, 0);
    assert.equal(actual.streamMode, 'non-stream');
  }
  assert.ok(!JSON.stringify(loaded).includes('synthetic-config-test-key'));
});
