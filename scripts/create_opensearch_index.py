"""Create the OpenSearch index (idempotent), with a friendly preflight check."""
from app.stores.vector_opensearch import create_index, get_client


def main():
    try:
        get_client().info()          # fails fast if OpenSearch isn't reachable
    except Exception:
        raise SystemExit(
            "\n  Cannot reach OpenSearch at localhost:9200.\n"
            "  Start the databases first:   make up\n"
            "  Then wait ~45s for it to boot (check: curl http://localhost:9200) and retry.\n"
            "  If it never comes up, give Docker Desktop >= 6 GB memory and run 'make up' again.\n"
        )
    create_index()


if __name__ == "__main__":
    main()
