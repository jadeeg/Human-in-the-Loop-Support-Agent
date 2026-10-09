# Intents (starter list: designer to refine)

| Intent | Example utterances | Required info (slots) | Risk | Outcome |
|---|---|---|---|---|
| cancel_order | "cancel my order", "I changed my mind" | order number, email | high | confirmation, then approval request |
| request_refund | "I want my money back", "refund order 12348" | order number, email | high | confirmation, then approval request |
| track_shipment | "where is my package?" | order number, email | low | status + tracking number |
| policy_question | "can I cancel after shipping?" | none | low | answer from policy |
| human_handoff | "let me talk to a person" | none | n/a | escalate |
| out_of_scope | "what's the weather?" | none | n/a | polite redirect to what Sol can do |

## Open design questions
- Customer says "I want my money back" with no order: cancel or refund? What do we ask?
- How many times do we re-ask for a missing slot before offering a human?
- What does Sol say when the policy blocks a request but an exception might exist?
- Tone for upset customers: when do we hand off immediately?
