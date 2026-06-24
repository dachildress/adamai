"""
Context loading subsystem.

Module map:
  - file_extractor.py   ContextFile, classification heuristics, per-format
                        text extraction (PDF, DOCX, TXT, MD), context_id
                        assignment, build_context_state
  - budget_manager.py   token estimation, Summarizer service hook with
                        cache, operator confirmation flow, [T0] Background
                        block builder, load_context_block orchestrator
  - _config.py          (private) verifier-style runtime config layer

Public API:

    from adam.context import (
        set_runtime_config,
        TEXT_DOCUMENT_EXTENSIONS, STRUCTURED_DATA_EXTENSIONS,
        ContextFile, detect_context_files, extract_text_for_file,
        build_context_state,
        load_context_block,
        build_background_block,
        classify_budget_status,
        DOCUMENT_GROUNDED_CLAIM_RULE,
    )
"""
from __future__ import annotations

from adam.context._config import set_runtime_config

from adam.context.file_extractor import (
    TEXT_DOCUMENT_EXTENSIONS,
    STRUCTURED_DATA_EXTENSIONS,
    ContextFile,
    _classify_file,
    _hash_file,
    _enumerate_context_files,
    detect_context_files,
    build_context_state,
    extract_text_for_file,
)

from adam.context.budget_manager import (
    PROMPT_VERSION_RE,
    _read_summarizer_prompt_version,
    _estimate_tokens,
    classify_budget_status,
    summarize_file,
    ContextLoadDecision,
    build_background_block,
    DOCUMENT_GROUNDED_CLAIM_RULE,
    load_context_block,
)

__all__ = [
    "set_runtime_config",
    "TEXT_DOCUMENT_EXTENSIONS",
    "STRUCTURED_DATA_EXTENSIONS",
    "ContextFile",
    "detect_context_files",
    "extract_text_for_file",
    "build_context_state",
    "load_context_block",
    "build_background_block",
    "classify_budget_status",
    "summarize_file",
    "ContextLoadDecision",
    "DOCUMENT_GROUNDED_CLAIM_RULE",
    "PROMPT_VERSION_RE",
]
