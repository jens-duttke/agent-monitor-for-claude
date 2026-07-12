<!--
Thanks for contributing! This tool reads local Claude Code data, so privacy and
a read-only, offline design are non-negotiable. Please confirm the checklist below.
-->

## What does this PR do?

<!-- A short description of the change and why it is needed. -->

## Related issue / discussion

<!-- e.g. Closes #NN, or a discussion link. Remove if none. -->

## Checklist

- [ ] The change does exactly what it claims, and nothing more.
- [ ] No conversation content is read (only control metadata: entry type, `stop_reason`, tool IDs/name, timestamps).
- [ ] No network access, no credential access, no file/registry writes.
- [ ] Parsing of Claude Code internals degrades safely on missing/renamed fields.
- [ ] Tests added/updated for the change; `python -m unittest discover -s tests` passes.
- [ ] `README.md` / `docs/` updated if user-facing behavior or settings changed.
- [ ] No new dependencies (or a clear justification is included).
