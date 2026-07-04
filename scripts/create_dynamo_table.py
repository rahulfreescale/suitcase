"""Create the DynamoDB app-state + interactions + corrections tables (idempotent)."""
from app.stores.appstate_dynamo import create_table as create_appstate
from app.stores.interactions import create_table as create_interactions
from app.stores.corrections import create_table as create_corrections
if __name__ == "__main__":
    create_appstate()
    create_interactions()
    create_corrections()
