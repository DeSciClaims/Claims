"""Registered transport dispositions from guide Appendix B; scientific findings are separate."""
CODES={
 'PIPELINE':{'PACKAGE_SCHEMA','OBJECT_IDENTITY','REFERENCE_UNRESOLVED','FAMILY_INTEGRITY','PARENT_PATH','ENUM_OR_CONDITIONAL','CLOSURE_INCONSISTENT','OBJECT_UNACCOUNTED','TABLE_PACKAGE_OR_COVERAGE','CSV_FORMAT_OR_DIMENSIONS','TABLE_REFERENCE','PACKAGE_MISSING','PACKAGE_TIMEOUT','PACKAGE_RESOURCE_LIMIT','PACKAGE_BUILD_FAILED','PACKAGE_SECURITY'},
 'SOURCE':{'SOURCE_UNAVAILABLE','SOURCE_VERSION_UNRESOLVED','SOURCE_UNREADABLE','SOURCE_INVENTORY_UNRESOLVED','SOURCE_TASK_UNRESOLVED','SOURCE_NO_TASKS','TARGET_DEFECT'},
 'EVALUATOR':{'CONFIG_INVALID','TRANSFER_FAILED','PERSISTENCE_FAILED','HASH_MISMATCH','RECEIPT_INCONSISTENT','LOOKUP_UNRESOLVED','EVIDENCE_LIMIT','PACKET_LIMIT','DEADLINE_EXCEEDED','CORRECTION_LIMIT','STAGE_REPLACEMENT_LIMIT','BUDGET_EXHAUSTED','FALLBACK_REFERENCE_UNAVAILABLE'},
 'MODEL':{'PROVIDER_ERROR','PROVIDER_TIMEOUT','RESPONSE_INVALID','RESPONSE_TRUNCATED'}}
STAGES={'CONFIGURATION','SOURCE_PREPARATION','EXECUTION','PACKAGE_COLLECTION','MECHANICAL','INDEXING','SAMPLING','TASK_BUILD','MATCHING','RENDERING','VERIFICATION','SILVER','SCORING','EXCLUSION','FALLBACK','PUBLICATION','REPLAY'}
WORKER_CODES={'build_failed':'PACKAGE_BUILD_FAILED','package_failed':'PACKAGE_MISSING','invalid_output':'PACKAGE_SCHEMA','timeout':'PACKAGE_TIMEOUT','cap_exceeded':'PACKAGE_RESOURCE_LIMIT','security_violation':'PACKAGE_SECURITY'}
BUDGETED_HOLD_CODE = 'BUDGETED_DIAGNOSTIC_ONLY'
CODES['EVALUATOR'].add(BUDGETED_HOLD_CODE)


def validate_error(error):
    if (error.category not in CODES or error.code not in CODES[error.category] or error.stage not in STAGES
        or error.code == BUDGETED_HOLD_CODE and error.stage != 'VERIFICATION'):
        raise ValueError('Unregistered transport error disposition: '+error.category+'/'+error.code+'/'+error.stage)


def hold_record(exc,stage):
    from .contracts import ErrorRecord
    if exc.category=='SOURCE':code='SOURCE_TASK_UNRESOLVED'
    elif exc.category=='MODEL':code='RESPONSE_INVALID'
    else:
        text=str(exc).lower()
        code='DEADLINE_EXCEEDED' if 'deadline' in text else 'PACKET_LIMIT' if 'shard' in text or 'packet' in text else 'EVIDENCE_LIMIT' if 'bound' in text else 'LOOKUP_UNRESOLVED'
    return ErrorRecord(category=exc.category,code=code,stage=stage,message=str(exc),retryable=getattr(exc,'retryable',False),output_refs=[])


def safe_message(error,secrets=()):
    value=str(error).replace('\x00','\\u0000')
    for secret in secrets:
        if secret:value=value.replace(secret,'[REDACTED]')
    return value
