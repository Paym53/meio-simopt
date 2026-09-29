> **Status note (27 Sep 2026).** This is the design specification the prototype was built from.
> Since then the ordering policy has been extended: the model now uses a single policy, an age-aware
> (s,S) rule with a DC order cap and a minimum physical stock at the RMW, plus a week-1 lookahead that
> chooses the committed orders by simulation. See `docs/policy_choice.md` and the README. Sections 5,
> 8 (steps 3a and 3d) and 12 are therefore superseded where they describe the plain (s,S) rule.

# MEIO Simulation-Optimisation Model — Prototype Specification (v5)

*Perishable two-echelon supply chain with production, stochastic lead times, non-stationary demand, and time-specific (s,S) policies*

This document is the single reference for building the first prototype. It replaces all earlier MILP formulations: the model is now **only** a simulation plus a search over policy parameters.

---

## Contents

1. What the model does, in one page
2. Network and time structure
3. Index sets
4. Input parameters
5. Policy parameters (what the search optimises)
6. Operational decisions (what the policy produces each week)
7. State and output variables
8. Weekly mechanics (the simulation step)
9. Rules and constraints (complete list)
10. Cost accounting
11. Service measurement and feasibility verdict
12. Simulation-optimisation method
13. Rolling re-optimisation (every review week)
14. Assumptions
15. Limitations
16. Points still to confirm
17. Prototype checklist
18. Glossary of key terms

---

## 1. What the model does, in one page

A **raw-material warehouse (RMW)** buys raw materials from external suppliers. A **production facility (PF)** turns them into a finished good (FG); it holds no stock. A **distribution centre (DC)** stores the FG and serves several sales **channels**.

Both stock points hold **perishable** stock. All lead times are **random**, and demand is described by **week-specific probability distributions** that change over time (non-stationary).

Every week, two ordering rules decide what happens:

- **DC rule (s,S):** if the DC's inventory position is below $s^{DC}_{f,t}$, order FG from production up to $S^{DC}_{f,t}$.
  - Production is released immediately as far as raw materials and capacity allow; the rest of the order is cancelled.
  - The raw materials for the released quantity are transported to production in the same week.
- **RMW rule (s,S):** if the echelon inventory position of material $r$ is below $s^{RM}_{r,t}$, order from the supplier up to $S^{RM}_{r,t}$.

The **policy parameters** $s$ and $S$ are specific to product or material, location and week. The **search** looks for the schedule of $(s,S)$ values with the lowest expected cost, subject to every (week × channel) fill-rate cell passing the feasibility test in Section 11.

Performance is estimated by simulating thousands of random futures (**seeds**), each with its own demand and lead-time draws.

The model is re-run at **every review week** from the actual current state. Only the current week's decisions are executed:

- how much FG to order from production;
- how much to release, transport and produce;
- how much raw material to order.

**What the search trades off:**

- **More stock** gives better service, but more holding and waste.
- **More raw-material stock** (the central buffer) means fewer production cancellations, but costs raw-material holding and waste.
- **Larger orders** mean fewer fixed costs and better discount tiers, but more stock and waste.

---

## 2. Network and time structure

```
 Supplier r ──(G̃_r)──► RMW ──ship──► PF ──produce──► transport ──► DC ──► channels c
               random    stock r      no stock       └──────(L̃_f, random)──────┘   stock f
```

- **Time unit.** One week. The review period is $R=1$, so every week is a review week.
- **Supplier lead time $\tilde G_r$.** Random, in weeks, from supplier order to "usable at the RMW". Nominal value 10 weeks.
- **DC lead time $\tilde L_f$.** Random, in weeks, from production release to "usable at the DC". It includes transport RMW → PF, production and delivery to the DC. Nominal value 6 weeks.
- **$L^{\max}_f$ and $G^{\max}_r$.** The largest possible values of $\tilde L_f$ and $\tilde G_r$. When several products exist, $L_{\max}=\max_f L^{\max}_f$.
- **$L^{\min}_f$ and $G^{\min}_r$.** The smallest possible values.
- **Order-preserving arrivals.** Orders on the same lane never overtake each other. If an order is delayed, every later order on that lane arrives no earlier than it, so arrivals can bunch.
- **Look-ahead horizon.** $\mathcal T=\{1,\dots,H\}$, where week 1 is the current review week.
- **Evaluation window.** $\mathcal W=\{L_{\max}+1,\dots,H\}$. Only these weeks count for service. Earlier weeks are fully determined by stock and orders that already exist.

