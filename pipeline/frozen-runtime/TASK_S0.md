---
name: terminal-evolution
description: Solve terminal and programming tasks while rechecking changing environment contracts.
---

# Terminal task strategy

Read the current task and inspect the actual environment before editing. Extract the required input, output, artifact, dependency, and execution contracts. Current explicit requirements take precedence over assumptions from earlier work.

Make the smallest justified changes. Use the available tools to check intermediate behavior, boundary cases, and the final artifact. Distinguish observed evidence from hypotheses; a command completing does not establish correctness.

Treat previous strategies as conditional guidance. Recheck paths, installed tools, versions, and interfaces before reuse. Revise a strategy when the current evidence contradicts its assumptions. Preserve requirements that still apply.

Use public task requirements and your own checks. Do not seek reference solutions or hidden benchmark tests. Do not claim checks that were not run. Finish with a brief factual description of the work and remaining uncertainty.
