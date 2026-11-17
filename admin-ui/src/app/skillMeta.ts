/** 状态与来源的展示口径。列表与抽屉共用，避免两处各写一套中文。 */

export interface StatusMeta {
  label: string;
  className: string;
  hint: string;
}

const STATUS_META: Record<string, StatusMeta> = {
  active: {
    label: '生效中',
    className: 'badge badge-ok',
    hint: '参与工具匹配，下一轮对话起可用',
  },
  pending: {
    label: '待确认',
    className: 'badge badge-info',
    hint: '已落盘但尚未生效；通过后才进工具表与 prompt',
  },
  disabled: {
    label: '已停用',
    className: 'badge badge-muted',
    hint: '不参与匹配；内容与历史都还在',
  },
  stale: {
    label: '依赖缺失',
    className: 'badge badge-danger',
    hint: '依赖的工具已不存在，不能通过新提案',
  },
  unavailable: {
    label: '不可用',
    className: 'badge badge-warn',
    hint: '解析或依赖检查未通过',
  },
};

export function statusMeta(status: string | undefined): StatusMeta {
  return (
    (status ? STATUS_META[status] : undefined) ?? {
      label: status && status.length > 0 ? status : '未知',
      className: 'badge badge-muted',
      hint: '',
    }
  );
}

/** `meta.origin`：`proposal` 是自动提案，`install`/`manual` 都是人放进来的。 */
export function originLabel(origin: string | undefined): string {
  switch (origin) {
    case 'proposal':
      return '自动提案';
    case 'install':
    case 'manual':
      return '手动安装';
    default:
      return origin && origin.length > 0 ? origin : '未知';
  }
}