---

## 3. Index sets

| Symbol | Meaning | Prototype |
|---|---|---|
| $f\in\mathcal F$ | finished goods | 1 product (indices kept for later) |
| $r\in\mathcal R_f$ | raw materials in the bill of materials of $f$ | 4 materials |
| $c\in\mathcal C$ | sales channels | e.g. 3 |
| $t\in\mathcal T=\{1,\dots,H\}$ | weeks of the look-ahead | |
| $\mathcal W\subseteq\mathcal T$ | evaluation window, $t>L_{\max}$ | |
| $b\in\{1,\dots,\bar b^{\max}_f\}$ | FG age classes (age 1 = arrived this week) | |
| $u\in\{1,\dots,\bar u_r\}$ | usable RM age classes | |
| $g\in\mathcal G$ | discount tiers | e.g. 3 |
| $k\in\mathcal K^{\text{search}},\ \mathcal K^{\text{hold}},\ \mathcal K^{\text{test}}$ | three **disjoint** seed sets | |

---

## 4. Input parameters

### 4.1 Demand and service

| Parameter | Meaning |
|---|---|
| $\mathcal D_{f,c,t}$ | Forecast distribution of demand for product $f$, channel $c$, week $t$. It must be possible to sample from it. Weeks are independent; the distributions differ by week. |
| $F_{f,c}$ | Target fill rate of channel $c$ |
| $\rho_{f,c}$ | Minimum remaining shelf life channel $c$ accepts [weeks] |

### 4.2 Shelf life

| Parameter | Meaning |
|---|---|
| $A_f$ | FG maximum shelf life [weeks]. It starts on arrival at the DC. |
| $\bar b_{f,c}=A_f-\rho_{f,c}$ | Oldest age channel $c$ accepts |
| $\bar b^{\max}_f=\max_c\bar b_{f,c}$ | Oldest sellable age; older stock is waste |
| $A^{RM}_r$ | RM maximum shelf life [weeks]. It starts on arrival at the RMW. |
| $\rho^{RM}_r$ | Minimum remaining RM life at shipment (default 1, to survive the transport week) |
| $\bar u_r=A^{RM}_r-\rho^{RM}_r$ | Oldest RM age that may still be shipped |

**Age convention.** A unit of age $b$ has $A-b$ weeks of remaining life. It can serve channel $c$ if $A-b\ge\rho_c$, which is the same as $b\le\bar b_c$.

### 4.3 Structure and capacity

| Parameter | Meaning |
|---|---|
| $a_{r,f}$ | Units of $r$ per unit of $f$ (prototype: 1 for each of the 4 materials) |
| $\bar P_{t}$ | Production capacity in week $t$ [FG units] |
| $\mathcal T^{\text{prod}}$ | Weeks in which production may be released |
| $\mathcal T^{\text{ord}}_r$ | Weeks in which supplier $r$ accepts orders (prototype: all weeks) |
| $\bar O_{r,t}$ | Supplier capacity per order (optional; default unlimited) |

### 4.4 Lot-sizing restrictions

| Parameter | Meaning |
|---|---|
| $BA_f$, $MOQ_f$ | FG batch size and minimum order quantity. $MOQ_f$ is a multiple of $BA_f$. |
| $BA^{RM}_r$, $MOQ^{RM}_r$ | RM batch size and minimum order quantity |

### 4.5 Lead times

| Parameter | Meaning |
|---|---|
| $p^{L}_f(\ell)$ | Probability that the DC lead time equals $\ell$ weeks, $\ell\in[L^{\min}_f,L^{\max}_f]$ |
| $p^{G}_r(\ell)$ | Probability that the supplier lead time equals $\ell$, $\ell\in[G^{\min}_r,G^{\max}_r]$ |

### 4.6 Costs

