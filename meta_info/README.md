# `meta_info/` — per-project personalization for HARP

This directory is the **only** place inside the HARP engine repo (A,
`SERVICE_ROOT`) where project-specific values live. Everything else in A is
deliberately project-agnostic so the engine itself can be reused across
arbitrary research projects.

## Why it exists

HARP has three repositories at runtime:

| Role | Path                        | Owns                                   |
|------|-----------------------------|----------------------------------------|
| A    | `harness-auto-research/`    | Engine code + project-agnostic templates |
| B    | `${workspace.dir}/`         | Live runtime config + research history |
| D    | `${target.repo_path}/`      | The codebase being optimised            |

Originally A held some project-specific values directly (in
`harness.yaml` / `userprompt.yaml` / `.cursorrules`), and B was created by
`cp`-ing those files. That coupled A and B together: any change to A's
templates leaked into B, and runtime scripts couldn't tell whether to read
A's "template" copy or B's "live" copy. The result was a class of bugs
where placeholders in A's templates (`<TARGET_NAME>`, `/absolute/path/...`)
silently fed into runtime via env.sh.

`meta_info/project.yaml` cuts the knot:

- **A holds only**: engine code, project-agnostic seed files
  (`program.md`, `memory.md`, `check.md`, `.mcp.json`, plan-registry
  template), and `meta_info/project.yaml` (this folder).
- **B holds**: everything the runtime needs (`harness.yaml`,
  `userprompt.yaml`, `.cursorrules`, `program.md`, ...), all
  rendered/copied at init time, owned by B thereafter.
- **The bridge**: `init_workspace.sh` reads `meta_info/project.yaml`
  ONCE, renders the templates with substituted values, writes them
  into B, then never touches B's config again. `env.sh` reads ONLY
  `meta_info/project.yaml::harness.workspace.dir` to know where B
  lives — every other runtime read targets `B/harness.yaml`.

## What goes in `project.yaml`

One file, three sections:

| Section        | Becomes                  | Substitution                           |
|----------------|--------------------------|----------------------------------------|
| `harness:`     | `B/harness.yaml`         | none — written verbatim                |
| `userprompt:`  | `B/userprompt.yaml`      | none — written verbatim                |
| `cursorrules:` | `B/.cursorrules`         | `${KERMT_ROOT}` `${SERVICE_ROOT}` `${WORK_DIR}` substituted from harness fields |

See `project.yaml` itself for the schema; it is heavily commented.

## Bootstrapping a new project

1. Copy `meta_info/project.yaml` to a new branch / fork of A.
2. Replace the `harness.targets[0]`, `harness.workspace.dir`,
   `userprompt.rules`, and `cursorrules.header` blocks with values for
   your project.
3. Run `bash scripts/init_workspace.sh` (it reads `meta_info/project.yaml`
   automatically). B is created and populated.
4. Run `bash scripts/quickstart.sh` to do the agent preflight.

## What you should never do

- **Never** edit `B/harness.yaml` and expect that change to flow back
  into `meta_info/project.yaml`. The data flow is A → B at init only.
  If you want to capture B's state for re-init, manually update
  `meta_info/project.yaml` to match.
- **Never** put project-agnostic content (engine logic, generic constraints)
  into `meta_info/`. That belongs in `program.md`, `check.md`, or scripts.
- **Never** reference `meta_info/project.yaml` from `B/`. B is decoupled
  from A by design — runtime code reads `B/harness.yaml` only.
