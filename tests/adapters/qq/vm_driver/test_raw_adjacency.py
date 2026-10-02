from __future__ import annotations

import ctypes
from types import SimpleNamespace

import pytest

from messenger_ai.adapters.qq.vm_driver.raw_adjacency import RawAdjacencyError
from messenger_ai.adapters.qq.vm_driver.session_identity import _GROUP_MARKERS
from messenger_ai.adapters.qq.vm_driver.transport import WindowsUIAQQAccessibility


class BudgetStopped(RuntimeError):
    pass


class ProviderFailure(RuntimeError):
    pass


_NOT_CACHED = object()


class Trace:
    def __init__(self):
        self.events, self.requests = [], []
        self.cached_properties = []
        self.inside_read, self.stopped, self.after_native = False, False, None

    def native(self, name):
        assert self.inside_read, f"unguarded native call: {name}"
        self.events.append(name)
        if self.after_native:
            self.after_native(name)

    def read(self, callback):
        if self.stopped:
            raise BudgetStopped()
        assert not self.inside_read
        self.inside_read = True
        try:
            value = callback()
        finally:
            self.inside_read = False
        if self.stopped:
            raise BudgetStopped()
        return value


class CacheRequest:
    def __init__(self, trace):
        object.__setattr__(self, "trace", trace)
        object.__setattr__(self, "properties", [])

    def __setattr__(self, name, value):
        assert name in {"TreeScope", "TreeFilter", "AutomationElementMode"}
        self.trace.native(f"set:{name}")
        object.__setattr__(self, name, value)

    def AddProperty(self, property_id):
        self.trace.native("AddProperty")
        self.properties.append(property_id)


class CachedElement:
    def __init__(self, trace, rid, children=(), class_name=_NOT_CACHED):
        self.trace, self.rid, self.children = trace, rid, children
        self.class_name = class_name
        self.empty_array = None
        self.length_override = None

    def GetCachedPropertyValueEx(self, property_id, ignore_default):
        self.trace.native("GetCachedPropertyValueEx")
        assert property_id in (30000, 30012) and ignore_default is True
        self.trace.cached_properties.append((self.rid, property_id))
        if property_id == 30000:
            return self.rid
        if self.class_name is _NOT_CACHED:
            raise ProviderFailure("property not cached")
        return self.class_name

    def GetCachedChildren(self):
        self.trace.native("GetCachedChildren")
        if not self.children and self.length_override is None:
            return self.empty_array
        return CachedArray(self.trace, self.children, self.length_override)

    def __getattr__(self, name):
        raise AssertionError(f"cache-only element must not request {name}")


class CachedArray:
    def __init__(self, trace, children, length_override=None):
        self.trace, self.children, self.length_override = trace, children, length_override

    @property
    def Length(self):
        self.trace.native("Length")
        return len(self.children) if self.length_override is None else self.length_override

    def GetElement(self, index):
        self.trace.native("GetElement")
        return self.children[index]


class LiveElement:
    def __init__(self, trace, rid, children=(), class_name="qq-outside-node"):
        self.trace, self.rid, self.children = trace, rid, list(children)
        self.class_name = class_name
        self.mutate_cache = None
        self.failure = None

    def BuildUpdatedCache(self, request):
        self.trace.native("BuildUpdatedCache")
        if self.failure:
            raise self.failure
        assert request.TreeScope == 3 and request.AutomationElementMode == 0
        assert request.properties in ([30000], [30000, 30012])
        # A returned snapshot owns values, never the previous snapshot or the
        # original live child's interface. Native-only current getters fail.
        with_classes = request.properties == [30000, 30012]
        children = tuple(CachedElement(self.trace, child.rid,
            class_name=child.class_name if with_classes else _NOT_CACHED) for child in self.children)
        snapshot = CachedElement(self.trace, self.rid, children,
            class_name=self.class_name if with_classes else _NOT_CACHED)
        if self.mutate_cache:
            self.mutate_cache(snapshot)
        return snapshot

    def __getattr__(self, name):
        raise AssertionError(f"live interface must not request {name}")


