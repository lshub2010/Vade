/**
 * 首次配置（`/setup`）的三段契约 —— 顺序是安全边界，不是排版偏好。
 *
 * 0. `POST /admin/setup/unlock` 用 `setup_token` 换 `__Host-ac_setup`；
 *    在此之前页面拿不到 `bind_url` / `fp8`，也不该画二维码。
 * 1. 手机扫码：`bind_url` 里带 26 字符一次性绑定票（10 分钟）。
 * 2. 浏览器直配：`POST /admin/setup`，非回环必须再带 `setup_token`。
 *
 * `GET /admin/setup` 在已初始化时回 **302 `/login`**。这里不能用 `api()`：
 * 它把 `redirect` 设成 `'error'`，302 会变成 TypeError，页面会把「已初始化」
 * 误报成「连不上 core」。所以探测单独走 `redirect: 'manual'`。
 */

import { ApiError, api, describeError } from './api';
import type { SetupInitializeResponse, SetupState, SetupUnlockResponse } from './types';

/** `acs_` + base64url(32B) = 47 字符（core/vade/admin/setup_token.py）。 */
export const SETUP_TOKEN_PREFIX = 'acs_';
export const SETUP_TOKEN_LENGTH = 47;

/** `GET /admin/setup` 的两种结果：还没初始化，或者已经初始化该去登录。 */
export type SetupProbe = { kind: 'initialized' } | { kind: 'state'; state: SetupState };

function redirectLike(response: Response): boolean {
  // `redirect:'manual'` 下浏览器给的是 opaqueredirect（status 0）；
  // 某些实现会把 3xx 原样暴露出来，两种都认。
  return response.type === 'opaqueredirect' || response.status === 0
    || (response.status >= 300 && response.status < 400);
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text.length === 0) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return null;
  }
}

function codeOf(payload: unknown): string | null {
  if (payload && typeof payload === 'object' && 'code' in payload) {
    const code = (payload as { code?: unknown }).code;
    if (typeof code === 'string' && code.length > 0) return code;
  }
  return null;
}

function messageOf(payload: unknown): string | null {
  if (payload && typeof payload === 'object' && 'message' in payload) {
    const message = (payload as { message?: unknown }).message;
    if (typeof message === 'string' && message.length > 0) return message;
  }
  return null;
}

/**
 * 探一次 `GET /admin/setup`。
 *
 * 返回 `initialized` 表示 core 已经配好了（302 `/login`，或 `setup_required=false`），
 * 调用方必须跳登录页 —— 已初始化的管理面不该留任何 setup 入口。
 */
export async function probeSetup(signal?: AbortSignal): Promise<SetupProbe> {
  const response = await fetch('/admin/setup', {
    method: 'GET',
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'manual',
    signal,
  });

  if (redirectLike(response)) return { kind: 'initialized' };

  const payload = await readJson(response);
  if (!response.ok) {
    throw new ApiError(
      response.status,
      codeOf(payload) ?? `HTTP_${response.status}`,
      messageOf(payload) ?? `请求失败（HTTP ${response.status}）`,
    );
  }

  const state = (payload ?? {}) as SetupState;
  if (state.setup_required !== true) return { kind: 'initialized' };
  return { kind: 'state', state };
}

/** 闸门：`setup_token` → `{bind_url, fp8, expires_at}` + `__Host-ac_setup`。 */
export async function unlockSetup(setupToken: string): Promise<SetupUnlockResponse> {
  return api<SetupUnlockResponse>('/admin/setup/unlock', {
    method: 'POST',
    body: { setup_token: setupToken },
    anonymous: true,
    quietAuthFailure: true,
  });
}

export interface SetupInitializeBody {
  /** 回环可省略；非回环必填（解锁后的页面在内存里带着，不渲染成表单字段）。 */
  setup_token?: string;
  password: string;
  /** 可选：P-256 65 字节 base64，或 `ssh-ed25519 AAAA… label` 一行。 */
  pubkey?: string;
}

