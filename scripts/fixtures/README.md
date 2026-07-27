# scripts/fixtures/

This directory is intentionally empty as of this commit.

`scripts/alpaca_probe.py` (§1.2, §11 Day 10) is built and tested, but it has
**not** been run against a real Alpaca paper account, for two independent
reasons:

1. The agent building this codebase has no real Alpaca paper API
   credentials, and will not ask the operator to hand any over — entering
   or relaying financial/API credentials on the operator's behalf is out of
   bounds regardless of who asks.
2. Even with credentials, the sandbox this agent runs in has no network
   path to `paper-api.alpaca.markets` — a credential-free reachability
   check from that sandbox returned `curl: (56) Received HTTP code 403 from
   proxy after CONNECT`, i.e. the egress proxy does not allow this host at
   all.

Consequently no fixture exists yet, and the three empirical questions this
unit was supposed to answer (settled vs. unsettled cash, real order-status
vocabulary, `supported_matrix()` accuracy) are **not** answered anywhere in
this codebase or its commit history. Answering them requires a human to:

```
python scripts/alpaca_probe.py --key-id <paper key id> \
    --secret-ref <keychain account name> --out scripts/fixtures/
```

on a machine that has both the real credentials (in the login keychain,
under mode PAPER) and real network access to Alpaca, then share the four
resulting JSON files back so they can be committed and analyzed.

Once real output exists, this README should be replaced by the four
captured files plus `capture_manifest.json`, and this note deleted.
