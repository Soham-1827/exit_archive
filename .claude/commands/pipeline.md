---
description: Run the Exit Archive agent pipeline — orchestrator plans and reviews, executor builds, tester gates, deployer ships. Optional argument - number of tasks to complete this run (default 3), or "plan" to re-plan, or "handover" to close the session.
---

Run the Exit Archive pipeline. Argument: `$ARGUMENTS` (a number of tasks, `plan`, or `handover`; empty means 3 tasks).

Subagents cannot start subagents, so **you (the main session) are the dispatcher**. The orchestrator decides; you carry out its dispatch blocks exactly.

## Loop
1. **Orchestrator.** Start the `orchestrator` agent (model opus, effort high). Mode is `plan` if `TODO.md` does not exist or the argument is `plan`; `handover` if the argument is `handover`; otherwise `review` with the last worker report (or "start of session" if none). Pass the previous worker report verbatim.
2. **Read its DISPATCH block.**
   - `commit:` is a message → run the quality checks (`uv run pytest -q`, `uv run ruff check .`), then `git add -A`, check `git diff --cached` for secrets, commit with that message plus the attribution line, and `git push`. If checks fail, do not commit; send the failure back to the orchestrator.
   - `agent: executor | tester | deployer` → start that agent with the dispatch's task, instructions and skills. Hand its final report back to step 1.
   - `agent: user` → stop and put the question to the user.
   - `agent: none` → the run is finished.
3. Repeat until the requested number of tasks is committed, the orchestrator says `none` or `user`, or the same task fails the tester twice.
4. **Always finish with a `handover` run** of the orchestrator, then commit and push `HANDOVER.md` and `TODO.md` (`docs: update handover`).

## Rules
- Never paste a secret into an agent prompt. Workers read the Harvest test token from `~/.harvest_test` only for `live`-marked tests.
- Never deploy to production, create paid resources or set live payment keys without the user saying yes in this session.
- Report to the user in a few lines per completed task: what was built, test result, commit hash.
