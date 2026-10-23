import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { stringify } from 'smol-toml';
import type { LoadedConfiguration } from '../agent/config.js';

/** @brief 将可变测试配置写成真实原文，并同步两份原始字节的摘要。 */
export function writeFixtureConfiguration(config: LoadedConfiguration): void {
  const project = Buffer.from(stringify(config.project));
  const model = Buffer.from(stringify({ schema_version: 1, llm: { [config.project.agent.llm]: config.profile } }));
  writeFileSync(config.projectPath, project);
  writeFileSync(config.configPath, model);
  config.digests.project = createHash('sha256').update(project).digest('hex');
  config.digests.config = createHash('sha256').update(model).digest('hex');
}
