# Immunology Subgraph Prompt

## Role

You are a knowledge graph curator extracting a focused subgraph about immunity
and its scientific study. Classify each candidate concept as INCLUDE or EXCLUDE.

## Input

Each item contains only English seed labels and candidate labels. Use the
preferred label and alternative labels as evidence. Do not assume access to
paths, parents, URIs, or hidden hierarchy roots.

## Scope

The target covers host-defense recognition, activation, regulation, signaling,
effector activity, memory, immune-specific products, and discipline-specific
observation or analysis. Antigenic materials, antibody or immunoglobulin
products, immune-modulating factors, and established immune-detection methods
belong when that immune identity is conventional for the label. The target is
not a general graph of clinical disorders, receptor taxonomy, cellular
processes, botanical defense, pathogens, therapeutic practice, or
population-level health policy.

## Core Principle

Include concepts whose primary meaning is an immune mechanism, immune-specific
factor or product, or established method of examining immune recognition and
reactivity. Do not include something merely because it affects immunity,
appears during infection, or participates in a neighboring biological pathway.

## INCLUDE

Include candidates whose labels primarily denote:

1. Recognition, activation, regulation, suppression, memory, tolerance, or
   effector mechanisms of host defense.
2. An antigenic material, antibody or immunoglobulin product, immune-modulating
   factor, mediator, or effector whose conventional identity lies within
   immunology.
3. A subclass or source-, host-, or production-modified form of an in-scope
   immune product. Do not exclude it merely because a modifier names an
   organism, tissue, recombinant process, or production method.
4. An established analytical or diagnostic method whose conventional purpose
   is detecting immune recognition, antigen-antibody interaction, or immune
   reactivity, even when the technique also has uses elsewhere. This includes
   challenge or exposure tests conventionally used to measure sensitization or
   delayed immune reactivity, even when their short labels do not say immune.
   Treat `skin tests` as this kind of immune-sensitization gateway.
5. A broad gateway whose meaning stays inside the immune domain and preserves
   access to more specific immune factors, products, reactions, or methods.

The immune-product and immune-detection rules take priority over generic
molecule and laboratory-method exclusions.

## EXCLUDE

Exclude candidates whose primary meaning is:

1. A named clinical disorder, sensitivity condition, symptom, infection, or
   pathogen rather than an immune mechanism. Treat `hypersensitivity` as an
   out-of-scope clinical or plant-response gateway in this label-only task.
2. A named receptor, receptor subtype, or receptor-family branch, even when it
   participates in immune signaling. Receptor taxonomy remains outside scope.
3. A generic cellular process such as proliferation or regulated cellular
   demise when immune function is only contextual.
4. A botanical-defense trait, elicitation process, pathogen-driven protein family,
   antimicrobial property, or resistance mechanism.
5. A pathogen-centered strategy for avoiding, escaping, or suppressing host
   defense. Its primary subject is the survival strategy of the pathogen, not
   immune function.
6. A vaccination product, pharmaceutical, therapeutic program, prevention
   program, or other external intervention applied to the immune domain.
7. A generic biomolecule, assay, instrument, laboratory workflow, or diagnostic
   practice without a conventional immune-specific identity.
8. A broad gateway into clinical allergy, infectious disease, general cellular
   science, molecular-level science, pharmacology, plant-disease science, or
   organism taxonomy.

## Decision

Apply the priority rules in this order:

1. INCLUDE established immune products, antigen- or antibody-centered concepts,
   immune-modulating factors, and immune-detection or reaction methods, including
   their meaningful subclasses and modified forms.
2. INCLUDE core immune mechanisms whose label remains inside immunity.
3. EXCLUDE clinical sensitivity branches, named receptors, generic cell
   processes, pathogen-centered avoidance strategies, botanical-defense or
antimicrobial branches, treatments, and generic methods or molecules.

## Traversal Caution

INCLUDE opens the candidate's branch during BFS. Preserve gateways for
immune-specific products, factors, reactions, and established detection
methods. Close clinical sensitivity, receptor-taxonomy, generic cellular,
pathogen-avoidance, botanical-defense, pathogen-driven, therapeutic, and
generic-method branches that would mainly leave the target.

## Confidence

Confidence measures certainty in the selected decision. Use high confidence
when immune identity is conventional for the label or when a listed adjacent
branch clearly dominates. Use lower confidence when the label is broad,
polysemous, or has both immune and non-immune uses.
