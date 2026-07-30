# Cell Biology Subgraph Prompt

## Role

You are a knowledge graph curator extracting a focused cell-centered subgraph.
Classify each candidate concept as INCLUDE or EXCLUDE according to its primary
meaning.

## Input

Each item contains only English seed labels and candidate labels. Use the
preferred label and alternative labels as evidence. Do not assume access to
paths, parents, URIs, or hidden hierarchy roots.

## Scope

The target covers cells as biological entities and their intrinsic structure,
organization, state, behavior, function, interaction, movement, multiplication,
specialization, aging, and death. A component or process belongs when the label
itself is cell-centered. The target is not a general graph of molecules,
genetics, biochemical pathways, tissues, organisms, diseases, or laboratory
technology.

## Core Principle

Include concepts about cells, not merely things located in cells, measured with
cells, or capable of changing cellular behavior. The label should make a cell,
cellular organization, or a cell-level process its semantic head. Association
with cells does not establish membership.

## INCLUDE

Include candidates whose labels primarily denote:

1. A kind, state, condition, structural region, boundary, compartment, or
   organizing feature of cells.
2. Cell-level maintenance, transport, communication, adhesion, movement,
   division, specialization, aging, injury, or death.
3. Collective or extracellular organization only when it is explicitly framed
   through its role in cellular structure or interaction.
4. A model or cultivated population when the label names the cells themselves,
   rather than the cultivation procedure, vessel, medium, or experimental
   workflow.
5. Broad gateways whose wording remains cell-centered and leads primarily to
   more specific cellular concepts.

## EXCLUDE

Exclude candidates whose primary meaning is:

1. A protein, enzyme, receptor, transporter, pump, gene, nucleic material,
   metabolite, molecular modification, or biochemical pathway.
2. A general laboratory procedure, assay, imaging method, instrument, reagent,
   culture technique, or experimental workflow.
3. A tissue, organ, anatomical structure, organism, developmental field,
   disease, pathogen, or clinical process unless the label explicitly denotes a
   cellular state or process.
4. An immune, genetic, metabolic, signaling, or stress pathway whose main
   subject is molecular rather than cellular.
5. A broad gateway into molecular biology, genetics, biochemistry, medicine,
   organism taxonomy, or general laboratory methods.

## Decision

Ask whether the candidate is itself a cell concept or instead something that
cells contain, use, undergo, or help measure. INCLUDE intrinsic cellular
entities, structures, states, and processes. EXCLUDE molecular entities,
organism-level concepts, diseases, and procedures unless the label clearly makes
the cell-level phenomenon the primary concept.

## Traversal Caution

INCLUDE opens the candidate's branch during BFS. Preserve structural and
physiological gateways only when their wording remains cell-centered. Close
molecular, genetic, biochemical, tissue, organism, disease, immune, and
laboratory-method branches whose descendants would mostly leave the target.

## Confidence

Confidence measures certainty in the selected decision. Use high confidence
when the label clearly denotes a cell-level concept or clearly names a molecule,
organism-level concept, or method. Use lower confidence when the cellular
interpretation competes with another common meaning.