class FrozenControl:
    def __init__(self, element):
        self._element = element

    @property
    def Element(self):
        raise AssertionError("must not invoke Element/Refind")

    def __getattr__(self, name):
        raise AssertionError(f"control wrapper must not request {name}")


def scene():
    trace, condition = Trace(), object()

    class Walker:
        @property
        def Condition(self):
            trace.native("Condition")
            return condition

    class Automation:
        def CreateCacheRequest(self):
            trace.native("CreateCacheRequest")
            request = CacheRequest(trace)
            trace.requests.append(request)
            return request

        def __getattr__(self, name):
            raise AssertionError(f"must use actual walker condition, not {name}")

    class Client:
        @property
        def IUIAutomation(self):
            trace.native("IUIAutomation")
            return automation

        @property
        def ViewWalker(self):
            trace.native("ViewWalker")
            return walker

    automation, walker, client = Automation(), Walker(), Client()

    def instance():
        trace.native("instance")
        return client

    factory = SimpleNamespace(instance=instance)
    get_children = SimpleNamespace(__globals__={"_AutomationClient": factory})
    auto = SimpleNamespace(Control=SimpleNamespace(GetChildren=get_children))
    port = object.__new__(WindowsUIAQQAccessibility)
    port._auto = auto
    leaf = LiveElement(trace, (1, 3))
    branch = LiveElement(trace, (1, 2), (leaf,))
    root = LiveElement(trace, (1, 1), (branch,))
    controls = tuple(FrozenControl(node) for node in (root, branch, leaf))
    return SimpleNamespace(trace=trace, condition=condition, port=port, auto=auto,
                           root=root, branch=branch, leaf=leaf, controls=controls)


def collect(state, *, controls=None, max_parents=1024, max_edges=1024):
    return state.port.cached_direct_adjacency(
        state.controls if controls is None else controls, read=state.trace.read,
        max_parents=max_parents, max_edges=max_edges)


def collect_outside(state, *, controls=None, max_parents=1024, max_edges=1024):
    return state.port.cached_outside_proof(
        state.controls if controls is None else controls, read=state.trace.read,
        max_parents=max_parents, max_edges=max_edges)


def test_actual_transport_producer_only_reads_fresh_shallow_cache_and_keeps_live_controls():
    state = scene()
    originals = tuple(control._element for control in state.controls)
    assert collect(state) == (((1, 1), ((1, 2),)), ((1, 2), ((1, 3),)), ((1, 3), ()))
    request, = state.trace.requests
    assert request.TreeFilter is state.condition
    assert state.trace.events.count("BuildUpdatedCache") == 3
    assert state.trace.events.count("GetCachedPropertyValueEx") == 5
    assert state.trace.events.count("GetElement") == 2
    assert tuple(control._element for control in state.controls) == originals
    # In particular, there is no children/type/class/Name/current/native-ID
    # method on either fake live interface. Any such access would have failed.


@pytest.mark.parametrize("change", ["insert", "delete", "reorder", "replace", "empty_leaf", "parent_id"])
def test_each_boundary_rebuilds_values_and_preserves_exact_order(change):
    state = scene()
    another = LiveElement(state.trace, (1, 4))
    state.root.children.append(another)
    before = collect(state)
    if change == "insert":
        state.branch.children.append(LiveElement(state.trace, (1, 5)))
    elif change == "delete":
        state.branch.children.clear()
    elif change == "reorder":
        state.root.children.reverse()
    elif change == "replace":
        state.branch.children[0] = LiveElement(state.trace, (1, 6))
    elif change == "empty_leaf":
        state.leaf.children.append(LiveElement(state.trace, (1, 7)))
    else:
        state.root.rid = (1, 8)
    after = collect(state)
    assert after != before
    assert len(state.trace.requests) == 2 and state.trace.requests[0] is not state.trace.requests[1]
    assert state.trace.events.count("BuildUpdatedCache") == 6
    if change == "reorder":
        assert after[0][1] == tuple(reversed(before[0][1]))
    if change == "empty_leaf":
        assert before[-1][1] == () and after[-1][1] == ((1, 7),)


