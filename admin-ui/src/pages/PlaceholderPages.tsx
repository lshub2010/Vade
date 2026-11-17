import type { ReactElement } from 'react';

export interface PlaceholderPageProps {
  title: string;
  summary: string;
  planned: string[];
  spec: string;
}

/** 尚未实现的页面：写清「没做」和「下一批做什么」，不摆假控件。 */
export function PlaceholderPage({ title, summary, planned, spec }: PlaceholderPageProps): ReactElement {
  return (
    <section className="page" aria-labelledby="placeholder-title">
      <header className="page-head">
        <div>
          <h1 id="placeholder-title">{title}</h1>
          <p className="muted">{summary}</p>
        </div>
        <span className="badge badge-warn">本批未实现</span>
      </header>

      <div className="empty empty-left">
        <p>
          这一页在 C5a 的后续批次交付。当前不渲染任何假控件 —— 能点但不生效比没有更糟。
        </p>
        <h2>计划内容</h2>
        <ul className="planned-list">
          {planned.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
        <p className="hint">
          契约与验收口径见 <code>{spec}</code>。
        </p>
      </div>
    </section>
  );
}

export function AuditPage(): ReactElement {
  return (
    <PlaceholderPage
      title="审计"
      summary="只追加的审计流：时间、模块、方法、耗时、状态、code。"
      spec="schema/admin-api.yaml#/admin/api/audit"
      planned={[
        '按 turn_id 过滤，分页游标，不做「加载全部」',
        '不显示参数原文，只显示 digest 与脱敏值',
        '与 /admin/api/stream 的事件编号对齐',
      ]}
    />
  );
}

