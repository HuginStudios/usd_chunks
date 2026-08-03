---
name: grill-me
description: Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "grill me".
---

Interview me relentlessly about every aspect of this plan until we reach a shared understanding. Walk down each branch of the design tree, resolving dependencies between decisions one-by-one. For each question, provide your recommended answer.

Ask the questions one at a time.

If a question can be answered by exploring the codebase, explore the codebase instead.

## After all decisions are resolved

Once the user approves the final summary:

1. **If NOT already in a project folder** (no package.json, no CONTEXT.md, no src/ directory):
   - Create a new project folder with a fitting name (ask the user to confirm the name)
   - Initialize git
   - Write CONTEXT.md with all decisions from the grill session

2. **If already in a project folder**:
   - Update or create CONTEXT.md with the decisions from this session

3. **Then run /to-issues** to break the plan into vertical slices and present them for approval.

Do NOT start building or installing dependencies. The next step after /to-issues is for the user to decide whether to run /sven or build manually.
