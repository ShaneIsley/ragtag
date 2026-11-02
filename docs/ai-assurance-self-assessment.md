# AI Assurance Framework self-assessment

Policy Desk assessed against UNSW's AI Assurance Framework, which is based on the NSW Government's. UNSW's Information Governance Policy requires an AI self-assurance assessment before any AI system or tool is used, other than an approved enterprise system such as Copilot (Section 6, procedure 2.1). This is that assessment, done by the operator, against the framework's seven parts.

Policy Desk isn't a UNSW system, so UNSW hasn't reviewed it; it follows the framework by choice. The quotations below are from UNSW's published framework and policy, as indexed by Policy Desk itself.

## Summary

| Part | Assessment | Residual risk | The main gap |
|---|---|---|---|
| [Sensitive data](#1-sensitive-data) | Meets, on condition that questions hold no personal information | Low | Masking can miss health information |
| [Harm](#2-harm) | Meets | Low to medium | Known wrong answers on four questions, published |
| [Fairness](#3-fairness) | Partly meets | Low | No way yet to flag or challenge an answer; no stakeholder testing |
| [Business value](#4-business-value) | Meets, for a demonstration | Low | No measure of the benefit to people |
| [Transparency and accountability](#5-transparency-and-accountability) | Meets | Low | Flagged answers' human review is planned, not built |
| [Resilience and safety](#6-resilience-and-safety) | Meets, with recorded simplifications | Low | No Vendor Security Risk Assessment of Microsoft or AWS |
| [Compliance](#7-compliance) | Meets, with one recorded exemption | Low | The answer model may process questions outside the OECD (EX-01) |

**Decision:** proceed as an independent demonstration, ending at the end of November 2026, on three conditions the page states: questions shouldn't hold personal information, answers aren't UNSW advice, and the sources should be checked before relying on an answer. A UNSW deployment would need the actions at the end first.

**Live evidence.** The site's status page (`/status`) shows, for each part, what's measured now, rebuilt after every scheduled check: questions answered, details removed, the latest run's facts and groundedness, injection cases resisted, test cases by audience, sources cited, the standby model's check and the judges' agreement.

## 1. Sensitive data

**UNSW asks:** "The AI system or tool does not involve the use or creation of highly sensitive, sensitive, commercial and/or personal data without consent/approval." Data must be "stored in systems that match or exceed the data's classification level", and sharing personal information requires a Data Sharing Agreement.

**How Policy Desk answers:**
- **The data it uses is Public.** All 17 sources are UNSW's public pages, classified Public. Contact details and identifiers in them are masked before indexing.
- **It needs no personal information, and asks for none.** The page says so at the question box. Names, contact details and ID numbers are removed from every question before it goes anywhere, and the page says what it removed.
- **It creates none.** It makes no prediction or record about a person. No question or answer is stored, no IP address is recorded, and traces hold no text. The status page publishes counts only, with counts from 1 to 4 shown as "fewer than 5".
- **Where data goes** is in the [data-flow diagram](design.md#48-data-and-privacy) and the [privacy impact assessment](privacy-impact-assessment.md), section 4.

**Evidence:** [`privacy-impact-assessment.md`](privacy-impact-assessment.md); [`design.md`](design.md), section 4.8; the masking rule on the golden set's four personal-details cases, in every full run; the status page's "Use" section.

**Gaps:** health information isn't masked, and detection can miss a detail written unusually (privacy risk R1, rated low after controls). A person who types personal information anyway sends it to a model that may process it outside Australia.

## 2. Harm

**UNSW asks:** that the system "is respectful of fundamental human rights, the rights of the child and/or the environment", considering harms including "incorrect advice or guidance", "unauthorised use of sensitive or personal information", "unintended identification or misidentification", "infringement of intellectual property rights" and "inconvenience or delays", and especially "AI-informed decision-making".

**How Policy Desk answers:**
- **No decisions about people.** It answers questions about rules. It doesn't decide, predict or record anything about a person, so the framework's concern with AI-informed decision-making doesn't arise.
- **Incorrect advice is the main harm, and it's measured.** Answers come only from the retrieved passages; it declines when nothing relevant is found; every statement is cited, and an answer without a valid citation is declined. The gate checks 40 questions before every release and against the live service nightly and weekly. No expected fact may be contradicted, and the share of expected facts stated must stay above 68%; it has run between 74% and 86% with the current prompt.
- **Known wrong answers are published**, not hidden: four questions sometimes get a wrong detail (APA and Harvard formats, DeepSeek's research exception, the misconduct penalty). They're listed on the status page and in the [system card](system-card.md).
- **Intellectual property.** Sources are used under UNSW's site notice or fair dealing: short passages, always linked to the page.
- **Delays.** The first question after a quiet spell can take about 14 seconds; 9 in 10 answers take under 10.

**Evidence:** [`evaluation.md`](evaluation.md); the status page's "Answer quality" and "Known issues"; the [system card](system-card.md)'s known limitations.

**Gaps:** about one expected fact in six is left out of an answer. Someone who acts on an answer without reading its source could miss a condition; the page says to check the source.

## 3. Fairness

**UNSW asks:** that "the outputs of the AI system or tool are equitable, inclusive, accessible and free from bias and there is a mechanism for challenging outcomes", with diverse and representative data, bias detection, stakeholder engagement, and continuous monitoring "using performance measures and fairness targets".

**How Policy Desk answers:**
- **Representative data.** The sources are UNSW's own rules for staff, students and researchers, chosen to cover all three. No data about people is used, so bias against a group of people can't come from the data.
- **Measured by audience.** The golden set has questions from staff (14), students (10), researchers (3) and anyone (13). The status page shows, for the latest full run, how many of each audience's questions had no failed check.
- **Continuous monitoring.** The nightly and weekly checks, judge calibration and drift checks run against the live service.
- **Accessible.** The page works with a keyboard and a screen reader, with labelled controls, a live status region and visible focus. It hasn't been audited against WCAG 2.2 AA.
- **Challenging an answer.** Every answer shows its sources, so a person can check it against UNSW's own words.

**Evidence:** the status page's framework section (test cases by audience); [`evals/golden.yaml`](../evals/golden.yaml); [`design.md`](design.md), section 2 (N4).

**Gaps:** no way yet for a person to flag or challenge an answer: the thumbs-down and its human review are planned (Phase 3). No testing with the people it serves. No accessibility audit.

## 4. Business value

**UNSW asks:** that the system "aligns with UNSW's core values, Strategic Plan, improves services or efficiencies and there is an approved budget for the initial and ongoing costs". Improvement "should be measurable", and it's worth considering "whether there is an alternative way to deliver the benefit".

**How Policy Desk answers:**
- **The benefit.** UNSW's rules on AI are spread across policies, guidance pages, fact sheets and FAQs; a common question can need three of them. Policy Desk answers in seconds, with the passages.
- **Measured.** Questions answered and declined, over 7 and 30 days, on the status page.
- **Cost.** About A$5 to 8 a month, mostly a secrets store and model calls, with budget alerts on both clouds (A$5 Azure, US$5 AWS). The operator funds it, and is the budget owner.
- **The alternative without AI** is UNSW's own search and pages, which every answer links to.

**Evidence:** [`design.md`](design.md), sections 1 and 4.13; the status page's "Use".

**Gaps:** no measure of whether people find answers faster or act more correctly: no user study, and little traffic. For a UNSW deployment, a business case with a sponsor.

## 5. Transparency and accountability

**UNSW asks:** that the system "provides identifiable, explainable, reliable, and interpretable outputs to the user and there is clear accountability", considering "the role of human judgment in assessing AI outputs".

**How Policy Desk answers:**
- **Identifiable.** The page says the answer was written by AI and which model wrote it, and when the standby model answered instead and why.
- **Explainable and interpretable.** Every statement cites its passage; the page shows each source's title, section and text, and how the answer was made. Each answer has a trace ID that finds every step: which passages were found and cited, the prompt versions, the model, the time each step took.
- **Reliable, and shown to be.** The checks' results, the judges' calibration, drift findings and known issues are public on the status page. Every run is recorded with what it measured and what measured it.
- **Accountability.** The operator owns the system, its decisions ([`decisions.md`](decisions.md)), its privacy assessment, its exemptions and its [runbook](runbook.md). Every alarm emails the operator.
- **Human judgment.** It decides nothing about people; people read its sources and decide.

**Evidence:** the page itself; [`design.md`](design.md), sections 4.4 and 4.10; the status page.

**Gaps:** review of flagged answers by a person is planned (Phase 3), not built.

## 6. Resilience and safety

**UNSW asks:** compliance with the Cyber Security Policy's mandatory requirements, with "a Vendor Security Risk Assessment" as "a critical first step", because "even small AI system projects may have privacy or security vulnerabilities".

**How Policy Desk answers:**
- **Security.** One least-privilege role per function; secrets in Secrets Manager, read at start-up; the only way in is CloudFront, which signs every request to the app; a strict content security policy; non-root container images, scanned on push. The mapping to the Cyber Security Policy's principles is in [`infra/README.md`](../infra/README.md#security-practice).
- **Safety against misuse.** Prompt injection is tested in every run (7 cases); Azure's content filter rates every question and answer.
- **Resilience.** Bedrock takes over when Azure OpenAI fails; the index rebuilds in one ingest run; everything rebuilds from Terraform; a bad release rolls back with one apply ([runbook](runbook.md)).
- **Monitoring.** Nine alarms by email; nightly and weekly checks against the live service; judge calibration; drift checks.

**Evidence:** [`design.md`](design.md), section 4.7; [`infra/README.md`](../infra/README.md); the [runbook](runbook.md); the status page's "Right now".

**Gaps:** no Vendor Security Risk Assessment: it relies on Microsoft's and AWS's published data protection terms. CloudFront's default certificate also accepts TLS 1.0 and 1.1 from old browsers. The failover model answers less well than the main one.

## 7. Compliance

**UNSW asks:** that the system "complies with UNSW policies, relevant legislation, and the Ethical and Responsible Use of Artificial Intelligence at UNSW principles", including "privacy, discrimination, copyright and government information and records legislation".

**How Policy Desk answers:**
- **Privacy.** A self-assessed privacy impact assessment against the NSW Information Protection Principles (Information Governance Policy, clause 5.9). The page's "How your question is handled" says the same in plain words.
- **Information leaving the OECD** (clause 5.10). The answer model's global processing is recorded as exemption [EX-01](exemptions.md), with compensating controls, and stated on the page.
- **Copyright.** Fair dealing and UNSW's site notice: short passages, linked.
- **Records.** No record of a person or a question is kept. Logs and traces are kept 30 days; evaluation records until the shutdown.
- **Discrimination.** It makes no decisions about people.
- **Data classification.** Public data, in systems that match it.

**Evidence:** [`privacy-impact-assessment.md`](privacy-impact-assessment.md); [`exemptions.md`](exemptions.md); [`design.md`](design.md), section 5.

**Gaps:** clause 5.10 asks for a referral to UNSW's Chief Information Security Officer before information goes outside the OECD. An independent service can't make one, so it's recorded as an exemption instead.

## Before a UNSW deployment

| Action | For |
|---|---|
| A privacy impact assessment by Legal & Compliance; refer EX-01 to the CISO, or use an in-region model | Sensitive data, Compliance |
| Vendor Security Risk Assessments of Microsoft and AWS, or reliance on UNSW's agreements with them | Resilience and safety |
| Build the thumbs-down and its human review; test with staff and students | Fairness, Transparency |
| An accessibility audit against WCAG 2.2 AA | Fairness |
| A business case with a sponsor and a measure of benefit | Business value |
| Sign-in through UNSW's Entra tenant; a custom domain with TLS 1.2 or later; WAF rate rules | Resilience and safety |

## Record

| | |
|---|---|
| Assessed by | The operator (Shane), as a self-assessment |
| Assessed | 2 November 2026 |
| Reviewed by UNSW | No: Policy Desk isn't a UNSW system |
| Next review | At any change to the models, the data, or what the service is for; the service ends at the end of November 2026 |
