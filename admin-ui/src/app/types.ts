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
