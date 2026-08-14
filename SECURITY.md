# Security Policy

## Supported versions

Only the latest commit on `main` is supported. There are no tagged releases or
maintained release branches yet. Operators should run current `main` (or a
commit they have reviewed) rather than assuming a stable version line.

## Reporting a vulnerability

Please report security vulnerabilities **privately**. Do not open a public
issue, pull request, or discussion for an exploitable finding.

**Preferred, when the repository offers it:** GitHub private vulnerability
reporting. Open the repository **Security** tab and, if the button is present,
use **Report a vulnerability**. This channel is the GitHub-recommended path
once a maintainer has enabled it in repository settings. Do not assume the
button is available until you see it.

**Fallback:** If that button is missing, open a private channel with the
repository owner using a contact method listed on their
[GitHub profile](https://github.com/XiyaoWang0519). Do not invent or guess an
email address, and do not file a public issue for exploitable findings.

This project bridges live phone calls and holds third-party API credentials
(Twilio, OpenAI, and agent/MCP tokens), so responsible disclosure matters. A
public issue could expose an exploitable path before a fix ships. Reports will
be acknowledged as quickly as practical, with a fix and disclosure timeline
worked out with the reporter.

There is no bounty program; reports are still appreciated.

## What to include

- A description of the issue and its impact (for example: unauthenticated MCP
  access, webhook forgery, replay, destination-policy bypass, secret leakage).
- The commit hash or approximate date you tested.
- Reproduction steps that use **dummy** credentials and reserved/test phone
  numbers where possible.
- Expected versus actual behavior.

## What not to include unless the maintainer asks

Do **not** send real API keys, bearer tokens, webhook secrets, production
`.env` files, real E.164 phone numbers, call recordings, or transcripts in the
initial report. Redact them. If a proof truly requires a live artifact, say so
and wait for a private follow-up channel.

## Scope notes

Please preserve, and do not bypass in a public PoC, the existing controls:
webhook signature checks, OpenAI delivery-id replay protection, destination
policy, explicit call confirmation, and MCP bearer plus agent-user-id checks.
