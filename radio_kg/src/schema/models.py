"""Shared data models for the ingestion pipeline.

`PipelineState` is the LangGraph shared state threaded through
DocAgent -> ExtractorAgent -> InspectorAgent -> SyncAgent.
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, TypedDict

from pydantic import BaseModel, Field


def _sec_to_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


class SourceRef(BaseModel):
    """Provenance for a fact. Audio material carries timestamps; non-audio
    material (PDF/web) degrades to file name + page/segment (PRD 7.2)."""

    source_type: Literal["audio", "document"] = "audio"
    program: str = ""
    episode: Optional[int] = None
    episode_label: str = ""          # e.g. "#1 こもればなし"
    broadcast_date: str = ""          # YYYY-MM-DD
    start_time: Optional[float] = None  # seconds (audio)
    end_time: Optional[float] = None
    file_name: str = ""               # document fallback
    page: Optional[int] = None        # document fallback
    segment: Optional[int] = None     # document fallback

    def citation(self) -> str:
        if self.source_type == "audio":
            span = ""
            if self.start_time is not None:
                span = f" {_sec_to_ts(self.start_time)}"
                if self.end_time is not None:
                    span += f"-{_sec_to_ts(self.end_time)}"
            if self.episode is not None:
                head = f"《{self.program}》第{self.episode}期"
            elif self.broadcast_date:
                head = f"《{self.program}》{self.broadcast_date}"
            else:
                head = f"《{self.program}》"
            return f"{head}{span}".strip()
        loc = ""
        if self.page is not None:
            loc = f" 第{self.page}页"
        elif self.segment is not None:
            loc = f" 第{self.segment}段"
        return f"{self.file_name}{loc}".strip()


class Chunk(BaseModel):
    """A window of aligned segments handed to the extractor."""

    chunk_id: str
    text: str                         # "[ts] content" lines, original Japanese for extraction
    retrieval_text: str = ""          # bilingual/search-expanded text for vector recall only
    source: SourceRef
    annotated_text: str = ""          # speaker-tagged text for extraction


class Entity(BaseModel):
    name: str
    type: str = "Entity"              # Person / Project / Joke / Place / ...
    aliases: list[str] = Field(default_factory=list)


class Triple(BaseModel):
    """An extracted [subject]-(relation)->[object] fact bound to provenance."""

    subject: Entity
    relation: str
    object: Entity
    confidence: float = 1.0
    source: SourceRef
    # resolved canonical ids filled in after disambiguation
    subject_id: Optional[str] = None
    object_id: Optional[str] = None


class Conflict(BaseModel):
    """Raised by SyncAgent when a new triple contradicts existing knowledge."""

    relation: str
    subject_name: str
    existing_object: str
    new_object: str
    new_source: SourceRef
    resolution: Optional[Literal["confirm", "overwrite", "ignore"]] = None


class InspectionIssue(BaseModel):
    """Raised by InspectorAgent when a triple may contain ASR / hallucination noise."""

    severity: Literal["auto_corrected", "review_required", "warning"]
    issue_type: str                  # domain_vocab / phonetic / graph_frequency
    entity_role: Literal["subject", "object"]
    relation: str
    original_name: str
    suggested_name: str = ""
    suggested_type: str = ""
    confidence: float = 0.0
    mechanisms: list[str] = Field(default_factory=list)
    reason: str = ""
    source: SourceRef


def _extend(a: list, b: list) -> list:
    return (a or []) + (b or [])


class PipelineState(TypedDict, total=False):
    """LangGraph shared state. List fields use additive reducers so nodes
    can append without clobbering."""

    episode_dir: str
    source: dict                      # SourceRef as dict (graph-serializable)
    host: str                         # session constant: personality/host
    guest: str                        # session constant: guest (if any)
    listeners: Annotated[list, _extend]  # session constants: letter-writers seen
    chunks: Annotated[list, _extend]
    annotated_chunks: Annotated[list, _extend]  # speaker-tagged chunks for extraction
    triples: Annotated[list, _extend]
    inspected_triples: Annotated[list, _extend]
    conflicts: Annotated[list, _extend]
    inspection_issues: Annotated[list, _extend]
    written: Annotated[list, _extend]
    dropped: Annotated[list, _extend]  # discarded ambiguous extractions
