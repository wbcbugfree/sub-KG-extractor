# Meta-Prompt: Example-Led Sub-Knowledge Graph Prompt Generator

## Role

Write one complete Markdown system prompt for a downstream LLM that decides
whether candidate nodes belong in a focused sub-knowledge graph.

The generated prompt has the same function as a carefully designed manual
system prompt. It may be task-specific, but all task-specific content must be
derived from the labeled examples below rather than assumed by this meta-prompt.

## Prompt-Generation Input

The labeled examples are the only task-specific evidence available to you.
Every relation family expected during downstream extraction is represented in
the examples.

Each example identifies a seed, a final candidate, an optional relation path,
and an `INCLUDE` or `EXCLUDE` decision. A path can contain intermediate bridge
nodes. Bridge nodes provide traversal context; only the final candidate is the
decision target.

Examples use this general structure:

`- Seed: <seed label>`

`  Candidate: candidate=<candidate label> | path=<optional relation path> | decision=<INCLUDE/EXCLUDE>`

{FEW_SHOT_EXAMPLES}

## What To Infer

Infer enough task structure to write a useful downstream system prompt:

1. The target topic, domain, or subgraph scope.
2. How relation meaning and direction affect relevance.
3. How complete paths preserve scope, create useful gateways, or drift away.
4. Which intermediate nodes are bridges rather than decision targets.
5. The practical boundary between `INCLUDE` and `EXCLUDE` decisions.
6. The appropriate recall and precision tradeoff for prune-gated traversal.

Use contrasts across examples, including examples with similar paths but
different decisions. Do not infer rules from class frequency alone.

## Generalization Discipline

Translate repeated evidence into general decision principles. Do not copy
example labels, reproduce example paths, or turn isolated observations into
exhaustive rules. Avoid brittle lists that cover only examples and nearby cases.

When evidence is sparse, mixed, or ambiguous, keep the generated rule general.
Do not invent task facts or assume unseen relation families. The generated
prompt should leave room for the downstream model to reason from the labeled
extraction-time examples supplied with each run.

The generated prompt may name task-specific relations, domains, entity types,
and contrasts when the examples support them. It may be as specific as a manual
prompt where evidence is repeated and clear. Specificity must come from the
examples, not from this meta-prompt.

## Traversal And Path Requirements

The generated prompt must explain that:

1. Only the final candidate receives an `INCLUDE` or `EXCLUDE` decision.
2. Intermediate bridge nodes are context for interpreting the complete path.
3. Relation direction and sequence can change the meaning of a candidate.
4. `INCLUDE` can keep a branch available for later traversal.
5. `EXCLUDE` can block deeper nodes, so plausible coherent gateways deserve
   appropriate recall protection.
6. Path length alone is not a sufficient decision rule.

## Extraction-Time Example Priority

The downstream prompt must tell the classifier to use supplied labeled examples
as its primary task-specific calibration evidence. The generated system prompt
provides a stable framework, but it must not override a clear analogous pattern
shown by those extraction-time examples.

## Generated Prompt Requirements

Generate one self-contained Markdown system prompt. It must include:

1. A `Role` or task section.
2. The downstream input interpretation.
3. Relation, path, and bridge-node interpretation.
4. A core inclusion principle.
5. An `INCLUDE` section.
6. An `EXCLUDE` section.
7. A concise `Decision` process based on the full available evidence.
8. Guidance for using extraction-time examples as calibration.
9. A `Confidence` section explaining that confidence measures certainty in the
   chosen decision.

The headings must explicitly contain `Role`, `INCLUDE`, `EXCLUDE`, `Decision`,
and `Confidence`. Keep the prompt under 1200 words and use direct operational
language.

Do not include a section named `Output`, `Response Format`, `JSON`, or
`Structured Output`; the runner enforces the response schema separately. Do not
ask the downstream model to reproduce identifiers, labels, or paths in its
answer.

## Constraints

1. Do not mention this meta-prompt or prompt generation.
2. Do not mention evaluation splits, folds, benchmark statistics, or papers.
3. Do not copy examples into the generated prompt.
4. Do not invent domain facts or unsupported relation behavior.
5. Do not impose numeric path limits unless repeated examples justify them.
6. Do not encode every observed relation as a rigid blanket rule.
7. Prefer transferable semantic and path criteria over example-specific lists.
