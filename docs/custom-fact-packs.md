# Fact packs of your own

A fact pack tells Topos one kind of fact to look for in your records and how to store it. Topos ships a set of them. You can add your own from a YAML file.

## Add one

1. Start from [the gardening example](examples/custom.gardening.yaml). Give the pack an id that starts with `custom.` (for example `custom.gardening`), a `version`, a `title`, and at least one predicate whose name also starts with `custom.`.
2. Check the file. This adds nothing and needs no terminal prompt, so a coding agent can run it for you:

   ```
   topos-node packs add --check my-pack.yaml
   ```

3. Try it. This runs the file's own examples on your local model and says which pass. It adds nothing, reads none of your records, and needs no terminal prompt either:

   ```
   topos-node packs try my-pack.yaml
   ```

4. Add it, switch it on and read the last 30 days for it, in your own terminal. The command shows what the pack would read and write, names all three effects, and asks `[y/N]` once:

   ```
   topos-node packs add my-pack.yaml --on --backfill 30d
   ```

The file is copied to `fact-packs/` beside the node's environment file (normally `~/.topos/fact-packs/`). The node does not need a restart. `topos-node packs remove custom.gardening` takes the pack out again; the facts it already wrote are kept.

You can take the steps apart. `topos-node packs add my-pack.yaml` only adds the pack, and it starts switched off: it reads nothing until you switch it on, and Topos never samples your records to suggest switching it on, as it can for a shipped pack that is off. `topos-node packs on custom.gardening` switches it on (so does the app, under Data → Fact packs). `topos-node packs backfill --pack custom.gardening` reads your history for it, as for any pack. None of this needs a Dialogues account.

## With a coding agent

A coding agent you connected can draft the file and test it by itself. These commands give it no way to add a pack, switch one on or read your history: each of those waits for your yes on a terminal, and there is no option that answers for you.

| The agent runs | What happens | Needs you |
| --- | --- | --- |
| `topos-node packs add --check --json FILE` | Says what the file declares and what adding it would do. Adds nothing. | no |
| `topos-node packs try --json FILE` | Runs the file's own examples on your local model. Writes nothing, reads none of your records. | no |
| `topos-node packs add FILE --on --backfill 30d --open-terminal --json` | Opens a Terminal window (macOS) where the question is waiting for you. | one yes |

Without `--open-terminal`, or off macOS, the last command prints the command for you to run in your own terminal. Either way the agent's command ends at once with exit code 2 and writes nothing. The window is tied to the file as the agent checked it: if the file changes before you answer, the command in the window refuses it.

After your yes the command reports each step: `added`, `switched_on`, `backfill_started`. If a step fails, the steps before it stay done and the command says where things stand. For example, if the pack cannot be switched on, it is added and switched off, and no history is read.

`--backfill` takes a number of days (`30d`, `90d`), `all`, or a day (`2026-08-01`). How many records the pack will read cannot be counted before the pack is added, so the question names the window, the model and this node's measured seconds per call, and the run says the count when it starts. `topos-node packs status --follow` watches the run and `topos-node packs stop` stops it.

`topos-node packs on ID` and `topos-node packs off ID` ask you the same way, once, on a terminal. `off` asks too: the switch is yours, and while a pack is off no app reads the facts it wrote. A pack that writes about other people (some of the packs Topos ships do) is never switched on from a terminal. Its consent is asked in the app.

### Which records a pack reads

A record is sent to the model for a pack when it holds one of the pack's routing phrases (`routing.exemplars`) or just one of its routing words. The words come from `routing.descriptors`, `routing.exemplars`, the allowed values of the pack's fields, and the texts of its own `eval.gold` examples, less a short list of words that would match anything. Topos errs toward reading too much here, because a record it skips is a fact it never finds.

So an everyday word costs you. One gold example that says "Every morning I water the basil" makes `every` and `morning` routing words, and most of your records then go to the model for this pack, one call each. `topos-node packs add --check --json FILE` lists the words (`routing.words`) and the phrases (`routing.phrases`) exactly as the engine computes them. Read the list and reword an example or a descriptor until the words are ones only the records you mean would hold. The check counts none of your records.

### What `packs try` tells you, and what it does not

`try` runs only the texts under the file's `eval.gold` and `eval.negative_controls` through the prompt and the parser a real run uses, on the local model you picked for facts. It needs Topos running. It runs at most 20 examples per call, one call each, and waits its turn behind whatever else your Topos is running on that model. It never loads a model ahead of time or keeps one loaded.

- A gold example passes when every fact it expects comes back: the same predicate, about you, and every field the example names with an equal value (case and spacing do not count). A field the example does not name is not compared. A gold example with no `expect` cannot be judged and is reported as such, and a try with one is not a pass: it ends `incomplete`, with exit code 3.
- A negative control passes when nothing comes back.

A pass is about those examples only. It does not show:

- how the pack reads your own records. `try` never reads them, by design;
- the check Topos makes on each fact it finds before storing it. That is a second model, and `try` does not run it;
- what the pack costs on your history. That depends on the routing words above and on how many of your records hold them;
- the same answer next time. A model can answer one text two ways.

