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
  const script = fileURLToPath(new URL('../agent_device.py', import.meta.url));
  return new Promise((resolve, reject) => {
    const child = spawn(options.python, [script, '--hdc', options.hdc, '--device', options.serial], {
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
