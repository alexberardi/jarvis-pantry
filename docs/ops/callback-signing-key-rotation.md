# Pantry callback signing key — 90-day manual rotation

The `PANTRY_CALLBACK_SIGNING_KEY` authenticates the `/v1/submissions/{id}/container-result` callback from `jarvis-pantry-runner` (GitHub Actions). It must match between the Pantry server env and the runner repo's GHA environment secret.

We rotate the key manually on a 90-day cadence. No automated rotation today — the audit-log story isn't built and the auth surface is small enough that manual rotation is fine.

## When to rotate

- Calendar: every 90 days from last rotation.
- Reactive: any time the key may have leaked (compromised host, exposed env-var dump, ex-maintainer with access).

## What the key needs to be

- ≥ 32 bytes of cryptographic randomness.
- ASCII-safe so it fits in env vars without escaping.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## Rotation procedure

Plan for a 1–2 minute window where in-flight callbacks may 401 — that's expected. Stalled `awaiting_container` rows are caught by the callback-timeout watcher and re-dispatched (see #22), so no submission gets stuck.

1. Generate the new key (command above). Copy it once.
2. **In `jarvis-pantry-runner`** — update the `pantry-callback` GHA environment secret `PANTRY_CALLBACK_SIGNING_KEY`:
   ```bash
   gh secret set PANTRY_CALLBACK_SIGNING_KEY \
     --env pantry-callback \
     --repo alexberardi/jarvis-pantry-runner
   ```
   (Paste the new value when prompted.) Verify it took:
   ```bash
   gh secret list --env pantry-callback --repo alexberardi/jarvis-pantry-runner
   ```
3. **In Pantry** — update the deployment's `PANTRY_CALLBACK_SIGNING_KEY` env var (Fly secrets, Docker compose `.env`, or whatever the active deploy uses) to the same value. Redeploy.
4. **Verify a round-trip**: submit a small test command and watch the logs for a 200 on `/v1/submissions/.../container-result`.
5. **Note the rotation date** in this doc's changelog below.

## Recovery if the keys diverge

Symptoms: all `/v1/submissions/.../container-result` posts return 401, submissions stack up in `awaiting_container`, the callback-timeout watcher kicks in and re-dispatches them.

Fix: re-run step 2 + 3 with a single fresh key on both sides. The re-dispatched submissions get a new nonce on each retry, so once both sides agree on the key they finalize on the next callback.

## What this key does NOT do

- It is not a database secret.
- It is not used by mobile / web clients — those auth via GitHub OAuth.
- It is not used by the local Docker runner (`PANTRY_CONTAINER_RUNNER=local`); rotation is a no-op there.

## Changelog

| Date | Rotated by | Notes |
|------|-----------|-------|
| 2026-05-19 | (initial) | First key set during #25 / #26 deploy. |
