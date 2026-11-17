/**
 * 技能安装区的纯函数：上限、摘要、正文头、错误文案。
 *
 * 这里**不解析 YAML**：frontmatter 只信 `/install/preview` 的返回值。读取本地
 * 文件只为两件事——算 sha256、显示正文前 30 行；正文头的切法只跳过开头的
 * `---` 块，不做字段解析。
 */

import { ApiError } from './api';
import { describeSkillError } from './skillError';

/** 设计口径（design §16.5 / FR-9.41）。zip 三项只用于说明，本批不提供 zip 上传。 */
export const INSTALL_LIMITS = {
  mdBytes: 64 * 1024,
  zipBytes: 1024 * 1024,
  unzippedBytes: 5 * 1024 * 1024,
  zipEntries: 32,
  previewTtlSeconds: 60,
  excerptLines: 30,
} as const;

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

/** 本地文件的 UTF-8 字节数，用来卡 64KB（`File.size` 已是字节，粘贴内容要自己算）。 */
export function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** `crypto.subtle` 在非安全上下文（http 页面）不存在；返回空串表示「算不了」。 */
export async function sha256Hex(text: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return '';
  const digest = await subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

export interface BodyHead {
  lines: string[];
  /** 原文还有更多行（被 30 行上限截断）。 */
  truncated: boolean;
}

/**
 * 正文前 N 行：跳过开头的 frontmatter 块，不解析字段。
 * 服务端摘录只有 400 字符；粘贴/上传时本地有全文，能真的给到 30 行。
 */
export function bodyHead(text: string, limit: number = INSTALL_LIMITS.excerptLines): BodyHead {
  let body = text;
  if (body.startsWith('---')) {
    const closing = body.indexOf('\n---', 3);
    if (closing !== -1) body = body.slice(closing + 4);
  }
  const lines = body.replace(/^\r?\n/, '').split(/\r?\n/);
  while (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  return { lines: lines.slice(0, limit), truncated: lines.length > limit };
}

/** 预检失败就地红字：把 core 的英文拒绝理由翻成能照着修的中文。 */
export function describeInstallError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403 && error.code === 'READ_ONLY_SESSION') {
      return '当前是只读会话：浏览器形态只能查看，安装与批准要在 Vade App 里做。';
    }
    if (error.status === 501) {
      return '本构建不含技能管理（501 UNSUPPORTED），安装接口不可用。';
    }
    if (error.status === 400 && error.code === 'INVALID_ARGUMENT') {
      return describeInvalidArgument(error.message);
    }
    if (error.status === 503 || error.code === 'UNAVAILABLE') {
      return `服务端暂时不可用：${error.message}`;
    }
    if (error.status === 409) {
      return `revision 冲突（${error.code}）：服务端状态和你看到的不一致，可能刚被其它设备处理。已重新读取，请核对最新版本。`;
    }
    if (error.status === 404) {
      return '目标不存在：这个 skill 可能已被卸载或移走。';
    }
  }
  return describeSkillError(error);
}

function describeInvalidArgument(message: string): string {
  const text = message.trim();
  if (text.includes('preview_token is required')) {
    return '缺少预检凭证：必须先在第一步预检，不能直接安装。';
  }
  if (text.includes('preview_token is unknown')) {
    return '预检凭证已过期或已被使用（一次性，60 秒有效）。请重新预检。';
  }
  if (text.includes('preview_token expired')) {
    return '预检已超过 60 秒，凭证过期。请重新预检。';
  }
  if (text.includes('preview_token does not match')) {
    return '预检凭证与内容不匹配：内容在预检后被改过，请重新预检。';
  }
  if (text.includes('skill URL must be https')) {
    return 'URL 必须是 https：http 会被服务端直接拒绝。';
  }
  if (text.includes('must not embed credentials')) {
    return 'URL 不能带用户名或密码。';
  }
  if (text.includes('private/loopback') || text.includes('link-local')) {
    return 'URL 指向回环 / 私网 / 链路本地地址，服务端按 SSRF 规则拒绝（云元数据地址也在内）。';
  }
  if (text.includes('does not resolve')) {
    return `域名解析失败：${text}`;
  }
  if (text.includes('too many redirects')) {
    return '重定向太多：最多 3 跳，且每一跳都会重新校验目标地址。';
  }
  if (text.includes('download failed')) {
    return `下载失败：${text.replace(/^skill download failed:\s*/, '')}。`;
  }
  if (text.includes('exceeds 1MB')) {
    return '下载内容超过 1MB 上限。';
  }
  if (text.includes('neither a zip nor UTF-8 markdown')) {
    return '下载到的内容既不是 zip 也不是 UTF-8 Markdown。';
  }
  if (text.includes('invalid skill')) {
    return `内容不合法：${text.replace(/^invalid skill:\s*/, '')}`;
  }
  if (text.startsWith('zip')) {
    return `zip 被拒绝：${text}`;
  }
  return `请求被拒绝：${text}`;
}

/** 批准（approve）的错误文案：409 要明确说「别再拿旧 revision 重试」。 */
export function describeApproveError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 409) {
      return 'revision 冲突（409）：这条待批准版本已被其它设备处理（可能已批准、驳回或被新安装替换）。列表已刷新，请回审核区核对最新 rev，不要再用旧 rev 重试。';
    }
    if (error.status === 404) {
      return '待批准版本不存在：可能已被批准或丢弃。列表已刷新，请回审核区确认。';
    }
    if (error.status === 403 && error.code === 'READ_ONLY_SESSION') {
      return '只读会话不能批准：请用 Vade App 打开管理面。';
    }
    if (error.status === 501) {
      return '本构建不含技能管理（501 UNSUPPORTED）。';
    }
  }
  return describeSkillError(error);
}
