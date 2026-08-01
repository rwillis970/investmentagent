# AI Investment Agent — Architecture and Delivery Plan v1.1

> **This file is canonical.** Edit the plan here. The formatted document
> (`AI Investment Agent Architecture v1.1.dc.html`) is derived from this file and is
> regenerated on request — do not edit it as the source of truth, and do not
> reconcile the two by hand. If they disagree, this file wins.
>
> Personal use only. Not investment advice.

| | |
|---|---|
| Document | Architecture & Delivery Plan v1.1 |
| Supersedes | v1.0 (retained for reference) |
| Deployment | Single laptop, local-first, cloud-portable |
| Day-14 target | Paper pilot, end to end. Live order ~Day 30 (§1.2) |
| Broker | Adapter interface; Robinhood not viable at Day 14 (§1.2) |
| Disabled | Options, crypto, short, margin, futures, forex, OTC |
| Pilot capital | $500, model budget $20/mo (§1.2) |
| Status | Draft for review |

Revised for a 14-day local production pilot with human approval on every order,
hybrid event-and-schedule cadence, configurable minimum holding period, controlled
self-improvement, and an extensible trade-capability registry. All v1.0 safety
controls retained; evidence-of-edge work moves after launch and keeps its kill
criterion.

---

## 1. What changes, and what the pilot proves

The change request is accepted in full on architecture and accepted with three named
exceptions on delivery (§14). Every safety control from v1.0 survives; what changes is
sequencing, cadence, deployment and capability configuration.

The single most important reframing to state plainly, since the rest of the document
depends on it: the Day-14 pilot validates *plumbing*, not *edge*. It demonstrates that
data arrives, materiality is detected, analysis is auditable, risk is enforced
deterministically, approval is required, orders are idempotent, state reconciles, and
every failure path lands on no trade. It demonstrates nothing about profitability,
because in fourteen days no point-in-time history exists to test against and no live
sample is large enough to measure. That is an acceptable trade — the working
application has value on its own — provided the distinction is never quietly lost.

### Three new risks introduced by this change request

**Pattern day trading and settlement.** A one-hour minimum hold makes same-day round
trips normal. In a margin account under $25,000 equity, four round trips in five
business days triggers a 90-day PDT restriction; in a cash account, selling before
settlement causes good-faith and free-riding violations. This is a hard regulatory
constraint, not a policy preference, and it must shape the minimum-hold defaults
(§4.4).

**Approval fatigue.** Human approval on every order is the central control, and it
degrades with volume. Twenty approval cards a day become rubber stamps within a week,
at which point the control exists on paper only. Approval throughput is therefore
treated as a rate-limited resource with a hard daily cap (§3.4).

**Short holds are tax-expensive.** Hour-to-day holds realise short-term gains at
ordinary income rates and generate wash sales that silently disallow losses. The lot
accountant and cooldown rules in §4.5 mitigate this; nothing eliminates it.

### 1.1 Change-impact map

| v1.0 section | Disposition | Change in v1.1 |
|---|---|---|
| §3 Strategy specification | Revised | Weekly-only cadence removed. Universe, breadth and turnover become risk-profile settings rather than fixed constants. Alpha hypothesis retained but demoted to a post-launch research track (§7.3). |
| §5 Evidence plane | Retained | Bitemporal append-only store unchanged. Paid point-in-time history deferred; the store accumulates its own forward history from Day 2. |
| §6 Extraction layer | Retained | Content-addressed cache unchanged and now doubles as the cost-control mechanism (§8.2). Adds event-triggered extraction alongside batch. |
| §7.1 Strategy function | Revised | Still pure, but now invoked by three triggers (event, routine, review) instead of one weekly window. Adds capability and holding policy as explicit inputs. |
| §7.2 Risk constrainer | Extended | Reserve basis becomes dual: settled cash executable, NLV as percentage basis, plus an absolute floor. Adds capability gate, holding-eligibility gate and PDT counter as pre-order constraints. |
| §8 Execution plane | Extended | Adds an approval token as a mandatory pre-submit input, laptop-lifecycle handling, and a signed pre-submit re-check against live price bounds. §8.3 restates invariant #2 at the interface level: cancel, close and replace are order kinds, not adapter methods. |
| §9 Evaluation governance | Revised | Pre-registration, experiment budget, deflated metrics and the project kill criterion all retained. Self-improvement is enabled from release but confined to the categories in §7.2. |
| §11 Technology | Revised | Single-VM assumption replaced by laptop-local with a documented cloud-migration seam (§8). |
| §12 Phases 0–5 | Replaced | Replaced by the 14-day backlog (§11). The v1.0 gates are not deleted — they move to a parallel post-launch validation track (§7.3) with the kill criterion intact. |
| New in v1.1 | Added | Cadence tiers (§3), holding policy (§4), trade capability registry (§5), approval protocol (§10), cost control plane (§8.2), laptop lifecycle (§8.1), PDT guard (§4.4). |

### 1.2 Decisions received, and what they change

Three answers arrived after the change request. Two of them alter the plan materially
and are recorded here rather than buried in a later section.

**Robinhood, with the option to change later.** Robinhood publishes no supported retail
trading API, and automated access is generally prohibited by its customer agreement. It
therefore cannot be the Day-14 execution broker without accepting an unquantified
account-termination risk on the account holding the capital. The requirement is honoured
a different way: the broker becomes a genuine swap seam — a single `BrokerAdapter`
interface with a paper simulator behind it, so any broker, Robinhood included, becomes a
drop-in the day a supported API exists, with no change to strategy, risk, holding,
approval or audit logic. Day 14 runs against the simulator; the first live adapter is
chosen at the review below.

**14 calendar days at 2–3 hours per day.** Roughly 35 working hours against a backlog
scoped for about 85. Rather than shrink scope silently, the target moves: **Day 14 is a
paper pilot, end to end** — collection, materiality, analysis, risk, holding gate,
approval token, simulated fill, reconciliation, audit — and the first live order moves to
approximately Day 30. Nothing is cut; the live adapter and the readiness review simply
land in the second half. The re-phased schedule is in §11.

**The Alpaca paper adapter moved ahead of the collectors (§11).** One API serves both
paper and live trading, so the adapter's HTTP and mapping logic — account and position
reads, settlement tracking, order submission, order lifecycle, idempotent resubmission
handling — is written once, generically enough to serve the eventual live class with no
rewrite, rather than as two separate builds at two separate points in the backlog. Building
the real Alpaca paper adapter (not just the abstract `BrokerAdapter` interface behind a
simulator) before the market-data/EDGAR/news collectors means capability gate 4 and the
approval-token consumption path are already live, tested code by the time Day 10 arrives —
so Day 10 is no longer "build the live adapter." It shrinks to what genuinely cannot be
pre-built: the live base URL, a separate keychain entry, re-authentication for activation,
and the import/authorization boundary tests proving the research package cannot reach live
credentials. Everything else Day 10 used to name — the pre-submit re-check of reserve,
capability, holding eligibility and the PDT counter — is inherited unchanged the moment the
live class subclasses the same `BrokerAdapter.submit()` the paper adapter already uses.

**$500 pilot capital.** Sensible for a functional test, and it makes the cost ratio
decisive: a $75 monthly model budget would be 15% of capital per month. The budget drops
to **$20/month with a $30 hard stop**, and the analysis cap falls accordingly. At this
size the pilot is explicitly a correctness exercise, not an investment — treat any P&L
over the first months as noise, because at $500 it is.

#### Still open — decide before the live adapter is built, not before Day 1

**Which broker actually executes.** Alpaca remains the recommendation: one API for paper
and live, fractional shares, no commission, and programmatic trading permitted rather
than tolerated. IBKR is the alternative. This decision is no longer blocking, because the
adapter interface means it can be made at Day 20 without rework.

**Cash or margin.** No longer a question you need to answer in advance: account posture
is now *detected* from the broker at reconciliation — equity, multiplier and the
pattern-day-trader flag — rather than declared in configuration. Config may still assert
a posture, and a mismatch between the assertion and the broker's reported state halts
trading rather than proceeding on an assumption. At $500 the PDT rule binds regardless, so
the day-trade guard is active from the first session.

---

## 2. Definition of production at Day 14

**In scope at Day 14**

- Laptop-hosted application, documented setup
- Paper and live modes, credential-isolated
- Continuous collection, event triggers, scheduled reviews
- Deterministic risk, reserve and capability enforcement
- Configurable minimum hold with audited early-exit path
- Human approval token required for every live order
- Idempotent submission, reconciliation, kill switch, restart recovery
- Cost metering against a monthly budget
- Playbook candidate generation without self-promotion
- One end-to-end approved live order at minimum size

**Explicitly not in scope**

- Any claim of demonstrated edge or positive expectancy
- Backtested validation (no point-in-time corpus yet)
- Unattended or autonomous live execution
- Options, crypto, shorting, margin, futures, forex, OTC
- Extended-hours or sub-minute execution
- Automatic promotion of any strategy or playbook
- Cloud deployment or high availability
- Multi-user control plane
- Meaningful capital at risk

Capital posture for the pilot: the smallest amount that makes the plumbing real. §14
recommends a specific figure. Scaling capital is gated on the post-launch validation
track, not on the pilot succeeding mechanically.

---

## 3. Cadence architecture

The requirement is continuous awareness without continuous model spend. The resolution is
a four-tier loop in which cost rises by roughly two orders of magnitude per tier and
frequency falls by the same factor. Only deterministic local code runs at high frequency;
the model is invoked when a numeric materiality threshold is crossed or a schedule fires.

| Tier | Work | Frequency | Model cost |
|---|---|---|---|
| T1 Collect | Poll prices, account, open orders; heartbeat freshness | 60 s (market hours) | none |
| T2 Watch | EDGAR and news feeds; dedupe; entity resolution; cheap classification | 5 min or push | Haiku tier |
| T3 Screen | Deterministic materiality score + eligibility gate — no model call, ever | 5–15 min | none |
| T4 Analyse | Structured research on a specific candidate; target portfolio; approval card | on trigger + schedule | Sonnet tier |

Gate chain: `T4 output → risk constrainer → capability gate → holding gate → PDT guard →
approval request → live pre-submit re-check → order`

T3 is the cost firewall: it is pure local arithmetic, so raising monitoring frequency
costs nothing, and only T3 can promote work into T4.

### 3.1 Configuration controls

All eight controls from Change Request §3.1 are adopted verbatim as independent settings,
plus three added here that the request implies but does not name.

```
max_model_analyses_per_day     : 8     # T4 budget, hard stop
max_approval_requests_per_day  : 4     # anti-fatigue cap, §3.4
max_day_trades_per_5_sessions  : 3     # PDT guard, §4.4
```

### 3.2 Materiality without a model in the loop

Change Request §13 asks how event materiality is defined and tuned without calling the
model on every tick. The answer is a deterministic score computed entirely from local
data, with the threshold calibrated to a budget rather than to intuition:

```
score = w1 * abs(ret_since_open) / atr_20
      + w2 * log(volume_so_far / median_volume_same_time)
      + w3 * filing_weight[form_type, item_codes]
      + w4 * earnings_proximity(t)
      + w5 * abs(ret_since_open - sector_ret) / atr_20      # idiosyncratic move
      - w6 * analyses_today / max_model_analyses_per_day   # budget brake

trigger if score >= threshold
         and symbol in eligible_universe
         and capability_allows(symbol)
         and not in_cooldown(symbol)
         and approvals_today < max_approval_requests_per_day
```

Filing weights are an explicit allowlist, not a heuristic: 8-K items 2.02, 4.02, 1.01,
5.02 and 7.01 carry weight; 10-K and 10-Q carry weight; routine ownership and
administrative forms carry none. This alone removes most feed volume before any scoring
happens.

