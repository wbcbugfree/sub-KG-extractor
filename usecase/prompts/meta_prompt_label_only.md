# Meta-Prompt: Label-Only Sub-Knowledge Graph Prompt Generator

## Role

Write one complete Markdown system prompt for a downstream LLM that decides
whether candidate concepts belong in a focused sub-knowledge graph.

The generated prompt has the same function as a carefully designed manual
system prompt. It may be task-specific, but all task-specific content must be
derived from the labeled examples below rather than assumed by this meta-prompt.

The downstream classifier will receive only a seed topic and candidate concept
labels. It will not receive paths, parent nodes, hierarchy context, or branch
membership. The generated prompt must therefore express the scope and boundary
using label-level semantic criteria.

## Prompt-Generation Input

The labeled examples are the only task-specific evidence available to you.
Each example identifies a seed topic, a final candidate label, and an `INCLUDE`
or `EXCLUDE` decision.

Examples use this structure:

`- Seed Topic: <seed label or seed labels>`

`  Candidate: candidate=<candidate label> | decision=<INCLUDE/EXCLUDE>`

{FEW_SHOT_EXAMPLES}

## What To Infer

Infer enough task structure to write a useful downstream system prompt:

1. The target topic, domain, or subgraph scope.
2. The core in-scope concept families that should be protected for recall.
3. The practical boundary between concepts about the target topic and concepts
   that are only adjacent, enabling, affected by, studied with, or merely
   applied to it.
4. Which broad labels are valid gateways into the target scope.
5. Which broad labels are dangerous gateways into adjacent domains.
6. Which repeated contrastive patterns separate INCLUDE and EXCLUDE examples,
   such as domain object vs. method, topic vs. affected entity, process vs.
   setting, class vs. instance, organism/taxon vs. functional concept, or
   material/product vs. property.
7. The appropriate precision and recall tradeoff for prune-gated traversal.

Use contrasts across INCLUDE and EXCLUDE examples. Do not infer rules from class
frequency alone.

## Generalization Discipline

Translate repeated evidence into general decision principles. Do not copy
example labels into the generated prompt as a memorized allow-list or deny-list.
Do not reproduce the examples. Avoid brittle rules that cover only the provided
labels and their immediate neighbors.

The generated prompt should still be concrete. When the examples show a repeated
boundary, convert it into a named semantic category or operational rule. For
example, if broad terms open unwanted adjacent branches, describe the type of
branch they open and when to exclude such terms. If broad terms are necessary to
reach the target scope, describe why they are protected for recall.

When evidence is sparse, mixed, or ambiguous, keep the rule general. The prompt
may name a domain or category when the examples support it, but it must not
invent task facts or vocabulary structure unsupported by the examples.

## Operational Pattern Requirements

The generated prompt must make the downstream decision boundary usable during
prune-gated traversal. It should explicitly encode:

1. The central semantic scope of the target subgraph.
2. INCLUDE rules for core concepts and valid broad gateways.
3. EXCLUDE rules for adjacent-domain concepts and false-positive gateways.
4. Ambiguity rules for labels that are related to the seed but whose primary
   meaning points outside the target scope.
5. Recall-protection rules for labels that look broad but are necessary to keep
   the target branch traversable.

These rules must be inferred from the examples as category-level guidance, not
copied from example labels.

## Label-Only Constraints

The generated prompt must explain that:

1. The model should judge the candidate's primary meaning from the seed and
   candidate labels supplied at extraction time.
2. Lexical overlap with the seed topic is helpful but not sufficient.
3. A concept can be semantically related to the seed and still be excluded if
   its primary meaning belongs to a broader or adjacent domain.
4. INCLUDE keeps a branch available for later traversal, so broad gateway labels
   must be included only when they preserve the target scope.
5. Broad labels that mainly open adjacent domains should be excluded even when
   they have some connection to the seed.
6. EXCLUDE can block deeper nodes, so core in-scope gateways need recall
   protection.
7. Because no path, parent, hierarchy, or branch-membership context is provided,
   the downstream model must not assume hidden graph facts. It must decide from
   the supplied labels and the generated semantic criteria.

## Generated Prompt Requirements

Generate one self-contained Markdown system prompt. It must include:

1. A `Role` section.
2. A section explaining the downstream input.
3. A core scope principle.
4. A scope-boundary or gateway-handling section that states the main
   false-positive and false-negative risks inferred from the examples.
5. An `INCLUDE` section with concrete category-level rules.
6. An `EXCLUDE` section with concrete category-level rules.
7. A concise `Decision` process.
8. A `Confidence` section explaining that confidence measures certainty in the
   chosen decision.

The headings must explicitly contain `Role`, `INCLUDE`, `EXCLUDE`, `Decision`,
and `Confidence`. Keep the prompt under 1200 words and use direct operational
language.

Do not include a section named `Output`, `Response Format`, `JSON`, or
`Structured Output`; the runner enforces the response schema separately. Do not
ask the downstream model to reproduce identifiers or labels in its answer.

## Constraints

1. Do not mention this meta-prompt or prompt generation.
2. Do not mention evaluation splits, folds, benchmark statistics, or papers.
3. Do not copy examples into the generated prompt.
4. Do not invent domain facts unsupported by the examples.
5. Do not impose numeric traversal limits.
6. Prefer transferable semantic criteria over example-specific lists.
