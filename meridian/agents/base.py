"""Socle commun des agents.

Un agent est un composant déterministe, testable et auditable. Il ne passe
jamais d'ordre : il produit un diagnostic ou une intention, le RiskAgent puis
l'ExecutionAgent décident de la suite. Cette séparation est volontaire — c'est
ce qui rend le système explicable et arrêtable.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class Agent:
    name = "agent"
    role = ""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name}>"


@dataclass
class AuditEvent:
    ts: str
    agent: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)


class AuditLog:
    """Journal append-only des décisions, lisible par un humain."""

    def __init__(self, path: str | None = None):
        self.events: list[AuditEvent] = []
        self.path = path

    def record(self, agent: str, action: str, **payload: Any) -> None:
        event = AuditEvent(
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            agent=agent,
            action=action,
            payload=payload,
        )
        self.events.append(event)
        if self.path:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.__dict__, ensure_ascii=False) + "\n")

    def tail(self, n: int = 20) -> list[AuditEvent]:
        return self.events[-n:]


class RiskVeto(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason
