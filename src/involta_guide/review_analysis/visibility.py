"""Visibility for public feeds and the visitor's location feed.

The outer review row must have alias r. Manual classification wins over late AI.
"""


def review_visibility(*, include_own: bool = False) -> str:
    other_hidden = "a.result->>'category' = 'other'"
    if include_own:
        other_hidden += " AND r.visitor IS DISTINCT FROM :visitor"
    own = "r.visitor = :visitor OR " if include_own else ""
    return f"""r.publication_status=1 AND r.status IN ('received','transcribed')
AND ({own}EXISTS (
 SELECT 1 FROM review_analyses a LEFT JOIN jobs j ON j.id=a.job_id
 WHERE a.review_id=r.id AND (
  a.manual_category='humor' OR (
   a.manual_category IS NULL AND a.source_text=r.transcript AND j.status='done'
   AND a.result->>'category' IN ('ok','humor')
  )
 )
))
AND NOT EXISTS (
 SELECT 1 FROM review_analyses a LEFT JOIN jobs j ON j.id=a.job_id
 WHERE a.review_id=r.id AND (
  a.manual_category='bugs' OR (
   a.manual_category IS NULL AND a.source_text=r.transcript AND j.status='done'
   AND (a.result->>'category' = 'bugs' OR ({other_hidden}))
  )
 )
)"""


PUBLIC_REVIEW = review_visibility()
VISITOR_REVIEW = review_visibility(include_own=True)
