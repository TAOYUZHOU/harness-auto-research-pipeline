# Installing the HARP skill

The skill works as a single `skill/` folder. There are two installation
modes — pick whichever matches your tooling.

## Mode 1 — Cursor (recommended on this machine)

Cursor looks for skills under `~/.cursor/skills/<name>/SKILL.md`.

```bash
# Symlink so future engine updates are picked up automatically.
ln -s "$(pwd)/skill" ~/.cursor/skills/harp
```

Verify it's discovered:

```bash
ls ~/.cursor/skills/harp/SKILL.md   # must exist
```

Restart any active Cursor session; the agent will see "HARP — Harness
for Auto-Research Pipelines" in its skill list and will use it
automatically when the user asks about auto-research / auto-iteration.

## Mode 2 — Claude Code

Claude Code reads project-scoped skills from `.claude/skills/`:

```bash
# Project-level (only this repo)
mkdir -p .claude/skills
ln -s "$(pwd)/skill" .claude/skills/harp

# OR user-level (all projects)
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skill" ~/.claude/skills/harp
```

## Mode 3 — Standalone shell tool (no agent)

Even without a skill-aware agent the scripts work fine. Add the skill's
`scripts/` to PATH:

```bash
echo 'export PATH="$HOME/code/harness-auto-research/skill/scripts:$PATH"' >> ~/.bashrc
source ~/.bashrc
harp_status.sh    # try it
```

## Required external tools

| Tool          | Why                                                  |
|---------------|------------------------------------------------------|
| `cursor-agent`| Engine + polish loop (see https://cursor.com/cli)    |
| `git` ≥ 2.30  | Experiment management                                |
| `gh`          | Only if workspace_remote.mode=auto                   |
| `python3` ≥ 3.10 | Engine (poll_tick.py)                             |

## Uninstall

```bash
rm ~/.cursor/skills/harp        # or .claude/skills/harp
bash $ENGINE/scripts/install_cron.sh remove
# leave the workspace B folder; it's your research history
```
