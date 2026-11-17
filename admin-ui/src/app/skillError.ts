/**
 * `/skills` 的错误文案。
 *
 * 与 `describeError` 的差别只在「审核」这几种状态码要说得更具体：
 * 403 只读、409 revision 冲突、404 提案已被别人处理、501 构建不含此功能。
 * 其余一律交回通用翻译，避免两套措辞各自演化。
 */

import { ApiError, describeError } from './api';

export function describeSkillError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403 && error.code === 'READ_ONLY_SESSION') {
      return '当前是只读会话：浏览器形态只能查看，审批与回滚要在 Vade App 里做。';
    }
    if (error.status === 409 && error.code === 'TOOL_CONFLICT') {
      return 'revision 冲突：服务器上的提案和你看到的不是同一版（可能刚被其它设备重新提交）。已重新读取，请再核对一次 diff。';
    }
    if (error.status === 409) {
      return `操作冲突（${error.code}）：${error.message}`;
    }
    if (error.status === 404) {
      return '目标已不存在：提案可能已在其它设备上通过、驳回或被卸载。已重新读取。';
    }
    if (error.status === 400 && error.code === 'INVALID_ARGUMENT') {
      return `请求被拒绝：${error.message}`;
    }
    if (error.status === 501) {
      return '本构建不含技能管理（501 UNSUPPORTED）。';
    }
  }
  return describeError(error);
}
