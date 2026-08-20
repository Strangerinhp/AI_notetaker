# MeetNote

Ứng dụng Flask tạo transcript, biên bản và báo cáo Word từ một hoặc
nhiều file audio. Mặc định lịch sử cuộc họp được lưu trong Microsoft SQL Server;
có thể dùng `--no-database` cho demo tạm thời không cần database.

## Chạy không có database

```powershell
python app.py --no-database
```

Chế độ này không kiểm tra, đọc hoặc ghi SQL Server. Sidebar và transcript editor
chỉ dùng dữ liệu trong RAM và mất khi tiến trình Flask dừng. Báo cáo DOCX được
ghi thành file riêng trong thư mục `word_documents/` (hoặc thư mục đặt bởi
`WORD_STORAGE_FOLDER`), không được giữ dưới dạng blob trong RAM. Giao diện upload
Word giống chế độ có database: file mới được lưu trên filesystem và viewer tải
lại ngay. Vì metadata cuộc họp không có database vẫn chỉ nằm trong RAM, sau khi
khởi động lại Flask, sidebar sẽ không tự tìm lại các file của phiên cũ. Có thể kết
hợp với Gemini:

```powershell
python app.py --no-database --api
```

Nếu không có `--api`, bước tóm tắt dùng model Ollama được cấu hình trong
`app.py`.

## Cài đặt trong giao diện

Nút **Cài đặt** ở cuối sidebar cho phép chọn model transcript và chỉnh prompt hệ
thống dùng cho các lần tóm tắt tiếp theo. Zipformer 30M là model transcript mặc
định. Cấu hình được lưu trong trình duyệt; transcript và tên báo cáo luôn được
backend gắn tự động sau phần prompt tùy chỉnh.

## Quy trình duyệt transcript trước khi tóm tắt

MeetNote không còn gọi LLM ngay sau khi phiên âm. Luồng xử lý hiện tại là:

1. Tạo transcript và, nếu được bật, chạy speaker diarization.
2. Mở transcript trong màn hình Markdown editor để người dùng kiểm tra.
3. Hiển thị timeline các lượt nói và cho phép đổi tên một speaker trên toàn bộ
   transcript, ví dụ `người nói 1` thành `Giám đốc An`.
4. Chỉ khi người dùng bấm **Tóm tắt transcript**, nội dung đã chỉnh sửa mới được
   gửi tới Ollama hoặc Gemini.
5. Bản tóm tắt được biên dịch thành DOCX và hiển thị ở màn hình Word viewer riêng,
   không nằm bên dưới transcript và không còn xuất hiện trong Markdown editor.
6. Người dùng có thể tải DOCX, chỉnh sửa bằng Microsoft Word rồi chọn lại file và
   bấm **Lưu file Word**. File mới được lưu trong private filesystem và viewer tải
   lại ngay lập tức. SQL Server chỉ giữ đường dẫn tương đối và metadata của file;
   chế độ `--no-database` giữ đường dẫn trong job hiện tại nhưng vẫn ghi DOCX ra đĩa.

## Demo một ngày trên Google Colab với Ollama

Chọn `Runtime → Change runtime type → T4 GPU`, sau đó chạy lần lượt các cell.
Không cần cài SQL Server, SSMS hay Microsoft ODBC Driver khi dùng
`--no-database`.

Không sửa `app.py` trong Colab. Trong toàn bộ hướng dẫn, chỉ cần thay URL GitHub
ở bước 1. Cờ `--no-database` quyết định app bỏ qua SQL Server; việc có dùng
Gemini hay không được quyết định bằng cờ `--api`.

Thứ tự bắt buộc là:

1. Clone repo và cài dependency.
2. Khởi động Ollama và tải model.
3. **Chạy `app.py` bằng cell khởi động Flask.**
4. Xác nhận Flask trả HTTP 200.
5. Khởi động Cloudflare Tunnel và mở URL được cấp.

### 1. Clone và cài dependency

