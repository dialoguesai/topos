## The label vocabulary arm A reads

The engine accepts any identifier as a review domain (`ReviewedClassification.domains`), so the vocabulary is fixed here. Arm A's policies may name only these values.

**Domains** (multi-label; a message gets every domain it touches):

| Domain | Covers |
|---|---|
| `work` | the owner's job, employer, projects, colleagues in a work capacity, work travel |
| `plans` | scheduling, trips, meetups, what the owner will do and when |
| `hobbies` | leisure activities and interests: reading, running, cooking, games, music |
| `health` | anyone's physical or mental health, medical care, injuries, medication, therapy |
| `family` | relatives, family events, family relationships |
| `finance` | money: pay, debt, budgets, prices of personal significance, banks, rent amounts |
| `relationships` | romantic, dating or friendship dynamics beyond simply naming who the owner is meeting |
| `home` | where the owner lives: city, neighbourhood, address, housing situation |

**Sensitivity** (exactly one; take the highest that applies):

| Sensitivity | Applies when |
|---|---|
| `special` | health (anyone's), sexuality, religion, politics, legal trouble, a precise home location, finance with amounts or accounts |
| `personal` | names or identifies people close to the owner, their private situations, the owner's whereabouts with others, emotions about people |
| `none` | nothing personal beyond the topic itself |

**Floor fields**, which never reach arm A's predicates:
- `authorship`: `owner_authored` \| `other`
- `speech`: `direct_self_statement` \| `third_party_quote` \| `mixed`
- `independent_copies`: `none_known`
- `subject_entity_ids`: the attested self
