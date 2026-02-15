---
description: Create a new TreLLM project with config entry, directory, and git init
allowed-tools: Bash, Read, Write, Edit
---

# Create TreLLM Project

Create a new project in the TreLLM configuration.

## Arguments

Parse `$ARGUMENTS` as: `<name> <directory> [alias]`

Example: `/create-project smugcoin ~/src/smugcoin smg`

## Steps

1. **Read the active config** at `~/.trellm/config.yaml`
2. **Check the project doesn't already exist** in the config (by name or alias). If it does, report the conflict and stop.
3. **Create the directory** if it doesn't already exist (use `mkdir -p` with tilde expanded)
4. **Initialize git** in the directory if it's not already a git repo (`git init` then `git branch -m main`)
5. **Add the project entry** under `claude.projects` in the config file, inserted alphabetically among existing projects:
   - `working_dir` set to the provided directory
   - `aliases` list with the alias if provided (omit if no alias)
6. **Verify** the config parses correctly:
   ```
   python -c "from trellm.config import load_config; c = load_config(); print(c.resolve_project('<name>'))"
   ```
7. **Report** what was created
