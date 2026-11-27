/** 管理面契约类型（对齐 schema/admin-api.yaml · C-08）。只放白名单字段，不放 manifest / 公钥。 */

export type NodeState = 'online' | 'offline' | 'pending' | 'degraded' | 'rejected';

export type AuthMode = 'module_key' | 'device_key' | 'browser_session';

/** `GET /admin/api/nodes` 的 Node 视图（§7.5 字段白名单，additionalProperties: false）。 */
export interface NodeView {
  id: string;
  kind: 'module' | 'device';
  module_id: string;
  instance_id: string;
  label: string | null;
  capabilities: string[];
  state: NodeState | string;
  endpoint: string | null;
  protocol: string;
  version: string | null;
  auth_mode: AuthMode | string | null;
  rtt_ms: number | null;
  /** epoch 秒；不是毫秒。 */
  last_seen: number | null;
  x: number | null;
  y: number | null;
  revoked: boolean;
  tool_count: number;
  tool_names: string[];
}

export interface NodeListResponse {
  items: NodeView[];
}

export interface DeviceSummary {
  device_id: string;
  label: string;
  fp8: string;
  alg: 'p256' | 'ed25519';
}

export interface LoginChallenge {
  challenge_id: string;
  /** base64(32B)，由原生层签名，页面不碰。 */
  challenge: string;
  expires_at: number;
  devices: DeviceSummary[];
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  mode: 'device';
  /** Session-bound double-submit token: every non-GET must send it. */
  csrf_token?: string;
  /** What the refresh signature covers; native layer keeps it with the token. */
  refresh_jti?: string;
}

export interface BrowserSession {
  mode: 'browser';
  expires_in: number;
  csrf_token: string;
}

export type LoginResponse = TokenPair | BrowserSession;

export interface Impact {
  tool_count: number;
  tool_names: string[];
  sessions_affected: number;
  changes: Record<string, unknown>;
}

export interface NodePatchResponse {
  ok: boolean;
  id: string;
  impact: Impact;
  audit_id: string | null;
}

export interface NodeRevokeResponse {
  ok: boolean;
  connections_closed: number;
  audit_id: string | null;
}

export interface OkResponse {
  ok: boolean;
}

export interface HealthResponse {
  ok: true;
  version?: string;
  e2e_enabled?: boolean;
  /** 契约目前没有这个字段；core 若下发了身份指纹就顺手用。 */
  fp8?: string;
}

// ------------------------------------------------------------------- setup

/**
 * `GET /admin/setup`（schema#SetupState）。非回环且未解锁时**只有**
 * `setup_required` 与 `requires_token` —— 页面不能指望在这里拿到 `bind_url`。
 */
export interface SetupState {
  setup_required: boolean;
  requires_token?: boolean;
  bind_url?: string;
  fp8?: string;
  /** epoch 秒；绑定票有效期 10 分钟。 */
  expires_at?: number;
}

/** `POST /admin/setup/unlock`：解锁成功后才有二维码，cookie 由 core 下发。 */
export interface SetupUnlockResponse {
  bind_url: string;
  fp8: string;
  expires_at: number;
}

/** `POST /admin/setup`（浏览器直配）的响应。 */
export interface SetupInitializeResponse {
  ok: boolean;
  initialized?: boolean;
  device?: AdminDevice | null;
}


// ---------------------------------------------------------------- settings

/** `GET /admin/api/devices` 的一行（schema#Device）。**不含公钥原文**。 */
export interface AdminDevice {
  device_id: string;
  label: string;
  alg: 'p256' | 'ed25519' | string;
  fp8: string;
  hardware_backed: boolean;
  /** 恒 `false`：`hardware_backed` 是设备自述，core 无法验证（design §7.12.3 ③）。 */
  attested?: boolean;
  /** epoch 秒。 */
  added_at: number;
  added_from_ip: string | null;
  last_used_at: number | null;
}

export interface DeviceListResponse {
  items: AdminDevice[];
  /** 设备上限，当前是 5；到顶时页面把「添加设备」置灰。 */
  max: number;
}

/** `POST /admin/api/devices/ticket`：10 分钟一次性绑定票。 */
export interface BindTicket {
  bind_ticket: string;
  bind_url: string;
  /** epoch 秒。 */
  expires_at: number;
}

export type ConfigSource = 'default' | 'core.toml' | 'overrides' | 'env' | 'cli';

/** `GET /admin/api/config` 的一项（schema#ConfigItem）。 */
export interface ConfigItem {
  key: string;
  value: unknown;
  default: unknown;
  source: ConfigSource | string;
  mode: 'hot' | 'restart';
  min?: number;
  max?: number;
  values?: string[];
}

export interface ConfigListResponse {
  items: ConfigItem[];
}

/** `PUT /admin/api/config`。`applied` 与 `needs_restart` 互斥；失败也可能回 `error`。 */
export interface ConfigPutResponse {
  applied?: boolean;
  needs_restart?: boolean;
  audit_id?: string | null;
  error?: string;
}

export interface ConfigUsage {
  day: string;
  tokens_used: number;
  tokens_budget: number;
  reviews_today: number;
  proposals_today: number;
}

/** `POST /admin/api/password`：成功后会发一套新 token，当前设备不掉线。 */
export interface PasswordChangeResponse {
  ok: boolean;
  mode?: string;
  expires_in?: number;
  access_token?: string;
  refresh_token?: string;
}

