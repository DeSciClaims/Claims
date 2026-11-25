"""Explicit protocol dispatch; scientific pools never enter claims/Silver parsing."""
from dataclasses import dataclass
from typing import Mapping,Callable,Any
from .publication import canonical_sha256
from .scientific_scoring import ScientificPaperEvaluation
from validator.scientific_result_v1.source_tasks.sampling import PROTOCOL
from validator.scientific_result_v1.source_tasks.packages import SCHEMA_HASH


@dataclass(frozen=True)
class FrozenScientificEvaluationPolicy:
    round_id:str
    configuration_sha256:str
    configuration:Mapping[str,Any]
    evaluate_pool:Callable
    rules_revision:int=1

    def __post_init__(self):
        self.verify()

    def verify(self):
        spec=self.configuration.get('scientific_scoring',{})
        from .scientific_first_round import configured_policy
        configured_policy(self.configuration,self.round_id)
        if canonical_sha256(self.configuration)!=self.configuration_sha256:raise ValueError('Scientific configuration hash differs')
        if spec.get('protocol_id')!=PROTOCOL or spec.get('schema_sha256')!=SCHEMA_HASH:raise ValueError('Scientific protocol/schema differs')
        if spec.get('source_scope_policy')!='supplied_sources_only':raise ValueError('Scientific source scope differs')
        if spec.get('organization_score') is not False or spec.get('canonical_organization') is not False:
            raise ValueError('Scientific scoring cannot reward a preferred organization')
        if spec.get('winner_selection')!='silver_tiebreak':raise ValueError('Unit-based Gold cannot override scientific receipts')
        if self.configuration.get('worker_policy',{}).get('output_schema')!='scientific_result_v1.1':raise ValueError('Scientific worker output format missing')

    def evaluate_for_stage(self,stage):
        if stage not in {'prescreen','screen','final','week_one'}:raise ValueError('Unknown scientific source stage')
        def evaluate(**kwargs):
            self.verify()
            if kwargs.get('stage')!=stage:raise ValueError('Scientific stage binding mismatch')
            # The complete pool is anonymized inside the protocol runner. Bronze is retrieval,
            # never a reference answer and never projected to logic.claims.
            result=self.evaluate_pool(round_id=self.round_id,configuration_sha256=self.configuration_sha256,
                                      rules_revision=self.rules_revision,**kwargs)
            if not isinstance(result,ScientificPaperEvaluation):raise ValueError('Scientific adapter returned claims result')
            if (result.pipeline.binding.round_id,result.pipeline.binding.stage,result.pipeline.binding.paper_id)!=(self.round_id,stage,kwargs['paper_id']):raise ValueError('Scientific result binding mismatch')
            return result
        evaluate.validation_protocol=PROTOCOL
        return evaluate

    def judge_known_answer(self,sealed_case):
        raise ValueError('Source-task corrections apply to the complete input pool; unit Gold is not supported')
