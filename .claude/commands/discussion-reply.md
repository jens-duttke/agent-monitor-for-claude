Generate a GitHub Discussion reply for a completed feature implementation.

# Steps

1. Run `git log -1 --format="%H"` to get the latest commit hash
2. Write the reply to `discussion-$ARGUMENTS-reply.md` in the project root

# Format

```markdown
Implemented in [`{short_hash}`](https://github.com/jens-duttke/agent-monitor-for-claude/commit/{full_hash}).

**{One-line summary of the new feature or change}**

{Code example if applicable, e.g. a JSON settings snippet in a fenced block}

- {Key detail: default behavior, backwards compatibility}
- {Key detail: validation, error handling}
- {Key detail: edge cases, fallbacks}

See [{Link text}]({URL to relevant docs section}) for details.
```

# Rules

- Use the short hash (first 7 chars) for display, full hash in the URL
- Keep bullet points concise - highlight what users need to know, not implementation details
- The code example is optional - include it when the feature has user-facing configuration
- The docs link should point to the specific section, not the whole page (use `#anchor`)
- Write in English
- Tell the user the file is ready and can be deleted after copying

# Input

`$ARGUMENTS` is the discussion number (e.g. `11`). Read the discussion content using `gh api repos/jens-duttke/agent-monitor-for-claude/discussions/$ARGUMENTS --jq '.title, .body'` to understand what was implemented, then compose the reply based on the actual changes in the latest commit.
