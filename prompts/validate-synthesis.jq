# Mirrors the deliberately small synthesis.schema.json; no schema engine needed.
type == "object" and
(keys == (["recommendation", "recommendation_rationale", "blocking_findings", "report_md", "review_body_md", "findings"] | sort)) and
(.recommendation | . == "COMMENT" or . == "REQUEST_CHANGES" or . == "APPROVE") and
(.blocking_findings | type == "number" and . >= 0 and . == floor) and
([.recommendation_rationale, .report_md, .review_body_md] | all(.[]; type == "string" and test("\\S"))) and
(.findings | type == "array" and all(.[];
  keys == (["id","title","validity","evidence","blocking","body_md"] | sort) and
  (.id | type == "string" and test("^[A-Za-z0-9_-]+$")) and
  ([.title,.evidence,.body_md] | all(.[]; type == "string" and test("\\S"))) and
  (.validity | IN("CONFIRMED","MODIFIED","NEW","UNVERIFIED","REJECTED")) and
  (.blocking | type == "boolean") and (.validity != "REJECTED" or .blocking == false))) and
([.findings[].id] | length == (unique | length)) and
(.blocking_findings == ([.findings[] | select(.blocking)] | length))
