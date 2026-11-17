import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactElement } from 'react';
import { ApiError } from '../app/api';
import { formatCountdown } from '../app/format';
import { navigate } from '../app/router';
import { QrCode } from '../app/QrCode';
import {
  describeSetupError,
  initializeSetup,
  probeSetup,
  unlockSetup,
  validatePubkey,
} from '../app/setup';
import { Link } from '../app/Shell';

type Phase = 'loading' | 'redirect' | 'error' | 'locked' | 'ready' | 'done';

/** 二维码与指纹是同一份解锁产物：一起出现，一起撤掉。 */
interface Ticket {
  bind_url: string;
  fp8: string;
  /** epoch 秒。 */
  expires_at: number;
}

type CopyState = 'idle' | 'ok' | 'fail';

export function SetupPage(): ReactElement {
  const [phase, setPhase] = useState<Phase>('loading');
  const [loadError, setLoadError] = useState('');
  const [gate, setGate] = useState<'loopback' | 'unlocked'>('loopback');
  const [ticket, setTicket] = useState<Ticket | null>(null);

  const [unlockToken, setUnlockToken] = useState('');
  const [unlockError, setUnlockError] = useState('');
  const [unlockBusy, setUnlockBusy] = useState(false);
  /**
   * 解锁时输入的令牌只留在内存里。非回环的 `POST /admin/setup` 仍要求它，
   * 但页面上不再渲染这个字段（31-C5a §T5 `/setup` B①），提交时从这里取。
   */
  const unlockSecret = useRef('');

  const [nowMs, setNowMs] = useState(() => Date.now());
  const [copyState, setCopyState] = useState<CopyState>('idle');

  const [form, setForm] = useState({ token: '', password: '', confirm: '', pubkey: '' });
  const [formError, setFormError] = useState('');
  const [formBusy, setFormBusy] = useState(false);

  const load = useCallback(async (signal?: AbortSignal): Promise<void> => {
    setPhase('loading');
    setLoadError('');
    try {
      const probe = await probeSetup(signal);
      if (signal?.aborted) return;

      if (probe.kind === 'initialized') {
        setPhase('redirect');
        navigate('/login', { replace: true });
        return;
      }

      const state = probe.state;
      if (state.bind_url && state.fp8 && typeof state.expires_at === 'number') {
        // 回环：core 直接给了码，闸门本来就不适用于本机。
        setGate('loopback');
        setTicket({ bind_url: state.bind_url, fp8: state.fp8, expires_at: state.expires_at });
        setNowMs(Date.now());
        setPhase('ready');
        return;
      }

      // `requires_token`：只有令牌输入框，二维码与指纹都还不存在。
      setTicket(null);
      setPhase('locked');
    } catch (error) {
      if (signal?.aborted) return;
      if (error instanceof ApiError && error.status === 404) {
        setPhase('redirect');
        navigate('/login', { replace: true });
        return;
      }
      setLoadError(describeSetupError(error));
      setPhase('error');
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // 倒计时：每秒重算一次剩余秒数；到 0 由渲染层整块换成「已过期」。
  useEffect(() => {
    if (ticket === null) return;
    setNowMs(Date.now());
    const id = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, [ticket]);

  const remaining = ticket === null ? 0 : Math.max(0, Math.ceil((ticket.expires_at * 1_000 - nowMs) / 1_000));
  const expired = ticket !== null && remaining <= 0;

  /** 回到闸门：清掉内存里的令牌、二维码与指纹，等下一次解锁。 */
  const backToGate = useCallback((message = ''): void => {
    unlockSecret.current = '';
    setTicket(null);
    setGate('loopback');
    setUnlockToken('');
    setUnlockError(message);
    setNowMs(Date.now());
    setCopyState('idle');
    setPhase('locked');
  }, []);

  const submitUnlock = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (unlockBusy) return;
    const token = unlockToken.trim();
    if (token.length === 0) return;

    setUnlockBusy(true);
    setUnlockError('');
    try {
      const result = await unlockSetup(token);
      unlockSecret.current = token;
      setUnlockToken('');
      setGate('unlocked');
      setTicket({ bind_url: result.bind_url, fp8: result.fp8, expires_at: result.expires_at });
      setNowMs(Date.now());
      setPhase('ready');
    } catch (error) {
      // 失败不把已填的令牌留在 DOM 里：清空、重填。
      setUnlockToken('');
      if (error instanceof ApiError && error.status === 404) {
        setPhase('redirect');
        navigate('/login', { replace: true });
        return;
      }
      setUnlockError(describeSetupError(error));
    } finally {
      setUnlockBusy(false);
    }
  };

  const submitInitialize = async (event: FormEvent<HTMLFormElement>): Promise<void> => {
    event.preventDefault();
    if (formBusy) return;
    setFormError('');

    const password = form.password;
    const pubkey = form.pubkey.trim();
    const token = gate === 'unlocked' ? unlockSecret.current : form.token.trim();

    // 便宜的前置检查只为了避免白跑一次 Argon2id；规则以 core 为准。
    if (password.length < 12) {
      setFormError('密码至少 12 个字符。');
      return;
    }
    if (password !== form.confirm) {
      setFormError('两次输入的密码不一致。');
      return;
    }
    const pubkeyError = validatePubkey(pubkey);
    if (pubkeyError) {
      setFormError(pubkeyError);
      return;
    }
    if (gate === 'unlocked' && token.length === 0) {
      backToGate('解锁已过期，请重新解锁。');
      return;
    }

    setFormBusy(true);
    try {
      await initializeSetup({
        ...(token.length > 0 ? { setup_token: token } : {}),
        password,
        ...(pubkey.length > 0 ? { pubkey } : {}),
      });
      unlockSecret.current = '';
      setForm({ token: '', password: '', confirm: '', pubkey: '' });
      setTicket(null);
      setPhase('done');
    } catch (error) {
      // 失败后不回填令牌：可见的输入清空；非回环的提交令牌随之作废。
      setForm((previous) => ({ ...previous, token: '' }));
      if (error instanceof ApiError && error.status === 404) {
        setPhase('redirect');
        navigate('/login', { replace: true });
        return;
      }
      if (error instanceof ApiError && error.status === 401) {
        backToGate('访问令牌已失效，请重新解锁。');
        return;
      }
      setFormError(describeSetupError(error));
    } finally {
      setFormBusy(false);
    }
  };

  const copyBindUrl = async (): Promise<void> => {
    if (ticket === null) return;
    try {
      await navigator.clipboard.writeText(ticket.bind_url);
      setCopyState('ok');
      window.setTimeout(() => setCopyState('idle'), 2_000);
    } catch {
      setCopyState('fail');
    }
  };

  if (phase === 'loading' || phase === 'redirect') {
    return (
      <div className="setup-page">
        <div className="setup-shell">
          <Brand />
          <div className="boot" role="status" aria-live="polite">
            <span className="boot-mark" aria-hidden="true">
              V
            </span>
            <span className="muted">{phase === 'redirect' ? '正在跳转登录…' : '正在检查是否已初始化…'}</span>
          </div>
        </div>
      </div>
    );
  }

  if (phase === 'error') {
    return (
      <div className="setup-page">
        <div className="setup-shell">
          <Brand />
          <section className="setup-card" aria-labelledby="setup-error-title">
            <h1 id="setup-error-title">首次配置</h1>
            <p className="alert" role="alert">
              {loadError.length > 0 ? loadError : '无法读取 setup 状态。'}
            </p>
            <p className="hint">
              页面必须先确认「这台 core 还没初始化」才继续。确认不了时不会画二维码，也不会渲染密码表单。
            </p>
            <button type="button" className="Button Button-primary" onClick={() => void load()}>
              重试
            </button>
          </section>
        </div>
      </div>
    );
  }

  if (phase === 'done') {
    return (
      <div className="setup-page">
        <div className="setup-shell">
          <Brand />
          <section className="setup-card setup-done" aria-labelledby="setup-done-title">
            <h1 id="setup-done-title">已初始化</h1>
            <p className="notice" role="status">
              已初始化，请用刚才的密码登录。
            </p>
            <p className="hint">
              <code>data/setup.token</code> 已被删除，这个地址刷新后会直接跳到登录页。
            </p>
            <button type="button" className="Button Button-primary Button-block" onClick={() => navigate('/login')}>
              去登录
            </button>
          </section>
        </div>
      </div>
    );
  }

  if (phase === 'locked') {
    return (
      <div className="setup-page">
        <div className="setup-shell">
          <Brand />
          <section className="setup-card setup-gate" aria-labelledby="setup-gate-title">
            <header className="setup-head">
              <div>
                <h1 id="setup-gate-title">首次配置</h1>
                <p className="auth-lede">
                  这台 core 还没有管理员密码。先过访问令牌这一关，之后才会显示绑定二维码 ——
                  否则「打开网页就能接管」。
                </p>
              </div>
              <span className="badge badge-warn">待解锁</span>
            </header>

            <form className="auth-form" onSubmit={(event) => void submitUnlock(event)} noValidate>
              <label className="field">
                <span className="field-label">访问令牌</span>
                <input
                  className="input input-mono"
                  type="password"
                  name="setup_token"
                  autoComplete="off"
                  autoCapitalize="off"
                  autoCorrect="off"
                  spellCheck={false}
                  autoFocus
                  required
                  placeholder="acs_…"
                  value={unlockToken}
                  onChange={(event) => {
                    setUnlockToken(event.target.value);
                    if (unlockError.length > 0) setUnlockError('');
                  }}
                  aria-invalid={unlockError.length > 0}
                  aria-describedby="setup-token-shape"
                />
              </label>
              <p className="hint" id="setup-token-shape">
                以 <code>acs_</code> 开头、共 47 个字符；每次重启 core 都会重新生成。
              </p>

              <div className="form-error" role="alert" aria-live="assertive">
                {unlockError}
              </div>

              <button
                type="submit"
                className="Button Button-primary Button-block"
                disabled={unlockBusy || unlockToken.trim().length === 0}
              >
                {unlockBusy ? '正在验证…' : '解锁'}
              </button>
            </form>

            <section className="setup-paths" aria-labelledby="setup-paths-title">
              <h2 id="setup-paths-title">令牌在哪</h2>
              <ol>
                <li>
                  <span className="setup-path-label">Docker 部署</span>
                  <code>docker exec &lt;容器名&gt; cat /data/setup.token</code>
                </li>
                <li>
                  <span className="setup-path-label">本机安装</span>
                  <code>core admin print-setup-token</code>
                </li>
                <li>
                  <span className="setup-path-label">启动日志</span>
                  <span>
                    core 每次启动会打印 <code>setup token: …</code>（默认截断）；想固定值就在启动时设{' '}
                    <code>VADE_SETUP_TOKEN</code>。
                  </span>
                </li>
              </ol>
            </section>

            <p className="hint">
              已经配好了？<Link to="/login">去登录</Link>。
            </p>
          </section>
        </div>
      </div>
    );
  }

  // phase === 'ready'
  const groups = ticket === null ? [] : (ticket.fp8.match(/.{1,4}/g) ?? [ticket.fp8]);

  return (
    <div className="setup-page">
      <div className="setup-shell">
        <Brand />

        <section className="setup-card" aria-labelledby="setup-title">
          <header className="setup-head">
            <div>
              <h1 id="setup-title">首次配置</h1>
              <p className="auth-lede">
                手机扫码是主路径；浏览器里直接配是备用。两条路径都会设同一个管理员密码。
              </p>
            </div>
            <span className={expired ? 'badge badge-danger' : 'badge badge-info'}>
              {expired ? '已过期' : `剩 ${formatCountdown(remaining)}`}
            </span>
          </header>

          {expired || ticket === null ? (
            <section className="setup-expired" aria-labelledby="setup-expired-title">
              <h2 id="setup-expired-title">已过期，请重新解锁</h2>
              <p className="hint">
                绑定票只有 10 分钟。过期的二维码扫了只会 401，所以这里不再保留那张图。
              </p>
              <button
                type="button"
                className="Button Button-primary"
                onClick={() => {
                  if (gate === 'unlocked') {
                    backToGate('二维码已过期，请重新输入访问令牌。');
                  } else {
                    void load();
                  }
                }}
              >
                {gate === 'unlocked' ? '重新解锁' : '获取新的二维码'}
              </button>
            </section>
          ) : (
            <section className="setup-scan" aria-label="扫码绑定">
              <div className="setup-qr">
                <QrCode value={ticket.bind_url} label="Vade 绑定二维码" />
              </div>

              <div className="setup-scan-side">
                <h2>用 Vade App 扫码</h2>
                <p className="setup-scan-lede">用 Vade App 扫码，核对指纹一致后继续。</p>

                <div className="setup-fp" aria-label="core 身份指纹">
                  <div className="fingerprint-label">core 身份指纹 fp8</div>
                  <div className="setup-fp-value" aria-label={ticket.fp8}>
                    {groups.map((group, index) => (
                      <span className="setup-fp-group" key={`${group}-${index}`}>
                        {group}
                      </span>
                    ))}
                  </div>
                  <p className="fingerprint-hint">
                    与 App 里显示的指纹逐组核对。不一致就停手 —— 说明连到的不是这台 core。
                  </p>
                </div>

                <div className="setup-timer">
                  <div className="setup-timer-label">二维码有效期</div>
                  <div className="setup-timer-value" aria-hidden="true">
                    {formatCountdown(remaining)}
                  </div>
                  <p className="hint">到期后会自动撤掉二维码，需要重新解锁。</p>
                </div>

                <div className="bind-url">
                  <code>{ticket.bind_url}</code>
                  <button type="button" className="Button Button-sm" onClick={() => void copyBindUrl()}>
                    {copyState === 'ok' ? '已复制' : '复制链接'}
                  </button>
                </div>
                {copyState === 'fail' ? (
                  <p className="hint" role="status">
                    复制失败：请手动选中上面的地址。
                  </p>
                ) : null}
              </div>
            </section>
          )}

          <details className="setup-alt">
            <summary>在浏览器里直接配置（备用）</summary>
            <form className="auth-form setup-form" onSubmit={(event) => void submitInitialize(event)} noValidate>
              <p className="hint">
                不必扫码也能配：这里直接设管理员密码。浏览器没有设备私钥，配完请回登录页用密码进入。
              </p>

              {gate === 'loopback' ? (
                <label className="field">
                  <span className="field-label">访问令牌（本机回环可留空）</span>
                  <input
                    className="input input-mono"
                    type="password"
                    name="setup_token"
                    autoComplete="off"
                    autoCapitalize="off"
                    autoCorrect="off"
                    spellCheck={false}
                    placeholder="acs_…"
                    value={form.token}
                    onChange={(event) => setForm((previous) => ({ ...previous, token: event.target.value }))}
                  />
                  <span className="hint">
                    不是在本机打开这个页面时必填：<code>docker exec &lt;容器名&gt; cat /data/setup.token</code>
                  </span>
                </label>
              ) : (
                <p className="hint">
                  已解锁：提交时会带上刚才的访问令牌，不需要再填一次。
                </p>
              )}

              <label className="field">
                <span className="field-label">管理员密码</span>
                <input
                  className="input"
                  type="password"
                  name="password"
                  autoComplete="new-password"
                  required
                  minLength={12}
                  value={form.password}
                  onChange={(event) => setForm((previous) => ({ ...previous, password: event.target.value }))}
                />
                <span className="hint">至少 12 个字符，不能跳过；确认框必须一致。</span>
              </label>

              <label className="field">
                <span className="field-label">确认密码</span>
                <input
                  className="input"
                  type="password"
                  name="password_confirm"
                  autoComplete="new-password"
                  required
                  value={form.confirm}
                  onChange={(event) => setForm((previous) => ({ ...previous, confirm: event.target.value }))}
                />
              </label>

              <label className="field">
                <span className="field-label">设备公钥（可选）</span>
                <textarea
                  className="input input-mono setup-pubkey"
                  name="pubkey"
                  rows={3}
                  spellCheck={false}
                  placeholder="P-256 65 字节 base64（04 开头），或 ssh-ed25519 AAAA… label"
                  value={form.pubkey}
                  onChange={(event) => setForm((previous) => ({ ...previous, pubkey: event.target.value }))}
                />
                <span className="hint">
                  留空就只用密码登录；给开发者用时可以贴 OpenSSH 的一行公钥。
                </span>
              </label>

              <div className="form-error" role="alert" aria-live="assertive">
                {formError}
              </div>

              <button type="submit" className="Button Button-primary Button-block" disabled={formBusy}>
                {formBusy ? '正在初始化…' : '完成初始化'}
              </button>
            </form>
          </details>

          <footer className="auth-footer">
            <span>二维码与指纹只在解锁后出现</span>
            <Link to="/login">已有密码，去登录</Link>
          </footer>
        </section>
      </div>
    </div>
  );
}

function Brand(): ReactElement {
  return (
    <div className="auth-brand">
      <span className="brand-mark" aria-hidden="true">
        V
      </span>
      <span className="brand-name">Vade 管理面</span>
    </div>
  );
}