| Parameter | Meaning | Charged on |
|---|---|---|
| $h_f$ | FG holding cost per unit per week | end-of-week DC stock |
| $w_f$ | FG waste cost per unit | scrapped FG |
| $K_f$ | Fixed cost per production release | weeks with $P_{f,t}>0$ |
| $Pr_{f,g}$ | Production (conversion) cost per unit in tier $g$ | released units $P_{f,t}$ |
| $Tr_{f,g}$ | PF → DC transport cost per unit in tier $g$ | released units $P_{f,t}$ |
| $[\underline B^{Pr}_{g},\overline B^{Pr}_{g}]$, $[\underline B^{Tr}_{g},\overline B^{Tr}_{g}]$ | Quantity bands of the tiers. They are contiguous; the tiers of production and transport are independent. | |
| $c^{RM}_r$ | RM purchase cost per unit | ordered units $O_{r,t}$ |
| $K^{RM}_r$ | Fixed cost per supplier order | weeks with $O_{r,t}>0$ |
| $h^{RM}_r$ | RM holding cost per unit per week | end-of-week RMW stock |
| $w^{RM}_r$ | RM waste cost per unit | scrapped RM |
| $c^{T}_r$ | RMW → PF transport cost per unit | shipped units $T_{r,t}$ |

$Pr$ contains **no material cost**; materials are paid through $c^{RM}_r$.

### 4.7 Initial state (read at every review)

| Input | Meaning |
|---|---|
| $I^0_{f,b}$ | FG units on hand at the DC, by age |
| $J^0_{r,u}$ | RM units on hand at the RMW, by age |
| FG pipeline | Released production not yet arrived: quantity, release week, and the earliest possible arrival given the elapsed time |
| RM pipeline | Supplier orders not yet arrived: quantity, order week, and the earliest possible arrival |

### 4.8 Search settings

| Setting | Meaning | Example |
|---|---|---|
| $n^{\text{search}}$, $n^{\text{hold}}$, $n^{\text{test}}$ | Seeds per set | 2,000 / 2,000 / 10,000+ |
| $Z$ | Safety multiplier in the search-time feasibility test | 2.0 |
| $m_{f,c,t}$ | Cell margin; starts at 0 | 0 |
| $\Delta^{m}$ | Minimum margin bump for a weak cell | e.g. 0.005 |
| $H$ | Look-ahead length | at least $G_{\max}+L_{\max}+$ a few weeks |

---

## 5. Policy parameters (what the search optimises)

For every product or material, location, and week:

| Location | Parameters | Meaning |
|---|---|---|
| DC, product $f$ | $s^{DC}_{f,t},\ S^{DC}_{f,t}$ | Reorder level and order-up-to level for FG, measured on the DC inventory position |
| RMW, material $r$ | $s^{RM}_{r,t},\ S^{RM}_{r,t}$ | Reorder level and order-up-to level for RM, measured on the **echelon** position of $r$ (Section 7) |

**Rules for the parameters**

- $0\le s_t\le S_t$, and both are integers.
- DC parameters only matter in weeks where a release can still arrive within the horizon: $t\le H-L^{\min}_f$. In later weeks no production is released.
- RM parameters only matter for $t\le H-G^{\min}_r-L^{\min}_f$. In later weeks no RM is ordered.
- **Size of the search space** (prototype with 1 product, 4 materials, $H=40$): about $2\cdot40+4\cdot2\cdot40=400$ integers. This is acceptable for the prototype. Reducing it is part of the later algorithm work (Section 15).

**Why the schedule is week-specific.** The forecast distributions change from week to week, and so do capacities and calendars. The best levels therefore change too. The link to the forecast is built into the evaluation: the search is scored on demand drawn from the forecast distributions. When the forecasts are sharper, lower levels already pass the fill-rate test, so the optimised schedule becomes cheaper.

---

## 6. Operational decisions (what the policy produces each week)

These are the **key decision variables**. In each seed and week they follow from the parameters and the current state via the rules in Section 8. At a real review, the week-1 values are committed.

| Variable | Meaning |
|---|---|
| $Q_{f,t}$ | FG ordered from production by the DC |
| $P_{f,t}$ | FG actually released to production: $Q$ capped by available RM and capacity. The remainder is cancelled. |
| $T_{r,t}$ | RM transported RMW → PF, equal to $\sum_f a_{r,f}P_{f,t}$, in the same week |
| $O_{r,t}$ | RM ordered from the supplier |

**When production takes place.** Production starts with the release in week $t$ and its output reaches the DC in week $t+\tilde L_f$ (order-preserving). The time spent in transport and production is part of $\tilde L_f$.

