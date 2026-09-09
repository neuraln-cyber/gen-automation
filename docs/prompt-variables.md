# Prompt variables

In **New Set** or **Experiment Lab**, use **Prompt variables** beneath **Set
presets**. Add a name and its prompt text, then use `*name*` in any batch or
shared prompt. **Insert** adds the token to the last prompt you clicked.

For example:

| Name | Value |
| --- | --- |
| character | traveler, silver hair |
| outfit | blue jacket, black trousers |
| setting | rooftop, blue sky, city skyline |
| background | `__backgrounds__` |

A batch can contain `*character*, *outfit*, *setting*, standing`. Change a
variable's value once and every batch referencing it uses the new text when
queued. Batch text remains editable and is not rewritten with the values.

- Save the preset to keep both definitions and batch templates. Existing
  presets still load; presets without variables clear previous definitions.
- Variables also follow drafts and preset JSON export/import. Like existing
  presets, they are stored on this device, not shared automatically across devices.
- Use existing TXT wildcard libraries as `__library_name__` in values; import
  the file in **Wildcards** first. Wildcards still draw separately per image.
- Names are case-sensitive and may contain letters, numbers, spaces,
  underscores, or hyphens, starting with a letter. Enter the name without stars.
- Empty values are allowed for optional text. Other variables may be referenced
  in a value; undefined names and circular references are rejected before queueing.
- Variables work in positive, negative, detailer, and multi-character prompt
  fields, including batch overrides. They do not change LoRA/subject selections
  or other non-prompt settings.

Variable text expands before wildcard versions are frozen. Later edits cannot
change an already queued set. Existing wildcard reproducibility and generation
limits remain unchanged; no new worker, storage service, or paid resource is needed.

Limits: 50 names, 64 characters per name, 20,000 characters per value or
expanded prompt, 100,000 value characters total, nesting depth 8, and 256
substitutions per prompt. No template expressions or filesystem paths execute.
