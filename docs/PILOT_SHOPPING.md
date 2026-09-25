# Pilot: first contact, typed by hand

**Run:** 2026-09-25, 00:38 to 03:40 local · **Spend:** ~0.7 credits on the agent, an
estimated $3 to $4 of OpenAI for the judge · **Suite:** `scripts/pilot_human.py`

---

## Why this pilot exists

A user reported the shop answering badly. They asked for a charger, were shown the Anker
323 Charger (33W) three times at $19.99, $27.99 and $39.99, then asked about a power bank
and were shown no power banks at all. Their words: *"quality is bad."*

The existing suites did not catch it, and it is worth understanding why rather than just
patching. `scripts/eval_run.py` and `scripts/eval_conversations.py` are written by people
who know what the agent can do, so their customers say *"my S1 Pro isn't sucking"*, a
sentence engineered to hit the disambiguation path. Both suites are about faults, because
the brief is about faults. But roughly half of real first contact is **shopping**, and
shopping had no tests at all. That is the gap the reported defect lived in.

So this pilot is nineteen people who found the shop today and typed like people:

| | |
|---|---|
| `p01` to `p06` | shopping: typos, budgets, comparisons, a Chinese shopper, a dog and wooden floors |
| `p07` to `p11` | the brief's own scenarios, phrased the way a person phrases them |
| `p12` to `p19` | the messy half: one-word openers, rudeness, mixed languages, a thing we do not sell, someone who just wants a human |

