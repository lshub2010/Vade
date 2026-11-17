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

export function SetupPage(): ReactElement {
  return (
    <section className="page" aria-labelledby="setup-title">
      <header className="page-head">
        <div>
          <h1 id="setup-title">首次配置</h1>
          <p className="muted">只在 core 返回 <code>setup_required=true</code> 时可达。</p>
        </div>
        <span className="badge badge-warn">本批未实现</span>
      </header>

      <div className="empty empty-left">
        <p>
          首次配置页在 C5a 的下一批交付。现在不渲染二维码，也不渲染密码表单 —— 顺序必须是
          <strong>先过闸、再给二维码</strong>，反了就等于「打开网页即可接管」。
        </p>
        <h2>实现时必须按这个顺序</h2>
        <ol className="planned-list">
          <li>非回环且没有 <code>__Host-ac_setup</code> 时，只渲染 setup token 输入框与取 token 的三条路径；提交 <code>POST /admin/setup/unlock</code>。</li>
          <li>解锁后才显示二维码（<code>bind_url</code>）、大字 <code>fp8</code> 与 10 分钟倒计时；过期就整块替换成「已过期，请重新解锁」。</li>
          <li>浏览器直配走折叠面板：setup token、密码 + 确认（不提供跳过）、可选设备公钥；失败时清空已填 token。</li>
        </ol>
        <p className="hint">
          已经有密码与会话时，请回到 <a href="#/login">登录</a>。不要在页面上写「推荐用 SSH 隧道」——
          SSH 已从用户路径移除（AMEND-17/A57）。
        </p>
      </div>
    </section>
  );
}
