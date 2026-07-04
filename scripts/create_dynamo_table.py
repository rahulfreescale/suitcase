"""Create the DynamoDB app-state + interactions tables (idempotent)."""
from app.stores.appstate_dynamo import create_table as create_appstate
from app.stores.interactions import create_table as create_interactions
if __name__ == "__main__":
    create_appstate()
    create_interactions()
