from loom import tools as agent_tools
from loom.tools import glob_match

from .agent_coder import AgentCoder
from .phase_prompts import PhasePrompts, memory_brief


class PhaseCoder(AgentCoder):
    """The agent of one project phase (see loom/phases.py): the agent loop with the phase's
    brief, limited to the phase's tools and the files it may write. The orchestrator runs
    it; it isn't a chat mode."""

    phase = None
    # The project's shared memory (loom/memory.py), which recall and record_decision use
    shared_memory = None
    # Why the phase can't run, like the provider rejecting tools
    failed = None

    def __init__(self, main_model, io, phase=None, shared_memory=None, **kwargs):
        self.phase = phase
        self.shared_memory = shared_memory
        if phase.tools is not None:
            # Building keeps the coding agent's own prompt
            self.gpt_prompts = PhasePrompts()
        super().__init__(main_model, io, **kwargs)

    @property
    def tools(self):
        project = agent_tools.project_schemas() if self.shared_memory else []
        if self.phase.tools is None:
            return super().tools + project
        return [
            schema
            for schema in agent_tools.schemas() + project
            if schema["function"]["name"] in self.phase.tools
        ]

    def system_prompt_extras(self):
        extra = super().system_prompt_extras() if self.phase.tools is None else []
        extra.append(self.phase_brief())
        return extra

    def phase_brief(self):
        phase = self.phase
        lines = [f"# Your phase: {phase.number}. {phase.title}", "", phase.brief, ""]
        lines.append("## Your tools")
        if phase.tools is None:
            lines.append("All of the coding agent's tools, and recall and record_decision.")
        else:
            lines.append(", ".join(phase.tools))
        lines += ["", "## What you may write"]
        lines.append(f"- {phase.document} (your {phase.document_title})")
        if phase.writable is None:
            lines.append("- Any file in the project")
        else:
            lines += [f"- {pattern}" for pattern in phase.writable]
        if self.shared_memory:
            lines += ["", memory_brief]
        return "\n".join(lines)

    def refuse_action(self, name, action):
        phase = self.phase
        if phase.tools is not None and name not in phase.tools:
            return f"the {phase.agent} can't use {name}. Its tools are: {', '.join(phase.tools)}."
        if action.kind == "edit" and not self.may_write(action):
            return (
                f"the {phase.agent} may only write {phase.document} and files matching"
                f" {', '.join(phase.writable) or 'nothing else'}, not {action.target}."
            )
        return None

    def may_write(self, action):
        if self.phase.writable is None:
            return True
        if not action.inside:
            return False
        if action.target == self.phase.document:
            return True
        return any(glob_match(pattern, action.target) for pattern in self.phase.writable)

    def preapproved(self, action):
        # The user reviews the document when the phase is done, so writing it needs no
        # question
        return action.kind == "edit" and action.inside and action.target == self.phase.document

    def tools_rejected(self):
        err = str(self.tools_error).strip().split("\n", 1)[0]
        self.tools_error = None
        self.failed = f"{self.main_model.name} can't use tools, which phase agents need: {err}"
        self.io.tool_error(self.failed)
