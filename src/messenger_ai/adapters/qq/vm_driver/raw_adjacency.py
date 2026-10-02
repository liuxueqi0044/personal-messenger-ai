"""Fresh, shallow UIA adjacency values for already-frozen parent controls.

Each call creates a new cache request and updates each parent's cache once.
The view is the transport's actual walker view. The adjacency API requests only
RuntimeIds; the outside proof additionally returns each parent's ClassName from
that same fresh cache. No descendants, text, current properties, control wrappers,
or input capabilities are fetched. Cache objects are discarded after extracting
values and never replace the original controls.
"""
from __future__ import annotations

import ctypes
from collections.abc import Callable, Sequence
from typing import Any, TypeVar


RuntimeId = tuple[int, ...]
DirectAdjacency = tuple[tuple[RuntimeId, tuple[RuntimeId, ...]], ...]
ParentClassNames = tuple[tuple[RuntimeId, str], ...]
OutsideProof = tuple[DirectAdjacency, ParentClassNames]
_T = TypeVar("_T")
_MAX_PARENTS = 1024
_MAX_EDGES = 1024
_RUNTIME_ID_PROPERTY = 30000
_CLASS_NAME_PROPERTY = 30012
_MAX_CLASS_NAME = 4096


class RawAdjacencyError(RuntimeError):
    code = "hybrid_group_adjacency_unproven"

    def __init__(self) -> None:
        super().__init__(self.code)


def _null_interface(value: Any) -> bool:
    # comtypes interface pointers derive from c_void_p. A requested empty
    # children collection can be a null COM pointer, not only Python None.
    return value is None or (
        isinstance(value, (ctypes.c_void_p, ctypes._Pointer)) and not bool(value)
    )


def _attribute(value: Any, name: str) -> Any:
    missing = object()
    result = getattr(value, name, missing)
    if result is missing or _null_interface(result):
        raise RawAdjacencyError()
    return result


def _invoke(value: Any, name: str, *args: Any) -> Any:
    method = _attribute(value, name)
    if not callable(method):
        raise RawAdjacencyError()
    return method(*args)


def _runtime_id(value: Any) -> RuntimeId:
    # comtypes converts VT_ARRAY|VT_I4 to a tuple. Do not coerce booleans,
    # strings, defaults, or arbitrary iterators into proof values.
    if (not isinstance(value, (tuple, list)) or not 1 <= len(value) <= 64
            or any(type(item) is not int for item in value)):
        raise RawAdjacencyError()
    return tuple(value)


def cached_direct_adjacency(
    controls: Sequence[Any], *, auto: Any,
    read: Callable[[Callable[[], _T]], _T],
    max_parents: int, max_edges: int,
) -> DirectAdjacency:
    """Return ordered current parent/child RuntimeIds within the caller's budget.

    ``read`` must check the original deadline/revocation before and after its
    callback. Native failures propagate unchanged; there is no slow fallback,
    locator lookup, cache reuse across boundaries, or replacement baseline.
    """
    return _cached_shallow_proof(
        controls, auto=auto, read=read, max_parents=max_parents,
        max_edges=max_edges, class_names=False,
    )[0]


def cached_outside_proof(
    controls: Sequence[Any], *, auto: Any,
    read: Callable[[Callable[[], _T]], _T],
    max_parents: int, max_edges: int,
) -> OutsideProof:
    """Return ``(ordered_edges, ordered_parent_classes)`` from fresh caches.

    Both tuples follow ``controls`` order. Every class entry is
    ``(parent_runtime_id, class_name)`` from the same parent cache as its edge.
    Classes must be actual strings of at most 4096 characters; an empty string
    is valid. Child classes are not read. The caller retains responsibility for
    comparing the complete adjacency and checking forbidden group markers.

    ``read`` brackets every native call with the original deadline/revocation.
    Provider failures propagate unchanged without a current read, refind,
    fallback traversal, or reuse of a cache from an earlier boundary.
    """
    return _cached_shallow_proof(
        controls, auto=auto, read=read, max_parents=max_parents,
        max_edges=max_edges, class_names=True,
    )


