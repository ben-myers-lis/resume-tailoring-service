"""Offline contract demonstration with synthetic fixtures; no live model call."""
import copy
import json
from app.runner import ROOT, load_json, run_pipeline


def main():
    outputs=iter([load_json(ROOT/'examples'/f'{x}.json') for x in ('normalized','draft','assessment')])
    def fixture_adapter(**kwargs):
        return copy.deepcopy(next(outputs))
    result=run_pipeline(load_json(ROOT/'examples/request.json'),fixture_adapter)
    print(json.dumps({'fixture':'SYNTHETIC — offline, no real candidate or model generation',
                      'delivery_status':result['delivery_status'],
                      'layout_status':result['layout_status'],
                      'stage_count':len(result['history']),
                      'resume_markdown':result['resume_markdown']},indent=2))


if __name__=='__main__':
    main()
