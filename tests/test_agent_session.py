import pytest
from pipeline.agent_session import (
    AgentSession, StackEntry, RoleContext,
    get_or_create, push_to_stack, pop_from_stack, clear,
)


def test_get_or_create_returns_same_session():
    s1 = get_or_create("recruiter-1", "session-a")
    s2 = get_or_create("recruiter-1", "session-a")
    assert s1 is s2


def test_get_or_create_different_session_ids_are_isolated():
    s1 = get_or_create("recruiter-1", "session-a")
    s2 = get_or_create("recruiter-1", "session-b")
    assert s1 is not s2


def test_push_and_pop_stack():
    session = get_or_create("recruiter-2", "session-x")
    session.search_stack.clear()

    entry1 = StackEntry(query="python dev", filters={}, results_preview=[], agent_reasoning="first")
    entry2 = StackEntry(query="senior python", filters={}, results_preview=[], agent_reasoning="second")

    push_to_stack(session, entry1)
    push_to_stack(session, entry2)

    assert len(session.search_stack) == 2

    popped = pop_from_stack(session)
    assert popped is entry2
    assert len(session.search_stack) == 1


def test_pop_from_single_entry_returns_none():
    session = get_or_create("recruiter-3", "session-y")
    session.search_stack.clear()
    push_to_stack(session, StackEntry(query="q", filters={}, results_preview=[], agent_reasoning=""))
    result = pop_from_stack(session)
    assert result is None  # cannot go back further


def test_clear_removes_session():
    get_or_create("recruiter-4", "session-z")
    clear("recruiter-4", "session-z")
    # New call creates a fresh session (different object)
    s_new = get_or_create("recruiter-4", "session-z")
    assert s_new.messages == []
    assert s_new.search_stack == []
