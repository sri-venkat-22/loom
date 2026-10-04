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

    # Paths of acceptance tests it may not change (test-driven Building)
    locked = frozenset()
    # The orchestrator checkpoints the project before each phase's run, for /rewind
    checkpoint_requests = False

    def __init__(
        self, main_model, io, phase=None, shared_memory=None, template=None, locked=None, **kwargs
    ):
        self.phase = phase
        self.shared_memory = shared_memory
        self.template = template
        self.locked = frozenset(locked or ())
        if phase.tools is not None:
            # Building keeps the coding agent's own prompt
            self.gpt_prompts = PhasePrompts()
        super().__init__(main_model, io, **kwargs)

    @property
    def tools(self):
        project = agent_tools.project_schemas() if self.shared_memory else []
        if self.phase.tools is None:
            return super().tools + project
        web = agent_tools.web_schemas() if self.web_tools else []
        task = agent_tools.task_schemas(self) if self.can_delegate() else []
        return [
            schema
            for schema in agent_tools.schemas() + web + project + task
            if schema["function"]["name"] in self.phase.tools
        ] + self.stitch_schemas()

    def can_delegate(self):
        """Building starts any sub-agent, and the phases with task in their tools
        read-only ones."""
        tools = self.phase.tools
        return super().can_delegate() and (tools is None or "task" in tools)

    def refuse_agent_type(self, agent_type, types):
        refusal = super().refuse_agent_type(agent_type, types)
        if refusal or self.phase.tools is None or agent_type.read_only:
            return refusal
        read_only = ", ".join(name for name, t in types.items() if t.read_only)
        return (
            f"the {self.phase.agent} may only start read-only agents ({read_only}), and the"
            f" {agent_type.name} agent can edit files or run commands."
        )

    def stitch_server(self):
        """The connected Google Stitch server, when the phase may use it."""
        if not self.mcp or self.phase.tools is None or "stitch" not in self.phase.tools:
            return None
        return self.mcp.stitch()

    def stitch_schemas(self):
        server = self.stitch_server()
        if not server:
            return []
        prefix = f"mcp__{server.name}__"
        return [
            schema
            for schema in self.mcp.tool_schemas()
            if schema["function"]["name"].startswith(prefix)
        ]

    def is_stitch_tool(self, name):
        server = self.stitch_server()
        return bool(server) and name.startswith(f"mcp__{server.name}__")

    def system_prompt_extras(self):
        extra = super().system_prompt_extras() if self.phase.tools is None else []
        if self.phase.tools is not None and self.web_tools and "web_fetch" in self.phase.tools:
            extra.append(self.gpt_prompts.web_tools_prompt)
        if self.phase.tools is not None and self.can_delegate():
            extra.append(self.gpt_prompts.phase_task_prompt)
        if self.stitch_server():
            extra.append(self.stitch_prompt())
            extra.append(self.gpt_prompts.stitch_phase_prompt)
        extra.append(self.phase_brief())
        return extra

    def phase_brief(self):
        phase = self.phase
        lines = [f"# Your phase: {phase.number}. {phase.title}", "", phase.brief, ""]
        lines.append("## Your tools")
        if phase.tools is None:
            lines.append("All of the coding agent's tools, and recall and record_decision.")
        else:
            tools = [
                name
                for name in phase.tools
                if (self.web_tools or name not in agent_tools.WEB_TOOLS)
                and name != "stitch"
                and (name != "task" or self.can_delegate())
            ]
            if self.stitch_server():
                tools.append(f"Google Stitch's tools (mcp__{self.stitch_server().name}__*)")
            lines.append(", ".join(tools))
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
        if phase.tools is not None and name not in phase.tools and not self.is_stitch_tool(name):
            return f"the {phase.agent} can't use {name}. Its tools are: {', '.join(phase.tools)}."
        if action.kind == "edit" and action.inside and action.target in self.locked:
            return (
                f"{action.target} is one of the approved acceptance tests, which are locked."
                " Make the code pass it instead; if the test is wrong, say so in your summary."
            )
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
