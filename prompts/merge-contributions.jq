# Persist human choices only for unchanged proposals. Changed drafts remain in
# the archived ledger, and newly resolved/concluded proposals lose selection.
$old[0] as $old |
reduce $model[0].contributions[] as $candidate (
  {format_version:1,discussion_hash:$hash,findings_hash:$findings_hash,
   next_id:($old.next_id // 1),proposals:[],main_signature:($old.main_signature // null)};
  ($candidate | .finding_ids |= sort | .discussion_keys |= sort) as $basis |
  ([$old.proposals[]? | select(.basis.finding_ids==$basis.finding_ids and .basis.target_key==$basis.target_key)][0]) as $prior |
  ([$discussion[0].items[] | select(.key as $key | $basis.discussion_keys|index($key))]) as $matched |
  (any($matched[]; .is_resolved==true)) as $resolved |
  ([$discussion[0].items[] | select(.key==$basis.target_key)][0]) as $target |
  (if $basis.classification!="DUPLICATE" and $resolved then "REVISIT" else $basis.classification end) as $class |
  (any($actions[0][]; .status=="published" and any(.finding_ids[]?; . as $id | $basis.finding_ids|index($id)))) as $published |
  ($basis.target_key==null or $target.can_reply==true) as $available |
  (if $prior then $prior.id else "p\(.next_id)" end) as $id |
  .next_id += (if $prior then 0 else 1 end) |
  .proposals += [{id:$id,basis:$basis,classification:$class,published:$published,
    destination:(if $target then {kind:"reply",key:$target.key,root_id:$target.root_id,url:$target.url}
      else {kind:"review",key:null,root_id:null,url:$pr_url} end),available:$available,
    selected:(if $published or ($available|not) or $class=="DUPLICATE" then false
      elif $prior.basis==$basis and $prior.classification==$class then $prior.selected
      else ($class=="NEW" or $class=="ADDITION") end),
    body_md:(if $prior.basis==$basis then $prior.body_md else $basis.body_md end),
    references:[$matched[] | {key,url:(.url // .comments[0].url)}]}])
