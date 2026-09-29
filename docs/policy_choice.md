# Choice of ordering policy

*Status: 27 September 2026. Decision: policy variant C is the main policy of the model.*

*Update 29 September 2026: C is now the **only** policy in the code. Variants A and B, the `--variant` option
and `compare_policies.py` were removed. This document stays as the record of why C was chosen; the
comparison below can be reproduced from the git history (commit before the removal).*

## 1. The three policy variants

| Variant | DC rule | RMW rule | Orders committed this week |
|---|---|---|---|
| **A** plain (s,S) | order up to S if position < s | same, on the echelon position | from the rule |
| **B** age-aware capped (s,S) | position − expected waste; order capped at `dc_cap` | echelon position − expected waste; also orders if physical RM < `rm_floor` | from the rule |
| **C** B + lookahead (**main policy**) | as B | as B | chosen by simulating candidate quantities from the current state |

- **Expected waste.** Stock expected to expire before a new order can arrive. The window is the median lead time, and the projection uses the mean forecast under the same FIFO, shelf-life-gate and channel-priority rules as the simulation.
- **Order cap and RMW minimum.** One value per product and one per material. They are constant over the horizon and tuned by the search.
- **Lookahead.** It evaluates all allowed DC quantities first, then an RM grid around the rule's quantity for each material in turn. It picks the cheapest candidate that fails no fill-rate cell that the rule's own quantity passes.

## 2. Why this policy (literature)

| Finding | Source |
|---|---|
| Base-stock policies are asymptotically optimal for perishables when lifetime, demand volume, penalty or outdating cost grow large | Bu, Gong & Chao, *Management Science* |
| Simulation-tuned base-stock / (s,S) policies are within 2.49 % of optimal on three perishable benchmarks | Farrington et al. 2025, *Annals of OR* |
| Stock-age-aware policies beat age-blind ones; BSP-low-EW is close to optimal in virtually all of 11,177 scenarios | Haijema & Minner 2019, *IJPE* |
| Subtracting expected outdating from the inventory position (EWA) gives substantial cost reductions | Broekmeulen & van Donselaar 2009, *C&OR* |
| An age-based policy in a multi-echelon platelet chain, tuned by simulation optimisation, beats order-up-to and EWA | Duan & Liao 2013, *IJPE* |
| Capped base-stock performs well in lost-sales models and is asymptotically optimal as lead times grow | Xin 2021, *Operations Research* |
| Constant order quantities only do well with stationary demand, short shelf life and LIFO | Minner & Transchel 2010, *OR Spectrum* |
| Improving the policy by simulated lookahead (rollout) from the actual state reaches gaps of ≤ 0.2 % | Temizöz et al. 2025, *EJOR* (Deep Controlled Learning) |

A fixed per-period quantity plan (R, Qₜ) was rejected as the policy. It cannot react to realised demand, and it cannot create a central raw-material buffer (proof in the v3 design notes). The "optimise the orders directly" idea is used where it helps: in the week-1 lookahead.

## 3. Comparison run (`compare_policies.py`, removed since)

**Setup.** 300 search / 600 hold-out / 5,000 test seeds, the same seeds for every variant, 36-week horizon.
- **A:** tuned from the quantile start.
- **B:** tuned warm-started from A's result. B's policy class contains A.
- **B0:** B tuned from the quantile start.
- **C:** B's rule plus the lookahead.

Mean horizon cost per test seed (failing cells in brackets):

| Configuration | A | B | B0 | C |
|---|---|---|---|---|
| 1 Base | 31,608 (1) | 31,118 (1) | 33,717 (0) | 31,118 (1) |
| 2 Large lots | 36,307 (0) | 36,499 (0) | 36,483 (0) | 36,445 (0) |
| 3 Long, variable lead times | 49,869 (25) | 51,180 (20) | 42,342 (22) | 51,118 (20) |
| 4 Short FG shelf life | 69,142 (24) | 62,843 (21) | 55,463 (28) | 62,833 (21) |
| 5 High demand uncertainty | 39,042 (0) | 39,559 (1) | 36,184 (0) | 39,440 (1) |
| 6 Cheap RM holding | 30,379 (0) | 30,044 (1) | 32,394 (0) | 30,044 (1) |

**Rolling check.** Base configuration, 16 reality paths × 20 review weeks, weeks 9–20. Decisions are executed weekly, and the rule is shifted each week but not re-tuned. Mean realised cost per week:

| Variant | Cost per week | vs A |
|---|---|---|
| A | 1,325 | – |
| B | 1,307 | −1.4 %, not significant |
| C | 1,211 | **−8.6 %** (SE 1.4 %) |

C also has higher realised fill rates: Online 0.998 vs 0.993, Outlet 0.991 vs 0.978.

**Conclusions**
1. **C is the best policy in operation.** At a single review it is never worse than B, by construction.
2. **B's logic pays off under uncertainty and perishability.**
   - High demand uncertainty: −7 %.
   - Long, variable lead times: −15 %.
   - Short shelf life: −9 to −20 %.
   - Large lots: B and A tie.
3. **The search start changes results by 7–18 %** (B vs B0), as much as the policy does. Improving the search is the next step, starting with multi-start (several start schedules).
4. **Configurations 3 and 4 cannot reach Retail's 98 % target with any policy.** Freshness gate and lead times are incompatible.

## 4. Open points
- Search algorithm: multi-start, fewer parameters (for example forecast-scaled levels), better optimisers.
- A separate freshness target for the strictest channel, if Retail cells stay binding.
- Realistic holding costs: unit value × annual rate / 52 + storage cost per unit-week.
- Several finished goods sharing raw materials (the code already supports several products).