@pytest.mark.parametrize("value", [None, (), [], "1.2", b"12", 42, True, (True, 2), (1.0,), tuple(range(65)), object()])
@pytest.mark.parametrize("site", ["parent", "child"])
def test_malformed_or_unsupported_runtime_ids_are_not_coerced(value, site):
    state = scene()
    if site == "parent":
        state.root.rid = value
    else:
        state.branch.rid = value
    with pytest.raises(RawAdjacencyError) as error:
        collect(state)
    assert error.value.code == "hybrid_group_adjacency_unproven"


@pytest.mark.parametrize("case", ["parent", "same_parent_children", "different_parents_children", "same_control"])
def test_duplicate_runtime_ids_or_parent_references_reject(case):
    state = scene()
    if case == "parent":
        state.leaf.rid = state.root.rid
    elif case == "same_parent_children":
        state.root.children.append(state.branch)
    elif case == "different_parents_children":
        state.leaf.children.append(state.branch)
    else:
        state.controls = (state.controls[0], state.controls[0])
    with pytest.raises(RawAdjacencyError):
        collect(state)


@pytest.mark.parametrize("null", [None, ctypes.c_void_p(), ctypes.POINTER(ctypes.c_int)()])
def test_requested_empty_leaf_accepts_only_null_native_array(null):
    state = scene()
    state.leaf.mutate_cache = lambda fresh: setattr(fresh, "empty_array", null)
    assert collect(state)[-1] == ((1, 3), ())


@pytest.mark.parametrize("length", [-1, True, 1.0, "1", 1025])
def test_invalid_array_lengths_reject_before_reading_any_child(length):
    state = scene()
    state.root.mutate_cache = lambda fresh: setattr(fresh, "length_override", length)
    with pytest.raises(RawAdjacencyError):
        collect(state)
    assert "GetElement" not in state.trace.events


@pytest.mark.parametrize("value", [0, False, [], ""])
def test_false_noninterface_arrays_are_malformed_not_empty(value):
    state = scene()
    state.leaf.mutate_cache = lambda fresh: setattr(fresh, "empty_array", value)
    with pytest.raises(RawAdjacencyError):
        collect(state)


def test_zero_length_nonnull_array_is_a_valid_empty_leaf():
    state = scene()
    state.leaf.mutate_cache = lambda fresh: setattr(fresh, "length_override", 0)
    assert collect(state)[-1] == ((1, 3), ())


def test_cumulative_edge_cap_stops_before_excess_child_access():
    state = scene()
    with pytest.raises(RawAdjacencyError):
        collect(state, max_edges=1)
    assert state.trace.events.count("BuildUpdatedCache") == 2
    assert state.trace.events.count("GetElement") == 1


@pytest.mark.parametrize("parents,edges", [(True, 1), (0, 1), (1025, 1), (1.0, 1), (3, True), (3, -1), (3, 1025), (2, 2)])
def test_invalid_trusted_limits_and_parent_cap_make_no_native_calls(parents, edges):
    state = scene()
    with pytest.raises(RawAdjacencyError):
        collect(state, max_parents=parents, max_edges=edges)
    assert state.trace.events == []


@pytest.mark.parametrize("controls", [(), [], None, "controls", iter(())])
def test_missing_or_unbounded_parent_sequence_is_rejected(controls):
    state = scene()
    with pytest.raises(RawAdjacencyError):
        state.port.cached_direct_adjacency(controls, read=state.trace.read, max_parents=1024, max_edges=1024)
    assert state.trace.events == []


