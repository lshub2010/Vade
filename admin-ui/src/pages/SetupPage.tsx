import type { ReactElement } from 'react';

/**
 * `/setup` 仍是占位页：只写清闸门顺序，**不渲染二维码**。
 *
 * 顺序必须是「先过闸、再给二维码」：反了就等于「打开网页即可接管」。
 * 本页不渲染任何假控件 —— 能点但不生效比没有更糟。
 */
export function SetupPage(): ReactElement {
  return (
    <section className="page" aria-labelledby="setup-title">
      <header className="page-head">
        <div>
          <h1 id="setup-title">首次配置</h1>
          <p className="muted">
            只在 core 返回 <code>setup_required=true</code> 时可达。
          </p>
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
          <li>
            非回环且没有 <code>__Host-ac_setup</code> 时，只渲染 setup token 输入框与取 token 的三条路径；
            提交 <code>POST /admin/setup/unlock</code>。
          </li>
          <li>
            解锁后才显示二维码（<code>bind_url</code>）、大字 <code>fp8</code> 与 10 分钟倒计时；
            过期就整块替换成「已过期，请重新解锁」。
          </li>
          <li>
            浏览器直配走折叠面板：setup token、密码 + 确认（不提供跳过）、可选设备公钥；
            失败时清空已填 token。
          </li>
        </ol>
        <p className="hint">
          已经有密码与会话时，请回到 <a href="#/login">登录</a>。不要在页面上写「推荐用 SSH 隧道」——
          SSH 已从用户路径移除（AMEND-17/A57）。
        </p>
      </div>
    </section>
  );
}
