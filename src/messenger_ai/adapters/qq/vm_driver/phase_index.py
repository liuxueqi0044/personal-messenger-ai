"""One-action UIA tree index.

The index deliberately owns live COM controls for one read phase only.  Callers
must discard it after any UI mutation and must never carry it across worker
commands or conversations.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UIAPhaseNode:
    control: Any
    automation_ancestors: tuple[str, ...]
    type_ancestors: tuple[str, ...]


class UIAPhaseIndex:
    """Materialize one UI tree and memoize its COM properties and patterns."""

    def __init__(self, *, window: object, root: Any,
                 pattern_loader: Callable[[Any, str, int], object | None]) -> None:
        self.window = window
        self.root = root
        self._pattern_loader = pattern_loader
        self._active = True
        self._nodes: tuple[UIAPhaseNode, ...] | None = None
        self._properties: dict[tuple[int, str], object] = {}
        self._patterns: dict[tuple[int, int], object | None] = {}
        # Subtree walks may create fresh Python wrappers for the same COM
        # element. Keep every cached wrapper alive until this phase closes:
        # otherwise Python can reuse its id and a different element inherits
        # stale ClassName, control type or pattern data from our cache.
        self._cached_controls: dict[int, Any] = {}
        # The materialized graph stores direct edges, not provider RuntimeIds.
        # Known subtree roots can then be walked with relative ancestor paths
        # without issuing another GetChildren call or aliasing fresh wrappers.
        self._children: dict[int, tuple[Any, ...]] = {}

    def __enter__(self) -> "UIAPhaseIndex":
        self._ensure_active()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def active(self) -> bool:
        return self._active

    def close(self) -> None:
        self._active = False
        self._nodes = None
        self._properties.clear()
        self._patterns.clear()
        self._cached_controls.clear()
        self._children.clear()
        self.root = None

    invalidate = close

    def property(self, control: Any, name: str, default: object = "") -> object:
        self._ensure_active()
        self._cached_controls[id(control)] = control
        key = (id(control), name)
        if key not in self._properties:
            self._properties[key] = getattr(control, name, default)
        return self._properties[key]

    def pattern(self, control: Any, getter_name: str,
                pattern_id: int) -> object | None:
        self._ensure_active()
        self._cached_controls[id(control)] = control
        key = (id(control), pattern_id)
        if key not in self._patterns:
            self._patterns[key] = self._pattern_loader(
                control, getter_name, pattern_id
            )
        return self._patterns[key]

    def call(self, control: Any, method_name: str,
             default: object = None) -> object:
        """Call a stable read method once and cache its immutable result."""

        self._ensure_active()
        self._cached_controls[id(control)] = control
        key = (id(control), f"{method_name}()")
        if key not in self._properties:
            method = self.property(control, method_name, None)
            self._properties[key] = method() if callable(method) else default
        return self._properties[key]

    def nodes(self) -> tuple[UIAPhaseNode, ...]:
        self._ensure_active()
        if self._nodes is not None:
            return self._nodes
        result: list[UIAPhaseNode] = []
        children_by_id: dict[int, tuple[Any, ...]] = {}
        self._cached_controls[id(self.root)] = self.root
        children_by_id[id(self.root)] = tuple(self.root.GetChildren())
        queue = deque(
            (item, (), ()) for item in children_by_id[id(self.root)]
        )
        while queue:
            item, automation_ancestors, type_ancestors = queue.popleft()
            result.append(UIAPhaseNode(
                control=item,
                automation_ancestors=automation_ancestors,
                type_ancestors=type_ancestors,
            ))
            item_id = str(self.property(item, "AutomationId", ""))
            item_type = str(self.property(item, "ControlTypeName", ""))
            children = tuple(item.GetChildren())
            children_by_id[id(item)] = children
            queue.extend(
                (
                    child,
                    automation_ancestors + ((item_id,) if item_id else ()),
                    type_ancestors + ((item_type.lower(),) if item_type else ()),
                )
                for child in children
            )
        # Publish the graph only after a complete traversal. An interrupted
        # provider enumeration cannot look like a complete indexed subtree.
        self._children = children_by_id
        self._nodes = tuple(result)
        return self._nodes

    def indexed_children(self, root: Any) -> tuple[Any, ...] | None:
        """Return known direct children, or None for an unknown fresh wrapper."""
        self._ensure_active()
        if root is self.root:
            self.nodes()
        if self._nodes is None or self._cached_controls.get(id(root)) is not root:
            return None
        return self._children.get(id(root))

    def subtree_nodes(self, root: Any) -> tuple[UIAPhaseNode, ...] | None:
        """Walk an indexed subtree with the original root-relative semantics.

        The supplied root itself is excluded. Its direct children have empty
        ancestor tuples; deeper children include only ancestors *below* that
        root. Unknown wrappers are never matched by Name or RuntimeId and must
        use the caller's safe fresh-provider fallback.
        """
        self._ensure_active()
        if root is self.root:
            return self.nodes()
        children = self.indexed_children(root)
        if children is None:
            return None
        result = []
        queue = deque((item, (), ()) for item in children)
        while queue:
            item, automation_ancestors, type_ancestors = queue.popleft()
            result.append(UIAPhaseNode(item, automation_ancestors, type_ancestors))
            item_id = str(self.property(item, "AutomationId", ""))
            item_type = str(self.property(item, "ControlTypeName", ""))
            queue.extend((child,
                automation_ancestors + ((item_id,) if item_id else ()),
                type_ancestors + ((item_type.lower(),) if item_type else ()))
                for child in self._children[id(item)])
        return tuple(result)

    def controls(self) -> Iterable[Any]:
        return (node.control for node in self.nodes())

    def _ensure_active(self) -> None:
        if not self._active:
            raise RuntimeError("UIA read phase is no longer valid")
