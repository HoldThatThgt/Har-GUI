import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import type { LoadedConfiguration } from './config.js';

/** @brief 核对原始项目字节后保存运行来源，不重建或改写已有项目原文。
 * @param config 已装载配置及原始摘要。 @param directory 本次独立运行目录。
 */
export async function saveRunConfiguration(config: LoadedConfiguration, directory: string): Promise<void> {
  try {
    const project = await readFile(config.projectPath);
    if (createHash('sha256').update(project).digest('hex') !== config.digests.project) {
      throw new Error('Project source digest changed since configuration was loaded');
    }
    await writeFile(path.join(directory, 'project.toml'), project, { flag: 'wx' });
    await writeFile(path.join(directory, 'configuration.json'), JSON.stringify(config, null, 2) + '\n', { flag: 'wx' });
  } catch (error) {
    console.error('Run provenance could not be saved:', String(error));
    throw error;
  }
}
