# Search algorithm (simulation-optimisation of the policy)

*Status: October 2026. Replaces the single-start coordinate search described in
`docs/model_specification_v5.md` §12.3. The policy is unchanged (age-aware capped (s,S) +
week-1 lookahead, `docs/policy_choice.md`), except for the price-break round-up of DC orders.*

## 0. Decomposition into independent groups (`meio/decompose.py`)

Before the search, products are split into groups that share nothing: two products are in the
same group if they are made at the same production site (shared capacity and closed weeks) or
use a common raw material (shared RMW stock, supplier orders). Union-find over these links gives
the connected components. Each group is a complete sub-model (its products, their materials and
sites, demand and initial state) and is searched on its own with its own search and hold-out
seeds; the evaluation window stays the global one (`evaluation_start`). The schedules, margins
and logs are merged afterwards; lookahead and the final verdict run on the full model.

Why: the search effort grows with the number of levels and cells, and moves for one group
cannot change another group's cost or service. Example data: {FG1, FG2, FG3} (site S1, shared
RM_A … RM_F) and {FG4} (site S2, RM_G … RM_I). With a single group the result is exactly that
of the search on the full model (tested). Constraints across products that are added later
(e.g. a joint MOQ for several items) become one more link type, so linked products stay in
one group.

## 1. What is optimised

Decision variables per review: the week-specific levels s_t and S_t of every product (DC) and
raw material (RMW), one order cap per product and one RMW minimum per material. Objective: the
mean total cost over the horizon on the search seeds. Hard constraints (chance constraint): in
every fill-rate cell (product × channel × week in the evaluation window) the share of futures
whose fill rate reaches F is at least α (default 98 %); the search rule is
`share − Z·SE − margin ≥ α`, hold-out and final verdict as in CLAUDE.md.

## 2. Why the old search fell short

| Weakness | Effect |
|---|---|
| One start (quantile levels, one week of lot, cap = 2 weeks of demand) | blind to setup cost, holding cost, price breaks and shelf life; the result depended on the start by 7–18 % |
| Moves on one (item, week) at a time | a larger lot needs the cap **and** all S levels **and** the RMW levels to move together, so these moves were never tried |
| All-units discounts | the cost landscape has separate valleys (lots at 10,100 / 15,100 in the default data); small steps cannot cross the dearer band between them |
| Accept any move that is cheaper and feasible on 200 search seeds | with thousands of candidates some look feasible only by chance (optimizer's curse); fill rates per seed are skewed, so small samples overestimate them |
| Cells declared unfixable were ignored | cheaper moves could make their service arbitrarily worse |

## 3. The new search

```
MULTI-START   classic quantile start, economic-lot start (EOQ cover per item, bounded by
              shelf life), price-break starts (lot = each discount break of each product)
              -> repair each, one coarse racing pass each, keep the best
              (smallest total service gap, then lowest cost)
round 1       REPAIR -> IMPROVE (coarse-to-fine) -> RESTRUCTURE -> HOLD-OUT check
round 2+      REPAIR -> one finer IMPROVE pass  -> HOLD-OUT check
last round    weak cells left -> REPAIR + one IMPROVE pass with the raised margins
```

**Coarse-to-fine pattern search (improve).** Moves act on blocks of order weeks: the whole
horizon of an item first (its safety level and lot as a whole), then halves, quarters … down to
single weeks. Move types: lower s and S (less safety stock), lower / raise S (smaller / larger
lots), and the **coordinated echelon move** that changes the DC lot together with the RMW
(s,S) of the weeks that feed those releases (BOM × the same step). An accepted move is repeated
in the same direction while it pays off; the step halves after every pass; move types that found
nothing at a level are skipped in the next pass. Whole-block moves keep the levels following the
forecast, so orders do not become nervous through erratic week-to-week levels.

**Restructure (iterated local search).** After the first improve phase, per product and price
break above its current typical lot: set every lot to the break (cap included), raise the
feeding RMW levels by 50 % or 100 % of the extra need, repair, improve once, and keep it only
if it is feasible, cheaper and confirmed (below).

**Confirmation seeds (optimizer's curse).** An independent set of 2 × the search seeds. Only
accepted moves are simulated on it: no cell may become weak there (share of futures meeting F < α, the hold-out
rule) that was not weak before. Restructure jumps must also be cheaper there.

**Closed production weeks.** If every release week that could reach a failing cell is closed (site or product calendar), the repair raises the levels of the latest open order week before them (pre-build), instead of declaring the cell unfixable.

**Service guard.** Cells the repair cannot fix are excluded from the target, but no improve phase
may lower their service below its value at the start of the phase. Repair counts a step as
progress only if the cell's lower bound rises by ≥ 0.1 pp.

**Hold-out.** Larger hold-out sets (quick 1,000, standard 1,500, full 2,500 seeds): they are
simulated once per round, and fill rates per seed are skewed (mostly 100 %, rare deep
shortages), so small samples overestimate them.

**Parallel evaluation.** The candidate moves of a block are simulated in worker processes
(`n_workers`, default: CPU cores − 1, at most 4). The first improving move in the fixed order
wins, exactly as one after the other: results are identical (tested).

## 4. Policy addition: price-break round-up

With all-units discounts a slightly larger order can be cheaper in total (default data:
9,900 units × 1.90 = 18,810 vs 10,100 units × 1.30 = 13,130). The DC order is therefore raised to
the price-break quantity with the lowest total production + transport cost, never above the
order cap. No parameters; without discount bands nothing changes.

## 5. Benchmark

See the pull request and `README.md` for the before/after comparison on eight instances.
