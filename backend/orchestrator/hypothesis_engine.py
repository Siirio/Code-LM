"""Hypothesis-driven code exploration engine.

Architecture: Final from day 1, implementation: incremental.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Set, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class HypothesisStatus(str, Enum):
    ACTIVE = "active"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass
class Fact:
    """A confirmed fact about the codebase."""
    text: str
    source: str  # file_path or node_name
    confidence: float = 1.0


@dataclass
class Hypothesis:
    """A hypothesis about how the codebase works."""
    text: str
    confidence: float  # 0.0-1.0
    status: HypothesisStatus = HypothesisStatus.ACTIVE
    supporting_facts: List[str] = field(default_factory=list)  # fact.text references


@dataclass
class Decision:
    """A decision made by the planner."""
    action: str  # "explore", "stop"
    target: Optional[str] = None  # node name for explore
    reason: str = ""
    confidence: float = 0.0
    hypothesis_update: Optional[str] = None


@dataclass
class ExplorationState:
    """State of the exploration process."""
    # === CONTEXT ===
    user_request: str
    discovered_goal: Optional[str] = None

    # === GRAPH STATE ===
    visited_nodes: Set[str] = field(default_factory=set)
    explored_files: Set[str] = field(default_factory=set)

    # === THINKING (CORE) ===
    hypotheses: List[Hypothesis] = field(default_factory=list)
    current_focus: List[str] = field(default_factory=list)

    # === FACTS ===
    confirmed_facts: List[Fact] = field(default_factory=list)
    pending_questions: List[str] = field(default_factory=list)

    # === DECISION MEMORY ===
    reasoning_log: List[Dict[str, Any]] = field(default_factory=list)
    irrelevant_paths: Set[str] = field(default_factory=set)

    # === CONTROL ===
    steps_taken: int = 0
    max_steps: int = 15

    # === CONFIDENCE ===
    confidence: float = 0.0

    def add_hypothesis(self, text: str, confidence: float = 0.5) -> None:
        """Add a new hypothesis."""
        self.hypotheses.append(
            Hypothesis(text=text, confidence=confidence, status=HypothesisStatus.ACTIVE)
        )
        logger.info(f"Added hypothesis: {text} (confidence: {confidence})")

    def add_fact(self, text: str, source: str, confidence: float = 1.0) -> None:
        """Add a confirmed fact."""
        self.confirmed_facts.append(Fact(text=text, source=source, confidence=confidence))
        logger.info(f"Added fact: {text} from {source}")

    def add_reasoning(self, step: int, decision: str, reason: str) -> None:
        """Log a reasoning step."""
        self.reasoning_log.append({
            "step": step,
            "decision": decision,
            "reason": reason
        })

    def should_stop(self) -> bool:
        """Determine if exploration should stop."""
        if self.steps_taken >= self.max_steps:
            logger.info(f"Stop: max steps reached ({self.max_steps})")
            return True

        # Simple confidence heuristic
        if self.confidence >= 0.9:
            logger.info(f"Stop: confidence threshold reached ({self.confidence})")
            return True

        # If we found the exact place to modify
        if self.discovered_goal and any("place to modify" in fact.text for fact in self.confirmed_facts):
            logger.info("Stop: discovered exact modification point")
            return True

        return False

    def update_confidence(self) -> None:
        """Update confidence based on facts and hypotheses."""
        # Simple heuristic for Phase 1
        fact_count = len(self.confirmed_facts)
        target_count = 5  # Target number of facts for high confidence

        self.confidence = min(1.0, fact_count / target_count)
        logger.debug(f"Updated confidence: {self.confidence} (facts: {fact_count})")


class HypothesisEngine:
    """Hypothesis-driven exploration engine."""

    def __init__(self, project_id: str, llm_provider=None):
        self.project_id = project_id
        self.llm = llm_provider  # Will be set later
        self.state = None

    async def explore(self, node_name: str) -> Dict[str, Any]:
        """Explore a node and its neighbors using Neo4j graph."""
        logger.info(f"Exploring node: {node_name}")

        try:
            from storage.neo4j_client import neo4j_client

            if not neo4j_client.is_connected:
                logger.warning("Neo4j not connected, using stub data")
                return self._stub_explore(node_name)

            # Query node details
            node_query = """
                MATCH (n {name: $node_name, project_id: $project_id})
                RETURN n.name AS name, n.layer AS layer, n.file_path AS file_path,
                       n.declared_role AS declared_role, n.module AS module
                LIMIT 1
            """
            nodes = await neo4j_client.query(
                node_query,
                {"node_name": node_name, "project_id": self.project_id}
            )

            if not nodes:
                logger.warning(f"Node {node_name} not found in graph")
                return self._stub_explore(node_name)

            node_info = nodes[0]

            # Query neighbors (imports relationships)
            neighbors_query = """
                MATCH (a {name: $node_name, project_id: $project_id})-[r:IMPORTS]->(b)
                RETURN b.name AS name, b.layer AS layer, b.file_path AS file_path,
                       type(r) AS relation_type
                LIMIT 10
            """
            neighbors = await neo4j_client.query(
                neighbors_query,
                {"node_name": node_name, "project_id": self.project_id}
            )

            # Query incoming relationships
            incoming_query = """
                MATCH (a)-[r:IMPORTS]->(b {name: $node_name, project_id: $project_id})
                RETURN a.name AS name, a.layer AS layer, a.file_path AS file_path,
                       type(r) AS relation_type
                LIMIT 10
            """
            incoming = await neo4j_client.query(
                incoming_query,
                {"node_name": node_name, "project_id": self.project_id}
            )

            # Extract facts
            facts = []
            if node_info.get("layer"):
                facts.append(f"{node_name} is a {node_info['layer']}")

            if neighbors:
                neighbor_names = [n["name"] for n in neighbors]
                facts.append(f"{node_name} imports: {', '.join(neighbor_names)}")

            if incoming:
                incoming_names = [n["name"] for n in incoming]
                facts.append(f"{node_name} is imported by: {', '.join(incoming_names)}")

            return {
                "node": node_name,
                "neighbors": [n["name"] for n in neighbors],
                "incoming": [n["name"] for n in incoming],
                "file_path": node_info.get("file_path", ""),
                "layer": node_info.get("layer", "Unknown"),
                "module": node_info.get("module", ""),
                "facts": facts
            }

        except Exception as e:
            logger.error(f"Error exploring node {node_name}: {e}", exc_info=True)
            return self._stub_explore(node_name)

    def _stub_explore(self, node_name: str) -> Dict[str, Any]:
        """Fallback stub exploration."""
        return {
            "node": node_name,
            "neighbors": ["SecurityConfig", "JwtFilter", "AuthService"],
            "incoming": [],
            "file_path": f"src/main/java/com/example/{node_name}.java",
            "layer": "Configuration",
            "module": "auth",
            "facts": [
                f"{node_name} uses JWT authentication",
                f"{node_name} imports AuthService"
            ]
        }

    async def decide_next_action(self, state: ExplorationState) -> Decision:
        """Decide the next action (planner stub)."""
        # Phase 1: Simple rule-based planner
        # If no hypotheses yet, create initial one
        if not state.hypotheses:
            return Decision(
                action="explore",
                target="SecurityConfig",  # Common starting point for auth
                reason="Initial hypothesis: authentication likely involves SecurityConfig",
                confidence=0.6,
                hypothesis_update="Authentication handled via SecurityConfig + JWT"
            )

        # If we have hypotheses but no facts yet
        if not state.confirmed_facts:
            # Explore the first node mentioned in hypotheses
            hypothesis = state.hypotheses[0]
            if "SecurityConfig" in hypothesis.text:
                return Decision(
                    action="explore",
                    target="SecurityConfig",
                    reason="Checking SecurityConfig based on hypothesis",
                    confidence=0.7
                )
            elif "AuthService" in hypothesis.text:
                return Decision(
                    action="explore",
                    target="AuthService",
                    reason="Checking AuthService based on hypothesis",
                    confidence=0.7
                )

        # After some exploration, decide based on findings
        for fact in state.confirmed_facts:
            if "JWT" in fact.text and "AuthService" not in state.visited_nodes:
                return Decision(
                    action="explore",
                    target="AuthService",
                    reason="JWT found, likely handled by AuthService",
                    confidence=0.8
                )

        # Default: stop if we have enough facts
        if len(state.confirmed_facts) >= 3:
            return Decision(
                action="stop",
                reason="Collected sufficient facts about authentication",
                confidence=0.85
            )

        # Fallback: stop anyway
        return Decision(
            action="stop",
            reason="No clear next step",
            confidence=0.5
        )

    async def investigate(self, user_request: str) -> ExplorationState:
        """Main investigation loop."""
        logger.info(f"Starting investigation: {user_request}")

        self.state = ExplorationState(user_request=user_request)

        # Initial hypothesis based on request
        if "auth" in user_request.lower() or "role" in user_request.lower():
            self.state.add_hypothesis(
                "Authentication handled via SecurityConfig + JWT",
                confidence=0.6
            )
            self.state.current_focus = ["SecurityConfig", "AuthService"]

        while not self.state.should_stop():
            self.state.steps_taken += 1
            logger.info(f"Step {self.state.steps_taken}/{self.state.max_steps}")

            # 1. Decide
            decision = await self.decide_next_action(self.state)
            self.state.add_reasoning(
                step=self.state.steps_taken,
                decision=f"{decision.action}({decision.target})" if decision.target else decision.action,
                reason=decision.reason
            )

            if decision.hypothesis_update:
                self.state.add_hypothesis(decision.hypothesis_update, decision.confidence)

            # 2. Execute
            if decision.action == "stop":
                logger.info("Planner decided to stop")
                break

            if decision.action == "explore" and decision.target:
                # Mark as visited
                self.state.visited_nodes.add(decision.target)

                # Explore the node
                result = await self.explore(decision.target)

                # 3. Update state
                for fact_text in result.get("facts", []):
                    self.state.add_fact(fact_text, source=result["node"])

                # Add file to explored files
                if "file_path" in result:
                    self.state.explored_files.add(result["file_path"])

            # 4. Update confidence
            self.state.update_confidence()

            # 5. Detect cycles (simple)
            if self.state.steps_taken >= 3:
                last_nodes = list(self.state.visited_nodes)[-3:]
                if len(set(last_nodes)) == 1:  # Same node 3 times
                    logger.warning(f"Cycle detected at {last_nodes[0]}")
                    self.state.irrelevant_paths.add(last_nodes[0])
                    break

        # Final confidence update
        self.state.update_confidence()

        if self.state.discovered_goal is None and self.state.confirmed_facts:
            # Try to infer goal from facts
            for fact in self.state.confirmed_facts:
                if "role" in fact.text.lower():
                    self.state.discovered_goal = "Add role field to User entity"
                    break

        logger.info(f"Investigation complete. Confidence: {self.state.confidence}")
        logger.info(f"Facts found: {len(self.state.confirmed_facts)}")
        logger.info(f"Hypotheses: {len(self.state.hypotheses)}")

        return self.state


async def run_hypothesis_engine(project_id: str, user_request: str) -> Dict[str, Any]:
    """Run the hypothesis engine and return context for code generation."""
    engine = HypothesisEngine(project_id)
    state = await engine.investigate(user_request)

    # Convert state to context for code generation
    context = {
        "user_request": state.user_request,
        "discovered_goal": state.discovered_goal,
        "confidence": state.confidence,
        "facts": [{"text": f.text, "source": f.source} for f in state.confirmed_facts],
        "hypotheses": [{"text": h.text, "confidence": h.confidence} for h in state.hypotheses],
        "visited_nodes": list(state.visited_nodes),
        "explored_files": list(state.explored_files),
        "steps_taken": state.steps_taken,
        "reasoning_log": state.reasoning_log
    }

    return context


# Simple test
if __name__ == "__main__":
    import asyncio

    async def test():
        context = await run_hypothesis_engine("test-project", "add role-based authentication")
        print(json.dumps(context, indent=2))

    asyncio.run(test())