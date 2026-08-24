# Translation protocol

Use this protocol after `prepare` and before review export.

## 1. Establish the run

1. Run `doctor`:

   ```bash
   python scripts/srt_translate_helper.py doctor --require-xlsx --work-dir work
   ```

2. Select the style profile, glossary, and approved-memory release explicitly when
   using non-default resources.
3. Run `prepare` once for the source SRT:

   ```bash
   python scripts/srt_translate_helper.py prepare input.zh.srt work/prepared.json
   ```

4. Keep the prepared artifact with every downstream artifact. Treat its source
   SHA-256 and cue fingerprints as the run identity.
5. Restart from `prepare` if the source SRT changes.

Do not repair malformed source structure during translation. Stop and report the
parser or timing error.

## 2. Read the complete source

Read the full prepared cue sequence before writing English. Determine:

- who is speaking and from which perspective;
- the video's purpose and practical factory context;
- tense, entities, numbers, units, product names, and process sequence;
- which contiguous cues form each complete thought;
- which glossary and approved-memory matches are relevant.

Treat an abstained retrieval as useful evidence that no approved match is safe.
Never force a memory example or glossary term into unrelated text.

## 3. Plan thought groups

Partition the cue sequence into contiguous thought groups. Assign every cue
exactly once and preserve source order. Use the smallest group that contains the
complete meaning.

For each group:

1. Reconstruct the complete Chinese thought.
2. Resolve omitted subjects, pronouns, and connectors from nearby cues.
3. Select terminology from confirmed glossary entries.
4. Consider only retrieval candidates whose meaning, speaker, domain, and
   perspective match.
5. Translate the thought as natural spoken English.
6. Redistribute that English across the original cue IDs without merging,
   deleting, duplicating, or reordering cues.

Keep grammar natural across cue boundaries. Do not make each cue an isolated
sentence when the source thought spans several cues.

## 4. Apply the PUREFAITH voice

Write concise, direct, conversational English for a practical factory owner or
factory-tour voiceover. Prefer concrete manufacturing vocabulary to brochure
language.

Preserve:

- first-person perspective;
- process order and causal relationships;
- material numbers, currencies, dates, dimensions, and units;
- approved product and factory terminology;
- intentional internal punctuation.

Remove terminal punctuation from every English cue through the configured
rendering rule. Do not remove punctuation needed inside a cue.

Treat approved wording as contextual evidence, not as a universal substitution.
When a retrieved example conflicts with the present meaning, translate the
present meaning and record no memory use.

## 5. Write a bound translation artifact

Prefer the v2 envelope:

```json
{
  "schema_version": "subtitle.translation.v2",
  "source": {
    "sha256": "<prepared source hash>",
    "cue_fingerprints": {
      "1": "<cue fingerprint>"
    }
  },
  "translations": [
    {
      "index": 1,
      "text": "Natural English",
      "source_fingerprint": "<cue fingerprint>"
    }
  ]
}
```

Include every source index exactly once. Do not emit empty, whitespace-only, or
punctuation-only text. Use the legacy translation list only when compatibility
with an existing run requires it; source-bound v2 artifacts provide stronger
protection.

## 6. Compile and inspect

Run `apply` with source binding enabled and request a report. Then run
`validate`:

```bash
python scripts/srt_translate_helper.py apply input.zh.srt work/translations.json work/draft.en.srt --require-source-binding --report work/apply-report.json
python scripts/srt_translate_helper.py validate input.zh.srt work/draft.en.srt --report work/validation.json
```

Treat these as hard failures:

- source hash or cue fingerprint mismatch;
- missing, extra, duplicate, fractional, or reordered cue IDs;
- changed timestamp text;
- empty or punctuation-only output;
- Chinese residue in an English-only output;
- remaining terminal punctuation under the PUREFAITH profile.

Inspect warnings about reading speed, unusually long cues, number or unit
consistency, and terminology. Resolve meaningful warnings before exporting the
review table.

## 7. Keep deterministic boundaries

Let the helper own parsing, hashing, retrieval ranking, review structure,
rendering, and QA. Use judgment only for thought grouping, translation, cue
redistribution, and semantic review.

Do not edit the active translation memory, glossary, style profile, release
pointer, prepared artifact, source hash, or cue fingerprints during a
translation run.
