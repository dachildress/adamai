"""
Tests for the ADAM lifecycle invariant: Operator is terminal-only.

ADAM lifecycle rule:
  Agents deliberate first. Synthesizer settles the result. Operator
  executes only after synthesis. No durable artifact may be produced
  before final synthesis.

These tests pin the routing behavior of adam.core.router.select_next_speaker:

  - Operator is NEVER selected during normal (non-wrap-up) deliberation,
    regardless of advisory content.
  - The former trigger phrases ("Decision Point", "ratified", "we should
    proceed with") no longer route to Operator; they just continue the debate.
  - The router always returns a valid advisory speaker for those phrases
    (never None, never Operator) -- i.e. removing the gate cannot strand a turn.
  - Artifact-generation instruction blocks are only reachable on the wrap-up
    Operator turn.
  - The wrap-up sequence still runs Synthesizer first, then Operator, with
    Operator's note being the closing-artifact pass.

Run:  python3 test_operator_terminal_only.py
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adam.core import router
from adam.core.router import (
    select_next_speaker,
    SentinelRegistry,
    set_advisory_cycle,
    get_advisory_cycle,
    ADVISORY_CYCLE,
)
from adam.core.session import WrapUpState


# ----------------------------------------------------------------------
# Test harness
# ----------------------------------------------------------------------

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))


def advisory_cycle() -> list:
    """The active advisory rotation, falling back to a standard trio."""
    cyc = get_advisory_cycle()
    if not cyc:
        cyc = ["Logician", "Seeker", "Visionary"]
        set_advisory_cycle(cyc)
    return cyc


def fresh_wrap_up(active: bool = False, synth_done: bool = False,
                  operator_done: bool = False) -> WrapUpState:
    """A WrapUpState in a chosen phase. synth_wrap_up_turn high so the
    NON-wrap-up tests stay in normal routing."""
    w = WrapUpState(synth_wrap_up_turn=99, operator_wrap_up_turn=100)
    if active:
        w.trigger("turn_budget")
    w.synth_done = synth_done
    w.operator_done = operator_done
    return w


def advisory_msg(text: str, agent: str = "Logician") -> dict:
    return {"role": "assistant", "content": text, "agent": agent}


def history_with_last_advisory(text: str) -> list:
    """A short, valid deliberation history whose most-recent advisory
    message contains `text`. Kept to TWO advisory messages so it stays
    below synth_cadence (3) -- otherwise the Synthesizer cadence would
    legitimately fire and mask the advisory-rotation assertion. Tests
    that specifically want cadence behavior build their own history."""
    cyc = advisory_cycle()
    a0 = cyc[0]
    a1 = cyc[1] if len(cyc) > 1 else cyc[0]
    return [
        advisory_msg("Opening analysis of the question.", a0),
        advisory_msg(text, a1),
    ]


def route(history, current_turn=3, wrap_up=None, sentinel_reg=None):
    if wrap_up is None:
        wrap_up = fresh_wrap_up(active=False)
    if sentinel_reg is None:
        sentinel_reg = SentinelRegistry()
    return select_next_speaker(
        history=history,
        synth_cadence=3,
        current_turn=current_turn,
        sentinel_reg=sentinel_reg,
        wrap_up=wrap_up,
    )


# ----------------------------------------------------------------------
# 1. Former trigger phrases do NOT route to Operator mid-debate
# ----------------------------------------------------------------------

def test_decision_point_does_not_trigger_operator():
    hist = history_with_last_advisory(
        "Decision Point: we should structure the K-5 plan around three phases."
    )
    agent, note, concern = route(hist)
    check("'Decision Point' does not select Operator",
          agent != "Operator", f"got {agent!r}")
    check("'Decision Point' returns a valid non-Operator deliberation speaker",
          agent in (set(advisory_cycle()) | {"Synthesizer", "Sentinel"}), f"got {agent!r}")
    check("'Decision Point' never returns None",
          agent is not None)


def test_ratified_does_not_trigger_operator():
    hist = history_with_last_advisory(
        "The committee ratified: adopt the phased rollout immediately."
    )
    agent, note, concern = route(hist)
    check("'ratified' does not select Operator",
          agent != "Operator", f"got {agent!r}")
    check("'ratified' returns a valid non-Operator deliberation speaker",
          agent in (set(advisory_cycle()) | {"Synthesizer", "Sentinel"}), f"got {agent!r}")


def test_proceed_with_does_not_trigger_operator():
    hist = history_with_last_advisory(
        "Given the analysis, we should proceed with the district-wide plan."
    )
    agent, note, concern = route(hist)
    check("'we should proceed with' does not select Operator",
          agent != "Operator", f"got {agent!r}")
    check("'we should proceed with' returns a valid non-Operator deliberation speaker",
          agent in (set(advisory_cycle()) | {"Synthesizer", "Sentinel"}), f"got {agent!r}")


# ----------------------------------------------------------------------
# 2. Operator never runs before wrap-up, across many turns / contents
# ----------------------------------------------------------------------

def test_operator_never_selected_during_normal_deliberation():
    phrases = [
        "Decision Point: proceed.",
        "We ratified: the plan.",
        "we should proceed with implementation.",
        "Let's create the final document now.",
        "Here is the implementation plan, ready to produce.",
        "I think we are done; produce the report.",
        "Ordinary analysis with no trigger words at all.",
    ]
    ok = True
    for turn in range(1, 12):          # well within the synth_wrap_up_turn=99
        for p in phrases:
            hist = history_with_last_advisory(p)
            agent, _, _ = route(hist, current_turn=turn)
            if agent == "Operator":
                ok = False
                print(f"        -> Operator wrongly selected at turn {turn} "
                      f"for phrase {p!r}")
            if agent is None:
                ok = False
                print(f"        -> None returned at turn {turn} for phrase {p!r}")
    check("Operator is never selected during normal deliberation (no wrap-up)",
          ok)


# ----------------------------------------------------------------------
# 3. Router always resolves a valid speaker (no stranding)
# ----------------------------------------------------------------------

def test_router_always_returns_valid_speaker():
    cyc = advisory_cycle()
    valid = set(cyc) | {"Synthesizer", "Sentinel"}
    ok = True
    samples = [
        "Decision Point: x.", "ratified: y.", "we should proceed with z.",
        "Plain deliberation text.", "Another contribution.",
        "FERPA student data concern raised here.",   # may route to Sentinel
    ]
    for turn in range(1, 8):
        for s in samples:
            hist = history_with_last_advisory(s)
            agent, _, _ = route(hist, current_turn=turn)
            if agent not in valid or agent == "Operator":
                ok = False
                print(f"        -> invalid speaker {agent!r} at turn {turn} "
                      f"for {s!r}")
    check("Router always returns a valid non-Operator speaker mid-debate", ok)


# ----------------------------------------------------------------------
# 4. Sentinel still fires mid-debate (only Operator was made terminal)
# ----------------------------------------------------------------------

def test_sentinel_still_fires_mid_debate():
    # A clear Sentinel predicate ("FERPA" / "student data") should still
    # route to Sentinel during normal deliberation.
    hist = history_with_last_advisory(
        "This plan would collect individual student data and may implicate FERPA."
    )
    agent, note, concern = route(hist, sentinel_reg=SentinelRegistry())
    check("Sentinel still fires on a risk predicate mid-debate",
          agent == "Sentinel", f"got {agent!r}")
    check("Sentinel fire reports a concern label",
          bool(concern), f"concern={concern!r}")


# ----------------------------------------------------------------------
# 5. Wrap-up sequence: Synthesizer first, then Operator (terminal artifact)
# ----------------------------------------------------------------------

def test_wrap_up_runs_synth_then_operator():
    hist = history_with_last_advisory("Final deliberation content.")

    # Wrap-up active, synth NOT done yet -> must force Synthesizer.
    w = fresh_wrap_up(active=True, synth_done=False, operator_done=False)
    agent, note, _ = route(hist, current_turn=99, wrap_up=w)
    check("Wrap-up forces Synthesizer before Operator",
          agent == "Synthesizer", f"got {agent!r}")

    # Wrap-up active, synth done, operator NOT done -> must force Operator.
    w2 = fresh_wrap_up(active=True, synth_done=True, operator_done=False)
    agent2, note2, _ = route(hist, current_turn=99, wrap_up=w2)
    check("Wrap-up forces Operator after Synthesizer",
          agent2 == "Operator", f"got {agent2!r}")
    check("Wrap-up Operator note describes the closing artifact pass",
          note2 is not None and "FINAL OPERATOR TURN" in note2,
          f"note start: {None if note2 is None else note2[:60]!r}")


# ----------------------------------------------------------------------
# 6. Artifact-generation instructions only appear on the wrap-up Operator turn
# ----------------------------------------------------------------------

def test_artifact_instructions_only_at_wrap_up():
    # During normal deliberation, NO returned note should carry the
    # artifact-delivery-mode instructions, for any phrase.
    artifact_marker = "ARTIFACT DELIVERY MODE"
    leaked = False
    for p in ["Decision Point: go.", "ratified: go.", "we should proceed with go.",
              "produce the final document", "plain text"]:
        hist = history_with_last_advisory(p)
        _, note, _ = route(hist)
        if note and artifact_marker in note:
            leaked = True
            print(f"        -> artifact instructions leaked mid-debate for {p!r}")
    check("Artifact-delivery instructions never appear mid-debate", not leaked)

    # On the wrap-up Operator turn, they SHOULD appear.
    w = fresh_wrap_up(active=True, synth_done=True, operator_done=False)
    hist = history_with_last_advisory("Final content.")
    _, wrap_note, _ = route(hist, current_turn=99, wrap_up=w)
    check("Artifact-delivery instructions present on wrap-up Operator turn",
          wrap_note is not None and artifact_marker in wrap_note)


# ----------------------------------------------------------------------

def main() -> int:
    print("ADAM Operator-terminal-only invariant tests")
    print("=" * 60)

    tests = [
        test_decision_point_does_not_trigger_operator,
        test_ratified_does_not_trigger_operator,
        test_proceed_with_does_not_trigger_operator,
        test_operator_never_selected_during_normal_deliberation,
        test_router_always_returns_valid_speaker,
        test_sentinel_still_fires_mid_debate,
        test_wrap_up_runs_synth_then_operator,
        test_artifact_instructions_only_at_wrap_up,
    ]
    for t in tests:
        print(f"\n{t.__name__}:")
        t()

    print("\n" + "=" * 60)
    print(f"RESULT: {PASSED} passed, {FAILED} failed")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
