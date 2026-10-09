# Conversation design: workspace for the designer

You own everything in this folder. The engineer's code reads two of these files directly:

| File | Used by | What happens when you edit it |
|---|---|---|
| `persona.md` | injected into the LLM system prompt | Sol's voice changes on the next message |
| `responses.json` | the backend, for every message that must NOT depend on the LLM (confirmation questions, "waiting for approval", approved/rejected/expired notices, errors) | wording changes immediately; keep every `{placeholder}` that the message already has |

Everything else here is design documentation the engineer builds tests from:

- `intents.md`: the intent list (also becomes eval test cases)
- `journeys.md`: customer journey maps
- `flows/`: dialogue-flow diagrams (export from Figma/FigJam as PNG or link)

## Why `responses.json` exists
Some sentences are too important to leave to a language model, especially "a specialist has to approve this first". The code sends those sentences itself, using your wording.
