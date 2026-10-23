import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, unlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';
import { test, type TestContext } from 'node:test';
import { verifyCapture, verifySavedScreenshot, verifyWindowChanges, type WindowChange } from '../agent/acceptance.js';
import type { CollectionAgent, CollectionDevice } from '../agent/device.js';
import type { RunState, WindowInfo } from '../agent/state.js';

const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAQAAAADCAIAAAA7ljmRAAAAE0lEQVR4nGP8+u07AwwwwVnoHABrRALop6WreQAAAABJRU5ErkJggg==',
  'base64',
);

function fileInfo(file: string, bytes: Buffer) {
  return { file, bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
}

function fixture(t: TestContext) {
  const parent = resolve(tmpdir());
  const directory = mkdtempSync(join(parent, 'har-gui-acceptance-test-'));
  t.after(() => {
    assert.equal(dirname(directory), parent);
    assert.ok(basename(directory).startsWith('har-gui-acceptance-test-'));
    rmSync(directory, { recursive: true, force: true });
  });
  mkdirSync(join(directory, 'arkui'));
  const attributes = {
    hostWindowId: '92', bundleName: 'org.example.calendar', displayId: '0',
    bounds: '[0,0][4,3]', type: 'root', visible: 'true',
  };
  const accessibility = [{ attributes, children: [] }];
  const arkui = { attributes: { type: 'root', ID: '0' }, source_attributes: { ID: '0' },
    children: [], raw: '    | ID: 0\r\n', source_line: 1, declared_child_count: 0 };
  const contents: Record<string, Buffer> = {
    'screenshot.png': PNG,
    'layout.json': Buffer.from(JSON.stringify({ attributes: {}, children: accessibility })),
    'accessibility.json': Buffer.from(JSON.stringify(accessibility)),
    'arkui/window-92.json': Buffer.from(JSON.stringify(arkui)),
    'arkui/window-92.txt': Buffer.from('|-> root childSize:0\r\n    | ID: 0\r\n'),
  };
  for (const [file, bytes] of Object.entries(contents)) writeFileSync(join(directory, file), bytes);
  const metadata: any = {
    capture_id: 'fixture-capture', status: 'complete', display: { id: 0, width: 4, height: 3 },
    coverage: { status: 'verified', covered_window_ids: [92], excluded_windows: [], unresolved_windows: [] },
    consistency: { status: 'observed', atomic: false, pixels: { status: 'observed', changed: false } },
    window_inventory: [{ window_id: 92 }], foreground_window_ids_without_tree: [],
    files: {
      screenshot: { ...fileInfo('screenshot.png', PNG), width: 4, height: 3 },
      layout: fileInfo('layout.json', contents['layout.json']),
      accessibility: fileInfo('accessibility.json', contents['accessibility.json']),
      arkui_92: fileInfo('arkui/window-92.json', contents['arkui/window-92.json']),
      arkui_raw_92: fileInfo('arkui/window-92.txt', contents['arkui/window-92.txt']),
    },
    windows: [{
      window_id: 92, process_id: 202, display_id: 0, ui_node_id: 920,
      bundle_name: 'org.example.calendar', z_order: 17,
      bounding_box: { x: 0, y: 0, width: 4, height: 3 },
      screen_bounds: { x: 0, y: 0, width: 4, height: 3 },
      trees: {
        accessibility: { file: 'accessibility.json', json_pointer: '/0', node_count: 1 },
        arkui: { ...fileInfo('arkui/window-92.json', contents['arkui/window-92.json']),
          raw: fileInfo('arkui/window-92.txt', contents['arkui/window-92.txt']), node_count: 1, scope: 'window' },
      },
    }],
  };
  const save = () => writeFileSync(join(directory, 'metadata.json'), JSON.stringify(metadata, null, 2));
  save();
  return { directory, metadata, contents, save };
}

test('accepts a paired screenshot and both indexed window trees without modifying evidence', async (t) => {
  const f = fixture(t);
  const before = new Map(Object.keys(f.contents).concat('metadata.json')
    .map((file) => [file, readFileSync(join(f.directory, file))]));
  const result = await verifyCapture(f.directory);
  assert.equal(result.capture_id, 'fixture-capture');
  assert.equal(result.windows[0].bundle_name, 'org.example.calendar');
  assert.equal(result.windows[0].trees.accessibility.json_pointer, '/0');
  assert.equal(result.windows[0].trees.arkui.file, 'arkui/window-92.json');
  for (const [file, bytes] of before) assert.deepEqual(readFileSync(join(f.directory, file)), bytes);
});

test('rejects additional screenshots anywhere inside a candidate', async (t) => {
  for (const name of ['screenshot-after.png', 'debug.JPG', 'arkui/observation.png']) {
    await t.test(name, async (child) => {
      const f = fixture(child);
      writeFileSync(join(f.directory, name), PNG);
      await assert.rejects(verifyCapture(f.directory), /exactly one screenshot/i);
    });
  }
});

test('rejects changed screenshot or tree bytes and incorrect recorded size or hash', async (t) => {
  for (const file of ['screenshot.png', 'accessibility.json', 'arkui/window-92.json']) {
    await t.test(`changed ${file}`, async (child) => {
      const f = fixture(child);
      const bytes = Buffer.from(f.contents[file]);
      bytes[bytes.length - 1] ^= 1;
      writeFileSync(join(f.directory, file), bytes);
      await assert.rejects(verifyCapture(f.directory), /digest mismatch/i);
    });
  }
  for (const field of ['bytes', 'sha256']) {
    await t.test(`incorrect ${field}`, async (child) => {
      const f = fixture(child);
      f.metadata.files.arkui_92[field] = field === 'bytes' ? 0 : '0'.repeat(64);
      f.save();
      await assert.rejects(verifyCapture(f.directory), /digest mismatch/i);
    });
  }
});

test('rejects absent tree declarations and empty tree evidence', async (t) => {
  for (const source of ['accessibility', 'arkui']) {
    await t.test(`missing ${source} declaration`, async (child) => {
      const f = fixture(child);
      delete f.metadata.windows[0].trees[source];
      f.save();
      await assert.rejects(verifyCapture(f.directory), /Incomplete window evidence/i);
    });
    await t.test(`empty ${source} tree`, async (child) => {
      const f = fixture(child);
      f.metadata.windows[0].trees[source].node_count = 0;
      f.save();
      await assert.rejects(verifyCapture(f.directory), /Incomplete window evidence/i);
    });
  }
});

test('requires actual files and index entries for the screenshot and each referenced tree', async (t) => {
  const artifacts = [
    ['screenshot', 'screenshot.png'],
    ['accessibility', 'accessibility.json'],
    ['arkui_92', 'arkui/window-92.json'],
  ];
  for (const [key, file] of artifacts) {
    await t.test(`missing indexed ${file}`, async (child) => {
      const f = fixture(child);
      unlinkSync(join(f.directory, file));
      await assert.rejects(verifyCapture(f.directory));
    });
    await t.test(`removed ${key} index`, async (child) => {
      const f = fixture(child);
      delete f.metadata.files[key];
      f.save();
      await assert.rejects(verifyCapture(f.directory));
    });
  }
  await t.test('missing ArkUI file cannot be hidden by removing its index', async (child) => {
    const f = fixture(child);
    unlinkSync(join(f.directory, 'arkui/window-92.json'));
    delete f.metadata.files.arkui_92;
    f.save();
    await assert.rejects(verifyCapture(f.directory));
  });
});

test('rejects incomplete candidates and candidates with no exposed windows', async (t) => {
  for (const status of ['partial', 'failed']) {
    await t.test(status, async (child) => {
      const f = fixture(child);
      f.metadata.status = status;
      f.save();
      await assert.rejects(verifyCapture(f.directory), /not complete/i);
    });
  }
  await t.test('no windows', async (child) => {
    const f = fixture(child);
    f.metadata.windows = [];
    f.save();
    await assert.rejects(verifyCapture(f.directory), /no exposed windows/i);
  });
});

/** 创建仅用于窗口几何验收的完整事实。 */
function geometry(overrides: Partial<WindowInfo> = {}): WindowInfo {
  const box = overrides.bounding_box ?? { x: 0, y: 0, width: 100, height: 100 };
  return { window_id: 92, process_id: 202, ui_node_id: 920, display_id: 0,
    bundle_name: 'org.example.calendar', z_order: 17, window_mode: 1, is_system: false, focused: true,
    bounding_box: box, screen_bounds: box, ...overrides };
}

/** 构造工具实际返回的变更记录，保留前后窗口事实。 */
function changed(before: WindowInfo, after: WindowInfo): WindowChange {
  return { window_id: after.window_id, before, after, bounds_changed: true };
}

test('checks only the final state after moving then resizing the same window and preserves all history', () => {
  const initial = geometry();
  const moved = geometry({ bounding_box: { x: 20, y: 30, width: 100, height: 100 } });
  const resized = geometry({ bounding_box: { x: 20, y: 30, width: 160, height: 140 } });
  const changes = [changed(initial, moved), changed(moved, resized)];
  const metadata = { windows: [resized] };
  const original = structuredClone({ metadata, changes });
  assert.deepEqual(verifyWindowChanges(metadata, changes), []);
  assert.deepEqual({ metadata, changes }, original);
  assert.equal(changes.length, 2);
});

test('accepts collector window identity published from its captured ArkUI session', () => {
  const after = geometry({ bounding_box: { x: 20, y: 30, width: 160, height: 140 } });
  const { ui_node_id: nodeId, ...window } = after;
  const captured = { ...window, trees: { arkui: { session: {
    window_id: window.window_id, process_id: window.process_id, display_id: window.display_id,
    is_system: false, ui_node_id: nodeId,
  } } } };
  const published = { ...captured, ui_node_id: captured.trees.arkui.session.ui_node_id };
  const history = [changed(geometry(), after)];
  assert.equal(published.ui_node_id, 920);
  assert.deepEqual(verifyWindowChanges({ windows: [published] }, history), []);
  assert.equal(published.trees.arkui.session.ui_node_id, published.ui_node_id);
});

test('validates the latest change independently for every window', (t) => {
  t.mock.method(console, 'error', () => {});
  const first = geometry();
  const second = geometry({ window_id: 93, process_id: 203, ui_node_id: 930 });
  const firstFinal = geometry({ bounding_box: { x: 10, y: 0, width: 100, height: 100 } });
  const secondFinal = { ...second, bounding_box: { x: 200, y: 20, width: 150, height: 90 },
    screen_bounds: { x: 200, y: 20, width: 150, height: 90 } };
  const changes = [changed(first, firstFinal), changed(second, secondFinal)];
  assert.deepEqual(verifyWindowChanges({ windows: [firstFinal, secondFinal] }, changes), []);
  assert.throws(() => verifyWindowChanges({ windows: [firstFinal, second] }, changes), /bounds do not match/);
});

test('rejects a missing final window and each strict identity mismatch', (t) => {
  t.mock.method(console, 'error', () => {});
  const before = geometry();
  const after = geometry({ bounding_box: { x: 20, y: 0, width: 100, height: 100 } });
  const changes = [changed(before, after)];
  assert.throws(() => verifyWindowChanges({ windows: [] }, changes), /identity does not match/);
  for (const override of [{ process_id: 999 }, { bundle_name: 'org.example.replaced' },
    { ui_node_id: 999 }, { display_id: 1 }, { window_id: 999 }]) {
    assert.throws(() => verifyWindowChanges({ windows: [{ ...after, ...override }] }, changes),
      /identity does not match/);
  }
  const missingIdentity = { ...after } as any;
  delete missingIdentity.ui_node_id;
  assert.throws(() => verifyWindowChanges({ windows: [missingIdentity] }, changes), /identity does not match/);
});

test('rejects a final exact screen box mismatch but does not depend on object field order', (t) => {
  t.mock.method(console, 'error', () => {});
  const initial = geometry();
  const after = geometry({ bounding_box: { x: 20, y: 30, width: 160, height: 140 } });
  const changes = [changed(initial, after)];
  assert.deepEqual(verifyWindowChanges({ windows: [{ ...after,
    screen_bounds: { height: 140, width: 160, y: 30, x: 20 } }] }, changes), []);
  for (const field of ['x', 'y', 'width', 'height'] as const) {
    const wrong = { ...after, screen_bounds: { ...after.screen_bounds, [field]: after.screen_bounds[field] + 1 } };
    assert.throws(() => verifyWindowChanges({ windows: [wrong] }, changes), /bounds do not match/);
  }
});

/** 建立一个前景窗口覆盖目标一半矩形的变更记录。 */
function overlapping() {
  const target = geometry();
  const before = geometry({ window_id: 93, process_id: 203, ui_node_id: 930, z_order: 20,
    bundle_name: 'org.example.notes', bounding_box: { x: 150, y: 0, width: 100, height: 100 } });
  const after = { ...before, bounding_box: { x: 50, y: 0, width: 100, height: 100 },
    screen_bounds: { x: 50, y: 0, width: 100, height: 100 } };
  const change = { ...changed(before, after), occlusion: {
    target_window_id: target.window_id, occluder_window_id: after.window_id, bounding_box_overlap_fraction: 0.5,
  } };
  return { target, before, after, change };
}

test('recomputes final rectangle overlap fractions without overwriting historical measurements', () => {
  const f = overlapping();
  const resized = { ...f.after, bounding_box: { x: 75, y: 0, width: 100, height: 100 },
    screen_bounds: { x: 75, y: 0, width: 100, height: 100 } };
  const changes = [f.change, changed(f.after, resized)];
  const verified = verifyWindowChanges({ windows: [f.target, resized] }, changes);
  assert.deepEqual(verified, [{ target_window_id: 92, occluder_window_id: 93,
    bounding_box_overlap_fraction: 0.25 }]);
  assert.equal(f.change.occlusion.bounding_box_overlap_fraction, 0.5);
});

test('repeated changes to the same occlusion pair yield one final measured relation', () => {
  const f = overlapping();
  const last = { ...f.after, bounding_box: { x: 75, y: 50, width: 100, height: 100 },
    screen_bounds: { x: 75, y: 50, width: 100, height: 100 } };
  const changes = [f.change, { ...changed(f.after, last), occlusion: {
    ...f.change.occlusion, bounding_box_overlap_fraction: 0.125,
  } }];
  assert.deepEqual(verifyWindowChanges({ windows: [f.target, last] }, changes), [{
    target_window_id: 92, occluder_window_id: 93, bounding_box_overlap_fraction: 0.125,
  }]);
});

test('rejects missing occlusion targets, reversed or equal z_order and different displays', (t) => {
  t.mock.method(console, 'error', () => {});
  const f = overlapping();
  const invalid = [[], [f.after], [f.target],
    [f.target, { ...f.after, z_order: f.target.z_order - 1 }],
    [f.target, { ...f.after, z_order: f.target.z_order }],
    [{ ...f.target, display_id: 1 }, f.after]];
  for (const windows of invalid) {
    assert.throws(() => verifyWindowChanges({ windows }, [f.change]),
      /identity does not match|wrong z_order/);
  }
});

test('rejects touching, separated and fully covering rectangles as partial occlusion', (t) => {
  t.mock.method(console, 'error', () => {});
  const f = overlapping();
  for (const box of [{ x: 100, y: 0, width: 100, height: 100 },
    { x: 120, y: 0, width: 100, height: 100 }, { x: 0, y: 0, width: 100, height: 100 },
    { x: -10, y: -10, width: 120, height: 120 }]) {
    const after = { ...f.after, bounding_box: box, screen_bounds: box };
    const change = { ...f.change, after };
    assert.throws(() => verifyWindowChanges({ windows: [f.target, after] }, [change]), /partial overlap/);
  }
});

test('saved-image acceptance binds the final digest without prematurely counting a sample', async (t) => {
  const f = fixture(t);
  const final = { ...geometry(), ...f.metadata.windows[0] };
  const before = geometry({ bounding_box: { x: 10, y: 10, width: 100, height: 100 } });
  const middle = geometry({ bounding_box: { x: 0, y: 0, width: 100, height: 100 } });
  const history = [changed(before, middle), changed(middle, final)];
  const state = { candidate: { path: f.directory, window_changes: history },
    task: { acceptance: 'Fixture passes', privacy: { enabled: false, reason: 'Synthetic fixture' } },
    signal: new AbortController().signal, accepted: [], event() {}, save() {} } as unknown as RunState;
  const device = { frozenScreenshot: undefined } as unknown as CollectionDevice;
  const agent = { aiAssert: async () => ({ pass: true, thought: 'Fixture passed' }) } as unknown as CollectionAgent;
  const accepted = await verifySavedScreenshot(state, device, agent,
    { path: f.directory, files: { screenshot: f.metadata.files.screenshot } });
  assert.equal(accepted.accepted_screenshot_sha256, f.metadata.files.screenshot.sha256);
  assert.deepEqual(state.candidate.window_changes, history);
  assert.equal(device.frozenScreenshot, undefined);
  assert.equal(state.accepted.length, 0);
});

test('unmatched visible windows and invalid observation evidence cannot reach saved-image acceptance', async (t) => {
  for (const mutate of [
    (metadata: any) => { metadata.foreground_window_ids_without_tree = [99]; },
    (metadata: any) => { metadata.window_inventory.push({ window_id: 99 }); },
    (metadata: any) => { metadata.coverage.unresolved_windows.push({ window_id: 99 }); },
    (metadata: any) => { metadata.consistency.status = 'failed'; },
    (metadata: any) => { metadata.consistency.status = 'verified'; },
  ]) {
    const f = fixture(t);
    mutate(f.metadata);
    f.save();
    await assert.rejects(verifyCapture(f.directory), /coverage|observ|consisten/i);
  }
});

test('observed raw pixel and tree changes remain diagnostics rather than a stability verdict', async (t) => {
  const f = fixture(t);
  f.metadata.consistency.pixels.changed = true;
  f.metadata.consistency.pixels.changed_pixel_count = 165;
  f.metadata.consistency.checks = { layout: { changed: true }, windows: { changed: false } };
  f.save();
  const result = await verifyCapture(f.directory);
  assert.equal(result.consistency.status, 'observed');
  assert.equal(result.consistency.pixels.changed, true);
  assert.equal(Object.hasOwn(result.consistency, 'stable'), false);
});

test('saved-image acceptance describes the privacy switch without changing custom acceptance', async (t) => {
  for (const enabled of [false, true]) {
    await t.test(`redaction ${enabled}`, async (child) => {
      const f = fixture(child);
      const acceptance = 'The requested custom fixture state is visible';
      const state = { candidate: { capture_id: 'frame' }, task: { acceptance, privacy: { enabled,
        reason: 'Synthetic fixture', regions: [{ bundle_name: 'app.fixture', resource_id: 'mail-body' }] } },
        signal: new AbortController().signal, event() {} } as unknown as RunState;
      const calls: any[][] = [];
      const device = { frozenScreenshot: undefined } as unknown as CollectionDevice;
      const agent = { aiAssert: async (...args: any[]) => {
        calls.push(args);
        assert.equal(device.frozenScreenshot, `data:image/png;base64,${PNG.toString('base64')}`);
        return { pass: true };
      } } as unknown as CollectionAgent;
      await verifySavedScreenshot(state, device, agent,
        { path: f.directory, files: { screenshot: f.metadata.files.screenshot } });
      assert.equal(calls.length, 1);
      assert.equal(calls[0][0], acceptance);
      assert.match(calls[0][2].context, enabled
        ? /Privacy redaction is enabled;.*postprocessed image/
        : /Privacy redaction is disabled;.*original image without privacy redaction/);
      assert.match(calls[0][2].context, /Apply the project acceptance statement as provided/);
      assert.equal(calls[0][2].abortSignal, state.signal);
      assert.equal(device.frozenScreenshot, undefined);
    });
  }
});

test('saved-image failure clears the frozen screenshot and never increments quota', async (t) => {
  const f = fixture(t);
  const state = { candidate: { capture_id: 'frame' },
    task: { acceptance: 'Must pass', privacy: { enabled: false, reason: 'Synthetic fixture' } },
    signal: new AbortController().signal, accepted: [], event() {} } as unknown as RunState;
  const device = { frozenScreenshot: undefined } as unknown as CollectionDevice;
  const agent = { aiAssert: async () => ({ pass: false, thought: 'Not the requested page' }) } as unknown as CollectionAgent;
  await assert.rejects(verifySavedScreenshot(state, device, agent,
    { path: f.directory, files: { screenshot: f.metadata.files.screenshot } }), /Project acceptance failed/);
  assert.equal(device.frozenScreenshot, undefined);
  assert.equal(state.accepted.length, 0);
});
