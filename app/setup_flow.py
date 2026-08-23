"""
app/setup_flow.py — create the GitHub App in one click, instead of six steps.

WHY
  The old quickstart asked the operator to create an App by hand: set a webhook
  URL, generate a secret, tick four permission groups, subscribe to four
  events, download a .pem. Every one of those is a place to get it wrong, and
  getting the permissions wrong produces the failure this project spent a
  release fixing — commands that refuse to run and cannot say why.

  GitHub's App-manifest flow removes the whole class: the manifest declares the
  webhook URL, the events and the permissions, GitHub creates the App from it,
  and hands back the credentials. There is nothing to tick.

THE FLOW
  1. GET  /setup            a page whose form POSTs the manifest to GitHub
  2.                        GitHub creates the App and redirects back with ?code
  3. GET  /setup/callback   exchanges the code for App ID, private key, secret

HANDLING OF SECRETS
  Step 3 returns a private key and a webhook secret. They are rendered once, in
  the operator's own browser, and never logged, stored or sent anywhere — the
  page says so, because a credential shown without that promise is one the
  reader has to assume leaked. `state` is a random value checked on return, so
  a callback the operator did not initiate cannot be replayed into this page.

WHAT THE MANIFEST ASKS FOR
  Only what the code calls. If the setup doctor later reports the collaborator
  probe failing, the App needs one more repository permission — the doctor
  reports that empirically rather than this file asserting a permission name it
  cannot verify.
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets

log = logging.getLogger(__name__)

_STATES: set[str] = set()
_MAX_STATES = 32


def _public_url() -> str:
    """Base URL GitHub should call back to. PUBLIC_URL, else the request host."""
    return (os.environ.get("PUBLIC_URL", "") or "").strip().rstrip("/")


def build_manifest(base_url: str, name: str = "") -> dict:
    """
    The App GitHub should create.

    Permissions are the ones the endpoints in this codebase actually call.
    Events are the four the handlers subscribe to — a fifth would be delivered
    and dropped, which costs the operator quota for nothing.
    """
    return {
        "name": name or "GitHub Autopilot",
        "url": "https://github.com/Shweta-Mishra-ai/github-autopilot",
        "hook_attributes": {"url": f"{base_url}/webhook", "active": True},
        "redirect_url": f"{base_url}/setup/callback",
        "public": False,
        "default_permissions": {
            "metadata": "read",
            "issues": "write",
            "pull_requests": "write",
            "contents": "write",
            "actions": "write",
            "checks": "read",
            "security_events": "read",
        },
        "default_events": ["push", "pull_request", "issues", "issue_comment"],
    }


def new_state() -> str:
    """A one-shot CSRF token for the round trip to GitHub."""
    state = secrets.token_urlsafe(24)
    if len(_STATES) >= _MAX_STATES:
        _STATES.clear()  # bounded; a stale state simply has to start over
    _STATES.add(state)
    return state


def consume_state(state: str) -> bool:
    """True once per state. A replayed callback fails here."""
    try:
        _STATES.remove(state)
        return True
    except KeyError:
        return False


def setup_page(base_url: str) -> str:
    """The one-button page. No secret is present on it."""
    manifest = json.dumps(build_manifest(base_url))
    state = new_state()
    perms = build_manifest(base_url)["default_permissions"]
    rows = "\n".join(
        f"<tr><td><code>{html.escape(k)}</code></td><td>{html.escape(v)}</td></tr>"
        for k, v in sorted(perms.items())
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<title>GitHub Autopilot — one-click setup</title>
<meta name="robots" content="noindex"/>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
        max-width: 46rem; margin: 3rem auto; padding: 0 1.25rem; }}
 h1 {{ font-size: 1.6rem; margin-bottom: .25rem; }}
 .sub {{ opacity: .75; margin-top: 0; }}
 button {{ font: inherit; font-weight: 600; padding: .7rem 1.4rem; border-radius: .5rem;
           border: 0; background: #2da44e; color: #fff; cursor: pointer; }}
 button:hover {{ background: #2c974b; }}
 table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
 td, th {{ text-align: left; padding: .35rem .6rem; border-bottom: 1px solid #8883; }}
 code {{ background: #8882; padding: .1rem .35rem; border-radius: .25rem; }}
 .note {{ border-left: 3px solid #8884; padding-left: .9rem; opacity: .85; }}
</style></head><body>
<h1>Set up GitHub Autopilot</h1>
<p class="sub">One click. GitHub creates the App with the right permissions,
webhook URL and events already set &mdash; nothing to tick.</p>

<form action="https://github.com/settings/apps/new?state={html.escape(state)}" method="post">
  <input type="hidden" name="manifest" value='{html.escape(manifest)}'/>
  <button type="submit">Create the GitHub App</button>
</form>

<h2>What it will ask for</h2>
<table><tr><th>Repository permission</th><th>Level</th></tr>{rows}</table>
<p>Events: <code>push</code>, <code>pull_request</code>, <code>issues</code>,
<code>issue_comment</code> &mdash; the four the handlers subscribe to.</p>

<p class="note">The next screen shows your App ID, private key and webhook
secret <strong>once</strong>. They are rendered in your browser and never
logged or stored by this server. Keep that tab open until you have pasted them
into your host's environment variables.</p>
</body></html>"""


