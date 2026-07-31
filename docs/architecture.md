# Architecture

## Goal

AI-Research-Agent is designed as an autonomous research analysis system rather than a simple chatbot. The system separates planning, retrieval, knowledge extraction, reasoning, writing, reflection, memory, and evaluation into explicit components.

The default user-facing report language is Chinese, while code modules and tool identifiers remain English for engineering clarity.

## Phase 4 Scope

Phase 4 extends GraphRAG into a fuller agent system:

- typed workflow state
- LLM provider abstraction
- Planner Agent
- tool registry
- LangGraph workflow
- CLI demo
- Search Agent
- PDF parsing utility
- Document Agent
- chunking and embedding
- in-memory vector store and Qdrant adapter
- RAG retrieval node
- Knowledge Agent
- entity extraction
- relation extraction
- in-memory graph store and Neo4j adapter
- graph path retrieval
- GraphRAG reasoning node
- memory context retrieval
- Critic Agent
- Reflection Agent
- Evaluation module
- long-term memory writeback

The workflow currently runs:

```mermaid
flowchart LR
    Q["Research Topic"] --> M["Memory Context Node"]
    M --> P["Planner Node"]
    P --> T["Tool Executor Node"]
    T --> S["Search Node"]
    S --> D["Document Node"]
    D --> K["Knowledge Node"]
    K --> R["Retrieval Node"]
    R --> G["Graph Reasoning Node"]
    G --> W["Synthesis Node"]
    W --> C["Critic Node"]
    C --> Ref["Reflection Node"]
    Ref --> MW["Memory Write Node"]
    MW --> END["Final Markdown Summary"]
```

## Target Multi-Agent Workflow

```mermaid
flowchart TD
    P["Planner Agent"] --> Search["Search Agent"]
    P --> Doc["Document Agent"]
    P --> Knowledge["Knowledge Agent"]
    P --> Reasoning["Reasoning Agent"]
    P --> Writer["Writer Agent"]
    Writer --> Critic["Critic Agent"]
    Critic --> Writer
```

## Storage Design

- Qdrant stores dense vector embeddings for document chunks.
- Neo4j stores entities, claims, papers, methods, datasets, and relationships.
- The memory module stores previous topics, user preferences, and reusable research context.

Phase 4 defaults to in-memory vector and graph stores so the demo runs without Docker. Set
`VECTOR_STORE_PROVIDER=qdrant` or pass `--vector-store qdrant` after starting Qdrant.
Set `GRAPH_STORE_PROVIDER=neo4j` or pass `--graph-store neo4j` after starting Neo4j.
Phase 4 stores long-term memories in `data/memory/research_memory.json`, which is ignored by Git.

## GraphRAG Design

The GraphRAG layer combines:

1. vector retrieval for semantically relevant chunks
2. entity linking from query to graph nodes
3. graph traversal for multi-hop context
4. LLM reasoning over combined vector and graph evidence
5. citation-aware report generation

Current GraphRAG reasoning uses deterministic local synthesis so it can run without API keys.
Current Phase 4 critic and evaluation use deterministic local scoring so tests remain stable.
LLM-powered critique can be added later behind the same Critic Agent interface.
