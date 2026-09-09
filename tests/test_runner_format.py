import logging

from scanner.runner import format_decision_line, format_summary
from scanner.scoring import Component, Decision


def _decision(**over):
    d = dict(chain="solana", address="ABCDEFGH123", eval_ts=10, as_of=9, anchor_ts=5, anchor_source="tape",
             since_anchor_s=4, score=75.4, tier="CONFIRMED", eligible=True, alertable=True, hard_vetoes=[],
             soft_flags=[], components=[Component("participation", 30, 30), Component("orderflow", 20.4, 25)])
    d.update(over)
    return Decision(**d)


def test_decision_line_formats_every_field_and_logs_without_error(caplog):
    line = format_decision_line(_decision(), "SYM")
    assert line == "decision solana SYM: score=75 [part=30/30 orde=20/25] tier=CONFIRMED anchor=5(tape) since=4s eligible=True *** ALERTABLE ***"
    with caplog.at_level(logging.INFO, logger="runner"):
        logging.getLogger("runner").info(line)
    assert "ALERTABLE" in caplog.text
    # no symbol, no anchor
    line2 = format_decision_line(_decision(anchor_ts=None, anchor_source="none", since_anchor_s=None, eligible=False,
                                           alertable=False, tier="WATCH"), None)
    assert line2.startswith("decision solana ABCDEFGH:") and "anchor=None(none) since=- eligible=False" in line2
    assert "ALERTABLE" not in line2
