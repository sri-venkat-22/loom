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
    # The project's template (loom/project_templates.py), or None
    template = None
    # Why the phase can't run, like the provider rejecting tools
    failed = None

    def __init__(self, main_model, io, phase=None, shared_memory=None, template=None, **kwargs):
        self.phase = phase
        self.shared_memory = shared_memory
        self.template = template
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
            lines += [f"- {pattern}" for pattern in self.writable()]
        lines += self.template_brief()
        if self.shared_memory:
            lines += ["", memory_brief]
        return "\n".join(lines)

    def template_brief(self):
        """The project template's brief for this phase, as lines of the phase's brief."""
        template = self.template
        if not template:
            return []
        parts = []
        if template.brief(self.phase.key):
            parts.append(template.brief(self.phase.key))
        if self.phase.key == "design" and template.test_command:
            parts.append(
                f"The template runs the tests with `{template.test_command}`: put that on the"
                " Test command line unless the design needs another."
            )
        if not parts:
            return []
        return ["", f"## The project template: {template.name}", ""] + parts

    def writable(self):
        """The globs of the files the phase may write besides its document, the template's
        for the phase included, or None for any file."""
        if self.phase.writable is None:
            return None
        extra = self.template.writable_for(self.phase.key) if self.template else []
        return tuple(self.phase.writable) + tuple(extra)

    def refuse_action(self, name, action):
        phase = self.phase
        if phase.tools is not None and name not in phase.tools:
            return f"the {phase.agent} can't use {name}. Its tools are: {', '.join(phase.tools)}."
        if action.kind == "edit" and not self.may_write(action):
            return (
                f"the {phase.agent} may only write {phase.document} and files matching"
                f" {', '.join(self.writable()) or 'nothing else'}, not {action.target}."
            )
        return None

    def may_write(self, action):
        writable = self.writable()
        if writable is None:
            return True
        if not action.inside:
            return False
        if action.target == self.phase.document:
            return True
        return any(glob_match(pattern, action.target) for pattern in writable)

    def preapproved(self, action):
        # The user reviews the document when the phase is done, so writing it needs no
        # question
        return action.kind == "edit" and action.inside and action.target == self.phase.document

    def tools_rejected(self):
        err = str(self.tools_error).strip().split("\n", 1)[0]
        self.tools_error = None
        self.failed = f"{self.main_model.name} can't use tools, which phase agents need: {err}"
        self.io.tool_error(self.failed)
