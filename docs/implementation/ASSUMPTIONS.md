# Assumptions and unresolved Phase 0 blockers

Nothing in this file is an authorization to spend, to lock a production schema, to
launch an OAuth issuer, or to publish public pricing. Owners and deadlines below
are tracking fields only.

**Rule:** do not invent a jurisdiction, retention policy, pricing model, provider
contract, or vendor choice to unblock coding.

## Unresolved ADRs (002–010)

| ID | Topic | Owner | Deadline | Status |
| --- | --- | --- | --- | --- |
| ADR-002 | Launch policy: customer jurisdiction, number country/type, destination matrix, allowed/excluded use cases, AI/identity disclosure, calling hours, consent/suppression, concurrency/duration/spend defaults | Product owner; qualified counsel for telephony/consent | Before managed-provider spend and before Phase 4 | unresolved |
| ADR-003 | Provider commercial model: Twilio ISV/reseller/subaccount permission, support route, number/compliance obligations, production limits, caller-ID attestation and spam-label remediation | Product owner; Twilio account owner | Before any subaccount, number purchase, or parent-credential automation | unresolved |
| ADR-004 | Identity and billing: OIDC/MFA provider, billing provider, account ownership, data-processor terms | Product owner | Before Phase 3 control-plane identity work | unresolved |
| ADR-005 | Managed compute: cell platform, private networking, disposable cell storage, shared Postgres, per-workspace DB/workload identity, WIF vs thin credential gateway, secret store/KMS, region | Product owner / infra | Before Phase 3 staging environment and before Phase 4 cell provisioner | unresolved |
| ADR-006 | Durable workflows: queue/workflow technology and operational owner | Product owner / infra | Before Phase 3 foundation and Phase 4 saga | unresolved |
| ADR-007 | Data lifecycle: artifact classifications, default/configurable retention, export, deletion, backup expiry, support access, audit retention | Product owner; qualified counsel for privacy | Before production schema lock-in and before any customer data | unresolved |
| ADR-008 | SLO and support: beta/GA SLOs, on-call coverage, incident severity, customer support and status communication | Product owner | Before Phase 8 limited beta | unresolved |
| ADR-009 | Pricing and risk: subscription/prepaid/postpaid, trial behavior, reserves, caps, refunds/chargebacks, delinquency | Product owner | Before Phase 7 billing and before any billable managed start | unresolved |
| ADR-010 | MCP client matrix: OpenClaw/Hermes/Grok/other versions and OAuth registration/discovery compatibility | Product owner | Before Phase 5 managed MCP OAuth | unresolved |

## Section 19 owner questions (unresolved)

These are copied as questions only. Recommended engineering defaults from the
program plan are **not** recorded as decisions.

1. What exact business/customer jurisdiction launches first?
2. Which number country/type is provisioned first?
3. Which destinations are allowed?
4. Which use cases are allowed/prohibited? Is personal-assistant calling included?
   Are business sales, marketing, debt, healthcare, employment, government,
   financial, or political calls excluded?
5. What must the voice agent disclose about being automated/AI, the customer it
   represents, callback identity, and any recording/transcription, and when?
6. Are customers businesses only, verified individuals, or both? What KYC/manual
   review is required?
7. Does managed service record audio?
8. What are transcript/context/result retention defaults and customer controls?
9. What consent/authority evidence must the customer attest or upload per call/use case?
10. Subscription, prepaid credit, or postpaid usage? Is any billable free trial allowed?
11. Who absorbs provider fraud, disputes, refunds, chargebacks, and pricing changes?
12. Which identity, MFA, billing, workflow/queue, cloud/runtime, Postgres, object,
    secret/KMS, and observability vendors are approved?
13. Is warm cell cost acceptable for a maximum 20-workspace live beta, and who owns
    the expansion ADR?
14. Is one active call per workspace acceptable? What global concurrency/provider
    contract exists?
15. What beta and GA support/on-call hours and response targets can the team operate?
16. Which MCP clients/versions are officially supported, and is a Grok connector a
    launch requirement?
17. Does managed service support BYO Twilio/OpenAI later? If so, is it a separate
    edition/tier?
18. Which data region launches first, and are any data-residency commitments planned?
19. What customer export/deletion SLA is promised, and what backup expiry is acceptable?
20. Is the managed repository entirely private, and who may approve public-core /
    private-adapter boundary changes?
21. What product name/domain/issuer/resource URI is permanent enough for OAuth token
    audience and client configuration?
22. What should a callee hear when calling a managed caller ID back, and what
    disclosure/consent text is approved for the launch jurisdiction?

**Owner for all of the above:** product owner (unassigned in this repository);
qualified counsel where the question is legal/compliance.

**Deadline:** before the matching Phase 0 gate in the program plan (no managed
provider spend, production schema lock-in, OAuth issuer launch, or public pricing
until the relevant answers are recorded).

## Working assumptions that are not product decisions

These are implementation assumptions for the public self-host edition only.

| Assumption | Owner | Deadline | Notes |
| --- | --- | --- | --- |
| Evaluation/dummy profile never originates OpenAI or Twilio requests from `start_phone_call` | Engineering | Phase 1 exit | Enforced in `CallService.start`; covered by tests |
| Default evaluation listener is loopback unless CLI `--unsafe-bind` | Engineering | Phase 1 exit | Compose publishes host loopback; the process inside the container listens on all interfaces. Bind policy is CLI `--unsafe-bind` only (not `AGENT_CALL_UNSAFE_BIND`, not a Settings field). |
| SQLite remains the only durable store for self-host | Engineering | Until a later public persistence ADR | No tenant columns |
| Fly user template must not default-target the maintainer app | Engineering | Phase 1 exit | Maintainer overlay lives under `deploy/maintainer/` |
