# Artwork Subgraph Relevance Classifier

## Task

You are curating a focused Wikidata subgraph for art, artworks, ownership, provenance, art-market actors, collecting institutions, and looted-art context. Given a seed artwork, person, museum, collection, dealer, collector, or institution, decide whether a candidate identified by its label belongs in this thematic subgraph.

## Input Format

Each batch item is numbered only for response matching:

`N. Seed: <seed label>`

`Candidate: candidate=<candidate label>`

Few-shot examples, when supplied, use the same structure with `decision=INCLUDE` or `decision=EXCLUDE`. Use the supplied seed and candidate labels as evidence. Do not expect QIDs, row IDs, or separate depth fields. Do not invent unprovided connections between the seed and candidate.

## Core Principle

This artwork/provenance extraction setting is strict about topical drift. Do not include a node merely because it has a loose association with art. Include specific art/provenance entities that remain meaningfully connected to the seed's artwork, collection, ownership, art-market, institutional, or looted-art context. Exclude generic reference, occupation, administrative, and side-branch entities that cause topical drift.

Ownership and collecting relevance need extra care. An owner, owned artwork, or collection asset can be relevant when it belongs to a coherent provenance, art-market, collecting, restitution, or looted-art neighborhood. Exclude arbitrary side holdings of a large museum, broad institution, or prolific owner when the supplied labels give no seed-specific provenance, collection, or art-market connection.

## Include

Include candidates that are:

1. Specific artworks, collection objects, collections, museums, galleries, provenance actors, owners, collecting actors, art-market actors, or art-market organizations meaningfully connected to the seed's ownership or provenance context.
2. Plausible artworks, owners, collections, institutions, or art-market actors in the same art/provenance neighborhood as the seed.
3. Specific provenance or looted-art resources, projects, investigations, catalogs, or sources when the supplied labels identify provenance, art looting, restitution, cultural heritage, a named connected artwork/person, or a relevant museum/collection catalog.
4. Named artworks associated with art-market or collecting actors/institutions when the labels support a specific provenance, collecting, or art-market connection to the seed.
5. Occupation labels only when the occupation itself denotes a role directly used in art-market, collection, provenance, restitution, or looted-art activity. Do not include a generic occupation merely because a person is otherwise connected.
6. Named people when the supplied labels support their relevance to the seed's art-market, ownership, family, or provenance context.
7. Specific residences, collections, or institutional assets of major collecting or looting actors when relevant to the seed's ownership/provenance context.
8. Named institutions, galleries, collections, archives, or museums that plausibly participate in ownership, collecting, display, or provenance history.

## Exclude

Exclude candidates that are:

1. Generic biographical, political, professional, creative, legal, military, academic, or business occupation labels, unless the role is specifically part of the art-market, provenance, restitution, or looted-art context.
2. Generic encyclopedias, authority files, catalog aggregators, library projects, database projects, or generic sources that do not add specific provenance or artwork context.
3. Administrative Wikimedia projects or focus lists, except provenance- or looted-art-focused projects.
4. Distant family, royal, political, or biographical entities outside the art/provenance task.
5. Generic occupation labels associated with an otherwise relevant person: including a person does not make every aspect of that person's biography relevant.
6. Arbitrary side holdings of an owner, museum, or institution when the labels give no art-market, provenance, collection, restitution, looted-art, or seed-neighborhood reason to keep them.
7. Generic subject labels, broad historical categories, or general places when they are not specifically useful for the seed's art/provenance context.
8. Reference, source, or project entities that do not add specific art, ownership, or provenance relevance.

## Decision Process

1. Read the supplied seed and candidate labels.
2. Ask whether the candidate belongs to the seed's art, collection, ownership, provenance, art-market, or looted-art context.
3. Prefer INCLUDE for specific named artworks with a meaningful ownership/provenance connection, art-market actors, collecting institutions, and provenance-specific resources.
4. Prefer EXCLUDE for broad occupations, generic sources, administrative projects, distant genealogy, and generic biographical details of otherwise relevant people.
5. When unsure about an ownership or collecting candidate, ask whether the supplied labels support its relevance to the seed's provenance, collecting, or art-market neighborhood.
6. When unsure, choose EXCLUDE if the only evidence is a generic source, broad occupation, distant family association, or unrelated holding of an owner or institution.
