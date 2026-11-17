"""Evidence-grounded B/C reflection over distinct-task trajectory bundles."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from diagnosis import (read, save, decode_json, public_trace, validate_package,
                       validate_package_fail_closed, validate_patch,
                       DIAGNOSIS_PROMPT, PATCH_PROMPT)
from model_api import chat


MEMORY_PATCH_PROMPT = PATCH_PROMPT.replace('"document":"format|task",\n', '').replace(
    '"content":"general reusable conditional rule"',
    '"title":"short strategy name","description":"when this strategy applies",'
    '"content":"the decision, action and observable verification"',
) + '''
Write ReasoningBank-style memory items. The decision model chooses the destination.
Use exact pairs from eligible_sources. From successful work, extract a decision
supported by its execution and observed result. From a failure, use check
outcomes to identify what failed. Ground repair advice in verified causal
evidence. When comparing practices, account for differences in input and
scoring. A binary score is an outcome, not a causal explanation.
An isolated controller replay confirms the stated historical mechanism only
for its checked conditions. Use current trajectory evidence to establish
whether the same mechanism is relevant to this practice.
Each field is one nonempty line. Title names the strategy. Description states
an observable trigger or applicability condition. Content gives one to three
actionable sentences including an observable check. Retain meaningful domain
signals while abstracting away instance-specific values, commands and paths.
Write one decision per item. Merge overlapping items, including rules that
repeat the same verification. Use the smallest sufficient check and state its
completion condition. Require extra checks only for a distinct remaining risk
supported by the evidence.
Prefer a strategy that addresses the scored failure over incidental tool errors.
Return no edit when the evidence does not support a transferable action.
'''


def diagnose_if_eligible(directory, public_task, destination):
    """Run attribution only for complete, valid, scored failures.

    Every selected trajectory still gets a structurally valid packet. Successful
    or infrastructure-failed attempts receive a deterministic empty packet and
    never consume a diagnosis-model call.
    """
    directory = Path(directory)
    destination = Path(destination)
    if (destination / 'diagnosis.json').exists():
        raise RuntimeError('Prior diagnosis exists; explicit cache use required')
    trace = public_trace(directory)
    save(destination / 'input-trace.json', trace)
    evaluation = read(directory / 'operator-evaluation.json')
    eligible = (evaluation.get('execution_complete') is True and
                evaluation.get('environment_valid') is True and
                evaluation.get('evaluation', {}).get('reward') == '0')
    save(destination / 'eligibility.json', {
        'eligible_for_attribution': eligible,
        'operator_only': True,
        'not_reflection_input': True,
        'policy': 'complete valid scored failures only',
    })
    if eligible:
        raw = chat(destination, 'diagnose', DIAGNOSIS_PROMPT,
                   {'public_task': public_task, 'trajectory': trace})
        package, audit = validate_package_fail_closed(decode_json(raw), trace)
    else:
        package = validate_package({'trajectory_id': trace['trajectory_id'], 'findings': []}, trace)
        audit = {'count': 0, 'policy': 'diagnosis model not called for ineligible trajectory', 'items': []}
    save(destination / 'evidence-downgrades.json', audit)
    save(destination / 'diagnosis.json', package)
    return {'public_task': public_task, 'trajectory': trace, 'diagnosis': package}


# Compatibility for the imported legacy pilot module. The unique runner calls
# diagnose_if_eligible directly; no legacy pilot execution is part of this run.
def diagnose(directory, public_task, destination):
    return diagnose_if_eligible(directory, public_task, destination)


def update(skills, inputs, group, destination, *, decide=None):
    """Apply one frozen-before-Skill update from k distinct task trajectories."""
    destination = Path(destination)
    save(destination / 'skill-before.json', skills)
    save(destination / 'input-manifest.json', {
        'group': group,
        'tasks': [item['task_id'] for item in inputs],
        'trajectory_ids': [item['trajectory']['trajectory_id'] for item in inputs],
        'diagnosis_hashes': [item['diagnosis']['package_sha256'] for item in inputs],
        'full_trajectories_supplied': True,
        'distinct_task_instances': len({item['task_id'] for item in inputs}) == len(inputs),
        'same_frozen_pre_update_skills': True,
        'separate_skill_documents': True,
    })
    packages = [item['diagnosis'] for item in inputs]
    common = {'current_skills': skills}
    try:
        if group.startswith('B'):
            def one(pair):
                index, item = pair
                raw = chat(destination, f'reflect-{index}',
                    PATCH_PROMPT + '\nAnalyze exactly ONE supplied distinct-task trajectory.',
                    {**common, 'input': item})
                patch = decode_json(raw)
                validate_patch(patch, [item['diagnosis']], skills, '\n\n')
                save(destination / f'candidate-{index}.json', patch)
                return patch
            with ThreadPoolExecutor(max_workers=2) as pool:
                patches = list(pool.map(one, enumerate(inputs, 1)))
            raw = chat(destination, 'merge', PATCH_PROMPT +
                '\nMerge supplied candidates without inventing evidence. Preserve source IDs.',
                {**common, 'candidates': patches, 'diagnosis_packets': packages})
        else:
            prompt = PATCH_PROMPT
            payload = {**common, 'inputs': inputs}
            if decide is not None:
                prompt = MEMORY_PATCH_PROMPT
                payload['eligible_sources'] = [
                    {'trajectory_id': p['trajectory_id'], 'finding_id': f['finding_id']}
                    for p in packages for f in p['findings'] if f['status'] == 'located']
                payload['inputs'] = [
                    {**item, 'diagnosis': {**package, 'findings': [f for f in package['findings']
                                                                if f['status'] == 'located']}}
                    for item, package in zip(inputs, packages, strict=True)]
            if decide is not None and not payload['eligible_sources']:
                raw = '{"edits": []}'
            else:
                raw = chat(destination, 'joint', prompt +
                    f'\nCompare exactly {len(inputs)} complete distinct-task trajectories and packets.', payload)
        patch = decode_json(raw)
        if decide is not None:
            patch = decide_patch(patch, inputs, skills, destination, decide)
        proposed = validate_patch(patch, packages, skills, '\n\n')
        save(destination / 'selected.json', patch)
        if decide is None:
            review = decode_json(chat(destination, 'content-review',
                'Review generalized Skill edits only. No hidden grader. Return JSON '
                '{"generic_only":true/false,"routing_correct":true/false,"reason":"..."}. '
                'Reject exact task IDs/paths, complete solutions, credentials or unverified final-cause claims. '
                'Check format rules concern submission/output contracts and task rules concern domain strategy.',
                {'proposed_edits': patch['edits'], 'source_findings': packages}))
            save(destination / 'content-review.json', review)
            if review.get('generic_only') is not True or review.get('routing_correct') is not True:
                raise ValueError('skill_content_or_routing_rejected')
        decision = {
            'status': 'accepted' if proposed != skills else 'valid_no_change',
            'failure_kind': None,
            'performance_gate': False,
            'evidence_reference_checks': 'passed',
            'causal_human_review': 'pending',
            'edits': len(patch['edits']),
        }
        if decide is not None:
            decision['skill_routing'] = 'jev'
    except Exception as exc:
        proposed = skills
        decision = {
            'status': 'update_failed_previous_skills_carried_forward',
            'failure_kind': str(exc).split(':', 1)[0],
            'error': str(exc),
            'performance_gate': False,
        }
        request_errors = [*destination.glob('calls/*/error.json'), *destination.glob('calls/*/*/error.json')]
        unknown = any(read(path).get('phase') == 'unknown' or read(path).get('outcome_unknown')
                      for path in request_errors)
        if isinstance(exc, SkillDecisionError) or unknown:
            decision['status'] = 'update_requires_inspection'
            save(destination / 'decision.json', decision)
            raise RuntimeError('Skill decision requires inspection') from exc
    save(destination / 'skill-after.json', proposed)
    save(destination / 'decision.json', decision)
    for name, text in proposed.items():
        (destination / f'{name.upper()}_SKILL.md').write_text(text, encoding='utf-8')
    return proposed, decision


class SkillDecisionError(RuntimeError):
    """The decision model did not return a valid decision."""


def decide_patch(patch, inputs, skills, destination, decide):
    if not isinstance(patch, dict) or not isinstance(patch.get('edits'), list) or len(patch['edits']) > 2:
        raise ValueError('patch_schema_error: expected <=2 edits')
    packages = [item['diagnosis'] for item in inputs]
    evidence = {}
    for item in inputs:
        package = item['diagnosis']
        for finding in package['findings']:
            if finding['status'] == 'located':
                evidence[package['trajectory_id'], finding['finding_id']] = {
                    'claim': finding['problem_or_observation'], 'claim_type': finding.get('claim_type'),
                    'public_requirement': finding.get('public_requirement'),
                    'quotes': [ref['quote'] for ref in finding['evidence']],
                    'training_reward': item['training_reward'],
                    'check_outcomes': item.get('check_outcomes'),
                    'verified_failure_mechanism': item.get('verified_failure_mechanism'),
                    'confirmed_replays': item.get('confirmed_replays', []),
                }
    accepted, rejected = [], []
    working_skills = skills
    for index, edit in enumerate(patch['edits'], 1):
        try:
            fields = {}
            for key in ('title', 'description', 'content'):
                value = edit.get(key)
                if not isinstance(value, str) or not value.strip() or len(value.splitlines()) != 1:
                    raise ValueError(f'patch_schema_error: invalid memory {key}')
                fields[key] = value.strip()
            edit = {
                'op': edit.get('op'), 'sources': edit.get('sources'),
                'content': '\n'.join(f'**{key.capitalize()}** {value}' for key, value in fields.items()),
            }
            # Validate content and references before sending them to the decision model.
            validate_patch({'edits': [{**edit, 'document': 'task'}]}, packages, skills, '\n\n')
        except (ValueError, TypeError, AttributeError) as error:
            rejected.append({'edit': index, 'reason': str(error)})
            continue
        sources = [evidence[ref['trajectory_id'], ref['finding_id']] for ref in edit['sources']]
        try:
            decision = decide(destination / 'jev' / f'edit-{index:02}', edit['content'], sources, working_skills)
        except Exception as error:
            raise SkillDecisionError('Jev did not decide on a valid candidate') from error
        if decision['adopted']:
            routed = {**edit, 'document': decision['document']}
            try:
                working_skills = validate_patch({'edits': [*accepted, routed]}, packages, skills, '\n\n')
            except ValueError as error:
                rejected.append({'edit': index, 'reason': str(error)})
                continue
            accepted.append(routed)
        else:
            rejected.append({'edit': index, 'reason': 'Jev did not adopt the rule'})
    save(destination / 'rejected-edits.json', rejected)
    return {**patch, 'edits': accepted}
