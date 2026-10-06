# Search algorithm (simulation-optimisation of the policy)

*Status: October 2026 (search v3). The policy is unchanged (age-aware capped (s,S) + week-1
lookahead, `docs/policy_choice.md`); only the way its levels are tuned changed. v2 tuned an
s and an S per item and week directly; v3 tunes a few forecast-scaled parameters per item and
adds week-specific exceptions only where they pay.*

## 0. Decomposition into independent groups (`meio/decompose.py`)

Before the search, products are split into groups that share nothing: two products are in the
same group if they are made at the same production site (shared capacity and closed weeks) or
use a common raw material (shared RMW stock, supplier orders). Union-find over these links gives
the connected components. Each group is a complete sub-model (its products, their materials and
sites, demand and initial state) and is searched on its own with its own search and hold-out
seeds; the evaluation window stays the global one (`evaluation_start`). The schedules, margins,
parameters and logs are merged afterwards; lookahead and the final verdict run on the full model.
Constraints across products that are added later (e.g. a joint MOQ for several items) become one
more link type, so linked products stay in one group.

## 1. What is optimised

Objective: the mean total cost over the horizon on the search seeds. Hard constraint (chance
constraint): in every fill-rate cell (product × channel × week of the evaluation window) the share
of futures whose fill rate reaches F is at least α (default 98 %); search rule
`share − Z·SE − margin ≥ α`, hold-out and final verdict as in `CLAUDE.md`.

**Search variables (v3).** The (s,S) levels of every week follow from the forecast and a few
parameters per item (`meio/parametric.py`):

| Item | Parameter | Meaning |
|---|---|---|
| finished good (DC) | `z` safety factor | s_t = Φ(z)-quantile of the demand over the protection interval t … t+τ_p+L̃ (random lead time, Monte Carlo) |
| | `cover` | lot S_t − s_t = mean forecast of `cover` weeks after that interval (fractional weeks pro rata) |
| | `min_lot` | smallest lot: 0 or a price-break quantity (all-units discounts) |
| | `cap` | order cap in weeks of mean demand (never below the largest lot) |
| | `end` | end of horizon: S_t ≤ Φ(z + end)-quantile of the demand left until week H |
| raw material (RMW, echelon) | `z`, `cover`, `end` | the same on the BOM-weighted demand over t … t+G̃+τ_p+L̃ |
| | `floor` | minimum physical RMW stock in weeks of mean use |

That is 5 numbers per finished good and 4 per raw material: 21 for the single-product examples
(v2: 125 week-specific levels and limits for the old one-product default, 245 for the built-in
36-week example) and 56 for the four-product example (v2: 363). Week-specific exceptions (below)
are added only where a parameter cannot express them.

**Why these parameters (literature).**

- *Safety stock that follows the forecast.* For non-stationary demand the target should come from
  the forecast over the lead time that precedes the protected week, with a fixed service factor;
  "forward days of coverage" targets lower service in seasonal ramps (the landslide effect)
  [Neale & Willems 2009, 2015]. Stationary safety-stock locations with levels that change with the
  demand work well in multi-echelon chains [Graves & Willems 2008]. → one `z` per item, the levels
  move with the forecast and its uncertainty.
