import { mkdir } from 'node:fs/promises';
import path from 'node:path';
import { localBridge } from './bridge.js';
import type { LoadedConfiguration } from './config.js';
import { saveRunConfiguration } from './provenance.js';
import { withRunOutput } from './run-output.js';
import { RunState } from './state.js';

/** @brief 导出已经持久验收的样本，不读取模型凭据或增加采集计数。
 * @param state 本轮配置、内部目录和实际交付目录。 @param python 本地 Python 可执行文件。
 * @param sample 已登记样本或调用者明确指定的样本身份。 @return 原登记样本及本次实际导出清单。
 */
export async function exportAccepted(state: RunState, python: string, sample: { sample_id: string }) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(sample.sample_id)) {
    console.error('Cannot export an invalid sample identity');
    throw new Error('Invalid export sample identity');
  }
  const receipts = path.join(state.directory, 'exports');
  await mkdir(receipts, { recursive: true });
  const result = await localBridge(python, { operation: 'export_sample', root: state.config.datasetPath,
    identity: state.datasetIdentity, sample_id: sample.sample_id, delivery: state.config.project.delivery,
    destination: state.deliveryDirectory, receipt: path.join(receipts, `${sample.sample_id}.json`),
  }, state.signal);
  if (result.new_samples !== 0 || result.sample.sample_id !== sample.sample_id) {
    console.error('Offline export changed sample identity or collection count');
    throw new Error('Offline export returned inconsistent sample facts');
  }
  state.event('sample_exported', { sample_id: sample.sample_id, export: result.export });
  return result;
}

/** @brief 执行纯本地导出并保存独立运行记录，原始登记及采集证据保持只读。 */
async function executeExport(
  config: LoadedConfiguration, python: string, options: { sampleId: string; projectSources?: string[] },
  directories: { internal: string; delivery: string },
): Promise<void> {
  const state = new RunState(config, directories.internal, directories.delivery, options.projectSources);
  try {
    await saveRunConfiguration(config, state.directory);
    state.save('running', { operation: 'export', sample_id: options.sampleId, new_samples: 0 });
    const result = await exportAccepted(state, python, { sample_id: options.sampleId });
    state.accepted = [result.sample];
    state.save('complete', { operation: 'export', sample: result.sample, export: result.export, new_samples: 0 });
  } catch (error) {
    console.error('Offline sample export failed:', String(error));
    state.save('failed', { operation: 'export', error: String(error), new_samples: 0 });
    throw error;
  }
}

/** @brief 为已有样本分配新交付目录，只读原采集事实并按项目声明导出。
 * @param config 当前项目及模型声明，模型凭据不会被读取。 @param python 本地 Python 可执行文件。
 * @param options 已登记样本身份及明确历史项目来源。 @return 包含实际产物和清单的结果目录。
 */
export async function exportSample(
  config: LoadedConfiguration, python: string, options: { sampleId: string; projectSources?: string[] },
): Promise<string> {
  return withRunOutput(config.outputPath, config.datasetPath, (internal, delivery) =>
    executeExport(config, python, options, { internal, delivery }));
}
