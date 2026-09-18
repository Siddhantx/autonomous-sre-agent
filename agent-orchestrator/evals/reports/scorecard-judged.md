# APOE Judged-Tier Scorecard

Agent: **qwen2.5:3b** · judge: **qwen2.5:3b** · 1 scenario(s)

> **Weak-judge caveat.** A 3B judge scoring a 3B agent is a weak judge. These scores are directional, not authoritative. Point `build_judge` at a stronger model to strengthen them.

| Scenario | Metric | Score | Passed | Reason |
|---|---|---|---|---|
| disk-fill | faithfulness | 1.00 | yes | The score is 1.00 because there are no contradictions with the retrieval context, indicating the `actual output` perfectly aligns with the provided information. |
| disk-fill | answerrelevancy | 0.00 | no | The score is 0.00 because the provided statements do not address the input about a runaway log file, which is irrelevant to the input. |
| disk-fill | hallucination | 0.00 | no | The score is 0.00 because the actual output contradicts the provided context regarding disk usage, with the output providing an unrelated root cause of budget e |
| disk-fill | reasoning_quality | 0.00 | no | The response does not provide a clear diagnosis supported by the evidence. The context mentions a full disk issue, but the actual output states 'unknown' as the |
