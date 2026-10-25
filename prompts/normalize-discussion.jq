# REST has every comment/reply; GraphQL supplies thread identity and resolution.
# The GraphQL comments(first:1) is intentionally ONLY the root, not a truncated
# source of conversation text. Every REST reply must map to one of these roots.
def comment:
  {id, author:.user.login, body, url:.html_url, created_at, updated_at};
def valid_comment:
  (.id | type == "number" and . > 0) and
  ([.user.login,.body,.html_url,.created_at,.updated_at] | all(.[]; type == "string"));
if (all($inline[0][]; valid_comment) and all($issues[0][]; valid_comment) and
    all($reviews[0][]; (.id|type)=="number" and (.user.login|type)=="string" and
      (.body|type)=="string" and (.html_url|type)=="string" and (.state|type)=="string" and
      (.state=="PENDING" or (.submitted_at|type)=="string")) and
    all($threads[0][]; (.id|type)=="string" and (.isResolved|type)=="boolean" and
      (.isOutdated|type)=="boolean" and (.viewerCanReply|type)=="boolean" and
      (.comments.nodes|length)==1 and (.comments.nodes[0].databaseId|type)=="number" and
      (.comments.totalCount|type)=="number")) | not
then error("Incomplete discussion records") else . end |
[$threads[0][] | .comments.nodes[0].databaseId] as $roots |
if ($roots | length != (unique | length)) or
   ([$inline[0][] | select(.in_reply_to_id == null) | .id] | sort) != ($roots | sort) or
   any($inline[0][]; (.in_reply_to_id // .id) as $root | ($roots | index($root)) == null)
then error("Thread roots and complete REST comments do not agree; refresh required") else . end |
if any($threads[0][]; . as $thread | .comments.nodes[0].databaseId as $root |
    ([$inline[0][] | select((.in_reply_to_id // .id)==$root)]|length) != $thread.comments.totalCount)
then error("Thread reply counts disagree; refresh required") else . end |
[
  ($threads[0][] | . as $thread | .comments.nodes[0].databaseId as $root |
    [$inline[0][] | select((.in_reply_to_id // .id) == $root)] | sort_by(.created_at,.id) |
    {key:("thread:"+$thread.id),kind:"thread",root_id:$root,
     url:([.[] | select(.id == $root)][0].html_url),path:$thread.path,line:$thread.line,
     original_line:$thread.originalLine,is_resolved:$thread.isResolved,
     is_outdated:$thread.isOutdated,can_reply:$thread.viewerCanReply,
     can_resolve:$thread.viewerCanResolve,can_unresolve:$thread.viewerCanUnresolve,
     resolved_by:$thread.resolvedBy.login,
     comments:map(comment)}),
  ($issues[0][] | {key:("comment:"+(.id|tostring)),kind:"comment",comments:[comment]}),
  ($reviews[0][] | select(.state != "PENDING") |
    {key:("review:"+(.id|tostring)),kind:"review",state,
     comments:[{id,author:.user.login,body,url:.html_url,created_at:.submitted_at,updated_at:.submitted_at}]})
] | sort_by(.key) | {format_version:1,items:.}
