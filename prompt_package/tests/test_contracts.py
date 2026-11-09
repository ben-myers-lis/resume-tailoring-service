import copy
import unittest
from pathlib import Path

from app.runner import (ContractError, ROOT, load_json, prepare_stage, run_pipeline,
                        validate_request, validate_normalized, validate_draft,
                        validate_assessment, render_markdown)


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.r, self.n, self.d, self.a = [load_json(ROOT/'examples'/f'{x}.json')
                                        for x in ('request','normalized','draft','assessment')]

    def test_complete_fixture(self):
        validate_request(self.r)
        validate_normalized(self.r,self.n)
        validate_draft(self.r,self.n,self.d)
        validate_assessment(self.r,self.n,self.d,self.a)

    def test_offline_pipeline(self):
        outputs=iter([self.n,self.d,self.a])
        result=run_pipeline(self.r,lambda **kwargs: copy.deepcopy(next(outputs)))
        self.assertEqual(result['delivery_status'],'READY')
        self.assertEqual(len(result['history']),3)
        self.assertIn('Synthetic Test Candidate',result['resume_markdown'])
        self.assertEqual(result['layout_status'],'LAYOUT_UNVERIFIED')

    def test_cross_candidate_source(self):
        self.r['sources'][0]['candidate_id']='another-person'
        with self.assertRaises(ContractError):validate_request(self.r)

    def test_new_jd_rejects_prior_draft(self):
        self.r['revision']={'selected_draft':self.d,'scope':'FULL','selected_bullet_ids':[],
                            'selected_sections':[],'feedback':'','assessment_feedback':''}
        with self.assertRaises(ContractError):validate_request(self.r)

    def test_unknown_source_kind(self):
        self.r['sources'][0]['kind']='GENERATED_RESUME'
        with self.assertRaises(ContractError):validate_request(self.r)

    def test_application_rejects_hypothetical_policy(self):
        self.r['options']['metric_policy']='LABELED_HYPOTHETICAL'
        with self.assertRaises(ContractError):validate_request(self.r)

    def test_illustrative_metrics_need_authorization(self):
        self.r['options'].update(mode='ILLUSTRATIVE',metric_policy='LABELED_HYPOTHETICAL')
        with self.assertRaises(ContractError):validate_request(self.r)

    def test_source_quote_must_exist(self):
        self.n['facts'][0]['quote']='Invented quotation'
        with self.assertRaises(ContractError):validate_normalized(self.r,self.n)

    def test_unconfirmed_cannot_be_promoted(self):
        self.r['sources'][0]['kind']='UNCONFIRMED'
        with self.assertRaises(ContractError):validate_normalized(self.r,self.n)

    def test_unconfirmed_cannot_support_claim(self):
        self.n['facts'][0]['status']='UNCONFIRMED'
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_external_context_is_not_personal_evidence(self):
        self.n['facts'][0]['source_id']='COMPANY1'
        self.r['external_context']=[{'source_id':'COMPANY1','kind':'OFFICIAL_COMPANY',
                                    'title':'Company page','url':None,'published_date':None,
                                    'retrieved_date':None,'content':self.n['facts'][0]['quote']}]
        with self.assertRaises(ContractError):validate_normalized(self.r,self.n)

    def test_requirement_quote_must_exist(self):
        self.n['requirements'][0]['quote']='Mandatory HIPAA experience'
        with self.assertRaises(ContractError):validate_normalized(self.r,self.n)

    def test_complete_term_accounting(self):
        self.d['term_decisions'].pop()
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_included_term_needs_evidence(self):
        self.d['term_decisions'][3].update(decision='INCLUDED',claim_ids=[])
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_missing_bullet_audit(self):
        self.a['bullet_audit'].pop()
        with self.assertRaises(ContractError):validate_assessment(self.r,self.n,self.d,self.a)

    def test_duplicate_bullet_audit(self):
        self.a['bullet_audit'].append(copy.deepcopy(self.a['bullet_audit'][0]))
        with self.assertRaises(ContractError):validate_assessment(self.r,self.n,self.d,self.a)

    def test_fake_layout_claim(self):
        self.a.update(layout_status='VERIFIED',page_count=2)
        with self.assertRaises(ContractError):validate_assessment(self.r,self.n,self.d,self.a)

    def test_assessment_matches_revision(self):
        self.a['revision_id']='old-draft'
        with self.assertRaises(ContractError):validate_assessment(self.r,self.n,self.d,self.a)

    def test_wrong_employer_evidence(self):
        self.d['claims'][0]['fact_ids']=['F3']
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_missing_illustrative_notice(self):
        self.r['options']['mode']='ILLUSTRATIVE'
        self.d.update(mode='ILLUSTRATIVE',status='ILLUSTRATIVE')
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_illustrative_keep_stays_illustrative(self):
        self.r['options']['mode']='ILLUSTRATIVE'
        self.d.update(mode='ILLUSTRATIVE',status='ILLUSTRATIVE',notices=['Illustrative resume draft'])
        self.d['claims'][0].update(status='ILLUSTRATIVE',fact_ids=[])
        self.a['verdict']='ILLUSTRATIVE'
        with self.assertRaises(ContractError):validate_assessment(self.r,self.n,self.d,self.a)
        self.a['bullet_audit'][0]['evidence_status']='ILLUSTRATIVE'
        validate_assessment(self.r,self.n,self.d,self.a)

    def test_hypothetical_metric_notice(self):
        self.r['options'].update(mode='ILLUSTRATIVE',metric_policy='LABELED_HYPOTHETICAL',hypothetical_metrics_authorized=True)
        self.d.update(mode='ILLUSTRATIVE',status='ILLUSTRATIVE',notices=['Illustrative resume draft'])
        self.d['claims'][1].update(status='ILLUSTRATIVE',fact_ids=[])
        self.d['metrics'][0].update(status='HYPOTHETICAL',fact_ids=[],assumptions=['Fictional example'])
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)
        self.d['notices'].append('Example metrics are hypothetical')
        validate_draft(self.r,self.n,self.d)

    def test_unreferenced_metric_rejected(self):
        self.d['metrics'][0]['bullet_ids']=[]
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_metric_requires_evidence(self):
        self.d['metrics'][0]['fact_ids']=[]
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_skills_not_bold(self):
        self.d['skills'][0]['items'][0]['text']='**Python**'
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_highlight_actual_occurrence(self):
        self.d['summary']['highlights'][0]['occurrence']=2
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_no_numeric_highlight(self):
        self.d['roles'][0]['bullets'][1]['highlights']=[{'phrase':'12','occurrence':1,'kind':'BUSINESS'}]
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_narrow_revision_preserves_other_sections(self):
        self.r['run_type']='REVISION'
        self.r['revision']={'selected_draft':copy.deepcopy(self.d),'scope':'SELECTED',
                            'selected_bullet_ids':['R1-B01'],'selected_sections':[],
                            'feedback':'Tighten one bullet.','assessment_feedback':''}
        self.d['summary']['text']='Changed summary'
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d)

    def test_removed_bullet_needs_lineage(self):
        old=copy.deepcopy(self.d)
        self.d['roles'][1]['bullets']=[]
        with self.assertRaises(ContractError):validate_draft(self.r,self.n,self.d,old)
        self.d['lineage']=[{'original_bullet_id':'R2-B01','disposition':'REMOVED',
                           'target_bullet_ids':[],'reason':'Example test removal'}]
        validate_draft(self.r,self.n,self.d,old)

    def test_ready_cannot_have_blocker(self):
        self.a['findings'][0]['severity']='BLOCKER'
        with self.assertRaises(ContractError):validate_assessment(self.r,self.n,self.d,self.a)

    def test_repair_loop_is_bounded(self):
        a1=copy.deepcopy(self.a);a1['verdict']='REVISE'
        d2=copy.deepcopy(self.d);d2['revision_id']='draft-002'
        a2=copy.deepcopy(a1);a2['revision_id']='draft-002'
        outputs=iter([self.n,self.d,a1,d2,a2])
        result=run_pipeline(self.r,lambda **kwargs: copy.deepcopy(next(outputs)))
        self.assertEqual(len(result['history']),5)
        self.assertEqual(result['delivery_status'],'REVIEW_NEEDED')

    def test_full_catalog_or_explicit_categories(self):
        prepared=prepare_stage('draft',self.r,self.n)
        self.assertEqual(len(prepared['input_data']['keyword_reference']['categories']),22)
        self.r['options']['keyword_category_ids']=['K01','K15']
        prepared=prepare_stage('draft',self.r,self.n)
        self.assertEqual(len(prepared['input_data']['keyword_reference']['categories']),2)
        self.assertEqual(prepared['input_data']['request']['jd'],self.r['jd'])

    def test_render_keeps_original_identity(self):
        text=render_markdown(self.r,self.d)
        self.assertIn('Senior Software Engineer',text)
        self.assertNotIn('Staff AI Engineer',text)
        skills=text.split('## Technical Skills')[1].split('## Professional Experience')[0]
        self.assertNotIn('**',skills)

    def test_sparse_profile_stays_sparse(self):
        self.r['sources']=[]
        self.n.update(facts=[],inventory=[])
        self.d.update(status='INCOMPLETE_EVIDENCE',summary={'text':'','claim_ids':[],'highlights':[]},skills=[],claims=[],metrics=[])
        for role in self.d['roles']:role['bullets']=[]
        for cov in self.d['requirement_coverage']:
            cov.update(status='MISSING',extent='NONE',fact_ids=[],bullet_ids=[],placement='OMITTED')
        for term in self.d['term_decisions']:term.update(decision='GAP',claim_ids=[])
        validate_normalized(self.r,self.n)
        validate_draft(self.r,self.n,self.d)


if __name__=='__main__':
    unittest.main()
