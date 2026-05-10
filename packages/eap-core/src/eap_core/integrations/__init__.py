"""Adapters and helpers for integrating EAP-Core with managed agent platforms.

Each submodule targets one platform (AWS Bedrock AgentCore, GCP
Vertex Agent Engine, etc.) and exposes thin helpers that wire our
existing abstractions (OTel observability, OAuth identity,
deploy targets) at the right endpoints.

These helpers are deliberately small. Heavy lifting lives in the
platform's own SDK; we just remove the boilerplate.
"""
