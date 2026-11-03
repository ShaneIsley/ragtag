# Privacy impact assessment

A self-assessment, recorded 31 October 2026.

UNSW's Information Governance Policy (§5.9) requires UNSW Legal & Compliance to do a privacy impact assessment before personal or health information is transferred outside NSW. Policy Desk isn't a UNSW system, so this assessment is written as that rule would require if it were. It follows the shape of the NSW Information and Privacy Commission's guidance and the Information Protection Principles in the *Privacy and Personal Information Protection Act 1998* (NSW) ("PPIP Act"). It hasn't been reviewed by UNSW Legal & Compliance, and it isn't legal advice.

## Summary

- Policy Desk needs no personal information, and isn't designed to collect any. Personal information reaches it only if someone types it into a question. Each request's IP address reaches the app but isn't recorded (since 1 November 2026).
- A question can leave NSW. The answer model (Azure OpenAI, global deployment) may process it in any country where Microsoft runs the model. Embeddings are processed in Australia, possibly outside NSW. From Phase 2, the failover model (Bedrock) runs in Sydney or Melbourne.
- Since 1 November 2026, Presidio removes names, contact details and identifiers (such as zIDs) from every question before it leaves the app, and the page says what it removed. Health information isn't detected, so the notice still asks people to leave personal details out.
- **Residual risk: low.** The project owner accepts it for a demonstration used by a handful of invited people, on the conditions at the end.

## 1. Is an assessment needed?

Yes, for two reasons:
- Questions are free text, so people may type names, student numbers or contact details. They may also type health information, for example when asking about AI tools and an adjustment for a disability. Health information is covered by the *Health Records and Information Privacy Act 2002* (NSW) instead of the PPIP Act.
- Questions are sent to services outside NSW, which is what §5.9 is about.

## 2. What the system does

Policy Desk answers questions about how AI may be used at UNSW, from 17 of UNSW's public policies and guidance pages. A person types a question on the page. The service searches the documents, asks a language model to answer from the passages it found, and shows the answer with its sources. There are no accounts or sign-in for people asking questions. Only the admin view (Phase 3) has a sign-in.

## 3. Personal information the system could handle

| Information | How it arrives | Needed? | Where it's kept, and for how long |
|---|---|---|---|
| The question | Typed by the person asking | No personal information is needed | Not kept by the service: it's held in memory while the answer is made, then discarded. Microsoft may store a question that its automated abuse checks flag, in Australia, for review by its staff |
| The answer | Written by the model; may repeat personal information from the question | No | Sent back to the browser only. Not kept |
| IP address | Every request | No | Not recorded. Until 1 November 2026 the app's request log may have recorded it, for 30 days; that log is now off (decisions, "Request log") |
| A trace of each answer (since 1 November 2026) | Every request | To find faults and monitor quality | CloudWatch Logs (`aws/spans`) in Sydney, for 30 days. IDs, timings, token counts, versions, the passages found and cited, the kinds of details masked (such as "name"), and Azure's ratings above safe (such as "self_harm:low"). Never the question, the answer or an IP address |
| Counts on the status page (since 2 November 2026) | Counted from the traces after each scheduled run | To show the service is checked and used | `status/snapshot.json` in the records bucket, Sydney, and the public status page (`/status`): counts of visitors' questions over the last 7 and 30 days, answered, declined, with details removed, and answered by the standby model. No text, times, IDs or safety ratings. Counts from 1 to 4 show as "fewer than 5". The evaluation's own questions aren't counted |
| Names and roles in the documents | UNSW's public pages, at ingest | No, but they come with the documents | The search index, in Australia East. Information in a publicly available publication isn't personal information under the PPIP Act (s 4(3)(b)). Since 1 November 2026, Presidio masks contact details and identifiers in the documents too, except the address formats the pages give for signing in (`zID@ad.unsw.edu.au`, `name@unsw.edu.au`, 3 November 2026), which are nobody's address |

Planned features change this table. Each needs this assessment revisited before it ships:
- **Conversation memory (Phase 2 step 5).** The page will send the last few turns with each question. They're still not stored on the server.
- **Telemetry (built 1 November 2026, exported from Lambda since the same day).** Spans carry IDs, token counts, versions and safety flags, never question or answer text and no client address; tests check both. Locally they go to Jaeger in compose; from Lambda to CloudWatch Transaction Search in Sydney (the table above).
- **Flagged answers (Phase 3 step 1).** A thumbs-down will store the masked question and its answer in DynamoDB in Sydney for 90 days.

## 4. Where a question goes

