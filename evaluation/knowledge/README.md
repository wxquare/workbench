# Knowledge baseline inputs

Start with [the collection checklist](collection-guide.md). The `*.example.json` files are schemas and placeholder examples only; they are not accepted as a business baseline.

Keep real manifests, questions, holdout data, import state, Run evidence, and review files under ignored local paths. Do not place credentials in these files.

For an initial, fully fictional Chinese dataset, see [the synthetic evaluation pack](synthetic/README.md): 24 seed entries, 32 dev questions, 12 holdout questions, and separate human review checklists. The user has approved all 32 dev input labels. All 24 seeds have been published locally in an isolated demo team, with version and chunk hashes verified; runtime mappings remain under ignored local artifacts. Holdout review and model output review remain pending. Its `synthetic` datasets cannot pass business acceptance.
