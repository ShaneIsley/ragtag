# Search index

[`index.json`](index.json) defines the Azure AI Search index. Create it with `PUT /indexes/policy-desk-chunks?api-version=2026-04-01`; the ingest job does this when the index is missing.

## Fields

Each document in the index is one chunk of a source:
- `content`: the passage text, searchable with the English analyser.
- `content_vector`: its embedding, 1,536 dimensions from Azure OpenAI's text-embedding-3-small.
- `section`: the heading path, used in citations.
- `source_id`, `url`, `publisher` and `licence`: from [`ingest/sources.yaml`](../ingest/sources.yaml).
- `content_hash`: lets the ingest job skip chunks that haven't changed.

The vector is searchable but not stored or returned, which keeps the index small. The `semantic-default` configuration exists only for the reranker experiment; normal queries don't use it.

## Size

380 chunks from the 17 UNSW sources, as indexed on 31 Oct 2026. Raw vectors take about 2.5 MB, which is well inside the Free tier's 50 MB.

## Query

The service embeds the question and sends one hybrid request with the question text and its vector:

```http
POST /indexes/policy-desk-chunks/docs/search?api-version=2026-04-01
Content-Type: application/json

{
  "search": "Who has to approve a high-risk AI system? Who has to approve a new AI system or tool?",
  "searchFields": "title,section,content",
  "searchMode": "any",
  "vectorQueries": [
    { "kind": "vector", "vector": [ /* the question, 1,536 numbers */ ], "fields": "content_vector", "k": 50 },
    { "kind": "vector", "vector": [ /* its broader rewrite */ ], "fields": "content_vector", "k": 50 }
  ],
  "select": "id,source_id,title,section,url,publisher,content",
  "top": 30,
  "debug": "vector"
}
```

The second text and vector come from `prompts/rewrite.v2.yaml`, which turns the question into the broader question it's a case of, so search also finds the general rule. The app keeps the first 12 results, at most 4 from any one source.

Azure AI Search merges the keyword and vector rankings with Reciprocal Rank Fusion, the same method an earlier offline prototype used in SQL. `searchMode: any` matches any word in the question. That is the same fix the prototype needed when its first keyword query required every word and found nothing.

`debug: vector` adds each result's cosine similarity for each vector query (`vectorSimilarity`). The app uses the first, the question's own, so a rewrite can't lift an off-topic question over the threshold. The fused `@search.score` only ranks, so the app declines on the best similarity instead (see `MIN_SIMILARITY` in [`app/answer.py`](../app/answer.py)). A passage that only the keyword search found has no similarity.
