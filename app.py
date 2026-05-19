import json
import os
import boto3
import mimetypes
import requests
import io
from PIL import Image
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from flask_jwt_extended import JWTManager, create_access_token, jwt_required
from werkzeug.security import check_password_hash
from datetime import timedelta
from botocore.client import Config
from flask import send_from_directory

# ===========================
# ENV + Flask 初始化
# ===========================
load_dotenv()
app = Flask(__name__)
CORS(app)

IMAGE_FOLDER = "./articles_images"
NEWS_FOLDER = "./articles"

AD_IMAGE_FOLDER = "./ads_images"
AD_DATA_FOLDER = "./ads"

# 建立目錄
for folder in [IMAGE_FOLDER, NEWS_FOLDER, AD_IMAGE_FOLDER, AD_DATA_FOLDER]:
    os.makedirs(folder, exist_ok=True)

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

# 修正 CORS 設定：明確允許 DELETE 方法與相關 Headers
CORS(app, resources={r"/*": {
    "origins": "*",
    "methods": ["GET", "POST", "DELETE", "OPTIONS"],
    "allow_headers": ["Content-Type", "Authorization", "X-API-KEY"]
}})


# ===========================
# R2 共用工具函式 (核心重構)
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


def r2_upload_file(local_path, r2_key, content_type=None):
    """通用上傳函式"""
    s3 = get_s3_client()
    if not content_type:
        content_type, _ = mimetypes.guess_type(local_path)
    content_type = content_type or "application/octet-stream"
    with open(local_path, "rb") as f:
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=r2_key,
            Body=f,
            ContentType=content_type,
        )


def r2_get_json(json_key):
    """通用 JSON 讀取 (支援 articles.json 或 ads.json)"""
    s3 = get_s3_client()
    try:
        obj = s3.get_object(Bucket=R2_BUCKET, Key=json_key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception as e:
        if "NoSuchKey" in str(e): return []
        return []


def r2_set_json(json_key, data_list):
    """通用 JSON 寫入"""
    s3 = get_s3_client()
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=json_key,
        Body=json.dumps(data_list, ensure_ascii=False, indent=2),
        ContentType="application/json",
    )

# ===========================
# 檢查原圖寬度
# ===========================
def process_and_resize_image(image_file, max_width=800):
    """
    讀取圖片、等比例縮放到指定寬度、並轉換為 RGB (準備存 WebP)
    """
    img = Image.open(image_file)
    orig_w, orig_h = img.size

    # 如果寬度超過設定值，才進行縮放
    if orig_w > max_width:
        ratio = max_width / float(orig_w)
        new_h = int(float(orig_h) * float(ratio))
        # 使用 LANCZOS 獲得最佳縮放品質
        img = img.resize((max_width, new_h), Image.Resampling.LANCZOS)

    # WebP 存檔前建議統一轉為 RGB
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    return img

# ===========================
# KV 相關 (不變)
# ===========================
def kv_get_user(username):
    url = f"{KV_BASE_URL}/user:{username}"
    response = requests.get(url, headers={"Authorization": f"Bearer {CF_API_TOKEN}"})
    if response.status_code == 404: return None
    return json.loads(response.text)


# ===========================
# 登入 API (不變)
# ===========================
@app.route("/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username")
    password = data.get("password")
    user = kv_get_user(username)
    if user and check_password_hash(user["password_hash"], password):
        return jsonify({"token": create_access_token(identity=username)})
    return jsonify({"error": "invalid credentials"}), 401


