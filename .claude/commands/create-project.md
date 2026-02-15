---
description: Create a new TreLLM project with config entry, directory, and git init
allowed-tools: Bash, Read, Write, Edit
---

# Create TreLLM Project

Create a new project in the TreLLM configuration.

## Arguments

The user will provide: project name, working directory, and optionally an alias.

Example: `/create-project smugcoin ~/src/smugcoin smg`

Parse the arguments as: `<name> <directory> [alias]`

## Steps

1. **Read the active config** at `~/.trellm/config.yaml`
2. **Check the project doesn't already exist** in the config (by name or alias)
3. **Add the project entry** under `claude.projects` in the config file:
   - `working_dir` set to the provided directory
   - `aliases` list with the alias if provided
4. **Create the directory** if it doesn't already exist (`mkdir -p`)
5. **Initialize git** in the directory if it's not already a git repo (`git init` with `main` branch)
6. **Verify** the config parses correctly by running:
   ```
   python -c "from trellm.config import load_config; c = load_config(); print(c.resolve_project('<name>'))"
   ```
7. **Report** what was created

## Example Config Entry

```yaml
    smugcoin:
      working_dir: "~/src/smugcoin"
      aliases: ["smg"]
```

If no alias is provided, omit the `aliases` line.