def test_all_1024_parents_and_edges_are_supported_without_following_new_children():
    state = scene()
    parents = [LiveElement(state.trace, (1, i)) for i in range(1024)]
    for i, parent in enumerate(parents):
        parent.children = [LiveElement(state.trace, (2, i))]
    controls = tuple(FrozenControl(parent) for parent in parents)
    result = collect(state, controls=controls)
    assert len(result) == 1024 and sum(len(children) for _, children in result) == 1024
    assert state.trace.events.count("BuildUpdatedCache") == 1024


@pytest.mark.parametrize("site", ["parent", "fresh", "child", "request"])
def test_null_required_interfaces_fail_without_refind_or_fallback(site):
    state = scene()
    if site == "parent":
        state.controls[0]._element = None
    elif site == "fresh":
        state.root.BuildUpdatedCache = lambda request: None
    elif site == "child":
        state.root.mutate_cache = lambda fresh: setattr(fresh, "children", (None,))
    else:
        client_type = state.auto.Control.GetChildren.__globals__["_AutomationClient"]
        original_instance = client_type.instance
        client_type.instance = lambda: SimpleNamespace(
            IUIAutomation=SimpleNamespace(CreateCacheRequest=lambda: None),
            ViewWalker=original_instance().ViewWalker)
    with pytest.raises(RawAdjacencyError):
        collect(state)


def test_unknown_com_failure_propagates_unchanged_without_retry():
    state = scene()
    failure = ProviderFailure("synthetic provider failure")
    state.branch.failure = failure
    with pytest.raises(ProviderFailure) as error:
        collect(state)
    assert error.value is failure
    assert state.trace.events.count("BuildUpdatedCache") == 2


def test_missing_cached_property_error_propagates_not_as_default_runtime_id():
    state = scene()
    failure = ProviderFailure("not cached")
    def missing(fresh):
        def read_property(*_):
            state.trace.native("GetCachedPropertyValueEx")
            raise failure
        fresh.GetCachedPropertyValueEx = read_property
    state.root.mutate_cache = missing
    with pytest.raises(ProviderFailure) as error:
        collect(state)
    assert error.value is failure and "GetCachedChildren" not in state.trace.events


@pytest.mark.parametrize("reason", ["utc", "monotonic", "revoked"])
def test_every_native_call_and_setup_is_bracketed_by_the_original_read_budget(reason):
    reference = scene()
    collect(reference)
    # Interrupt after each individual native API/property, including singleton
    # setup, request setters, cached array access and its last property read.
    for stop_at in range(1, len(reference.trace.events) + 1):
        state = scene()
        def stop(_):
            if len(state.trace.events) == stop_at:
                state.trace.stopped = reason
        state.trace.after_native = stop
        with pytest.raises(BudgetStopped):
            collect(state)
        assert len(state.trace.events) == stop_at
    state = scene()
    state.trace.stopped = reason
    with pytest.raises(BudgetStopped):
        collect(state)
    assert state.trace.events == []


def test_transport_does_not_assume_raw_or_control_condition_and_never_creates_wrappers():
    state = scene()
    state.auto.Control.CreateControlFromElement = lambda *_: pytest.fail("no wrappers")
    collect(state)
    assert state.trace.requests[0].TreeFilter is state.condition
    assert all(control._element is live for control, live in zip(
        state.controls, (state.root, state.branch, state.leaf), strict=True))


def test_outside_proof_pairs_parent_classes_with_same_fresh_cached_ids():
    state = scene()
    state.root.class_name, state.branch.class_name, state.leaf.class_name = "root", "branch", ""
    originals = tuple(control._element for control in state.controls)
    edges, classes = collect_outside(state)
    assert edges == (((1, 1), ((1, 2),)), ((1, 2), ((1, 3),)), ((1, 3), ()))
    assert classes == (((1, 1), "root"), ((1, 2), "branch"), ((1, 3), ""))
    request, = state.trace.requests
    assert request.TreeFilter is state.condition
    assert request.properties == [30000, 30012]
    assert state.trace.events.count("BuildUpdatedCache") == 3
    assert state.trace.events.count("GetCachedPropertyValueEx") == 8
    assert [rid for rid, prop in state.trace.cached_properties if prop == 30012] == [(1, 1), (1, 2), (1, 3)]
    assert tuple(control._element for control in state.controls) == originals


