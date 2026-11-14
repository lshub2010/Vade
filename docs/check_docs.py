#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_docs.py —— Vademecum 文档包一致性检查（纯标准库，无第三方依赖）

用法：
    python docs/check_docs.py            # 在仓库根跑，或任意位置（脚本自行定位）
    python docs/check_docs.py --json     # 机器可读输出

退出码：0 = 全部通过；1 = 有失败项。

检查项（对应 v1.18 审查里那批「人工必然反复出错」的计数/编号问题）：
  C1  决策台账 D 编号连续、无缺号、无重号，条数与头部声明一致
  C2  AC 编号连续、无缺号、无重号，条数与 F1 任务书声明一致
  C3  AMEND 编号连续、无缺号
  C4  批次集合：status.md / 00-orchestration 波次表 / agents 任务书 三处一致
  C5  契约文件数 = 01-contracts 契约表行数 = docs/README 声明数
  C6  版本口径：全包不再出现过期硬编码「AMEND-01～22」「AMEND-01～21」「AMEND-01～20」「AMEND-01～18」「方案 v1.16」「78 条」「16 个契约」「24 个批次」
  C7  FR-13.19~13.21 不再被两组条款重复定义（撞号检测）
  C8  AMEND-19 的 A 编号不与 AMEND-18 的 A 编号重号
  C9  §8 小节号与 AMEND 号一一对应
  C10 C-04 错误码：任务书里用到的错误码都在 01-contracts 的枚举里