Tuning is inverted from the usual approach. Rather than choosing a threshold and
discovering the cost, you declare the budget — a target number of analyses per day — and
the calibrator replays the last sixty sessions of collected events to solve for the
threshold that produces it. The budget brake term makes the system self-limiting within a
day: as the count approaches the cap, the effective bar rises, so the last analyses of the
day are spent on genuinely larger events. Re-calibration is a weekly job and is logged as
a policy version, because changing the threshold changes what the system trades.

### 3.3 Event-driven prompt requirements

Adopted as specified. Each approval request states why it is time-sensitive, carries
source timestamps, the observed price at analysis, the proposed order, confidence, bull
and bear cases, contradictory evidence, portfolio and reserve impact, and an explicit
expiry. Approvals are bound to a price band: if the quote moves outside the band, or new
evidence arrives above a configured delta, the token is invalidated and a fresh decision
is required. Low-materiality observations are batched into the next scheduled review
rather than interrupting.

### 3.4 Approval as a rate-limited resource

This is an addition, and it is the one place where the change request's own goals
conflict. Aggressive settings — five-minute screening, one-hour holds, event-driven
triggers — can generate far more approval requests than a person can meaningfully
evaluate, and an approval that is not meaningfully evaluated is not a control.

The system therefore treats your attention as a scarce, metered resource. A hard daily cap
on approval requests, defaulting to four. Competing candidates ranked by materiality so the
cap is spent on the largest opportunities rather than the earliest. Batching of anything
below the immediate-action bar into a single scheduled review. And a dashboard metric that
tracks median time-to-decision and approve rate — if median decision time falls below
roughly twenty seconds or the approve rate exceeds about ninety percent, the dashboard
flags probable rubber-stamping and recommends lowering the cap. Every approval also
records elapsed decision time in the audit log, which makes the control auditable after
the fact rather than merely nominal.

---

## 4. Minimum holding period

Adopted as specified, with ISO 8601 durations, independence from both analysis cadence and
risk profile, and the position-level fields from Change Request §4.2. Two additions
follow, both of which the requirement makes necessary: enforcement must be lot-level
rather than position-level, and short holds must be reconciled with day-trading
regulation.

### 4.1 Lot-level enforcement

A position built from three fills has three eligibility times, three governing policy
versions and three tax characters. Enforcing at position level would either block eligible
sells or permit ineligible ones. Eligibility is therefore computed per lot, and a partial
sell consumes only eligible lots:

```
lot.earliest_normal_exit_at = lot.opened_at
                            + lot.holding_policy.minimum_holding_period
# opened_at is the FILL timestamp from the broker, never order submit time
# policy version is captured at fill and frozen for the life of the lot

sellable_qty(symbol, t) = sum(lot.qty for lot in lots(symbol)
                              if lot.earliest_normal_exit_at <= t
                              and lot.settled)
```

Freezing the policy version at fill time matters: shortening the minimum hold must not
retroactively release positions that were opened under a longer commitment. Lengthening it
likewise does not trap existing lots. This is the same immutability principle as the
evidence store, applied to policy.

**Confirmed against a real Alpaca paper account (§13 probe, 2026-07-27; see
`scripts/fixtures/`, `agent/broker/alpaca.py`)**: there is no field anywhere in Alpaca's
cash-account API surface — not `/v2/account`, not Account Activities — that distinguishes
settled from unsettled cash. `lot.settled` above therefore cannot be read from the broker at
all; it can only be a locally computed expectation (fill time plus the account's known T+1
settlement lag), owned by whatever local ledger eventually tracks lots — that ledger is not
yet built. `AccountSnapshot.settled_cash`/`unsettled_cash` (`agent/broker/base.py`) are
consequently an approximation of the account's cash *total*
(`settled_cash = cash, unsettled_cash = 0.0`, always), not a source `sellable_qty` can
consult per lot; the broker simply does not expose the distinction this formula's
`lot.settled` term needs. Free-riding protection under a cash account (§4.4) is bound by the
same limit: it can only be enforced from the local ledger's own settlement expectations,
never from broker-reported state, because no broker-reported state carries it.

