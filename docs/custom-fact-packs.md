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

4. Switch it on in the app (Data → Fact packs). A pack you add starts switched off and reads nothing until you do. Topos never samples your records to suggest switching it on, as it can for a shipped pack that is off.
5. `topos-node packs backfill --pack custom.gardening` reads your history for it, as for any pack.

The file is copied to `fact-packs/` beside the node's environment file (normally `~/.topos/fact-packs/`). The node does not need a restart. `topos-node packs remove custom.gardening` takes the pack out again; the facts it already wrote are kept.

## What a pack you add can and cannot do

- **It cannot take the place of a pack Topos ships.** Its id and its predicates must be in the `custom.` namespace, which no shipped pack uses. A file that names a shipped pack or predicate is refused, and at run time a shipped pack always wins.
- **It describes you only.** `net_subject: allow` is refused. A fact it finds about another person is held for your review, never written, the same as for a shipped pack that describes only you.
- **Its facts are stored like a shipped pack's.** They are `owner_only`, with the sensitivity the pack declares (`none`, `personal` or `special`).
- **An app reads them only through a grant that names the pack**, exactly as for a shipped pack (`facts:custom.gardening` on the approval prompt). The same hold-backs apply to that read: nothing from a pack that is switched off, no special-class fact, and nothing that touches a person or record you marked Off-limits.
- **A grant names the pack's id, not its definition.** If an app already holds a grant for `custom.gardening` and you add a new version, or remove the pack and add a different file under the same id, that grant covers the facts the new definition writes once you switch the pack on. You are not asked to approve the app again. `packs add` says how many connected apps hold such a grant before it asks you. `topos-node agents list --json` shows what each app reads, and `topos-node agents disconnect <app>` ends an app's grant.
- **A coding agent you connected, and a routine, need no grant.** They are not apps. They put questions to your node, and an answer Topos gives them can draw on the facts of a pack you added, as it can on the facts of a shipped pack. Nothing holds a fact back from them because its pack is one you added. What narrows it is what narrows a shipped pack: Off-limits, and the pack's sensitivity, where a pack that declares `special` is treated as a shipped special-class pack is. Whether a pack you add should be held back from coding agents and routines altogether has not been decided.
- **A share with another person cannot include them yet.** The fact categories a share can name are a fixed list that the control plane and the node agree on, and a pack you add is not on it. A fact from such a pack is refused for every share.
- **Changing it switches it off.** The switch records the exact definition you switched on. If the file changes (you add a new version, or edit it by hand), or the file is removed or stops validating, the pack is switched off and reads nothing until you switch it on again. Removing a pack and adding the same file again also starts switched off. From the moment it is off, no app reads the facts it wrote earlier; they are kept, and an app reads them again once you switch the pack on.
- **Its sensitivity is what the file says.** For a shipped pack, Topos decides which facts are special-class (health, beliefs and the like), and those are never given to an app. For a pack you add, that rests on the `sensitivity_class` in the file, which `packs add` shows you before it asks. Topos does not check it against what the pack looks for. If the pack looks for health, beliefs or similar, declare it `special`: a pack that looks for them and declares `personal` is read as a personal pack by an app whose grant names it, by a coding agent and by a routine.
- **Read a pack file before you add it.** `packs add` shows the pack's id, version, title, what it reads, its sensitivity and the kinds of fact it writes. The wording that tells the model what to look for (`guidance`, `routing`) is in the file itself.
- **An app cannot add one.** Nothing but the `packs add` command writes to the pack directory, and that command asks a person on a terminal. There is no route, relay message or tool for it.

## Format

A pack you add uses the format of the shipped packs, with these limits:

| Field | Rule |
| --- | --- |
| `pack` | `custom.<name>`: lowercase letters, digits and underscores, up to three more dotted parts |
| `version` | text such as `"1.0.0"`; a changed pack needs a new version |
| `title` | 1 to 120 characters on one line, no control characters |
| `sensitivity_class` | `none`, `personal` or `special` |
| `role_policy` | `authored_only` (only what you wrote), `authored_addressed` (also what was written to you) or `any_with_label` (also what you only saw; each fact says which) |
| `net_subject` | `deny`, or left out |
| `disclosure_default` | `owner_only`, or left out |
| `predicates` | 1 to 50. Each has a `name` starting with `custom.`, `cardinality` (`single`, `multi`), `temporal` (`interval`, `episodic`, `point`), `altitude: stated`, and a `value_type` |
| a predicate's `key_fields`, `required_fields` | optional; a list of fields its `value_schema` declares |
| a predicate's `event_identity` | optional; `once`, `dated` or `windowed:<days>` |
| a predicate's `note` | optional; text of up to 2000 characters |
| `value_type` | a plain type (`string`, `text`, `number`, `boolean`, `date`, `date_or_approx`, `date_range_or_approx`, `recurring_date`, or a person or entity reference type), `enum` with a `values` list, or `structured` with a `value_schema` of up to 50 fields |
| a `value_schema` field | a plain type, a list of allowed values, `{enum: [...]}`, or `{scale: <a scale Topos ships>}` |
| `eval.gold`, `eval.negative_controls` | at least one example each, with a `text`. They are checked for shape; adding a pack does not run or grade them |
| `consumers` | at least one name, for example `[assistant_context]` |
| `routing` | optional; `descriptors` and `exemplars`, each a list of up to 200 short texts |
| `guidance` | optional; `definitions` (text) and `abstention` (a list of up to 50 texts) |
| `revision` | optional; only `supersedes_on` (a structured predicate of the pack mapped to a list of its fields) and `noop_when` (short text). `exclusive_with` and `closes` are not supported in a pack you add yet |
| `synthesis` | not supported in a pack you add yet |
| the file as a whole | plain YAML: every key is text, no anchors or aliases, at most 12 levels deep |

A node holds up to 20 packs you added, each file up to 256 KB. A file that breaks a rule is refused with one sentence saying which; so is a file the YAML reader cannot read at all (a date that does not exist, a number thousands of digits long, brackets nested without end). If a file already in the pack directory stops validating, `topos-node packs list` names it and says why, and every other pack keeps working and keeps its switch, the other packs you added included.