def _cached_shallow_proof(
    controls: Sequence[Any], *, auto: Any,
    read: Callable[[Callable[[], _T]], _T],
    max_parents: int, max_edges: int, class_names: bool,
) -> OutsideProof:
    if (type(max_parents) is not int or not 1 <= max_parents <= _MAX_PARENTS
            or type(max_edges) is not int or not 0 <= max_edges <= _MAX_EDGES
            or not isinstance(controls, (tuple, list))
            or not 1 <= len(controls) <= max_parents):
        raise RawAdjacencyError()
    parents = tuple(controls)
    if len({id(parent) for parent in parents}) != len(parents):
        raise RawAdjacencyError()

    # Use only the interfaces already held by the completed opening phase.
    # Control.Element can silently Refind when _element is missing; never call
    # it here, since re-resolving a frozen parent would change the proof scope.
    elements = tuple(read(lambda parent=parent: _attribute(parent, "_element"))
                     for parent in parents)
    get_children = _attribute(_attribute(auto, "Control"), "GetChildren")
    namespace = _attribute(get_children, "__globals__")
    if not isinstance(namespace, dict) or "_AutomationClient" not in namespace:
        raise RawAdjacencyError()
    client = read(lambda: _invoke(namespace["_AutomationClient"], "instance"))
    automation = read(lambda: _attribute(client, "IUIAutomation"))
    walker = read(lambda: _attribute(client, "ViewWalker"))
    condition = read(lambda: _attribute(walker, "Condition"))
    request = read(lambda: _invoke(automation, "CreateCacheRequest"))
    if _null_interface(request):
        raise RawAdjacencyError()
    read(lambda: setattr(request, "TreeScope", 3))  # Element | Children only.
    read(lambda: setattr(request, "TreeFilter", condition))
    read(lambda: setattr(request, "AutomationElementMode", 0))  # Cache-only.
    read(lambda: _invoke(request, "AddProperty", _RUNTIME_ID_PROPERTY))
    if class_names:
        read(lambda: _invoke(request, "AddProperty", _CLASS_NAME_PROPERTY))

    result, parent_classes = [], []
    seen_parents: set[RuntimeId] = set()
    seen_children: set[RuntimeId] = set()
    edge_count = 0
    for element in elements:
        fresh = read(lambda: _invoke(element, "BuildUpdatedCache", request))
        if _null_interface(fresh):
            raise RawAdjacencyError()
        parent_id = _runtime_id(read(lambda: _invoke(
            fresh, "GetCachedPropertyValueEx", _RUNTIME_ID_PROPERTY, True)))
        if parent_id in seen_parents:
            raise RawAdjacencyError()
        seen_parents.add(parent_id)
        if class_names:
            class_name = read(lambda: _invoke(
                fresh, "GetCachedPropertyValueEx", _CLASS_NAME_PROPERTY, True))
            if type(class_name) is not str or len(class_name) > _MAX_CLASS_NAME:
                raise RawAdjacencyError()
            parent_classes.append((parent_id, class_name))
        children = read(lambda: _invoke(fresh, "GetCachedChildren"))
        count = 0 if _null_interface(children) else read(lambda: _attribute(children, "Length"))
        if type(count) is not int or count < 0 or edge_count + count > max_edges:
            raise RawAdjacencyError()
        edge_count += count
        child_ids = []
        for index in range(count):
            child = read(lambda index=index: _invoke(children, "GetElement", index))
            if _null_interface(child):
                raise RawAdjacencyError()
            child_id = _runtime_id(read(lambda: _invoke(
                child, "GetCachedPropertyValueEx", _RUNTIME_ID_PROPERTY, True)))
            if child_id in seen_children:
                raise RawAdjacencyError()
            seen_children.add(child_id)
            child_ids.append(child_id)
        result.append((parent_id, tuple(child_ids)))
    return tuple(result), tuple(parent_classes)
