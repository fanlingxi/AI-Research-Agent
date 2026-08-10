# Domain Plugin Boundary

## Purpose

Domain Plugins add a narrowly governed workflow to the shared platform. The Core does not need to understand research citation behavior or game formula calculation, while plugins do not receive the authority to create another runtime, persistence layer, or arbitrary tool surface.

## Static first-party registry

The current registry contains exactly two first-party plugins:

| Plugin | Key | Governed knowledge | Workflow | Output |
| --- | --- | --- | --- | --- |
| Research | research | Paper, Method, Dataset, Metric | foundation and research | research_report |
| Game Modeling | game_modeling | Formula, Patch | model | game_model_report |

A project may enable or disable these known plugins. A WorkspaceTask persists a plugin routing key; an AgentRun then pins key, version, contract version, and workflow key for its full lifecycle.

## Plugin contract

A plugin declares static metadata:

- plugin key, version, contract version, display name, and domain;
- supported workflows, checkpoint namespace, and minimum execution budgets;
- permitted tool permissions, governed knowledge node types, and Artifact types;
- whether it may propose Memory changes;
- a reviewed Runtime port type.

A plugin implements workflow construction only against supplied Runtime ports. It does not receive a persistence handle, a global service locator, an external network client, or a mechanism to install code dynamically.

## Platform-owned finalization

Plugins return a domain finalization command containing structured output, rendered text, validation metadata, Artifact metadata, and optional proposal payload. The shared Runtime validates the run/plugin pin and persists output, Artifact, and proposal atomically.

This boundary ensures that Research and Game Modeling share the same ContextSnapshot, trace, provenance, and human-governance model.

## Domain-specific safety

### Research

Research requires cited evidence from the selected ContextSnapshot. One bounded repair is permitted; unresolved citation violations are routed to review rather than materializing normal business output.

### Game Modeling

Game Modeling calculation is deterministic. It consumes scoped Formula/Patch data, validates patch version, and fails closed on mismatch. It does not claim a broader game ontology or live-service data integration.

## Not implemented

There is no Creative plugin, third-party plugin marketplace, dynamic discovery mechanism, plugin-owned database, MCP integration, multi-agent coordination, or web-search tool in the current public product.