Exit codes: `try` ends with 0 when every example passed, 3 when one failed, could not be run or could not be judged, 4 when Topos is not running, 5 when no local model is picked for facts or the model did not answer, and 1 when it was refused: the file is not a pack, it holds more than 20 examples, or another try is running.

## What a pack you add can and cannot do

- **It cannot take the place of a pack Topos ships.** Its id and its predicates must be in the `custom.` namespace, which no shipped pack uses. A file that names a shipped pack or predicate is refused, and at run time a shipped pack always wins.
- **It describes you only.** `net_subject: allow` is refused. A fact it finds about another person is held for your review, never written, the same as for a shipped pack that describes only you.
- **Its facts are stored like a shipped pack's.** They are `owner_only`, with the sensitivity the pack declares (`none`, `personal` or `special`).
- **An app reads them only through a grant that names the pack**, exactly as for a shipped pack (`facts:custom.gardening` on the approval prompt). The same hold-backs apply to that read: nothing from a pack that is switched off, no special-class fact, and nothing that touches a person or record you marked Off-limits.
- **A grant names the pack's id, not its definition.** If an app already holds a grant for `custom.gardening` and you add a new version, or remove the pack and add a different file under the same id, that grant covers the facts the new definition writes once you switch the pack on. You are not asked to approve the app again. `packs add` says how many connected apps hold such a grant before it asks you. `topos-node agents list --json` shows what each app reads, and `topos-node agents disconnect <app>` ends an app's grant.
- **A coding agent you connected, and a routine, need no grant.** They are not apps. They put questions to your node, and an answer Topos gives them can draw on the facts of a pack you added, as it can on the facts of a shipped pack. Nothing holds a fact back from them because its pack is one you added. What narrows it is what narrows a shipped pack: Off-limits, and the pack's sensitivity, where a pack that declares `special` is treated as a shipped special-class pack is. This is the owner's ruling (DR-74, 12 November 2026): a coding agent and a routine read the facts of a pack you added exactly as they read a shipped pack's, and nothing is held back from them because you added the pack.
- **Marks and clean-up work as for a shipped pack.** An app may record your reaction and rank on one of its facts only if you connected it with `--feedback facts:custom.gardening` (and `--reads` naming the pack too). `topos-node packs retract-reactions` counts and ends its facts by the same rules as a shipped pack's. It never reads the turns before a message: that reading is ruled for the two Moments packs only (DR-62).
- **A share with another person cannot include them yet.** The fact categories a share can name are a fixed list that the control plane and the node agree on, and a pack you add is not on it. A fact from such a pack is refused for every share.
- **Changing it switches it off.** The switch records the exact definition you switched on, whether you switched it on in the app or with `packs on`. If the file changes (you add a new version, or edit it by hand), or the file is removed or stops validating, the pack is switched off and reads nothing until you switch it on again. Removing a pack and adding the same file again also starts switched off. From the moment it is off, no app reads the facts it wrote earlier; they are kept, and an app reads them again once you switch the pack on.
- **Its sensitivity is what the file says.** For a shipped pack, Topos decides which facts are special-class (health, beliefs and the like), and those are never given to an app. For a pack you add, that rests on the `sensitivity_class` in the file, which `packs add` shows you before it asks. Topos does not check it against what the pack looks for. If the pack looks for health, beliefs or similar, declare it `special`: a pack that looks for them and declares `personal` is read as a personal pack by an app whose grant names it, by a coding agent and by a routine.
- **Read a pack file before you add it.** `packs add` shows the pack's id, version, title, what it reads, its sensitivity and the kinds of fact it writes. The wording that tells the model what to look for (`guidance`, `routing`) is in the file itself.
- **An app cannot add one.** Nothing but the `packs add` command writes to the pack directory, and that command asks a person on a terminal. There is no route, relay message or tool for it. The terminal's switch (`packs on`, `packs off`) takes your yes on a terminal too: a coding agent's own request to that switch is refused.

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
| `eval.gold`, `eval.negative_controls` | at least one example each, with a `text`. A gold example lists the facts it expects under `expect` (`predicate` and `value`); a negative control expects none. Adding a pack does not run or grade them; `topos-node packs try` does. The words of the gold texts also route records to the pack (see "Which records a pack reads") |
| `consumers` | at least one name, for example `[assistant_context]` |
| `routing` | optional; `descriptors` and `exemplars`, each a list of up to 200 short texts |
| `guidance` | optional; `definitions` (text) and `abstention` (a list of up to 50 texts) |
| `revision` | optional; only `supersedes_on` (a structured predicate of the pack mapped to a list of its fields) and `noop_when` (short text). `exclusive_with` and `closes` are not supported in a pack you add yet |
| `synthesis` | not supported in a pack you add yet |
| the file as a whole | plain YAML: every key is text, no anchors or aliases, at most 12 levels deep |

A node holds up to 20 packs you added, each file up to 256 KB. A file that breaks a rule is refused with one sentence saying which; so is a file the YAML reader cannot read at all (a date that does not exist, a number thousands of digits long, brackets nested without end). If a file already in the pack directory stops validating, `topos-node packs list` names it and says why, and every other pack keeps working and keeps its switch, the other packs you added included.
