import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactElement } from 'react';
import { ApiError, api } from '../app/api';
import { useAuth } from '../app/auth';
import { describeSkillError } from '../app/skillError';
import { originLabel, statusMeta } from '../app/skillMeta';
import type { SkillListResponse, SkillSummary } from '../app/types';
import { SkillDrawer } from './SkillDrawer';

interface SkillRow {
  name: string;
  /** 非 pending 的那条（active/disabled/stale/unavailable）；只有待安装时为 null。 */
  current: SkillSummary | null;
  /** 待审提案（`pending/` 里的那条）。 */
  proposal: SkillSummary | null;
  /** 卡片上展示状态用的那条：有 current 用 current，否则用 proposal。 */
  display: SkillSummary;
}

/**
 * `list_all(include_pending=True)` 会把「active 技能」和「它的 pending 提案」各返回一行。
 * 页面按 name 合成一张卡片：状态取生效版本，另加「有提案待审」标记，
 * 并把有提案的排到前面 —— 有待办事项的地方不应该让人滚动寻找。
 */
function groupSkills(items: SkillSummary[]): SkillRow[] {
  const byName = new Map<string, SkillRow>();
  for (const item of items) {
    const existing = byName.get(item.name);
    if (item.status === 'pending') {
      if (existing) {
        existing.proposal = item;
      } else {
        byName.set(item.name, {
          name: item.name,
          current: null,
          proposal: item,
          display: item,
        });
      }
      continue;
    }
    if (existing) {
      // 同一个名字出现两条非 pending（不应发生）：保留先到的，别静默丢行。
      // pending 先到时 display 还是提案，这里要换成生效版本，否则状态显示成「待确认」。
      if (existing.current === null) {
        existing.current = item;
        existing.display = item;
      }
      continue;
    }
    byName.set(item.name, { name: item.name, current: item, proposal: null, display: item });
  }

  return [...byName.values()].sort((left, right) => {
    const leftPending = left.proposal !== null ? 0 : 1;
    const rightPending = right.proposal !== null ? 0 : 1;
    if (leftPending !== rightPending) return leftPending - rightPending;
    return left.name.localeCompare(right.name);
  });
}

function SkillCard({
  row,
  readOnly,
  onOpen,
}: {
  row: SkillRow;
  readOnly: boolean;
  onOpen: (name: string) => void;
}): ReactElement {
  const meta = statusMeta(row.display.status);
  const proposal = row.proposal;
  const stale = row.display.status === 'stale';
  const open = (): void => onOpen(row.name);

  return (
    <li className={proposal ? 'skill-card skill-card-pending' : 'skill-card'}>
      <div className="skill-head">
        <div className="skill-title">
          <button type="button" className="link-button skill-name" onClick={open}>
            {row.name}
          </button>
          <p className="muted skill-desc-line">
            {row.display.description && row.display.description.length > 0
              ? row.display.description
              : '（无描述）'}
          </p>
        </div>
        <div className="skill-badges">
          <span className={meta.className} title={meta.hint}>
            {meta.label}
          </span>
          {proposal && row.current ? (
            <span className="badge badge-info">有提案待审</span>
          ) : null}
        </div>
      </div>

      <dl className="skill-meta skill-meta-card">
        <div>
          <dt>当前 rev</dt>
          <dd>
            {row.current ? (
              <code>{row.current.revision}</code>
            ) : (
              <>
                <code>{row.display.revision}</code> <span className="muted">（未生效）</span>
              </>
            )}
          </dd>
        </div>
        <div>
          <dt>来源</dt>
          <dd>{originLabel(row.display.origin)}</dd>
        </div>
        <div>
          <dt>提案</dt>
          <dd>
            {proposal ? (
              <>
                <code>rev {proposal.revision}</code>
              </>
            ) : (
              '无'
            )}
          </dd>
        </div>
      </dl>

      {stale ? (
        <p className="skill-stale">
          依赖的工具已不存在：不能通过新提案，只能驳回或回滚。
        </p>
      ) : null}

      <div className="skill-card-actions">
        <button type="button" className="Button Button-sm" onClick={open}>
          {proposal ? (readOnly ? '查看 diff' : '审核') : '查看'}
        </button>
      </div>
    </li>
  );
}