The data-flow diagram in [`design.md`](design.md#48-data-and-privacy), section 4.8, shows each step with its classification.

| Step | Service | Where processing happens | Protection |
|---|---|---|---|
| Browser to the service | Amazon CloudFront | The edge location nearest the person asking (in Australia, for someone in Australia) | HTTPS. Nothing is cached or logged at the edge |
| The app | AWS Lambda | Sydney (NSW) | TLS 1.2 from CloudFront; each request is signed for this distribution |
| Turning the question into a vector | Azure OpenAI embeddings, `Standard` deployment, Australia East resource | Australia. Microsoft may process between Australian regions for capacity, so possibly outside NSW | TLS 1.2. Not stored, not used for training |
| Search | Azure AI Search | Australia East (NSW) | TLS 1.2. Query text isn't logged (no diagnostic logging is set up) |
| Rewrite and answer | Azure OpenAI chat, `GlobalStandard` deployment | Any country where Microsoft runs the model, including countries outside the OECD (see [EX-01](exemptions.md#ex-01-the-answer-model-may-process-questions-outside-the-oecd)) | TLS 1.2. Not stored or used for training. Flagged questions are stored in Australia ([Microsoft](https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/openai/data-privacy)) |
| Failover or second answer (since 1 Nov 2026): when Azure OpenAI doesn't respond, or the person chooses "Try the other model" | Amazon Bedrock, AU inference profile | Sydney or Melbourne | TLS 1.2. Bedrock doesn't store prompts or answers, or use them for training |
| Back to the browser | CloudFront | As above | HTTPS |

## 5. Assessment against the Information Protection Principles

| Principle (PPIP Act) | Assessment |
|---|---|
| Collect only for a lawful purpose, and only what's reasonably necessary (s 8) | The service needs no personal information, and the page asks people not to enter any. No IP addresses are recorded. **Meets** |
| Collect from the person, and tell them what will happen to it (ss 9–10) | The page's "How your question is handled" section says who runs the service, where questions go, what's kept and what to leave out, and it sits next to the question box. **Meets** |
| Collection relevant, not excessive or intrusive (s 11) | One question at a time, capped at 500 characters. No accounts, cookies or tracking. A light or dark theme choice is kept in the visitor's own browser and never sent. **Meets** |
| Store securely, keep no longer than needed (s 12) | Questions and answers aren't stored. Logs are encrypted, kept for 30 days, and readable only by the AWS account's administrators. **Meets** |
| Access, correction and accuracy (ss 13–16) | Nothing is kept that's linked to a person. Answers are about UNSW's rules, not about people. **Not applicable** |
| Use only for the purpose collected (s 17) | A question is used only to answer it. **Meets** |
| Disclose only within limits (s 18) | Microsoft and AWS process questions as service providers, under their data protection terms. **Meets** |
| Disclosure outside NSW (s 19(2); for health information, HPP 14) | Can happen (section 4). Section 19(2) allows it on grounds including that the recipient is bound by protections substantially similar to the principles, or that reasonable steps have been taken so it won't handle the information inconsistently with them ([IPC guideline](https://www.ipc.nsw.gov.au/guidance-transborder-disclosure-principle-section-192)). Microsoft's [Data Protection Addendum](https://aka.ms/DPA) and the AWS Data Processing Addendum are those protections. **Meets, with the controls in section 6** |

UNSW's Information Governance Policy §5.10 adds a step for countries outside the OECD. That is exemption [EX-01](exemptions.md#ex-01-the-answer-model-may-process-questions-outside-the-oecd).

## 6. Risks and controls

| # | Risk | Likelihood | Consequence | Rating | Controls | Residual |
|---|---|---|---|---|---|---|
| R1 | Someone types personal or health information, which is then processed outside NSW, or outside Australia | Possible | Minor | Medium | A notice at the question box and on the page; nothing stored; 500-character cap; masking of names, contact details and identifiers before the question leaves the app (since 1 Nov 2026), with the page saying what was removed | Low |
| R2 | Masking a name could also catch a tool or service name, such as Copilot or Scout, and spoil the answer; or detection could miss a detail, such as a phone number written without its area code | Possible | Minor | Medium | An allow-list of tool and service names, tested on the golden set's questions; the gate's masking rule on its personal-details cases; the notice still asks people to leave personal details out. Names in the documents aren't masked: they're public, and answers need the roles and units they name | Low |
| R3 | IP addresses kept in logs | Closed | | | The request log is off since 1 November 2026, so no address is recorded | None |
| R4 | An answer repeats personal information from the question | Possible | Negligible | Low | Shown only to the person who asked; not kept | Low |
| R5 | Microsoft flags a question and its staff read it | Rare | Minor | Low | Stored in Australia; reviewers work from secure workstations under just-in-time approval ([Microsoft](https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/openai/data-privacy)) | Low |
| R6 | Someone gets into the AWS account and reads the logs | Rare | Negligible (traces and error types; no text or IP addresses) | Low | MFA on the AWS root user (condition 4); one least-privilege role per function; no long-lived keys in CI; budget alerts on unusual spend | Low |
| R7 | A trace shows that a question was rated, for example, self-harm low, and someone with access to the AWS account matches it by time to the request log's IP address | Closed | | | The request log is off since 1 November 2026, so there's no address to match | None |
| R8 | Someone who knows a person asked a question that week recognises it in a count on the status page | Rare | Negligible: a count says nothing about what was asked | Low | Counts only, over 7 or 30 days, with no text, times or safety ratings; counts from 1 to 4 show as "fewer than 5" | Very low |

## 7. Conditions

The residual risk is accepted on these conditions:
1. The notice stays next to the question box, and the page's description of what happens to a question matches what the system does.
2. The link went only to invited people until masking shipped. Masking shipped on 1 November 2026 and the notice was updated, so this condition is met; sharing the link more widely is the owner's call.
3. Revisit this assessment before any change that stores, logs or sends questions somewhere new, including the three planned features in section 3. Update "Data residency" in `infra/README.md` in the same change.
4. MFA is on for the AWS root user and the Azure account that administers the service. The owner confirms this at each review.

## 8. Record

| | |
|---|---|
| Assessed by | The project owner (Shane), as a self-assessment |
| Recorded | 31 October 2026; revisited 1 November 2026 for masking, for traces exported from Lambda, and for turning off the request log; and 2 November 2026 for the counts on the status page (R3 and R7 closed) |
| Reviewed by UNSW Legal & Compliance | No: Policy Desk isn't a UNSW system |
| Next review | At the end of Phase 2, or at any change in condition 3 |
| Service ends | The end of November 2026, when every store above is deleted (`infra/README.md`, Decommissioning) |