@pytest.mark.parametrize("marker", sorted(_GROUP_MARKERS))
def test_outside_proof_refreshes_existing_parent_class_group_marker_without_new_edges(marker):
    state = scene()
    before_edges, before_classes = collect_outside(state)
    state.branch.class_name = "ordinary " + marker
    after_edges, after_classes = collect_outside(state)
    assert after_edges == before_edges
    assert after_classes != before_classes
    assert after_classes[1] == ((1, 2), "ordinary " + marker)
    assert _GROUP_MARKERS & set(after_classes[1][1].split()) == {marker}
    assert len(state.trace.requests) == 2 and state.trace.requests[0] is not state.trace.requests[1]
    assert state.trace.events.count("BuildUpdatedCache") == 6


@pytest.mark.parametrize("value", [None, object(), 42, True, b"group-user", (), [], {}, "x" * 4097])
def test_outside_proof_rejects_unknown_nonstring_or_oversize_cached_parent_class(value):
    state = scene()
    state.root.class_name = value
    with pytest.raises(RawAdjacencyError) as error:
        collect_outside(state)
    assert error.value.code == "hybrid_group_adjacency_unproven"
    assert "GetCachedChildren" not in state.trace.events
    assert state.trace.events.count("BuildUpdatedCache") == 1


def test_outside_proof_rejects_str_subclass_without_coercion():
    class NonPlainString(str):
        def __str__(self):
            pytest.fail("must not coerce cached class")
    state = scene()
    state.root.class_name = NonPlainString("group-user")
    with pytest.raises(RawAdjacencyError):
        collect_outside(state)


@pytest.mark.parametrize("class_name", ["", "x" * 4096])
def test_outside_proof_allows_empty_and_maximum_length_real_class_strings(class_name):
    state = scene()
    state.root.class_name = class_name
    assert collect_outside(state)[1][0] == ((1, 1), class_name)


def test_outside_proof_missing_cached_class_propagates_without_current_property_or_fallback():
    state = scene()
    state.root.mutate_cache = lambda fresh: setattr(fresh, "class_name", _NOT_CACHED)
    with pytest.raises(ProviderFailure, match="property not cached"):
        collect_outside(state)
    assert state.trace.events.count("BuildUpdatedCache") == 1
    assert "GetCachedChildren" not in state.trace.events


def test_outside_proof_cached_class_com_failure_propagates_same_exception_once():
    state = scene()
    failure = ProviderFailure("synthetic cached class COM failure")
    def broken_class(fresh):
        original = fresh.GetCachedPropertyValueEx
        def property_value(property_id, ignore_default):
            if property_id == 30012:
                state.trace.native("GetCachedPropertyValueEx")
                raise failure
            return original(property_id, ignore_default)
        fresh.GetCachedPropertyValueEx = property_value
    state.root.mutate_cache = broken_class
    with pytest.raises(ProviderFailure) as error:
        collect_outside(state)
    assert error.value is failure
    assert state.trace.events.count("BuildUpdatedCache") == 1
    assert "GetCachedChildren" not in state.trace.events


def test_outside_proof_build_updated_cache_failure_never_retries_or_falls_back():
    state = scene()
    failure = ProviderFailure("synthetic updated cache failure")
    state.branch.failure = failure
    with pytest.raises(ProviderFailure) as error:
        collect_outside(state)
    assert error.value is failure and state.trace.events.count("BuildUpdatedCache") == 2


def test_outside_proof_parent_cached_id_drift_is_not_replaced_by_retained_live_id():
    state = scene()
    before_edges, before_classes = collect_outside(state)
    state.root.mutate_cache = lambda fresh: setattr(fresh, "rid", (8, 9))
    edges, classes = collect_outside(state)
    assert edges != before_edges and classes != before_classes
    assert edges[0][0] == classes[0][0] == (8, 9)
    assert state.root.rid == (1, 1)


