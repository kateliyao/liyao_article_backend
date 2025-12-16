# 用於Cludflare KV，將帳號和加密過的密碼更新上去
from werkzeug.security import generate_password_hash
import json

# 假設要建立的帳號與密碼對應
users = {
    "sa": "XXXX",
    "LY001": "XXXX"
}

hashed_users = {}
for username, password in users.items():
    hashed_users[username] = generate_password_hash(password)

# 輸出結果
print(json.dumps(hashed_users, indent=2))
