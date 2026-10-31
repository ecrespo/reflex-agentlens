# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/). Entries are derived from Conventional Commits.

## [Unreleased]

### Added

- Approved v0.1 specification chain (Constitution, PRD, API Spec, Tech Design, Data Model,
  Implementation Plan, Tasks) and the spike results in Tech Design §11.
- Package skeleton, uv workspace (`site/`, `examples/`), and quality gates: ruff, mypy, import
  contracts, pytest with a coverage gate, REQ traceability script, CI and supply-chain workflows.
- `reflex_agentlens.core`: error hierarchy; OTel trace/span ID and ULID helpers; UTC/nanosecond
  time helpers; exact nano-USD money and LLM call cost; API key generation, salted hashing and
  HMAC tokens; configuration models with the TOML → environment → CLI source chain, `*_FILE`
  secrets and masked rendering; JSON logging with a content guard.
