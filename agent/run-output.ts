import { mkdir, readFile, readdir, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';

/** @brief 固定证据位置与可以改名的运行结果入口。 */
interface RunOutput {
  /** 至少三位的递增运行序号。 */
  index: string;
  /** 始终保持不变的绝对证据目录。 */
  artifacts: string;
  /** 执行期间的结果入口，不用于持久证据引用。 */
  result: string;
}

/** @brief 只按同根运行目录名称计算下一个序号，不读取旧运行内容。 */
function nextIndex(names: string[]): string {
  let maximum = 0n;
  for (const name of names) {
    const match = /^(?:\.(\d+)|(\d+)-(?:running|success|failed))$/.exec(name);
    if (!match) continue;
    const value = BigInt(match[1] ?? match[2]);
    if (value > maximum) maximum = value;
  }
  return (maximum + 1n).toString().padStart(3, '0');
}

/** @brief 独占创建固定证据目录，使用已有目录名称防止并发复用序号。 */
async function reserveOutput(outputRoot: string): Promise<RunOutput> {
  const root = path.resolve(outputRoot);
  await mkdir(root, { recursive: true });
  for (;;) {
    const index = nextIndex(await readdir(root));
    const artifacts = path.join(root, `.${index}`);
    try { await mkdir(artifacts); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      console.error('Run output index was reserved concurrently:', index);
      continue;
    }
    const result = path.join(root, `${index}-running`);
    await mkdir(result);
    return { index, artifacts, result };
  }
}

/** @brief 生成始终指向固定证据位置的结果字段。 */
function identity(output: RunOutput): object {
  return { run_index: output.index, artifacts_directory: output.artifacts, updated_at: new Date().toISOString() };
}

/** @brief 独立尝试保存两份运行摘要，任一写入失败都报告错误。 */
async function writeSummaries(output: RunOutput, summary: object): Promise<void> {
  const content = JSON.stringify(summary, null, 2) + '\n';
  const results = await Promise.allSettled([output.artifacts, output.result].map((directory) =>
    writeFile(path.join(directory, 'run.json'), content)));
  const failures = results.filter((result) => result.status === 'rejected');
  for (const failure of failures) console.error('Run summary could not be saved:', String(failure.reason));
  if (failures.length) throw failures[0].reason;
}

/** @brief 读取执行者保存的完整摘要，拒绝非对象结果。 */
async function readSummary(output: RunOutput): Promise<Record<string, unknown>> {
  const summary: unknown = JSON.parse(await readFile(path.join(output.artifacts, 'run.json'), 'utf8'));
  if (!summary || typeof summary !== 'object' || Array.isArray(summary)) {
    throw new Error('Run summary must be an object');
  }
  return summary as Record<string, unknown>;
}

/** @brief 保存完整成功摘要，仅修改不被证据引用的结果目录名称。 */
async function finishSuccess(output: RunOutput): Promise<string> {
  const summary = await readSummary(output);
  if (summary.status === 'failed') throw new Error('Execution returned with a failed run summary');
  await writeSummaries(output, { ...summary, ...identity(output), status: 'complete' });
  const destination = path.join(path.dirname(output.result), `${output.index}-success`);
  await rename(output.result, destination);
  return destination;
}

/** @brief 保留完整运行摘要并记录执行或清理的真实错误。 */
async function finishFailure(output: RunOutput, error: unknown): Promise<void> {
  const summary = await readSummary(output);
  await writeSummaries(output, { ...summary, ...identity(output), status: 'failed', error: String(error) });
  await rename(output.result, path.join(path.dirname(output.result), `${output.index}-failed`));
}

/** @brief 在固定证据目录执行运行，返回递增序号和最终状态组成的结果目录。
 * @param outputRoot 项目配置指定的输出根目录。 @param execute 包含全部执行与清理步骤的运行函数。
 * @return 只保存最终运行摘要的结果目录，完整证据位于摘要声明的固定目录。
 */
export async function withRunOutput(
  outputRoot: string, execute: (directory: string) => Promise<void>,
): Promise<string> {
  let output: RunOutput | undefined;
  try {
    output = await reserveOutput(outputRoot);
    await writeSummaries(output, { status: 'running', ...identity(output) });
    await execute(output.artifacts);
    return await finishSuccess(output);
  } catch (error) {
    console.error('Run output execution failed:', String(error));
    if (output) {
      try { await finishFailure(output, error); }
      catch (finalizationError) {
        console.error('Failed run result could not be finalized:', String(finalizationError));
        throw new AggregateError([error, finalizationError], 'Run execution and result finalization failed');
      }
    }
    throw error;
  }
}
