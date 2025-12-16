import json
import os
import boto3
import mimetypes
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from flask_jwt_extended import JWTManager, create_access_token, jwt_required
from werkzeug.security import check_password_hash
from datetime import timedelta
from botocore.client import Config

# ===========================
# ENV + Flask 初始化
# ===========================
load_dotenv()
app = Flask(__name__)
CORS(app)
print("========== APP START ==========")

IMAGE_FOLDER = "./articles_images"
NEWS_FOLDER = "./articles"
os.makedirs(IMAGE_FOLDER, exist_ok=True)
os.makedirs(NEWS_FOLDER, exist_ok=True)

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
API_KEY = os.getenv("API_KEY")
R2_BUCKET = os.getenv("R2_BUCKET")

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_NAMESPACE_ID = os.getenv("CF_NAMESPACE_ID")
CF_API_TOKEN = os.getenv("CF_API_TOKEN")
KV_BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_NAMESPACE_ID}/values"

app.config["JWT_SECRET_KEY"] = JWT_SECRET_KEY
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=1)  # 一小時過期，需要刷新網頁才能正常使用
jwt = JWTManager(app)

# ===========================
# R2 相關函式
# ===========================
def get_s3_client():
    return boto3.client(
        "s3",
        region_name="auto",
        endpoint_url=f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
        config=Config(signature_version="s3v4"),
    )

def r2_upload_file(local_path, r2_key):
    """上傳單一檔案到 R2"""
    s3 = get_s3_client()
    content_type, _ = mimetypes.guess_type(local_path)
    content_type = content_type or "application/octet-stream"
    with open(local_path, "rb") as f:
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=r2_key,
            Body=f,
            ContentType=content_type,
        )

def r2_download_articles_json():
    """嘗試從 R2 下載 articles.json，不存在回傳空 array"""
    s3 = get_s3_client()
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key="articles.json")
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception as e:
        if "NoSuchKey" in str(e):
            return []
        print("讀取 R2 articles.json 失敗:", e)
        return []

def r2_upload_articles_json(content_list):
    """上傳主檔 articles.json"""
    s3 = get_s3_client()
    s3.put_object(
        Bucket=R2_BUCKET,
        Key="articles.json",
        Body=json.dumps(content_list, ensure_ascii=False, indent=2),
        ContentType="application/json",
    )

# ===========================
# KV 相關函式
# ===========================
def kv_get_user(username):
    """從 Cloudflare KV 讀取帳號資料"""
    url = f"{KV_BASE_URL}/user:{username}"
    response = requests.get(url, headers={"Authorization": f"Bearer {CF_API_TOKEN}"})
    if response.status_code == 404:
        return None
    return json.loads(response.text)

# ===========================
# 登入 API
# ===========================
@app.route("/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username")
    password = data.get("password")

    user = kv_get_user(username)
    if user and check_password_hash(user["password_hash"], password):
        return jsonify({"token": create_access_token(identity=username)})
    else:
        return jsonify({"error": "invalid credentials"}), 401

# ===========================
# SAVE API（上傳文章 + 圖片 + 主檔）
# ===========================
@app.route("/save", methods=["POST"])
@jwt_required()
def save():
    print("JWT ok, checking API Key...", request.headers.get("X-API-KEY"))
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Invalid API Key"}), 401

    try:
        raw_data = request.form.get("data")
        data = json.loads(raw_data)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

        # (1) 儲存圖片 + 上傳 R2
        if "image" in request.files:
            image = request.files["image"]
            img_filename = f"{timestamp}_{image.filename}"
            local_img_path = os.path.join(IMAGE_FOLDER, img_filename)
            image.save(local_img_path)
            data["image"] = f"articles_images/{img_filename}"
            r2_upload_file(local_img_path, data["image"])
        else:
            data["image"] = None

        # (2) 儲存個別文章 JSON + 上傳 R2
        post_filename = f"news_{timestamp}.json"
        local_post_path = os.path.join(NEWS_FOLDER, post_filename)
        with open(local_post_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        r2_upload_file(local_post_path, f"articles/{post_filename}")

        # (3) 更新 articles.json
        articles_list = r2_download_articles_json()
        new_entry = {
            "filename": post_filename,
            "title": data.get("title"),
            "subtitle": data.get("subtitle"),
            "content": data.get("content"),
            "date": data.get("date"),
            "image": data.get("image"),
        }
        articles_list.insert(0, new_entry)
        r2_upload_articles_json(articles_list)

        return jsonify({"success": True})
    except Exception as e:
        print("ERROR:", e)
        return jsonify({"success": False, "error": str(e)})

# ===========================
# MAIN（本地測試）
# ===========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print("Starting server on port:", port)
    app.run(host="0.0.0.0", port=port)
