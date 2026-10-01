# Spikes

A spike is a time-boxed investigation that answers **one question** before anyone
builds a feature. linemon is used as evidence, so new capabilities are thought
through first: what the right approach is, whether it fits the
[product principles](../../AGENTS.md#product-principles), and what it costs on a
Raspberry Pi.

## Lifecycle

1. **Issue.** Each spike is a GitHub issue labelled `spike`, created from the spike
   template. It states the question, approach, constraints, deliverables, "done when"
   and a time-box. The [roadmap](../roadmap.md) lists them by phase.
2. **Investigation.** Prototype code is fine, in a branch or under `docs/spikes/`, but
   it doesn't get merged into the product. Stop at the time-box: an incomplete answer
   with what was learned is better than a spike that never ends.
3. **Findings note.** Add `docs/spikes/NNN-short-name.md`, where `NNN` is the issue
   number, using the template below, in a pull request that closes the issue.
4. **Follow-up.** Open implementation issues (or park the idea) as the note recommends,
   and update the [roadmap](../roadmap.md).

A spike that finds the feature doesn't fit the principles, or isn't worth it, has
succeeded: that is an answer too.

## Findings note template

```markdown
# NNN: <spike title>

Issue: #NNN · Time spent: <hours> of <time-box> · Date: YYYY-MM-DD

## Question
The question from the issue, unchanged.

## Answer
Two or three sentences: what we should do, or not do, and why.

## What we tried
Approaches, measurements and results. Numbers over adjectives; say on what hardware.

## Fit with the principles
Anything that touches the principles or boundaries, and how the recommendation
respects them. Note any trade-off explicitly.

## Recommendation
- Follow-up issues: #…, #…
- Or: parked, because …

## Open questions
What's still unknown, and what would answer it.
```
