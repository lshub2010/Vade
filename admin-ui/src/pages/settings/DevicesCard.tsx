import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactElement } from 'react';
import { api, describeError } from '../../app/api';
import { ConfirmDialog } from '../../app/ConfirmDialog';
import { formatCountdown, formatFingerprint, relativeTime, absoluteTime } from '../../app/format';
import type { AdminDevice, BindTicket, DeviceListResponse } from '../../app/types';
import { StepUpDialog } from './StepUpDialog';

const POLL_INTERVAL_MS = 3_000;
const POLL_WINDOW_MS = 120_000;

type PollState = 'idle' | 'polling' | 'found' | 'timeout';

interface TicketState {
  ticket: BindTicket;
  /** 取票那一刻的设备 id；之后出现的新 id 就是刚绑上的那台。 */
  baseline: string[];
}

/** 已绑定设备 + step-up 添加（绑定票 / OpenSSH 公钥）+ 吊销。 */
export function DevicesCard({ readOnly }: { readOnly: boolean }): ReactElement {
  const [items, setItems] = useState<AdminDevice[]>([]);
  const [max, setMax] = useState(5);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [flash, setFlash] = useState('');

  const [stepUp, setStepUp] = useState<'ticket' | 'pubkey' | null>(null);
  const [stepBusy, setStepBusy] = useState(false);
  const [stepError, setStepError] = useState('');

  const [ticketState, setTicketState] = useState<TicketState | null>(null);
  const [poll, setPoll] = useState<PollState>('idle');
  const [foundLabel, setFoundLabel] = useState('');
  const [now, setNow] = useState(() => Date.now());
  const [copied, setCopied] = useState(false);

  const [pubkeyText, setPubkeyText] = useState('');
  const [pubkeyLabel, setPubkeyLabel] = useState('');
  const [pubkeyError, setPubkeyError] = useState('');

  const [revokeTarget, setRevokeTarget] = useState<AdminDevice | null>(null);
  const [revokeBusy, setRevokeBusy] = useState(false);
  const [revokeError, setRevokeError] = useState('');

  const load = useCallback(async (): Promise<void> => {
    try {
      const result = await api<DeviceListResponse>('/admin/api/devices');
      setItems(result.items);
      setMax(typeof result.max === 'number' && result.max > 0 ? result.max : 5);
      setLoadError('');
    } catch (cause) {
      setLoadError(describeError(cause));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 倒计时：绑定票 10 分钟、自动检查 2 分钟。
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    if (!flash) return;
    const id = window.setTimeout(() => setFlash(''), 4_000);
    return () => window.clearTimeout(id);
  }, [flash]);

  const atLimit = items.length >= max;
  const remainingMs = ticketState ? ticketState.ticket.expires_at * 1000 - now : 0;

  const checkOnce = useCallback(async (): Promise<void> => {
    if (!ticketState) return;
    try {
      const result = await api<DeviceListResponse>('/admin/api/devices');
      setItems(result.items);
      setMax(result.max);
      const fresh = result.items.find((device) => !ticketState.baseline.includes(device.device_id));
      if (fresh) {
        setPoll('found');
        setFoundLabel(fresh.label);
        setFlash(`新设备已绑定：${fresh.label}`);
      }
    } catch {
      // 轮询失败不打扰用户：下一轮继续，绑定票本身不受影响。
    }
  }, [ticketState]);

  // 每 3 秒查一次，最多 2 分钟；发现新条目就停。
  useEffect(() => {
    if (!ticketState || poll !== 'polling') return;
    const startedAt = Date.now();
    let cancelled = false;
    const id = window.setInterval(() => {
      if (cancelled) return;
      if (Date.now() - startedAt > POLL_WINDOW_MS) {
        setPoll('timeout');
        return;
      }
      void checkOnce();
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [ticketState, poll, checkOnce]);

  const closeTicket = (): void => {
    setTicketState(null);
    setPoll('idle');
    setFoundLabel('');
    setCopied(false);
  };

  const openTicketStepUp = (): void => {
    setStepError('');
    setStepUp('ticket');
  };

  const submitStepUp = async (password: string): Promise<void> => {
    setStepBusy(true);
    setStepError('');
    try {
      if (stepUp === 'ticket') {
        const ticket = await api<BindTicket>('/admin/api/devices/ticket', {
          method: 'POST',
          body: { password },
          // 密码错回 401：不能让全局监听把当前会话踢掉。
          quietAuthFailure: true,
        });
        setTicketState({ ticket, baseline: items.map((device) => device.device_id) });
        setPoll('polling');
        setFoundLabel('');
        setStepUp(null);
        setNow(Date.now());
        return;
      }

      const parsed = parseOpenSsh(pubkeyText);
      if (!parsed.ok) {
        setStepError(parsed.error);
        return;
      }
      await api<AdminDevice>('/admin/api/devices', {
        method: 'POST',
        body: {
          password,
          pubkey: parsed.line,
          alg: 'ed25519',
          label: (pubkeyLabel.trim() || parsed.label || '开发者设备').slice(0, 64),
          hardware_backed: false,
        },
        quietAuthFailure: true,
      });
      setStepUp(null);
      setPubkeyText('');
      setPubkeyLabel('');
      setPubkeyError('');
      setFlash('已登记新设备。软件密钥会被标注「可被提取」。');
      await load();
    } catch (cause) {
      setStepError(stepUpMessage(cause));
    } finally {
      setStepBusy(false);
    }
  };

  const submitPubkey = (): void => {
    const parsed = parseOpenSsh(pubkeyText);
    if (!parsed.ok) {
      setPubkeyError(parsed.error);
      return;
    }
    setPubkeyError('');
    setStepError('');
    setStepUp('pubkey');
  };

  const revoke = async (): Promise<void> => {
    if (!revokeTarget) return;
    setRevokeBusy(true);
    setRevokeError('');
    try {
      await api<{ ok: boolean }>(
        `/admin/api/devices/${encodeURIComponent(revokeTarget.device_id)}/revoke`,
        { method: 'POST', body: {} },
      );
      setFlash(`已吊销「${revokeTarget.label}」`);
      setRevokeTarget(null);
      await load();
    } catch (cause) {
      setRevokeError(describeError(cause));
    } finally {
      setRevokeBusy(false);
    }
  };

  const copyUrl = async (url: string): Promise<void> => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2_000);
    } catch {
      setFlash('复制失败：请手动选择地址复制');
    }
  };

  const ticketBlock = useMemo(() => {
    if (!ticketState) return null;
    const url = ticketState.ticket.bind_url;
    const seconds = Math.max(0, Math.floor(remainingMs / 1000));
    return (
      <div className="ticket-panel" role="status" aria-live="polite">
        <div className="ticket-head">
          <h3>等待新设备完成绑定</h3>
          <span className={seconds <= 0 ? 'badge badge-danger' : 'badge badge-info'}>
            {seconds <= 0 ? '已过期' : `剩 ${formatCountdown(seconds)}`}
          </span>
        </div>

        {seconds <= 0 ? (
          <p className="alert alert-warn">
            绑定票已过期（有效期 10 分钟）。请重新点「添加设备」获取新的绑定票 ——
            过期的地址扫了也只会 401。
          </p>
        ) : (
          <>
            <p className="hint">
              在 Vade App 的「添加设备」里扫码，或用手机直接打开下面的地址完成绑定。
              二维码内容就是这个地址，本页不额外画图以免引入第三方依赖。
            </p>
            <div className="bind-url">
              <code>{url}</code>
              <button type="button" className="Button Button-sm" onClick={() => void copyUrl(url)}>
                {copied ? '已复制' : '复制链接'}
              </button>
            </div>
          </>
        )}

        {poll === 'found' ? (
          <p className="notice">已绑定：{foundLabel || '新设备'}。列表已刷新。</p>
        ) : null}
        {poll === 'polling' ? (
          <p className="hint">每 3 秒自动检查一次设备列表，最多 2 分钟；不用手动刷新。</p>
        ) : null}
        {poll === 'timeout' ? (
          <p className="hint">
            已停止自动检查（满 2 分钟）。绑定票在 10 分钟内仍然有效，可以点「检查一次」。
          </p>
        ) : null}

        <div className="card-actions">
          {poll === 'timeout' ? (
            <button type="button" className="Button Button-sm" onClick={() => void checkOnce()}>
              检查一次
            </button>
          ) : null}
          <button type="button" className="Button Button-sm Button-ghost" onClick={closeTicket}>
            关闭
          </button>
        </div>
      </div>
    );
  }, [ticketState, remainingMs, poll, foundLabel, copied, checkOnce]);

  return (
    <section className="card" aria-labelledby="settings-devices">
      <header className="card-head">
        <div>
          <h2 id="settings-devices">已绑定设备</h2>
          <p className="card-sub">
            {items.length} / {max} 台 · 到上限要先移除一台不用的设备 · 只显示 fp8，不显示公钥原文
          </p>
        </div>
        <div className="card-head-actions">
          <button type="button" className="Button Button-sm Button-ghost" onClick={() => void load()} disabled={loading}>
            {loading ? '刷新中…' : '刷新'}
          </button>
          {readOnly ? null : (
            <button
              type="button"
              className="Button Button-sm Button-primary"
              onClick={openTicketStepUp}
              disabled={atLimit}
              title={atLimit ? '先移除一台不用的设备' : undefined}
            >
              添加设备
            </button>
          )}
        </div>
      </header>

      {readOnly ? (
        <p className="notice notice-readonly">
          浏览器只读会话：设备列表可以看，登记与吊销按钮不渲染。要添加设备请用 Vade App 打开管理面。
        </p>
      ) : null}

      {atLimit && !readOnly ? (
        <p className="hint">已达 {max} 台上限：先移除一台不用的设备，再添加新的（不会自动淘汰）。</p>
      ) : null}

      {loadError ? (
        <div className="alert" role="alert">
          <span>读取设备列表失败：{loadError}</span>
          <button type="button" className="Button Button-sm" onClick={() => void load()}>
            重试
          </button>
        </div>
      ) : null}

      {loading && items.length === 0 && !loadError ? <p className="muted">正在读取设备…</p> : null}

      {!loading && items.length === 0 && !loadError ? (
        <div className="empty">
          <p>还没有登记设备。手机上的 Vade App 通过绑定票登记后，会出现在这里。</p>
        </div>
      ) : null}

      <ul className="device-list">
        {items.map((device) => (
          <li className="device-item" key={device.device_id}>
            <div className="device-head">
              <div className="device-title">
                <p className="device-label">{device.label}</p>
                <code className="device-fp8" title="core 身份指纹之外的第二把钥匙：设备公钥指纹">
                  {formatFingerprint(device.fp8)}
                </code>
              </div>
              <div className="device-badges">
                <span className={device.alg === 'ed25519' ? 'badge badge-warn' : 'badge badge-info'}>
                  {device.alg}
                </span>
                {device.hardware_backed ? (
                  <span className="badge badge-ok">硬件密钥</span>
                ) : (
                  <span className="badge badge-warn">软件密钥，可被提取</span>
                )}
              </div>
            </div>

            <dl className="meta-grid">
              <div>
                <dt>添加时间</dt>
                <dd title={absoluteTime(device.added_at)}>{relativeTime(device.added_at)}</dd>
              </div>
              <div>
                <dt>来源 IP</dt>
                <dd>{device.added_from_ip ?? '未记录'}</dd>
              </div>
              <div>
                <dt>最后使用</dt>
                <dd title={absoluteTime(device.last_used_at)}>{relativeTime(device.last_used_at)}</dd>
              </div>
            </dl>

            {device.hardware_backed ? (
              <p className="hint">
                硬件背书是设备自述（<code>attested=false</code>），core 无法验证 —— 别把它当授权依据。
              </p>
            ) : null}

            {readOnly ? null : (
              <div className="card-actions">
                <button
                  type="button"
                  className="Button Button-sm Button-danger-ghost"
                  onClick={() => {
                    setRevokeError('');
                    setRevokeTarget(device);
                  }}
                >
                  吊销
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>

      {ticketBlock}

      {readOnly ? null : (
        <details className="alt-add">
          <summary>加一台设备的备用方式（开发者）</summary>
          <div className="alt-add-body">
            <p className="hint">
              只接受 OpenSSH 一行 <code>ssh-ed25519 AAAA... label</code>。这样登记的是软件密钥，
              会被标成「可被提取」；手机等日常设备请走上面的绑定票。
            </p>
            <label className="field">
              <span className="field-label">公钥</span>
              <textarea
                className="input textarea"
                rows={3}
                spellCheck={false}
                placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... dev@laptop"
                value={pubkeyText}
                onChange={(event) => setPubkeyText(event.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">标签（可选）</span>
              <input
                className="input"
                maxLength={64}
                placeholder="例如：dev-laptop"
                value={pubkeyLabel}
                onChange={(event) => setPubkeyLabel(event.target.value)}
              />
            </label>
            <p className="form-error" role="alert">
              {pubkeyError}
            </p>
            <div className="card-actions">
              <button
                type="button"
                className="Button Button-sm"
                onClick={submitPubkey}
                disabled={atLimit || pubkeyText.trim().length === 0}
              >
                用公钥登记
              </button>
            </div>
          </div>
        </details>
      )}

      {flash ? (
        <div className="toast" role="status">
          {flash}
        </div>
      ) : null}

      {stepUp ? (
        <StepUpDialog
          title={stepUp === 'ticket' ? '添加设备 · 确认密码' : '用公钥登记设备 · 确认密码'}
          lede={
            stepUp === 'ticket'
              ? '登记新设备是一次敏感操作：再输一次当前密码，然后会拿到一张 10 分钟有效的绑定票。'
              : '再输一次当前密码。公钥本身不是凭据，但「谁能登记公钥」必须是已证明身份的人。'
          }
          confirmLabel={stepUp === 'ticket' ? '获取绑定票' : '登记设备'}
          busy={stepBusy}
          error={stepError}
          onSubmit={(password) => void submitStepUp(password)}
          onCancel={() => {
            if (stepBusy) return;
            setStepUp(null);
            setStepError('');
          }}
        />
      ) : null}

      {revokeTarget ? (
        <ConfirmDialog
          title="吊销这台设备？"
          confirmLabel="吊销设备"
          danger
          busy={revokeBusy}
          onConfirm={() => void revoke()}
          onCancel={() => {
            if (revokeBusy) return;
            setRevokeTarget(null);
            setRevokeError('');
          }}
        >
          <p>
            吊销「<strong>{revokeTarget.label}</strong>」（fp8 <code>{formatFingerprint(revokeTarget.fp8)}</code>）后，
            这台设备换发的所有会话立即失效，它下次请求会 401。当前这台设备不受影响；绑定关系解除后可重新登记。
          </p>
          {revokeError ? <p className="form-error">{revokeError}</p> : null}
        </ConfirmDialog>
      ) : null}
    </section>
  );
}

function stepUpMessage(cause: unknown): string {
  const message = describeError(cause);
  if (message === '密码或设备不正确') return '当前密码不正确';
  if (message === '目标不存在，可能已被移除') return '设备不存在，可能已被吊销';
  return message.length > 0 ? message : '操作失败，请重试';
}

/** 只接受 OpenSSH 一行；返回原始行交给后端解析（后端支持 ssh-ed25519 一行）。 */
export function parseOpenSsh(
  text: string,
): { ok: true; line: string; label: string } | { ok: false; error: string } {
  const line = text.trim().replace(/\s+/g, ' ');
  if (line.length === 0) return { ok: false, error: '请粘贴公钥' };
  const parts = line.split(' ');
  const type = parts[0] ?? '';
  if (type !== 'ssh-ed25519') {
    if (type.startsWith('ssh-') || type.startsWith('ecdsa-')) {
      return { ok: false, error: '只接受 ssh-ed25519；P-256 设备密钥请在 App 里用绑定票登记' };
    }
    return { ok: false, error: '格式应为 ssh-ed25519 AAAA... [标签]' };
  }
  const body = parts[1] ?? '';
  if (!/^[A-Za-z0-9+/=]{20,}$/.test(body)) return { ok: false, error: '公钥主体不是合法 base64' };
  return { ok: true, line, label: parts.slice(2).join(' ') };
}
