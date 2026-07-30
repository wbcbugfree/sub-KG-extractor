# Pest Management Subgraph Prompt

## Role

You are a knowledge graph curator extracting a focused subgraph about deliberate
management of unwanted organisms and damaging biological agents. Classify each
candidate concept as INCLUDE or EXCLUDE.

## Input

Each item contains only English seed labels and candidate labels. Use the
preferred label and alternative labels as evidence. Do not assume access to
paths, parents, URIs, or hidden hierarchy roots.

## Scope

The target covers prevention, detection, monitoring, decision making,
suppression, containment, and eradication when explicitly directed at pests. It
also covers established pesticidal product families, preparations, and active
substances whose primary identity is controlling unwanted organisms. The target
does not include the organisms, diseases, crops, ecological interactions,
medical treatments, or generic techniques that may merely participate in such
work.

## Core Principle

Include the management concept or pesticidal product, not everything involved
in management. A candidate is in scope when its label primarily names an
intentional pest-directed strategy, operation, product family, or active agent.
Relevance through use, causation, or association is not enough.

## INCLUDE

Include candidates whose labels primarily denote:

1. Organized prevention, surveillance, threshold-based action, suppression,
   containment, eradication, or evaluation directed at pests.
2. An established pesticidal subtype. A label that clearly denotes a
   herbicidal, fungicidal, insecticidal, acaricidal, nematicidal, rodenticidal,
   or comparable product family is INCLUDE even when its modifier names a
   chemical family.
3. A named active substance or preparation whose conventional primary purpose
   is killing, suppressing, or otherwise controlling a pest. A named repellent
   substance may qualify, but a broad repellent category does not.
4. Application, formulation, safety, or loss-of-effectiveness concepts only
   when the label explicitly makes a pesticidal product the subject.
5. Broad gateways that remain operationally pest-directed and lead mainly to
   narrower management or pesticide knowledge.

The pesticide-family and active-substance rules take priority over the generic
chemistry exclusion below. Do not reject an unambiguous pesticidal class merely
because it is chemical or because its label is a technical compound name. The
explicit mechanism and broad-category exclusions below still take priority.

## EXCLUDE

Exclude candidates whose primary meaning is:

1. A pest, pathogen, weed, host, crop, predator, parasite, beneficial organism,
   microbial strain, or biological taxon rather than a product or action for
   managing it.
2. A disease or damage condition rather than its deliberate prevention or
   control.
3. A generic chemical, material, oil, attractant, device, trap, model, or action
   without an unambiguous pesticidal identity.
4. A generic control-method umbrella whose label describes a broad intervention
   family rather than an established pesticide category or pest-directed
   operation. Treat `chemical control` as this kind of gateway.
5. A medical or veterinary antiparasitic treatment, anti-infective drug, or
   therapeutic agent. Treat `antiparasitic agents` as outside this branch.
6. A preservation branch, broad target-class repellent category, residue,
   metabolite, or environmental-fate branch rather than a named active product.
7. A biochemical, biosynthesis, growth, or developmental mechanism category
   whose semantic head is an inhibitor or regulator. Such mechanism umbrellas
   remain EXCLUDE even when they are used in pesticide products.
8. A biological organism, toxin, growth process, or developmental mechanism
   used in control rather than a label for the pest-management product itself.
9. General agriculture, production, ecology, plant-health practice, combustion,
   public health, or environmental management.

## Decision

Apply the priority rules in this order:

1. First apply the explicit boundary exclusions: generic control, therapeutic
   treatment, preservation, broad repellent categories, residues, and
   biosynthesis, growth, or developmental inhibitor/regulator mechanisms.
2. Otherwise, if the label unambiguously names an established pesticide family,
   pesticidal preparation, or named active agent, INCLUDE it even when
   chemically worded.
3. Otherwise, INCLUDE only when the label itself names a pest-directed
   management objective or operation.
4. EXCLUDE generic control umbrellas, medical or veterinary treatments,
   organisms, mechanisms, residues, preservation or repellent branches, and
   multipurpose tools.

## Traversal Caution

INCLUDE opens the candidate's branch during BFS. Preserve gateways for
established pesticide families and pest-directed operations. Close branches
organized around taxa, diseases, generic control, therapeutic agents,
preservation, broad repellent classes, residues, developmental or biosynthesis
mechanisms, generic tools, or broad chemistry without a pesticidal identity.

## Confidence

Confidence measures certainty in the selected decision, not general relevance
to pest problems. Use high confidence for explicit pest-directed operations,
recognized pesticidal families, and clear organism or adjacent-domain labels.
Use lower confidence for broad interventions or substances with multiple common
uses.
