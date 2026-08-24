---
name: srt-zh-en-subtitle-translator
description: Translate, review, validate, and safely learn from Chinese-to-English SRT subtitles while preserving source cue IDs, timestamps, encoding, BOM, newlines, and boundaries. Use for PUREFAITH/纯信科技 factory and corporate short videos, verified English SRT plus bilingual XLSX delivery, context-aware complete-thought translation, reviewed-table finalization, subtitle QA, approved terminology retrieval, learning candidates, translation-memory migration, governed promotion, and rollback.
---

# Chinese SRT to Business-Approved English

Preserve the source SRT exactly outside cue text. Produce concise, natural
spoken English that needs minimal business review.

## Use the bundled resources

- Resolve a Python 3.10+ executable before running commands. In Codex Desktop,
  call `load_workspace_dependencies` and use the returned Python path when
  `python` is not on `PATH`. Every command below uses `python` as that resolved
  executable.
- Run `scripts/srt_translate_helper.py` for preparation, compilation, review,
  finalization, candidate proposal, and validation.
- Run `scripts/migrate_memory_v2.py` for memory migration, approval, promotion,
  status, and rollback.
- Read `references/protocols/translation.md` completely before translating a
  new source.
- Read `references/protocols/review-learning.md` completely before consuming a
  reviewed table or changing governed memory.
- Use `references/style-profile.json`, `references/glossary.json`, and the
  active approved memory as evidence. Do not edit them during a run.

## Choose the workflow

- For an ordinary request to translate one Chinese SRT and return an English
  SRT plus a Chinese-English comparison workbook, read
  `references/protocols/simple-delivery.md` completely and use the `deliver`
  command. This is the default for simple delegated jobs.
- For translation work that needs manual control over intermediate artifacts,
  follow the preparation, application, review export, and validation sections
  below.
- For a returned review workbook or any memory update, read
  `references/protocols/review-learning.md` completely. Do not learn or promote
  anything during a simple delivery task.

## Enforce the non-negotiable rules

- Preserve cue IDs, raw timestamps, order, cue count, timing settings, encoding,
  BOM policy, newline style, and block layout.
- Read the complete cue sequence before translating.
- Reconstruct each complete thought across contiguous cues.
- Translate the complete thought naturally, then redistribute English across
  the same cue IDs.
- Preserve speaker perspective, facts, process order, material numbers, units,
  currencies, names, and terminology.
- Evaluate number preservation across the complete thought group; allow a
  number to move between cues inside that group.
- Prefer confirmed glossary terms and relevant approved examples.
- Abstain from a retrieved example when meaning, speaker, domain, or perspective
  differs.
- Keep PUREFAITH English direct, conversational, and concrete.
- Strip terminal punctuation from every rendered PUREFAITH cue while preserving
  necessary internal punctuation.
- Reject empty, whitespace-only, punctuation-only, formula, Chinese, and
  placeholder revisions.
- Treat `英文修改版` as final only when its value passes audit; otherwise use a
  valid `英文` fallback and report it.

## Start with a doctor check

Run the doctor in the intended workspace. Require XLSX support when the workflow
uses Excel:

```bash
python scripts/srt_translate_helper.py doctor --require-xlsx --work-dir work
```

Stop on a failed runtime, resource, dependency, or writable-directory check.

## Prepare and translate

Create the source-bound context pack:

```bash
python scripts/srt_translate_helper.py prepare input.zh.srt work/prepared.json
```

Read all of `prepared.json`. Use its:

- source hash and cue fingerprints;
- full cue sequence and adjacent context;
- glossary and preferred-phrase matches;
- retrieval status, explanations, conflicts, and abstentions;
- translation principles and mandatory process.

Partition the source into complete-thought groups. Translate each group as a
whole and redistribute it without changing cue membership.

Write a source-bound v2 translation artifact:

```json
{
  "schema_version": "subtitle.translation.v2",
  "source": {
    "sha256": "<prepared source hash>",
    "cue_fingerprints": {
      "1": "<prepared cue fingerprint>"
    }
  },
  "translations": [
    {
      "index": 1,
      "source_fingerprint": "<prepared cue fingerprint>",
      "text": "Natural English"
    }
  ]
}
```

Include every source cue exactly once.

Compile and validate source binding:

```bash
python scripts/srt_translate_helper.py apply input.zh.srt work/translations.json work/draft.en.srt --require-source-binding --report work/apply-report.json
python scripts/srt_translate_helper.py validate input.zh.srt work/draft.en.srt --report work/draft-validation.json
```

