System map · v1

# Fitness Agent System Map

Every layer, agent, store, tool server and trigger on one canvas. Signals come up from the bottom, authority comes down from the control plane, and one event follows the numbered loop.

Scroll sideways on a narrow screen.

1. 1**Event arrives**Wearable sync, cron tick, anomaly, or a chat message lands on the event bus and wakes the control plane.
2. 2**Context pack**Plan slice, recent metrics, top memories and active experiments are assembled for this event.
3. 3**Agents propose**Relevant specialists run in parallel and return typed proposals. They never write state directly.
4. 4**Reconcile**Conflicts get at most two rounds of debate, then the Orchestrator rules.
5. 5**Critic + policy**The Safety Critic reviews; hard constraints run as code; watchdogs can force a stop.
6. 6**Tier gate**Auto-apply, notify and wait out a veto window, ask for approval, or escalate to a professional.
7. 7**Reflect**Later, the outcome is compared with the prediction and the lesson goes into procedural memory.