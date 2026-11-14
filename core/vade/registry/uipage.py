"""C-19 (RFC-0009): declarative module pages — the ONE validator.

Everything a module can put on a page is decided here, and every violation is
**fail-closed**: an unknown node type, an unknown component, an unknown action
kind, a script-shaped key, a binding outside the three read-only roots or a
limit breach makes the whole page untrusted. There is no "render the rest"
path (D109 / FR-8.78 收窄, RFC-0009 §7.2): a half-rendered page is a layout the
module did not design, and the negotiation it invites ("show my half first") is
worse than an error card.

Three checkpoints share these rules (RFC §7.1):

* registration — `registry/manifest.py` calls :func:`validate_ui`; a bad page is
  dropped with a warning, the module's other capabilities still register;
* fetch — `ui/page.get` for `ref` pages re-runs :func:`validate_page` after the
  sha256 check;
* render — the clients run the same table against the JSON they received.

Nothing here evaluates anything: bindings are paths (≤4 segments, `[a-z0-9_]`),
never expressions. There is deliberately no evaluator in this file, which is
what makes "no arbitrary script" a property of the code rather than a promise
(§10.1).

No `eval`, no template engine, no WebView, no remote resource: `props` are
closed sets per node type/component and the only URL a page can name is an
`open_url` action target restricted to http/https (opened by the host browser,
never loaded into a page).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, NoReturn, Sequence
from urllib.parse import urlsplit

from ..errors import VadeError

__all__ = [
    "UIPageError",
    "UIAction",
    "UINode",
    "UIPage",
    "validate_page",
    "validate_ui",
    "serialize",
    "page_digest",
    "validate_binding_path",
    "check_navigate",
    "check_open_url",
    "check_state",
    "PAGE_ID_RE",
    "NODE_TYPES",
    "COMPONENTS",
    "HOST_COMPONENTS",
    "ACTION_KINDS",
    "BINDING_ROOTS",
    "BINDING_FORMATS",
    "NAVIGATE_SETTINGS_TABS",
    "OPEN_URL_SCHEMES",
    "MAX_PAGES",
    "MAX_NODES",
    "MAX_DEPTH",
    "MAX_CHILDREN",
    "MAX_TEXT_CHARS",
    "MAX_ACTIONS_PER_NODE",
    "MAX_ACTIONS_PER_PAGE",
    "MAX_TITLE_CHARS",
    "MAX_INLINE_BYTES",
    "MAX_REF_BYTES",
    "MAX_UI_BLOCK_BYTES",
    "MAX_LIST_ITEMS",
    "MAX_PATH_SEGMENTS",
    "MAX_STATE_BYTES",
    "MAX_STATE_KEYS",
    "MAX_PAGE_CACHE_BYTES",
    "MAX_MODULE_CACHE_BYTES",
    "MAX_REFRESH_S",
]

# --------------------------------------------------------------------- limits
#: RFC-0009 §7.3. Registration drops a page, render refuses it; neither
#: truncates, because a truncated page is a validation failure wearing a
#: normal-looking face.
MAX_PAGES = 8
MAX_NODES = 256
MAX_DEPTH = 8
MAX_CHILDREN = 32
MAX_TEXT_CHARS = 4096
MAX_ACTIONS_PER_NODE = 4
MAX_ACTIONS_PER_PAGE = 64
MAX_TITLE_CHARS = 40
MAX_INLINE_BYTES = 8 * 1024
MAX_REF_BYTES = 64 * 1024
MAX_UI_BLOCK_BYTES = 48 * 1024
MAX_LIST_ITEMS = 512
MAX_PATH_SEGMENTS = 4
MAX_STATE_BYTES = 8 * 1024
MAX_STATE_KEYS = 64
MAX_PAGE_CACHE_BYTES = 64 * 1024
MAX_MODULE_CACHE_BYTES = 512 * 1024
MAX_REFRESH_S = 3600
MAX_ACTIONS_CONTENT = 20

PAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
NODE_ID_RE = PAGE_ID_RE
PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_]{1,32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: RFC-0009 §4.3. Four layout primitives + one text leaf + `component` refs.
PRIMITIVES = ("column", "row", "tabs", "list", "text")
NODE_TYPES = PRIMITIVES + ("component",)

#: RFC-0009 §4.5 rev 1 — the only seven names a page may use. `D93`'s other
#: four are host components, not page components; naming one is phishing, so
#: it is rejected with a message that says exactly that.
COMPONENTS = ("Button", "TextField", "Card", "ListItem", "Switch", "ToolCallCard", "AttachmentChip")
HOST_COMPONENTS = frozenset({"MessageBubble", "InputBar", "ApprovalDialog", "BottomBar"})

#: RFC §6's three kinds, plus `open_url` required by the C-19 implementation
#: brief. `open_url` never loads a remote resource into a page: the host opens
#: the user's browser and only http/https survive validation (§10.1).
ACTION_KINDS = ("navigate", "tools/call", "ui/submit", "open_url")

NAVIGATE_SETTINGS_TABS = frozenset({"appearance", "models", "devices", "about"})
OPEN_URL_SCHEMES = frozenset({"http", "https"})

#: RFC §5.1: exactly three roots. A fourth root would mean "read core", not
#: "read this module's state" (§5.1's closing paragraph).
BINDING_ROOTS = ("module.status", "page.state", "item")
MODULE_STATUS_FIELDS = frozenset({"online", "version", "instance_id"})

#: §5.2: presentation-only formats. Unknown ones degrade to text (§7.2).
BINDING_FORMATS = frozenset({"text", "int", "decimal", "percent", "bytes", "datetime", "relative"})

#: §6.5: hosts a page may not send the user to. A page is untrusted input; a
#: deep link into the router's admin panel or a loopback service is a
#: same-origin request the user cannot see coming, and `169.254.169.254` is the
#: cloud metadata endpoint. Names are compared after `.`-splitting so
#: `127.0.0.1.evil.com` is still allowed (it resolves to evil.com, not loopback)
#: while `127.0.0.1` and `[::1]` are not.
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain")
BLOCKED_HOST_EXACT = frozenset({"localhost"})
PRIVATE_IPV4_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "0.", "255.")
#: 172.16.0.0/12 — only the second octet decides, so it is a range check.
PRIVATE_IPV4_SECOND_OCTETS = range(16, 32)
_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
#: `127`, `127.1`, `10.1.2.3.4` — everything `inet_aton` would treat as a literal.
_DOTTED_NUMERIC_RE = re.compile(r"^\d{1,3}(\.\d{1,3})*$")

#: §7.2: any key whose meaning could be "run something". Compared after
#: lower-casing and dropping `_`/`-`, so `onClick`, `on_click` and `ON-CLICK`
#: are one entry — a normalisation the attacker cannot dodge by spelling.
SCRIPT_KEYS = frozenset({
    "script", "scriptsrc", "eval", "js", "javascript", "onload", "onerror",
    "onclick", "onchange", "onsubmit", "html", "innerhtml", "outerhtml",
    "template", "expression", "lambda", "function", "fn", "url", "src", "href",
    "styleurl", "stylesheet", "iframe", "webview", "object", "embed", "import",
})

_PAGE_KEYS = frozenset({"id", "title", "icon", "entry", "version", "requires", "schema", "refresh_s"})
_ACTION_KEYS = {
    "navigate": frozenset({"kind", "to"}),
    "open_url": frozenset({"kind", "to"}),
    "tools/call": frozenset({"kind", "tool", "args", "confirm_text"}),
    "ui/submit": frozenset({"kind", "content"}),
}
_EVENTS = {
    "Button": ("click",),
    "TextField": ("change",),
    "Card": ("click",),
    "ListItem": ("click",),
    "Switch": ("change",),
    "ToolCallCard": ("click",),
    "AttachmentChip": ("click",),
}
_ENTRY_VALUES = ("auto", "panel", "capabilities")
_ICONS = ("list", "chart", "settings", "tool", "file", "none")
_TEXT_STYLES = ("title", "subtitle", "body", "caption", "mono")
_BUTTON_STYLES = ("primary", "secondary", "danger", "text")
_TOOL_STATES = ("running", "ok", "failed", "waiting_approval")
_GAPS = ("none", "s", "m", "l")
_ALIGNS = ("start", "center", "end")


class UIPageError(VadeError):
    """`MANIFEST_INVALID` at a JSON Pointer, with up to three located reasons.

    `locate` is what the error card shows (§7.1: 模块名 / 页面标题 / 最多 3 条定位);
    `pointer` is the same string the registration warning carries, so a module
    author can find the node without guessing.
    """

    def __init__(self, pointer: str, message: str) -> None:
        self.pointer = pointer
        self.reason = message
        super().__init__("MANIFEST_INVALID", f"{pointer or '/'}: {message}")


# --------------------------------------------------------------------- shapes
@dataclass(frozen=True, slots=True)
class UIAction:
    """One `on.<event>` action (RFC §6). Validated, never executed here."""

    kind: str
    pointer: str
    #: `navigate`/`open_url`: the `to` string; `tools/call`: the tool name.
    target: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    content: tuple[Mapping[str, Any], ...] = ()
    confirm_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.target is not None:
            out["to" if self.kind in ("navigate", "open_url") else "tool"] = self.target
        if self.args:
            out["args"] = dict(self.args)
        if self.content:
            out["content"] = [dict(part) for part in self.content]
        if self.confirm_text is not None:
            out["confirm_text"] = self.confirm_text
        return out


@dataclass(frozen=True, slots=True)
class UINode:
    """A validated node. `children` is empty for leaves by construction."""

    type: str
    pointer: str
    id: str | None = None
    component: str | None = None
    data_ref: str | None = None
    props: Mapping[str, Any] = field(default_factory=dict)
    visible: Any = None
    children: tuple["UINode", ...] = ()
    actions: tuple[tuple[str, UIAction], ...] = ()

    @property
    def event_names(self) -> tuple[str, ...]:
        return tuple(event for event, _action in self.actions)


@dataclass(frozen=True, slots=True)
class UIPage:
    """One accepted page (`manifest.ui.pages[i]`)."""

    page_id: str
    title: str
    version: int
    schema: Mapping[str, Any]
    pointer: str
    entry: str = "auto"
    icon: str = "none"
    requires_session: bool = False
    refresh_s: int = 0
    root: UINode | None = None
    #: `(page_id, sha256)` when `schema` is `{"ref": ...}`; None when inline.
    ref: tuple[str, str] | None = None
    warnings: tuple[str, ...] = ()

    @property
    def is_ref(self) -> bool:
        return self.ref is not None

    def component_names(self) -> tuple[str, ...]:
        """Every `component` name used, in document order (tests + entry gating)."""
        return tuple(node.component for node in self.walk() if node.component is not None)

    def actions(self) -> tuple[UIAction, ...]:
        """Every action of the page (≤64), in document order."""
        out: list[UIAction] = []
        for node in self.walk():
            out.extend(action for _event, action in node.actions)
        return tuple(out)

    def walk(self) -> Iterable[UINode]:
        if self.root is None:
            return ()
        stack = [self.root]
        ordered: list[UINode] = []
        while stack:
            node = stack.pop()
            ordered.append(node)
            stack.extend(reversed(node.children))
        return ordered


# ------------------------------------------------------------------ utilities
def serialize(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def page_digest(schema: Mapping[str, Any]) -> str:
    return hashlib.sha256(serialize(schema)).hexdigest()


def _safe_size(value: Any) -> int | None:
    """Serialised size, or None when the value contains a cycle.

    `json.dumps` raises on a self-referential dict; a validator that lets that
    escape has failed open with a traceback instead of a refusal.
    """
    try:
        return len(serialize(value))
    except (ValueError, TypeError, RecursionError):
        return None


def _norm_key(key: str) -> str:
    return str(key).lower().replace("_", "").replace("-", "")


def _fail(pointer: str, message: str) -> NoReturn:
    raise UIPageError(pointer, message)


def _require_str(value: Any, pointer: str, *, minimum: int = 0, maximum: int | None = None) -> str:
    if not isinstance(value, str):
        _fail(pointer, f"must be a string, got {type(value).__name__}")
    if len(value) < minimum:
        _fail(pointer, f"must be at least {minimum} characters")
    if maximum is not None and len(value) > maximum:
        _fail(pointer, f"length {len(value)} > {maximum}")
    return value


def _require_int(value: Any, pointer: str, *, minimum: int, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(pointer, f"must be an integer, got {type(value).__name__}")
    if value < minimum:
        _fail(pointer, f"value {value} < minimum {minimum}")
    if maximum is not None and value > maximum:
        _fail(pointer, f"value {value} > maximum {maximum}")
    return value


def _require_mapping(value: Any, pointer: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(pointer, f"must be an object, got {type(value).__name__}")
    return value


def _require_list(value: Any, pointer: str) -> Sequence[Any]:
    if not isinstance(value, (list, tuple)):
        _fail(pointer, f"must be an array, got {type(value).__name__}")
    return value


# ------------------------------------------------------------- script scanning
def _scan_forbidden(value: Any, pointer: str, seen: set[int] | None = None) -> None:
    """Any script-shaped key OR inline `<script` anywhere in the page.

    Structural unknown keys are already refused by the closed `props` sets; this
    runs first so `{"props": {"on_load": "..."}}` is reported as *script*, not as
    an unknown prop — the error a module author (or a reviewer) needs to see.

    `seen` rejects a **cyclic** structure instead of recursing to RecursionError.
    A JSON frame cannot carry a cycle, but a manifest built in-process (or a
    malicious decoder) can, and "validator crashes" is not a refusal.
    """
    if seen is None:
        seen = set()
    container = isinstance(value, (Mapping, list, tuple))
    identity = id(value) if container else 0
    if container:
        if identity in seen:
            _fail(pointer, "cyclic structure in a page (C-19 fail-closed)")
        seen.add(identity)
    try:
        if isinstance(value, Mapping):
            for key, sub in value.items():
                if _norm_key(key) in SCRIPT_KEYS and not str(key).startswith("x-"):
                    _fail(f"{pointer}/{key}", "script-shaped key is forbidden (C-19 fail-closed)")
                _scan_forbidden(sub, f"{pointer}/{key}", seen)
        elif isinstance(value, (list, tuple)):
            for index, sub in enumerate(value):
                _scan_forbidden(sub, f"{pointer}/{index}", seen)
        elif isinstance(value, str) and "<script" in value.lower():
            _fail(pointer, "inline <script is forbidden (C-19 fail-closed)")
    finally:
        # Only the current path matters: the same dict reused twice as siblings
        # is legal JSON (a shared fragment), a dict containing itself is not.
        if container:
            seen.discard(identity)


# ---------------------------------------------------------------- data binding
def validate_binding_path(path: Any, *, allow_item: bool, pointer: str) -> str:
    """§5.1/§5.2: a path inside one of exactly three roots, ≤4 segments.

    `session.*`, `audit.*`, another module's `page.state` — everything else is
    rejected here, at registration, instead of being rendered as an empty value
    at runtime. The delegation brief is explicit: a page may not *ask* to read
    something outside the three roots.
    """
    text = _require_str(path, pointer, minimum=1)
    segments = text.split(".")
    if len(segments) > MAX_PATH_SEGMENTS:
        _fail(pointer, f"binding path {text!r} has {len(segments)} segments > {MAX_PATH_SEGMENTS}")
    for index, segment in enumerate(segments):
        if PATH_SEGMENT_RE.match(segment) is None:
            _fail(
                pointer,
                f"binding segment {index} {segment!r} violates ^[a-z0-9_]{{1,32}}$ "
                "(no case, no brackets, no wildcards)",
            )
    root = ".".join(segments[:2])
    if root == "module.status":
        if len(segments) != 3 or segments[2] not in MODULE_STATUS_FIELDS:
            _fail(
                pointer,
                f"module.status binds exactly online|version|instance_id, got {text!r}",
            )
    elif root == "page.state":
        if not 3 <= len(segments) <= MAX_PATH_SEGMENTS:
            _fail(pointer, f"page.state needs a field, got {text!r}")
    elif segments[0] == "item":
        if not allow_item:
            _fail(pointer, "item.* is only legal inside a `list` template child")
        if not 2 <= len(segments) <= MAX_PATH_SEGMENTS:
            _fail(pointer, f"item.* needs a field, got {text!r}")
    else:
        _fail(
            pointer,
            f"binding root {segments[0]!r} is not allowed; only "
            + " / ".join(BINDING_ROOTS)
            + " may be read (C-19 §5.1)",
        )
    return text


def _bind(value: Any, *, pointer: str, allow_item: bool, warnings: list[str]) -> str | None:
    """Validate `{"bind": path, "format"?: ...}`; returns the path.

    An unknown `format` is decorative (§7.2): keep the binding, drop the format,
    record one warning. Anything else is structural and rejects the page.
    """
    mapping = _require_mapping(value, pointer)
    extra = set(mapping) - {"bind", "format"}
    if extra:
        _fail(f"{pointer}", f"binding value has unknown source key(s) {sorted(extra)}")
    if "bind" not in mapping:
        _fail(pointer, "binding value needs `bind`")
    path = validate_binding_path(mapping["bind"], allow_item=allow_item, pointer=f"{pointer}/bind")
    fmt = mapping.get("format")
    if fmt is not None:
        if not isinstance(fmt, str):
            _fail(f"{pointer}/format", f"format must be a string, got {type(fmt).__name__}")
        if fmt not in BINDING_FORMATS:
            warnings.append(f"{pointer}/format: unknown format {fmt!r} renders as text")
    return path


# ---------------------------------------------------------------------- props
#: `(name, spec)` per node type / component. The closed set IS the security
#: property (§4.5): an unknown prop key is rejected, never ignored.
_PropSpec = tuple[Any, ...]

PROP_SPECS: dict[tuple[str, str | None], dict[str, _PropSpec]] = {
    ("column", None): {"gap": ("enum", _GAPS)},
    ("row", None): {"gap": ("enum", _GAPS), "align": ("enum", _ALIGNS), "wrap": ("bool",)},
    ("tabs", None): {"selected": ("int", 0, MAX_CHILDREN)},
    ("list", None): {"empty_text": ("str", 0, 200), "item_fields": ("str_list",)},
    ("text", None): {
        "text": ("str", 0, MAX_TEXT_CHARS),
        "style": ("enum", _TEXT_STYLES),
        "max_lines": ("int", 1, 8),
        "format": ("format",),
    },
    ("component", "Button"): {
        "text": ("str", 0, 80),
        "style": ("enum", _BUTTON_STYLES),
        "enabled": ("bool",),
    },
    ("component", "TextField"): {
        "label": ("str", 0, 80),
        "placeholder": ("str", 0, 80),
        "value": ("input",),
        "multiline": ("bool",),
        "max_length": ("int", 1, 4096),
    },
    ("component", "Card"): {"title": ("str", 0, 120), "subtitle": ("str", 0, 200), "elevated": ("bool",)},
    ("component", "ListItem"): {
        "title": ("str", 0, 200),
        "subtitle": ("str", 0, 200),
        "leading_icon": ("str", 0, 64),
        "trailing_text": ("str", 0, 64),
    },
    ("component", "Switch"): {"label": ("str", 0, 120), "enabled": ("bool",)},
    ("component", "ToolCallCard"): {
        "tool": ("str", 0, 64),
        "state": ("enum", _TOOL_STATES),
        "args_preview": ("str", 0, 512),
        "result_summary": ("str", 0, 512),
    },
    ("component", "AttachmentChip"): {
        "file_id": ("str", 0, 128),
        "mime": ("str", 0, 128),
        "name": ("str", 0, 200),
        "state": ("str", 0, 32),
    },
}


def _prop_value(spec: _PropSpec, value: Any, *, pointer: str, allow_item: bool, warnings: list[str]) -> Any:
    kind = spec[0]
    if kind == "input":
        # `$input` is an action parameter, not state (§5.3, N2): the only place
        # it may appear is TextField.value, and only as {"input": id}.
        mapping = _require_mapping(value, pointer)
        if set(mapping) != {"input"}:
            _fail(pointer, 'TextField.value accepts exactly {"input": "<id>"} (no $input in state)')
        input_id = mapping["input"]
        if not isinstance(input_id, str) or PATH_SEGMENT_RE.match(input_id) is None:
            _fail(f"{pointer}/input", "input id must match ^[a-z0-9_]{1,32}$")
        return {"input": input_id}
    if isinstance(value, Mapping):
        _bind(value, pointer=pointer, allow_item=allow_item, warnings=warnings)
        return dict(value)
    if kind == "bool":
        if not isinstance(value, bool):
            _fail(pointer, f"must be a boolean, got {type(value).__name__}")
        return value
    if kind == "int":
        return _require_int(value, pointer, minimum=int(spec[1]), maximum=int(spec[2]) if spec[2] is not None else None)
    if kind == "enum":
        if not isinstance(value, str) or value not in spec[1]:
            _fail(pointer, f"must be one of {list(spec[1])}, got {value!r}")
        return value
    if kind == "format":
        if not isinstance(value, str):
            _fail(pointer, f"format must be a string, got {type(value).__name__}")
        if value not in BINDING_FORMATS:
            warnings.append(f"{pointer}: unknown format {value!r} renders as text")
            return "text"
        return value
    if kind == "str_list":
        items = _require_list(value, pointer)
        out: list[str] = []
        for index, item in enumerate(items):
            if not isinstance(item, str) or PATH_SEGMENT_RE.match(item) is None:
                _fail(f"{pointer}/{index}", "must match ^[a-z0-9_]{1,32}$")
            out.append(item)
        if len(out) > 64:
            _fail(pointer, f"{len(out)} item_fields > 64")
        return out
    if kind == "str":
        return _require_str(value, pointer, minimum=int(spec[1]), maximum=int(spec[2]))


def _validate_props(
    raw: Any,
    *,
    node_type: str,
    component: str | None,
    pointer: str,
    allow_item: bool,
    allow_tab_title: bool,
    warnings: list[str],
) -> dict[str, Any]:
    props = _require_mapping(raw, pointer) if raw is not None else {}
    specs = PROP_SPECS[(node_type, component)]
    allowed = set(specs) | ({"tab_title"} if allow_tab_title else set())
    unknown = sorted(set(props) - allowed)
    if unknown:
        _fail(f"{pointer}/{unknown[0]}", f"unknown prop key(s) {unknown}; props are a closed set (C-19 §4.4)")
    out: dict[str, Any] = {}
    for key, value in props.items():
        if key == "tab_title":
            out[key] = _require_str(value, f"{pointer}/{key}", minimum=1, maximum=40)
            continue
        out[key] = _prop_value(
            specs[key], value, pointer=f"{pointer}/{key}", allow_item=allow_item, warnings=warnings
        )
    if allow_tab_title and "tab_title" not in out:
        _fail(f"{pointer}/tab_title", "every tabs child needs props.tab_title (1..40)")
    return out


# -------------------------------------------------------------------- actions
def _validate_arg_value(
    value: Any, *, pointer: str, allow_item: bool, warnings: list[str], depth: int = 0
) -> Any:
    """§6.4: literal scalars/arrays, `{"bind"}` or `{"input"}`. No nested objects."""
    if isinstance(value, Mapping):
        keys = set(value)
        if "bind" in keys:
            _bind(value, pointer=pointer, allow_item=allow_item, warnings=warnings)
            return dict(value)
        if "input" in keys:
            if keys - {"input", "as"}:
                _fail(pointer, f"input argument has unknown key(s) {sorted(keys - {'input', 'as'})}")
            input_id = value["input"]
            if not isinstance(input_id, str) or PATH_SEGMENT_RE.match(input_id) is None:
                _fail(f"{pointer}/input", "input id must match ^[a-z0-9_]{1,32}$")
            as_type = value.get("as")
            if as_type not in (None, "int", "str"):
                _fail(f"{pointer}/as", f"input coercion must be int|str, got {as_type!r}")
            return dict(value)
        _fail(pointer, f"unknown argument source key(s) {sorted(keys)}; only bind/input are allowed")
    if isinstance(value, (list, tuple)):
        if depth >= 1:
            _fail(pointer, "nested arrays are not a literal argument (C-19 §6.4)")
        return [
            _validate_arg_value(item, pointer=f"{pointer}/{index}", allow_item=allow_item,
                                warnings=warnings, depth=depth + 1)
            for index, item in enumerate(value)
        ]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    _fail(pointer, f"argument must be string|number|bool|null, got {type(value).__name__}")


def _validate_action(
    raw: Any,
    *,
    pointer: str,
    module_tools: Sequence[str],
    page_ids: Sequence[str],
    allow_item: bool,
    warnings: list[str],
) -> UIAction:
    action = _require_mapping(raw, pointer)
    kind = action.get("kind")
    if not isinstance(kind, str):
        _fail(f"{pointer}/kind", "action needs a string `kind`")
    if kind not in ACTION_KINDS:
        _fail(
            f"{pointer}/kind",
            f"unknown action kind {kind!r}; only {list(ACTION_KINDS)} are allowed (C-19 §6)",
        )
    unknown = sorted(set(action) - _ACTION_KEYS[kind])
    if unknown:
        _fail(f"{pointer}/{unknown[0]}", f"unknown key(s) {unknown} on a {kind!r} action")

    if kind in ("navigate", "open_url"):
        target = _require_str(action.get("to"), f"{pointer}/to", minimum=1, maximum=2048)
        if kind == "navigate":
            check_navigate(target, pointer=f"{pointer}/to", page_ids=page_ids)
        else:
            check_open_url(target, pointer=f"{pointer}/to")
        return UIAction(kind=kind, pointer=pointer, target=target)

    if kind == "tools/call":
        tool = _require_str(action.get("tool"), f"{pointer}/tool", minimum=1, maximum=64)
        if TOOL_NAME_RE.match(tool) is None:
            _fail(f"{pointer}/tool", f"tool {tool!r} must match ^[a-z][a-z0-9_]{{0,63}}$")
        if tool not in module_tools:
            # §6.2: a page may only call tools its own module declared. Owning
            # the page is what authorises the call, so an undeclared name is a
            # manifest lie, not a runtime 404.
            _fail(
                f"{pointer}/tool",
                f"tool {tool!r} is not declared in capabilities.tools of this module",
            )
        args_raw = action.get("args", {})
        args_node = _require_mapping(args_raw, f"{pointer}/args")
        if len(args_node) > 64:
            _fail(f"{pointer}/args", f"{len(args_node)} args > 64")
        args = {
            name: _validate_arg_value(value, pointer=f"{pointer}/args/{name}",
                                      allow_item=allow_item, warnings=warnings)
            for name, value in args_node.items()
        }
        confirm = action.get("confirm_text")
        if confirm is not None:
            confirm = _require_str(confirm, f"{pointer}/confirm_text", minimum=1, maximum=120)
        return UIAction(kind=kind, pointer=pointer, target=tool, args=args, confirm_text=confirm)

    # ui/submit
    parts_raw = _require_list(action.get("content"), f"{pointer}/content")
    if not 1 <= len(parts_raw) <= MAX_ACTIONS_CONTENT:
        _fail(f"{pointer}/content", f"content needs 1..{MAX_ACTIONS_CONTENT} parts")
    parts: list[Mapping[str, Any]] = []
    for index, part_raw in enumerate(parts_raw):
        part_pointer = f"{pointer}/content/{index}"
        part = _require_mapping(part_raw, part_pointer)
        if part.get("type") != "text":
            # Attachments are out in v1 (N5/Q4): only text parts reach ui/submit.
            _fail(f"{part_pointer}/type", "only type:\"text\" is allowed in a ui/submit part")
        unknown = set(part) - {"type", "text"}
        if unknown:
            _fail(f"{part_pointer}", f"unknown part key(s) {sorted(unknown)}")
        text = part.get("text")
        if isinstance(text, Mapping):
            _validate_arg_value(text, pointer=f"{part_pointer}/text", allow_item=allow_item, warnings=warnings)
            parts.append({"type": "text", "text": dict(text)})
        else:
            parts.append({"type": "text", "text": _require_str(text, f"{part_pointer}/text", minimum=1,
                                                              maximum=MAX_TEXT_CHARS)})
    return UIAction(kind=kind, pointer=pointer, content=tuple(parts))


def check_navigate(target: str, *, pointer: str, page_ids: Sequence[str]) -> None:
    if target.startswith("module:"):
        page_id = target[len("module:"):]
        if PAGE_ID_RE.match(page_id) is None:
            _fail(pointer, f"module target {page_id!r} is not a page id (^[a-z][a-z0-9_-]{{0,31}}$)")
        if page_id not in page_ids:
            _fail(
                pointer,
                f"navigate target {target!r} is not a page declared by this module "
                f"(declared: {sorted(page_ids)})",
            )
        return
    if target == "session:current":
        return
    if target.startswith("session:"):
        token = target[len("session:"):]
        if SESSION_TOKEN_RE.match(token) is None:
            _fail(pointer, f"session target {token!r} is not a session id")
        return
    if target.startswith("settings:"):
        tab = target[len("settings:"):]
        if tab not in NAVIGATE_SETTINGS_TABS:
            _fail(pointer, f"settings tab {tab!r} must be one of {sorted(NAVIGATE_SETTINGS_TABS)}")
        return
    # No `http://`, no `webview:`, no bare paths: navigate is in-host only and
    # the only deep link is the page itself (§8.3).
    _fail(
        pointer,
        f"navigate target {target!r} must be module:<page_id> | session:current | "
        "session:<id> | settings:<tab>",
    )


def check_open_url(target: str, *, pointer: str) -> None:
    """http/https only, and the host the user reads is the host that opens.

    The scheme check alone was not enough: `https://user:pw@ok.example/x` reads
    as `ok.example` but the browser goes to `ok.example` — fine — while
    `https://***@evil.com/x` reads as `***` and goes to `evil.com`; `127.0.0.1`
    and `[::1]` are requests into the user's own machine; `%0a`/`%0d` and a
    `#javascript:` fragment are how a target survives a downstream renderer that
    decodes before it validates. Every one of them is refused here, at
    registration and again at routing time.
    """
    if any(ord(ch) < 0x20 for ch in target):
        _fail(pointer, "URL contains control characters")
    lowered = target.lower()
    for encoded in ("%0a", "%0d", "%09", "%00"):
        if encoded in lowered:
            # A percent-encoded control character is the same attack one decode
            # later; a downstream renderer that unquotes before validating
            # would otherwise see a newline where the check saw `%0a`.
            _fail(pointer, f"URL contains an encoded control character ({encoded})")
    try:
        parts = urlsplit(target)
    except ValueError:
        _fail(pointer, f"{target!r} is not a parseable URL")
    if parts.scheme.lower() not in OPEN_URL_SCHEMES:
        _fail(
            pointer,
            f"scheme {parts.scheme!r} is not allowed; open_url accepts http/https only "
            "(no javascript:, file:, data:, intent:)",
        )
    if not parts.netloc:
        _fail(pointer, f"{target!r} has no host")
    if "@" in parts.netloc:
        # `user:pw@host` and `***@host` both make the readable part disagree
        # with the real host. No page needs credentials in a link it shows the
        # user, so the whole shape is refused rather than stripped.
        _fail(
            pointer,
            f"{target!r} carries userinfo (`user:pw@`); the visible host would not be the host that opens",
        )
    if parts.fragment and parts.fragment.lstrip().lower().startswith("javascript:"):
        _fail(pointer, "URL fragment starts with `javascript:` (a renderer that follows it executes script)")
    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        _fail(pointer, f"{target!r} has no host")
    _check_open_url_host(host, pointer=pointer)


def _check_open_url_host(host: str, *, pointer: str) -> None:
    """Refuse loopback, private and metadata targets (literal hosts only)."""
    lowered = host.lower()
    if lowered in BLOCKED_HOST_EXACT or lowered.endswith(BLOCKED_HOST_SUFFIXES):
        _fail(pointer, f"host {host!r} is a local name; open_url may not reach the user's machine")
    if lowered.startswith("[") and lowered.endswith("]"):
        lowered = lowered[1:-1]
    if ":" in lowered:
        # IPv6: `::1` loopback, `fc00::/7` unique-local, `fe80::/10` link-local.
        if lowered in ("::1", "::"):
            _fail(pointer, f"host {host!r} is the IPv6 loopback; open_url may not reach the user's machine")
        if lowered.startswith("fc") or lowered.startswith("fd") or lowered.startswith("fe80"):
            _fail(pointer, f"host {host!r} is an IPv6 private address; open_url may not reach the user's machine")
        return
    match = _IPV4_RE.match(lowered)
    if match is None:
        # Not a dotted quad. A bare numeric host is still a literal address to
        # every resolver in use (`http://127.1/` and `http://127/` both reach
        # loopback; inet_aton accepts 1..4 parts), so it is judged on its first
        # label rather than waved through.
        if _DOTTED_NUMERIC_RE.match(lowered) and lowered.split(".")[0] in ("0", "10", "127", "169", "172", "192", "255"):
            _fail(pointer, f"host {host!r} is a private/loopback address in shorthand form; open_url may not reach the user's machine")
        return
    octets = [int(group) for group in match.groups()]
    if any(value > 255 for value in octets):
        # Not a real IPv4 literal; leave it to the host's resolver rather than
        # guessing what a lenient parser would do with it.
        return
    if lowered.startswith(PRIVATE_IPV4_PREFIXES):
        _fail(pointer, f"host {host!r} is a private/loopback IPv4 address; open_url may not reach the user's machine")
    if octets[1] in PRIVATE_IPV4_SECOND_OCTETS and octets[0] == 172:
        _fail(pointer, f"host {host!r} is a private IPv4 address (172.16/12); open_url may not reach the user's machine")


# ----------------------------------------------------------------------- nodes
def _validate_node(
    raw: Any,
    *,
    pointer: str,
    depth: int,
    module_tools: Sequence[str],
    page_ids: Sequence[str],
    allow_item: bool,
    in_tabs: bool,
    counter: list[int],
    actions_total: list[int],
    ids: set[str],
    warnings: list[str],
) -> UINode:
    if depth > MAX_DEPTH:
        _fail(pointer, f"node depth {depth} > {MAX_DEPTH}")
    node = _require_mapping(raw, pointer)
    counter[0] += 1
    if counter[0] > MAX_NODES:
        _fail(pointer, f"page has more than {MAX_NODES} nodes")

    node_type = node.get("type")
    if not isinstance(node_type, str):
        _fail(f"{pointer}/type", "node needs a string `type`")
    if node_type not in NODE_TYPES:
        _fail(
            f"{pointer}/type",
            f"unknown node type {node_type!r}; only {list(NODE_TYPES)} are allowed "
            "(no grid/stack/iframe/webview)",
        )

    component: str | None = None
    if node_type == "component":
        component = node.get("component")
        if not isinstance(component, str) or component not in COMPONENTS:
            if component in HOST_COMPONENTS:
                _fail(
                    f"{pointer}/component",
                    f"{component!r} is a HOST component, not a page component; "
                    "a page may not impersonate the conversation, input bar, approval dialog or nav bar",
                )
            _fail(
                f"{pointer}/component",
                f"unknown component {component!r}; C-19 rev 1 allows only {list(COMPONENTS)}",
            )

    allowed_keys = {"type", "props", "children", "on", "id", "visible", "data_ref"}
    if node_type == "component":
        allowed_keys.add("component")
    unknown = sorted(set(node) - allowed_keys)
    if unknown:
        _fail(f"{pointer}/{unknown[0]}", f"unknown node key(s) {unknown} (C-19 §4.4 closed shape)")

    node_id: str | None = None
    if "id" in node:
        node_id = _require_str(node["id"], f"{pointer}/id", minimum=1, maximum=32)
        if NODE_ID_RE.match(node_id) is None:
            _fail(f"{pointer}/id", f"id {node_id!r} must match ^[a-z][a-z0-9_-]{{0,31}}$")
        if node_id in ids:
            _fail(f"{pointer}/id", f"duplicate node id {node_id!r} in this page")
        ids.add(node_id)

    is_container = node_type in ("column", "row", "tabs", "list") or component == "Card"
    children_raw = node.get("children")
    if children_raw is not None and not is_container:
        _fail(f"{pointer}/children", f"a {component or node_type} leaf may not carry children")
    if is_container and children_raw is None:
        _fail(f"{pointer}/children", f"a {component or node_type} container needs children")

    children: list[UINode] = []
    if children_raw is not None:
        items = _require_list(children_raw, f"{pointer}/children")
        if len(items) > MAX_CHILDREN:
            _fail(f"{pointer}/children", f"{len(items)} children > {MAX_CHILDREN}")
        if node_type == "list" and len(items) != 1:
            _fail(f"{pointer}/children", f"a list has exactly 1 template child, got {len(items)}")
        child_allow_item = allow_item or node_type == "list"
        for index, child in enumerate(items):
            children.append(
                _validate_node(
                    child,
                    pointer=f"{pointer}/children/{index}",
                    depth=depth + 1,
                    module_tools=module_tools,
                    page_ids=page_ids,
                    allow_item=child_allow_item,
                    in_tabs=node_type == "tabs",
                    counter=counter,
                    actions_total=actions_total,
                    ids=ids,
                    warnings=warnings,
                )
            )

    props = _validate_props(
        node.get("props"),
        node_type=node_type,
        component=component,
        pointer=f"{pointer}/props",
        allow_item=allow_item,
        allow_tab_title=in_tabs,
        warnings=warnings,
    )

    data_ref: str | None = None
    if "data_ref" in node:
        data_ref = validate_binding_path(node["data_ref"], allow_item=allow_item, pointer=f"{pointer}/data_ref")

    visible = node.get("visible")
    if visible is not None:
        if isinstance(visible, Mapping):
            _bind(visible, pointer=f"{pointer}/visible", allow_item=allow_item, warnings=warnings)
            visible = dict(visible)
        elif not isinstance(visible, bool):
            _fail(f"{pointer}/visible", "visible must be a bool or {\"bind\": path} (no expressions)")

    events_allowed = _EVENTS.get(component or "", ())
    actions: list[tuple[str, UIAction]] = []
    on_raw = node.get("on")
    if on_raw is not None:
        on = _require_mapping(on_raw, f"{pointer}/on")
        if len(on) > MAX_ACTIONS_PER_NODE:
            _fail(f"{pointer}/on", f"{len(on)} actions > {MAX_ACTIONS_PER_NODE} per node")
        for event, action_raw in on.items():
            if event not in events_allowed:
                _fail(
                    f"{pointer}/on/{event}",
                    f"unknown event {event!r} for {component or node_type}; "
                    f"allowed: {list(events_allowed) or 'none (this node takes no input)'}",
                )
            action = _validate_action(
                action_raw,
                pointer=f"{pointer}/on/{event}",
                module_tools=module_tools,
                page_ids=page_ids,
                allow_item=allow_item,
                warnings=warnings,
            )
            actions_total[0] += 1
            if actions_total[0] > MAX_ACTIONS_PER_PAGE:
                _fail(f"{pointer}/on/{event}", f"page has more than {MAX_ACTIONS_PER_PAGE} actions")
            if component == "AttachmentChip" and action.kind != "navigate":
                # Q4: the chip is read-only; its only legal click is a route.
                _fail(f"{pointer}/on/{event}", "AttachmentChip.click may only navigate (read-only chip)")
            actions.append((event, action))

    return UINode(
        type=node_type,
        pointer=pointer,
        id=node_id,
        component=component,
        data_ref=data_ref,
        props=props,
        visible=visible,
        children=tuple(children),
        actions=tuple(actions),
    )


# ----------------------------------------------------------------------- pages
def validate_page(
    raw: Any,
    *,
    module_tools: Sequence[str] = (),
    page_ids: Sequence[str] = (),
    pointer: str = "/ui/pages/0",
    allow_ref: bool = True,
    inline_max_bytes: int | None = None,
) -> UIPage:
    """Validate one page declaration. Raises :class:`UIPageError` on any doubt.

    `inline_max_bytes` exists because the 8 KiB inline ceiling is a *manifest*
    budget, not a body budget: the body of a `ref` page arrives through
    `page/get` and is allowed `MAX_REF_BYTES` (RFC Q1 — the whole reason refs
    exist). `ui/page.get` re-validates that body here with
    `allow_ref=False, inline_max_bytes=MAX_REF_BYTES`; without the override the
    8 KiB ceiling applied to the fetched tree and every page above 8 KiB was
    refused, which made the ref mechanism dead.
    """
    _scan_forbidden(raw, pointer)
    page = _require_mapping(raw, pointer)
    unknown = sorted(key for key in page if key not in _PAGE_KEYS and not str(key).startswith("x-"))
    if unknown:
        _fail(
            f"{pointer}/{unknown[0]}",
            f"unknown page key(s) {unknown}; C-19's page block is additionalProperties:false "
            "(an unknown key is a script hook, not forward compatibility)",
        )
    warnings: list[str] = []

    page_id = _require_str(page.get("id"), f"{pointer}/id", minimum=3, maximum=32)
    if PAGE_ID_RE.match(page_id) is None:
        _fail(f"{pointer}/id", f"page id {page_id!r} must match ^[a-z][a-z0-9_-]{{0,31}}$")

    title = _require_str(page.get("title"), f"{pointer}/title", minimum=1, maximum=MAX_TITLE_CHARS)

    version = _require_int(page.get("version"), f"{pointer}/version", minimum=1)

    entry = page.get("entry", "auto")
    if entry not in _ENTRY_VALUES:
        _fail(f"{pointer}/entry", f"entry {entry!r} must be one of {list(_ENTRY_VALUES)}")

    icon = page.get("icon", "none")
    if not isinstance(icon, str):
        _fail(f"{pointer}/icon", f"icon must be a string, got {type(icon).__name__}")
    if icon not in _ICONS:
        # Decorative unknown: degrade, never reject (§7.2's dividing line is
        # "can this field change behaviour").
        warnings.append(f"{pointer}/icon: unknown icon {icon!r}, falling back to none")
        icon = "none"

    requires_session = False
    requires = page.get("requires", [])
    requires_list = _require_list(requires, f"{pointer}/requires")
    if len(requires_list) > 1:
        _fail(f"{pointer}/requires", "requires is [\"session\"] or absent, at most 1 entry")
    for index, item in enumerate(requires_list):
        if item != "session":
            _fail(f"{pointer}/requires/{index}", f"unknown requirement {item!r}; only 'session' exists in v1")
        requires_session = True

    refresh_s = page.get("refresh_s")
    if refresh_s is not None:
        refresh_s = _require_int(refresh_s, f"{pointer}/refresh_s", minimum=1, maximum=MAX_REFRESH_S)
    else:
        refresh_s = 0

    schema_raw = _require_mapping(page.get("schema"), f"{pointer}/schema")
    ref: tuple[str, str] | None = None
    root: UINode | None = None

    if "ref" in schema_raw:
        if not allow_ref:
            _fail(f"{pointer}/schema", "a nested ref is not allowed; page/get returns the tree itself")
        ref_block = _require_mapping(schema_raw["ref"], f"{pointer}/schema/ref")
        unknown = sorted(set(schema_raw) - {"ref"})
        if unknown:
            _fail(f"{pointer}/schema", f"a ref page has only `ref`, got extra key(s) {unknown}")
        unknown = sorted(set(ref_block) - {"page_id", "sha256"})
        if unknown:
            _fail(f"{pointer}/schema/ref", f"unknown ref key(s) {unknown}")
        ref_page = _require_str(ref_block.get("page_id"), f"{pointer}/schema/ref/page_id", minimum=1, maximum=32)
        if ref_page != page_id:
            _fail(
                f"{pointer}/schema/ref/page_id",
                f"ref names page {ref_page!r} but the declaration is {page_id!r} (a page may not borrow another's body)",
            )
        digest = _require_str(ref_block.get("sha256"), f"{pointer}/schema/ref/sha256", minimum=64, maximum=64)
        if SHA256_RE.match(digest) is None:
            _fail(f"{pointer}/schema/ref/sha256", "sha256 must be 64 lowercase hex characters")
        ref = (ref_page, digest)
    else:
        size = len(serialize(schema_raw))
        ceiling = MAX_INLINE_BYTES if inline_max_bytes is None else int(inline_max_bytes)
        if size > ceiling:
            _fail(
                f"{pointer}/schema",
                f"inline schema is {size} bytes > {ceiling} (use a ref for big pages)",
            )
        counter = [0]
        actions_total = [0]
        root = _validate_node(
            schema_raw,
            pointer=f"{pointer}/schema",
            depth=1,
            module_tools=module_tools,
            page_ids=page_ids,
            allow_item=False,
            in_tabs=False,
            counter=counter,
            actions_total=actions_total,
            ids=set(),
            warnings=warnings,
        )
        if not requires_session:
            for action in _walk_actions(root):
                if action.kind == "ui/submit":
                    _fail(
                        action.pointer,
                        "a page with a ui/submit action must declare requires:[\"session\"] (§6.3)",
                    )

    page_obj = UIPage(
        page_id=page_id,
        title=title,
        version=version,
        schema=dict(schema_raw),
        pointer=pointer,
        entry=str(entry),
        icon=icon,
        requires_session=requires_session,
        refresh_s=int(refresh_s or 0),
        root=root,
        ref=ref,
        warnings=tuple(warnings),
    )
    if ref is not None:
        # A ref page's body is only known after `page/get`; a submit action may
        # still lurk inside. §7.4 keeps the entry, §7.1 re-validates at fetch —
        # and `ui/page.get` re-runs this function with `requires_session` set
        # from the declaration, so the refusal happens before a node renders.
        return page_obj
    return page_obj


def _walk_actions(node: UINode) -> Iterable[UIAction]:
    stack = [node]
    while stack:
        current = stack.pop()
        for _event, action in current.actions:
            yield action
        stack.extend(current.children)


def validate_ui(
    raw: Any,
    *,
    module_tools: Sequence[str] = (),
    limits: Mapping[str, int] | None = None,
) -> tuple[tuple[UIPage, ...], tuple[str, ...]]:
    """Validate `manifest.ui`; returns (accepted pages, warnings).

    A bad page is **dropped, not fatal** (§7.1): one UI typo must not kill a
    module's tools. The module card shows "page X failed validation" from the
    warning. An unreadable `ui` block drops every page and records one warning —
    never a silent ignore, never a partial accept.
    """
    if raw is None:
        return (), ()
    if not isinstance(raw, Mapping):
        return (), (f"/ui: must be an object, got {type(raw).__name__}",)

    unknown = sorted(key for key in raw if key != "pages" and not str(key).startswith("x-"))
    if unknown:
        return (), (
            f"/ui/{unknown[0]}: unknown key(s) {unknown}; the ui block is additionalProperties:false",
        )

    pages_raw = raw.get("pages", [])
    if not isinstance(pages_raw, (list, tuple)):
        return (), (f"/ui/pages: must be an array, got {type(pages_raw).__name__}",)

    max_pages = int((limits or {}).get("max_pages", MAX_PAGES))
    if len(pages_raw) > max_pages:
        return (), (f"/ui/pages: {len(pages_raw)} pages > {max_pages}",)

    block_bytes = _safe_size(raw)
    max_block = int((limits or {}).get("max_ui_block_bytes", MAX_UI_BLOCK_BYTES))
    if block_bytes is None:
        return (), ("/ui: cyclic or unserializable page block (C-19 fail-closed)",)
    if block_bytes > max_block:
        return (), (f"/ui: {block_bytes} bytes > {max_block} (page block budget)",)

    declared_ids: list[str] = []
    for page in pages_raw:
        if isinstance(page, Mapping) and isinstance(page.get("id"), str):
            declared_ids.append(str(page["id"]))
    accepted: list[UIPage] = []
    warnings: list[str] = []
    for index, page_raw in enumerate(pages_raw):
        pointer = f"/ui/pages/{index}"
        try:
            page = validate_page(
                page_raw,
                module_tools=module_tools,
                page_ids=declared_ids,
                pointer=pointer,
            )
        except UIPageError as exc:
            label = page_raw.get("id") if isinstance(page_raw, Mapping) else None
            warnings.append(f"{pointer}{f' ({label})' if label else ''}: {exc.reason}")
            continue
        if any(existing.page_id == page.page_id for existing in accepted):
            warnings.append(f"{pointer} ({page.page_id}): duplicate page id, later declaration dropped")
            continue
        accepted.append(page)
        warnings.extend(page.warnings)
    return tuple(accepted), tuple(warnings)


# ------------------------------------------------------------------ page state
def check_state(state: Any, *, pointer: str = "/state") -> dict[str, Any]:
    """§5.1: `page.state` is a flat object of scalars, ≤8 KiB / ≤64 keys."""
    if not isinstance(state, Mapping):
        _fail(pointer, f"state must be an object, got {type(state).__name__}")
    if len(state) > MAX_STATE_KEYS:
        _fail(pointer, f"{len(state)} keys > {MAX_STATE_KEYS}")
    out: dict[str, Any] = {}
    for key, value in state.items():
        if not isinstance(key, str) or PATH_SEGMENT_RE.match(key) is None:
            _fail(f"{pointer}/{key}", "state key must match ^[a-z0-9_]{1,32}$")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            _fail(f"{pointer}/{key}", f"state value must be string|number|bool|null, got {type(value).__name__}")
        if isinstance(value, str) and len(value) > MAX_TEXT_CHARS:
            _fail(f"{pointer}/{key}", f"state string {len(value)} > {MAX_TEXT_CHARS}")
        out[key] = value
    if len(serialize(out)) > MAX_STATE_BYTES:
        _fail(pointer, f"state exceeds {MAX_STATE_BYTES} bytes")
    return out
