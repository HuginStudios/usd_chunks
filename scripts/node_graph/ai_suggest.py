"""
AI suggestions for the node graph using ollama.
Runs queries in background threads, returns suggestions async.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable

_OLLAMA_MODEL = "qwen3:8b"


def _call_ollama(prompt: str, max_tokens: int = 200) -> str:
    """Blocking ollama call. Run in a background thread."""
    try:
        import ollama

        resp = ollama.chat(
            model=_OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            think=False,
            options={"temperature": 0.3, "num_predict": max_tokens},
        )
        return resp["message"]["content"].strip()
    except Exception:
        return ""


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences from an LLM code response."""
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    return text.strip()


def _get_node_names() -> list[str]:
    from . import nodes

    return [n.NODE_NAME for n in nodes.ALL_NODES]


def suggest_next_nodes(
    graph_context: str, callback: Callable[[list[str]], None]
) -> None:
    """Suggest next nodes based on current graph. Calls callback(list[str]) on completion."""
    node_names = _get_node_names()

    prompt = f"""You are a USD node graph assistant. Given the current graph state, suggest 3-5 nodes the user should add next.

Available nodes: {", ".join(node_names)}

Current graph:
{graph_context}

Reply with ONLY a JSON list of node names, e.g. ["Set Display Color", "Get Prim Info"]. No explanation."""

    def _run() -> None:
        resp = _call_ollama(prompt, max_tokens=100)
        try:
            start = resp.find("[")
            end = resp.rfind("]") + 1
            if start >= 0 and end > start:
                suggestions = json.loads(resp[start:end])
                if isinstance(suggestions, list):
                    callback([s for s in suggestions if s in node_names])
                    return
        except Exception:
            pass
        callback([])

    threading.Thread(target=_run, daemon=True).start()


def suggest_for_prim(
    prim_path: str, prim_type: str, callback: Callable[[list[str]], None]
) -> None:
    """Suggest what to do with a specific prim."""
    node_names = _get_node_names()

    prompt = f"""You are a USD node graph assistant. A user has a prim:
Path: {prim_path}
Type: {prim_type}

Suggest 3 nodes they'd most likely want to connect next.
Available nodes: {", ".join(node_names)}

Reply with ONLY a JSON list of node names. No explanation."""

    def _run() -> None:
        resp = _call_ollama(prompt, max_tokens=80)
        try:
            suggestions = json.loads(resp)
            if isinstance(suggestions, list):
                callback([s for s in suggestions if s in node_names])
                return
        except Exception:
            pass
        callback([])

    threading.Thread(target=_run, daemon=True).start()


def suggest_python_completion(
    code_before: str,
    code_after: str,
    inputs_desc: str,
    callback: Callable[[str], None],
) -> None:
    """Complete code at cursor position — fill in the middle."""
    prompt = f"""Complete this Python code at the cursor position marked with <CURSOR>.
Available variables: stage (Usd.Stage), inputs (dict with keys: in_1, in_2, in_3).
Set 'output' to return a value.
{f"Connected inputs: {inputs_desc}" if inputs_desc else ""}

```python
{code_before}<CURSOR>{code_after}
```

Reply with ONLY the text that goes at <CURSOR>. No explanation, no markdown, no repeating existing code."""

    def _run() -> None:
        callback(_strip_code_fences(_call_ollama(prompt, max_tokens=150)))

    threading.Thread(target=_run, daemon=True).start()


def suggest_python_code(
    current_code: str,
    inputs_desc: str,
    callback: Callable[[str], None],
) -> None:
    """Suggest Python code based on what the user has written so far."""
    prompt = f"""You are a USD Python assistant. Complete or improve this code.
Available variables: stage (Usd.Stage), inputs (dict with keys: in_1, in_2, in_3).
Set 'output' to return a value downstream.

{f"Connected inputs: {inputs_desc}" if inputs_desc else "No inputs connected."}

Current code:
```python
{current_code}
```

Write the completed code. Reply with ONLY Python code, no markdown fences, no explanation. Keep it concise."""

    def _run() -> None:
        callback(_strip_code_fences(_call_ollama(prompt, max_tokens=200)))

    threading.Thread(target=_run, daemon=True).start()


def get_graph_context(graph) -> str:
    """Serialize current graph state into a text description for the AI."""
    lines = []
    for node in graph.all_nodes():
        if not hasattr(node, "input_ports"):
            continue
        name = node.name()
        node_type = getattr(node, "NODE_NAME", type(node).__name__)
        props = {}
        if hasattr(node, "model") and hasattr(node.model, "custom_properties"):
            props = {k: v for k, v in node.model.custom_properties.items() if v}
        connections = []
        for port in node.output_ports():
            for cp in port.connected_ports():
                connections.append(f"{port.name()} → {cp.node().name()}.{cp.name()}")
        line = f"- {name} ({node_type})"
        if props:
            line += f" props={props}"
        if connections:
            line += f" connects: {', '.join(connections)}"
        lines.append(line)
    return "\n".join(lines) if lines else "Empty graph"
