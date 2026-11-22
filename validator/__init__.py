"""Validator packages; legacy runners are imported lazily."""
__all__ = ["AgentV1ValidatorRunner", "JudgeV1Runner", "JudgeV2Runner"]

def __getattr__(name):
    if name == "AgentV1ValidatorRunner":
        from .agent_v1 import AgentV1ValidatorRunner
        return AgentV1ValidatorRunner
    if name == "JudgeV1Runner":
        from .judge_v1 import JudgeV1Runner
        return JudgeV1Runner
    if name == "JudgeV2Runner":
        from .judge_v2 import JudgeV2Runner
        return JudgeV2Runner
    raise AttributeError(name)
