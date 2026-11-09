"""Rebuild the self-contained conversation file, manifest, and distributable ZIP."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT=Path(__file__).resolve().parents[1]
COMPONENTS=[
    ('CORE POLICY','prompts/00_core.txt'),
    ('CONVERSATION WORKFLOW','CHAT_WORKFLOW.txt'),
    ('CANDIDATE AND JD INPUT TEMPLATE','CHAT_INPUT_TEMPLATE.txt'),
    ('INTEGRATED AI KEYWORD REFERENCE — VOCABULARY, NOT EXPERIENCE','reference/ai_keyword_reference.txt'),
    ('INTEGRATED RESUME ASSESSMENT RUBRIC','reference/assessment_rubric.txt'),
]


def main():
    header='''RESUME TAILORING SERVICE — COMPLETE PORTABLE PROMPTS v1.0.0
Self-contained conversation edition. Assembled November 5, 2026 UTC.

Use this complete file with original candidate information and the current JD.
It includes the core instructions, run/revision/maintenance workflow, input
template, all 22 keyword-reference categories, and the assessment rubric.
No other account, memory, named project, or external prompt is required.

Default: NEW_JD, APPLICATION, EVIDENCED_ONLY, RESUME_AND_REPORT.
The original facts govern claims; the JD governs emphasis; the keyword bank
supplies vocabulary; assessment checks the draft. Never mix these roles.

Read the whole set before drafting. Use the input template. Complete the
supported work without an optional-fact checkpoint. Label hypothetical work.
If a capability such as browsing or PDF rendering is absent, state its limit.
This file cannot supply missing facts or make different models equally capable.

The companion ZIP contains modular application prompts, JSON schemas, a
provider-neutral Python starter, synthetic examples, and regression cases.
That ZIP is optional for conversation use; this file stands on its own.
'''
    sections=[header]
    for title,path in COMPONENTS:
        sections += ['\n'+'='*76+'\n'+title+'\n'+'='*76+'\n', (ROOT/path).read_text(encoding='utf-8')]
    combined=ROOT/'Resume_Tailoring_Complete_Prompts_v1.txt'
    combined.write_text('\n'.join(sections),encoding='utf-8')
    def eligible(p):
        return p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc' and p.name!='manifest.json'
    files=sorted(p for p in ROOT.rglob('*') if eligible(p))
    manifest={'package':'Resume Tailoring Service Portable','version':'1.0.0',
              'files':[{'path':str(p.relative_to(ROOT)), 'bytes':p.stat().st_size,
                        'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]}
    (ROOT/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    destination=ROOT.parent/'Resume_Tailoring_Portable_v1.zip'
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
        for p in files+[ROOT/'manifest.json']:
            z.write(p,arcname=str(Path(ROOT.name)/p.relative_to(ROOT)))
    print(json.dumps({'combined_file':str(combined),'zip_file':str(destination),
                      'files':len(files)+1,'zip_bytes':destination.stat().st_size}))


if __name__=='__main__':
    main()
