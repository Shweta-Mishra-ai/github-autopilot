# Migrating

## V6 → V7

If you ran V6, three things now behave differently. All three exist to make the
bot quieter and more honest.

| Before | Now |
|--------|-----|
| A PR open posted **4 comments**, every push posted 2 more, none ever edited | **One sticky comment per PR**, edited in place. Collapsible sections. |
| Every secret finding opened an issue | Only **critical/high** severity does. Medium/low are logged. |
| A push with nothing to report still commented | The bot **stays silent** when it has nothing to say. |

Two more, less visible:

- **Repo memory is on by default.** It was opt-in, which meant it never worked in
  cloud deployments. Content is now redacted before storage — code bodies stripped,
  secret-shaped strings replaced. Set `MEMORY_ALLOW_CLOUD=0` for the old behaviour.
  See [docs/ai-system/memory.md](docs/ai-system/memory.md).
- **Unparseable model output no longer renders.** Previously a non-JSON response
  fell through to defaults and published "Score: 7/10 — no issues found" for a
  review that never ran. The bot now says it could not analyse the change.

---

## V7.1 → V7.2

No configuration changes are required — every V7.2 change is either a bug fix
or an opt-in feature that stays inert until you configure it.

Two things are worth turning on, and neither is on by default:

| Set | To get |
|-----|--------|
| `MEMORY_BACKUP_KEY` + `MEMORY_BACKUP_REPO` + `MEMORY_BACKUP_TOKEN` | Encrypted memory backup every 15 days. All three are required together — a key with no destination encrypts something and drops it, so a partial set counts as unconfigured. |
| `OLLAMA_HOST` | A local triage gate that skips the cloud review on changes with no reviewable logic. It can only ever skip work: every error path answers "review it". |

One thing worth **checking** rather than setting:

- `TRUSTED_PROXY_HOPS` decides which entry of `X-Forwarded-For` the per-IP rate
  limit trusts. The default of `1` is correct for Render and every similar
  platform. Set it to `0` if anything reaches the app directly — with no proxy
  in front, the whole header is caller-supplied.

Full detail for every change: [CHANGELOG.md](../CHANGELOG.md).
