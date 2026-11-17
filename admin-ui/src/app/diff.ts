/**
 * 解析后端算好的 unified diff（`GET /admin/api/skills/{name}` 的 `pending_diff`）。
 *
 * 前端**不**解析 YAML、不自己比对两版正文：diff 的唯一来源是后端（31-C5a §T5）。
 * 这里只做一件事：把连续的 `-` / `+` 行按序配成左右两栏，让「左旧右新」能逐行对齐。
 * 一侧多出来的行留空，不做任何内容上的猜测。
 */

export type DiffRowKind = 'equal' | 'add' | 'del' | 'change';

export interface DiffCell {
  /** 该侧的行号；这一侧没有对应行时为 null。 */
  line: number | null;
  text: string;
  kind: DiffRowKind;
}

export interface DiffRow {
  left: DiffCell | null;
  right: DiffCell | null;
  kind: DiffRowKind;
}

export interface DiffHunk {
  /** 原样的 `@@ -a,b +c,d @@`，给需要对照原文的人看。 */
  header: string;
  rows: DiffRow[];
}

export interface ParsedDiff {
  /** `---` 侧文件名（后端写成 `name@rev` 或 `/dev/null`）。 */
  from: string;
  /** `+++` 侧文件名（后端写成 `name@pending`）。 */
  to: string;
  hunks: DiffHunk[];
  /** 后端按 20k 字符截断、或 hunk 声明的行数没走完时为 true。 */
  truncated: boolean;
}

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

export function parseUnifiedDiff(text: string): ParsedDiff {
  const parsed: ParsedDiff = { from: '', to: '', hunks: [], truncated: false };
  if (!text) return parsed;

  let hunk: DiffHunk | null = null;
  let oldLine = 0;
  let newLine = 0;
  let oldExpected = 0;
  let newExpected = 0;
  let oldSeen = 0;
  let newSeen = 0;
  let deletes: string[] = [];
  let inserts: string[] = [];

  // 把缓冲的一段删除/新增配对成行。行号在这里才递增，所以块的起始行号就是
  // 进入缓冲时两侧的“下一行”。
  const flush = (): void => {
    if (hunk === null) {
      deletes = [];
      inserts = [];
      return;
    }
    const target = hunk;
    const count = Math.max(deletes.length, inserts.length);
    for (let index = 0; index < count; index += 1) {
      const leftText = index < deletes.length ? deletes[index]! : null;
      const rightText = index < inserts.length ? inserts[index]! : null;
      const kind: DiffRowKind =
        leftText !== null && rightText !== null ? 'change' : leftText !== null ? 'del' : 'add';
      const left: DiffCell | null =
        leftText === null ? null : { line: oldLine++, text: leftText, kind };
      const right: DiffCell | null =
        rightText === null ? null : { line: newLine++, text: rightText, kind };
      target.rows.push({ left, right, kind });
    }
    deletes = [];
    inserts = [];
  };

  const closeHunk = (): void => {
    flush();
    if (hunk !== null && (oldSeen < oldExpected || newSeen < newExpected)) {
      // 后端把 diff 截到 20k 字符时会停在一段中间：行数对不上就是截断的证据。
      parsed.truncated = true;
    }
  };

  for (const raw of text.split('\n')) {
    if (raw.length === 0) continue;

    if (raw.startsWith('@@')) {
      closeHunk();
      const match = HUNK_RE.exec(raw);
      oldLine = match ? Number(match[1]) : 1;
      newLine = match ? Number(match[3]) : 1;
      oldExpected = match && match[2] !== undefined ? Number(match[2]) : 1;
      newExpected = match && match[4] !== undefined ? Number(match[4]) : 1;
      oldSeen = 0;
      newSeen = 0;
      hunk = { header: raw, rows: [] };
      parsed.hunks.push(hunk);
      continue;
    }

    if (hunk === null) {
      // hunk 之外的 `---` / `+++` 是文件头。带 `-` 前缀的正文行不会走到这里。
      if (raw.startsWith('--- ')) {
        parsed.from = raw.slice(4).trim();
        continue;
      }
      if (raw.startsWith('+++ ')) {
        parsed.to = raw.slice(4).trim();
        continue;
      }
      continue;
    }

    if (raw.startsWith('\\')) continue; // "\ No newline at end of file"

    const marker = raw[0];
    const body = raw.slice(1);

    if (marker === ' ') {
      flush();
      hunk.rows.push({
        left: { line: oldLine++, text: body, kind: 'equal' },
        right: { line: newLine++, text: body, kind: 'equal' },
        kind: 'equal',
      });
      oldSeen += 1;
      newSeen += 1;
    } else if (marker === '-') {
      deletes.push(body);
      oldSeen += 1;
    } else if (marker === '+') {
      inserts.push(body);
      newSeen += 1;
    } else {
      // 截断可能落在行首：没有前缀的行按上下文显示，宁可多显示一行也不吞内容。
      flush();
      hunk.rows.push({
        left: { line: oldLine++, text: raw, kind: 'equal' },
        right: { line: newLine++, text: raw, kind: 'equal' },
        kind: 'equal',
      });
      oldSeen += 1;
      newSeen += 1;
    }
  }

  closeHunk();
  return parsed;
}
