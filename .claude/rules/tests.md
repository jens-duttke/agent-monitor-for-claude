---
paths:
  - "tests/**"
---
- Python: `unittest` only; mock time by patching `time.time()`/`datetime`; temp dirs plus `CLAUDE_CONFIG_DIR` for file-based tests; patch settings with `mock.patch.object(module, 'current_settings', return_value=Settings(...))`.
- JS: `node:test` + `node:assert`; `logic.js` required directly; controller tests run the scripts in one shared `vm` context with `render`, the DOM and the bridge replaced by recorders.
- Tests for the other OS's backend decorate every class with the file's `_WINDOWS_ONLY`/`_LINUX_ONLY` skip, so a green run means everything applicable to this OS passed.
- Keep `ImportWithoutPyGObjectTest` a subprocess - a mock would only prove the mock.
- `tests/js/logic.test.js` contains a NUL byte, so git does not normalize its line endings: never rewrite it in Windows text mode, and check `git diff --stat` after editing it.
