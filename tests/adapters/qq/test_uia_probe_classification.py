from scripts.qq_uia_readonly_probe import classify


def report(**signals):
    return {
        "succeeded": True,
        "node_count_examined": 100,
        "pattern_counts": {"text": 0},
        "automation_ids": [{"value": "conversation-list", "count": 1}],
        "semantic_signals": {
            "named_text_nodes": 10,
            "selectable_conversation_candidates": 3,
            "composer_candidates": 1,
            "exact_send_button_candidates": 1,
            **signals,
        },
    }


def test_complete_control_patterns_are_small_tool_candidate():
    verdict, missing = classify(report())
    assert verdict == "small_tool_candidate"
    assert missing == []


def test_visible_tree_without_action_patterns_is_large_architecture():
    verdict, missing = classify(
        report(
            named_text_nodes=0,
            selectable_conversation_candidates=0,
            composer_candidates=0,
            exact_send_button_candidates=0,
        )
    )
    assert verdict == "large_architecture_likely"
    assert "composer" in missing
    assert "send_button" in missing


def test_partial_patterns_are_medium_driver_work():
    verdict, missing = classify(
        report(composer_candidates=0, exact_send_button_candidates=0)
    )
    assert verdict == "medium_driver_work"
    assert missing == ["composer", "send_button"]


def test_complete_but_unstable_custom_tree_remains_medium():
    candidate = report()
    candidate["automation_ids"] = []
    candidate["semantic_signals"]["selectable_conversation_candidates"] = 0
    candidate["semantic_signals"]["left_pane_invoke_candidates"] = 4
    candidate["semantic_signals"]["send_keyword_candidates"] = 1
    verdict, missing = classify(candidate)
    assert verdict == "medium_driver_work"
    assert missing == ["stable_selector_metadata"]


def test_failed_probe_is_indeterminate():
    verdict, missing = classify({"succeeded": False, "error_code": "NO_WINDOW"})
    assert verdict == "indeterminate"
    assert missing == ["NO_WINDOW"]