/** 电脑路径：写 `admin.json` → 删 `setup.token` → 本页 thereafter 不可达。 */
export async function initializeSetup(body: SetupInitializeBody): Promise<SetupInitializeResponse> {
  return api<SetupInitializeResponse>('/admin/setup', {
    method: 'POST',
    body,
    anonymous: true,
    quietAuthFailure: true,
  });
}

/** 形状检查只用于提示，不拦提交：粘进来的令牌可能带空格，服务端说了算。 */
export function hasSetupTokenShape(value: string): boolean {
  return new RegExp(`^${SETUP_TOKEN_PREFIX}[A-Za-z0-9_-]{${SETUP_TOKEN_LENGTH - SETUP_TOKEN_PREFIX.length}}$`).test(
    value.trim(),
  );
}

const INVALID_ARGUMENT_TEXT: Array<[needle: string, text: string]> = [
  ['at least 12', '密码至少 12 个字符。'],
  ['too long', '密码太长了（上限 256 个字符）。'],
  ['common-password', '这个密码太常见，请换一个。'],
  ['too few distinct', '密码里的不同字符太少，请换一个。'],
  ['public key is empty', '设备公钥是空的。'],
  ['not valid base64', '设备公钥不是合法 base64。'],
  ['p256 public key', 'P-256 公钥必须是 65 字节、以 04 开头的未压缩点。'],
  ['ed25519 public key', 'Ed25519 公钥必须是 32 字节原始公钥。'],
  ['cannot parse public key', '设备公钥解析失败：请贴 `ssh-ed25519 AAAA… label` 一行，或 65 字节 base64。'],
  ['unsupported key type', '不支持这种公钥类型：只接受 P-256 或 Ed25519。'],
];

/** 把 setup 的失败翻译成人话；401 一律是「令牌不对」，不是「密码不对」。 */
export function describeSetupError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'SETUP_UNAUTHORIZED') {
      return '访问令牌不正确。令牌每次重启 core 都会重新生成，请重新取一次。';
    }
    if (error.code === 'ADMIN_LOCKED' || error.status === 429) {
      return '尝试次数过多，请稍后再试。';
    }
    if (error.status === 404) return '管理面已经初始化，请直接登录。';
    if (error.code === 'INVALID_ARGUMENT') {
      const hit = INVALID_ARGUMENT_TEXT.find(([needle]) => error.message.includes(needle));
      if (hit) return hit[1];
      return error.message;
    }
  }
  return describeError(error);
}

/**
 * 客户端只做形状检查，解析仍归 core（`normalize_pubkey`）：
 * - `ssh-ed25519 AAAA… label` 一行；
 * - P-256 未压缩点 base64（65 字节，04 开头）——开发者手动填的形态；
 * - Ed25519 原始 32 字节 base64（长度够短，也放行）。
 *
 * 返回 `null` 表示「看起来没问题或留空」，否则返回一句给用户看的话。
 */
export function validatePubkey(text: string): string | null {
  const value = text.trim();
  if (value.length === 0) return null;

  if (value.startsWith('ssh-ed25519') || value.startsWith('-----BEGIN')) {
    if (value.startsWith('ssh-ed25519') && value.split(/\s+/).length < 2) {
      return 'ssh-ed25519 公钥应形如「ssh-ed25519 AAAA… 备注」，贴完整一行。';
    }
    return null;
  }

  const compact = value.replace(/\s+/g, '');
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(compact)) {
    return '设备公钥不是合法 base64：贴 P-256 公钥（65 字节）或 ssh-ed25519 一行。';
  }
  try {
    const padded = compact + '='.repeat((4 - (compact.length % 4)) % 4);
    const raw = atob(padded);
    const bytes = Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
    if (bytes.length === 65 && bytes[0] === 0x04) return null;
    if (bytes.length === 32) return null;
    return `公钥解出 ${bytes.length} 字节：P-256 需要 65 字节（04 开头），Ed25519 需要 32 字节。`;
  } catch {
    return '设备公钥不是合法 base64。';
  }
}
