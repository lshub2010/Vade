#!/usr/bin/env python3
import re, markdown, html as H, os

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "OVERVIEW.md")
OUT = os.path.join(BASE, "..", "overview.html")

md = open(SRC, encoding="utf-8").read()

# ---------- 图形组件 ----------
ARCH = """
<div class="arch">
  <div class="arch-side">
    <div class="node"><b>工具模块</b><small>另一台机器</small></div>
    <div class="node"><b>记忆模块</b><small>本机进程</small></div>
    <div class="node"><b>模型模块</b><small>容器</small></div>
    <div class="node"><b>客户端模块</b><small>桌面 / 手机 / 浏览器</small></div>
  </div>
  <div class="arch-link"><span>WSS :8443</span><em>TLS + Noise</em></div>
  <div class="arch-core">
    <div class="core-head">core（10 个组件）</div>
    <div class="core-grid">
      <span>transport</span><span>crypto-gateway</span><span>registry</span>
      <span>router</span><span>session-store</span><span>agent-loop</span>
      <span>policy / approval</span><span>audit</span><span>admin-ui</span>
      <span class="single">llm-adapter</span>
    </div>
    <div class="core-note">没有组件知道某个具体模块存在</div>
    <div class="core-note">admin-ui 独立 :8444（默认回环，可对外，浏览器只读）· 模块 app 自托管 :8080</div>
  </div>
</div>
"""

FRAME = """
<div class="frame">
  <div class="fld" style="flex:1"><b>ver</b><small>1B</small></div>
  <div class="fld" style="flex:1"><b>type</b><small>1B</small></div>
  <div class="fld" style="flex:1"><b>flags</b><small>1B</small></div>
  <div class="fld" style="flex:1"><b>rsv</b><small>1B</small></div>
  <div class="fld" style="flex:2"><b>payload_len</b><small>4B · BE</small></div>
  <div class="fld" style="flex:2"><b>counter</b><small>8B · BE</small></div>
  <div class="fld wide"><b>ciphertext</b><small>payload_len + 16B tag</small></div>
</div>
"""

SDK = """
<div class="sdk">
  <div class="sdk-top">protocol-core（Rust）<small>Noise · AEAD · 帧 · RPC · seq · 重连</small></div>
  <div class="sdk-mid"></div>
  <div class="sdk-bot">
    <div><b>Kotlin</b><small>JVM / Native</small><em>Compose MP</em></div>
    <div><b>Swift</b><small>iOS App</small><em>SwiftUI</em></div>
    <div><b>Python</b><small>模块 SDK</small><em>—</em></div>
  </div>
</div>
"""

def _rw(tag, cls, name, kids=""):
    return '<div class="t-row"><span class="tag %s">%s</span><span class="t-name">%s</span>%s</div>' % (cls, tag, name, kids)

def _kids(inner, cls=""):
    return '<div class="t-kids %s">%s</div>' % (cls, inner)

_mods = "".join(_rw("M%d" % i, "m", n) for i, n in enumerate(
    ["fs 文件读写", "shell 命令执行", "web 网络抓取",
     "memory 两层 + 演化", "mcp-bridge 生态桥", "model 模型适配"], 1))

_f1 = _rw("F1", "f", "联调 + 安全 + 打包")
_u2 = _rw("U2", "u", "miuix 皮肤")
_u3 = _rw("U3", "u", "glass 皮肤")
_u4 = _rw("U4", "u", "iOS", _kids(_f1))
_u1 = _rw("U1", "u", "UI 骨架 + material", _kids(_u2 + _u3 + _u4))
_c5b = _rw("C5b", "", "拓扑画布")
_c5 = _rw("C5a", "", "审批 / 审计 / admin API", _kids(_c5b))
_c4 = _rw("C4", "p", "Python SDK + echo", _kids(_mods, "m"))
_c3b = _rw("C3b", "", "skill 加载器")
_fork = '<div class="t-fork">' + _c4 + _c5 + _c3b + _u1 + '</div>'
_c3 = _rw("C3", "", "会话 / 队列 / Loop", _kids(_fork))
_c2 = _rw("C2", "", "传输 / 注册表", _kids(_c3))
_c1 = _rw("C1", "", "协议核心（Rust）", _kids(_c2))
_c0 = _rw("C0", "b0", "验证 spike", _kids(_c1))

