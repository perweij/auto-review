def text: type=="string" and test("\\S");
($findings[0].findings | map(select(.validity!="REJECTED"))) as $eligible |
[$eligible[].id] as $ids |
[$discussion[0].items[].key] as $keys |
type=="object" and keys==["contributions","validity_conflicts"] and
(.contributions|type)=="array" and (.validity_conflicts|type)=="array" and
([.contributions[].finding_ids[]] | sort) == ($ids|sort) and
all(.contributions[];
  . as $c |
  keys==(["finding_ids","classification","discussion_keys","target_key","explanation","delta","body_md"]|sort) and
  (.finding_ids|type=="array" and length>0 and all(.[];type=="string")) and
  (.classification|IN("NEW","ADDITION","REVISIT","DUPLICATE","UNCERTAIN")) and
  (.discussion_keys|type=="array" and length==(unique|length) and all(.[]; . as $key | $keys|index($key))) and
  (.explanation|text) and (.delta|type)=="string" and (.body_md|type)=="string" and
  (if .classification=="DUPLICATE" then .delta=="" and .body_md=="" else (.body_md|text) end) and
  (if .classification=="NEW" then .target_key==null and (.discussion_keys|length)==0 else true end) and
  (if (.classification|IN("ADDITION","REVISIT","DUPLICATE")) then (.discussion_keys|length)>0 else true end) and
  (if (.classification|IN("ADDITION","REVISIT")) then (.delta|text) else true end) and
  (if .target_key==null then
     (if (.classification|IN("ADDITION","REVISIT")) then all(.discussion_keys[]; startswith("thread:")|not) else true end)
   else (.target_key|type)=="string" and (.discussion_keys|index($c.target_key))!=null and
     any($discussion[0].items[]; .key==$c.target_key and .kind=="thread") end) and
  (if any($eligible[]; .validity=="UNVERIFIED" and (.id as $id | $c.finding_ids|index($id)))
   then .classification=="UNCERTAIN" else true end)) and
all(.validity_conflicts[];
  keys==["evidence_keys","finding_id","reason"] and
  (.finding_id as $id | $ids|index($id))!=null and (.reason|text) and
  (.evidence_keys|type=="array" and length>0 and all(.[];. as $key|$keys|index($key))))
