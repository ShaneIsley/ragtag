# Exemptions

Where Policy Desk departs from the UNSW policies it follows by choice (see "Security practice" in [`infra/README.md`](../infra/README.md#security-practice)). Policy Desk isn't a UNSW system, so no UNSW officer has approved these. Each entry says who would have to.

UNSW's Cyber Security Policy (v4.1, §5.1) requires any non-compliance to be approved under the Cyber Security Standard – Framework Exemption, "including a mandatory risk assessment and agreed compensating controls". That standard is published only to staff, so the fields below follow common practice: the requirement, the departure, a risk assessment, the controls, an owner, and when the entry is reviewed and ends.

When an exemption affects what happens to someone's question, the page says so under "How your question is handled" (`app/templates/index.html`). Change both together.

## EX-01: The answer model may process questions outside the OECD

| | |
|---|---|
| **Requirement** | UNSW Information Governance Policy §5.10: "Before transmission of digital information to non-OECD member countries (including for processing or storage) occurs, the proposed transmission must be referred to the Chief Information Security Officer or their delegate for additional control requirements." |
| **Departure** | The chat model (gpt-5-mini) is a `GlobalStandard` deployment. Microsoft says that for Global deployments "prompts and responses may be processed in any geography where the relevant model sold by Azure is deployed" ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/ai-foundry/responsible-ai/openai/data-privacy), updated 18 May 2026). It doesn't say which geographies, and Azure has regions in countries outside the OECD. Data stored at rest, including Microsoft's abuse-monitoring store, stays in Australia, the resource's geography. |
| **What is sent** | Two calls per question, both to the chat deployment: the question, for the search rewrite; then the question and up to 12 passages from public UNSW documents, for the answer. No name, account, IP address or other identifier of the person asking is sent. |
| **Why** | Australia East stopped accepting new in-region pay-as-you-go ("Standard") chat deployments in September 2026. In-region provisioned throughput is reserved capacity priced for production, and Azure has no Australian data zone. Bedrock's AU profile (Sydney or Melbourne) is the in-country option; it is the failover model (Phase 2), not the main one (`docs/decisions.md`, "Answer model"). Re-examined 31 October 2026 and kept: the design keeps Azure OpenAI as the main model, and the exemption is the recorded way to handle the departure. |
| **Classification** | Documents: Public. Questions: meant to be Public. A question holds personal information only if the person asking ignores the page's notice. |
| **Risk** | That a question containing personal information is processed in a country outside the OECD. Likelihood: unlikely (a handful of invited users, a notice at the question box, routing that Microsoft controls). Consequence: minor (one question, not kept by this service, not used for training, protected by Microsoft's data protection terms). Rating: **low**. |
| **Compensating controls** | 1. A notice at the question box, and a "How your question is handled" section on the page, ask people not to include personal details and say the question may be processed outside Australia and outside the OECD (in place). 2. Questions and answers aren't stored by the service, and logs don't contain them (in place). 3. Questions are capped at 500 characters (in place). 4. Microsoft's [Products and Services Data Protection Addendum](https://aka.ms/DPA) applies; prompts aren't used for training; abuse-monitoring data is stored in Australia (in place). 5. Until masking shipped, the link went only to invited people (met). 6. Presidio masks names, contact details and identifiers in every question before it's sent, and the page says what it removed (in place since 1 November 2026). |
| **Residual risk** | Low |
| **Owner** | Shane (project owner) |
| **Would need approval from** | UNSW's Chief Information Security Officer or delegate |
| **Recorded** | 31 October 2026 |
| **Review** | At the end of Phase 2, or sooner if: the service starts storing questions (Phase 3 feedback); the link is shared beyond invited people; Microsoft offers an Australian data zone or in-region Standard deployments again. |
| **Ends when** | The chat model moves to in-country processing (Bedrock's AU profile or Azure in-region), or the service is decommissioned (planned for the end of November 2026) |
