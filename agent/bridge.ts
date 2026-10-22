import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

/** @brief 设备连接的主机参数。 */
export interface DeviceOptions {
  /** Python 可执行文件。 */
  python: string;
  /** 官方 HDC 路径。 */
  hdc: string;
  /** 本次独占使用的设备序列号。 */
  serial: string;
}

/** @brief 运行受限 Python 桥接，错误时保留英文诊断。 */
export async function bridge(options: DeviceOptions, request: object, signal?: AbortSignal): Promise<any> {
  return executeBridge(options.python, ['--hdc', options.hdc, '--device', options.serial], request, signal);
}

/** @brief 离线后处理不传设备参数，不依赖连接设备的存在。 */
export async function localBridge(python: string, request: object, signal?: AbortSignal): Promise<any> {
  return executeBridge(python, [], request, signal);
}

/** @brief 通过标准输入输出执行一次结构化请求，不解释任意 shell 命令。 */
async function executeBridge(
  python: string, connection: string[], request: object, signal?: AbortSignal,
): Promise<any> {
  const script = fileURLToPath(new URL('../agent_device.py', import.meta.url));
  return new Promise((resolve, reject) => {
    const child = spawn(python, [script, ...connection], {
      cwd: path.dirname(script), windowsHide: true, signal, env: { ...process.env, PYTHONUTF8: '1' },
    });
    let output = '';
    let errorOutput = '';
    child.stdout.setEncoding('utf8').on('data', (part: string) => { output += part; });
    child.stderr.setEncoding('utf8').on('data', (part: string) => { errorOutput += part; });
    child.on('error', (error) => { console.error('Device process failed:', error.message); reject(error); });
    child.on('close', (code) => {
      try {
        if (code !== 0) throw new Error(`Device operation failed (${code}): ${errorOutput.trim()}`);
        const message = JSON.parse(output);
        if (message.error) throw new Error(message.error.message);
        resolve(message.result);
      } catch (error) { console.error('Device response failed:', String(error)); reject(error); }
    });
    child.stdin.end(JSON.stringify(request));
  });
}
