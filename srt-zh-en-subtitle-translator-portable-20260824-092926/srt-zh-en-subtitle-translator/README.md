# SRT Chinese-English subtitle translator

This package is an executable workflow, not a prompt-only reference. An AI
agent must read `SKILL.md` and run the bundled Python commands rather than
handcrafting the subtitle or spreadsheet.

For a routine Chinese SRT translation, use the simple-delivery workflow in
`references/protocols/simple-delivery.md`. Its required result is:

1. `final.en.srt`
2. `bilingual-review.xlsx` with `序号｜中文｜英文｜英文修改版｜时间线`
3. JSON validation reports showing every check passed

The final delivery command is:

```bash
python scripts/srt_translate_helper.py deliver input.zh.srt work/translations.json output
```

The command requires a source-bound translation JSON created from `prepare`.
If it fails, the job is incomplete; do not substitute a manually created
workbook.

Python 3.10+ and `openpyxl` are required for XLSX output. Run this first:

```bash
python scripts/srt_translate_helper.py doctor --require-xlsx --work-dir work
```

See `COPY_TO_AGENT.txt` for a ready-to-send task instruction.
