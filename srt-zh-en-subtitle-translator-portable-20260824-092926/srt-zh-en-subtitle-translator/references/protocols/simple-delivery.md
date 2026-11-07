# Simple delivery protocol

Use this protocol when the user gives one Chinese SRT and wants a routine
English translation with a bilingual review workbook. Do not use learning,
approval, promotion, migration, or rollback commands in this mode.

## Required inputs and outputs

Input:

- the original Chinese SRT;
- a source-bound `subtitle.translation.v2` JSON created after `prepare`.

Successful delivery must contain all of these files:

- `final.en.srt` — English-only SRT with the original cue IDs and timestamps;
- `bilingual-review.xlsx` — comparison workbook whose first five visible
  columns are `序号`, `中文`, `英文`, `英文修改版`, `时间线`;
- `delivery-report.json` — summary proving all delivery checks passed;
- `apply-report.json`, `review-export.json`, `review-audit.json`, and
  `final-validation.json` — machine-readable evidence.

If any required file is missing or any report has `passed: false`, the task is
not complete. Never replace the workbook with a prose table, Markdown, an
English-only spreadsheet, or a manually assembled workbook.

## Procedure

1. From the skill directory, run the environment check:

   ```bash
   python scripts/srt_translate_helper.py doctor --require-xlsx --work-dir work
   ```

2. Prepare the exact source and read all of the resulting context pack:

   ```bash
   python scripts/srt_translate_helper.py prepare input.zh.srt work/prepared.json
   ```

3. Translate complete thoughts, then redistribute the English across the same
   cue IDs. Write a source-bound `subtitle.translation.v2` JSON using the source
   hash and cue fingerprints from `prepared.json`. Include each cue exactly
   once.

4. Create and verify the entire delivery in one command:

   ```bash
   python scripts/srt_translate_helper.py deliver input.zh.srt work/translations.json output
   ```

5. Read `output/delivery-report.json`. Deliver files only when `passed` is
   `true`, `source_bound` is `true`, every value under `checks` is `true`, and
   the cue count matches the source. Inspect reading-speed warnings in
   `final-validation.json`. Read the English in cue order as described in
   `translation.md`; combine this with the source-to-draft check when required.
   Use one `prepare` and one `deliver` on the happy path. Avoid separate
   apply/export/audit commands before `deliver`. When a group needs repair,
   update only that group and rerun `deliver` to keep the SRT and XLSX in sync.

## Translation contract

- Read the complete source before translating.
- Recover meaning across adjacent cues instead of translating cues in
  isolation.
- Keep English short, direct, conversational, and concrete.
- Preserve speaker perspective, facts, numbers, units, names, and process order.
- Preserve every cue ID, raw timestamp, order, and cue boundary.
- Remove terminal punctuation from every English cue; preserve necessary
  internal punctuation.
- Do not leave Chinese characters, blank translations, placeholders, formulas,
  or punctuation-only entries in the English output.

## Stop conditions

Stop and report the exact error instead of improvising when:

- `doctor` fails;
- the source SRT cannot be parsed;
- the translation JSON is unbound, incomplete, duplicated, or stale;
- XLSX support is unavailable;
- `deliver` returns a nonzero exit code;
- any required report fails.
