# Security policy

TickForge Community intentionally ships without a live-broker implementation.

## Supported versions

Security fixes are applied to the latest release and the `main` branch.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting feature. Do not open a public issue for
credential exposure, authentication bypasses, order-state corruption, or unsafe execution paths.

## Operator responsibilities

- Never commit `.env`, broker credentials, certificate files, account snapshots, or trade ledgers.
- Bind the API to `127.0.0.1` unless it is protected by an authenticated private network.
- Use a random control token of at least 32 characters.
- Keep simulation mode enabled while developing or testing an adapter.
- Treat a broker timeout as an unknown order state; reconcile before submitting another order.
