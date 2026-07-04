"""OpenSearch vector store: kNN semantic + keyword + metadata filtering.

The same code targets local Docker OpenSearch and Amazon OpenSearch Service;
only auth/SSL differ via config.
"""
from __future__ import annotations
from opensearchpy import OpenSearch, RequestsHttpConnection
from app.config import get_settings

_s = get_settings()


def get_client() -> OpenSearch:
    if _s.opensearch_use_aws_auth:
        import boto3
        from requests_aws4auth import AWS4Auth
        cred = boto3.Session().get_credentials()
        auth = AWS4Auth(cred.access_key, cred.secret_key, _s.aws_region, "es",
                        session_token=cred.token)
        return OpenSearch(
            hosts=[{"host": _s.opensearch_host, "port": _s.opensearch_port}],
            http_auth=auth, use_ssl=True, verify_certs=True,
            connection_class=RequestsHttpConnection,
        )
    return OpenSearch(
        hosts=[{"host": _s.opensearch_host, "port": _s.opensearch_port}],
        http_auth=(_s.opensearch_user, _s.opensearch_password),
        use_ssl=_s.opensearch_use_ssl, verify_certs=False,
    )


INDEX_BODY = {
    "settings": {"index": {"knn": True}},
    "mappings": {"properties": {
        "embedding": {"type": "knn_vector", "dimension": _s.embed_dim,
                      "method": {"name": "hnsw", "engine": "lucene",
                                 "space_type": "cosinesimil"}},
        "text": {"type": "text"},
        "city": {"type": "keyword"},
        "country": {"type": "keyword"},
        "region": {"type": "keyword"},
        "section": {"type": "keyword"},
        "page": {"type": "integer"},
        # Source isolation: shared KB chunks have shared=true; per-user uploaded
        # docs carry the owner's user_id so retrieval can scope to "shared OR mine".
        "shared": {"type": "boolean"},
        "user_id": {"type": "keyword"},
        "source": {"type": "keyword"},      # e.g. "guide" | "user_upload"
        "doc_name": {"type": "keyword"},    # original filename for uploads
    }},
}


def create_index() -> None:
    c = get_client()
    if c.indices.exists(_s.opensearch_index):
        print(f"index '{_s.opensearch_index}' already exists")
        return
    c.indices.create(_s.opensearch_index, body=INDEX_BODY)
    print(f"created index '{_s.opensearch_index}'")


def index_chunks(chunks: list[dict]) -> None:
    """chunks: [{embedding, text, city, country, region, section, page, shared, ...}]"""
    from opensearchpy.helpers import bulk
    c = get_client()
    actions = [{"_index": _s.opensearch_index, "_source": ch} for ch in chunks]
    bulk(c, actions)
    c.indices.refresh(_s.opensearch_index)


def _filter_clause(meta_filter: dict | None) -> list[dict]:
    """meta_filter like {'city': 'Lisbon'} -> OpenSearch term filters."""
    if not meta_filter:
        return []
    return [{"term": {k: v}} for k, v in meta_filter.items() if v]


def semantic_search(vector: list[float], k: int, meta_filter: dict | None = None):
    body = {"size": k, "query": {"bool": {
        "filter": _filter_clause(meta_filter),
        "must": [{"knn": {"embedding": {"vector": vector, "k": k}}}],
    }}}
    hits = get_client().search(index=_s.opensearch_index, body=body)["hits"]["hits"]
    return [{"id": h["_id"], "score": h["_score"], **h["_source"]} for h in hits]


def keyword_search(keywords: list[str], k: int, meta_filter: dict | None = None):
    query = " ".join(keywords)
    body = {"size": k, "query": {"bool": {
        "filter": _filter_clause(meta_filter),
        "must": [{"multi_match": {"query": query, "fields": ["text^2", "section"]}}],
    }}}
    hits = get_client().search(index=_s.opensearch_index, body=body)["hits"]["hits"]
    return [{"id": h["_id"], "score": h["_score"], **h["_source"]} for h in hits]
