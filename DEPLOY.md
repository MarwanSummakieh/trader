# Deploying to a NAS (no terminal required)

Two ways to run this on a NAS. **Prebuilt image is recommended** — the NAS
never builds anything, so updates are just "pull + recreate" in the Docker UI.

In both cases you supply your own `.env` on the NAS. It is read when the
container starts and is **never** part of the image, so your keys never leave
the NAS.

## Recommended: prebuilt image from GitHub (ghcr.io)

Every push to `main` builds the image and publishes it to
`ghcr.io/marwansummakieh/trader:latest` (see
`.github/workflows/docker-publish.yml`).

**One-time, after the first successful workflow run:** make the package
public so the NAS can pull it without logging in — GitHub → your profile →
Packages → `trader` → Package settings → Change visibility → Public.

On the NAS:

1. Create a folder and put two files in it:
   - `docker-compose.ghcr.yml` from this repo, renamed to `docker-compose.yml`
   - your `.env` (see the header of that file for the minimum contents)
2. In the UGOS Docker app: **Project → Create**, point it at the folder.
3. It pulls `ghcr.io/marwansummakieh/trader:latest` and starts — no build.

**To update later:** in the Docker UI, pull the image again and recreate the
containers. No source upload, no build step.

## Alternative: build from GitHub on the NAS

If you prefer not to use a registry, Docker can clone and build the repo
itself. Use a `docker-compose.yml` whose services build from the git URL:

```yaml
    build: "https://github.com/MarwanSummakieh/trader.git#main"
```

You still add `.env` yourself. The downside: the NAS builds locally, and
picking up a new commit requires a genuine rebuild (not just "recreate").

## Verifying which build is running

- Bot container logs open with `Day Trading Bot — starting up v<VERSION>`.
- The dashboard header shows `v<VERSION>` next to the logo.
- `GET /api/status` returns the same `version`.

If the version is old after updating, the deploy didn't take — the image is
stale (rebuild / re-pull) rather than the code.

## The ledgers are separate from the code

Both trade databases live in the `trader_data` Docker volume — the stock
instance's `/app/data/ledger.db` and the crypto instance's
`/app/data/ledger-crypto.db` — which updates never touch. To wind down open
trades before switching brokers, use the **Exec/Terminal tab** of the
relevant bot container (`bot` or `bot-crypto`) in the Docker UI:

```
python close_all.py --yes
```

Or delete the `trader_data` volume for a clean start ($10,000 stocks /
$1,000 crypto).
