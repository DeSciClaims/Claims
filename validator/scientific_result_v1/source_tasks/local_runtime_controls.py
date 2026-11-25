"""Owner amendments to local diagnostic scheduling, outside scientific requests."""


def validate(value):
    required={'kind', 'shared_max_llm_requests', 'max_provider_attempts'}
    if (not required <= set(value) or set(value)-required-{'implementation_amendment','max_submissions_per_paper'}
        or value['kind'] != 'claims_local_diagnostic_runtime_controls_v1'
        or type(value['shared_max_llm_requests']) is not int
        or not 1 <= value['shared_max_llm_requests'] <= 500
        or type(value['max_provider_attempts']) is not int
        or value['max_provider_attempts'] not in {1, 2}):
        raise ValueError('Invalid local diagnostic runtime controls')
    workers=value.get('max_submissions_per_paper',1)
    if type(workers) is not int or not 1<=workers<=4:
        raise ValueError('Local submission concurrency must be between one and four')
    amendment=value.get('implementation_amendment')
    if amendment is not None:
        if (set(amendment)!={'from_implementation_digest','to_implementation_digest','change_manifest_sha256'}
            or any(not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for v in amendment.values())):
            raise ValueError('Invalid local implementation amendment')
    return value
