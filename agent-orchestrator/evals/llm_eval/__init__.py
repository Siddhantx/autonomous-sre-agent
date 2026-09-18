"""GenAI evaluation layer for APOE.

Sits *on top of* the agent — it never changes how the agent behaves. The
default-deny safety architecture is untouched: this package observes, scores
and reports, and its red-team suite asserts the safety engine holds.

Modules
    dataset  versioned fault-benchmark loading + validation
"""
