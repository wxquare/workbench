# Tool Conversation Evaluation

The `tool-conversation-eval-v1` dataset evaluates observable tool behavior in
`conversation.tool_use`. It is separate from knowledge citation quality.

Use `workbench eval validate`, `run`, and `score` with a reviewed dev dataset
and a JSON config whose `expected_graph_id` is `conversation.tool_use`.
`run_tool_experiment` reuses the existing request keys and journal; a restart
never creates a second Run for an already accepted case.

Rules check call sequence, allowlists, terminal status, tool basis lines, and
evidence readability. They cannot approve semantic quality or facts. A
completed report therefore keeps `quality_status=unavailable` until a human
reviewer signs each case.

The web search contract is in `web-search-contract.json`. A real run requires
an approved external MCP that enforces `domain_policy_id` before sending the
search. Search snippets are not fetched pages and are not knowledge-base
citations.
