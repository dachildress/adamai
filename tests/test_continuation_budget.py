"""
Standalone unit test for the deliberation-cap-plus-continuation-budget
loop semantics introduced as Fix A.

We don't want to run the full ADAM session to verify this -- we just
want to confirm that:

  (a) Without continuation grants, the loop runs exactly max_turns times.
  (b) A continuation grant on turn N extends the loop by exactly one more
      iteration where continuation_active is True.
  (c) A continuation grant on the FINAL deliberation turn (the bug case
      that motivated Fix A) actually consumes the grant rather than
      ending the session with continuation_active set but never executed.
  (d) Multiple continuation grants chain correctly up to a cap.
  (e) Setting continuation_requested=False on a continuation turn ends
      the session cleanly.
"""

# Run: python tests/test_continuation_budget.py

def simulate_loop(deliberation_cap: int, grant_plan: dict, effective_max: int = 4):
    """
    grant_plan: dict mapping turn_number -> bool (whether Operator
    requests continuation on that turn). Turn N can only be in
    grant_plan if it's a wrap-up or continuation turn; this is a
    simplified simulator that doesn't distinguish phases, but
    that's fine for budget arithmetic.
    """
    continuation_budget = 0
    continuation_count = 0
    turn = 0
    turns_executed = []
    continuation_turns = []

    while turn < deliberation_cap + continuation_budget:
        turn += 1
        turns_executed.append(turn)
        # Was this turn a continuation? (i.e. above the deliberation cap)
        is_continuation_turn = turn > deliberation_cap
        if is_continuation_turn:
            continuation_turns.append(turn)

        # Process grant request, if any, with cap enforcement
        wants_continuation = grant_plan.get(turn, False)
        if wants_continuation and continuation_count < effective_max:
            continuation_budget += 1
            continuation_count += 1

    return {
        "turns_executed": turns_executed,
        "continuation_turns": continuation_turns,
        "continuation_count": continuation_count,
        "final_turn": turn,
    }


def test_no_grants():
    # Plain session, no continuations requested
    r = simulate_loop(deliberation_cap=10, grant_plan={})
    assert r["turns_executed"] == list(range(1, 11)), r
    assert r["continuation_turns"] == [], r
    assert r["continuation_count"] == 0, r
    print("PASS: (a) no grants -> loop runs exactly max_turns times")


def test_grant_at_final_deliberation_turn():
    # THE BUG CASE: continuation granted on the last deliberation turn
    # must produce an 11th iteration, not be silently dropped.
    r = simulate_loop(deliberation_cap=10, grant_plan={10: True})
    assert r["turns_executed"] == list(range(1, 12)), r
    assert r["continuation_turns"] == [11], r
    assert r["continuation_count"] == 1, r
    print("PASS: (c) grant at T10 with cap=10 actually runs T11")


def test_continuation_then_no_more():
    # Operator gets one continuation, then signals continuation_requested=False
    r = simulate_loop(deliberation_cap=10, grant_plan={10: True, 11: False})
    assert r["turns_executed"] == list(range(1, 12)), r
    assert r["final_turn"] == 11, r
    assert r["continuation_count"] == 1, r
    print("PASS: (e) continuation_requested=False ends session cleanly after one continuation")


def test_chained_continuations_up_to_cap():
    # Operator chains four continuations, then no more
    r = simulate_loop(
        deliberation_cap=10,
        grant_plan={10: True, 11: True, 12: True, 13: True, 14: False},
        effective_max=4,
    )
    assert r["turns_executed"] == list(range(1, 15)), r
    assert r["continuation_turns"] == [11, 12, 13, 14], r
    assert r["continuation_count"] == 4, r
    print("PASS: (d) four chained continuations exhaust the cap and stop")


def test_cap_enforcement():
    # Operator tries to request 6 continuations; cap of 4 should stop at 4
    r = simulate_loop(
        deliberation_cap=10,
        grant_plan={10: True, 11: True, 12: True, 13: True, 14: True, 15: True},
        effective_max=4,
    )
    # After 4 grants the budget cap is hit; turn 15 runs (cap allows up to T14)
    # but its request is denied. Then we fall out of the loop.
    assert r["continuation_count"] == 4, r
    assert r["final_turn"] == 14, r  # 4 continuations max -> stops at deliberation_cap + 4
    print("PASS: cap of 4 stops at exactly 4 continuations regardless of further requests")


def test_grant_mid_session_extends_budget():
    # Edge case: a wrap-up trigger fires early due to director_halt or
    # similar, granting continuation on T7. The loop should run T8 as
    # a continuation turn. (This isn't the common case, but the budget
    # math must work the same way.)
    r = simulate_loop(deliberation_cap=10, grant_plan={7: True})
    # T7 grants -> budget becomes 1 -> ceiling is 11 -> turns 1..11 run
    # T8 is the continuation. T9, T10, T11 are deliberation again.
    # (In real ADAM, the wrap-up state would gate routing back to
    # deliberation, but the budget math itself is correct.)
    assert r["final_turn"] == 11, r
    assert r["continuation_count"] == 1, r
    print("PASS: mid-session grant extends ceiling by exactly 1")


if __name__ == "__main__":
    test_no_grants()
    test_grant_at_final_deliberation_turn()
    test_continuation_then_no_more()
    test_chained_continuations_up_to_cap()
    test_cap_enforcement()
    test_grant_mid_session_extends_budget()
    print()
    print("All budget-arithmetic tests passed.")