```python
!git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
%cd YOUR_REPOSITORY
!apt-get update -qq
!apt-get install -y -qq ffmpeg
!pip install -r requirements.txt
```

Nếu dùng tách người nói, cài DiariZen và bản `pyannote-audio` đi kèm source của
DiariZen. Không cài thêm `pyannote.audio>=4` vào môi trường này:

```python
%cd /content
!git clone --recursive https://github.com/BUTSpeechFIT/DiariZen.git
%cd /content/DiariZen
!pip install -r requirements.txt
!pip install -e .
!pip install -e "/content/DiariZen/pyannote-audio[dev,testing]" -c constraints.txt
%cd /content/YOUR_REPOSITORY
```

DiariZen công bố môi trường tham chiếu là Python 3.10, PyTorch 2.1.1 và CUDA
12.1. Nếu runtime hiện tại không tương thích với các phiên bản này, hãy dùng
runtime/container CUDA tương ứng thay vì trộn DiariZen với pyannote 4.

Có thể kiểm tra GPU trước khi chạy app:

```python
import torch

assert torch.cuda.is_available(), "DiariZen cần CUDA GPU"
print(torch.cuda.get_device_name(0))
```

### 2. Cài, khởi động Ollama và tải Qwen 3.5 9B

```python
!curl -fsSL https://ollama.com/install.sh | sh
```

```python
import subprocess
import time

ollama_log = open("/content/ollama.log", "w")
ollama_process = subprocess.Popen(
    ["ollama", "serve"],
    stdout=ollama_log,
    stderr=subprocess.STDOUT,
)
time.sleep(3)
```

```python
!ollama pull qwen3.5:9b
!ollama list
```

`qwen3.5:9b` được app dùng cho bước tóm tắt qua Ollama. Không thêm `--api` nếu
muốn dùng Qwen để tóm tắt.

### 3. Chạy `app.py` để khởi động Flask

Đây chính là bước chạy `app.py`. Chạy cell này **một lần**, sau khi
`ollama pull qwen3.5:9b` hoàn tất và trước khi chạy Cloudflare Tunnel:

```python
import requests

flask_log = open("/content/meetnote.log", "w")
flask_process = subprocess.Popen(
    ["python", "app.py", "--no-database"],
    stdout=flask_log,
    stderr=subprocess.STDOUT,
)
time.sleep(5)
!tail -n 30 /content/meetnote.log

if flask_process.poll() is not None:
    raise RuntimeError("app.py đã dừng. Xem lỗi trong /content/meetnote.log")

response = requests.get("http://127.0.0.1:5001", timeout=10)
print("Flask HTTP status:", response.status_code)
```

Kết quả phải là `Flask HTTP status: 200`. Lệnh trên dùng Ollama/Qwen để tóm
tắt và không đụng đến SQL Server. Nó tương đương với lệnh terminal:

```bash
python app.py --no-database
```

Nếu muốn Gemini tóm tắt thay cho Ollama, chỉ đổi danh sách lệnh trong cell thành:

```python
["python", "app.py", "--no-database", "--api"]
```

Đây là thay đổi cờ chạy, không phải sửa `app.py`. Khi dùng `--api`, cần đặt
`GEMINI_API_KEY` trong Colab Secrets. Khi dùng `--api`, có thể bỏ qua bước cài
và khởi động Ollama vì các engine transcript còn lại không phụ thuộc Ollama.

### 4. Tạo URL demo tạm thời

```python
!wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /usr/local/bin/cloudflared
!chmod +x /usr/local/bin/cloudflared
```

```python
!pkill -f cloudflared || true
```

Cell cuối cùng dùng HTTP/2 để tránh trường hợp kết nối QUIC từ Colab bị treo:

```python
!cloudflared tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:5001
```

Không dừng cell cuối. Sau vài giây, output sẽ có khung chứa dòng dạng:

```text
https://random-words.trycloudflare.com
```

