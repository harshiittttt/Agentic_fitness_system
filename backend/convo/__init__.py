"""The Conversation Core: an LLM-led voice conversation for the gym coach (docs/CONVERSATIONAL_AGENT_PLAN.md, Phase 1; contracts in docs/CONVO_CONTRACTS.md).

    turn.py      one turn as an event stream (gates -> state + dialogue -> streaming model -> grounded sentences / guarded tools)
    session.py   dialogue per connection (spoken text only, interruptions, idempotent turn ids)
    state.py     the exact workout state block, language detection, long-term facts
    prompt.py    the system prompt (persona, language mirror, tool rules, how a coach talks)
    tools.py     the ~11 voice tools wrapping live.py / voice_control / fitness_os
    guards.py    deterministic safety: ASR confidence, heard numbers, plausibility, skip rule
    ground.py    numbers in speech, sentence streaming, per-sentence grounding
    llm.py       streaming Gemini client with a live-model fallback chain
    proactive.py events from the app (rest over, idle) and the talking budget

VOICE_BRAIN=legacy|converse (default legacy) picks which brain /convo/turn runs; `?brain=` overrides per request.
"""
