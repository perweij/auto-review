"""External behavioral oracle: run in a frozen candidate; no live providers/GitHub."""
import json
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(sys.argv[1]).resolve()/'lib'))
from pr_review import settings

results=[]
def check(name, fn):
    try:
        fn()
        results.append({'case':name,'passed':True})
    except Exception as exc:
        results.append({'case':name,'passed':False,'error':str(exc) or type(exc).__name__})

def expect(actual,expected):
    assert actual==expected,(actual,expected)

def explicit_default(provider, canonical=False):
    other='codex' if provider=='claude' else 'claude'
    defaults=({'processing':{'provider':other,'model':'chosen','effort':'high'}} if canonical else {'processing_provider':other})
    result=settings.resolve({'reviewers':[provider],'defaults':defaults})['settings']
    expect(result['processing']['provider'],other)
    if canonical:expect(result['processing']['model'],'chosen')

def saved_legacy(provider):
    other='codex' if provider=='claude' else 'claude'
    defaults=settings.validate({'reviewers':[other],'processing_provider':other})
    manifest={'configuration':{'reviewers':[provider],provider+'_model':'saved'}}
    session=SimpleNamespace(pointer=lambda _:None,path=Path('/not-used'))
    with patch.object(settings,'Session',return_value=session),patch.object(settings,'read_json',return_value=manifest):
        result=settings.resolve({'session':'/not-used','defaults':defaults})['settings']
    expect(result['reviewers'],[provider]);expect(result['reviewer_slots'][0]['model'],'saved')
    expect(result['processing']['provider'],provider)

for provider in ('claude','codex'):
    check('explicit-default-'+provider,lambda p=provider:explicit_default(p))
    check('canonical-default-'+provider,lambda p=provider:explicit_default(p,True))
    check('legacy-saved-'+provider,lambda p=provider:saved_legacy(p))
    check('legacy-implicit-'+provider,lambda p=provider:expect(settings.validate(settings.from_manifest({'configuration':{'reviewers':[p]}}))['processing']['provider'],p))
    other='codex' if provider=='claude' else 'claude'
    check('explicit-request-'+provider,lambda p=provider,o=other:expect(settings.resolve({'reviewers':[p],'defaults':{'processing_provider':p},'processing_provider':o})['settings']['processing']['provider'],o))
    check('canonical-roundtrip-'+provider,lambda p=provider,o=other:expect(settings.validate(settings.from_manifest({'configuration':settings.validate({'reviewers':[p],'processing_provider':o})}))['processing']['provider'],o))

def first_review_guards():
    config=settings.validate({'reviewers':['codex','claude']})
    expect(config['reviewers'],['claude','codex'])
    from pr_review.reviews import Review
    review=Review.__new__(Review); review.settings=config
    expect(review.dual,True)
    session=SimpleNamespace(pointer=lambda _:None,path=Path('/not-used'))
    with patch.object(settings,'Session',return_value=session),patch.object(settings,'read_json',return_value={'configuration':dict(settings.DEFAULTS)}):
        value=settings.resolve({'session':'/not-used','reviewers':['claude']})['settings']
    expect(value['processing']['provider'],'claude')
check('first-review-compatibility',first_review_guards)

def unchanged_generation():
    from pr_review.reviews import Review
    review=Review.__new__(Review)
    review.manifest={'configuration':dict(settings.DEFAULTS),'phases':{'synthesis':{'status':'completed'}}}
    review.settings=settings.validate(settings.from_manifest(review.manifest))
    review.runtime=SimpleNamespace(permission='default')
    review.tools,review.viewer,review.permission={},None,None
    review.path=Path('/unused')
    review.scope,review.begin,review.save=Mock(),Mock(),Mock()
    with patch('pr_review.reviews.say'): review.configure('review',[])
    review.begin.assert_not_called()
check('first-review-unchanged-generation',unchanged_generation)

def processing_catalog():
    from pr_review import automation,models,reviews,repository,sessions
    config=settings.validate({'reviewers':['claude'],'processing_provider':'codex'})
    catalogs={p:{'provider':p,'models':[{'value':config[p+'_model'],'efforts':[config[p+'_effort']]}]} for p in ('claude','codex')}
    controller=Mock(); controller.operation={'operation_id':'fixture'}
    controller.publish.return_value='published'
    repo=SimpleNamespace(common=Path('/unused'),pr=11,lock=Path('/unused'))
    with patch.object(repository.Repository,'resolve',return_value=repo), \
         patch.object(automation,'atomic_json'),patch.object(automation,'locked',return_value=nullcontext()), \
         patch.object(sessions,'locate',return_value=(None,None)),patch.object(settings,'resolve',return_value={'settings':config}), \
         patch.object(models,'discover',side_effect=lambda r:catalogs[r['provider']]), \
         patch.object(reviews,'execute',return_value='session'):
        expect(automation._execute({'pr':11},controller),'published')
check('first-review-processing-catalog',processing_catalog)
if '--extended' in sys.argv:
    def preset_precedence(provider, canonical):
        defaults=settings.validate({'reviewers':[provider]}) if canonical else {'reviewers':[provider]}
        value=settings.resolve({'defaults':defaults,'economical':True})['settings']
        expect(value['reviewers'],['claude','codex'])
        expect(value['processing']['provider'],'codex')
        expect(value['reviewer_slots'][0]['model'],settings.ECONOMICAL['claude_model'])
        expect(value['reviewer_slots'][1]['model'],settings.ECONOMICAL['codex_model'])
    for provider in ('claude','codex'):
        for canonical in (False,True):
            check('preset-over-defaults-'+provider+('-canonical' if canonical else '-legacy'),
                  lambda p=provider,c=canonical:preset_precedence(p,c))
print(json.dumps(results,indent=2))
raise SystemExit(any(not r['passed'] for r in results))