def exchange_code(code: str) -> tuple[dict, str]:
    """
    Trade the temporary code for real credentials. Returns (data, error).

    The code is single-use and short-lived, so a failure here is nearly always
    a stale or reused link rather than a misconfiguration.
    """
    import requests

    try:
        r = requests.post(
            f"https://api.github.com/app-manifests/{code}/conversions",
            headers={"Accept": "application/vnd.github+json"},
            timeout=20,
        )
    except Exception as e:
        return {}, f"Could not reach GitHub: {type(e).__name__}"
    if r.status_code >= 400:
        return (
            {},
            f"GitHub refused the exchange ({r.status_code}). The code is single-use — start again at /setup.",
        )
    try:
        return r.json(), ""
    except ValueError:
        return {}, "GitHub returned a response this app could not parse."


def credentials_page(data: dict, base_url: str) -> str:
    """Render the credentials once, with what to do with each."""
    app_id = html.escape(str(data.get("id", "")))
    slug = html.escape(str(data.get("slug", "")))
    secret = html.escape(str(data.get("webhook_secret", "")))
    pem = html.escape(str(data.get("pem", "")))
    install = html.escape(f"https://github.com/apps/{slug}/installations/new") if slug else "#"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<title>GitHub Autopilot — your credentials</title>
<meta name="robots" content="noindex"/>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
        max-width: 52rem; margin: 3rem auto; padding: 0 1.25rem; }}
 textarea {{ width: 100%; height: 11rem; font: 12px ui-monospace,SFMono-Regular,Menlo,monospace; }}
 input {{ width: 100%; font: 13px ui-monospace,monospace; padding: .4rem; }}
 label {{ display:block; font-weight:600; margin-top:1.1rem; }}
 .warn {{ border-left: 3px solid #d1242f; padding-left: .9rem; }}
 code {{ background: #8882; padding: .1rem .35rem; border-radius: .25rem; }}
 a.btn {{ display:inline-block; margin-top:1.5rem; font-weight:600; padding:.7rem 1.4rem;
          border-radius:.5rem; background:#2da44e; color:#fff; text-decoration:none; }}
</style></head><body>
<h1>App created &mdash; copy these now</h1>
<p class="warn"><strong>Shown once.</strong> This page is not stored and cannot
be reloaded. Paste each value into your host's environment variables before
closing the tab.</p>

<label for="a">GITHUB_APP_ID</label>
<input id="a" value="{app_id}" readonly onclick="this.select()"/>

<label for="s">GITHUB_WEBHOOK_SECRET</label>
<input id="s" value="{secret}" readonly onclick="this.select()"/>

<label for="p">GITHUB_PRIVATE_KEY</label>
<textarea id="p" readonly onclick="this.select()">{pem}</textarea>

<p>You still need <code>GROQ_API_KEY</code> from
<a href="https://console.groq.com">console.groq.com</a> (free), and
<code>REDIS_URL</code>, which the Render blueprint wires automatically.</p>

<a class="btn" href="{install}">Install the App on your repositories &rarr;</a>

<p style="margin-top:2rem">Then check it end to end:
<code>{html.escape(base_url)}/setup/doctor?repo=owner/name&amp;installation_id=&lt;id&gt;</code>
&mdash; it probes each capability and names anything that will not work.</p>
</body></html>"""
