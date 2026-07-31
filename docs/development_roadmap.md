# Development Roadmap

## Phase 0: Planning

- Define architecture
- Define modules
- Define development phases
- Define GitHub-quality deliverables

## Phase 1: Basic Agent Framework

- Create project structure
- Configure Python environment
- Implement settings loader
- Implement LLM provider abstraction
- Implement Planner Agent
- Implement basic tool calling
- Implement LangGraph workflow
- Provide CLI run command

## Phase 2: Research Pipeline

- Add web search and paper search tools
- Add PDF parsing
- Add document chunking
- Add embeddings
- Add Qdrant or Chroma retrieval
- Add Search Agent, Document Agent, and Reasoning Agent integration
- Add offline fallback for local demos

## Phase 3: GraphRAG

- Add entity extraction
- Add relation extraction
- Add Neo4j persistence
- Add graph retrieval
- Add graph-enhanced reasoning
- Combine vector hits and graph paths in the workflow
- Keep in-memory fallback for local development

## Phase 4: Agent Capability Enhancement

- Add long-term memory
- Add reflection loop
- Add Critic Agent
- Add evaluation metrics
- Add execution tracing
- Add memory recall before planning
- Add quality review and report revision after synthesis

## Phase 5: Productization

- Add FastAPI endpoints
- Add Streamlit UI
- Add Docker Compose
- Add complete README
- Add demo screenshots and example reports
