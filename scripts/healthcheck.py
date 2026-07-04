"""Verify every dependency is reachable before you run the app."""
from app.config import get_settings
_s = get_settings()


def check(name, fn):
    try:
        fn(); print(f"  ok   {name}")
    except Exception as e:
        print(f"  FAIL {name}: {type(e).__name__}: {e}")


def main():
    print("Health check:")
    check("opensearch", lambda: __import__("app.stores.vector_opensearch",
          fromlist=["get_client"]).get_client().info())

    def dynamo():
        import boto3
        kw = {"region_name": _s.aws_region}
        if _s.dynamodb_endpoint:
            kw["endpoint_url"] = _s.dynamodb_endpoint
        list(boto3.resource("dynamodb", **kw).tables.all())
    check("dynamodb", dynamo)

    def postgres():
        import psycopg
        psycopg.connect(_s.postgres_dsn).close()
    check("postgres", postgres)

    def structured():
        from app.stores.factory import run_structured_select
        run_structured_select("SELECT stay_id, city, name FROM stays LIMIT 1")
    check(f"structured ({_s.structured_backend})", structured)

    def llm():
        from app.llm import chat
        chat([{"role": "user", "content": "reply with: ok"}], max_tokens=5)
    check("llm", llm)


if __name__ == "__main__":
    main()
