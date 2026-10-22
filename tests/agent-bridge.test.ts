import assert from 'node:assert/strict';
import childProcess from 'node:child_process';
import { syncBuiltinESMExports } from 'node:module';
import { test, type TestContext } from 'node:test';
import { DeviceOperationError, localBridge } from '../agent/bridge.js';

/** @brief 用独立进程返回指定标准输出和退出码，完全不连接设备。 */
function responseProcess(t: TestContext, output: string, code: number) {
  const original = childProcess.spawn;
  const script = `process.stdin.resume();process.stdin.on('end',()=>{
    process.stderr.write('PRIVATE_STDERR_BODY');process.stdout.write(process.argv[1]);
    process.exitCode=Number(process.argv[2]);});`;
  const spawn = t.mock.method(childProcess, 'spawn', () =>
    original(process.execPath, ['-e', script, output, String(code)]));
  syncBuiltinESMExports();
  t.mock.method(console, 'error', () => {});
  t.after(() => { spawn.mock.restore(); syncBuiltinESMExports(); });
  return spawn;
}

test('nonzero bridge exit preserves structured safe error diagnostics without stderr or another channel', async (t) => {
  const diagnostics = { phase: 'get_text', request: { x: 23, y: 45, mode: 'replace', window_id: 100 },
    input: { status: 'failed', error_type: 'UiComponentDisappearError' }, close: { status: 'completed' } };
  const spawn = responseProcess(t, JSON.stringify({ error: { message: 'Native text input failed', diagnostics } }), 1);
  await assert.rejects(localBridge('unused-python', { operation: 'input_text' }), (error: unknown) => {
    assert.ok(error instanceof DeviceOperationError);
    assert.equal(error.message, 'Native text input failed');
    assert.deepEqual(error.diagnostics, diagnostics);
    assert.ok(!String(error).includes('PRIVATE_STDERR_BODY'));
    return true;
  });
  assert.equal(spawn.mock.callCount(), 1);
});

test('missing structured bridge error fails without treating stderr as a replacement protocol', async (t) => {
  for (const output of ['PRIVATE_INVALID_STDOUT', JSON.stringify({ result: { ignored: true } })]) {
    await t.test(output.startsWith('{') ? 'nonzero result' : 'invalid JSON', async (child) => {
      responseProcess(child, output, 1);
      await assert.rejects(localBridge('unused-python', {}), (error: unknown) => {
        assert.ok(error instanceof Error);
        assert.match(error.message, /not valid JSON|without a structured error/);
        assert.ok(!String(error).includes('PRIVATE'));
        return true;
      });
    });
  }
});
