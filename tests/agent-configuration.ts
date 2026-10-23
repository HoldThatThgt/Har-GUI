import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { stringify } from 'smol-toml';
import type { LoadedConfiguration } from '../agent/config.js';

/** @brief 匿名配置明确选择五种已有采集来源，不依赖生产默认值。 */
export const FIXTURE_DELIVERY = {
  directory: '.',
  artifacts: [
    { source: 'screenshot', path: '{capture_id}_shot.png' },
    { source: 'accessibility', path: '{capture_id}_tree.json' },
    { source: 'application_accessibility', path: '{capture_id}_windows_tree.json' },
    { source: 'application_arkui', path: '{capture_id}_windows_arkui.json' },
    { source: 'windows', path: '{capture_id}_wms.json' },
  ],
} satisfies LoadedConfiguration['project']['delivery'];

/** @brief 将可变测试配置写成真实原文，并同步两份原始字节的摘要。 */
export function writeFixtureConfiguration(config: LoadedConfiguration): void {
  const project = Buffer.from(stringify(config.project));
  const model = Buffer.from(stringify({ schema_version: 1, llm: { [config.project.agent.llm]: config.profile } }));
  writeFileSync(config.projectPath, project);
  writeFileSync(config.configPath, model);
  config.digests.project = createHash('sha256').update(project).digest('hex');
  config.digests.config = createHash('sha256').update(model).digest('hex');
}