_legend = ('<div class="legend">'
  '<span><i class="dot a"></i>串行依赖</span>'
  '<span><i class="dot b"></i>core 批次</span>'
  '<span><i class="dot c"></i>模块并行（6 个）</span>'
  '<span><i class="dot d"></i>UI 并行</span>'
  '</div>')


TOPO = """
<div class="topo-wrap"><div class="topo">
  <svg class="topo-lines" viewBox="0 0 720 420" fill="none">
    <path d="M298 192 H204 V86 H180"   class="ln online"/>
    <path d="M298 210 H180"            class="ln degraded"/>
    <path d="M298 228 H204 V334 H180"  class="ln offline"/>
    <path d="M422 192 H516 V86 H540"   class="ln online"/>
    <path d="M422 210 H540"            class="ln online"/>
    <path d="M422 228 H516 V334 H540"  class="ln pending"/>
  </svg>

  <div class="tn core"><b>Agent 核心</b><small>10 个组件<br>私钥仅在此</small></div>

  <div class="tn mod fs"><span class="ic">▤</span><b>fs</b><small>wss://10.0.1.7:9101</small></div>
  <div class="tn mod sh"><span class="ic">▸</span><b>shell</b><small>wss://10.0.1.7:9102</small></div>
  <div class="tn mod wb"><span class="ic">◍</span><b>web</b><small>wss://10.0.1.9:9103</small></div>

  <div class="tn dev ph"><span class="ic">▯</span><b>手机客户端</b><small>wss://core.example:8443</small></div>
  <div class="tn dev pc"><span class="ic">▭</span><b>桌面客户端</b><small>wss://core.example:8443</small></div>
  <div class="tn pend"><span class="ic">＋</span><b>待接入</b><small>配对码 青松-A7K2M9</small></div>
</div></div>
<div class="topo-legend">
  <span><i class="lg on"></i>在线</span>
  <span><i class="lg de"></i>降级（超时/熔断）</span>
  <span><i class="lg of"></i>已配对未连接</span>
  <span><i class="lg rj"></i>握手失败 / 吊销</span>
  <span><i class="lg pe"></i>邀请位</span>
</div>
"""

PARALLEL = '\n<div class="tree">' + _c0 + '</div>' + _legend + '\n'

FIGS = [ARCH, FRAME, SDK, TOPO, PARALLEL]
i = [0]
def sub(m):
    n = i[0]; i[0] += 1
    return "\n\n" + (FIGS[n] if n < len(FIGS) else "") + "\n\n"

md = re.sub(r"```[a-z]*\n.*?```", sub, md, flags=re.S)

body = markdown.markdown(md, extensions=["tables", "fenced_code", "sane_lists", "attr_list"])

# 目录
heads = re.findall(r'<h2>(.*?)</h2>', body)
def slug(t):
    t = re.sub(r"<[^>]+>", "", t)
    return "s%d" % (heads.index(t) if t in heads else 0)
n = [0]
def h2sub(m):
    k = n[0]; n[0] += 1
    return '<h2 id="s%d">%s</h2>' % (k, m.group(1))
body = re.sub(r"<h2>(.*?)</h2>", h2sub, body)
toc = "".join('<a href="#s%d">%s</a>' % (k, re.sub(r"<[^>]+>", "", t)) for k, t in enumerate(heads))