Read the reports. Stop if any source hash, cue fingerprint, structure, language,
or final-text gate fails.

For a simple delegated job, replace the separate apply, export, audit, and
validate commands with the mandatory one-command delivery gate:

```bash
python scripts/srt_translate_helper.py deliver input.zh.srt work/translations.json output
```

Do not handcraft or substitute the comparison workbook. A successful command
must create `final.en.srt`, `bilingual-review.xlsx`, and the JSON reports in the
output directory.

## Export, audit, and finalize review

Export a source-bound review table:

```bash
python scripts/srt_translate_helper.py export-review input.zh.srt work/translations.json review.xlsx --require-source-binding --report work/review-export.json
```

Ask the reviewer to edit only `英文修改版`. Preserve the visible source columns
and hidden v2 identity columns.

Audit the returned table against the exact source:

```bash
python scripts/srt_translate_helper.py audit-review review.xlsx work/review-audit.json --source input.zh.srt --clean-output work/review-clean.xlsx --strict
```

Read the complete audit report. Resolve blocking issues. Do not finalize a
wrong, stale, structurally edited, or unbound table.

Finalize only from the audited source-bound table:

```bash
python scripts/srt_translate_helper.py finalize input.zh.srt work/review-clean.xlsx output.final.en.srt --report work/finalize-report.json
python scripts/srt_translate_helper.py validate input.zh.srt output.final.en.srt --report work/final-validation.json
```

Use the original SRT, not spreadsheet cells, as the structure authority.

## Propose learning without mutating memory

Append pending candidates only after strict audit:

```bash
python scripts/srt_translate_helper.py propose-learn work/review-clean.xlsx --source input.zh.srt --state-root work/memory-v2 --report work/learn-report.json
```

Treat `learn` as a compatibility alias for `propose-learn`. Confirm that the
report says the active release was not mutated.

Exclude unchanged rows, invalid revisions, unresolved audit rows, holdout or
regression-gold documents, and unbound corrections. Keep complete-thought
context and normal sentence punctuation in canonical memory; keep rendered cue
variants punctuation-free.

## Approve and promote explicitly

Do not approve or promote without explicit authorization. Append an approval
event:

```bash
python scripts/migrate_memory_v2.py approve --state-root work/memory-v2 --candidate-id <candidate-id> --decision approved --reviewer <reviewer-id> --rationale "<reason>" --apply
```

Promote only an approved candidate:

```bash
python scripts/migrate_memory_v2.py promote --state-root work/memory-v2 --candidate-id <candidate-id> --actor <actor-id> --style-version 2.0.0 --termbase-version 1.0.0 --apply
```

Verify the new immutable release and active pointer:

```bash
python scripts/migrate_memory_v2.py status --state-root work/memory-v2
python scripts/migrate_memory_v2.py releases --state-root work/memory-v2
```

Never write learned examples directly into
`references/translation-memory.json`.

## Migrate and roll back safely

Plan legacy migration without writes:

```bash
python scripts/migrate_memory_v2.py migrate --state-root work/memory-v2
```

Inspect the result. Apply and activate only after approval:

```bash
python scripts/migrate_memory_v2.py migrate --state-root work/memory-v2 --apply --activate
```

Roll back by repointing to an existing verified release:

```bash
python scripts/migrate_memory_v2.py rollback --state-root work/memory-v2 --release-id <release-id> --actor <actor-id> --reason "<reason>" --apply
```

Keep later releases and candidate logs. Do not delete data during rollback.

## Maintain legacy compatibility

Accept legacy translation lists and legacy review tables for existing jobs.
Prefer source-bound v2 artifacts for new jobs. Parse real review aliases,
including `中文`/`英文` and `中文字幕`/`英文字幕`, after optional title and instruction
rows.

Never allow compatibility mode to bypass strict audit before finalization or
learning.

## Report the output

Report:

- source, prepared, translation, review, audit, final SRT, and validation paths;
- cue count and exact structure-preservation status;
- source-binding status;
- retrieval matches, abstentions, and conflicts;
- valid revisions, rejected cells, and fallbacks;
- review revision rate;
- candidate IDs and confirmation that proposal did not mutate a release;
- previous and active release IDs after promotion or rollback;
- report paths and any remaining warnings.

Do not paste the complete SRT, review table, candidate log, or memory release
unless requested.
