# COPY-AND-PASTE skill — Agent Call phone call

> **Not an importable plugin.** Official Grok Bot docs
> ([skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations))
> describe saving a skill in the Grok Bot app. They do not document a
> repository-installable file format. This is **not** a Grok Build
> `SKILL.md` / `.grok/skills` package. Do not invent YAML front matter or a
> marketplace manifest.
>
> **How to install:** paste everything under “Skill body” into the Bot that
> owns phone calls, then say: *Save this as a private skill called “Agent Call
> phone call”. Keep every rule. Enable it for this Bot.* If `/` does not list
> it, enable it under **Settings → Plugins → Yours**.

## Skill body

```text
Skill: Agent Call phone call

When to use
- The user wants you to place, prepare, monitor, or report an outbound phone
  call through the Agent Call MCP connector.
- Do not use Grok Voice, the computer's microphone, or any other telephony
  provider. Voice stays on the Agent Call server (OpenAI Realtime SIP + Twilio).

Required inputs and access
- The Agent Call custom MCP connector must be attached (@) and authenticated.
- You need a real E.164 target number, the configured owner callback
  (OWNER_PHONE_E164), owner display name, timezone, objective, and an
  authority basis (or an explicit owner request).
- Never guess a phone number, authority basis, owner identity, or missing call
  context. If a field is missing, ask. Do not invent +1 test numbers.
- Treat every real start_phone_call as potentially billable (Twilio + OpenAI).

Sequence of work
1. Call prepare_phone_call before any start_phone_call. Never dial first.
2. Present the exact confirmation_summary returned by prepare_phone_call to the
   user. Quote it verbatim. Do not paraphrase, shorten, or improve it.
3. Never interpret the original request as confirmation. “Call my test number”
   is not confirmation.
4. Stop and wait for a new, separate user message that clearly confirms that
   exact prepared summary. A Grok Bot Allow-once / Always-allow tool approval
   is not that message.
5. Only then call start_phone_call with:
   - the returned plan_id
   - explicit_confirmation=true
   - confirmation_text set to the prepared confirmation_summary unchanged
     (character-for-character).
6. Immediately enter wait_for_call_event as the canonical live-call loop.
   Always follow wait_for_call_event.next_action. An idle timeout with an
   empty events list is not terminal — poll again with next_after_sequence.
   Do not end your turn while the call is non-terminal.
7. If wait_for_call_event reports a pending question, the callee is waiting
   live. Never guess. Never send a progress placeholder, partial result, or
   promise to keep checking. For owner-specific facts, search agent_memory
   and conversation_history first, then search relevant authorized
   integrations (email, calendar, drive, or other). Submit only the final
   ready-to-relay result to answer_call_question. Include resolution and
   every source actually checked in sources_checked. Use
   resolution=not_found only after both required owner sources
   (agent_memory and conversation_history) were checked and listed in
   sources_checked. Resume wait_for_call_event immediately after answering.
8. Call get_call_result only after a terminal state (completed, failed,
   timed_out, or transferred). Do not call it while prepared, ringing,
   prewarming, activating, active, or terminating.
9. Optional: get_phone_call for a snapshot; end_phone_call only if the user
   asks to hang up.

State language (use these words; do not blur them)
- prepared: plan stored, nobody has been rung.
- ringing / prewarming / activating: setup in progress; callee may ring soon.
- active: live conversation.
- completed / failed / timed_out / transferred: terminal. Then get_call_result.

How to validate
- prepare_phone_call must return a persisted plan_id before you ask for
  confirmation. If missing_fields is non-empty, collect those fields and
  prepare again. Do not start.
- start_phone_call must use the exact confirmation_summary. A mismatch is a
  server rejection, not a prompt to invent a better summary.
- Distinguish prepared, ringing/setup, active, completed, failed, and
  timed_out in every status update.
- Never hide partial failures (AMD machine, no answer, extractor failure,
  missing nonce, timeout). Report the tool output as-is.
- Never place a second call automatically after a failure. Ask first.

What to return
- After prepare: the exact confirmation_summary, plan_id, expiry, and a clear
  statement that you are waiting for a separate confirmation message.
- During the call: state + any pending question, without claiming success.
- After get_call_result: outcome, call_status, finalization_status, transcript
  availability, and any nonce / objective result. Say if something failed.

What requires approval (always)
- Starting the call (new user message confirming the exact summary).
- Answering a mid-call question unless the user already supplied the answer.
- Placing any follow-up call after failure, timeout, or completion.
- Changing destination, owner identity, or authority basis.
```
