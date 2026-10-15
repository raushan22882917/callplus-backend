"""Compiles a visual agent flow (Global/Start/Agent/End nodes + labeled
transition edges, as edited in the frontend's Flow Studio canvas) into the
system prompt the LLM actually receives in app/services/agent_engine.py.

There is no separate per-call state machine tracking "which node is the
call currently on" — the flow's structure and transition labels are written
directly into one compiled prompt as staged instructions, and the LLM
follows them turn by turn the same way it follows any other prompt section.
This is a deliberate scope choice: it makes editing the flow immediately
and honestly change real agent behavior, without the much larger project of
threading per-call node state through the realtime Go gateway and the
internal turn-loop endpoints.
"""

_TYPE_ORDER = {"global": 0, "start": 1, "agent": 2, "end": 3}


def compile_flow_to_system_prompt(nodes: list[dict], edges: list[dict]) -> str:
    if not nodes:
        return ""

    ordered = sorted(
        nodes,
        key=lambda n: (_TYPE_ORDER.get(n.get("type"), 2), n.get("stepNumber") or 0),
    )

    sections: list[str] = []
    for node in ordered:
        title = node.get("title") or node.get("id", "Untitled step")
        prompt = (node.get("prompt") or "").strip()
        outgoing = [e for e in edges if e.get("from") == node.get("id") and e.get("label")]

        block = f"## {title}\n{prompt}"
        if outgoing:
            lines = (f'- If "{e["label"]}", move to the next relevant step.' for e in outgoing)
            block += "\n\nTransitions from this step:\n" + "\n".join(lines)

        sections.append(block)

    return "\n\n".join(sections)