---

## 7. State and output variables (per seed $k$, week $t$)

### 7.1 Stock at the DC

| Variable | Meaning |
|---|---|
| $I_{f,b,t}$ | FG on hand of age $b$ at the end of week $t$ |
| $\text{Pipe}^{DC}_{f,t}$ | Released FG not yet arrived |
| $IP^{DC}_{f,t}=\sum_b(\text{on hand after receipts})+\text{Pipe}^{DC}_{f,t}$ | DC inventory position, used by the DC rule |

### 7.2 Stock at the RMW

| Variable | Meaning |
|---|---|
| $J_{r,u,t}$ | RM on hand of age $u$ at the end of week $t$ |
| $\text{Pipe}^{RM}_{r,t}$ | Supplier orders not yet arrived |
| $EIP_{r,t}=\text{RM on hand}+\text{Pipe}^{RM}_{r,t}+\sum_f a_{r,f}\,IP^{DC}_{f,t}$ | Echelon inventory position, used by the RMW rule |

**Why the echelon position.** RM already converted into FG (at the DC or in the pipeline) still counts as "covered". The RMW rule therefore reacts to the end-customer situation, not to the lumpy production orders it happens to receive. Internal moves (shipping, producing) do not change $EIP$; cancellations and waste lower it.

### 7.3 Flows and outcomes

| Variable | Meaning |
|---|---|
| $S_{f,c,b,t}$ | Units of age $b$ sold to channel $c$ |
| $V_{f,c,t}$ | Lost sales of channel $c$ |
| $W_{f,t}$, $W^{RM}_{r,t}$ | Scrapped FG and RM |
| $X^{\text{canc}}_{f,t}=Q_{f,t}-P_{f,t}$ | Cancelled production quantity |
| $\text{fill}_{f,c,t,k}=1-V/d$ | Fill rate of the cell in seed $k$ (defined only if $d>0$) |
| $\text{Cost}_k$ | Total cost of seed $k$ over the horizon |

---

## 8. Weekly mechanics (the simulation step)

For each seed $k$ and week $t=1,\dots,H$, the following steps run in this order.

### Step 1 — Ageing (start of week)

- All FG and RM units still on hand become one week older.
- Units that left the usable range were already scrapped at the end of the previous week (Step 6).

### Step 2 — Receipts

- **RMW:** supplier orders whose arrival week equals $t$ enter at age 1.
- **DC:** production releases whose arrival week equals $t$ enter at age 1.
- Goods received this week can be used this week.

### Step 3 — Review and decisions (every week)

**3a. DC ordering** (only if $t\le H-L^{\min}_f$ and $t\in\mathcal T^{\text{prod}}$; otherwise $Q_{f,t}=0$).

1. Compute $IP^{DC}_{f,t}$: all sellable FG on hand plus the FG pipeline.
2. If $IP^{DC}_{f,t}<s^{DC}_{f,t}$:
   - raw need $=S^{DC}_{f,t}-IP^{DC}_{f,t}$;
   - round **up** to a multiple of $BA_f$;
   - if the result is below $MOQ_f$, raise it to $MOQ_f$;
   - that is $Q_{f,t}$.
3. Otherwise $Q_{f,t}=0$.

**3b. Production release with cancellation.**

$$P_{f,t}=\min\Big(Q_{f,t},\ \min_{r\in\mathcal R_f}\Big\lfloor\frac{\text{usable RM}_{r,t}}{a_{r,f}}\Big\rfloor,\ \bar P_t\Big)$$

- Usable RM means ages $u\le\bar u_r$.
- $Q_{f,t}-P_{f,t}$ is cancelled and forgotten; the DC rule re-orders next week if still needed.
- Batch size and MOQ are **not** re-applied to the reduced quantity (Q2 in Section 16).
- The released quantity $P_{f,t}$ gets a lead-time draw $\ell$ from $p^L_f$. Its arrival week is
  $$\text{arr}=\max\big(t+\ell,\ \text{arrival week of the previous release}\big)$$
  (order-preserving). It joins the FG pipeline.

**3c. RM transport.**

- Ship $T_{r,t}=\sum_f a_{r,f}P_{f,t}$ from the RMW, **oldest usable age first**.
- All materials travel together, and the shipped RM leaves the RMW books.

