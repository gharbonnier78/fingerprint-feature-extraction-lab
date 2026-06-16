# ISO/IEC 19794-2 and BioCTS quarantine note

The legacy notebook contains an experimental binary minutiae writer and a parser that reads the same private layout. This branch is preserved for forensic history but is **not** included in the canonical run.

Reasons:

1. The notebook moves between ISO/IEC 19794-2:2005 and 2011 assumptions.
2. Header and record fields were inferred ad hoc rather than mapped clause by clause.
3. Field widths and semantics were modified when the generated candidate count no longer fit an assumed field.
4. Angle/type/quality conventions are inconsistent across cells.
5. A writer and parser based on the same assumptions can round-trip a structurally wrong format.
6. BioCTS issues were reported, and no independent conformance pass is included in the uploaded evidence.

The cleaned pipeline emits CSV/JSON research data only. A future standards branch must identify the exact edition/profile, use the licensed specification, add independent decoding and golden vectors, and pass the corresponding NIST BioCTS suite before any conformance label is used.