/**
 * `GET /admin/api/audit` 的一行（schema#AuditRow）。
 *
 * 服务端已经脱敏：`args_redacted` / `extra` 里命中 `password|token|secret|key|
 * credential` 的字段值是 `"[redacted]"`。页面不再请求原文，也不猜。
 */
export interface AuditRow {
  /** 天表内的自增主键；跨天可重复，游标里与 `ts` 一起用。 */
  id?: number;
  /** epoch 秒（不是毫秒）。 */
  ts: number;
  kind: string;
  session_id?: string | null;
  turn_id?: string | null;
  module_id?: string | null;
  instance_id?: string | null;
  method?: string | null;
  tool?: string | null;
  duration_ms?: number | null;
  status?: string | null;
  code?: string | null;
  request_id?: string | null;
  /** 确认人：设备 id / `loopback_admin` / `system`。 */
  actor?: string | null;
  args_digest?: string | null;
  args_redacted?: Record<string, unknown> | null;
  extra?: Record<string, unknown> | null;
}

export interface AuditListResponse {
  items: AuditRow[];
  next_cursor: string | null;
}

// ---------------------------------------------------------------- skills

/** `SkillSummary.status`；`rejected` 是目录不是状态，不会出现。 */
export type SkillStatus = 'active' | 'pending' | 'disabled' | 'stale' | 'unavailable';

/** `GET /admin/api/skills` 的一行（schema#SkillSummary + core 实际下发的字段）。 */
export interface SkillSummary {
  name: string;
  description?: string;
  revision: number;
  status: SkillStatus | string;
  origin?: string;
  available?: boolean;
  unavailable_reason?: string;
  last_used_at?: number | null;
  fail_count?: number;
  derived_from?: string | null;
}

export interface SkillListResponse {
  items: SkillSummary[];
}

/** `.history/<name>/<rev>.md` 的索引项。只有名字与大小，没有内容。 */
export interface SkillHistoryEntry {
  revision: number;
  bytes: number;
}

/**
 * `POST /admin/api/skills/install/preview` 的响应。
 *
 * schema 只冻结了 `preview_token/name/frontmatter/excerpt/missing_tools/conflict`；
 * core 还下发 `valid`、`missing_sections`、`files`、`conflict_with`。后者按可选字段读，
 * 缺了也不影响预检展示（旧构建只是少几行提示）。
 */
export interface InstallPreview {
  /** 60 秒一次性；`valid=false` 时为空串。安装时必须原样带回。 */
  preview_token: string;
  name: string;
  frontmatter: Record<string, unknown>;
  /** 服务端截取的正文头部（当前实现 ≤400 字符），页面不再解析 YAML。 */
  excerpt: string;
  missing_tools: string[];
  conflict: boolean;
  /** `active` / `pending`，说明和哪种版本重名。 */
  conflict_with?: string | null;
  valid?: boolean;
  missing_sections?: string[];
  /** zip/dir 来源里的资源文件名；文本来源为空。 */
  files?: string[];
}

/** `GET /admin/api/skills/{name}`。diff 由后端算，页面不解析 YAML。 */
export interface SkillDetail extends SkillSummary {
  when_to_use?: string;
  triggers?: string[];
  requires_tools?: string[];
  body: string;
  /** 没有提案、或提案正文与当前版本没有差异时为 null。 */
  pending_diff: string | null;
  /** 有提案时是提案的 revision（approve/reject 的 CAS token）。 */
  pending_revision: number | null;
  history: SkillHistoryEntry[];
}

// ---------------------------------------------------------------- memory

export type MemoryLayer = 'session' | 'global';

/** `staging` 待确认、`active` 参与召回、`archived` 手动归档、`superseded` 被新版本替换。 */
export type MemoryStatus = 'staging' | 'active' | 'archived' | 'superseded';

/**
 * `GET /admin/api/memory` 的一行（schema#MemoryItem）。
 *
 * 契约只开放条目正文与指针，**不含** `user_text` / `assistant_text`：
 * 记忆是从对话里抽出来的摘要，页面不请求原始对话，也不猜。
 * `text` 是模型生成的文本，按纯文本渲染，不解析成 HTML/Markdown。
 */
export interface MemoryItem {
  id: string;
  text?: string;
  layer: MemoryLayer | string;
  namespace: string;
  status: MemoryStatus | string;
  session_id?: string | null;
  turn_id?: string | null;
  /** epoch 秒（不是毫秒）。 */
  created_at: number;
  /** epoch 秒；`null` = 不过期（全局层通常为 null）。 */
  expires_at?: number | null;
  hit_count?: number;
  /** 本条替换掉的旧条目 id；`GET /{id}` 才保证有链。 */
  supersedes?: string[];
}

export interface MemoryListResponse {
  items: MemoryItem[];
  /** 没有更多页时为 null；页面用它决定是否渲染「加载更多」。 */
  next_cursor: string | null;
}

/** `GET /admin/api/memory/stats`：配额与计数，不含任何正文。 */
export interface MemoryStats {
  global_count: number;
  global_limit: number;
  staging_count: number;
  archived_count: number;
  /** epoch 秒；空库时为 null。 */
  oldest_at: number | null;
}