export function SkillsPage(): ReactElement {
  const { readOnly } = useAuth();
  const [items, setItems] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [unsupported, setUnsupported] = useState(false);
  const [flash, setFlash] = useState('');
  const [openName, setOpenName] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal, silent = false): Promise<void> => {
    if (!silent) setLoading(true);
    try {
      const data = await api<SkillListResponse>('/admin/api/skills', { signal });
      setItems(data.items);
      setUnsupported(false);
      setLoadError('');
    } catch (cause) {
      if (signal?.aborted) return;
      if (cause instanceof ApiError && cause.status === 501) {
        setUnsupported(true);
        setLoadError('');
      } else {
        setLoadError(describeSkillError(cause));
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  useEffect(() => {
    if (!flash) return;
    const timer = window.setTimeout(() => setFlash(''), 8_000);
    return () => window.clearTimeout(timer);
  }, [flash]);

  const rows = useMemo(() => groupSkills(items), [items]);
  const pendingCount = rows.filter((row) => row.proposal !== null).length;

  const handleChanged = useCallback(
    (message: string): void => {
      if (message.length > 0) setFlash(message);
      void load(undefined, true);
    },
    [load],
  );

  return (
    <section className="page" aria-labelledby="skills-title">
      <header className="page-head">
        <div>
          <h1 id="skills-title">技能</h1>
          <p className="muted">
            共 {rows.length} 个 · {pendingCount} 个待审提案 · 审核只影响下一轮对话
            {readOnly ? ' · 只读' : ''}
          </p>
        </div>
        <button
          type="button"
          className="Button Button-ghost"
          onClick={() => void load()}
          disabled={loading}
        >
          {loading ? '刷新中…' : '刷新'}
        </button>
      </header>

      {readOnly ? (
        <div className="notice notice-readonly" role="status">
          浏览器会话只能查看：列表、diff、正文都能读，通过 / 驳回 / 回滚按钮不渲染。要审核请用 Vade App
          打开管理面。
        </div>
      ) : null}

      {loadError ? (
        <div className="alert" role="alert">
          <span>读取技能列表失败：{loadError}</span>
          <button type="button" className="Button Button-sm" onClick={() => void load()}>
            重试
          </button>
        </div>
      ) : null}

      {unsupported ? (
        <div className="empty empty-left">
          <h2>本构建不含技能管理</h2>
          <p>
            core 返回 <code>501 UNSUPPORTED</code>：这个构建没有装配 <code>SkillLoader</code>。
            换一个带技能加载器的构建后这里会显示列表，不渲染假控件。
          </p>
        </div>
      ) : null}

      {loading && rows.length === 0 && !loadError && !unsupported ? (
        <p className="muted">正在读取技能列表…</p>
      ) : null}

      {!loading && rows.length === 0 && !loadError && !unsupported ? (
        <div className="empty">
          <h2>还没有技能</h2>
          <p className="muted">
            安装与自动提案都会先落到 <code>skills/pending/</code>，待审的会出现在这里。
          </p>
        </div>
      ) : null}

      <ul className="skill-list">
        {rows.map((row) => (
          <SkillCard
            key={row.name}
            row={row}
            readOnly={readOnly}
            onOpen={(name) => setOpenName(name)}
          />
        ))}
      </ul>

      {openName ? (
        <SkillDrawer
          name={openName}
          readOnly={readOnly}
          onClose={() => setOpenName(null)}
          onChanged={handleChanged}
        />
      ) : null}

      {flash ? (
        <div className="toast" role="status">
          {flash}
        </div>
      ) : null}
    </section>
  );
}
