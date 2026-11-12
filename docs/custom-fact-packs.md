# Fact packs of your own

A fact pack tells Topos one kind of fact to look for in your records and how to store it. Topos ships a set of them. You can add your own from a YAML file.

## Add one

1. Start from [the gardening example](examples/custom.gardening.yaml). Give the pack an id that starts with `custom.` (for example `custom.gardening`), a `version`, a `title`, and at least one predicate whose name also starts with `custom.`.
2. Check the file. This adds nothing and needs no terminal prompt, so a coding agent can run it for you:

   ```
   topos-node packs add --check my-pack.yaml
   ```

3. Add it, in your own terminal. The command shows what the pack would read and write and asks `[y/N]` once:

   ```
   topos-node packs add my-pack.yaml
   ```

4. Switch it on in the app (Data → Fact packs). A pack you add starts switched off and reads nothing until you do.
5. `topos-node packs backfill --pack custom.gardening` reads your history for it, as for any pack.

The file is copied to `fact-packs/` beside the node's environment file (normally `~/.topos/fact-packs/`). The node does not need a restart. `topos-node packs remove custom.gardening` takes the pack out again; the facts it already wrote are kept.

## What a pack you add can and cannot do

- **It cannot take the place of a pack Topos ships.** Its id and its predicates must be in the `custom.` namespace, which no shipped pack uses. A file that names a shipped pack or predicate is refused, and at run time a shipped pack always wins.
- **It describes you only.** `net_subject: allow` is refused. A fact it finds about another person is held for your review, never written, the same as for a shipped pack that describes only you.
- **Its facts are yours.** They are stored `owner_only`, with the sensitivity the pack declares (`none`, `personal` or `special`).
- **An app reads them only through a grant that names the pack**, exactly as for a shipped pack (`facts:custom.gardening` on the approval prompt). The same hold-backs apply to that read: nothing from a pack that is switched off, no special-class fact, and nothing that touches a person or record you marked Off-limits.
- **A share with another person cannot include them yet.** The fact categories a share can name are a fixed list that the control plane and the node agree on, and a pack you add is not on it. A fact from such a pack is refused for every share.
- **Changing it switches it off.** The switch records the exact definition you switched on. If the file changes (you add a new version, or edit it by hand), or the file is removed or stops validating, the pack is switched off and reads nothing until you switch it on again.
- **An app cannot add one.** Nothing but the `packs add` command writes to the pack directory, and that command asks a person on a terminal. There is no route, relay message or tool for it.

## Format

A pack you add uses the format of the shipped packs, with these limits:

| Field | Rule |
| --- | --- |
| `pack` | `custom.<name>`: lowercase letters, digits and underscores, up to three more dotted parts |
| `version` | text such as `"1.0.0"`; a changed pack needs a new version |
| `title` | 1 to 120 characters |
| `sensitivity_class` | `none`, `personal` or `special` |
| `role_policy` | `authored_only` (only what you wrote), `authored_addressed` (also what was written to you) or `any_with_label` (also what you only saw; each fact says which) |
| `net_subject` | `deny`, or left out |
| `disclosure_default` | `owner_only`, or left out |
| `predicates` | 1 to 50. Each has a `name` starting with `custom.`, `cardinality` (`single`, `multi`), `temporal` (`interval`, `episodic`, `point`), `altitude: stated`, and a `value_type` |
| `value_type` | a plain type (`string`, `text`, `number`, `boolean`, `date`, `date_or_approx`, `date_range_or_approx`, `recurring_date`, or a person or entity reference type), `enum` with a `values` list, or `structured` with a `value_schema` of up to 50 fields |
| a `value_schema` field | a plain type, a list of allowed values, `{enum: [...]}`, or `{scale: <a scale Topos ships>}` |
| `eval.gold`, `eval.negative_controls` | at least one example each, with a `text`. They are checked for shape; adding a pack does not run or grade them |
| `consumers` | at least one name, for example `[assistant_context]` |
| `synthesis` | not supported in a pack you add yet |

A node holds up to 20 packs you added, each file up to 256 KB. A file that breaks a rule is refused with one sentence saying which. If a file already in the pack directory stops validating, `topos-node packs list` names it and says why, and every other pack keeps working.
