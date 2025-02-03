import json
import sys

BLOCKED_USERS_FILE = "blocked_users.json"

def load_json_data(file_path):
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {file_path}: {e}")
        return []

def save_json_data(file_path, data):
    try:
        with open(file_path, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Error saving {file_path}: {e}")

if len(sys.argv) < 2:
    print("Usage: python unblock.py <user_id>")
    sys.exit(1)

try:
    user_id = int(sys.argv[1])
except ValueError:
    print("User ID must be numeric.")
    sys.exit(1)

blocked = load_json_data(BLOCKED_USERS_FILE)
if user_id in blocked:
    blocked.remove(user_id)
    save_json_data(BLOCKED_USERS_FILE, blocked)
    print(f"User {user_id} has been unblocked.")
else:
    print(f"User {user_id} is not in the block list.")
