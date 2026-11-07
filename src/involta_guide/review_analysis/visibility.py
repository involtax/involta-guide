"""Public feeds show new/ok feedback; categorization does not apply suggestions.

The outer review row must have alias r. Manual classification wins over late AI.
"""

PUBLIC_REVIEW = """r.publication_status=1 AND r.status IN ('received','transcribed')
AND NOT EXISTS (
 SELECT 1 FROM review_analyses a LEFT JOIN jobs j ON j.id=a.job_id
 WHERE a.review_id=r.id AND (
  a.manual_category='bugs' OR (
   a.manual_category IS NULL AND a.source_text=r.transcript AND j.status='done'
   AND a.result->>'category' IN ('bugs','other')
  )
 )
)"""
