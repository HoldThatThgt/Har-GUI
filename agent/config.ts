import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { MODEL_FAMILY_VALUES, type TModelConfig, type TModelFamily } from '@midscene/shared/env/types';
import { parse } from 'smol-toml';
import { z } from 'zod';

const nonEmpty = z.string().regex(/\S/, 'Must not be blank');
const profileSchema = z.object({
  provider: z.literal('openai_compatible'),
  base_url: z.string().url().regex(/^https?:\/\//, 'Must use HTTP or HTTPS'),
  model: nonEmpty,
  model_family: nonEmpty.refine(
    (value) => value !== 'jev' && MODEL_FAMILY_VALUES.includes(value as TModelFamily),
    'Model family is not supported as the default Midscene model',
  ),
  api_key_env: nonEmpty,
  supports_vision: z.literal(true),
  pricing: z.object({
    currency: z.literal('USD'),
    input_cache_hit_per_million: z.number().finite().nonnegative(),
    input_cache_miss_per_million: z.number().finite().nonnegative(),
    output_per_million: z.number().finite().nonnegative(),
  }).strict().optional(),
}).strict();

const projectSchema = z.object({
  schema_version: z.literal(1),
  run: z.object({
    name: nonEmpty,
    output: nonEmpty,
    max_actions: z.number().int().positive(),
    max_duration_seconds: z.number().finite().positive(),
  }).strict(),
  agent: z.object({ llm: nonEmpty }).strict(),
  tools: z.object({ enabled: z.array(nonEmpty).min(1) }).strict(),
  tasks: z.array(z.object({
    name: nonEmpty,
    instruction: nonEmpty,
    acceptance: nonEmpty,
    target_count: z.number().int().positive(),
    bundle_names: z.array(nonEmpty).min(1).optional(),
  }).strict()).min(1),
}).strict();

const configurationSchema = z.object({
  schema_version: z.literal(1),
  llm: z.record(nonEmpty, profileSchema),
}).strict();

/** @brief 描述一次通用采集任务的配置，不包含模型部署。 */
export type Project = z.infer<typeof projectSchema>;

/** @brief 模型连接、能力和可选估算单价声明，只保存凭据环境变量名。 */
export type ModelProfile = z.infer<typeof profileSchema>;

/** @brief 静态装载结果，路径均为绝对路径，摘要来自原始文件字节。 */
export interface LoadedConfiguration {
  /** 已校验的任务描述。 */
  project: Project;
  /** 项目选中的模型配置。 */
  profile: ModelProfile;
  /** 项目文件的绝对路径。 */
  projectPath: string;
  /** 模型配置文件的绝对路径。 */
  configPath: string;
  /** 相对项目目录解析后的输出目录。 */
  outputPath: string;
  /** 两份配置文件的 SHA-256 十六进制摘要。 */
  digests: { project: string; config: string };
}

/** @brief 记录配置错误并停止装载。 @param message 不含凭据值的错误说明。 */
function fail(message: string): never {
  console.error(`Configuration error: ${message}`);
  throw new Error(message);
}

/** @brief 读取原始 TOML 字节与解析结果。 @param path 文件绝对路径。 @return 原文与对象。 */
function readDocument(path: string): { raw: Buffer; data: unknown } {
  let raw: Buffer;
  try {
    raw = readFileSync(path);
  } catch {
    fail(`Cannot read configuration file: ${path}`);
  }
  try {
    return { raw, data: parse(raw.toString('utf8')) };
  } catch {
    fail(`Invalid TOML in configuration file: ${path}`);
  }
}

/** @brief 校验配置结构。 @param schema 字段约束。 @param data 解析对象。 @param path 来源文件。 */
function validate<T>(schema: z.ZodType<T>, data: unknown, path: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    const details = result.error.issues.map((issue) => `${issue.path.join('.')}: ${issue.message}`);
    fail(`Invalid configuration ${path}: ${details.join('; ')}`);
  }
  return result.data;
}

/** @brief 拒绝重复声明。 @param values 已声明名称。 @param label 用于错误定位的字段名。 */
function requireUnique(values: string[], label: string): void {
  const seen = new Set<string>();
  for (const value of values) {
    if (seen.has(value)) fail(`Duplicate ${label}: ${value}`);
    seen.add(value);
  }
}

/** @brief 冻结运行快照中的嵌套对象与数组。 @param value 已完成校验的值。 @return 同一冻结值。 */
function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

/** @brief 装载任务和模型配置，不访问设备、模型或凭据值。
 * @param projectPath 项目 TOML 路径。 @param configPath 模型 TOML 路径。
 * @param registeredTools 已实现的工具名称。 @return 校验并冻结的配置与来源摘要。
 */
export function loadConfiguration(
  projectPath: string,
  configPath: string,
  registeredTools: readonly string[],
): LoadedConfiguration {
  const projectFile = resolve(projectPath);
  const configFile = resolve(configPath);
  const projectDocument = readDocument(projectFile);
  const configDocument = readDocument(configFile);
  const project = validate(projectSchema, projectDocument.data, projectFile);
  const config = validate(configurationSchema, configDocument.data, configFile);
  requireUnique(project.tools.enabled, 'tool');
  requireUnique(project.tasks.map((task) => task.name), 'task');
  const available = new Set(registeredTools);
  for (const name of project.tools.enabled) {
    if (!available.has(name)) fail(`Unknown tool: ${name}`);
  }
  if (!Object.hasOwn(config.llm, project.agent.llm)) fail(`Unknown model profile: ${project.agent.llm}`);
  const profile = config.llm[project.agent.llm];
  return freeze({
    project,
    profile,
    projectPath: projectFile,
    configPath: configFile,
    outputPath: resolve(dirname(projectFile), project.run.output),
    digests: {
      project: createHash('sha256').update(projectDocument.raw).digest('hex'),
      config: createHash('sha256').update(configDocument.raw).digest('hex'),
    },
  });
}

/** @brief 在运行时读取指定凭据，生成隔离的 Midscene 模型配置。
 * @param profile 已通过静态校验的模型声明。 @return 仅供 SDK 使用的配置，禁止记录或序列化。
 */
export function resolveModelConfig(profile: ModelProfile): TModelConfig {
  const key = process.env[profile.api_key_env];
  if (!key?.trim()) fail(`Missing model credential environment variable: ${profile.api_key_env}`);
  return {
    MIDSCENE_MODEL_NAME: profile.model,
    MIDSCENE_MODEL_FAMILY: profile.model_family,
    MIDSCENE_MODEL_BASE_URL: profile.base_url,
    MIDSCENE_MODEL_API_KEY: key,
    MIDSCENE_MODEL_AUTH_MODE: 'api-key',
    MIDSCENE_MODEL_PROTOCOL: 'openai-chat',
    MIDSCENE_MODEL_REASONING_ENABLED: 'false',
    MIDSCENE_MODEL_RETRY_COUNT: '0',
    MIDSCENE_MODEL_STREAM_MODE: 'non-stream',
  };
}