CSS = """
:root{
  --bg:#fbf9f4; --paper:#fff; --ink:#15130f; --ink2:#3a352d; --muted:#7d766a;
  --line:#e6e0d5; --teal:#1f6f6b; --amber:#a8702a;
}
*{box-sizing:border-box}
body{margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.78 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{display:flex; max-width:1420px; margin:0 auto}
aside{width:246px; flex:0 0 246px; padding:46px 0 80px 40px; position:sticky; top:0;
  align-self:flex-start; max-height:100vh; overflow:auto}
aside .brand{font-size:11px; letter-spacing:.14em; color:var(--teal); font-weight:700; margin-bottom:6px}
aside .bn{font-size:19px; font-weight:650; line-height:1.4; margin-bottom:26px}
aside nav a{display:block; color:var(--ink2); text-decoration:none; font-size:13.2px;
  padding:5px 8px 5px 18px; text-indent:-11px; border-radius:5px; line-height:1.5}
aside nav a:hover{background:#f0ebe0; color:var(--teal)}
main{flex:1; min-width:0; max-width:900px; padding:52px 60px 130px}
h1{font-size:33px; line-height:1.25; margin:0 0 10px; letter-spacing:-.015em}
h2{font-size:21px; margin:56px 0 16px; padding-left:14px; border-left:4px solid var(--teal);
  line-height:1.4; letter-spacing:-.01em}
h3{font-size:16.5px; margin:32px 0 10px; color:var(--ink)}
p{margin:13px 0; color:var(--ink2)}
blockquote{margin:18px 0; padding:12px 18px; background:#f5f1e7; border-left:3px solid var(--line);
  color:var(--muted); font-size:13.6px; border-radius:0 6px 6px 0}
blockquote p{margin:0; color:var(--muted)}
hr{border:none; border-top:1px solid var(--line); margin:46px 0}
table{width:100%; border-collapse:collapse; margin:20px 0; font-size:13.1px}
th{text-align:left; padding:9px 11px; border-bottom:1.5px solid #d9d2c4; color:var(--ink);
  font-weight:650; font-size:12.4px; letter-spacing:.02em; background:#faf7f0}
td{padding:9px 11px; border-bottom:1px solid var(--line); vertical-align:top; color:var(--ink2); line-height:1.65}
td:first-child{font-weight:600; color:var(--ink); white-space:nowrap}
tr:last-child td{border-bottom:none}
code{font-family:"SF Mono",Menlo,Consolas,monospace; font-size:12.4px; background:#f2ede1;
  padding:1.5px 5px; border-radius:4px; color:#6b4c1e}
strong{font-weight:650; color:var(--ink)}
a{color:var(--teal)}

/* arch */
.arch{display:grid; grid-template-columns:180px 96px 1fr; align-items:center; gap:0; margin:26px 0}
.arch-side{display:flex; flex-direction:column; gap:8px; justify-content:center}
.node{background:var(--paper); border:1px solid var(--line); border-radius:8px; padding:8px 12px;
  font-size:12.6px; line-height:1.45; box-shadow:0 1px 0 #efe9dc}
.node small{display:block; color:var(--muted); font-size:11px; margin-top:1px}
.arch-link{position:relative; margin:0 6px; height:54px; display:flex; flex-direction:column;
  justify-content:center; align-items:center; gap:2px}
.arch-link::before{content:""; position:absolute; left:0; right:0; top:50%; border-top:1px dashed #c9bda6}
.arch-link span{position:relative; z-index:1; background:var(--bg); padding:0 4px;
  font-size:10.8px; color:var(--teal); font-weight:700; letter-spacing:.1em; line-height:1.4}
.arch-link em{position:relative; z-index:1; background:var(--bg); padding:0 4px; font-style:normal;
  font-size:9.8px; color:var(--muted); line-height:1.35; letter-spacing:.02em}
.arch-core{background:var(--paper); border:1px solid var(--line); border-radius:10px; padding:14px 16px;
  box-shadow:0 1px 0 #efe9dc}
.core-head{font-size:12px; font-weight:700; letter-spacing:.06em; color:var(--teal); margin-bottom:10px}
.core-grid{display:grid; grid-template-columns:repeat(3,1fr); gap:6px}
.core-grid span{font-family:"SF Mono",Menlo,monospace; font-size:11.2px; background:#f7f4ec;
  border:1px solid #ece5d7; border-radius:5px; padding:5px 7px; text-align:center; color:var(--ink2)}
.core-grid .single{grid-column:1 / -1}
.core-note{font-size:11.4px; color:var(--muted); margin-top:9px}

/* frame */
.frame{display:flex; gap:3px; margin:24px 0 8px}
.fld{background:var(--paper); border:1px solid var(--line); border-radius:5px; padding:9px 4px;
  text-align:center; min-width:0}
.fld b{display:block; font-family:"SF Mono",Menlo,monospace; font-size:11.4px; color:var(--ink)}
.fld small{display:block; font-size:10px; color:var(--muted); margin-top:2px}
.fld.wide{flex:4; background:#f2f8f7; border-color:#cfe2df}
.cap{font-size:12.2px; color:var(--muted); margin:0 0 22px}

/* sdk */
.sdk{margin:24px 0}
.sdk-top{background:var(--paper); border:1px solid var(--line); border-radius:8px; padding:11px 15px;
  font-weight:650; font-size:13.2px}
.sdk-top small{font-weight:400; color:var(--muted); font-size:11.6px; margin-left:10px}
.sdk-mid{position:relative; text-align:center; padding:9px 0; font-size:10.8px;
  letter-spacing:.14em; font-weight:700}
.sdk-mid::before{content:""; position:absolute; left:0; right:0; top:50%; border-top:1px dashed #cdc2ac}
.sdk-mid::after{content:"UniFFI"; position:relative; background:var(--bg); padding:0 10px; color:var(--teal)}
.sdk-bot{display:grid; grid-template-columns:repeat(3,1fr); gap:10px}
.sdk-bot>div{background:var(--paper); border:1px solid var(--line); border-radius:8px; padding:10px 12px;
  text-align:center; font-size:12.6px}
.sdk-bot b{display:block}
.sdk-bot small{display:block; color:var(--muted); font-size:10.8px; margin-top:1px}
.sdk-bot em{display:block; font-style:normal; font-size:10.8px; color:var(--teal); margin-top:4px}

/* tree */
.tree{margin:22px 0 4px; font-size:13px}
.t-row{padding:3.6px 0; display:block; line-height:1.55}
.t-row>.tag{display:inline-block; margin-right:9px; vertical-align:1.5px}
.t-row>.t-name{display:inline}
.t-kids{display:block; margin-left:7px; padding-left:18px; border-left:1px solid #ded6c6}
.t-fork{display:block; border-left:2.5px solid #cfe2df; margin-left:-2.5px; padding-left:17px}
.tag{font-family:"SF Mono",Menlo,monospace; font-size:10.6px; font-weight:700; padding:1.5px 6px;
  border-radius:4px; background:#eee8db; color:#6b6255; flex:0 0 auto}
.tag.p{background:#e3f0ee; color:var(--teal)}
.tag.b0{background:#f6e8d3; color:var(--amber)}
.tag.m{background:#e3f0ee; color:var(--teal)}
.tag.u{background:#f3e6d2; color:#8a5f22}
.tag.f{background:#2b2721; color:#f7f3ea}
.t-name{color:var(--ink2)}
.legend{display:flex; gap:16px; margin:8px 0 24px; font-size:11.6px; color:var(--muted); align-items:center}
.legend span{display:flex; align-items:center; gap:5px}
.t-kids.m{border-left:2px solid #cfe2df}
.dot{width:7px; height:7px; border-radius:50%; display:inline-block}
.dot.a{background:#c9c0ae}
.dot.b{background:#b9b0a0}
.dot.c{background:var(--teal)}
.dot.d{background:#c08a3e}
.tree .t-kids{margin-top:1px}


.topo-wrap{overflow-x:auto; margin:18px 0 4px}
.topo{position:relative; width:720px; height:420px}
.topo-lines{position:absolute; inset:0; width:720px; height:420px; z-index:1}
.ln{stroke-width:1.7; stroke-linecap:round; stroke-linejoin:round}
.ln.online{stroke:#2f7d5e}
.ln.degraded{stroke:#c08a3e; stroke-dasharray:7 5; animation:flow 1.1s linear infinite}
.ln.offline{stroke:#b8b0a2; stroke-dasharray:5 5}
.ln.pending{stroke:#3b6ea5; stroke-dasharray:2 5}
@keyframes flow{to{stroke-dashoffset:-12}}
.tn{position:absolute; z-index:3; width:158px; padding:8px 11px 9px; background:#fff;
  border:1px solid var(--line); border-radius:9px; box-shadow:0 1px 3px rgba(60,45,20,.06)}
.tn b{display:block; font-size:12.8px; color:var(--ink); font-weight:650; line-height:1.35}
.tn small{display:block; font-size:9.7px; color:var(--muted); margin-top:2px;
  font-family:"SF Mono",Menlo,Consolas,monospace; white-space:nowrap;
  overflow:hidden; text-overflow:ellipsis; letter-spacing:-.01em}
.tn .ic{position:absolute; right:8px; top:7px; font-size:11px; color:#b3aca2}
.tn.core{left:298px; top:178px; width:124px; padding:12px 10px 13px; text-align:center;
  background:#2b2721; border-color:#2b2721; box-shadow:0 1px 4px rgba(43,39,33,.16)}
.tn.core b{color:#f7f3ea; font-size:13.4px}
.tn.core small{color:#b5ab99; white-space:normal; line-height:1.55; font-size:9.8px}
.tn.fs{left:22px; top:58px}
.tn.sh{left:22px; top:182px}
.tn.wb{left:22px; top:306px}
.tn.ph{left:540px; top:58px}
.tn.pc{left:540px; top:182px}
.tn.pend{left:540px; top:306px; border-style:dashed; background:#fbfaf6; box-shadow:none}
.tn.pend b{color:var(--muted)}
.tn.fs{border-left:3px solid #2f7d5e}
.tn.sh{border-left:3px solid #c08a3e}
.tn.wb{border-left:3px solid #b8b0a2}
.tn.ph,.tn.pc{border-left:3px solid #2f7d5e}
.topo-legend{display:flex; flex-wrap:wrap; gap:14px; font-size:11.2px; color:var(--muted); margin:6px 0 26px}
.topo-legend span{display:flex; align-items:center; gap:5px}
.lg{width:16px; height:0; border-top-width:1.8px; border-top-style:solid}
.lg.on{border-color:#2f7d5e}
.lg.de{border-color:#c08a3e; border-top-style:dashed}
.lg.of{border-color:#b8b0a2; border-top-style:dashed}
.lg.rj{border-color:#b4524a; border-top-style:dotted; border-top-width:2.4px}
.lg.pe{border-color:#3b6ea5; border-top-style:dotted}
footer{margin-top:70px; padding-top:18px; border-top:1px solid var(--line);
  font-size:11.8px; color:var(--muted); display:flex; justify-content:space-between}
@media (max-width:1080px){
  aside{display:none}
  main{padding:36px 22px 90px; max-width:none}
  .arch{grid-template-columns:1fr; gap:12px}
  .arch-link{border:none; height:22px}
  .arch-link span{top:-4px}
  .arch-link em{display:none}
  .core-grid{grid-template-columns:repeat(2,1fr)}
  .frame{flex-wrap:wrap}
}
@media print{
  aside{display:none} main{max-width:none; padding:0}
  h2{page-break-after:avoid} table,.arch,.sdk,.topo-wrap{page-break-inside:avoid}
}
"""

HTML = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vademecum（Vade）开发概览</title><style>{CSS}</style></head>
<body><div class="wrap">
<aside>
  <div class="brand">VADEMECUM</div>
  <div class="bn">开发概览<br><span style="font-size:12px;color:var(--muted);font-weight:400">需求 + 方案 汇总视图</span></div>
  <nav>{toc}</nav>
</aside>
<main>
{body}
<footer><span>Vademecum（Vade） · 方案 v1.17 · 需求基线 AMEND-19</span><span>2026-11-10</span></footer>
</main></div></body></html>"""

open(OUT, "w", encoding="utf-8").write(HTML)
print("written", OUT, len(HTML))
