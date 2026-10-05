# Search algorithm (simulation-optimisation of the policy)

*Status: October 2026. Replaces the single-start coordinate search described in
`docs/model_specification_v5.md` §12.3. The policy is unchanged (age-aware capped (s,S) +
week-1 lookahead, `docs/policy_choice.md`), except for the price-break round-up of DC orders.*

## 1. What is optimised

Decision variables per review: the week-specific levels s_t and S_t of every product (DC) and
raw material (RMW), one order cap per product and one RMW minimum per material. Objective: the
mean total cost over the horizon on the search seeds. Hard constraints: every fill-rate cell
(product × channel × week in the evaluation window) satisfies the search rule
`mean − Z·SE − margin ≥ F`. Hold-out and test rules are unchanged (CLAUDE.md).

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
forecast, so orders do not become nervous (no bullwhip from erratic levels).

**Restructure (iterated local search).** After the first improve phase, per product and price
break above its current typical lot: set every lot to the break (cap included), raise the
feeding RMW levels by 50 % or 100 % of the extra need, repair, improve once, and keep it only
if it is feasible, cheaper and confirmed (below).

**Confirmation seeds (optimizer's curse).** An independent set of 2 × the search seeds. Only
accepted moves are simulated on it: no cell may become weak there (mean fill < F, the hold-out
rule) that was not weak before. Restructure jumps must also be cheaper there.

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

## 5. Bullwhip

The KPI table reports a **bullwhip ratio** per stage (production releases, supplier orders): how
strongly the stage's orders react to demand uncertainty. Per seed, the orders over the whole order
window are summed, and so is the demand they serve (FG equivalents, shifted by the median lead
time); the ratio is the variance of the order totals across seeds divided by the variance of the
demand totals. Totals remove the lumpiness of lot sizing (lots of 10,000 units are not
"bullwhip"); what remains is amplification of demand uncertainty. About 1 = passed on
one-to-one, clearly above 1 = bullwhip. (Variance of weekly orders would mostly measure lot
sizes: a policy with economic lots would always look "nervous".) The RMW policy works on the echelon position (RM at the RMW, on
order and already inside FG), so it reacts to end-customer demand, not to the lumpy releases.

## 6. Benchmark

See the pull request and `README.md` for the before/after comparison on eight instances.
