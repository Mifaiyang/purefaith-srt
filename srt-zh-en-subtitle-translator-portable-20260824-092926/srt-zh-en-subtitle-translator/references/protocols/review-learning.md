# Review, learning, and release protocol

Use this protocol when exporting a review table, consuming human corrections,
learning approved wording, or changing the active memory release.

## Contents

1. Export and protect the review table
2. Audit reviewed data
3. Finalize the SRT
4. Propose learning candidates
5. Promote a memory release
6. Roll back safely
7. Report the outcome

## 1. Export and protect the review table

Export the table from the original source SRT and its bound translation
artifact. Keep these visible columns in order:

```bash
python scripts/srt_translate_helper.py export-review input.zh.srt work/translations.json review.xlsx --require-source-binding --report work/review-export.json
```

1. `序号`
2. `中文`
3. `英文`
4. `英文修改版`
5. `时间线`

Keep the v2 identity columns in the file:

- `__source_sha256`
- `__cue_fingerprint`
- `__schema_version`

Treat the first five columns as review content and the identity columns as
immutable metadata. In XLSX output, hide or protect internal columns when the
writer supports it.

Use `英文修改版` only for an intentional human correction. Leave it blank to
retain `英文`.

## 2. Audit reviewed data

Run `audit-review` with `--source` and `--strict` before finalization or
learning. Bind the audit to the exact source used for export.

```bash
python scripts/srt_translate_helper.py audit-review review.xlsx work/review-audit.json --source input.zh.srt --clean-output work/review-clean.xlsx --strict
```

Reject or flag:

- a source SHA-256 or cue fingerprint mismatch;
- a wrong or stale review table;
- missing, extra, duplicate, reordered, or fractional cue IDs;
- changed timeline or Chinese source cells;
- spreadsheet formulas or formula errors in review fields;
- blank, whitespace-only, or punctuation-only final English;
- Chinese text in an English revision;
- configured placeholders such as `12`, `-`, `/`, `同上`, or
  `same as above`;
- terminal punctuation forbidden by the active profile.

Distinguish a numeric-only placeholder from legitimate English containing
numbers. Accept text such as `We invested RMB 12 million` after all other
checks pass.

Fall back from an invalid `英文修改版` to `英文` only when the original English is
valid and the report records the fallback. In strict mode, block unresolved
identity, structure, formula, empty-output, or language failures.

Read the entire versioned audit report. Do not rely only on the command's
console summary.

## 3. Finalize the SRT

Run `finalize` against the original source, audited review table, and active
style profile. Request a report. Then run `validate` against the final SRT.

```bash
python scripts/srt_translate_helper.py finalize input.zh.srt work/review-clean.xlsx output.final.en.srt --report work/finalize-report.json
python scripts/srt_translate_helper.py validate input.zh.srt output.final.en.srt --report work/final-validation.json
```

Use the original SRT as the only source of cue IDs and timestamps. Never rebuild
structure from spreadsheet cells.

Require:

- an audit pass;
- one final English value per source cue;
- exact numbering and timestamp preservation;
- no Chinese residue;
- no terminal punctuation under the PUREFAITH profile;
- no empty or punctuation-only cue.

Stop if finalization or validation reports any hard issue.

## 4. Propose learning candidates

Generate a candidate artifact from an audited table. Do not mutate the active
translation memory during proposal.

```bash
python scripts/srt_translate_helper.py propose-learn work/review-clean.xlsx --source input.zh.srt --state-root work/memory-v2 --report work/learn-report.json
```

Treat `learn` as a compatibility alias for `propose-learn`.

Read each edited cue with its full thought group, including unchanged
neighbors. Check for duplicated connectors, inconsistent perspective,
unsupported product claims, and reading-speed regressions introduced by
individual cell edits. Keep the original workbook as evidence. If the source
hash cannot be verified, retain the workbook as local development feedback
and use it to improve general guidance. Resume candidate ingestion only
when the matching original SRT is available and strict audit passes.

Include only valid, intentional corrections where the approved final differs
from the draft. Exclude:

- placeholders and formulas;
- unchanged rows;
- unresolved audit issues;
- corrections without source binding;
- holdout or regression-gold documents;
- inferred glossary or style rules.

Record source context, draft, approved final, source/run fingerprints, profile
and resource versions, and approval evidence. Store candidates in an
append-only log or an isolated candidate file.

Treat migrated legacy memory as `legacy_unverified` and ineligible for active
retrieval until explicitly curated.

## 5. Promote a memory release

Promote only after explicit approval.

1. Append an approval event:

   ```bash
   python scripts/migrate_memory_v2.py approve --state-root work/memory-v2 --candidate-id <candidate-id> --decision approved --reviewer <reviewer-id> --rationale "<reason>" --apply
   ```

2. Validate every candidate and its provenance.
3. Detect exact duplicates and conflicting approved variants.
4. Promote the approved candidate:

   ```bash
   python scripts/migrate_memory_v2.py promote --state-root work/memory-v2 --candidate-id <candidate-id> --actor <actor-id> --style-version 2.0.0 --termbase-version 1.0.0 --apply
   ```

5. Create and verify a new immutable release directory and manifest.
6. Run the regression suite.
7. Update the active-release pointer atomically only after every gate passes.

Never rewrite an old release. Never promote directly into the legacy
`translation-memory.json`.

If validation, writing, or regression fails, leave the prior pointer and
release files usable. Report any quarantined temporary artifact.

## 6. Roll back safely

List available releases and their manifests before rollback. Select the exact
known-good release ID. Verify its checksums and compatibility, then update the
active pointer atomically.

```bash
python scripts/migrate_memory_v2.py releases --state-root work/memory-v2
python scripts/migrate_memory_v2.py rollback --state-root work/memory-v2 --release-id <release-id> --actor <actor-id> --reason "<reason>" --apply
```

Keep later releases for diagnosis. Do not delete them as part of rollback.
Re-run `doctor` and a smoke validation after changing the pointer.

Run migration into a new release location. Require a second identical migration
to produce the same record IDs and logical result.

```bash
python scripts/migrate_memory_v2.py migrate --state-root work/memory-v2
python scripts/migrate_memory_v2.py migrate --state-root work/memory-v2 --apply --activate
```

## 7. Report the outcome

Report:

- source, output, audit, and validation paths;
- cue count and exact structure status;
- source-binding status;
- valid revisions, rejected cells, and fallbacks;
- revision rate;
- candidate count and exclusions;
- previous and new active release IDs when promotion or rollback occurs;
- regression and atomic-update status.

Do not paste the full SRT, review table, memory records, or sensitive subtitle
content unless requested.