# ===========================
# 文章 SAVE API (維持原欄位)
# ===========================
@app.route("/save", methods=["POST", "OPTIONS"])
@jwt_required()
def save():
    if request.method == "OPTIONS": return jsonify({"success": True}), 200
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Invalid API Key"}), 401

    try:
        raw_data = request.form.get("data")
        data = json.loads(raw_data)

        # 💡 [第一步] 這裡利用前端傳來的原始 data 做編輯判定（完全沒被破壞）
        existing_filename = data.get("filename")
        if existing_filename:
            post_filename = existing_filename  # 編輯模式：沿用舊檔名
        else:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            post_filename = f"news_{timestamp}.json"  # 新增模式：產生新檔名

        # (1) 圖片處理：轉 WebP + 寬度 800px
        if "image" in request.files:
            image_file = request.files["image"]
            img = process_and_resize_image(image_file, max_width=800)

            img_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            webp_filename = f"{img_timestamp}_article.webp"
            local_webp_path = os.path.join(IMAGE_FOLDER, webp_filename)
            img.save(local_webp_path, "webp", quality=80)

            data["image"] = f"articles_images/{webp_filename}"
            r2_upload_file(local_webp_path, data["image"], content_type="image/webp")
        else:
            if existing_filename:
                old_list = r2_get_json("articles.json")
                old_entry = next((a for a in old_list if a["filename"] == existing_filename), None)
                if old_entry:
                    data["image"] = old_entry.get("image")

        # 💡 [第二步] 在判定結束後、準備寫入檔案前，強行把正確的檔名塞回 data 中！
        # 這樣如果是新文章（原本 filename 為 null），此處就會被更正為新產生的檔名。
        # 如果是編輯文章，此處就是把舊檔名再次確認填入，完全不衝突。
        data["filename"] = post_filename

        # (2) 儲存個別文章 JSON
        local_post_path = os.path.join(NEWS_FOLDER, post_filename)
        with open(local_post_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        r2_upload_file(local_post_path, f"articles/{post_filename}")

        # (3) 更新 articles.json (索引檔)
        articles_list = r2_get_json("articles.json")
        new_entry = {
            "filename": post_filename,
            "title": data.get("title"),
            "subtitle": data.get("subtitle"),
            "content": data.get("content"),
            "date": data.get("date"),
            "image": data.get("image"),
            "linkText": data.get("linkText"),
            "linkUrl": data.get("linkUrl"),
            "keywords": data.get("keywords"),
            "extraSections": data.get("extraSections"),
            "deployDomains": data.get("deployDomains")
        }

        if existing_filename:
            articles_list = [a for a in articles_list if a["filename"] != existing_filename]

        articles_list.insert(0, new_entry)
        r2_set_json("articles.json", articles_list)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ===========================
# 廣告 SAVE API (新增：含 WebP 壓縮)
# ===========================
@app.route("/save_ad", methods=["POST", "OPTIONS"])
@jwt_required()
def save_ad():
    if request.method == "OPTIONS": return jsonify({"success": True}), 200
    if request.headers.get("X-API-KEY") != API_KEY:
        return jsonify({"error": "Invalid API Key"}), 401

    try:
        raw_data = request.form.get("data")
        data = json.loads(raw_data)

        # 💡 關鍵 1：判斷是「新增」還是「編輯」
        existing_filename = data.get("filename")  # 前端傳來的 editFilename

        if existing_filename:
            # 編輯模式：延用舊檔名
            ad_filename = existing_filename
        else:
            # 新增模式：產生新檔名
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            ad_filename = f"ad_{timestamp}.json"

        # (1) 處理圖片
        if "image" in request.files:
            # 使用者有上傳新圖片
            image_file = request.files["image"]
            img = process_and_resize_image(image_file, max_width=800)

            # 為了避免快取問題，即便是編輯，圖片檔名可以加個小標記或維持原樣
            # 這裡建議使用時間戳記確保圖片唯一
            img_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            webp_filename = f"{img_timestamp}_ad.webp"
            local_webp_path = os.path.join(AD_IMAGE_FOLDER, webp_filename)
            img.save(local_webp_path, "webp", quality=80)

            data["image"] = f"ads_images/{webp_filename}"
            r2_upload_file(local_webp_path, data["image"], content_type="image/webp")
        else:
            # 💡 關鍵 2：編輯模式下若沒傳新圖，需保留 R2 上的舊圖路徑
            if existing_filename:
                old_ads = r2_get_json("ads.json")
                old_entry = next((a for a in old_ads if a["filename"] == existing_filename), None)
                if old_entry:
                    data["image"] = old_entry.get("image")

        # 💡 核心修正：在寫入子層檔案前，強行把本次最終確定的 ad_filename 塞進 data 物件中！
        # 這樣不管是新廣告（原本為 null）還是編輯廣告，子層 JSON 內的 filename 就絕對會有值。
        data["filename"] = ad_filename

        # (2) 儲存/覆蓋個別廣告 JSON
        local_ad_path = os.path.join(AD_DATA_FOLDER, ad_filename)
        with open(local_ad_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        r2_upload_file(local_ad_path, f"ads/{ad_filename}")

        # (3) 更新 ads.json (索引檔)
        ads_list = r2_get_json("ads.json")

        new_entry = {
            "filename": ad_filename,
            "title": data.get("title"),
            "intro": data.get("intro"),
            "link": data.get("link"),
            "date": data.get("date"),
            "keywords": data.get("keywords"),
            "image": data.get("image"),
        }

        # 💡 關鍵 3：如果是編輯，先刪除舊的再插入新的，或直接替換
        if existing_filename:
            # 濾掉舊的，把新的塞在最前面 (或維持原位)
            ads_list = [a for a in ads_list if a["filename"] != existing_filename]

        ads_list.insert(0, new_entry)
        r2_set_json("ads.json", ads_list)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# ===========================
# DELETE API (文章與廣告共用邏輯)
# ===========================
def generic_delete(json_key, filename, folder_prefix):
    """通用刪除邏輯內部函式"""
    try:
        data_list = r2_get_json(json_key)
        target = next((a for a in data_list if a["filename"] == filename), None)
        if not target: return False, "找不到紀錄"

        # 更新索引
        new_list = [a for a in data_list if a["filename"] != filename]
        r2_set_json(json_key, new_list)

        # 刪除 R2 檔案
        s3 = get_s3_client()
        s3.delete_object(Bucket=R2_BUCKET, Key=f"{folder_prefix}/{filename}")
        if target.get("image"):
            s3.delete_object(Bucket=R2_BUCKET, Key=target["image"])
        return True, None
    except Exception as e:
        return False, str(e)


@app.route("/delete", methods=["DELETE", "OPTIONS"])
@jwt_required()
def delete_article():
    if request.method == "OPTIONS": return jsonify({"success": True}), 200
    if request.headers.get("X-API-KEY") != API_KEY: return jsonify({"error": "Invalid API Key"}), 401

    filename = request.args.get("filename")
    success, error = generic_delete("articles.json", filename, "articles")
    return jsonify({"success": success, "error": error})


@app.route("/delete_ad", methods=["DELETE", "OPTIONS"])
@jwt_required()
def delete_ad():
    if request.method == "OPTIONS": return jsonify({"success": True}), 200
    if request.headers.get("X-API-KEY") != API_KEY: return jsonify({"error": "Invalid API Key"}), 401

    filename = request.args.get("filename")
    success, error = generic_delete("ads.json", filename, "ads")
    return jsonify({"success": success, "error": error})


@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-API-KEY')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS')
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
