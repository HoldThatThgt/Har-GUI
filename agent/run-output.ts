import { randomUUID } from 'node:crypto';
import { mkdir, readFile, readdir, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

/** @brief 集中保存内部证据，只对实际交付目录改变运行状态名称。 */
interface RunOutput {
  /** 同根目录中至少三位的递增序号。 */
  index: string;
  /** 数据集内部不会改名的运行证据目录。 */
  artifacts: string;
  /** 包含实际产物与相对引用清单的运行目录。 */
  result: string;
}

/** @brief 从既有目录名延续序号，不读取或修改任何历史内容。 */
function nextIndex(names: string[]): string {
  let maximum = 0n;
  for (const name of names) {
    const match = /^(?:\.(\d+)|(\d+)-(?:running|success|failed))$/.exec(name);
    if (match) {
      const value = BigInt(match[1] ?? match[2]);
      if (value > maximum) maximum = value;
    }
  }
  return (maximum + 1n).toString().padStart(3, '0');
}

/** @brief 独占分配结果目录，运行原件集中保存到数据集内部。 */
async function reserveOutput(outputRoot: string, storageRoot: string): Promise<RunOutput> {
  const root = path.resolve(outputRoot);
  const runs = path.resolve(storageRoot, 'runs');
  await mkdir(root, { recursive: true });
  await mkdir(runs, { recursive: true });
  for (;;) {
    const index = nextIndex(await readdir(root));
    const result = path.join(root, `${index}-running`);
    try { await mkdir(result); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      console.error('Run output index was reserved concurrently:', index);
      continue;
    }
    const artifacts = path.join(runs, randomUUID());
    await mkdir(artifacts);
    return { index, artifacts, result };
  }
}

/** @brief 只更新交付清单的最终状态，不改任何样本文件或相对引用。 */
async function finish(output: RunOutput, status: 'complete' | 'failed', error?: unknown): Promise<string> {
  const internalFile = path.join(output.artifacts, 'run.json');
  const summary = JSON.parse(await readFile(internalFile, 'utf8'));
  if (!summary || typeof summary !== 'object' || Array.isArray(summary)) {
    throw new Error('Internal run summary must be an object');
  }
  if (status === 'complete' && summary.status === 'failed') {
    throw new Error('Execution returned with a failed internal run summary');
  }
  const failure = status === 'failed' ? { error: String(error) } : {};
  await writeFile(internalFile, JSON.stringify({ ...summary, run_index: output.index,
    artifacts_directory: output.artifacts, status, ...failure }, null, 2) + '\n');
  const filename = path.join(output.result, 'manifest.json');
  const manifest = JSON.parse(await readFile(filename, 'utf8'));
  if (!manifest || typeof manifest !== 'object' || manifest.run_index !== output.index
    || !Array.isArray(manifest.samples)) {
    throw new Error('Run delivery manifest must contain a samples array');
  }
  await writeFile(filename, JSON.stringify({ ...manifest, status, ...failure }, null, 2) + '\n');
  const suffix = status === 'complete' ? 'success' : 'failed';
  const destination = path.join(path.dirname(output.result), `${output.index}-${suffix}`);
  await rename(output.result, destination);
  return destination;
}

/** @brief 执行采集或离线导出，将实际交付留在递增结果目录。
 * @param outputRoot 项目声明的用户交付根目录。 @param storageRoot 数据集内部根目录。
 * @param execute 使用稳定证据目录和本轮交付目录的完整执行流程。
 * @return 包含实际交付文件和 manifest.json 的最终结果目录。
 */
export async function withRunOutput(
  outputRoot: string, storageRoot: string,
  execute: (internalDirectory: string, resultDirectory: string) => Promise<void>,
): Promise<string> {
  let output: RunOutput | undefined;
  try {
    output = await reserveOutput(outputRoot, storageRoot);
    await writeFile(path.join(output.artifacts, 'run.json'), JSON.stringify({
      status: 'running', run_index: output.index, artifacts_directory: output.artifacts,
    }, null, 2) + '\n');
    await writeFile(path.join(output.result, 'manifest.json'), JSON.stringify({
      status: 'running', run_index: output.index, samples: [],
    }, null, 2) + '\n');
    await execute(output.artifacts, output.result);
    return await finish(output, 'complete');
  } catch (error) {
    console.error('Run output execution failed:', String(error));
    if (output) {
      try { await finish(output, 'failed', error); }
      catch (closingError) {
        console.error('Failed run result could not be finalized:', String(closingError));
        throw new AggregateError([error, closingError], 'Run execution and result finalization failed');
      }
    }
    throw error;
  }
}
