"""Pydantic input models for the research knowledge graph.

Used by `research_server.py`'s `record_research_facts` tool to validate the
JSON payload before it's handed to `ResearchGraph.record_facts`.
"""

from typing import Literal

from pydantic import BaseModel


class SourceRef(BaseModel):
    url: str
    title: str = ""
    credibility: str = ""  # official-docs | blog | forum | paper | ...


class LibraryFact(BaseModel):
    name: str
    aliases: list[str] = []
    summary: str
    verdict: Literal["adopted", "rejected", "trial", "considering"] = "considering"
    sources: list[str] = []  # urls; each must also appear in ResearchFacts.sources


class ApiFact(BaseModel):
    name: str
    aliases: list[str] = []
    summary: str
    quirks: str = ""
    sources: list[str] = []


class ConceptFact(BaseModel):
    name: str
    aliases: list[str] = []
    summary: str
    sources: list[str] = []


class ResearchFacts(BaseModel):
    topic: str
    libraries: list[LibraryFact] = []
    apis: list[ApiFact] = []
    concepts: list[ConceptFact] = []
    sources: list[SourceRef] = []
