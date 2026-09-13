# Chạy MeetNote với database

## 1. Tạo môi trường Python

```powershell
conda create -n meetnote
conda activate meetnote
python -m pip install -r requirements.txt
```

## 2. Cấu hình database

1. Trên máy chủ database, chuẩn bị Microsoft SQL Server. Dùng tài khoản quản trị
   có quyền tạo database và bảng cho bước khởi tạo.
2. Kết nối tới SQL Server bằng SSMS, mở file `database.sql` và nhấn **Execute**
   hoặc `F5`. Nếu không dùng tên `MeetNote`, thay tên database ở đầu file
   `database.sql` trước khi chạy.
3. Trên máy chủ chạy ứng dụng, cài Microsoft ODBC Driver 18 for SQL Server.
4. Đặt thông tin kết nối trong cùng terminal sẽ chạy ứng dụng.

PowerShell:

```powershell
$env:SQLSERVER_SERVER="tcp:DB_HOST,1433"
$env:SQLSERVER_DATABASE="MeetNote"
$env:SQLSERVER_DRIVER="ODBC Driver 18 for SQL Server"
$env:SQLSERVER_USERNAME="DB_USERNAME"
$env:SQLSERVER_PASSWORD="DB_PASSWORD"
```

## 3. Thiết lập speaker diarization

1. Chấp nhận quyền truy cập model:
   `pyannote/speaker-diarization-community-1`.
2. Tạo Hugging Face read token.
3. Đặt token trong PowerShell:

```powershell
$env:HF_TOKEN="hf_..."
```

## 4. Khởi động Ollama

Trong một terminal riêng:

```powershell
ollama serve
```

Trong terminal khác:

```powershell
ollama pull qwen3.5:9b
```

## 5. Chạy ứng dụng

```powershell
python app.py
```

Mở trình duyệt tại:

```text
http://localhost:5001
```
