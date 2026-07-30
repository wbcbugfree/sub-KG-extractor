# Artwork Subgraph Relevance Classifier

## Task

You are curating a focused Wikidata subgraph for art, artworks, ownership, provenance, art-market actors, collecting institutions, and looted-art context. Given a seed artwork, person, museum, collection, dealer, collector, or institution, decide whether a candidate reached by a relation path belongs in this thematic subgraph.

## Input Format

Each batch item is numbered only for response matching:

`N. Seed: <seed label>`

`Candidate: candidate=<candidate label> | path=<seed --predicate--> ... --> <candidate>`

Few-shot examples, when supplied, use the same structure with `decision=INCLUDE` or `decision=EXCLUDE`. Treat the path as the main evidence. Do not expect QIDs, row IDs, or separate depth fields.

Paths may contain intermediate bridge nodes that are shown only to explain how the candidate is reached. Classify only the final `candidate=<candidate label>` node; do not separately decide whether intermediate bridge nodes should be included.

## Core Principle

This artwork/provenance extraction setting is strict about topical drift. Do not include a node merely because it is reachable through an allowed Wikidata property. Include specific art/provenance entities that remain meaningfully connected to the seed's artwork, collection, ownership, art-market, institutional, or looted-art context. Exclude generic reference, occupation, administrative, and side-branch entities that cause topical drift.

Ownership paths need extra care. An `owner of` / `owned by` chain can be relevant when it stays inside a coherent provenance, art-market, collecting, restitution, or looted-art neighborhood. Do not treat path length alone as evidence for exclusion. Exclude fan-out through a large museum, broad institution, or prolific owner only when the final candidate looks like an arbitrary side holding with no seed-specific provenance, collection, or art-market connection.

## Include

Include candidates that are:

1. Specific artworks, collection objects, collections, museums, galleries, provenance actors, owners, collecting actors, art-market actors, or art-market organizations on a coherent ownership or provenance path.
2. Concrete ownership continuations through `owner of` / `owned by` when the candidate is a plausible artwork, owner, collection, institution, or art-market actor in the same art/provenance neighborhood.
3. Specific provenance or looted-art resources, projects, investigations, catalogs, or sources when the path clearly says provenance, art looting, restitution, cultural heritage, a named connected artwork/person, or a relevant museum/collection catalog.
4. Named artworks reached by repeated `owner of` / `owned by` links through art-market or collecting actors/institutions when the path still preserves a specific provenance, collecting, or art-market connection to the seed, even when the path is several hops long.
5. Candidates reached through `occupation` only when the occupation itself denotes a role directly used in art-market, collection, provenance, restitution, or looted-art activity. Require path context; do not include an occupation merely because the person is otherwise connected.
6. Named people reached by relation labels `significant person` (`P3342`), `spouse` (`P26`), `father` (`P22`), or `child` (`P40`) when the person remains close to a connected art-market, ownership, family, or provenance actor.
7. Specific residences, collections, or institutional assets of major collecting or looting actors when the path remains part of the ownership/provenance story.
8. Named institutions, galleries, collections, archives, or museums that plausibly participate in ownership, collecting, display, or provenance history.

## Exclude

Exclude candidates that are:

1. Candidates reached through `occupation` when the candidate label is a generic biographical, political, professional, creative, legal, military, academic, or business role, unless the path makes that role specifically part of the art-market, provenance, restitution, or looted-art context.
2. Generic encyclopedias, authority files, catalog aggregators, library projects, database projects, or generic sources that do not add specific provenance or artwork context.
3. Administrative Wikimedia projects or focus lists, except provenance- or looted-art-focused projects.
4. Distant family, royal, political, or biographical chains where the path has left the art/provenance task.
5. Occupation labels reached from a connected significant person when the occupation is not itself an art-market/provenance role. Include the person if relevant, but do not include every `occupation` candidate of that person.
6. Side holdings reached only because an owner, museum, or institution owns many unrelated works, when the label/path gives no art-market, provenance, collection, restitution, looted-art, or seed-neighborhood reason to keep it.
7. Generic subject labels, broad historical categories, or general places when they are not specifically useful for the seed's art/provenance context.
8. Candidates whose path is mostly a reference/source/project expansion rather than an art, ownership, or provenance relationship.

## Decision Process

1. Read the entire path, not only the candidate label.
2. Use intermediate bridge nodes as context for the relationship chain, but make the INCLUDE/EXCLUDE decision only for the final candidate.
3. Ask whether the path still tells an art, collection, ownership, provenance, art-market, or looted-art story about the seed.
4. Prefer INCLUDE for specific named artworks on coherent ownership chains, art-market actors, collecting institutions, and provenance-specific resources.
5. Prefer EXCLUDE for broad occupations, generic sources, administrative projects, distant genealogy, and occupation expansions from otherwise relevant people.
6. When unsure about an ownership chain, ask whether the final candidate is still part of the seed's provenance, collecting, or art-market neighborhood. Do not use depth alone as the deciding factor.
7. When unsure, choose EXCLUDE if the only evidence is a generic source, broad occupation, distant family chain, or unrelated owner/institution fan-out.
