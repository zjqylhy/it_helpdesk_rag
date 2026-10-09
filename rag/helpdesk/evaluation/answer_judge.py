async def judge(models, case, output):
    if output['status'] == 'error':
        return {'correct': False if case.expected_behavior == 'answer' else None, 'needs_review': True, 'reason': '执行失败'}
    expected = {'answer':'completed', 'refuse':'refused', 'clarify':'clarify'}[case.expected_behavior]
    if output['status'] != expected:
        return {'correct': False, 'needs_review': False, 'reason': '期望行为不匹配'}
    result = await models.json('你是独立评委，不知道实验名称。依据参考事实和原文，逐条检查必要事实是否被完整正确覆盖，禁止事实是否出现，组织/条件是否正确，回答陈述是否受其引用来源支持。拒答/澄清需判断理由是否合理。输出 {"facts":[{"fact":"原样必要事实","pass":true,"reason":"..."}],"has_error":false,"behavior_reason_valid":true,"citation_support":true,"reason":"..."}。不得因回答流畅就通过。',
                               {'question': case.question, 'expected_behavior': case.expected_behavior, 'required_facts':case.required_facts,
                                'forbidden_facts':case.forbidden_facts,'reference_answer':case.reference_answer,
                                'answer':output['answer'],'evidence':output['evidence'],'citations':output['citations']})
    facts = result.get('facts')
    if not isinstance(facts,list) or [f.get('fact') for f in facts] != case.required_facts or any(type(f.get('pass')) is not bool for f in facts) or any(type(result.get(k)) is not bool for k in ('has_error','behavior_reason_valid','citation_support')):
        raise ValueError('评委结果不符合逐事实评分格式')
    result['correct'] = all(f['pass'] for f in facts) and not result['has_error'] and result['behavior_reason_valid']
    result['needs_review'] = True
    return result