39 turns. Every turn carries deterministic checks; a judge from a different model family
(GPT-5, against the agent's DeepSeek) grades the whole conversation on a shopper's rubric,
including one axis no assertion can decide: **do the product cards answer the sentence they
are attached to?**

---

## What it found, and what was done

### 1. One product, three prices

`q=charger` returned the same charger three times. The catalogue stores a model once per
storefront: 1,493 rows collapse to 1,362 models, and 113 of them carry two or three
listings differing only in currency. `canonical_id` exists for exactly this and was
**written by `scripts/link_variants.py` and never read at query time**. The frontend then
printed every price with a dollar sign, so three currencies read as three prices.

*Fixed.* `search_products` now collapses to one listing per model (`canonical_id`, then
identical names for rows the linker never paired), preferring the dollar listing, then the
one with a photograph. `lib/money.ts` on the frontend formats with the row's own currency
and `currencyDisplay: "symbol"`. The narrow form was wrong: it prints Australian dollars as
a bare `$` and reintroduces the ambiguity.

### 2. No power banks in a power bank question

The planner sent `category="power bank"`; the column holds `power_bank`. The filter matched
nothing, the search returned zero rows, and the reply carried no products. Underneath that,
ranking was by whole-string trigram similarity, which rewards short names: *"fast charging
power bank"* put a **Fast Charging Power Strip** above every power bank in the catalogue.

*Fixed.* Categories the planner types are normalised against the real values and rejected
if invented. Matching is now word-led rather than phrase-led: the **head word** of the query
decides which rows qualify, and words are weighted by how rare they are in the catalogue, so
"charging" (300 names) counts for little and "iphone" counts for a lot. Supporting pieces:
plurals match singular product names (*vacuums* → *Vacuum*), matching respects word
boundaries (`%trip%` used to match *Power Strip*), and a word no product name resembles ends
the search rather than returning six near-misses, but only when that word **is** the request.
That qualifier matters, because "robot vacuum, i have a dog and wooden floors" also contains words no product carries.

Calibration for that last rule is in the code: misspellings score 0.57 and up on
`word_similarity` (*vacuuum* 0.88, *chargr* 0.71, *powerbnk* 0.67), things nobody here sells
score 0.43 and below (*fridge* 0.43, *dyson* 0.33, *samsung* 0.25). One exception breaks it
and it is the likeliest typo of all: *anekr* scores 0.33, because trigram similarity is blind
to transposed letters, so brand-shaped words are checked against the four brand names directly.

### 3. A picker offering products nobody had seen

Two chargers were recommended; the customer asked *"does it come with a cable"*; the agent
replied with a picker headed **"which one do you have"** listing three products that had
never appeared. Somebody who owns nothing yet cannot answer that question.

*Fixed.* The session now remembers every SKU it has actually put on screen
(`AgentState.shown_skus`, persisted in `sessions.meta.shown`). When a follow-up is ambiguous
and something on screen matches, the candidates narrow to what the customer can see; if
exactly one matches, it resolves instead of asking.

### 4. Cards nobody could buy from

Cards appeared with a blank where the price goes (19 rows carry no price, mostly refurbished
stock), and a bundle of a vacuum with two floor cleaners led the answer to *"the cheapest one
that empties itself"*.

*Fixed.* Unpriced rows never reach the shelf, and bundles sort behind every single product
that answers on its own. The shelf is also ordered by what the reply actually names: a
customer was told about a charger at $22.99 with no card for it on screen while six others sat
underneath.

### 5. Links and specs invented out of nothing

The guard already checked prices, SKUs and error codes against tool output. It did not check
**links**, and a draft sent a customer to a product page on the EU site that no tool had
returned. Separately, and more often, the agent asserted specifications it had never looked
up: that a 323 charger lacks the wattage for a MacBook Air, that a 20K power bank clears
airport security, that one model's brush timings apply to another.

*Partly fixed.* G3 now checks URLs. The composer policy gained a rule that a specification is
a fact and not an inference, covering wattage, capacity, compatibility and what is allowed on
a plane, and that the wattage in a product's name is part of its name, not a spec sheet. This reduced
the behaviour; it did not end it. See *What is still open*.

### 6. Shopping was being handled like fault diagnosis

The biggest one, and the last found. See *What is still open* for the measurement.

### 7. One thing was made worse before it was made better

A gate was added that hid the product grid when the search looked weak. It made things worse
in a way worth recording: the composer still narrated those products and their prices from the
same tool output, so customers got prices with nothing to click, and once a price attached to
the wrong product. **Hiding the evidence does not improve the claim.** The gate was removed and
the fix moved into retrieval, where it belonged.

---

## What the numbers say, and what they cannot

Ten pilot runs, four of them on the final code. The honest reading:

**Deterministic checks, which are the signal.** Duplicate cards, priceless cards and
bundle-first never recurred after the fixes. Across the last four runs (156 turns), one check
failure total, and it was a price the reply quoted without a card, which fix #4 then addressed.

**Judge scores, which are not signal at this sample size.** Four runs of *identical code* were
compared conversation by conversation:

| axis | mean spread, same conversation, same code | worst |
|---|---|---|
| honesty | **2.42** | 5 |
| products | **2.14** | 4 |
| memory | 1.89 | 5 |
| understood | 1.58 | 4 |
| usefulness | 1.42 | 4 |
| manner | 0.53 | 2 |

The judge disagrees with itself by two and a half points out of five on honesty, and
**pass/fail flipped between runs in 8 of 19 conversations**. Any before-and-after comparison
smaller than that spread is noise, so the axis means are reported here and no improvement is
claimed from them. Only `manner`, at 0.53, is stable enough to read.

What survives that test is the judge's **reproducible** criticals, the ones it raised in three
of four runs:

| runs flagged | conversation | what it says |
|---|---|---|
| 3/4 | `p06_vacuum_shopping` | cards answer the category but ignore the qualifier ("the cheapest one that empties itself") |
| 3/4 | `p14_not_sold_here` | asked for an iPhone case, shown adjacent products instead of "we don't sell those" |
| 1/4 | six others | noise |

**No regression elsewhere.** 259 unit tests pass (250 before, plus 9 written from tonight's
findings). `eval_run.py` 27/27. `eval_conversations.py` 30/30.

---

## What is still open

### The largest finding was not about cards at all

`p01`, the conversation the user actually reported, scored 0 or 1 out of 5 on *understood*
in **every run**: 1, 1, 1, 0, 1. Nothing else in this pilot was that stable, which made it
the one score worth believing without averaging, and it was measuring something real. The
agent was treating shopping as if it were fault diagnosis. Told *"it is for iphone, the fast
charging one"*, it replied *"which iPhone do you have, and are you after just a charger or a
cable too?"*. A customer who owns nothing yet was being asked to identify a device they had
not bought.

So the composer policy gained one rule: when the customer is choosing what to buy rather
than fixing what they own, recommend first. Name two or three products that fit what they
have already said, and if something is still missing, ask for it after the recommendation
rather than instead of one.

| | `p01` *understood* |
|---|---|
| before, 5 runs | 1, 1, 1, 0, 1 |
| after, 3 runs | 3, 2, 2 |

The distributions do not overlap: every score after the change is higher than every score
before it. Three runs is a small sample and this is one conversation, but the metric had no
variance at all beforehand, which is what makes the separation readable. Worth re-measuring
before anyone treats it as settled.

### Still open

1. **Cards are qualifier-blind.** "The cheapest self-emptying one" and "the smallest to carry"
   return the right category in roughly the right order, but nothing filters or reorders by the
   qualifier, and a follow-up can introduce models the customer has not seen. `shown_skus`
   already exists; the natural next step is to let a referential follow-up narrow the shelf the
   way it already narrows the picker.
2. **"We don't sell that" still shows a shelf.** Asked for an iPhone case, the search finds
   iPhone accessories, so the prose says one thing and the cards say another.
3. **Spec inference is reduced, not gone.** The policy rule helps; a guard that checks
   compatibility claims the way G3 checks prices would help more.
4. **The scenario suite's rubric judge is down.** `eval_run.py` scored 1 of 20 judged cases:
   the RKAPI account is at `-$0.001`, so 19 came back 403. Structural checks are unaffected
   (27/27), but the rubric numbers in that suite are currently blind. Unrelated to this work.

---

## Cost

| | credits | note |
|---|---|---|
| thirteen pilot runs | ~0.70 | 39 turns each, three of them the shopping half only |
| `eval_run.py` | 0.037 | 0.0014 per turn |
| `eval_conversations.py` | 0.090 | 64 turns |

The agent side is effectively free at this scale. The judge is the spend: roughly 220 GPT-5
calls at maybe 10k input tokens each, which is an estimated **$3 to $4 of real money**. The
API bill is not readable from here, so treat that as arithmetic rather than a meter reading. It
is inside the $10 cap either way, and latency, not money, is what limits how many runs fit in a
night: p50 11s per turn, p95 29s.

## Re-running it

```bash
python scripts/pilot_human.py                 # all nineteen, judged
python scripts/pilot_human.py --no-judge      # checks only, no OpenAI spend
python scripts/pilot_human.py --name p01      # just the reported conversation
python -m pytest tests/test_pilot_shopping.py # the regressions it produced
```

Reports land in `logs/pilot_human.md` with every transcript, and the raw JSON beside it.
