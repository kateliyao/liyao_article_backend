# 使用官方 Python 影像
FROM python:3.11-slim

# 設定工作目錄
WORKDIR /app

# 複製 requirements.txt 並安裝依賴
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 複製程式碼
COPY . .

# 設定環境變數
ENV PORT=8080

# 對外開放端口
EXPOSE 8080

# 執行 Flask
CMD ["python", "app.py"]