@pytest.mark.parametrize("change", ["insert", "delete", "reorder", "replace", "empty_leaf"])
def test_outside_proof_each_boundary_refreshes_all_ordered_edges_without_following_new_children(change):
    state = scene()
    state.root.children.append(LiveElement(state.trace, (1, 4)))
    before, _ = collect_outside(state)
    if change == "insert":
        state.branch.children.append(LiveElement(state.trace, (1, 5)))
    elif change == "delete":
        state.branch.children.clear()
    elif change == "reorder":
        state.root.children.reverse()
    elif change == "replace":
        state.branch.children[0] = LiveElement(state.trace, (1, 6))
    else:
        state.leaf.children.append(LiveElement(state.trace, (1, 7)))
    after, classes = collect_outside(state)
    assert after != before
    assert classes == tuple((rid, "qq-outside-node") for rid in ((1, 1), (1, 2), (1, 3)))
    assert len(state.trace.requests) == 2 and state.trace.requests[0] is not state.trace.requests[1]
    assert all(request.properties == [30000, 30012] and request.TreeFilter is state.condition
               for request in state.trace.requests)
    assert state.trace.events.count("BuildUpdatedCache") == 6
    if change == "reorder":
        assert after[0][1] == tuple(reversed(before[0][1]))


@pytest.mark.parametrize("value", [None, (), "1.2", True, (True, 2), (1.0,), tuple(range(65))])
@pytest.mark.parametrize("site", ["parent", "child"])
def test_outside_proof_keeps_strict_cached_runtime_id_contract(value, site):
    state = scene()
    if site == "parent":
        state.root.rid = value
    else:
        state.branch.rid = value
    with pytest.raises(RawAdjacencyError):
        collect_outside(state)


@pytest.mark.parametrize("parents,edges", [(True, 1), (0, 1), (1025, 1), (3, True), (3, -1), (3, 1025), (2, 2)])
def test_outside_proof_keeps_parent_and_edge_caps_before_native_calls(parents, edges):
    state = scene()
    with pytest.raises(RawAdjacencyError):
        collect_outside(state, max_parents=parents, max_edges=edges)
    assert state.trace.events == []


def test_outside_proof_cumulative_edge_cap_stops_before_excess_child_access():
    state = scene()
    with pytest.raises(RawAdjacencyError):
        collect_outside(state, max_edges=1)
    assert state.trace.events.count("BuildUpdatedCache") == 2
    assert state.trace.events.count("GetElement") == 1


@pytest.mark.parametrize("reason", ["utc", "monotonic", "revoked"])
def test_outside_proof_every_native_call_obeys_original_read_budget_before_and_after(reason):
    reference = scene()
    collect_outside(reference)
    for stop_at in range(1, len(reference.trace.events) + 1):
        state = scene()
        def stop(_):
            if len(state.trace.events) == stop_at:
                state.trace.stopped = reason
        state.trace.after_native = stop
        with pytest.raises(BudgetStopped):
            collect_outside(state)
        assert len(state.trace.events) == stop_at
    state = scene()
    state.trace.stopped = reason
    with pytest.raises(BudgetStopped):
        collect_outside(state)
    assert state.trace.events == []


def test_old_adjacency_api_stays_rid_only_after_outside_proof_and_with_uncached_class():
    state = scene()
    collect_outside(state)
    state.trace.cached_properties.clear()
    state.root.class_name = object()
    assert collect(state) == (((1, 1), ((1, 2),)), ((1, 2), ((1, 3),)), ((1, 3), ()))
    assert state.trace.requests[-1].properties == [30000]
    assert all(property_id == 30000 for _, property_id in state.trace.cached_properties)
    assert state.trace.requests[0] is not state.trace.requests[1]
