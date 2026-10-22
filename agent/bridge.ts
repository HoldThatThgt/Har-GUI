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

/** @brief 传递 Python 标准输出中的安全错误与结构化诊断。 */
export class DeviceOperationError extends Error {
  /** Python 原生输入边界生成的不含正文的诊断。 */
  readonly diagnostics?: object;

  /** @brief 绑定桥接错误的安全消息与可选诊断。 */
  constructor(message: string, diagnostics?: object) {
    super(message);
    this.diagnostics = diagnostics;
  }
}

/** @brief 无论退出码如何均只解释标准输出 JSON，不以 stderr 作为替代协议。 */
function readResponse(output: string, code: number | null): any {
  let message: any;
  try { message = JSON.parse(output); }
  catch { throw new Error('Device response is not valid JSON'); }
  if (message.error) {
    if (typeof message.error.message !== 'string') throw new Error('Device response has no error message');
    throw new DeviceOperationError(message.error.message, message.error.diagnostics);
  }
  if (code !== 0) throw new Error(`Device operation exited (${code}) without a structured error`);
  return message.result;
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
    child.stdout.setEncoding('utf8').on('data', (part: string) => { output += part; });
    child.stderr.resume();
    child.on('error', (error) => { console.error('Device process failed:', error.message); reject(error); });
    child.on('close', (code) => {
      try {
        resolve(readResponse(output, code));
      } catch (error) { console.error('Device response failed:', String(error)); reject(error); }
    });
    child.stdin.end(JSON.stringify(request));
  });
}
