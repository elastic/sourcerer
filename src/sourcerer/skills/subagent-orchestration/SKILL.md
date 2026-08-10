---
name: "subagent-orchestration"
description: "Use when a request decomposes into multiple independent subtasks that can be worked on separately and combined afterward."
---
# Parallel subagent delegation

## Default: delegate as one task
Unless you already know of multiple distinct, already-located targets, delegate the whole investigation to a single subagent as one task. A subagent working serially through a multi-step investigation costs less than N subagents each paying their own fixed overhead (system prompt, skill instructions, tool schemas) - and can't redundantly re-search ground it's already covered, the way parallel subagents can.

## Cost test (apply before fanning out to more than one)
Each subagent pays its own token cost for every search it runs, plus its own fixed overhead. If two or more candidate tasks would plausibly search the same files or directories to get oriented, spawning them separately means paying that discovery cost once per subagent instead of once total. Shared search space is redundant spend, regardless of whether the tasks are otherwise independent.

Before writing more than one task, ask: **would these subagents likely re-derive the same location or content to do their job?** If yes:
- **Locate first, fan out second.** Resolve the shared target yourself (or with one subagent call) - get the actual file paths, directories, or line ranges - before spawning anything else.
- **Hand subagents targets, not search problems.** Once located, write each task around a concrete pointer ("read `x-pack/.../esql_tool.ts` lines 40-120 and explain X") rather than a keyword strategy ("search for terms like esql, execute_query, ... in agent builder directories"). A subagent given a known path does one cheap read call; a subagent given a search problem burns tokens on multiple exploratory searches - and burns them again per subagent if several are all guessing at the same thing.
- **Only fan out once discovery is done and targets have diverged.** If after locating things there's genuinely nothing left to parallelize (one file, one concern), stick with a single task.

## How to use
1. Locate the relevant files/directories first - directly, or with a single subagent call - until you have concrete targets, not just a search strategy.
2. Decide whether more than one task is actually warranted using the cost test above. Default to one.
3. If more than one, break the work into tasks each built around one already-known target (a specific path, directory, or narrow sub-question) - one per subagent. Count = number of distinct targets, not number of ways to search for the same thing.
4. Call the `sourcerer.subagents.spawn` tool with `tasks` set to that list.
5. The tool's response contains each subagent's raw result. Compose your final answer directly from them.

## When NOT to use
- Sequential tasks where step 2 depends on step 1's output.
- Any task set where you don't yet have concrete targets - locate first; fanning out multiple open-ended searches over the same unknown location is the single most expensive pattern this skill can produce.
- A single target/concern - just answer directly or delegate as one task; the fan-out overhead isn't worth it.
- Very large fan-outs (15-20+) - reconsider whether that many distinct targets actually exist.