Settled-cash *reconciliation* is a separate, narrower check and is unaffected by this gap:
`agent.reconciliation.reconcile_settled_cash` compares the broker's cash total against a
local total by exact equality, never a tolerance (Option A of the options considered for
this check; see that module's own docstring for why no tolerance is introduced). That
verifies the two *totals* agree — it says nothing about, and cannot say anything about,
which individual lots are settled.

**Confirmed against a real Alpaca paper account (2026-07-28): exact equality was right, but
it must be exact `Decimal` equality, not exact binary-`float` equality.** Once the local
ledger this section's earlier revision predicted (`agent.ledger.Ledger`) was actually built
and run against a real fractional-share fill (0.027087234 shares), `reconcile_settled_cash`
halted on `broker reports settled cash 480.01, local figure is 480.010000529276` — not a real
discrepancy, but binary-`float` arithmetic's own representational noise on a fractional-share
computation, surfacing at the fifteenth decimal place. The fix is not a tolerance (Option A's
own reasoning against one stands unchanged — a tolerance reopens exactly the "what magnitude
counts as real" question exact equality exists to avoid); it is removing `float` from the
comparison entirely. Every money and share-quantity field this reaches — `AccountSnapshot`/
`Position`/`Execution`/`BrokerOrder` (`agent/broker/base.py`, `agent/broker/alpaca.py`,
`agent/broker/simulator.py`), `Fill`/`Lot`/`Ledger` (`agent/ledger.py`, `agent/holding.py`),
`LedgerStore`/`ExecutionQuarantineStore`'s on-disk rows (`agent/ledger_store.py`,
`agent/execution_quarantine.py`) — is now `decimal.Decimal`, constructed via the one shared
coercion rule in `agent/money.py` (never `Decimal(a_float)` directly, which would just
capture the float's own imprecision; always via `str()` first, or parsed directly from
Alpaca's own decimal-string API response). Integer minor units (e.g. cents) were considered
and rejected: the domain has no single natural scale — Alpaca prices carry three decimal
digits, fractional-share quantities carry up to nine — so picking one fixed scale would
reintroduce the same ambiguity a tolerance would. `reconcile_settled_cash`/
`reconcile_positions`'s own comparison logic is *unchanged* by this fix: two exact `Decimal`
values either agree exactly or they do not, so exact equality, the original design, still
holds without modification — only the type flowing through it changed. `agent.audit.AuditLog`
still rejects `Decimal` by design (`_assert_json_native`); the one call site that ever passed
money into an audit payload (`agent.startup.run_startup`'s `reconcile_account` row) now
stringifies it first. Two now-unnecessary `+ 1e-9` epsilon guards (one in
`agent.ledger.Ledger.record_fill`'s overdraw check, one in
`agent.broker.simulator.SimulatorBroker._submit_impl`'s cash/share-sufficiency checks) were
removed outright rather than widened, since exact `Decimal` arithmetic never produces the
binary rounding residue those guards existed to forgive. One boundary outside the money
system proper still compares a `StagedOrder`'s own (deliberately unconverted) `float` qty
against `agent.holding.sellable_qty`'s now-`Decimal` return (`agent/pipeline.py`'s holding
gate) — resolved with a single `float()` conversion at that one call site, preserving
`StagedOrder`'s existing float fields and its own pre-existing epsilon-tolerance semantics
unchanged, since that tolerance is a caller-supplied-quantity allowance unrelated to (and
untouched by) the settled-cash exact-equality invariant above.

**Lot disposal order — an internal `lot_id` does not control which lot Alpaca actually
sells (confirmed 2026-07-27, `agent/lot_selection.py`).** The `sellable_qty` formula above
was originally evaluated per lot in isolation — summing whichever individual lots happened
to be hold-eligible and settled, regardless of order. That is unsafe: if an older lot is
still inside its minimum hold while a newer lot (opened under a shorter policy version)
already happens to be eligible, a broker that disposes of lots in FIFO order will actually
consume the older, still-held lot first on any sell, *not* the lot our own bookkeeping
believed it was selling. The system could therefore record a normal, in-policy exit while
the broker's own disposal silently violated the very minimum hold the gate exists to
enforce. `sellable_qty` (`agent/holding.py`) now walks lots in the broker's actual disposal
order and sums only the maximal leading run that is settled and past its own hold — the
first ineligible lot blocks everything behind it, with no override or bypass path.

What Alpaca's actual disposal method is, established from primary sources before writing
any code (not assumed): the Customer Agreement
(`files.alpaca.markets/disclosures/library/AcctAppMarginAndCustAgmt.pdf`, V25.2026.06) does
**not** name a disposal method anywhere — its only relevant clause, §39 "Tax Reporting; Tax
Withholding," says only that cost-basis information will be reported to the IRS "in
accordance with applicable law." Alpaca's own product documentation does answer it:
["Position Average Entry Price
Calculation"](https://docs.alpaca.markets/us/docs/position-average-entry-price-calculation)
states that Alpaca uses Weighted Average for same-day (intraday) positions and Compressed
FIFO for end-of-day positions — same-day buys are compressed into one weighted-average lot,
then day-aggregates are consumed oldest-day-first. No specific-identification, HIFO, LIFO or
tax-optimised alternative is documented, and none is reachable through the order-submission
API (`docs.alpaca.markets/us/docs/orders-at-alpaca` lists no lot or tax-lot parameter of any
kind) — corroborated by Alpaca's own GitHub issue tracker
(`alpacahq/Alpaca-API#213`, "Selling from a specific lot": *"it's not something we can
handle on our end"*) and by multiple community forum threads (2020–2024) describing the
same fixed-FIFO, no-designation behaviour with no contradicting report found.

`agent/lot_selection.py`'s `LotSelectionPolicy` records this as a versioned, single
supported method (`BROKER_FIFO`); specific identification, HIFO, LIFO and tax-optimised
selection are enumerated but refuse to run (`UnsupportedLotSelectionPolicy`) rather than
being silently approximated as FIFO. One known, recorded gap: our own `Lot` is one lot per
BUY fill, so `disposal_order` approximates Alpaca's method as plain fill-time FIFO across
all open lots — it does not replicate Alpaca's same-day weighted-average compression across
multiple same-day buys. Not expected to bind for this pilot's shape (no same-day
pyramiding into one symbol), but a real approximation, not an exact replica, and named here
rather than assumed away. `agent.ledger.Ledger.disposal_records()` records, for every SELL
fill, both the lot our strategy intended and the lot the broker's actual disposal order
would consume first, so any divergence between the two is visible rather than silently
invisible.

**A partially-filled BUY diverges from Alpaca's own lot count too — strictly more
conservative, not a correctness gap (found reviewing the deferred defect list, 2026-07-30;
see `agent/fill_sync.py`'s own "WHY EACH BUY-FILL INCREMENT BECOMES ITS OWN LOT" section).**
The disposal-order finding above already named the general shape of this gap ("our own `Lot`
is one lot per BUY fill... it does not replicate Alpaca's same-day weighted-average
compression across multiple same-day buys") for the *pyramiding* case — several separate
same-day BUY orders into one symbol — and judged it unlikely to bind for this pilot's shape.
The narrower case here is more ordinary and does not require pyramiding at all: a *single*
BUY order that fills in several partial executions (a routine limit-order outcome, not an
edge case) becomes N independent `Lot` rows in this ledger, one per fill increment, each with
its own `holding_policy` clock starting at that increment's own `filled_at` (§4.1's own
formula above, `agent/ledger.py`'s `record_fill`) — while Alpaca compresses the same
same-day fills into a single weighted-average lot for its own accounting (cited above:
["Position Average Entry Price
Calculation"](https://docs.alpaca.markets/us/docs/position-average-entry-price-calculation),
no new claim added here). Strategy-side and broker-side lot counts genuinely diverge for
every multi-execution fill.

This is a fail-safe divergence, not a correctness gap. `sellable_qty` walks lots in disposal
order and sums only the maximal leading run that has individually cleared its own hold
(above) — the *whole* filled quantity is never eligible any earlier under N clocks than it
would be under one clock anchored to the same (earliest) fill, because every later
increment's own clock is an *additional* condition on top of, never a replacement for, the
earlier ones. N clocks can only withhold quantity a single compressed clock would have
already released (the later increments' own tail), never release quantity a single clock
would still be withholding — the same "strictly stricter, never looser" direction as the
Decimal-precision and disposal-order findings elsewhere in this section. No code change
follows from this: it is a documented, accepted divergence between strategy-lot and
broker-lot counts, not a defect.

**Externally-originated fills — quarantine, not a guess or a halt (found running the loop
against the real paper account, 2026-07-28).** `agent.fill_sync.sync_fills` recovers a BUY's
intended `holding_policy_version` and a SELL's intended `lot_id` from an `OrderRecord`
written when this system staged the order (§8.3). A trade placed directly in the broker's
own dashboard — normal operator behaviour, not an error — stages nothing, so neither value
exists. Refusing to guess either is correct; treating that refusal as fatal was not: the
execution never leaves the broker, so it re-triggered the same refusal on every subsequent
cycle, halting the scheduled loop forever with no path forward.

Two resolutions were considered. Auto-ingest under whichever holding-policy version is
"current" was rejected as the general answer, for one reason that outweighs its simplicity:
it is BUY-only. A SELL's missing value is a `lot_id`, which must name one real, specific,
already-open lot with enough remaining quantity — there is no analogous safe default, and
choosing the wrong one is a silent misbooking, not a conservative fallback. Adopting
auto-ingest for BUY would still leave SELL needing a second, different mechanism for the
same underlying problem (unresolved intent) — the kind of duplication this plan's control
architecture avoids elsewhere (one path from store to orders; one disposal-order
computation, above). The adopted answer instead handles both sides uniformly:
`agent.execution_quarantine.ExecutionQuarantineStore` quarantines the execution — the loop
keeps running, and the execution is neither recorded nor lost — pending one explicit,
permanent operator decision: **admit**, supplying the exact missing field (never any other),
or **reject**, excluding it from the ledger forever. Both the quarantine and the resolution
are recorded in the audit log. An admitted execution's `Fill` is written through the same
`Ledger.record_fill` validation as any other — an operator-supplied lot_id or policy version
that is wrong is refused exactly as any other caller's would be, never silently accepted.

This changes what "policy version captured at fill" (above) means for an admitted BUY: the
version is whichever one the operator names at *admission* time, which may be later than the
trade's real placement time at the broker — the gap bounded by how long the execution sat
quarantined, not by the reconciliation cadence alone. Whether this matters depends entirely
on whether the holding-policy version in force ever actually changes between those two
instants: today it does not, because the real entry point
(`scripts.run_agent.build_account_runtime`) registers exactly one version, always named
`"config"`, with no mechanism to swap it while a process runs — so the distinction is
currently moot in practice. It stops being moot the day a live version-swap mechanism is
built (not yet designed): a version is immutable once registered (`HoldingPolicyRegistry.
register`) and a lot's minimum hold is permanently frozen to whichever version resolves at
the moment its `Fill` is recorded (invariant #5) — an admission made after such a swap would
freeze the externally-placed lot under the *new* version, not the one actually in force when
the human placed the trade. Quarantine does not remove this ambiguity; it converts it from a
silent default into a conscious, audited, one-time human choice.

A manually-placed SELL hits the identical quarantine path (no resolvable `lot_id`) and is
worse in one respect the BUY case does not share: `Ledger.record_fill` accepts exactly one
`lot_id` per SELL fill and enforces that its quantity does not exceed that lot's remaining
balance. A real external sale spanning more than one lot has no single correct `lot_id` an
operator can supply at all — it cannot be admitted as one `Fill` under the ledger's current
one-lot-per-fill model without being split, by hand, into multiple synthetic fills with
invented per-lot quantities and fabricated fill ids, which this system has no built-in,
verifiable way to derive from a single broker execution. This is the same shape as the
already-named CLOSE/multi-lot gap (§8.3): a CLOSE-originated order submits as a plain SELL
with no single intended `lot_id` either, and its fills land on this exact path. Quarantine
does not solve that gap — an operator still cannot admit a multi-lot execution safely — but
it does mean a CLOSE order's fills, like a genuine external sell's, are no longer fatal to
the loop; they wait for an operator instead of halting it forever.

### 4.2 Early exit

All six exception categories from Change Request §4.3 are adopted. The mechanism has four
properties: an exception must name its category, the category must be evidenced by a
specific fact reference in the store, the approval card shows remaining normal hold time
alongside the exception reason, and the exception is recorded as its own audited object
rather than as a flag on the order.

Sequence:

1. Exit proposed for a lot where `now < earliest_normal_exit_at`
2. Holding gate rejects by default and emits an `EarlyExitRequest` — never a silent bypass
3. Request must carry category, fact reference, remaining hold time, and loss avoided vs
   cost incurred
4. Unevidenced category → rejected at the gate, audited, no card shown
5. Human approval required, with the same expiry and price-band binding as any order
6. PDT guard re-evaluated: an early exit that would trip a day-trade limit is blocked even
   when approved (§4.4)
7. Rate of early exits tracked weekly — a rising rate means the minimum hold is
   misconfigured, not that the exception works well

Step 7 is the honest check: an override used routinely has become the rule.

### 4.3 Stop losses under a minimum hold

These two requirements interact in a way worth stating explicitly. A stop-loss is an
exception category, so a stop event inside the hold window produces an approval request
rather than an automatic sell — which means a gap-down while you are asleep or away is not
protected by the minimum hold, only delayed by it. Two mitigations, both configurable: a
resting broker-side stop order placed at fill time for positions above a notional
threshold, which executes without needing local process or human presence; and a hard rule
that a stop-loss exception approval is the one card allowed to bypass the daily approval
cap. Anything else creates a scenario where a genuine emergency queues behind routine
requests.

### 4.4 Day-trade and settlement guard

A minimum hold measured in minutes or hours makes intraday round trips the norm, which
puts the pilot directly into regulated territory. The guard is a deterministic pre-order
check, evaluated alongside the reserve and capability gates:

| Account posture | Constraint | System behaviour |
|---|---|---|
| Cash account | Selling shares bought with unsettled funds causes good-faith violations; three in twelve months restricts the account for 90 days | Only settled lots are sellable — already enforced by `sellable_qty` in §4.1. Effective minimum hold becomes T+1 regardless of policy; the dashboard shows the binding constraint so the setting is not silently ignored. |
| Margin account under $25k | Four day trades in five business days triggers a 90-day pattern-day-trader restriction | Rolling five-session day-trade counter, capped at three. An order that would be the fourth is rejected pre-approval with the reason surfaced. The counter is reconciled against the broker's own count, not just computed locally. |
| Margin account over $25k | No PDT restriction, but margin remains DISABLED by capability policy | Counter still runs as an observability metric; settled-cash-only funding still enforced. |

The practical consequence for defaults: sub-day minimum holds are only coherent in a
margin account above the PDT threshold, and even then are limited by the day-trade counter.
Below that, `PT1H` is a setting the account cannot honour. The system accepts the
configuration and reports the effective binding constraint rather than pretending to
comply.

### 4.5 Cooldown and tax interaction

The trade cooldown from Change Request §3.1 does double duty. It prevents oscillation on a
symbol whose signal is flickering around a threshold, and when set to at least 31 days for
loss-making exits it prevents wash sales outright. The rebalancer already prefers
long-term lots and avoids repurchase inside the wash window; the cooldown makes it a policy
guarantee rather than a preference. At hour-scale holds, expect essentially all gains to be
short-term — the dashboard reports realised short-term gains and disallowed wash-sale
losses month to date, so the tax cost of the cadence setting is visible while it is being
chosen rather than in April.

---

## 5. Trade capability policy

Adopted as specified, with the five-state model, initial settings, order and session
policy, and enablement workflow from Change Request §5. The architectural commitment is
that no code anywhere says "stocks only"; every instrument decision reads a versioned
policy, and the policy is only writable through an audited change request that the agent
cannot initiate.

States, weakest to strongest:

| State | Meaning |
|---|---|
| DISABLED | no research path |
| RESEARCH_ONLY | analyse only |
| PAPER_ONLY | simulate only |
| APPROVAL_REQUIRED | live with approval |
| PRODUCTION_ALLOWED | eligible; approval still on in pilot |

**Forward transitions** — one step at a time, each requiring a `CapabilityChangeRequest`
with readiness evidence (Appendix A), passing integration and failure tests, a versioned
readiness decision and explicit human confirmation.

**Backward transitions** — to DISABLED, immediate, single-actor, no evidence required.
De-escalation must never be harder than escalation.

**Forbidden** — the agent, the extraction layer, the playbook optimiser and any
model-generated artefact cannot propose, request or effect a status change. Enforced by an
import boundary and a table-driven test over every dimension.

### 5.1 Where the gate sits

Capability is checked at four independent points, deliberately redundantly, because a
single gate is a single point of failure:

1. **Universe construction** — ineligible instruments never enter the candidate set, so the
   model is never asked about them.
2. **Risk constrainer** — a target weight on a non-allowed instrument is zeroed and audited
   as a policy violation, not silently dropped.
3. **Pre-submit check** — the order's asset class, side, funding type, order type, session
   and time-in-force are each re-verified against the policy version pinned in the run
   manifest.
4. **Adapter guard** — the broker adapter raises on any instrument whose class is not
   explicitly allowlisted, independent of everything upstream. This is the layer that
   catches a bug in the other three.

The proof requested in Change Request §13 is a table-driven test over the full
cross-product of asset class, side, funding, order type, session and time-in-force — every
combination not explicitly permitted must be rejected, with an assertion at each of the
four gates. It includes adversarial inputs: an OCC-format option symbol, a crypto pair, a
short side, an extended-hours session, a GTC time-in-force and an OTC ticker. The test
fails if any combination reaches the adapter's submit call, and it is wired to fail the
build rather than merely report.

---

## 6. Risk profiles and cash reserve

Change Request §13 asks for initial safe numeric defaults rather than deferral. Below are
proposed defaults, tightened from the change request's examples in three places where the
pilot's small capital and unvalidated strategy justify more caution: minimum holds respect
§4.4, new positions per day respect the approval cap, and drawdown pause thresholds are
given as explicit numbers.

| Setting | Conservative | Moderate | Aggressive | Platform max |
|---|---|---|---|---|
| Minimum normal hold | P14D | P2D | PT4H | PT15M floor |
| Min settled cash % of NLV | 30% | 20% | 10% | 5% floor |
| Absolute settled cash floor | $100 | $75 | $50 | $25 |
| Max position % NLV | 3% | 5% | 10% | 15% |
| Max sector % NLV | 15% | 20% | 25% | 35% |
| Routine decision interval | Daily | 4 hours | Hourly | 15 min floor |
| Max new positions / day | 1 | 3 | 5 | = approval cap |
| Drawdown pause (peak-trough) | 4% | 7% | 12% | 20% |
| Trade cooldown per symbol | P30D | P5D | P1D | PT1H |
| Trade approval | Required | Required | Required | Not relaxable in pilot |

Two notes on the table. Aggressive is `PT4H` rather than `PT1H` because a four-hour hold
placed in the morning usually resolves same-session without forcing a round trip; a
one-hour hold reliably produces day trades and collides with §4.4. And selecting
Aggressive never touches capability policy — it cannot enable options, crypto, shorting,
margin or extended hours, which remain independent of the risk profile by design.

**The table is a preset table, not documentation of independent fields.** `risk_profile`
selects the column; each setting takes that column's value unless explicitly overridden in
config, and an override outside the platform max is rejected at load, not clamped to it. A
profile that is validated for membership and then never read is not a risk profile — it is a
label. Two rules follow: selecting a profile must actually change behaviour with no other
config edits, and a combination the profile forbids must fail validation rather than load.
Concretely, `risk_profile: "AGGRESSIVE"` with `minimum_holding_period: "PT1H"` is the
misconfiguration this section exists to prevent, and it must be rejected at load, not merely
clamped.

**Consequence: AGGRESSIVE is unloadable at the pilot's actual cash posture, for two
independent reasons.** AGGRESSIVE's own `PT4H` minimum hold is sub-day, and §4.4's rule that
a cash or margin-under-25k account cannot honour a sub-day hold applies at load, not only at
runtime — so `risk_profile: "AGGRESSIVE"` with `assert_account_posture: "CASH"` (the pilot's
actual posture at $500 capital; `assert_account_posture` itself defaults to `UNKNOWN`, not
`CASH`, so this only surfaces once the posture is asserted honestly) fails validation on that
basis alone. Separately, and regardless of posture, AGGRESSIVE's own
`max_new_positions_per_day: 5` exceeds the platform default `max_approval_requests_per_day:
4` (§3.4) — a config that only changes `risk_profile` to `AGGRESSIVE` and nothing else fails
for this reason even before the posture question comes up. Both failures are independent:
fixing one and not the other still refuses to load. Neither constraint is relaxed for
AGGRESSIVE. An account whose posture is later *detected* to be cash or margin-under-25k has
to fail the same way even if config guessed differently, so weakening the sub-day/posture
rule here would only move that failure from load time to runtime, silently — and the
approval-cap relationship in §3.4 does not carry a risk-profile exception either. Loading
AGGRESSIVE for real therefore requires both `assert_account_posture: "MARGIN_OVER_25K"` (an
account posture that then still has to be confirmed against the broker before trading
starts, per §9.1) and an explicit `max_approval_requests_per_day` override of at least 5 —
which is to say, AGGRESSIVE is not usable in this pilot's actual $500 cash-account
deployment at all, by design, not by oversight.

### 6.1 Reserve semantics

The dual basis from Change Request §6.1 is adopted, and it is an improvement on v1.0: net
liquidation value is the stable denominator for the percentage, settled cash is what can
actually be spent, and an absolute floor prevents the percentage from becoming meaningless
on a small account.

```
required_reserve = max(nlv * min_settled_cash_pct_of_nlv,
                       min_absolute_settled_cash)

investable_cash  = settled_cash
                 - pending_buy_notional
                 - estimated_fees
                 - required_reserve

# checked twice: at target construction, and at pre-submit with live cash
# unsettled sale proceeds are never investable
# every decision record carries: cash_before, required_reserve,
#   proposed_amount, approved_amount, expected_cash_after
```

Neither the model nor the playbook optimiser can write any reserve field. This is enforced
the same way capability status is: separate write path, import boundary, and a test
asserting that no model-originated artefact can reach the configuration table.

**A BUY that exceeds investable cash is resized, not rejected.** Risk is applied to the
target weight vector, not per order (§1, §6.1): staging a BUY builds the post-trade target
weight for the whole book and runs it through the one shared constrainer, which clips
per-name and sector caps, then scales the entire vector down if its total notional exceeds
investable cash. If that scaling brings the requested symbol's authorised weight below what
was asked but still above zero, the order is sized down to what the reserve actually permits
rather than refused outright; it is rejected only when the authorised weight comes back at
zero. This is deliberately different from this section's own reject-at-load rule for an
out-of-range risk-profile override: that rule governs a *configuration* value at load time,
where there is no partial-credit notion of "half a valid config" to fall back to, so the only
sound response to an invalid combination is to refuse to start. A BUY's target weight, by
contrast, is a runtime quantity with a well-defined smaller value that still satisfies every
constraint — so resizing to it is not a laxer version of the same rule, it is the correct
behaviour for a different kind of decision, at a different layer.

---

## 7. Controlled self-improvement

Self-improvement is active from the first release, as required. The reconciliation with
v1.0's statistical caution is a distinction between two kinds of improvement that the
change request already draws, and which this document makes structural.

**Class A — active from day one.** Verifiable against ground truth, not against P&L:
extraction accuracy and schema conformance; entity resolution and document
classification; citation coverage and unsupported-claim reduction; research sequencing and
contradiction checks; confidence calibration against realised outcomes; token and cost
efficiency at equal quality; checklist completeness and rationale readability. Measurable
on a labelled sample of tens of documents. A held-out set of 100 human-checked extractions
is enough. Safe to iterate weekly.

**Class B — gated by statistics.** Only verifiable against P&L, so needs sample size:
signal weights and feature selection; entry and exit thresholds; materiality threshold
weights; sizing and ranking logic; holding-period and cooldown tuning. Candidates may be
generated and evaluated from day one, and the machinery is built on Day 11 — but promotion
requires the pre-registration, experiment budget and deflated metrics from v1.0 §9, which
need history the pilot does not yet have.

This split satisfies both requirements honestly. The system does improve itself
continuously from release, in the areas where improvement can be demonstrated within days.
It does not pretend that four weeks of live trades can validate a change to signal
weighting, because it cannot — and a promotion made on that basis is how a self-improving
system converges on noise while appearing to learn.

### 7.1 Candidate lifecycle

Every candidate carries hypothesis, change set, evaluation window and decision rule,
recorded before results exist. Candidates are versioned as playbooks with a parent
pointer, evaluated against the active version, and — for Class B — required to pass paper
or shadow operation before any promotion. Negative results are retained permanently.
Promotion is a human action with the evaluation report attached; rollback to the last
approved playbook is a single command and is tested on Day 12.

### 7.2 Immutable boundary

No candidate, playbook or model output may alter trade capabilities, reserve settings, risk
maxima, holding-policy bounds, mode state, credentials, audit configuration or the approval
requirement. These fields live in a separate schema with a separate write path; the
optimiser's database role has no grant on them. The Day-12 test suite attempts each of
these writes and asserts failure.

**Status: no enforcement exists yet, for any of these fields.** There is no optimiser to
grant or deny a database role to, no database roles or grants have been created, and no test
attempts a forbidden write. Mode state today lives in its separate file (§9.2's
`ModeStore`, built ahead of schedule) purely by virtue of being a different Python object
with no shared code path to the store an optimiser would use — not because anything has
denied it access. The same is true of the other six fields: the separation described above
is the target this boundary is being built toward, not a control operating today. Day 11
builds the candidate-generation machinery this boundary exists to constrain; Day 12 is where
the forbidden-write test suite referenced above is actually built and where this paragraph
should be deleted once it stops being true.

### 7.3 Post-launch validation track

v1.0's phase gates are not discarded; they run in parallel after launch, on a slower clock,
against history the running system accumulates from Day 2. Weeks 3–6 build the benchmark
harness and attribution over collected data. Weeks 6–12 establish a deterministic baseline
and the walk-forward runner. Month 4 onward runs the pre-registered feature experiments.
The point-in-time corpus purchase decision moves here, informed by whether the live system
has produced anything worth testing.

**Sequenced ahead of the playbook machinery, deliberately (§11).** This track starts on
Day 4's collected data, weeks before Day 11 builds Class A candidate generation and long
before Class B has enough accumulated history to act on anything. The ordering is the
point: pick quality is measurable on its own well before there is a self-directed playbook
mechanism whose output that measurement would otherwise be the only check on.

**Kill criterion — carried forward unchanged.** If after twelve months of live operation
the agent has not exceeded a buy-and-hold benchmark at equivalent cash reserve, net of
costs, model spend and taxes, it is retired and the capital is indexed. Shipping in
fourteen days changes when this is measured, not whether.

---

## 8. Local deployment

| Layer | Laptop implementation | Migration seam |
|---|---|---|
| Application | Single Python process group, one repo | Containerise unchanged; no service split required |
| API / UI | FastAPI on localhost, server-rendered dashboard | Same app behind a reverse proxy plus real auth |
| Transactional data | PostgreSQL in a local container | Connection string only |
| Research data | Parquet on disk, DuckDB for analysis | Object-store path behind a storage interface |
| Scheduling | launchd or systemd timer, advisory lock, run leases | Cloud scheduler; lease logic unchanged |
| Secrets | OS keychain, separate entries per mode | Managed vault behind the same provider interface |
| Audit | Append-only table with hash chain, plus JSONL mirror | Ship the JSONL to an immutable archive |
| Backups | Nightly encrypted dump, one off-device copy | Managed backup; restore drill unchanged |

### 8.1 Laptop lifecycle

A laptop sleeps, loses network and gets closed mid-session. Change Request §13 asks how
this is handled; the answer is that the system is designed to be killed at any instant,
which is a cheaper property to build than uptime.

| Condition | Behaviour |
|---|---|
| Sleep during market hours | Power assertion held while a run lease is active; on wake, clock skew is detected, all in-flight work is abandoned, and the cycle restarts from broker reconciliation. Missed windows are skipped, never caught up in a burst. |
| Network loss | Data staleness exceeds threshold, so the freshness gate produces no trade. Any submitted-but-unacknowledged order is resolved on reconnect by querying `client_order_id`, never by resubmitting. |
| Process restart | Crash-only design: no in-memory state survives by design. Startup sequence is reconcile → verify audit hash chain → expire stale approvals → resume. Tested on Day 12 by killing the process mid-submit. |
| Laptop unavailable at open | No trading occurs. Resting broker-side stops (§4.3) remain the only protection that survives the machine being off — which is the honest limit of local-first deployment and should inform how much capital the pilot carries. |
| Approval pending at close | Approvals expire rather than carry over. An unexpired approval is re-validated against live price bounds before submission regardless of age. |

### 8.2 Cost control plane

Adopted as specified. Every model call and provider request writes a `CostLedger` row with
units and estimated cost; the ledger rolls up per analysis, document, day, week and month.
Configuration exposes a monthly budget, a warning threshold and a hard stop.

Three enforcement points, in order of how much they save: the T3 screen, which is free and
rejects most work before it becomes a model call; the extraction cache, which makes
re-analysis of an already-seen document cost nothing; and the daily analysis cap, which
bounds the worst case. At the hard stop, T4 analysis pauses and the dashboard says so — but
T1 collection, reconciliation, risk evaluation, the holding gate and the kill switch keep
running. Budget exhaustion must never weaken a control; it only stops the system from
forming new opinions.

**Estimated pilot running cost.** Infrastructure is zero. Data is zero to low double digits
monthly using EDGAR plus the broker's included market data. Model spend at a cap of 8
analyses per day, mixed Haiku classification and Sonnet analysis with a cached instruction
prefix, lands in the low tens of dollars monthly. Suggested budget for a $500 pilot:
**$20/month, warning at $15, hard stop at $30**, with the daily analysis cap lowered to 8.
Note the ratio to capital — $20/month against $500 is still 4% monthly, which is a reason
to keep the pilot short and to treat its P&L as noise, not a reason to hide the number.

**Measured cost estimate (review round 2, 2026-08-01).** The paragraph above was a
qualitative guess. This one is measured: it runs the same `build_analysis_prompt` and
`_estimate_input_tokens`/`_price` functions `run_analysis` itself uses for its pre-call
budget check (§8.2's own enforcement point), against the real committed 10-K fixture
(`scripts/fixtures/edgar/AAPL_10K_0000320193-25-000079.htm`, 1,520,208 bytes), plus a
synthetic market-snapshot fact — not a separate approximation done outside the code path
it's meant to describe.

`agent.filing_text.extract_filing_text` yields 209,728 characters from the raw fixture
(13.8% extraction ratio). The full analysis prompt — system instructions, the boundary
delimiters, and the per-fact envelope/line-numbering `build_analysis_prompt` adds around
the extracted text — is larger than the raw extraction alone: 1,279 system characters plus
217,520 user characters, 218,799 total. At this codebase's own chars-per-token heuristic
(`agent.analysis._CHARS_PER_TOKEN_ESTIMATE = 4`), that is **54,699 estimated input tokens**
— about 4% above a back-of-envelope 209,728/4, because the prompt carries more than just
the extracted document text.

At configured rates (`config.example.json`'s `t4_input_price_per_million_tokens=2.0`,
`t4_output_price_per_million_tokens=10.0`) and the same worst-case output-token assumption
`run_analysis`'s own pre-call brake uses (`max_output_tokens=4000`, since actual output
length is unknown before the call completes): input cost **$0.1094**, output cost **$0.04**,
**$0.1494 per analysis, worst case**. This is a ceiling, not an expected average — a cache
hit costs $0, and an accepted analysis's real output is typically well under the 4,000-token
ceiling, so realized per-analysis cost should usually run below this figure.

At the configured cap of 8 analyses/day, over 21 trading days/month (verified against
`agent.market_calendar.is_trading_day` for August and September 2026, both 21; October
2026 is 22, November 2026 is 20 — 21 is a representative, not an assumed, figure) — 168
analyses/month — the worst-case monthly total is **8 × 21 × $0.1494 ≈ $25.10**. That sits
above the $20 target and below the $30 hard stop, confirming the plan's own reading: `w6`'s
budget brake (§3.2, `w6 * analyses_today / max_model_analyses_per_day`) is expected to
engage for part of most months by design, not as a bug, and realized spend should land
below this worst-case ceiling because it assumes zero cache hits and maximum output length
on every one of the 8 daily slots.

**Does this change `max_model_analyses_per_day` (currently 8)?** No — this estimate does
not by itself justify lowering it. $25.10 is a worst-case ceiling roughly 25% above the $20
target but still $4.90 under the $30 hard stop; the plan already treats $20 as a target
rather than a cap and holds the $10 gap to the hard stop as headroom for exactly this kind
of worst-case-vs-actual variance. Lowering the cap (e.g. to 6/day, worst case ≈ $18.82/month)
would buy more headroom under the target at the cost of screening out more candidates before
any of them reach a model call. The number that should actually decide this is *observed*
pilot spend, not this pre-pilot estimate — if realized month-to-date cost tracks close to the
$25 ceiling rather than nearer $20, that is the trigger to revisit the cap, not this
calculation. The value is unchanged here per instruction; this is a report, not a fix.

### 8.3 One gated path to broker-side effect

Invariant #2 was previously stated as *one code path from store to orders* and enforced only
where it was tested: the `submit` path. That was a point patch, not an invariant. Any second
method on the broker adapter that produces a broker-side effect is a second path, and it
reaches the broker without a signed `StagedOrder`, without a capability re-check and
without gate 4. The hole was not hypothetical: `cancel(client_order_id: str)` was abstract on the
adapter, took a bare string, and was ungated — the same hole as `submit`, already merged,
lower consequence only because it reduces exposure rather than creating it. It was closed in
commit 556e2c2, together with the `__init_subclass__` tripwire and the submit-signature
test. What follows records the invariant that fix had to satisfy; it is a report of a closed
defect, not a still-open concern.

The invariant was therefore restated at the interface level, before a concrete broker
adapter existed to be refactored around it. **Every broker-side effect is an order kind, and
every order kind is staged.** The adapter's public surface is the staging call; adapters
implement private `_*_impl` methods that are unreachable without a Gatekeeper-signed token.
A convenience wrapper — `replace_order`, `close_position` — is not a new method; it is a
helper that constructs an order of the corresponding kind.

| Order kind | Exposure | Risk constrain | Capability + signature | Notes |
|---|---|---|---|---|
| BUY | Creates | Full | Required | Unchanged. The reference path. |
| SELL | Reduces | Full | Required | Holding gate and PDT guard apply here, not to CANCEL. |
| CANCEL | Reduces intent | Skipped | Required | Withdraws an unfilled order. Never blocked by a risk limit — a control must not be able to trap an order in the market. |
| CLOSE | Reduces | Full | Required | A whole-position SELL with quantity resolved from reconciled broker state. No separate privilege. |
| REPLACE | Both | Full | Required | Cancels and creates in one broker call, so it is constrained as a new order, not as an amendment. Deferred: not implemented for the pilot; single order in flight makes cancel-then-submit sufficient. |

**Why the tripwire is not the mechanism.** A CI check that fails when the adapter grows a
new public method is worth having, and it should also assert that `submit`'s signature is
unchanged, since Python offers no way to forbid the override. But it catches subclasses, not
an edit to the base class itself, and a tripwire that a determined author can delete in the
same commit is a smoke detector, not a wall. The wall is that no adapter method can produce
a broker-side effect without a token it cannot mint. Enforced there, a new write method is
not dangerous — it is merely useless.

---

## 9. Data model and configuration

All nine entities from Change Request §9.1 are adopted. Additions and clarifications below;
entities from v1.0 not listed here are unchanged.

| Entity | Fields |
|---|---|
| `TradeCapabilityPolicy` | version, asset_class_status{}, side_status{}, funding_status{}, order_type_status{}, session_status{}, tif_status{}, symbol_allowlist, symbol_blocklist, effective_at, approved_by, approval_evidence_ref |
| `HoldingPolicy` | version, minimum_holding_period, cooldown_period, early_exit_categories[], evidence_required, effective_at |
| `PositionLot` | lot_id, symbol, opened_at, qty, cost_basis, settled, settles_at, earliest_normal_exit_at, holding_policy_version, closed_at, realised_gain, term, wash_sale_flag |
| `OpportunityEvent` | event_id, type, source_id, observed_at, effective_at, symbols[], materiality_score, score_components{}, threshold_version, analysis_status, suppressed_reason |
| `ApprovalRequest` | request_id, run_id, proposal_snapshot, risk_result, price_at_analysis, price_band_low, price_band_high, expires_at, decision, decided_by, decided_at, **decision_elapsed_ms**, invalidated_reason |
| `ApprovalToken` | *new* — token_id, request_id, order_fingerprint, price_band, expires_at, consumed_at, single_use |
| `EarlyExitRequest` | *new* — request_id, lot_id, category, evidence_fact_ref, remaining_hold, approval_id, outcome |
| `DayTradeCounter` | *new* — session_date, round_trips[], rolling_count, broker_reported_count, reconciled_at |
| `CapabilityChangeRequest` | request_id, dimension, from_status, to_status, prerequisites[], test_results, cost_impact, approved_by, approved_at |
| `CostLedger` | entry_id, provider, operation, units, estimated_cost, actual_cost, budget_period, run_id, cache_hit |
| `PlaybookCandidate` | candidate_id, parent_version, class (A\|B), change_set, hypothesis, decision_rule, evaluation_results, shadow_status, approved_by |
| `RunManifest` | run_id, as_of, trigger (EVENT\|ROUTINE\|REVIEW), mode, code_commit, cadence_config_version, holding_policy_version, capability_policy_version, risk_policy_version, playbook_version, threshold_version, prompt_versions[], model_ids[], store_watermark |

The `ApprovalToken` is the mechanism that makes "no live order without approval" checkable
rather than merely intended. It is single-use, bound to an order fingerprint and a price
band, and consumed atomically at submission — so a replay, a duplicate run or a restart
cannot reuse it, and an order whose parameters drift from what was approved cannot find a
valid token.

### 9.1 Configuration example

This is `config.example.json` as it exists in the repository. It is kept in sync with the
schema `config.py` validates; loading it verbatim must succeed, and an unknown key must be
rejected.

```json
{
  "mode": "PAPER",
  "require_human_trade_approval": true,
  "risk_profile": "MODERATE",
  "assert_account_posture": "CASH",
  "minimum_settled_cash_pct_of_nlv": 20,
  "minimum_absolute_settled_cash": 75,
  "minimum_holding_period": "P2D",
  "trade_cooldown_period": "P5D",
  "max_position_pct": 5,
  "max_sector_pct": 20,
  "drawdown_pause_pct": 7,
  "data_collection_interval_seconds": 60,
  "event_feed_interval_minutes": 5,
  "opportunity_screen_interval_minutes": 5,
  "routine_decision_interval_minutes": 240,
  "event_driven_analysis_enabled": true,
  "approval_expiration_minutes": 30,
  "approval_min_display_seconds": 10,
  "max_model_analyses_per_day": 8,
  "max_approval_requests_per_day": 4,
  "max_new_positions_per_day": 3,
  "max_day_trades_per_5_sessions": 3,
  "monthly_budget_usd": 20,
  "budget_warning_usd": 15,
  "budget_hard_stop_usd": 30,
  "trade_capabilities": {
    "US_EQUITY": "PRODUCTION_ALLOWED",
    "ETF": "PRODUCTION_ALLOWED",
    "OPTIONS": "DISABLED",
    "CRYPTO": "DISABLED",
    "SHORT_SELLING": "DISABLED",
    "MARGIN": "DISABLED",
    "FUTURES": "DISABLED",
    "FOREX": "DISABLED",
    "OTC": "DISABLED"
  },
  "sides": {
    "BUY": "PRODUCTION_ALLOWED",
    "SELL": "PRODUCTION_ALLOWED",
    "SELL_SHORT": "DISABLED",
    "BUY_TO_COVER": "DISABLED"
  },
  "funding": {
    "SETTLED_CASH": "PRODUCTION_ALLOWED",
    "MARGIN": "DISABLED",
    "UNSETTLED_CASH": "DISABLED"
  },
  "order_types": {
    "LIMIT": "PRODUCTION_ALLOWED",
    "MARKET": "PRODUCTION_ALLOWED",
    "STOP": "PRODUCTION_ALLOWED",
    "TRAILING_STOP": "PAPER_ONLY"
  },
  "sessions": { "REGULAR": "PRODUCTION_ALLOWED", "EXTENDED": "DISABLED" },
  "time_in_force": { "DAY": "PRODUCTION_ALLOWED", "GTC": "DISABLED" }
}
```

`assert_account_posture` is asserted, then verified against the broker; a mismatch halts
trading. `full_portfolio_review_schedule` is not yet a field — scheduled reviews arrive with
the cadence loop (Day 4), and the key must be added to the schema in the same commit that
reads it.

### 9.2 Mode transitions

Mode is not a free-text field and membership validation is not sufficient. This is NOT a
single linear chain — that shape was tried, found to be wrong, and replaced (see the
TOPOLOGY CORRECTION below for why). There are two distinct pieces: an escalation ORDERING
of four modes, and PAUSED, which sits outside that ordering entirely.

**The escalation ordering.** Four modes, one step at a time:

```
DISABLED  ⇄  RESEARCH  ⇄  PAPER  ⇄  PRODUCTION_ACTIVE
```

Forward movement is one step at a time. DISABLED is reachable immediately and
unconditionally from any state. PAPER → PRODUCTION_ACTIVE additionally requires
re-authentication and explicit confirmation. Loading a config that names a mode more than
one step ahead of the persisted current mode is a startup error, not a silent adoption —
otherwise "DISABLED to PRODUCTION_ACTIVE in one step is impossible" (§12 criterion 3, and
the Day-1 exit criterion in §11) is enforced by nothing.

**PAUSED is not a fifth rung on that ladder.** It is an emergency-stop OVERLAY, reachable
immediately and unconditionally from every one of the four modes above (and from itself),
because a kill switch must never be blocked by the same state machine it exists to
override. Entering it carries no confirmation requirement. Leaving it is defined as
returning to the SPECIFIC mode the system was persisted in immediately before the pause —
recorded at the moment PAUSED is entered, never derived from "the next mode in some
ordering" — or to DISABLED, the universal full reset. Resuming into PRODUCTION_ACTIVE
specifically still requires the same re-authentication and explicit confirmation the
initial promotion does; resuming into anything else requires neither.

**TOPOLOGY CORRECTION (real gap found running the loop for the first time).** An earlier
version of this section modeled PAUSED as the last element of a single five-mode tuple
`DISABLED ⇄ RESEARCH ⇄ PAPER ⇄ PRODUCTION_ACTIVE ⇄ PAUSED`, with legality derived from
plain index adjacency. That shape is wrong for a mode reachable from everywhere, for two
independent reasons:

1. **Dead end.** PAUSED's only index-adjacent neighbour was PRODUCTION_ACTIVE. A system
   paused from DISABLED, RESEARCH, or PAPER had no legal one-step path back to where it
   actually was — only DISABLED (discarding all memory of the prior mode and forcing a
   full re-climb) or PRODUCTION_ACTIVE (gated by confirmation and, separately, a live
   adapter that does not exist yet). A single failed first startup left no way back to
   operating short of hand-editing the durable mode store — defeating the entire point
   of persisting it.
2. **Escalation bypass (found while designing the fix for #1, independently more
   serious).** Because entering PAUSED is unconditional from anywhere, and PAUSED →
   PRODUCTION_ACTIVE was "legal" (index-adjacent) regardless of what mode PAUSED was
   actually entered from, the old shape permitted DISABLED → PAUSED (one unconditional
   hop) → PRODUCTION_ACTIVE (one hop, merely confirmed) as a two-hop path to live trading
   from an install that had never actually operated in RESEARCH or PAPER — silently
   defeating the one-step escalation rule this whole section exists to enforce. This was
   not exploitable end-to-end only because no live adapter exists yet (§11 Day 10) — an
   accidental mitigation elsewhere, not a property of the mode state machine itself.

The fix: PAUSED is removed from the escalation ordering entirely and modeled as its own
case, per the two paragraphs above. `agent.mode_store.ModeChange` records `paused_from` on
any row transitioning into PAUSED — whether via a failed startup (`agent.startup._halt`) or
a deliberate one (an operator setting `mode: PAUSED`, or the `--advance-mode-to PAUSED`
operator command below) — and `agent.mode.is_legal_step`/`assert_legal_startup` accept it
as an explicit parameter, since these are pure functions with no store access of their own.
An unsupplied or unknown `paused_from` allows nothing but DISABLED — default deny, matching
this plan's other fail-safe-on-uncertainty gates (Appendix E: "an unlisted value is
DISABLED").

**Enforcement.** This check is enforced in exactly one place: `agent.startup.run_startup`,
reading the persisted mode (and, when it is PAUSED, the recorded `paused_from`) from
`agent.mode_store.ModeStore` — the durable, append-only, separate-file store described in
§7.2's immutable boundary. `agent.config.load` validates only that a mode name is a known
value (`agent.mode.MODES`, all five — PAUSED included, even though it is not a member of
the four-mode escalation ordering `agent.mode.CHAIN`); it does not read `ModeStore` and does
not check transition legality, deliberately — a config loader independently re-deriving "the
mode the system was last in" would be a second reader of one durable value, free to be
called with a stale or simply wrong persisted mode. `run_startup` is the sole code path real
orders ever flow through (§1's one-code-path invariant), so it is the sole enforcer of this
rule, backed by the one store that actually persists it.

**Operator path around a fresh-install dead end.** `scripts/run_agent.py --advance-mode-to
MODE` advances the persisted mode one legal step — including resuming out of PAUSED — with
no broker adapter, no account, and no reconciliation constructed at all, exactly the same
`assert_legal_startup`/`ModeStore` path `run_startup` itself uses. This exists because the
real scheduled loop constructs a broker adapter for every configured account unconditionally,
before `run_startup` ever runs — so setting `mode: RESEARCH` in config to legally take the
first escalation step does not work: the adapter is hardcoded to PAPER and refuses a
RESEARCH-bound secrets provider before `run_startup` gets a chance to run at all. The same
structural gap affects PAPER → PRODUCTION_ACTIVE for a more fundamental reason: no live
adapter exists in this codebase yet (§11 Day 10), so nothing can operate in that mode
regardless of how the persisted value is reached.

---

## 10. Dashboard and approval card

All always-visible controls from Change Request §10.1 are adopted, plus month-to-date
realised short-term gains, wash-sale disallowed losses, the day-trade counter with its
rolling window, and the approval-quality metrics from §3.4.

The approval card follows Change Request §10.2 section for section. Three design rules
govern it, all aimed at the failure mode where the card becomes a button rather than a
decision:

- The bear case and contradictory evidence sit *above* the approve action, not below it, and
  are never collapsed by default.
- The card states what will be true after the fill — reserve percentage, sector exposure,
  concentration, earliest normal exit, day-trade count — rather than only what is true now.
  Post-trade state is the thing being decided.
- Approve is not the default focus target, there is no keyboard shortcut for it, and a card
  younger than a configurable minimum display time cannot be approved. Deliberate friction
  on the irreversible action.

Modify-within-bounds is supported as specified: quantity or notional may be reduced, and a
limit price may be moved adversely to the trade, both without re-analysis. Any change that
increases size, loosens the limit or alters the instrument invalidates the card and requires
a fresh decision — otherwise "modify" becomes a bypass of the risk constrainer.

---

## 11. Fourteen-day backlog

The change request's day sequence is sound and is adopted with four amendments, marked ▲
below. Each day ends with its tests green and a commit; a slipped exit criterion pushes the
pilot date rather than shipping the day unfinished.

At 2–3 hours per evening (§1.2) the fourteen numbered days below are **work units, not
calendar days**. Days 1–10 fit in the first fortnight and land the paper pilot; Days 11–14
plus the live adapter fall in the following two weeks, putting the first live order near
calendar Day 30. Two consequences worth naming: Day 14's live order becomes Day 14's
*paper* order, with the live equivalent repeated against a real broker before capital is
committed; and, per §1.2's re-sequencing ▲, Day 3's broker work is no longer read-only-plus-
simulator — it builds the real Alpaca paper adapter (read and write both, one API serving
paper and live) ahead of Day 4's collectors, which is why Day 8's paper-execution content
below has folded into Day 3 and Day 10 is now the shrunk live-only remainder. Evaluation and
attribution (§7.3) are likewise sequenced ahead of Day 11's playbook machinery, not after
it — see the note below the table.

| Day | Deliverable | Exit criterion |
|---|---|---|
| 1 | Repo, local env, config schema with validation, mode state machine defaulting to DISABLED, audit table with hash chain | App starts; tests and lint run; invalid config rejected with a readable error |
| 2 | Postgres, Parquet store with `observed_at`/`effective_at`, `as_of()` accessor, secrets abstraction, audit events | No credentials in source; property test proves `as_of` cannot read the future |
| 3 | ▲ Alpaca paper adapter, read AND write — account/position reconciliation, settlement tracking, day-trade counter, broker capability probe, idempotent order submission, order lifecycle, partial fills — built once as one Alpaca-API unit, ahead of the collectors (§1.2) | Positions, settled cash, open orders and day-trade count reconcile; fractional and order-type support documented from live API responses; approved paper orders fill and reconcile; duplicate submit is a no-op |
| 4 | Market data, EDGAR and news collectors, market calendar, T3 materiality screen with threshold calibration harness | Events and freshness visible; screen produces a ranked candidate list with zero model calls |
| 5 | Structured research analysis, extraction cache, schema validation, prompt-injection isolation, cost metering | Schema-valid analysis with source timestamps and citations; cache hit costs nothing; cost row written per call |
| 6 | Risk profiles, target portfolio, dual-basis reserve enforcement, capability policy and all four capability gates | Disabled-capability proposals rejected at every gate; buys resize correctly against settled cash; golden reserve cases pass |
| 7 | Lot-level holding period, cooldown, early-exit workflow, ▲ PDT guard and tax-lot accounting with wash-sale detection | Golden tests pass for hour, day and week holds; unevidenced early exit rejected; fourth day trade blocked |
| 8 | ▲ Folded into Day 3 above — paper execution is part of the one Alpaca adapter unit, not a separate later build | (see Day 3) |
| 9 | Approval inbox, single-use tokens with price bands, expiry, notifications, ▲ daily approval cap and decision-time logging | No live path without a valid unexpired token; token cannot be reused; out-of-band price invalidates |
| 10 | ▲ Shrunk: live base URL, separate keychain entry, re-authentication for activation, import/authorization boundary tests. Gate 4, approval-token consumption and the pre-submit re-check of reserve, capability, holding eligibility and the PDT counter are already shared code, inherited unchanged from the Day-3 adapter — not rebuilt here | Import and authorization boundary tests pass; research package cannot reach live credentials |
| 11 | Playbook versioning, Class A candidate generation and evaluation, rollback, immutable-boundary enforcement | Candidate evaluated but cannot self-promote; every forbidden write from §7.2 fails; rollback restores prior version |
| 12 | Failure suite: restart mid-submit, stale data, duplicate callback, sleep/wake, network loss, kill switch, hash-chain verification | Every anomaly resolves to no trade with no duplicate or orphaned order |
| 13 | Readiness review, operator runbook, backup and restore drill, ▲ adversarial self-review in a fresh session (Appendix C.6) | Checklist approved; restore from backup verified; no unresolved high-severity review finding |
| 14 | Controlled live pilot: minimum-size order, human approval, full reconciliation, then flat | One approved live order placed, filled, reconciled and closed safely; audit trail complete end to end |

**Evaluation and attribution move ahead of the playbook machinery (§7.3).** Day 11 is
where Class A candidate generation is built, and Class B (the self-directed one) needs
months of accumulated history before it has anything to act on. The benchmark harness and
attribution work in §7.3 does not wait for either: it starts on the same collected-data
foundation Day 4 lays down, weeks before Day 11's playbook versioning exists at all. The
practical effect: pick quality — is this system's judgment any good — becomes a measurable
question months before there is a Class B with enough history to reward or penalise a
pick, rather than the two tracks being built in the order they are numbered.

Prioritisation rule, adopted verbatim: anything not required for safe end-to-end operation
by Day 14 is deferred. No premium corpora, no microservices, no elaborate dashboard, no
additional asset classes. Interfaces are preserved for growth; implementations are not built
ahead of need.

---

## 12. Day-14 acceptance criteria

1. Application runs on the laptop from documented setup instructions.
2. Paper and live modes are explicit, visually distinct and credential-isolated.
3. Production activation requires re-authentication and explicit confirmation; DISABLED to
   PRODUCTION_ACTIVE in one step is impossible.
4. Only long US equities and approved ETFs are production-enabled, funded by settled cash.
5. Options and cryptocurrency are demonstrably blocked at all four capability gates.
6. Trade capabilities change only through an authorized, versioned, audited policy change
   that no model-originated artefact can initiate.
7. Collection, event monitoring, screening and routine decisions run at independently
   configurable intervals.
8. A material event triggers analysis and an approval prompt without waiting for the
   routine schedule.
9. Minimum holding period supports minutes, hours, days and weeks, enforced per lot at fill
   time.
10. Normal early sells are rejected; early exits require an evidenced category and human
    approval.
11. The day-trade guard blocks an order that would breach the rolling limit, and the
    effective binding constraint is displayed when account posture overrides the configured
    hold.
12. Risk profile and dual-basis settled-cash reserve are enforced deterministically at
    target construction and again pre-submit.
13. Every live order requires a valid, unexpired, single-use approval token bound to an
    order fingerprint and price band.
14. Order submission is idempotent; broker state is reconciled before and after trading.
15. Kill switch, stale-data protection, sleep/wake and restart recovery are tested and
    resolve to no trade.
16. Every decision pins data, model, prompt, playbook, threshold, risk, holding and
    capability policy versions in a run manifest.
17. The optimiser produces a Class A candidate improvement and cannot activate it without
    approval.
18. Model and data costs are metered against a configured budget; the hard stop pauses
    analysis without weakening any control.
19. Approval decision time and approve rate are logged, and the dashboard surfaces probable
    rubber-stamping.
20. The audit hash chain verifies from genesis, and a restore from backup reproduces state.
21. No broker-side effect is reachable except through a staged, signed order kind (§8.3);
    `cancel` included.

---

## 13. Answers to the requested decisions

Change Request §13 asks for answers rather than deferrals. Each is answered below with the
verification step that confirms it, since several depend on live API behaviour that
documentation alone should not settle.

**Which broker, and which functions verified?** Per §1.2, no broker is wired for live
execution by Day 14. A single `BrokerAdapter` interface is built on Day 3 with the paper
simulator behind it, and the live implementation lands around Day 20 once the broker is
chosen. Alpaca remains the recommendation when that day comes: one API for paper and live,
fractional shares, no per-trade commission, and programmatic trading permitted rather than
tolerated. Whichever is chosen, the first session against it records actual API responses
rather than trusting documentation: account and positions read, settled versus unsettled
cash, open-order query, day-trade count and pattern-day-trader flag, submission with
`client_order_id`, cancel, and query-by-client-id after a simulated timeout. Robinhood
cannot fill this role today — no supported retail automation API, and automated access is
generally prohibited by its customer agreement.

**Which order types for fractional shares and regular-hours execution?** Expect fractional
orders to be restricted — typically day-only, regular hours, without stop or trailing-stop
support, and often market or limit only. That restriction shapes the design rather than
being a footnote: it means resting broker-side stops (§4.3) are unavailable on fractional
positions, so any position that needs stop protection must be whole-share. Day 3 probes each
combination against the live paper API and writes the supported matrix into the capability
policy as data, so the system's own configuration reflects what the broker actually accepts
rather than what was assumed.

**Which data sources, and what monthly budget?** First release uses only free or included
sources: SEC EDGAR submissions and full-text search for filings, the broker's included
market data for quotes and bars, a public market calendar, and public company news feeds. No
paid subscription and no historical backfill in the first fourteen days. Total data cost
approximately zero; model cost budgeted at $20/month with a $30 hard stop (§8.2). The
point-in-time corpus decision moves to the post-launch track, where it can be justified by
whether the live system has produced anything worth testing.

**How is event materiality defined and tuned without a model per tick?** A deterministic
local score over volatility-normalised return, relative volume, an explicit filing-type
allowlist, earnings proximity and idiosyncratic move, with a budget brake term (§3.2).
Tuning is inverted: declare the analyses-per-day budget and let the calibrator solve for the
threshold by replaying sixty sessions of collected events. Threshold changes are versioned
because they change what the system trades.

**Initial safe numeric defaults for the three profiles?** Given in the §6 table, with
platform maxima. Moderate is the recommended pilot default. Aggressive is deliberately set
at a four-hour minimum hold rather than one hour, because a one-hour hold reliably produces
day trades and collides with the constraint in §4.4.

**Smallest safe live test order and rollback procedure?** A $10 notional fractional buy of a
large, liquid broad-market ETF, placed as a limit order inside the spread during regular
hours, mid-session away from the open and close. Rollback: sell to flat immediately after
reconciliation confirms the fill, then set mode to PAUSED. The pilot's purpose is to prove
the path end to end, so the position is closed the same session by design rather than held.
Pilot funding is $500 (§1.2), so total capital at risk during the first live test is the $10
order notional. At this size fills and reserve arithmetic are real while a total loss is an
acceptable tuition payment — which is the whole point of the figure.

**How are sleep, internet loss and restart handled?** Crash-only design with
reconcile-on-start, run leases, skip-not-catch-up on missed windows, and idempotent
resolution by client order ID (§8.1). The honest limitation: while the laptop is off,
nothing is monitored, and only resting broker-side stops protect open positions. That is an
argument for the small capital figure above and for whole-share positions where stop
protection matters.

**What test proves disabled capabilities cannot reach execution?** A table-driven suite over
the full cross-product of asset class, side, funding, order type, session and time-in-force,
asserting rejection at each of the four gates in §5.1, with adversarial inputs including an
OCC option symbol, a crypto pair, a short side, extended-hours session, GTC time-in-force
and an OTC ticker. A companion test asserts the adapter's submit call is never reached, and
an import-boundary test asserts the research package cannot import the live adapter.
Build-failing, not advisory.

**What is required later to enable options or crypto safely?** Options need an instrument
model with contract, multiplier, expiry, exercise and assignment; a maximum-loss risk model
that is not linear in notional; approval-level Greeks and expiry-risk display; broker
approval level; and paper validation through an expiration cycle including assignment.
Crypto needs a crypto-capable adapter, 24/7 session handling that breaks the market-calendar
assumption throughout, custody and transfer semantics, a separate volatility and gap risk
package, and distinct tax treatment. Both progress one status at a time through Appendix A,
and neither is in scope for this plan.

---

## 14. Requirements that cannot honestly be met in 14 days

Change Request §14.1 asks for this list explicitly. Three items, each with what is delivered
instead.

| Cannot be met | Why | Delivered instead |
|---|---|---|
| Any validation that the strategy works | No point-in-time corpus exists at Day 14, so no backtest is trustworthy; and no live sample of days is large enough to measure anything. | A system whose controls are proven and whose history accumulates from Day 2, plus the post-launch validation track (§7.3) with the kill criterion intact. |
| Class B self-improvement in production | Promoting a signal-weight change on a handful of trades is optimising against noise. The machinery can exist; the evidence cannot. | Full candidate generation, evaluation and rollback built on Day 11, active for Class A from launch, with Class B promotion gated on sample size (§7). |
| Sub-day minimum holds as configured | Cash-account settlement and the PDT rule are external regulation, not policy the platform controls (§4.4). | The setting is accepted and honoured where the account posture permits; where it does not, the effective binding constraint is enforced and displayed rather than silently ignored. |

### Two answers needed before Day 1

**Account type and size.** Cash versus margin, and whether equity exceeds $25,000,
determines whether sub-day holds are achievable at all, and therefore which defaults are
coherent. This is the one input that changes §4 and §6 materially.

**Fourteen days of what.** Fourteen consecutive full days is a different plan from fourteen
calendar days at evenings and weekends. The backlog assumes roughly six focused hours per
day; at two hours per day it is a five-week plan and the Day-14 date should move rather than
the scope silently shrinking. *(Answered in §1.2 — 2–3 hours per day; the paper-pilot
re-target is the consequence.)*

One further note, offered rather than asked for. The riskiest thing in this plan is not the
schedule; it is that a system which looks and behaves like a working investment platform is
psychologically very persuasive, and it will be persuasive on Day 14 while having
demonstrated nothing about returns. The controls in §7.3 and the kill criterion exist to
hold that line. Keeping the pilot capital small is the cheapest form of the same protection.

---

## Appendix C. Build prompts, day-mapped

Six sessions covering the fourteen days. Attach this document to every session. Require
tests before implementation on anything touching money, time or ordering. Recommended tier
per prompt; see Appendix D.

### C.1 — Days 1–2 · Skeleton, store, audit chain
*Opus tier · plan mode*

```
Read the attached architecture plan v1.1 fully, then propose a plan for
Days 1-2 only and wait for approval.

Scope: repo skeleton, config schema, mode state machine, bitemporal store,
audit hash chain, secrets abstraction. Local laptop, Postgres in a
container, Parquet on disk.

Hard constraints:
- Every fact carries observed_at and effective_at; append-only. No UPDATE
  or DELETE in the data layer.
- store.as_of(t) must be incapable of returning observed_at > t. Prove it
  with property-based tests.
- Mode starts DISABLED. DISABLED -> PRODUCTION_ACTIVE in one step must be
  impossible; assert it.
- Config is validated on load and rejects unknown keys and out-of-bounds
  values with readable errors.
- Audit events form a hash chain verifiable from genesis.
- Nothing in the research package may import anything holding live
  credentials. Enforce with an import-boundary test now, while the
  codebase is small.

Tests first. Ask about anything underspecified. Do not add features not in
the document.
```

### C.2 — Days 3–4 · Broker read path, collectors, materiality screen
*Sonnet tier*

```
Days 3-4. Read-only broker integration and the collection tiers.

1. Alpaca read-only adapter against the PAPER endpoint: account, positions,
   open orders, settled vs unsettled cash, day-trade count. Reconciliation
   treats broker state as truth and repairs local state.
2. A capability probe that submits and immediately cancels test orders to
   discover, empirically, which combinations the broker accepts:
   fractional vs whole share, market/limit/stop/trailing, day vs GTC,
   regular vs extended. Write the discovered matrix into the capability
   policy as data and print it. Do not assume from documentation.
3. T1 collector: prices, account, open orders on an interval, with
   freshness watermarks per source.
4. T2 collector: EDGAR submissions and full-text search, plus news feeds,
   with dedupe by content hash and entity resolution to tickers.
5. Market calendar: sessions, holidays, early closes, next_session(t). No
   hardcoded market hours anywhere else in the codebase.
6. T3 materiality screen exactly as specified in §3.2 — pure local
   arithmetic, zero model calls, plus the calibration harness that solves
   for a threshold given a target analyses-per-day budget by replaying
   collected events.

The screen is the cost firewall for the whole system, so it needs unit
tests with hand-computed scores and a test asserting it makes no network
calls to a model provider.

Note: the probe's cancels go through the staged CANCEL path in §8.3, not a
bare cancel() call.
```

### C.3 — Days 5–7 · Analysis, risk, capability, holding, PDT
*Opus tier*

```
Days 5-7. The decision plane. This is the correctness-critical core.

Day 5 — structured analysis. Claude call producing schema-validated output
with citations and source timestamps. Extraction cache keyed by
sha256(doc) + prompt_version + model_id + schema_version; a cache hit
makes zero API calls. Document text goes in a delimited untrusted-content
block; the extraction path has no access to credentials, config or the
order path — test it. Invalid output is logged and skipped, never retried
in a loop and never silently defaulted. Every call writes a CostLedger row.

Day 6 — risk and capability. Implement risk_constrain over the weight
vector before any order exists, with the dual-basis reserve from §6.1:
required_reserve = max(nlv * pct, absolute_floor), investable from settled
cash only. Then the TradeCapabilityPolicy and all four gates from §5.1.
Golden cases: reserve exactly binding, sector cap binding across two
names, position and sector binding together, target summing above 1.0,
and unsettled proceeds excluded.

Day 7 — holding policy and PDT. Lot-level enforcement per §4.1:
earliest_normal_exit_at from the FILL timestamp, policy version frozen at
fill, sellable_qty over eligible settled lots only. Early-exit workflow
per §4.2 — default reject, evidenced category required, approval needed,
audited as its own object. Tax lots with FIFO and specific-lot, short vs
long term, and 61-day wash-sale detection. Rolling five-session day-trade
counter capped per config and reconciled against the broker's own count.

Golden tests for PT1H, P1D and P7D holds. A test asserting that shortening
the minimum hold does not release lots opened under a longer policy.
```

### C.4 — Days 8–10 · Execution, approval tokens, live path
*Opus tier*

```
Days 8-10. Execution and the approval control.

Day 8 — paper execution. Single-threaded, one order in flight,
client_order_id from (run_id, symbol, sequence). Order lifecycle including
partial fills, rejections, cancels. Reconcile before and after. A
duplicate submit must be a no-op, proven by test. Order kinds per §8.3:
BUY, SELL, CANCEL, CLOSE all staged; REPLACE defined and NotImplemented.

Day 9 — approval. This is the central control, so build it as a token
rather than a boolean:
- ApprovalToken is single-use, bound to an order fingerprint and a price
  band, with an expiry. Consumed atomically at submission.
- No code path may submit a live order without consuming a valid token.
  Assert it by searching for every call site of the submit function.
- A quote outside the price band, or new evidence beyond a delta,
  invalidates the token.
- Daily approval cap enforced, competing candidates ranked by materiality,
  and a stop-loss exception allowed to bypass the cap.
- Log decision_elapsed_ms on every decision.
- Approve is not the default focus target, has no keyboard shortcut, and
  is disabled until a minimum display time has elapsed.

Day 10 — live adapter behind the disabled state machine. Separate
credentials in a separate keychain entry loaded by a separate process.
Re-authentication for activation. Pre-submit re-check of reserve, live
cash, capability, holding eligibility, PDT counter and price band. Import
and authorization boundary tests must pass.

Do not relax the token model to make anything convenient. If it is
inconvenient, say so and stop.
```

### C.5 — Days 11–12 · Playbooks, dashboard, failure suite
*Sonnet tier*

```
Days 11-12. Self-improvement machinery, dashboard, and the failure suite.

Day 11 — playbook versioning with parent pointers. Class A candidate
generation and evaluation against a labelled held-out set of ~100
human-checked extractions (extraction accuracy, citation coverage,
calibration, cost per analysis). Class B candidates may be generated and
evaluated but cannot be promoted — enforce that in code, not in
documentation. Rollback to the last approved playbook as a single command.
Then the immutable-boundary tests: attempt, from the optimiser's database
role, to write capability status, reserve settings, risk maxima, holding
bounds, mode, credentials, audit config and the approval requirement.
Every one must fail.

Day 12 — dashboard and failure suite. Dashboard: PAPER/LIVE banner,
effective limits, per-position earliest normal exit, reserve and
investable cash, capability statuses, schedules, pending approvals with
countdown, all policy versions, broker/data/model health, kill switch,
last reconciliation, month-to-date cost vs budget, realised short-term
gains, wash-sale disallowed losses, day-trade counter, and the approval
quality metrics from §3.4.

Failure suite — every one must resolve to no trade with no duplicate or
orphaned order: kill the process mid-submit; duplicate broker callback;
stale data past threshold; sleep and wake with clock skew; network loss
after submit but before acknowledgement; storage full; corrupt local
state; audit hash chain broken; approval expired between decision and
submit.
```

### C.6 — Days 13–14 · Adversarial review and live pilot
*Opus tier · fresh session*

```
Start a FRESH session with no implementation context. Read the repository
and the attached plan, and review adversarially before any live order is
placed.

Hunt specifically for:
- Any path where a live order can be submitted without consuming a valid,
  unexpired, single-use approval token.
- Any path where an order reaches the adapter without passing
  risk_constrain, the capability gate, the holding gate and the PDT guard.
- Any broker-side effect reachable without a staged, signed order kind —
  cancel, replace, close, or a new adapter method (§8.3).
- Any disabled capability that can reach execution — run the cross-product
  test and try to defeat it.
- Any look-ahead in the store or in the screen.
- Any place a lot's holding policy version can be mutated after fill.
- Any credential reachable from the research or extraction package.
- Any non-idempotent order path, or a retry that could duplicate a fill.
- Any silent exception swallow, or a bare except around a risk check.
- Any mutation of a supposedly immutable row, including audit rows.
- Any model-originated artefact that can write a policy, capability or
  reserve field.

Report findings by severity with file and line. Fix nothing in this
session — I want the list first.

Then, separately: write the operator runbook. Daily start and stop,
what each dashboard number means, how to use the kill switch, what to do
when reconciliation mismatches, how to restore from backup, and the exact
Day-14 live test procedure — $10 notional limit order on a liquid
broad-market ETF, mid-session, approved manually, reconciled, then sold to
flat and mode set to PAUSED.
```

### Prompting discipline

Attach this document to every session. Without it, the model will reintroduce per-order risk
checks, a second backtest code path, and a boolean `approved=True` flag in place of the
token — all three are more common in training data than what is specified here.

Treat any suggestion to loosen a tolerance, widen a price band or skip a gate "for now" as a
finding rather than a fix. On a fourteen-day schedule the pressure to do exactly that is the
main risk to the controls.

---

## Appendix D. Model selection

Unchanged in principle from v1.0, with the runtime tiers now mapped onto the cadence
architecture. Pin exact model IDs in config and record the resolved ID in every run manifest
— a floating alias breaks the reproducibility guarantee in the extraction cache.

| Use | Tier | Reason |
|---|---|---|
| Store, risk, capability, holding, approval token (C.1, C.3, C.4) | Opus tier | Correctness-critical and subtle. A gap in the token model or the holding gate is the difference between a control and the appearance of one. |
| Collectors, screen, dashboard, failure suite (C.2, C.5) | Sonnet tier | Well-specified work against clear contracts; the bulk of the fourteen days. |
| Adversarial review before live (C.6) | Opus tier, fresh session | Finding an authority bypass is harder than writing the code, and a session without implementation context reviews more honestly. |
| T4 runtime analysis — the core research call | Sonnet tier | Needs real comprehension of hedged financial language. Periodically sample against an Opus-tier run on the same documents; degrading agreement after a model change is then a detectable event rather than invisible drift. |
| T2 classification, entity resolution, dedupe | Haiku tier | High volume, low difficulty. Filters the feed so the expensive tier only reads what matters. |
| Approval-card rationale prose | Haiku or Sonnet | Presentation of a decision already made. Never an input to it. |

Tier names are stable; version numbers are not. Read Anthropic's current model list at build
time, pin the exact ID in config, and record it in the run manifest. Mid-2026 reporting
places the tiers at Haiku 4.5 for volume work, a current Sonnet as the balanced default, a
current Opus above it, and a higher tier above Opus for the hardest reasoning — verify
against Anthropic's own documentation rather than this paragraph.

Cost discipline is unchanged: a cached instruction prefix so the marginal cost is the
document itself, batch submission for anything not latency-sensitive, and the T3 screen
doing the real work of keeping spend down. If the monthly model bill rises with the number
of analyses rather than with the number of new documents, the cache is broken.

---

## Appendix E. Initial safety boundary

Long US listed equities and approved ETFs. Settled cash only. No margin, leverage, shorting,
options, cryptocurrency, futures, forex or OTC. Regular market hours. Day time-in-force.
Human approval required for every order, with a single-use token bound to price and expiry.
Configurable minimum holding period enforced per lot, with evidenced and approved early exit
only. Deterministic risk, reserve, capability, holding and day-trade checks evaluated before
every submission. Fail-safe to no trade on any uncertainty in data, broker state, policy or
process health.

Appendix A (capability expansion readiness checklist) and Appendix B (per-dimension initial
capability policy) are adopted verbatim from Change Request v1.1 §Appendix A and §5.2
respectively, and are not restated here. Enabling any dimension requires the full checklist
plus a versioned `CapabilityChangeRequest` per §5.

---

## Change log

| Version | Change |
|---|---|
| v1.1 | Revision of v1.0 per Change Request v1.1. |
| v1.1 + §8.3 | Invariant #2 restated at the `BrokerAdapter` interface: every broker-side effect is a staged, signed order kind. Adds the order-kind table, gates `cancel`, defers `REPLACE`. Acceptance criterion 21 added. |
| v1.1 fix | §13 model budget corrected to $20/$30 (was $75/$100, contradicting §1.2 and §8.2). |
| v1.1 fix | Approval cap standardised at 4/day; §3.4's prose said six, §3.1 and §9.1 said four, and the code had already standardised on four. |
| v1.1 fix | §9.1 config example replaced with the repository's actual `config.example.json`. The previous example omitted `sides`, `funding`, `max_position_pct`, `max_sector_pct`, `drawdown_pause_pct`, `max_new_positions_per_day` and `approval_min_display_seconds`, and included `full_portfolio_review_schedule`, which the schema does not yet accept. |
| v1.1 add | §9.2 mode transitions written out explicitly. Criterion 3 and the Day-1 exit criterion require a transition guard; membership validation alone does not provide one. |
| v1.1 add | §6 states that the profile table is a preset table — `risk_profile` must drive the defaults and reject contradictory overrides, not sit unread beside independent fields. |
| v1.1 confirm | §4.1 records a real-account finding (§13 probe, 2026-07-27): Alpaca's cash-account API has no settled/unsettled cash field anywhere. `lot.settled` and cash-account free-riding protection can only be enforced from a local ledger's own T+1 expectation, never from broker-reported state. Settled-cash reconciliation (exact equality, Option A) is unaffected — it checks that two cash *totals* agree, not which lots are settled. The local ledger itself remains unbuilt. |
| v1.1 confirm | §4.1 records a second confirmed finding (2026-07-27, `agent/lot_selection.py`): an internal `lot_id` does not control which lot Alpaca actually disposes of. Alpaca's own documentation (not the Customer Agreement, which names no method) confirms Compressed FIFO for end-of-day positions, Weighted Average intraday, and no API-level lot designation. `sellable_qty` was evaluating hold-eligibility per lot in isolation, which could believe a seasoned lot was sold while the broker's FIFO order actually disposed of a fresh one — fixed to walk lots in broker-actual disposal order and block on the first ineligible FIFO predecessor, no override path. `Ledger.disposal_records()` now records intended-vs-broker-actual lot per SELL fill. |
| v1.1 fix | §9.2 topology corrected (real gap found running the loop for the first time): PAUSED was modeled as the last element of a single five-mode chain, making it a dead end (no legal one-step path back to a mode paused from DISABLED, RESEARCH, or PAPER) and — independently, more seriously — permitting DISABLED → PAUSED → PRODUCTION_ACTIVE as an unintended two-hop bypass of the one-step escalation rule. PAUSED is now modeled as an overlay outside the four-mode escalation ordering, recording the specific mode it was paused from (`paused_from`) and requiring confirmation on resume only when that mode is PRODUCTION_ACTIVE. See §9.2 for the full correction. |
| v1.1 fix | §4.1 records a real-account finding (2026-07-28): a manually-placed BUY in the broker's own dashboard has no staged `OrderRecord`, so `agent.fill_sync.sync_fills` correctly refused to guess its holding-policy version — but the refusal was fatal, halting the scheduled loop every cycle forever with no path forward. `agent.execution_quarantine.ExecutionQuarantineStore` now quarantines an execution with unresolved intent (a BUY missing `holding_policy_version`, or a SELL/CLOSE missing `lot_id`) instead of raising; the loop continues, and an operator resolves it via `scripts.run_agent --admit-execution`/`--reject-execution`, both recorded in the audit log. See §4.1 for the full reasoning, including why quarantine (not an auto-ingest default) was chosen for both sides uniformly, and how this interacts with the already-named CLOSE/multi-lot gap (§8.3). |
| v1.1 fix | §4.1 records a real-account finding (2026-07-28): running the loop against a real fractional-share fill (0.027087234 shares) halted `reconcile_settled_cash` on binary-`float` representational noise at the fifteenth decimal place (`480.01` vs `480.010000529276`), not a real discrepancy — the exact-equality design (Option A) was right, but binary `float` cannot carry it exactly. Every money/quantity field reaching that comparison (`AccountSnapshot`, `Position`, `Execution`, `BrokerOrder`, `Fill`, `Lot`, and their on-disk rows in `LedgerStore`/`ExecutionQuarantineStore`) is now `decimal.Decimal`, via one shared coercion rule (`agent/money.py`); integer minor units were considered and rejected (no single natural scale across three-decimal prices and nine-decimal fractional-share quantities). The comparison logic itself is unchanged — exact equality still holds, only the type does not lie about itself anymore. See §4.1 for the full reasoning and `agent/money.py`'s own docstring for the coercion rule. |
