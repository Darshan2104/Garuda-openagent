"""Admission scope and persisted bindings describe the same FIFO lane."""

import json

import pytest

from garuda.runtime.capacity import CapacityStore
from garuda.runtime.queue import QueueError, QueueStore


@pytest.mark.parametrize("alias", ["other:native", "u:alias"], ids=["user-alias", "harness-alias"])
def test_scope_alias_cannot_give_the_same_user_and_harness_a_second_fifo_lane(tmp_path, alias):
    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
    queue.enqueue("u:native", "first", user="u", harness="native",
                  session_id="first", config_digest="first-config")
    try:
        queue.enqueue(alias, "second", user="u", harness="native",
                      session_id="second", config_digest="second-config")
    except QueueError:
        pass  # Early admission refusal satisfies the contract.
    else:
        try:
            claimed = queue.try_claim(alias, "second")
        except QueueError:
            claimed = False
        assert not claimed, "an alias jumped the same user/harness FIFO"
    assert queue.try_claim("u:native", "first")
    assert capacity.holders("native") == ["first"]
    assert queue.release("u:native", "first")


@pytest.mark.parametrize("alias", ["other:native", "u:alias"], ids=["user-alias", "harness-alias"])
def test_preexisting_scope_mismatch_refuses_selection_without_rewriting_bindings(tmp_path, alias):
    capacity = CapacityStore(tmp_path / "capacity")
    queue = QueueStore(tmp_path / "queue", capacity=capacity, ceiling=lambda _: 1)
    queue.enqueue("u:native", "first", user="u", harness="native",
                  session_id="first", config_digest="frozen-config")
    source = queue.root / "state.json"
    document = json.loads(source.read_bytes())
    document["scopes"][alias] = document["scopes"].pop("u:native")
    source.write_text(json.dumps(document))
    preserved = source.read_bytes()
    with pytest.raises(QueueError):
        queue.try_claim(alias, "first")
    assert source.read_bytes() == preserved
    assert not capacity.root.exists()
