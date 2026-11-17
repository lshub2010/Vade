import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, ReactElement } from 'react';
import { ApiError, api } from '../app/api';
import { ConfirmDialog } from '../app/ConfirmDialog';
import {
  INSTALL_LIMITS,
  bodyHead,
  describeApproveError,
  describeInstallError,
  formatBytes,
  sha256Hex,
  utf8Bytes,
} from '../app/install';
import type { InstallPreview, SkillSummary } from '../app/types';

type SourceKind = 'upload' | 'url';

interface InstalledState {
  summary: SkillSummary;
  approved: boolean;
  /** approve 收到 409/404：旧 revision 已作废，不能再拿它重试。 */
  conflict: boolean;
}

export interface SkillInstallPanelProps {
  readOnly: boolean;
  /** 列表接口回 501：这个构建没有 SkillLoader，安装区不渲染控件。 */
  unsupported: boolean;
  /** 安装/批准成功后让列表刷新；空字符串表示只刷新不提示。 */
  onChanged: (message: string) => void;
  /** 「在审核区查看」：切回审核 tab 并打开同名抽屉。 */
  onReview: (name: string) => void;
}

function asText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === null || value === undefined) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return '';
  }
}

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => asText(item)).filter((item) => item.length > 0);
}

function checkHttpsUrl(value: string): string {
  const trimmed = value.trim();
  if (trimmed.length === 0) return '';
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return '不是合法的 URL。要形如 https://example.com/SKILL.md。';
  }
  if (parsed.protocol !== 'https:') return '只接受 https URL：http 会被服务端拒绝。';
  if (parsed.username.length > 0 || parsed.password.length > 0) {
    return 'URL 不能带用户名或密码。';
  }
  return '';
}

const STEPS = ['预检', '安装到待批准', '批准生效'] as const;

/** 三步进度条：stage=4 表示三步都完成。 */
function StageBar({ stage }: { stage: number }): ReactElement {
  return (
    <ol className="install-steps" aria-label="安装三步">
      {STEPS.map((label, index) => {
        const position = index + 1;
        const state = position < stage ? 'done' : position === stage ? 'current' : 'todo';
        return (
          <li
            key={label}
            className={`install-step install-step-${state}`}
            aria-current={state === 'current' ? 'step' : undefined}
          >
            <span className="install-step-no" aria-hidden="true">
              {state === 'done' ? '✓' : position}
            </span>
            <span>{label}</span>
          </li>
        );
      })}
    </ol>
  );
}

function SourceCard({
  checked,
  disabled,
  title,
  desc,
  onSelect,
}: {
  checked: boolean;
  disabled: boolean;
  title: string;
  desc: string;
  onSelect: () => void;
}): ReactElement {
  return (
    <label className={checked ? 'source-card source-card-active' : 'source-card'}>
      <input
        className="source-radio"
        type="radio"
        name="install-source"
        checked={checked}
        disabled={disabled}
        onChange={onSelect}
      />
      <span className="source-body">
        <span className="source-title">{title}</span>
        <span className="source-desc">{desc}</span>
      </span>
    </label>
  );
}

