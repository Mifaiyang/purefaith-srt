# Corpus collection and direct-delivery qualification

Use this protocol for corpus collection, blind evaluation, or requests to
deliver without business review. Normal delivery still follows the existing
translation and simple-delivery protocols.

## Build a private corpus

- Collect the original Chinese SRT, the pre-review English draft if available,
  the business-reviewed workbook, and the approved English SRT. Prefer a
  recorded approval or a verified published version over a filename marked
  final. Record approval evidence or mark it pending.
- Maintain a local inventory with video ID, topic, source provenance, SHA-256
  hashes, cue count, evidence of approval, split, validation result, and notes.
  Keep original bytes. Store raw files and manifests outside shared repos.
- Pair on the whole video. Compare cue IDs and timecodes against the original
  source before aligning bilingual text. Quarantine unmatched pairs and flag
  reconstructed sources and legacy formatting. Keep known structural failures
  as negative fixtures. Preserve semantic reference text separately from
  normalized subtitle rendering.
- Group alternate edits, recuts, and near-duplicates into the same split.
  Keep development and blind evaluation sets separate by video family.
  Check existing memory and prior experiments for exposure before calling a
  video blind. Once a result has informed changes, use a fresh blind batch
  for release qualification.
- For development examples, reconstruct complete thoughts before capturing
  approved wording. Tag corrections by terminology, fact/number/unit,
  meaning/omission, speaker perspective, and readability/style. Use the
  existing strict audit and candidate approval workflow before promotion.
  Legacy unbound material can support offline diagnosis.

## Compare a baseline and a candidate

Freeze the source hashes, model version, skill revision, glossary and style
versions, and memory release. Generate both versions from Chinese only.
Withhold approved English, reviewer changes, and any overlapping examples
until both outputs are locked. Compare by whole thought and by whole video,
using approved English as evidence. Accept valid alternative phrasing.

Record structure pass rate, material errors, videos requiring any substantive
edit, and the share eligible for direct delivery. A material error includes a
changed or omitted fact, unsupported claim, wrong unit or quantity, wrong
entity or process term, or a shifted speaker or causal relationship. Track
optional style edits separately. Track groups requiring resegmentation or
compression, remaining reading-speed warnings, and end-to-end elapsed time
under comparable conditions. Count deferred videos in the total, including
cases where all delivered files passed. Report results by topic and sample size.

## Qualify direct delivery by topic

Start in shadow mode: mark whether the draft would have been eligible, then
compare with trusted review results. A starting pilot rule is two fresh blind
batches with at least 10 independent videos total per topic, all passing
structural checks and zero material errors among would-be direct deliveries.
At least 90% of all pilot videos must be usable without substantive edits.
Treat this threshold as a pilot policy and report observed counts. Record
qualification with the exact configuration and requalify after material
changes or a serious escaped error.

For each new video, run a separate source-to-draft comparison after generation.
Read every complete thought, checking speaker, claim, process sequence, names,
numbers, units, negation, and whether English can be read in its assigned cues.
Use the cue-order readback and duration checks in `translation.md` in the same
review pass. Unresolved readability issues block direct-ready status. Apply
deterministic source-binding and delivery checks after any edit. Mark
`direct_ready` only when the topic and configuration have qualification
evidence and all semantic issues are resolved. Otherwise mark `review_required`
and provide the cue IDs and exact questions for the reviewer. Unknown product
names, conflicting technical terms, and ambiguous source speech require
evidence before direct delivery. Record the status, topic, qualification
reference, and unresolved items in the handoff. The helper's delivery report
covers file integrity and does not assign direct-delivery status.