**3d. RMW ordering** (only if $t\le H-G^{\min}_r-L^{\min}_f$ and $t\in\mathcal T^{\text{ord}}_r$; otherwise $O_{r,t}=0$).

1. Compute $EIP_{r,t}$ **after** 3b–3c: RM on hand + RM pipeline + $a_r\times$(FG on hand + FG pipeline, including this week's release).
2. If $EIP_{r,t}<s^{RM}_{r,t}$:
   - raw need $=S^{RM}_{r,t}-EIP_{r,t}$;
   - round up to $BA^{RM}_r$, raise to $MOQ^{RM}_r$ if needed, cap at $\bar O_{r,t}$;
   - that is $O_{r,t}$.
3. The order gets a lead-time draw from $p^G_r$ and an order-preserving arrival week, then joins the RM pipeline.

### Step 4 — Demand and allocation

1. Draw demand $d_{f,c,t}$ from $\mathcal D_{f,c,t}$.
2. Go through the age buckets from **oldest to youngest**.
3. Within a bucket $b$, serve the channels that accept that age ($b\le\bar b_{f,c}$) and still have open demand, in priority order:
   - tightest shelf-life requirement first (smallest $\bar b_{f,c}$);
   - on ties, the higher target fill rate $F_{f,c}$ first.
4. Each channel takes as much as it needs from the bucket.
5. Demand still open after all buckets is **lost**: $V_{f,c,t}$.

### Step 5 — Record service

For each channel, if $d_{f,c,t}>0$:
$$\text{fill}_{f,c,t,k}=1-\frac{V_{f,c,t}}{d_{f,c,t}}.$$

### Step 6 — End of week

1. **FG waste:** units on hand at age $\bar b^{\max}_f$ can serve no channel next week, so they are scrapped: $W_{f,t}$.
2. **RM waste:** units at age $\bar u_r$ could not be shipped next week, so they are scrapped: $W^{RM}_{r,t}$.
3. **Holding cost** is charged on the remaining stock (after waste removal).
4. Record all costs of the week (Section 10).

---

## 9. Rules and constraints (complete list)

Every constraint below is enforced **inside the simulation logic**; none is a separate optimisation constraint. The only constraint handled by the search is service (C20).

| # | Rule | Where |
|---|---|---|
| C1 | Stock never negative; sales, shipments and waste are limited by what is physically present | Steps 3–6 |
| C2 | A unit ages exactly one week per week; arrivals enter at age 1 | Steps 1–2 |
| C3 | Receipts are usable in the week of arrival | Step 2 |
| C4 | FG shelf-life gate: channel $c$ only receives ages $b\le\bar b_{f,c}$ | Step 4 |
| C5 | FG waste: unsold units at age $\bar b^{\max}_f$ are scrapped at week end | Step 6 |
| C6 | RM shelf-life gate: only ages $u\le\bar u_r$ may be shipped | Step 3b–3c |
| C7 | RM waste: unshipped units at age $\bar u_r$ are scrapped at week end | Step 6 |
| C8 | FIFO withdrawal at both locations (oldest eligible first) | Steps 3c, 4 |
| C9 | Channel priority within an age bucket: tightest shelf-life requirement, then higher $F$ | Step 4 |
| C10 | Lost sales, no backorders | Step 4 |
| C11 | DC (s,S) ordering rule, with batch rounding and MOQ | Step 3a |
| C12 | Production only in allowed weeks, and within capacity $\bar P_t$ | Step 3a–3b |
| C13 | Bill of materials: releasing 1 FG unit consumes $a_{r,f}$ units of every $r$ | Step 3b–3c |
| C14 | No production without material: the release is capped by usable RM; the shortfall is cancelled | Step 3b |
| C15 | No stock at production: RM is shipped in the release week and consumed on arrival | Step 3c |
| C16 | RMW (s,S) ordering rule on the echelon position, with batch rounding, MOQ and supplier capacity | Step 3d |
| C17 | Supplier orders only in allowed weeks | Step 3d |
| C18 | Stochastic, order-preserving lead times on both lanes | Steps 3b, 3d |
| C19 | No orders whose earliest possible arrival lies beyond the horizon | Steps 3a, 3d |
| C20 | Service: every (week × channel) cell in $\mathcal W$ must pass the feasibility test | Section 11 |
| C21 | Parameter validity: $0\le s_t\le S_t$, integers | Section 5 |

---

## 10. Cost accounting (per seed, summed over all weeks)

$$
\text{Cost}_k=\sum_t\Bigg[\sum_f\Big(\underbrace{h_f\,\text{FG on hand}}_{\text{holding}}+\underbrace{w_f W_{f,t}}_{\text{waste}}+\underbrace{K_f\,\mathbb 1[P_{f,t}>0]}_{\text{fixed}}+\underbrace{Pr_{f,g(P)}\,P_{f,t}+Tr_{f,g'(P)}\,P_{f,t}}_{\text{tiered, all-units}}\Big)+\sum_r\Big(c^{RM}_rO_{r,t}+K^{RM}_r\mathbb 1[O_{r,t}>0]+h^{RM}_r\,\text{RM on hand}+w^{RM}_rW^{RM}_{r,t}+c^T_rT_{r,t}\Big)\Bigg]
$$

**Rules**

- **All-units tiers.** The tier $g(P)$ is the band containing $P$; its rate applies to **all** units. Production and transport tiers are looked up independently. At an exact band boundary, the cheaper tier is used.
- **What is charged on what.**
  - Production, transport and fixed production costs are charged on **released** units $P$, never on cancelled ones.
  - RM purchase cost is charged when the order is placed.
- **Nothing is charged** for FG or RM in transit. Stock left at week $H$ has no value and no penalty.
- **Objective.** Mean cost over the search seeds:
  $$\overline{\text{Cost}}=\frac1{n^{\text{search}}}\sum_k\text{Cost}_k.$$

---

## 11. Service measurement and feasibility verdict

### 11.1 Cell statistics

A **cell** is one (week $t\in\mathcal W$ × channel $c$) pair. For a given seed set, only seeds with positive demand in the cell are counted ($n_{c,t}$ of them):

$$\text{mean\_fill}_{c,t}=\frac1{n_{c,t}}\sum_{k}\text{fill}_{c,t,k},\qquad SE_{c,t}=\frac{\text{sd}_k(\text{fill}_{c,t,k})}{\sqrt{n_{c,t}}}$$

A cell with zero demand in every seed passes automatically.

### 11.2 Feasibility during the search (on the search seeds)

A cell is **feasible** when

$$\text{mean\_fill}_{c,t}-Z\cdot SE_{c,t}-\text{cell\_margin}_{c,t}\ \ge\ F_c.$$

- $SE$ is small with many seeds and large with few.
- $Z$ is the safety multiplier (this run: 2.0).
- $\text{cell\_margin}_{c,t}$ starts at 0 and is raised for any cell that the hold-out check finds weak.

A **schedule** of $(s,S)$ values is feasible when **every cell in the evaluation window** passes.

### 11.3 Hold-out check (on the hold-out seeds)

After a search round, the best feasible schedule is simulated on the hold-out seeds. A cell is **weak** if its hold-out $\text{mean\_fill}<F_c$. For each weak cell:

$$\text{cell\_margin}_{c,t}\ \leftarrow\ \text{cell\_margin}_{c,t}+\max\big(\Delta^m,\ F_c-\text{mean\_fill}^{\text{hold}}_{c,t}\big)$$

The search then continues with the stricter margins. Stop when the hold-out check finds no weak cell, or after a maximum number of rounds.

### 11.4 Final verdict (on the untouched test seeds)

The reported, honest rule is:

$$\text{mean\_fill}^{\text{test}}_{c,t}\ \ge\ F_c\qquad\text{for every cell in }\mathcal W.$$

- No $Z$, no margin.
- The test seeds are used **only once**, at the end.
- The safety margin is a search-time discipline, not a display trick.
- The report shows every cell's test mean fill, its standard error, and pass or fail.

### 11.5 Notes

- This per-seed mean-fill definition replaces the chance-type reading of v4.
- With many cells ($|\mathcal W|\times|\mathcal C|$), a few will look weak on the hold-out seeds purely by chance. The margin mechanism absorbs this, and the resulting schedule is slightly conservative. That is intended.

---

## 12. Simulation-optimisation method (baseline for the prototype)

The goal is a simple, correct, reproducible search. Algorithmic improvements come later.

### 12.1 Seeds and common random numbers

- **What a seed contains.** One complete random future: demand draws $d_{f,c,t}$ for all weeks and channels, **plus** one lead-time draw per lane per week, $\ell^{L}_{f,t}$ and $\ell^{G}_{r,t}$.
- **Why lead times are drawn per week.** They are indexed by week, not by order number. The same seed therefore gives the same random environment to every candidate schedule, even if the schedules order in different weeks (common random numbers). This makes cost differences between candidates much more precise.
- **Seed sets.** The three sets (search, hold-out, test) never overlap.

### 12.2 Initial schedule (distribution-based start)

For each week $t$, estimate by Monte Carlo from the forecast distributions and the lead-time distributions:

- $s^{DC}_{f,t}$ = the $q_0$-quantile of total demand over $[t,\ t+\tilde L_f]$;
- $S^{DC}_{f,t}$ = the $q_0$-quantile over $[t,\ t+\tilde L_f+m_0]$, rounded to at least $s+BA_f$;
- $s^{RM}_{r,t}$ and $S^{RM}_{r,t}$: the same with window $[t,\ t+\tilde G_r+\tilde L_f]$ (plus $m_0$ for $S$), multiplied by $a_{r,f}$.

Use independent draws here, not the search seeds. Typical starting values are $q_0\approx F$ and $m_0=1$–$2$ weeks. This start already reflects week-specific uncertainty and forecast accuracy.

### 12.3 Search loop

```
initialise schedule x from 12.2; cell_margin = 0
repeat (outer round):
    A. REPAIR  (make x feasible on search seeds)
       while some cell (t, c) in W fails:
           diagnose the earliest failing week t:
             - if many cancellations fed week t  → raise s^RM, S^RM for the weeks that feed it
             - else                              → raise s^DC, S^DC for release weeks t - L_max … t - L_min
           raise by one step (e.g. one batch); re-simulate
    B. IMPROVE (lower cost while staying feasible)
       for each parameter block (location × week), in a varying order:
           try decreasing s, S or the gap S - s by one step
           try increasing the gap S - s (fewer orders, better tiers)
           accept a move only if the schedule stays feasible AND mean cost decreases
       shrink the step size when no move is accepted; stop at the minimum step
    C. HOLD-OUT CHECK (11.3): bump margins of weak cells
until no weak cell or max rounds reached
FINAL: simulate x on test seeds → verdict (11.4) and report
```

**Practical notes**

- Accept moves on the **search seeds only**.
- Diagnostics are recorded per seed and week: cancellations, RM shortage, FG waste, lost sales.
- **Vectorisation.** Simulate all seeds at once, one array per age vector; the allocation step is a loop over age buckets and channels.
- **Reproducibility.** Fix all random seeds, and log every accepted move with its cost and feasibility.

---

## 13. Rolling re-optimisation (every review week)

1. **Read the current state:**
   - DC and RMW stock by age;
   - both pipelines with elapsed times;
   - new forecast distributions;
   - updated capacities, calendars, batch sizes and MOQs;
   - updated lead-time distributions.
2. **Pipeline arrivals.** For orders already under way, the simulation draws the *remaining* lead time conditional on the time already elapsed, and keeps them order-preserving.
3. **Warm start.** Shift last week's optimised schedule forward by one week, and fill the new last week from 12.2.
4. **Run the search** (Section 12) and the final verdict.
5. **Commit week 1**, computed from the known current state with the optimised $s_1$ and $S_1$:
   - $Q_{f,1}$ and the release $P_{f,1}$ (production start);
   - $T_{r,1}$ (transport to production);
   - $O_{r,1}$ (raw-material order).
6. **Log** the decisions, the schedule, cell statistics and costs.

---

## 14. Assumptions

1. Weekly time buckets with review every week; decisions are made at the start of the week, before demand is known.
2. Demand is independent across weeks and channels; its distribution per week is given (non-stationary).
3. Lost sales, no backorders.
4. Lead times are random with known distributions, independent across weeks and lanes, and order-preserving within each lane.
5. The four raw materials of a release travel together. Transport and production time are inside $\tilde L_f$.
6. The production facility holds no stock; RM is consumed on arrival there.
7. FG shelf life restarts on arrival at the DC, independent of the RM's age. RM only needs $\rho^{RM}_r$ weeks of remaining life at shipment.
8. Shelf life starts on arrival at each location; nothing ages in transit.
9. FIFO withdrawal and channel priority as in Step 4.
10. Shortfalls in production releases are cancelled, not backlogged.
11. All-units discounts on production and transport, based on the released quantity.
12. Unlimited storage at the DC and the RMW.
13. No value or penalty for stock remaining at the horizon end.
14. The search optimises week-specific $(s,S)$ schedules. Everything else follows from the rules.

---

## 15. Limitations

1. **Policy class.** The best $(s,S)$ schedule is found, not the best possible policy. The rules ignore stock ages (e.g. units about to expire still count in the position).
2. **No global optimality.** The search is a heuristic on noisy estimates, so the result is a good, validated schedule.
3. **High dimension.** About $2H$ DC and $2H\cdot|\mathcal R|$ RM parameters. Coordinate search is slow and may stop in local optima. To be improved later, e.g. shared parameters for identical materials, parameterised schedules, or better search algorithms.
4. **Independence.** Demand and lead-time independence ignores disruption periods and demand correlation.
5. **FG shelf life** is independent of RM age.
6. **Capacity** is applied in the release week, not in the actual production week.
7. **Horizon end effects.** Stock at $H$ has no value, and orders near the end are suppressed (C19).
8. **Service definition.** The mean of per-seed fill rates is not the same as the ratio of total served to total demand; weeks with small demand weigh equally.
9. **Integer granularity.** For small weekly demand, a single lost unit causes a large fill drop in that seed.
10. **Validation cost.** The full rolling procedure (re-optimising each week) can only be validated on a limited number of outer seeds.

---

## 16. Points still to confirm

- **Q1.** In cells where a seed has zero demand, is that seed excluded (as assumed) or counted as fill = 1?
- **Q2.** When a release is cut by RM or capacity, should the released quantity still respect the batch size and MOQ (round down, or cancel entirely if below MOQ)? Assumed: no.
- **Q3.** Should a delay also hold up the lane in weeks without an order? Assumed: no; only actual orders block later ones, with lead times drawn per order week.
- **Q4.** What does the supplier's "guaranteed 10 weeks" mean under randomness (an upper bound or a nominal value), and is delivery history available?
- **Q5.** Weak-cell rule: is "hold-out mean below $F$" the right trigger, and is $\Delta^m=0.005$ a sensible default?
- **Q6.** Should $L_{\max}$ be taken per product or globally? It only differs once several products exist.

---

## 17. Prototype checklist

1. **Data interfaces:**
   - samplers for $\mathcal D_{f,c,t}$, $p^L_f$ and $p^G_r$;
   - cost, tier, shelf-life, capacity and calendar tables;
   - the state reader.
2. **Simulator** (Section 8), vectorised over seeds.
3. **Unit tests:**
   - conservation per seed: FG arrivals = sold + scrapped + change in stock; the same for RM;
   - order preservation: arrival weeks never decrease along a lane;
   - allocation: a hand-computed case with 3 channels;
   - cancellation: RM shortage for exactly one material limits the release;
   - tier lookup at band boundaries;
   - echelon invariance: $EIP$ is unchanged by shipment and release, apart from cancellations and waste.
4. **Cell statistics** and the feasibility test (Section 11).
5. **Initial schedule** (12.2), then the search loop (12.3).
6. **Hold-out margin loop** and the final test verdict.
7. **Rolling wrapper** (Section 13) with warm start and commit.
8. **Report:** decisions, schedules, cell table (mean fill, SE, margin, pass/fail), cost breakdown, waste, cancellations.

---

## 18. Glossary of key terms

- **Seed.** One complete random future (demand and lead times).
- **Cell.** One (week × channel) combination in the evaluation window.
- **Inventory position (DC).** FG on hand plus FG released but not yet arrived.
- **Echelon position (RMW).** RM on hand, plus RM on order, plus the RM contained in FG on hand and in the pipeline.
- **Release.** The part of a DC order that production actually starts; the rest is cancelled.
- **Order-preserving.** Later orders never arrive before earlier ones on the same lane.
- **Cell margin.** An extra safety buffer on a cell's fill-rate test, added when the hold-out check finds that cell weak.
