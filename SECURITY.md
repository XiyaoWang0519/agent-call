# Security Policy

## Supported Versions

Only the latest commit on `main` is supported. There are no maintained release
branches; always run against current `main`.

## Reporting a Vulnerability

Please report security vulnerabilities privately rather than in a public
issue.

**Preferred:** GitHub private vulnerability reporting — open the
repository's **Security** tab and use **"Report a vulnerability"**.

**Fallback:** If that button is unavailable, open a private channel with the
repository owner via their [GitHub profile](https://github.com/XiyaoWang0519)
(email or other contact listed there). Do not file a public issue for
exploitable findings.

**Maintainer prerequisite before making this repository public:** enable
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
in repository settings (**Settings → Code security → Private vulnerability
reporting**) so the Security-tab flow above works for external reporters.

This project bridges live phone calls and handles third-party API credentials
(Twilio, OpenAI, Poke), so responsible disclosure matters — a public issue
could expose an exploitable path before a fix ships. We'll acknowledge
reports as quickly as we can and work with you on a fix and disclosure
timeline.

There is no bounty program; reports are still very much appreciated.
