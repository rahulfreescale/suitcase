"""Test the planning conversation agent over a multi-turn dialogue.
Run from repo root with venv active:  python test_conversation.py

Simulates a user talking to the planning host and shows, each turn, whether the
agent ASKS or PLANS, and the contract it has accumulated. No UI needed.
"""
from app.agents.plan_conversation import converse, build_request_from_contract

# a scripted user, to see how the agent gathers info conversationally
USER_TURNS = [
    "plan a trip to Rome with a wheelchair",   # missing days -> should ASK
    "5 days",                                   # has req -> may ask 1 more or plan
    "probably December, and I hate the cold",   # rich preference -> should be near plan
    "vegetarian, mid-range budget",             # plenty now -> should PLAN
]

def main():
    contract = None
    asked = 0
    sid = "test-convo-session"
    print("=== PLANNING CONVERSATION SIMULATION ===\n")
    for i, msg in enumerate(USER_TURNS, 1):
        print(f"[turn {i}] USER: {msg}")
        r = converse(msg, contract, session_id=sid, user_id="test-user", asked=asked)
        contract = r["contract"]
        asked = r["asked"]
        print(f"          ACTION: {r['action'].upper()}  (questions asked: {asked})")
        print(f"          HOST:   {r['message']}")
        print(f"          contract: destination={contract.get('destination')}, "
              f"days={contract.get('trip_length_days')}, "
              f"travelers={contract.get('travelers')}, "
              f"prefs={contract.get('preferences')}")
        if r["action"] == "plan":
            req = build_request_from_contract(contract)
            print(f"\n=== READY TO PLAN. Request string handed to pipeline: ===")
            print(f"    \"{req}\"")
            break
        print()

if __name__ == "__main__":
    main()
