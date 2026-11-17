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
