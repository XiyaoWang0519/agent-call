# Manual live SIP canary

This is the operator validation guide linked from the [README](../README.md).

For unattended real calls to dedicated automated callee and owner numbers, use the
[automated live-phone harness](live-phone-runbook.md). It records received audio,
tests conversation and tools, and reports independent audio and provider-cleanup evidence.
The manual canary below still requires a person with a phone.

> [!CAUTION]
> The following commands place a **real billable call** to `OWNER_PHONE_E164`. They need real credentials, a public HTTPS URL, and a human with a phone. Do not run them in CI, on a fork against someone else's app, or casually while browsing the repo.

Before trusting a deployment, make it prove itself with a real call:

```bash
uv run python scripts/run_sip_canary.py --mode full
```

Answer the phone, say the printed nonce when asked, and talk over the assistant once. Then run the second variant to prove the deterministic finalizer does not depend on `record_call_outcome`:

```bash
uv run python scripts/run_sip_canary.py --mode no-outcome-tool
```

Both exit nonzero if any gate fails. The debug evidence endpoint they use requires `DEBUG_API_TOKEN`.

> [!NOTE]
> The voice model is fixed to `gpt-live-1`; the task backend is `gpt-5.6-terra`. The previous Realtime and mini-model configuration paths have been removed. Verify this exact integration with received audio before relying on it.
