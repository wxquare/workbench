"""Stable identifiers for the graph implementations installed by the worker."""

from enum import StrEnum


class GraphId(StrEnum):
    SOP_CONDITIONAL = 'sop.conditional'
    RCA_SYNTHETIC = 'rca.synthetic'
    RCA_PROPOSAL = 'rca.proposal'
    RCA_BATCH_PROPOSAL = 'rca.batch_proposal'
    KNOWLEDGE_SCOPED_EXCERPT = 'knowledge.scoped_excerpt'
    CONVERSATION_SINGLE_TURN = 'conversation.single_turn'
    CONVERSATION_MULTI_TURN = 'conversation.multi_turn'
    CONVERSATION_TOOL_USE = 'conversation.tool_use'
    KNOWLEDGE_CITATION_REVIEW = 'knowledge.citation_review'


ALL_GRAPH_IDS = frozenset(GraphId)