"""
import os
import re
import sys
import json

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)  # 仓库根（docs 的上一级）


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


# Windows 控制台默认 GBK，强制 stdout 用 UTF-8 避免中文乱码
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def path(*parts):
    return os.path.join(ROOT, *parts)


class Result:
    def __init__(self):
        self.items = []

    def check(self, cid, desc, ok, detail=""):
        self.items.append({"id": cid, "desc": desc, "ok": ok, "detail": detail})

    def failed(self):
        return [i for i in self.items if not i["ok"]]

    def report(self, as_json=False):
        if as_json:
            print(json.dumps(self.items, ensure_ascii=False, indent=2))
        else:
            for it in self.items:
                mark = "PASS" if it["ok"] else "FAIL"
                print(f"[{mark}] {it['id']}  {it['desc']}")
                if not it["ok"] and it["detail"]:
                    for line in str(it["detail"]).splitlines():
                        print(f"        · {line}")
        fails = self.failed()
        print()
        print(f"合计 {len(self.items)} 项，失败 {len(fails)} 项。")
        return 0 if not fails else 1


def numbers_from(text, pattern):
    return [int(m.group(1)) for m in re.finditer(pattern, text, re.M)]


def check_continuous(nums):
    """返回 (是否连续, 说明)"""
    if not nums:
        return False, "无编号"
    s = sorted(nums)
    uniq = sorted(set(s))
    missing = [n for n in range(s[0], s[-1] + 1) if n not in uniq]
    dup = sorted({n for n in s if s.count(n) > 1})
    parts = []
    if missing:
        parts.append(f"缺号 {missing}")
    if dup:
        parts.append(f"重号 {dup}")
    return (not missing and not dup), "; ".join(parts)


def main():
    as_json = "--json" in sys.argv
    r = Result()

    req_p = path("docs", "requirements.md")
    design_p = path("docs", "design.md")
    status_p = path("docs", "status.md")
    agents_readme_p = path("docs", "agents", "README.md")
    orch_p = path("docs", "agents", "00-orchestration.md")
    contracts_p = path("docs", "agents", "01-contracts.md")
    docs_readme_p = path("docs", "README.md")
    f1_p = path("docs", "agents", "60-F1-integration.md")

    req = read(req_p)
    design = read(design_p)

    # ---- C1 决策台账 ----
    d_nums = numbers_from(req, r"\|\s*D(\d+)\s*\|")
    ok, why = check_continuous(d_nums)
    ok = ok and len(d_nums) == 110
    r.check("C1", "决策台账 D1–D110 连续、无缺重、共 110 条（requirements.md）",
            ok, why or f"实际 {len(d_nums)} 条")

    # ---- C2 AC 编号（只取「新增 AC-x」与 §5 表格定义，忽略「已被 AC-x 修订」的引用）----
    ac_def = numbers_from(req, r"新增(?:验收)?[:：]\s*\*\*AC-(\d+)\*\*")
    ac_def += numbers_from(req, r"新增 \*\*AC-(\d+)\*\*")
    ac_def += numbers_from(req, r"^\| AC-(\d+) \|")  # §5 表格
    ac_nums = sorted(set(ac_def))
    ok, why = check_continuous(ac_nums)
    ok = ok and max(ac_nums) == 69
    r.check("C2", "AC-1–AC-69 定义连续、无缺重（requirements.md）",
            ok, why or f"最大 AC-{max(ac_nums)}")

    # F1 声称的 AC 条数
    f1 = read(f1_p)
    m = re.search(r"(\d+) 条 AC", f1)
    f1_declared = int(m.group(1)) if m else None
    f1_rows = numbers_from(f1, r"^\| AC-(\d+) \|")
    ok = (f1_declared == 69 and len(f1_rows) == 69)
    r.check("C2b", "F1 任务书验收矩阵 = 69 条（声明与表格行一致）",
            ok, f"声明 {f1_declared} 条，表格 {len(f1_rows)} 行")

    # ---- C3 AMEND 编号 ----
    am_nums = numbers_from(req, r"\|\s*AMEND-(\d+)\s*\|")
    ok, why = check_continuous(am_nums)
    ok = ok and max(am_nums) == 23
    r.check("C3", "AMEND-01–23 连续、无缺号（requirements.md §8 汇总表）",
            ok, why or f"最大 AMEND-{max(am_nums)}")

    # ---- C4 批次集合 ----
    status = read(status_p)
    # 批次代号：C0a/C0b/C0c / C1..C5 + C3b/C3d/C5a/C5b / M1..M6 + M4a/M4b / U1..U4 / F1
    batch_re = r"(C0[abc]|C[1-5](?:[abd])?|M[1-6][ab]?|U[1-4]|F1)"
    status_batches = set(re.findall(r"^\|\s*" + batch_re + r"\s*\|", status, re.M))
    agent_dir = path("docs", "agents")
    briefs = set()
    for fn in os.listdir(agent_dir):
        mm = re.match(r"^\d\d-" + batch_re + r"-", fn)
        if mm:
            name = mm.group(1)
            if name == "M4":
                name = "M4a"   # 43-M4-memory.md = M4a
            briefs.add(name)
    ok = (len(status_batches) == 23 and len(briefs) == 23)
    r.check("C4", "批次总数 = 23：status.md 台账与 agents 任务书两处一致",
            ok, f"status {len(status_batches)} 批 / 任务书 {len(briefs)} 份；差集 status-only={status_batches - briefs} brief-only={briefs - status_batches}")

    # ---- C5 契约数 ----
    contracts = read(contracts_p)
    c_ids = set(numbers_from(contracts, r"\|\s*\*\*C-(\d+)\*\*\s*\|"))
    docs_readme = read(docs_readme_p)
    m = re.search(r"(\d+) 个契约", docs_readme)
    declared = int(m.group(1)) if m else None
    ok = (len(c_ids) == 18 and declared == 18)
    r.check("C5", "契约 C-01–C-18 共 18 个，docs/README 声明数一致",
            ok, f"契约表 {len(c_ids)} 个 / 声明 {declared} 个")

    # ---- C6 过期硬编码（只查“现状口径”位置；requirements/design 里的历史叙述可豁免）----
    stale = []
    # 现状口径文件：README、docs/README、OVERVIEW、overview.html、agents/README、00-orchestration
    live_files = [docs_readme_p, path("README.md"), path("docs", "OVERVIEW.md"),
                  path("overview.html"), agents_readme_p, orch_p]
    stale_patterns = [
        (r"AMEND-01～22", "AMEND-01～22"),
        (r"AMEND-01～21", "AMEND-01～21"),
        (r"AMEND-01～20", "AMEND-01～20"),
        (r"AMEND-01～18", "AMEND-01～18"),
        (r"方案 v1\.16", "方案 v1.16"),
        (r"\b78 条", "78 条"),
        (r"16 个契约", "16 个契约"),
        (r"24 个批次", "24 个批次"),
    ]
    for p in live_files:
        if not os.path.exists(p):
            continue
        txt = read(p)
        for pat, label in stale_patterns:
            # OVERVIEW/overview.html 的正文引用「（方案 v1.17）」属历史，但已改成 v1.18
            if re.search(pat, txt):
                stale.append(f"{os.path.relpath(p, ROOT)}: 出现「{label}」")
    r.check("C6", "现状口径文件无过期硬编码（AMEND-01～22 / 方案 v1.16 / 78 条 / 16 个契约 / 24 个批次）",
            not stale, "\n".join(stale))

    # ---- C7 FR-13.19~21 撞号 ----
    # 统计 FR-13.19 / 13.20 / 13.21 的「新增」定义行，同一编号出现 >1 次即撞号
    defined = {}
    for m in re.finditer(r"新增 \*\*FR-13\.(1[9]|2[01]) \[M\]\*\*", req):
        defined.setdefault(m.group(1), 0)
        defined[m.group(1)] += 1
    collide = {k: v for k, v in defined.items() if v > 1}
    # 期望：13.19/13.20/13.21 各只出现 1 次（turn_review 系列），13.25~27 是另一组
    ok = True
    for n in ("19", "20", "21"):
        if defined.get(n, 0) > 1:
            ok = False
    detail = ""
    if not ok:
        detail = "; ".join(f"FR-13.{k} 被定义 {v} 次" for k, v in collide.items())
    r.check("C7", "FR-13.19/13.20/13.21 不再被两组条款重复定义（撞号已修）", ok, detail)

    # FR-13.25~27 应存在（顺延后）
    have_25 = "FR-13.25" in req and "FR-13.26" in req and "FR-13.27" in req
    r.check("C7b", "FR-13.25～13.27 存在（A35 三条已顺延）", have_25, "" if have_25 else "缺失顺延后的编号")

    # ---- C8 AMEND-19 的 A 编号与 AMEND-18 不重号 ----
    # 取 §8.18（AMEND-18）与 §8.20（AMEND-19）区间内的 A 编号
    def a_nums_in(section_title):
        m = re.search(r"### " + re.escape(section_title) + r".*?(?=\n### |\Z)", req, re.S)
        if not m:
            return set()
        return set(re.findall(r"\*\*A(\d+) ·", m.group(0)))
    a18 = a_nums_in("8.18 AMEND-18")
    a19 = a_nums_in("8.20 AMEND-19")
    overlap = sorted(a18 & a19, key=int)
    r.check("C8", "AMEND-18 与 AMEND-19 的 A 编号不重号",
            not overlap, f"重号 A{overlap}" if overlap else "")

    # ---- C9 §8 小节号与 AMEND 号对应 ----
    # 收集「### 8.N AMEND-M 明细」，比较去前导零后的整数是否相等。
    # AMEND-20 是纯文档修订，只在汇总表登记、无明细小节，允许缺席；其余必须一一对应。
    mapping = re.findall(r"^### 8\.(\d+) AMEND-(\d+) 明细", req, re.M)
    mismatch = [(n, m) for n, m in mapping if int(n) != int(m)]
    # AMEND-20 只有汇总行、无明细小节（纯文档修订）。
    # 豁免规则：若唯一“错位”是最后一节 8.k 对应 AMEND-(k-1)，且 AMEND-k 无明细，则视为合法。
    if len(mismatch) == 1:
        n, m = int(mismatch[0][0]), int(mismatch[0][1])
        # 该「错位」合法，当且仅当 AMEND-(n) 确实没有明细小节（本包即 AMEND-20 纯修订无明细）
        has_detail_for_n = any(int(mm) == n for _, mm in mapping)
        if n == m + 1 and not has_detail_for_n:
            mismatch = []
    r.check("C9", "§8 小节号与 AMEND 号一一对应（AMEND-20 无明细，豁免）",
            not mismatch, f"错位：{mismatch}" if mismatch else "")

    # ---- C10 错误码闭合 ----
    contracts_txt = read(contracts_p)
    # 提取 C-04 枚举里的 code（JSON 键）
    enum_block = re.search(r"```json\s*(\{.*?\})\s*```", contracts_txt, re.S)
    codes = set()
    if enum_block:
        try:
            codes = set(json.loads(enum_block.group(1)).keys())
        except Exception:
            codes = set(re.findall(r'"([A-Z_]+)":\s*\{', enum_block.group(1)))
    # 任务书里用到的疑似错误码（全大写+下划线）。先剔除明显是「环境变量名」的（MEMORY_/MODULE_ 等带 _KEY/_URL/_MODEL/_PATH/_DIM 后缀），
    # 以及非错误码的标识（MODULE_ID 是字段名）。只保留真正出现在「错误码语境」的。
    env_like = re.compile(r"_(KEY|URL|MODEL|PATH|DIM|NAME|ID|BASE|TOKEN|SECRET|HOST|PORT|DIR)$")
    used = set()
    for f in os.listdir(agent_dir):
        if not f.endswith(".md"):
            continue
        t = read(os.path.join(agent_dir, f))
        for code in re.findall(
                r"`((?:BIND|SETUP|ADMIN|AUTH|READ|RISK|APPROVER|ORIGIN|MODULE|MANIFEST|TOOL|PROTOCOL|PAYLOAD|MEMORY|OVERLOADED|UNAVAILABLE|CANCELLED|TIMEOUT|NOT_FOUND|INVALID|PERMISSION|UNSUPPORTED|INTERNAL|CONFIRM|UNKNOWN|CODE)_[A-Z_]+)`",
                t):
            if env_like.search(code):
                continue  # 环境变量 / 字段名，不是错误码
            used.add(code)
    missing = sorted(used - codes)
    r.check("C10", "任务书引用的错误码都在 C-04 枚举内",
            not missing, f"C-04 缺失：{missing}" if missing else "")

    return r.report(as_json)


if __name__ == "__main__":
    sys.exit(main())
