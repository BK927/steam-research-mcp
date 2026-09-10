# Plugin routing evaluation

These fixtures evaluate whether Codex loads the Steam plugin only when it is useful. They are data, not automated unit tests.

- `direct.jsonl`: 10 prompts that explicitly name Steam.
- `indirect.jsonl`: 10 prompts whose Steam intent is implicit.
- `unrelated-negative.jsonl`: prompts that should not select this Steam plugin.
- `result.schema.json`: one JSONL result record schema.

For a manual run, install the source plugin through the normal reviewed workflow, start a fresh Codex task for every fixture, submit only its `prompt`, and record which plugins and tools were selected. Compare `selected_plugins` with `expected_plugin`; a `null` expectation means this Steam plugin should not load. Do not reuse a task because prior tool discovery changes context.

Suggested output file: `results/YYYY-MM-DD-runtime.jsonl`. Validate each record against `result.schema.json`, then report direct recall, indirect recall, negative precision, wrong-plugin selection, and tool-call success separately.

Status: fixtures and schema prepared; no new Codex tasks were created and no routing run has been executed.
