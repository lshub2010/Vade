import { useEffect, useState } from 'react';
import type { ReactElement } from 'react';
import { api } from '../../app/api';
import { useAuth } from '../../app/auth';
import { formatConfigValue, metaOf, sourceLabel } from '../../app/configMeta';
import { formatFingerprint } from '../../app/format';
import { readHost } from '../../app/host';
import type { ConfigItem, HealthResponse } from '../../app/types';

/** 只读镜像里单列显示（顺序即 design §7.9 列的顺序）。 */
const MIRROR_KEYS = ['policy.approval_timeout_s', 'audit.retention_days', 'e2e.enabled'];

/**
 * 其余生效配置：只显示 `value + source`，**不给编辑框**（design §7.9）。
 *
 * 这一块回答的是「我改了 core.toml 怎么没生效」——所以来源比数值更重要。
 */
export function ReadOnlyCard({ items }: { items: ConfigItem[] }): ReactElement {
  const { fp8, mode } = useAuth();
  const [version, setVersion] = useState<string | null>(null);
  const injectedHost = readHost().adminHost ?? null;
  const pageHost = typeof window === 'undefined' ? '—' : window.location.host;

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const health = await api<HealthResponse>('/admin/api/health', {
          anonymous: true,
          quietAuthFailure: true,
        });
        if (!cancelled && typeof health.version === 'string' && health.version.length > 0) {
          setVersion(health.version);
        }
      } catch {
        // 远端只回 {ok:true}，版本本来就可能拿不到；不显示这一行即可。
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const byKey = new Map(items.map((item) => [item.key, item]));
  const mirror = MIRROR_KEYS.map((key) => byKey.get(key)).filter(
    (item): item is ConfigItem => item !== undefined,
  );
  const restartGroup = items.filter(
    (item) => item.mode === 'restart' && !MIRROR_KEYS.includes(item.key),
  );

  return (
    <section className="card" aria-labelledby="settings-readonly">
      <header className="card-head">
        <div>
          <h2 id="settings-readonly">其余生效配置（只读）</h2>
          <p className="card-sub">
            本页不写 <code>core.toml</code>：这里显示当前生效值与来源，用来核对「改了 toml 怎么没生效」。
            没有编辑框。
          </p>
        </div>
      </header>

      <div className="config-group">
        <h3 className="config-group-title">运行身份</h3>
        <div className="readonly-rows">
          <ReadOnlyRow
            label="监听地址（管理面）"
            value={injectedHost ?? pageHost}
            source={injectedHost ? '宿主注入' : '本页地址'}
            hint="core 没有通过 API 下发监听地址；这里显示本页实际连接的地址。Host 校验用的是 core 的 admin_host 白名单，本页能打开就说明当前地址在名单内。"
          />
          <ReadOnlyRow
            label="管理面 host"
            value={pageHost}
            source="本页地址"
            hint="浏览器把 Host 头带到这里；写操作要被接受，Host 必须命中白名单。"
          />
          <ReadOnlyRow
            label="core 身份指纹 fp8"
            value={fp8 ? formatFingerprint(fp8) : '未提供'}
            source={fp8 ? '/admin/api/health 或宿主注入' : '未提供'}
            mono
            hint="SHA-256(core_identity_pub)[:16]。与二维码 / core --print-fingerprint 一致才输入密码。"
          />
          <ReadOnlyRow label="会话形态" value={mode === 'webview' ? 'App 内 WebView' : '系统浏览器（只读）'} source="宿主注入" />
          {version ? <ReadOnlyRow label="core 版本" value={version} source="/admin/api/health" /> : null}
        </div>
      </div>

      {mirror.length > 0 ? (
        <div className="config-group">
          <h3 className="config-group-title">生效值（只读镜像）</h3>
          <div className="readonly-rows">
            {mirror.map((item) => (
              <ReadOnlyRow
                key={item.key}
                label={metaOf(item.key).label}
                value={formatConfigValue(item)}
                source={sourceLabel(item.source)}
                tag={item.mode === 'restart' ? '需重启' : '热生效'}
                hint={metaOf(item.key).hint}
                code={item.key}
              />
            ))}
          </div>
        </div>
      ) : null}

      <div className="config-group">
        <h3 className="config-group-title">需重启组</h3>
        {restartGroup.length === 0 ? (
          <p className="hint">本版本没有额外的「只读 + 需重启」项；需重启的项已在各自行内标注。</p>
        ) : (
          <div className="readonly-rows">
            {restartGroup.map((item) => (
              <ReadOnlyRow
                key={item.key}
                label={metaOf(item.key).label}
                value={formatConfigValue(item)}
                source={sourceLabel(item.source)}
                tag="需重启"
                hint={metaOf(item.key).hint}
                code={item.key}
              />
            ))}
          </div>
        )}
      </div>

      <p className="hint">
        密钥类（PSK、私钥、TLS 材料）一律不在管理面返回；这里看不到它们不是缺陷。
      </p>
    </section>
  );
}

function ReadOnlyRow({
  label,
  value,
  source,
  tag,
  hint,
  code,
  mono = false,
}: {
  label: string;
  value: string;
  source: string;
  tag?: string;
  hint?: string;
  code?: string;
  mono?: boolean;
}): ReactElement {
  return (
    <div className="readonly-row">
      <div className="readonly-main">
        <div className="config-label">
          <span className="config-name">{label}</span>
          {code ? <code className="config-key">{code}</code> : null}
          {tag ? <span className={tag === '需重启' ? 'badge badge-warn' : 'badge badge-muted'}>{tag}</span> : null}
        </div>
        {hint ? <p className="hint">{hint}</p> : null}
      </div>
      <dl className="triple">
        <div>
          <dt>值</dt>
          <dd className={mono ? 'mono' : undefined}>{value}</dd>
        </div>
        <div>
          <dt>来源</dt>
          <dd>{source}</dd>
        </div>
      </dl>
    </div>
  );
}