Đó là link giao diện. Cell này phải tiếp tục hiện trạng thái đang chạy để giữ
tunnel hoạt động. Không chia sẻ rộng URL vì app chưa có đăng nhập. Giữ runtime
Colab, `flask_process`, `ollama_process` và cell Tunnel chạy suốt buổi demo.

Mỗi khi Colab bị `Disconnect and delete runtime`, phải chạy lại các cell theo
đúng thứ tự trên vì máy ảo và dữ liệu RAM đã bị xóa. Bạn vẫn không cần sửa bất
kỳ dòng nào trong `app.py`.

Trên T4, cấu hình nhẹ nhất là chọn **Zipformer 30M** và tắt diarization.
Whisper `large-v3`, DiariZen và Qwen cùng giữ model trên GPU có thể vượt VRAM.
Nếu cần diarization, nên dùng Zipformer và `--api` để dành VRAM cho DiariZen;
bản ứng dụng này không có fallback diarization bằng CPU.

## Whisper cho cuộc họp đa ngôn ngữ

Whisper được giữ lại cho các cuộc họp tiếng Anh hoặc có nhiều ngôn ngữ. App
truyền `language=None` để Whisper tự nhận diện thay vì ép tiếng Việt. Model mặc
định hiện là `large-v3`; model này chính xác nhưng chậm và cần nhiều RAM/VRAM
hơn NghiASR hoặc Zipformer.

## Zipformer 30M

Dropdown transcript có thêm `hynt/Zipformer-30M-RNNT-6000h`. Model dùng
`sherpa-onnx` giống NghiASR nên không cần cài thêm dependency. Lần sử dụng đầu
tiên, app tải bộ ONNX `int8` và token table từ Hugging Face; các lần sau dùng
cache trên máy. Có thể đổi cấu hình mà không sửa code:

```powershell
$env:ZIPFORMER_QUANTIZATION="int8" # hoặc fp32
$env:ZIPFORMER_NUM_THREADS="4"
python app.py
```

Model Zipformer này có giấy phép **CC BY-NC-ND 4.0**, vì vậy chỉ nên dùng cho
nghiên cứu/demo phi thương mại nếu chưa có giấy phép khác từ tác giả.

## Tách người nói bằng DiariZen (Whisper, NghiASR và Zipformer)

Giao diện có tùy chọn **Tách người nói** cho Whisper, NghiASR và Zipformer. Khi
bật, app thực hiện pipeline sau:

1. Ghép các file theo đúng thứ tự tải lên và đổi toàn bộ cuộc họp thành WAV
   mono 16 kHz bằng FFmpeg.
2. Chạy `BUT-FIT/diarizen-wavlm-large-s80-md-v2` bằng CUDA trên toàn bộ cuộc họp
   để phân cụm người nói.
3. Bỏ các lượt speaker ngắn hơn ngưỡng trên giao diện (mặc định `2.0` giây)
   khỏi bước căn speaker; audio gốc không bị cắt bỏ.
4. Chạy model ASR đã chọn trên audio liên tục bằng sliding window 30 giây,
   overlap 5 giây. Mỗi cửa sổ chỉ giữ hypothesis thuộc vùng trung tâm của nó để
   không lặp chữ trong phần overlap.
5. Dùng word/token timestamps để gán kết quả ASR về lượt nói của DiariZen và tạo
   transcript dạng `[00:00:05 - 00:00:12] người nói 1: ...`.

Có thể chỉnh hai tham số sliding window mà không sửa code:

```powershell
$env:DIARIZATION_ASR_WINDOW_SECONDS="30"
$env:DIARIZATION_ASR_OVERLAP_SECONDS="5"
python app.py
```

Overlap lớn hơn cung cấp thêm ngữ cảnh tại biên nhưng làm tăng thời gian xử lý.

### Thiết lập lần đầu

DiariZen chưa được cài qua `requirements.txt` vì project yêu cầu bản
`pyannote-audio` nằm trong chính repository của nó. Trên Linux/CUDA, cài theo
source chính thức trước khi chạy app:

