# Contributing

Contributions that improve simulation fidelity, observability, testing, documentation, or safety
are welcome.

1. Create a focused branch.
2. Add tests for behavior changes.
3. Run `pytest` and `ruff check .`.
4. Do not include real credentials, account data, private endpoints, or claims of profitability.

Live broker adapters must remain opt-in, fail closed on ambiguous order state, and include a
simulation test suite before they can be considered for inclusion.
