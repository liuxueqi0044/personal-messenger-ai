import pytest

pytest.importorskip(
    "hypothesis", reason="Hypothesis is optional in the minimal test runtime"
)
from hypothesis import given
from hypothesis import strategies as st

from messenger_ai.domain.models import Draft
from messenger_ai.testing.fakes import FakeClock


@given(st.integers(min_value=0, max_value=100000))
def test_fake_clock_never_moves_back(seconds):
    c = FakeClock()
    before = c.now()
    after = c.advance(seconds)
    assert after >= before


@given(st.text(min_size=0, max_size=100))
def test_draft_hash_is_deterministic(text):
    a = Draft(conversation_id="c", contact_id="u", text=text, rule_version="r")
    b = Draft(conversation_id="c", contact_id="u", text=text, rule_version="r")
    assert a.text_hash == b.text_hash