```bash
git clone --recursive https://github.com/BUTSpeechFIT/DiariZen.git
cd DiariZen
python -m pip install -r requirements.txt
python -m pip install -e .
python -m pip install -e "./pyannote-audio[dev,testing]" -c constraints.txt
cd /path/to/AI_notetaker-copy
python -m pip install -r requirements.txt
python app.py --no-database --api
```

Model và embedding được tải từ Hugging Face trong lần chạy đầu. Có thể đặt
`HF_TOKEN` nếu môi trường Hugging Face của bạn yêu cầu xác thực hoặc gặp giới
hạn tải xuống, nhưng không cần chấp nhận model pyannote Community-1 nữa.

Ứng dụng kiểm tra CUDA khi DiariZen được bật và báo lỗi ngay nếu PyTorch không
nhận GPU. `DIARIZATION_DEVICE` có thể để trống hoặc đặt thành `cuda:0`; CPU và
Windows không nằm trong phạm vi hỗ trợ của bản thử nghiệm này.

Batch size inference của DiariZen mặc định là `16`. Nếu GPU vẫn hết VRAM, giảm
xuống `8` hoặc `4` trước khi khởi động Flask:

```python
import os

os.environ["DIARIZEN_BATCH_SIZE"] = "8"
```

Nếu biết chính xác số người tham gia, nhập số đó trên giao diện để phân cụm ổn
định hơn. Ngưỡng `2.0` giây bám theo notebook tham chiếu nhưng có thể làm mất
các câu chen ngang ngắn; giảm ngưỡng nếu các câu này quan trọng. Pipeline mới
không bật loudness normalization để giữ hành vi audio hiện có của repo.

Source code DiariZen dùng giấy phép MIT, nhưng trọng số model Large s80 v2 dùng
CC BY-NC 4.0. Chỉ sử dụng model này cho nghiên cứu hoặc demo phi thương mại nếu
chưa có giấy phép khác từ tác giả.

## Tình trạng SQL Server trên máy này

Đã kiểm tra ngày 05/08/2026:

- SQL Server default instance: `MSSQLSERVER` — đang `Running`, tự khởi động.
- Server name dùng trong SSMS và ứng dụng: `.` hoặc `localhost`.
- SQL Server Browser: không cần cho default instance chạy cùng máy.
- ODBC Driver 18 for SQL Server: đã cài.
- `pyodbc 5.3.0`: đã cài vào môi trường `evn_meet`.
- SSMS 22.8.2: `D:\SSMS\Common7\IDE\SSMS.exe`.
- TCP/IP và Named Pipes đang tắt; Shared Memory đang bật. Điều này đủ cho app
  và SQL Server chạy trên cùng máy.

## Thiết lập database lần đầu

### 1. Mở SSMS và kết nối

Mở:

```powershell
& "D:\SSMS\Common7\IDE\SSMS.exe"
```

Trong cửa sổ kết nối chọn:

- **Server type:** Database Engine
- **Server name:** `.`
- **Authentication:** Windows Authentication
- Bật **Trust server certificate** nếu SSMS hiển thị lựa chọn này.

Không cần nhập username/password khi dùng Windows Authentication.

### 2. Tạo database và bảng

Trong SSMS chọn `File → Open → File`, mở file `database.sql` của repo, sau đó
bấm **Execute** hoặc `F5`.

Nếu database đã được tạo bởi phiên bản cũ, vẫn chạy lại file này một lần. Script
chỉ thêm các cột nullable còn thiếu (`WordFilePath`, `WordFileName`,
`WordUpdatedAt`) và không xóa transcript hay lịch sử hiện có. Cột
`WordDocument` cũ (nếu có) được giữ nguyên để tránh xóa dữ liệu ngoài ý muốn,
nhưng app không còn đọc hoặc ghi blob này.

Khi thành công, Object Explorer sẽ có:

