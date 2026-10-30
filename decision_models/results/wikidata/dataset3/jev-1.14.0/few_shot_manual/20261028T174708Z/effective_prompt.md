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

## Ground-Truth Keep/Prune Examples

Use these labeled examples only to calibrate the INCLUDE/EXCLUDE boundary for this dataset.
They are not candidates in the current batch; classify only the candidates supplied by the user message.

- Seed: Cypresses
  Candidate: candidate=Apple, Pear and Orange | path=Cypresses --owned by--> Justin K. Thannhauser --owner of--> La Vie --owned by--> Étienne Bignou --owner of--> Apple, Pear and Orange | decision=INCLUDE
- Seed: Cypresses
  Candidate: candidate=Two Diggers among Trees | path=Cypresses --owned by--> Justin K. Thannhauser --owner of--> Lormes:  Goat-Girl Sitting Beside a Stream in a Forest --owned by--> Hugo Nathan --owner of--> Two Diggers among Trees | decision=INCLUDE
- Seed: Cypresses
  Candidate: candidate=Westphalian State Museum of Art and Cultural History | path=Cypresses --owned by--> Justin K. Thannhauser --owner of--> Still Life with Mangoes and a Hibiscus Flower --owned by--> Galerie Fischer --owner of--> Romantic Painting with Ruins --owned by--> Westphalian State Museum of Art and Cultural History | decision=INCLUDE
- Seed: Foundation E.G. Bührle Collection
  Candidate: candidate=WikiProject Provenance | path=Foundation E.G. Bührle Collection --owner of--> Portrait of a man --owned by--> Marquis de Lacaze --on focus list of Wikimedia project--> WikiProject Provenance | decision=INCLUDE
- Seed: Hugo Cassirer
  Candidate: candidate=art thief | path=Hugo Cassirer --owner of--> Avenue at Chantilly --owned by--> Paul Cassirer --owner of--> The Quai Malaquais and the Institute --owned by--> Bruno Lohse --occupation--> art thief | decision=INCLUDE
- Seed: Hugo Cassirer
  Candidate: candidate=art history | path=Hugo Cassirer --owner of--> Avenue at Chantilly --owned by--> Paul Cassirer --owner of--> Tilla Durieux --described by source--> Heilbrunn Timeline of Art History --main subject--> art history | decision=INCLUDE
- Seed: Alfred Flechtheim
  Candidate: candidate=Margit Bornemisza | path=Alfred Flechtheim --owner of--> A Centennial of Independence --owned by--> J. Paul Getty Museum --owner of--> A Woman Preparing Bread and Butter for a Boy --owned by--> Heinrich, Baron Thyssen-Bornemisza de Kászon --spouse--> Margit Bornemisza | decision=INCLUDE
- Seed: Metropolitan Museum of Art
  Candidate: candidate=Walter Feilchenfeldt Jr. | path=Metropolitan Museum of Art --owner of--> Tilla Durieux --owned by--> Walter Feilchenfeldt --child--> Walter Feilchenfeldt Jr. | decision=INCLUDE
- Seed: Gottlieb Friedrich Reber
  Candidate: candidate=Ettore Sestieri | path=Gottlieb Friedrich Reber --significant person--> Alessandro Contini Bonacossi --significant person--> Ettore Sestieri | decision=INCLUDE
- Seed: Hugo Nathan
  Candidate: candidate=Bergier commission | path=Hugo Nathan --owner of--> Lormes:  Goat-Girl Sitting Beside a Stream in a Forest --owned by--> Justin K. Thannhauser --owner of--> Still Life with Mangoes and a Hibiscus Flower --owned by--> Galerie Fischer --investigation of--> Bergier commission | decision=INCLUDE
- Seed: Cleveland Museum of Art
  Candidate: candidate=Reclaimed. Paintings from the Collection of Jacques Goudstikker | path=Cleveland Museum of Art --owner of--> Saint John the Baptist --owned by--> Jacques Goudstikker collection --described by source--> Reclaimed. Paintings from the Collection of Jacques Goudstikker | decision=INCLUDE
- Seed: Metropolitan Museum of Art
  Candidate: candidate=Woman with a Towel | path=Metropolitan Museum of Art --owner of--> Young man in Mayo costume --owned by--> Louisine Havemeyer --owner of--> Woman with a Towel | decision=EXCLUDE
- Seed: Cypresses
  Candidate: candidate=Wolf Dietrich Freiherr von Amstetter-Zwerbach und Grabeneck | path=Cypresses --owned by--> Justin K. Thannhauser --owner of--> Still Life with Mangoes and a Hibiscus Flower --owned by--> Galerie Fischer --owner of--> Romantic Painting with Ruins --owned by--> Wolf Dietrich Freiherr von Amstetter-Zwerbach und Grabeneck | decision=EXCLUDE
- Seed: Alexej von Jawlensky
  Candidate: candidate=Sytin Military Encyclopedia | path=Alexej von Jawlensky --spouse--> Marianne von Werefkin --father--> Vladimir Veryovkin --described by source--> Sytin Military Encyclopedia | decision=EXCLUDE
- Seed: Alfred Flechtheim
  Candidate: candidate=WikiProject PCC Wikidata Pilot/Smithsonian Libraries Dibner Library portraits | path=Alfred Flechtheim --owner of--> A Centennial of Independence --owned by--> Wilhelm Uhde --spouse--> Sonia Delaunay --spouse--> Robert Delaunay --on focus list of Wikimedia project--> WikiProject PCC Wikidata Pilot/Smithsonian Libraries Dibner Library portraits | decision=EXCLUDE
- Seed: Alexej von Jawlensky
  Candidate: candidate=politician | path=Alexej von Jawlensky --spouse--> Marianne von Werefkin --father--> Vladimir Veryovkin --child--> Pyotr Veryovkin --occupation--> politician | decision=EXCLUDE
- Seed: Metropolitan Museum of Art
  Candidate: candidate=Suzanne Manet | path=Metropolitan Museum of Art --owner of--> Young man in Mayo costume --owned by--> Édouard Manet --spouse--> Suzanne Manet | decision=EXCLUDE
- Seed: Alfred Flechtheim
  Candidate: candidate=Theo van Gogh | path=Alfred Flechtheim --owner of--> Two Poplars in the Alpilles near Saint-Rémy --owned by--> Johanna van Gogh-Bonger --child--> Vincent Willem van Gogh --child--> Theo van Gogh | decision=EXCLUDE
- Seed: Hugo Cassirer
  Candidate: candidate=William A. M. Burden I | path=Hugo Cassirer --owner of--> Avenue at Chantilly --owned by--> William A. M. Burden --father--> William A. M. Burden I | decision=INCLUDE
- Seed: Metropolitan Museum of Art
  Candidate: candidate=Wolfgang, Count Palatine of Zweibrücken | path=Metropolitan Museum of Art --owner of--> Portrait of a Man, Said to be Christopher Columbus (born about 1446, died 1506) --owned by--> Philip Louis, Count Palatine of Neuburg --father--> Wolfgang, Count Palatine of Zweibrücken | decision=EXCLUDE
- Seed: Metropolitan Museum of Art
  Candidate: candidate=woman | path=Metropolitan Museum of Art --owner of--> Young man in Mayo costume --owned by--> Louisine Havemeyer --owner of--> The Source --main subject--> woman | decision=EXCLUDE