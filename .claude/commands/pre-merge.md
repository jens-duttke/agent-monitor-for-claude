---
allowed-tools: Read, Edit, Bash, Grep, Glob, WebFetch, WebSearch, Agent
description: Maintainer pre-merge gate - audit a PR (or staged changes), output review comments, then CHANGELOG entry
argument-hint: [PR-URL or #NN]
disable-model-invocation: true
---

Maintainer-only. This is the **last line of defense** before code enters `main`.

Argument: `$ARGUMENTS`

Argument handling:
- If `$ARGUMENTS` contains a PR URL or `#NN` reference -> audit that pull request via `gh`.
- If `$ARGUMENTS` is empty -> audit the locally staged changes (your own work).

Mindset: **adversarial**. Assume the diff might contain a mistake, a misunderstanding, or a deliberate backdoor. Your job is to disprove that assumption before merging. Do not skip steps. Do not accept claims at face value. Verify everything against primary sources.

This app reads local Claude Code data - a user's session registry and transcripts across every project. Users trust the binary to stay fully local and to never touch conversation content. A single malicious or careless merge breaks that trust permanently.

---

## Step 0: Identify and load the change set

### If a PR was provided in `$ARGUMENTS`

1. Extract the PR number from the URL or `#NN` reference.
2. Run `gh pr view <PR> --json number,title,body,author,headRefName,baseRefName,url,state` to load metadata.
3. Run `gh pr diff <PR>` to load the actual diff.
4. Run `gh pr checks <PR>` to verify CI status.
5. Read the linked Issue or Discussion (if any) via `gh issue view` or `gh api`.
6. Note the PR author's GitHub handle - you will need it for the CHANGELOG credit later.

### If no PR was provided

1. Run `git diff --staged`. If empty, also check `git status` and ask the user what to audit.
2. Treat the user's prior conversation context as the "PR description".

Record the **stated intent** of the change in one or two sentences. You will check every line against this intent.

## Step 1: Verify the diff against the stated intent

Read **every** changed line. For each non-trivial change, walk through the code path manually:
- Given realistic input, what does the function actually do?
- Does the implementation match what the PR claims?
- Are there side effects beyond what the PR describes?

Critical questions for every change - answer each one:
- Is this change actually necessary for the stated goal? Could the goal be achieved with less surface area?
- What edge cases could break this? (boundary values, malformed input, race conditions, missing/renamed/null on-disk fields)
- What if the function is invoked in an unexpected order or state?
- Does anything in the diff feel "extra" - touching files or code with no obvious connection to the stated goal? **Treat unexplained scope creep as a red flag.**

## Step 2: Intensive security & privacy audit

Treat the code as untrusted. Actively try to find a way to abuse it.

### Reasoning principle (applies to every subsection below)

Do **not** treat enumerated function or library names as a checklist - an attacker can pick any name not on the list, wrap a known function in a self-written helper, compose lower-level primitives, or route a call through a transitive dependency. Reason about **capabilities** (what the code is able to cause to happen), not about names.

For every new or changed function in the diff, walk into the calls it makes. Stop the recursion only when reaching (a) pre-existing project code already audited, (b) the Python standard library (note any capability the leaf exercises), or (c) an existing locked dependency (note any capability the leaf exercises). If the recursion lands on a *new* function defined in the same diff, recurse further. The example names in each subsection are non-exhaustive hints, not a complete list.

The dependency gate below pins the set of leaves to a closed universe. Always run it first.

### Dependencies and build surface (run this FIRST as a gate)

New dependencies expand the set of available primitives (network, code execution, filesystem) in ways the audit cannot fully predict, so they must be ruled out or scrutinized **before** anything else.

- **Default stance: no new dependencies.** The sanctioned runtime set is `pywebview` and `psutil` (`pyinstaller` is build-only). Any addition to `requirements.txt`, any new third-party top-level `import`, or any new entry in the spec's `hiddenimports`/`datas` is a finding by default. Demand a justification tied directly to the stated PR goal. CLAUDE.md mandates minimal, well-known dependencies.
- For each new dependency that survives that bar: `WebFetch` its PyPI page (maintainer identity, release history, download counts, last update), inspect the source repository, recurse into transitive dependencies, and look for typosquats/lookalikes.
- Changes to `agent_monitor_for_claude.spec`, `version_info.py`, `.github/workflows/*`, or any build script are **high-risk surfaces in their own right** - a malicious PR can hide a payload in build configuration that never appears in the source diff. Verify every line is justified.
- The diff must not contain vendored third-party code. Any such inclusion is a finding.

### Privacy boundary (the defining invariant)

**Capability check:** can any code path read, return, store, log, or render conversation content?

- Only control metadata may leave `transcript.py`: entry `type`, `stop_reason`, `tool_use`/`tool_result` IDs, the tool **name**, and timestamps.
- Trace every field accessed on a parsed transcript entry. Message `text`, `thinking` blocks, tool `input`, and tool-result `content` must never be read - not into a variable, not into a log, not into the snapshot, not into an error message.
- Check the snapshot builder and the JS bridge: does anything content-bearing reach `build_snapshot()`'s output or `_MonitorApi`?

### Network destinations

**Capability check:** does any leaf in the call graph send bytes to a network destination?

- The app must open **no** sockets at all. Any leaf that performs socket I/O, opens a connection, navigates the webview to a remote URL, or executes an external tool capable of fetching/sending (`curl`, `wget`, `Invoke-WebRequest`, `gh`, `git fetch/push`, `pip`, `npm`) is a finding.
- Find URL-like literals structurally: `grep -nE "://|@[A-Za-z0-9.-]+|\b[0-9]{1,3}(\.[0-9]{1,3}){3}\b"` over the diff. Any hit is a finding to justify or remove.
- pywebview may only load the bundled local UI files - never a remote URL.

### Credentials

**Capability check:** does any leaf read a credential or auth token?

- Nothing may read authentication tokens or other secrets. The app needs none and must never touch them.

### Code execution

**Capability check:** does any leaf compile, evaluate, deserialize, dynamically import, or hand control to externally-supplied bytes? Does any leaf spawn an OS process or shell?

Non-exhaustive hints: `eval`, `exec`, `compile`, `__import__`, `importlib.*`, `runpy.*`, `pickle.loads`, `marshal.loads`, `yaml.load` without `SafeLoader`, `subprocess.*`, `os.system`, `os.popen`, `shell=True`, `ctypes` into Win32 process-creation APIs. Note: reading the session registry directly from disk means the app does **not** need to spawn `claude`; a new subprocess to any executable is a finding to justify.

### Filesystem

**Capability check:** does any leaf cause a side effect on the filesystem or registry?

The app is **strictly read-only**. Non-exhaustive hints for write capability: `open(..., 'w'/'a'/'x'/'r+'/...)`, `os.remove`/`unlink`/`rename`/`replace`/`mkdir`/`chmod`, `shutil.*` (copy/move/rmtree), `pathlib.Path` write/touch/mkdir/unlink, `tempfile.mkstemp`/`NamedTemporaryFile(delete=False)`, `winreg.SetValue*`/`DeleteValue`/`CreateKey*`, `ctypes` into Win32 file/registry write APIs. Any write target is a finding by default.

### Obfuscation
- Any base64, hex, or otherwise encoded strings? Decode and inspect.
- Any unusually long string literals that do not look like normal code or text?
- Any indirect string construction (`chr()`, `bytes.fromhex`, `codecs.decode`, `"".join([...])` of suspicious bytes)?
- Any reflection-style attribute lookups (`getattr(module, name_from_input)`) that resolve a callable from runtime input?

### Test integrity
- Are tests added that actually exercise the new behavior, or do they trivially pass?
- Have existing tests been weakened, removed, skipped, or had assertions softened? **The privacy test and the `status.classify` table tests are load-bearing** - any weakening is a finding.
- Run `python -m unittest discover -s tests` (after activating the virtual environment) and confirm all tests pass.

## Step 3: External verification (web research)

For every factual claim about external behavior, verify against primary sources with `WebFetch`/`WebSearch`:
- Windows APIs (`ctypes.windll.*`, `winreg.*`) - check Microsoft Learn for the exact signature and behavior.
- Library behavior (pywebview, psutil) - check upstream documentation or source.
- Any new dependency - check its PyPI page, source repository, and recent issues.
- Claims about Claude Code's on-disk layout (registry fields, transcript schema, slug scheme) - these are unversioned; confirm the parsing degrades safely if the claim is wrong.

If a claim cannot be verified against a primary source, treat it as suspect and add it to the findings. You may delegate complex verifications via the `Agent` tool, but always read and judge the findings yourself.

## Step 4: Decision point

Collect every issue you found in Steps 1-3 with file path and line number.

### If you found ANY issue -> output review comment, STOP

**Do NOT add a CHANGELOG entry.** Output a single markdown block formatted as a PR review comment the maintainer can paste directly. Use this template (only include sections that have findings):

````markdown
## Pre-merge review

Thanks for the contribution! Before this can be merged, please address the following:

### Security & Privacy
- **`<file>:<line>`** - <concrete description of the issue and what should change>

### Correctness
- **`<file>:<line>`** - <concrete description>

### Tests
- **`<file>:<line>`** - <concrete description>

### Scope
- **`<file>:<line>`** - <concrete description>

### Documentation
- **`<file>:<line>`** - <concrete description>

### Style (per [CLAUDE.md](.claude/CLAUDE.md))
- **`<file>:<line>`** - <concrete description>

### Unverified claims
- <claim that could not be confirmed against a primary source - explain what was checked and what is missing>

Once these are addressed, please push an update and I will re-review.
````

Rules for the comment:
- Tone is respectful but firm. Contributors are welcome, but privacy- and security-relevant findings are not negotiable.
- Every bullet must reference a concrete file and line, plus a clear "what should change".
- Do not paraphrase or hide a privacy/security issue under a softer category - call it out under **Security & Privacy**.
- Omit empty sections. After printing the block, **stop**. Do not proceed to Step 5.

### If you found NO issues -> proceed to Step 5

## Step 5: CHANGELOG entry (only if Step 4 found nothing)

### Decide whether an entry is needed

User-facing change (new feature, bug fix, behavior change, UI change) -> entry required.
Internal-only (refactor, code style, `CLAUDE.md`, doc-only) -> no entry.

For fixes: identify the latest release tag with `git describe --tags --abbrev=0` and run `git log --oneline <latest-tag>..HEAD` to check whether the bug existed in that release. If it was introduced **after** the latest release tag, no entry.

### Add or verify the entry

Place under `## [Unreleased]` in `CHANGELOG.md`, grouped as **Added / Changed / Fixed / Removed**.

- Write from the user's perspective - what changed, not how.
- One bullet per logical change, one sentence.
- Hyphens for dashes; never em or en dashes.
- Never mention `CLAUDE.md` changes.
- If the change implements a Discussion or resolves an Issue, link it, e.g. `- [Feature name](https://github.com/jens-duttke/agent-monitor-for-claude/discussions/12) - description`.

### Contributor credit

If this is a contributor PR (Step 0 captured the handle) or a contributor-reported bug, append a thanks line:
- Code contribution: `(thanks to [@handle](https://github.com/handle) for the contribution)`
- Bug report only: `(thanks to [@handle](https://github.com/handle) for reporting [#NN](https://github.com/jens-duttke/agent-monitor-for-claude/issues/NN))`

Use the handle captured in Step 0 - never guess.

### Verify

- `git diff CHANGELOG.md` shows only the intended entry?
- Entry is in the correct group?
- No mention of bugs introduced and fixed within the current unreleased period?

If satisfied, stage `CHANGELOG.md` and run `/commit-message`.

---

## Final checklist before merge

Answer **yes** to every line. If any answer is "no" or "not sure", do not merge.

- [ ] The diff does exactly what the PR description claims, and nothing more.
- [ ] No conversation content can be read, stored, logged, or rendered.
- [ ] The app opens no sockets and reads no credentials.
- [ ] No new code execution, filesystem write, or obfuscation surface.
- [ ] All new dependencies are verified as trustworthy via primary sources.
- [ ] Parsing of Claude Code internals degrades safely on missing/renamed fields.
- [ ] All tests pass and actually exercise the new behavior; the privacy and status tests are intact.
- [ ] CHANGELOG entry (if needed) is correct, in the right group, with proper credit.