```text
Databases
└── MeetNote
    └── Tables
        └── dbo.MeetingHistory
```

Nếu chưa thấy, bấm chuột phải `Databases` và chọn `Refresh`.

### 3. Nhập lịch sử cũ (tùy chọn, chạy một lần)

```powershell
python scripts/migrate_results_to_sqlserver.py
```

Script đọc các file `results/*.txt` cũ. Những ID đã có trong database sẽ được
bỏ qua, không ghi đè nội dung đã sửa.

### 4. Chạy ứng dụng

Với Ollama:

```powershell
python app.py
```

Với Gemini:

```powershell
python app.py --api
```

Ứng dụng kiểm tra kết nối và bảng `dbo.MeetingHistory` trước khi mở cổng 5001.
Nếu database chưa được tạo hoặc đăng nhập thất bại, terminal sẽ báo nguyên nhân.

## Có cần mở SSMS mỗi lần không?

Không. SSMS chỉ là giao diện quản trị. Dịch vụ `MSSQLSERVER` mới là database
server và trên máy này nó đã được đặt `Automatic`.

Kiểm tra nhanh:

```powershell
Get-Service MSSQLSERVER
```

Nếu trạng thái không phải `Running`, mở PowerShell bằng quyền Administrator:

```powershell
Start-Service MSSQLSERVER
```

## Cấu hình kết nối

Mặc định [database.py](database.py) dùng:

```text
Driver: ODBC Driver 18 for SQL Server
Server: .
Database: MeetNote
Authentication: Windows Authentication
Encrypt: no
```

Không cần sửa code với cấu hình hiện tại. Nếu sau này dùng SQL Server
Authentication, đặt biến trong cùng PowerShell trước khi chạy app:

```powershell
$env:SQLSERVER_USERNAME="meetnote_user"
$env:SQLSERVER_PASSWORD="mật_khẩu"
python app.py --api
```

Các biến tùy chọn khác:

```text
SQLSERVER_SERVER
SQLSERVER_DATABASE
SQLSERVER_DRIVER
SQLSERVER_USERNAME
SQLSERVER_PASSWORD
WORD_STORAGE_FOLDER
```

`WORD_STORAGE_FOLDER` mặc định là thư mục private `word_documents/` trong repo.
Database chỉ lưu đường dẫn tương đối bên dưới thư mục này. Khi triển khai nhiều
instance/container, hãy trỏ biến này tới cùng một persistent/shared volume; một
đường dẫn trên ổ đĩa cục bộ của instance khác sẽ không đọc được.

## Bảng MeetingHistory

Bảng lưu UUID, tên báo cáo, thứ tự file audio, engine, transcript, Markdown nguồn
của biên bản, đường dẫn tương đối tới file DOCX, tên file Word, timeline
diarization, trạng thái, số file, tổng dung lượng, thời điểm tạo, hoàn tất, chỉnh
sửa Word cuối và cập nhật cuối. Sidebar hiển thị cả cuộc họp đang chờ duyệt
transcript, đang tóm tắt, tóm tắt lỗi và đã hoàn tất.

Khi sửa transcript, `LastEditedAt` và `UpdatedAt` được cập nhật. Khi lưu một
DOCX mới, file được ghi atomically trên filesystem rồi `WordFilePath`,
`WordFileName`, `WordUpdatedAt`, `LastEditedAt` và `UpdatedAt` được cập nhật trong
database. Giới hạn upload Word là 25 MB.

## Nếu kết nối local vẫn thất bại

Mở `SQLServerManager17.msc` bằng Run as administrator:

1. `SQL Server Network Configuration → Protocols for MSSQLSERVER`.
2. Bật `TCP/IP`.
3. Mở `TCP/IP → IP Addresses → IPAll`, giữ `TCP Port = 1433`.
4. Khởi động lại `SQL Server (MSSQLSERVER)`.
5. Chạy app với:

```powershell
$env:SQLSERVER_SERVER="tcp:localhost,1433"
python app.py --api
```
