"""Reproduce the narrow receipted-self-publication finding on a frozen checkout.
Adapted from docs/evaluations/pr4/reproductions.json#check_c_remaining.py.
No live GitHub/provider calls; use an absolute checkout path argument.
"""
import copy, json, pathlib, sys
from unittest.mock import patch
source=pathlib.Path(sys.argv[1]).resolve()
sys.path[:0]=[str(source/'tests'),str(source/'lib')]
from test_owner import OwnerTest
from pr_review.common import ApiError
results=[]
t=OwnerTest();t.setUp()
try:
 batch=t.fix()['batches'][-1]
 for i in range(2):t.request('owner-mutate',operation='reply',item='thread:T1',body='Reply '+str(i))
 ids=[r['id'] for r in t.view['replies']];t.request('owner-mutate',operation='stage',replies=ids);preview=t.request('owner-prepare')
 def posting(*args,**kwargs):
  receipt=t.publish(*args,**kwargs);comment=copy.deepcopy(t.feedback[0]['comments'][0]);comment.update(id=receipt['id'],author='author',body=receipt['body']);t.feedback[0]['comments'].append(comment);return receipt
 with patch('pr_review.owner_publication.gh',side_effect=posting):
  try:t.request('owner-publish',token=preview['token'])
  except ApiError:pass
 view=t.request('owner-read');assert len(t.posts)==1
 assert sum(r['status']=='published' for r in view['replies'])==1
 assert sum(r['status']=='unstaged' for r in view['replies'])==1 and view['batches'][-1]['discussion_stale']
 results.append(dict(finding='F4',verdict='confirmed',evidence='Real owner publication of two replies to one thread sends only first. Fixture reflects its receipt in discussion; next refresh unstages second and marks verified batch stale.'))
finally:t.doCleanups()

print(json.dumps(results,indent=2))