- *Stationary parameters on the local forecast.* Non-stationary (s,S) policies are approximated
  well by stationary parameters applied to the near-future forecast [Silver 1978; Bollapragada &
  Morton 1999; Tarim & Kingsman's (R,S) policies]. → a lot `cover` in weeks of forecast instead of
  a lot size per week.
- *Perishability.* The order-up-to level is compared with the position net of the expected waste
  (EWA policy, Broekmeulen & van Donselaar 2009; Pauls-Worm et al. 2014; the "base stock with
  estimated waste" benchmark in De Moor et al. 2022) - the policy already does this - and the lot
  must sell within the usable shelf life of the strictest channel, which bounds `cover`
  [Gutierrez-Alcoba et al. 2017].
- *Finite horizon.* Nothing after week H is costed or served, so an order-up-to level above what
  can still sell by H is pure cost; finite-horizon optimal base-stock levels fall towards the end.
  → the ceiling (remaining-demand quantile) and the `end` shift.
- *Search method.* Simulation-based optimisation of a few policy parameters per stage with common
  random numbers [Fu 2002; Köchel & Nieländer 2005]; service-constrained increments by greedy
  marginal analysis (largest service gain per unit of cost, as in METRIC-type allocation
  [Sherbrooke 1968]); candidate comparison with racing and confirmation on independent replications
  (ranking & selection / OCBA idea [Chen et al. 2000]).

## 2. The algorithm

```
MULTI-START   classic start (z at the strictest fill-rate target, 1 week of cover = the simple
              heuristic, also the reported baseline), economic-lot start (EOQ cover, bounded by
              shelf life), price-break starts (min_lot = each discount break)
              -> global repair, one coarse line-search pass ("race"); keep the best
              (feasible, fewest cells left out, smallest service gap, lowest cost)
PHASE A       parameters only:  repair (global)  ->  line search (grid halves every pass)
PHASE B       exceptions:       local repair of the deferred cells -> trim -> polish
HOLD-OUT      weak cells: margin, repair on the hold-out futures if the search seeds cannot
              see the risk, local repair, one finer line-search pass; up to max_outer_rounds
```

**Repair = greedy marginal analysis.** For the earliest failing cell all fixes that can feed it
are simulated at once (in parallel): in phase A the safety factor `z` or the `end` shift of the
product and of every raw material that cut its releases in any future, or the cap if it cut the
orders; in phase B (local) a raise of the DC levels of the release weeks that feed the cell
(pre-building before closed production weeks, or before capacity-bound weeks), of the RMW levels
of a binding material in the weeks whose deliveries can arrive in time, or the `end` shift. The
fix with the largest reduction of the total service gap per unit of extra cost wins (fixes that
move the cell itself first). A step without progress doubles the next step (a share measured on a
few hundred futures needs a visible change). A cell that no parameter can fix is *deferred* to the
local repair (and may not get worse meanwhile); one that no fix moves for `repair_patience` steps
is declared unfixable and its raises are undone.

**Line search.** Per parameter a grid of values around the current one (e.g. z ± 0.15 … 1.0,
cover ± 0.5 … 2 weeks, floor ×0 … ×1.25, all price breaks), simulated at once on the search seeds
(common random numbers); the cheapest feasible value that the confirmation seeds accept is kept.
A DC cover move also comes with the covers of its materials (echelon coordination). The grid halves
every pass; the search stops when a pass gains less than 0.05 %. A value that is at least 0.5 %
cheaper but breaks a few cells - a price-break lot, one supplier order less of an expensive
material - gets a short repair first and is kept if it is then still cheaper and confirmed
(large-neighbourhood move; a repair only adds cost, so it stops as soon as it cannot win).

**Exceptions (phase B).** The local repair, a trim (drop an item's offsets if that is cheaper and
still feasible) and one polish pass of the v2 block moves on the full schedule (quarters of the
horizon down to single weeks, at most 2 repeats) catch what no parameter can express: the first
weeks after the review (state-driven), pre-building before closed weeks, the last orders before
the horizon. The result is stored as sparse offsets on top of the parameter levels.

**Hold-out.** As in v2, a weak cell gets a margin. New: a cell that passes in (almost) all search
futures can still be weak on the larger hold-out set - its risk is too rare for 200 futures, so no
repair on the search seeds can see progress. Such a cell is repaired on the hold-out futures
themselves (the fix with the largest needed gain per unit of cost) until its share clears α by Z
standard errors. The final verdict stays on the untouched test futures.

**Confirmation seeds, service guard, parallel evaluation** as in v2: an accepted move may not make
a cell weak on 2 × search-seed independent futures; deferred and unfixable cells may not get worse
within a phase; candidates are simulated in worker processes with exactly the sequential result
(tested).

**Warm start.** The parameters are horizon-free, so the next weekly review can start from the last
one's parameters (`Searcher(start_values=...)`, `rolling_demo.py`) without shifting any schedule.

## 3. Benchmark (quick preset, same seeds; cost and failing cells on 2,000 test futures)

Quick preset, every instance alone with 3 worker processes (the app's setting), same seeds for
both searches. Cost = mean total cost over the horizon on 2,000 test futures (with the week-1
lookahead); failing cells = cells below α on those futures; time = search + lookahead.

| Instance | v2 cost | v3 cost | Δ cost | failing cells v2 → v3 | worst cell v2 → v3 | time v2 → v3 |
|---|---:|---:|---:|---|---|---|
| D0 one FG (old default) | 384,507 | 365,003 | −5.1 % | 0 → 0 | 99.3 → 98.7 % | 76 → 56 s |
| D1 D0 + random lead times | 397,319 | 381,224 | −4.1 % | 6 → 6 | 81.8 → 81.8 % | 117 → 111 s |
| D2 D0, demand sd × 1.6 | 437,352 | 395,327 | −9.6 % | 0 → 0 | 98.8 → 98.2 % | 58 → 110 s |
| B0 built-in (36 weeks) | 45,946 | 32,144 | −30.0 % | 0 → 1 | 98.3 → 97.9 % | 147 → 97 s |
| B1 B0, shelf life 10 | 72,567 | 69,466 | −4.3 % | 27 → 30 | 65.9 → 56.2 % | 344 → 188 s |
| B2 B0, sd × 1.6 | 66,014 | 35,457 | −46.3 % | 2 → 1 | 97.4 → 97.8 % | 109 → 109 s |
| B3 B0, large lots | 40,450 | 34,693 | −14.2 % | 0 → 0 | 98.9 → 98.8 % | 84 → 109 s |
| B4 B0, long random lead times | 55,240 | 46,371 | −16.1 % | 28 → 25 | 75.4 → 93.9 % | 405 → 252 s |
| M0 four FG (app default) | 1,226,828 | 1,180,311 | −3.8 % | 1 → 2 | 97.4 → 96.8 % | 896 → 430 s |
| M1 M0, sd × 1.3 | 1,222,488 | 1,239,670 | +1.4 % | 2 → 0 | 96.5 → 98.3 % | 787 → 401 s |

Total time 3,024 s → 1,863 s (−38 %); the four-product default runs about twice as fast. Cost is
lower on 9 of 10 instances (M1: +1.4 % with two more cells passing). B1 and B4 have many cells no
policy can serve (short shelf life, long lead times); v3 keeps far more service on B4 and is a
little weaker on B1. The single misses on B0 and M0 are cells the search could not fix within
the quick preset's two hold-out rounds (97–98 %); `standard` and `full` use more seeds and rounds.
Slower than v2: D2 and B3 (single product, short runs), where the repaired large moves and the
marginal-analysis repair cost more simulations than v2's single-direction raises.

## References

- Bollapragada, S., Morton, T. E. (1999). A simple heuristic for computing nonstationary (s, S)
  policies. *Operations Research* 47(4), 576-584.
- Broekmeulen, R., van Donselaar, K. (2009). A heuristic to manage perishable inventory with batch
  ordering, positive lead-times, and time-varying demand. *Computers & OR* 36(11), 3013-3018.
- Chen, C.-H., Lin, J., Yücesan, E., Chick, S. E. (2000). Simulation budget allocation for further
  enhancing the efficiency of ordinal optimization. *Discrete Event Dynamic Systems* 10.
- De Moor, B. J., Gijsbrechts, J., Boute, R. N. (2022). Reward shaping to improve the performance of
  deep reinforcement learning in perishable inventory management. *EJOR* 301(2).
- Fu, M. C. (2002). Optimization for simulation: theory vs. practice. *INFORMS J. on Computing*
  14(3), 192-215.
- Graves, S. C., Willems, S. P. (2008). Strategic inventory placement in supply chains:
  nonstationary demand. *MSOM* 10(2), 278-287.
- Gutierrez-Alcoba, A., Rossi, R., Martin-Barragan, B., Hendrix, E. M. T. (2017). A simple heuristic
  for perishable item inventory control under non-stationary stochastic demand. *IJPR* 55(7).
- Köchel, P., Nieländer, U. (2005). Simulation-based optimisation of multi-echelon inventory
  systems. *International Journal of Production Economics* 93-94.
- Neale, J. J., Willems, S. P. (2009). Managing inventory in supply chains with nonstationary
  demand. *Interfaces* 39(5), 388-399.
- Neale, J. J., Willems, S. P. (2015). The failure of practical intuition: how forward-coverage
  inventory targets cause the landslide effect. *POM* 24(4), 535-546.
- Pauls-Worm, K. G. J., Hendrix, E. M. T., Haijema, R., van der Vorst, J. G. A. J. (2014). An MILP
  approximation for ordering perishable products with non-stationary demand and service level
  constraints. *IJPE* 157, 133-146.
- Sherbrooke, C. C. (1968). METRIC: a multi-echelon technique for recoverable item control.
  *Operations Research* 16(1), 122-141.
- Silver, E. A. (1978). Inventory control under a probabilistic time-varying demand pattern.
  *AIIE Transactions* 10(4), 371-379.
- Tarim, S. A., Kingsman, B. G. (2004). The stochastic dynamic production/inventory lot-sizing
  problem with service-level constraints. *International Journal of Production Economics* 88.
