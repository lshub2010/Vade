"""Model capability block (RFC-0005 §4.3/§4.5).

The shape is declared by the provider (manifest `capabilities.models[]` for a
module, `[llm.capabilities]` for the builtin) and normalised here, once:

* `null` means UNKNOWN and is never interchangeable with `false`/`[]` — the
  whole point of the block is that a missing declaration must not be read as
  "does not support it";
* a single bad field is dropped (set to `null`) with a warning, it does not
  reject the whole manifest: one typo must not take a module offline
  (C-03 "unknown capability types are ignored with a warning");
* more than `MAX_MODELS` entries is a manifest error, checked by the caller,
  so a manifest cannot become a second model directory.

Pure data + parsing: no config, no registry, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = [
    "MODALITIES",
    "MAX_MODELS",
    "INT_FIELDS",
    "ModelSpec",
    "parse_models",
    "normalise_capabilities",
]

#: Allowed `input_modalities` values (RFC-0005 §4.2).
MODALITIES = ("text", "image", "audio", "file")

#: One manifest may declare at most this many models (§4.3).
MAX_MODELS = 16

#: Positive-integer capability fields; anything else becomes `null`.
INT_FIELDS = ("max_image_bytes", "max_image_px", "max_images_per_turn", "context_window")

_NAME_MAX = 128


def _warning(warnings: list[str] | None, message: str) -> None:
    if warnings is not None:
        warnings.append(message)


def _positive_int(value: Any, field: str, where: str, warnings: list[str] | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _warning(warnings, f"{where}.{field}: {value!r} is not a positive integer; treated as unknown (null)")
        return None
    return int(value)


def normalise_capabilities(
    raw: Any,
    *,
    where: str = "capabilities",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Normalise one capability block into the six wire fields (+display_name).

    Always returns every key, with `None` for anything not validly declared, so
    downstream code never has to distinguish "absent" from "empty".
    """
    block = raw if isinstance(raw, Mapping) else {}
    if raw is not None and not isinstance(raw, Mapping):
        _warning(warnings, f"{where}: expected an object, got {type(raw).__name__}")

    modalities: tuple[str, ...] | None = None
    declared = block.get("input_modalities")
    if declared is not None:
        if not isinstance(declared, (list, tuple)):
            _warning(warnings, f"{where}.input_modalities: expected an array, got {type(declared).__name__}")
        else:
            kept: list[str] = []
            for value in declared:
                if not isinstance(value, str) or value not in MODALITIES:
                    _warning(warnings, f"{where}.input_modalities: unknown modality {value!r} dropped")
                    continue
                if value not in kept:
                    kept.append(value)
            kept = kept[: len(MODALITIES)]
            modalities = tuple(kept) or None

    supports_tools = block.get("supports_tools")
    if supports_tools is not None and not isinstance(supports_tools, bool):
        _warning(warnings, f"{where}.supports_tools: {supports_tools!r} is not a boolean; treated as unknown (null)")
        supports_tools = None

    display_name = block.get("display_name")
    if display_name is not None:
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > _NAME_MAX:
            _warning(warnings, f"{where}.display_name: must be a non-empty string <= {_NAME_MAX} chars")
            display_name = None

    out: dict[str, Any] = {
        "input_modalities": list(modalities) if modalities is not None else None,
        "supports_tools": supports_tools,
        "display_name": display_name.strip() if isinstance(display_name, str) else None,
    }
    for field in INT_FIELDS:
        out[field] = _positive_int(block.get(field), field, where, warnings)
    return out


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One model declared by a module (`capabilities.models[]`)."""

    name: str
    display_name: str | None = None
    input_modalities: tuple[str, ...] | None = None
    max_image_bytes: int | None = None
    max_image_px: int | None = None
    max_images_per_turn: int | None = None
    context_window: int | None = None
    supports_tools: bool | None = None

    def capabilities(self) -> dict[str, Any]:
        """The six capability fields, `None` where undeclared (§4.2)."""
        return {
            "input_modalities": list(self.input_modalities) if self.input_modalities is not None else None,
            "max_image_bytes": self.max_image_bytes,
            "max_image_px": self.max_image_px,
            "max_images_per_turn": self.max_images_per_turn,
            "context_window": self.context_window,
            "supports_tools": self.supports_tools,
        }

    def merge(self, other: "ModelSpec") -> "ModelSpec":
        """Conservative intersection of two instances' declarations (§4.3).

        `min` for limits, logical AND for booleans, `null` poisons: if one
        instance does not know a field, the merged answer is unknown rather
        than the other instance's optimistic claim.
        """
        modalities: tuple[str, ...] | None = None
        if self.input_modalities is not None and other.input_modalities is not None:
            shared = [m for m in self.input_modalities if m in other.input_modalities]
            modalities = tuple(shared) or None
        tools: bool | None = None
        if self.supports_tools is not None and other.supports_tools is not None:
            tools = bool(self.supports_tools and other.supports_tools)

        def both(a: int | None, b: int | None) -> int | None:
            return None if a is None or b is None else min(a, b)

        return ModelSpec(
            name=self.name,
            display_name=self.display_name or other.display_name,
            input_modalities=modalities,
            max_image_bytes=both(self.max_image_bytes, other.max_image_bytes),
            max_image_px=both(self.max_image_px, other.max_image_px),
            max_images_per_turn=both(self.max_images_per_turn, other.max_images_per_turn),
            context_window=both(self.context_window, other.context_window),
            supports_tools=tools,
        )


def parse_models(raw: Any, *, warnings: list[str] | None = None) -> tuple[ModelSpec, ...]:
    """Parse `capabilities.models[]`, dropping bad entries one by one.

    The caller enforces `MAX_MODELS` (a manifest error); everything inside an
    entry is tolerated field-by-field so a typo cannot take a module offline.
    """
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        _warning(warnings, f"capabilities.models: expected an array, got {type(raw).__name__}")
        return ()

    specs: list[ModelSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"capabilities.models[{index}]"
        if not isinstance(item, Mapping):
            _warning(warnings, f"{where}: expected an object, got {type(item).__name__}; entry dropped")
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip() == "*":
            # `*` is the module-wide default entry the catalog synthesises for a
            # chat module that declares nothing (§4.3). A declared `*` would
            # collide with it and be read as "any model", so it is dropped.
            _warning(warnings, f"{where}.name: '*' is reserved for the synthesised module default; entry dropped")
            continue
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > _NAME_MAX
            or "/" in name
            or any(ch.isspace() for ch in name)
        ):
            _warning(warnings, f"{where}.name: must be a non-empty string <= {_NAME_MAX} chars without '/' or whitespace; entry dropped")
            continue
        name = name.strip()
        if name in seen:
            _warning(warnings, f"{where}.name: duplicate model {name!r} in this manifest; entry dropped")
            continue
        seen.add(name)
        caps = normalise_capabilities(item, where=where, warnings=warnings)
        specs.append(
            ModelSpec(
                name=name,
                display_name=caps["display_name"],
                input_modalities=tuple(caps["input_modalities"]) if caps["input_modalities"] else None,
                max_image_bytes=caps["max_image_bytes"],
                max_image_px=caps["max_image_px"],
                max_images_per_turn=caps["max_images_per_turn"],
                context_window=caps["context_window"],
                supports_tools=caps["supports_tools"],
            )
        )
    return tuple(specs)
