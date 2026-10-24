# New outputs require severity; cached legacy outputs may omit it.
# Pass --argjson require_severity true when validating a new model response.
def location:
  type=="object" and (keys==(["path","side","line","start_line"]|sort)) and
  (.path|type=="string" and length>0) and (.side|IN("LEFT","RIGHT")) and
  ([.line,.start_line]|all(.[]; type=="number" and .>0 and .==floor)) and .start_line<=.line;
type == "object" and
(keys == (["recommendation", "recommendation_rationale", "blocking_findings", "report_md", "review_body_md", "findings"] | sort)) and
(.recommendation | . == "COMMENT" or . == "REQUEST_CHANGES" or . == "APPROVE") and
(.blocking_findings | type == "number" and . >= 0 and . == floor) and
([.recommendation_rationale, .report_md, .review_body_md] | all(.[]; type == "string" and test("\\S"))) and
(.findings | type == "array" and all(.[];
  ((keys - ["locations","primary_location","severity"]) == (["id","title","validity","evidence","blocking","body_md"] | sort)) and
  (if has("severity") then (.severity | IN("Critical","High","Medium","Low"))
   else ($ARGS.named.require_severity != true) end) and
  ((has("primary_location")|not) or .primary_location==null or (.primary_location|location)) and
  ((has("locations")|not) or (.locations|type=="array" and all(.[]; location))) and
  (.id | type == "string" and test("^[A-Za-z0-9_-]+$")) and
  ([.title,.evidence,.body_md] | all(.[]; type == "string" and test("\\S"))) and
  (.validity | IN("CONFIRMED","MODIFIED","NEW","UNVERIFIED","REJECTED")) and
  (.blocking | type == "boolean") and (.validity != "REJECTED" or .blocking == false))) and
([.findings[].id] | length == (unique | length)) and
(.blocking_findings == ([.findings[] | select(.blocking)] | length))
