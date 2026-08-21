"""Phase 6's product layer: the dashboard that reports what the engine did.

Strictly a *reader* of the engine's databases plus a thin control surface
(pause/resume). Nothing in this package is on the dictation latency path, and
nothing here may fabricate a metric -- see CLAUDE.md's Phase 6 rule.
"""

from __future__ import annotations

__all__: list[str] = []
