"""Prompt assembly, contracts, bounded orchestration, and Markdown rendering.

Python 3.10+. The application supplies a generate_json callable. No provider,
credential, network operation, candidate store, or PDF renderer is embedded.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import re
from datetime import date
from pathlib import Path
from typing import Callable

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.0.0'
STAGES = {
    'normalize': ('01_normalize.txt', 'normalized'),
    'draft': ('02_draft.txt', 'draft'),
    'assess': ('03_assess.txt', 'assessment'),
    'repair': ('04_repair.txt', 'draft'),
    'revision': ('05_revision.txt', 'draft'),
}


class ContractError(ValueError):
    """The host must not deliver an artifact that failed a contract."""


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def schema(name):
    return load_json(ROOT / 'schemas' / f'{name}.schema.json')


def validate_schema(name, value):
    s = schema(name)
    Draft202012Validator.check_schema(s)
    errors = list(Draft202012Validator(s, format_checker=FormatChecker()).iter_errors(value))
    if errors:
        msg = '; '.join(f'{list(e.path)}: {e.message}' for e in errors[:8])
        raise ContractError(f'{name}: {msg}')


def require(ok, message):
    if not ok:
        raise ContractError(message)


def indexed(items, key):
    result = {item[key]: item for item in items}
    require(len(result) == len(items), f'Duplicate {key}')
    return result


def role_ids(request):
    return [r['role_id'] for r in request['candidate']['employment']]


def check_envelope(request, output):
    expected = {'package_version': VERSION, 'run_id': request['run_id'],
                'candidate_id': request['candidate']['candidate_id'],
                'jd_id': request['jd']['jd_id']}
    for key, value in expected.items():
        require(output[key] == value, f'Wrong {key}')


def valid_date_string(value):
    require(bool(re.fullmatch(r'\d{4}-\d{2}(?:-\d{2})?', value)), 'Use ISO year-month or date')
    return date.fromisoformat(value if len(value) == 10 else value + '-01')


def validate_request(request):
    validate_schema('request', request)
    cid = request['candidate']['candidate_id']
    indexed(request['candidate']['employment'], 'role_id')
    indexed(request['sources'], 'source_id')
    indexed(request['external_context'], 'source_id')
    require(not ({s['source_id'] for s in request['sources']} &
                 {s['source_id'] for s in request['external_context']}), 'Source ID namespace collision')
    for source in request['sources']:
        require(source['candidate_id'] == cid, 'Cross-candidate source')
    opt = request['options']
    if opt['mode'] == 'APPLICATION':
        require(opt['metric_policy'] == 'EVIDENCED_ONLY', 'APPLICATION requires EVIDENCED_ONLY')
        require(not opt['hypothetical_metrics_authorized'], 'APPLICATION cannot authorize fictional metrics')
        require(opt['illustrative_scenario'] is None, 'APPLICATION cannot use an illustrative scenario')
    if opt['metric_policy'] == 'LABELED_HYPOTHETICAL':
        require(opt['mode'] == 'ILLUSTRATIVE' and opt['hypothetical_metrics_authorized'],
                'Hypothetical metrics need explicit illustrative authorization')
    known_categories = {x['id'] for x in load_json(ROOT / 'reference/ai_keyword_catalog.json')['categories']}
    require(set(opt['keyword_category_ids']) <= known_categories, 'Unknown keyword category')
    as_of = date.fromisoformat(request['as_of_date'])
    for role in request['candidate']['employment']:
        start = valid_date_string(role['start_date'])
        require(start <= as_of, 'Future employment start is not completed work')
        if role['is_current']:
            require(role['end_date'] is None, 'Current role must use null end_date')
        else:
            require(role['end_date'] is not None, 'Noncurrent role needs end_date')
            end = valid_date_string(role['end_date'])
            require(start <= end <= as_of, 'Invalid or future employment interval')
    if request['run_type'] == 'NEW_JD':
        require(request['revision'] is None, 'NEW_JD must not receive a selected generated draft')
    else:
        rev = request['revision']
        require(rev is not None, 'REVISION requires selected draft and scope')
        validate_schema('draft', rev['selected_draft'])
        require(rev['selected_draft']['candidate_id'] == cid, 'Selected draft belongs to another candidate')
        if rev['scope'] == 'SELECTED':
            require(bool(rev['selected_bullet_ids'] or rev['selected_sections']), 'Empty revision scope')
            require(set(rev['selected_bullet_ids']) <= set(bullets(rev['selected_draft'])), 'Unknown selected bullet')


def validate_normalized(request, normalized):
    validate_schema('normalized', normalized)
    check_envelope(request, normalized)
    require(normalized['profile_version'] == request['candidate']['profile_version'], 'Wrong profile version')
    sources = indexed(request['sources'], 'source_id')
    facts = indexed(normalized['facts'], 'fact_id')
    indexed(normalized['requirements'], 'requirement_id')
    roles = set(role_ids(request))
    status = {'CANDIDATE_STATEMENT': 'SUPPLIED', 'CANDIDATE_DOCUMENT': 'DOCUMENTED', 'UNCONFIRMED': 'UNCONFIRMED'}
    for fact in facts.values():
        require(fact['source_id'] in sources, 'Fact cites unknown/noncandidate source')
        source = sources[fact['source_id']]
        require(fact['quote'].strip() and fact['quote'] in source['content'], 'Fact quote not found verbatim')
        require(fact['status'] == status[source['kind']], 'Source status was promoted or changed')
        require(fact['role_id'] is None or fact['role_id'] in roles, 'Unknown fact role')
    for req in normalized['requirements']:
        require(req['quote'].strip() and req['quote'] in request['jd']['text'], 'Requirement quote not in JD')
    for item in normalized['inventory']:
        require(item['fact_ids'] and set(item['fact_ids']) <= set(facts), 'Inventory lacks fact references')
        require(item['role_id'] is None or item['role_id'] in roles, 'Unknown inventory role')
    external_ids = {s['source_id'] for s in request['external_context']}
    for ctx in normalized['company_context']:
        require(set(ctx['source_ids']) <= external_ids, 'Company context cites unknown external evidence')
        require(set(ctx['candidate_participation_fact_ids']) <= set(facts), 'Unknown participation evidence')
        if ctx['alignment'] == 'DOCUMENTED_RELATED':
            require(bool(ctx['source_ids']), 'Documented company context requires a source')


def bullets(draft):
    all_bullets = [b for r in draft['roles'] for b in r['bullets']]
    return indexed(all_bullets, 'bullet_id')


def highlight_ranges(block):
    spans = []
    for h in block['highlights']:
        matches = list(re.finditer(re.escape(h['phrase']), block['text']))
        require(len(matches) >= h['occurrence'], 'Highlight phrase/occurrence not in text')
        match = matches[h['occurrence'] - 1]
        span = (match.start(), match.end())
        require(not any(a < span[1] and span[0] < b for a, b in spans), 'Overlapping highlights')
        require(not re.fullmatch(r'[\d\s,.%+−–-]+', h['phrase']), 'Standalone numeric highlight')
        require(h['phrase'].lower() not in {'and', 'built', 'led', 'used', 'developed'}, 'Generic highlighted lead-in')
        spans.append(span)
    return sorted(spans)


def check_coverage(normalized, coverage, terms, draft):
    require(set(indexed(coverage, 'requirement_id')) ==
            {r['requirement_id'] for r in normalized['requirements']}, 'Incomplete requirement coverage')
    expected_terms = {t for r in normalized['requirements'] for t in r['technical_terms']}
    require(set(indexed(terms, 'term')) == expected_terms, 'Incomplete JD technical-term accounting')
    known_facts = {f['fact_id'] for f in normalized['facts']}
    known_bullets = set(bullets(draft))
    known_claims = {c['claim_id'] for c in draft['claims']}
    for row in coverage:
        require(set(row['fact_ids']) <= known_facts, 'Coverage cites unknown facts')
        require(set(row['bullet_ids']) <= known_bullets, 'Coverage cites unknown current bullets')
    for row in terms:
        require(set(row['claim_ids']) <= known_claims, 'Term decision cites unknown claims')
        if row['decision'] in {'INCLUDED','ALTERNATIVE_COVERED'}:
            require(bool(row['claim_ids']), 'Included term or alternative lacks claim references')


def validate_scope(request, draft):
    rev = request['revision']
    if not rev or rev['scope'] == 'FULL':
        return
    old = rev['selected_draft']
    allowed_sections = set(rev['selected_sections'])
    allowed_bullets = set(rev['selected_bullet_ids'])
    for section in ('summary', 'skills'):
        if section not in allowed_sections:
            require(draft[section] == old[section], f'Out-of-scope {section} edit')
    old_b, new_b = bullets(old), bullets(draft)
    require(set(new_b) <= set(old_b), 'Narrow revision added a new bullet without full scope')
    for bid, bullet in old_b.items():
        if bid not in allowed_bullets:
            require(new_b.get(bid) == bullet, f'Out-of-scope edit: {bid}')
    for old_role, new_role in zip(old['roles'], draft['roles']):
        require(old_role['role_id'] == new_role['role_id'], 'Out-of-scope role change')
        if 'role_scopes' not in allowed_sections:
            require(old_role['scope'] == new_role['scope'], 'Out-of-scope role scope edit')
        retained = [b['bullet_id'] for b in old_role['bullets'] if b['bullet_id'] in new_b]
        require(retained == [b['bullet_id'] for b in new_role['bullets']], 'Narrow revision moved bullets')


def validate_draft(request, normalized, draft, previous=None):
    validate_schema('draft', draft)
    check_envelope(request, draft)
    require(draft['mode'] == request['options']['mode'], 'Draft changed mode')
    require([r['role_id'] for r in draft['roles']] == role_ids(request), 'Draft changed role order or identity')
    facts = indexed(normalized['facts'], 'fact_id')
    claims = indexed(draft['claims'], 'claim_id')
    metrics = indexed(draft['metrics'], 'metric_id')
    current = bullets(draft)
    roles = set(role_ids(request))
    profile_paths = {'name', 'headline', 'education', 'certifications', 'contacts'} | {f'employment.{rid}' for rid in roles}
    for claim in claims.values():
        require(claim['role_id'] is None or claim['role_id'] in roles, 'Claim belongs to unknown role')
        require(set(claim['fact_ids']) <= set(facts), 'Claim cites unknown fact')
        require(set(claim['profile_paths']) <= profile_paths, 'Unknown structured profile reference')
        if claim['status'] != 'ILLUSTRATIVE':
            require(bool(claim['fact_ids'] or claim['profile_paths']), 'Factual claim lacks evidence')
            require(all(facts[f]['status'] != 'UNCONFIRMED' for f in claim['fact_ids']), 'Unconfirmed fact used as supported')
            if claim['role_id'] is not None:
                require(all(facts[f]['role_id'] in (None, claim['role_id']) for f in claim['fact_ids']), 'Source evidence moved to another employer')
        else:
            require(draft['mode'] == 'ILLUSTRATIVE', 'Illustrative claim in APPLICATION')
    if draft['mode'] == 'ILLUSTRATIVE':
        require('Illustrative resume draft' in draft['notices'], 'Missing illustrative notice')
        require(draft['status'] == 'ILLUSTRATIVE', 'Illustrative draft has misleading status')
    else:
        require(draft['status'] != 'ILLUSTRATIVE', 'APPLICATION has illustrative status')
    blocks = [draft['summary']] + [r['scope'] for r in draft['roles']] + list(current.values())
    for block in blocks:
        require(set(block['claim_ids']) <= set(claims), 'Unknown claim in text')
        require(not block['text'].strip() or bool(block['claim_ids']), 'Unattributed text block')
        require('**' not in block['text'], 'Use highlight fields instead of raw bold markup')
        highlight_ranges(block)
    for group in draft['skills']:
        for item in group['items']:
            require(item['claim_ids'] and set(item['claim_ids']) <= set(claims), 'Skill lacks claim evidence')
            require('**' not in item['text'], 'Skills cannot be bold')
    for role in draft['roles']:
        for bullet in role['bullets']:
            require(set(bullet['metric_ids']) <= set(metrics), 'Bullet cites unknown metric')
            for mid in bullet['metric_ids']:
                require(bullet['bullet_id'] in metrics[mid]['bullet_ids'], 'Bullet/metric references disagree')
            for cid in bullet['claim_ids']:
                require(claims[cid]['role_id'] in (None, role['role_id']), 'Claim moved to wrong employer')
    for metric in metrics.values():
        require(metric['claim_id'] in claims, 'Metric has unknown claim')
        require(bool(metric['bullet_ids']) and set(metric['bullet_ids']) <= set(current), 'Metric has missing bullet')
        require(set(metric['fact_ids']) <= set(facts), 'Metric cites unknown fact')
        for bid in metric['bullet_ids']:
            require(metric['metric_id'] in current[bid]['metric_ids'], 'Metric/bullet references disagree')
        if metric['status'] == 'HYPOTHETICAL':
            require(request['options']['mode'] == 'ILLUSTRATIVE' and
                    request['options']['metric_policy'] == 'LABELED_HYPOTHETICAL' and
                    request['options']['hypothetical_metrics_authorized'], 'Unauthorized hypothetical metric')
            require('Example metrics are hypothetical' in draft['notices'], 'Missing hypothetical metric notice')
            require(claims[metric['claim_id']]['status'] == 'ILLUSTRATIVE', 'Fictional metric marked supported')
        else:
            require(bool(metric['fact_ids']), 'Actual metric lacks source facts')
            require(all(facts[f]['status'] != 'UNCONFIRMED' for f in metric['fact_ids']), 'Unconfirmed actual metric')
            require(claims[metric['claim_id']]['status'] != 'ILLUSTRATIVE', 'Actual metric has illustrative claim')
            if metric['status'] == 'DERIVED':
                require(bool(metric['calculation']), 'Derived metric lacks calculation')
    removed = indexed(draft['lineage'], 'original_bullet_id')
    require(not (set(removed) & set(current)), 'Removed bullet is still current')
    for row in removed.values():
        require(set(row['target_bullet_ids']) <= set(current), 'Lineage points to missing target')
        if row['disposition'] == 'MERGED':
            require(bool(row['target_bullet_ids']), 'Merge needs target')
    if previous is not None:
        prior_ids = set(bullets(previous)) | {x['original_bullet_id'] for x in previous['lineage']}
        require(prior_ids <= set(current) | set(removed), 'Removed/merged audit lineage lost')
    check_coverage(normalized, draft['requirement_coverage'], draft['term_decisions'], draft)
    indexed(draft['role_plan'], 'role_id')
    for plan in draft['role_plan']:
        require(plan['role_id'] in roles, 'Unknown role plan')
        require(set(plan['requirement_ids']) <= {r['requirement_id'] for r in normalized['requirements']}, 'Unknown planned requirement')
    require({p['role_id'] for p in draft['role_plan']} == roles, 'Role plan omitted employment')
    validate_scope(request, draft)


def validate_assessment(request, normalized, draft, assessment):
    validate_schema('assessment', assessment)
    check_envelope(request, assessment)
    require(assessment['revision_id'] == draft['revision_id'], 'Assessment is for another revision')
    expected = set(bullets(draft)) | {r['original_bullet_id'] for r in draft['lineage']}
    require(set(indexed(assessment['bullet_audit'], 'bullet_id')) == expected, 'Incomplete bullet audit')
    require(assessment['layout_status'] == 'LAYOUT_UNVERIFIED' and assessment['page_count'] is None,
            'Starter has no renderer evidence; model cannot certify layout')
    needed_sections = {'IDENTITY_HISTORY','SUMMARY','SKILLS','METRICS','BUSINESS_CONTEXT',
                       'TECHNICAL_COHERENCE','JD_COVERAGE','READABILITY','LAYOUT'}
    require(set(indexed(assessment['section_checks'], 'section')) == needed_sections, 'Incomplete section audit')
    indexed(assessment['findings'], 'finding_id')
    check_coverage(normalized, assessment['requirement_coverage'], assessment['term_decisions'], draft)
    severe = any(not f['resolved'] and f['severity'] in {'BLOCKER','MAJOR'} for f in assessment['findings'])
    if assessment['verdict'] in {'READY','ILLUSTRATIVE'}:
        require(not severe, 'Passing verdict has unresolved material findings')
    if assessment['verdict'] == 'READY':
        require(draft['mode'] == 'APPLICATION' and draft['status'] == 'COMPLETE_DRAFT', 'Misleading ready verdict')
        require(all(r['evidence_status'] in {'SUPPORTED','TRANSFERABLE'} for r in assessment['bullet_audit'] if r['disposition']=='RETAINED'), 'Ready verdict contains unsupported bullets')
    if assessment['verdict'] == 'ILLUSTRATIVE':
        require(draft['mode'] == 'ILLUSTRATIVE', 'Wrong illustrative verdict')
    for row in assessment['bullet_audit']:
        require(set(row['fact_ids']) <= {f['fact_id'] for f in normalized['facts']}, 'Audit cites unknown fact')
        current_b = bullets(draft)
        if row['bullet_id'] in current_b:
            require(row['disposition'] == 'RETAINED', 'Current bullet audit has wrong disposition')
            claims = {c['claim_id']: c for c in draft['claims']}
            statuses = {claims[c]['status'] for c in current_b[row['bullet_id']]['claim_ids']}
            if 'ILLUSTRATIVE' in statuses:
                require(row['evidence_status'] in {'ILLUSTRATIVE','MIXED','UNSUPPORTED'}, 'Illustrative bullet promoted by assessor')
        else:
            lineage = {r['original_bullet_id']: r for r in draft['lineage']}[row['bullet_id']]
            require(row['disposition'] == lineage['disposition'], 'Removed/merged audit disposition mismatch')
            require(row['target_bullet_ids'] == lineage['target_bullet_ids'], 'Audit lineage target mismatch')


def prepare_stage(stage, request, normalized=None, draft=None, assessment=None):
    require(stage in STAGES, 'Unknown stage')
    prompt_file, output_type = STAGES[stage]
    core = (ROOT / 'prompts/00_core.txt').read_text(encoding='utf-8')
    prompt = (ROOT / 'prompts' / prompt_file).read_text(encoding='utf-8')
    data = {'request': copy.deepcopy(request)}
    for key, value in (('normalized', normalized), ('draft', draft), ('assessment', assessment)):
        if value is not None:
            data[key] = copy.deepcopy(value)
    if stage != 'normalize':
        data['assessment_rubric'] = (ROOT / 'reference/assessment_rubric.txt').read_text(encoding='utf-8')
    if stage in {'draft','revision','repair'}:
        catalog = load_json(ROOT / 'reference/ai_keyword_catalog.json')
        selected = set(request['options']['keyword_category_ids'])
        if selected:
            catalog['categories'] = [c for c in catalog['categories'] if c['id'] in selected]
        data['keyword_reference'] = catalog
    return {'instructions': core + '\n\n' + prompt,
            'input_data': data, 'output_schema': schema(output_type)}


def escape_md(text):
    text = html.escape(text, quote=False)
    return re.sub(r'([\\`*_{}\[\]<>#|])', r'\\\1', text)


def styled(block):
    text = block['text']
    ranges = highlight_ranges(block)
    result, pos = [], 0
    for start, end in ranges:
        result += [escape_md(text[pos:start]), '**' + escape_md(text[start:end]) + '**']
        pos = end
    result.append(escape_md(text[pos:]))
    return ''.join(result)


def render_markdown(request, draft):
    """Text rendering only. Never claims page count or PDF inspection."""
    profile = request['candidate']
    lines = [f'**{escape_md(n)}**' for n in draft['notices']]
    if profile['name']:
        lines += ['', '# ' + escape_md(profile['name'])]
    if profile['headline']:
        lines += ['', escape_md(profile['headline'])]
    if profile['contacts']:
        lines += ['', ' · '.join(escape_md(c['value']) for c in profile['contacts'])]
    if draft['summary']['text']:
        lines += ['', '## Professional Summary', '', styled(draft['summary'])]
    if draft['skills']:
        lines += ['', '## Technical Skills', '']
        lines += ['- ' + ', '.join(escape_md(i['text']) for i in g['items']) for g in draft['skills'] if g['items']]
    if draft['roles']:
        lines += ['', '## Professional Experience']
    originals = {r['role_id']: r for r in profile['employment']}
    for role in draft['roles']:
        original = originals[role['role_id']]
        end = 'Present' if original['is_current'] else original['end_date']
        lines += ['', '### ' + escape_md(original['employer']), '',
                  escape_md(original['official_title']) + ' | ' + original['start_date'] + ' – ' + str(end)]
        if original['location']:
            label = 'Headquarters: ' if original['location_type'] == 'HEADQUARTERS' else ''
            lines += [escape_md(label + original['location'])]
        if role['scope']['text']:
            lines += ['', styled(role['scope'])]
        if role['bullets']:
            lines += [''] + ['- ' + styled(b) for b in role['bullets']]
    for key, title in [('education','Education'),('certifications','Certifications')]:
        if profile[key]:
            lines += ['', '## ' + title, ''] + [escape_md(x) for x in profile[key]]
    return '\n'.join(lines).strip() + '\n'


def run_pipeline(request: dict, generate_json: Callable[..., dict]) -> dict:
    """Run with an externally supplied adapter. Contract failures raise; no delivery."""
    request = copy.deepcopy(request)
    validate_request(request)
    history = []

    def call(stage, normalized=None, draft=None, assessment=None):
        output = generate_json(**prepare_stage(stage, request, normalized, draft, assessment))
        validate_schema(STAGES[stage][1], output)
        history.append({'stage': stage, 'output': copy.deepcopy(output)})
        return output

    normalized = call('normalize')
    validate_normalized(request, normalized)
    first = 'revision' if request['run_type'] == 'REVISION' else 'draft'
    previous = request['revision']['selected_draft'] if request['revision'] else None
    draft = call(first, normalized)
    validate_draft(request, normalized, draft, previous)
    assessment = call('assess', normalized, draft)
    validate_assessment(request, normalized, draft, assessment)
    for _ in range(request['options']['max_repair_rounds']):
        if assessment['verdict'] not in {'REVISE','REVIEW_NEEDED'}:
            break
        previous = draft
        draft = call('repair', normalized, draft, assessment)
        require(draft['revision_id'] != previous['revision_id'], 'Repair needs a new revision ID')
        validate_draft(request, normalized, draft, previous)
        assessment = call('assess', normalized, draft)
        validate_assessment(request, normalized, draft, assessment)
    status = assessment['verdict']
    if status == 'REVISE':
        status = 'REVIEW_NEEDED'
    blocking = any(not f['resolved'] and f['severity'] == 'BLOCKER' for f in assessment['findings'])
    return {'package_version': VERSION, 'request': request, 'normalized': normalized,
            'draft': draft, 'assessment': assessment, 'history': history,
            'delivery_status': status, 'layout_status': 'LAYOUT_UNVERIFIED',
            'resume_markdown': None if blocking else render_markdown(request, draft),
            'report': {'run_type': request['run_type'], 'mode': draft['mode'],
                       'profile_version': request['candidate']['profile_version'],
                       'as_of_date': request['as_of_date'], 'delivery_status': status,
                       'metric_policy': request['options']['metric_policy'],
                       'inputs': {'candidate_id': request['candidate']['candidate_id'],
                                  'source_ids': [s['source_id'] for s in request['sources']],
                                  'external_source_ids': [s['source_id'] for s in request['external_context']],
                                  'jd_id': request['jd']['jd_id'], 'run_id': request['run_id'],
                                  'revision_id': draft['revision_id'], 'package_version': VERSION},
                       'source_conflicts': normalized['source_conflicts'],
                       'company_context': normalized['company_context'],
                       'source_inventory': normalized['inventory'],
                       'requested_formats': request['options']['formats'],
                       'available_output': 'MARKDOWN_PREVIEW' if not blocking else 'WITHHELD_BLOCKING_FINDINGS',
                       'draft_notes': {k: draft[k] for k in ['status','requirement_coverage','term_decisions','role_plan','gaps','assumptions','technical_limits','company_notes','confirmation_items','metrics','changes','lineage']},
                       'assessment': assessment, 'layout_status': 'LAYOUT_UNVERIFIED'}}


def main():
    parser = argparse.ArgumentParser(description='Prepare provider-neutral model payloads; no API call.')
    parser.add_argument('request')
    parser.add_argument('--stage', choices=STAGES, default='normalize')
    parser.add_argument('--normalized')
    parser.add_argument('--draft')
    parser.add_argument('--assessment')
    args = parser.parse_args()
    request = load_json(args.request)
    validate_request(request)
    normalized = load_json(args.normalized) if args.normalized else None
    draft = load_json(args.draft) if args.draft else None
    assessment = load_json(args.assessment) if args.assessment else None
    if args.stage != 'normalize':
        require(normalized is not None, 'This stage needs --normalized')
        validate_normalized(request, normalized)
    if args.stage in {'assess','repair'}:
        require(draft is not None, 'This stage needs --draft')
        validate_draft(request, normalized, draft)
    if args.stage == 'repair':
        require(assessment is not None, 'Repair needs --assessment')
        validate_assessment(request, normalized, draft, assessment)
    print(json.dumps(prepare_stage(args.stage, request, normalized, draft, assessment), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
