def compact($limit): gsub("\\s+"; " ") | if length>$limit then .[:$limit]+"…" else . end;
def location:
  if .path then .path + (if (.line // .original_line) then ":"+((.line // .original_line)|tostring) else "" end)
  else "existing discussion" end;
([.proposals[].classification] | group_by(.) | map("\(length) \(.[0])") | join(" · ")),
(.proposals[] | select(.classification!="DUPLICATE") | . as $proposal |
  ([$findings[0].findings[] | select(.id as $id | $proposal.basis.finding_ids|index($id)) | .title] | join("; ") | compact(160)) as $title |
  ([$discussion[0].items[] | select(.key==$proposal.destination.key)][0]) as $thread |
  "[\(if .published then "posted" elif .selected then "x" else " " end)] \(.id) \(.classification): \($title)",
  (if .destination.kind=="reply" then
      "    Reply to \(if $thread.is_resolved then "resolved thread" elif .classification=="REVISIT" then "earlier thread (revisit)" else "existing thread" end) · \($thread|location)" +
      (if $thread.is_outdated then " (outdated location)" else "" end),
      "    Thread: @\($thread.comments[0].author): \($thread.comments[0].body|compact(110))"
   elif (.references|length)>0 then
      "    Main review item with links to earlier discussion",
      (.references[] | "    Related discussion → \(.url)")
   else "    New item in main review" end),
  "    → \(.destination.url)\(if .available then "" else " (reply unavailable)" end)",
  (if .classification=="UNCERTAIN" then "    Uncertain match or evidence; inspect before selecting."
   elif .classification=="REVISIT" then "    Follow-up to a concluded discussion; inspect before selecting."
   else empty end))