export function SkillInstallPanel({
  readOnly,
  unsupported,
  onChanged,
  onReview,
}: SkillInstallPanelProps): ReactElement {
  const [kind, setKind] = useState<SourceKind>('upload');
  const [rawText, setRawText] = useState('');
  const [fileName, setFileName] = useState('');
  const [fileSize, setFileSize] = useState(0);
  const [url, setUrl] = useState('');
  const [inputError, setInputError] = useState('');

  const [preview, setPreview] = useState<InstallPreview | null>(null);
  /** 预检时的本地原文：只用于算 sha256 和切正文前 30 行，不做 YAML 解析。 */
  const [previewedText, setPreviewedText] = useState('');
  /** null=计算中，''=当前环境算不了（非安全上下文）。 */
  const [digest, setDigest] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [previewing, setPreviewing] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [expiresAt, setExpiresAt] = useState(0);
  const [clock, setClock] = useState(() => Date.now());

  const [installing, setInstalling] = useState(false);
  const [installError, setInstallError] = useState('');
  const [installed, setInstalled] = useState<InstalledState | null>(null);
  const [approveOpen, setApproveOpen] = useState(false);
  const [approving, setApproving] = useState(false);
  const [approveError, setApproveError] = useState('');

  const previewSeq = useRef(0);
  const busy = previewing || installing || approving;

  // 60 秒倒计时：到点自动停表，别让一个 interval 一直空转。
  useEffect(() => {
    if (expiresAt === 0) return;
    setClock(Date.now());
    const timer = window.setInterval(() => {
      const now = Date.now();
      setClock(now);
      if (now >= expiresAt) window.clearInterval(timer);
    }, 1_000);
    return () => window.clearInterval(timer);
  }, [expiresAt]);

  const clearPreview = useCallback((): void => {
    previewSeq.current += 1;
    setPreview(null);
    setPreviewedText('');
    setDigest(null);
    setPreviewError('');
    setAcknowledged(false);
    setExpiresAt(0);
    setInstallError('');
  }, []);

  const chooseSource = (next: SourceKind): void => {
    if (next === kind) return;
    setKind(next);
    setInputError('');
    clearPreview();
  };

  const onFile = async (event: ChangeEvent<HTMLInputElement>): Promise<void> => {
    const file = event.target.files?.[0];
    // 清掉 input 的值，同一个文件改了内容再选一次也会触发 onChange。
    event.target.value = '';
    if (!file) return;
    clearPreview();
    setInputError('');
    setFileName(file.name);
    setFileSize(file.size);

    if (!/\.md$/i.test(file.name)) {
      setRawText('');
      setInputError(`只接受 .md 文件（当前是 ${file.name}）。`);
      return;
    }
    if (file.size > INSTALL_LIMITS.mdBytes) {
      setRawText('');
      setInputError(
        `文件 ${formatBytes(file.size)}，超过单文件 ${formatBytes(INSTALL_LIMITS.mdBytes)} 的上限。请先精简再上传。`,
      );
      return;
    }
    try {
      const text = await file.text();
      if (utf8Bytes(text) > INSTALL_LIMITS.mdBytes) {
        setRawText('');
        setInputError(
          `文件按 UTF-8 计算有 ${formatBytes(utf8Bytes(text))}，超过 ${formatBytes(INSTALL_LIMITS.mdBytes)} 上限。`,
        );
        return;
      }
      setRawText(text);
    } catch {
      setRawText('');
      setInputError('读取文件失败：浏览器没能读到这个文件，请重新选择或直接粘贴内容。');
    }
  };

  const onTextChange = (event: ChangeEvent<HTMLTextAreaElement>): void => {
    clearPreview();
    setInputError('');
    setFileName('');
    setFileSize(0);
    setRawText(event.target.value);
  };

  const onUrlChange = (event: ChangeEvent<HTMLInputElement>): void => {
    clearPreview();
    setInputError('');
    setUrl(event.target.value);
  };

  const urlProblem = useMemo(() => (kind === 'url' ? checkHttpsUrl(url) : ''), [kind, url]);
  const uploadBytes = useMemo(
    () => (kind === 'upload' ? utf8Bytes(rawText) : 0),
    [kind, rawText],
  );
  const overLimit = uploadBytes > INSTALL_LIMITS.mdBytes;

  const canPreview =
    !busy &&
    (kind === 'upload'
      ? rawText.trim().length > 0 && !overLimit
      : url.trim().length > 0 && urlProblem.length === 0);

  const remaining = expiresAt > 0 ? Math.max(0, Math.ceil((expiresAt - clock) / 1_000)) : 0;
  const tokenLive = expiresAt > 0 && remaining > 0;
  const hasToken = (preview?.preview_token.length ?? 0) > 0;
  const tokenExpired = expiresAt > 0 && remaining === 0;

  const fm = preview?.frontmatter ?? {};
  const fmName = asText(fm.name);
  const fmDescription = asText(fm.description);
  const fmWhen = asText(fm.when_to_use);
  const fmTriggers = stringList(fm.triggers);
  const fmRequires = stringList(fm.requires_tools);
  const missingTools = preview?.missing_tools ?? [];
  const missingSections = preview?.missing_sections ?? [];
  const files = preview?.files ?? [];

  const previewBody = useMemo(() => {
    if (!preview) return { lines: [] as string[], truncated: false };
    return bodyHead(previewedText.length > 0 ? previewedText : preview.excerpt);
  }, [preview, previewedText]);

  const resetAll = (): void => {
    setInstalled(null);
    setApproveError('');
    setApproveOpen(false);
    setKind('upload');
    setRawText('');
    setFileName('');
    setFileSize(0);
    setUrl('');
    setInputError('');
    clearPreview();
  };

  const runPreview = async (): Promise<void> => {
    if (!canPreview) return;
    const content = rawText;
    const seq = previewSeq.current + 1;
    previewSeq.current = seq;
    setPreviewing(true);
    setPreviewError('');
    setInstallError('');
    setPreview(null);
    setPreviewedText('');
    setDigest(null);
    setAcknowledged(false);
    setExpiresAt(0);

    try {
      const body =
        kind === 'upload'
          ? { source: 'upload', content }
          : { source: 'url', url: url.trim() };
      const result = await api<InstallPreview>('/admin/api/skills/install/preview', {
        method: 'POST',
        body,
      });
      if (seq !== previewSeq.current) return;

      setPreview(result);
      if (kind === 'upload') {
        setPreviewedText(content);
        setDigest(null);
        void sha256Hex(content).then(
          (value) => {
            if (seq === previewSeq.current) setDigest(value);
          },
          () => {
            if (seq === previewSeq.current) setDigest('');
          },
        );
      } else {
        setDigest('');
      }

      if (result.valid === false) {
        setPreviewError(
          '预检未通过：服务端判定这份内容不能安装（常见原因是缺少必需章节，或 frontmatter 不合规）。修正后重新预检。',
        );
        return;
      }
      if (result.preview_token.length === 0) {
        setPreviewError('预检未通过：服务端没有下发 preview_token，安装会被后端直接拒绝。');
        return;
      }
      setExpiresAt(Date.now() + INSTALL_LIMITS.previewTtlSeconds * 1_000);
      setClock(Date.now());
    } catch (cause) {
      if (seq === previewSeq.current) setPreviewError(describeInstallError(cause));
    } finally {
      setPreviewing(false);
    }
  };

  const install = async (): Promise<void> => {
    if (!preview || !hasToken || !tokenLive || !acknowledged || installing) return;
    setInstalling(true);
    setInstallError('');
    try {
      const summary = await api<SkillSummary>('/admin/api/skills/install', {
        method: 'POST',
        body: { preview_token: preview.preview_token },
      });
      setInstalled({ summary, approved: summary.status === 'active', conflict: false });
      setApproveError('');
      setPreview(null);
      setPreviewedText('');
      setDigest(null);
      setAcknowledged(false);
      setExpiresAt(0);
      setRawText('');
      setFileName('');
      setFileSize(0);
      setUrl('');
      onChanged(
        `已安装 ${summary.name}（rev ${summary.revision}）：已进 pending，待批准，尚未生效`,
      );
    } catch (cause) {
      setInstallError(describeInstallError(cause));
      // 服务端先 pop 凭证再校验：任何一次失败的 install 都可能已经把 token 消费掉。
      setExpiresAt(0);
    } finally {
      setInstalling(false);
    }
  };

  const approve = async (): Promise<void> => {
    if (!installed || installed.approved || installed.conflict) return;
    setApproving(true);
    setApproveError('');
    try {
      const summary = await api<SkillSummary>(
        `/admin/api/skills/${encodeURIComponent(installed.summary.name)}/approve`,
        { method: 'POST', body: { revision: installed.summary.revision } },
      );
      setInstalled({ summary, approved: true, conflict: false });
      setApproveOpen(false);
      onChanged(
        `已批准 ${summary.name}：rev ${summary.revision} 生效，下一轮对话起可用`,
      );
    } catch (cause) {
      setApproveOpen(false);
      setApproveError(describeApproveError(cause));
      if (cause instanceof ApiError && (cause.status === 409 || cause.status === 404)) {
        setInstalled((previous) => (previous ? { ...previous, conflict: true } : previous));
        onChanged('');
      }
    } finally {
      setApproving(false);
    }
  };

  if (readOnly) {
    return (
      <div className="install-panel">
        <div className="notice notice-readonly" role="status">
          浏览器会话是只读的：上传、预检、安装、批准都需要写权限（服务端会对写请求回{' '}
          <code>403 READ_ONLY_SESSION</code>）。这里<strong>不渲染任何写控件</strong>
          ；请在 Vade App 内打开管理面安装技能。
        </div>
        <div className="notice">
          审核区的列表、diff 与正文仍然可读。安装区在只读形态下只是这段说明，避免给一个「看起来能点、点了 403」的界面。
        </div>
      </div>
    );
  }

  if (unsupported) {
    return (
      <div className="install-panel">
        <div className="empty empty-left">
          <h2>本构建不含技能管理</h2>
          <p>
            列表接口返回 <code>501 UNSUPPORTED</code>：这个 core 没有装配{' '}
            <code>SkillLoader</code>，预检与安装都会失败。这里不渲染上传控件，也不假装能装。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="install-panel">
      <StageBar stage={installed ? (installed.approved ? 4 : 3) : preview ? 2 : 1} />

      {installed ? (
        <section
          className={
            installed.approved ? 'install-result install-result-ok' : 'install-result'
          }
          aria-labelledby="install-result-title"
        >
          <div className="skill-section-head">
            <h2 id="install-result-title" className="install-h2">
              {installed.approved ? '已批准并生效' : '已安装，待批准'}
            </h2>
            <span className={installed.approved ? 'badge badge-ok' : 'badge badge-info'}>
              {installed.approved ? 'active' : 'pending'}
            </span>
          </div>

          <p className="install-result-name">
            <code>{installed.summary.name}</code>
            <span className="muted"> · rev </span>
            <code>{installed.summary.revision}</code>
          </p>

          {installed.approved ? (
            <p className="hint">
              已进入工具表与 prompt，<strong>下一轮对话起可用</strong>；当前会话不受影响。
            </p>
          ) : (
            <div className="notice">
              <strong>此时它还不生效。</strong> 文件只在 <code>skills/pending/</code> 里：不在工具表、不进
              prompt，下一轮对话也不会看到它。批准后才会变成 <code>active</code>
              。这不等于「装坏了」。
            </div>
          )}

          {approveError ? (
            <p className="form-error" role="alert">
              {approveError}
            </p>
          ) : null}

          <div className="install-result-actions">
            {!installed.approved && !installed.conflict ? (
              <button
                type="button"
                className="Button Button-primary"
                disabled={approving}
                onClick={() => setApproveOpen(true)}
              >
                批准并启用…
              </button>
            ) : null}
            <button
              type="button"
              className="Button"
              onClick={() => onReview(installed.summary.name)}
            >
              在审核区查看
            </button>
            <button type="button" className="Button Button-ghost" onClick={resetAll}>
              再装一个
            </button>
          </div>
        </section>
      ) : (
        <>
          <section className="install-section" aria-labelledby="install-source-title">
            <h2 id="install-source-title" className="install-h2">
              1 · 选择来源
            </h2>

            <div className="source-grid" role="radiogroup" aria-label="技能来源">
              <SourceCard
                checked={kind === 'upload'}
                disabled={busy}
                title="粘贴 / 上传 .md"
                desc="单文件 SKILL.md，≤ 64KB。粘贴框支持直接改内容。"
                onSelect={() => chooseSource('upload')}
              />
              <div className="source-card source-card-disabled" aria-disabled="true">
                <span className="source-radio source-radio-static" aria-hidden="true" />
                <span className="source-body">
                  <span className="source-title">
                    上传 .zip <span className="badge badge-muted">未接线</span>
                  </span>
                  <span className="source-desc">
                    后端契约只有 <code>upload</code> / <code>url</code> 两个来源，没有 zip
                    字段。这一项置灰，不做假上传。
                  </span>
                </span>
              </div>
              <SourceCard
                checked={kind === 'url'}
                disabled={busy}
                title="从 https URL 安装"
                desc="由 core 下载；只接受 https，回环 / 私网地址会被 SSRF 规则拒绝。"
                onSelect={() => chooseSource('url')}
              />
            </div>

            {kind === 'upload' ? (
              <div className="install-inputs">
                <div className="field">
                  <label className="field-label" htmlFor="install-file">
                    选择 .md 文件
                  </label>
                  <input
                    id="install-file"
                    className="input input-file"
                    type="file"
                    accept=".md,text/markdown,text/plain"
                    disabled={busy}
                    onChange={(event) => {
                      void onFile(event);
                    }}
                  />
                  {fileName.length > 0 ? (
                    <p className="hint">
                      已读取 <code>{fileName}</code>
                      {fileSize > 0 ? `（${formatBytes(fileSize)}）` : ''}
                    </p>
                  ) : null}
                </div>

                <div className="field">
                  <label className="field-label" htmlFor="install-text">
                    或直接粘贴内容
                  </label>
                  <textarea
                    id="install-text"
                    className="input textarea install-textarea"
                    rows={10}
                    value={rawText}
                    disabled={busy}
                    spellCheck={false}
                    aria-invalid={overLimit}
                    aria-describedby={overLimit ? 'install-text-error' : 'install-text-hint'}
                    placeholder={'---\nname: my-skill\ndescription: …\n---\n\n## When to Use\n…'}
                    onChange={onTextChange}
                  />
                  {overLimit ? (
                    <p className="form-error" id="install-text-error" role="alert">
                      内容 {formatBytes(uploadBytes)}，超过单文件 64KB 上限。
                    </p>
                  ) : (
                    <p className="hint" id="install-text-hint">
                      {formatBytes(uploadBytes)} / 64KB · 文件必须是 UTF-8 文本
                    </p>
                  )}
                </div>
              </div>
            ) : (
              <div className="field">
                <label className="field-label" htmlFor="install-url">
                  https URL
                </label>
                <input
                  id="install-url"
                  className="input"
                  type="url"
                  inputMode="url"
                  autoComplete="off"
                  spellCheck={false}
                  value={url}
                  disabled={busy}
                  placeholder="https://example.com/SKILL.md"
                  aria-invalid={urlProblem.length > 0}
                  aria-describedby={urlProblem.length > 0 ? 'install-url-error' : 'install-url-hint'}
                  onChange={onUrlChange}
                />
                {urlProblem.length > 0 ? (
                  <p className="form-error" id="install-url-error" role="alert">
                    {urlProblem}
                  </p>
                ) : (
                  <p className="hint" id="install-url-hint">
                    core 以 15 秒超时下载，最多 3 跳重定向；每跳都重新校验目标地址。URL
                    里不能带凭据。
                  </p>
                )}
              </div>
            )}

            {inputError ? (
              <p className="form-error" role="alert">
                {inputError}
              </p>
            ) : null}

            <div className="install-limits">
              <p className="install-limits-title">上传限制</p>
              <ul>
                <li>
                  单个 <code>.md</code> ≤ 64KB
                </li>
                <li>
                  zip ≤ 1MB · 解压后 ≤ 5MB · 条目 ≤ 32（本批不提供 zip 上传）
                </li>
                <li>
                  zip 内不得含可执行文件（<code>.sh</code> / <code>.py</code> /{' '}
                  <code>.js</code> / <code>.exe</code> 等）、符号链接或 <code>..</code>{' '}
                  路径
                </li>
                <li>URL 只接受 https；私网、回环、链路本地与云元数据地址一律拒绝</li>
              </ul>
            </div>

            <div className="install-actions">
              <button
                type="button"
                className="Button Button-primary"
                disabled={!canPreview}
                onClick={() => {
                  void runPreview();
                }}
              >
                {previewing ? '预检中…' : '预检'}
              </button>
              {previewing ? <span className="hint">解析 frontmatter、依赖与重名…</span> : null}
            </div>

            {previewError ? (
              <p className="form-error" role="alert">
                {previewError}
              </p>
            ) : null}
          </section>

          {preview ? (
            <section className="install-section" aria-labelledby="install-preview-title">
              <div className="skill-section-head">
                <h2 id="install-preview-title" className="install-h2">
                  2 · 预检结果
                </h2>
                <span className="skill-badges">
                  {preview.valid === false ? (
                    <span className="badge badge-danger">不可安装</span>
                  ) : (
                    <span className="badge badge-ok">校验通过</span>
                  )}
                  {preview.conflict ? <span className="badge badge-warn">重名</span> : null}
                </span>
              </div>

              <dl className="install-meta">
                <div>
                  <dt>name</dt>
                  <dd>
                    <code>{preview.name.length > 0 ? preview.name : fmName || '（未解析出）'}</code>
                  </dd>
                </div>
                <div>
                  <dt>来源</dt>
                  <dd>{kind === 'upload' ? '手动安装 · 上传' : '手动安装 · URL'}</dd>
                </div>
                <div>
                  <dt>正文前 30 行</dt>
                  <dd>{previewBody.lines.length} 行</dd>
                </div>
                <div>
                  <dt>依赖工具</dt>
                  <dd>
                    {fmRequires.length} 个
                    {missingTools.length > 0 ? ` · 缺 ${missingTools.length}` : ''}
                  </dd>
                </div>
              </dl>

              {preview.conflict ? (
                <div className="alert alert-warn" role="status">
                  <div>
                    <strong>同名冲突</strong>
                    <p>
                      {preview.conflict_with === 'pending'
                        ? 'pending/ 里已经有同名版本：安装会把它移进 .trash，替换成这份新内容。'
                        : '已经存在同名的生效版本：安装只会生成新的 pending 提案，批准后才替换当前版本；当前生效内容不受影响。'}
                    </p>
                  </div>
                </div>
              ) : null}

              {missingSections.length > 0 ? (
                <div className="alert alert-warn" role="alert">
                  <div>
                    <strong>缺少必需章节</strong>
                    <p>
                      {missingSections.map((section) => (
                        <code key={section}>{section}</code>
                      ))}
                      。补上这些 <code>##</code> 章节再预检。
                    </p>
                  </div>
                </div>
              ) : null}

              <div className="install-subsection">
                <h3 className="install-subtitle">解析后的 frontmatter</h3>
                <dl className="install-fm">
                  <div>
                    <dt>name</dt>
                    <dd>
                      <code>{fmName || '—'}</code>
                    </dd>
                  </div>
                  <div>
                    <dt>description</dt>
                    <dd>{fmDescription || '—'}</dd>
                  </div>
                  <div>
                    <dt>when_to_use</dt>
                    <dd>{fmWhen || '—'}</dd>
                  </div>
                  <div>
                    <dt>triggers</dt>
                    <dd>
                      {fmTriggers.length > 0 ? (
                        <span className="chip-row">
                          {fmTriggers.map((trigger) => (
                            <code key={trigger}>{trigger}</code>
                          ))}
                        </span>
                      ) : (
                        '—'
                      )}
                    </dd>
                  </div>
                </dl>
                <details className="install-details">
                  <summary>完整 frontmatter（JSON）</summary>
                  <pre className="skill-body install-json">
                    {JSON.stringify(preview.frontmatter, null, 2)}
                  </pre>
                </details>
              </div>

              <div className="install-subsection">
                <h3 className="install-subtitle">requires_tools</h3>
                {fmRequires.length === 0 ? (
                  <p className="muted">未声明依赖工具。</p>
                ) : (
                  <ul className="chip-row install-tool-chips">
                    {fmRequires.map((tool) => {
                      const missing = missingTools.includes(tool);
                      return (
                        <li key={tool} className={missing ? 'chip chip-missing' : 'chip'}>
                          <code>{tool}</code>
                          {missing ? <span className="chip-flag">缺失</span> : null}
                        </li>
                      );
                    })}
                  </ul>
                )}
                {missingTools.length > 0 ? (
                  <p className="form-error" role="alert">
                    当前没有这些工具：{missingTools.join(' / ')}。安装仍可继续，但审核时要确认它还能不能跑。
                  </p>
                ) : null}
              </div>

              {files.length > 0 ? (
                <div className="install-subsection">
                  <h3 className="install-subtitle">附带文件</h3>
                  <span className="chip-row">
                    {files.map((file) => (
                      <code key={file}>{file}</code>
                    ))}
                  </span>
                </div>
              ) : null}

              <div className="install-subsection">
                <div className="skill-section-head">
                  <h3 className="install-subtitle">
                    正文前 {previewBody.lines.length} 行
                  </h3>
                  <span className="muted">
                    {previewedText.length > 0 ? '本地原文' : '服务端摘录（≤400 字符）'}
                  </span>
                </div>
                <pre className="skill-body install-excerpt">
                  {previewBody.lines.length > 0 ? previewBody.lines.join('\n') : '（空）'}
                </pre>
                {previewBody.truncated ? (
                  <p className="hint">只显示前 30 行；完整正文在安装后从审核抽屉里读。</p>
                ) : null}
              </div>

              <dl className="install-meta install-token-meta">
                <div>
                  <dt>SHA-256</dt>
                  <dd>
                    <code className="install-token">
                      {kind === 'url'
                        ? '—（下载在服务端，接口未回传摘要）'
                        : digest === null
                          ? '计算中…'
                          : digest.length > 0
                            ? digest
                            : '当前环境不支持本地计算（服务端仍会校验内容）'}
                    </code>
                  </dd>
                </div>
                <div>
                  <dt>preview_token</dt>
                  <dd>
                    <code className="install-token">
                      {preview.preview_token.length > 0 ? preview.preview_token : '（无）'}
                    </code>
                  </dd>
                </div>
              </dl>

              {hasToken ? (
                tokenExpired ? (
                  <p className="form-error" role="alert">
                    预检已过期（60 秒一次性）。过期的 token 后端一律拒绝，请重新预检。
                  </p>
                ) : (
                  <p className="hint">
                    preview_token 剩 <strong>{remaining}</strong> 秒；一次性，安装成功或被拒绝后都要重新预检。
                  </p>
                )
              ) : null}

              <label className="install-ack">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  disabled={!tokenLive || !hasToken}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                />
                <span>
                  我已查看上面的 frontmatter 与正文，确认内容可以安装（不是「先装上试试」）。
                </span>
              </label>

              <div className="install-actions">
                <button
                  type="button"
                  className="Button Button-primary"
                  disabled={!hasToken || !tokenLive || !acknowledged || installing}
                  onClick={() => {
                    void install();
                  }}
                >
                  {installing ? '安装中…' : '安装到待批准（不生效）'}
                </button>
                <span className="hint">安装只写 skills/pending/，批准后才 active。</span>
              </div>

              {installError ? (
                <p className="form-error" role="alert">
                  {installError}
                </p>
              ) : null}

              {installError.length > 0 ? (
                <div className="install-actions">
                  <button
                    type="button"
                    className="Button Button-sm"
                    disabled={previewing}
                    onClick={() => {
                      void runPreview();
                    }}
                  >
                    重新预检
                  </button>
                </div>
              ) : null}
            </section>
          ) : null}
        </>
      )}

      {installed && approveOpen ? (
        <ConfirmDialog
          title={`批准 ${installed.summary.name} 并启用？`}
          confirmLabel="批准并启用"
          busy={approving}
          onCancel={() => setApproveOpen(false)}
          onConfirm={() => {
            void approve();
          }}
        >
          <p>
            会把 <code>pending/</code> 里的 rev {installed.summary.revision} 转正为{' '}
            <code>active</code>（revision+1）；如果有当前生效版本，旧版本进{' '}
            <code>.history/</code>，可以回滚。
          </p>
          <p>
            <strong>下一轮对话起生效，当前会话不受影响。</strong>
          </p>
        </ConfirmDialog>
      ) : null}
    </div>
  );
}
