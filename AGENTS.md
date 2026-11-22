# StreamFrame change review

Before implementing every change, assess its impact on the viewer: the opening
hook, attention and retention through the clip, and the ending or payoff.
Consider indirect effects from ranking, import, editing defaults, and automation.
For changes with no viewer-facing impact, state why. Do not claim a retention
improvement without audience data.

For changes that affect the exported clip, verify a rendered example at phone
size, with sound on and off as relevant. Compare affected parts against the
approved reference and check that the hook remains readable, footage starts
immediately, and the ending does not repeat dialogue or linger unnecessarily.
Summarize the viewer impact in the handoff.

## Publishing handover

Social publishing is configured entirely by `.env` at the repository root. Read
[`docs/PUBLISHING.md`](docs/PUBLISHING.md) for setup and
[`docs/PUBLISHING-DECISIONS.md`](docs/PUBLISHING-DECISIONS.md) for why it works
this way before changing behaviour.

Prompt to start from:

> Set up this repository's social publishing on this machine. Read `README.md`,
> `START.md` and `docs/PUBLISHING.md` first. `.env` is already in the repository
> root; never commit it. Run `./streamframe publish doctor` and resolve what it
> reports. Install Python and Node dependencies as the README describes. Then run
> `./streamframe publish auth <platform>` for each configured platform, but stop
> and show me the authorization URL — a human must approve it and paste the
> callback back. Do not change `.env` values; if this machine publishes to a
> different Instagram Page, ask before editing
> `STREAMFRAME_INSTAGRAM_PAGE_ID`.

Rules for that work:

- **Authorization is the only step that needs a human.** Do not script consent,
  reuse another machine's tokens, or suppress the browser step.
- `publish doctor` exits non-zero until every listed platform is authorized.
  That is the expected state, not a configuration bug.
- Tokens live in `~/.streamframe/publish/credentials.json` (mode 0600) and are
  per machine by design: revocation is per grant, and a teammate must authorize
  with their own account so their posts are attributable to them.
- Do not add a scheduler, a background daemon, or a second server. Publishing
  runs in the existing server worker, or from the CLI.
- Delivery behaviour belongs in the ledger or a platform client. Read the
  invariants in `docs/PUBLISHING-DECISIONS.md` before touching `jobs.py`.
- Never publish to test the setup without saying so: a Reel is public and cannot
  be unlisted.
